"""macOS AX → shared ``ui_*`` vocabulary. Pure tables, no pyobjc import."""

from __future__ import annotations

from .model import RawNode
from .normalize import Verbs, role_table, states_of

_ROLE = {
    "AXButton": "button",
    "AXMenuButton": "button",
    "AXDisclosureTriangle": "button",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXLink": "link",
    "AXTextField": "text_field",
    "AXSearchField": "text_field",
    "AXTextArea": "text_area",
    "AXStaticText": "static_text",
    "AXHeading": "static_text",
    "AXMenu": "menu",
    "AXMenuBar": "menu",
    "AXMenuItem": "menu_item",
    "AXMenuBarItem": "menu_item",
    "AXPopUpButton": "combo_box",
    "AXComboBox": "combo_box",
    "AXList": "list",
    "AXTable": "table",
    "AXGrid": "table",
    "AXOutline": "tree",
    "AXBrowser": "tree",
    "AXRow": "row",
    "AXCell": "cell",
    "AXSlider": "slider",
    "AXIncrementor": "slider",
    "AXWindow": "window",
    "AXSheet": "dialog",
    "AXDrawer": "dialog",
    "AXPopover": "dialog",
    "AXGroup": "group",
    "AXSplitGroup": "group",
    "AXRadioGroup": "group",
    "AXTabGroup": "group",
    "AXLayoutArea": "group",
    "AXWebArea": "group",
    "AXImage": "image",
    "AXScrollArea": "scroll_area",
    "AXToolbar": "toolbar",
}

_SUBROLE = {
    ("AXRadioButton", "AXTabButton"): "tab",
    ("AXWindow", "AXDialog"): "dialog",
    ("AXWindow", "AXSystemDialog"): "dialog",
    ("AXRow", "AXOutlineRow"): "tree_item",
    ("AXCheckBox", "AXSwitch"): "checkbox",
}

_PRESS_ACTIONS = frozenset({"AXPress", "AXOpen", "AXConfirm", "AXPick"})
_TEXT_ROLES = frozenset({"text_field", "text_area", "combo_box"})
_SELECTABLE_ROLES = frozenset({"row", "list_item", "tree_item", "cell"})


role_of = role_table(_ROLE, _SUBROLE)


def normalize(raw: RawNode) -> tuple[str, list[str], list[str]]:
    """``(role, verbs, states)`` for one AX element.

    States list only what is notable (``disabled``, not ``enabled``), which
    keeps outline lines short.
    """
    role = role_of(raw.native_role, raw.subrole)
    actions = set(raw.actions)
    pressable = bool(actions & _PRESS_ACTIONS)
    verbs = Verbs()
    add = verbs.add

    if role == "checkbox":
        if pressable or raw.value_settable:
            add("toggle")
    elif role in ("tab", "radio"):
        if pressable or raw.selected is not None:
            add("select")
    elif raw.native_role in ("AXMenuBarItem", "AXMenuButton", "AXDisclosureTriangle") or role == "combo_box":
        if pressable or "AXShowMenu" in actions or role == "combo_box":
            add("expand")
    elif pressable:
        add("press")

    if role in _SELECTABLE_ROLES and raw.selected is not None:
        add("select")
    if role == "tree_item" and raw.expanded is not None and raw.expandable is not False:
        add("expand")
    if raw.value_settable and role in ("text_field", "text_area", "combo_box", "slider"):
        add("set_value")
    if role == "slider" and actions & {"AXIncrement", "AXDecrement"}:
        add("set_value")
    if (role in _TEXT_ROLES and raw.focused is not None) or "AXRaise" in actions:
        add("focus")
    if "AXScrollToVisible" in actions:
        add("scroll_to")

    return role, verbs.list(), states_of(raw, role)


def carries_state_value(native_role: str, subrole: str = "") -> bool:
    """True when AXValue encodes state (checkbox 0/1) and shouldn't show as value."""
    return role_of(native_role, subrole) in ("checkbox", "radio", "tab")
