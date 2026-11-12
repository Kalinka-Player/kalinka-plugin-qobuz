import asyncio
import functools
import logging
from typing import ClassVar, Optional
from kalinka_plugin_sdk import (
    DynamicFieldDecl,
    OptionalPackageSpec,
    PlayQueueEventType,
    PlaybackStateChangedEvent,
    RequestMoreTracksEvent,
    TracksAddedEvent,
    TracksRemovedEvent,
    wheel_architecture,
)
from kalinka_plugin_sdk.module_health import ModuleHealthState, ModuleState
from kalinka_plugin_sdk.plugin import InputPluginContext, InputModulePlugin
from kalinka_plugin_sdk.inputmodule import InputModule

from .auth import Credential, QobuzAuth, TokenHolder
from .config_model import QobuzConfig
from .connect.cloud_service import ConnectService
from .connect.pairing import (
    Phase,
    PairingService,
    default_device_name,
    software_version,
)
from .connect.store import LinkStore, default_store_path
from .optional_packages import OPTIONAL_PACKAGES
from .playlist_index import (
    PlaylistIndex,
    PlaylistStore,
    default_index_path,
    editorial_playlists,
)
from .qobuz_autoplay import QobuzAutoplay
from .qobuz_reporter import QobuzReporter
from .qobuz import QobuzClient, QobuzInputModule

logger = logging.getLogger(__name__.split(".")[-1])

STATUS_FIELD = "connect_status"

_KEY_BY_IMPORT_NAME = {
    spec.import_name or key: key for key, spec in OPTIONAL_PACKAGES.items()
}


