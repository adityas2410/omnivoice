import asyncio
import os
import tkinter as tk

import pytest

from omnivoice.windows.focus import FocusLease, FocusService, InvalidTargetError
from omnivoice.windows.hotkey import GlobalHotkey, parse_hotkey
from omnivoice.windows.keyboard import KeyboardExecutor, WindowsInputBackend


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


@pytest.mark.asyncio
async def test_guarded_typing_into_owned_edit_control() -> None:
    service = FocusService(lambda _: None)
    service.start()
    root = tk.Tk()
    root.title("OmniVoice integration test")
    root.geometry("360x100")
    root.attributes("-topmost", True)
    entry = tk.Entry(root)
    entry.pack(fill="x", padx=20, pady=25)
    root.update()
    entry.focus_force()
    root.update()

    pumping = True

    async def pump_window() -> None:
        while pumping:
            root.update()
            await asyncio.sleep(0.01)

    pump_task = asyncio.create_task(pump_window())
    try:
        await asyncio.sleep(0.05)
        try:
            lease = await service.capture()
        except InvalidTargetError as exc:
            if "off-screen" in str(exc):
                pytest.skip("the test host has no visible interactive desktop")
            raise
        assert await service.watch(lease)
        executor = KeyboardExecutor(WindowsInputBackend())
        await executor.type_text("Ω", lambda: service.matches(lease), asyncio.Event())
        root.update()
        assert entry.get() == "Ω"
    finally:
        await service.stop()
        pumping = False
        await pump_task
        root.destroy()
