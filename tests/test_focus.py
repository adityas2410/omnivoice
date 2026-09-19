from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnivoice.windows.focus import (
    MAX_SELECTED_TEXT_CHARACTERS,
    FocusLease,
    FocusService,
    InvalidTargetError,
    SelectionContext,
    leases_match,
)


EDIT = 50004
COMBO_BOX = 50003
TEXT = 50020
DOCUMENT = 50030
VALUE_PATTERN = 10002
TEXT_PATTERN = 10014
IS_READ_ONLY_ATTRIBUTE = 40015
RANGE_START = 0
RANGE_END = 1


class FakeUnknown:
    """Return a prepared fake when production code requests an interface."""

    def __init__(self, interface: object) -> None:
        self._interface = interface

    def QueryInterface(self, interface_type: object) -> object:
        return self._interface


class FakeValuePattern:
    def __init__(self, *, read_only: bool) -> None:
        self.CurrentIsReadOnly = read_only


class FakeTextRange:
    def __init__(
        self,
        *,
        read_only: bool = False,
        text: str = "",
        start: int = 0,
        end: int = 0,
    ) -> None:
        self._read_only = read_only
        self.text = text
        self.start = start
        self.end = end

    def GetAttributeValue(self, attribute_id: int) -> bool:
        assert attribute_id == IS_READ_ONLY_ATTRIBUTE
        return self._read_only

    def GetText(self, max_length: int) -> str:
        return self.text if max_length < 0 else self.text[:max_length]

    def Clone(self) -> FakeTextRange:
        return FakeTextRange(
            read_only=self._read_only,
            text=self.text,
            start=self.start,
            end=self.end,
        )

    def CompareEndpoints(
        self, source_endpoint: int, other: FakeTextRange, target_endpoint: int
    ) -> int:
        source = self.start if source_endpoint == RANGE_START else self.end
        target = other.start if target_endpoint == RANGE_START else other.end
        return source - target


class FakeTextRangeArray:
    def __init__(self, ranges: list[FakeTextRange]) -> None:
        self._ranges = ranges
        self.Length = len(ranges)

    def GetElement(self, index: int) -> FakeTextRange:
        return self._ranges[index]


class FakeTextPattern:
    def __init__(
        self,
        *,
        read_only: bool,
        selection: list[FakeTextRange] | None = None,
    ) -> None:
        self.DocumentRange = FakeTextRange(read_only=read_only)
        self.selection = selection or []

    def GetSelection(self) -> FakeTextRangeArray:
        return FakeTextRangeArray(self.selection)


class FakeElement:
    def __init__(
        self,
        control_type: int,
        runtime_id: tuple[int, ...],
        *,
        has_focus: bool = True,
        native_window_handle: int | None = 20,
        patterns: dict[int, object] | None = None,
    ) -> None:
        self.CurrentControlType = control_type
        self.CurrentHasKeyboardFocus = has_focus
        self.CurrentIsEnabled = True
        self.CurrentIsKeyboardFocusable = True
        self.CurrentIsPassword = False
        self.CurrentIsOffscreen = False
        self.CurrentProcessId = 10
        self.CurrentNativeWindowHandle = native_window_handle
        self._runtime_id = runtime_id
        self._patterns = patterns or {}

    def GetRuntimeId(self) -> tuple[int, ...]:
        return self._runtime_id

    def GetCurrentPattern(self, pattern_id: int) -> FakeUnknown:
        return FakeUnknown(self._patterns[pattern_id])


class NullComElement:
    """Behave like the false-but-not-None pointer comtypes returns at a root."""

    def __bool__(self) -> bool:
        return False

    @property
    def CurrentControlType(self) -> int:
        raise ValueError("NULL COM pointer access")


class FakeWalker:
    def __init__(self, parents: dict[FakeElement, object]) -> None:
        self._parents = parents

    def GetParentElement(self, element: FakeElement) -> object:
        return self._parents.get(element)


class FakeAutomation:
    def __init__(
        self,
        focused: FakeElement,
        parents: dict[FakeElement, object] | None = None,
    ) -> None:
        self._focused = focused
        self.RawViewWalker = FakeWalker(parents or {})

    def GetFocusedElement(self) -> FakeElement:
        return self._focused


