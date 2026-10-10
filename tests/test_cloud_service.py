"""Connect playback end to end: pairing's session in, the cloud and the output out."""

import asyncio
import json
import time

import httpx
import pytest
import pytest_asyncio

from kalinka_plugin_qobuz.auth import TokenHolder
from kalinka_plugin_qobuz.connect import cloud_link
from kalinka_plugin_qobuz.connect.cloud_service import ConnectService
from kalinka_plugin_qobuz.connect.proto import qconnect_pb2 as qc
from kalinka_plugin_qobuz.connect.session_token import SessionToken
from kalinka_plugin_qobuz.connect.store import LinkState

from conftest import (
    API_JWT,
    GOOD_SECRET,
    QCONNECT_JWT,
    RENEWED_JWT,
    RENEWED_QCONNECT_JWT,
    FakeQobuzApi,
    assert_no_secrets,
    bearer,
    uat,
)
from fake_cloud import SESSION_UUID, FakeCloud
from test_cloud_renderer import FakeDirect

pytestmark = pytest.mark.asyncio

DEVICE_UUID = "8f2c1f4e-3d6b-4a57-9c1e-2b7d9e0a4c11"
PLAYING, STOPPED = 2, 1


@pytest.fixture(autouse=True)
def quick_backoff(monkeypatch):
    monkeypatch.setattr(cloud_link, "BACKOFF_START_S", 0.01)
    monkeypatch.setattr(cloud_link, "BACKOFF_MAX_S", 0.02)


@pytest_asyncio.fixture
async def cloud():
    fake = await FakeCloud().start()
    yield fake
    await fake.stop()


class Harness:
    def __init__(self, cloud: FakeCloud, *, exp: int = 0, bearer_credential=None, refresh=None):
        self.cloud = cloud
        self.api = FakeQobuzApi()
        holder = TokenHolder()
        holder.install(uat())
        self.client = self.api.client(holder)
        # Chosen when the link was checked.
        self.client.sec = GOOD_SECRET
        self.direct = FakeDirect()
        self.persisted: list[SessionToken] = []
        self.refresh_requests: list[httpx.Request] = []
        self.refresh = refresh
        self.service = ConnectService(
            direct=self.direct,
            client=self.client,
            format_id=27,
            device_name="Kalinka (test)",
            software_version="kalinka-qobuz-test",
            bearer=lambda: bearer_credential,
            persist=self.persisted.append,
            make_http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self._refresh)),
        )
        self.link = LinkState(
            device_uuid=DEVICE_UUID,
            linked=True,
            credential=uat(),
            session=SessionToken(jwt=QCONNECT_JWT, endpoint=cloud.url, exp=exp, session_id="sess-1"),
        )

    def _refresh(self, request: httpx.Request) -> httpx.Response:
        self.refresh_requests.append(request)
        if self.refresh is None:
            return httpx.Response(401, json={"status": "error"})
        return httpx.Response(200, json=self.refresh)

    async def close(self):
        await self.service.stop()
        await self.client.aclose()


def _set_state(**fields) -> qc.QConnectMessage:
    return qc.QConnectMessage(
        message_type=qc.SRVR_RNDR_SET_STATE, srvr_rndr_set_state=qc.RendererSetStateMessage(**fields)
    )


async def test_a_handoff_joins_the_session_as_the_active_renderer(cloud):
    harness = Harness(cloud)

    harness.service.session_ready(harness.link, handed_over=True)
    await cloud.until(lambda: qc.RNDR_SRVR_JOIN_SESSION in cloud.kinds())
    await cloud.until(lambda: cloud.states())

    join = next(m for m in cloud.received if m.message_type == qc.RNDR_SRVR_JOIN_SESSION)
    assert join.rndr_srvr_join_session.is_active
    assert cloud.tokens[-1] == QCONNECT_JWT
    assert "selected in the Qobuz app" in harness.service.status_markdown()
    await harness.close()


async def test_a_track_cast_from_the_app_plays_on_the_output_and_is_reported(cloud, caplog):
    harness = Harness(cloud)
    harness.service.session_ready(harness.link, handed_over=True)
    await cloud.until(lambda: cloud.states())

    await cloud.push(
        _set_state(
            playing_state=PLAYING,
            current_position=0,
            current_track=qc.QueueTrackWithContext(queue_item_id=1, track_id=5966783),
        )
    )
    await cloud.until(lambda: harness.direct.holds and harness.direct.hold.calls)
    await cloud.until(lambda: any(s.current_queue_item_id == 1 for s in cloud.states()))

    hold = harness.direct.hold
    assert hold.calls[0][0] == "play"
    assert hold.calls[0][1] == "https://streaming.qobuz.test/5966783"
    assert "playing *Synthetic song 5966783*" in harness.service.status_markdown()

    await harness.service.stop()
    await cloud.until(lambda: cloud.states()[-1].playing_state == STOPPED)
    assert hold.calls[-1] == ("release",)
    await harness.client.aclose()
    assert_no_secrets(caplog.text)


async def test_a_stored_session_joins_as_available_after_a_restart(cloud):
    harness = Harness(cloud)

    harness.service.session_ready(harness.link, handed_over=False)
    await cloud.until(lambda: qc.RNDR_SRVR_JOIN_SESSION in cloud.kinds())

    join = next(m for m in cloud.received if m.message_type == qc.RNDR_SRVR_JOIN_SESSION)
    assert not join.rndr_srvr_join_session.is_active
    assert harness.direct.holds == []
    await harness.close()


