from typing import ClassVar
from pathlib import Path
from pydantic import Field, ConfigDict
from kalinka_plugin_sdk import ModuleIconFiles
from kalinka_plugin_sdk.module_config import ModuleConfig
from enum import Enum


class QobuzAudioFormat(str, Enum):
    MP3 = "MP3 320kbps"
    CD = "CD 16-bit 44.1KHz"
    HIRES_96 = "Hi-Res 24-bit 96KHz"
    HIRES_192 = "Hi-Res 24-bit 192KHz"


class QobuzConfig(ModuleConfig):
    """Qobuz input module settings."""

    model_config = ConfigDict(use_enum_values=True)

    __module_icon__: ClassVar[str] = "music_note_outlined"
    __module_icon_color__: ClassVar[str] = "#C9A96A"  # gold
    __module_icon_files__: ClassVar[ModuleIconFiles] = ModuleIconFiles(
        small=Path(__file__).with_name("icons") / "small.svg",
        standard=Path(__file__).with_name("icons") / "standard.svg",
        monochrome=True,
    )
    __preview_fields__: ClassVar[list[str]] = ["format"]

    name: str = Field(
        default="qobuz",
        title="Qobuz",
        description=(
            "Streaming from a Qobuz subscription, up to studio-quality "
            "hi-res. Link your account by choosing this player in the Qobuz app."
        ),
        frozen=True,
        exclude=True,
    )
    format: QobuzAudioFormat = Field(
        default=QobuzAudioFormat.HIRES_192,
        title="Audio quality",
        json_schema_extra={"importance": "simple", "setup": "prompt"},
    )
    unpair: bool = Field(
        default=False,
        title="Unpair Qobuz account on next restart",
        json_schema_extra={
            "help": (
                "Forget the linked Qobuz account and its tokens, then wait "
                "for a new pairing from the Qobuz app. Resets itself once done."
            ),
            # One-shot trigger: the framework resets this (persist-first)
            # before the plugin acts, so it fires at most once per arming.
            "one_shot": True,
            "importance": "simple",
        },
    )
    connect_device_name: str = Field(
        default="",
        title="Qobuz Connect device name",
        description=(
            "Name this player has in the Qobuz app's device picker, for "
            "pairing and for playing. Empty uses \"Kalinka (<hostname>)\"."
        ),
        json_schema_extra={"importance": "simple"},
    )
    connect_port: int = Field(
        default=8183,
        ge=1024,
        le=65535,
        title="Qobuz Connect pairing port",
        description="TCP port the Qobuz app connects to while pairing.",
    )