MODULE = SimpleNamespace(
    UIA_ComboBoxControlTypeId=COMBO_BOX,
    UIA_EditControlTypeId=EDIT,
    UIA_DocumentControlTypeId=DOCUMENT,
    UIA_TextControlTypeId=TEXT,
    UIA_PaneControlTypeId=50033,
    UIA_CustomControlTypeId=50025,
    UIA_ValuePatternId=VALUE_PATTERN,
    UIA_TextPatternId=TEXT_PATTERN,
    UIA_IsReadOnlyAttributeId=IS_READ_ONLY_ATTRIBUTE,
    IUIAutomationValuePattern=object(),
    IUIAutomationTextPattern=object(),
    TextPatternRangeEndpoint_Start=RANGE_START,
    TextPatternRangeEndpoint_End=RANGE_END,
)


def test_focus_leases_match_all_identity_fields() -> None:
    lease = FocusLease((1, 2, 3), 10, 20, EDIT)

    assert leases_match(lease, FocusLease((1, 2, 3), 10, 20, EDIT))
    assert not leases_match(lease, FocusLease((1, 2, 4), 10, 20, EDIT))
    assert not leases_match(lease, FocusLease((1, 2, 3), 11, 20, EDIT))
    assert not leases_match(lease, FocusLease((1, 2, 3), 10, 21, EDIT))


def test_capture_accepts_missing_native_window_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    element = FakeElement(EDIT, (1, 2, 3), native_window_handle=None)
    monkeypatch.setattr(FocusService, "_is_writable", staticmethod(lambda *_: True))

    lease = FocusService._capture(FakeAutomation(element), MODULE)

    assert lease.native_window_handle is None
    assert lease.runtime_id == (1, 2, 3)


def test_capture_normalizes_zero_native_window_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    element = FakeElement(EDIT, (1, 2, 3), native_window_handle=0)
    monkeypatch.setattr(FocusService, "_is_writable", staticmethod(lambda *_: True))

    lease = FocusService._capture(FakeAutomation(element), MODULE)

    assert lease.native_window_handle is None


def test_capture_normalizes_focused_text_child_to_edit_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = FakeElement(TEXT, (4, 5, 6))
    parent = FakeElement(EDIT, (1, 2, 3), has_focus=False)
    monkeypatch.setattr(FocusService, "_is_writable", staticmethod(lambda *_: True))

    lease = FocusService._capture(FakeAutomation(child, {child: parent}), MODULE)

    assert lease.runtime_id == (1, 2, 3)
    assert lease.control_type == EDIT


def test_capture_accepts_editable_combo_box() -> None:
    combo_box = FakeElement(
        COMBO_BOX,
        (1, 2, 3),
        patterns={VALUE_PATTERN: FakeValuePattern(read_only=False)},
    )

    lease = FocusService._capture(FakeAutomation(combo_box), MODULE)

    assert lease.control_type == COMBO_BOX


def test_capture_rejects_selection_only_combo_box() -> None:
    combo_box = FakeElement(COMBO_BOX, (1, 2, 3))

    with pytest.raises(InvalidTargetError, match="ComboBox control is read-only"):
        FocusService._capture(FakeAutomation(combo_box), MODULE)


def test_combo_box_requires_value_pattern() -> None:
    combo_box = FakeElement(
        COMBO_BOX,
        (1, 2, 3),
        patterns={TEXT_PATTERN: FakeTextPattern(read_only=False)},
    )

    with pytest.raises(InvalidTargetError, match="ComboBox control is read-only"):
        FocusService._capture(FakeAutomation(combo_box), MODULE)


def test_capture_accepts_writable_document() -> None:
    document = FakeElement(
        DOCUMENT,
        (1, 2, 3),
        patterns={TEXT_PATTERN: FakeTextPattern(read_only=False)},
    )

    lease = FocusService._capture(FakeAutomation(document), MODULE)

    assert lease.control_type == DOCUMENT


def test_capture_rejects_read_only_document() -> None:
    document = FakeElement(
        DOCUMENT,
        (1, 2, 3),
        patterns={TEXT_PATTERN: FakeTextPattern(read_only=True)},
    )

    with pytest.raises(InvalidTargetError, match="Document control is read-only"):
        FocusService._capture(FakeAutomation(document), MODULE)


def test_null_com_parent_ends_target_resolution_safely() -> None:
    text = FakeElement(TEXT, (1, 2, 3))

    with pytest.raises(InvalidTargetError, match=r"not supported \(Text\)"):
        FocusService._capture(
            FakeAutomation(text, {text: NullComElement()}),
            MODULE,
        )


