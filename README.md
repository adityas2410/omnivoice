# OmniVoice

OmniVoice is a Windows-first, local voice-dictation CLI. It records English speech while a global push-to-talk hotkey is held, transcribes it with `whisper.cpp`, and types the literal transcript only if the original editable field still owns focus. Windows SAPI provides short status confirmations such as “Done.”

## Safety model

Every request is bound before recording begins:

```text
Press hotkey
→ validate and bind the focused control with Windows UI Automation
→ hear a short ready beep
→ record while the hotkey remains held
→ transcribe locally after release
→ revalidate focus and released modifiers
→ type through guarded SendInput
```

Changing fields or windows cancels the remaining request. OmniVoice never restores focus and never attempts an automatic rollback. During insertion it checks the focus lease between logical characters and emits UTF-16 surrogate pairs atomically.

Windows synthetic keyboard input is not transactional, so a very small race remains between the final focus check and Windows dispatching an input event. UI Automation focus monitoring and repeated identity checks narrow that boundary.

## Supported targets

The focused control must be enabled, visible, keyboard-focusable, non-password, and unambiguously writable. Supported UI Automation surfaces include standard `Edit` controls, editable `ComboBox` controls with a writable `ValuePattern`, and writable `Document` controls with reliable `TextPattern` metadata.

Selection-only combo boxes and read-only controls are rejected. Rich editors, VS Code editor surfaces, and browser `contenteditable` regions depend on the evidence exposed by their UI Automation provider.

OmniVoice normally cannot inject input into an application running at a higher Windows integrity level. Keep OmniVoice and its target at the same privilege level; do not elevate OmniVoice merely to bypass this protection.

## Installation

Requirements:

- Windows 11
- CPython 3.13.5
- Approximately 550 MB of free disk space for local speech assets

Create and activate a project environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Download and verify the pinned `whisper.cpp` `b5130` x64 BLAS package and English `small.en` model:

```powershell
omnivoice speech setup
```

The files are installed under `%LOCALAPPDATA%\OmniVoice\speech`. Startup never downloads them. Re-run the verified installation with:

```powershell
omnivoice speech setup --force
```

List PortAudio microphone identifiers and names when selecting a non-default input:

```powershell
omnivoice speech devices
```

## Dictation

Start the CLI:

```powershell
omnivoice
```

Then:

1. Focus a supported text field.
2. Press and hold `Ctrl+Alt+Space`.
3. Wait for the short ready beep, then speak.
4. Release `Space` to stop recording while releasing `Ctrl` and `Alt` normally.
5. Keep focus on the same field during local transcription and insertion.

Recording stops accepting audio after 30 seconds and waits for hotkey release before transcription. Silent or empty audio is discarded. A transcript containing a NUL character, no text, or more than 2,000 characters is rejected.

On the documented CPU-only baseline, local transcription can take time after the hotkey is released. `/cancel` remains available throughout recording, transcription, and typing.

## Guarded self-test

The fixed marker test bypasses the microphone and STT provider while exercising the same focus lease and guarded keyboard path:

1. Enter `/selftest arm`.
2. Within 30 seconds, focus a supported empty field.
3. Hold `Ctrl+Alt+Space` until OmniVoice confirms that the target is bound, then release it.
4. Keep focus unchanged for the two-second diagnostic delay.

The test types:

```text
[OmniVoice safety test]
```

Authorization is one-shot and is consumed by the attempt, including a rejected or cancelled attempt.

## Terminal commands

```text
/help          Show command help
/status        Show request, provider, voice, microphone, and self-test state
/models        Show agent models configured in YAML
/model NAME    Select an agent model for this session
/selftest arm  Permit one guarded fixed-marker insertion for 30 seconds
/cancel        Cancel recording, transcription, processing, or typing
/quit          Shut down and unregister all workers and Windows handlers
```

`prompt_toolkit` preserves unfinished terminal input while background statuses are printed.

## Configuration

On first launch, OmniVoice automatically creates
`%APPDATA%\OmniVoice\config.yaml` with every setting and prints that path during
startup. This per-user file is the normal place to add or remove named model
profiles for both packaged and source installations. Its location is always
available through:

```powershell
omnivoice config paths
```

The generated file looks like:

```yaml
agent:
  default_model: null
  models: {}

hotkey:
  push_to_talk: "ctrl+alt+space"

speech:
  stt:
    enabled: true
    provider: "whisper_cpp"
    model: "small.en"
    language: "en"
    timeout_seconds: 60
    threads: null
    executable_path: null
    model_path: null
  tts:
    enabled: true
    provider: "windows_sapi"
    voice: "Microsoft Zira Desktop"
    rate: 0
    volume: 100
  microphone:
    device: null
  recording:
    max_seconds: 30
    sample_rate: 16000
    minimum_seconds: 0.15
    silence_rms_threshold: 80
```

Fresh installations intentionally contain no model assumptions, so `/models`
reports no configured models. Add named profiles and choose one default, for
example:

```yaml
agent:
  default_model: "groq-fast"
  models:
    groq-fast: "groq:openai/gpt-oss-20b"
    ollama-local: "ollama:qwen3:8b"
```

Agent models use named profiles so `/model NAME` can change the active model for
the current process. `default_model` is restored whenever OmniVoice starts and
must name an entry in `models`; runtime switching never rewrites the YAML file.
`/models` only displays configured profiles and does not contact Groq or Ollama.
OmniVoice also creates an instruction-only `%APPDATA%\OmniVoice\.env` and prints
that path during startup. Model selectors determine credential lookup:
`groq:...` reads `GROQ_API_KEY`, while local `ollama:...` uses its
OpenAI-compatible endpoint at `http://localhost:11434/v1` and needs no key. Add
only the provider keys you use. A `.env` in the current working directory is
also loaded for source-development workflows, and existing process environment
variables take precedence.
An empty `agent` configuration remains valid while AI actions are unavailable.

Pass `--config C:\path\to\config.yaml` only when intentionally using a
different file.

`threads: null` chooses a bounded value from the available CPU count. `device: null` uses the Windows default input. A device may instead be a numeric identifier or exact name from `omnivoice speech devices`. `executable_path` and `model_path` override the managed assets.

STT and TTS can be disabled independently. With STT disabled or unavailable, the CLI and guarded self-test still run. If the configured SAPI voice is missing, OmniVoice warns and uses the Windows default voice. TTS failures never change a successful keyboard outcome.

Supported hotkeys contain zero or more of `ctrl`, `alt`, `shift`, and `win`, plus exactly one letter, digit, function key, or supported named key. F12 is rejected because Windows reserves it for debugging.

## Privacy and logs

Audio stays local and is sent only to the configured local `whisper.cpp` process. Each request uses a temporary WAV and transcript output; both are deleted after success, cancellation, timeout, or failure. No speech API is contacted.

Operational logs contain provider and model names, timings, state changes, byte and character counts, control metadata, and error categories. They do not contain audio, transcript text, spoken status content, focused-control contents, generated files, subprocess output, or temporary filenames.

## Testing

The default suite uses fake microphone, STT, TTS, focus, and keyboard backends. It does not record audio, play speech, contact the network, or type globally:

```powershell
python -m pytest
```

Desktop integration checks are opt-in:

```powershell
$env:OMNIVOICE_WINDOWS_INTEGRATION = "1"
python -m pytest -m windows_integration
Remove-Item Env:OMNIVOICE_WINDOWS_INTEGRATION
```
