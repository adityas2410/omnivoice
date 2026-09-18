"""Local whisper.cpp speech-to-text adapter."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from omnivoice.speech.ports import (
    Readiness,
    Recording,
    SpeechTimeoutError,
    SpeechToTextError,
)


LOGGER = logging.getLogger(__name__)
MAX_TRANSCRIPT_CHARACTERS = 2_000


class WhisperCppSTT:
    """Run a pinned or user-supplied whisper.cpp executable without a shell."""

    provider = "whisper_cpp"

    def __init__(
        self,
        *,
        executable: Path,
        model_path: Path,
        model: str,
        language: str,
        timeout_seconds: float,
        threads: int | None,
    ) -> None:
        self._executable = executable
        self._model_path = model_path
        self.model = model
        self._language = language
        self._timeout_seconds = timeout_seconds
        self._threads = threads or max(1, min(8, (os.cpu_count() or 2) - 1))
        self._process: asyncio.subprocess.Process | None = None

    @property
    def readiness(self) -> Readiness:
        """Check local assets without executing or downloading anything."""

        if not self._executable.is_file():
            return Readiness(False, "run 'omnivoice speech setup'")
        if not self._model_path.is_file():
            return Readiness(False, "run 'omnivoice speech setup'")
        return Readiness(True, "local assets ready")

    def command(self, recording: Recording, output_base: Path) -> list[str]:
        """Build an inspectable argument array with no command-shell parsing."""

        return [
            str(self._executable),
            "-m",
            str(self._model_path),
            "-f",
            str(recording.path),
            "-l",
            self._language,
            "-t",
            str(self._threads),
            "-otxt",
            "-of",
            str(output_base),
            "-nt",
            "-np",
        ]

    async def transcribe(
        self, recording: Recording, cancelled: asyncio.Event
    ) -> str:
        """Transcribe one WAV and remove whisper.cpp output in every outcome."""

        readiness = self.readiness
        if not readiness.ready:
            raise SpeechToTextError("Local transcription assets are not installed")
        output_base = recording.path.with_suffix(".whisper")
        transcript_path = Path(f"{output_base}.txt")
        started = asyncio.get_running_loop().time()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command(recording, output_base),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            process_task = asyncio.create_task(self._process.wait())
            cancel_task = asyncio.create_task(cancelled.wait())
            done, _ = await asyncio.wait(
                {process_task, cancel_task},
                timeout=self._timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_task.result():
                await self._terminate_process()
                process_task.cancel()
                await asyncio.gather(process_task, return_exceptions=True)
                raise asyncio.CancelledError
            if process_task not in done:
                await self._terminate_process()
                process_task.cancel()
                await asyncio.gather(process_task, return_exceptions=True)
                raise SpeechTimeoutError("Local transcription timed out")
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            if process_task.result() != 0:
                raise SpeechToTextError("Local transcription process failed")
            try:
                raw = transcript_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise SpeechToTextError("Local transcription produced no result") from exc
            transcript = normalize_transcript(raw)
            LOGGER.info(
                "event=transcription_completed provider=%s model=%s duration_seconds=%.3f character_count=%s",
                self.provider,
                self.model,
                asyncio.get_running_loop().time() - started,
                len(transcript),
            )
            return transcript
        except asyncio.CancelledError:
            # Task cancellation is rarer than the cooperative event path, but
            # must still terminate the native process before dropping its handle.
            await self._terminate_process()
            raise
        except FileNotFoundError as exc:
            raise SpeechToTextError("Local transcription executable is unavailable") from exc
        finally:
            self._process = None
            transcript_path.unlink(missing_ok=True)

    async def shutdown(self) -> None:
        """Terminate a running child so no inference process survives exit."""

        await self._terminate_process()

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()
            await process.wait()


def normalize_transcript(raw: str) -> str:
    """Collapse segment whitespace while preserving literal words and punctuation."""

    if "\x00" in raw:
        raise SpeechToTextError("Transcription contained an invalid NUL character")
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        raise SpeechToTextError("Transcription was empty")
    if len(normalized) > MAX_TRANSCRIPT_CHARACTERS:
        raise SpeechToTextError("Transcription exceeded the safe typing limit")
    return normalized
