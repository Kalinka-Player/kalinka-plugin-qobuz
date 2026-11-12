"""Offline stand-ins for Qobuz and for the pairing service's collaborators.

Every token here is synthetic. Tests assert these exact strings never reach
logs, exceptions or status text.
"""

import asyncio
import base64
import hashlib
import json
from typing import Optional

import httpx
import pytest

from kalinka_plugin_qobuz.account import AccountInfo
from kalinka_plugin_qobuz.auth import Credential, CredentialKind, QobuzAuth, TokenHolder
from kalinka_plugin_qobuz.qobuz import AppBundle, QobuzClient, QobuzInputModule


def make_jwt(claims: dict, signature: str = "c2lnbmF0dXJl") -> str:
    def encode(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'HS256', 'typ': 'JWT'})}.{encode(claims)}.{signature}"


API_JWT = make_jwt({"sub": "synthetic-user", "exp": 4102444800, "scope": "api"})
RENEWED_JWT = make_jwt({"sub": "synthetic-user", "exp": 4102448400, "scope": "api"}, "cmVuZXdlZA")
OTHER_JWT = make_jwt({"sub": "other-user", "exp": 4102444800, "scope": "api"}, "b3RoZXI")
QCONNECT_JWT = make_jwt({"sub": "synthetic-user", "aud": "qws"}, "cWNvbm5lY3Q")
RENEWED_QCONNECT_JWT = make_jwt({"sub": "synthetic-user", "aud": "qws", "n": 2}, "cmVuZXdlZHFj")
ISSUED_UAT = "synthetic-user-auth-token-0123456789abcdef"
OTHER_UAT = "synthetic-other-auth-token-fedcba9876543210"
SECRETS = (API_JWT, RENEWED_JWT, OTHER_JWT, QCONNECT_JWT, RENEWED_QCONNECT_JWT, ISSUED_UAT, OTHER_UAT)

APP_ID = "123456789"
GOOD_SECRET = "0123456789abcdef0123456789abcdef"
BUNDLE = AppBundle(app_id=APP_ID, secrets=["wrong-secret", GOOD_SECRET])

USER = {
    "id": 1705826,
    "login": "synthetic",
    "display_name": "Synthetic Listener",
    "credential": {"id": 42, "label": "Studio", "parameters": {"short_label": "Studio"}},
}
ACCOUNT = AccountInfo(user_id=1705826, credential_id=42, label="Studio", display_name="Synthetic Listener")
OTHER_ACCOUNT = AccountInfo(user_id=99, credential_id=7, label="Sublime", display_name="Other Listener")


def bearer(token: str = API_JWT, exp: int = 0) -> Credential:
    return Credential(CredentialKind.BEARER, token, exp)


def uat(token: str = ISSUED_UAT) -> Credential:
    return Credential(CredentialKind.USER_AUTH_TOKEN, token)


def assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


class FakeQobuzApi:
    """Answers the Qobuz REST endpoints the plugin uses.

    Everything needs a credential: a Bearer from ``valid_tokens`` or a user
    auth token from ``valid_uats``. A Bearer login also issues ``ISSUED_UAT``
    unless ``issue_uat`` is off. ``status`` forces an answer for an endpoint.
    """

    def __init__(self, valid_tokens=(API_JWT,), account_endpoint: str = "user/login"):
        self.valid_tokens = set(valid_tokens)
        self.valid_uats = {ISSUED_UAT}
        self.issue_uat = True
        self.uat_login_status = 200
        self.account_endpoint = account_endpoint
        self.user = dict(USER)
        self.status: dict[str, int] = {}
        self.missing_tracks: set[int] = set()
        self.featured = [{"id": "gone", "streamable": False}, {"id": "alb1"}]
        self.album_tracks = [{"id": 111, "streamable": False}, {"id": 222, "streamable": True}]
        self.requests: list[httpx.Request] = []

    def endpoint_calls(self) -> list[str]:
        return [_endpoint(request) for request in self.requests]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, holder: TokenHolder, bundle: AppBundle = BUNDLE, **auth) -> QobuzClient:
        client = QobuzClient(auth=QobuzAuth(holder, **auth), transport=self.transport())
        client.configure_app(bundle.app_id, bundle.secrets)
        return client

    def handle(self, request: httpx.Request) -> httpx.Response:
        # A snapshot: a retried request is the same object, re-headered.
        self.requests.append(httpx.Request(request.method, request.url, headers=request.headers.copy()))
        endpoint = _endpoint(request)
        if endpoint in self.status:
            return httpx.Response(self.status[endpoint], json={"status": "error"})
        by_bearer = request.headers.get("Authorization") in {
            f"Bearer {token}" for token in self.valid_tokens
        }
        by_uat = request.headers.get("X-User-Auth-Token") in self.valid_uats
        if endpoint == "track/getFileUrl":
            return self._file_url(request, by_bearer or by_uat)
        if not (by_bearer or by_uat):
            return httpx.Response(401, json={"status": "error", "code": 401})
        if endpoint == "user/login":
            return self._login(request, by_bearer)
        if endpoint == self.account_endpoint:
            return httpx.Response(200, json=self.user)
        if endpoint == "user/get":
            return httpx.Response(400, json={"status": "error"})
        if endpoint == "favorite/getUserFavoriteIds":
            return httpx.Response(200, json={"albums": [], "artists": [], "tracks": []})
        if endpoint == "playlist/getUserPlaylists":
            return httpx.Response(200, json={"playlists": {"items": [], "total": 0}})
        if endpoint == "album/getFeatured":
            return httpx.Response(200, json={"albums": {"items": self.featured, "total": len(self.featured)}})
        if endpoint == "album/get":
            return httpx.Response(200, json={"id": request.url.params["album_id"], "tracks": {"items": self.album_tracks}})
        if endpoint == "track/get":
            return httpx.Response(200, json=track_body(int(request.url.params["track_id"])))
        return httpx.Response(404)

    def _login(self, request: httpx.Request, by_bearer: bool) -> httpx.Response:
        if self.account_endpoint != "user/login":
            return httpx.Response(400, json={"status": "error"})
        if not by_bearer:
            if self.uat_login_status != 200:
                return httpx.Response(self.uat_login_status, json={"status": "error"})
            token = request.headers["X-User-Auth-Token"]
            return httpx.Response(200, json={"user": self.user, "user_auth_token": token})
        body = {"user": self.user}
        if self.issue_uat:
            body["user_auth_token"] = ISSUED_UAT
        return httpx.Response(200, json=body)

    def _file_url(self, request: httpx.Request, authorized: bool) -> httpx.Response:
        params = request.url.params
        expected = hashlib.md5(
            (
                f"trackgetFileUrlformat_id{params['format_id']}intentstreamtrack_id"
                f"{params['track_id']}{params['request_ts']}{GOOD_SECRET}"
            ).encode()
        ).hexdigest()
        if params["request_sig"] != expected:
            return httpx.Response(400, json={"status": "error", "message": "Invalid Request Signature"})
        if not authorized:
            return httpx.Response(401, json={"status": "error"})
        if int(params["track_id"]) in self.missing_tracks:
            return httpx.Response(404, json={"status": "error", "message": "No result matching given argument"})
        return httpx.Response(
            200,
            json={
                "url": f"https://streaming.qobuz.test/{params['track_id']}",
                "format_id": int(params["format_id"]),
                "duration": 240,
                "mime_type": "audio/flac",
            },
        )


