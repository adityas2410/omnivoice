"""Typed AI action plans and the deterministic execution policy."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


MAX_PLAN_ACTIONS = 5
MAX_PLAN_TEXT_CHARACTERS = 2_000
ALLOWED_SHORTCUTS: tuple[tuple[str, ...], ...] = (
    ("enter",),
    ("ctrl", "s"),
    ("ctrl", "z"),
)
_ALLOWED_SHORTCUT_SET = frozenset(ALLOWED_SHORTCUTS)


class ActionPlanRejected(ValueError):
    """Raised when a structurally valid plan violates local execution policy."""


class InsertTextAction(BaseModel):
    """Request bounded, non-control Unicode text at the caret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["insert_text"]
    text: str = Field(min_length=1, max_length=MAX_PLAN_TEXT_CHARACTERS)


class ShortcutAction(BaseModel):
    """Request one key chord, subject to the separate code-owned allowlist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["shortcut"]
    keys: tuple[str, ...] = Field(min_length=1, max_length=4)


Action = Annotated[
    InsertTextAction | ShortcutAction,
    Field(discriminator="type"),
]


class ActionPlan(BaseModel):
    """Complete model output; an empty action list means unsupported."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actions: tuple[Action, ...] = Field(max_length=MAX_PLAN_ACTIONS)


def validate_action_plan(plan: ActionPlan) -> ActionPlan:
    """Apply policy to the complete plan before its first action can execute."""

    text_characters = 0
    for action in plan.actions:
        if isinstance(action, InsertTextAction):
            if any(not character.isprintable() for character in action.text):
                raise ActionPlanRejected(
                    "Generated text contained a control character and was rejected."
                )
            text_characters += len(action.text)
            if text_characters > MAX_PLAN_TEXT_CHARACTERS:
                raise ActionPlanRejected(
                    "Generated text exceeded the safe typing limit."
                )
        elif tuple(action.keys) not in _ALLOWED_SHORTCUT_SET:
            raise ActionPlanRejected("The plan requested an unsupported shortcut.")
    return plan


def planner_instructions() -> str:
    """Describe capabilities without teaching an intent-to-shortcut lookup table."""

    shortcuts = ", ".join("+".join(keys) for keys in ALLOWED_SHORTCUTS)
    return (
        "Convert the user's request into one complete keyboard action plan. "
        "Use insert_text to type generated text at the current caret. "
        "Use shortcut for a Windows key chord. "
        f"The only permitted shortcut chords are: {shortcuts}. "
        "Use your knowledge of Windows shortcuts to choose a permitted chord; "
        "do not invent keys or actions. Return actions in execution order. "
        "If the request cannot be completed using only these actions, return an "
        "empty actions list. Never include control characters in inserted text."
    )
