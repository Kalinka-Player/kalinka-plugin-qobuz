"""Joining a Qobuz Connect session and routing what the cloud says."""

import logging
import uuid

import pytest

from kalinka_plugin_qobuz.connect import frames
from kalinka_plugin_qobuz.connect.cloud_queue import CloudQueue
from kalinka_plugin_qobuz.connect.cloud_session import ConnectSession, DeviceIdentity
from kalinka_plugin_qobuz.connect.proto import qconnect_pb2 as qc

DEVICE_UUID = "8f2c1f4e-3d6b-4a57-9c1e-2b7d9e0a4c11"
SESSION_UUID = "0b0e7c9a-1111-4222-8333-944455556666"
IDENTITY = DeviceIdentity(
    device_uuid=DEVICE_UUID,
    friendly_name="Kalinka (living room)",
    software_version="kalinka-qobuz-5.1.0",
    max_audio_quality=3,
)

pytestmark = pytest.mark.asyncio


class FakePlayback:
    def __init__(self):
        self.active = False
        self.calls = []

    async def on_joined(self):
        self.calls.append(("joined",))

    async def on_set_state(self, message):
        self.calls.append(("set_state", message))

    async def on_set_volume(self, message):
        self.calls.append(("set_volume", message.volume))

    async def on_set_max_quality(self, level):
        self.calls.append(("max_quality", level))

    async def on_mute(self, muted):
        self.calls.append(("mute", muted))

    async def on_loop_mode(self, mode):
        self.calls.append(("loop", mode))

    async def on_queue_changed(self):
        self.calls.append(("queue_changed",))

    async def on_set_active(self, active):
        self.active = active
        self.calls.append(("set_active", active))

    async def on_active_renderer(self, ours):
        self.calls.append(("active_renderer", ours))

    async def on_shown_volume(self, percent):
        self.calls.append(("shown_volume", percent))


class Harness:
    def __init__(self):
        self.sent: list[qc.QConnectMessage] = []
        self.established = 0
        self.connected = True
        self.queue = CloudQueue()
        self.playback = FakePlayback()
        self.session = ConnectSession(IDENTITY, self.queue, self._send, self._established)
        self.session.attach(self.playback)

    async def _send(self, messages):
        if not self.connected:
            return False
        self.sent.extend(messages)
        return True

    def _established(self):
        self.established += 1

    def kinds(self):
        return [m.message_type for m in self.sent]

    def joins(self):
        return [m.rndr_srvr_join_session for m in self.sent if m.message_type == qc.RNDR_SRVR_JOIN_SESSION]

    async def hear(self, **fields):
        await self.session.on_messages([qc.QConnectMessage(**fields)])

    async def session_state(self, active_renderer_id=None, session_uuid=SESSION_UUID):
        body = qc.CtrlSessionStateMessage(session_uuid=uuid.UUID(session_uuid).bytes)
        if active_renderer_id is not None:
            body.active_renderer_id = active_renderer_id
        await self.hear(message_type=qc.SRVR_CTRL_SESSION_STATE, srvr_ctrl_session_state=body)

    async def active_renderer(self, renderer_id):
        await self.hear(
            message_type=qc.SRVR_CTRL_ACTIVE_RENDERER_CHANGED,
            srvr_ctrl_active_renderer_changed=qc.CtrlActiveRendererChangedMessage(
                active_renderer_id=renderer_id
            ),
        )

    def told_active(self):
        return [c[1] for c in self.playback.calls if c[0] == "active_renderer"]

    async def add_renderer(self, renderer_id, device_uuid=DEVICE_UUID, **info):
        device = qc.DeviceInfoMessage(**info)
        if device_uuid is not None:
            device.device_uuid = uuid.UUID(device_uuid).bytes
        await self.hear(
            message_type=qc.SRVR_CTRL_ADD_RENDERER,
            srvr_ctrl_add_renderer=qc.CtrlAddRendererMessage(renderer_id=renderer_id, device_info=device),
        )


@pytest.fixture
def harness():
    return Harness()


async def test_a_connection_joins_as_a_controller_first(harness):
    await harness.session.on_connected()

    assert harness.kinds() == [qc.CTRL_SRVR_JOIN_SESSION, qc.CTRL_SRVR_ASK_FOR_QUEUE_STATE]
    info = harness.sent[0].ctrl_srvr_join_session.device_info
    assert frames.uuid_text(info.device_uuid) == DEVICE_UUID
    assert (info.friendly_name, info.brand, info.device_type) == ("Kalinka (living room)", "Kalinka", 2)
    assert info.capabilities.max_audio_quality == 3
    assert not harness.sent[0].ctrl_srvr_join_session.HasField("session_uuid")


