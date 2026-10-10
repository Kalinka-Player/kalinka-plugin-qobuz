"""Pairing: one validated account at a time, and a lock only unpairing opens."""

import asyncio
import os
import stat
import time

import httpx
import pytest

from kalinka_plugin_sdk.module_health import ModuleHealthState

from kalinka_plugin_qobuz.account import AccessCheckError, Validated
from kalinka_plugin_qobuz.auth import AuthenticationError, QobuzAuth, TokenHolder
from kalinka_plugin_qobuz.connect.pairing import PairingService, Phase
from kalinka_plugin_qobuz.connect.refresh import TokenRefresher
from kalinka_plugin_qobuz.connect.session_token import SessionToken
from kalinka_plugin_qobuz.connect.store import LinkState, LinkStore
from kalinka_plugin_qobuz.qobuz import QobuzClient

from conftest import (
    ACCOUNT,
    API_JWT,
    APP_ID,
    BUNDLE,
    GOOD_SECRET,
    ISSUED_UAT,
    OTHER_ACCOUNT,
    OTHER_JWT,
    OTHER_UAT,
    QCONNECT_JWT,
    RENEWED_JWT,
    RENEWED_QCONNECT_JWT,
    FakeAdvertiser,
    FakeReceiver,
    FakeRefresher,
    FakeSessionSink,
    assert_no_secrets,
    bearer,
    handoff_body,
    settle,
    uat,
)


class _Validator:
    """Answers validation per token, exchanging it as Qobuz does; ``gate``
    holds it until released."""

    def __init__(self):
        self.results = {
            API_JWT: (ACCOUNT, uat()),
            OTHER_JWT: (OTHER_ACCOUNT, uat(OTHER_UAT)),
            ISSUED_UAT: (ACCOUNT, uat()),
        }
        self.calls = []
        self.gate = None

    async def __call__(self, client, holder):
        token = holder.current().token
        self.calls.append(token)
        if self.gate is not None:
            await self.gate.wait()
        result = self.results[token]
        if isinstance(result, Exception):
            raise result
        account, credential = result
        holder.install(credential)
        client.sec = GOOD_SECRET
        return Validated(account=account, credential=credential)


class _Harness:
    def __init__(self, tmp_path, *, receiver_failure=None, bundle_failures=0, sessions=None):
        FakeRefresher.instances = []
        self.sessions = sessions
        self.accounts = {API_JWT: ACCOUNT, OTHER_JWT: OTHER_ACCOUNT}
        self.store = LinkStore(str(tmp_path / "qobuz" / "connect.json"))
        self.holder = TokenHolder()
        self.client = QobuzClient(auth=QobuzAuth(self.holder), transport=_unreachable())
        self.validator = _Validator()
        self.advertiser = FakeAdvertiser()
        self.receivers = []
        self.receiver_failure = receiver_failure
        self.bundle_failures = bundle_failures
        self.sleeps = []

    def service(self, *, make_refresher=FakeRefresher) -> PairingService:
        return PairingService(
            client=self.client,
            holder=self.holder,
            store=self.store,
            device_name="Kalinka (test)",
            port=8183,
            load_bundle=self._bundle,
            validate=self.validator,
            identify=self._identify,
            sessions=self.sessions,
            make_probe_client=lambda holder: QobuzClient(auth=QobuzAuth(holder), transport=_unreachable()),
            make_receiver=self._receiver,
            advertiser=self.advertiser,
            make_refresher=make_refresher,
            sleep=self._sleep,
        )

    async def _identify(self, client, credential):
        return self.accounts[credential.token]

    async def _bundle(self):
        if self.bundle_failures:
            self.bundle_failures -= 1
            raise httpx.ConnectError("offline")
        return BUNDLE

    def _receiver(self, handlers, port):
        failure, self.receiver_failure = self.receiver_failure, None
        receiver = FakeReceiver(handlers, port, fail=failure)
        self.receivers.append(receiver)
        return receiver

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    def link(self, credential=None, account=ACCOUNT) -> LinkState:
        state = LinkState(
            device_uuid=self.store.load_or_create().device_uuid,
            linked=True,
            linked_at=1,
            credential=credential or bearer(API_JWT, 0),
            account=account,
        )
        self.store.save(state)
        return state


def _unreachable():
    def refuse(request):
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.MockTransport(refuse)


async def _waiting(harness) -> PairingService:
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.WAITING)
    return service