def test_control_type_name_identifies_combo_box() -> None:
    assert FocusService._control_type_name(MODULE, COMBO_BOX) == "ComboBox"


def selection_target(ranges: list[FakeTextRange]) -> tuple[FakeAutomation, FocusLease]:
    element = FakeElement(
        EDIT,
        (1, 2, 3),
        patterns={
            VALUE_PATTERN: FakeValuePattern(read_only=False),
            TEXT_PATTERN: FakeTextPattern(read_only=False, selection=ranges),
        },
    )
    automation = FakeAutomation(element)
    return automation, FocusService._capture(automation, MODULE)


def test_selection_capture_returns_none_without_text_pattern() -> None:
    element = FakeElement(
        EDIT,
        (1, 2, 3),
        patterns={VALUE_PATTERN: FakeValuePattern(read_only=False)},
    )
    automation = FakeAutomation(element)
    lease = FocusService._capture(automation, MODULE)

    assert FocusService._capture_selection(automation, MODULE, lease) is None


def test_selection_capture_treats_degenerate_range_as_no_selection() -> None:
    automation, lease = selection_target([FakeTextRange(start=4, end=4)])

    assert FocusService._capture_selection(automation, MODULE, lease) is None


def test_selection_capture_clones_one_bounded_range() -> None:
    selected = FakeTextRange(text="selected words", start=4, end=18)
    automation, lease = selection_target([selected])

    snapshot = FocusService._capture_selection(automation, MODULE, lease)

    assert snapshot is not None
    assert snapshot.context.lease == lease
    assert snapshot.context.text == "selected words"
    assert snapshot.text_range is not selected
    assert FocusService._selection_matches_current(
        automation, MODULE, snapshot.context, snapshot
    )


def test_selection_capture_stops_multiple_ranges() -> None:
    automation, lease = selection_target(
        [
            FakeTextRange(text="one", start=0, end=3),
            FakeTextRange(text="two", start=5, end=8),
        ]
    )

    with pytest.raises(InvalidTargetError, match="Multiple text selections"):
        FocusService._capture_selection(automation, MODULE, lease)


@pytest.mark.parametrize(
    ("length", "allowed"),
    [
        (MAX_SELECTED_TEXT_CHARACTERS, True),
        (MAX_SELECTED_TEXT_CHARACTERS + 1, False),
    ],
)
def test_selection_capture_enforces_text_limit(length: int, allowed: bool) -> None:
    automation, lease = selection_target(
        [FakeTextRange(text="x" * length, start=0, end=length)]
    )

    if allowed:
        snapshot = FocusService._capture_selection(automation, MODULE, lease)
        assert snapshot is not None
        assert len(snapshot.context.text) == length
    else:
        with pytest.raises(InvalidTargetError, match="exceeds 4000"):
            FocusService._capture_selection(automation, MODULE, lease)


def test_selection_capture_detects_oversized_astral_text() -> None:
    text = "😀" * (MAX_SELECTED_TEXT_CHARACTERS + 1)
    automation, lease = selection_target(
        [FakeTextRange(text=text, start=0, end=len(text))]
    )

    with pytest.raises(InvalidTargetError, match="exceeds 4000"):
        FocusService._capture_selection(automation, MODULE, lease)


def test_selection_revalidation_detects_endpoint_or_text_changes() -> None:
    current = FakeTextRange(text="same", start=2, end=6)
    automation, lease = selection_target([current])
    snapshot = FocusService._capture_selection(automation, MODULE, lease)
    assert snapshot is not None

    current.start = 8
    current.end = 12
    assert not FocusService._selection_matches_current(
        automation, MODULE, snapshot.context, snapshot
    )

    current.start = 2
    current.end = 6
    current.text = "diff"
    assert not FocusService._selection_matches_current(
        automation, MODULE, snapshot.context, snapshot
    )


def test_selection_revalidation_requires_retained_matching_token() -> None:
    current = FakeTextRange(text="same", start=2, end=6)
    automation, lease = selection_target([current])
    snapshot = FocusService._capture_selection(automation, MODULE, lease)
    assert snapshot is not None
    other = SelectionContext("other", lease, snapshot.context.text)

    assert not FocusService._selection_matches_current(
        automation, MODULE, other, snapshot
    )
    assert not FocusService._selection_matches_current(
        automation, MODULE, snapshot.context, None
    )
