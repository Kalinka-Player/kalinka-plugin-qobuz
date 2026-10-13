"""Playing what the Qobuz app asks for, and telling it what really plays."""

import asyncio
import logging
import time

import pytest
import pytest_asyncio
from kalinka_plugin_sdk.datamodel import (
    Album,
    AudioInfo,
    DeviceVolume,
    EntityId,
    EntityType,
    OutputInfo,
    PlaybackState,
    PlayerStateEnum,
)
from kalinka_plugin_sdk.direct_playback import (
    OutputUnavailable,
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from kalinka_plugin_sdk.inputmodule import DirectUrl, Track, TrackSource

from kalinka_plugin_qobuz.connect.cloud_queue import CloudQueue, QueueItem
from kalinka_plugin_qobuz.connect import cloud_renderer
from kalinka_plugin_qobuz.connect.cloud_renderer import VOLUME_ECHO_S, ConnectRenderer, _quality_level
from kalinka_plugin_qobuz.connect.proto import qconnect_pb2 as qc

pytestmark = pytest.mark.asyncio

STOPPED, PLAYING, PAUSED = 1, 2, 3
BUFFERING, OK = 1, 2


class FakeHold:
    def __init__(self, title, listener):
        self.title = title
        self.listener = listener
        self.active = True
        self.renderer_id = "rid-a"
        self.state = PlaybackState()
        self.calls = []
        self.volume = DeviceVolume(max_volume=100, current_volume=40)

    async def play(self, source, track, *, start_offset_ms=0):
        self.calls.append(("play", source.source.url, start_offset_ms))

    async def set_next(self, source, track=None):
        self.calls.append(("next", source.source.url if source is not None else None))

    async def pause(self):
        self.calls.append(("pause",))

    async def resume(self):
        self.calls.append(("resume",))

    async def seek(self, position_ms):
        self.calls.append(("seek", position_ms))

    async def set_volume(self, percent):
        self.volume = DeviceVolume(max_volume=100, current_volume=percent)
        self.calls.append(("volume", percent))
        self.listener.on_volume(self.volume)

    async def get_volume(self):
        return self.volume

    async def release(self):
        self.active = False
        self.calls.append(("release",))

    def show(
        self,
        state,
        position=0,
        duration_ms=240000,
        *,
        sample_rate=44100,
        bits=16,
        mime_type="audio/flac",
        output=None,
    ):
        self.listener.on_state(
            PlaybackState(
                state=state,
                position=position,
                timestamp_ns=time.monotonic_ns(),
                mime_type=mime_type,
                audio_info=AudioInfo(
                    sample_rate=sample_rate,
                    bits_per_sample=bits,
                    channels=2,
                    duration_ms=duration_ms,
                    output=output,
                ),
            )
        )


class FakeDirect:
    def __init__(self):
        self.holds: list[FakeHold] = []
        self.unavailable = ""

    async def acquire(self, title, listener):
        if self.unavailable:
            raise OutputUnavailable(self.unavailable)
        hold = FakeHold(title, listener)
        self.holds.append(hold)
        listener.on_volume(hold.volume)
        return hold

    @property
    def hold(self) -> FakeHold:
        return self.holds[-1]


class FakeReporter:
    def __init__(self):
        self.states: list[qc.RendererStateMessage] = []
        self.volumes: list[int] = []
        self.muted: list[bool] = []
        self.qualities: list[int] = []
        self.file_qualities: list[tuple] = []
        self.device_qualities: list[tuple] = []

    async def send_state(self, state):
        self.states.append(state)
        return True

    async def send_volume(self, percent):
        self.volumes.append(percent)
        return True

    async def send_muted(self, muted):
        self.muted.append(muted)
        return True

    async def send_max_quality(self, level):
        self.qualities.append(level)
        return True

    async def send_file_quality(self, quality):
        self.file_qualities.append(
            (quality.sampling_rate, quality.bit_depth, quality.nb_channels, quality.audio_quality)
        )
        return True

    async def send_device_quality(self, quality):
        self.device_qualities.append((quality.sampling_rate, quality.bit_depth, quality.nb_channels))
        return True


class FakeTracks:
    def __init__(self):
        self.resolved = []
        self.error = None

    async def resolve(self, track_id, format_id):
        self.resolved.append((track_id, format_id))
        if self.error is not None:
            raise self.error
        return (
            TrackSource(
                source=DirectUrl(url=f"https://streaming.qobuz.test/{track_id}"), format="audio/flac"
            ),
            track_of(track_id),
        )


def track_of(track_id):
    entity = EntityId(id=str(track_id), type=EntityType.TRACK, source="qobuz")
    return Track(id=entity, title=f"song {track_id}", duration=240, album=Album(id=entity, title="album"))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, format_id=27):
        self.direct = FakeDirect()
        self.reporter = FakeReporter()
        self.tracks = FakeTracks()
        self.queue = CloudQueue()
        self.clock = Clock()
        self.renderer = ConnectRenderer(
            reporter=self.reporter,
            queue=self.queue,
            direct=self.direct,
            tracks=self.tracks,
            format_id=format_id,
            clock=self.clock,
        )

    async def settle(self):
        await self.renderer.settled()

    async def line_up(self):
        """Let a line-up timer that is due fire, and its fetch land."""
        for _ in range(3):
            await asyncio.sleep(0.01)
            await self.settle()

    def lined_up(self):
        return [c[1] for c in self.direct.hold.calls if c[0] == "next"]

    async def activate(self):
        await self.renderer.on_set_active(True)
        await self.settle()

    async def set_state(self, *, state=None, position=None, current=None, following=None):
        message = qc.RendererSetStateMessage(queue_version=qc.QueueVersionRef(major=1, minor=0))
        if state is not None:
            message.playing_state = state
        if position is not None:
            message.current_position = position
        if current is not None:
            message.current_track.CopyFrom(qc.QueueTrackWithContext(queue_item_id=current[0], track_id=current[1]))
        if following is not None:
            message.next_track.CopyFrom(qc.QueueTrackWithContext(queue_item_id=following[0], track_id=following[1]))
        await self.renderer.on_set_state(message)
        await self.settle()

    async def cast(self, current=(1, 111), position=0, following=(2, 222)):
        await self.activate()
        await self.set_state(state=PLAYING, position=position, current=current, following=following)
        self.clock.now += 10

    def last(self):
        return self.reporter.states[-1]

    async def shutdown(self):
        await self.renderer.shutdown()


