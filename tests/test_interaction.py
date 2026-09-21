import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

import pytest

import omnivoice.interaction as interaction
from omnivoice.actions import (
    ActionPlan,
    InsertTextAction,
    ReplaceSelectionAction,
    ShortcutAction,
)
from omnivoice.context import (
    CapturedContext,
    ContextBoundaryChanged,
    DocumentTextContext,
    UIContext,
)
from omnivoice.config import AgentConfig
from omnivoice.interaction import (
    SELF_TEST_TEXT,
    InteractionController,
    RequestMode,
    RequestState,
)
from omnivoice.models import ModelRegistry, ModelSelection
from omnivoice.planning import PlanGenerationError
from omnivoice.speech.ports import Readiness, Recording, SpeechToTextError
from omnivoice.windows.focus import FocusLease, SelectionContext
from omnivoice.windows.keyboard import INPUT, KeyboardExecutor


LEASE = FocusLease((1, 2, 3), 100, 200, 50004)


class FakeFocus:
    def __init__(self) -> None:
        self.current = LEASE
        self.watched: FocusLease | None = None
        self.selection: SelectionContext | None = None
        self.selection_valid = True
        self.selection_checks = 0
        self.clears = 0
        self.capture_context_flags: list[bool] = []

    async def capture(self, *, include_context: bool = False) -> FocusLease:
        self.capture_context_flags.append(include_context)
        return self.current

    async def matches(self, lease: FocusLease) -> bool:
        return self.current == lease

    async def watch(self, lease: FocusLease) -> bool:
        if self.current != lease:
            return False
        self.watched = lease
        return True

    async def capture_selection(self, lease: FocusLease) -> SelectionContext | None:
        assert lease == self.current
        return self.selection

    async def selection_matches(self, selection: SelectionContext) -> bool:
        self.selection_checks += 1
        return (
            self.selection_valid
            and self.current == selection.lease
            and self.selection == selection
        )

    async def clear_watch(self) -> None:
        self.watched = None
        self.clears += 1


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


class FakeRecorder:
    readiness = Readiness(True, "fake microphone")

    def __init__(self, path: Path, *, silent: bool = False) -> None:
        self.path = path
        self.silent = silent
        self.started = False
        self.cancelled = False
        self.limit = asyncio.Event()

    async def start(self) -> None:
        self.path.write_bytes(b"temporary audio")
        self.started = True

    async def stop(self) -> Recording:
        self.started = False
        return Recording(
            path=self.path,
            duration_seconds=1.0,
            byte_count=32_000,
            rms=0 if self.silent else 500,
            peak=0 if self.silent else 1_000,
        )

    async def cancel(self) -> None:
        self.cancelled = True
        self.started = False
        self.path.unlink(missing_ok=True)

    async def wait_until_limit(self) -> None:
        await self.limit.wait()

    async def shutdown(self) -> None:
        await self.cancel()


class FakeSTT:
    provider = "fake"
    model = "literal"
    readiness = Readiness(True, "ready")

    def __init__(self, transcript: str = "private transcript") -> None:
        self.transcript = transcript
        self.calls = 0
        self.error: Exception | None = None

    async def transcribe(self, recording: Recording, cancelled: asyncio.Event) -> str:
        del recording, cancelled
        self.calls += 1
        if self.error:
            raise self.error
        return self.transcript

    async def shutdown(self) -> None:
        return None


