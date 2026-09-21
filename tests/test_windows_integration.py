import asyncio
import ctypes
import os
import tkinter as tk

import pytest

from omnivoice.context import ContextCaptureLimits
from omnivoice.speech.audio import list_input_devices
from omnivoice.windows.focus import FocusLease, FocusService, InvalidTargetError
from omnivoice.windows.context import UIContextService
from omnivoice.windows.hotkey import GlobalHotkey, parse_hotkey
from omnivoice.windows.keyboard import KeyboardExecutor, WindowsInputBackend
from omnivoice.windows.speech import WindowsSapiTTS


pytestmark = [
    pytest.mark.windows_integration,
    pytest.mark.skipif(
        os.environ.get("OMNIVOICE_WINDOWS_INTEGRATION") != "1",
        reason="set OMNIVOICE_WINDOWS_INTEGRATION=1 to interact with the desktop",
    ),
]


@pytest.mark.asyncio
async def test_uia_service_starts_and_stops() -> None:
    service = FocusService(lambda _: None)
    service.start()
    try:
        try:
            result = await service.capture()
        except InvalidTargetError:
            result = None
        assert result is None or isinstance(result, FocusLease)
    finally:
        await service.stop()


def test_f24_hotkey_registers_and_unregisters() -> None:
    listener = GlobalHotkey(parse_hotkey("f24"), lambda: None, lambda: None)
    listener.start()
    listener.stop()


def test_portaudio_enumerates_input_devices() -> None:
    devices = list_input_devices()

    assert all(isinstance(identifier, int) and name for identifier, name in devices)


@pytest.mark.asyncio
async def test_sapi_initializes_and_closes_without_speaking() -> None:
    warnings: list[str] = []
    tts = WindowsSapiTTS(
        voice_name="Microsoft Zira Desktop",
        rate=0,
        volume=100,
        warning=warnings.append,
    )

    await tts.start()
    try:
        assert tts.readiness.ready
        assert tts.voice_name
    finally:
        await tts.shutdown()


@pytest.mark.asyncio
async def test_guarded_typing_into_owned_edit_control() -> None:
    service = FocusService(lambda _: None)
    context_service = UIContextService(
        ContextCaptureLimits(
            document_max_characters=50_000,
            semantic_max_characters=20_000,
            semantic_max_elements=500,
            semantic_max_depth=16,
            target_before_max_characters=16_000,
            target_after_max_characters=8_000,
            capture_timeout_seconds=5,
        )
    )
    service.start()
    context_service.start()
    root = tk.Tk()
    root.title("OmniVoice integration test")
    root.geometry("360x100")
    root.attributes("-topmost", True)
    entry = tk.Entry(root)
    entry.pack(fill="x", padx=20, pady=25)
    root.update()
    entry.focus_force()
    root.update()
    if int(ctypes.windll.user32.GetForegroundWindow()) != int(root.winfo_id()):
        root.destroy()
        pytest.skip("Windows did not grant foreground focus to the temporary test window")

    pumping = True

    async def pump_window() -> None:
        while pumping:
            root.update()
            await asyncio.sleep(0.01)

    pump_task = asyncio.create_task(pump_window())
    try:
        await asyncio.sleep(0.05)
        try:
            lease = await service.capture(include_context=True)
        except InvalidTargetError as exc:
            if "off-screen" in str(exc):
                pytest.skip("the test host has no visible interactive desktop")
            raise
        assert await service.watch(lease)
        captured = await context_service.capture(lease, asyncio.Event())
        assert captured.ui_context.status in {"complete", "partial"}
        executor = KeyboardExecutor(WindowsInputBackend())
        await executor.type_text("Ω", lambda: service.matches(lease), asyncio.Event())
        root.update()
        assert entry.get() == "Ω"
    finally:
        await context_service.stop()
        await service.stop()
        pumping = False
        await pump_task
        root.destroy()


@pytest.mark.asyncio
async def test_capture_and_replace_owned_text_selection() -> None:
    service = FocusService(lambda _: None)
    service.start()
    root = tk.Tk()
    root.title("OmniVoice selection integration test")
    root.geometry("360x100")
    root.attributes("-topmost", True)
    entry = tk.Entry(root)
    entry.insert(0, "original text")
    entry.selection_range(0, len("original"))
    entry.pack(fill="x", padx=20, pady=25)
    root.update()
    entry.focus_force()
    entry.selection_range(0, len("original"))
    root.update()
    if int(ctypes.windll.user32.GetForegroundWindow()) != int(root.winfo_id()):
        root.destroy()
        pytest.skip("Windows did not grant foreground focus to the temporary test window")

    pumping = True

    async def pump_window() -> None:
        while pumping:
            root.update()
            await asyncio.sleep(0.01)

    pump_task = asyncio.create_task(pump_window())
    try:
        await asyncio.sleep(0.05)
        lease = await service.capture()
        assert await service.watch(lease)
        selection = await service.capture_selection(lease)
        if selection is None:
            pytest.skip("the Tk edit provider does not expose a UIA text selection")
        assert selection.text == "original"
        assert await service.selection_matches(selection)
        executor = KeyboardExecutor(WindowsInputBackend())
        await executor.type_text(
            "rewritten", lambda: service.matches(lease), asyncio.Event()
        )
        root.update()
        assert entry.get() == "rewritten text"
    finally:
        await service.stop()
        pumping = False
        await pump_task
        root.destroy()
