"""Focused editable-control validation through Windows UI Automation."""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypeAlias


LOGGER = logging.getLogger(__name__)


class FocusError(RuntimeError):
    """Base error for UI Automation failures."""


class InvalidTargetError(FocusError):
    """Raised when the focused control is not confidently editable."""


@dataclass(frozen=True, slots=True)
class FocusLease:
    """Carry only comparable UIA identity data outside the owning COM thread."""

    runtime_id: tuple[int, ...]
    process_id: int
    native_window_handle: int
    control_type: int


Operation: TypeAlias = Literal["capture", "matches", "watch", "clear", "stop"]


@dataclass(slots=True)
class _Command:
    """Transfer one focus operation and its result across the thread boundary."""

    operation: Operation
    future: Future[Any]
    lease: FocusLease | None = None


def leases_match(left: FocusLease, right: FocusLease) -> bool:
    """Require every captured identity field to remain unchanged."""

    return (
        left.runtime_id == right.runtime_id
        and left.process_id == right.process_id
        and left.native_window_handle == right.native_window_handle
        and left.control_type == right.control_type
    )


class FocusService:
    """Own UIA COM objects on one MTA thread and expose async snapshots."""

    def __init__(self, on_focus_lost: Callable[[FocusLease], None]) -> None:
        self._on_focus_lost = on_focus_lost
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._focus_dirty = threading.Event()
        self._started = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._watched: FocusLease | None = None

    def start(self, timeout: float = 5.0) -> None:
        """Start the MTA worker and surface initialization failures immediately."""

        if self._thread is not None:
            raise FocusError("Focus service is already running")
        self._started.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="omnivoice-uia", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise FocusError("Timed out while starting Windows UI Automation")
        if self._startup_error is not None:
            error = self._startup_error
            self._thread.join(timeout=1.0)
            self._thread = None
            raise FocusError(f"Could not start Windows UI Automation: {error}") from error

    async def capture(self) -> FocusLease:
        """Capture and validate the control holding keyboard focus."""

        return await self._submit("capture")

    async def matches(self, lease: FocusLease) -> bool:
        """Compare the focused editable control with an existing lease."""

        return await self._submit("matches", lease)

    async def watch(self, lease: FocusLease) -> bool:
        """Begin focus-loss monitoring after an immediate identity check."""

        return await self._submit("watch", lease)

    async def clear_watch(self) -> None:
        """Stop associating focus events with the completed request."""

        await self._submit("clear")

    async def stop(self, timeout: float = 5.0) -> None:
        """Remove the COM handler on its owner thread and wait for shutdown."""

        thread = self._thread
        if thread is None:
            return
        await self._submit("stop")
        thread.join(timeout)
        if thread.is_alive():
            raise FocusError("UI Automation thread did not stop cleanly")
        self._thread = None

    async def _submit(self, operation: Operation, lease: FocusLease | None = None) -> Any:
        """Bridge an asyncio caller to the blocking COM worker."""

        if self._thread is None or not self._thread.is_alive():
            raise FocusError("Focus service is not running")
        future: Future[Any] = Future()
        self._commands.put(_Command(operation, future, lease))
        return await asyncio.wrap_future(future)

    def _run(self) -> None:
        """Own COM initialization, UIA objects, handlers, and focus queries."""

        automation: Any = None
        handler: Any = None
        module: Any = None
        try:
            comtypes_was_loaded = "comtypes" in sys.modules
            # comtypes initializes COM as soon as it is imported. Setting this
            # flag first ensures this worker uses the MTA required by UIA events.
            sys.coinit_flags = 0  # COINIT_MULTITHREADED for comtypes import-time setup.
            import comtypes
            import comtypes.client

            if comtypes_was_loaded:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as module  # type: ignore[attr-defined]

            automation = comtypes.client.CreateObject(
                module.CUIAutomation8,
                interface=module.IUIAutomation,
            )
            dirty = self._focus_dirty

            class FocusChangedHandler(comtypes.COMObject):
                _com_interfaces_ = [module.IUIAutomationFocusChangedEventHandler]

                def HandleFocusChangedEvent(self, sender: Any) -> int:
                    # Keep the system callback non-blocking. The owner thread will
                    # re-query focus and apply the full validation policy.
                    dirty.set()
                    return 0

            handler = FocusChangedHandler()
            automation.AddFocusChangedEventHandler(None, handler)
            self._started.set()

            running = True
            while running:
                try:
                    command = self._commands.get(timeout=0.05)
                except queue.Empty:
                    command = None

                if command is not None:
                    try:
                        if command.operation == "capture":
                            result = self._capture(automation, module)
                        elif command.operation == "matches":
                            result = self._matches_current(automation, module, command.lease)
                        elif command.operation == "watch":
                            result = self._matches_current(automation, module, command.lease)
                            self._watched = command.lease if result else None
                        elif command.operation == "clear":
                            self._watched = None
                            result = None
                        else:
                            self._watched = None
                            result = None
                            running = False
                        command.future.set_result(result)
                    except BaseException as exc:
                        command.future.set_exception(exc)

                if self._focus_dirty.is_set():
                    self._focus_dirty.clear()
                    watched = self._watched
                    if watched is not None:
                        try:
                            still_current = self._matches_current(automation, module, watched)
                        except FocusError:
                            still_current = False
                        if not still_current:
                            self._watched = None
                            # The callback may enter application code only after
                            # COM work and identity comparison are complete.
                            self._on_focus_lost(watched)
        except BaseException as exc:
            self._startup_error = exc
            self._started.set()
            while True:
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    break
                command.future.set_exception(FocusError(str(exc)))
        finally:
            if automation is not None and handler is not None:
                try:
                    automation.RemoveFocusChangedEventHandler(handler)
                except BaseException:
                    LOGGER.exception("event=focus_handler_removal_failed")
            try:
                import comtypes

                comtypes.CoUninitialize()
            except BaseException:
                pass
            self._started.set()

    @staticmethod
    def _matches_current(automation: Any, module: Any, lease: FocusLease | None) -> bool:
        """Fail closed when the current target is invalid or no longer identical."""

        if lease is None:
            return False
        try:
            current = FocusService._capture(automation, module)
        except InvalidTargetError:
            return False
        return leases_match(current, lease)

    @staticmethod
    def _capture(automation: Any, module: Any) -> FocusLease:
        """Build a lease only for an unambiguously writable standard Edit."""

        try:
            element = automation.GetFocusedElement()
            if element is None:
                raise InvalidTargetError("No control currently has keyboard focus")
            if not bool(element.CurrentHasKeyboardFocus):
                raise InvalidTargetError("The focused control did not confirm keyboard focus")
            if not bool(element.CurrentIsEnabled):
                raise InvalidTargetError("The focused control is disabled")
            if not bool(element.CurrentIsKeyboardFocusable):
                raise InvalidTargetError("The focused control is not keyboard-focusable")
            if bool(element.CurrentIsPassword):
                raise InvalidTargetError("Password fields are not supported")
            if bool(element.CurrentIsOffscreen):
                raise InvalidTargetError("The focused control is off-screen")

            control_type = int(element.CurrentControlType)
            if control_type != int(module.UIA_EditControlTypeId):
                raise InvalidTargetError("Only standard Edit controls are supported")
            if not FocusService._is_writable(element, module):
                raise InvalidTargetError("The focused Edit control is read-only or ambiguous")

            runtime_id = tuple(int(part) for part in element.GetRuntimeId())
            if not runtime_id:
                raise InvalidTargetError("The focused control has no stable runtime identifier")
            return FocusLease(
                runtime_id=runtime_id,
                process_id=int(element.CurrentProcessId),
                native_window_handle=int(element.CurrentNativeWindowHandle),
                control_type=control_type,
            )
        except InvalidTargetError:
            raise
        except BaseException as exc:
            raise FocusError(f"Windows UI Automation could not inspect focus: {exc}") from exc

    @staticmethod
    def _is_writable(element: Any, module: Any) -> bool:
        """Accept only UIA patterns that explicitly report writable content."""

        try:
            unknown = element.GetCurrentPattern(module.UIA_ValuePatternId)
            value_pattern = unknown.QueryInterface(module.IUIAutomationValuePattern)
            return not bool(value_pattern.CurrentIsReadOnly)
        except BaseException:
            pass

        try:
            unknown = element.GetCurrentPattern(module.UIA_TextPatternId)
            text_pattern = unknown.QueryInterface(module.IUIAutomationTextPattern)
            value = text_pattern.DocumentRange.GetAttributeValue(
                module.UIA_IsReadOnlyAttributeId
            )
            return value is False or value == 0
        except BaseException:
            return False
