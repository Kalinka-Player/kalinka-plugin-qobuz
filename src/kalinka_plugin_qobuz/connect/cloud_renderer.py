"""Playing what the Qobuz app asks for, on Kalinka's renderer.

The cloud names a track and a state; this takes the server's output through
DirectPlayback and plays it, and reports back what is really playing so the
app follows. Controls pressed in Kalinka arrive as transport requests and are
applied here too, so both apps agree. When the server takes the output back,
the app is told playback stopped. The track that follows is lined up on the
renderer shortly before the current one ends, so it plays on without a gap.

Echo handling follows Pibuz: the cloud repeats every state report as a
SET_STATE naming no change, and right after a handoff replays a pause or stop
at the start of the track, neither of which may be acted on.

Protocol details adapted from Pibuz (https://github.com/PhilipVinc/pibuz,
crates/qconnect-app/src/renderer.rs and crates/pibuz/src/qconnect/report.rs),
MIT License: Copyright (c) 2024 blitzkriegfc, Copyright (c) 2026 Filippo Vicentini.
"""

import asyncio
import logging
import time
from typing import Callable, Optional, Protocol

from kalinka_plugin_sdk.datamodel import DeviceVolume, PlaybackState, PlayerStateEnum, Track
from kalinka_plugin_sdk.direct_playback import (
    DirectPlayback,
    DirectPlaybackSession,
    HoldEnded,
    OutputUnavailable,
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from kalinka_plugin_sdk.inputmodule import TrackSource

from . import frames
from .cloud_queue import CloudQueue, QueueItem, item_of
from .proto import qconnect_pb2 as qc

logger = logging.getLogger(__name__.split(".")[-1])

HOLD_TITLE = "Qobuz Connect"

PLAYING_STOPPED = 1
PLAYING_PLAYING = 2
PLAYING_PAUSED = 3
BUFFER_BUFFERING = 1
BUFFER_OK = 2
LOOP_ONE = 2
LOOP_ALL = 3

REPORT_INTERVAL_S = 2.0
# A pause or stop this soon after our own load, at the track's start, is the
# cloud replaying the handoff.
LOAD_ECHO_S = 1.5
LOAD_ECHO_POSITION_MS = 1000
# Joining a live session replays SetActive(false) right after the load.
DEACTIVATE_GRACE_S = 5.0
SEEK_TOLERANCE_MS = 2000
RESTART_PREV_AFTER_MS = 3000
# The next track is lined up on the renderer this long before the current one
# ends, and no sooner: Qobuz stream addresses expire.
LINE_UP_BEFORE_END_MS = 10_000
# The output echoes a volume we set; an echo this late is still ours, and
# passing it on would pull the app's slider back while it is being dragged.
VOLUME_ECHO_S = 1.0
# How long the Qobuz app may show a volume that is not the output's before it
# is told again: time for a volume command of its own to arrive first.
SHOWN_VOLUME_RECHECK_S = 1.0

# Connect's quality levels and the Qobuz format ids they stand for.
LEVEL_MP3, LEVEL_CD, LEVEL_HIRES, LEVEL_HIRES_ABOVE_96K = 1, 2, 3, 4
FORMAT_FOR_LEVEL = {LEVEL_MP3: 5, LEVEL_CD: 6, LEVEL_HIRES: 7, LEVEL_HIRES_ABOVE_96K: 27}
LEVEL_FOR_FORMAT = {format_id: level for level, format_id in FORMAT_FOR_LEVEL.items()}


class Reporter(Protocol):
    async def send_state(self, state: qc.RendererStateMessage) -> bool: ...

    async def send_volume(self, percent: int) -> bool: ...

    async def send_muted(self, muted: bool) -> bool: ...

    async def send_max_quality(self, level: int) -> bool: ...

    async def send_file_quality(self, quality: qc.RendererFileAudioQualityChangedMessage) -> bool: ...

    async def send_device_quality(
        self, quality: qc.RendererDeviceAudioQualityChangedMessage
    ) -> bool: ...


class TrackResolver(Protocol):
    async def resolve(self, track_id: int, format_id: int) -> tuple[TrackSource, Track]:
        """The stream and the metadata for a Qobuz track."""
        ...


class _HoldListener:
    """One hold's DirectPlaybackListener: tags what it hears with that hold.

    A hold that has been replaced can still be heard from; the tag lets the
    renderer tell its late news from the current hold's.
    """

    def __init__(self, renderer: "ConnectRenderer", generation: int):
        self._renderer = renderer
        self._generation = generation

    def on_state(self, state: PlaybackState) -> None:
        self._renderer._from_hold(self._generation, self._renderer._output_state, state)

    def on_finished(self) -> None:
        self._renderer._from_hold(self._generation, self._renderer._finished)

    def on_next_started(self, track: Track) -> None:
        self._renderer._from_hold(self._generation, self._renderer._next_started, track)

    def on_command(self, request: TransportRequest) -> None:
        self._renderer._from_hold(self._generation, self._renderer._command, request)

    def on_revoked(self, reason: RevokeReason) -> None:
        self._renderer._from_hold(self._generation, self._renderer._revoked, reason)

    def on_volume(self, volume: DeviceVolume) -> None:
        self._renderer._from_hold(self._generation, self._renderer._output_volume, volume)


class ConnectRenderer:
    """The Playback the session hands renderer traffic to.

    Every entry point only queues its work: one lane runs it all in arrival
    order, so a track load waits for nothing it races with, and no callback
    the server makes runs into its per-call budget while the network is slow.

    @param format_id The plugin's configured quality; Connect never exceeds it.
    """

    def __init__(
        self,
        *,
        reporter: Reporter,
        queue: CloudQueue,
        direct: DirectPlayback,
        tracks: TrackResolver,
        format_id: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._reporter = reporter
        self._queue = queue
        self._direct = direct
        self._tracks = tracks
        self._cap_level = LEVEL_FOR_FORMAT.get(format_id, LEVEL_HIRES_ABOVE_96K)
        # The quality the app chose, within the plugin's; and the one the
        # current track was fetched at.
        self._level = self._cap_level
        self._loaded_level = self._cap_level
        self._clock = clock
        self._active = False
        self._hold: Optional[DirectPlaybackSession] = None
        self._current: Optional[QueueItem] = None
        self._next: Optional[QueueItem] = None
        self._track: Optional[Track] = None
        self._queue_version: Optional[tuple[int, int]] = None
        self._state: Optional[PlaybackState] = None
        self._intent = PLAYING_STOPPED
        self._loaded_at = float("-inf")
        self._loop_mode = 0
        self._volume: Optional[int] = None
        # Volumes we set, and when: the output's echo of one is not news.
        self._volumes_set: dict[int, float] = {}
        # What the Qobuz app shows for this player, as the cloud tells controllers.
        self._shown_volume: Optional[int] = None
        self._shown_recheck: Optional[asyncio.TimerHandle] = None
        # The stream and device formats last reported, so each goes once.
        self._file_quality: Optional[qc.RendererFileAudioQualityChangedMessage] = None
        self._device_quality: Optional[qc.RendererDeviceAudioQualityChangedMessage] = None
        self._muted_from: Optional[int] = None
        # The track whose stream was fetched again after failing; once each.
        self._refetched: Optional[QueueItem] = None
        # The track lined up on the renderer to follow, with the level it was
        # fetched at; the one whose stream is being fetched; and the timer that
        # starts that fetch.
        self._lined_up: Optional[tuple[QueueItem, int]] = None
        # Tracks taken back off the renderer since it last played anything:
        # one it may still have moved on to before it heard.
        self._taken_back: set[int] = set()
        self._fetching: Optional[tuple[QueueItem, int]] = None
        self._next_fetch: Optional[asyncio.Task] = None
        self._line_up_timer: Optional[asyncio.TimerHandle] = None
        # Where playback stands while the output reports nothing: the offset a
        # load started at, or where it was when the output went.
        self._settled_position = 0
        self._ticker: Optional[asyncio.Task] = None
        self._jobs: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._generation = 0
        self._closed = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def now_playing(self) -> Optional[Track]:
        return self._track if self._hold is not None else None

    async def shutdown(self) -> None:
        """Tell the app playback stopped, give the output back, stop the lane."""
        self._submit(self._shut_down)
        await self.settled()
        self._closed = True
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def settled(self) -> None:
        """Wait until everything queued so far has run."""
        await self._jobs.join()

    # ------------------------------------------------------------------
    # Playback: what the cloud asks for

    async def on_joined(self) -> None:
        self._submit(self._joined)

    async def on_set_active(self, active: bool) -> None:
        self._submit(self._set_active, active)

    async def on_active_renderer(self, ours: bool) -> None:
        self._submit(self._active_renderer, ours)

    async def on_set_state(self, message: qc.RendererSetStateMessage) -> None:
        self._submit(self._set_state, message)

    async def on_set_volume(self, message: qc.RendererSetVolumeMessage) -> None:
        self._submit(self._set_volume_from, message)

    async def on_set_max_quality(self, level: int) -> None:
        self._submit(self._set_max_quality, level)

    async def on_mute(self, muted: bool) -> None:
        self._submit(self._mute, muted)

    async def on_loop_mode(self, mode: int) -> None:
        self._submit(self._set_loop_mode, mode)

    async def on_queue_changed(self) -> None:
        self._submit(self._line_up_next)

    async def on_shown_volume(self, percent: int) -> None:
        self._submit(self._shown_volume_changed, percent)

    # ------------------------------------------------------------------
    # The lane

    def _from_hold(self, generation: int, job, *args) -> None:
        """Queue news from a hold; it is dropped if that hold has been replaced."""
        self._submit(self._if_current, generation, job, *args)

    async def _if_current(self, generation: int, job, *args) -> None:
        if generation == self._generation:
            await job(*args)

    def _submit(self, job, *args) -> None:
        if self._closed:
            # News after shutdown: no lane runs it, and none may be started.
            return
        self._jobs.put_nowait((job, args))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="qobuz-connect-renderer")

    async def _work(self) -> None:
        while True:
            job, args = await self._jobs.get()
            try:
                await job(*args)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Qobuz Connect: %s failed", job.__name__)
            finally:
                self._jobs.task_done()

    async def _shut_down(self) -> None:
        self._stop_ticker()
        if self._shown_recheck is not None:
            self._shown_recheck.cancel()
        if self._active:
            self._intent = PLAYING_STOPPED
            await self._report()
        await self._release()

    async def _joined(self) -> None:
        await self._report()
        volume = self._volume_to_show()
        if volume is not None:
            await self._reporter.send_volume(volume)
        await self._reporter.send_max_quality(self._level)
        self._file_quality = self._device_quality = None
        await self._report_quality()

    async def _set_active(self, active: bool) -> None:
        if active:
            if not self._active:
                self._active = True
                logger.info("Qobuz Connect: this player is the active renderer")
                # The cloud drops a volume report from a renderer it does not
                # yet take for the active one: the join's may have gone.
                volume = self._volume_to_show()
                if volume is not None:
                    await self._reporter.send_volume(volume)
                self._file_quality = self._device_quality = None
            await self._report()
            await self._report_quality()
        elif self._clock() - self._loaded_at >= DEACTIVATE_GRACE_S:
            await self._deactivate()

    async def _active_renderer(self, ours: bool) -> None:
        if ours:
            await self._set_active(True)
        else:
            await self._deactivate()

    async def _set_state(self, message: qc.RendererSetStateMessage) -> None:
        await self._apply_state(message)
        await self._line_up_next()

    async def _apply_state(self, message: qc.RendererSetStateMessage) -> None:
        if message.HasField("queue_version"):
            self._queue_version = (message.queue_version.major, message.queue_version.minor)
        if message.HasField("next_track"):
            self._next = item_of(message.next_track)
        has_state = message.HasField("playing_state")
        has_position = message.HasField("current_position")
        has_track = message.HasField("current_track")
        if not (has_state or has_position or has_track):
            return
        if logger.isEnabledFor(logging.INFO):
            logger.info("Qobuz Connect asks: %s", _describe(message))
        state = message.playing_state if has_state else None
        # The app can name a start just before the track's, such as -319 ms;
        # the output takes offsets from the track's start only.
        position = max(0, message.current_position) if has_position else None
        if has_track:
            item = item_of(message.current_track)
            if item is not None and item != self._current:
                await self._load(item, position or 0, state or PLAYING_PLAYING)
                return
        if state == PLAYING_PLAYING:
            await self._resume(position)
        elif state in (PLAYING_PAUSED, PLAYING_STOPPED):
            if self._handoff_echo(position):
                logger.info("Qobuz Connect: ignored as the replay of the handoff")
                return
            if state == PLAYING_PAUSED:
                await self._pause()
            else:
                await self._stop()
        elif position is not None:
            await self._seek(position)

    async def _set_volume_from(self, message: qc.RendererSetVolumeMessage) -> None:
        current = self._volume if self._volume is not None else await self._read_volume()
        if message.HasField("volume"):
            target = message.volume
        elif message.HasField("volume_delta") and current is not None:
            target = current + message.volume_delta
        else:
            return
        await self._set_volume(max(0, min(100, target)))

    async def _set_max_quality(self, level: int) -> None:
        self._level = max(LEVEL_MP3, min(level, self._cap_level))
        await self._reporter.send_max_quality(self._level)
        hold = self._hold
        if self._current is None or hold is None or not hold.active:
            return
        if self._intent == PLAYING_STOPPED:
            return
        playing = self._stream_level() or self._loaded_level
        # Down to below what plays, or up from a level the track was held to.
        if self._level < playing or (
            self._level > self._loaded_level and playing >= self._loaded_level
        ):
            logger.info("Qobuz Connect: switching to quality level %d", self._level)
            state = PLAYING_PAUSED if self._intent == PLAYING_PAUSED else PLAYING_PLAYING
            await self._load(self._current, self._position(), state)
        else:
            # The track lined up to follow may have been fetched at another level.
            await self._line_up_next()

    async def _set_loop_mode(self, mode: int) -> None:
        self._loop_mode = mode
        await self._line_up_next()

    async def _mute(self, muted: bool) -> None:
        if muted and self._muted_from is None:
            # Unmuting restores this, so it must be the output's, not a guess.
            current = self._volume if self._volume is not None else await self._read_volume()
            self._muted_from = current if current is not None else 100
            await self._set_volume(0, report=False)
        elif not muted and self._muted_from is not None:
            restore, self._muted_from = self._muted_from, None
            await self._set_volume(restore, report=False)
        await self._reporter.send_muted(muted)

    async def _output_state(self, state: PlaybackState) -> None:
        self._state = state
        if state.state is PlayerStateEnum.ERROR:
            current = self._current
            # Stream addresses expire, and one carried to another renderer may
            # have: a fresh one, from where playback had reached.
            if current is not None and self._refetched != current:
                self._refetched = current
                logger.warning("Qobuz Connect: the stream failed; fetching it again")
                await self._load(current, state.position or 0, PLAYING_PLAYING)
                return
            logger.warning("Qobuz Connect: the renderer could not play the track")
            self._intent = PLAYING_STOPPED
            await self._release()
        elif state.state is PlayerStateEnum.PAUSED:
            self._intent = PLAYING_PAUSED
        elif state.state in (PlayerStateEnum.PLAYING, PlayerStateEnum.BUFFERING):
            self._intent = PLAYING_PLAYING
        await self._report()
        await self._report_quality()
        await self._line_up_next()

    async def _output_volume(self, volume: DeviceVolume) -> None:
        """The output's volume, from Kalinka, the host, or our own echo."""
        percent = _percent(volume)
        now = self._clock()
        self._volumes_set = {
            level: at for level, at in self._volumes_set.items() if now - at < VOLUME_ECHO_S
        }
        if percent is None or percent == self._volume:
            return
        # A device with fewer than 100 steps echoes the nearest one it has,
        # which reads back as a percentage a little off the one we set.
        tolerance = 50 / volume.max_volume + 0.5 if 0 < volume.max_volume < 100 else 0
        if any(abs(percent - level) <= tolerance for level in self._volumes_set):
            return
        self._volume = percent
        await self._reporter.send_volume(percent)
        if self._muted_from is not None and percent > 0:
            self._muted_from = None
            await self._reporter.send_muted(False)

    async def _shown_volume_changed(self, percent: int) -> None:
        """The Qobuz app shows ``percent``: our report, or a level of its own.

        On a takeover the app broadcasts the level it remembers for this
        player, which is no command, and a report the cloud dropped leaves the
        app's old level up. A level other than the output's that is still
        shown a moment later is corrected by reporting the output's again.
        """
        self._shown_volume = percent
        if self._shown_recheck is None and self._shows_other_volume():
            self._shown_recheck = asyncio.get_running_loop().call_later(
                SHOWN_VOLUME_RECHECK_S, self._submit, self._recheck_shown_volume
            )

    async def _recheck_shown_volume(self) -> None:
        self._shown_recheck = None
        if not self._shows_other_volume():
            return
        volume = self._volume_to_show()
        logger.info(
            "Qobuz Connect shows volume %d%% for this player; reporting the output's %d%%",
            self._shown_volume,
            volume,
        )
        await self._reporter.send_volume(volume)

    def _shows_other_volume(self) -> bool:
        shown, volume = self._shown_volume, self._volume_to_show()
        return self._active and None not in (shown, volume) and shown != volume

    def _volume_to_show(self) -> Optional[int]:
        """The volume the app should show: under its mute, the one it restores."""
        return self._muted_from if self._muted_from is not None else self._volume

    async def _finished(self) -> None:
        following = self._upcoming()
        if following is None:
            self._intent = PLAYING_STOPPED
            await self._report()
            await self._release()
            return
        await self._load(following, 0, PLAYING_PLAYING)

    async def _next_started(self, track: Track) -> None:
        """The renderer moved on to the track lined up: it plays already."""
        lined_up = self._lined_up
        if lined_up is None or track.id.id != str(lined_up[0].track_id):
            if track.id.id not in {str(taken) for taken in self._taken_back}:
                # Late news from before something else was played: that plays now.
                logger.info("Qobuz Connect: ignored the move on to a track since replaced")
                return
            # One taken back as the renderer moved on to it: play what follows now.
            logger.info("Qobuz Connect: the renderer moved on to a track no longer next")
            self._lined_up = None
            await self._finished()
            return
        self._lined_up = None
        self._taken_back.clear()
        item, level = lined_up
        logger.info("Qobuz Connect: track %s follows without a gap", item.track_id)
        self._current = item
        self._track = track
        self._loaded_level = level
        self._intent = PLAYING_PLAYING
        self._state = None
        self._settled_position = 0
        await self._report()
        await self._line_up_next()

    async def _command(self, request: TransportRequest) -> None:
        if request.kind is TransportKind.PAUSE:
            await self._pause()
        elif request.kind is TransportKind.RESUME:
            await self._resume(None)
        elif request.kind is TransportKind.SEEK and request.position_ms is not None:
            await self._seek(request.position_ms)
        elif request.kind is TransportKind.NEXT:
            following = self._following()
            if following is not None:
                await self._load(following, 0, PLAYING_PLAYING)
        elif request.kind is TransportKind.PREV:
            earlier = self._queue.before(self._current)
            if earlier is None or self._position() > RESTART_PREV_AFTER_MS:
                await self._seek(0)
            else:
                await self._load(earlier, 0, PLAYING_PLAYING)
        await self._line_up_next()

    async def _revoked(self, reason: RevokeReason) -> None:
        self._hold = None
        self._drop_next()
        self._settle_position()
        self._stop_ticker()
        self._intent = PLAYING_STOPPED
        logger.info("Qobuz Connect playback stopped: Kalinka took the output (%s)", reason.value)
        await self._report()

    # ------------------------------------------------------------------

    async def _load(self, item: QueueItem, position_ms: int, state: int) -> None:
        self._current = item
        self._loaded_at = self._clock()
        # Playing anything takes the renderer's successor away.
        self._drop_next()
        if state == PLAYING_STOPPED:
            self._intent = PLAYING_STOPPED
            await self._release()
            await self._report()
            return
        level = self._level
        format_id = FORMAT_FOR_LEVEL[level]
        try:
            source, track = await self._tracks.resolve(item.track_id, format_id)
            # A position from the app can be stale; past the end it would ask
            # the renderer for a start that is not in the stream.
            if track.duration and position_ms >= track.duration * 1000:
                position_ms = 0
            hold = await self._ensure_hold()
            await hold.play(source, track, start_offset_ms=position_ms)
            if state == PLAYING_PAUSED:
                await hold.pause()
        except (OutputUnavailable, HoldEnded) as exc:
            logger.warning("Qobuz Connect cannot play here: %s", exc)
            await self._stop()
            return
        except Exception as exc:
            logger.warning(
                "Qobuz Connect could not load track %s (%s)", item.track_id, type(exc).__name__
            )
            # Whatever played before would carry on under a report of stopped.
            await self._stop()
            return
        self._track = track
        self._loaded_level = level
        self._intent = state
        self._state = None
        self._settled_position = position_ms
        await self._report(buffering=state == PLAYING_PLAYING)
        self._start_ticker()
        await self._line_up_next()

    async def _resume(self, position: Optional[int]) -> None:
        hold = self._hold
        if hold is None or not hold.active:
            if self._current is not None:
                at = position if position is not None else self._position()
                await self._load(self._current, at, PLAYING_PLAYING)
            else:
                logger.warning("Qobuz Connect asks to play, but names no track and none is loaded")
            return
        await hold.resume()
        if position is not None:
            await self._seek(position)
        self._intent = PLAYING_PLAYING
        await self._report()

    async def _pause(self) -> None:
        if self._hold is not None and self._hold.active:
            await self._hold.pause()
        self._intent = PLAYING_PAUSED
        await self._report()

    async def _stop(self) -> None:
        self._intent = PLAYING_STOPPED
        await self._release()
        await self._report()

    async def _seek(self, position_ms: int) -> None:
        hold = self._hold
        if hold is None or not hold.active:
            return
        # A seek to the start restarts the track, except before the output has
        # reported since the load: the stream is still starting there.
        near = abs(position_ms - self._position()) <= SEEK_TOLERANCE_MS
        if near and (position_ms != 0 or self._state is None):
            return
        await hold.seek(position_ms)

    async def _deactivate(self) -> None:
        # Chosen again, it reports stopped until the cloud names what to play.
        self._intent = PLAYING_STOPPED
        if not self._active and self._hold is None:
            return
        self._active = False
        logger.info("Qobuz Connect: this player is no longer the active renderer")
        await self._release()

    async def _ensure_hold(self) -> DirectPlaybackSession:
        if self._hold is None or not self._hold.active:
            self._generation += 1
            self._hold = await self._direct.acquire(
                HOLD_TITLE, _HoldListener(self, self._generation)
            )
        return self._hold

    async def _release(self) -> None:
        hold, self._hold = self._hold, None
        self._drop_next()
        if hold is not None:
            # What it said before letting go is late news now.
            self._generation += 1
            self._settle_position()
        self._stop_ticker()
        if hold is not None:
            await hold.release()

    async def _set_volume(self, percent: int, *, report: bool = True) -> None:
        hold = self._hold
        if hold is not None and hold.active:
            self._volumes_set[percent] = self._clock()
            try:
                await hold.set_volume(percent)
            except (OutputUnavailable, HoldEnded) as exc:
                logger.info("Qobuz Connect volume not applied: %s", exc)
        self._volume = percent
        if report:
            await self._reporter.send_volume(percent)

    async def _read_volume(self) -> Optional[int]:
        hold = self._hold
        if hold is None or not hold.active:
            return self._volume
        return _percent(await hold.get_volume())

    def _upcoming(self) -> Optional[QueueItem]:
        """What plays when the current track ends, as the loop mode has it."""
        if self._loop_mode == LOOP_ONE and self._current is not None:
            return self._current
        following = self._following()
        if following is None and self._loop_mode == LOOP_ALL:
            return self._queue.first()
        return following

    def _following(self) -> Optional[QueueItem]:
        # The cloud's own idea of what follows wins over the queue we mirror.
        if self._next is not None and self._next != self._current:
            return self._next
        return self._queue.after(self._current)

    # ------------------------------------------------------------------
    # The track that follows, lined up on the renderer

    def _next_target(self) -> Optional[tuple[QueueItem, int]]:
        """The track to line up after the current one, and the level to fetch it at."""
        hold = self._hold
        if hold is None or not hold.active or self._current is None:
            return None
        following = self._upcoming()
        return (following, self._level) if following is not None else None

    async def _line_up_next(self) -> None:
        """Keep the track that follows lined up on the renderer, so it plays on without a gap.

        Its stream is fetched only shortly before the current track ends, as
        the server's own queue does it. Anything that moves that moment or
        changes what follows calls this again; a track that ends with nothing
        lined up still moves on, through on_finished, after a gap.
        """
        target = self._next_target()
        if self._fetching is not None and self._fetching != target:
            self._cancel_next_fetch()
        if self._lined_up is not None and self._lined_up != target:
            # What follows changed after it was lined up.
            self._taken_back.add(self._lined_up[0].track_id)
            self._lined_up = None
            hold = self._hold
            if hold is not None and hold.active:
                try:
                    await hold.set_next(None)
                except HoldEnded:
                    pass
        self._cancel_line_up_timer()
        if target is None or target in (self._lined_up, self._fetching):
            return
        duration = self._duration()
        if self._intent != PLAYING_PLAYING or not duration:
            return
        delay_ms = max(0, duration - self._position() - LINE_UP_BEFORE_END_MS)
        self._line_up_timer = asyncio.get_running_loop().call_later(
            delay_ms / 1000, self._submit, self._fetch_next, target
        )

    async def _fetch_next(self, target: tuple[QueueItem, int]) -> None:
        self._line_up_timer = None
        if self._fetching is not None or target == self._lined_up:
            return
        if target != self._next_target():
            return
        self._fetching = target
        self._next_fetch = asyncio.create_task(
            self._resolve_next(target), name="qobuz-connect-next"
        )

    async def _resolve_next(self, target: tuple[QueueItem, int]) -> None:
        """Off the lane: a slow network holds up no control meanwhile."""
        item, level = target
        try:
            source, track = await self._tracks.resolve(item.track_id, FORMAT_FOR_LEVEL[level])
        except Exception as exc:
            logger.warning(
                "Qobuz Connect could not fetch the next track %s (%s)",
                item.track_id,
                type(exc).__name__,
            )
            source = track = None
        self._submit(self._next_fetched, target, source, track)

    async def _next_fetched(
        self,
        target: tuple[QueueItem, int],
        source: Optional[TrackSource],
        track: Optional[Track],
    ) -> None:
        if target != self._fetching:
            return  # given up on while it was fetched
        self._fetching = self._next_fetch = None
        hold = self._hold
        if source is None or hold is None or not hold.active or target != self._next_target():
            return
        try:
            await hold.set_next(source, track)
        except (HoldEnded, ValueError) as exc:
            logger.info("Qobuz Connect: the next track was not lined up: %s", exc)
            return
        self._lined_up = target
        logger.info("Qobuz Connect: track %s lined up to follow", target[0].track_id)

    def _drop_next(self) -> None:
        """Forget the track that follows; the server drops it with what played."""
        self._lined_up = None
        self._taken_back.clear()
        self._cancel_next_fetch()
        self._cancel_line_up_timer()

    def _cancel_next_fetch(self) -> None:
        fetch, self._next_fetch = self._next_fetch, None
        self._fetching = None
        if fetch is not None:
            fetch.cancel()

    def _cancel_line_up_timer(self) -> None:
        timer, self._line_up_timer = self._line_up_timer, None
        if timer is not None:
            timer.cancel()

    # ------------------------------------------------------------------

    def _handoff_echo(self, position: Optional[int]) -> bool:
        return (
            self._clock() - self._loaded_at < LOAD_ECHO_S
            and (position or 0) <= LOAD_ECHO_POSITION_MS
        )

    def _position(self) -> int:
        state = self._state
        if state is None or state.position is None:
            return self._settled_position
        if state.state is not PlayerStateEnum.PLAYING:
            return state.position
        elapsed_ms = (time.monotonic_ns() - state.timestamp_ns) // 1_000_000
        position = state.position + max(0, elapsed_ms)
        duration = self._duration()
        return min(position, duration) if duration else position

    def _settle_position(self) -> None:
        """The output stopped reporting: hold the position where it reached."""
        self._settled_position = self._position()
        self._state = None

    def _duration(self) -> Optional[int]:
        state = self._state
        if state is not None and state.audio_info is not None and state.audio_info.duration_ms:
            return state.audio_info.duration_ms
        if self._track is not None and self._track.duration:
            return self._track.duration * 1000
        return None

    async def _report(self, *, buffering: bool = False) -> None:
        if not self._active:
            return
        state = self._state
        if state is not None and state.state is PlayerStateEnum.BUFFERING:
            buffering = True
        version = self._queue_version or self._queue.version
        message = qc.RendererStateMessage(
            playing_state=self._intent,
            buffer_state=BUFFER_BUFFERING if buffering else BUFFER_OK,
            current_position=qc.PlaybackPositionMessage(
                timestamp=frames.now_ms(), value=self._position()
            ),
            queue_version=qc.QueueVersionRef(major=version[0], minor=version[1]),
        )
        duration = self._duration()
        if duration:
            message.duration = duration
        current = self._queue.reported_id(self._current)
        if current:
            message.current_queue_item_id = current
        following = self._following()
        if following is not None and following.queue_item_id > 0:
            message.next_queue_item_id = following.queue_item_id
        await self._reporter.send_state(message)

    async def _report_quality(self) -> None:
        """Tell the app the format that plays and the one the device runs at."""
        state = self._state
        info = state.audio_info if state is not None else None
        if not self._active or info is None or not info.sample_rate or not info.bits_per_sample:
            return
        channels = info.channels or 2
        file_quality = qc.RendererFileAudioQualityChangedMessage(
            sampling_rate=info.sample_rate,
            bit_depth=info.bits_per_sample,
            nb_channels=channels,
            audio_quality=_quality_level(info.sample_rate, info.bits_per_sample, state.mime_type),
        )
        if file_quality != self._file_quality and await self._reporter.send_file_quality(
            file_quality
        ):
            self._file_quality = file_quality
        output = info.output
        if output is None or not output.sample_rate:
            return
        device_quality = qc.RendererDeviceAudioQualityChangedMessage(
            sampling_rate=output.sample_rate,
            bit_depth=output.bits_per_sample or info.bits_per_sample,
            nb_channels=output.channels or channels,
        )
        if device_quality != self._device_quality and await self._reporter.send_device_quality(
            device_quality
        ):
            self._device_quality = device_quality

    def _stream_level(self) -> Optional[int]:
        """The level of the stream that plays, once the output has said."""
        state = self._state
        info = state.audio_info if state is not None else None
        if info is None or not info.sample_rate or not info.bits_per_sample:
            return None
        return _quality_level(info.sample_rate, info.bits_per_sample, state.mime_type)

    def _start_ticker(self) -> None:
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick(), name="qobuz-connect-report")

    def _stop_ticker(self) -> None:
        ticker, self._ticker = self._ticker, None
        if ticker is not None and ticker is not asyncio.current_task():
            ticker.cancel()

    async def _tick(self) -> None:
        while self._hold is not None:
            await asyncio.sleep(REPORT_INTERVAL_S)
            self._submit(self._report_progress)

    async def _report_progress(self) -> None:
        if self._hold is not None and self._intent == PLAYING_PLAYING:
            await self._report()


