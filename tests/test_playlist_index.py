"""Editorial playlists matched to a mood: which playlists are indexed, what a
refresh embeds, and what a search answers."""

import asyncio
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pytest

from kalinka_plugin_qobuz import playlist_index
from kalinka_plugin_qobuz.auth import AuthenticationError
from kalinka_plugin_qobuz.playlist_index import (
    EditorialPlaylist,
    PlaylistIndex,
    PlaylistStore,
    editorial_playlists,
    text_of,
)


VOCABULARY = ("rain", "party", "christmas", "sleep", "jazz")


class WordEmbedder:
    """Embeds a text as the vocabulary words it contains, so a query for a
    word lands nearest the playlists that name it."""

    model_id = "all-MiniLM-L6-v2"
    model_version = 1

    def __init__(self, available: bool = True, missing: tuple[str, ...] = ()):
        self.embedded: list[str] = []
        self.checked = False
        self._available = available
        self._missing = list(missing)

    async def available(self) -> bool:
        self.checked = True
        return self._available

    def missing_runtime(self) -> list[str]:
        return list(self._missing)

    async def embed(self, texts):
        self.embedded += texts
        return [self._vector(text.lower()) for text in texts]

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        weights = [1.0 if word in text else 0.0 for word in VOCABULARY] + [0.1]
        vector = np.array(weights, dtype=np.float32)
        return vector / np.linalg.norm(vector)


def playlist(
    id: int,
    name: str,
    description: Optional[str] = "",
    tracks: int = 10,
    genres: tuple[str, ...] = (),
    tags: tuple[dict, ...] = (),
) -> dict:
    """A playlist as Qobuz's featured listings return one."""
    return {
        "id": id,
        "name": name,
        "description": description,
        "tracks_count": tracks,
        "owner": {"id": 2, "name": "Qobuz"},
        "images300": [f"https://img.qobuz.test/{id}.jpg"],
        "genres": [{"id": n, "name": genre} for n, genre in enumerate(genres)],
        "tags": list(tags),
    }


def tag(slug: str, name: str, genre: bool = False) -> dict:
    """A playlist's tag: a category, a genre or a theme."""
    return {
        "slug": slug,
        "name_json": json.dumps({"fr": f"{name} (fr)", "en": name}),
        "genre_tag": {"genre_id": "1", "name": name} if genre else None,
    }


RAINY = playlist(1, "Rainy Day Chill", "Soft songs for the rain")
PARTY = playlist(2, "Party Hits", "Everything loud")
RAIN_JAZZ = playlist(3, "Rain and Jazz", "Late-night jazz while it pours with rain")


class Listing:
    """Stands in for what Qobuz lists now; a test changes it between refreshes."""

    def __init__(self, *playlists: dict):
        self.playlists = list(playlists)
        self.fails_with: Optional[Exception] = None
        self.calls = 0

    async def __call__(self) -> list[EditorialPlaylist]:
        self.calls += 1
        if self.fails_with is not None:
            raise self.fails_with
        return [
            EditorialPlaylist(json.loads(json.dumps(p)), text_of(p, set()))
            for p in self.playlists
        ]


def _store_path(tmp_path):
    return tmp_path / "qobuz" / "playlists.sqlite"


class Link:
    """Whether a Qobuz account is linked, as the credential holder says."""

    def __init__(self, linked: bool = True):
        self._linked = asyncio.Event()
        if linked:
            self._linked.set()

    def link(self) -> None:
        self._linked.set()

    def unlink(self) -> None:
        self._linked.clear()

    async def wait(self) -> None:
        await self._linked.wait()


def _index(tmp_path, listing, embedder=None, model=("all-MiniLM-L6-v2", 1), link=None):
    embedder = embedder or WordEmbedder()
    store = PlaylistStore(str(_store_path(tmp_path)), model)
    return PlaylistIndex(store, embedder, listing, (link or Link()).wait), embedder


async def _ready(tmp_path, *playlists, **kwargs):
    index, embedder = _index(tmp_path, Listing(*playlists), **kwargs)
    await index.load()
    await index.refresh()
    return index, embedder


async def _eventually(condition, timeout: float = 5) -> None:
    """Wait for ``condition()``, giving the store's threads real time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition never held"
        await asyncio.sleep(0.01)


async def _eventually_found(index, query: str, ids: list[int], timeout: float = 5) -> None:
    """Search until the answer is ``ids``: a refresh makes what it embedded
    searchable only once it has stored all of it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _ids(await index.search(query, 10)) != ids:
        assert loop.time() < deadline, f"{query!r} never found {ids}"
        await asyncio.sleep(0.01)


def _ids(records: list[dict]) -> list[int]:
    return [record["id"] for record in records]


@pytest.mark.asyncio
async def test_suggests_the_closest_playlists_first_and_none_that_barely_match(tmp_path):
    index, _ = await _ready(tmp_path, RAINY, PARTY, RAIN_JAZZ)

    assert _ids(await index.search("rain", 10)) == [1, 3]
    assert _ids(await index.search("rain", 1)) == [1]


