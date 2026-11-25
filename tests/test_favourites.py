"""The linked account's favourites: read whole from Qobuz's own lists, paged
by Qobuz, and changed only where a change can stick."""

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from kalinka_plugin_sdk.datamodel import EntityId, EntityType, FavoriteIds
from kalinka_plugin_sdk.filters import UnsupportedFilter
from kalinka_plugin_sdk.inputmodule import SearchType, SourceUnavailableError

from conftest import fake_module, track_body
from kalinka_plugin_qobuz.auth import REJECTED_LINK, AuthenticationError
from kalinka_plugin_qobuz.favourites import Favourites

BASE = "https://www.qobuz.com/api.json/0.2/"
LISTENER = 7
OWN, SUBSCRIBED, UNKNOWN = "11", "12", "13"


def _favourite_track(id):
    """A row of favorite/getUserFavorites, as far as the plugin reads one."""
    track = track_body(id)
    artist = {"id": 5, "name": "Synthetic Performer"}
    return {**track, "album": {**track["album"], "artist": artist}, "favorited_at": 1700000000 + id}


class Account:
    """The listener's favourites on Qobuz, answered as its API answers.

    ``status`` forces an answer for an endpoint, ``failing`` breaks the
    connection for one, and ``body`` replaces what one answers.
    """

    def __init__(self):
        self.marked = {"tracks": [3, 2, 1], "albums": ["a1"], "artists": [5]}
        self.playlists = {OWN: LISTENER, SUBSCRIBED: 99}
        self.elsewhere = {UNKNOWN: 99}
        self.status: dict[str, int] = {}
        self.body: dict[str, dict] = {}
        self.failing: set[str] = set()
        self.calls: list[tuple[str, str, dict]] = []

    def endpoints(self):
        return [endpoint for _, endpoint, _ in self.calls]

    def handle(self, request: httpx.Request) -> httpx.Response:
        endpoint = str(request.url).split("?")[0].removeprefix(BASE)
        params = dict(request.url.params)
        self.calls.append((request.method, endpoint, params))
        if endpoint in self.failing:
            raise httpx.ConnectError("no route", request=request)
        if endpoint in self.status:
            return httpx.Response(self.status[endpoint], json={"status": "error"})
        if endpoint in self.body:
            return httpx.Response(200, json=self.body[endpoint])
        return httpx.Response(200, json=getattr(self, endpoint.replace("/", "_"))(params))

    def favorite_getUserFavoriteIds(self, params):
        return dict(self.marked)

    def favorite_getUserFavorites(self, params):
        offset, limit = int(params["offset"]), int(params["limit"])
        ids = self.marked[params["type"]]
        rows = [_favourite_track(id) for id in ids[offset : offset + limit]]
        return {params["type"]: {"offset": offset, "limit": limit, "total": len(ids), "items": rows}}

    def playlist_getUserPlaylists(self, params):
        offset, limit = int(params["offset"]), int(params["limit"])
        rows = [
            {"id": int(id), "owner": {"id": owner}} for id, owner in self.playlists.items()
        ]
        return {"playlists": {"total": len(rows), "items": rows[offset : offset + limit]}}

    def playlist_get(self, params):
        id = params["playlist_id"]
        owner = {**self.playlists, **self.elsewhere}[id]
        return {"id": int(id), "owner": {"id": owner}}

    def favorite_create(self, params):
        for kind in self.marked:
            if kind[:-1] + "_ids" in params:
                self.marked[kind].insert(0, params[kind[:-1] + "_ids"])
        return {"status": "success"}

    def favorite_delete(self, params):
        for kind in self.marked:
            id = params.get(kind[:-1] + "_ids")
            self.marked[kind] = [m for m in self.marked[kind] if str(m) != id]
        return {"status": "success"}

    def playlist_subscribe(self, params):
        self.playlists[params["playlist_id"]] = 99
        return {"status": "success"}

    def playlist_unsubscribe(self, params):
        self.playlists.pop(params["playlist_id"])
        return {"status": "success"}


@pytest.fixture
def account():
    return Account()


@pytest_asyncio.fixture
async def client(account):
    session = httpx.AsyncClient(transport=httpx.MockTransport(account.handle))
    yield SimpleNamespace(
        session=session, base=BASE, user_id=LISTENER, require_account=lambda: None
    )
    await session.aclose()


@pytest.fixture
def favourites(client):
    return Favourites(client)


@pytest.fixture
def module(client, favourites):
    module, _ = fake_module()
    module.qobuz_client = client
    module._favourites = favourites
    return module


def _id(kind, id):
    return EntityId(id=id, type=kind, source="qobuz")


