"""Request lifecycle and guarded self-test orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
from enum import StrEnum
from typing import Callable, Protocol

from omnivoice.windows.focus import FocusError, FocusLease, InvalidTargetError
from omnivoice.windows.keyboard import InputError, KeyboardExecutor


LOGGER = logging.getLogger(__name__)
SELF_TEST_TEXT = "[OmniVoice safety test]"
SELF_TEST_ARM_SECONDS = 30.0
SELF_TEST_PROCESSING_SECONDS = 2.0


class RequestState(StrEnum):
    """Expose each externally meaningful stage of a hotkey request."""

    IDLE = "idle"
    LISTENING = "listening"
    VALIDATING = "validating"
    PROCESSING = "processing"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class FocusPort(Protocol):
    """Describe only the focus operations needed by request orchestration."""

    async def capture(self) -> FocusLease: ...

    async def matches(self, lease: FocusLease) -> bool: ...

    async def watch(self, lease: FocusLease) -> bool: ...

    async def clear_watch(self) -> None: ...


class InteractionController:
    """Serialize hotkey requests and enforce the focus lease."""

    def __init__(
        self,
        focus: FocusPort,
        keyboard: KeyboardExecutor,
        status: Callable[[str], None],
    ) -> None:
        self._focus = focus
        self._keyboard = keyboard
        self._status = status
        self.state = RequestState.IDLE
        self.last_outcome: RequestState | None = None
        self._armed_until = 0.0
        self._active_lease: FocusLease | None = None
        self._cancelled = asyncio.Event()
        self._request_task: asyncio.Task[None] | None = None
        self._shutting_down = False

    @property
    def is_armed(self) -> bool:
        """Report whether one real self-test insertion is still authorized."""

        return time.monotonic() < self._armed_until

    def arm_self_test(self) -> None:
        """Grant one time-limited authorization to emit the fixed marker."""

        self._armed_until = time.monotonic() + SELF_TEST_ARM_SECONDS
        self._status(
            "Self-test armed for 30 seconds and one attempt. "
            "Focus a supported text field, then hold and release the hotkey."
        )
        LOGGER.info("event=self_test_armed expires_in_seconds=30")

    def describe_status(self) -> str:
        """Return operational state without exposing focused or typed text."""

        last = self.last_outcome.value if self.last_outcome is not None else "none"
        armed = "yes" if self.is_armed else "no"
        return f"state={self.state.value}, self_test_armed={armed}, last_outcome={last}"

    def hotkey_pressed(self) -> None:
        """Begin one request or reject overlapping push-to-talk input."""

        if self._shutting_down:
            return
        if self.state is not RequestState.IDLE:
            self._status(f"Busy ({self.state.value}); hotkey press ignored.")
            LOGGER.info("event=hotkey_ignored state=%s", self.state.value)
            return
        self._cancelled = asyncio.Event()
        self._set_state(RequestState.LISTENING)
        self._status("Push-to-talk active. Release the hotkey to validate the target.")

    def hotkey_released(self) -> None:
        """Consume any self-test grant and start asynchronous validation."""

        if self._shutting_down or self.state is not RequestState.LISTENING:
            return
        armed = self._consume_arm()
        self._request_task = asyncio.create_task(
            self._process_release(armed), name="omnivoice-request"
        )

    def focus_lost(self, lease: FocusLease) -> None:
        """Cancel only when UIA reports loss of the active request's target."""

        if self._active_lease == lease and self.state in {
            RequestState.PROCESSING,
            RequestState.EXECUTING,
        }:
            self.cancel("Focus changed. Request cancelled.")

    def cancel(self, reason: str = "Request cancelled.") -> None:
        """Signal cooperative cancellation without sending compensating input."""

        if self.state is RequestState.IDLE:
            self._status("No active request to cancel.")
            return
        self._cancelled.set()
        self._status(reason)
        LOGGER.info("event=request_cancel_requested state=%s", self.state.value)
        if self.state is RequestState.LISTENING:
            self._finish(RequestState.CANCELLED)

    async def shutdown(self) -> None:
        """Stop active work and remove focus monitoring before loop shutdown."""

        self._shutting_down = True
        if self.state is not RequestState.IDLE:
            self._cancelled.set()
        task = self._request_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self._safe_clear_watch()

    def _consume_arm(self) -> bool:
        # Authorization is one-shot even when validation or execution fails.
        armed = self.is_armed
        self._armed_until = 0.0
        return armed

    async def _process_release(self, armed: bool) -> None:
        """Validate, bind, delay, revalidate, and optionally type one marker."""

        try:
            self._set_state(RequestState.VALIDATING)
            self._status("Validating the focused control...")
            lease = await self._focus.capture()
            self._active_lease = lease
            LOGGER.info(
                "event=target_captured pid=%s hwnd=%s control_type=%s",
                lease.process_id,
                lease.native_window_handle,
                lease.control_type,
            )

            if self._cancelled.is_set():
                self._finish(RequestState.CANCELLED)
                return

            if not armed:
                self._status("Editable target detected. Use /selftest arm to permit test typing.")
                self._finish(RequestState.COMPLETED)
                return

            if not await self._focus.watch(lease):
                # Watching also performs an immediate second comparison, closing
                # the gap between the initial snapshot and event subscription.
                self._cancelled.set()
                self._status("Focus changed. Request cancelled.")
                self._finish(RequestState.CANCELLED)
                return

            self._set_state(RequestState.PROCESSING)
            self._status("Target bound. Simulating two seconds of processing...")
            if await self._wait_or_cancel(SELF_TEST_PROCESSING_SECONDS):
                self._finish(RequestState.CANCELLED)
                return

            if not await self._keyboard.wait_for_modifiers_released(timeout=1.0):
                # Residual push-to-talk modifiers could transform typed text into
                # destructive application shortcuts, so failure is fail-closed.
                self._cancelled.set()
                self._status("A modifier key remained held. Request cancelled.")
                self._finish(RequestState.CANCELLED)
                return
            if not await self._focus.matches(lease):
                self._cancelled.set()
                self._status("Focus changed. Request cancelled.")
                self._finish(RequestState.CANCELLED)
                return

            self._set_state(RequestState.EXECUTING)
            self._status("Typing the armed safety marker...")
            await self._keyboard.type_text(
                SELF_TEST_TEXT,
                lambda: self._focus.matches(lease),
                self._cancelled,
            )
            self._status("Self-test completed.")
            self._finish(RequestState.COMPLETED)
        except InvalidTargetError as exc:
            message = f"Target rejected: {exc}"
            if armed:
                message += " Self-test authorization was consumed; run /selftest arm again."
            self._status(message)
            LOGGER.info("event=target_rejected reason=%s", type(exc).__name__)
            self._finish(RequestState.CANCELLED)
        except asyncio.CancelledError:
            self._cancelled.set()
            if self.state is not RequestState.IDLE:
                self._status("Request cancelled before all input was sent.")
                self._finish(RequestState.CANCELLED)
        except (FocusError, InputError) as exc:
            message = f"Request failed safely: {exc}"
            if armed:
                message += " Self-test authorization was consumed; run /selftest arm again."
            self._status(message)
            # This is an expected fail-closed outcome already shown to the user;
            # keep metadata available at INFO without duplicating it by default.
            LOGGER.info("event=request_failed error_type=%s", type(exc).__name__)
            self._finish(RequestState.FAILED)
        except BaseException:
            self._status("Request failed safely because of an unexpected internal error.")
            LOGGER.exception("event=request_failed error_type=unexpected")
            self._finish(RequestState.FAILED)
        finally:
            self._active_lease = None
            await self._safe_clear_watch()
            self._request_task = None

    async def _wait_or_cancel(self, seconds: float) -> bool:
        """Make simulated processing immediately responsive to cancellation."""

        try:
            await asyncio.wait_for(self._cancelled.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def _safe_clear_watch(self) -> None:
        """Remove a lease watch without masking the request's main outcome."""

        try:
            await self._focus.clear_watch()
        except FocusError:
            LOGGER.exception("event=focus_watch_clear_failed")

    def _set_state(self, state: RequestState) -> None:
        """Record a metadata-only state transition."""

        self.state = state
        LOGGER.info("event=request_state state=%s", state.value)

    def _finish(self, outcome: RequestState) -> None:
        """Remember the terminal outcome and return the controller to idle."""

        self._set_state(outcome)
        self.last_outcome = outcome
        self._set_state(RequestState.IDLE)
