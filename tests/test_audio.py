import asyncio
import wave
from pathlib import Path

import pytest

from omnivoice.speech.audio import SoundDeviceRecorder, _pcm_metrics


class FakeStream:
    def __init__(self, callback: object) -> None:
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


class FakeSoundDevice:
    def __init__(self) -> None:
        self.stream: FakeStream | None = None

    def RawInputStream(self, **kwargs: object) -> FakeStream:
        self.stream = FakeStream(kwargs["callback"])
        return self.stream


@pytest.mark.asyncio
async def test_recorder_writes_mono_pcm_wav_and_reports_metrics(tmp_path: Path) -> None:
    sounddevice = FakeSoundDevice()
    recorder = SoundDeviceRecorder(
        device=None,
        sample_rate=16_000,
        max_seconds=30,
        temp_directory=tmp_path,
        sounddevice_module=sounddevice,
    )

    await recorder.start()
    assert sounddevice.stream is not None
    sounddevice.stream.callback(b"\xe8\x03\x18\xfc" * 100, 200, None, None)
    recording = await recorder.stop()

    assert recording.byte_count == 400
    assert recording.rms == 1000
    assert recording.peak == 1000
    with wave.open(str(recording.path), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnframes() == 200
    recording.cleanup()
    assert not recording.path.exists()


@pytest.mark.asyncio
async def test_recorder_stops_accepting_bytes_at_limit(tmp_path: Path) -> None:
    sounddevice = FakeSoundDevice()
    recorder = SoundDeviceRecorder(
        device=2,
        sample_rate=8_000,
        max_seconds=0.001,
        temp_directory=tmp_path,
        sounddevice_module=sounddevice,
    )

    await recorder.start()
    assert sounddevice.stream is not None
    sounddevice.stream.callback(b"\x01\x00" * 100, 100, None, None)
    await asyncio.wait_for(recorder.wait_until_limit(), timeout=0.1)
    recording = await recorder.stop()

    assert recording.byte_count == 16
    assert recording.reached_limit
    recording.cleanup()


@pytest.mark.asyncio
async def test_cancel_removes_request_file(tmp_path: Path) -> None:
    sounddevice = FakeSoundDevice()
    recorder = SoundDeviceRecorder(
        device=None,
        sample_rate=16_000,
        max_seconds=1,
        temp_directory=tmp_path,
        sounddevice_module=sounddevice,
    )

    await recorder.start()
    created = list(tmp_path.glob("*.wav"))
    await recorder.cancel()

    assert len(created) == 1
    assert not created[0].exists()
    assert sounddevice.stream is not None and sounddevice.stream.closed


def test_empty_pcm_metrics_are_zero() -> None:
    assert _pcm_metrics(b"") == (0.0, 0)