async def test_a_newer_handoff_reconnects_with_its_token(cloud):
    harness = Harness(cloud)
    harness.service.session_ready(harness.link, handed_over=False)
    await cloud.until(lambda: cloud.tokens == [QCONNECT_JWT])

    newer = LinkState(
        device_uuid=DEVICE_UUID,
        linked=True,
        credential=uat(),
        session=SessionToken(jwt=RENEWED_QCONNECT_JWT, endpoint=cloud.url),
    )
    harness.service.session_ready(newer, handed_over=True)
    await cloud.until(lambda: RENEWED_QCONNECT_JWT in cloud.tokens)

    await harness.close()


async def test_a_session_about_to_expire_is_renewed_with_the_api_token(cloud):
    harness = Harness(
        cloud,
        exp=int(time.time()) + 60,
        bearer_credential=bearer(API_JWT),
        refresh={"jwt_qws": {"jwt": RENEWED_QCONNECT_JWT, "exp": int(time.time()) + 7200}},
    )

    harness.service.session_ready(harness.link, handed_over=False)
    await cloud.until(lambda: cloud.tokens)

    assert cloud.tokens == [RENEWED_QCONNECT_JWT]
    [request] = harness.refresh_requests
    assert request.headers["Authorization"] == f"Bearer {API_JWT}"
    assert dict(httpx.QueryParams(request.content.decode())) == {"jwt": "jwt_qws"}
    assert [t.jwt for t in harness.persisted] == [RENEWED_QCONNECT_JWT]
    await harness.close()


async def test_a_handoff_during_a_renewal_wins_over_it(cloud):
    harness = Harness(cloud, exp=int(time.time()) + 60, bearer_credential=bearer(API_JWT))
    answer = asyncio.Event()

    async def slow_refresh(request):
        harness.refresh_requests.append(request)
        await answer.wait()
        renewed = {"jwt": "renewed-previous-session", "exp": int(time.time()) + 7200}
        return httpx.Response(200, json={"jwt_qws": renewed})

    harness.service._make_http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(slow_refresh))
    harness.service.session_ready(harness.link, handed_over=False)
    await cloud.until(lambda: harness.refresh_requests)

    newer = LinkState(
        device_uuid=DEVICE_UUID,
        linked=True,
        credential=uat(),
        session=SessionToken(jwt=RENEWED_QCONNECT_JWT, endpoint=cloud.url),
    )
    harness.service.session_ready(newer, handed_over=True)
    answer.set()
    await cloud.until(lambda: cloud.states())

    assert set(cloud.tokens) == {RENEWED_QCONNECT_JWT}
    assert harness.persisted == []
    assert "choose" not in harness.service.status_markdown()
    await harness.close()


async def test_an_expired_session_waits_for_the_app(cloud):
    harness = Harness(cloud, exp=int(time.time()) - 3600, bearer_credential=None)

    harness.service.session_ready(harness.link, handed_over=False)
    await cloud.until(lambda: "choose *Kalinka (test)*" in harness.service.status_markdown())

    assert cloud.tokens == []
    await harness.close()


async def test_a_fresh_handoff_recovers_an_expired_session_and_can_play(cloud):
    harness = Harness(cloud, exp=int(time.time()) - 3600)
    harness.service.session_ready(harness.link, handed_over=False)
    await cloud.until(lambda: "choose *Kalinka (test)*" in harness.service.status_markdown())
    newer = harness.link.with_session(
        SessionToken(jwt=RENEWED_QCONNECT_JWT, endpoint=cloud.url), None
    )

    harness.service.session_ready(newer, handed_over=True)
    await cloud.until(lambda: cloud.states())
    await cloud.push(_set_state(
        playing_state=PLAYING,
        current_track=qc.QueueTrackWithContext(queue_item_id=1, track_id=5966783),
    ))
    await cloud.until(lambda: harness.direct.holds and harness.direct.hold.calls)

    assert cloud.tokens == [RENEWED_QCONNECT_JWT]
    assert harness.direct.hold.calls[0][0] == "play"
    await harness.close()


async def test_an_expired_session_reconnects_with_the_renewed_api_credential(cloud):
    now = int(time.time())
    harness = Harness(
        cloud, exp=now + 3600,
        refresh={"jwt_qws": {"jwt": RENEWED_QCONNECT_JWT, "exp": now + 10800}},
    )
    harness.service._clock = lambda: now
    harness.service.session_ready(harness.link, handed_over=True)
    await cloud.until(lambda: cloud.states())
    await cloud.push(_set_state(
        playing_state=PLAYING,
        current_track=qc.QueueTrackWithContext(queue_item_id=1, track_id=5966783),
    ))
    await cloud.until(lambda: harness.direct.holds and harness.direct.hold.calls)

    now += 7200
    harness.service._bearer = lambda: bearer(RENEWED_JWT, now + 3600)
    await cloud.drop()
    await cloud.until(lambda: RENEWED_QCONNECT_JWT in cloud.tokens)

    assert harness.refresh_requests[0].headers["Authorization"] == f"Bearer {RENEWED_JWT}"
    assert "choose" not in harness.service.status_markdown()
    assert harness.direct.hold.active
    assert len(harness.direct.holds) == 1
    await harness.close()


async def test_an_unlinked_account_leaves_the_session(cloud):
    harness = Harness(cloud)
    harness.service.session_ready(harness.link, handed_over=True)
    await cloud.until(lambda: cloud.states())

    harness.service.session_ended()
    await cloud.until(lambda: harness.service.status_markdown() == "")

    await harness.close()


async def test_a_link_from_before_connect_playback_asks_for_the_app(cloud):
    harness = Harness(cloud)
    older = LinkState(device_uuid=DEVICE_UUID, linked=True, credential=uat())

    harness.service.session_ready(older, handed_over=False)

    assert "choose *Kalinka (test)* in the Qobuz app" in harness.service.status_markdown()
    assert cloud.tokens == []
    await harness.close()
