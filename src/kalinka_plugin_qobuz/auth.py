"""The credential every Qobuz REST request carries, supplied per request.

The REST client never holds a token itself: it asks a TokenHolder at send
time, so a renewed or cleared credential reaches every request in flight from
then on, whoever holds the client.
"""

import asyncio
import hashlib
import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__.split(".")[-1])


class AuthenticationError(Exception):
    """No usable Qobuz credential. The message is shown to users."""


NOT_LINKED = "Qobuz is not linked. Choose this player in the Qobuz app to link it."
STARTING = "Qobuz is starting up."
EXPIRED = "The Qobuz link has expired. Unpair Qobuz in settings, then pair again."


class CredentialKind(str, Enum):
    BEARER = "bearer"
    USER_AUTH_TOKEN = "user_auth_token"


# Unix seconds pass 10**11 in the year 5138, so a larger `exp` is milliseconds.
_MILLISECONDS_THRESHOLD = 10**11


def unix_seconds(value: Any) -> int:
    """A Qobuz ``exp`` as absolute Unix seconds, 0 when it is not a usable time."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return 0
    # JSON parsing admits NaN and Infinity, which no integer can hold.
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    if value > _MILLISECONDS_THRESHOLD:
        logger.info("Qobuz gave an expiry in milliseconds; converted")
        return int(value // 1000)
    return int(value)


def fingerprint(secret: str) -> str:
    """A short, stable tag that tells two tokens apart in logs without revealing either."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class Credential:
    """One Qobuz account credential and the header that carries it.

    @note ``exp`` is absolute Unix seconds, 0 when Qobuz gave no expiry.
    """

    kind: CredentialKind
    token: str
    exp: int = 0

    def headers(self) -> dict[str, str]:
        if self.kind is CredentialKind.BEARER:
            return {"Authorization": f"Bearer {self.token}"}
        return {"X-User-Auth-Token": self.token}

    def __repr__(self) -> str:
        return f"Credential({self.kind.value}, #{fingerprint(self.token)}, exp={self.exp})"

    __str__ = __repr__


_CREDENTIAL_HEADERS = ("Authorization", "X-User-Auth-Token")


class TokenHolder:
    """The account credential currently in force, or why there is none.

    @note ``generation`` changes whenever the credential is replaced or cleared,
        so work that started with one credential can tell it has been superseded.
    """

    def __init__(self, unavailable_reason: str = STARTING):
        self._credential: Optional[Credential] = None
        self._reason = unavailable_reason
        self.generation = 0
        self._installed = asyncio.Event()

    @property
    def credential(self) -> Optional[Credential]:
        return self._credential

    def current(self) -> Credential:
        if self._credential is None:
            raise AuthenticationError(self._reason)
        return self._credential

    def install(self, credential: Credential) -> None:
        self._credential = credential
        self.generation += 1
        self._installed.set()

    def clear(self, reason: str) -> None:
        self._credential = None
        self._reason = reason
        self.generation += 1
        self._installed.clear()

    async def wait_for_credential(self) -> None:
        """Return once a credential is in force: at once while one is."""
        await self._installed.wait()


RenewHook = Callable[[Credential], Awaitable[bool]]


class QobuzAuth(httpx.Auth):
    """httpx auth that adds the holder's credential header to each request.

    With no credential it raises AuthenticationError before anything is sent.
    On a 401 it asks ``on_unauthorized`` to renew the rejected credential and,
    if that installed a new one, sends the request once more.
    """

    requires_request_body = True

    def __init__(self, holder: TokenHolder, on_unauthorized: Optional[RenewHook] = None):
        self._holder = holder
        self._on_unauthorized = on_unauthorized

    def require_credential(self) -> None:
        """@throw AuthenticationError while there is no credential, as a request would."""
        self._holder.current()

    def sync_auth_flow(self, request):
        raise RuntimeError("QobuzAuth renews credentials asynchronously; use httpx.AsyncClient")

    async def async_auth_flow(self, request):
        credential = self._holder.current()
        _apply(request, credential)
        response = yield request
        if response.status_code != 401 or self._on_unauthorized is None:
            return
        if await self._on_unauthorized(credential):
            _apply(request, self._holder.current())
            yield request


def _apply(request: httpx.Request, credential: Credential) -> None:
    for name in _CREDENTIAL_HEADERS:
        request.headers.pop(name, None)
    request.headers.update(credential.headers())