@pytest_asyncio.fixture
async def harness():
    harness = Harness()
    yield harness
    await harness.shutdown()


async def test_a_cast_track_is_played_on_the_output_and_reported(harness):
    await harness.cast(position=15000)

    hold = harness.direct.hold
    assert hold.title == "Qobuz Connect"
    assert hold.calls == [("play", "https://streaming.qobuz.test/111", 15000)]
    report = harness.last()
    assert (report.playing_state, report.buffer_state) == (PLAYING, BUFFERING)
    assert report.current_queue_item_id == 1
    assert report.next_queue_item_id == 2
    assert report.duration == 240000
    assert (report.queue_version.major, report.queue_version.minor) == (1, 0)


async def test_the_outputs_state_is_reported_back(harness):
    await harness.cast()

    harness.direct.hold.show(PlayerStateEnum.PLAYING, position=4000)
    await harness.settle()

    report = harness.last()
    assert (report.playing_state, report.buffer_state) == (PLAYING, OK)
    assert report.current_position.value >= 4000


async def test_the_clouds_echo_of_a_report_changes_nothing(harness):
    await harness.cast()
    before = list(harness.direct.hold.calls)

    await harness.set_state(following=(2, 222))

    assert harness.direct.hold.calls == before
    assert len(harness.direct.holds) == 1


async def test_what_the_cloud_asks_for_is_logged_but_not_its_echoes(harness, caplog):
    with caplog.at_level(logging.INFO, logger="cloud_renderer"):
        await harness.cast(position=15000)
        await harness.set_state(state=PAUSED)
        await harness.set_state(following=(3, 333))

    assert [r.getMessage() for r in caplog.records if "asks:" in r.getMessage()] == [
        "Qobuz Connect asks: playing, at 15000 ms, track 111 (item 1)",
        "Qobuz Connect asks: paused",
    ]


