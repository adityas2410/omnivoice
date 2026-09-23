"""One-shot structured model requests for constrained keyboard action plans."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pydantic_ai
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
    UserError,
)
from pydantic_ai.models import Model, infer_model
from pydantic_ai.output import NativeOutput
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from omnivoice.actions import (
    ActionPlan,
    ActionPlanRejected,
    format_action_plan,
    planner_instructions,
    validate_action_plan,
)
from omnivoice.context import CapturedContext
from omnivoice.models import ModelSelection


MODEL_REQUEST_TIMEOUT_SECONDS = 20.0
PLAN_DEADLINE_SECONDS = 30.0
MAX_OUTPUT_TOKENS = 1_024
LOGGER = logging.getLogger(__name__)
_SAFE_PROVIDER_VALUE = re.compile(r"[A-Za-z0-9._:/-]{1,128}\Z")

# OmniVoice owns its interactive terminal.  Pydantic AI otherwise prints a
# first-run promotional banner (including ANSI escapes on some Windows
# terminals) when the first model request starts.
pydantic_ai.BANNER_ENABLED = False


class PlanGenerationError(RuntimeError):
    """Report a content-free provider failure suitable for the terminal."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _safe_provider_value(value: object) -> str | None:
    """Keep provider metadata bounded and free of arbitrary response text."""

    if not isinstance(value, str) or not _SAFE_PROVIDER_VALUE.fullmatch(value):
        return None
    return value


