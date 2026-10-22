from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.asyncio

from omnivoice.cli import HELP, CommandDispatcher
from omnivoice.config import AgentConfig
from omnivoice.interaction import RequestState
from omnivoice.models import ModelRegistry


@dataclass
class FakeController:
    state: RequestState = RequestState.IDLE
    cancelled: bool = False
    context_enabled: bool = False
    last_context_inspection: str | None = None

    def describe_status(self) -> str:
        return f"state={self.state.value}"

    def cancel(self, reason: str = "Request cancelled.") -> None:
        self.cancelled = True

    def set_context_enabled(self, enabled: bool) -> bool:
        if self.state is not RequestState.IDLE:
            return False
        self.context_enabled = enabled
        return True

    def clear_context_inspection(self) -> None:
        self.last_context_inspection = None


def make_dispatcher(
    *, state: RequestState = RequestState.IDLE, configured: bool = True
) -> tuple[CommandDispatcher, ModelRegistry, list[str], list[str]]:
    config = (
        AgentConfig(
            default_model="groq-fast",
            models={
                "groq-fast": "groq:openai/gpt-oss-20b",
                "ollama-local": "ollama:qwen3:8b",
            },
        )
        if configured
        else AgentConfig()
    )
    models = ModelRegistry(config)
    statuses: list[str] = []
    writes: list[str] = []
    dispatcher = CommandDispatcher(
        FakeController(state=state),  # type: ignore[arg-type]
        lambda: "runtime=ready",
        models,
        statuses.append,
        writes.append,
    )
    return dispatcher, models, statuses, writes


async def test_help_lists_model_commands() -> None:
    dispatcher, _, _, writes = make_dispatcher()

    assert await dispatcher.dispatch("/help")

    assert writes == [HELP]
    assert "/models" in HELP
    assert "/model NAME" in HELP
    assert "/context on" in HELP
    assert "/context inspect" in HELP


async def test_context_commands_are_session_only_and_validate_usage() -> None:
    dispatcher, _, statuses, _ = make_dispatcher()

    await dispatcher.dispatch("/context")
    await dispatcher.dispatch("/context on")
    await dispatcher.dispatch("/context")
    await dispatcher.dispatch("/context maybe")

    assert statuses == [
        "UI context is disabled for this session.",
        "UI context is enabled for this session.",
        "Usage: /context [on|off|inspect|clear]",
    ]


async def test_context_inspection_is_explicit_and_clearable() -> None:
    dispatcher, _, statuses, writes = make_dispatcher()
    controller = dispatcher._controller

    await dispatcher.dispatch("/context inspect")
    controller.last_context_inspection = '{"document":"private"}'
    await dispatcher.dispatch("/context inspect")
    await dispatcher.dispatch("/context clear")
    await dispatcher.dispatch("/context inspect")

    assert writes == [
        "Last captured UI context (session memory; captured before model-budget trimming):\n"
        '{"document":"private"}\n'
    ]
    assert statuses == [
        "No captured UI context is available for inspection.",
        "Last captured UI context cleared.",
        "No captured UI context is available for inspection.",
    ]


async def test_context_inspection_is_rejected_while_busy() -> None:
    dispatcher, _, statuses, writes = make_dispatcher(state=RequestState.PROCESSING)

    await dispatcher.dispatch("/context inspect")

    assert statuses == ["Busy (processing); UI context inspection is unavailable."]
    assert writes == []


async def test_models_lists_current_and_default_without_provider_access() -> None:
    dispatcher, _, statuses, _ = make_dispatcher()

    await dispatcher.dispatch("/models")

    assert statuses == [
        "Configured agent models:\n"
        "  groq-fast: groq:openai/gpt-oss-20b [current, default]\n"
        "  ollama-local: ollama:qwen3:8b"
    ]


async def test_model_switch_updates_status_but_not_default() -> None:
    dispatcher, models, statuses, _ = make_dispatcher()

    await dispatcher.dispatch("/model ollama-local")
    await dispatcher.dispatch("/status")

    assert models.current_alias == "ollama-local"
    assert models.default_alias == "groq-fast"
    assert statuses[0] == "Agent model switched to ollama-local (ollama:qwen3:8b)."
    assert "agent_model=ollama-local (ollama:qwen3:8b)" in statuses[1]


async def test_model_switch_rejects_busy_request() -> None:
    dispatcher, models, statuses, _ = make_dispatcher(state=RequestState.LISTENING)

    await dispatcher.dispatch("/model ollama-local")

    assert models.current_alias == "groq-fast"
    assert statuses == ["Busy (listening); agent model was not changed."]


async def test_model_switch_reports_no_op_unknown_and_usage_errors() -> None:
    dispatcher, models, statuses, _ = make_dispatcher()

    await dispatcher.dispatch("/model groq-fast")
    await dispatcher.dispatch("/model missing")
    await dispatcher.dispatch("/model")
    await dispatcher.dispatch("/model groq-fast extra")
    await dispatcher.dispatch("/models extra")

    assert models.current_alias == "groq-fast"
    assert "already active" in statuses[0]
    assert "Available models: groq-fast, ollama-local" in statuses[1]
    assert statuses[2:] == ["Usage: /model NAME", "Usage: /model NAME", "Usage: /models"]


