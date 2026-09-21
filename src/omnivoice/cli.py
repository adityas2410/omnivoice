"""Interactive slash-command console."""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from omnivoice.interaction import InteractionController, RequestState
from omnivoice.models import ModelRegistry, ModelSelectionError


HELP = """Commands:
  /help          Show this help
  /status        Show hotkeys, model, request, speech, microphone, and self-test state
  /models        Show configured agent models
  /model NAME    Select an agent model for this session
  /context       Show active-window context state
  /context on    Enable UI context for this session
  /context off   Disable UI context for this session
  /selftest arm  Permit one guarded dictation-hotkey test insertion for 30 seconds
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
    ) -> None:
        self._controller = controller
        self._runtime_status = runtime_status
        self._models = models
        self._status = status
        self._write = write

    def dispatch(self, line: str) -> bool:
        """Handle one normalized line; return false when the console should exit."""

        if line == "/help":
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
        elif line.startswith("/context"):
            self._status("Usage: /context [on|off]")
        elif line == "/selftest arm":
            self._controller.arm_self_test()
        elif line == "/cancel":
            self._controller.cancel()
        elif line == "/quit":
            return False
        elif line.startswith("/selftest"):
            self._status("Usage: /selftest arm")
        elif line.startswith("/"):
            self._status(f"Unknown command: {line}. Use /help.")
        else:
            self._status("Only slash commands are accepted. Use /help.")
        return True

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
    ) -> None:
        """Read and dispatch slash commands until the user requests shutdown."""

        dispatcher = CommandDispatcher(
            controller,
            runtime_status,
            models,
            self.status,
            lambda text: print(text, end=""),
        )
        self.status("Ready. Use /help for commands.")
        with patch_stdout():
            while True:
                try:
                    line = (await self._session.prompt_async("omnivoice> ")).strip()
                except EOFError:
                    return
                except KeyboardInterrupt:
                    # Ctrl+C cancels work but keeps the long-running CLI alive.
                    controller.cancel("Cancellation requested from the terminal.")
                    continue

                if not line:
                    continue
                if not dispatcher.dispatch(line):
                    return
