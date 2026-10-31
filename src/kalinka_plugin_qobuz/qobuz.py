import asyncio
import copy
import functools
import hashlib
import inspect
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, FrozenSet, List, Optional

import httpx
from pydantic import BaseModel, PositiveInt

from .auth import AuthenticationError, QobuzAuth
from .bundle import load_bundle
from .config_model import QobuzAudioFormat, QobuzConfig

if TYPE_CHECKING:
    from .account import AccountInfo

from kalinka_plugin_sdk.datamodel import (
    Album,
    Artist,
    BrowseItem,
    BrowseItemList,
    CardSize,
    Catalog,
    CatalogRole,
    CoverImage,
    EmptyList,
    EntityId,
    EntityType,
    FavoriteIds,
    Genre,
    Label,
    Owner,
    Playlist,
    Preview,
    PreviewContentType,
    PreviewType,
    Track,
)
from kalinka_plugin_sdk.filters import (
    or_unfiltered,
    FilterKind,
    FilterOp,
    FilterQuery,
    FilterSpec,
    FilterValue,
    FilterValueList,
    UnsupportedFilter,
)
from kalinka_plugin_sdk.inputmodule import (
    ContentInfo,
    DirectUrl,
    InputModule,
    ModuleCapability,
    SearchType,
    SourceUnavailableError,
    TrackInfo,
    TrackSource,
)

logger = logging.getLogger(__name__.split(".")[-1])


class InvalidAppIdError(Exception):
    pass


class InvalidAppSecretError(Exception):
    pass


class AppSecretPendingError(Exception):
    """No app secret is selected yet: the account check that picks one has
    not completed. The message is shown to users."""


class InvalidQuality(Exception):
    pass


class NonStreamable(Exception):
    pass


# Retried: failures that are discovered in milliseconds and are genuinely
# transient. Timeouts are deliberately NOT here — a timeout has already
# consumed the full per-attempt budget, and an upstream that just hung is
# very likely to hang again, so retrying turns one slow request into
# several (3s became 7.5s worst-case). Timeouts propagate immediately.
_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,          # refused / unreachable / DNS
    httpx.ReadError,             # connection reset mid-response
    httpx.RemoteProtocolError,   # server dropped a keep-alive connection
    httpx.ProxyError,
)


class RetryTransport(httpx.AsyncHTTPTransport):
    def __init__(self, read_retries=2, **kwargs):
        super().__init__(**kwargs)
        # read_retries is the total number of attempts (1 initial + N-1 retries).
        # Kept low so a request fails fast when the server has no internet —
        # otherwise the playqueue's resolution slot stays occupied far too long.
        self.read_retries = read_retries
        self.backoff_factor = 0.5

    async def handle_async_request(self, request) -> httpx.Response:
        attempt = 1
        while True:
            try:
                response = await super().handle_async_request(request)
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt >= self.read_retries:
                    raise
                logger.warning("Retry %d due to %r", attempt, exc)
                await asyncio.sleep(self.backoff_factor * attempt)
                attempt += 1
                continue
            if 500 <= response.status_code < 600 and attempt < self.read_retries:
                # Release the connection before retrying; abandoning the
                # response leaks connection back-pressure under repeated 5xx.
                await response.aclose()
                logger.warning(
                    "Retry %d due to status code=%d", attempt, response.status_code
                )
                await asyncio.sleep(self.backoff_factor * attempt)
                attempt += 1
                continue
            # Final attempt's 5xx is returned to the caller, not swallowed.
            return response


# The code below is partially based on the code from
# qobuz-dl by vitiko98, fc7


def artist_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.ARTIST, source="qobuz")


def album_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.ALBUM, source="qobuz")


def track_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.TRACK, source="qobuz")


def playlist_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.PLAYLIST, source="qobuz")


def label_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.LABEL, source="qobuz")


# Above the taxonomy's size, so one request holds all of it.
GENRE_PAGE = 500
# Width over height of an editorial playlist's image_rectangle banner.
PLAYLIST_BANNER_RATIO = 1 / 0.475
# Banners are stored without CORS headers, so a browser cannot draw them
# straight from Qobuz; the server serves them through get_content_info.
PLAYLIST_BANNER_BASE = "https://static.qobuz.com/images/playlists/"
BANNER_FILE = re.compile(r"[0-9]+_[0-9a-f]{32}_rectangle\.jpg")
BANNER_ASSET = "banner-"

# Qobuz's featured endpoints take genre_ids and union them; they offer no text
# parameter, so those shelves declare genre alone.
GENRE_FILTER = FilterSpec(
    id="genre",
    kind=FilterKind.VALUES,
    label="Genre",
    ops=[FilterOp.ANY],
)

GENRE_FILTERABLE = {
    "recent-releases",
    "new-releases",
    "playlist-by-category",
    "qobuz-playlists",
    "press-awards",
    "most-streamed",
}


def _catalog_filters(endpoint: str) -> List[FilterSpec]:
    """What a shelf can be filtered by. Playlist categories are
    ``playlist-by-category_<slug>`` and filter like their parent."""
    base = endpoint.split("_")[0]
    return [GENRE_FILTER] if base in GENRE_FILTERABLE else []


def _genre_values(endpoint: str, filter: FilterQuery) -> List[str]:
    """The selected genre ids, refusing what this shelf never offered."""
    declared = {spec.id for spec in _catalog_filters(endpoint)}
    filter.reject_undeclared(declared)

    genres = filter.values("genre") if "genre" in declared else None
    if not genres:
        return []
    if genres.all or genres.none:
        raise UnsupportedFilter(
            "genre", "this source can only match any of the genres given"
        )
    return list(genres.any)


def genre_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.GENRE, source="qobuz")


def user_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.USER, source="qobuz")


def catalog_id(id: str) -> EntityId:
    return EntityId(id=id, type=EntityType.CATALOG, source="qobuz")


class LastUpdate(BaseModel):
    favorite_tracks_ts: int = 0
    favorite_albums_ts: int = 0
    favorite_artists_ts: int = 0
    favorite_playlists_ts: int = 0


