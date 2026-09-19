# OmniVoice

OmniVoice is a Windows-first voice-dictation and constrained AI-action CLI. It records English speech while a global push-to-talk hotkey is held and transcribes it locally with `whisper.cpp`. The dictation hotkey types the literal transcript, while the separate agent hotkey asks a configured Groq or local Ollama model for one validated keyboard-action plan. Both paths operate only while the original editable field still owns focus. Windows SAPI provides short fixed confirmations such as “Done.”

## Safety model

Every request is bound before recording begins:

```text
Press hotkey
→ validate and bind the focused control with Windows UI Automation
→ hear a short ready beep
→ record while the hotkey remains held
→ transcribe locally after release
→ literal hotkey: type the transcript
→ agent hotkey: revalidate any captured selection, then generate one action plan
→ revalidate focus, selection when used, and released modifiers before guarded SendInput
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

## Constrained AI actions

Focus a supported field, hold `Ctrl+Alt+Shift+Space`, speak a request, and release
`Space`. OmniVoice transcribes locally, snapshots the session's selected model,
and starts one structured model run, with at most one schema-correction retry.
This path has no conversation history, model tools, screen access, or autonomous
execution loop.

The initial action vocabulary is deliberately small:

- Insert at most 2,000 printable Unicode characters at the caret.
- Replace one explicitly selected range with at most 2,000 generated characters.
- Send exactly `Enter`, `Ctrl+S`, or `Ctrl+Z`.
- Execute at most five actions in one plan.

The model receives the permitted chord list and uses its own Windows knowledge to
interpret requests such as “put the second sentence on a new line”; the prompt
does not contain a mapping for every phrase. OmniVoice validates the complete returned plan before its first
action. Model-produced CR/LF line breaks are converted into separately guarded
`Enter` actions. Unknown actions, extra fields, other control characters, and any shortcut not in
the code-owned allowlist stop the entire plan. Focus is checked before every
action and between inserted characters. Execution stops without guessed rollback
if focus changes, cancellation occurs, or Windows accepts only part of an input.
The parsed model output is printed as single-line JSON before execution; control
characters are escaped so the exact proposed structure remains visible.

### Transform selected text

To rewrite, summarize, correct, or translate existing text, highlight one contiguous
range before pressing the agent hotkey. OmniVoice reads at most 4,000 selected
characters through Windows UI Automation, keeps the captured range on its UIA
worker, and checks the exact range and contents again before model use and before
typing. The model receives the spoken request and selected text as separate JSON
fields and may return:

```json
{"actions":[{"type":"replace_selection","text":"Rewritten text"}]}
```

The selected text is treated as source material, never as instructions. It is sent
only to the selected model and is not written to logs. Multiline replacements use
guarded Enter presses. Multiple selections, selections above 4,000 characters,
changed selections, and implicit `insert_text` or `Enter` actions over a selection
stop without keyboard input. `Ctrl+S` and `Ctrl+Z` remain available without
consuming the selection.

Controls that expose only `ValuePattern` continue to support ordinary caret
insertion but cannot use selection-aware transformation. OmniVoice does not use
the clipboard, navigate with arrow keys, delete selections, or replace whole fields.

Groq requires `GROQ_API_KEY`. Local Ollama uses
`http://localhost:11434/v1` without a key, but Ollama must be running and the
selected model must already be installed. Models are never downloaded or probed
by startup, `/models`, or `/model`.

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
/status        Show both hotkeys, model, request, speech, microphone, and self-test state
/models        Show agent models configured in YAML
/model NAME    Select an agent model for this session
/selftest arm  Permit one guarded dictation-hotkey insertion for 30 seconds
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
  agent_push_to_talk: "ctrl+alt+shift+space"

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
Selecting a configured profile changes only the current process. Provider
credentials and model availability are checked only when the agent hotkey uses
that profile. A configured local Ollama profile may therefore be listed and
selected even when its model is not installed; the request then fails without
sending keyboard input.

Pass `--config C:\path\to\config.yaml` only when intentionally using a
different file.

`threads: null` chooses a bounded value from the available CPU count. `device: null` uses the Windows default input. A device may instead be a numeric identifier or exact name from `omnivoice speech devices`. `executable_path` and `model_path` override the managed assets.

STT and TTS can be disabled independently. With STT disabled or unavailable, the CLI and guarded self-test still run. If the configured SAPI voice is missing, OmniVoice warns and uses the Windows default voice. TTS failures never change a successful keyboard outcome.

Supported hotkeys contain zero or more of `ctrl`, `alt`, `shift`, and `win`, plus exactly one letter, digit, function key, or supported named key. F12 is rejected because Windows reserves it for debugging. Dictation and agent chords must be different, including when their modifiers are written in a different order.

## Privacy and logs

Audio stays local and is sent only to the configured local `whisper.cpp` process. Each request uses a temporary WAV and transcript output; both are deleted after success, cancellation, timeout, or failure. Literal dictation contacts no LLM. Only the transcript produced by the agent hotkey is sent to its snapshotted Groq or local Ollama model.

Operational logs contain provider and model names, timings, state changes, byte and character counts, control metadata, and error categories. They do not contain credentials, audio, transcript text, prompts, model responses, generated text, spoken status content, focused-control contents, subprocess output, or temporary filenames.

## Testing

The default suite uses fake microphone, STT, TTS, model, focus, and keyboard backends. It explicitly disables real Pydantic AI model requests and does not record audio, play speech, contact the network, consume provider quota, or type globally:

```powershell
python -m pytest
```

Desktop integration checks are opt-in:

```powershell
$env:OMNIVOICE_WINDOWS_INTEGRATION = "1"
python -m pytest -m windows_integration
Remove-Item Env:OMNIVOICE_WINDOWS_INTEGRATION
```
