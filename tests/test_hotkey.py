import pytest

from omnivoice.windows.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    HotkeyError,
    parse_hotkey,
)


def test_parse_default_hotkey() -> None:
    spec = parse_hotkey("ctrl+alt+space")

    assert spec.modifiers == ("ctrl", "alt")
    assert spec.trigger == "space"
    assert spec.trigger_vk == 0x20
    assert spec.modifier_mask == MOD_CONTROL | MOD_ALT | MOD_NOREPEAT


@pytest.mark.parametrize(
    "value",
    ["", "ctrl+alt", "a+b", "ctrl+ctrl+a", "ctrl+f12", "ctrl+not-a-key"],
)
def test_invalid_hotkeys_are_rejected(value: str) -> None:
    with pytest.raises(HotkeyError):
        parse_hotkey(value)
