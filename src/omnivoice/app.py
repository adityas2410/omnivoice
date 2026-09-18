"""OmniVoice application lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Sequence

from omnivoice.cli import Console
from omnivoice.config import ConfigError, default_config_path, load_config
from omnivoice.interaction import InteractionController
from omnivoice.models import ModelRegistry
from omnivoice.speech.audio import SoundDeviceRecorder, list_input_devices
from omnivoice.speech.ports import SpeechError
from omnivoice.speech.setup import (
    SpeechSetupError,
    default_whisper_executable,
    default_whisper_model,
    install_local_speech,
)
from omnivoice.speech.whisper_cpp import WhisperCppSTT
from omnivoice.windows.focus import FocusError, FocusLease, FocusService
from omnivoice.windows.hotkey import GlobalHotkey, HotkeyError, parse_hotkey
from omnivoice.windows.keyboard import KeyboardExecutor, WindowsInputBackend
from omnivoice.windows.speech import DisabledTTS, WindowsReadyCue, WindowsSapiTTS


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Define runtime and explicit local-speech maintenance commands."""

    parser = argparse.ArgumentParser(description="Safe Windows keyboard automation by voice")
    parser.add_argument("--config", type=Path, help="Path to an OmniVoice YAML configuration")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
    )
    commands = parser.add_subparsers(dest="command")
    speech = commands.add_parser("speech", help="Manage local speech components")
    speech_commands = speech.add_subparsers(dest="speech_command", required=True)
    setup = speech_commands.add_parser(
        "setup", help="Download and verify the pinned local STT assets"
    )
    setup.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing installation after new downloads verify",
    )
    speech_commands.add_parser("devices", help="List available microphone inputs")
    config = commands.add_parser("config", help="Inspect configuration locations")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("path", help="Print the user configuration path")
    return parser


async def run(args: argparse.Namespace) -> int:
    """Own and coordinate every long-lived runtime component."""

    config, loaded_path = load_config(args.config)
    spec = parse_hotkey(config.hotkey.push_to_talk)
    models = ModelRegistry(config.agent)
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
    stt = None
    recorder = None
    if config.speech.stt.enabled:
        executable = (
            config.speech.stt.executable_path or default_whisper_executable()
        ).expanduser()
        model_path = (
            config.speech.stt.model_path or default_whisper_model()
        ).expanduser()
        stt = WhisperCppSTT(
            executable=executable,
            model_path=model_path,
            model=config.speech.stt.model,
            language=config.speech.stt.language,
            timeout_seconds=config.speech.stt.timeout_seconds,
            threads=config.speech.stt.threads,
        )
        recorder = SoundDeviceRecorder(
            device=config.speech.microphone.device,
            sample_rate=config.speech.recording.sample_rate,
            max_seconds=config.speech.recording.max_seconds,
        )

    if config.speech.tts.enabled:
        tts = WindowsSapiTTS(
            voice_name=config.speech.tts.voice,
            rate=config.speech.tts.rate,
            volume=config.speech.tts.volume,
            warning=console.status,
        )
    else:
        tts = DisabledTTS()

    controller = InteractionController(
        focus,
        keyboard,
        console.status,
        recorder=recorder,
        stt=stt,
        tts=tts,
        ready_cue=WindowsReadyCue(),
        minimum_recording_seconds=config.speech.recording.minimum_seconds,
        silence_rms_threshold=config.speech.recording.silence_rms_threshold,
        recording_limit_seconds=config.speech.recording.max_seconds,
    )
    controller_holder["controller"] = controller

    hotkey = GlobalHotkey(
        spec,
        lambda: loop.call_soon_threadsafe(controller.hotkey_pressed),
        lambda: loop.call_soon_threadsafe(controller.hotkey_released),
    )

    config_description = str(loaded_path) if loaded_path is not None else "built-in defaults"

    def runtime_status() -> str:
        if stt is None:
            stt_status = "stt=disabled"
            microphone_status = "microphone=unused"
        else:
            readiness = stt.readiness
            state = "ready" if readiness.ready else f"not ready ({readiness.detail})"
            stt_status = f"stt={stt.provider}:{stt.model} {state}"
            microphone_readiness = recorder.readiness
            microphone_state = "ready" if microphone_readiness.ready else "not ready"
            microphone_status = (
                f"microphone={microphone_state} ({microphone_readiness.detail})"
            )
        tts_readiness = tts.readiness
        if not config.speech.tts.enabled:
            tts_status = "tts=disabled"
        elif tts_readiness.ready:
            tts_status = f"tts=windows_sapi ready ({tts.voice_name})"
        else:
            tts_status = f"tts=windows_sapi not ready ({tts_readiness.detail})"
        return ", ".join(
            (
                f"hotkey={spec.display_name}",
                f"config={config_description}",
                stt_status,
                tts_status,
                microphone_status,
            )
        )

    try:
        # Start focus monitoring before accepting hotkeys so a request can never
        # enter the controller without its safety dependency being available.
        focus.start()
        try:
            await tts.start()
        except SpeechError as exc:
            console.status(f"Status speech unavailable: {exc}")
        hotkey.start()
        console.status(f"Configuration: {config_description}")
        console.status(f"Push-to-talk hotkey: {spec.display_name}")
        selection = models.snapshot()
        if selection is None:
            console.status("Agent model: not configured.")
        else:
            console.status(
                f"Agent model: {selection.alias} ({selection.selector}) [default]."
            )
        if stt is not None and not stt.readiness.ready:
            console.status(
                "Local transcription is not ready. Run 'omnivoice speech setup'; "
                "the guarded self-test remains available."
            )
        await console.run(
            controller,
            runtime_status,
            models,
        )
        return 0
    finally:
        # Shutdown runs while the event loop is alive, allowing pending request
        # cancellation and COM commands to complete before their threads exit.
        await controller.shutdown()
        if recorder is not None:
            await recorder.shutdown()
        if stt is not None:
            await stt.shutdown()
        await tts.shutdown()
        try:
            hotkey.stop()
        finally:
            await focus.stop()


def _run_speech_command(args: argparse.Namespace) -> int:
    """Run setup/device discovery without starting hotkey or UIA services."""

    if args.speech_command == "setup":
        install_local_speech(force=args.force)
        return 0
    if args.speech_command == "devices":
        devices = list_input_devices()
        if not devices:
            print("No microphone input devices were found.")
            return 0
        print("Input devices:")
        for identifier, name in devices:
            print(f"  {identifier}: {name}")
        return 0
    raise ConfigError("Unknown speech command")


def _run_config_command(args: argparse.Namespace) -> int:
    """Expose configuration discovery without starting Windows services."""

    if args.config_command == "path":
        print(default_config_path())
        return 0
    raise ConfigError("Unknown config command")


def main(argv: Sequence[str] | None = None) -> None:
    """Parse startup options, configure metadata logging, and run the CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if args.command == "speech":
            exit_code = _run_speech_command(args)
        elif args.command == "config":
            exit_code = _run_config_command(args)
        else:
            exit_code = asyncio.run(run(args))
    except (
        ConfigError,
        HotkeyError,
        FocusError,
        SpeechError,
        SpeechSetupError,
        OSError,
    ) as exc:
        LOGGER.error("event=startup_failed error_type=%s", type(exc).__name__)
        print(f"OmniVoice could not start: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        exit_code = 130
    raise SystemExit(exit_code)
