# OmniVoice

**Universal voice AI agent that turns spoken prompts into keyboard operations for web and desktop applications.**

Click into a text field, hold your push-to-talk hotkey, say what you need, and release. OmniVoice understands the request, performs the required keyboard workflow in the focused application, and speaks back with concise status updates.

## Features

- **Push-to-talk voice control** for any focused application
- **Multi-step keyboard workflows** from one spoken instruction
- **Focus-aware field validation** before using model tokens or typing
- **Selected-text replacement** and full-field rewriting
- **Context-aware screenshots** when visual UI state is needed
- **Focus-change protection** that cancels requests when the target changes
- **Configurable AI models, STT, TTS, and push-to-talk hotkeys**
- **Spoken status feedback** for completion, errors, and clarification
- **Local PostgreSQL conversation histories**

```text
Focused editable field
        ↓
Push-to-talk hotkey
        ↓
Speech prompt
        ↓
Speech-to-text
        ↓
AI agent
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

## Example workflows

One prompt can produce a complete sequence of keyboard actions. OmniVoice plans the sequence, executes it in order, and then reports the result.

| Spoken prompt | Keyboard sequence |
| --- | --- |
| “Write a professional response to this email and save it.” | Generates the response → types it → presses `Ctrl+S`. |
| “Make the selected paragraph more concise and save the document.” | Reads the selection → replaces it with the revision → presses `Ctrl+S`. |
| “Go to the next field, enter my email address, then continue.” | Presses `Tab` → types the address → continues with the next keyboard action. |
| “Undo that, rewrite the sentence professionally, and save.” | Presses `Ctrl+Z` → types the revision → presses `Ctrl+S`. |

The AI agent works through typed keyboard tools:

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

### Visual context

The AI agent can take a screenshot of the active application when visual context is needed to complete a keyboard workflow. It uses the screenshot to understand visible UI state, then chooses the next keyboard action.

Screenshots complement Windows UI Automation: accessibility data identifies focused controls and text state, while visual context explains what is currently visible around them. Keyboard input remains OmniVoice's execution mechanism.

### Focus-change protection

Each voice request is bound to the field that was focused when push-to-talk was released. If you click another field, button, window, or any other element while the AI agent is processing:

```text
Focus changes
→ request is cancelled
→ planned keyboard actions are discarded
→ no text is inserted anywhere
→ OmniVoice says: “Focus changed. Request cancelled.”
```

OmniVoice never moves focus back or sends the result to the newly focused element. To continue, focus the desired field and give the prompt again.

## Voice feedback

OmniVoice speaks concise feedback through your selected speakers or headphones:

- “Editable text field detected.”
- “I need clarification before making a change.”
- “The draft was inserted and the save command was sent.”

Speech input and speech output are independently pluggable. This keeps OmniVoice compatible with different STT and TTS providers while the AI agent remains independent of any single model vendor.

## Configuration

Edit the repository-root [config.yaml](config.yaml) to select the agent model, speech models, and push-to-talk shortcut.

```yaml
agent:
  model: <provider>:<model>

speech:
  stt: <provider>:<model>
  tts: <provider>:<model>

hotkey:
  push_to_talk: <key-combination>
```

For example:

```yaml
agent:
  model: "openai:gpt-5.2"

speech:
  stt: "elevenlabs:scribe_v2"
  tts: "elevenlabs:eleven_flash_v2_5"

hotkey:
  push_to_talk: "ctrl+alt+space"
```

The agent model uses the `<provider>:<model>` format. Keyboard workflows use models with tool-calling support, and screenshot context uses models with image-input support. STT and TTS use the same provider-and-model naming pattern, so they can be selected independently.

Hotkey values use lowercase key names joined with `+`. Examples include `ctrl+alt+space`, `ctrl+shift+space`, and `f8`.

Keep provider credentials in environment variables or a local `.env` file. Do not place credentials in `config.yaml`.

## Conversation histories

OmniVoice keeps selectable conversation histories in local PostgreSQL storage. Each history preserves prompts, agent responses, and action records, giving the agent the right conversational context when you return to a task.

Raw microphone audio is not retained by default.

## Privacy and observability

- You choose the application and field that receives keyboard input.
- Microphone capture occurs only while push-to-talk is held.
- Credentials stay outside version control.
- Local observability records operational metadata such as timing, selected model, and action status.
- Screenshots are captured only when the AI agent requests visual context for the active application.
- Logs exclude microphone audio, prompts, selected text, clipboard content, generated text, and typed keystrokes.
- Pydantic Logfire can be enabled for developer observability when desired.

## Architecture

```text
CLI runtime
├── global hotkey listener
├── speech boundary (STT and TTS)
├── Windows UI Automation focus validation
├── active-application screenshot context
├── AI agent and typed keyboard tools
├── keyboard adapter
├── local PostgreSQL conversation history
└── local metadata-only observability
```

## Technology stack

- **Python** — application runtime
- **Pydantic AI** — model-agnostic AI-agent framework and typed tool orchestration
- **PostgreSQL** — local conversation-history storage
- **Windows UI Automation** — focused editable-field validation
- **Screenshot context** — visual UI-state awareness for keyboard workflows
- **Pluggable STT and TTS providers** — speech input and spoken status feedback

## Repository layout

```text
.
├── README.md                 # Product documentation
├── LICENSE                   # Project license
├── .gitignore                # Local secrets and Python artifacts
├── pyproject.toml            # Python project metadata and dependencies
├── config.yaml               # Model and hotkey settings
├── src/omnivoice/
│   ├── agent.py              # AI agent loop
│   ├── voice.py              # STT and TTS boundary
│   ├── hotkey.py             # Global push-to-talk
│   ├── ui_automation.py      # Focus validation
│   ├── keyboard.py           # Keyboard execution
│   ├── history.py            # Conversation histories
│   ├── database.py           # PostgreSQL persistence
│   ├── app.py                # Application lifecycle
│   ├── cli.py                # Terminal commands
│   ├── config.py             # Runtime configuration
│   ├── tools.py              # Typed agent tools
│   └── observability.py      # Local operational logging
└── tests/                    # Automated test suite
```