async def test_the_named_session_is_joined_as_an_available_renderer(harness):
    await harness.session.on_connected()

    await harness.session_state()

    [join] = harness.joins()
    assert frames.uuid_text(join.session_uuid) == SESSION_UUID
    assert join.is_active is False
    assert join.reason == 1
    assert join.initial_state.playing_state == 1
    assert harness.established == 1
    assert ("joined",) in harness.playback.calls
    assert harness.kinds()[-1] == qc.CTRL_SRVR_ASK_FOR_QUEUE_STATE


async def test_after_a_handoff_it_joins_as_the_active_renderer(harness):
    harness.session.join_as_active_next()
    await harness.session.on_connected()

    await harness.session_state()

    assert harness.joins()[0].is_active is True
    assert ("set_active", True) in harness.playback.calls


async def test_the_same_session_is_joined_once(harness):
    await harness.session.on_connected()
    await harness.session_state()
    await harness.session_state()

    assert len(harness.joins()) == 1


async def test_a_reconnect_rejoins_as_a_reconnection_keeping_its_part(harness):
    await harness.session.on_connected()
    await harness.session_state()
    harness.playback.active = True

    await harness.session.on_connected()
    await harness.session_state()

    rejoin = harness.joins()[-1]
    assert rejoin.reason == 2
    assert rejoin.is_active is True


async def test_its_own_renderer_id_is_learnt_from_the_device_uuid(harness):
    await harness.add_renderer(4, device_uuid=str(uuid.uuid4()), friendly_name="Phone")
    await harness.add_renderer(7)

    assert harness.session.renderer_id == 7


async def test_the_renderer_list_heard_before_the_session_is_named_is_kept(harness):
    """The cloud lists the session's renderers first, then names the session."""
    harness.session.join_as_active_next()
    await harness.session.on_connected()
    await harness.add_renderer(4)
    await harness.session_state(active_renderer_id=4)

    await harness.active_renderer(1)

    assert harness.session.renderer_id == 4
    assert harness.told_active() == [False]


@pytest.mark.parametrize("reconnect", [True, False])
async def test_an_id_from_another_session_is_forgotten(harness, reconnect):
    await harness.session.on_connected()
    await harness.add_renderer(7)
    await harness.session_state()
    if reconnect:
        # A session this player has not joined does not list it.
        await harness.session.on_connected()

    await harness.session_state(session_uuid=str(uuid.uuid4()))

    assert harness.session.renderer_id is None


async def test_without_a_uuid_the_name_identifies_it(harness):
    await harness.add_renderer(9, device_uuid=None, friendly_name="Kalinka (living room)", brand="Kalinka")

    assert harness.session.renderer_id == 9


async def test_the_active_renderer_is_told_to_the_playback(harness):
    await harness.add_renderer(7)

    await harness.active_renderer(7)
    await harness.active_renderer(3)

    assert harness.told_active() == [True, False]


@pytest.mark.parametrize("own_id", [7, None])
async def test_no_active_renderer_is_never_this_player(harness, own_id):
    """The Qobuz app took playback onto its own speakers: stop here."""
    if own_id is not None:
        await harness.add_renderer(own_id)

    await harness.active_renderer(-1)

    assert harness.told_active() == [False]


async def test_a_handoff_is_not_undone_by_the_session_state_it_answers(harness):
    harness.session.join_as_active_next()
    await harness.session.on_connected()

    # Named before this player claimed playback: the app played on its own.
    await harness.session_state(active_renderer_id=-1)

    assert ("set_active", True) in harness.playback.calls
    assert harness.told_active() == []


async def _playing_then_reconnected(harness):
    await harness.session.on_connected()
    await harness.session_state()
    await harness.add_renderer(7)
    harness.playback.active = True
    harness.playback.calls.clear()
    await harness.session.on_connected()


@pytest.mark.parametrize("named", [-1, 7])
async def test_a_reconnect_keeps_playing_through_the_session_state_it_answers(harness, named):
    await _playing_then_reconnected(harness)

    await harness.session_state(active_renderer_id=named)

    assert harness.joins()[-1].is_active is True
    assert harness.told_active() == []


async def test_a_reconnect_after_another_renderer_took_over_leaves_it_there(harness):
    await _playing_then_reconnected(harness)

    await harness.session_state(active_renderer_id=3)

    assert harness.joins()[-1].is_active is False
    # Told before it joins, so it does not report playing to the session.
    calls = [c[0] for c in harness.playback.calls]
    assert calls.index("active_renderer") < calls.index("joined")
    assert harness.told_active() == [False]


