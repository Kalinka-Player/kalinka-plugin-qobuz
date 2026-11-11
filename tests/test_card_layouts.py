"""Which Qobuz listings a client opens as cards: every one of catalogs,
albums or curated playlists, so the root and each shelf on it, each playlist
theme, and an artist's albums."""

import pytest
from kalinka_plugin_sdk.datamodel import (
    EntityId,
    EntityType,
    PreviewContentType,
    PreviewType,
)

from conftest import fake_module

CARDS = (PreviewType.IMAGE_TEXT, PreviewType.CAROUSEL)


def _module(payload=None):
    module, _ = fake_module(payload)
    return module


def _catalog(slug):
    return EntityId(id=slug, type=EntityType.CATALOG, source="qobuz")


def test_the_root_opens_as_cards_of_its_catalogs():
    preview = _module().root_preview()
    assert preview.type == PreviewType.IMAGE_TEXT
    assert preview.content_type == PreviewContentType.CATALOG


@pytest.mark.asyncio
async def test_every_shelf_on_the_root_opens_as_cards():
    shelves = (await _module().browse(_catalog("root"))).items
    assert shelves
    assert [
        shelf.name
        for shelf in shelves
        if shelf.catalog.preview_config.type not in CARDS
    ] == []


@pytest.mark.asyncio
async def test_each_playlist_theme_opens_as_cards_of_its_playlists():
    tags = {"tags": [{"slug": "jazz", "name_json": '{"en": "Jazz"}'}]}
    (theme,) = (await _module(tags).browse(_catalog("playlist-by-category"))).items
    assert theme.catalog.preview_config.type == PreviewType.IMAGE_TEXT
    assert theme.catalog.preview_config.content_type == PreviewContentType.PLAYLIST


def test_an_artists_albums_open_as_cards():
    (artist,) = _module()._artists_to_browse_category(
        [{"id": 7, "name": "Nina Simone", "image": None, "albums_count": 3}]
    )
    albums = next(section for section in artist.sections if section.name == "Albums")
    assert albums.catalog.preview_config.type == PreviewType.IMAGE_TEXT
    assert albums.catalog.preview_config.content_type == PreviewContentType.ALBUM
