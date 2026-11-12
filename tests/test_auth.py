"""One place decides which credential header a Qobuz request carries."""

import asyncio

import httpx
import pytest

from kalinka_plugin_qobuz.auth import (
    AuthenticationError,
    Credential,
    CredentialKind,
    NOT_LINKED,
    QobuzAuth,
    TokenHolder,
)

from conftest import API_JWT, APP_ID, RENEWED_JWT, assert_no_secrets, bearer


def _linked(credential=None) -> TokenHolder:
    holder = TokenHolder()
    holder.install(credential or bearer())
    return holder


@pytest.mark.asyncio
async def test_a_linked_request_carries_the_bearer_and_the_app_id(api):
    client = api.client(_linked())

    await client.session.get(client.base + "user/login")

    sent = api.requests[0].headers
    assert sent["Authorization"] == f"Bearer {API_JWT}"
    assert sent["X-App-Id"] == APP_ID
    assert "X-User-Auth-Token" not in sent


@pytest.mark.asyncio
async def test_a_user_auth_token_rides_its_own_header_alone(api):
    client = api.client(_linked(Credential(CredentialKind.USER_AUTH_TOKEN, "uat-synthetic")))

    await client.session.get(client.base + "track/get")

    sent = api.requests[0].headers
    assert sent["X-User-Auth-Token"] == "uat-synthetic"
    assert "Authorization" not in sent


@pytest.mark.asyncio
async def test_an_unlinked_request_fails_before_anything_is_sent(api):
    holder = TokenHolder()
    holder.clear(NOT_LINKED)
    client = api.client(holder)

    with pytest.raises(AuthenticationError, match="not linked"):
        await client.session.get(client.base + "user/login")

    assert api.requests == []


@pytest.mark.asyncio
async def test_a_renewed_credential_reaches_the_next_request_in_place(api):
    api.valid_tokens.add(RENEWED_JWT)
    holder = _linked()
    client = api.client(holder)

    holder.install(bearer(RENEWED_JWT))
    await client.session.get(client.base + "user/login")

    assert api.requests[0].headers["Authorization"] == f"Bearer {RENEWED_JWT}"


@pytest.mark.asyncio
async def test_a_401_renews_and_retries_once_with_the_new_credential(api):
    api.valid_tokens = {RENEWED_JWT}
    holder = _linked()
    rejected = []

    async def renew(credential):
        rejected.append(credential)
        holder.install(bearer(RENEWED_JWT))
        return True

    client = api.client(holder, on_unauthorized=renew)
    response = await client.session.get(client.base + "user/login")

    assert response.status_code == 200
    assert rejected == [bearer()]
    assert [r.headers["Authorization"] for r in api.requests] == [
        f"Bearer {API_JWT}",
        f"Bearer {RENEWED_JWT}",
    ]


@pytest.mark.asyncio
async def test_a_401_that_cannot_be_renewed_is_returned_as_is(api):
    api.valid_tokens = set()

    async def cannot_renew(credential):
        return False

    client = api.client(_linked(), on_unauthorized=cannot_renew)
    response = await client.session.get(client.base + "user/login")

    assert response.status_code == 401
    assert len(api.requests) == 1


def test_a_credential_never_shows_its_token():
    credential = bearer(API_JWT, exp=123)

    assert_no_secrets(repr(credential), str(credential), f"{credential}")
    assert "exp=123" in repr(credential)


def test_the_holder_counts_every_change():
    holder = TokenHolder()
    holder.install(bearer())
    holder.clear(NOT_LINKED)

    assert holder.generation == 2
    with pytest.raises(AuthenticationError, match="not linked"):
        holder.current()


@pytest.mark.asyncio
async def test_waiting_for_a_credential_ends_when_one_is_installed_and_holds_once_cleared():
    holder = TokenHolder()
    waiting = asyncio.create_task(holder.wait_for_credential())
    await asyncio.sleep(0)
    assert not waiting.done()

    holder.install(bearer())
    await asyncio.wait_for(waiting, timeout=1)
    await asyncio.wait_for(holder.wait_for_credential(), timeout=1)

    holder.clear(NOT_LINKED)
    again = asyncio.create_task(holder.wait_for_credential())
    await asyncio.sleep(0)
    assert not again.done()
    again.cancel()


def test_a_sync_client_is_refused():
    with httpx.Client(auth=QobuzAuth(_linked()), transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        with pytest.raises(RuntimeError, match="AsyncClient"):
            client.get("https://qobuz.test/")
