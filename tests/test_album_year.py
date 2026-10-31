"""The album's release year reaches every card and playback metadata."""

import pytest

from kalinka_plugin_qobuz.config_model import QobuzConfig
from kalinka_plugin_qobuz.qobuz import QobuzInputModule


def _api_album(**dates) -> dict:
    """One album as the Qobuz API returns it, trimmed to what the cards need."""
    return {
        "id": "alb1",
        "title": "Avalon",
        "artist": {"id": 7, "name": "Roxy Music"},
        "image": {"small": "https://img.qobuz.test/s.jpg"},
        "label": {"id": 3, "name": "Label"},
        "genre": {"id": 4, "name": "Rock"},
        "duration": 2280,
        "tracks_count": 10,
        **dates,
    }


@pytest.mark.parametrize("album_context", [False, True])
@pytest.mark.parametrize(
    "dates,expected",
    [
        ({"release_date_original": "1982-05-28"}, 1982),
        ({"released_at": 391392000}, 1982),
        ({"release_date_original": "1982-05-28", "released_at": 1262304000}, 1982),
        ({"release_date_original": None}, None),
        ({}, None),
    ],
)
def test_album_year_reaches_album_track_and_playback_metadata(
    album_context, dates, expected
):
    module = QobuzInputModule(QobuzConfig(), object())
    album = _api_album(**dates)
    track = {
        "id": 999,
        "title": "More Than This",
        "duration": 260,
        "performer": {"id": 7, "name": "Roxy Music"},
    }
    if not album_context:
        track["album"] = album

    album_item = module._albums_to_browse_category([album])[0]
    track_item = module._tracks_to_browse_categories([track], album_meta=album)[0]
    playback = module._track_to_track_info({**track, "album": album}).metadata

    assert album_item.album.model_dump(mode="json")["year"] == expected
    for metadata in (track_item.track, playback):
        assert metadata.model_dump(mode="json")["album"]["year"] == expected
