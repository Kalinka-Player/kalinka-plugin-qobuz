"""How a Qobuz playlist's pictures map onto a cover's sizes."""

import httpx
import pytest

import kalinka_plugin_qobuz.qobuz as qz
from kalinka_plugin_qobuz.auth import QobuzAuth, TokenHolder
from kalinka_plugin_sdk.inputmodule import SourceUnavailableError

from conftest import ISSUED_UAT, uat

BANNER = "6297595_0837491bcfafee492601093f83d60d01_rectangle.jpg"


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


def test_a_banner_qobuz_keeps_from_browsers_is_linked_through_the_server():
    image = _image(image_rectangle=[qz.PLAYLIST_BANNER_BASE + BANNER])
    assert image.large == f"/content/qobuz/banner-{BANNER}"
    assert image.aspect_ratio == qz.PLAYLIST_BANNER_RATIO


def test_a_playlist_without_a_banner_is_square():
    image = _image(images150=["cover-150"], images300=["cover-300"])
    assert image.large == "cover-300"
    assert image.aspect_ratio == 1.0
    assert image.thumbnail == "cover-150"


def test_a_playlist_without_pictures_states_no_shape():
    image = _image()
    assert image.large is None
    assert image.aspect_ratio is None


def _module(answer):
    requests = []

    def handle(request):
        requests.append(request)
        return answer(request)

    holder = TokenHolder()
    holder.install(uat())
    client = qz.QobuzClient(
        auth=QobuzAuth(holder), transport=httpx.MockTransport(handle)
    )
    module = qz.QobuzInputModule.__new__(qz.QobuzInputModule)
    module.qobuz_client = client
    return module, requests


@pytest.mark.asyncio
async def test_a_banner_is_served_from_qobuz_without_the_account():
    module, requests = _module(lambda r: httpx.Response(200, content=b"jpeg"))

    info = await module.get_content_info(f"banner-{BANNER}")

    assert info.mime_type == "image/jpeg"
    assert info.size == 4
    assert info.reader().read() == b"jpeg"
    assert str(requests[0].url) == qz.PLAYLIST_BANNER_BASE + BANNER
    assert ISSUED_UAT not in str(requests[0].headers)
    await module.qobuz_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset_id",
    [BANNER, "banner-../../covers/x.jpg", "banner-1_abc_rectangle.jpg", "dash-1"],
)
async def test_only_a_banner_is_fetched(asset_id):
    module, requests = _module(lambda r: httpx.Response(200, content=b"jpeg"))

    assert await module.get_content_info(asset_id) is None
    assert requests == []


@pytest.mark.asyncio
async def test_a_banner_qobuz_no_longer_holds_is_absent():
    module, _ = _module(lambda r: httpx.Response(403))

    assert await module.get_content_info(f"banner-{BANNER}") is None


@pytest.mark.asyncio
async def test_an_unreachable_store_is_a_passing_failure():
    def refuse(request):
        raise httpx.ConnectError("refused")

    for answer in (refuse, lambda r: httpx.Response(503)):
        module, _ = _module(answer)
        with pytest.raises(SourceUnavailableError):
            await module.get_content_info(f"banner-{BANNER}")
