"""Autoplay proposes Qobuz's own suggestions for the queued Qobuz tracks, and
leaves adding them to the queue to the server."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from kalinka_plugin_sdk.datamodel import Genre
from kalinka_plugin_sdk.inputmodule import ContinuationRequest, ModuleCapability

from kalinka_plugin_qobuz.auth import AuthenticationError
from kalinka_plugin_qobuz.config_model import QobuzConfig
from kalinka_plugin_qobuz.qobuz import QobuzInputModule, metadata_from_track
from kalinka_plugin_qobuz.qobuz_autoplay import QobuzAutoplay, autoplay_seed

from conftest import track_body


def _client(session, suggestions):
    return SimpleNamespace(
        session=session,
        base="https://qobuz.test/",
        get_tracks_meta=AsyncMock(return_value=[track_body(111)]),
        require_account=lambda: None,
    )


def _suggest(requests, ids):
    def answer(request):
        assert request.url.path == "/dynamic/suggest"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"tracks": {"items": [{"id": i} for i in ids]}, "algorithm": "test"}
        )

    return answer


@pytest.mark.asyncio
async def test_suggestions_are_asked_with_the_seeds_metadata_and_what_was_heard():
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_suggest(requests, [222, 333, 444]))) as session:
        client = _client(session, None)
        module = QobuzInputModule(QobuzConfig(), client)
        module._autoplay.amount_to_request = 3

        proposed = await module.continue_queue(
            ContinuationRequest(seeds=["111"], exclude=["111", "100"], count=2)
        )

    assert proposed == ["222", "333"]
    client.get_tracks_meta.assert_awaited_once_with([111])
    [sent] = requests
    assert sent["limit"] == 3
    assert sent["listened_tracks_ids"] == [100]
    assert sent["track_to_analysed"][0]["track_id"] == 111


@pytest.mark.asyncio
async def test_later_requests_are_served_from_the_same_batch():
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_suggest(requests, [222, 333, 444]))) as session:
        autoplay = QobuzAutoplay(_client(session, None), metadata_from_track)

        first = await autoplay.propose(ContinuationRequest(seeds=["111"], count=1))
        second = await autoplay.propose(ContinuationRequest(seeds=["111", "222"], exclude=["222"], count=1))

    assert (first, second) == (["222"], ["333"])
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_a_turned_down_track_asks_for_a_new_batch_without_it_as_a_seed():
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_suggest(requests, [222, 333]))) as session:
        client = _client(session, None)
        autoplay = QobuzAutoplay(client, metadata_from_track)

        await autoplay.propose(ContinuationRequest(seeds=["111"], count=1))
        again = await autoplay.propose(
            ContinuationRequest(seeds=["111", "222"], avoid=["222"], exclude=["111", "222"], count=1)
        )

    assert again == ["333"]
    assert len(requests) == 2
    assert client.get_tracks_meta.await_args_list[-1].args == ([111],)


@pytest.mark.asyncio
async def test_nothing_is_proposed_without_a_seed_or_a_linked_account():
    autoplay = QobuzAutoplay(None, metadata_from_track)
    assert await autoplay.propose(ContinuationRequest(seeds=["5"], avoid=["5"], count=1)) == []

    def unlinked():
        raise AuthenticationError("not linked")

    module = QobuzInputModule(QobuzConfig(), SimpleNamespace(require_account=unlinked))
    assert await module.continue_queue(ContinuationRequest(seeds=["111"])) == []
    assert ModuleCapability.CONTINUATION not in module.capabilities()
    assert ModuleCapability.FAVORITES not in module.capabilities()


def test_a_linked_account_offers_continuation_and_favourites():
    module = QobuzInputModule(QobuzConfig(), SimpleNamespace(require_account=lambda: None))

    assert module.capabilities() == frozenset(
        {ModuleCapability.CATALOGS, ModuleCapability.CONTINUATION, ModuleCapability.FAVORITES}
    )


@pytest.mark.parametrize("genres", [[], [4], [4, 7]])
def test_a_seed_is_described_by_its_artist_first_genre_label_and_id(genres):
    seed = metadata_from_track(track_body(111))
    seed.album.genres = [Genre(id=f"kalinka:qobuz:genre:{g}", name=f"Genre {g}") for g in genres]

    assert autoplay_seed(seed) == {
        "artist_id": 5,
        "genre_id": genres[0] if genres else None,
        "label_id": 3,
        "track_id": 111,
    }


def test_sparse_metadata_still_describes_a_seed():
    seed = metadata_from_track(track_body(111))
    seed.performer = None
    seed.album.genres = []
    seed.album.label = None

    assert autoplay_seed(seed) == {"artist_id": None, "genre_id": None, "label_id": None, "track_id": 111}