class FakeTTS:
    readiness = Readiness(True, "Fake Voice")
    voice_name = "Fake Voice"

    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[str] = []
        self.stops = 0
        self.fail = fail

    async def start(self) -> None:
        return None

    async def speak(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("tts failed")
        self.messages.append(text)

    async def stop(self) -> None:
        self.stops += 1

    async def shutdown(self) -> None:
        return None


class FakeCue:
    def __init__(self) -> None:
        self.calls = 0

    async def play(self) -> None:
        self.calls += 1


class FakePlanner:
    def __init__(self, plan: ActionPlan | None = None) -> None:
        self.plan = plan if plan is not None else ActionPlan(actions=())
        self.calls: list[tuple[str, ModelSelection, str | None]] = []
        self.started = asyncio.Event()
        self.wait_for_cancellation = False
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None
        self.contexts: list[CapturedContext | None] = []

    async def generate(
        self,
        transcript: str,
        selection: ModelSelection,
        cancelled: asyncio.Event,
        selected_text: str | None = None,
        context: CapturedContext | None = None,
    ) -> ActionPlan:
        self.contexts.append(context)
        self.calls.append((transcript, selection, selected_text))
        self.started.set()
        if self.wait_for_cancellation:
            await cancelled.wait()
            raise asyncio.CancelledError
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.plan


class FakeContext:
    health = "ready"

    def __init__(self) -> None:
        self.calls: list[tuple[FocusLease, str | None]] = []
        self.result = CapturedContext(
            ui_context=UIContext(
                status="partial",
                document_text=DocumentTextContext(content="private page context"),
            )
        )
        self.error: Exception | None = None

    async def capture(
        self,
        lease: FocusLease,
        cancelled: asyncio.Event,
        *,
        selected_text: str | None = None,
    ) -> CapturedContext:
        assert not cancelled.is_set()
        self.calls.append((lease, selected_text))
        if self.error is not None:
            raise self.error
        return self.result


def configured_models() -> ModelRegistry:
    return ModelRegistry(
        AgentConfig(
            default_model="groq-fast",
            models={
                "groq-fast": "groq:openai/gpt-oss-20b",
                "ollama-local": "ollama:qwen3:8b",
            },
        )
    )


async def wait_for_state(controller: InteractionController, state: RequestState) -> None:
    for _ in range(200):
        if controller.state is state:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"controller did not enter {state}")


async def wait_until_idle(controller: InteractionController) -> None:
    for _ in range(300):
        if controller.state is RequestState.IDLE and controller.last_outcome is not None:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("controller did not return to idle")


def make_controller(
    tmp_path: Path,
    *,
    backend: FakeBackend | None = None,
    focus: FakeFocus | None = None,
    recorder: FakeRecorder | None = None,
    stt: FakeSTT | None = None,
    tts: FakeTTS | None = None,
    statuses: list[str] | None = None,
    planner: FakePlanner | None = None,
    models: ModelRegistry | None = None,
    context_service: FakeContext | None = None,
    context_enabled: bool = False,
) -> tuple[InteractionController, FakeBackend, FakeRecorder, FakeSTT, FakeTTS]:
    actual_backend = backend or FakeBackend()
    actual_recorder = recorder or FakeRecorder(tmp_path / "recording.wav")
    actual_stt = stt or FakeSTT()
    actual_tts = tts or FakeTTS()
    status_list = statuses if statuses is not None else []
    controller = InteractionController(
        focus or FakeFocus(),
        KeyboardExecutor(actual_backend),
        status_list.append,
        recorder=actual_recorder,
        stt=actual_stt,
        tts=actual_tts,
        ready_cue=FakeCue(),
        planner=planner,
        models=models,
        context_service=context_service,
        context_enabled=context_enabled,
    )
    return controller, actual_backend, actual_recorder, actual_stt, actual_tts


