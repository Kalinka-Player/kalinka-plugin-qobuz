"""Which Qobuz listings a client opens as cards: every one of catalogs or
albums, so the root and each shelf on it, and an artist's albums."""

import pytest
from kalinka_plugin_sdk.datamodel import (
    EntityId,
    EntityType,
    PreviewContentType,
    PreviewType,
)

import kalinka_plugin_qobuz.qobuz as qz

CARDS = (PreviewType.IMAGE_TEXT, PreviewType.CAROUSEL)


class _Client:
    def require_account(self):
        return None


def _module():
    module = qz.QobuzInputModule.__new__(qz.QobuzInputModule)
    module.qobuz_client = _Client()
    return module


def test_the_root_opens_as_cards_of_its_catalogs():
    preview = _module().root_preview()
    assert preview.type == PreviewType.IMAGE_TEXT
    assert preview.content_type == PreviewContentType.CATALOG


@pytest.mark.asyncio
async def test_every_shelf_on_the_root_opens_as_cards():
    root = EntityId(id="root", type=EntityType.CATALOG, source="qobuz")
    shelves = (await _module().browse(root)).items
    assert shelves
    assert [
        shelf.name
        for shelf in shelves
        if shelf.catalog.preview_config.type not in CARDS
    ] == []


def test_an_artists_albums_open_as_cards():
    (artist,) = _module()._artists_to_browse_category(
        [{"id": 7, "name": "Nina Simone", "image": None, "albums_count": 3}]
    )
    albums = next(section for section in artist.sections if section.name == "Albums")
    assert albums.catalog.preview_config.type == PreviewType.IMAGE_TEXT
    assert albums.catalog.preview_config.content_type == PreviewContentType.ALBUM
