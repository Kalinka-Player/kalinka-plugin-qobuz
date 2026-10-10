"""Keeps a linked Bearer credential alive past its expiry.

Qobuz issues no refresh token. The live credential itself is exchanged for a
new one at ``qws/refreshToken``, which works independently of any Connect
session; once the credential is too far past its expiry, the link is over.

Protocol details adapted from Pibuz (https://github.com/PhilipVinc/pibuz,
crates/pibuz/src/qconnect/pairing.rs), MIT License:
Copyright (c) 2024 blitzkriegfc, Copyright (c) 2026 Filippo Vicentini.
"""

import asyncio
import enum
import logging
import random
import time
from typing import Awaitable, Callable, Optional

import httpx

from ..auth import Credential, CredentialKind, TokenHolder, fingerprint, unix_seconds

logger = logging.getLogger(__name__.split(".")[-1])

REFRESH_URL = "https://www.qobuz.com/api.json/0.2/qws/refreshToken"

# Renew this long before expiry, plus up to JITTER_S more to allow for clock skew.
LEAD_S = 300
JITTER_S = 60
# Pibuz keeps trying until this long after expiry; after that the credential is dead.
GRACE_S = 300
BACKOFF_S = (15, 30, 60, 120, 300)
# A 401 from any endpoint may renew at most this often, so an endpoint that
# refuses the credential for other reasons cannot hammer refreshToken.
REACTIVE_COOLDOWN_S = 60
# Long sleeps are split so a wall-clock jump (NTP at boot) is noticed.
MAX_SLEEP_S = 3600

EXPIRED_REASON = "Qobuz refused to renew the link"


class Outcome(enum.Enum):
    RENEWED = "renewed"
    REJECTED = "rejected"
    FAILED = "failed"
    STALE = "stale"


class TokenRefresher:
    """Renews the holder's Bearer credential on schedule and after a 401.

    @param persist Stores a renewed credential; called before it is installed.
    @param on_expired Told, once, that the credential cannot be renewed.
    @note A renewal that finishes after the holder moved on (unpaired, relinked,
        expired) is discarded.
    """

    def __init__(
        self,
        *,
        holder: TokenHolder,
        http: httpx.AsyncClient,
        app_id: str,
        persist: Callable[[Credential], None],
        on_expired: Callable[[str], None],
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0, JITTER_S),
    ):
        self._holder = holder
        self._http = http
        self._app_id = app_id
        self._persist = persist
        self._on_expired = on_expired
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._scheduled_for: Optional[Credential] = None
        self._margin = 0.0
        self._last_reactive = float("-inf")

    @property
    def next_refresh_at(self) -> Optional[float]:
        credential = self._holder.credential
        if not renewable(credential):
            return None
        return credential.exp - self._margin_for(credential)

    def start(self) -> None:
        """Schedule renewal; a no-op while it runs. It stops by itself once the
        holder has no renewable credential, and may be started again."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="qobuz-token-refresh")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def on_unauthorized(self, rejected: Credential) -> bool:
        """Renew after a 401. True when the holder now has a different credential."""
        async with self._lock:
            current = self._holder.credential
            if current is None:
                return False
            if current != rejected:
                return True
            if current.kind is not CredentialKind.BEARER:
                return False
            if self._clock() - self._last_reactive < REACTIVE_COOLDOWN_S:
                return False
            self._last_reactive = self._clock()
            logger.info(  # log-safe: an 8-hex SHA-256 prefix, not the token
                "Qobuz rejected credential #%s; renewing it", fingerprint(current.token)
            )
            outcome = await self._renew(current)
        if outcome is Outcome.REJECTED:
            self._on_expired(EXPIRED_REASON)
        return outcome is Outcome.RENEWED

    def _margin_for(self, credential: Credential) -> float:
        if credential is not self._scheduled_for:
            self._scheduled_for = credential
            self._margin = LEAD_S + self._jitter()
        return self._margin

    async def _run(self) -> None:
        failures = 0
        while True:
            credential = self._holder.credential
            if not renewable(credential):
                return
            now = self._clock()
            due = credential.exp - self._margin_for(credential)
            if now < due:
                await self._sleep(min(due - now, MAX_SLEEP_S))
                continue
            async with self._lock:
                if self._holder.credential != credential:
                    continue
                outcome = await self._renew(credential)
            if outcome is Outcome.RENEWED or outcome is Outcome.STALE:
                failures = 0
                continue
            if outcome is Outcome.REJECTED or self._clock() > credential.exp + GRACE_S:
                self._on_expired(EXPIRED_REASON)
                return
            delay = BACKOFF_S[min(failures, len(BACKOFF_S) - 1)]
            failures += 1
            logger.warning("Qobuz link renewal failed; retrying in %ds", delay)
            await self._sleep(delay)

    async def _renew(self, credential: Credential) -> Outcome:
        generation = self._holder.generation
        try:
            response = await self._http.post(
                REFRESH_URL,
                headers={
                    "X-App-Id": self._app_id,
                    "Authorization": f"Bearer {credential.token}",
                },
                data={"jwt": "jwt_api"},
            )
        except httpx.TransportError as exc:
            if self._holder.generation != generation:
                return Outcome.STALE
            logger.warning("Qobuz link renewal: network error (%s)", type(exc).__name__)
            return Outcome.FAILED
        # A fresh handoff supersedes failures as well as successful renewals.
        # In particular, an old 401 must not expire the replacement credential.
        if self._holder.generation != generation:
            logger.info("Qobuz link renewal finished after the link changed; discarded")
            return Outcome.STALE
        if response.status_code in (401, 403):
            logger.warning("Qobuz link renewal refused: HTTP %d", response.status_code)
            return Outcome.REJECTED
        renewed = _renewed_credential(response)
        if renewed is None:
            logger.warning("Qobuz link renewal: unusable answer (HTTP %d)", response.status_code)
            return Outcome.FAILED
        try:
            self._persist(renewed)
        except OSError as exc:
            logger.error(
                "Could not store the renewed Qobuz link (%s); it is in use until restart",
                exc.strerror or type(exc).__name__,
            )
        self._holder.install(renewed)
        logger.info(  # log-safe: 8-hex SHA-256 prefixes, not the tokens
            "Qobuz link renewed: #%s -> #%s, expires in %s",
            fingerprint(credential.token),
            fingerprint(renewed.token),
            f"{renewed.exp - int(self._clock())}s" if renewed.exp else "(no expiry given)",
        )
        return Outcome.RENEWED


def renewable(credential: Optional[Credential]) -> bool:
    """Whether ``credential`` is one this module can renew: a Bearer with an expiry."""
    return (
        credential is not None
        and credential.kind is CredentialKind.BEARER
        and credential.exp > 0
    )


def _renewed_credential(response: httpx.Response) -> Optional[Credential]:
    if not response.is_success:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    payload = body.get("jwt_api") if isinstance(body, dict) else None
    if not isinstance(payload, dict):
        return None
    token = payload.get("jwt")
    if not isinstance(token, str) or not token:
        return None
    return Credential(CredentialKind.BEARER, token, unix_seconds(payload.get("exp")))