_PLAYING_NAMES = {PLAYING_STOPPED: "stopped", PLAYING_PLAYING: "playing", PLAYING_PAUSED: "paused"}


def _describe(message: qc.RendererSetStateMessage) -> str:
    """What a SET_STATE changes, for the log."""
    parts = []
    if message.HasField("playing_state"):
        state = message.playing_state
        parts.append(_PLAYING_NAMES.get(state, f"state {state}"))
    if message.HasField("current_position"):
        parts.append(f"at {message.current_position} ms")
    if message.HasField("current_track"):
        item = item_of(message.current_track)
        parts.append(
            f"track {item.track_id} (item {item.queue_item_id})" if item else "no track"
        )
    return ", ".join(parts)


def _percent(volume: Optional[DeviceVolume]) -> Optional[int]:
    """A device's volume as a percentage of its range; None if it has none."""
    if volume is None or not volume.supported:
        return None
    if volume.max_volume > 0:
        return round(volume.current_volume * 100 / volume.max_volume)
    return volume.current_volume


def _quality_level(sample_rate: int, bit_depth: int, mime_type: Optional[str]) -> int:
    """Where a stream sits on Connect's quality scale.

    MP3 decodes to what CD does, so only its type tells it apart. Above 96 kHz
    is Qobuz's top format, not Pibuz's 192 kHz line, so 176.4 kHz ranks there.
    """
    if mime_type == "audio/mpeg":
        return LEVEL_MP3
    if sample_rate > 96_000:
        return LEVEL_HIRES_ABOVE_96K
    if bit_depth > 16 or sample_rate > 48_000:
        return LEVEL_HIRES
    return LEVEL_CD