async def test_the_handoff_replay_is_logged_as_ignored(harness, caplog):
    await harness.activate()
    await harness.set_state(state=PLAYING, current=(1, 111))

    with caplog.at_level(logging.INFO, logger="cloud_renderer"):
        await harness.set_state(state=PAUSED, position=0)

    assert harness.direct.hold.calls[-1][0] == "play"
    assert "ignored as the replay of the handoff" in caplog.text


async def test_a_play_request_with_nothing_to_play_is_logged(harness, caplog):
    await harness.activate()

    with caplog.at_level(logging.WARNING, logger="cloud_renderer"):
        await harness.set_state(state=PLAYING, position=5000)

    assert harness.direct.holds == []
    assert "names no track and none is loaded" in caplog.text


async def test_nothing_is_reported_while_another_renderer_is_active(harness):
    await harness.set_state(state=PLAYING, current=(1, 111))

    assert harness.reporter.states == []
    assert len(harness.direct.holds) == 1


async def test_pause_resume_and_seek(harness):
    await harness.cast()
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=60000)
    await harness.settle()

    await harness.set_state(state=PAUSED)
    await harness.set_state(state=PLAYING)
    await harness.set_state(position=120000)
    await harness.set_state(position=61000)

    assert hold.calls[1:] == [("pause",), ("resume",), ("seek", 120000)]
    assert harness.last().playing_state == PLAYING


async def test_a_pause_replayed_right_after_the_handoff_is_ignored(harness):
    await harness.activate()
    await harness.set_state(state=PLAYING, current=(1, 111))

    await harness.set_state(state=PAUSED, position=0)
    harness.clock.now += 2
    await harness.set_state(state=PAUSED, position=0)

    assert [c for c in harness.direct.hold.calls if c == ("pause",)] == [("pause",)]


async def test_stop_gives_the_output_back_and_says_so(harness):
    await harness.cast()

    await harness.set_state(state=STOPPED)

    assert harness.direct.hold.calls[-1] == ("release",)
    assert harness.last().playing_state == STOPPED


async def test_the_app_quality_is_capped_by_the_plugins(harness):
    capped = Harness(format_id=7)
    await capped.activate()
    await capped.renderer.on_set_max_quality(4)
    await capped.settle()
    await capped.set_state(state=PLAYING, current=(1, 111))

    assert capped.tracks.resolved == [(111, 7)]
    assert capped.reporter.qualities == [3]
    await capped.shutdown()


async def test_the_app_can_ask_for_less(harness):
    await harness.activate()
    await harness.renderer.on_set_max_quality(2)
    await harness.settle()

    await harness.set_state(state=PLAYING, current=(1, 111))

    assert harness.tracks.resolved == [(111, 6)]


async def test_the_format_that_plays_is_reported_once(harness):
    await harness.cast()
    output = OutputInfo(sample_rate=192000, bits_per_sample=24, channels=2)

    harness.direct.hold.show(PlayerStateEnum.PLAYING, sample_rate=96000, bits=24, output=output)
    harness.direct.hold.show(PlayerStateEnum.PLAYING, sample_rate=96000, bits=24, output=output)
    await harness.settle()

    assert harness.reporter.file_qualities == [(96000, 24, 2, 3)]
    assert harness.reporter.device_qualities == [(192000, 24, 2)]


async def test_the_format_is_reported_again_after_a_rejoin(harness):
    await harness.cast()
    harness.direct.hold.show(PlayerStateEnum.PLAYING, sample_rate=96000, bits=24)
    await harness.settle()

    await harness.renderer.on_joined()
    await harness.settle()

    assert harness.reporter.file_qualities == [(96000, 24, 2, 3)] * 2


@pytest.mark.parametrize(
    ("sample_rate", "bits", "mime_type", "level"),
    [
        (44100, 16, "audio/mpeg", 1),
        (44100, 16, "audio/flac", 2),
        (48000, 24, "audio/flac", 3),
        (96000, 24, "audio/flac", 3),
        (176400, 24, "audio/flac", 4),
        (192000, 24, "audio/flac", 4),
    ],
)
async def test_formats_rank_as_qobuzs_quality_levels(sample_rate, bits, mime_type, level):
    assert _quality_level(sample_rate, bits, mime_type) == level


