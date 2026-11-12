"""Mood search's runtime is offered as optional packages.

The server does not install onnxruntime or tokenizers, so the plugin offers
them, reports what the shared embedder says it lacks once Qobuz is linked,
and ships the allow-list the install helper checks requests against.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from kalinka_plugin_sdk import ModuleHealthState, ModuleState, optional_packages_manifest

from kalinka_plugin_qobuz import module_setup, optional_packages
from kalinka_plugin_qobuz.config_model import QobuzConfig
from kalinka_plugin_qobuz.module_setup import KalinkaPluginQobuz

SCRIPT = Path(optional_packages.__file__)


class FakeEmbedder:
    """Names the runtime it lacks, as the server's shared embedder does."""

    def __init__(self, missing=()):
        self._missing = list(missing)

    def missing_runtime(self) -> list[str]:
        return list(self._missing)


class FakePairing:
    def __init__(self, state: ModuleState, linked: bool):
        self._state = state
        self.link = SimpleNamespace(linked=linked)

    def health(self) -> ModuleState:
        return self._state


LINKED = ModuleState(state=ModuleHealthState.READY)


@pytest.fixture(autouse=True)
def on_a_64_bit_pi(monkeypatch):
    monkeypatch.setattr(module_setup, "wheel_architecture", lambda: "aarch64")


def _plugin(
    missing=(), pairing=LINKED, linked=True, embedder=True, **config
) -> KalinkaPluginQobuz:
    plugin = KalinkaPluginQobuz()
    plugin._pairing = FakePairing(pairing, linked)
    plugin._context = SimpleNamespace(
        config=QobuzConfig(**config),
        embedder=FakeEmbedder(missing) if embedder else None,
    )
    return plugin


@pytest.mark.asyncio
async def test_a_missing_runtime_is_offered_for_install():
    plugin = _plugin(missing=["onnxruntime", "tokenizers"])

    state = await plugin.get_state()

    assert state.state is ModuleHealthState.WARNING
    assert state.missing_packages == ["onnxruntime", "tokenizers"]
    assert await plugin.required_packages() == ["onnxruntime", "tokenizers"]


@pytest.mark.asyncio
async def test_a_runtime_this_box_cannot_install_names_the_setting(monkeypatch):
    monkeypatch.setattr(module_setup, "wheel_architecture", lambda: "armv7l")
    plugin = _plugin(missing=["onnxruntime", "tokenizers"])

    state = await plugin.get_state()

    assert state.state is ModuleHealthState.WARNING
    assert not state.missing_packages
    assert "`onnxruntime`" in state.message
    assert "**Mood / AI search** off" in state.message
    assert await plugin.required_packages() == []


@pytest.mark.asyncio
async def test_a_runtime_package_this_plugin_does_not_offer_says_to_update():
    plugin = _plugin(missing=["sentencepiece"])

    state = await plugin.get_state()

    assert state.state is ModuleHealthState.WARNING
    assert "`sentencepiece`" in state.message
    assert "Update the plugin" in state.message
    assert await plugin.required_packages() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plugin",
    [
        pytest.param(lambda: _plugin(), id="runtime installed"),
        pytest.param(
            lambda: _plugin(missing=["onnxruntime"], ai_search_enabled=False),
            id="setting off",
        ),
        pytest.param(
            lambda: _plugin(missing=["onnxruntime"], embedder=False),
            id="server without an embedder",
        ),
    ],
)
async def test_nothing_to_install(plugin):
    plugin = plugin()
    assert (await plugin.get_state()).state is ModuleHealthState.READY
    assert await plugin.required_packages() == []


@pytest.mark.asyncio
async def test_a_player_never_linked_installs_nothing():
    """Nothing on Qobuz works unlinked, so a box that never linked it is not
    worth the runtime's download."""
    unlinked = ModuleState(state=ModuleHealthState.ERROR, message="Not linked")
    plugin = _plugin(missing=["onnxruntime"], pairing=unlinked, linked=False)

    assert await plugin.get_state() is unlinked
    assert await plugin.required_packages() == []


@pytest.mark.asyncio
async def test_a_link_on_its_way_back_says_so_and_still_queues_the_runtime():
    reconnecting = ModuleState(state=ModuleHealthState.WARNING, message="Reconnecting")
    plugin = _plugin(missing=["onnxruntime"], pairing=reconnecting, linked=True)

    assert await plugin.get_state() is reconnecting
    assert await plugin.required_packages() == ["onnxruntime"]


def test_the_plugin_offers_the_runtime():
    assert KalinkaPluginQobuz.OPTIONAL_PACKAGES is optional_packages.OPTIONAL_PACKAGES
    assert set(optional_packages.OPTIONAL_PACKAGES) == {"onnxruntime", "tokenizers"}


def test_run_as_a_script_it_prints_the_manifest():
    """The deb build runs the file, not the package, so the plugin's runtime
    deps need not be installed where it builds."""
    printed = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True
    ).stdout

    assert json.loads(printed) == optional_packages_manifest(
        "qobuz", optional_packages.OPTIONAL_PACKAGES
    )