@pytest.mark.asyncio
async def test_direct_dictation_types_literal_transcript_and_cleans_audio(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    controller, backend, recorder, stt, tts = make_controller(tmp_path)

    with caplog.at_level(logging.INFO):
        controller.hotkey_pressed()
        await wait_for_state(controller, RequestState.LISTENING)
        controller.hotkey_released()
        await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.COMPLETED
    assert len(backend.sent) == len(stt.transcript)
    assert not recorder.path.exists()
    assert tts.messages == ["Done."]
    assert stt.transcript not in caplog.text


@pytest.mark.asyncio
async def test_armed_self_test_bypasses_audio_and_stt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(interaction, "SELF_TEST_PROCESSING_SECONDS", 0.0)
    controller, backend, recorder, stt, _ = make_controller(tmp_path)

    controller.arm_self_test()
    controller.hotkey_pressed()
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released()
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.COMPLETED
    assert len(backend.sent) == len(SELF_TEST_TEXT)
    assert not recorder.started
    assert stt.calls == 0
    assert not controller.is_armed


@pytest.mark.asyncio
async def test_release_before_target_ready_cancels(tmp_path: Path) -> None:
    statuses: list[str] = []
    controller, backend, _, _, _ = make_controller(tmp_path, statuses=statuses)

    controller.hotkey_pressed()
    controller.hotkey_released()
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.CANCELLED
    assert backend.sent == []
    assert any("before the target was ready" in status for status in statuses)


def test_expired_arm_is_not_active(tmp_path: Path) -> None:
    controller, *_ = make_controller(tmp_path)
    controller.arm_self_test()
    controller._armed_until = 0.0

    assert not controller.is_armed


@pytest.mark.asyncio
async def test_hotkey_press_is_ignored_while_busy(tmp_path: Path) -> None:
    statuses: list[str] = []
    controller, *_ = make_controller(tmp_path, statuses=statuses)
    controller.hotkey_pressed()
    controller.hotkey_pressed()

    assert controller.state is RequestState.VALIDATING
    assert "ignored" in statuses[-1]
    controller.cancel()
    await wait_until_idle(controller)


@pytest.mark.asyncio
async def test_focus_change_during_listening_cancels(tmp_path: Path) -> None:
    focus = FakeFocus()
    controller, backend, recorder, _, tts = make_controller(tmp_path, focus=focus)

    controller.hotkey_pressed()
    await wait_for_state(controller, RequestState.LISTENING)
    controller.focus_lost(LEASE)
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.CANCELLED
    assert backend.sent == []
    assert recorder.cancelled
    assert tts.messages == ["Cancelled."]


@pytest.mark.asyncio
async def test_silent_capture_never_reaches_stt(tmp_path: Path) -> None:
    recorder = FakeRecorder(tmp_path / "recording.wav", silent=True)
    controller, backend, _, stt, tts = make_controller(tmp_path, recorder=recorder)

    controller.hotkey_pressed()
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released()
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.CANCELLED
    assert backend.sent == []
    assert stt.calls == 0
    assert tts.messages == ["No speech detected."]


@pytest.mark.asyncio
async def test_transcription_failure_is_reported_without_content(tmp_path: Path) -> None:
    statuses: list[str] = []
    stt = FakeSTT("do not log this")
    stt.error = SpeechToTextError("provider failed")
    controller, backend, _, _, tts = make_controller(
        tmp_path, stt=stt, statuses=statuses
    )

    controller.hotkey_pressed()
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released()
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.FAILED
    assert backend.sent == []
    assert tts.messages == ["Transcription failed."]
    assert "do not log this" not in " ".join(statuses)


@pytest.mark.asyncio
async def test_partial_input_fails_and_tts_failure_does_not_mask_outcome(
    tmp_path: Path,
) -> None:
    controller, _, _, _, _ = make_controller(
        tmp_path,
        backend=FakeBackend(partial=True),
        tts=FakeTTS(fail=True),
    )

    controller.hotkey_pressed()
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released()
    await wait_until_idle(controller)

    assert controller.last_outcome is RequestState.FAILED


@pytest.mark.asyncio
async def test_literal_dictation_never_calls_action_planner(tmp_path: Path) -> None:
    planner = FakePlanner(
        ActionPlan(actions=(ShortcutAction(type="shortcut", keys=("ctrl", "s")),))
    )
    controller, _, _, _, _ = make_controller(
        tmp_path, planner=planner, models=configured_models()
    )

    controller.hotkey_pressed(RequestMode.DICTATION)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.DICTATION)
    await wait_until_idle(controller)

    assert planner.calls == []
    assert controller.last_outcome is RequestState.COMPLETED


@pytest.mark.asyncio
async def test_literal_dictation_never_captures_enabled_ui_context(tmp_path: Path) -> None:
    context_service = FakeContext()
    focus = FakeFocus()
    controller, _, _, _, _ = make_controller(
        tmp_path,
        focus=focus,
        context_service=context_service,
        context_enabled=True,
    )

    controller.hotkey_pressed(RequestMode.DICTATION)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.DICTATION)
    await wait_until_idle(controller)

    assert context_service.calls == []
    assert focus.capture_context_flags == [False]


