import threading
from types import SimpleNamespace

from omnivoice.context import ContextCaptureLimits, DocumentTextContext
from omnivoice.windows.context import UIContextService, _Accumulator
from omnivoice.windows.focus import FocusLease


START = 0
END = 1
CHARACTER = 0
TEXT_PATTERN = 10014
VALUE_PATTERN = 10002
DOCUMENT = 50030
EDIT = 50004
TEXT = 50020
BUTTON = 50000


class FakeUnknown:
    def __init__(self, value: object) -> None:
        self.value = value

    def QueryInterface(self, interface: object) -> object:
        del interface
        return self.value


class FakeRange:
    def __init__(self, text: str, start: int, end: int) -> None:
        self.text = text
        self.start = start
        self.end = end

    def Clone(self) -> "FakeRange":
        return FakeRange(self.text, self.start, self.end)

    def MoveEndpointByRange(
        self, endpoint: int, other: "FakeRange", other_endpoint: int
    ) -> None:
        value = other.start if other_endpoint == START else other.end
        if endpoint == START:
            self.start = value
        else:
            self.end = value

    def MoveEndpointByUnit(self, endpoint: int, unit: int, count: int) -> int:
        assert unit == CHARACTER
        old = self.start if endpoint == START else self.end
        new = min(len(self.text), max(0, old + count))
        if endpoint == START:
            self.start = new
        else:
            self.end = new
        return new - old

    def GetText(self, maximum: int) -> str:
        return self.text[self.start : self.end][:maximum]


class FakeRanges:
    def __init__(self, selected: FakeRange) -> None:
        self.Length = 1
        self.selected = selected

    def GetElement(self, index: int) -> FakeRange:
        assert index == 0
        return self.selected


class FakeTextPattern:
    def __init__(self, text: str, selected: FakeRange) -> None:
        self.DocumentRange = FakeRange(text, 0, len(text))
        self.selected = selected

    def GetSelection(self) -> FakeRanges:
        return FakeRanges(self.selected)

    def RangeFromChild(self, child: object) -> FakeRange:
        del child
        return self.selected


class FakeValuePattern:
    def __init__(self, value: str) -> None:
        self.CurrentValue = value
        self.CurrentIsReadOnly = False


class FakeTarget:
    def __init__(self, patterns: dict[int, object]) -> None:
        self.patterns = patterns

    def GetCurrentPattern(self, pattern_id: int) -> FakeUnknown:
        return FakeUnknown(self.patterns[pattern_id])


MODULE = SimpleNamespace(
    UIA_TextPatternId=TEXT_PATTERN,
    UIA_ValuePatternId=VALUE_PATTERN,
    IUIAutomationTextPattern=object(),
    IUIAutomationValuePattern=object(),
    TextPatternRangeEndpoint_Start=START,
    TextPatternRangeEndpoint_End=END,
    TextUnit_Character=CHARACTER,
    UIA_DocumentControlTypeId=DOCUMENT,
    UIA_EditControlTypeId=EDIT,
    UIA_TextControlTypeId=TEXT,
    UIA_ButtonControlTypeId=BUTTON,
    HeadingLevel_None=80050,
)


def limits(**changes: int | float) -> ContextCaptureLimits:
    values: dict[str, int | float] = {
        "document_max_characters": 50,
        "semantic_max_characters": 2_000,
        "semantic_max_elements": 20,
        "semantic_max_depth": 8,
        "target_before_max_characters": 10,
        "target_after_max_characters": 5,
        "capture_timeout_seconds": 5,
    }
    values.update(changes)
    return ContextCaptureLimits(**values)  # type: ignore[arg-type]


def test_target_text_context_excludes_selection_and_keeps_nearest_text() -> None:
    text = "a" * 20 + "selected" + "b" * 20
    selected = FakeRange(text, 20, 28)
    target = FakeTarget({TEXT_PATTERN: FakeTextPattern(text, selected)})

    context = UIContextService._capture_target(target, MODULE, limits())

    assert context.source == "text_pattern"  # type: ignore[union-attr]
    assert context.before == "a" * 10  # type: ignore[union-attr]
    assert context.after == "b" * 5  # type: ignore[union-attr]
    assert context.truncated_before  # type: ignore[union-attr]
    assert context.truncated_after  # type: ignore[union-attr]


def test_target_value_fallback_is_independently_bounded() -> None:
    target = FakeTarget({VALUE_PATTERN: FakeValuePattern("v" * 30)})

    context = UIContextService._capture_target(target, MODULE, limits())

    assert context.source == "value_pattern"  # type: ignore[union-attr]
    assert context.content == "v" * 15  # type: ignore[union-attr]
    assert context.truncated  # type: ignore[union-attr]