@pytest.mark.asyncio
async def test_an_unlinked_player_opens_pairing(tmp_path):
    harness = _Harness(tmp_path)
    service = await _waiting(harness)

    assert harness.receivers[0].started
    advert = harness.advertiser.adverts[0]
    assert advert.device_uuid == service.link.device_uuid
    assert service.connect_info() == {"current_session_id": "", "app_id": APP_ID}
    assert service.display_info()["serial_number"] == service.link.device_uuid
    with pytest.raises(AuthenticationError):
        harness.holder.current()
    health = service.health()
    assert health.state is ModuleHealthState.ERROR
    assert health.message.startswith("Not linked")
    await service.stop()


@pytest.mark.asyncio
async def test_a_valid_handoff_links_and_closes_pairing(tmp_path):
    harness = _Harness(tmp_path)
    service = await _waiting(harness)

    status, body = await service.handoff(handoff_body())
    assert (status, body) == (200, {})
    await settle(lambda: not harness.advertiser.running and harness.receivers[0].stopped)

    assert service.phase is Phase.LINKED
    assert harness.holder.current() == uat()
    assert (harness.client.user_id, harness.client.credential_id) == (1705826, 42)
    assert harness.client.sec == GOOD_SECRET
    stored = harness.store.load_or_create()
    assert stored.linked and stored.account == ACCOUNT and stored.credential == uat()
    assert stat.S_IMODE(os.stat(harness.store.path).st_mode) == 0o600
    assert FakeRefresher.instances == []
    assert service.health().state is ModuleHealthState.READY
    await service.stop()


@pytest.mark.asyncio
async def test_without_an_issued_token_the_bearer_is_linked_and_renewed(tmp_path):
    harness = _Harness(tmp_path)
    harness.validator.results[API_JWT] = (ACCOUNT, bearer(API_JWT, 4102444800))
    service = await _waiting(harness)

    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED)

    assert harness.holder.current() == bearer(API_JWT, 4102444800)
    assert harness.store.load_or_create().credential == bearer(API_JWT, 4102444800)
    assert FakeRefresher.instances[0].started
    await service.stop()


@pytest.mark.asyncio
async def test_once_linked_every_further_handoff_is_refused(tmp_path):
    harness = _Harness(tmp_path)
    service = await _waiting(harness)
    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED)

    status, body = await service.handoff(handoff_body(api_jwt=OTHER_JWT, session_id="second-phone"))

    assert status == 400 and body == {"error": "device is not accepting pairing"}
    assert harness.holder.current() == uat()
    assert harness.store.load_or_create().account == ACCOUNT
    assert harness.validator.calls == [API_JWT]
    await service.stop()


@pytest.mark.asyncio
async def test_a_failed_check_leaves_nothing_installed_and_pairing_open(tmp_path):
    harness = _Harness(tmp_path)
    harness.validator.results[API_JWT] = AccessCheckError("favourites", "HTTP 403", status=403)
    service = await _waiting(harness)

    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.WAITING and harness.validator.calls)

    with pytest.raises(AuthenticationError):
        harness.holder.current()
    assert not harness.store.load_or_create().linked
    assert harness.advertiser.running
    status = service.status_markdown()
    assert "Last attempt failed: favourites: HTTP 403" in status
    assert_no_secrets(status)
    health = service.health()
    assert health.state is ModuleHealthState.ERROR
    assert "Last attempt failed: favourites: HTTP 403" in health.message
    await service.stop()


@pytest.mark.asyncio
async def test_a_repeated_handoff_is_one_pairing_and_a_different_one_waits_its_turn(tmp_path):
    harness = _Harness(tmp_path)
    harness.validator.gate = asyncio.Event()
    service = await _waiting(harness)

    first = await service.handoff(handoff_body())
    repeat = await service.handoff(handoff_body())
    other = await service.handoff(handoff_body(api_jwt=OTHER_JWT, session_id="other-phone"))
    assert service.connect_info()["current_session_id"] == "sess-1234-abcd"
    harness.validator.gate.set()
    await settle(lambda: service.phase is Phase.LINKED)

    assert first == repeat == (200, {})
    assert other == (400, {"error": "pairing already in progress"})
    assert harness.validator.calls == [API_JWT]
    assert harness.store.load_or_create().account == ACCOUNT
    await service.stop()


