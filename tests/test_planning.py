import asyncio
import json
from collections.abc import Callable

import pytest
import pydantic_ai
from pydantic_ai import ModelProfile
from pydantic_ai import models as pydantic_models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.test import TestModel

import omnivoice.planning as planning
from omnivoice.actions import ActionPlanRejected
from omnivoice.models import ModelSelection
from omnivoice.planning import (
    OLLAMA_LOCAL_BASE_URL,
    ActionPlanGenerator,
    ModelHandle,
    PlanGenerationError,
    build_model,
)


@pytest.fixture(autouse=True)
def forbid_real_model_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pydantic_models, "ALLOW_MODEL_REQUESTS", False)


def model_factory(
    model: object, closed: list[bool] | None = None
) -> Callable[[ModelSelection], ModelHandle]:
    async def close() -> None:
        if closed is not None:
            closed.append(True)

    return lambda selection: ModelHandle(model=model, close=close)


def native_test_model(output: object) -> TestModel:
    """Make TestModel emulate a provider-native JSON-schema response."""

    return TestModel(
        custom_output_text=json.dumps(output),
        profile=ModelProfile(supports_json_schema_output=True),
    )


@pytest.mark.asyncio
async def test_test_model_returns_typed_plan_without_network() -> None:
    model = native_test_model(
        {
            "actions": [
                {"type": "insert_text", "text": "hello"},
                {"type": "shortcut", "keys": ["ctrl", "s"]},
            ]
        }
    )
    closed: list[bool] = []
    generator = ActionPlanGenerator(model_factory(model, closed))

    plan = await generator.generate(
        "write a greeting and save it",
        ModelSelection("test", "groq:test"),
        asyncio.Event(),
    )

    assert [action.type for action in plan.actions] == ["insert_text", "shortcut"]
    assert closed == [True]
    assert pydantic_ai.BANNER_ENABLED is False
    assert model.last_model_request_parameters is not None
    assert model.last_model_request_parameters.output_mode == "native"


@pytest.mark.asyncio
async def test_function_model_corrects_one_invalid_output() -> None:
    calls = 0

    def respond(messages: list[object], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        del messages
        calls += 1
        arguments = (
            {"actions": [{"type": "shortcut", "keys": []}]}
            if calls == 1
            else {"actions": [{"type": "shortcut", "keys": ["ctrl", "z"]}]}
        )
        return ModelResponse(parts=[TextPart(json.dumps(arguments))])

    generator = ActionPlanGenerator(model_factory(FunctionModel(respond)))

    plan = await generator.generate(
        "undo that",
        ModelSelection("test", "groq:test"),
        asyncio.Event(),
    )

    assert calls == 2
    assert plan.actions[0].type == "shortcut"


@pytest.mark.asyncio
async def test_repeated_malformed_output_is_sanitized() -> None:
    generator = ActionPlanGenerator(
        model_factory(native_test_model({"actions": "private malformed"}))
    )

    with pytest.raises(PlanGenerationError, match="valid action plan") as error:
        await generator.generate(
            "private transcript",
            ModelSelection("test", "groq:test"),
            asyncio.Event(),
        )

    assert "private" not in error.value.user_message


@pytest.mark.asyncio
async def test_policy_rejection_is_not_misreported_as_provider_failure() -> None:
    generator = ActionPlanGenerator(
        model_factory(
            native_test_model(
                {
                    "actions": [{"type": "shortcut", "keys": ["alt", "f4"]}]
                }
            )
        )
    )

    with pytest.raises(ActionPlanRejected, match="unsupported shortcut") as error:
        await generator.generate(
            "close this",
            ModelSelection("test", "groq:test"),
            asyncio.Event(),
        )

    assert 'Model output: {"actions":' in str(error.value)
    assert '"keys":["alt","f4"]' in str(error.value)


@pytest.mark.asyncio
async def test_cancellation_discards_late_model_output() -> None:
    started = asyncio.Event()

    async def respond(messages: list[object], info: AgentInfo) -> ModelResponse:
        del messages
        started.set()
        await asyncio.Event().wait()
        return ModelResponse(parts=[TextPart('{"actions": []}')])

    cancelled = asyncio.Event()
    generator = ActionPlanGenerator(model_factory(FunctionModel(respond)))
    task = asyncio.create_task(
        generator.generate(
            "private transcript",
            ModelSelection("test", "groq:test"),
            cancelled,
        )
    )
    await started.wait()
    cancelled.set()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_provider_constructor_failure_is_sanitized() -> None:
    def fail(selection: ModelSelection) -> ModelHandle:
        del selection
        raise RuntimeError("private provider detail")

    generator = ActionPlanGenerator(fail)

    with pytest.raises(PlanGenerationError, match="Groq request failed") as error:
        await generator.generate(
            "private transcript",
            ModelSelection("test", "groq:test"),
            asyncio.Event(),
        )

    assert "private" not in error.value.user_message


@pytest.mark.asyncio
async def test_outer_deadline_cancels_model_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    closed: list[bool] = []

    async def never_returns(messages: list[object], info: AgentInfo) -> ModelResponse:
        del messages, info
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(planning, "PLAN_DEADLINE_SECONDS", 0.01)
    generator = ActionPlanGenerator(
        model_factory(FunctionModel(never_returns), closed)
    )

    with pytest.raises(PlanGenerationError, match="timed out"):
        await generator.generate(
            "private transcript",
            ModelSelection("test", "groq:test"),
            asyncio.Event(),
        )

    assert started.is_set()
    assert closed == [True]


def test_missing_groq_key_fails_before_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(PlanGenerationError, match="GROQ_API_KEY"):
        build_model(ModelSelection("groq-fast", "groq:test-model"))


@pytest.mark.asyncio
async def test_provider_factory_builds_explicit_groq_and_local_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-secret-never-print")
    groq_handle = build_model(ModelSelection("groq-fast", "groq:test-model"))
    ollama_handle = build_model(ModelSelection("local", "ollama:qwen3:8b"))
    try:
        assert isinstance(groq_handle.model, GroqModel)
        assert isinstance(ollama_handle.model, OllamaModel)
        assert ollama_handle.model.base_url.rstrip("/") == OLLAMA_LOCAL_BASE_URL
        assert groq_handle.model.model_name == "test-model"
        assert ollama_handle.model.model_name == "qwen3:8b"
    finally:
        await groq_handle.close()
        await ollama_handle.close()
