from dataclasses import dataclass

from omnivoice.cli import HELP, CommandDispatcher
from omnivoice.config import AgentConfig, OmniVoiceConfig
from omnivoice.interaction import RequestState
from omnivoice.models import ModelRegistry


@dataclass
class FakeController:
    state: RequestState = RequestState.IDLE
    armed: bool = False
    cancelled: bool = False

    def describe_status(self) -> str:
        return f"state={self.state.value}"

    def arm_self_test(self) -> None:
        self.armed = True

    def cancel(self, reason: str = "Request cancelled.") -> None:
        self.cancelled = True


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


def test_help_lists_model_commands() -> None:
    dispatcher, _, _, writes = make_dispatcher()

    assert dispatcher.dispatch("/help")

    assert writes == [HELP]
    assert "/models" in HELP
    assert "/model NAME" in HELP


def test_models_lists_current_and_default_without_provider_access() -> None:
    dispatcher, _, statuses, _ = make_dispatcher()

    dispatcher.dispatch("/models")

    assert statuses == [
        "Configured agent models:\n"
        "  groq-fast: groq:openai/gpt-oss-20b [current, default]\n"
        "  ollama-local: ollama:qwen3:8b"
    ]


def test_models_uses_built_in_profiles_without_user_config() -> None:
    models = ModelRegistry(OmniVoiceConfig().agent)
    statuses: list[str] = []
    dispatcher = CommandDispatcher(
        FakeController(),  # type: ignore[arg-type]
        lambda: "runtime=ready",
        models,
        statuses.append,
        lambda text: None,
    )

    dispatcher.dispatch("/models")

    assert "groq-fast: groq:openai/gpt-oss-20b [current, default]" in statuses[0]
    assert "groq-large: groq:openai/gpt-oss-120b" in statuses[0]
    assert "ollama-local: ollama:qwen3:8b" in statuses[0]


def test_model_switch_updates_status_but_not_default() -> None:
    dispatcher, models, statuses, _ = make_dispatcher()

    dispatcher.dispatch("/model ollama-local")
    dispatcher.dispatch("/status")

    assert models.current_alias == "ollama-local"
    assert models.default_alias == "groq-fast"
    assert statuses[0] == "Agent model switched to ollama-local (ollama:qwen3:8b)."
    assert "agent_model=ollama-local (ollama:qwen3:8b)" in statuses[1]


def test_model_switch_rejects_busy_request() -> None:
    dispatcher, models, statuses, _ = make_dispatcher(state=RequestState.LISTENING)

    dispatcher.dispatch("/model ollama-local")

    assert models.current_alias == "groq-fast"
    assert statuses == ["Busy (listening); agent model was not changed."]


def test_model_switch_reports_no_op_unknown_and_usage_errors() -> None:
    dispatcher, models, statuses, _ = make_dispatcher()

    dispatcher.dispatch("/model groq-fast")
    dispatcher.dispatch("/model missing")
    dispatcher.dispatch("/model")
    dispatcher.dispatch("/model groq-fast extra")
    dispatcher.dispatch("/models extra")

    assert models.current_alias == "groq-fast"
    assert "already active" in statuses[0]
    assert "Available models: groq-fast, ollama-local" in statuses[1]
    assert statuses[2:] == ["Usage: /model NAME", "Usage: /model NAME", "Usage: /models"]


def test_empty_model_list_and_unknown_selection() -> None:
    dispatcher, _, statuses, _ = make_dispatcher(configured=False)

    dispatcher.dispatch("/models")
    dispatcher.dispatch("/model local")
    dispatcher.dispatch("/status")

    assert statuses[0] == "No agent models are configured."
    assert statuses[1].endswith("Available models: none configured.")
    assert "agent_model=not configured" in statuses[2]


def test_quit_stops_dispatch_loop() -> None:
    dispatcher, _, _, _ = make_dispatcher()

    assert not dispatcher.dispatch("/quit")
