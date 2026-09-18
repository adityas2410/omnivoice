import os
from pathlib import Path

import pytest
import yaml

from omnivoice.config import (
    ConfigError,
    OmniVoiceConfig,
    config_for_logging,
    load_config,
)


def test_missing_default_config_is_created_without_assumed_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_data = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(app_data))

    config, loaded = load_config()

    expected_path = app_data / "OmniVoice" / "config.yaml"
    assert loaded == expected_path
    assert expected_path.exists()
    credentials_path = app_data / "OmniVoice" / ".env"
    assert credentials_path.exists()
    credentials = credentials_path.read_text(encoding="utf-8")
    assert "GROQ_API_KEY=" not in credentials
    assert "OLLAMA_API_KEY=" not in credentials
    assert config.agent.default_model is None
    assert config.agent.models == {}
    written = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
    assert "API keys belong in" in expected_path.read_text(encoding="utf-8")
    assert written["agent"]["default_model"] is None
    assert written["agent"]["models"] == {}
    assert config.hotkey.push_to_talk == "ctrl+alt+space"
    assert config.speech.stt.provider == "whisper_cpp"
    assert config.speech.stt.model == "small.en"
    assert config.speech.tts.voice == "Microsoft Zira Desktop"
    assert config.speech.recording.max_seconds == 30.0


def test_existing_user_config_is_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_data = tmp_path / "appdata"
    user_directory = app_data / "OmniVoice"
    user_directory.mkdir(parents=True)
    user_config = user_directory / "config.yaml"
    user_config.write_text("hotkey:\n  push_to_talk: f9\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(app_data))

    config, loaded = load_config()

    assert loaded == user_config
    assert config.hotkey.push_to_talk == "f9"


def test_user_credentials_file_is_loaded_without_overriding_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_data = tmp_path / "appdata"
    working_directory = tmp_path / "working"
    user_directory = app_data / "OmniVoice"
    working_directory.mkdir()
    user_directory.mkdir(parents=True)
    (user_directory / ".env").write_text(
        "GROQ_API_KEY=file-test-key\n", encoding="utf-8"
    )
    monkeypatch.chdir(working_directory)
    monkeypatch.setenv("APPDATA", str(app_data))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    load_config()

    assert os.environ["GROQ_API_KEY"] == "file-test-key"
    monkeypatch.setenv("GROQ_API_KEY", "process-test-key")
    load_config()
    assert os.environ["GROQ_API_KEY"] == "process-test-key"


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
