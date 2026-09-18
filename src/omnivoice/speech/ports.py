"""Narrow contracts shared by the interaction controller and speech adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SpeechError(RuntimeError):
    """Base class for failures that must stop the current speech pipeline."""


class RecorderError(SpeechError):
    """Raised when microphone capture cannot start or finish safely."""


class SpeechToTextError(SpeechError):
    """Raised when an STT provider cannot produce an acceptable transcript."""


class SpeechTimeoutError(SpeechToTextError):
    """Raised when transcription exceeds its configured deadline."""


@dataclass(frozen=True, slots=True)
class Readiness:
    """Describe provider availability without exposing captured content."""

    ready: bool
    detail: str


@dataclass(slots=True)
class Recording:
    """Carry a temporary WAV and content-free capture measurements."""

    path: Path
    duration_seconds: float
    byte_count: int
    rms: float
    peak: int
    reached_limit: bool = False

    def is_silent(self, minimum_seconds: float, rms_threshold: int) -> bool:
        """Reject empty, extremely short, or conservatively silent captures."""

        return (
            self.byte_count == 0
            or self.duration_seconds < minimum_seconds
            or self.rms <= rms_threshold
        )

    def cleanup(self) -> None:
        """Remove request audio without logging its content or temporary name."""

        self.path.unlink(missing_ok=True)


class AudioRecorder(Protocol):
    """Capture exactly one bounded push-to-talk recording at a time."""

    @property
    def readiness(self) -> Readiness: ...

    async def start(self) -> None: ...

    async def stop(self) -> Recording: ...

    async def cancel(self) -> None: ...

    async def wait_until_limit(self) -> None: ...

    async def shutdown(self) -> None: ...


class SpeechToText(Protocol):
    """Convert a WAV to literal text while honoring cooperative cancellation."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def readiness(self) -> Readiness: ...

    async def transcribe(
        self, recording: Recording, cancelled: asyncio.Event
    ) -> str: ...

    async def shutdown(self) -> None: ...


class TextToSpeech(Protocol):
    """Speak fixed status messages without blocking request completion."""

    @property
    def readiness(self) -> Readiness: ...

    @property
    def voice_name(self) -> str | None: ...

    async def start(self) -> None: ...

    async def speak(self, text: str) -> None: ...

    async def stop(self) -> None: ...

    async def shutdown(self) -> None: ...


class ReadyCue(Protocol):
    """Signal that focus is bound and microphone capture is about to begin."""

    async def play(self) -> None: ...