async def test_the_apps_quality_choice_is_confirmed_and_plays_at_once(harness):
    await harness.cast()
    harness.direct.hold.show(PlayerStateEnum.PLAYING, position=30000, sample_rate=192000, bits=24)
    await harness.settle()

    await harness.renderer.on_set_max_quality(2)
    await harness.settle()

    assert harness.reporter.qualities[-1] == 2
    assert harness.tracks.resolved[-1] == (111, 6)
    play = [c for c in harness.direct.hold.calls if c[0] == "play"][-1]
    assert 30000 <= play[2] < 31000


async def test_a_paused_track_stays_paused_at_the_new_quality(harness):
    await harness.cast()
    harness.direct.hold.show(PlayerStateEnum.PAUSED, position=30000, sample_rate=192000, bits=24)
    await harness.settle()

    await harness.renderer.on_set_max_quality(2)
    await harness.settle()

    assert harness.direct.hold.calls[-2:] == [("play", "https://streaming.qobuz.test/111", 30000), ("pause",)]


async def test_a_choice_that_changes_nothing_that_plays_keeps_playing(harness):
    await harness.cast()
    harness.direct.hold.show(PlayerStateEnum.PLAYING, sample_rate=44100, bits=16)
    await harness.settle()

    await harness.renderer.on_set_max_quality(3)
    await harness.settle()

    assert harness.tracks.resolved == [(111, 27)]
    assert harness.reporter.qualities == [3]


async def test_a_higher_choice_fetches_a_track_held_below_it_again(harness):
    await harness.activate()
    await harness.renderer.on_set_max_quality(2)
    await harness.set_state(state=PLAYING, current=(1, 111))
    harness.direct.hold.show(PlayerStateEnum.PLAYING, sample_rate=44100, bits=16)
    await harness.settle()

    await harness.renderer.on_set_max_quality(4)
    await harness.settle()

    assert harness.tracks.resolved == [(111, 6), (111, 27)]


async def test_a_finished_track_moves_on_to_the_next(harness):
    await harness.cast(following=(2, 222))

    harness.direct.hold.listener.on_finished()
    await harness.settle()

    assert harness.direct.hold.calls[-1] == ("play", "https://streaming.qobuz.test/222", 0)
    assert harness.last().current_queue_item_id == 2


async def test_the_next_track_is_lined_up_shortly_before_the_end(harness):
    await harness.cast(following=(2, 222))
    hold = harness.direct.hold

    hold.show(PlayerStateEnum.PLAYING, position=200000)
    await harness.line_up()
    assert harness.lined_up() == []

    hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()

    assert harness.lined_up() == ["https://streaming.qobuz.test/222"]
    assert harness.tracks.resolved == [(111, 27), (222, 27)]


async def test_a_lined_up_track_plays_on_without_being_played_again(harness):
    await harness.cast(following=(2, 222))
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()

    hold.listener.on_next_started(track_of(222))
    await harness.settle()

    assert [c for c in hold.calls if c[0] == "play"] == [("play", "https://streaming.qobuz.test/111", 0)]
    assert harness.renderer.now_playing.title == "song 222"
    report = harness.last()
    assert (report.playing_state, report.current_queue_item_id) == (PLAYING, 2)
    assert report.current_position.value < 1000


async def test_the_clouds_echo_of_the_track_that_plays_on_reloads_nothing(harness):
    await harness.cast(following=(2, 222))
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()
    hold.listener.on_next_started(track_of(222))
    await harness.settle()

    await harness.set_state(state=PLAYING, current=(2, 222), following=(3, 333))

    assert [c for c in hold.calls if c[0] == "play"] == [("play", "https://streaming.qobuz.test/111", 0)]


async def test_a_new_next_track_replaces_the_one_lined_up(harness):
    await harness.cast(following=(2, 222))
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()

    await harness.set_state(following=(3, 333))
    await harness.line_up()

    assert harness.lined_up() == [
        "https://streaming.qobuz.test/222",
        None,
        "https://streaming.qobuz.test/333",
    ]


async def test_a_queue_change_lines_up_what_now_follows(harness):
    harness.queue.items = [QueueItem(1, 111), QueueItem(2, 222)]
    await harness.cast(following=None)
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()

    harness.queue.items.insert(1, QueueItem(5, 555))
    await harness.renderer.on_queue_changed()
    await harness.line_up()

    assert harness.lined_up() == [
        "https://streaming.qobuz.test/222",
        None,
        "https://streaming.qobuz.test/555",
    ]