class QobuzClient:
    """Qobuz REST API client.

    The account credential comes from ``auth`` on every request, so one
    client serves every account the plugin links over its lifetime.
    """

    def __init__(self, auth: QobuzAuth, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._auth = auth
        self.id: Optional[str] = None
        self.secrets: list[str] = []
        self.session = httpx.AsyncClient(
            auth=auth,
            # 2 attempts (1 initial + 1 retry), 3s per attempt. RetryTransport
            # already retries connect failures, so httpx-level retries=0 avoids
            # multiplying the worst-case wait.
            transport=transport
            or RetryTransport(read_retries=2, retries=0, http2=True, http1=False),
            # 3s read/write/pool keeps runtime calls fast-failing; connect gets a
            # longer budget so a slow-to-come-up network doesn't fail startup.
            timeout=httpx.Timeout(3.0, connect=15.0),
        )
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0",
            }
        )

        # The image store speaks HTTP/1.1 only, and needs no credential.
        # An injected transport answers for it too.
        self._image_transport = transport
        self._images: Optional[httpx.AsyncClient] = None

        self.base = "https://www.qobuz.com/api.json/0.2/"
        self.sec = None
        self.user_id = None
        self.credential_id = None
        self.label = ""
        self.reset_caches()

    def configure_app(self, app_id: str, secrets: list[str]) -> None:
        """Identify requests as the Qobuz web player, whose bundle supplied these."""
        self.id = str(app_id)
        self.secrets = list(secrets)
        self.session.headers["X-App-Id"] = self.id

    def require_account(self) -> None:
        """@throw AuthenticationError while no account is linked, as any request would."""
        self._auth.require_credential()

    def install_account(self, account: "AccountInfo") -> None:
        """Adopt the account that streaming reports and playlist ownership refer to."""
        self.user_id = account.user_id
        self.credential_id = account.credential_id
        self.label = account.label

    def reset_caches(self) -> None:
        """Forget everything learnt from the previous account."""
        # a short living cache to be used for reporting purposes
        self.track_url_response_cache = {}
        self.last_update = LastUpdate()
        self.cached = {"albums": {}, "artists": {}, "tracks": {}, "playlists": {}}

    async def aclose(self) -> None:
        await self.session.aclose()
        if self._images is not None:
            await self._images.aclose()

    async def get_image(self, url: str) -> Optional[bytes]:
        """The picture at ``url``, or None where Qobuz has none there.

        @throw SourceUnavailableError when Qobuz could not be reached.
        """
        if self._images is None:
            # Inside the server's 3s budget for get_content_info.
            self._images = httpx.AsyncClient(
                transport=self._image_transport, timeout=httpx.Timeout(2.5)
            )
        try:
            response = await self._images.get(url)
        except httpx.HTTPError as exc:
            raise SourceUnavailableError("Qobuz did not send the picture.") from exc
        if response.is_server_error:
            raise SourceUnavailableError("Qobuz did not send the picture.")
        # The store answers 403, not 404, for a picture it does not hold.
        return response.content if response.is_success else None

    async def test_secret(self, sec, track_id) -> bool:
        """Whether Qobuz accepts ``sec``'s signature. Only a 400 means it does
        not: a missing track or a refused account says nothing about the secret."""
        response = await self.session.get(
            self.base + "track/getFileUrl",
            params=self._file_url_params(track_id, 5, sec),
        )
        return response.status_code != 400

    async def cfg_setup(self, track_id):
        """Select the app secret, probing with ``track_id``.

        The secret in use stays until another is found, so streams keep
        working while a check of the shared client is under way or fails.
        """
        for secret in self.secrets:
            # Falsy secrets
            if not secret:
                continue

            if await self.test_secret(secret, track_id):
                self.sec = secret
                return

        raise InvalidAppSecretError("Can't find any valid app secret.")

    def _file_url_params(self, track_id, fmt_id, sec) -> dict:
        unix = time.time()
        r_sig = "trackgetFileUrlformat_id{}intentstreamtrack_id{}{}{}".format(
            fmt_id, track_id, unix, sec
        )
        return {
            "request_ts": unix,
            "request_sig": hashlib.md5(r_sig.encode("utf-8")).hexdigest(),
            "track_id": track_id,
            "format_id": fmt_id,
            "intent": "stream",
        }

    async def get_track_url(self, track_id, fmt_id=5, sec=None):
        epoint = "track/getFileUrl"
        if int(fmt_id) not in (5, 6, 7, 27):
            raise InvalidQuality("Invalid quality id: choose between 5, 6, 7 or 27")
        secret = self.sec if sec is None else sec
        if secret is None:
            # Signing with no secret only earns a 400 that reads as a bad
            # secret. No link, or an expired one, is the better reason.
            self.require_account()
            raise AppSecretPendingError(STILL_CONNECTING)
        params = self._file_url_params(track_id, fmt_id, secret)

        r = None
        for _ in range(3):
            try:
                r = await self.session.get(self.base + epoint, params=params)
                break
            except ConnectionError as e:
                logger.error(f"Connection error, retrying {_}: {e}")

        if r is None:
            raise ConnectionError("Failed to get a response after retries.")

        if r.status_code == 400:
            raise InvalidAppSecretError(f"Invalid app secret: {r.json()}.")

        r.raise_for_status()
        self.track_url_response_cache[str(track_id)] = r.json()
        return r.json()

    async def get_track_meta(self, track_id):
        epoint = "track/get"
        params = {"track_id": track_id}
        r = await self.session.get(self.base + epoint, params=params)
        if r.status_code == 400:
            raise InvalidAppSecretError(f"Invalid app secret: {r.json()}.")

        r.raise_for_status()
        return r.json()

    async def get_tracks_meta(self, track_ids: list[int]):
        epoint = "track/getList"
        params = {"tracks_id": track_ids}
        r = await self.session.post(self.base + epoint, json=params)

        if r.status_code == 400:
            raise InvalidAppSecretError(f"Invalid app secret: {r.json()}.")

        r.raise_for_status()
        return r.json()["tracks"]["items"]

    async def get_qobuz_last_update(self):
        r = await self.session.get(self.base + "user/lastUpdate")
        r.raise_for_status()

        return r.json()["last_update"]

    async def get_user_playlists(self, offset: int = 0, limit: int = 50, owner_id=None):
        # Owner filtering has to happen locally, which breaks server-side
        # pagination (a page of raw results shrinks unpredictably after
        # filtering). Fetch the full list and paginate here, exactly once.
        r = await self.session.get(
            self.base + "playlist/getUserPlaylists",
            params={"offset": 0, "limit": 500},
        )

        r.raise_for_status()

        all_playlists = r.json()["playlists"]["items"]

        if owner_id is not None:
            filtered_playlists = [
                playlist
                for playlist in all_playlists
                if playlist["owner"]["id"] == owner_id
            ]
        else:
            filtered_playlists = all_playlists

        playlists = {
            "offset": offset,
            "limit": limit,
            "total": len(filtered_playlists),
            "items": filtered_playlists[offset : offset + limit],
        }

        return {"playlists": playlists}

    async def get_user_favorites(
        self, type: SearchType, offset: int = 0, limit: int = 50
    ):
        last_update = (await self.get_qobuz_last_update())[
            {
                SearchType.album: "favorite_album",
                SearchType.artist: "favorite_artist",
                SearchType.track: "favorite_track",
            }[type]
        ]

        type_name = f"{type.name}s"

        if last_update != getattr(self.last_update, f"favorite_{type_name}_ts"):
            logger.info(f"Updating user favorites cache for {type_name}")
            r = await self.session.get(
                self.base + "favorite/getUserFavorites",
                params={"type": type.value + "s", "offset": 0, "limit": 500},
            )

            r.raise_for_status()

            self.cached[type_name] = copy.deepcopy(r.json()[type_name])

            setattr(self.last_update, f"favorite_{type_name}_ts", last_update)

        retval = {
            "offset": offset,
            "limit": limit,
            "total": len(self.cached[type_name]["items"]),
            "items": self.cached[type_name]["items"][offset : offset + limit],
        }

        return {type_name: retval}


