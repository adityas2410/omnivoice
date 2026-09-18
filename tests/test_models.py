import pytest

from omnivoice.config import AgentConfig
from omnivoice.models import ModelRegistry, ModelSelection, ModelSelectionError


def configured_registry() -> ModelRegistry:
    return ModelRegistry(
        AgentConfig(
            default_model="groq-fast",
            models={
                "groq-fast": "groq:openai/gpt-oss-20b",
                "ollama-local": "ollama:qwen3:8b",
            },
        )
    )


def test_registry_starts_at_default_and_preserves_order() -> None:
    registry = configured_registry()

    assert registry.default_alias == "groq-fast"
    assert registry.snapshot() == ModelSelection(
        "groq-fast", "groq:openai/gpt-oss-20b"
    )
    assert [item.alias for item in registry.configured] == [
        "groq-fast",
        "ollama-local",
    ]


def test_registry_switch_is_session_only_and_snapshot_is_immutable() -> None:
    registry = configured_registry()
    original = registry.snapshot()

    selected, changed = registry.select("ollama-local")

    assert changed
    assert selected == ModelSelection("ollama-local", "ollama:qwen3:8b")
    assert original == ModelSelection("groq-fast", "groq:openai/gpt-oss-20b")
    assert configured_registry().current_alias == "groq-fast"


def test_selecting_active_model_is_a_no_op() -> None:
    registry = configured_registry()

    selected, changed = registry.select("groq-fast")

    assert selected.alias == "groq-fast"
    assert not changed


def test_unknown_model_does_not_change_selection() -> None:
    registry = configured_registry()

    with pytest.raises(ModelSelectionError):
        registry.select("missing")

    assert registry.current_alias == "groq-fast"


def test_empty_registry_has_no_snapshot() -> None:
    registry = ModelRegistry(AgentConfig())

    assert registry.configured == ()
    assert registry.snapshot() is None
    assert registry.describe_status() == "agent_model=not configured"
