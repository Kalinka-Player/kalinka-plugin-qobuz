"""Renewal of a linked Bearer credential: on schedule, after a 401, and never stale."""

import asyncio

import httpx
import pytest

from kalinka_plugin_qobuz.auth import Credential, CredentialKind, TokenHolder
from kalinka_plugin_qobuz.connect import refresh
from kalinka_plugin_qobuz.connect.refresh import REFRESH_URL, TokenRefresher

from conftest import API_JWT, APP_ID, OTHER_JWT, RENEWED_JWT, assert_no_secrets, bearer

NOW = 1_700_000_000.0
EXP = int(NOW) + 3600


class _Time:
    def __init__(self):
        self.now = NOW
        self.sleeps = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class _RefreshApi:
    """qws/refreshToken: answers from a script, one entry per call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.during_request = None

    def handle(self, request):
        self.requests.append(request)
        if self.during_request:
            self.during_request()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, json={"jwt_api": answer})


def _refresher(api, holder, clock, *, events=None):
    events = events if events is not None else []
    return TokenRefresher(
        holder=holder,
        http=httpx.AsyncClient(transport=httpx.MockTransport(api.handle)),
        app_id=APP_ID,
        persist=lambda credential: events.append(("persist", credential, holder.credential)),
        on_expired=lambda reason: events.append(("expired", reason)),
        clock=clock.clock,
        sleep=clock.sleep,
        jitter=lambda: 0.0,
    ), events


def _holder(credential):
    holder = TokenHolder()
    holder.install(credential)
    return holder


@pytest.mark.asyncio
async def test_renewal_is_due_five_minutes_before_expiry_and_stored_before_use():
    clock = _Time()
    api = _RefreshApi({"jwt": RENEWED_JWT, "exp": 0})
    holder = _holder(bearer(API_JWT, EXP))
    refresher, events = _refresher(api, holder, clock)

    await refresher._run()

    assert clock.sleeps == [3600 - 300]
    request = api.requests[0]
    assert str(request.url) == REFRESH_URL
    assert request.headers["Authorization"] == f"Bearer {API_JWT}"
    assert request.headers["X-App-Id"] == APP_ID
    assert request.content == b"jwt=jwt_api"
    renewed = bearer(RENEWED_JWT, 0)
    assert events == [("persist", renewed, bearer(API_JWT, EXP))]
    assert holder.credential == renewed


@pytest.mark.asyncio
async def test_failures_back_off_then_expire_after_the_grace_period(caplog):
    clock = _Time()
    clock.now = EXP - 300
    api = _RefreshApi(503)
    holder = _holder(bearer(API_JWT, EXP))
    refresher, events = _refresher(api, holder, clock)

    await refresher._run()

    assert clock.sleeps[:5] == [15, 30, 60, 120, 300]
    assert clock.now > EXP + refresh.GRACE_S
    assert events == [("expired", refresh.EXPIRED_REASON)]
    assert holder.credential == bearer(API_JWT, EXP)
    assert_no_secrets(caplog.text)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_a_refused_renewal_ends_the_link_at_once(status):
    clock = _Time()
    clock.now = EXP - 60
    refresher, events = _refresher(_RefreshApi(status), _holder(bearer(API_JWT, EXP)), clock)

    await refresher._run()

    assert events == [("expired", refresh.EXPIRED_REASON)]


@pytest.mark.asyncio
async def test_a_network_error_is_retried():
    clock = _Time()
    clock.now = EXP - 60
    api = _RefreshApi(httpx.ConnectError("down"), {"jwt": RENEWED_JWT, "exp": 0})
    holder = _holder(bearer(API_JWT, EXP))
    refresher, _ = _refresher(api, holder, clock)

    await refresher._run()

    assert clock.sleeps == [15]
    assert holder.credential.token == RENEWED_JWT


@pytest.mark.asyncio
async def test_a_renewal_finishing_after_the_link_changed_is_discarded():
    clock = _Time()
    clock.now = EXP - 60
    api = _RefreshApi({"jwt": RENEWED_JWT, "exp": 0})
    holder = _holder(bearer(API_JWT, EXP))
    api.during_request = lambda: holder.install(bearer(OTHER_JWT, 0))
    refresher, events = _refresher(api, holder, clock)

    await refresher._run()

    assert holder.credential == bearer(OTHER_JWT, 0)
    assert events == []


@pytest.mark.parametrize("answer", [401, 403, 503, httpx.ConnectError("down")])
@pytest.mark.asyncio
async def test_a_failed_old_renewal_cannot_expire_a_new_handoff(answer):
    clock = _Time()
    clock.now = EXP + refresh.GRACE_S + 1
    holder = _holder(bearer(API_JWT, EXP))
    api = _RefreshApi(answer)
    api.during_request = lambda: holder.install(bearer(OTHER_JWT, 0))
    refresher, events = _refresher(api, holder, clock)

    await refresher._run()

    assert events == []
    assert holder.credential == bearer(OTHER_JWT, 0)


@pytest.mark.asyncio
async def test_a_renewal_after_unpairing_restores_nothing():
    clock = _Time()
    clock.now = EXP - 60
    api = _RefreshApi({"jwt": RENEWED_JWT, "exp": 0})
    holder = _holder(bearer(API_JWT, EXP))
    api.during_request = lambda: holder.clear("unpaired")
    refresher, events = _refresher(api, holder, clock)

    await refresher._run()

    assert holder.credential is None
    assert events == []


@pytest.mark.asyncio
async def test_no_expiry_means_no_schedule():
    clock = _Time()
    api = _RefreshApi(503)
    refresher, _ = _refresher(api, _holder(bearer(API_JWT, 0)), clock)

    await refresher._run()

    assert api.requests == [] and clock.sleeps == []


@pytest.mark.asyncio
async def test_a_401_renews_once_and_later_401s_wait_for_the_cooldown():
    clock = _Time()
    api = _RefreshApi({"jwt": RENEWED_JWT, "exp": 0}, 503)
    holder = _holder(bearer(API_JWT, 0))
    refresher, _ = _refresher(api, holder, clock)

    assert await refresher.on_unauthorized(bearer(API_JWT, 0)) is True
    assert holder.credential.token == RENEWED_JWT
    assert await refresher.on_unauthorized(holder.credential) is False
    assert len(api.requests) == 1


@pytest.mark.asyncio
async def test_a_401_for_an_already_renewed_credential_just_retries():
    clock = _Time()
    api = _RefreshApi(503)
    holder = _holder(bearer(RENEWED_JWT, 0))
    refresher, _ = _refresher(api, holder, clock)

    assert await refresher.on_unauthorized(bearer(API_JWT, 0)) is True
    assert api.requests == []


@pytest.mark.asyncio
async def test_a_user_auth_token_is_never_sent_for_renewal():
    clock = _Time()
    api = _RefreshApi(503)
    uat = Credential(CredentialKind.USER_AUTH_TOKEN, "uat-synthetic")
    refresher, _ = _refresher(api, _holder(uat), clock)

    assert await refresher.on_unauthorized(uat) is False
    await refresher._run()
    assert api.requests == []


@pytest.mark.asyncio
async def test_stop_cancels_a_pending_renewal():
    holder = _holder(bearer(API_JWT, int(NOW) + 10**6))
    refresher = TokenRefresher(
        holder=holder,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        app_id=APP_ID,
        persist=lambda credential: None,
        on_expired=lambda reason: None,
    )
    refresher.start()
    await asyncio.sleep(0)

    await refresher.stop()

    assert refresher._task is None


@pytest.mark.asyncio
async def test_a_renewed_expiry_in_milliseconds_is_read_as_seconds():
    clock = _Time()
    api = _RefreshApi({"jwt": RENEWED_JWT, "exp": (EXP + 3600) * 1000})
    holder = _holder(bearer(API_JWT, EXP))
    refresher, _ = _refresher(api, holder, clock)

    assert await refresher.on_unauthorized(bearer(API_JWT, EXP)) is True

    assert holder.credential == bearer(RENEWED_JWT, EXP + 3600)