@pytest.mark.asyncio
async def test_every_favourite_is_reported_and_a_playlist_of_ones_own_is_not(favourites, account):
    assert await favourites.ids() == {
        EntityType.TRACK: ["3", "2", "1"],
        EntityType.ALBUM: ["a1"],
        EntityType.ARTIST: ["5"],
        EntityType.PLAYLIST: [SUBSCRIBED],
    }
    assert sorted(account.endpoints()) == [
        "favorite/getUserFavoriteIds", "playlist/getUserPlaylists",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["favorite/getUserFavoriteIds", "playlist/getUserPlaylists"])
@pytest.mark.parametrize("status", [500, 429])
async def test_ids_fail_whole_rather_than_leave_a_kind_out(favourites, account, endpoint, status):
    account.status[endpoint] = status
    with pytest.raises(SourceUnavailableError):
        await favourites.ids()


@pytest.mark.asyncio
async def test_a_kind_qobuz_leaves_out_has_none(favourites, account):
    account.body["favorite/getUserFavoriteIds"] = {"albums": ["a1"], "artists": []}
    ids = await favourites.ids()
    assert (ids[EntityType.TRACK], ids[EntityType.ALBUM]) == ([], ["a1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    {"albums": [], "artists": [], "tracks": {"items": []}},
    ["tracks"],
])
async def test_ids_qobuz_answers_in_another_shape_fail(favourites, account, answer):
    account.body["favorite/getUserFavoriteIds"] = answer
    with pytest.raises(SourceUnavailableError):
        await favourites.ids()


@pytest.mark.asyncio
async def test_ids_cut_off_at_qobuz_s_limit_fail_rather_than_pass_for_all(
    favourites, account
):
    account.marked["tracks"] = list(range(5000))
    with pytest.raises(SourceUnavailableError, match="more favourites"):
        await favourites.ids()


@pytest.mark.asyncio
async def test_subscribed_playlists_are_read_past_qobuz_s_first_page(favourites, account):
    account.playlists = {str(id): LISTENER for id in range(100, 580)}
    account.playlists.update({str(id): 99 for id in range(600, 640)})
    ids = await favourites.ids()
    assert ids[EntityType.PLAYLIST] == [str(id) for id in range(600, 640)]
    offsets = [
        params["offset"]
        for _, endpoint, params in account.calls
        if endpoint == "playlist/getUserPlaylists"
    ]
    assert offsets == ["0", "500"]


@pytest.mark.asyncio
async def test_a_link_qobuz_rejects_says_so(favourites, account):
    account.status["favorite/getUserFavoriteIds"] = 401
    with pytest.raises(SourceUnavailableError, match="unpair Qobuz"):
        await favourites.ids()
    assert REJECTED_LINK


@pytest.mark.asyncio
async def test_a_page_is_qobuzs_own_page_however_deep(favourites, account):
    account.marked["tracks"] = list(range(700, 0, -1))
    page = (await favourites.page(SearchType.track, 600, 50))["tracks"]
    assert (page["total"], [row["id"] for row in page["items"]]) == (
        700, list(range(100, 50, -1)),
    )
    assert account.calls == [(
        "GET", "favorite/getUserFavorites",
        {"type": "tracks", "offset": "600", "limit": "50"},
    )]


@pytest.mark.asyncio
async def test_favourite_playlists_are_the_subscribed_ones(favourites, account):
    account.playlists.update({"21": 99, "22": 99})
    page = (await favourites.page(SearchType.playlist, 1, 5))["playlists"]
    assert (page["total"], [row["id"] for row in page["items"]]) == (3, [21, 22])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,id,create,field", [
    (EntityType.TRACK, "42", "favorite/create", "track_ids"),
    (EntityType.ALBUM, "a2", "favorite/create", "album_ids"),
    (EntityType.ARTIST, "9", "favorite/create", "artist_ids"),
])
async def test_a_favourite_is_marked_and_unmarked_through_its_own_field(
    favourites, account, kind, id, create, field
):
    await favourites.add(_id(kind, id))
    assert account.calls == [("POST", create, {field: id})]
    assert id in (await favourites.ids())[kind]
    account.calls.clear()
    await favourites.remove(_id(kind, id))
    assert account.calls == [("POST", "favorite/delete", {field: id})]
    assert id not in (await favourites.ids())[kind]


@pytest.mark.asyncio
async def test_a_playlist_is_subscribed_to_and_unsubscribed_from(favourites, account):
    await favourites.add(_id(EntityType.PLAYLIST, UNKNOWN))
    assert account.endpoints() == ["playlist/get", "playlist/subscribe"]
    assert UNKNOWN in (await favourites.ids())[EntityType.PLAYLIST]
    account.calls.clear()
    await favourites.remove(_id(EntityType.PLAYLIST, SUBSCRIBED))
    assert account.endpoints() == ["playlist/get", "playlist/unsubscribe"]
    assert SUBSCRIBED not in (await favourites.ids())[EntityType.PLAYLIST]