def track_body(track_id: int) -> dict:
    """A track/get answer, as far as the plugin reads one."""
    return {
        "id": track_id,
        "title": f"Synthetic song {track_id}",
        "duration": 240,
        "performer": {"id": 5, "name": "Synthetic Performer"},
        "album": {
            "id": "alb1",
            "title": "Synthetic Album",
            "image": {"small": "https://img.qobuz.test/s.jpg", "large": "https://img.qobuz.test/l.jpg"},
            "label": {"id": 3, "name": "Label"},
            "genre": {"id": 4, "name": "Jazz"},
        },
    }


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.is_success = True

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeSession:
    """Records every request; answers each with the same payload, an empty
    featured listing unless told otherwise."""

    def __init__(self, payload=None):
        self.calls = []
        self._payload = payload or {"albums": {"items": [], "total": 0}}

    async def get(self, url, params=None):
        self.calls.append((url, params or {}))
        return FakeResponse(self._payload)


class FakeClient:
    def __init__(self, payload=None):
        self.base = "https://qobuz.test/"
        self.session = FakeSession(payload)

    def require_account(self):
        return None


def fake_module(payload=None, playlist_index=None):
    """An input module over a :class:`FakeClient`, built without logging in."""
    client = FakeClient(payload)
    module = QobuzInputModule.__new__(QobuzInputModule)
    module.qobuz_client = client
    module.format_id = 5
    module._playlist_index = playlist_index
    return module, client


def _endpoint(request: httpx.Request) -> str:
    return request.url.path.split("/api.json/0.2/", 1)[-1]


@pytest.fixture
def api() -> FakeQobuzApi:
    return FakeQobuzApi()


class FakeReceiver:
    """Stands in for HandoffReceiver; the test calls the handlers directly."""

    def __init__(self, handlers, port: int, fail: Optional[OSError] = None):
        self.handlers = handlers
        self.port = port
        self.fail = fail
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        if self.fail is not None:
            raise self.fail
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class FakeAdvertiser:
    def __init__(self):
        self.adverts = []
        self.running = False
        self.stops = 0

    async def start(self, advert) -> None:
        self.adverts.append(advert)
        self.running = True

    async def stop(self) -> None:
        self.running = False
        self.stops += 1


class FakeSessionSink:
    """Records what pairing tells Connect playback."""

    def __init__(self):
        self.ready = []
        self.ended = 0

    def session_ready(self, link, *, handed_over):
        self.ready.append((link.session, handed_over))

    def session_ended(self):
        self.ended += 1


class FakeRefresher:
    """Records how the pairing service drives renewal."""

    instances: list["FakeRefresher"] = []

    def __init__(self, *, holder, http, app_id, persist, on_expired, clock):
        self.holder = holder
        self.persist = persist
        self.on_expired = on_expired
        self.started = False
        self.stopped = False
        self.renew_result = False
        self.next_refresh_at = None
        FakeRefresher.instances.append(self)

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def on_unauthorized(self, rejected) -> bool:
        return self.renew_result


async def settle(condition, *, turns: int = 200) -> None:
    """Let the event loop run until ``condition()`` holds."""
    for _ in range(turns):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def handoff_body(
    api_jwt: str = API_JWT,
    session_id: str = "sess-1234-abcd",
    api_exp: int = 4102444800,
    qconnect_jwt: str = QCONNECT_JWT,
    endpoint: str = "wss://qws.qobuz.test/ws",
    **extra,
) -> bytes:
    body = {
        "session_id": session_id,
        "jwt_qconnect": {"jwt": qconnect_jwt, "exp": 4102444800, "endpoint": endpoint},
        "jwt_api": {"jwt": api_jwt, "exp": api_exp},
    }
    body.update(extra)
    return json.dumps(body).encode()