async def test_a_connection_that_dropped_before_joining_does_not_end_the_reconnect(harness):
    await _playing_then_reconnected(harness)

    await harness.session.on_connected()
    await harness.session_state(active_renderer_id=-1)

    assert harness.joins()[-1].is_active is True
    assert harness.told_active() == []


async def test_a_new_session_is_joined_as_available_even_while_playing(harness):
    await _playing_then_reconnected(harness)
    await harness.add_renderer(9)

    await harness.session_state(active_renderer_id=3, session_uuid=str(uuid.uuid4()))

    rejoin = harness.joins()[-1]
    assert (rejoin.is_active, rejoin.reason) == (False, 1)
    assert harness.told_active() == [False]


async def test_a_new_session_naming_no_renderer_stands_a_playing_player_down(harness):
    await _playing_then_reconnected(harness)

    await harness.session_state(session_uuid=str(uuid.uuid4()))

    assert harness.joins()[-1].is_active is False
    assert harness.told_active() == [False]


async def test_a_session_naming_this_player_is_joined_as_its_active_renderer(harness):
    """Restarted while the Qobuz app still has this player selected."""
    await harness.session.on_connected()
    await harness.add_renderer(4)

    await harness.session_state(active_renderer_id=4)

    assert harness.joins()[-1].is_active is True
    assert ("set_active", True) in harness.playback.calls
    assert harness.told_active() == []


async def test_a_reconnect_after_losing_its_id_does_not_claim_over_another(harness):
    await harness.session.on_connected()
    await harness.session_state()
    await harness.add_renderer(7)
    harness.playback.active = True
    await harness.hear(
        message_type=qc.SRVR_CTRL_REMOVE_RENDERER,
        srvr_ctrl_remove_renderer=qc.CtrlRemoveRendererMessage(renderer_id=7),
    )
    await harness.session.on_connected()

    await harness.session_state(active_renderer_id=3)

    assert harness.joins()[-1].is_active is False
    assert harness.told_active() == [False]


async def test_a_reconnect_the_session_names_as_active_claims_it(harness):
    await harness.session.on_connected()
    await harness.session_state()
    await harness.add_renderer(7)
    await harness.session.on_connected()

    await harness.session_state(active_renderer_id=7)

    assert harness.joins()[-1].is_active is True
    assert ("set_active", True) in harness.playback.calls
    assert harness.told_active() == []


async def test_coming_back_to_a_session_on_the_same_connection_is_no_reconnect(harness):
    other = str(uuid.uuid4())
    await harness.session.on_connected()
    await harness.session_state()
    await harness.session_state(session_uuid=other)
    harness.playback.active = True

    await harness.session_state()

    rejoin = harness.joins()[-1]
    assert (rejoin.is_active, rejoin.reason) == (False, 1)


async def test_an_active_renderer_change_naming_nothing_means_none(harness):
    await harness.add_renderer(7)
    await harness.active_renderer(7)

    await harness.hear(
        message_type=qc.SRVR_CTRL_ACTIVE_RENDERER_CHANGED,
        srvr_ctrl_active_renderer_changed=qc.CtrlActiveRendererChangedMessage(),
    )

    assert harness.told_active() == [True, False]


async def test_a_joined_session_naming_no_renderer_stops_it_here(harness):
    harness.session.join_as_active_next()
    await harness.session.on_connected()
    await harness.session_state(active_renderer_id=-1)

    await harness.session_state(active_renderer_id=-1)

    assert harness.told_active() == [False]


async def test_joining_as_available_hears_who_plays(harness):
    await harness.session.on_connected()

    await harness.session_state(active_renderer_id=-1)

    assert harness.joins()[0].is_active is False
    assert harness.told_active() == [False]


async def test_every_message_heard_is_traced_at_debug(harness, caplog):
    caplog.set_level(logging.DEBUG)

    await harness.active_renderer(7)
    await harness.hear(message_type=9999)

    assert [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG] == [
        "Qobuz Connect cloud sent SRVR_CTRL_ACTIVE_RENDERER_CHANGED",
        "Qobuz Connect cloud sent 9999",
    ]


async def test_each_change_of_active_renderer_is_logged_once(harness, caplog):
    caplog.set_level(logging.INFO)
    await harness.add_renderer(7)

    for renderer_id in (7, 7, -1, -1, 3):
        await harness.active_renderer(renderer_id)

    assert [r.getMessage() for r in caplog.records if "session" in r.getMessage()] == [
        "Qobuz Connect session plays on renderer 7 (this player: 7)",
        "Qobuz Connect session has no active renderer",
        "Qobuz Connect session plays on renderer 3 (this player: 7)",
    ]


