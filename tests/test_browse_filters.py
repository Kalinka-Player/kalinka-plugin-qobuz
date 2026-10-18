"""Which Qobuz shelves filter, and how a selection becomes genre_ids.

Qobuz's featured endpoints union the ids they are given and take no text
parameter, so those shelves declare genre alone and only ``any``.
"""

import pytest
from kalinka_plugin_sdk.datamodel import EntityId, EntityType
from kalinka_plugin_sdk.filters import FilterOp, FilterQuery, UnsupportedFilter
from kalinka_plugin_sdk.inputmodule import ModuleCapability

import kalinka_plugin_qobuz.qobuz as qz


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.is_success = True

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _Session:
    """Records every request; answers with an empty featured payload."""

    def __init__(self, payload=None):
        self.calls = []
        self._payload = payload or {"albums": {"items": [], "total": 0}}

    async def get(self, url, params=None):
        self.calls.append((url, params or {}))
        return _Response(self._payload)


class _Client:
    def __init__(self, payload=None):
        self.base = "https://qobuz.test/"
        self.session = _Session(payload)


def _module(payload=None):
    client = _Client(payload)
    module = qz.QobuzInputModule.__new__(qz.QobuzInputModule)
    module.qobuz_client = client
    module.format_id = 5
    return module, client


def test_qobuz_offers_its_catalogs_as_a_home_source():
    module, _ = _module()
    assert module.capabilities() == frozenset({ModuleCapability.CATALOGS})


def _catalog(slug: str) -> EntityId:
    return EntityId(id=slug, type=EntityType.CATALOG, source="qobuz")


def _query(document) -> FilterQuery:
    return FilterQuery.model_validate(document)



@pytest.mark.parametrize(
    "endpoint",
    ["recent-releases", "new-releases", "press-awards", "most-streamed"],
)
def test_featured_shelves_declare_genre_only(endpoint):
    specs = qz._catalog_filters(endpoint)
    assert [spec.id for spec in specs] == ["genre"]
    # No text parameter exists on these endpoints, so no text field is offered.
    assert specs[0].ops == [FilterOp.ANY]


def test_a_playlist_category_filters_like_its_parent():
    assert qz._catalog_filters("playlist-by-category_jazz") == [qz.GENRE_FILTER]


def test_a_shelf_without_filters_declares_none():
    assert qz._catalog_filters("album-suggestions_123") == []



@pytest.mark.asyncio
async def test_selected_genres_become_comma_joined_ids():
    module, client = _module()
    await module.browse(
        _catalog("new-releases"), filter=_query({"genre": {"any": ["64", "112"]}})
    )

    _, params = client.session.calls[0]
    assert params["genre_ids"] == "64,112"


@pytest.mark.asyncio
async def test_an_unfiltered_shelf_sends_no_genre_ids():
    module, client = _module()
    await module.browse(_catalog("new-releases"))

    _, params = client.session.calls[0]
    assert params["genre_ids"] == ""



@pytest.mark.asyncio
async def test_intersection_is_refused_rather_than_quietly_unioned():
    module, client = _module()
    with pytest.raises(UnsupportedFilter) as excinfo:
        await module.browse(
            _catalog("new-releases"), filter=_query({"genre": {"all": ["64", "112"]}})
        )

    assert excinfo.value.field == "genre"
    assert client.session.calls == []


@pytest.mark.asyncio
async def test_text_on_a_featured_shelf_is_refused():
    module, client = _module()
    with pytest.raises(UnsupportedFilter) as excinfo:
        await module.browse(
            _catalog("new-releases"), filter=_query({"q": {"contains": "miles"}})
        )

    assert excinfo.value.field == "q"
    assert client.session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "type",
    [EntityType.ALBUM, EntityType.ARTIST, EntityType.PLAYLIST],
)
async def test_a_listing_that_offers_no_filters_refuses_one(type):
    module, client = _module()
    with pytest.raises(UnsupportedFilter):
        await module.browse(
            EntityId(id="1", type=type, source="qobuz"),
            filter=_query({"genre": {"any": ["64"]}}),
        )
    assert client.session.calls == []



GENRES = {
    "genres": {
        "total": 3,
        "items": [
            {"id": 64, "name": "Jazz"},
            {"id": 112, "name": "Rock"},
            {"id": 91, "name": "Classical"},
        ],
    }
}


@pytest.mark.asyncio
async def test_vocabulary_comes_from_qobuz_with_its_own_ids():
    module, _ = _module(GENRES)
    values = await module.list_filter_values(_catalog("new-releases"), "genre")

    assert values.total == 3
    assert [(value.id, value.name) for value in values.items][:1] == [("64", "Jazz")]


@pytest.mark.asyncio
async def test_vocabulary_narrows_by_label():
    module, _ = _module(GENRES)
    values = await module.list_filter_values(_catalog("new-releases"), "genre", q="cla")

    assert [value.name for value in values.items] == ["Classical"]
    assert values.total == 1


@pytest.mark.asyncio
async def test_vocabulary_pages_what_it_counts():
    module, client = _module(GENRES)
    values = await module.list_filter_values(
        _catalog("new-releases"), "genre", offset=1, limit=1
    )

    # The whole taxonomy is asked for once and paged here, so total and the
    # page it slices are the same list.
    assert client.session.calls[0][1]["limit"] == qz.GENRE_PAGE
    assert values.total == 3
    assert [value.name for value in values.items] == ["Rock"]


@pytest.mark.asyncio
async def test_no_vocabulary_where_the_field_is_not_offered():
    module, _ = _module(GENRES)
    with pytest.raises(UnsupportedFilter):
        await module.list_filter_values(_catalog("new-releases"), "year")
    with pytest.raises(UnsupportedFilter):
        await module.list_filter_values(_catalog("album-suggestions_1"), "genre")
