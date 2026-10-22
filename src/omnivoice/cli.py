"""Interactive slash-command console."""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from omnivoice.interaction import InteractionController, RequestState
from omnivoice.models import ModelRegistry, ModelSelectionError
from omnivoice.history import ChatHistory, HistoryError, safe_text
from omnivoice.chat_ui import SessionPicker, replay


HELP = """Commands:
  /chats         List saved chats
  /chat new [NAME]  Create and select a chat
  /chat open [ID]   Pick or resume a chat and show its full conversation
  /chat rename NAME  Rename the active chat
  /chat inspect [ID]  Show a full conversation without switching
  /chat delete ID    Delete a chat and its history
  /chat retry        Retry unavailable chat storage
  /help          Show this help
  /status        Show hotkeys, model, request, speech, microphone, and context state
  /models        Show configured agent models
  /model NAME    Select an agent model for this session
  /context       Show active-window context state
  /context on    Enable UI context for this session
  /context off   Disable UI context for this session
  /context inspect  Show the last captured context as JSON
  /context clear    Forget the last captured context
  /cancel        Cancel the active request
  /quit          Shut down OmniVoice
"""


class CommandDispatcher:
    """Dispatch slash commands independently of the interactive prompt."""

    def __init__(
        self,
        controller: InteractionController,
        runtime_status: Callable[[], str],
        models: ModelRegistry,
        status: Callable[[str], None],
        write: Callable[[str], None],
        history: ChatHistory | None = None,
    ) -> None:
        self._controller = controller
        self._runtime_status = runtime_status
        self._models = models
        self._status = status
        self._write = write
        self._history = history

    async def dispatch(self, line: str) -> bool:
        """Handle one normalized line; return false when the console should exit."""

        if line == "/chats" or line == "/chat" or line.startswith("/chat "):
            await self._chat(line)
        elif line == "/help":
            self._write(HELP)
        elif line == "/status":
            self._status(
                f"{self._runtime_status()}, {self._models.describe_status()}, "
                f"{self._controller.describe_status()}"
            )
        elif line == "/models":
            self._show_models()
        elif line.startswith("/models"):
            self._status("Usage: /models")
        elif line == "/model" or line.startswith("/model "):
            self._select_model(line)
        elif line == "/context":
            state = "enabled" if self._controller.context_enabled else "disabled"
            self._status(f"UI context is {state} for this session.")
        elif line in {"/context on", "/context off"}:
            self._controller.set_context_enabled(line.endswith(" on"))
        elif line == "/context inspect":
            self._inspect_context()
        elif line == "/context clear":
            self._controller.clear_context_inspection()
            self._status("Last captured UI context cleared.")
        elif line.startswith("/context"):
            self._status("Usage: /context [on|off|inspect|clear]")
        elif line == "/cancel":
            self._controller.cancel()
        elif line == "/quit":
            return False
        elif line.startswith("/"):
            self._status(f"Unknown command: {line}. Use /help.")
        else:
            self._status("Only slash commands are accepted. Use /help.")
        return True

    async def _chat(self, line: str) -> None:
        history = self._history
        if history is None:
            self._status("Chat storage is unavailable.")
            return
        listing = line == "/chats"
        if not listing and self._controller.state is not RequestState.IDLE:
            self._status(f"Busy ({self._controller.state.value}); chat was not changed.")
            return
        if history.busy:
            self._status("Chat selection or replay already in progress.")
            return
        if not listing:
            history.busy = True
        try:
            if listing:
                for session in await history.list_sessions():
                    marker = " *" if history.active and session.id == history.active.id else ""
                    self._write(f"{session.id[:8]}  {safe_text(session.name, multiline=False)}{marker}  {session.updated_at}  {safe_text(session.preview, multiline=False)}\n")
                return
            _, _, args = line.partition(" ")
            command, _, argument = args.partition(" ")
            argument = argument.strip()
            if command == "retry" and not argument:
                await history.retry()
                self._status("Chat storage ready. Pending outcomes saved; no actions replayed.")
                return
            if not history.healthy:
                self._status("Chat storage unavailable; use /chat retry.")
                return
            if command == "new":
                session = await history.create(argument or "New chat")
            elif command == "open":
                ident = argument or await SessionPicker(await history.list_sessions()).run()
                if ident is None:
                    return
                session = await history.activate(ident)
            elif command == "rename" and argument:
                await history.rename(argument)
                self._status("Chat renamed.")
                return
            elif command == "delete" and argument:
                if not await history.delete(argument):
                    self._status("Chat deleted.")
                    return
                session = history.active
            elif command == "inspect":
                session = await history.resolve(argument) if argument else history.active
            else:
                self._status("Usage: /chats or /chat new [NAME]|open [ID]|rename NAME|inspect [ID]|delete ID|retry")
                return
            if session is not None:
                await replay(history, session, self._write)
        except HistoryError as exc:
            self._status(str(exc))
        finally:
            if not listing:
                history.busy = False

    def _inspect_context(self) -> None:
        if self._controller.state is not RequestState.IDLE:
            self._status(
                f"Busy ({self._controller.state.value}); UI context inspection is unavailable."
            )
            return
        snapshot = self._controller.last_context_inspection
        if snapshot is None:
            self._status("No captured UI context is available for inspection.")
            return
        self._write(
            "Last captured UI context (session memory; captured before model-budget trimming):\n"
            f"{snapshot}\n"
        )

    def _show_models(self) -> None:
        configured = self._models.configured
        if not configured:
            self._status("No agent models are configured.")
            return
        lines = ["Configured agent models:"]
        for selection in configured:
            labels = []
            if selection.alias == self._models.current_alias:
                labels.append("current")
            if selection.alias == self._models.default_alias:
                labels.append("default")
            marker = f" [{', '.join(labels)}]" if labels else ""
            lines.append(f"  {selection.alias}: {selection.selector}{marker}")
        self._status("\n".join(lines))

    def _select_model(self, line: str) -> None:
        parts = line.split()
        if len(parts) != 2:
            self._status("Usage: /model NAME")
            return
        if self._controller.state is not RequestState.IDLE:
            self._status(
                f"Busy ({self._controller.state.value}); agent model was not changed."
            )
            return
        alias = parts[1]
        try:
            selection, changed = self._models.select(alias)
        except ModelSelectionError:
            available = ", ".join(item.alias for item in self._models.configured)
            suffix = available if available else "none configured"
            self._status(f"Unknown agent model {alias!r}. Available models: {suffix}.")
            return
        if changed:
            self._status(
                f"Agent model switched to {selection.alias} ({selection.selector})."
            )
        else:
            self._status(
                f"Agent model already active: {selection.alias} ({selection.selector})."
            )


