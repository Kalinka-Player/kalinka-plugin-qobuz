"""Autoplay consumes current SDK metadata and appends real track information."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from kalinka_plugin_sdk.datamodel import Genre
from kalinka_plugin_sdk.events import RequestMoreTracksEvent, TracksAddedEvent

from kalinka_plugin_qobuz.config_model import QobuzConfig
from kalinka_plugin_qobuz.qobuz import QobuzInputModule, metadata_from_track
from kalinka_plugin_qobuz.qobuz_autoplay import QobuzAutoplay

from conftest import track_body


@pytest.mark.asyncio
@pytest.mark.parametrize("genres", [[], [4], [4, 7]])
async def test_recommendations_use_first_album_genre_and_extend_queue(genres):
    seed = metadata_from_track(track_body(111))
    seed.album.genres = [
        Genre(id=f"kalinka:qobuz:genre:{genre}", name=f"Genre {genre}")
        for genre in genres
    ]
    requests = []

    def suggest(request):
        assert request.url.path == "/dynamic/suggest"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"tracks": {"items": [{"id": 222}, {"id": 333}]}, "algorithm": "test"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(suggest)) as session:
        client = SimpleNamespace(
            session=session,
            base="https://qobuz.test/",
            get_tracks_meta=AsyncMock(return_value=[track_body(222)]),
        )
        queue = SimpleNamespace(add=AsyncMock())
        browser = QobuzInputModule(QobuzConfig(), client)
        autoplay = QobuzAutoplay(client, queue, browser, amount_to_request=2)
        autoplay.add_tracks(TracksAddedEvent(tracks=[seed], index=0))

        await autoplay.add_recommendation(RequestMoreTracksEvent())

        assert requests == [{
            "limit": 2,
            "listened_tracks_ids": [],
            "track_to_analysed": [{
                "artist_id": 5,
                "genre_id": genres[0] if genres else None,
                "label_id": 3,
                "track_id": 111,
            }],
        }]
        client.get_tracks_meta.assert_awaited_once_with([222])
        queue.add.assert_awaited_once()
        [added] = queue.add.call_args.args[0]
        assert added.id.source == "qobuz" and added.id.id == "222"
        assert added.metadata.title == "Synthetic song 222"
        assert autoplay.remaining_tracks == [333]


def test_autoplay_accepts_sparse_queue_metadata():
    seed = metadata_from_track(track_body(111))
    seed.performer = None
    seed.album.genres = []
    seed.album.label = None
    autoplay = QobuzAutoplay(None, None, None)

    assert autoplay._track_meta_to_autoplay(seed) == {
        "artist_id": None,
        "genre_id": None,
        "label_id": None,
        "track_id": 111,
    }
