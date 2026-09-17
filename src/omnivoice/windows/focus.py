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
    # UIA providers are allowed to omit a native HWND for virtualized controls.
    # The runtime ID and process ID remain the primary request-local identity.
    native_window_handle: int | None
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
        """Build a lease only for a supported control proven to be writable."""

        try:
            focused = automation.GetFocusedElement()
            # comtypes represents a NULL interface as a false pointer object,
            # not necessarily as Python's None.
            if not focused:
                raise InvalidTargetError("No control currently has keyboard focus")
            if not bool(focused.CurrentHasKeyboardFocus):
                raise InvalidTargetError("The focused control did not confirm keyboard focus")

            # Browser and framework providers sometimes put keyboard focus on a
            # text child inside the actual Edit control. Resolve that narrow case
            # while requiring ComboBox and Document targets to hold focus directly.
            element = FocusService._resolve_target(automation, module, focused)
            if not bool(element.CurrentIsEnabled):
                raise InvalidTargetError("The focused control is disabled")
            if not bool(element.CurrentIsKeyboardFocusable):
                raise InvalidTargetError("The focused control is not keyboard-focusable")
            if bool(element.CurrentIsPassword):
                raise InvalidTargetError("Password fields are not supported")
            if bool(element.CurrentIsOffscreen):
                raise InvalidTargetError("The focused control is off-screen")

            control_type = FocusService._required_int(
                element.CurrentControlType, "control type"
            )
            if not FocusService._is_writable(element, module, control_type):
                name = FocusService._control_type_name(module, control_type)
                raise InvalidTargetError(
                    f"The focused {name} control is read-only or ambiguous"
                )

            raw_runtime_id = element.GetRuntimeId()
            if raw_runtime_id is None:
                raise InvalidTargetError("The focused control has no runtime identifier")
            runtime_id = tuple(
                FocusService._required_int(part, "runtime identifier")
                for part in raw_runtime_id
            )
            if not runtime_id:
                raise InvalidTargetError("The focused control has no stable runtime identifier")
            return FocusLease(
                runtime_id=runtime_id,
                process_id=FocusService._required_int(
                    element.CurrentProcessId, "process identifier"
                ),
                native_window_handle=FocusService._optional_int(
                    element.CurrentNativeWindowHandle
                ),
                control_type=control_type,
            )
        except InvalidTargetError:
            raise
        except BaseException as exc:
            raise FocusError(f"Windows UI Automation could not inspect focus: {exc}") from exc

    @staticmethod
    def _resolve_target(automation: Any, module: Any, focused: Any) -> Any:
        """Resolve a directly focused supported control or an Edit wrapper."""

        edit_type = FocusService._required_int(
            module.UIA_EditControlTypeId, "Edit control type"
        )
        combo_box_type = FocusService._required_int(
            module.UIA_ComboBoxControlTypeId, "ComboBox control type"
        )
        document_type = FocusService._required_int(
            module.UIA_DocumentControlTypeId, "Document control type"
        )
        focused_type = FocusService._required_int(
            focused.CurrentControlType, "focused control type"
        )
        if focused_type in {edit_type, combo_box_type, document_type}:
            return focused

        try:
            walker = automation.RawViewWalker
        except BaseException as exc:
            name = FocusService._control_type_name(module, focused_type)
            raise InvalidTargetError(
                f"The focused control type is not supported ({name})"
            ) from exc

        element = focused
        # A shallow limit prevents malformed providers from causing unbounded
        # traversal while covering the wrapper nodes used by browsers and WPF.
        for _ in range(6):
            try:
                element = walker.GetParentElement(element)
            except BaseException:
                break
            # A typed NULL COM pointer compares unequal to None but is false.
            # Stop before reading a property from it.
            if not element:
                break
            control_type = FocusService._required_int(
                element.CurrentControlType, "ancestor control type"
            )
            if control_type == edit_type:
                return element

        name = FocusService._control_type_name(module, focused_type)
        raise InvalidTargetError(
            f"The focused control type is not supported ({name})"
        )

    @staticmethod
    def _required_int(value: Any, property_name: str) -> int:
        """Convert a required UIA integer with a useful provider error."""

        if value is None:
            raise FocusError(f"UI Automation did not provide the {property_name}")
        return int(value)

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        """Normalize optional UIA integer properties without failing a lease."""

        if value is None:
            return None
        converted = int(value)
        return converted or None

    @staticmethod
    def _control_type_name(module: Any, control_type: int) -> str:
        """Turn common UIA control IDs into safe, useful diagnostics."""

        names = {
            getattr(module, "UIA_ComboBoxControlTypeId", -1): "ComboBox",
            getattr(module, "UIA_EditControlTypeId", -1): "Edit",
            getattr(module, "UIA_DocumentControlTypeId", -1): "Document",
            getattr(module, "UIA_TextControlTypeId", -1): "Text",
            getattr(module, "UIA_PaneControlTypeId", -1): "Pane",
            getattr(module, "UIA_CustomControlTypeId", -1): "Custom",
        }
        return names.get(control_type, f"Unknown ({control_type})")

    @staticmethod
    def _is_writable(element: Any, module: Any, control_type: int) -> bool:
        """Require the writable pattern appropriate for the target's type."""

        edit_type = FocusService._required_int(
            module.UIA_EditControlTypeId, "Edit control type"
        )
        combo_box_type = FocusService._required_int(
            module.UIA_ComboBoxControlTypeId, "ComboBox control type"
        )
        document_type = FocusService._required_int(
            module.UIA_DocumentControlTypeId, "Document control type"
        )

        # UIA requires an editable ComboBox to expose ValuePattern. Requiring it
        # prevents selection-only drop-downs from being treated as text fields.
        if control_type in {edit_type, combo_box_type}:
            try:
                unknown = element.GetCurrentPattern(module.UIA_ValuePatternId)
                value_pattern = unknown.QueryInterface(module.IUIAutomationValuePattern)
                return not bool(value_pattern.CurrentIsReadOnly)
            except BaseException:
                if control_type == combo_box_type:
                    return False

        # TextPattern cannot set content itself, but its read-only attribute is
        # the UIA signal needed before guarded SendInput targets a text surface.
        if control_type in {edit_type, document_type}:
            try:
                unknown = element.GetCurrentPattern(module.UIA_TextPatternId)
                text_pattern = unknown.QueryInterface(module.IUIAutomationTextPattern)
                value = text_pattern.DocumentRange.GetAttributeValue(
                    module.UIA_IsReadOnlyAttributeId
                )
                return value is False or value == 0
            except BaseException:
                return False

        return False
