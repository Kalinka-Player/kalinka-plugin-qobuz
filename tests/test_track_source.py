"""What a track hands the server to play.

Qobuz signs a time-limited URL per play and serves the audio itself, so a
track resolves to a DirectUrl the renderer fetches — never to a ModuleAsset,
which would ask the server to serve bytes it does not have.
"""

import pytest
from kalinka_plugin_sdk.inputmodule import DirectUrl, TrackSource

from kalinka_plugin_qobuz.config_model import QobuzConfig
from kalinka_plugin_qobuz.qobuz import QobuzInputModule, qobuz_source_retriever


class _FakeClient:
    """Answers get_track_url the way the Qobuz API does, recording the format
    it was asked for."""

    def __init__(self):
        self.asked = []

    async def get_track_url(self, id, fmt_id):
        self.asked.append((id, fmt_id))
        return {
            "url": f"https://streaming.qobuz.test/{id}?signature=abc",
            "mime_type": "audio/flac",
        }


@pytest.mark.asyncio
async def test_source_retriever_returns_a_direct_url():
    client = _FakeClient()

    source = await qobuz_source_retriever(client, 12345, 27)

    assert source == TrackSource(
        source=DirectUrl(url="https://streaming.qobuz.test/12345?signature=abc"),
        format="audio/flac",
    )
    assert client.asked == [(12345, 27)]


def _api_track(track_id: int = 999) -> dict:
    """One track as the Qobuz API returns it, trimmed to what metadata needs."""
    return {
        "id": track_id,
        "title": "Song",
        "duration": 240,
        "performer": {"id": 7, "name": "Performer"},
        "album": {
            "id": "alb1",
            "title": "Album",
            "artist": {"id": 7, "name": "Performer"},
            "image": {"small": "https://img.qobuz.test/s.jpg"},
            "label": {"id": 3, "name": "Label"},
            "genre": {"id": 4, "name": "Jazz"},
        },
    }


@pytest.mark.asyncio
async def test_a_track_resolves_its_source_at_play_time():
    """The signed URL is minted per play, so the retriever must not have been
    called while the track was merely being listed."""
    client = _FakeClient()
    module = QobuzInputModule(QobuzConfig(), client)

    info = module._track_to_track_info(_api_track())
    assert client.asked == []

    source = await info.source_retriever()
    assert isinstance(source.source, DirectUrl)
    assert source.source.url.startswith("https://streaming.qobuz.test/999")
    assert client.asked == [(999, module.format_id)]


@pytest.mark.parametrize("album_context", [False, True])
@pytest.mark.parametrize(
    "numbers,expected",
    [
        ({"track_number": 7, "media_number": 2}, (7, 2)),
        ({"track_number": 7}, (7, None)),
        ({"media_number": 2}, (None, 2)),
        ({"track_number": None, "media_number": None}, (None, None)),
        ({}, (None, None)),
    ],
)
def test_track_positions_reach_browse_and_playback_metadata(
    album_context, numbers, expected
):
    module = QobuzInputModule(QobuzConfig(), _FakeClient())
    track = {**_api_track(), **numbers}
    playback = module._track_to_track_info(track).metadata
    album = track.pop("album") if album_context else {}
    item = module._tracks_to_browse_categories([track], album_meta=album)[0]

    for metadata in (item.track, playback):
        payload = metadata.model_dump(mode="json")
        assert (payload["track_number"], payload["disc_number"]) == expected