async def test_nothing_is_lined_up_while_paused(harness):
    await harness.cast(following=(2, 222))

    harness.direct.hold.show(PlayerStateEnum.PAUSED, position=235000)
    await harness.line_up()

    assert harness.lined_up() == []


async def test_looping_one_track_lines_it_up_again(harness):
    await harness.cast(following=(2, 222))
    await harness.renderer.on_loop_mode(cloud_renderer.LOOP_ONE)

    harness.direct.hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()

    assert harness.lined_up() == ["https://streaming.qobuz.test/111"]


async def test_a_next_track_that_cannot_be_fetched_follows_after_a_gap(harness):
    await harness.cast(following=(2, 222))
    hold = harness.direct.hold
    harness.tracks.error = RuntimeError("no stream")

    hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()
    assert harness.lined_up() == []

    harness.tracks.error = None
    hold.listener.on_finished()
    await harness.settle()

    assert ("play", "https://streaming.qobuz.test/222", 0) in hold.calls


async def test_moving_on_to_a_track_no_longer_next_plays_the_one_that_is(harness):
    await harness.cast(following=(2, 222))
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()
    # Back to the start: the next change of what follows fetches nothing yet.
    hold.show(PlayerStateEnum.PLAYING, position=1000)
    await harness.set_state(following=(3, 333))

    hold.listener.on_next_started(track_of(222))
    await harness.settle()

    assert hold.calls[-1] == ("play", "https://streaming.qobuz.test/333", 0)
    assert harness.last().current_queue_item_id == 3


async def test_moving_on_heard_after_another_track_was_played_skips_nothing(harness):
    await harness.cast(following=(2, 222))
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=235000)
    await harness.line_up()

    # The renderer moved on just as the app named another track to play.
    await harness.set_state(state=PLAYING, current=(3, 333), following=(4, 444))
    hold.listener.on_next_started(track_of(222))
    await harness.settle()

    assert [c for c in hold.calls if c[0] == "play"] == [
        ("play", "https://streaming.qobuz.test/111", 0),
        ("play", "https://streaming.qobuz.test/333", 0),
    ]
    assert harness.last().current_queue_item_id == 3


async def test_the_end_of_the_queue_gives_the_output_back(harness):
    harness.queue.items = []
    await harness.activate()
    await harness.set_state(state=PLAYING, current=(1, 111))

    harness.direct.hold.listener.on_finished()
    await harness.settle()

    assert harness.direct.hold.calls[-1] == ("release",)
    assert harness.last().playing_state == STOPPED


async def test_controls_pressed_in_kalinka_are_applied_and_reported(harness):
    await harness.cast(following=(2, 222))
    hold = harness.direct.hold
    listener = hold.listener

    listener.on_command(TransportRequest(TransportKind.PAUSE))
    listener.on_command(TransportRequest(TransportKind.SEEK, position_ms=90000))
    listener.on_command(TransportRequest(TransportKind.NEXT))
    await harness.settle()

    assert ("pause",) in hold.calls
    assert ("seek", 90000) in hold.calls
    assert hold.calls[-1] == ("play", "https://streaming.qobuz.test/222", 0)
    assert harness.last().current_queue_item_id == 2


async def test_previous_restarts_a_track_well_under_way(harness):
    await harness.cast()
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=30000)
    await harness.settle()

    hold.listener.on_command(TransportRequest(TransportKind.PREV))
    await harness.settle()

    assert hold.calls[-1] == ("seek", 0)


async def test_losing_the_output_tells_the_app_playback_stopped(harness):
    await harness.cast()
    hold = harness.direct.hold
    hold.active = False

    hold.listener.on_revoked(RevokeReason.QUEUE_PLAY)
    await harness.settle()

    assert harness.last().playing_state == STOPPED
    assert harness.renderer.now_playing is None


