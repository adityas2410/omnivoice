"""Interactive slash-command console."""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from omnivoice.interaction import InteractionController


HELP = """Commands:
  /help          Show this help
  /status        Show runtime and self-test state
  /selftest arm  Permit one guarded test insertion for 30 seconds
  /cancel        Cancel the active request
  /quit          Shut down OmniVoice
"""


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
    ) -> None:
        """Read and dispatch slash commands until the user requests shutdown."""

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
                if line == "/help":
                    print(HELP, end="")
                elif line == "/status":
                    self.status(f"{runtime_status()}, {controller.describe_status()}")
                elif line == "/selftest arm":
                    controller.arm_self_test()
                elif line == "/cancel":
                    controller.cancel()
                elif line == "/quit":
                    return
                elif line.startswith("/selftest"):
                    self.status("Usage: /selftest arm")
                elif line.startswith("/"):
                    self.status(f"Unknown command: {line}. Use /help.")
                else:
                    self.status("Only slash commands are accepted. Use /help.")
