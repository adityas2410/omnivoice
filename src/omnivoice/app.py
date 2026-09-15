"""OmniVoice application lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Sequence

from omnivoice.cli import Console
from omnivoice.config import ConfigError, load_config
from omnivoice.interaction import InteractionController
from omnivoice.windows.focus import FocusError, FocusLease, FocusService
from omnivoice.windows.hotkey import GlobalHotkey, HotkeyError, parse_hotkey
from omnivoice.windows.keyboard import KeyboardExecutor, WindowsInputBackend


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Define the small startup interface shared by the module and console script."""

    parser = argparse.ArgumentParser(description="Safe Windows keyboard automation by voice")
    parser.add_argument("--config", type=Path, help="Path to an OmniVoice YAML configuration")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """Own and coordinate every long-lived runtime component."""

    config, loaded_path = load_config(args.config)
    spec = parse_hotkey(config.hotkey.push_to_talk)
    console = Console()
    loop = asyncio.get_running_loop()
    controller_holder: dict[str, InteractionController] = {}

    def on_focus_lost(lease: FocusLease) -> None:
        # UIA invokes this from a COM callback thread; state changes must happen
        # on the asyncio thread where the request controller is owned.
        controller = controller_holder.get("controller")
        if controller is not None:
            loop.call_soon_threadsafe(controller.focus_lost, lease)

    focus = FocusService(on_focus_lost)
    keyboard = KeyboardExecutor(WindowsInputBackend())
    controller = InteractionController(focus, keyboard, console.status)
    controller_holder["controller"] = controller

    hotkey = GlobalHotkey(
        spec,
        lambda: loop.call_soon_threadsafe(controller.hotkey_pressed),
        lambda: loop.call_soon_threadsafe(controller.hotkey_released),
    )

    config_description = str(loaded_path) if loaded_path is not None else "built-in defaults"

    try:
        # Start focus monitoring before accepting hotkeys so a request can never
        # enter the controller without its safety dependency being available.
        focus.start()
        hotkey.start()
        console.status(f"Configuration: {config_description}")
        console.status(f"Push-to-talk hotkey: {spec.display_name}")
        await console.run(
            controller,
            lambda: f"hotkey={spec.display_name}, config={config_description}",
        )
        return 0
    finally:
        # Shutdown runs while the event loop is alive, allowing pending request
        # cancellation and COM commands to complete before their threads exit.
        await controller.shutdown()
        try:
            hotkey.stop()
        finally:
            await focus.stop()


def main(argv: Sequence[str] | None = None) -> None:
    """Parse startup options, configure metadata logging, and run the CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        exit_code = asyncio.run(run(args))
    except (ConfigError, HotkeyError, FocusError, OSError) as exc:
        LOGGER.error("event=startup_failed error_type=%s", type(exc).__name__)
        print(f"OmniVoice could not start: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        exit_code = 130
    raise SystemExit(exit_code)