@pytest.mark.asyncio
async def test_a_playlist_of_ones_own_is_refused_before_qobuz_is_asked_to_change_it(
    favourites, account
):
    for change in (favourites.add, favourites.remove):
        with pytest.raises(ValueError, match="your own"):
            await change(_id(EntityType.PLAYLIST, OWN))
    assert account.endpoints() == ["playlist/get", "playlist/get"]
    assert account.playlists[OWN] == LISTENER


@pytest.mark.asyncio
@pytest.mark.parametrize("item,error", [
    (EntityId(id="1", type=EntityType.TRACK, source="tidal"), LookupError),
    (_id(EntityType.CATALOG, "new-releases"), ValueError),
    (_id(EntityType.GENRE, "4"), ValueError),
    (_id(EntityType.USER, "7"), ValueError),
])
async def test_what_can_never_be_a_qobuz_favourite_is_refused_unsent(
    favourites, account, item, error
):
    for change in (favourites.add, favourites.remove):
        with pytest.raises(error):
            await change(item)
    assert account.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [
    (404, LookupError), (400, SourceUnavailableError), (500, SourceUnavailableError),
])
async def test_a_change_qobuz_refuses_is_told_apart(favourites, account, status, error):
    account.status.update({"favorite/create": status, "favorite/delete": status})
    for change in (favourites.add, favourites.remove):
        with pytest.raises(error):
            await change(_id(EntityType.TRACK, "42"))


@pytest.mark.asyncio
async def test_a_change_qobuz_does_not_confirm_is_not_reported_made(favourites, account):
    account.body["favorite/create"] = {"status": "error"}
    with pytest.raises(SourceUnavailableError):
        await favourites.add(_id(EntityType.TRACK, "42"))


@pytest.mark.asyncio
async def test_an_unreachable_qobuz_is_unavailable_not_a_crash(favourites, account):
    account.failing.update({"favorite/create", "favorite/getUserFavoriteIds"})
    with pytest.raises(SourceUnavailableError):
        await favourites.add(_id(EntityType.TRACK, "42"))
    with pytest.raises(SourceUnavailableError):
        await favourites.ids()


@pytest.mark.asyncio
async def test_a_change_while_unlinked_reports_that_nothing_changed(favourites, client):
    async def unlinked(*args, **kwargs):
        raise AuthenticationError("Qobuz is not linked")

    client.session.request = unlinked
    with pytest.raises(SourceUnavailableError, match="not linked"):
        await favourites.add(_id(EntityType.TRACK, "42"))
    with pytest.raises(AuthenticationError):
        await favourites.ids()


@pytest.mark.asyncio
async def test_the_module_reports_ids_as_entities(module):
    assert await module.get_favorite_ids() == FavoriteIds(
        tracks=[_id(EntityType.TRACK, id) for id in ("3", "2", "1")],
        albums=[_id(EntityType.ALBUM, "a1")],
        artists=[_id(EntityType.ARTIST, "5")],
        playlists=[_id(EntityType.PLAYLIST, SUBSCRIBED)],
    )


@pytest.mark.asyncio
async def test_favourite_tracks_list_with_when_each_was_marked(module):
    page = await module.list_favorite(SearchType.track, "", 1, 2)
    assert [(item.id.id, item.timestamp) for item in page.items] == [
        ("2", 1700000002), ("1", 1700000001),
    ]
    assert (page.total, page.offset, page.limit) == (3, 1, 2)


@pytest.mark.asyncio
async def test_a_search_of_favourites_is_refused_rather_than_ignored(module, account):
    with pytest.raises(UnsupportedFilter) as refused:
        await module.list_favorite(SearchType.track, "blue")
    assert refused.value.field == "filter"
    assert account.calls == []


@pytest.mark.asyncio
async def test_the_root_leads_to_each_kind_of_favourite(module, account):
    def catalog(slug):
        return _id(EntityType.CATALOG, slug)

    root = (await module.browse(catalog("root"))).items
    assert "My Favourites" in [shelf.name for shelf in root]
    kinds = (await module.browse(catalog("favourites"))).items
    assert [kind.name for kind in kinds] == [
        "Favourite Tracks", "Favourite Albums", "Favourite Artists", "Favourite Playlists",
    ]
    account.calls.clear()
    tracks = await module.browse(catalog("favourite-tracks"), 0, 2)
    assert [item.id.id for item in tracks.items] == ["3", "2"]
    assert account.calls == [(
        "GET", "favorite/getUserFavorites",
        {"type": "tracks", "offset": "0", "limit": "2"},
    )]
