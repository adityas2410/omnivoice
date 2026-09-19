import pytest
from pydantic import ValidationError

from omnivoice.actions import (
    ALLOWED_SHORTCUTS,
    ActionPlan,
    ActionPlanRejected,
    InsertTextAction,
    ShortcutAction,
    format_action_plan,
    planner_instructions,
    validate_action_plan,
)


def test_valid_plan_preserves_short_sequence() -> None:
    plan = ActionPlan.model_validate(
        {
            "actions": [
                {"type": "insert_text", "text": "First sentence."},
                {"type": "shortcut", "keys": ["enter"]},
                {"type": "insert_text", "text": "Second sentence."},
            ]
        }
    )

    assert validate_action_plan(plan) is plan
    assert isinstance(plan.actions[0], InsertTextAction)
    assert isinstance(plan.actions[1], ShortcutAction)
    assert isinstance(plan.actions[2], InsertTextAction)


def test_empty_plan_is_structurally_valid_for_unsupported_request() -> None:
    assert validate_action_plan(ActionPlan(actions=())).actions == ()


def test_model_output_format_is_single_line_json_with_escaped_controls() -> None:
    plan = ActionPlan(
        actions=(InsertTextAction(type="insert_text", text="first\nsecond"),)
    )

    rendered = format_action_plan(plan)

    assert "\\n" in rendered
    assert "\n" not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"actions": [{"type": "unknown"}]},
        {"actions": [{"type": "insert_text"}]},
        {"actions": [{"type": "insert_text", "text": 123}]},
        {"actions": [{"type": "shortcut", "keys": ["ctrl", "s"], "delay": 1}]},
        {"actions": [{"type": "shortcut", "keys": [1, 2]}]},
        {"actions": [], "extra": True},
        {"actions": [{"type": "shortcut", "keys": []}]},
        {"actions": [{"type": "insert_text", "text": "x"}] * 6},
    ],
)
def test_malformed_plans_fail_schema_validation(payload: object) -> None:
    with pytest.raises(ValidationError):
        ActionPlan.model_validate(payload)


@pytest.mark.parametrize("text", ["a\tb", "nul\x00value", "escape\x1b", "a\vvalue"])
def test_control_characters_are_rejected_before_execution(text: str) -> None:
    plan = ActionPlan(actions=(InsertTextAction(type="insert_text", text=text),))

    with pytest.raises(ActionPlanRejected, match="control character"):
        validate_action_plan(plan)


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n"])
def test_line_breaks_become_guarded_enter_actions(separator: str) -> None:
    plan = ActionPlan(
        actions=(
            InsertTextAction(
                type="insert_text",
                text=f"First sentence.{separator}Second sentence.",
            ),
        )
    )

    validated = validate_action_plan(plan)

    assert [action.type for action in validated.actions] == [
        "insert_text",
        "shortcut",
        "insert_text",
    ]
    assert validated.actions[1] == ShortcutAction(type="shortcut", keys=("enter",))


def test_line_break_expansion_still_obeys_action_limit() -> None:
    plan = ActionPlan(
        actions=(
            InsertTextAction(type="insert_text", text="one\ntwo"),
            InsertTextAction(type="insert_text", text="three"),
            InsertTextAction(type="insert_text", text="four"),
            InsertTextAction(type="insert_text", text="five"),
        )
    )

    with pytest.raises(ActionPlanRejected, match="safe action limit"):
        validate_action_plan(plan)


def test_total_text_limit_cannot_be_bypassed_with_multiple_actions() -> None:
    plan = ActionPlan(
        actions=(
            InsertTextAction(type="insert_text", text="a" * 1_500),
            InsertTextAction(type="insert_text", text="b" * 501),
        )
    )

    with pytest.raises(ActionPlanRejected, match="safe typing limit"):
        validate_action_plan(plan)


@pytest.mark.parametrize(
    "keys",
    [("alt", "f4"), ("ctrl", "shift", "s"), ("win", "r"), ("s", "ctrl")],
)
def test_unlisted_shortcuts_are_rejected_as_complete_chords(
    keys: tuple[str, ...]
) -> None:
    plan = ActionPlan(actions=(ShortcutAction(type="shortcut", keys=keys),))

    with pytest.raises(ActionPlanRejected, match="unsupported shortcut"):
        validate_action_plan(plan)


def test_prompt_derives_allowed_chords_without_intent_mappings() -> None:
    instructions = planner_instructions()

    assert ALLOWED_SHORTCUTS == (("enter",), ("ctrl", "s"), ("ctrl", "z"))
    assert "enter" in instructions
    assert "line break" in instructions
    assert "CR or LF" in instructions
    assert "ctrl+s" in instructions
    assert "ctrl+z" in instructions
    assert "save" not in instructions.lower()
    assert "undo" not in instructions.lower()