@dataclass(frozen=True)
class AppBundle:
    """What the Qobuz web player's bundle says about the app it ships."""

    app_id: str
    secrets: list[str]


async def load_app_bundle() -> AppBundle:
    # The web-bundle fetch is the slowest part of Qobuz startup; bracket it
    # with explicit INFO logs so the server log makes it obvious when this
    # phase starts, finishes, and how long it took.
    logger.info("Loading Qobuz web bundle (app id + secrets)...")
    bundle_start = time.monotonic()
    bundle = await load_bundle()
    app_id = bundle.get_app_id()
    secrets = [secret for secret in bundle.get_secrets().values() if secret]
    logger.info(
        "Qobuz web bundle loaded in %.1fs (app id %s, %d secret(s))",
        time.monotonic() - bundle_start,
        app_id,
        len(secrets),
    )
    return AppBundle(app_id=app_id, secrets=secrets)


def _first(images):
    return images[0] if images else None


def _served_banner(url):
    """A banner's link on this server; any other picture as Qobuz gave it."""
    name = url.removeprefix(PLAYLIST_BANNER_BASE) if url else ""
    if not BANNER_FILE.fullmatch(name):
        return url
    return f"/content/qobuz/{BANNER_ASSET}{name}"


REJECTED_LINK = "Qobuz did not accept this player's link. If it persists, unpair Qobuz and pair again."
STILL_CONNECTING = "Qobuz is still connecting to your account. Try again in a moment."


async def qobuz_source_retriever(qobuz_client, id, format_id) -> TrackSource:
    # Qobuz issues a time-limited URL the renderer fetches for itself; the
    # server has no copy of the file to serve in its place.
    try:
        track = await qobuz_client.get_track_url(id, fmt_id=format_id)
    except (AuthenticationError, AppSecretPendingError) as exc:
        raise SourceUnavailableError(str(exc)) from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise SourceUnavailableError(REJECTED_LINK) from None
        raise
    return TrackSource(
        source=DirectUrl(url=track["url"]), format=track["mime_type"]
    )


def _when_unlinked(fallback: Callable[[dict], object]):
    """Answer ``fallback(call_arguments)`` instead of failing while no account is linked.

    The server merges browse, search and favourites results from every source
    in one gather, so a Qobuz failure there would blank every source's results.
    """

    def decorate(method):
        signature = inspect.signature(method)

        @functools.wraps(method)
        async def call(self, *args, **kwargs):
            try:
                return await method(self, *args, **kwargs)
            except AuthenticationError:
                bound = signature.bind(self, *args, **kwargs)
                bound.apply_defaults()
                return fallback(bound.arguments)

        return call

    return decorate


def _empty_page(arguments: dict) -> BrowseItemList:
    return EmptyList(arguments["offset"], arguments["limit"])


def _no_filter_values(arguments: dict) -> FilterValueList:
    return FilterValueList(
        offset=arguments["offset"], limit=arguments["limit"], total=0, items=[]
    )


def append_str(s1: str, s2: str) -> str:
    if not s2:
        return s1
    else:
        return s1.strip() + f" ({s2.strip()})"


def album_year(album: dict) -> Optional[int]:
    """The year Qobuz says the release came out, or None where it says nothing."""
    date = album.get("release_date_original") or ""
    if date[:4].isdigit():
        return int(date[:4])
    if released_at := album.get("released_at"):
        return datetime.fromtimestamp(released_at, timezone.utc).year
    return None


def metadata_from_track(track, album_meta={}):
    album_info = track.get("album", album_meta)
    version = album_info.get("version", None)
    return Track(
        **{
            "id": track_id(str(track["id"])),
            "title": append_str(track["title"], track.get("version", None)),
            "performer": (
                Artist(
                    name=track["performer"]["name"],
                    id=artist_id(str(track["performer"]["id"])),
                )
                if "performer" in track
                else Artist(
                    name=album_info["artist"].get("name", None),
                    id=artist_id(str(album_info["artist"].get("id", None))),
                )
            ),
            "duration": track["duration"],
            "track_number": track.get("track_number"),
            "disc_number": track.get("media_number"),
            "album": Album(
                id=album_id(str(album_info["id"])),
                title=append_str(album_info["title"], version),
                year=album_year(album_info),
                image=album_info["image"],
                label=Label(
                    id=label_id(str(album_info["label"]["id"])),
                    name=album_info["label"]["name"],
                ),
                genres=[
                    Genre(
                        id=genre_id(str(album_info["genre"]["id"])),
                        name=album_info["genre"]["name"],
                    )
                ],
            ),
            "replaygain_peak": track.get("audio_info", {}).get(
                "replaygain_track_peak", None
            ),
            "replaygain_gain": track.get("audio_info", {}).get(
                "replaygain_track_gain", None
            ),
        }
    )


