"""AI search answers with Qobuz's editorial playlists, as one card."""

import pytest
from kalinka_plugin_sdk.datamodel import PreviewContentType
from kalinka_plugin_sdk.inputmodule import ModuleCapability

from kalinka_plugin_qobuz.auth import AuthenticationError

from conftest import fake_module
from test_playlist_index import PARTY, RAIN_JAZZ, RAINY


class FixedIndex:
    """Suggests the same playlists for any query."""

    def __init__(self, *playlists: dict, answers: bool = True):
        self._playlists = list(playlists)
        self._answers = answers
        self.asked: list[tuple[str, int]] = []

    def can_answer(self) -> bool:
        return self._answers

    async def search(self, query: str, limit: int) -> list[dict]:
        self.asked.append((query, limit))
        return self._playlists[:limit]


@pytest.mark.asyncio
async def test_suggestions_are_one_card_of_playlists_best_first():
    module, _ = fake_module(playlist_index=FixedIndex(RAINY, RAIN_JAZZ))

    result = await module.ai_search("rain", 0, 20)

    [card] = result.items
    assert card.catalog.preview_config.content_type is PreviewContentType.PLAYLIST
    assert [item.name for item in card.sections] == ["Rainy Day Chill", "Rain and Jazz"]
    assert [item.playlist.id.id for item in card.sections] == ["1", "3"]
    assert card.catalog.preview_config.items_count == 2


@pytest.mark.asyncio
async def test_a_page_skips_what_earlier_pages_showed():
    index = FixedIndex(RAINY, PARTY, RAIN_JAZZ)
    module, _ = fake_module(playlist_index=index)

    [card] = (await module.ai_search("rain", 1, 1)).items

    assert [item.name for item in card.sections] == ["Party Hits"]
    assert index.asked == [("rain", 2)]


@pytest.mark.asyncio
async def test_nothing_without_an_index_a_query_or_a_match():
    without_index, _ = fake_module()
    assert (await without_index.ai_search("rain")).items == []

    module, _ = fake_module(playlist_index=FixedIndex())
    assert (await module.ai_search("rain")).items == []
    assert (await module.ai_search("   ")).items == []


@pytest.mark.asyncio
async def test_an_unlinked_source_suggests_nothing():
    module, client = fake_module(playlist_index=FixedIndex(RAINY))

    def unlinked():
        raise AuthenticationError("not linked")

    client.require_account = unlinked

    assert (await module.ai_search("rain")).items == []


def test_ai_search_is_offered_only_with_an_index_that_can_answer():
    module, _ = fake_module(playlist_index=FixedIndex())
    assert module.capabilities() == frozenset(
        {ModuleCapability.CATALOGS, ModuleCapability.AI_SEARCH}
    )

    for without in (fake_module()[0], fake_module(playlist_index=FixedIndex(answers=False))[0]):
        assert without.capabilities() == frozenset({ModuleCapability.CATALOGS})
