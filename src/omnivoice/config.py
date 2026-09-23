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
# Add models under agent.models as: alias: "provider:model-name"
# Set agent.default_model to one of those aliases.
# Provider prefixes and credentials follow Pydantic AI's model documentation.
# Examples: google:..., anthropic:..., openai:..., groq:..., ollama:...
# API keys belong in %APPDATA%\\OmniVoice\\.env, never in this YAML file.
# speech.stt.model selects a local ggml-MODEL.bin file. The setup command
# installs small.en; install other Whisper model files in the speech/models folder.
# speech.tts.voice names an installed Windows SAPI voice.

"""

CREDENTIALS_TEMPLATE = """# OmniVoice provider credentials. Keep this file private.
# Add only keys required by providers selected in config.yaml.
# Use the standard environment variable documented by the Pydantic AI provider.
# Examples: GOOGLE_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY.
# Local providers may require a base URL such as OLLAMA_BASE_URL.
"""


DEFAULT_MODEL_INPUT_TOKEN_BUDGET = 1_000_000


def _validate_model_selector(selector: str, alias: str) -> str:
    if selector != selector.strip() or any(char.isspace() for char in selector):
        raise ValueError(f"model selector for {alias!r} must not contain whitespace")
    provider, separator, model_name = selector.partition(":")
    if not separator or not provider or not model_name:
        raise ValueError(f"model selector for {alias!r} must use '<provider>:<model>'")
    return selector


class ModelProfileConfig(BaseModel):
    """Optionally attach an estimated input budget to one model profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: str
    input_token_budget: int = Field(
        default=DEFAULT_MODEL_INPUT_TOKEN_BUDGET, ge=2_048, le=1_000_000
    )


class AgentContextConfig(BaseModel):
    """Bound opt-in request-scoped Windows UI Automation context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["off", "uia"] = "off"
    document_max_characters: int = Field(default=100_000, ge=1_000, le=500_000)
    semantic_max_characters: int = Field(default=40_000, ge=1_000, le=200_000)
    semantic_max_elements: int = Field(default=1_000, ge=10, le=5_000)
    semantic_max_depth: int = Field(default=24, ge=1, le=64)
    target_before_max_characters: int = Field(default=32_000, ge=0, le=200_000)
    target_after_max_characters: int = Field(default=16_000, ge=0, le=200_000)
    capture_timeout_seconds: float = Field(default=5.0, gt=0, le=30)


class AgentConfig(BaseModel):
    """Validate named agent models without contacting their providers."""

    model_config = ConfigDict(extra="forbid")

    default_model: str | None = None
    models: dict[str, str | ModelProfileConfig] = Field(default_factory=dict)
    context: AgentContextConfig = Field(default_factory=AgentContextConfig)

    @field_validator("models")
    @classmethod
    def validate_models(
        cls, models: dict[str, str | ModelProfileConfig]
    ) -> dict[str, str | ModelProfileConfig]:
        """Keep aliases predictable and selectors provider-qualified."""

        for alias, profile in models.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", alias):
                raise ValueError(
                    "model aliases must contain only lowercase letters, digits, "
                    "hyphens, or underscores"
                )
            selector = profile if isinstance(profile, str) else profile.selector
            _validate_model_selector(selector, alias)
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
    """Configure distinct literal-dictation and AI push-to-talk chords."""

    model_config = ConfigDict(extra="forbid")

    push_to_talk: str = "ctrl+alt+space"
    agent_push_to_talk: str = "ctrl+alt+shift+space"

    @model_validator(mode="after")
    def validate_distinct_hotkeys(self) -> HotkeyConfig:
        """Reject chords that Windows would treat as the same registration."""

        if _normalized_hotkey(self.push_to_talk) == _normalized_hotkey(
            self.agent_push_to_talk
        ):
            raise ValueError("push_to_talk and agent_push_to_talk must be different")
        return self


def _normalized_hotkey(value: str) -> tuple[frozenset[str], str]:
    """Normalize modifier ordering for duplicate configuration detection."""

    parts = [part.strip().lower() for part in value.split("+") if part.strip()]
    modifiers = frozenset(
        part for part in parts if part in {"ctrl", "alt", "shift", "win"}
    )
    triggers = [part for part in parts if part not in modifiers]
    trigger = triggers[0] if len(triggers) == 1 else "\0".join(triggers)
    return modifiers, trigger


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

    @field_validator("model")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", value):
            raise ValueError("speech.stt.model must be a Whisper model name")
        return value


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
        generated = {
            "agent": {
                "default_model": config.agent.default_model,
                "models": config.agent.models,
                "context": {"mode": config.agent.context.mode},
            },
            "hotkey": config.hotkey.model_dump(mode="json"),
            "speech": {
                "stt": {"model": config.speech.stt.model},
                "tts": {"voice": config.speech.tts.voice},
            },
        }
        serialized = CONFIG_HEADER + yaml.safe_dump(
            generated, sort_keys=False, allow_unicode=True
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
        "agent_context_mode": config.agent.context.mode,
        "push_to_talk": config.hotkey.push_to_talk,
        "agent_push_to_talk": config.hotkey.agent_push_to_talk,
        "stt_enabled": config.speech.stt.enabled,
        "stt_provider": config.speech.stt.provider,
        "stt_model": config.speech.stt.model,
        "tts_enabled": config.speech.tts.enabled,
        "tts_provider": config.speech.tts.provider,
        "recording_limit_seconds": config.speech.recording.max_seconds,
    }
