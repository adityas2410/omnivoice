# OmniVoice

OmniVoice is a Windows-first Python CLI for guarded keyboard automation. It combines a concurrent terminal, a global push-to-talk hotkey, focused editable-control validation, focus-change cancellation, and deliberately armed test typing.

## Safety model

OmniVoice binds every request to the supported editable control focused when the push-to-talk hotkey is released.

```text
Hotkey released
→ focused control validated with Windows UI Automation
→ request bound to an opaque focus identity
→ simulated processing delay
→ focus revalidated
→ armed test marker typed with SendInput
```

If the focused control changes, the remaining request is cancelled. OmniVoice never restores focus and never attempts an automatic rollback.

Windows synthetic keyboard input is not transactional. There is an unavoidable, very small race between checking focus and Windows dispatching an input event. OmniVoice reduces this risk by monitoring UI Automation focus events and revalidating between logical characters.

## Supported targets

OmniVoice accepts only controls that Windows UI Automation identifies unambiguously as:

- Currently focused, enabled, visible, and keyboard-focusable
- A standard `Edit`, editable `ComboBox`, or writable `Document` control
- Writable through the pattern required for that control type: `ValuePattern` for editable combo boxes and `TextPattern` read-only metadata for document surfaces
- Not a password field

Selection-only combo boxes and read-only documents are rejected. Rich editors, VS Code editor surfaces, and browser `contenteditable` regions depend on whether their UI Automation provider exposes a supported focused control and unambiguously reports it as writable.

OmniVoice normally cannot inject input into an application running at a higher Windows integrity level. Run the target and OmniVoice at the same privilege level; do not elevate OmniVoice merely to bypass this protection.

## Development setup

Requirements:

- Windows 11
- CPython 3.13.5

Create and activate a project environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Run the CLI:

```powershell
omnivoice
```

You can also run it as a module:

```powershell
python -m omnivoice
```

## Guarded self-test

Real keyboard input is disabled unless the one-shot self-test is explicitly armed.

1. Start OmniVoice in a terminal.
2. Enter `/selftest arm`.
3. Within 30 seconds, focus a supported empty text field.
4. Hold and release `Ctrl+Alt+Space`.
5. Keep focus on that field for the two-second simulated processing delay.

If validation succeeds and focus stays unchanged, OmniVoice types:

```text
[OmniVoice safety test]
```

The arm is consumed by the attempt whether it succeeds or fails. Without arming, the hotkey performs validation only and never types.

To test focus protection, arm the self-test, release the hotkey over a supported field, and switch to another field during the two-second delay. OmniVoice must cancel without typing.

## Terminal commands

```text
/help          Show command help
/status        Show the current request, configuration, and arming state
/selftest arm  Permit one guarded test insertion for 30 seconds
/cancel        Cancel the active request
/quit          Shut down and unregister Windows handlers
```

The terminal uses `prompt_toolkit`, so background hotkey and request statuses are rendered without discarding an unfinished command line.

## Configuration

Pass a file explicitly:

```powershell
omnivoice --config C:\path\to\config.yaml
```

Without `--config`, OmniVoice reads `%APPDATA%\OmniVoice\config.yaml`. If that file is absent, it uses built-in defaults and does not create a file. See `config.example.yaml`:

```yaml
hotkey:
  push_to_talk: "ctrl+alt+space"
```

Supported hotkeys contain zero or more of `ctrl`, `alt`, `shift`, and `win`, plus exactly one letter, digit, function key, or supported named key. F12 is rejected because Windows reserves it for debugging.

## Testing

The default suite uses fake focus and input backends and never sends global keyboard input:

```powershell
pytest
```

Desktop integration tests are opt-in because they register a real global hotkey and initialize desktop UI Automation:

```powershell
$env:OMNIVOICE_WINDOWS_INTEGRATION = "1"
pytest -m windows_integration
```

Operational logs contain state changes, timings, process IDs, window handles, control types, and error categories. They do not contain focused text or generated keystroke contents.
