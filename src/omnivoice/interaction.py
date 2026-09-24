"""Focus-bound dictation and AI action request orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
from enum import StrEnum
from typing import Callable, Protocol

from omnivoice.actions import (
    ActionPlan,
    ActionPlanRejected,
    InsertTextAction,
    ReplaceSelectionAction,
    ShortcutAction,
    format_action_plan,
    validate_action_plan,
)
from omnivoice.context import (
    CapturedContext,
    ContextBoundaryChanged,
    unavailable_context,
)
from omnivoice.models import ModelRegistry, ModelSelection
from omnivoice.planning import ModelTokenUsage, PlanGenerationError
from omnivoice.speech.ports import (
    AudioRecorder,
    ReadyCue,
    RecorderError,
    Recording,
    SpeechToText,
    SpeechToTextError,
    TextToSpeech,
)
from omnivoice.windows.focus import (
    FocusError,
    FocusLease,
    InvalidTargetError,
    SelectionContext,
)
from omnivoice.windows.keyboard import InputError, KeyboardExecutor


LOGGER = logging.getLogger(__name__)


def _usable_spoken_summary(summary: str | None) -> str | None:
    """Ignore unsuitable model speech without invalidating safe keyboard actions."""

    if summary is None:
        return None
    speech = summary.strip()
    return speech if 0 < len(speech) <= 300 and speech.isprintable() else None


class RequestState(StrEnum):
    """Expose each externally meaningful stage of a hotkey request."""

    IDLE = "idle"
    VALIDATING = "validating"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    PROCESSING = "processing"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RequestMode(StrEnum):
    """Distinguish literal dictation from one-shot AI interpretation."""

    DICTATION = "dictation"
    AGENT = "agent"


class FocusPort(Protocol):
    """Describe only the focus operations needed by request orchestration."""

    async def capture(self, *, include_context: bool = False) -> FocusLease: ...

    async def matches(self, lease: FocusLease) -> bool: ...

    async def watch(self, lease: FocusLease) -> bool: ...

    async def capture_selection(self, lease: FocusLease) -> SelectionContext | None: ...

    async def selection_matches(self, selection: SelectionContext) -> bool: ...

    async def clear_watch(self) -> None: ...


class ActionPlanPort(Protocol):
    """Describe the one-shot model boundary required by orchestration."""

    last_usage: ModelTokenUsage | None

    async def generate(
        self,
        transcript: str,
        selection: ModelSelection,
        cancelled: asyncio.Event,
        selected_text: str | None = None,
        context: CapturedContext | None = None,
    ) -> ActionPlan: ...


class ContextPort(Protocol):
    """Describe the isolated read-only context operation used by agent requests."""

    @property
    def health(self) -> str: ...

    async def capture(
        self,
        lease: FocusLease,
        cancelled: asyncio.Event,
        *,
        selected_text: str | None = None,
    ) -> CapturedContext: ...


class _RequestCancelled(Exception):
    """Carry a user-facing cancellation reason through the async pipeline."""


class _RequestRejected(Exception):
    """Carry a safe policy rejection without treating it as an internal failure."""


class InteractionController:
    """Serialize requests and keep speech-generated input bound to one focus lease."""

    def __init__(
        self,
        focus: FocusPort,
        keyboard: KeyboardExecutor,
        status: Callable[[str], None],
        *,
        recorder: AudioRecorder | None = None,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        ready_cue: ReadyCue | None = None,
        planner: ActionPlanPort | None = None,
        models: ModelRegistry | None = None,
        context_service: ContextPort | None = None,
        context_enabled: bool = False,
        minimum_recording_seconds: float = 0.15,
        silence_rms_threshold: int = 80,
        recording_limit_seconds: float = 30.0,
    ) -> None:
        self._focus = focus
        self._keyboard = keyboard
        self._status = status
        self._recorder = recorder
        self._stt = stt
        self._tts = tts
        self._ready_cue = ready_cue
        self._planner = planner
        self._models = models
        self._context_service = context_service
        self._context_enabled = context_enabled
        self._last_context_inspection: str | None = None
        self._minimum_recording_seconds = minimum_recording_seconds
        self._silence_rms_threshold = silence_rms_threshold
        self._recording_limit_seconds = recording_limit_seconds
        self.state = RequestState.IDLE
        self.last_outcome: RequestState | None = None
        self._active_lease: FocusLease | None = None
        self._active_mode: RequestMode | None = None
        self._cancelled = asyncio.Event()
        self._released = asyncio.Event()
        self._request_task: asyncio.Task[None] | None = None
        self._shutting_down = False

    def describe_status(self) -> str:
        """Return request state without exposing focused, spoken, or typed text."""

        last = self.last_outcome.value if self.last_outcome is not None else "none"
        mode = self._active_mode.value if self._active_mode is not None else "none"
        context = "enabled" if self._context_enabled else "disabled"
        context_health = (
            self._context_service.health if self._context_service is not None else "unavailable"
        )
        return (
            f"state={self.state.value}, mode={mode}, last_outcome={last}, "
            f"context={context}, context_worker={context_health}"
        )

    @property
    def context_enabled(self) -> bool:
        return self._context_enabled

    @property
    def last_context_inspection(self) -> str | None:
        """Return the last capture as explicit diagnostic JSON, if one exists."""

        return self._last_context_inspection

    def clear_context_inspection(self) -> None:
        """Forget the session-memory diagnostic snapshot."""

        self._last_context_inspection = None

    def set_context_enabled(self, enabled: bool) -> bool:
        """Change session-only context state when no request is active."""

        if self.state is not RequestState.IDLE:
            self._status(
                f"Busy ({self.state.value}); UI context was not changed."
            )
            return False
        self._context_enabled = enabled
        state = "enabled" if enabled else "disabled"
        health = (
            self._context_service.health if self._context_service is not None else "unavailable"
        )
        self._status(f"UI context {state} for this session (worker={health}).")
        return True

    def hotkey_pressed(self, mode: RequestMode = RequestMode.DICTATION) -> None:
        """Start press-time target binding or reject an overlapping request."""

        if self._shutting_down:
            return
        if self.state is not RequestState.IDLE:
            self._status(f"Busy ({self.state.value}); hotkey press ignored.")
            LOGGER.info("event=hotkey_ignored state=%s", self.state.value)
            return
        self._cancelled = asyncio.Event()
        self._released = asyncio.Event()
        selection = (
            self._models.snapshot()
            if mode is RequestMode.AGENT and self._models
            else None
        )
        self._active_mode = mode
        self._set_state(RequestState.VALIDATING)
        self._request_task = asyncio.create_task(
            self._run_request(mode, selection, self._context_enabled),
            name="omnivoice-request",
        )

    def hotkey_released(self, mode: RequestMode = RequestMode.DICTATION) -> None:
        """Tell the active request to stop capture."""

        if (
            not self._shutting_down
            and self.state is not RequestState.IDLE
            and mode is self._active_mode
        ):
            self._released.set()

    def focus_lost(self, lease: FocusLease) -> None:
        """Cancel whenever UIA reports loss of the request's bound target."""

        if self._active_lease == lease and self.state in {
            RequestState.VALIDATING,
            RequestState.LISTENING,
            RequestState.TRANSCRIBING,
            RequestState.PROCESSING,
            RequestState.EXECUTING,
        }:
            self.cancel("Focus changed. Request cancelled.")

    def cancel(self, reason: str = "Request cancelled.") -> None:
        """Signal every stage without refocusing or attempting keyboard rollback."""

        if self.state is RequestState.IDLE:
            self._status("No active request to cancel.")
            return
        self._cancelled.set()
        self._released.set()
        self._status(reason)
        LOGGER.info("event=request_cancel_requested state=%s", self.state.value)

    async def shutdown(self) -> None:
        """Cancel active work and release request-scoped resources."""

        self._shutting_down = True
        if self.state is not RequestState.IDLE:
            self._cancelled.set()
            self._released.set()
        task = self._request_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self._safe_clear_watch()

    async def _run_request(
        self,
        mode: RequestMode,
        selection: ModelSelection | None,
        context_enabled: bool,
    ) -> None:
        """Run one request mode against one immutable focus lease."""

        recording: Recording | None = None
        recorder_active = False
        selection_context: SelectionContext | None = None
        model_requested = False
        completion_speech = "Done."
        try:
            await self._stop_pending_speech()
            if mode is RequestMode.AGENT and selection is None:
                raise _RequestRejected(
                    "No agent model is configured. Add a model profile to config.yaml."
                )
            self._status("Validating and binding the focused control...")
            lease = await self._focus.capture(
                include_context=mode is RequestMode.AGENT and context_enabled
            )
            self._active_lease = lease
            LOGGER.info(
                "event=target_captured pid=%s hwnd=%s control_type=%s",
                lease.process_id,
                lease.native_window_handle,
                lease.control_type,
            )
            if self._cancelled.is_set():
                raise _RequestCancelled("Request cancelled.")
            if self._released.is_set():
                raise _RequestCancelled("Hotkey released before the target was ready.")
            if not await self._focus.watch(lease):
                raise _RequestCancelled("Focus changed. Request cancelled.")
            if mode is RequestMode.AGENT:
                selection_context = await self._focus.capture_selection(lease)

            self._require_dictation_ready()
            await self._play_ready_cue()
            if self._cancelled.is_set() or self._released.is_set():
                raise _RequestCancelled("Hotkey released before recording was ready.")
            assert self._recorder is not None
            await self._recorder.start()
            recorder_active = True
            self._set_state(RequestState.LISTENING)
            self._status("Listening. Speak now, then release the hotkey.")
            await self._wait_for_recording_end()
            if self._cancelled.is_set():
                raise _RequestCancelled("Request cancelled.")

            recording = await self._recorder.stop()
            recorder_active = False
            if self._cancelled.is_set():
                raise _RequestCancelled("Request cancelled.")
            if recording.is_silent(
                self._minimum_recording_seconds,
                self._silence_rms_threshold,
            ):
                self._status("No speech detected.")
                await self._speak(
                    "Request not completed."
                    if mode is RequestMode.AGENT
                    else "No speech detected."
                )
                self._finish(RequestState.CANCELLED)
                return

            self._set_state(RequestState.TRANSCRIBING)
            self._status("Transcribing locally...")
            assert self._stt is not None
            transcript = await self._stt.transcribe(recording, self._cancelled)
            if self._cancelled.is_set():
                raise _RequestCancelled("Request cancelled.")
            if mode is RequestMode.DICTATION:
                await self._execute_text(lease, transcript)
                completion = "Dictation completed."
            else:
                if self._planner is None or selection is None:
                    raise PlanGenerationError("AI action planning is unavailable.")
                if selection_context is not None and not await self._focus.selection_matches(
                    selection_context
                ):
                    raise _RequestCancelled("Selected text changed. Request cancelled.")
                captured_context: CapturedContext | None = None
                if context_enabled:
                    self._last_context_inspection = None
                    if not await self._focus.matches(lease):
                        raise _RequestCancelled("Focus changed. Request cancelled.")
                    self._set_state(RequestState.PROCESSING)
                    self._status("Capturing bounded context from the active window...")
                    captured_context = await self._capture_context(
                        lease, selection_context
                    )
                    if self._cancelled.is_set() or not await self._focus.matches(lease):
                        raise _RequestCancelled("Focus changed. Request cancelled.")
                    if (
                        selection_context is not None
                        and not await self._focus.selection_matches(selection_context)
                    ):
                        raise _RequestCancelled(
                            "Selected text changed. Request cancelled."
                        )
                    self._last_context_inspection = json.dumps(
                        {
                            "selected_text": (
                                selection_context.text
                                if selection_context is not None
                                else None
                            ),
                            "target_context": (
                                captured_context.target_context.model_dump(mode="json")
                                if captured_context.target_context is not None
                                else None
                            ),
                            "ui_context": captured_context.ui_context.model_dump(
                                mode="json"
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    self._status(captured_context.metadata_summary())
                self._set_state(RequestState.PROCESSING)
                self._status(
                    f"Generating an action plan with {selection.alias} "
                    f"({selection.selector})..."
                )
                model_requested = True
                plan = validate_action_plan(
                    await self._planner.generate(
                        transcript,
                        selection,
                        self._cancelled,
                        selected_text=(
                            selection_context.text
                            if selection_context is not None
                            else None
                        ),
                        context=captured_context,
                    ),
                    has_selection=selection_context is not None,
                )
                self._status(f"Model output: {format_action_plan(plan)}")
                if not plan.actions:
                    raise _RequestRejected(
                        "The request cannot be completed with the permitted actions."
                    )
                await self._execute_plan(lease, plan, selection_context)
                completion = "AI action plan completed."
                completion_speech = _usable_spoken_summary(plan.spoken_summary) or "Done."
            self._status(completion)
            self._finish(RequestState.COMPLETED)
            await self._speak(completion_speech)
        except InvalidTargetError as exc:
            message = f"Target unavailable: {exc}"
            self._status(message)
            LOGGER.info("event=target_unavailable reason=%s", type(exc).__name__)
            self._finish(RequestState.CANCELLED)
            await self._speak(
                "Request not completed."
                if mode is RequestMode.AGENT
                else "That field isn't supported."
            )
        except _RequestCancelled as exc:
            if str(exc) and not self._cancelled.is_set():
                self._status(str(exc))
            self._cancelled.set()
            self._finish(RequestState.CANCELLED)
            await self._speak("Cancelled.")
        except (ActionPlanRejected, _RequestRejected) as exc:
            self._status(str(exc) or "The request could not be completed.")
            LOGGER.info("event=request_not_completed reason=%s", type(exc).__name__)
            self._finish(RequestState.CANCELLED)
            await self._speak("Request not completed.")
        except asyncio.CancelledError:
            self._cancelled.set()
            if self.state is not RequestState.IDLE:
                self._status("Request cancelled before all input was sent.")
                self._finish(RequestState.CANCELLED)
            await self._speak("Cancelled.")
        except SpeechToTextError as exc:
            self._status(f"Transcription failed: {exc}")
            LOGGER.info("event=transcription_failed error_type=%s", type(exc).__name__)
            self._finish(RequestState.FAILED)
            await self._speak(
                "Request failed."
                if mode is RequestMode.AGENT
                else "Transcription failed."
            )
        except RecorderError as exc:
            self._status(f"Recording failed safely: {exc}")
            LOGGER.info("event=recording_failed error_type=%s", type(exc).__name__)
            self._finish(RequestState.FAILED)
            await self._speak(
                "Request failed." if mode is RequestMode.AGENT else "Cancelled."
            )
        except PlanGenerationError as exc:
            self._status(exc.user_message)
            LOGGER.info("event=plan_generation_failed error_type=%s", type(exc).__name__)
            self._finish(RequestState.FAILED)
            await self._speak("Request failed.")
        except (FocusError, InputError) as exc:
            message = f"Request failed safely: {exc}"
            self._status(message)
            LOGGER.info("event=request_failed error_type=%s", type(exc).__name__)
            self._finish(RequestState.FAILED)
            await self._speak(
                "Request failed." if mode is RequestMode.AGENT else "Cancelled."
            )
        except BaseException as exc:
            self._status("Request failed safely because of an unexpected internal error.")
            # Arbitrary provider exception messages can contain content, so the
            # request log records only their class rather than a traceback.
            LOGGER.error("event=request_failed error_type=%s", type(exc).__name__)
            self._finish(RequestState.FAILED)
            await self._speak(
                "Request failed." if mode is RequestMode.AGENT else "Cancelled."
            )
        finally:
            if model_requested and self._planner is not None:
                usage = self._planner.last_usage
                self._status(
                    usage.summary() if usage is not None else "Model usage: unavailable"
                )
            if recorder_active and self._recorder is not None:
                await self._safe_cancel_recorder()
            if recording is not None:
                self._safe_cleanup_recording(recording)
            self._active_lease = None
            self._active_mode = None
            await self._safe_clear_watch()
            self._request_task = None
            self._set_state(RequestState.IDLE)

    async def _capture_context(
        self,
        lease: FocusLease,
        selection: SelectionContext | None,
    ) -> CapturedContext:
        service = self._context_service
        if service is None:
            return unavailable_context("worker_unavailable")
        try:
            return await service.capture(
                lease,
                self._cancelled,
                selected_text=selection.text if selection is not None else None,
            )
        except ContextBoundaryChanged as exc:
            raise _RequestCancelled("Window changed. Request cancelled.") from exc
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            LOGGER.info(
                "event=context_capture_failed error_type=%s", type(exc).__name__
            )
            return unavailable_context("capture_failed")

    def _require_dictation_ready(self) -> None:
        if self._recorder is None or self._stt is None:
            raise SpeechToTextError("Speech-to-text is disabled")
        readiness = self._stt.readiness
        if not readiness.ready:
            raise SpeechToTextError(readiness.detail)

    async def _play_ready_cue(self) -> None:
        if self._ready_cue is None:
            return
        try:
            await self._ready_cue.play()
        except BaseException as exc:
            # A cue is useful feedback but is not part of the focus or input
            # safety boundary, so a device-specific beep failure is non-fatal.
            LOGGER.info("event=ready_cue_failed error_type=%s", type(exc).__name__)
            self._status("Ready cue unavailable; recording will continue.")

    async def _wait_for_recording_end(self) -> None:
        """Stop accepting audio at the limit but wait for physical key release."""

        assert self._recorder is not None
        release_task = asyncio.create_task(self._released.wait())
        cancel_task = asyncio.create_task(self._cancelled.wait())
        limit_task = asyncio.create_task(self._recorder.wait_until_limit())
        tasks = {release_task, cancel_task, limit_task}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done and cancel_task.result():
                raise _RequestCancelled("Request cancelled.")
            if limit_task in done:
                limit = f"{self._recording_limit_seconds:g}"
                self._status(f"{limit}-second recording limit reached. Release the hotkey.")
                done, _ = await asyncio.wait(
                    {release_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done and cancel_task.result():
                    raise _RequestCancelled("Request cancelled.")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_for_release_or_cancel(self) -> None:
        release_task = asyncio.create_task(self._released.wait())
        cancel_task = asyncio.create_task(self._cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {release_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_task.result():
                raise _RequestCancelled("Request cancelled.")
        finally:
            for task in (release_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(release_task, cancel_task, return_exceptions=True)

    async def _execute_text(self, lease: FocusLease, text: str) -> None:
        """Perform final modifier and focus checks before guarded SendInput."""

        if not await self._keyboard.wait_for_modifiers_released(timeout=1.0):
            raise _RequestCancelled("A modifier key remained held. Request cancelled.")
        if not await self._focus.matches(lease):
            raise _RequestCancelled("Focus changed. Request cancelled.")
        self._set_state(RequestState.EXECUTING)
        self._status("Typing into the bound field...")
        await self._keyboard.type_text(
            text,
            lambda: self._focus.matches(lease),
            self._cancelled,
        )

    async def _execute_plan(
        self,
        lease: FocusLease,
        plan: ActionPlan,
        selection: SelectionContext | None = None,
    ) -> None:
        """Execute a fully prevalidated plan while preserving its focus lease."""

        if not await self._keyboard.wait_for_modifiers_released(timeout=1.0):
            raise _RequestCancelled("A modifier key remained held. Request cancelled.")
        self._set_state(RequestState.EXECUTING)
        self._status("Executing the validated action plan...")
        for action in plan.actions:
            if self._cancelled.is_set() or not await self._focus.matches(lease):
                raise _RequestCancelled("Focus changed. Request cancelled.")
            if isinstance(action, InsertTextAction):
                await self._keyboard.type_text(
                    action.text,
                    lambda: self._focus.matches(lease),
                    self._cancelled,
                )
            elif isinstance(action, ReplaceSelectionAction):
                if selection is None or not await self._focus.selection_matches(selection):
                    raise _RequestCancelled("Selected text changed. Request cancelled.")
                await self._replace_selection(lease, action.text)
            elif isinstance(action, ShortcutAction):
                await self._keyboard.press_shortcut(
                    action.keys,
                    lambda: self._focus.matches(lease),
                    self._cancelled,
                )

    async def _replace_selection(self, lease: FocusLease, text: str) -> None:
        """Replace the active range, translating only CR/LF into guarded Enter presses."""

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        parts = normalized.split("\n")
        for index, part in enumerate(parts):
            if part:
                await self._keyboard.type_text(
                    part,
                    lambda: self._focus.matches(lease),
                    self._cancelled,
                )
            if index < len(parts) - 1:
                await self._keyboard.press_shortcut(
                    ("enter",),
                    lambda: self._focus.matches(lease),
                    self._cancelled,
                )

    async def _stop_pending_speech(self) -> None:
        if self._tts is None:
            return
        try:
            await self._tts.stop()
        except BaseException as exc:
            LOGGER.info("event=tts_stop_failed error_type=%s", type(exc).__name__)

    async def _speak(self, text: str) -> None:
        """Keep fixed status speech best-effort and out of request outcomes."""

        if self._tts is None:
            return
        try:
            await self._tts.speak(text)
        except BaseException as exc:
            # Never include the status text in logs; operational metadata is enough.
            LOGGER.info("event=tts_speak_failed error_type=%s", type(exc).__name__)

    async def _safe_cancel_recorder(self) -> None:
        try:
            assert self._recorder is not None
            await self._recorder.cancel()
        except BaseException as exc:
            LOGGER.info("event=recording_cancel_failed error_type=%s", type(exc).__name__)

    @staticmethod
    def _safe_cleanup_recording(recording: Recording) -> None:
        try:
            recording.cleanup()
        except OSError as exc:
            # Do not log the request-scoped filename.
            LOGGER.info("event=recording_cleanup_failed error_type=%s", type(exc).__name__)

    async def _safe_clear_watch(self) -> None:
        try:
            await self._focus.clear_watch()
        except FocusError:
            LOGGER.exception("event=focus_watch_clear_failed")

    def _set_state(self, state: RequestState) -> None:
        self.state = state
        LOGGER.info("event=request_state state=%s", state.value)

    def _finish(self, outcome: RequestState) -> None:
        self._set_state(outcome)
        self.last_outcome = outcome
