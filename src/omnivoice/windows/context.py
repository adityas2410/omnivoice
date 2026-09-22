"""Read-only, request-scoped UI Automation context on an isolated MTA thread."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal

from omnivoice.context import (
    CapturedContext,
    ContextBoundaryChanged,
    ContextCaptureLimits,
    DocumentTextContext,
    SemanticItem,
    SemanticOutlineContext,
    TextTargetContext,
    UIContext,
    ValueTargetContext,
    unavailable_context,
)
from omnivoice.windows.focus import FocusLease, FocusService


LOGGER = logging.getLogger(__name__)


class ContextError(RuntimeError):
    """Raised for context-worker lifecycle failures."""


@dataclass(slots=True)
class _Accumulator:
    lock: threading.Lock = field(default_factory=threading.Lock)
    target: TextTargetContext | ValueTargetContext | None = None
    document: DocumentTextContext | None = None
    semantic: SemanticOutlineContext | None = None
    window_title: str | None = None

    def update(self, name: str, value: Any) -> None:
        with self.lock:
            setattr(self, name, value)

    def snapshot(
        self,
        reason: Literal[
            "not_supported",
            "empty",
            "timeout",
            "worker_unavailable",
            "capture_failed",
        ]
        | None = None,
    ) -> CapturedContext:
        with self.lock:
            target = self.target
            document = self.document
            semantic = self.semantic
            window_title = self.window_title
        available = int(document is not None) + int(semantic is not None)
        if available == 2:
            status: Literal["complete", "partial", "unavailable"] = "complete"
        elif available or target is not None:
            status = "partial"
        else:
            status = "unavailable"
        if status == "unavailable" and reason is None:
            reason = "empty"
        return CapturedContext(
            target_context=target,
            ui_context=UIContext(
                status=status,
                window_title=window_title,
                document_text=document,
                semantic_outline=semantic,
                reason=reason if status != "complete" else None,
            ),
        )


@dataclass(slots=True)
class _CaptureCommand:
    lease: FocusLease
    limits: ContextCaptureLimits
    selected_text: str | None
    future: Future[CapturedContext]
    cancelled: threading.Event = field(default_factory=threading.Event)
    accumulator: _Accumulator = field(default_factory=_Accumulator)


@dataclass(slots=True)
class _StopCommand:
    future: Future[None]


_Command = _CaptureCommand | _StopCommand


class UIContextService:
    """Capture UI context without sharing COM objects or focus-event ownership."""

    def __init__(self, limits: ContextCaptureLimits) -> None:
        self._limits = limits
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._started = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._busy = False
        self._health: Literal["stopped", "ready", "busy", "degraded"] = "stopped"

    @property
    def health(self) -> str:
        with self._state_lock:
            return self._health

    def start(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            raise ContextError("UI context service is already running")
        self._started.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run, name="omnivoice-uia-context", daemon=True
        )
        self._thread.start()
        if not self._started.wait(timeout):
            with self._state_lock:
                self._health = "degraded"
            raise ContextError("Timed out while starting UI context capture")
        if self._startup_error is not None:
            error = self._startup_error
            self._thread = None
            raise ContextError(f"Could not start UI context capture: {error}") from error

    async def capture(
        self,
        lease: FocusLease,
        cancelled: asyncio.Event,
        *,
        selected_text: str | None = None,
    ) -> CapturedContext:
        if cancelled.is_set():
            raise asyncio.CancelledError
        if lease.context_anchor is None:
            return unavailable_context("not_supported")
        with self._state_lock:
            thread = self._thread
            if thread is None or not thread.is_alive() or self._busy:
                return unavailable_context("worker_unavailable")
            self._busy = True
            self._health = "busy"

        future: Future[CapturedContext] = Future()
        command = _CaptureCommand(lease, self._limits, selected_text, future)
        self._commands.put(command)
        wrapped = asyncio.wrap_future(future)
        cancel_task = asyncio.create_task(cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {wrapped, cancel_task},
                timeout=self._limits.capture_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_task.result():
                command.cancelled.set()
                raise asyncio.CancelledError
            if wrapped in done:
                return await wrapped
            command.cancelled.set()
            with self._state_lock:
                self._health = "degraded"
            return command.accumulator.snapshot("timeout")
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def stop(self, timeout: float = 0.75) -> None:
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            future: Future[None] = Future()
            self._commands.put(_StopCommand(future))
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
            except (TimeoutError, ContextError, asyncio.CancelledError):
                pass
        thread.join(timeout)
        self._thread = None
        with self._state_lock:
            self._health = "stopped"

    def _run(self) -> None:
        automation: Any = None
        try:
            comtypes_was_loaded = "comtypes" in sys.modules
            sys.coinit_flags = 0
            import comtypes
            import comtypes.client

            if comtypes_was_loaded:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as module  # type: ignore[attr-defined]

            automation = comtypes.client.CreateObject(
                module.CUIAutomation8, interface=module.IUIAutomation
            )
            with self._state_lock:
                self._health = "ready"
            self._started.set()
            running = True
            while running:
                command = self._commands.get()
                if isinstance(command, _StopCommand):
                    if not command.future.done():
                        command.future.set_result(None)
                    running = False
                    continue
                try:
                    result = self._capture_context(automation, module, command)
                except ContextBoundaryChanged as exc:
                    if not command.future.done():
                        command.future.set_exception(exc)
                    with self._state_lock:
                        self._busy = False
                        self._health = "ready"
                    continue
                except BaseException as exc:
                    LOGGER.info("event=context_capture_failed error_type=%s", type(exc).__name__)
                    result = command.accumulator.snapshot("capture_failed")
                if not command.future.done():
                    command.future.set_result(result)
                with self._state_lock:
                    self._busy = False
                    self._health = "ready"
        except BaseException as exc:
            self._startup_error = exc
            with self._state_lock:
                self._health = "degraded"
                self._busy = False
            self._started.set()
        finally:
            try:
                import comtypes

                comtypes.CoUninitialize()
            except BaseException:
                pass
            self._started.set()

    @staticmethod
    def _capture_context(
        automation: Any, module: Any, command: _CaptureCommand
    ) -> CapturedContext:
        deadline = time.monotonic() + command.limits.capture_timeout_seconds
        target, top_level, document = UIContextService._resolve_elements(
            automation, module, command.lease
        )
        command.accumulator.update(
            "window_title", UIContextService._bounded_property(top_level, "CurrentName", 512)
        )

        UIContextService._check(command, deadline)
        try:
            target_context = UIContextService._capture_target(
                target, module, command.limits
            )
            if target_context is not None:
                command.accumulator.update("target", target_context)
        except BaseException:
            pass

        UIContextService._check(command, deadline)
        document_context: DocumentTextContext | None = None
        if document is not None:
            try:
                document_context = UIContextService._capture_document(
                    document, target, module, command.limits
                )
                if document_context is not None:
                    command.accumulator.update("document", document_context)
            except BaseException:
                pass

        UIContextService._check(command, deadline)
        try:
            ancestors = (
                UIContextService._ancestor_path(
                    automation.RawViewWalker,
                    document,
                    top_level,
                    command.lease.process_id,
                )
                if document is not None
                else ()
            )
            semantic = UIContextService._capture_semantic(
                automation,
                module,
                document or top_level,
                command.lease,
                command.limits,
                "\n".join(
                    value
                    for value in (
                        document_context.content if document_context is not None else "",
                        command.selected_text or "",
                    )
                    if value
                ),
                command,
                deadline,
                ancestors=ancestors,
            )
            if semantic is not None:
                command.accumulator.update("semantic", semantic)
        except BaseException:
            pass
        UIContextService._check(command, deadline)
        UIContextService._resolve_elements(automation, module, command.lease)
        return command.accumulator.snapshot()

    @staticmethod
    def _resolve_elements(
        automation: Any, module: Any, lease: FocusLease
    ) -> tuple[Any, Any, Any | None]:
        anchor = lease.context_anchor
        if anchor is None:
            raise ContextBoundaryChanged("Context anchor is unavailable")
        target = FocusService._current_target(automation, module)
        if (
            int(target.CurrentProcessId) != anchor.process_id
            or tuple(int(part) for part in target.GetRuntimeId())
            != anchor.target_runtime_id
        ):
            raise ContextBoundaryChanged("Focused target changed")

        top_level = None
        if anchor.top_level_window_handle:
            candidate = automation.ElementFromHandle(anchor.top_level_window_handle)
            if candidate and (
                int(candidate.CurrentProcessId) == anchor.process_id
                and tuple(int(part) for part in candidate.GetRuntimeId())
                == anchor.top_level_runtime_id
            ):
                top_level = candidate

        walker = automation.RawViewWalker
        element = target
        document = None
        for _ in range(64):
            runtime_id = tuple(int(part) for part in element.GetRuntimeId())
            if runtime_id == anchor.document_runtime_id:
                document = element
            if runtime_id == anchor.top_level_runtime_id:
                top_level = element
                break
            parent = walker.GetParentElement(element)
            if not parent or int(parent.CurrentProcessId) != anchor.process_id:
                break
            element = parent
        if top_level is None:
            raise ContextBoundaryChanged("Containing window changed")
        return target, top_level, document

    @staticmethod
    def _ancestor_path(
        walker: Any, document: Any, top_level: Any, process_id: int
    ) -> tuple[Any, ...]:
        """Return only the containing path, never sibling application chrome."""

        reversed_path: list[Any] = []
        element = document
        top_id = tuple(int(part) for part in top_level.GetRuntimeId())
        for _ in range(64):
            parent = walker.GetParentElement(element)
            if not parent or int(parent.CurrentProcessId) != process_id:
                break
            reversed_path.append(parent)
            if tuple(int(part) for part in parent.GetRuntimeId()) == top_id:
                break
            element = parent
        return tuple(reversed(reversed_path))

    @staticmethod
    def _capture_target(
        target: Any, module: Any, limits: ContextCaptureLimits
    ) -> TextTargetContext | ValueTargetContext | None:
        try:
            unknown = target.GetCurrentPattern(module.UIA_TextPatternId)
            pattern = unknown.QueryInterface(module.IUIAutomationTextPattern)
            ranges = pattern.GetSelection()
            if int(ranges.Length) == 1:
                selected = ranges.GetElement(0)
                before_range = selected.Clone()
                before_range.MoveEndpointByRange(
                    module.TextPatternRangeEndpoint_End,
                    selected,
                    module.TextPatternRangeEndpoint_Start,
                )
                before_requested = limits.target_before_max_characters + 1
                before_moved = int(
                    before_range.MoveEndpointByUnit(
                        module.TextPatternRangeEndpoint_Start,
                        module.TextUnit_Character,
                        -before_requested,
                    )
                )
                before = str(before_range.GetText(before_requested * 2 + 1))
                truncated_before = abs(before_moved) > limits.target_before_max_characters
                before = before[-limits.target_before_max_characters :] if limits.target_before_max_characters else ""

                after_range = selected.Clone()
                after_range.MoveEndpointByRange(
                    module.TextPatternRangeEndpoint_Start,
                    selected,
                    module.TextPatternRangeEndpoint_End,
                )
                after_requested = limits.target_after_max_characters + 1
                after_moved = int(
                    after_range.MoveEndpointByUnit(
                        module.TextPatternRangeEndpoint_End,
                        module.TextUnit_Character,
                        after_requested,
                    )
                )
                after = str(after_range.GetText(after_requested * 2 + 1))
                truncated_after = abs(after_moved) > limits.target_after_max_characters
                after = after[: limits.target_after_max_characters]
                if before or after:
                    return TextTargetContext(
                        before=before,
                        after=after,
                        truncated_before=truncated_before,
                        truncated_after=truncated_after,
                    )
        except BaseException:
            pass

        try:
            unknown = target.GetCurrentPattern(module.UIA_ValuePatternId)
            pattern = unknown.QueryInterface(module.IUIAutomationValuePattern)
            if bool(pattern.CurrentIsReadOnly):
                return None
            limit = limits.target_before_max_characters + limits.target_after_max_characters
            value = str(pattern.CurrentValue)
            if not value:
                return None
            return ValueTargetContext(
                content=value[:limit], truncated=len(value) > limit
            )
        except BaseException:
            return None

    @staticmethod
    def _capture_document(
        document: Any,
        target: Any,
        module: Any,
        limits: ContextCaptureLimits,
    ) -> DocumentTextContext | None:
        unknown = document.GetCurrentPattern(module.UIA_TextPatternId)
        pattern = unknown.QueryInterface(module.IUIAutomationTextPattern)
        max_characters = limits.document_max_characters
        marker = "\n[focused editable control]\n"
        text_budget = max(0, max_characters - len(marker))
        before_limit = int(text_budget * 0.8)
        after_limit = text_budget - before_limit
        anchor_range = None
        try:
            anchor_range = pattern.RangeFromChild(target)
        except BaseException:
            try:
                ranges = pattern.GetSelection()
                if int(ranges.Length) == 1:
                    anchor_range = ranges.GetElement(0)
            except BaseException:
                anchor_range = None

        if anchor_range:
            before_range = anchor_range.Clone()
            before_range.MoveEndpointByRange(
                module.TextPatternRangeEndpoint_End,
                anchor_range,
                module.TextPatternRangeEndpoint_Start,
            )
            moved_before = int(
                before_range.MoveEndpointByUnit(
                    module.TextPatternRangeEndpoint_Start,
                    module.TextUnit_Character,
                    -(before_limit + 1),
                )
            )
            before = str(before_range.GetText((before_limit + 1) * 2 + 1))
            before = before[-before_limit:] if before_limit else ""

            after_range = anchor_range.Clone()
            after_range.MoveEndpointByRange(
                module.TextPatternRangeEndpoint_Start,
                anchor_range,
                module.TextPatternRangeEndpoint_End,
            )
            moved_after = int(
                after_range.MoveEndpointByUnit(
                    module.TextPatternRangeEndpoint_End,
                    module.TextUnit_Character,
                    after_limit + 1,
                )
            )
            after = str(after_range.GetText((after_limit + 1) * 2 + 1))
            after = after[:after_limit]
            if not before and not after:
                return None
            content = before + marker + after
            return DocumentTextContext(
                content=content,
                truncated_before=abs(moved_before) > before_limit,
                truncated_after=abs(moved_after) > after_limit,
            )

        text = str(pattern.DocumentRange.GetText(max_characters * 2 + 1))
        if not text.strip():
            return None
        return DocumentTextContext(
            content=text[:max_characters],
            truncated_after=len(text) > max_characters,
        )

    @staticmethod
    def _capture_semantic(
        automation: Any,
        module: Any,
        root: Any,
        lease: FocusLease,
        limits: ContextCaptureLimits,
        document_text: str,
        command: _CaptureCommand,
        deadline: float,
        *,
        ancestors: tuple[Any, ...] = (),
    ) -> SemanticOutlineContext | None:
        walker = automation.ContentViewWalker
        cache = UIContextService._build_cache_request(automation, module)
        if len(ancestors) >= limits.semantic_max_depth:
            keep = max(0, limits.semantic_max_depth - 2)
            ancestors = (ancestors[0], *ancestors[-keep:]) if keep else ancestors[:1]
        stack: list[tuple[Any, int]] = [(root, len(ancestors))]
        items: list[SemanticItem] = []
        seen: set[tuple[int, str, str | None, str | None, tuple[str, ...]]] = set()
        visited = 0
        serialized = 0
        truncated = False
        normalized_document = " ".join(document_text.casefold().split())

        for depth, ancestor in enumerate(ancestors):
            if visited >= limits.semantic_max_elements:
                return SemanticOutlineContext(items=tuple(items), truncated=True) if items else None
            item = UIContextService._semantic_item(ancestor, module, lease, depth)
            if item is None:
                continue
            encoded = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
            if serialized + len(encoded) > limits.semantic_max_characters:
                return SemanticOutlineContext(items=tuple(items), truncated=True) if items else None
            items.append(item)
            serialized += len(encoded)
            visited += 1

        while stack:
            UIContextService._check(command, deadline)
            element, depth = stack.pop()
            if depth > limits.semantic_max_depth:
                truncated = True
                continue
            visited += 1
            if visited > limits.semantic_max_elements:
                truncated = True
                break
            if UIContextService._bool_property(element, module, "CurrentIsPassword", "UIA_IsPasswordPropertyId"):
                continue

            item = UIContextService._semantic_item(element, module, lease, depth)
            if item is not None:
                normalized_name = " ".join((item.name or "").casefold().split())
                passive_duplicate = (
                    item.role == "text"
                    and normalized_name
                    and normalized_name in normalized_document
                    and not item.states
                    and not item.focused
                )
                signature = (
                    item.depth,
                    item.role,
                    item.name,
                    item.description,
                    item.states,
                )
                encoded = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
                if not passive_duplicate and signature not in seen:
                    if serialized + len(encoded) > limits.semantic_max_characters:
                        truncated = True
                        break
                    seen.add(signature)
                    items.append(item)
                    serialized += len(encoded)

            if depth < limits.semantic_max_depth:
                children: list[Any] = []
                child = UIContextService._walker_call(
                    walker, "GetFirstChildElementBuildCache", "GetFirstChildElement", element, cache
                )
                while child:
                    children.append(child)
                    child = UIContextService._walker_call(
                        walker, "GetNextSiblingElementBuildCache", "GetNextSiblingElement", child, cache
                    )
                for child in reversed(children):
                    stack.append((child, depth + 1))

        if not items:
            return None
        return SemanticOutlineContext(items=tuple(items), truncated=truncated)

    @staticmethod
    def _semantic_item(
        element: Any, module: Any, lease: FocusLease, depth: int
    ) -> SemanticItem | None:
        control_type = UIContextService._int_property(
            element, module, "CurrentControlType", "UIA_ControlTypePropertyId"
        )
        role = UIContextService._role(module, control_type)
        name = UIContextService._bounded_property(element, "CurrentName", 1_024)
        description = UIContextService._string_property(
            element,
            module,
            "CurrentFullDescription",
            "UIA_FullDescriptionPropertyId",
            1_024,
        )
        aria_role = UIContextService._string_property(
            element, module, "CurrentAriaRole", "UIA_AriaRolePropertyId", 128
        )
        if aria_role:
            role = aria_role.casefold()
        states: list[str] = []
        aria_properties = UIContextService._string_property(
            element,
            module,
            "CurrentAriaProperties",
            "UIA_AriaPropertiesPropertyId",
            512,
        )
        if aria_properties:
            states.append(f"aria:{aria_properties}")
        if not UIContextService._bool_property(
            element, module, "CurrentIsEnabled", "UIA_IsEnabledPropertyId", default=True
        ):
            states.append("disabled")
        heading = UIContextService._int_property(
            element, module, "CurrentHeadingLevel", "UIA_HeadingLevelPropertyId"
        )
        heading_level = UIContextService._heading_level(module, heading)
        if heading_level is not None:
            states.append(f"level={heading_level}")
            role = "heading"
        if UIContextService._bool_property(
            element,
            module,
            "CurrentIsSelectionItemPatternAvailable",
            "UIA_IsSelectionItemPatternAvailablePropertyId",
        ) and UIContextService._bool_property(
            element, module, "CurrentIsSelected", "UIA_SelectionItemIsSelectedPropertyId"
        ):
            states.append("selected")
        if UIContextService._bool_property(
            element,
            module,
            "CurrentIsTogglePatternAvailable",
            "UIA_IsTogglePatternAvailablePropertyId",
        ):
            toggle = UIContextService._int_property(
                element, module, "CurrentToggleState", "UIA_ToggleToggleStatePropertyId"
            )
            if toggle is not None:
                states.append(
                    {0: "unchecked", 1: "checked", 2: "indeterminate"}.get(
                        toggle, f"toggle={toggle}"
                    )
                )
        if UIContextService._bool_property(
            element,
            module,
            "CurrentIsExpandCollapsePatternAvailable",
            "UIA_IsExpandCollapsePatternAvailablePropertyId",
        ):
            expand = UIContextService._int_property(
                element,
                module,
                "CurrentExpandCollapseState",
                "UIA_ExpandCollapseExpandCollapseStatePropertyId",
            )
            if expand is not None:
                states.append(
                    {
                        0: "collapsed",
                        1: "expanded",
                        2: "partially_expanded",
                        3: "leaf",
                    }.get(expand, f"expand={expand}")
                )
        focused = UIContextService._bool_property(
            element, module, "CurrentHasKeyboardFocus", "UIA_HasKeyboardFocusPropertyId"
        )
        try:
            focused = focused or tuple(int(part) for part in element.GetRuntimeId()) == lease.runtime_id
        except BaseException:
            pass
        if focused and role in {"edit", "document", "combobox", "textbox"}:
            # Some custom providers put the current editable value in Name or
            # FullDescription. Keep the focused structural entry but never risk
            # duplicating its contents through the semantic source.
            name = None
            description = None
        if not name and not description and not states and not focused and role in {"pane", "group", "custom", "text"}:
            return None
        return SemanticItem(
            depth=depth,
            role=role,
            name=name,
            description=description if description != name else None,
            states=tuple(states),
            focused=focused,
        )

    @staticmethod
    def _build_cache_request(automation: Any, module: Any) -> Any:
        try:
            cache = automation.CreateCacheRequest()
            cache.TreeScope = getattr(module, "TreeScope_Element", 1)
            for name in (
                "UIA_ControlTypePropertyId",
                "UIA_NamePropertyId",
                "UIA_FullDescriptionPropertyId",
                "UIA_HeadingLevelPropertyId",
                "UIA_IsEnabledPropertyId",
                "UIA_HasKeyboardFocusPropertyId",
                "UIA_IsPasswordPropertyId",
                "UIA_SelectionItemIsSelectedPropertyId",
                "UIA_IsSelectionItemPatternAvailablePropertyId",
                "UIA_ToggleToggleStatePropertyId",
                "UIA_IsTogglePatternAvailablePropertyId",
                "UIA_ExpandCollapseExpandCollapseStatePropertyId",
                "UIA_IsExpandCollapsePatternAvailablePropertyId",
                "UIA_AriaRolePropertyId",
                "UIA_AriaPropertiesPropertyId",
            ):
                property_id = getattr(module, name, None)
                if property_id is not None:
                    cache.AddProperty(property_id)
            return cache
        except BaseException:
            return None

    @staticmethod
    def _walker_call(
        walker: Any,
        cached_name: str,
        current_name: str,
        element: Any,
        cache: Any,
    ) -> Any:
        if cache is not None:
            method = getattr(walker, cached_name, None)
            if method is not None:
                try:
                    return method(element, cache)
                except BaseException:
                    pass
        return getattr(walker, current_name)(element)

    @staticmethod
    def _role(module: Any, control_type: int | None) -> str:
        names = {
            "Button": "button",
            "Calendar": "calendar",
            "CheckBox": "checkbox",
            "ComboBox": "combobox",
            "Document": "document",
            "Edit": "edit",
            "Group": "group",
            "Header": "header",
            "HeaderItem": "header_item",
            "Hyperlink": "link",
            "List": "list",
            "ListItem": "list_item",
            "Menu": "menu",
            "MenuItem": "menu_item",
            "Pane": "pane",
            "RadioButton": "radio",
            "StatusBar": "status",
            "Tab": "tabs",
            "TabItem": "tab",
            "Table": "table",
            "Text": "text",
            "ToolBar": "toolbar",
            "Tree": "tree",
            "TreeItem": "tree_item",
            "Window": "window",
        }
        for suffix, role in names.items():
            if control_type == getattr(module, f"UIA_{suffix}ControlTypeId", object()):
                return role
        return "custom"

    @staticmethod
    def _heading_level(module: Any, value: int | None) -> int | None:
        """Normalize UIA's 8005x heading enum without treating None as a heading."""

        if value is None:
            return None
        none_value = int(getattr(module, "HeadingLevel_None", 80050))
        if value == none_value:
            return None
        if none_value < value <= none_value + 9:
            return value - none_value
        # Retain compatibility with providers and test doubles that expose 1-9.
        if 1 <= value <= 9:
            return value
        return None

    @staticmethod
    def _bounded_property(element: Any, name: str, limit: int) -> str | None:
        value = None
        cached_name = name.replace("Current", "Cached", 1)
        for candidate in (cached_name, name):
            try:
                value = getattr(element, candidate)
                break
            except BaseException:
                continue
        if value is None:
            return None
        text = " ".join(str(value).split())
        return text[:limit] if text else None

    @staticmethod
    def _string_property(
        element: Any,
        module: Any,
        current_name: str,
        property_name: str,
        limit: int,
    ) -> str | None:
        value = UIContextService._bounded_property(element, current_name, limit)
        if value is not None:
            return value
        property_id = getattr(module, property_name, None)
        if property_id is None:
            return None
        try:
            raw = element.GetCurrentPropertyValue(property_id)
        except BaseException:
            return None
        if not isinstance(raw, str):
            return None
        text = " ".join(raw.split())
        return text[:limit] if text else None

    @staticmethod
    def _bool_property(
        element: Any,
        module: Any,
        current_name: str,
        property_name: str,
        *,
        default: bool = False,
    ) -> bool:
        cached_name = current_name.replace("Current", "Cached", 1)
        for candidate in (cached_name, current_name):
            try:
                return bool(getattr(element, candidate))
            except BaseException:
                continue
        property_id = getattr(module, property_name, None)
        if property_id is not None:
            try:
                return bool(element.GetCurrentPropertyValue(property_id))
            except BaseException:
                pass
        return default

    @staticmethod
    def _int_property(
        element: Any, module: Any, current_name: str, property_name: str
    ) -> int | None:
        cached_name = current_name.replace("Current", "Cached", 1)
        for candidate in (cached_name, current_name):
            try:
                value = getattr(element, candidate)
                return int(value) if value is not None else None
            except BaseException:
                continue
        property_id = getattr(module, property_name, None)
        if property_id is not None:
            try:
                value = element.GetCurrentPropertyValue(property_id)
                return int(value) if value is not None else None
            except BaseException:
                pass
        return None

    @staticmethod
    def _check(command: _CaptureCommand, deadline: float) -> None:
        if command.cancelled.is_set():
            raise ContextError("Context capture was cancelled")
        if time.monotonic() >= deadline:
            raise ContextError("Context capture deadline expired")
