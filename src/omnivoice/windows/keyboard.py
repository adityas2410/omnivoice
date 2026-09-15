"""Safe, injectable keyboard input built on the Windows SendInput API."""

from __future__ import annotations

import asyncio
import ctypes
import struct
import time
from collections.abc import Awaitable, Callable, Sequence
from ctypes import wintypes
from typing import Protocol


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C

_KEYS = {
    "alt": VK_MENU,
    "ctrl": VK_CONTROL,
    "shift": VK_SHIFT,
    "win": VK_LWIN,
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

ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    """Match the Win32 KEYBDINPUT memory layout exactly."""

    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    """Keep the INPUT union at the ABI-required size on 32- and 64-bit Windows."""

    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    """Complete the native INPUT union even though OmniVoice does not use it."""

    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    """Represent one native event accepted by the SendInput function."""

    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


class InputError(RuntimeError):
    """Raised when Windows does not accept a complete input sequence."""


class InputBackend(Protocol):
    """Make global input replaceable by a non-invasive test backend."""

    def send(self, inputs: Sequence[INPUT]) -> int: ...

    def is_key_down(self, virtual_key: int) -> bool: ...


class WindowsInputBackend:
    """Call user32 with explicit signatures and Windows last-error tracking."""

    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        )
        self._user32.SendInput.restype = wintypes.UINT
        self._user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        self._user32.GetAsyncKeyState.restype = wintypes.SHORT

    def send(self, inputs: Sequence[INPUT]) -> int:
        """Submit one uninterrupted array and return Windows' accepted count."""

        if not inputs:
            return 0
        array_type = INPUT * len(inputs)
        array = array_type(*inputs)
        return int(self._user32.SendInput(len(array), array, ctypes.sizeof(INPUT)))

    def is_key_down(self, virtual_key: int) -> bool:
        """Read the live key state used to guard against held modifiers."""

        return bool(self._user32.GetAsyncKeyState(virtual_key) & 0x8000)


def _input(virtual_key: int, scan_code: int, flags: int) -> INPUT:
    """Build a zero-timestamp keyboard event for Windows to timestamp."""

    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(
            wVk=virtual_key,
            wScan=scan_code,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        ),
    )


def unicode_inputs(character: str) -> list[INPUT]:
    """Encode one Python character into atomic UTF-16 press/release events."""

    if len(character) != 1:
        raise ValueError("unicode_inputs expects exactly one logical character")
    encoded = character.encode("utf-16-le", errors="surrogatepass")
    units = struct.unpack(f"<{len(encoded) // 2}H", encoded)
    events: list[INPUT] = []
    for unit in units:
        events.append(_input(0, unit, KEYEVENTF_UNICODE))
        events.append(_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def _key_to_vk(name: str) -> int:
    """Map a tool-facing key name to a Windows virtual-key code."""

    normalized = name.strip().lower()
    if normalized in _KEYS:
        return _KEYS[normalized]
    if len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
        return ord(normalized.upper())
    if normalized.startswith("f") and normalized[1:].isdigit():
        number = int(normalized[1:])
        if 1 <= number <= 24:
            return 0x6F + number
    raise ValueError(f"Unsupported key: {name!r}")


def shortcut_inputs(keys: Sequence[str]) -> list[INPUT]:
    """Press a shortcut in order and release every key in reverse order."""

    if not keys:
        raise ValueError("Shortcut cannot be empty")
    virtual_keys = [_key_to_vk(key) for key in keys]
    events = [_input(key, 0, 0) for key in virtual_keys]
    events.extend(_input(key, 0, KEYEVENTF_KEYUP) for key in reversed(virtual_keys))
    return events


class KeyboardExecutor:
    """Apply keyboard actions only while the focus lease remains valid."""

    def __init__(self, backend: InputBackend) -> None:
        self._backend = backend

    async def wait_for_modifiers_released(self, timeout: float = 1.0) -> bool:
        """Wait briefly so push-to-talk modifiers cannot alter emitted input."""

        deadline = time.monotonic() + timeout
        modifier_keys = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
        while time.monotonic() < deadline:
            if not any(self._backend.is_key_down(key) for key in modifier_keys):
                return True
            await asyncio.sleep(0.02)
        return not any(self._backend.is_key_down(key) for key in modifier_keys)

    async def type_text(
        self,
        text: str,
        is_target_current: Callable[[], Awaitable[bool]],
        cancelled: asyncio.Event,
    ) -> None:
        """Type text one logical character at a time under focus validation."""

        for character in text:
            if cancelled.is_set() or not await is_target_current():
                raise asyncio.CancelledError
            events = unicode_inputs(character)
            # A surrogate pair is sent in one call so Windows cannot interleave
            # another input event between its UTF-16 code units.
            inserted = self._backend.send(events)
            if inserted != len(events):
                self._release_unicode(events)
                raise InputError(
                    f"Windows accepted {inserted} of {len(events)} input events"
                )
            await asyncio.sleep(0)

    async def press_shortcut(
        self,
        keys: Sequence[str],
        is_target_current: Callable[[], Awaitable[bool]],
        cancelled: asyncio.Event,
    ) -> None:
        """Send one shortcut only after cancellation and focus checks pass."""

        if cancelled.is_set() or not await is_target_current():
            raise asyncio.CancelledError
        events = shortcut_inputs(keys)
        inserted = self._backend.send(events)
        if inserted != len(events):
            self._release_keys(keys)
            raise InputError(
                f"Windows accepted {inserted} of {len(events)} shortcut events"
            )

    def _release_keys(self, keys: Sequence[str]) -> None:
        # A partial SendInput call may leave a modifier held logically. Releasing
        # every requested key is harmless and prevents sticky keyboard state.
        releases = [_input(_key_to_vk(key), 0, KEYEVENTF_KEYUP) for key in reversed(keys)]
        self._backend.send(releases)

    def _release_unicode(self, events: Sequence[INPUT]) -> None:
        # Complete possible unmatched Unicode key-down events after partial input.
        releases = [
            _input(0, event.ki.wScan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            for event in events
            if not event.ki.dwFlags & KEYEVENTF_KEYUP
        ]
        self._backend.send(releases)