def _provider_error_code(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        return _safe_provider_value(error.get("code"))
    return _safe_provider_value(body.get("code"))


def _provider_request_id(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    for name in ("x-request-id", "x-groq-request-id", "request-id"):
        value = _safe_provider_value(headers.get(name))
        if value is not None:
            return value
    return None


def _http_failure_category(status_code: int) -> str:
    if status_code == 400:
        return "invalid request"
    if status_code == 401:
        return "authentication rejected"
    if status_code == 403:
        return "permission denied"
    if status_code == 404:
        return "model or endpoint not found"
    if status_code == 408:
        return "provider timeout"
    if status_code == 413:
        return "request too large"
    if status_code == 422:
        return "request rejected"
    if status_code == 429:
        return "rate limited"
    if status_code >= 500:
        return "provider unavailable"
    return "HTTP error"


def _provider_failure(
    selection: ModelSelection, exc: BaseException
) -> PlanGenerationError:
    """Create useful diagnostics without exposing provider response bodies."""

    provider = selection.selector.partition(":")[0]
    display_name = (
        provider.replace("-", " ").replace("/", " ").title()
        if provider
        else "Model provider"
    )
    if isinstance(exc, ModelHTTPError):
        category = _http_failure_category(exc.status_code)
        provider_code = _provider_error_code(exc.body)
        request_id = _provider_request_id(exc.headers)
        LOGGER.info(
            "event=model_provider_failed provider=%s category=http "
            "status_code=%s provider_code=%s request_id=%s",
            provider,
            exc.status_code,
            provider_code or "none",
            request_id or "none",
        )
        details = [f"HTTP {exc.status_code}"]
        if provider_code is not None:
            details.append(f"code {provider_code}")
        if request_id is not None:
            details.append(f"request ID {request_id}")
        return PlanGenerationError(
            f"{display_name} request failed: {category} ({', '.join(details)})."
        )
    if isinstance(exc, ModelAPIError):
        LOGGER.info(
            "event=model_provider_failed provider=%s category=connection error_type=%s",
            provider,
            type(exc).__name__,
        )
        return PlanGenerationError(
            f"{display_name} connection failed before a valid HTTP response was received."
        )
    LOGGER.info(
        "event=model_provider_failed provider=%s category=unexpected error_type=%s",
        provider,
        type(exc).__name__,
    )
    return PlanGenerationError(
        f"{display_name} request failed due to an unexpected provider error."
    )


@dataclass(frozen=True, slots=True)
class ModelHandle:
    """Pair one request model with generic provider lifecycle cleanup."""

    model: Model
    close: Callable[[], Awaitable[None]]


def build_model(selection: ModelSelection) -> ModelHandle:
    """Resolve any installed Pydantic AI provider from its standard selector."""

    try:
        model = infer_model(selection.selector)
    except (ImportError, ModuleNotFoundError) as exc:
        raise PlanGenerationError(
            "The selected Pydantic AI provider dependency is not installed."
        ) from exc
    except (UserError, ValueError) as exc:
        raise PlanGenerationError(
            "The selected model could not be configured. Check its Pydantic AI "
            "provider prefix, model name, and provider credentials."
        ) from exc

    async def close_model() -> None:
        await model.__aexit__(None, None, None)

    return ModelHandle(model=model, close=close_model)


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
        selected_text: str | None = None,
        context: CapturedContext | None = None,
    ) -> ActionPlan:
        """Run one bounded request and discard any result arriving after cancellation."""

        if cancelled.is_set():
            raise asyncio.CancelledError
        handle: ModelHandle | None = None
        run_task: asyncio.Task[ActionPlan] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        try:
            handle = self._model_factory(selection)
            await handle.model.__aenter__()
            request = _build_request(
                transcript,
                selected_text,
                context,
                selection.input_token_budget,
            )
            run_task = asyncio.create_task(
                self._run(request, handle.model), name="omnivoice-model-request"
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
            try:
                return validate_action_plan(plan, has_selection=selected_text is not None)
            except ActionPlanRejected as exc:
                raise ActionPlanRejected(
                    f"{exc} Model output: {format_action_plan(plan)}"
                ) from exc
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
            raise _provider_failure(selection, exc) from exc
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


def _estimated_tokens(value: str) -> int:
    """Use one documented, deterministic estimate across Groq and Ollama."""

    return math.ceil(len(value.encode("utf-8")) / 3)


def _build_request(
    transcript: str,
    selected_text: str | None,
    context: CapturedContext | None,
    input_token_budget: int,
) -> str:
    """Serialize context and trim optional material before provider contact."""

    payload: dict[str, Any] = {
        "request": transcript,
        "selected_text": selected_text,
        "target_context": (
            context.target_context.model_dump(mode="json")
            if context is not None and context.target_context is not None
            else None
        ),
        "ui_context": (
            context.ui_context.model_dump(mode="json") if context is not None else None
        ),
    }
    overhead = planner_instructions() + json.dumps(
        ActionPlan.model_json_schema(), ensure_ascii=False
    )

    def serialize() -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def estimate() -> int:
        return _estimated_tokens(overhead + serialize())

    mandatory = {
        "request": transcript,
        "selected_text": selected_text,
        "target_context": None,
        "ui_context": None,
    }
    mandatory_text = json.dumps(mandatory, ensure_ascii=False, separators=(",", ":"))
    if _estimated_tokens(overhead + mandatory_text) > input_token_budget:
        raise PlanGenerationError(
            "The spoken request and selected text exceed the selected model input budget."
        )

    while estimate() > input_token_budget:
        ui_context = payload.get("ui_context")
        outline = ui_context.get("semantic_outline") if isinstance(ui_context, dict) else None
        items = outline.get("items") if isinstance(outline, dict) else None
        if isinstance(items, list) and len(items) > 100:
            del items[max(100, len(items) - max(1, len(items) // 4)) :]
            outline["truncated"] = True
            continue

        document = ui_context.get("document_text") if isinstance(ui_context, dict) else None
        content = document.get("content") if isinstance(document, dict) else None
        if isinstance(content, str) and len(content) > 12_000:
            document["content"] = _preserve_edges(content, max(12_000, len(content) * 3 // 4))
            document["truncated_before"] = True
            document["truncated_after"] = True
            continue

        if isinstance(items, list) and items:
            del items[max(0, len(items) - max(1, len(items) // 4)) :]
            outline["truncated"] = True
            if not items:
                ui_context["semantic_outline"] = None
                ui_context["status"] = "partial" if document else "unavailable"
            continue

        target = payload.get("target_context")
        if isinstance(target, dict) and target.get("source") == "text_pattern":
            before = str(target.get("before", ""))
            after = str(target.get("after", ""))
            if len(before) > 4_000 or len(after) > 2_000:
                target["before"] = before[-max(4_000, len(before) * 3 // 4) :]
                target["after"] = after[: max(2_000, len(after) * 3 // 4)]
                target["truncated_before"] = True
                target["truncated_after"] = True
                continue
        elif isinstance(target, dict) and target.get("source") == "value_pattern":
            value = str(target.get("content", ""))
            if len(value) > 6_000:
                target["content"] = _preserve_edges(value, max(6_000, len(value) * 3 // 4))
                target["truncated"] = True
                continue

        if isinstance(content, str) and content:
            new_length = max(0, len(content) - max(1_000, len(content) // 4))
            document["content"] = _preserve_edges(content, new_length)
            document["truncated_before"] = True
            document["truncated_after"] = True
            if not document["content"]:
                ui_context["document_text"] = None
                ui_context["status"] = "unavailable"
            continue

        if target is not None:
            payload["target_context"] = None
            continue
        raise PlanGenerationError(
            "The request context exceeds the selected model input budget."
        )
    return serialize()


def _preserve_edges(text: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    if len(text) <= maximum:
        return text
    marker = "\n[context omitted]\n"
    available = maximum - len(marker)
    if available <= 0:
        return text[:maximum]
    head = available * 4 // 5
    return text[:head] + marker + text[-(available - head) :]
