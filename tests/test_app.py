import argparse
from pathlib import Path

import pytest

from omnivoice.app import _run_config_command, build_parser


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