class KalinkaPluginQobuz(InputModulePlugin):
    REQUIRES_SDK = ">=3.18,<4"
    PLUGIN_ID = "qobuz"
    CONFIG_MODEL = QobuzConfig
    OPTIONAL_PACKAGES: ClassVar[dict[str, OptionalPackageSpec]] = OPTIONAL_PACKAGES
    DYNAMIC_FIELDS: ClassVar[dict[str, DynamicFieldDecl]] = {
        STATUS_FIELD: DynamicFieldDecl(
            section_id="",
            label="Qobuz account",
            widget="rich_text",
            value_type="str",
        ),
    }

    def __init__(self):
        self.reporter = None
        self.interface: Optional[InputModule] = None
        self._qobuz_tasks = None
        self._client: Optional[QobuzClient] = None
        self._pairing: Optional[PairingService] = None
        self._connect: Optional[ConnectService] = None
        # Captured at setup so the package checks read the current config.
        self._context: Optional[InputPluginContext] = None
        self._index_task: Optional[asyncio.Task] = None

    def get_interface(self) -> Optional[InputModule]:
        return self.interface

    async def get_state(self) -> ModuleState:
        """Linking comes first; once linked, mood search without its runtime
        is a WARNING naming the packages for the modules page to offer, or,
        where this box cannot install them, the setting to turn off."""
        if self._pairing is not None:
            state = self._pairing.health()
            if state.state is not ModuleHealthState.READY:
                return state
        to_install, no_wheel, not_offered = self._mood_search_lacks()
        if no_wheel:
            return ModuleState(
                state=ModuleHealthState.WARNING,
                message=(
                    "Mood search is not available on this device: "
                    + ", ".join(f"`{name}`" for name in no_wheel)
                    + f" cannot be installed on {wheel_architecture()}. Turn "
                    "**Mood / AI search** off in this module's settings."
                ),
            )
        if not_offered:
            return ModuleState(
                state=ModuleHealthState.WARNING,
                message=(
                    "Mood search needs "
                    + ", ".join(f"`{name}`" for name in not_offered)
                    + ", which this version of the Qobuz plugin does not "
                    "offer to install. Update the plugin, or turn **Mood / AI "
                    "search** off in this module's settings."
                ),
            )
        if to_install:
            return ModuleState(
                state=ModuleHealthState.WARNING,
                message=(
                    "Mood search unavailable. Missing package(s): "
                    + ", ".join(f"`{key}`" for key in to_install)
                    + "."
                ),
                missing_packages=to_install,
            )
        return ModuleState(state=ModuleHealthState.READY)

    async def required_packages(self) -> list[str]:
        """Packages mood search needs under the current config, which may
        hold changes the user staged since setup. None while one of them
        cannot be installed here, since the rest would be of no use."""
        to_install, no_wheel, not_offered = self._mood_search_lacks()
        return [] if no_wheel or not_offered else to_install

    def _mood_search_lacks(self) -> tuple[list[str], list[str], list[str]]:
        """What mood search runs on and this box lacks under the current
        config, as the shared embedder names its runtime: the keys of the
        packages this box can install, the names of those with no wheel for
        it, and the names of those this plugin does not offer.

        Nothing while no account is linked: a box that never linked Qobuz
        never installs the runtime."""
        if self._context is None or self._context.embedder is None:
            return [], [], []
        if self._pairing is not None and not self._pairing.link.linked:
            return [], [], []
        if not getattr(self._context.config, "ai_search_enabled", False):
            return [], [], []
        architecture = wheel_architecture()
        to_install: list[str] = []
        no_wheel: list[str] = []
        not_offered: list[str] = []
        for name in self._context.embedder.missing_runtime():
            key = _KEY_BY_IMPORT_NAME.get(name)
            if key is None:
                not_offered.append(name)
            elif OPTIONAL_PACKAGES[key].has_wheels_for(architecture):
                to_install.append(key)
            else:
                no_wheel.append(name)
        return to_install, no_wheel, not_offered

    async def resolve_dynamic_field(self, path: str):
        if path != STATUS_FIELD:
            raise KeyError(path)
        if self._pairing is None:
            return "Qobuz is not running."
        lines = [self._pairing.status_markdown()]
        if self._connect is not None:
            lines.append(self._connect.status_markdown())
        elif self._pairing.phase is Phase.LINKED:
            lines.append(
                "Playing from the Qobuz app needs a newer Kalinka server; "
                "browsing and playback from Kalinka work as usual."
            )
        return "\n\n".join(line for line in lines if line)

    async def _setup_jobs(
        self,
        client,
        context: InputPluginContext,
    ):
        if self.interface is None:
            return

        autoplay = QobuzAutoplay(
            qobuz_client=client,
            playqueue=context.playqueue,
            track_browser=self.interface,
        )
        self.reporter = QobuzReporter(client)

        try:
            async with context.listener.stream(
                [
                    PlayQueueEventType.RequestMoreTracks,
                    PlayQueueEventType.TracksAdded,
                    PlayQueueEventType.TracksRemoved,
                    PlayQueueEventType.PlaybackStateChanged,
                ]
            ) as stream:  # pyright: ignore[reportGeneralTypeIssues]
                async for item in stream:
                    try:
                        if isinstance(item, RequestMoreTracksEvent):
                            await autoplay.add_recommendation(item)
                        elif isinstance(item, TracksAddedEvent):
                            autoplay.add_tracks(item)
                        elif isinstance(item, TracksRemovedEvent):
                            autoplay.remove_tracks(item)
                        elif isinstance(item, PlaybackStateChangedEvent):
                            self.reporter.on_state_changed(item)
                    except Exception as e:
                        # One failed event (Qobuz unlinked, a network blip) must
                        # not stop autoplay and reporting for the whole session.
                        logger.warning("Qobuz could not handle %s: %s", type(item).__name__, e)

        except asyncio.CancelledError:
            logger.info("Playback state listener cancelled")
            if self.reporter:
                await self.reporter.shutdown()
            return
        except Exception as e:
            logger.error(f"Error in playback state listener: {e}")
            raise

        logger.info("Playback state listener stopped")

    async def setup(
        self,
        context: InputPluginContext,
    ) -> None:
        # No network here: an unlinked player, or one booting before its
        # network is up, must still come up and show how to link Qobuz.
        self._context = context
        config = QobuzConfig(**context.config.model_dump())
        holder = TokenHolder()
        self._client = QobuzClient(auth=QobuzAuth(holder, on_unauthorized=self._renew_after_401))
        playlist_index = None
        embedder = context.embedder
        if config.ai_search_enabled and embedder is not None:
            playlist_index = PlaylistIndex(
                PlaylistStore(
                    default_index_path(), (embedder.model_id, embedder.model_version)
                ),
                embedder,
                functools.partial(editorial_playlists, self._client),
                holder.wait_for_credential,
            )
            self._index_task = asyncio.create_task(playlist_index.run())
        interface = QobuzInputModule(config, self._client, playlist_index)
        self.interface = interface
        device_name = config.connect_device_name.strip() or default_device_name()
        # None on a server that cannot play outside the queue.
        direct = context.direct_playback
        if direct is not None:
            self._connect = ConnectService(
                direct=direct,
                client=self._client,
                format_id=interface.format_id,
                device_name=device_name,
                software_version=software_version(),
                bearer=self._api_bearer,
                persist=self._store_session,
            )
        self._pairing = PairingService(
            client=self._client,
            holder=holder,
            store=LinkStore(default_store_path()),
            device_name=device_name,
            port=config.connect_port,
            sessions=self._connect,
        )
        if config.unpair:
            self._pairing.forget_link()
        self._pairing.start()
        self._qobuz_tasks = asyncio.create_task(self._setup_jobs(self._client, context))

    def _api_bearer(self) -> Optional[Credential]:
        return self._pairing.api_bearer() if self._pairing is not None else None

    def _store_session(self, session) -> None:
        if self._pairing is not None:
            self._pairing.store_session(session)

    async def _renew_after_401(self, credential: Credential) -> bool:
        if self._pairing is None:
            return False
        return await self._pairing.renew_after_401(credential)

    async def shutdown(self):
        if self._index_task is not None:
            self._index_task.cancel()
            await asyncio.gather(self._index_task, return_exceptions=True)
            self._index_task = None

        if self._pairing:
            await self._pairing.stop()

        if self._connect:
            await self._connect.stop()

        if self._qobuz_tasks:
            self._qobuz_tasks.cancel()
            try:
                await self._qobuz_tasks
            except asyncio.CancelledError:
                pass

        if self.reporter:
            await self.reporter.shutdown()

        if self._client:
            await self._client.aclose()