def test_document_context_is_centered_around_target_and_omits_target_value() -> None:
    text = "before-" * 10 + "private target" + "-after" * 10
    start = text.index("private target")
    selected = FakeRange(text, start, start + len("private target"))
    pattern = FakeTextPattern(text, selected)
    document = FakeTarget({TEXT_PATTERN: pattern})

    context = UIContextService._capture_document(document, object(), MODULE, limits())

    assert "private target" not in context.content  # type: ignore[union-attr]
    assert "[focused editable control]" in context.content  # type: ignore[union-attr]
    assert len(context.content) <= 50  # type: ignore[union-attr]
    assert context.truncated_before  # type: ignore[union-attr]
    assert context.truncated_after  # type: ignore[union-attr]


class FakeElement:
    def __init__(
        self,
        control_type: int,
        runtime_id: tuple[int, ...],
        *,
        name: str = "",
        heading: int = 0,
        focused: bool = False,
        password: bool = False,
    ) -> None:
        self.CurrentControlType = control_type
        self.CurrentName = name
        self.CurrentFullDescription = ""
        self.CurrentAriaRole = ""
        self.CurrentHeadingLevel = heading
        self.CurrentHasKeyboardFocus = focused
        self.CurrentIsPassword = password
        self.CurrentIsEnabled = True
        self.runtime_id = runtime_id

    def GetRuntimeId(self) -> tuple[int, ...]:
        return self.runtime_id


class FakeWalker:
    def __init__(self, children: dict[FakeElement, list[FakeElement]]) -> None:
        self.children = children
        self.parents: dict[FakeElement, tuple[FakeElement, int]] = {}
        for parent, values in children.items():
            for index, value in enumerate(values):
                self.parents[value] = (parent, index)

    def GetFirstChildElement(self, element: FakeElement) -> FakeElement | None:
        values = self.children.get(element, [])
        return values[0] if values else None

    def GetNextSiblingElement(self, element: FakeElement) -> FakeElement | None:
        parent, index = self.parents[element]
        values = self.children[parent]
        return values[index + 1] if index + 1 < len(values) else None


def test_semantic_outline_preserves_structure_and_filters_duplicates_and_passwords() -> None:
    root = FakeElement(DOCUMENT, (1,), name="Job page")
    heading = FakeElement(TEXT, (2,), name="Senior Developer", heading=1)
    duplicate = FakeElement(TEXT, (3,), name="Job description")
    button = FakeElement(BUTTON, (4,), name="Apply")
    focused_edit = FakeElement(EDIT, (9,), name="private draft", focused=True)
    password = FakeElement(EDIT, (5,), name="Secret", password=True)
    secret_child = FakeElement(TEXT, (6,), name="actual secret")
    walker = FakeWalker(
        {
            root: [heading, duplicate, button, focused_edit, password],
            password: [secret_child],
        }
    )
    automation = SimpleNamespace(ContentViewWalker=walker)
    command = SimpleNamespace(cancelled=threading.Event())

    context = UIContextService._capture_semantic(
        automation,
        MODULE,
        root,
        FocusLease((9,), 10, 20, EDIT),
        limits(),
        "Job description",
        command,
        10**20,
    )

    names = [item.name for item in context.items]  # type: ignore[union-attr]
    roles = [item.role for item in context.items]  # type: ignore[union-attr]
    assert names == ["Job page", "Senior Developer", "Apply", None]
    assert roles == ["document", "heading", "button", "edit"]
    assert context.items[-1].focused  # type: ignore[union-attr]
    assert all(item.name != "actual secret" for item in context.items)  # type: ignore[union-attr]


def test_semantic_item_ignores_no_heading_and_unavailable_pattern_defaults() -> None:
    element = FakeElement(TEXT, (7,), name="Plain text", heading=80050)
    element.CurrentToggleState = 2
    element.CurrentExpandCollapseState = 3
    element.CurrentIsTogglePatternAvailable = False
    element.CurrentIsExpandCollapsePatternAvailable = False
    element.CurrentIsSelectionItemPatternAvailable = False

    item = UIContextService._semantic_item(
        element, MODULE, FocusLease((99,), 10, 20, EDIT), 2
    )

    assert item is not None
    assert item.role == "text"
    assert item.states == ()


def test_semantic_item_keeps_states_only_for_available_patterns() -> None:
    element = FakeElement(BUTTON, (8,), name="Disclosure", heading=80051)
    element.CurrentIsTogglePatternAvailable = True
    element.CurrentToggleState = 1
    element.CurrentIsExpandCollapsePatternAvailable = True
    element.CurrentExpandCollapseState = 1
    element.CurrentIsSelectionItemPatternAvailable = False

    item = UIContextService._semantic_item(
        element, MODULE, FocusLease((99,), 10, 20, EDIT), 2
    )

    assert item is not None
    assert item.role == "heading"
    assert item.states == ("level=1", "checked", "expanded")


def test_accumulator_returns_completed_sources_on_partial_timeout() -> None:
    accumulator = _Accumulator()
    accumulator.update("document", DocumentTextContext(content="private document"))

    result = accumulator.snapshot("timeout")

    assert result.ui_context.status == "partial"
    assert result.ui_context.reason == "timeout"
    assert result.ui_context.document_text.content == "private document"  # type: ignore[union-attr]