@pytest.mark.asyncio
async def test_suggests_nothing_before_the_first_refresh(tmp_path):
    index, embedder = _index(tmp_path, Listing(RAINY))
    await index.load()

    assert await index.search("rain", 10) == []
    assert embedder.embedded == []


@pytest.mark.asyncio
async def test_a_refresh_embeds_only_new_and_changed_playlists(tmp_path):
    listing = Listing(RAINY, PARTY)
    index, embedder = _index(tmp_path, listing)
    await index.load()
    await index.refresh()
    assert len(embedder.embedded) == 2

    await index.refresh()
    assert len(embedder.embedded) == 2

    listing.playlists = [RAINY, playlist(2, "Party Hits", "Christmas party"), RAIN_JAZZ]
    await index.refresh()
    assert embedder.embedded[2:] == [
        "Party Hits. Christmas party",
        "Rain and Jazz. Late-night jazz while it pours with rain",
    ]
    assert _ids(await index.search("christmas", 10)) == [2]


@pytest.mark.asyncio
async def test_a_changed_record_shows_without_embedding_again(tmp_path):
    listing = Listing(RAINY)
    index, embedder = _index(tmp_path, listing)
    await index.load()
    await index.refresh()

    listing.playlists = [playlist(1, "Rainy Day Chill", "Soft songs for the rain", tracks=42)]
    await index.refresh()

    [record] = await index.search("rain", 10)
    assert record["tracks_count"] == 42
    assert len(embedder.embedded) == 1 + 1


@pytest.mark.asyncio
async def test_a_playlist_gone_from_qobuz_is_no_longer_suggested(tmp_path):
    listing = Listing(RAINY, RAIN_JAZZ)
    index, _ = _index(tmp_path, listing)
    await index.load()
    await index.refresh()

    listing.playlists = [RAIN_JAZZ]
    await index.refresh()

    assert _ids(await index.search("rain", 10)) == [3]


@pytest.mark.asyncio
async def test_vectors_outlive_a_restart(tmp_path):
    await _ready(tmp_path, RAINY, PARTY)

    index, embedder = _index(tmp_path, Listing(RAINY, PARTY))
    await index.load()

    assert _ids(await index.search("rain", 10)) == [1]
    assert embedder.embedded == ["rain"]


@pytest.mark.asyncio
async def test_another_model_embeds_every_playlist_again(tmp_path):
    await _ready(tmp_path, RAINY, PARTY)

    index, embedder = _index(
        tmp_path, Listing(RAINY, PARTY), model=("all-MiniLM-L6-v2", 2)
    )
    await index.load()
    assert await index.search("rain", 10) == []

    await index.refresh()
    assert embedder.embedded == [text_of(RAINY, set()), text_of(PARTY, set())]


@pytest.mark.asyncio
async def test_an_unreadable_store_is_started_over(tmp_path):
    _store_path(tmp_path).parent.mkdir(parents=True)
    _store_path(tmp_path).write_bytes(b"not a database at all " * 100)

    index, _ = await _ready(tmp_path, RAINY)

    assert _ids(await index.search("rain", 10)) == [1]


@pytest.mark.asyncio
async def test_a_store_deleted_while_in_use_suggests_nothing_until_the_next_refresh(tmp_path):
    index, _ = await _ready(tmp_path, RAINY)
    for path in _store_path(tmp_path).parent.iterdir():
        path.unlink()

    assert await index.search("rain", 10) == []

    await index.load()
    await index.refresh()
    assert _ids(await index.search("rain", 10)) == [1]


