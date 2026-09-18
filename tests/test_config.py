from pathlib import Path

import pytest

from omnivoice.config import ConfigError, OmniVoiceConfig, config_for_logging, load_config


def test_missing_default_config_uses_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))

    config, loaded = load_config()

    assert loaded is None
    assert config.agent.default_model is None
    assert config.agent.models == {}
    assert config.hotkey.push_to_talk == "ctrl+alt+space"
    assert config.speech.stt.provider == "whisper_cpp"
    assert config.speech.stt.model == "small.en"
    assert config.speech.tts.voice == "Microsoft Zira Desktop"
    assert config.speech.recording.max_seconds == 30.0


def test_explicit_config_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("hotkey:\n  push_to_talk: f8\n", encoding="utf-8")

    config, loaded = load_config(path)

    assert loaded == path
    assert config.hotkey.push_to_talk == "f8"


def test_missing_explicit_config_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "missing.yaml")


def test_unknown_configuration_key_fails(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("hotkey:\n  typo: f8\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_config(path)


def test_speech_components_can_be_disabled_and_overridden(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
speech:
  stt:
    enabled: false
    executable_path: C:/tools/whisper-cli.exe
    model_path: C:/models/custom.bin
    threads: 4
  tts:
    enabled: false
    voice: Microsoft David Desktop
  microphone:
    device: 3
  recording:
    max_seconds: 12
""",
        encoding="utf-8",
    )

    config, _ = load_config(path)

    assert not config.speech.stt.enabled
    assert config.speech.stt.threads == 4
    assert config.speech.stt.executable_path == Path("C:/tools/whisper-cli.exe")
    assert not config.speech.tts.enabled
    assert config.speech.microphone.device == 3
    assert config.speech.recording.max_seconds == 12


@pytest.mark.parametrize(
    "yaml_text",
    [
        "speech:\n  stt:\n    timeout_seconds: 0\n",
        "speech:\n  stt:\n    threads: 0\n",
        "speech:\n  tts:\n    volume: 101\n",
        "speech:\n  recording:\n    max_seconds: 0\n",
        "speech:\n  stt:\n    provider: unknown\n",
        "speech:\n  tts:\n    provider: unknown\n",
    ],
)
def test_invalid_speech_configuration_fails(tmp_path: Path, yaml_text: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_config(path)


def test_logging_view_contains_metadata_but_not_paths() -> None:
    config = OmniVoiceConfig()
    logged = config_for_logging(config)

    assert logged["agent_default_model"] is None
    assert logged["agent_model_count"] == 0
    assert logged["stt_provider"] == "whisper_cpp"
    assert "executable_path" not in logged
    assert "model_path" not in logged


def test_explicit_empty_agent_configuration_disables_models(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("agent: {}\n", encoding="utf-8")

    config, _ = load_config(path)

    assert config.agent.default_model is None
    assert config.agent.models == {}


def test_named_agent_models_load_in_yaml_order(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
agent:
  default_model: groq-fast
  models:
    groq-fast: groq:openai/gpt-oss-20b
    groq-large: groq:openai/gpt-oss-120b
    ollama-local: ollama:qwen3:8b
""",
        encoding="utf-8",
    )

    config, _ = load_config(path)

    assert config.agent.default_model == "groq-fast"
    assert list(config.agent.models) == ["groq-fast", "groq-large", "ollama-local"]
    assert config.agent.models["ollama-local"] == "ollama:qwen3:8b"


@pytest.mark.parametrize(
    "yaml_text",
    [
        "agent:\n  model: groq:openai/gpt-oss-20b\n",
        "agent:\n  default_model: missing\n  models:\n    local: ollama:qwen3:8b\n",
        "agent:\n  models:\n    local: ollama:qwen3:8b\n",
        "agent:\n  default_model: local\n  models: {}\n",
        "agent:\n  default_model: BadAlias\n  models:\n    BadAlias: ollama:qwen3:8b\n",
        "agent:\n  default_model: local\n  models:\n    local: qwen3:8b\n",
        "agent:\n  default_model: local\n  models:\n    local: openai:gpt-5\n",
        "agent:\n  default_model: local\n  models:\n    local: 'ollama:'\n",
        "agent:\n  default_model: local\n  models:\n    local: ' ollama:qwen3'\n",
    ],
)
def test_invalid_agent_model_configuration_fails(
    tmp_path: Path, yaml_text: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_config(path)
