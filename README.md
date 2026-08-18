# OmniVoice

**Universal voice AI agent that turns spoken prompts into keyboard operations for web and desktop applications.**

Click into a text field, hold your push-to-talk hotkey, say what you need, and release. OmniVoice understands the request, performs the required keyboard workflow in the focused application, and speaks back with concise status updates.

OmniVoice is not voice dictation. Your speech is an instruction for an AI agent, not text to copy verbatim.

> “Write a blog about transformers and save the document.”

OmniVoice generates the blog, types it into the focused document, and sends `Ctrl+S`. If a Save As dialog appears, you choose the filename and location.

```text
Focused editable field
        ↓
Push-to-talk hotkey
        ↓
Speech prompt
        ↓
Speech-to-text
        ↓
Pydantic AI agent
        ↓
Keyboard workflow + spoken status
```

## How it works

```text
Click into the target field
        ↓
Hold push-to-talk and speak
        ↓
Release the hotkey
        ↓
OmniVoice validates the focused control
        ↓
The agent completes the keyboard workflow
        ↓
OmniVoice speaks the result
```

OmniVoice runs from the terminal while you work normally in Word, browsers, editors, chat applications, and other Windows software.

## Keyboard workflows

| Spoken prompt | Result |
| --- | --- |
| “Write a professional response to this email.” | Generates and types a response. |
| “Make the selected paragraph more concise.” | Rewrites and replaces the selected text. |
| “Go to the next field.” | Presses `Tab`. |
| “Save this.” | Presses `Ctrl+S`. |
| “Undo that.” | Presses `Ctrl+Z`. |

The Pydantic AI agent works through typed keyboard tools:

- Type generated text
- Press keys and shortcuts
- Hold and release modifiers
- Navigate with `Tab` and arrow keys
- Select, replace, delete, and confirm text

Each workflow follows a controlled sequence:

```text
Validate focus
→ retrieve permitted context when needed
→ plan keyboard actions
→ execute the action sequence
→ speak status
```

## Focus-aware editing

OmniVoice uses Windows UI Automation to inspect the currently focused control before it works with text. This confirms that the target is an editable, enabled field and keeps keyboard actions directed at the place you selected.

For existing-text edits, OmniVoice reads explicitly selected text as context and replaces that selection with the result. This keeps document, browser, and form workflows precise.

## Voice feedback

OmniVoice speaks concise feedback through your selected speakers or headphones:

- “Editable text field detected.”
- “I need clarification before making a change.”
- “The draft was inserted and the save command was sent.”

Speech input and speech output are independently pluggable. This keeps OmniVoice compatible with different STT and TTS providers while the Pydantic AI agent remains independent of any single model vendor.

## Configuration

Edit the repository-root [config.yaml](config.yaml) to select the agent model and push-to-talk shortcut:

```yaml
agent:
  model: <provider>:<model>

hotkey:
  push_to_talk: <your-hotkey>
```

The agent model uses Pydantic AI's `<provider>:<model>` format. Pydantic AI resolves the correct model integration, connection provider, and model profile automatically. Keyboard workflows use models with tool-calling support.

Keep provider credentials in environment variables or a local `.env` file. Do not place credentials in `config.yaml`.

## Conversation histories

OmniVoice keeps selectable conversation histories in local PostgreSQL storage. Each history preserves prompts, agent responses, and action records, giving the agent the right conversational context when you return to a task.

Raw microphone audio is not retained by default.

## Privacy and observability

- You choose the application and field that receives keyboard input.
- Microphone capture occurs only while push-to-talk is held.
- Credentials stay outside version control.
- Local observability records operational metadata such as timing, selected model, and action status.
- Logs exclude microphone audio, prompts, selected text, clipboard content, generated text, and typed keystrokes.
- Pydantic Logfire can be enabled for developer observability when desired.

## Architecture

```text
CLI runtime
├── global hotkey listener
├── speech boundary (STT and TTS)
├── Windows UI Automation focus validation
├── Pydantic AI agent and typed keyboard tools
├── keyboard adapter
├── local PostgreSQL conversation history
└── local metadata-only observability
```

## Repository layout

```text
.
├── README.md                 # Product documentation
├── LICENSE                   # MIT license
├── .gitignore                # Local secrets and Python artifacts
├── pyproject.toml            # Python project metadata and dependencies
├── config.yaml               # Model and hotkey settings
├── src/omnivoice/
│   ├── agent.py              # Pydantic AI agent loop
│   ├── voice.py              # STT and TTS boundary
│   ├── hotkey.py             # Global push-to-talk
│   ├── ui_automation.py      # Focus validation
│   ├── keyboard.py           # Keyboard execution
│   ├── history.py            # Conversation histories
│   ├── database.py           # PostgreSQL persistence
│   └── ...
└── tests/                    # Automated test suite
```

## License

OmniVoice is released under the [MIT License](LICENSE).