async def test_the_volume_the_app_shows_for_this_player_is_told_to_the_playback(harness):
    await harness.add_renderer(7)

    for renderer_id, volume in ((7, 35), (3, 80)):
        await harness.hear(
            message_type=qc.SRVR_CTRL_VOLUME_CHANGED,
            srvr_ctrl_volume_changed=qc.CtrlVolumeChangedMessage(renderer_id=renderer_id, volume=volume),
        )

    assert [c for c in harness.playback.calls if c[0] == "shown_volume"] == [("shown_volume", 35)]


async def test_a_pending_set_active_is_not_an_answer(harness):
    await harness.hear(message_type=qc.SRVR_RNDR_SET_ACTIVE, srvr_rndr_set_active=qc.RendererSetActiveMessage())
    await harness.hear(
        message_type=qc.SRVR_RNDR_SET_ACTIVE, srvr_rndr_set_active=qc.RendererSetActiveMessage(active=True)
    )

    assert harness.playback.calls == [("set_active", True)]


async def test_renderer_commands_reach_the_playback(harness):
    state = qc.RendererSetStateMessage(playing_state=3)
    await harness.hear(message_type=qc.SRVR_RNDR_SET_STATE, srvr_rndr_set_state=state)
    await harness.hear(
        message_type=qc.SRVR_RNDR_SET_VOLUME, srvr_rndr_set_volume=qc.RendererSetVolumeMessage(volume=30)
    )
    await harness.hear(
        message_type=qc.SRVR_RNDR_SET_MAX_AUDIO_QUALITY,
        srvr_rndr_set_max_audio_quality=qc.RendererSetMaxAudioQualityMessage(max_audio_quality=2),
    )
    await harness.hear(
        message_type=qc.SRVR_RNDR_MUTE_VOLUME, srvr_rndr_mute_volume=qc.RendererMuteVolumeMessage(value=True)
    )

    assert harness.playback.calls == [
        ("set_state", state),
        ("set_volume", 30),
        ("max_quality", 2),
        ("mute", True),
    ]


async def test_queue_changes_are_kept_and_a_reorder_refetched(harness):
    await harness.hear(
        message_type=qc.SRVR_CTRL_QUEUE_TRACKS_ADDED,
        srvr_ctrl_queue_tracks_added=qc.QueueTracksAddedMessage(
            tracks=[qc.QueueTrack(queue_item_id=1, track_id=100)]
        ),
    )
    assert [i.track_id for i in harness.queue.items] == [100]
    assert harness.sent == []
    assert harness.playback.calls == [("queue_changed",)]

    await harness.hear(
        message_type=qc.SRVR_CTRL_QUEUE_TRACKS_REORDERED,
        srvr_ctrl_queue_tracks_reordered=qc.QueueTracksReorderedMessage(queue_item_ids=[1]),
    )
    assert harness.kinds() == [qc.CTRL_SRVR_ASK_FOR_QUEUE_STATE]


async def test_a_join_that_could_not_be_sent_is_tried_again(harness):
    await harness.session.on_connected()
    harness.connected = False
    await harness.session_state()
    harness.connected = True

    await harness.session_state()

    assert len(harness.joins()) == 1


async def test_reports_are_framed_as_the_renderer_messages(harness):
    await harness.session.send_state(qc.RendererStateMessage(playing_state=2))
    await harness.session.send_volume(55)
    await harness.session.send_max_quality(3)
    await harness.session.send_muted(False)
    await harness.session.send_file_quality(
        qc.RendererFileAudioQualityChangedMessage(sampling_rate=96000, audio_quality=3)
    )
    await harness.session.send_device_quality(
        qc.RendererDeviceAudioQualityChangedMessage(sampling_rate=192000)
    )

    assert harness.kinds() == [
        qc.RNDR_SRVR_STATE_UPDATED,
        qc.RNDR_SRVR_VOLUME_CHANGED,
        qc.RNDR_SRVR_MAX_AUDIO_QUALITY_CHANGED,
        qc.RNDR_SRVR_VOLUME_MUTED,
        qc.RNDR_SRVR_FILE_AUDIO_QUALITY_CHANGED,
        qc.RNDR_SRVR_DEVICE_AUDIO_QUALITY_CHANGED,
    ]
    assert harness.sent[0].rndr_srvr_state_updated.state.playing_state == 2
    assert harness.sent[4].rndr_srvr_file_audio_quality_changed.audio_quality == 3
    assert harness.sent[5].rndr_srvr_device_audio_quality_changed.sampling_rate == 192000