@pytest.mark.asyncio
async def test_simultaneous_handoffs_link_exactly_one_account(tmp_path):
    harness = _Harness(tmp_path)
    service = await _waiting(harness)

    results = await asyncio.gather(
        service.handoff(handoff_body()),
        service.handoff(handoff_body(api_jwt=OTHER_JWT, session_id="other-phone")),
    )
    await settle(lambda: service.phase is Phase.LINKED)

    assert sorted(status for status, _ in results) == [200, 400]
    assert harness.validator.calls == [API_JWT]
    await service.stop()


@pytest.mark.asyncio
async def test_a_check_that_outlives_stop_installs_nothing(tmp_path):
    harness = _Harness(tmp_path)
    harness.validator.gate = asyncio.Event()
    service = await _waiting(harness)
    await service.handoff(handoff_body())
    await settle(lambda: harness.validator.calls)

    await service.stop()
    harness.validator.gate.set()
    await asyncio.sleep(0)

    assert harness.holder.credential is None
    assert not harness.store.load_or_create().linked
    assert not harness.advertiser.running and harness.receivers[0].stopped


@pytest.mark.asyncio
async def test_an_unsavable_link_is_not_installed(tmp_path, monkeypatch):
    harness = _Harness(tmp_path)
    service = await _waiting(harness)

    def full_disk(state):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(harness.store, "save", full_disk)
    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.WAITING and harness.validator.calls)

    assert harness.holder.credential is None
    assert "could not save the link (No space left on device)" in service.status_markdown()
    await service.stop()


@pytest.mark.asyncio
async def test_a_stored_link_is_restored_without_opening_pairing(tmp_path):
    harness = _Harness(tmp_path)
    harness.link()
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.LINKED)

    assert harness.receivers == [] and harness.advertiser.adverts == []
    assert harness.client.user_id == 1705826
    assert FakeRefresher.instances[0].started
    assert harness.holder.current() == uat()
    assert harness.store.load_or_create().credential == uat()
    await service.stop()


@pytest.mark.asyncio
async def test_a_stored_user_auth_token_is_restored_without_renewal(tmp_path):
    harness = _Harness(tmp_path)
    harness.link(credential=uat())
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.LINKED)

    assert harness.holder.current() == uat()
    assert FakeRefresher.instances == []
    await service.stop()


@pytest.mark.asyncio
async def test_a_rejected_stored_link_expires_but_stays_locked(tmp_path):
    harness = _Harness(tmp_path)
    harness.link()
    harness.validator.results[API_JWT] = AccessCheckError("account", "HTTP 401", status=401)
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.EXPIRED)

    assert harness.receivers == [] and harness.advertiser.adverts == []
    with pytest.raises(AuthenticationError, match="expired"):
        harness.holder.current()
    assert harness.store.load_or_create().linked
    assert "Unpair Qobuz account on next restart" in service.status_markdown()
    assert service.health().state is ModuleHealthState.ERROR
    status, _ = await service.handoff(handoff_body(api_jwt=OTHER_JWT))
    assert status == 400
    await service.stop()


@pytest.mark.asyncio
async def test_a_stored_link_waits_out_a_network_outage(tmp_path):
    harness = _Harness(tmp_path)
    harness.link()
    outage = [httpx.ConnectError("down"), httpx.ConnectError("down")]
    real = harness.validator
    during_outage = []

    async def flaky(client, holder):
        if outage:
            during_outage.append(service.health().state)
            raise outage.pop(0)
        return await real(client, holder)

    harness.validator = flaky
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.LINKED)

    assert harness.sleeps == [2, 4]
    assert harness.receivers == []
    assert during_outage == [ModuleHealthState.WARNING] * 2
    await service.stop()


@pytest.mark.asyncio
async def test_renewal_giving_up_expires_the_link_and_keeps_pairing_closed(tmp_path):
    harness = _Harness(tmp_path)
    harness.link(credential=bearer(API_JWT, 4102444800))
    harness.validator.results[API_JWT] = (ACCOUNT, bearer(API_JWT, 4102444800))
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.LINKED)

    FakeRefresher.instances[0].on_expired("Qobuz refused to renew the link")

    assert service.phase is Phase.EXPIRED
    assert harness.holder.credential is None
    assert harness.receivers == []
    assert harness.store.load_or_create().linked
    await service.stop()


@pytest.mark.asyncio
async def test_a_renewed_credential_is_stored(tmp_path):
    harness = _Harness(tmp_path)
    harness.link(credential=bearer(API_JWT, 4102444800))
    harness.validator.results[API_JWT] = (ACCOUNT, bearer(API_JWT, 4102444800))
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.LINKED)

    FakeRefresher.instances[0].persist(bearer(OTHER_JWT, 4102448400))

    assert harness.store.load_or_create().credential == bearer(OTHER_JWT, 4102448400)
    await service.stop()


