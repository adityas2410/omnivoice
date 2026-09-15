import asyncio
import ctypes
from collections.abc import Sequence

import pytest

from omnivoice.windows.keyboard import (
    INPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    InputError,
    KeyboardExecutor,
    shortcut_inputs,
    unicode_inputs,
)


class FakeBackend:
    def __init__(self, partial: bool = False) -> None:
        self.partial = partial
        self.sent: list[list[INPUT]] = []
        self.down: set[int] = set()

    def send(self, inputs: Sequence[INPUT]) -> int:
        batch = list(inputs)
        self.sent.append(batch)
        return len(batch) - 1 if self.partial else len(batch)

    def is_key_down(self, virtual_key: int) -> bool:
        return virtual_key in self.down


def test_unicode_bmp_character_has_press_and_release() -> None:
    events = unicode_inputs("A")

    assert len(events) == 2
    assert events[0].ki.wScan == ord("A")
    assert events[0].ki.dwFlags == KEYEVENTF_UNICODE
    assert events[1].ki.dwFlags == KEYEVENTF_UNICODE | KEYEVENTF_KEYUP


def test_input_structure_matches_windows_abi() -> None:
    expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28

    assert ctypes.sizeof(INPUT) == expected_size


def test_unicode_astral_character_keeps_surrogate_pair_atomic() -> None:
    events = unicode_inputs("😀")

    assert len(events) == 4
    assert [event.ki.wScan for event in events] == [0xD83D, 0xD83D, 0xDE00, 0xDE00]


def test_shortcut_releases_keys_in_reverse_order() -> None:
    events = shortcut_inputs(("ctrl", "s"))

    assert [event.ki.wVk for event in events] == [0x11, ord("S"), ord("S"), 0x11]
    assert [bool(event.ki.dwFlags & KEYEVENTF_KEYUP) for event in events] == [
        False,
        False,
        True,
        True,
    ]


@pytest.mark.asyncio
async def test_typing_stops_before_send_when_target_changes() -> None:
    backend = FakeBackend()
    executor = KeyboardExecutor(backend)
    checks = iter((True, False))

    with pytest.raises(asyncio.CancelledError):
        await executor.type_text("ab", lambda: _async_value(next(checks)), asyncio.Event())

    assert len(backend.sent) == 1


@pytest.mark.asyncio
async def test_partial_input_is_an_error() -> None:
    backend = FakeBackend(partial=True)
    executor = KeyboardExecutor(backend)

    with pytest.raises(InputError, match="accepted"):
        await executor.type_text("a", lambda: _async_value(True), asyncio.Event())

    assert len(backend.sent) == 2
    assert all(event.ki.dwFlags & KEYEVENTF_KEYUP for event in backend.sent[1])


@pytest.mark.asyncio
async def test_partial_shortcut_attempts_key_cleanup() -> None:
    backend = FakeBackend(partial=True)
    executor = KeyboardExecutor(backend)

    with pytest.raises(InputError, match="shortcut events"):
        await executor.press_shortcut(
            ("ctrl", "s"), lambda: _async_value(True), asyncio.Event()
        )

    assert len(backend.sent) == 2
    assert all(event.ki.dwFlags & KEYEVENTF_KEYUP for event in backend.sent[1])


async def _async_value(value: bool) -> bool:
    return value
