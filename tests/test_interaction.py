import asyncio
import logging
from collections.abc import Sequence

import pytest

import omnivoice.interaction as interaction
from omnivoice.interaction import InteractionController, RequestState, SELF_TEST_TEXT
from omnivoice.windows.focus import FocusLease
from omnivoice.windows.keyboard import INPUT, KeyboardExecutor


LEASE = FocusLease((1, 2, 3), 100, 200, 50004)


class FakeFocus:
    def __init__(self) -> None:
        self.current = LEASE
        self.watched: FocusLease | None = None

    async def capture(self) -> FocusLease:
        return self.current

    async def matches(self, lease: FocusLease) -> bool:
        return self.current == lease

    async def watch(self, lease: FocusLease) -> bool:
        if self.current != lease:
            return False
        self.watched = lease
        return True

    async def clear_watch(self) -> None:
        self.watched = None


class FakeBackend:
    def __init__(self, *, partial: bool = False) -> None:
        self.partial = partial
        self.sent: list[list[INPUT]] = []
        self.down: set[int] = set()

    def send(self, inputs: Sequence[INPUT]) -> int:
        batch = list(inputs)
        self.sent.append(batch)
        return len(batch) - 1 if self.partial else len(batch)

    def is_key_down(self, virtual_key: int) -> bool:
        return virtual_key in self.down


async def wait_until_idle(controller: InteractionController) -> None:
    for _ in range(100):
        if controller.state is RequestState.IDLE and controller.last_outcome is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("controller did not return to idle")


@pytest.mark.asyncio
async def test_armed_self_test_types_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interaction, "SELF_TEST_PROCESSING_SECONDS", 0.0)
    focus = FakeFocus()
    backend = FakeBackend()
    statuses: list[str] = []
    controller = InteractionController(focus, KeyboardExecutor(backend), statuses.append)

    controller.arm_self_test()
    controller.hotkey_pressed()
    controller.hotkey_released()
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.COMPLETED
    assert len(backend.sent) == len(SELF_TEST_TEXT)
    assert not controller.is_armed
    assert focus.watched is None


@pytest.mark.asyncio
async def test_unarmed_hotkey_only_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interaction, "SELF_TEST_PROCESSING_SECONDS", 0.0)
    backend = FakeBackend()
    controller = InteractionController(FakeFocus(), KeyboardExecutor(backend), lambda _: None)

    controller.hotkey_pressed()
    controller.hotkey_released()
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.COMPLETED
    assert backend.sent == []


def test_expired_arm_is_not_active() -> None:
    controller = InteractionController(
        FakeFocus(), KeyboardExecutor(FakeBackend()), lambda _: None
    )
    controller.arm_self_test()
    controller._armed_until = 0.0

    assert not controller.is_armed


def test_hotkey_press_is_ignored_while_busy() -> None:
    statuses: list[str] = []
    controller = InteractionController(
        FakeFocus(), KeyboardExecutor(FakeBackend()), statuses.append
    )
    controller.hotkey_pressed()
    controller.hotkey_pressed()

    assert controller.state is RequestState.LISTENING
    assert "ignored" in statuses[-1]


@pytest.mark.asyncio
async def test_focus_change_during_processing_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interaction, "SELF_TEST_PROCESSING_SECONDS", 1.0)
    focus = FakeFocus()
    backend = FakeBackend()
    controller = InteractionController(focus, KeyboardExecutor(backend), lambda _: None)

    controller.arm_self_test()
    controller.hotkey_pressed()
    controller.hotkey_released()
    for _ in range(100):
        if controller.state is RequestState.PROCESSING:
            break
        await asyncio.sleep(0.01)
    controller.focus_lost(LEASE)
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.CANCELLED
    assert backend.sent == []


@pytest.mark.asyncio
async def test_partial_input_fails_without_logging_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(interaction, "SELF_TEST_PROCESSING_SECONDS", 0.0)
    controller = InteractionController(
        FakeFocus(), KeyboardExecutor(FakeBackend(partial=True)), lambda _: None
    )

    with caplog.at_level(logging.INFO):
        controller.arm_self_test()
        controller.hotkey_pressed()
        controller.hotkey_released()
        await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.FAILED
    assert SELF_TEST_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_failed_armed_attempt_tells_user_to_rearm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(interaction, "SELF_TEST_PROCESSING_SECONDS", 0.0)
    statuses: list[str] = []
    controller = InteractionController(
        FakeFocus(), KeyboardExecutor(FakeBackend(partial=True)), statuses.append
    )

    controller.arm_self_test()
    controller.hotkey_pressed()
    controller.hotkey_released()
    await wait_until_idle(controller)

    assert any("/selftest arm again" in status for status in statuses)