async def test_playing_again_from_the_app_takes_the_output_back(harness):
    await harness.cast(position=0)
    first = harness.direct.hold
    first.show(PlayerStateEnum.PLAYING, position=50000)
    await harness.settle()
    first.active = False
    first.listener.on_revoked(RevokeReason.QUEUE_PLAY)
    await harness.settle()

    await harness.set_state(state=PLAYING)

    assert len(harness.direct.holds) == 2
    assert harness.direct.hold.calls == [("play", "https://streaming.qobuz.test/111", 50000)]


async def test_late_news_from_a_replaced_hold_is_ignored(harness):
    await harness.cast()
    first = harness.direct.hold
    first.active = False
    await harness.set_state(state=PLAYING)
    second = harness.direct.hold
    assert second is not first

    first.listener.on_revoked(RevokeReason.QUEUE_PLAY)
    first.listener.on_finished()
    await harness.settle()

    assert harness.renderer.now_playing is not None
    assert second.calls == [("play", "https://streaming.qobuz.test/111", 0)]
    harness.clock.now += 10
    await harness.renderer.on_set_state(qc.RendererSetStateMessage(playing_state=PAUSED))
    await harness.settle()
    assert second.calls[-1] == ("pause",)


async def test_a_failed_stream_is_fetched_again_once_from_where_it_was(harness):
    await harness.cast()
    hold = harness.direct.hold

    hold.show(PlayerStateEnum.ERROR, position=42000)
    await harness.settle()

    assert harness.tracks.resolved == [(111, 27), (111, 27)]
    assert hold.calls[-1] == ("play", "https://streaming.qobuz.test/111", 42000)
    assert hold.active

    hold.show(PlayerStateEnum.ERROR, position=42000)
    await harness.settle()

    assert hold.calls[-1] == ("release",)
    assert harness.last().playing_state == STOPPED


async def test_the_same_position_repeated_after_a_load_does_not_seek(harness):
    """The cloud repeats the takeover's position; the stream is still starting,
    and a seek then is what froze the renderer."""
    await harness.activate()
    await harness.set_state(state=PLAYING, position=15000, current=(1, 111))
    harness.clock.now += 10

    await harness.set_state(state=PLAYING, position=15000)
    await harness.set_state(position=15500)

    assert [c for c in harness.direct.hold.calls if c[0] == "seek"] == []


async def test_playback_resumes_where_the_output_was_lost(harness):
    await harness.cast()
    first = harness.direct.hold
    first.show(PlayerStateEnum.PLAYING, position=50000)
    await harness.settle()
    first.active = False
    first.listener.on_revoked(RevokeReason.QUEUE_PLAY)
    await harness.settle()

    await asyncio.sleep(0.3)
    await harness.set_state(state=PLAYING)

    [(_, _, offset)] = harness.direct.hold.calls
    assert 50000 <= offset < 50200


async def test_a_position_past_the_end_starts_the_track_over(harness):
    await harness.activate()

    await harness.set_state(state=PLAYING, position=10_000_000, current=(1, 111))

    assert harness.direct.hold.calls == [("play", "https://streaming.qobuz.test/111", 0)]


async def test_a_position_before_the_start_plays_from_the_start(harness):
    """The app asked for -319 ms; the server's renderer refused the offset
    after the play call returned, and Kalinka stayed buffering."""
    await harness.activate()

    await harness.set_state(state=PLAYING, position=-319, current=(1, 111))

    assert harness.direct.hold.calls == [("play", "https://streaming.qobuz.test/111", 0)]
    assert harness.last().current_position.value == 0


async def test_a_seek_before_the_start_seeks_to_the_start(harness):
    await harness.cast()
    hold = harness.direct.hold
    hold.show(PlayerStateEnum.PLAYING, position=60000)
    await harness.settle()

    await harness.set_state(position=-500)

    assert hold.calls[1:] == [("seek", 0)]


async def test_another_renderer_taking_over_stops_playback_here(harness):
    await harness.cast()
    reports = len(harness.reporter.states)

    await harness.renderer.on_active_renderer(False)
    await harness.settle()

    assert harness.direct.hold.calls[-1] == ("release",)
    assert not harness.renderer.active
    assert len(harness.reporter.states) == reports


async def test_a_set_inactive_replayed_after_the_load_is_ignored(harness):
    await harness.activate()
    await harness.set_state(state=PLAYING, current=(1, 111))

    await harness.renderer.on_set_active(False)
    await harness.settle()

    assert harness.renderer.active
    assert harness.direct.hold.active


