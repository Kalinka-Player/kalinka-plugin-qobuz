"""The plugin as the server drives it: setup never waits on Qobuz, an unlinked
source degrades instead of breaking merged results, and pairing never touches
the play queue."""

import asyncio
import contextlib
import functools
import logging

import httpx
import pytest
import pytest_asyncio
from kalinka_plugin_sdk.datamodel import EntityId, EntityType, FavoriteIds
from kalinka_plugin_sdk.events import TracksRemovedEvent
from kalinka_plugin_sdk.inputmodule import ModuleCapability, SearchType, SourceUnavailableError
from kalinka_plugin_sdk.module_health import ModuleHealthState

from kalinka_plugin_qobuz import module_setup
from kalinka_plugin_qobuz.account import Validated
from kalinka_plugin_qobuz.auth import AuthenticationError
from kalinka_plugin_qobuz.config_model import QobuzConfig
from kalinka_plugin_qobuz.connect.pairing import PairingService, Phase
from kalinka_plugin_qobuz.connect.receiver import HandoffReceiver
from kalinka_plugin_qobuz.connect.store import LinkState, LinkStore, default_store_path

from kalinka_plugin_qobuz.connect import cloud_link
from kalinka_plugin_qobuz.connect.proto import qconnect_pb2 as qc

from conftest import ACCOUNT, BUNDLE, FakeAdvertiser, assert_no_secrets, bearer, handoff_body, settle
from fake_cloud import FakeCloud
from test_cloud_renderer import FakeDirect


class _Listener:
    def __init__(self):
        self.events: asyncio.Queue = asyncio.Queue()

    @contextlib.asynccontextmanager
    async def stream(self, event_types):
        async def items():
            while True:
                yield await self.events.get()

        yield items()


class _PlayQueueSpy:
    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"pairing reached the play queue: {name}")


class _Context:
    def __init__(self, config: QobuzConfig):
        self.config = config
        self.listener = _Listener()
        self.playqueue = _PlayQueueSpy()
        self.direct_playback = None
        self.embedder = None


