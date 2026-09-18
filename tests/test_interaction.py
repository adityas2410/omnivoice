import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

import pytest

import omnivoice.interaction as interaction
from omnivoice.interaction import InteractionController, RequestState, SELF_TEST_TEXT
from omnivoice.speech.ports import Readiness, Recording, SpeechToTextError
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
