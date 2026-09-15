"""Runtime configuration loading and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(RuntimeError):
    """Raised when an OmniVoice configuration file cannot be loaded."""


class AgentConfig(BaseModel):
    """Validate the optional agent selector without activating an agent."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None


class HotkeyConfig(BaseModel):
    """Configure the single global push-to-talk chord."""

    model_config = ConfigDict(extra="forbid")

    push_to_talk: str = "ctrl+alt+space"


class SpeechConfig(BaseModel):
    """Validate independently selectable speech provider identifiers."""

    model_config = ConfigDict(extra="forbid")

    stt: str | None = None
    tts: str | None = None


class OmniVoiceConfig(BaseModel):
    """Reject misspelled settings while supplying safe runtime defaults."""

    model_config = ConfigDict(extra="forbid")

    agent: AgentConfig = Field(default_factory=AgentConfig)
    hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)


def default_config_path() -> Path:
    """Return the per-user Windows configuration location."""

    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "OmniVoice" / "config.yaml"
    return Path.home() / "AppData" / "Roaming" / "OmniVoice" / "config.yaml"


def load_config(explicit_path: Path | None = None) -> tuple[OmniVoiceConfig, Path | None]:
    """Load an explicit or user configuration, otherwise return safe defaults."""

    path = explicit_path if explicit_path is not None else default_config_path()
    if not path.exists():
        if explicit_path is not None:
            raise ConfigError(f"Configuration file does not exist: {path}")
        return OmniVoiceConfig(), None

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read configuration {path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration root must be a mapping: {path}")

    try:
        return OmniVoiceConfig.model_validate(raw), path
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration {path}:\n{exc}") from exc


def config_for_logging(config: OmniVoiceConfig) -> dict[str, Any]:
    """Return the non-secret runtime fields that are safe to log."""

    return {"push_to_talk": config.hotkey.push_to_talk}