async def _validate(client, holder):
    client.sec = "secret"
    return Validated(account=ACCOUNT, credential=holder.current())


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Everything but the loopback pairing endpoint stays off the network."""
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))

    async def bundle():
        return BUNDLE

    monkeypatch.setattr(
        module_setup,
        "PairingService",
        functools.partial(
            PairingService,
            load_bundle=bundle,
            validate=_validate,
            advertiser=FakeAdvertiser(),
            make_receiver=lambda handlers, port: HandoffReceiver(handlers, host="127.0.0.1", port=0),
        ),
    )


@pytest_asyncio.fixture
async def cloud(monkeypatch):
    monkeypatch.setattr(cloud_link, "BACKOFF_START_S", 0.01)
    fake = await FakeCloud().start()
    yield fake
    await fake.stop()


async def _set_up(config=None, direct=None, embedder=None):
    plugin = module_setup.KalinkaPluginQobuz()
    context = _Context(config or QobuzConfig())
    if direct is not None:
        context.direct_playback = direct
    context.embedder = embedder
    await plugin.setup(context)
    await settle(lambda: plugin._pairing.phase in (Phase.WAITING, Phase.LINKED))
    return plugin, context


@pytest.mark.asyncio
async def test_setup_finishes_unlinked_and_says_how_to_link(offline):
    plugin, _ = await _set_up()

    assert plugin.get_interface() is not None
    state = await plugin.get_state()
    assert state.state is ModuleHealthState.ERROR
    assert "choose Kalinka (" in state.message
    status = await plugin.resolve_dynamic_field(module_setup.STATUS_FIELD)
    assert "**Not linked.**" in status
    with pytest.raises(KeyError):
        await plugin.resolve_dynamic_field("anything.else")
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_an_unlinked_source_lists_nothing(offline):
    plugin, _ = await _set_up()
    module = plugin.get_interface()
    shelf = EntityId(id="new-releases", type=EntityType.CATALOG, source="qobuz")
    root = EntityId(id="root", type=EntityType.CATALOG, source="qobuz")

    assert (await module.browse(root)).items == []
    assert (await module.browse(shelf, 0, 10)).items == []
    assert (await module.playlist_user_list(0, 10)).items == []
    assert (await module.search(SearchType.album, "x", 0, 10)).items == []
    assert (await module.list_favorite(SearchType.track, "", 0, 10)).items == []
    assert await module.get_favorite_ids() == FavoriteIds()
    assert (await module.list_filter_values(shelf, "genre", 0, 10)).items == []
    with pytest.raises(AuthenticationError, match="not linked"):
        await module.get_track_info(["123"])
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_an_unlinked_track_is_unavailable_with_the_reason(offline):
    plugin, _ = await _set_up()
    info = plugin.get_interface()._track_to_track_info(
        {
            "id": 999,
            "title": "Song",
            "duration": 240,
            "performer": {"id": 7, "name": "Performer"},
            "album": {
                "id": "alb1",
                "title": "Album",
                "image": {"small": "https://img.qobuz.test/s.jpg"},
                "label": {"id": 3, "name": "Label"},
                "genre": {"id": 4, "name": "Jazz"},
            },
        }
    )

    with pytest.raises(SourceUnavailableError, match="not linked"):
        await info.source_retriever()
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_a_handoff_over_http_links_without_touching_the_play_queue(offline, caplog):
    plugin, context = await _set_up()
    port = plugin._pairing._receiver.port

    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient() as http:
            response = await http.post(
                f"http://127.0.0.1:{port}/streamcore/connect-to-qconnect", content=handoff_body()
            )
        await settle(lambda: plugin._pairing.phase is Phase.LINKED)

    assert response.status_code == 200
    assert context.playqueue.touched == []
    assert (await plugin.get_state()).state is ModuleHealthState.READY
    root = EntityId(id="root", type=EntityType.CATALOG, source="qobuz")
    assert (await plugin.get_interface().browse(root)).total > 0
    assert "**Linked** as Synthetic Listener (Studio)" in await plugin.resolve_dynamic_field(
        module_setup.STATUS_FIELD
    )
    assert_no_secrets(caplog.text)
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_an_armed_unpair_forgets_the_link_and_reopens_pairing(offline):
    store = LinkStore(default_store_path())
    identity = store.load_or_create()
    store.save(LinkState(identity.device_uuid, True, 1, bearer(), ACCOUNT))

    plugin, _ = await _set_up(QobuzConfig(unpair=True))

    assert plugin._pairing.phase is Phase.WAITING
    stored = store.load_or_create()
    assert not stored.linked and stored.credential is None
    assert stored.device_uuid == identity.device_uuid
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_a_failing_event_does_not_stop_autoplay_and_reporting(offline, caplog):
    plugin, context = await _set_up()

    with caplog.at_level(logging.WARNING):
        await context.listener.events.put(TracksRemovedEvent(indices=[5]))
        await settle(lambda: "could not handle TracksRemovedEvent" in caplog.text)

    assert not plugin._qobuz_tasks.done()
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_shutdown_leaves_nothing_running(offline):
    before = asyncio.all_tasks()
    plugin, _ = await _set_up()
    port = plugin._pairing._receiver.port

    await plugin.shutdown()

    assert asyncio.all_tasks() - before == set()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    assert plugin._client.session.is_closed


class _Embedder:
    """The server's shared embedder, whose model is still on its way."""

    model_id = "all-MiniLM-L6-v2"
    model_version = 1

    def __init__(self, missing: tuple[str, ...] = ()):
        self._missing = list(missing)

    async def available(self) -> bool:
        await asyncio.Event().wait()
        return True

    def missing_runtime(self) -> list[str]:
        return list(self._missing)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config, embedder, offered",
    [
        pytest.param(QobuzConfig(), _Embedder(), True, id="on"),
        pytest.param(QobuzConfig(ai_search_enabled=False), _Embedder(), False, id="off"),
        pytest.param(QobuzConfig(), None, False, id="server without an embedder"),
        pytest.param(
            QobuzConfig(), _Embedder(missing=("onnxruntime",)), False, id="runtime missing"
        ),
    ],
)
async def test_mood_search_needs_the_setting_an_embedder_that_runs_here_and_a_link(
    offline, config, embedder, offered
):
    plugin, _ = await _set_up(config, embedder=embedder)
    assert ModuleCapability.AI_SEARCH not in plugin.get_interface().capabilities()

    await _link_over_http(plugin)

    capabilities = plugin.get_interface().capabilities()
    assert (ModuleCapability.AI_SEARCH in capabilities) is offered
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_shutdown_stops_the_playlist_refresh(offline):
    before = asyncio.all_tasks()
    plugin, _ = await _set_up(embedder=_Embedder())

    await plugin.shutdown()

    assert asyncio.all_tasks() - before == set()


async def _link_over_http(plugin, **handoff):
    port = plugin._pairing._receiver.port
    async with httpx.AsyncClient() as http:
        response = await http.post(
            f"http://127.0.0.1:{port}/streamcore/connect-to-qconnect", content=handoff_body(**handoff)
        )
    assert response.status_code == 200
    await settle(lambda: plugin._pairing.phase is Phase.LINKED)


@pytest.mark.asyncio
async def test_on_an_older_server_linking_says_app_playback_needs_a_newer_one(offline):
    plugin, _ = await _set_up()

    await _link_over_http(plugin)

    status = await plugin.resolve_dynamic_field(module_setup.STATUS_FIELD)
    assert "needs a newer Kalinka server" in status
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_linking_joins_the_accounts_connect_session(offline, cloud):
    plugin, context = await _set_up(direct=FakeDirect())

    await _link_over_http(plugin, endpoint=cloud.url)
    await cloud.until(lambda: qc.RNDR_SRVR_JOIN_SESSION in cloud.kinds())
    await cloud.until(lambda: cloud.states())

    status = await plugin.resolve_dynamic_field(module_setup.STATUS_FIELD)
    assert "**Linked**" in status
    assert "**Qobuz Connect:** selected in the Qobuz app." in status
    assert context.playqueue.touched == []
    await plugin.shutdown()
    assert cloud.states()[-1].playing_state == 1
