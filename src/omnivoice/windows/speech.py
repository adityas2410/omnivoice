"""Windows status speech and ready-earcon adapters."""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from omnivoice.speech.ports import Readiness, SpeechError


LOGGER = logging.getLogger(__name__)
SVS_FLAGS_ASYNC = 1
SVS_PURGE_BEFORE_SPEAK = 2


class SapiBackend(Protocol):
    """Keep COM mechanics replaceable by a silent test backend."""

    def initialize(self, voice_name: str | None, rate: int, volume: int) -> tuple[str, bool]: ...

    def speak(self, text: str) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class _ComtypesSapiBackend:
    """Own every SAPI COM object on the worker thread that created it."""

    def __init__(self) -> None:
        self._voice: object | None = None
        self._comtypes: object | None = None

    def initialize(self, voice_name: str | None, rate: int, volume: int) -> tuple[str, bool]:
        comtypes_was_loaded = "comtypes" in sys.modules
        # comtypes initializes COM during its first import on a thread. Set the
        # mode first; if another worker imported it earlier, initialize this
        # thread explicitly instead.
        sys.coinit_flags = 0  # COINIT_MULTITHREADED
        import comtypes
        import comtypes.client

        if comtypes_was_loaded:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        self._comtypes = comtypes
        voice = comtypes.client.CreateObject("SAPI.SpVoice", dynamic=True)
        selected = False
        actual_name = "Windows default voice"
        if voice_name:
            voices = voice.GetVoices()
            for index in range(int(voices.Count)):
                token = voices.Item(index)
                description = str(token.GetDescription())
                normalized_description = description.casefold()
                normalized_requested = voice_name.casefold()
                # Some SAPI installations append locale text to the friendly
                # name returned by PowerShell, so accept that descriptive suffix.
                if (
                    normalized_description == normalized_requested
                    or normalized_description.startswith(f"{normalized_requested} -")
                ):
                    voice.Voice = token
                    actual_name = description
                    selected = True
                    break
        # Reading the default Voice token through late-bound SAPI can emit a
        # noisy COM "interface not registered" diagnostic on some Windows 11
        # systems. Leaving the selected default untouched is sufficient here.
        voice.Rate = rate
        voice.Volume = volume
        self._voice = voice
        return actual_name, selected or voice_name is None

    def speak(self, text: str) -> None:
        if self._voice is not None:
            # Purging makes a new status replace stale speech from the prior state.
            self._voice.Speak(text, SVS_FLAGS_ASYNC | SVS_PURGE_BEFORE_SPEAK)

    def stop(self) -> None:
        if self._voice is not None:
            # A synchronous empty purge makes stop() an actual barrier before
            # the ready beep and microphone capture begin.
            self._voice.Speak("", SVS_PURGE_BEFORE_SPEAK)

    def close(self) -> None:
        self.stop()
        self._voice = None
        if self._comtypes is not None:
            self._comtypes.CoUninitialize()
            self._comtypes = None


@dataclass(slots=True)
class _SpeechCommand:
    operation: str
    text: str | None = None
    done: threading.Event | None = None


class WindowsSapiTTS:
    """Queue best-effort fixed statuses to one COM-owning worker thread."""

    def __init__(
        self,
        *,
        voice_name: str | None,
        rate: int,
        volume: int,
        warning: Callable[[str], None],
        backend_factory: Callable[[], SapiBackend] = _ComtypesSapiBackend,
    ) -> None:
        self._configured_voice = voice_name
        self._rate = rate
        self._volume = volume
        self._warning = warning
        self._backend_factory = backend_factory
        self._commands: queue.Queue[_SpeechCommand] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._startup_error: BaseException | None = None
        self._ready = False
        self._voice_name: str | None = None

    @property
    def readiness(self) -> Readiness:
        if self._ready:
            return Readiness(True, self._voice_name or "Windows default voice")
        if self._startup_error is not None:
            return Readiness(False, "Windows speech is unavailable")
        return Readiness(False, "not started")

    @property
    def voice_name(self) -> str | None:
        return self._voice_name

    async def start(self) -> None:
        """Start the COM owner and surface initialization without blocking the loop."""

        if self._thread is not None:
            return
        self._started.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="omnivoice-sapi",
            daemon=True,
        )
        self._thread.start()
        ready = await asyncio.to_thread(self._started.wait, 5.0)
        if not ready:
            raise SpeechError("Windows status speech timed out during startup")
        if self._startup_error is not None:
            raise SpeechError("Windows status speech could not start") from self._startup_error

    async def speak(self, text: str) -> None:
        """Replace pending speech; callers must pass only fixed application text."""

        if self._ready:
            self._commands.put(_SpeechCommand("speak", text))

    async def stop(self) -> None:
        """Purge queued and currently speaking status audio."""

        if self._thread is not None and self._thread.is_alive():
            done = threading.Event()
            self._commands.put(_SpeechCommand("stop", done=done))
            completed = await asyncio.to_thread(done.wait, 2.0)
            if not completed:
                raise SpeechError("Windows status speech did not stop promptly")

    async def shutdown(self) -> None:
        """Purge speech and join the COM worker before process exit."""

        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            self._commands.put(_SpeechCommand("shutdown"))
            await asyncio.to_thread(thread.join, 5.0)
        if thread.is_alive():
            raise SpeechError("Windows status speech thread did not stop cleanly")
        self._thread = None
        self._ready = False

    def _run(self) -> None:
        backend: SapiBackend | None = None
        try:
            backend = self._backend_factory()
            actual_voice, exact_match = backend.initialize(
                self._configured_voice,
                self._rate,
                self._volume,
            )
            self._voice_name = actual_voice
            self._ready = True
            self._started.set()
            if self._configured_voice and not exact_match:
                self._warning(
                    f"TTS voice '{self._configured_voice}' was not found; "
                    f"using '{actual_voice}'."
                )
            while True:
                command = self._commands.get()
                try:
                    if command.operation == "shutdown":
                        return
                    if command.operation == "stop":
                        backend.stop()
                    elif command.text is not None:
                        backend.speak(command.text)
                finally:
                    if command.done is not None:
                        command.done.set()
        except BaseException as exc:
            self._startup_error = exc
            LOGGER.info("event=tts_unavailable error_type=%s", type(exc).__name__)
        finally:
            self._ready = False
            if backend is not None:
                try:
                    backend.close()
                except BaseException:
                    LOGGER.info("event=tts_close_failed category=sapi")
            self._started.set()


class DisabledTTS:
    """Represent intentionally disabled status speech without special casing callers."""

    readiness = Readiness(False, "disabled")
    voice_name: str | None = None

    async def start(self) -> None:
        return None

    async def speak(self, text: str) -> None:
        del text

    async def stop(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class WindowsReadyCue:
    """Play a short system beep before opening the microphone."""

    async def play(self) -> None:
        import winsound

        # Playback finishes before capture starts, preventing the cue from
        # becoming part of the user's transcription.
        await asyncio.to_thread(winsound.Beep, 880, 60)