@pytest.mark.asyncio
async def test_an_incomplete_stored_link_expires(tmp_path):
    harness = _Harness(tmp_path)
    harness.store.save(LinkState(device_uuid=harness.store.load_or_create().device_uuid, linked=True))
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.EXPIRED)

    assert harness.receivers == []
    await service.stop()


@pytest.mark.asyncio
async def test_unpair_then_a_new_pairing_links_another_account(tmp_path):
    harness = _Harness(tmp_path)
    old = harness.link()
    service = harness.service()

    service.forget_link()
    service.start()
    await settle(lambda: service.phase is Phase.WAITING)
    await service.handoff(handoff_body(api_jwt=OTHER_JWT, session_id="new-phone"))
    await settle(lambda: service.phase is Phase.LINKED)

    stored = harness.store.load_or_create()
    assert stored.device_uuid == old.device_uuid
    assert stored.account == OTHER_ACCOUNT
    assert harness.holder.current() == uat(OTHER_UAT)
    await service.stop()


@pytest.mark.asyncio
async def test_pairing_waits_for_the_network_before_advertising(tmp_path):
    harness = _Harness(tmp_path, bundle_failures=2)
    service = harness.service()
    service.start()
    await settle(lambda: service.phase is Phase.WAITING)

    assert harness.sleeps == [2, 4]
    assert harness.client.id == APP_ID
    await service.stop()


@pytest.mark.asyncio
async def test_a_taken_port_is_reported_and_retried(tmp_path):
    harness = _Harness(tmp_path, receiver_failure=OSError(98, "Address already in use"))
    service = harness.service()
    service.start()
    await settle(lambda: harness.sleeps)

    assert service.phase is Phase.UNAVAILABLE
    assert "cannot listen on port 8183 (Address already in use)" in service.status_markdown()
    await settle(lambda: service.phase is Phase.WAITING)
    await service.stop()


@pytest.mark.parametrize(
    "prepare",
    ["waiting", "linked", "expired"],
)
@pytest.mark.asyncio
async def test_status_text_never_carries_a_token(tmp_path, prepare):
    harness = _Harness(tmp_path)
    if prepare != "waiting":
        harness.link()
    if prepare == "expired":
        harness.validator.results[API_JWT] = AccessCheckError("account", "HTTP 401", status=401)
    service = harness.service()
    service.start()
    await settle(lambda: service.phase in (Phase.WAITING, Phase.LINKED, Phase.EXPIRED))

    assert_no_secrets(service.status_markdown(), str(service.health()))
    await service.stop()


@pytest.mark.asyncio
async def test_with_connect_playback_pairing_stays_open_and_hands_the_session_over(tmp_path):
    sink = FakeSessionSink()
    harness = _Harness(tmp_path, sessions=sink)
    service = await _waiting(harness)

    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED and sink.ready)

    [(session, handed_over)] = sink.ready
    assert handed_over and session.jwt == QCONNECT_JWT
    assert session.session_id == "sess-1234-abcd"
    assert harness.advertiser.running and not harness.receivers[0].stopped
    stored = harness.store.load_or_create()
    assert stored.session.jwt == QCONNECT_JWT
    assert stored.api_bearer.token == API_JWT
    assert service.connect_info()["current_session_id"] == "sess-1234-abcd"
    await service.stop()


@pytest.mark.asyncio
async def test_a_restored_link_hands_its_session_over_and_listens(tmp_path):
    sink = FakeSessionSink()
    harness = _Harness(tmp_path, sessions=sink)
    state = harness.link(credential=uat())
    harness.store.save(state.with_session(SessionToken(jwt=QCONNECT_JWT, endpoint="wss://q"), None))
    service = harness.service()
    service.start()
    await settle(lambda: harness.advertiser.running)

    assert sink.ready == [(SessionToken(jwt=QCONNECT_JWT, endpoint="wss://q"), False)]
    assert service.phase is Phase.LINKED
    assert harness.holder.current() == uat()
    await service.stop()