async def test_standing_down_releases_a_hold_taken_before_becoming_the_renderer(harness):
    await harness.set_state(state=PLAYING, current=(1, 111))

    await harness.renderer.on_active_renderer(False)
    await harness.settle()

    assert harness.direct.hold.calls[-1] == ("release",)


async def test_a_player_told_to_stand_down_while_idle_reports_stopped_on_its_return(harness):
    await harness.set_state(state=PAUSED)

    await harness.renderer.on_active_renderer(False)
    await harness.activate()

    assert harness.last().playing_state == STOPPED


async def test_a_player_that_stood_down_reports_stopped_on_its_return(harness):
    await harness.cast()
    await harness.renderer.on_active_renderer(False)

    await harness.renderer.on_set_active(True)
    await harness.settle()

    assert harness.last().playing_state == STOPPED


async def test_a_track_that_cannot_be_fetched_is_reported_stopped(harness):
    harness.tracks.error = RuntimeError("no stream")
    await harness.activate()

    await harness.set_state(state=PLAYING, current=(1, 111))

    assert harness.direct.holds == []
    assert harness.last().playing_state == STOPPED


async def test_a_track_that_cannot_be_fetched_stops_what_played_before(harness):
    await harness.cast()
    hold = harness.direct.hold
    harness.tracks.error = RuntimeError("no stream")

    await harness.set_state(state=PLAYING, current=(2, 222))

    assert hold.calls[-1] == ("release",)
    assert harness.last().playing_state == STOPPED
    assert harness.renderer.now_playing is None


async def test_no_output_to_play_on_is_reported_stopped(harness):
    harness.direct.unavailable = "no renderer is connected"
    await harness.activate()

    await harness.set_state(state=PLAYING, current=(1, 111))

    assert harness.last().playing_state == STOPPED


async def test_volume_and_mute_go_through_the_hold(harness):
    await harness.cast()
    hold = harness.direct.hold

    await harness.renderer.on_set_volume(qc.RendererSetVolumeMessage(volume=30))
    await harness.renderer.on_set_volume(qc.RendererSetVolumeMessage(volume_delta=5))
    await harness.renderer.on_mute(True)
    await harness.renderer.on_mute(False)
    await harness.settle()

    assert [c for c in hold.calls if c[0] == "volume"] == [
        ("volume", 30),
        ("volume", 35),
        ("volume", 0),
        ("volume", 35),
    ]
    assert harness.reporter.volumes[-2:] == [30, 35]
    assert harness.reporter.muted == [True, False]


async def test_the_outputs_volume_is_reported_as_playback_starts(harness):
    await harness.cast()

    assert harness.reporter.volumes == [40]


async def test_a_volume_change_on_the_output_reaches_the_app(harness):
    await harness.cast()
    listener = harness.direct.hold.listener

    listener.on_volume(DeviceVolume(max_volume=100, current_volume=55))
    listener.on_volume(DeviceVolume(max_volume=160, current_volume=40))
    listener.on_volume(DeviceVolume(max_volume=160, current_volume=40))
    listener.on_volume(DeviceVolume(supported=False))
    await harness.settle()

    assert harness.reporter.volumes == [40, 55, 25]


async def test_the_outputs_echo_of_the_apps_volume_is_not_sent_back(harness):
    await harness.cast()
    listener = harness.direct.hold.listener

    await harness.renderer.on_set_volume(qc.RendererSetVolumeMessage(volume=30))
    await harness.renderer.on_set_volume(qc.RendererSetVolumeMessage(volume=35))
    listener.on_volume(DeviceVolume(max_volume=100, current_volume=30))
    await harness.settle()
    assert harness.reporter.volumes == [40, 30, 35]

    harness.clock.now += VOLUME_ECHO_S
    listener.on_volume(DeviceVolume(max_volume=100, current_volume=30))
    await harness.settle()
    assert harness.reporter.volumes == [40, 30, 35, 30]


