"""Native Windows global push-to-talk hotkey support."""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable


WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0x4F56

_MODIFIERS = {
    "alt": (MOD_ALT, 0x12),
    "ctrl": (MOD_CONTROL, 0x11),
    "shift": (MOD_SHIFT, 0x10),
    "win": (MOD_WIN, 0x5B),
}

_NAMED_KEYS = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}


class HotkeyError(RuntimeError):
    """Raised when a hotkey is invalid or cannot be registered."""


@dataclass(frozen=True, slots=True)
class HotkeySpec:
    """Store a normalized chord and its native Windows representation."""

    modifiers: tuple[str, ...]
    trigger: str
    modifier_mask: int
    trigger_vk: int

    @property
    def modifier_vks(self) -> tuple[int, ...]:
        return tuple(_MODIFIERS[name][1] for name in self.modifiers)

    @property
    def display_name(self) -> str:
        return "+".join((*self.modifiers, self.trigger))


def _key_to_vk(name: str) -> int:
    """Translate a configured trigger name to a Windows virtual-key code."""

    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if len(name) == 1 and name.isascii() and name.isalnum():
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 24:
            return 0x6F + number
    raise HotkeyError(f"Unsupported hotkey trigger: {name!r}")


def parse_hotkey(value: str) -> HotkeySpec:
    """Validate one modifier chord with exactly one trigger key."""

    parts = [part.strip().lower() for part in value.split("+") if part.strip()]
    if not parts:
        raise HotkeyError("Hotkey cannot be empty")
    if len(parts) != len(set(parts)):
        raise HotkeyError("Hotkey cannot contain duplicate keys")

    modifiers = tuple(part for part in parts if part in _MODIFIERS)
    triggers = [part for part in parts if part not in _MODIFIERS]
    if len(triggers) != 1:
        raise HotkeyError("Hotkey must contain exactly one non-modifier trigger key")
    trigger = triggers[0]
    if trigger == "f12":
        raise HotkeyError("F12 is reserved by Windows and cannot be registered")

    mask = MOD_NOREPEAT
    for modifier in modifiers:
        mask |= _MODIFIERS[modifier][0]
    return HotkeySpec(modifiers, trigger, mask, _key_to_vk(trigger))


class GlobalHotkey:
    """Register a global chord and report trigger press/release transitions."""

    def __init__(
        self,
        spec: HotkeySpec,
        on_pressed: Callable[[], None],
        on_released: Callable[[], None],
    ) -> None:
        self.spec = spec
        self._on_pressed = on_pressed
        self._on_released = on_released
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread_id: int | None = None

    def start(self, timeout: float = 3.0) -> None:
        """Start the message thread and fail synchronously on registration errors."""

        if self._thread is not None:
            raise HotkeyError("Hotkey listener is already running")
        self._stop.clear()
        self._started.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="omnivoice-hotkey", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise HotkeyError("Timed out while starting the hotkey listener")
        if self._startup_error is not None:
            error = self._startup_error
            self._thread.join(timeout=1.0)
            self._thread = None
            raise HotkeyError(str(error)) from error

    def stop(self, timeout: float = 3.0) -> None:
        """Wake the message thread, unregister the chord, and join it."""

        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout)
        if thread.is_alive():
            raise HotkeyError("Hotkey listener did not stop cleanly")
        self._thread = None

    def _run(self) -> None:
        """Own the Windows message queue associated with RegisterHotKey."""

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        registered = False
        try:
            registered = bool(
                user32.RegisterHotKey(
                    None,
                    HOTKEY_ID,
                    self.spec.modifier_mask,
                    self.spec.trigger_vk,
                )
            )
            if not registered:
                code = ctypes.get_last_error()
                raise HotkeyError(
                    f"Could not register {self.spec.display_name!r}; "
                    f"it may already be in use (Windows error {code})"
                )
            self._started.set()

            message = wintypes.MSG()
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message != WM_HOTKEY or message.wParam != HOTKEY_ID:
                    continue
                self._on_pressed()
                # RegisterHotKey reports activation but has no release message.
                # Poll only the trigger key so releasing Space ends capture even
                # if the user is still releasing Ctrl or Alt.
                while (
                    not self._stop.is_set()
                    and user32.GetAsyncKeyState(self.spec.trigger_vk) & 0x8000
                ):
                    time.sleep(0.01)
                if not self._stop.is_set():
                    self._on_released()
        except BaseException as exc:
            self._startup_error = exc
            self._started.set()
        finally:
            if registered:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            self._thread_id = None
            self._started.set()