@pytest.mark.asyncio
async def test_a_linked_player_takes_its_accounts_next_session(tmp_path):
    sink = FakeSessionSink()
    harness = _Harness(tmp_path, sessions=sink)
    service = await _waiting(harness)
    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED and sink.ready)

    status, _ = await service.handoff(
        handoff_body(qconnect_jwt=RENEWED_QCONNECT_JWT, session_id="sess-next")
    )
    await settle(lambda: len(sink.ready) == 2)

    assert status == 200
    session, handed_over = sink.ready[-1]
    assert handed_over and session.jwt == RENEWED_QCONNECT_JWT
    assert harness.store.load_or_create().session.jwt == RENEWED_QCONNECT_JWT
    assert harness.holder.current() == uat()
    await service.stop()


@pytest.mark.asyncio
async def test_a_linked_player_refuses_another_accounts_session(tmp_path, caplog):
    sink = FakeSessionSink()
    harness = _Harness(tmp_path, sessions=sink)
    service = await _waiting(harness)
    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED and sink.ready)

    await service.handoff(handoff_body(api_jwt=OTHER_JWT, qconnect_jwt=RENEWED_QCONNECT_JWT))
    await settle(lambda: "linked to another Qobuz account" in caplog.text)

    assert len(sink.ready) == 1
    assert harness.store.load_or_create().session.jwt == QCONNECT_JWT
    assert harness.store.load_or_create().account == ACCOUNT
    assert_no_secrets(caplog.text)
    await service.stop()


@pytest.mark.asyncio
async def test_without_connect_playback_a_linked_player_refuses_handoffs(tmp_path):
    harness = _Harness(tmp_path)
    service = await _waiting(harness)
    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED)

    status, body = await service.handoff(handoff_body(qconnect_jwt=RENEWED_QCONNECT_JWT))

    assert status == 400 and body == {"error": "device is not accepting pairing"}
    await service.stop()


@pytest.mark.asyncio
async def test_unpairing_and_expiry_end_the_session(tmp_path):
    sink = FakeSessionSink()
    harness = _Harness(tmp_path, sessions=sink)
    harness.link()
    service = harness.service()

    service.forget_link()

    assert sink.ended == 1

    harness.link()
    expiring = harness.service()
    expiring._link = harness.store.load_or_create()
    expiring._expire("Qobuz refused the stored link")
    assert sink.ended == 2


@pytest.mark.asyncio
async def test_an_expired_link_stops_offering_the_player(tmp_path):
    sink = FakeSessionSink()
    harness = _Harness(tmp_path, sessions=sink)
    service = await _waiting(harness)
    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED and sink.ready)
    assert harness.advertiser.running

    service._expire("Qobuz refused the stored link")
    await settle(lambda: not harness.advertiser.running and harness.receivers[0].stopped)

    assert service.phase is Phase.EXPIRED
    assert sink.ended == 1
    status, _ = await service.handoff(handoff_body())
    assert status == 400
    await service.stop()


@pytest.mark.asyncio
async def test_a_link_that_expires_while_listening_is_retried_is_not_offered(tmp_path):
    sink = FakeSessionSink()
    harness = _Harness(tmp_path, sessions=sink, receiver_failure=OSError(98, "Address already in use"))
    state = harness.link(credential=uat())
    harness.store.save(state.with_session(SessionToken(jwt=QCONNECT_JWT, endpoint="wss://q"), None))
    retry = asyncio.Event()

    async def sleep(seconds):
        harness.sleeps.append(seconds)
        await retry.wait()

    harness._sleep = sleep
    service = harness.service()
    service.start()
    await settle(lambda: harness.sleeps)

    service._expire("Qobuz refused the stored link")
    retry.set()
    await asyncio.sleep(0.05)

    assert len(harness.receivers) == 1
    assert not harness.advertiser.running
    await service.stop()


@pytest.mark.asyncio
async def test_the_api_token_is_offered_only_while_it_lives(tmp_path):
    harness = _Harness(tmp_path, sessions=FakeSessionSink())
    service = harness.service()

    harness.holder.install(bearer(API_JWT))
    assert service.api_bearer() == bearer(API_JWT)

    harness.holder.install(uat())
    service._link = service.link.with_session(None, bearer(OTHER_JWT, exp=4102444800))
    assert service.api_bearer() == bearer(OTHER_JWT, exp=4102444800)

    service._link = service.link.with_session(None, bearer(OTHER_JWT, exp=1))
    assert service.api_bearer() is None


