"""The linked account's favourites on Qobuz.

Qobuz keeps tracks, albums and artists as favourites. A playlist is
subscribed to instead, and one the account owns is never a favourite: it
cannot be subscribed to, and it is listed among the account's own.
"""

import asyncio

import httpx

from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.inputmodule import SearchType, SourceUnavailableError

from .auth import REJECTED_LINK, AuthenticationError

# The request field each kind's id is marked and unmarked through.
FIELDS = {
    EntityType.TRACK: "track_ids",
    EntityType.ALBUM: "album_ids",
    EntityType.ARTIST: "artist_ids",
}
# The most ids, and the most playlists, Qobuz answers in one request.
MOST_IDS = 5000
MOST_PLAYLISTS = 500
UNREADABLE = "Qobuz returned favourites Kalinka cannot read."
TOO_MANY = "Qobuz holds more favourites than Kalinka can read at once."


class Favourites:
    """Reads and changes the favourites of the account ``client`` is linked to.

    A read raises :class:`AuthenticationError` while no account is linked,
    as any request does; a change reports that as the source being
    unavailable, since nothing was changed.
    """

    def __init__(self, client):
        self._client = client

    async def _request(self, method: str, endpoint: str, **params) -> dict:
        try:
            response = await self._client.session.request(
                method, self._client.base + endpoint, params=params
            )
        except httpx.HTTPError:
            raise SourceUnavailableError("Qobuz could not be reached.") from None
        if response.status_code == 401:
            raise SourceUnavailableError(REJECTED_LINK)
        if response.status_code == 404:
            raise LookupError("Qobuz has no such item.")
        if not response.is_success:
            raise SourceUnavailableError("Qobuz could not complete the request.")
        return response.json()

    async def _subscribed(self) -> list[dict]:
        """Every playlist the account subscribes to. They are listed among
        its own, so the list is read to the end Qobuz gives it."""
        playlists = []
        while True:
            body = await self._request(
                "GET",
                "playlist/getUserPlaylists",
                offset=len(playlists),
                limit=MOST_PLAYLISTS,
            )
            listed = body.get("playlists") if isinstance(body, dict) else None
            page = listed.get("items") if isinstance(listed, dict) else None
            if not isinstance(page, list):
                raise SourceUnavailableError(UNREADABLE)
            playlists.extend(page)
            total = listed.get("total")
            if not page or not isinstance(total, int) or len(playlists) >= total:
                break
        return [
            playlist
            for playlist in playlists
            if playlist["owner"]["id"] != self._client.user_id
        ]

    async def ids(self) -> dict[EntityType, list[str]]:
        """Every favourite's Qobuz id by kind, whole. A kind Qobuz leaves out
        has none; one as long as the most it answers may have more."""
        marked, playlists = await asyncio.gather(
            self._request("GET", "favorite/getUserFavoriteIds", limit=MOST_IDS),
            self._subscribed(),
        )
        if not isinstance(marked, dict):
            raise SourceUnavailableError(UNREADABLE)
        ids = {}
        for kind in FIELDS:
            listed = marked.get(kind.value + "s", [])
            if not isinstance(listed, list):
                raise SourceUnavailableError(UNREADABLE)
            if len(listed) >= MOST_IDS:
                raise SourceUnavailableError(TOO_MANY)
            ids[kind] = [str(id) for id in listed]
        ids[EntityType.PLAYLIST] = [str(playlist["id"]) for playlist in playlists]
        return ids

    async def page(self, kind: SearchType, offset: int, limit: int) -> dict:
        """One page of one kind, as the Qobuz list the mappers read."""
        if kind == SearchType.playlist:
            playlists = await self._subscribed()
            return {
                "playlists": {
                    "total": len(playlists),
                    "items": playlists[offset : offset + limit],
                }
            }
        key = kind.value + "s"
        body = await self._request(
            "GET", "favorite/getUserFavorites", type=key, offset=offset, limit=limit
        )
        if not isinstance(body.get(key), dict):
            raise SourceUnavailableError(UNREADABLE)
        return {key: body[key]}

    async def add(self, item: EntityId) -> None:
        await self._change(item, "favorite/create", "playlist/subscribe")

    async def remove(self, item: EntityId) -> None:
        await self._change(item, "favorite/delete", "playlist/unsubscribe")

    async def _change(self, item: EntityId, favourite: str, playlist: str) -> None:
        if item.source != "qobuz":
            raise LookupError("This item does not belong to Qobuz.")
        try:
            if item.type == EntityType.PLAYLIST:
                await self._refuse_own(item.id)
                body = await self._request("POST", playlist, playlist_id=item.id)
            elif item.type in FIELDS:
                body = await self._request(
                    "POST", favourite, **{FIELDS[item.type]: item.id}
                )
            else:
                raise ValueError(
                    "Qobuz keeps favourite tracks, albums, artists and playlists only."
                )
        except AuthenticationError as exc:
            raise SourceUnavailableError(str(exc)) from None
        if body.get("status") != "success":
            raise SourceUnavailableError("Qobuz did not make the change.")

    async def _refuse_own(self, playlist_id: str) -> None:
        playlist = await self._request(
            "GET", "playlist/get", playlist_id=playlist_id, offset=0, limit=0
        )
        if playlist.get("owner", {}).get("id") == self._client.user_id:
            raise ValueError("A playlist of your own cannot be a favourite.")
