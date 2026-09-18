"""Runtime configuration loading and validation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigError(RuntimeError):
    """Raised when an OmniVoice configuration file cannot be loaded."""


CONFIG_HEADER = """# OmniVoice configuration.
# Add model profiles under agent.models as: alias: "provider:model-name"
# Set agent.default_model to one of those aliases.
# Provider mapping: groq:... uses GROQ_API_KEY; local ollama:... uses no key.
# API keys belong in %APPDATA%\\OmniVoice\\.env, never in this YAML file.

"""

CREDENTIALS_TEMPLATE = """# OmniVoice provider credentials. Keep this file private.
# Add only keys required by providers selected in config.yaml.
# Example: a groq:... model reads GROQ_API_KEY from this file.
# Local ollama:... models need no API key.
"""


class AgentConfig(BaseModel):
    """Validate named agent models without contacting their providers."""

    model_config = ConfigDict(extra="forbid")

    default_model: str | None = None
    models: dict[str, str] = Field(default_factory=dict)

    @field_validator("models")
    @classmethod
    def validate_models(cls, models: dict[str, str]) -> dict[str, str]:
        """Keep aliases predictable and selectors provider-qualified."""

        for alias, selector in models.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", alias):
                raise ValueError(
                    "model aliases must contain only lowercase letters, digits, "
                    "hyphens, or underscores"
                )
            if selector != selector.strip() or any(
                char.isspace() for char in selector
            ):
                raise ValueError(f"model selector for {alias!r} must not contain whitespace")
            provider, separator, model_name = selector.partition(":")
            if not separator or not model_name:
                raise ValueError(
                    f"model selector for {alias!r} must use '<provider>:<model>'"
                )
            if provider not in {"groq", "ollama"}:
                raise ValueError(
                    f"model selector for {alias!r} uses unsupported provider {provider!r}"
                )
        return models

    @model_validator(mode="after")
    def validate_default_model(self) -> AgentConfig:
        """Require configured profiles to have one valid startup default."""

        if not self.models:
            if self.default_model is not None:
                raise ValueError(
                    "default_model cannot be set when no agent models are configured"
                )
            return self
        if self.default_model is None:
            raise ValueError("default_model is required when agent models are configured")
        if self.default_model not in self.models:
            raise ValueError("default_model must name an entry in agent.models")
        return self


class HotkeyConfig(BaseModel):
    """Configure the single global push-to-talk chord."""

    model_config = ConfigDict(extra="forbid")

    push_to_talk: str = "ctrl+alt+space"


class STTConfig(BaseModel):
    """Configure the replaceable speech-to-text provider."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    provider: Literal["whisper_cpp"] = "whisper_cpp"
    model: str = "small.en"
    language: str = "en"
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    threads: int | None = Field(default=None, ge=1, le=64)
    executable_path: Path | None = None
    model_path: Path | None = None


class TTSConfig(BaseModel):
    """Configure fixed application-status speech independently from STT."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    provider: Literal["windows_sapi"] = "windows_sapi"
    voice: str | None = "Microsoft Zira Desktop"
    rate: int = Field(default=0, ge=-10, le=10)
    volume: int = Field(default=100, ge=0, le=100)


class MicrophoneConfig(BaseModel):
    """Select a PortAudio input device by numeric ID or exact name."""

    model_config = ConfigDict(extra="forbid")

    device: int | str | None = None


class RecordingConfig(BaseModel):
    """Bound microphone capture and conservative silence detection."""

    model_config = ConfigDict(extra="forbid")

    max_seconds: float = Field(default=30.0, gt=0, le=120)
    sample_rate: int = Field(default=16_000, ge=8_000, le=48_000)
    minimum_seconds: float = Field(default=0.15, ge=0, le=5)
    silence_rms_threshold: int = Field(default=80, ge=0, le=2_000)


class SpeechConfig(BaseModel):
    """Group independently configurable local speech components."""

    model_config = ConfigDict(extra="forbid")

    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    microphone: MicrophoneConfig = Field(default_factory=MicrophoneConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)


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


def default_credentials_path() -> Path:
    """Return the per-user provider-credentials file location."""

    return default_config_path().with_name(".env")


def _load_environment_files() -> None:
    """Create the user credentials template and load local/user env files safely."""

    credentials_path = default_credentials_path()
    try:
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        if not credentials_path.exists():
            with credentials_path.open(
                "x", encoding="utf-8", newline="\n"
            ) as credentials_file:
                credentials_file.write(CREDENTIALS_TEMPLATE)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ConfigError(
            f"Could not create provider credentials file {credentials_path}: {exc}"
        ) from exc

    working_credentials = Path.cwd() / ".env"
    if working_credentials != credentials_path:
        load_dotenv(working_credentials, override=False)
    load_dotenv(credentials_path, override=False)


def load_config(explicit_path: Path | None = None) -> tuple[OmniVoiceConfig, Path | None]:
    """Load a config or create a discoverable per-user config on first launch."""

    _load_environment_files()
    path = explicit_path if explicit_path is not None else default_config_path()
    if not path.exists():
        if explicit_path is not None:
            raise ConfigError(f"Configuration file does not exist: {path}")
        config = OmniVoiceConfig()
        serialized = CONFIG_HEADER + yaml.safe_dump(
            config.model_dump(mode="json"), sort_keys=False, allow_unicode=True
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="\n") as config_file:
                config_file.write(serialized)
        except FileExistsError:
            # Another process completed first-run setup; validate its file below.
            pass
        except OSError as exc:
            raise ConfigError(f"Could not create configuration {path}: {exc}") from exc
        else:
            return config, path

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

    return {
        "agent_default_model": config.agent.default_model,
        "agent_model_count": len(config.agent.models),
        "push_to_talk": config.hotkey.push_to_talk,
        "stt_enabled": config.speech.stt.enabled,
        "stt_provider": config.speech.stt.provider,
        "stt_model": config.speech.stt.model,
        "tts_enabled": config.speech.tts.enabled,
        "tts_provider": config.speech.tts.provider,
        "recording_limit_seconds": config.speech.recording.max_seconds,
    }