@pytest.mark.asyncio
async def test_run_keeps_going_when_the_store_cannot_be_opened(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(playlist_index, "RETRY_S", 0)
    (tmp_path / "qobuz").write_text("a file where the store's folder should be")
    index, _ = _index(tmp_path, Listing(RAINY))
    task = asyncio.create_task(index.run())
    try:
        await _eventually(lambda: "could not refresh" in caplog.text)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_it_can_answer_only_where_the_embedder_runs():
    assert _index(Path("unused"), Listing())[0].can_answer()
    lacking = WordEmbedder(missing=("onnxruntime",))
    assert not _index(Path("unused"), Listing(), embedder=lacking)[0].can_answer()


@pytest.mark.asyncio
async def test_an_embedder_without_its_model_cannot_answer(tmp_path):
    index, _ = _index(tmp_path, Listing(RAINY), embedder=WordEmbedder(available=False))

    await asyncio.wait_for(index.run(), timeout=5)

    assert not index.can_answer()


@pytest.mark.asyncio
async def test_run_neither_lists_nor_loads_the_embedder_until_an_account_is_linked(tmp_path):
    link = Link(linked=False)
    listing = Listing(RAINY)
    index, embedder = _index(tmp_path, listing, link=link)
    task = asyncio.create_task(index.run())
    try:
        await asyncio.sleep(0.05)
        assert listing.calls == 0
        assert not embedder.checked

        link.link()
        await _eventually_found(index, "rain", [1])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_an_unlink_during_a_refresh_waits_for_the_next_link(tmp_path):
    link = Link()
    listing = Listing(RAINY)

    async def unlinked_on_the_first_call() -> list[EditorialPlaylist]:
        if listing.calls == 0:
            listing.calls += 1
            link.unlink()
            raise AuthenticationError("not linked")
        return await listing()

    index, embedder = _index(tmp_path, unlinked_on_the_first_call, link=link)
    task = asyncio.create_task(index.run())
    try:
        await _eventually(lambda: listing.calls == 1)
        await asyncio.sleep(0.05)
        assert listing.calls == 1

        link.link()
        await _eventually(lambda: len(embedder.embedded) == 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_keeps_going_after_a_failed_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(playlist_index, "RETRY_S", 0)
    listing = Listing(RAINY)
    listing.fails_with = RuntimeError("Qobuz is down")
    index, embedder = _index(tmp_path, listing)
    task = asyncio.create_task(index.run())
    try:
        await asyncio.sleep(0.05)
        listing.fails_with = None
        await _eventually(lambda: len(embedder.embedded) == 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_without_the_embedder_lists_nothing(tmp_path):
    listing = Listing(RAINY)
    listing.fails_with = AssertionError("listed without an embedder")
    index, _ = _index(tmp_path, listing, embedder=WordEmbedder(available=False))

    await asyncio.wait_for(index.run(), timeout=5)


def test_a_playlist_is_matched_on_its_name_and_description():
    assert text_of(playlist(1, "Rainy Day", " Soft songs "), set()) == "Rainy Day. Soft songs"
    assert text_of(playlist(1, "Rainy Day", None), set()) == "Rainy Day"


def test_genres_and_themes_come_before_the_description_and_categories_not_at_all():
    """The embedder may cut a long description short, but never what leads."""
    record = playlist(
        1,
        "Snow Day",
        "Warm songs for cold days",
        genres=("Jazz", "Pop/Rock"),
        tags=(
            tag("mood", "Moods"),
            tag("jazz", "Jazz", genre=True),
            tag("advent", "Christmas"),
            tag("vs-christmas", "Christmas"),
            tag("newyearseve", "New Year's Eve"),
        ),
    )

    assert text_of(record, {"mood", "focus"}) == (
        "Snow Day. Genres: Jazz, Pop/Rock. Tags: Christmas, New Year's Eve. "
        "Warm songs for cold days"
    )


class _Response:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class CatalogueSession:
    """Answers playlist/getTags with the categories and pages
    playlist/getFeatured over every editorial playlist."""

    def __init__(self, everything: list[dict], categories: list[str]):
        self._everything = everything
        self._categories = categories
        self.requests: list[tuple[str, dict]] = []

    async def get(self, url: str, params: dict) -> _Response:
        endpoint = url.removeprefix("https://qobuz.test/")
        self.requests.append((endpoint, dict(params)))
        if endpoint == "playlist/getTags":
            return _Response({"tags": [{"slug": slug} for slug in self._categories]})
        assert endpoint == "playlist/getFeatured"
        assert params["type"] == "editor-picks"
        offset, limit = params["offset"], params["limit"]
        return _Response(
            {
                "playlists": {
                    "items": self._everything[offset : offset + limit],
                    "total": len(self._everything),
                }
            }
        )


class CatalogueClient:
    def __init__(self, session: CatalogueSession):
        self.base = "https://qobuz.test/"
        self.session = session


@pytest.mark.asyncio
async def test_playlists_listed_only_under_people_are_left_out(monkeypatch):
    monkeypatch.setattr(playlist_index, "PAGE", 2)
    mood, artist = tag("mood", "Moods"), tag("artist", "Artists")
    listening, label = tag("danslecasque", "Listening with..."), tag("label", "Label Stories")
    jazz = tag("jazz", "Jazz", genre=True)
    session = CatalogueSession(
        [
            playlist(1, "Rainy", tags=(mood,)),
            playlist(2, "Guest picks a mood", tags=(mood, listening)),
            playlist(3, "An artist", tags=(artist,)),
            playlist(4, "An artist's jazz", tags=(artist, jazz)),
            playlist(5, "A guest", tags=(listening,)),
            playlist(6, "A label", tags=(label,)),
            playlist(7, "Uncategorised", tags=(jazz,)),
        ],
        ["mood", "artist", "danslecasque", "label", "focus"],
    )

    kept = await editorial_playlists(CatalogueClient(session))

    assert [p.record["id"] for p in kept] == [1, 2, 7]
    assert kept[0].text == "Rainy"
    assert [
        (endpoint, params.get("offset")) for endpoint, params in session.requests
    ] == [
        ("playlist/getTags", None),
        ("playlist/getFeatured", 0),
        ("playlist/getFeatured", 2),
        ("playlist/getFeatured", 4),
        ("playlist/getFeatured", 6),
    ]
