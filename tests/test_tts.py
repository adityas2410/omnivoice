import asyncio

import pytest

from omnivoice.windows.speech import WindowsSapiTTS


class FakeSapiBackend:
    def __init__(self, *, exact: bool = True) -> None:
        self.exact = exact
        self.initialized: tuple[str | None, int, int] | None = None
        self.spoken: list[str] = []
        self.stops = 0
        self.closed = False

    def initialize(
        self, voice_name: str | None, rate: int, volume: int
    ) -> tuple[str, bool]:
        self.initialized = (voice_name, rate, volume)
        return (voice_name or "Default Voice") if self.exact else "Default Voice", self.exact

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stops += 1

    def close(self) -> None:
        self.closed = True


async def wait_for(predicate: object) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("worker did not process command")


@pytest.mark.asyncio
async def test_sapi_worker_applies_voice_rate_volume_and_shuts_down() -> None:
    backend = FakeSapiBackend()
    warnings: list[str] = []
    tts = WindowsSapiTTS(
        voice_name="Microsoft Zira Desktop",
        rate=2,
        volume=80,
        warning=warnings.append,
        backend_factory=lambda: backend,
    )

    await tts.start()
    await tts.speak("Done.")
    await wait_for(lambda: backend.spoken == ["Done."])
    await tts.stop()
    await wait_for(lambda: backend.stops == 1)
    await tts.shutdown()

    assert backend.initialized == ("Microsoft Zira Desktop", 2, 80)
    assert warnings == []
    assert backend.closed


@pytest.mark.asyncio
async def test_missing_configured_voice_warns_and_uses_default() -> None:
    backend = FakeSapiBackend(exact=False)
    warnings: list[str] = []
    tts = WindowsSapiTTS(
        voice_name="Missing Voice",
        rate=0,
        volume=100,
        warning=warnings.append,
        backend_factory=lambda: backend,
    )

    await tts.start()
    assert tts.voice_name == "Default Voice"
    assert tts.readiness.ready
    assert len(warnings) == 1
    await tts.shutdown()