@pytest.mark.asyncio
async def test_a_renewed_session_is_stored(tmp_path):
    harness = _Harness(tmp_path, sessions=FakeSessionSink())
    harness.link()
    service = harness.service()

    service.store_session(SessionToken(jwt=RENEWED_QCONNECT_JWT, endpoint="wss://q", exp=99))

    assert harness.store.load_or_create().session.jwt == RENEWED_QCONNECT_JWT
    assert harness.store.load_or_create().linked


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", [False, True])
async def test_connect_renews_its_bearer_while_rest_keeps_its_persistent_token(tmp_path, restored):
    harness = _Harness(tmp_path, sessions=FakeSessionSink())
    if restored:
        state = harness.link(credential=uat())
        harness.store.save(state.with_session(
            SessionToken(jwt=QCONNECT_JWT, endpoint="wss://q"),
            bearer(API_JWT, 4102444800),
        ))
        service = harness.service()
        service.start()
    else:
        service = await _waiting(harness)
        await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED)

    [refresher] = FakeRefresher.instances
    assert refresher.started
    assert refresher.holder is not harness.holder
    renewed = bearer(RENEWED_JWT, 4102448400)
    refresher.persist(renewed)
    refresher.holder.install(renewed)

    assert service.api_bearer() == renewed
    stored = harness.store.load_or_create()
    assert stored.api_bearer == renewed
    assert stored.session.jwt == QCONNECT_JWT
    assert stored.credential == harness.holder.current() == uat()
    await service.stop()
    assert refresher.stopped


@pytest.mark.asyncio
async def test_a_new_handoff_replaces_the_connect_renewal_credential(tmp_path):
    harness = _Harness(tmp_path, sessions=FakeSessionSink())
    harness.accounts[RENEWED_JWT] = ACCOUNT
    service = await _waiting(harness)
    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED)
    [refresher] = FakeRefresher.instances
    generation = refresher.holder.generation

    await service.handoff(handoff_body(api_jwt=RENEWED_JWT, api_exp=4102448400))
    await settle(lambda: len(harness.sessions.ready) == 2)

    assert refresher.holder.current() == bearer(RENEWED_JWT, 4102448400)
    assert refresher.holder.generation > generation
    assert harness.holder.current() == uat()
    assert len(FakeRefresher.instances) == 1
    await service.stop()


@pytest.mark.asyncio
async def test_connect_renewal_expiry_preserves_the_account_and_accepts_a_new_handoff(tmp_path):
    harness = _Harness(tmp_path, sessions=FakeSessionSink())
    harness.accounts[RENEWED_JWT] = ACCOUNT
    service = await _waiting(harness)
    await service.handoff(handoff_body())
    await settle(lambda: service.phase is Phase.LINKED)
    [refresher] = FakeRefresher.instances

    refresher.on_expired("renewal refused")

    assert service.phase is Phase.LINKED
    assert service.api_bearer() is None
    assert harness.store.load_or_create().api_bearer is None
    assert harness.holder.current() == uat()
    assert harness.advertiser.running
    assert harness.sessions.ended == 0
    await service.handoff(handoff_body(api_jwt=RENEWED_JWT, api_exp=4102448400))
    await settle(lambda: len(harness.sessions.ready) == 2)
    assert service.api_bearer() == bearer(RENEWED_JWT, 4102448400)
    await service.stop()


@pytest.mark.asyncio
async def test_connects_renewed_api_token_outlives_the_original_handoff(tmp_path, caplog):
    now = int(time.time())
    harness = _Harness(tmp_path, sessions=FakeSessionSink())
    state = harness.link(credential=uat())
    harness.store.save(state.with_session(
        SessionToken(jwt=QCONNECT_JWT, endpoint="wss://q", exp=now + 60),
        bearer(API_JWT, now + 60),
    ))
    requests = []

    def refresh(request):
        requests.append(request)
        return httpx.Response(200, json={"jwt_api": {"jwt": RENEWED_JWT, "exp": now + 3600}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as http:
        def make_refresher(**kwargs):
            return TokenRefresher(**{**kwargs, "http": http, "jitter": lambda: 0})

        service = harness.service(make_refresher=make_refresher)
        service._clock = lambda: now
        service.start()
        try:
            await settle(lambda: service.phase is Phase.LINKED and requests)
            assert requests[0].headers["Authorization"] == f"Bearer {API_JWT}"
            assert requests[0].content == b"jwt=jwt_api"
            now += 120  # Both credentials handed over by the app have expired.
            assert service.api_bearer() == bearer(RENEWED_JWT, now - 120 + 3600)
            assert harness.store.load_or_create().api_bearer == service.api_bearer()
            assert harness.holder.current() == uat()
            assert_no_secrets(caplog.text)
        finally:
            await service.stop()
