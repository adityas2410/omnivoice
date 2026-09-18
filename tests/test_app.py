import argparse
from pathlib import Path

import pytest

from omnivoice.app import _run_config_command, build_parser


def test_config_path_command_is_discoverable() -> None:
    args = build_parser().parse_args(["config", "path"])

    assert args.command == "config"
    assert args.config_command == "path"


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