async def test_an_echo_rounded_to_a_coarse_devices_steps_is_not_sent_back(harness):
    await harness.cast()

    await harness.renderer.on_set_volume(qc.RendererSetVolumeMessage(volume=55))
    # 55% of 30 steps is 16.5, which the device holds as 16: 53%.
    harness.direct.hold.listener.on_volume(DeviceVolume(max_volume=30, current_volume=16))
    await harness.settle()

    assert harness.reporter.volumes == [40, 55]


async def test_muting_before_the_outputs_volume_is_heard_restores_the_output_s(harness):
    await harness.cast()
    harness.renderer._volume = None

    await harness.renderer.on_mute(True)
    await harness.renderer.on_mute(False)
    await harness.settle()

    assert [c for c in harness.direct.hold.calls if c[0] == "volume"] == [
        ("volume", 0),
        ("volume", 40),
    ]


async def test_turning_the_volume_up_elsewhere_ends_the_apps_mute(harness):
    await harness.cast()
    await harness.renderer.on_mute(True)
    await harness.settle()

    harness.direct.hold.listener.on_volume(DeviceVolume(max_volume=100, current_volume=25))
    await harness.settle()

    assert harness.reporter.volumes[-1] == 25
    assert harness.reporter.muted == [True, False]


@pytest.fixture
def quick_recheck(monkeypatch):
    monkeypatch.setattr(cloud_renderer, "SHOWN_VOLUME_RECHECK_S", 0.01)


async def _after_recheck(harness):
    await asyncio.sleep(0.05)
    await harness.settle()


async def test_a_level_the_app_shows_of_its_own_is_corrected(harness, quick_recheck, caplog):
    await harness.cast()

    with caplog.at_level(logging.INFO, logger="cloud_renderer"):
        await harness.renderer.on_shown_volume(70)
        await _after_recheck(harness)

    assert harness.reporter.volumes == [40, 40]
    assert "shows volume 70% for this player" in caplog.text


async def test_the_clouds_echo_of_our_reports_is_left_alone(harness, quick_recheck):
    await harness.cast()
    await harness.renderer.on_set_volume(qc.RendererSetVolumeMessage(volume=30))
    await harness.renderer.on_set_volume(qc.RendererSetVolumeMessage(volume=35))

    await harness.renderer.on_shown_volume(30)
    await harness.renderer.on_shown_volume(35)
    await _after_recheck(harness)

    assert harness.reporter.volumes == [40, 30, 35]


async def test_a_volume_command_right_after_the_apps_level_is_obeyed(harness, monkeypatch):
    monkeypatch.setattr(cloud_renderer, "SHOWN_VOLUME_RECHECK_S", 0.2)
    await harness.cast()

    await harness.renderer.on_shown_volume(70)
    await harness.settle()
    await asyncio.sleep(0.02)
    await harness.renderer.on_set_volume(qc.RendererSetVolumeMessage(volume=70))
    await asyncio.sleep(0.3)
    await harness.settle()

    assert harness.reporter.volumes == [40, 70]
    assert harness.direct.hold.volume.current_volume == 70


async def test_under_the_apps_mute_the_level_it_restores_is_the_one_shown(harness, quick_recheck):
    await harness.cast()
    await harness.renderer.on_mute(True)
    await harness.settle()
    harness.clock.now += VOLUME_ECHO_S

    await harness.renderer.on_shown_volume(40)
    await _after_recheck(harness)
    assert harness.reporter.volumes == [40]

    await harness.renderer.on_shown_volume(55)
    await _after_recheck(harness)
    assert harness.reporter.volumes == [40, 40]


async def test_a_player_that_is_not_active_corrects_nothing(harness, quick_recheck):
    await harness.cast()
    await harness.renderer.on_active_renderer(False)

    await harness.renderer.on_shown_volume(70)
    await _after_recheck(harness)

    assert harness.reporter.volumes == [40]


async def test_becoming_active_again_reports_the_volume(harness):
    await harness.cast()
    await harness.renderer.on_active_renderer(False)

    await harness.renderer.on_set_active(True)
    await harness.settle()

    assert harness.reporter.volumes == [40, 40]


async def test_shutdown_tells_the_app_and_gives_the_output_back():
    harness = Harness()
    await harness.cast()

    await harness.shutdown()

    assert harness.last().playing_state == STOPPED
    assert harness.direct.hold.calls[-1] == ("release",)