async def test_empty_model_list_and_unknown_selection() -> None:
    dispatcher, _, statuses, _ = make_dispatcher(configured=False)

    await dispatcher.dispatch("/models")
    await dispatcher.dispatch("/model local")
    await dispatcher.dispatch("/status")

    assert statuses[0] == "No agent models are configured."
    assert statuses[1].endswith("Available models: none configured.")
    assert "agent_model=not configured" in statuses[2]


async def test_quit_stops_dispatch_loop() -> None:
    dispatcher, _, _, _ = make_dispatcher()

    assert not await dispatcher.dispatch("/quit")


async def test_chat_commands_replay_and_keep_model_selection(tmp_path):
    from omnivoice.history import ChatHistory
    from omnivoice.actions import ActionPlan, InsertTextAction
    history = ChatHistory(tmp_path / "history.sqlite3")
    await history.start()
    dispatcher, models, statuses, writes = make_dispatcher()
    dispatcher._history = history
    try:
        first = history.active.id
        ident = await history.begin(first, "hello", "source", "test:model")
        await history.save_plan(ident, ActionPlan(actions=(InsertTextAction(type="insert_text", text="reply"),)))
        await history.finish(ident, "completed", None, 1, False)
        await dispatcher.dispatch("/chat new CV editing")
        second = history.active.id
        assert second != first and history.active.name == "CV editing"
        await dispatcher.dispatch("/chat rename Updated CV")
        assert history.active.name == "Updated CV"
        await dispatcher.dispatch("/chat inspect " + first)
        assert history.active.id == second
        assert "You:\nhello" in "".join(writes)
        assert "AI:\nInsert text:\nreply" in "".join(writes)
        await dispatcher.dispatch("/chat open " + first[:8])
        assert history.active.id == first
        assert "Result: completed" in writes[-1]
        assert models.current_alias == "groq-fast"
        await dispatcher.dispatch("/chats")
        assert "Updated CV" in "".join(writes)
        await dispatcher.dispatch("/chat delete " + first)
        assert history.active.id == second
        await dispatcher.dispatch("/chat inspect")
        assert "No messages yet." in writes[-1]
    finally:
        await history.close()


async def test_chat_busy_guards_and_picker_cancellation(tmp_path, monkeypatch):
    from omnivoice.history import ChatHistory
    from omnivoice.chat_ui import SessionPicker
    history = ChatHistory(tmp_path / "history.sqlite3")
    await history.start()
    dispatcher, _, statuses, writes = make_dispatcher(state=RequestState.PROCESSING)
    dispatcher._history = history
    try:
        first = history.active.id
        for command in ["/chat new x", "/chat open", "/chat inspect", "/chat rename x", "/chat delete " + first, "/chat retry"]:
            await dispatcher.dispatch(command)
            assert "Busy" in statuses[-1]
        await dispatcher.dispatch("/chats")
        assert writes
        dispatcher._controller.state = RequestState.IDLE
        async def cancel(picker):
            assert history.busy
            return None
        monkeypatch.setattr(SessionPicker, "run", cancel)
        await dispatcher.dispatch("/chat open")
        assert history.active.id == first and not history.busy
        async def choose(picker):
            assert history.busy
            return first
        monkeypatch.setattr(SessionPicker, "run", choose)
        await dispatcher.dispatch("/chat open")
        assert "No messages yet." in writes[-1]
        assert not history.busy
    finally:
        await history.close()


async def test_console_restores_full_history_before_prompt_at_latest(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext
    import omnivoice.cli as cli
    from omnivoice.history import ChatHistory
    from omnivoice.actions import ActionPlan
    history = ChatHistory(tmp_path / "history.sqlite3")
    await history.start()
    try:
        for text in ["earlier message", "latest message"]:
            ident = await history.begin(history.active.id, text, None, "test:model")
            await history.save_plan(ident, ActionPlan(actions=()))
            await history.finish(ident, "rejected", "rejected", 0, False)
        prompts = []
        class FakePrompt:
            async def prompt_async(self, label):
                prompts.append(label)
                output = capsys.readouterr().out
                assert output.index("earlier message") < output.index("latest message")
                assert output.rstrip().endswith("completed actions: 0")
                assert not history.busy
                return "/quit"
        monkeypatch.setattr(cli, "PromptSession", FakePrompt)
        monkeypatch.setattr(cli, "patch_stdout", nullcontext)
        dispatcher, models, _, _ = make_dispatcher()
        await cli.Console().run(dispatcher._controller, lambda: "ready", models, history)
        assert prompts == ["omnivoice [New chat]> "]
    finally:
        await history.close()
