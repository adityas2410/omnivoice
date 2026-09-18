"""Bounded microphone capture using sounddevice's bundled PortAudio runtime."""

from __future__ import annotations

import asyncio
import logging
import math
import tempfile
import threading
import wave
from array import array
from pathlib import Path
from typing import Any

from omnivoice.speech.ports import Readiness, RecorderError, Recording


LOGGER = logging.getLogger(__name__)


def list_input_devices() -> list[tuple[int, str]]:
    """Return stable PortAudio IDs and names for devices with input channels."""

    try:
        import sounddevice as sd

        devices: list[tuple[int, str]] = []
        for index, info in enumerate(sd.query_devices()):
            if int(info["max_input_channels"]) > 0:
                devices.append((index, str(info["name"])))
        return devices
    except BaseException as exc:
        raise RecorderError("Microphone devices could not be enumerated") from exc


class SoundDeviceRecorder:
    """Own one raw mono stream and keep its callback bounded and non-blocking."""

    def __init__(
        self,
        *,
        device: int | str | None,
        sample_rate: int,
        max_seconds: float,
        temp_directory: Path | None = None,
        sounddevice_module: Any | None = None,
    ) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._max_seconds = max_seconds
        self._max_bytes = int(sample_rate * max_seconds * 2)
        self._temp_directory = temp_directory
        self._sd = sounddevice_module
        self._stream: Any = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._limit_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._path: Path | None = None
        self._reached_limit = False

    @property
    def readiness(self) -> Readiness:
        """Probe whether PortAudio can open the selected mono input format."""

        label = "default input device" if self._device is None else str(self._device)
        try:
            if self._sd is None:
                import sounddevice as sd

                module = sd
            else:
                module = self._sd
            check = getattr(module, "check_input_settings", None)
            if check is not None:
                check(
                    device=self._device,
                    channels=1,
                    dtype="int16",
                    samplerate=self._sample_rate,
                )
            return Readiness(True, label)
        except BaseException:
            return Readiness(False, f"{label} unavailable")

    async def start(self) -> None:
        """Create request storage and start one PortAudio input stream."""

        if self._stream is not None:
            raise RecorderError("A microphone recording is already active")
        self._loop = asyncio.get_running_loop()
        self._limit_event = asyncio.Event()
        self._buffer = bytearray()
        self._reached_limit = False
        fd, raw_path = tempfile.mkstemp(
            prefix="omnivoice-recording-",
            suffix=".wav",
            dir=self._temp_directory,
        )
        # The audio callback never performs disk I/O. The bounded PCM buffer is
        # written to this request-scoped WAV only after the stream has stopped.
        import os

        os.close(fd)
        self._path = Path(raw_path)
        try:
            await asyncio.to_thread(self._open_stream)
        except BaseException as exc:
            self._remove_path()
            self._stream = None
            raise RecorderError("The microphone could not be opened") from exc
        LOGGER.info(
            "event=recording_started sample_rate=%s max_seconds=%s",
            self._sample_rate,
            self._max_seconds,
        )

    def _open_stream(self) -> None:
        """Open the blocking PortAudio object away from the asyncio loop."""

        if self._sd is None:
            import sounddevice as sd

            self._sd = sd
        stream = self._sd.RawInputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            device=self._device,
            callback=self._audio_callback,
        )
        try:
            stream.start()
        except BaseException:
            # Construction can succeed even when activation fails; close that
            # half-open PortAudio object before surfacing the recorder error.
            stream.close()
            raise
        self._stream = stream

    def _audio_callback(
        self, indata: Any, frames: int, timing: Any, status: Any
    ) -> None:
        """Copy at most the configured PCM budget from PortAudio's callback."""

        del frames, timing
        if status:
            # PortAudio status details can contain device-specific text. Record
            # only a category and continue; a later empty capture fails closed.
            LOGGER.warning("event=recording_stream_status category=portaudio")
        chunk = bytes(indata)
        notify = False
        with self._lock:
            remaining = self._max_bytes - len(self._buffer)
            if remaining > 0:
                self._buffer.extend(chunk[:remaining])
            if len(self._buffer) >= self._max_bytes and not self._reached_limit:
                self._reached_limit = True
                notify = True
        if notify and self._loop is not None and self._limit_event is not None:
            self._loop.call_soon_threadsafe(self._limit_event.set)

    async def wait_until_limit(self) -> None:
        """Wait until the callback has accepted the full configured duration."""

        event = self._limit_event
        if event is None:
            raise RecorderError("No microphone recording is active")
        await event.wait()

    async def stop(self) -> Recording:
        """Close the stream, write a canonical WAV, and return safe metadata."""

        stream = self._stream
        path = self._path
        if stream is None or path is None:
            raise RecorderError("No microphone recording is active")
        self._stream = None
        try:
            await asyncio.to_thread(self._close_stream, stream)
            with self._lock:
                pcm = bytes(self._buffer)
                reached_limit = self._reached_limit
            await asyncio.to_thread(self._write_wav, path, pcm)
            rms, peak = _pcm_metrics(pcm)
            duration = len(pcm) / (self._sample_rate * 2)
            recording = Recording(
                path=path,
                duration_seconds=duration,
                byte_count=len(pcm),
                rms=rms,
                peak=peak,
                reached_limit=reached_limit,
            )
            self._path = None
            LOGGER.info(
                "event=recording_stopped duration_seconds=%.3f byte_count=%s reached_limit=%s",
                duration,
                len(pcm),
                reached_limit,
            )
            return recording
        except BaseException as exc:
            self._remove_path()
            raise RecorderError("The microphone recording could not be finalized") from exc
        finally:
            self._reset_capture()

    async def cancel(self) -> None:
        """Stop active capture and delete its request-scoped temporary WAV."""

        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                await asyncio.to_thread(self._close_stream, stream)
            except BaseException:
                LOGGER.info("event=recording_cancel_close_failed category=portaudio")
        self._remove_path()
        self._reset_capture()

    async def shutdown(self) -> None:
        """Ensure no PortAudio stream or request recording survives shutdown."""

        await self.cancel()

    @staticmethod
    def _close_stream(stream: Any) -> None:
        try:
            stream.stop()
        finally:
            stream.close()

    def _write_wav(self, path: Path, pcm: bytes) -> None:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._sample_rate)
            wav_file.writeframes(pcm)

    def _remove_path(self) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None

    def _reset_capture(self) -> None:
        with self._lock:
            self._buffer = bytearray()
        self._limit_event = None
        self._loop = None
        self._reached_limit = False


def _pcm_metrics(pcm: bytes) -> tuple[float, int]:
    """Compute content-free RMS and peak measurements for signed 16-bit PCM."""

    if not pcm:
        return 0.0, 0
    samples = array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0.0, 0
    peak = max(abs(sample) for sample in samples)
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return math.sqrt(mean_square), peak
