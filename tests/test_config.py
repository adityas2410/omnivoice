from pathlib import Path

import pytest

from omnivoice.config import ConfigError, load_config


def test_missing_default_config_uses_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))

    config, loaded = load_config()

    assert loaded is None
    assert config.hotkey.push_to_talk == "ctrl+alt+space"


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
