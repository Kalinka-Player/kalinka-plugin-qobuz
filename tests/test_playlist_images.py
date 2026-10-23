"""How a Qobuz playlist's pictures map onto a cover's sizes."""

import kalinka_plugin_qobuz.qobuz as qz


def _image(**pictures):
    module = qz.QobuzInputModule.__new__(qz.QobuzInputModule)
    playlist = {
        "id": 1,
        "name": "Jazz Classics",
        "owner": {"name": "Qobuz", "id": 2},
        "description": "",
        "tracks_count": 3,
        **pictures,
    }
    return module._qobuz_playlist_to_playlist(playlist).image


def test_an_editorial_playlist_shows_its_banner_and_says_its_shape():
    image = _image(
        images150=["cover-150"],
        images300=["cover-300"],
        image_rectangle=["banner"],
        image_rectangle_mini=["banner-mini"],
    )
    assert image.large == "banner"
    assert image.aspect_ratio == qz.PLAYLIST_BANNER_RATIO
    # A row leads with a square, never the banner's mini copy.
    assert image.thumbnail == image.small == "cover-150"


def test_a_playlist_without_a_banner_is_square():
    image = _image(images150=["cover-150"], images300=["cover-300"])
    assert image.large == "cover-300"
    assert image.aspect_ratio == 1.0
    assert image.thumbnail == "cover-150"


def test_a_playlist_without_pictures_states_no_shape():
    image = _image()
    assert image.large is None
    assert image.aspect_ratio is None