class QobuzInputModule(InputModule):
    def __init__(
        self,
        config: QobuzConfig,
        qobuz_client: QobuzClient,
    ):
        self.format_id = (5, 6, 7, 27)[
            list(QobuzAudioFormat).index(QobuzAudioFormat(config.format))
        ]
        logger.info(f"Selecting Format '{config.format}', id = {self.format_id}")
        self.qobuz_client = qobuz_client
        self.last_update = LastUpdate()
        self.user_playlist = []

    def module_name(self) -> str:
        return "Qobuz"

    def capabilities(self) -> FrozenSet[ModuleCapability]:
        return frozenset({ModuleCapability.CATALOGS})

    @_when_unlinked(_empty_page)
    async def search(
        self, type: SearchType, query: str, offset=0, limit=50
    ) -> BrowseItemList:
        return await self._search_items(type, query, offset, limit)

    @_when_unlinked(_empty_page)
    async def browse(
        self,
        entity_id: EntityId,
        offset: PositiveInt = 0,
        limit: PositiveInt = 50,
        filter: Optional[FilterQuery] = None,
    ) -> BrowseItemList:
        filter = or_unfiltered(filter)
        if entity_id.type == EntityType.CATALOG:
            return await self._browse_catalog(
                entity_id.id, offset=offset, limit=limit, filter=filter
            )

        # An album, artist or playlist listing declares no filters, so any
        # field sent to one is refused rather than dropped.
        filter.reject_undeclared(())
        if entity_id.type == EntityType.ALBUM:
            return await self._browse_album(entity_id.id, offset, limit)
        elif entity_id.type == EntityType.PLAYLIST:
            return await self._browse_playlist(entity_id.id, offset, limit)
        elif entity_id.type == EntityType.ARTIST:
            return await self._browse_artist(entity_id.id, offset, limit)
        else:
            return EmptyList(offset, limit)

    async def _browse_album(
        self, id: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "album/get",
            params={"album_id": id, "offset": offset, "limit": limit},
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        rjson = response.json()

        album_meta = rjson.copy()
        del album_meta["tracks"]

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=rjson["tracks"]["total"],
            items=self._tracks_to_browse_categories(
                rjson["tracks"]["items"],
                album_meta=album_meta,
            ),
        )

    async def _browse_playlist(
        self, id: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "playlist/get",
            params={
                "playlist_id": id,
                "offset": offset,
                "limit": limit,
                "extra": "tracks",
            },
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        rjson = response.json()

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=rjson["tracks"]["total"],
            items=self._tracks_to_browse_categories(
                rjson["tracks"]["items"],
            ),
        )

    async def _browse_artist(
        self, id: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "artist/get",
            params={
                "artist_id": id,
                "offset": offset,
                "limit": limit,
                "extra": "albums",
            },
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        rjson = response.json()

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=rjson["albums"]["total"],
            items=self._albums_to_browse_category(response.json()["albums"]["items"]),
        )

    @_when_unlinked(_empty_page)
    async def list_favorite(
        self, type: SearchType, filter: str, offset: int = 0, limit: int = 50
    ) -> BrowseItemList:
        if type == SearchType.playlist:
            rjson = await self.qobuz_client.get_user_playlists(offset=0, limit=500)
        else:
            rjson = await self.qobuz_client.get_user_favorites(
                type, offset=0, limit=500
            )

        result = self._format_list_response(rjson, offset, limit)
        filtered_items = [
            item
            for item in result.items
            if (item.name and filter.lower() in item.name.lower())
            or (item.subname and filter.lower() in item.subname.lower())
        ]

        filtered_result = BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(filtered_items),
            items=filtered_items[offset : offset + limit],
        )

        return filtered_result

    async def _browse_catalog(
        self,
        endpoint: str,
        offset: int = 0,
        limit: int = 50,
        filter: Optional[FilterQuery] = None,
    ) -> BrowseItemList:
        genre_ids = _genre_values(endpoint, or_unfiltered(filter))

        if endpoint == "root":
            # Every shelf needs an account, so an unlinked Qobuz lists none.
            self.qobuz_client.require_account()
            all_items = [
                BrowseItem(
                    id=catalog_id("recent-releases"),
                    name="Recent Releases",
                    subname="The latest albums added to Qobuz",
                    can_browse=True,
                    can_add=False,
                    catalog=Catalog(
                        id=catalog_id("recent-releases"),
                        title="Recent Releases",
                        filters=[GENRE_FILTER],
                        preview_config=Preview(
                            type=PreviewType.CAROUSEL,
                            content_type=PreviewContentType.ALBUM,
                            items_count=5,
                            rows_count=1,
                            aspect_ratio=1.0,
                        ),
                        role=CatalogRole.HIDE_ON_HOME,
                    ),
                ),
                BrowseItem(
                    id=catalog_id("new-releases"),
                    name="New Releases",
                    subname="New albums spotlighted by Qobuz's editors",
                    can_browse=True,
                    can_add=False,
                    catalog=Catalog(
                        id=catalog_id("new-releases"),
                        title="New Releases",
                        filters=[GENRE_FILTER],
                        preview_config=Preview(
                            type=PreviewType.IMAGE_TEXT,
                            content_type=PreviewContentType.ALBUM,
                            items_count=20,
                            rows_count=2,
                            aspect_ratio=1.0,
                        ),
                        role=CatalogRole.DISCOVERY,
                    ),
                ),
                BrowseItem(
                    id=catalog_id("playlist-by-category"),
                    name="Playlist By Category",
                    subname="Qobuz's curated playlists, grouped by theme",
                    can_browse=True,
                    can_add=False,
                    catalog=Catalog(
                        id=catalog_id("playlist-by-category"),
                        title="Playlist By Category",
                        filters=[GENRE_FILTER],
                        preview_config=Preview(
                            type=PreviewType.TEXT_ONLY,
                            content_type=PreviewContentType.CATALOG,
                            items_count=20,
                            rows_count=2,
                            aspect_ratio=PLAYLIST_BANNER_RATIO,
                        ),
                        role=CatalogRole.INDEX,
                    ),
                ),
                BrowseItem(
                    id=catalog_id("qobuz-playlists"),
                    name="Qobuz Playlists",
                    subname="Playlists hand-picked by Qobuz's editors",
                    can_browse=True,
                    can_add=False,
                    catalog=Catalog(
                        id=catalog_id("qobuz-playlists"),
                        title="Qobuz Playlists",
                        filters=[GENRE_FILTER],
                        preview_config=Preview(
                            type=PreviewType.IMAGE_TEXT,
                            content_type=PreviewContentType.PLAYLIST,
                            rows_count=2,
                            items_count=20,
                            aspect_ratio=PLAYLIST_BANNER_RATIO,
                        ),
                        role=CatalogRole.HIDE_ON_HOME,
                    ),
                ),
                BrowseItem(
                    id=catalog_id("press-awards"),
                    name="Press Awards",
                    subname="Albums honoured by the music press",
                    can_browse=True,
                    can_add=False,
                    catalog=Catalog(
                        id=catalog_id("press-awards"),
                        title="Press Awards",
                        filters=[GENRE_FILTER],
                        preview_config=Preview(
                            type=PreviewType.IMAGE_TEXT,
                            content_type=PreviewContentType.ALBUM,
                            items_count=14,
                            rows_count=1,
                            aspect_ratio=1.0,
                            card_size=CardSize.LARGE,
                        ),
                        role=CatalogRole.FEATURED,
                    ),
                ),
                BrowseItem(
                    id=catalog_id("most-streamed"),
                    name="Most Streamed",
                    subname="Qobuz's most-streamed albums right now",
                    can_browse=True,
                    can_add=False,
                    catalog=Catalog(
                        id=catalog_id("most-streamed"),
                        title="Top Releases",
                        filters=[GENRE_FILTER],
                        preview_config=Preview(
                            type=PreviewType.IMAGE_TEXT,
                            content_type=PreviewContentType.ALBUM,
                            rows_count=2,
                            aspect_ratio=1.0,
                            items_count=20,
                            card_size=CardSize.SMALL,
                        ),
                        role=CatalogRole.DISCOVERY,
                    ),
                ),
            ]
            return BrowseItemList(
                offset=offset,
                limit=limit,
                total=len(all_items),
                items=all_items[offset : offset + limit],
            )
        elif endpoint == "recent-releases":
            res = await self._get_new_releases(
                "new-releases-full", offset, max(0, min(5 - offset, limit)), genre_ids
            )
            res.total = min(res.total, 5)
            return res
        elif endpoint == "new-releases":
            return await self._get_new_releases(
                "new-releases-full", offset, limit, genre_ids
            )
        elif endpoint == "qobuz-playlists":
            return await self._get_qobuz_playlists(offset, limit, genre_ids)
        elif endpoint == "playlist-by-category":
            return await self._get_playists_by_category(offset, limit, genre_ids)
        elif endpoint == "press-awards":
            return await self._get_new_releases(
                "press-awards", offset, limit, genre_ids
            )
        elif endpoint == "most-streamed":
            return await self._get_new_releases(
                "most-streamed", offset, limit, genre_ids
            )
        else:
            ep = endpoint.split("_")
            if len(ep) > 1:
                if ep[0] == "playlist-by-category":
                    return await self._get_qobuz_playlists(
                        offset, limit, genre_ids, ep[1]
                    )
                elif ep[0] == "album-suggestions":
                    return await self._suggest_albums_similar_to(ep[1], offset, limit)
                elif ep[0] == "playlist-suggestions":
                    return await self._suggest_playlists_similar_to(
                        ep[1], offset, limit
                    )
                elif ep[0] == "similar-artists":
                    return await self._suggest_artists_similar_to(ep[1], offset, limit)

        return EmptyList(offset, limit)

    async def _get_new_releases(
        self, type: str, offset: int, limit: int, genre_ids: List[str]
    ) -> BrowseItemList:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "/album/getFeatured",
            params={
                "type": type,
                "offset": offset,
                "limit": limit,
                "genre_ids": ",".join(genre_ids),
            },
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        rjson = response.json()

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=rjson["albums"]["total"],
            items=self._albums_to_browse_category(response.json()["albums"]["items"]),
        )

    async def _get_qobuz_playlists(
        self,
        offset: int,
        limit: int,
        genre_ids: List[str],
        tags: str | None = None,
    ):
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "/playlist/getFeatured",
            params={
                "type": "editor-picks",
                "offset": offset,
                "limit": limit,
                "genre_ids": ",".join(genre_ids),
                "tags": tags,
            },
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        rjson = response.json()

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=rjson["playlists"]["total"],
            items=self._playlists_to_browse_category(
                response.json()["playlists"]["items"]
            ),
        )

    async def _get_playists_by_category(
        self, offset: int, limit: int, genre_ids: List[str]
    ):
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "/playlist/getTags",
            params={
                "offset": offset,
                "limit": limit,
                "genre_ids": ",".join(genre_ids),
            },
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        tags = response.json()["tags"]

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(tags),
            items=[
                BrowseItem(
                    id=catalog_id("playlist-by-category_" + tags[i]["slug"]),
                    name=json.loads(tags[i]["name_json"])["en"],
                    can_browse=True,
                    can_add=False,
                    catalog=Catalog(
                        id=catalog_id("playlist-by-category_" + tags[i]["slug"]),
                        title=json.loads(tags[i]["name_json"])["en"],
                        filters=[GENRE_FILTER],
                        preview_config=Preview(
                            type=PreviewType.TEXT_ONLY,
                            aspect_ratio=PLAYLIST_BANNER_RATIO,
                        ),
                    ),
                )
                for i in range(len(tags))
            ][offset : offset + limit],
        )

    async def _get_artist_tracks(self, artist_id: str, offset: int, limit: int):
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "/artist/getTracks",
            params={
                "id": artist_id,
                "offset": offset,
                "limit": limit,
            },
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        rjson = response.json()

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=rjson["tracks"]["total"],
            items=self._tracks_to_browse_categories(response.json()["tracks"]["items"]),
        )

    async def get_track_info(self, track_ids: list[str]) -> list[TrackInfo]:
        if len(track_ids) == 0:
            return []

        all_tracks = []
        chunk_size = 49
        for i in range(0, len(track_ids), chunk_size):
            chunk = track_ids[i : i + chunk_size]
            tracks = await self.qobuz_client.get_tracks_meta([int(_) for _ in chunk])
            all_tracks.extend(tracks)
        return [self._track_to_track_info(track) for track in all_tracks]

    def _track_to_track_info(self, track):
        async def async_source_retriever():
            return await qobuz_source_retriever(
                self.qobuz_client, track["id"], self.format_id
            )

        track_info = TrackInfo(
            id=track_id(str(track["id"])),
            source_retriever=async_source_retriever,
            metadata=metadata_from_track(track),
        )

        return track_info

    def _tracks_to_browse_categories(self, tracks, album_meta={}):
        result = []
        for track in tracks:
            tid = str(track["id"])
            album = track.get("album", album_meta)
            album_version = album.get("version", None)

            browse_item = BrowseItem(
                id=track_id(tid),
                name=append_str(track["title"], track.get("version", None)),
                subname=(
                    track["performer"]["name"]
                    if "performer" in track
                    else album.get("artist", {"name": None})["name"]
                ),
                can_browse=False,
                can_add=True,
                track=Track(
                    id=track_id(tid),
                    title=append_str(track["title"], track.get("version", None)),
                    duration=track["duration"],
                    track_number=track.get("track_number"),
                    disc_number=track.get("media_number"),
                    performer=(
                        Artist(
                            id=artist_id(str(track["performer"]["id"])),
                            name=track["performer"]["name"],
                        )
                        if "performer" in track
                        else None
                    ),
                    album=Album(
                        id=album_id(str(album["id"])),
                        title=append_str(album["title"], album_version),
                        year=album_year(album),
                        artist=Artist(
                            name=album["artist"]["name"],
                            id=artist_id(str(album["artist"]["id"])),
                        ),
                        image=CoverImage(**album["image"]),
                    ),
                    playlist_track_id=str(track.get("playlist_track_id", None)),
                ),
            )
            if "favorited_at" in track:
                browse_item.timestamp = track["favorited_at"]

            result.append(browse_item)
        return result

    async def _search_items(self, item_type, query, offset, limit):
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + item_type.value + "/search",
            params={"query": query, "limit": limit, "offset": offset},
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        return self._format_list_response(response.json(), offset, limit)

    def _format_list_response(self, items, offset, limit):
        if "tracks" in items:
            return BrowseItemList(
                offset=offset,
                limit=limit,
                total=items["tracks"]["total"],
                items=self._tracks_to_browse_categories(items["tracks"]["items"]),
            )

        if "albums" in items:
            return BrowseItemList(
                offset=offset,
                limit=limit,
                total=items["albums"]["total"],
                items=self._albums_to_browse_category(items["albums"]["items"]),
            )

        if "playlists" in items:
            return BrowseItemList(
                offset=offset,
                limit=limit,
                total=items["playlists"]["total"],
                items=self._playlists_to_browse_category(items["playlists"]["items"]),
            )

        if "artists" in items:
            return BrowseItemList(
                offset=offset,
                limit=limit,
                total=items["artists"]["total"],
                items=self._artists_to_browse_category(items["artists"]["items"]),
            )

        return EmptyList(offset, limit)

    def _artists_to_browse_category(self, artists):
        return [
            BrowseItem(
                id=artist_id(str(artist["id"])),
                name=artist["name"],
                subname=None,
                can_browse=True,
                can_add=False,
                timestamp=artist.get("favorited_at", 0),
                artist=Artist(
                    id=artist_id(str(artist["id"])),
                    name=artist["name"],
                    image=(
                        CoverImage(
                            thumbnail=artist["image"].get("small", None),
                            small=artist["image"].get("medium", None),
                            large=artist["image"].get("large", None),
                        )
                        if artist["image"]
                        else None
                    ),
                    album_count=artist["albums_count"],
                ),
                sections=[
                    BrowseItem(
                        id=artist_id(str(artist["id"])),
                        name="Albums",
                        can_browse=True,
                        can_add=False,
                        catalog=Catalog(
                            id=artist_id(str(artist["id"])),
                            title="Albums",
                            preview_config=Preview(
                                type=PreviewType.TILE,
                                content_type=PreviewContentType.ALBUM,
                                items_count=10,
                                rows_count=1,
                                aspect_ratio=1.0,
                                card_size=CardSize.SMALL,
                            ),
                        ),
                    ),
                    BrowseItem(
                        id=catalog_id("similar-artists_" + str(artist["id"])),
                        name="Similar artists",
                        can_browse=True,
                        can_add=False,
                        catalog=Catalog(
                            id=catalog_id("similar-artists_" + str(artist["id"])),
                            title="Similar artists",
                            preview_config=Preview(
                                type=PreviewType.IMAGE_TEXT,
                                content_type=PreviewContentType.ARTIST,
                                items_count=5,
                                rows_count=1,
                                aspect_ratio=1.0,
                                card_size=CardSize.SMALL,
                            ),
                        ),
                    ),
                ],
            )
            for artist in artists
        ]

    def _albums_to_browse_category(self, albums):
        return [
            BrowseItem(
                id=album_id(str(album["id"])),
                name=append_str(album["title"], album.get("version", None)),
                subname=(
                    (artist := self._extract_artist_from_album(album)) and artist.name
                ),
                can_browse=True,
                can_add=True,
                timestamp=album.get("favorited_at", 0),
                album=Album(
                    id=album_id(str(album["id"])),
                    title=append_str(album["title"], album.get("version", None)),
                    year=album_year(album),
                    artist=artist,
                    image=(
                        CoverImage(
                            thumbnail=album["image"].get("thumbnail", None),
                            small=album["image"].get("small", None),
                            large=album["image"].get("large", None),
                        )
                        if "image" in album and album["image"]
                        else None
                    ),
                    duration=album["duration"],
                    track_count=album.get("track_count", album.get("tracks_count", 0)),
                    genres=[
                        Genre(
                            id=genre_id(str(album["genre"]["id"])),
                            name=album["genre"]["name"],
                        )
                    ],
                ),
                sections=[
                    BrowseItem(
                        id=album_id(str(album["id"])),
                        name="Tracks",
                        can_browse=True,
                        can_add=True,
                        catalog=Catalog(
                            id=album_id(str(album["id"])),
                            title="Tracks",
                            preview_config=Preview(
                                type=PreviewType.TILE_NUMBERED,
                                content_type=PreviewContentType.TRACK,
                                items_count=10,
                                rows_count=1,
                                aspect_ratio=1.0,
                                card_size=CardSize.SMALL,
                            ),
                        ),
                    ),
                    *(
                        [
                            BrowseItem(
                                id=artist.id,
                                name="More from this artist",
                                can_browse=True,
                                can_add=False,
                                catalog=Catalog(
                                    id=artist.id,
                                    title="More from this artist",
                                    preview_config=Preview(
                                        type=PreviewType.IMAGE_TEXT,
                                        content_type=PreviewContentType.ALBUM,
                                        items_count=10,
                                        rows_count=1,
                                        aspect_ratio=1.0,
                                        card_size=CardSize.SMALL,
                                    ),
                                ),
                            )
                        ]
                        if artist
                        else []
                    ),
                    BrowseItem(
                        id=catalog_id("album-suggestions_" + str(album["id"])),
                        name="You may also like",
                        can_browse=True,
                        can_add=False,
                        catalog=Catalog(
                            id=catalog_id("album-suggestions_" + str(album["id"])),
                            title="You may also like",
                            preview_config=Preview(
                                type=PreviewType.IMAGE_TEXT,
                                content_type=PreviewContentType.ALBUM,
                                items_count=5,
                                rows_count=1,
                                aspect_ratio=1.0,
                                card_size=CardSize.SMALL,
                            ),
                        ),
                    ),
                ],
            )
            for album in albums
        ]

    def _extract_artist_from_album(self, album) -> Optional[Artist]:
        if "artist" in album:
            return Artist(
                id=artist_id(str(album["artist"]["id"])),
                name=album["artist"]["name"],
            )
        elif "performer" in album:
            return Artist(
                id=artist_id(str(album["performer"]["id"])),
                name=album["performer"]["name"],
            )
        elif "artists" in album:
            return Artist(
                id=artist_id(str(album["artists"][0]["id"])),
                name=album["artists"][0]["name"],
            )
        else:
            return None

    def _playlists_to_browse_category(self, playlists):
        return [
            BrowseItem(
                id=playlist_id(str(playlist["id"])),
                name=playlist["name"],
                subname=playlist["owner"]["name"],
                can_browse=True,
                can_add=True,
                playlist=self._qobuz_playlist_to_playlist(playlist),
                sections=[
                    BrowseItem(
                        id=playlist_id(str(playlist["id"])),
                        name="Tracks",
                        can_browse=True,
                        can_add=True,
                        catalog=Catalog(
                            id=playlist_id(str(playlist["id"])),
                            title="Tracks",
                            preview_config=Preview(
                                type=PreviewType.TILE,
                                content_type=PreviewContentType.TRACK,
                                items_count=15,
                                rows_count=1,
                                aspect_ratio=1.0,
                                card_size=CardSize.SMALL,
                            ),
                        ),
                    ),
                    BrowseItem(
                        id=catalog_id("playlist-suggestions_" + str(playlist["id"])),
                        name="Similar playlists",
                        can_browse=True,
                        can_add=False,
                        catalog=Catalog(
                            id=catalog_id(
                                "playlist-suggestions_" + str(playlist["id"])
                            ),
                            title="Similar playlists",
                            preview_config=Preview(
                                type=PreviewType.IMAGE_TEXT,
                                content_type=PreviewContentType.PLAYLIST,
                                items_count=9,
                                rows_count=1,
                                aspect_ratio=PLAYLIST_BANNER_RATIO,
                                card_size=CardSize.SMALL,
                            ),
                        ),
                    ),
                ],
            )
            for playlist in playlists
        ]

    def _qobuz_playlist_to_playlist(self, playlist):
        square = _first(playlist.get("images150")) or _first(playlist.get("images"))
        large_square = _first(playlist.get("images300")) or square
        banner = _served_banner(_first(playlist.get("image_rectangle")))
        large = banner or large_square
        shape = PLAYLIST_BANNER_RATIO if banner else 1.0 if large else None

        return Playlist(
            id=playlist_id(str(playlist["id"])),
            name=playlist["name"],
            owner=Owner(
                name=playlist["owner"]["name"],
                id=user_id(str(playlist["owner"]["id"])),
            ),
            image=CoverImage(
                small=square,
                large=large,
                # A row leads with a square, which the banner's mini copy is not.
                thumbnail=square or large_square,
                aspect_ratio=shape,
            ),
            description=playlist["description"],
            track_count=playlist["tracks_count"],
        )

    @_when_unlinked(lambda arguments: FavoriteIds())
    async def get_favorite_ids(self) -> FavoriteIds:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "favorite/getUserFavoriteIds",
            params={"limit": 5000},
        )

        if response.is_success != True:
            return FavoriteIds()

        rjson = response.json()

        return FavoriteIds(
            albums=[album_id(str(id)) for id in rjson["albums"]],
            artists=[artist_id(str(id)) for id in rjson["artists"]],
            tracks=[track_id(str(id)) for id in rjson["tracks"]],
            playlists=await self._get_favorite_playlist_ids(),
        )

    async def _get_favorite_playlist_ids(self) -> list[EntityId]:
        rjson = await self.qobuz_client.get_user_playlists(limit=500)

        return [
            playlist_id(str(playlist["id"])) for playlist in rjson["playlists"]["items"]
        ]

    async def add_to_favorite(self, id: str):
        entity_id = EntityId.from_string(id)

        if entity_id.type == EntityType.PLAYLIST:
            endpoint = "playlist/subscribe"
            params = {"playlist_id": entity_id.id}
        else:
            endpoint = "favorite/create"
            params = {entity_id.type.value + "_ids": entity_id.id}

        response = await self.qobuz_client.session.post(
            self.qobuz_client.base + endpoint, params=params
        )

        response.raise_for_status()

        rjson = response.json()

        logger.info(f"Add to favorite response: {rjson}")

        if "status" not in rjson or rjson["status"] != "success":
            raise Exception(f"Failed to add to favorite: {response.text}")

    async def remove_from_favorite(self, id: str):
        entity_id = EntityId.from_string(id)

        if entity_id.type == EntityType.PLAYLIST:
            endpoint = "playlist/unsubscribe"
            params = {"playlist_id": entity_id.id}
        else:
            endpoint = "favorite/delete"
            params = {entity_id.type.value + "_ids": entity_id.id}

        response = await self.qobuz_client.session.post(
            self.qobuz_client.base + endpoint, params=params
        )

        response.raise_for_status()

        rjson = response.json()

        if "status" not in rjson or rjson["status"] != "success":
            raise Exception(f"Failed to remove from favorite: {response.text}")

    @_when_unlinked(_no_filter_values)
    async def list_filter_values(
        self,
        catalog_id: EntityId,
        field: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> FilterValueList:
        if field != "genre" or not _catalog_filters(catalog_id.id):
            raise UnsupportedFilter(field, "no such vocabulary here")

        # Asked for whole: the taxonomy is a few dozen entries, and narrowing
        # by label has to see all of them to page the result truthfully.
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "genre/list",
            params={"limit": GENRE_PAGE},
        )
        response.raise_for_status()

        needle = q.casefold()
        matching = [
            genre
            for genre in response.json()["genres"]["items"]
            if needle in genre["name"].casefold()
        ]
        return FilterValueList(
            offset=offset,
            limit=limit,
            total=len(matching),
            items=[
                FilterValue(id=str(genre["id"]), name=genre["name"])
                for genre in matching[offset : offset + limit]
            ],
        )

    async def get(self, entity_id: EntityId) -> BrowseItem:
        if entity_id.type == EntityType.ALBUM:
            return await self._album_get(entity_id.id)
        elif entity_id.type == EntityType.PLAYLIST:
            return await self._playlist_get(entity_id.id)
        elif entity_id.type == EntityType.ARTIST:
            return await self._artist_get(entity_id.id)
        elif entity_id.type == EntityType.TRACK:
            return await self._track_get(entity_id.id)
        else:
            raise ValueError(f"Unsupported EntityId type: {entity_id.type.name}")

    async def _album_get(self, id: str) -> BrowseItem:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "album/get",
            params={"album_id": id, "offset": 0, "limit": 0},
        )

        response.raise_for_status()

        rjson = response.json()

        return self._albums_to_browse_category([rjson])[0]

    async def _playlist_get(self, id: str) -> BrowseItem:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "playlist/get",
            params={
                "playlist_id": id,
                "offset": 0,
                "limit": 0,
            },
        )

        response.raise_for_status()

        rjson = response.json()

        return self._playlists_to_browse_category([rjson])[0]

    async def _artist_get(self, id: str) -> BrowseItem:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "artist/get",
            params={
                "artist_id": id,
            },
        )

        response.raise_for_status()

        rjson = response.json()

        return self._artists_to_browse_category([rjson])[0]

    async def _track_get(self, id: str) -> BrowseItem:
        rjson = await self.qobuz_client.get_track_meta(id)
        return self._tracks_to_browse_categories([rjson])[0]

    def _to_playlist_response(self, obj):
        return Playlist(
            id=playlist_id(str(obj["id"])),
            name=obj["name"],
            description=obj["description"],
            track_count=obj["tracks_count"],
            owner=Owner(
                name=obj["owner"]["name"],
                id=user_id(str(obj["owner"]["id"])),
            ),
        )

    @_when_unlinked(_empty_page)
    async def playlist_user_list(
        self, offset: int = 0, limit: int = 25
    ) -> BrowseItemList:
        rjson = await self.qobuz_client.get_user_playlists(
            offset=offset, limit=limit, owner_id=self.qobuz_client.user_id
        )

        return self._format_list_response(rjson, offset, limit)

    async def playlist_create(self, name, description) -> Playlist:
        response = await self.qobuz_client.session.post(
            self.qobuz_client.base + "playlist/create",
            params={"name": name, "description": description},
        )

        response.raise_for_status()

        rjson = response.json()

        return self._to_playlist_response(rjson)

    async def playlist_update(self, id, name, description) -> Playlist:
        response = await self.qobuz_client.session.post(
            self.qobuz_client.base + "playlist/update",
            params={
                "playlist_id": EntityId.from_string(id).id,
                "name": name,
                "description": description,
            },
        )

        response.raise_for_status()

        rjson = response.json()

        return self._to_playlist_response(rjson)

    async def playlist_delete(self, id):
        response = await self.qobuz_client.session.post(
            self.qobuz_client.base + "playlist/delete",
            params={"playlist_id": EntityId.from_string(id).id},
        )

        response.raise_for_status()

        return response.json()

    async def playlist_add_tracks(
        self, id: str, track_ids: List[str], allow_duplicates: bool
    ) -> Playlist:
        response = await self.qobuz_client.session.post(
            self.qobuz_client.base + "playlist/addTracks",
            params={
                "no_duplicate": not allow_duplicates,
                "playlist_id": EntityId.from_string(id).id,
                "track_ids": ",".join(
                    (EntityId.from_string(track_id).id for track_id in track_ids)
                ),
            },
        )

        response.raise_for_status()
        rjson = response.json()

        return self._to_playlist_response(rjson)

    async def playlist_remove_tracks(self, id, playlist_track_ids):
        response = await self.qobuz_client.session.post(
            self.qobuz_client.base + "playlist/deleteTracks",
            params={
                "playlist_id": EntityId.from_string(id).id,
                "playlist_track_ids": ",".join(playlist_track_ids),
            },
        )

        response.raise_for_status()
        rjson = response.json()

        return self._to_playlist_response(rjson)

    async def _suggest_albums_similar_to(
        self, id: str, offset: int = 0, limit: int = 25
    ) -> BrowseItemList:
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "album/suggest",
            params={"album_id": id},
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        rjson = response.json()
        albums = rjson["albums"]["items"][offset : offset + limit]

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=int(rjson["albums"]["limit"]),
            items=self._albums_to_browse_category(albums),
        )

    async def _suggest_playlists_similar_to(
        self, id: str, offset: int, limit: int = 10
    ):
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "playlist/get",
            params={"playlist_id": id, "extra": "getSimilarPlaylists"},
        )

        if response.is_success != True:
            return EmptyList(offset, limit)

        rjson = response.json()
        playlists = rjson["similarPlaylist"]["items"][offset : offset + limit]

        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=len(rjson["similarPlaylist"]["items"]),
            items=self._playlists_to_browse_category(playlists),
        )

    async def _suggest_artists_similar_to(self, id: str, offset: int, limit: int = 10):
        response = await self.qobuz_client.session.get(
            self.qobuz_client.base + "artist/getSimilarArtists",
            params={"artist_id": id, "offset": offset, "limit": limit},
        )
        if response.is_success != True:
            return EmptyList(offset, limit)
        rjson = response.json()
        artists = rjson["artists"]["items"]
        return BrowseItemList(
            offset=offset,
            limit=limit,
            total=rjson["artists"]["total"],
            items=self._artists_to_browse_category(artists),
        )

    async def get_resource_path(self, id) -> str | None:
        return None

    async def get_content_info(self, asset_id: str) -> Optional[ContentInfo]:
        name = asset_id.removeprefix(BANNER_ASSET)
        if name == asset_id or not BANNER_FILE.fullmatch(name):
            return None
        data = await self.qobuz_client.get_image(PLAYLIST_BANNER_BASE + name)
        if data is None:
            return None
        return ContentInfo(
            mime_type="image/jpeg", reader=lambda: io.BytesIO(data), size=len(data)
        )
