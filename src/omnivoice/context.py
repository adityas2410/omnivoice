"""Typed, content-safe boundaries for request-scoped UI context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnivoice.config import AgentContextConfig


class ContextBoundaryChanged(RuntimeError):
    """Signal that the anchored target or containing window no longer matches."""


class TextTargetContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["text_pattern"] = "text_pattern"
    before: str
    after: str
    truncated_before: bool = False
    truncated_after: bool = False


class ValueTargetContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["value_pattern"] = "value_pattern"
    content: str
    truncated: bool = False


TargetContext = TextTargetContext | ValueTargetContext


class DocumentTextContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    truncated_before: bool = False
    truncated_after: bool = False


class SemanticItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    depth: int
    role: str
    name: str | None = None
    description: str | None = None
    states: tuple[str, ...] = ()
    focused: bool = False


class SemanticOutlineContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[SemanticItem, ...]
    truncated: bool = False


class UIContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["complete", "partial", "unavailable"]
    window_title: str | None = None
    document_text: DocumentTextContext | None = None
    semantic_outline: SemanticOutlineContext | None = None
    reason: Literal[
        "not_supported",
        "empty",
        "timeout",
        "worker_unavailable",
        "capture_failed",
    ] | None = None


class CapturedContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_context: TargetContext | None = None
    ui_context: UIContext

    def metadata_summary(self) -> str:
        """Describe sizes and truncation without exposing captured content."""

        parts: list[str] = []
        target = self.target_context
        if isinstance(target, TextTargetContext):
            count = len(target.before) + len(target.after)
            suffix = " truncated" if target.truncated_before or target.truncated_after else ""
            parts.append(f"target=text {count} chars{suffix}")
        elif isinstance(target, ValueTargetContext):
            suffix = " truncated" if target.truncated else ""
            parts.append(f"target=value {len(target.content)} chars{suffix}")

        document = self.ui_context.document_text
        if document is not None:
            suffix = (
                " truncated"
                if document.truncated_before or document.truncated_after
                else ""
            )
            parts.append(f"document={len(document.content)} chars{suffix}")

        outline = self.ui_context.semantic_outline
        if outline is not None:
            suffix = " truncated" if outline.truncated else ""
            parts.append(f"semantic={len(outline.items)} elements{suffix}")

        if not parts:
            return f"context={self.ui_context.status}"
        return f"context={self.ui_context.status}; " + ", ".join(parts)


@dataclass(frozen=True, slots=True)
class ContextCaptureLimits:
    document_max_characters: int
    semantic_max_characters: int
    semantic_max_elements: int
    semantic_max_depth: int
    target_before_max_characters: int
    target_after_max_characters: int
    capture_timeout_seconds: float

    @classmethod
    def from_config(cls, config: AgentContextConfig) -> ContextCaptureLimits:
        return cls(
            document_max_characters=config.document_max_characters,
            semantic_max_characters=config.semantic_max_characters,
            semantic_max_elements=config.semantic_max_elements,
            semantic_max_depth=config.semantic_max_depth,
            target_before_max_characters=config.target_before_max_characters,
            target_after_max_characters=config.target_after_max_characters,
            capture_timeout_seconds=config.capture_timeout_seconds,
        )


def unavailable_context(
    reason: Literal[
        "not_supported", "empty", "timeout", "worker_unavailable", "capture_failed"
    ],
) -> CapturedContext:
    return CapturedContext(
        ui_context=UIContext(status="unavailable", reason=reason)
    )
