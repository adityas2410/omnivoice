import asyncio
import logging
from pathlib import Path

import pytest

from omnivoice.speech.ports import Recording, SpeechToTextError
from omnivoice.speech.whisper_cpp import WhisperCppSTT, normalize_transcript


def make_adapter(tmp_path: Path, **overrides: object) -> WhisperCppSTT:
    executable = tmp_path / "whisper-cli.exe"
    model = tmp_path / "model.bin"
    executable.write_bytes(b"exe")
    model.write_bytes(b"model")
    values = {
        "executable": executable,
        "model_path": model,
        "model": "small.en",
        "language": "en",
        "timeout_seconds": 1.0,
        "threads": 3,
    }
    values.update(overrides)
    return WhisperCppSTT(**values)


def make_recording(tmp_path: Path) -> Recording:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"wav")
    return Recording(path, 1.0, 32_000, 500, 1_000)


def test_normalize_transcript_collapses_segments_but_preserves_text() -> None:
    assert normalize_transcript("  Hello,\n\nworld!  ") == "Hello, world!"


@pytest.mark.parametrize("raw", ["", " \n ", "bad\x00text", "x" * 2001])
def test_normalize_transcript_rejects_unsafe_results(raw: str) -> None:
    with pytest.raises(SpeechToTextError):
        normalize_transcript(raw)


def test_command_is_an_argument_array_with_expected_options(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    recording = make_recording(tmp_path)
    output = tmp_path / "result"

    command = adapter.command(recording, output)

    assert command[0].endswith("whisper-cli.exe")
    assert command[command.index("-m") + 1].endswith("model.bin")
    assert command[command.index("-f") + 1].endswith("audio.wav")
    assert command[command.index("-t") + 1] == "3"
    assert command[command.index("-of") + 1] == str(output)
    assert "-otxt" in command


@pytest.mark.asyncio
async def test_transcribe_reads_and_removes_provider_output_without_logging_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = make_adapter(tmp_path)
    recording = make_recording(tmp_path)
    secret = "private dictated words"

    class Process:
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

    async def fake_create(*args: str, **kwargs: object) -> Process:
        del kwargs
        output_base = Path(args[args.index("-of") + 1])
        Path(f"{output_base}.txt").write_text(secret, encoding="utf-8")
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with caplog.at_level(logging.INFO):
        result = await adapter.transcribe(recording, asyncio.Event())

    assert result == secret
    assert not recording.path.with_suffix(".whisper.txt").exists()
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_terminates_running_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = make_adapter(tmp_path)
    recording = make_recording(tmp_path)
    cancelled = asyncio.Event()

    class Process:
        returncode: int | None = None
        terminated = False
        finished = asyncio.Event()

        async def wait(self) -> int:
            await self.finished.wait()
            return self.returncode or 0

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 1
            self.finished.set()

        def kill(self) -> None:
            self.terminate()

    process = Process()

    async def fake_create(*args: str, **kwargs: object) -> Process:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    task = asyncio.create_task(adapter.transcribe(recording, cancelled))
    await asyncio.sleep(0)
    cancelled.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated


def test_missing_assets_report_setup_guidance(tmp_path: Path) -> None:
    adapter = WhisperCppSTT(
        executable=tmp_path / "missing.exe",
        model_path=tmp_path / "missing.bin",
        model="small.en",
        language="en",
        timeout_seconds=1,
        threads=1,
    )

    assert not adapter.readiness.ready
    assert "speech setup" in adapter.readiness.detail
