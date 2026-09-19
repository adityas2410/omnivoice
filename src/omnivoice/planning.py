"""One-shot structured model requests for constrained keyboard action plans."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from groq import AsyncGroq
from openai import AsyncOpenAI
import pydantic_ai
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.output import NativeOutput
from pydantic_ai.providers.groq import GroqProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from omnivoice.actions import (
    ActionPlan,
    ActionPlanRejected,
    planner_instructions,
    validate_action_plan,
)
from omnivoice.models import ModelSelection


MODEL_REQUEST_TIMEOUT_SECONDS = 20.0
PLAN_DEADLINE_SECONDS = 30.0
MAX_OUTPUT_TOKENS = 1_024
OLLAMA_LOCAL_BASE_URL = "http://localhost:11434/v1"

# OmniVoice owns its interactive terminal.  Pydantic AI otherwise prints a
# first-run promotional banner (including ANSI escapes on some Windows
# terminals) when the first model request starts.
pydantic_ai.BANNER_ENABLED = False


class PlanGenerationError(RuntimeError):
    """Report a content-free provider failure suitable for the terminal."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(frozen=True, slots=True)
class ModelHandle:
    """Pair one request model with cleanup for its provider SDK client."""

    model: Any
    close: Callable[[], Awaitable[None]]


def build_model(selection: ModelSelection) -> ModelHandle:
    """Lazily construct exactly the provider selected for this request."""

    provider_name, _, model_name = selection.selector.partition(":")
    if provider_name == "groq":
        api_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not api_key:
            raise PlanGenerationError(
                "Groq is not configured. Add GROQ_API_KEY to the provider credentials file."
            )
        client = AsyncGroq(
            api_key=api_key,
            max_retries=0,
            timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
        )
        model = GroqModel(model_name, provider=GroqProvider(groq_client=client))
        return ModelHandle(model=model, close=client.close)

    if provider_name == "ollama":
        client = AsyncOpenAI(
            base_url=OLLAMA_LOCAL_BASE_URL,
            api_key="ollama-local",
            max_retries=0,
            timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
        )
        model = OllamaModel(model_name, provider=OllamaProvider(openai_client=client))
        return ModelHandle(model=model, close=client.close)

    # Configuration validation should make this unreachable, but request-time
    # code remains fail-closed if a registry is constructed another way.
    raise PlanGenerationError("The selected model provider is unsupported.")


class ActionPlanGenerator:
    """Turn one transcript into one typed plan without exposing any tools."""

    def __init__(
        self,
        model_factory: Callable[[ModelSelection], ModelHandle] = build_model,
    ) -> None:
        self._model_factory = model_factory
        self._agent = Agent(
            instructions=planner_instructions(),
            output_type=NativeOutput(
                ActionPlan,
                name="submit_action_plan",
                description="Return the complete bounded keyboard action plan.",
            ),
            retries=1,
        )

    async def generate(
        self,
        transcript: str,
        selection: ModelSelection,
        cancelled: asyncio.Event,
    ) -> ActionPlan:
        """Run one bounded request and discard any result arriving after cancellation."""

        if cancelled.is_set():
            raise asyncio.CancelledError
        handle: ModelHandle | None = None
        run_task: asyncio.Task[ActionPlan] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        try:
            handle = self._model_factory(selection)
            run_task = asyncio.create_task(
                self._run(transcript, handle.model), name="omnivoice-model-request"
            )
            cancel_task = asyncio.create_task(cancelled.wait())
            async with asyncio.timeout(PLAN_DEADLINE_SECONDS):
                done, _ = await asyncio.wait(
                    {run_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancel_task in done and cancel_task.result():
                    run_task.cancel()
                    await asyncio.gather(run_task, return_exceptions=True)
                    raise asyncio.CancelledError
                plan = await run_task
            if cancelled.is_set():
                raise asyncio.CancelledError
            return validate_action_plan(plan)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise PlanGenerationError("The model request timed out.") from exc
        except ActionPlanRejected:
            raise
        except PlanGenerationError:
            raise
        except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
            raise PlanGenerationError(
                "The model did not return a valid action plan."
            ) from exc
        except BaseException as exc:
            if selection.selector.startswith("ollama:"):
                message = (
                    "Local Ollama request failed. Ensure Ollama is running and the "
                    "selected model is installed."
                )
            else:
                message = "Groq request failed. Check the model, network, and API quota."
            raise PlanGenerationError(message) from exc
        finally:
            for task in (run_task, cancel_task):
                if task is not None and not task.done():
                    task.cancel()
            pending = [
                task for task in (run_task, cancel_task) if task is not None
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if handle is not None:
                try:
                    await handle.close()
                except BaseException:
                    pass

    async def _run(self, transcript: str, model: Any) -> ActionPlan:
        result = await self._agent.run(
            transcript,
            model=model,
            model_settings=ModelSettings(
                temperature=0,
                max_tokens=MAX_OUTPUT_TOKENS,
                timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
                parallel_tool_calls=False,
            ),
            usage_limits=UsageLimits(
                request_limit=2,
                output_tokens_limit=MAX_OUTPUT_TOKENS,
            ),
        )
        return result.output