class Console:
    """Render background status safely while preserving unfinished input."""

    def __init__(self) -> None:
        self._session: PromptSession[str] = PromptSession()

    def status(self, message: str) -> None:
        """Write a user-facing runtime message above the active prompt."""

        print(f"[omnivoice] {message}", flush=True)

    async def run(
        self,
        controller: InteractionController,
        runtime_status: Callable[[], str],
        models: ModelRegistry,
        history: ChatHistory | None = None,
    ) -> None:
        """Read and dispatch slash commands until the user requests shutdown."""

        dispatcher = CommandDispatcher(
            controller,
            runtime_status,
            models,
            self.status,
            lambda text: print(text, end=""),
            history,
        )
        self.status("Ready. Use /help for commands.")
        with patch_stdout():
            if history is not None and history.healthy and history.active is not None:
                history.busy = True
                try:
                    await replay(history, history.active, lambda text: print(text, end=""))
                except HistoryError as exc:
                    self.status(str(exc))
                finally:
                    history.busy = False
            while True:
                try:
                    label = safe_text(history.active.name, multiline=False) if history and history.active else "no chat"
                    line = (await self._session.prompt_async(f"omnivoice [{label}]> ")).strip()
                except EOFError:
                    return
                except KeyboardInterrupt:
                    # Ctrl+C cancels work but keeps the long-running CLI alive.
                    controller.cancel("Cancellation requested from the terminal.")
                    continue

                if not line:
                    continue
                if not await dispatcher.dispatch(line):
                    return