@pytest.mark.asyncio
async def test_agent_captures_context_after_transcription_and_passes_typed_data(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    context_service = FakeContext()
    focus = FakeFocus()
    planner = FakePlanner(
        ActionPlan(actions=(InsertTextAction(type="insert_text", text="reply"),))
    )
    statuses: list[str] = []
    controller, _, _, _, _ = make_controller(
        tmp_path,
        planner=planner,
        focus=focus,
        models=configured_models(),
        context_service=context_service,
        context_enabled=True,
        statuses=statuses,
    )

    with caplog.at_level(logging.INFO):
        controller.hotkey_pressed(RequestMode.AGENT)
        await wait_for_state(controller, RequestState.LISTENING)
        controller.hotkey_released(RequestMode.AGENT)
        await wait_until_idle(controller)

    assert context_service.calls == [(LEASE, None)]
    assert focus.capture_context_flags == [True]
    assert planner.contexts == [context_service.result]
    assert any("document=20 chars" in status for status in statuses)
    assert "private page context" not in caplog.text


def test_context_session_toggle_is_rejected_while_busy(tmp_path: Path) -> None:
    statuses: list[str] = []
    controller, _, _, _, _ = make_controller(tmp_path, statuses=statuses)
    controller.state = RequestState.LISTENING

    assert not controller.set_context_enabled(True)
    assert not controller.context_enabled
    assert statuses == ["Busy (listening); UI context was not changed."]


@pytest.mark.asyncio
async def test_window_change_during_context_capture_cancels_before_model(
    tmp_path: Path,
) -> None:
    context_service = FakeContext()
    context_service.error = ContextBoundaryChanged("private provider detail")
    planner = FakePlanner()
    controller, backend, _, _, _ = make_controller(
        tmp_path,
        planner=planner,
        models=configured_models(),
        context_service=context_service,
        context_enabled=True,
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert planner.calls == []
    assert backend.sent == []
    assert controller.last_outcome is RequestState.CANCELLED


@pytest.mark.asyncio
async def test_agent_request_executes_validated_text_and_shortcut(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    statuses: list[str] = []
    planner = FakePlanner(
        ActionPlan(
            actions=(
                InsertTextAction(type="insert_text", text="First sentence."),
                ShortcutAction(type="shortcut", keys=("enter",)),
                InsertTextAction(type="insert_text", text="Second sentence."),
            )
        )
    )
    controller, backend, _, stt, tts = make_controller(
        tmp_path, planner=planner, models=configured_models(), statuses=statuses
    )

    with caplog.at_level(logging.INFO):
        controller.hotkey_pressed(RequestMode.AGENT)
        await wait_for_state(controller, RequestState.LISTENING)
        controller.hotkey_released(RequestMode.AGENT)
        await wait_until_idle(controller)

    assert planner.calls == [
        (
            stt.transcript,
            ModelSelection("groq-fast", "groq:openai/gpt-oss-20b"),
            None,
        )
    ]
    assert len(backend.sent) == len("First sentence.Second sentence.") + 1
    assert controller.last_outcome is RequestState.COMPLETED
    assert tts.messages == ["Done."]
    assert any(
        status.startswith('Model output: {"actions":') for status in statuses
    )
    assert stt.transcript not in caplog.text
    assert "hello" not in caplog.text


@pytest.mark.asyncio
async def test_agent_replaces_stable_selection_with_multiline_text_and_saves(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    focus = FakeFocus()
    focus.selection = SelectionContext("selection", LEASE, "private original text")
    planner = FakePlanner(
        ActionPlan(
            actions=(
                ReplaceSelectionAction(
                    type="replace_selection", text="First line.\nSecond line."
                ),
                ShortcutAction(type="shortcut", keys=("ctrl", "s")),
            )
        )
    )
    controller, backend, _, stt, tts = make_controller(
        tmp_path,
        focus=focus,
        planner=planner,
        models=configured_models(),
    )

    with caplog.at_level(logging.INFO):
        controller.hotkey_pressed(RequestMode.AGENT)
        await wait_for_state(controller, RequestState.LISTENING)
        controller.hotkey_released(RequestMode.AGENT)
        await wait_until_idle(controller)

    assert planner.calls == [
        (
            stt.transcript,
            ModelSelection("groq-fast", "groq:openai/gpt-oss-20b"),
            "private original text",
        )
    ]
    assert len(backend.sent) == len("First line.Second line.") + 2
    assert focus.selection_checks == 2
    assert focus.clears == 1
    assert controller.last_outcome is RequestState.COMPLETED
    assert tts.messages == ["Done."]
    assert "private original text" not in caplog.text


@pytest.mark.asyncio
async def test_agent_folds_trailing_enter_into_selection_replacement(
    tmp_path: Path,
) -> None:
    focus = FakeFocus()
    focus.selection = SelectionContext("selection", LEASE, "original text")
    planner = FakePlanner(
        ActionPlan(
            actions=(
                ReplaceSelectionAction(
                    type="replace_selection", text="Replacement sentence."
                ),
                ShortcutAction(type="shortcut", keys=("enter",)),
            )
        )
    )
    statuses: list[str] = []
    controller, backend, _, _, tts = make_controller(
        tmp_path,
        focus=focus,
        planner=planner,
        models=configured_models(),
        statuses=statuses,
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert len(backend.sent) == len("Replacement sentence.") + 1
    assert focus.selection_checks == 2
    assert focus.clears == 1
    assert controller.last_outcome is RequestState.COMPLETED
    assert tts.messages == ["Done."]
    assert any('"text":"Replacement sentence.\\n"' in status for status in statuses)


@pytest.mark.asyncio
async def test_selection_change_after_transcription_prevents_model_and_input(
    tmp_path: Path,
) -> None:
    focus = FakeFocus()
    focus.selection = SelectionContext("selection", LEASE, "original")

    class InvalidatingSTT(FakeSTT):
        async def transcribe(
            self, recording: Recording, cancelled: asyncio.Event
        ) -> str:
            result = await super().transcribe(recording, cancelled)
            focus.selection_valid = False
            return result

    planner = FakePlanner()
    controller, backend, _, _, tts = make_controller(
        tmp_path,
        focus=focus,
        stt=InvalidatingSTT(),
        planner=planner,
        models=configured_models(),
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert planner.calls == []
    assert backend.sent == []
    assert controller.last_outcome is RequestState.CANCELLED
    assert tts.messages == ["Cancelled."]


@pytest.mark.asyncio
async def test_selection_change_during_planning_prevents_replacement(
    tmp_path: Path,
) -> None:
    focus = FakeFocus()
    focus.selection = SelectionContext("selection", LEASE, "original")
    planner = FakePlanner(
        ActionPlan(
            actions=(
                ReplaceSelectionAction(type="replace_selection", text="replacement"),
            )
        )
    )
    planner.gate = asyncio.Event()
    controller, backend, _, _, tts = make_controller(
        tmp_path, focus=focus, planner=planner, models=configured_models()
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await planner.started.wait()
    focus.selection_valid = False
    planner.gate.set()
    await wait_until_idle(controller)

    assert backend.sent == []
    assert controller.last_outcome is RequestState.CANCELLED
    assert tts.messages == ["Cancelled."]


@pytest.mark.asyncio
async def test_active_selection_cannot_be_overwritten_by_insert_action(
    tmp_path: Path,
) -> None:
    focus = FakeFocus()
    focus.selection = SelectionContext("selection", LEASE, "original")
    planner = FakePlanner(
        ActionPlan(
            actions=(InsertTextAction(type="insert_text", text="implicit overwrite"),)
        )
    )
    controller, backend, _, _, tts = make_controller(
        tmp_path, focus=focus, planner=planner, models=configured_models()
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert backend.sent == []
    assert controller.last_outcome is RequestState.CANCELLED
    assert tts.messages == ["Request not completed."]


@pytest.mark.asyncio
async def test_partial_selection_replacement_stops_without_rollback(
    tmp_path: Path,
) -> None:
    focus = FakeFocus()
    focus.selection = SelectionContext("selection", LEASE, "original")
    planner = FakePlanner(
        ActionPlan(
            actions=(
                ReplaceSelectionAction(type="replace_selection", text="replacement"),
            )
        )
    )
    backend = FakeBackend(partial=True)
    controller, _, _, _, tts = make_controller(
        tmp_path,
        backend=backend,
        focus=focus,
        planner=planner,
        models=configured_models(),
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert len(backend.sent) == 2
    assert controller.last_outcome is RequestState.FAILED
    assert tts.messages == ["Request failed."]


@pytest.mark.asyncio
async def test_agent_without_configured_model_rejects_before_recording(
    tmp_path: Path,
) -> None:
    statuses: list[str] = []
    planner = FakePlanner()
    controller, backend, recorder, stt, tts = make_controller(
        tmp_path,
        planner=planner,
        models=ModelRegistry(AgentConfig()),
        statuses=statuses,
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert planner.calls == []
    assert not recorder.started
    assert stt.calls == 0
    assert backend.sent == []
    assert any("No agent model" in status for status in statuses)
    assert tts.messages == ["Request not completed."]


@pytest.mark.asyncio
async def test_unrelated_hotkey_release_cannot_stop_active_agent_recording(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(ActionPlan(actions=()))
    controller, _, _, _, _ = make_controller(
        tmp_path, planner=planner, models=configured_models()
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.DICTATION)
    await asyncio.sleep(0.02)
    assert controller.state is RequestState.LISTENING
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)


@pytest.mark.asyncio
async def test_model_selection_is_snapshotted_when_agent_hotkey_starts(
    tmp_path: Path,
) -> None:
    models = configured_models()
    planner = FakePlanner(ActionPlan(actions=()))
    controller, _, _, _, _ = make_controller(
        tmp_path, planner=planner, models=models
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    models.select("ollama-local")
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert planner.calls[0][1] == ModelSelection(
        "groq-fast", "groq:openai/gpt-oss-20b"
    )


@pytest.mark.asyncio
async def test_agent_request_does_not_consume_dictation_self_test_arm(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(ActionPlan(actions=()))
    controller, _, _, _, _ = make_controller(
        tmp_path, planner=planner, models=configured_models()
    )
    controller.arm_self_test()

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert controller.is_armed


@pytest.mark.asyncio
async def test_focus_loss_during_planning_cancels_without_input(tmp_path: Path) -> None:
    focus = FakeFocus()
    planner = FakePlanner()
    planner.wait_for_cancellation = True
    controller, backend, _, _, tts = make_controller(
        tmp_path, focus=focus, planner=planner, models=configured_models()
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await planner.started.wait()
    controller.focus_lost(LEASE)
    await wait_until_idle(controller)

    assert backend.sent == []
    assert controller.last_outcome is RequestState.CANCELLED
    assert tts.messages == ["Cancelled."]


@pytest.mark.asyncio
async def test_whole_plan_policy_rejection_happens_before_first_action(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(
        ActionPlan(
            actions=(
                InsertTextAction(type="insert_text", text="must not type"),
                ShortcutAction(type="shortcut", keys=("alt", "f4")),
            )
        )
    )
    controller, backend, _, _, tts = make_controller(
        tmp_path, planner=planner, models=configured_models()
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert backend.sent == []
    assert controller.last_outcome is RequestState.CANCELLED
    assert tts.messages == ["Request not completed."]


@pytest.mark.asyncio
async def test_focus_change_between_actions_stops_remaining_plan(tmp_path: Path) -> None:
    focus = FakeFocus()

    class FocusChangingBackend(FakeBackend):
        def send(self, inputs: Sequence[INPUT]) -> int:
            result = super().send(inputs)
            focus.current = FocusLease((9, 9, 9), 900, 901, 50004)
            return result

    planner = FakePlanner(
        ActionPlan(
            actions=(
                InsertTextAction(type="insert_text", text="a"),
                ShortcutAction(type="shortcut", keys=("ctrl", "s")),
            )
        )
    )
    backend = FocusChangingBackend()
    controller, _, _, _, _ = make_controller(
        tmp_path,
        backend=backend,
        focus=focus,
        planner=planner,
        models=configured_models(),
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert len(backend.sent) == 1
    assert controller.last_outcome is RequestState.CANCELLED


@pytest.mark.asyncio
async def test_provider_failure_sends_no_input_and_uses_fixed_status(
    tmp_path: Path,
) -> None:
    planner = FakePlanner()
    planner.error = PlanGenerationError(
        "Local Ollama request failed. Ensure Ollama is running and the selected model is installed."
    )
    controller, backend, _, _, tts = make_controller(
        tmp_path, planner=planner, models=configured_models()
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert backend.sent == []
    assert controller.last_outcome is RequestState.FAILED
    assert tts.messages == ["Request failed."]


@pytest.mark.asyncio
async def test_partial_shortcut_stops_remaining_agent_actions(tmp_path: Path) -> None:
    planner = FakePlanner(
        ActionPlan(
            actions=(
                ShortcutAction(type="shortcut", keys=("ctrl", "s")),
                InsertTextAction(type="insert_text", text="must not type"),
            )
        )
    )
    backend = FakeBackend(partial=True)
    controller, _, _, _, tts = make_controller(
        tmp_path,
        backend=backend,
        planner=planner,
        models=configured_models(),
    )

    controller.hotkey_pressed(RequestMode.AGENT)
    await wait_for_state(controller, RequestState.LISTENING)
    controller.hotkey_released(RequestMode.AGENT)
    await wait_until_idle(controller)

    assert len(backend.sent) == 2  # failed shortcut batch, then key-up cleanup
    assert controller.last_outcome is RequestState.FAILED
    assert tts.messages == ["Request failed."]
