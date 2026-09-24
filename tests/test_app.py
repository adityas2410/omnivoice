import argparse
import logging
from pathlib import Path

import pytest

from omnivoice.app import (
    _GOOGLE_AFC_WARNING,
    _run_config_command,
    _start_hotkeys,
    _stop_hotkeys,
    _suppress_spurious_google_afc_warning,
    build_parser,
)
from omnivoice.windows.hotkey import HotkeyError


class FakeHotkey:
    def __init__(self, name: str, events: list[str], *, start_fails: bool = False) -> None:
        self.name = name
        self.events = events
        self.start_fails = start_fails

    def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.start_fails:
            raise HotkeyError(f"{self.name} failed")

    def stop(self) -> None:
        self.events.append(f"stop:{self.name}")


def test_config_path_command_is_discoverable() -> None:
    args = build_parser().parse_args(["config", "path"])

    assert args.command == "config"
    assert args.config_command == "path"

    paths_args = build_parser().parse_args(["config", "paths"])
    assert paths_args.config_command == "paths"


def test_config_path_command_prints_user_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    args = argparse.Namespace(config_command="path")

    assert _run_config_command(args) == 0
    assert capsys.readouterr().out.strip() == str(
        tmp_path / "OmniVoice" / "config.yaml"
    )


def test_config_paths_command_prints_config_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    args = argparse.Namespace(config_command="paths")

    assert _run_config_command(args) == 0
    output = capsys.readouterr().out
    assert f"Configuration: {tmp_path / 'OmniVoice' / 'config.yaml'}" in output
    assert f"Provider credentials: {tmp_path / 'OmniVoice' / '.env'}" in output


def test_both_hotkeys_start_and_stop_in_safe_order() -> None:
    events: list[str] = []
    hotkeys = (FakeHotkey("dictation", events), FakeHotkey("agent", events))

    _start_hotkeys(hotkeys)  # type: ignore[arg-type]
    _stop_hotkeys(hotkeys)  # type: ignore[arg-type]

    assert events == [
        "start:dictation",
        "start:agent",
        "stop:agent",
        "stop:dictation",
    ]


def test_second_hotkey_start_failure_unregisters_first() -> None:
    events: list[str] = []
    hotkeys = (
        FakeHotkey("dictation", events),
        FakeHotkey("agent", events, start_fails=True),
    )

    with pytest.raises(HotkeyError, match="agent failed"):
        _start_hotkeys(hotkeys)  # type: ignore[arg-type]

    assert events == ["start:dictation", "start:agent", "stop:dictation"]


def test_only_known_google_afc_warning_is_suppressed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("google_genai.models")
    logger.addFilter(_suppress_spurious_google_afc_warning)
    try:
        with caplog.at_level(logging.WARNING, logger="google_genai.models"):
            logger.warning(_GOOGLE_AFC_WARNING)
            logger.warning("A different Google warning")
            logger.error("A Google error")
    finally:
        logger.removeFilter(_suppress_spurious_google_afc_warning)

    assert _GOOGLE_AFC_WARNING not in caplog.text
    assert "A different Google warning" in caplog.text
    assert "A Google error" in caplog.text
