"""Linux AT-SPI → shared ``ui_*`` vocabulary. Pure tables, no gi import.

An AT-SPI source fills ``RawNode`` with the role name (``"push button"``) as
``native_role`` and ``"multi-line"`` as ``subrole`` for a multi-line text.
``actions`` holds the Action interface's names, lowercased, without the
GActions GTK 4 lists there (``win.close``, ``clipboard.copy``), plus markers
for the other things that decide verbs: ``Selectable``, ``HasPopup``,
``Focusable`` (states) and ``EditableText`` (an editable text interface).
"""

from __future__ import annotations

from .model import RawNode
from .normalize import Verbs, role_table, states_of

_ROLE = {
    "push button": "button",
    "button": "button",
    "toggle button": "button",
    "check box": "checkbox",
    "switch": "checkbox",
    "radio button": "radio",
    "page tab": "tab",
    "link": "link",
    "entry": "text_field",
    "password text": "text_field",
    "text": "text_field",
    "editbar": "text_field",
    "document text": "text_area",
    "terminal": "text_area",
    "label": "static_text",
    "static": "static_text",
    "caption": "static_text",
    "heading": "static_text",
    "paragraph": "static_text",
    "menu": "menu",
    "menu bar": "menu",
    "popup menu": "menu",
    "menu item": "menu_item",
    "check menu item": "menu_item",
    "radio menu item": "menu_item",
    "tearoff menu item": "menu_item",
    "combo box": "combo_box",
    "list": "list",
    "list box": "list",
    "list item": "list_item",
    "table row": "row",
    "table cell": "cell",
    "table column header": "cell",
    "table row header": "cell",
    "column header": "cell",
    "row header": "cell",
    "table": "table",
    "tree table": "table",
    "tree": "tree",
    "tree item": "tree_item",
    "slider": "slider",
    "spin button": "slider",
    "scroll bar": "scroll_area",
    "scroll pane": "scroll_area",
    "frame": "window",
    "window": "window",
    "dialog": "dialog",
    "alert": "dialog",
    "file chooser": "dialog",
    "color chooser": "dialog",
    "font chooser": "dialog",
    "panel": "group",
    "filler": "group",
    "section": "group",
    "grouping": "group",
    "form": "group",
    "viewport": "group",
    "page tab list": "group",
    "layered pane": "group",
    "split pane": "group",
    "internal frame": "group",
    "root pane": "group",
    "landmark": "group",
    "status bar": "group",
    "document frame": "group",
    "document web": "group",
    "image": "image",
    "icon": "image",
    "tool bar": "toolbar",
}

_SUBROLE = {
    ("text", "multi-line"): "text_area",
}

# Action names that press. GTK uses "click", Qt "Press", Chromium "activate"
# or "jump" (links).
PRESS_ACTIONS = ("click", "press", "activate", "jump", "open")
EXPAND_ACTIONS = ("expand or contract", "expand")
# Roles whose press flips a state, so the verb is toggle.
TOGGLE_ROLES = frozenset({"toggle button", "check box", "switch", "check menu item"})

_TEXT_ROLES = frozenset({"text_field", "text_area", "combo_box"})
_SELECTABLE_ROLES = frozenset({"row", "list_item", "tree_item", "cell", "tab"})
_VALUE_ROLES = frozenset({"text_field", "text_area", "combo_box", "slider"})
_SCROLL_ROLES = frozenset({"row", "list_item", "tree_item", "cell", "link"})


role_of = role_table(_ROLE, _SUBROLE)


def normalize(raw: RawNode) -> tuple[str, list[str], list[str]]:
    """``(role, verbs, states)`` for one AT-SPI accessible.

    AT-SPI has one generic action list, so the role decides what a press
    means: on a check box it toggles, on a menu it opens a submenu, on a radio
    button it selects. Selection comes from the ``selectable`` state (the
    parent's Selection interface does it), not from an action.
    """
    role = role_of(raw.native_role, raw.subrole)
    acts = set(raw.actions)
    press = any(a in acts for a in PRESS_ACTIONS)
    verbs = Verbs()
    add = verbs.add

    if "toggle" in acts or (press and raw.native_role in TOGGLE_ROLES):
        add("toggle")
    if ("Selectable" in acts and role in _SELECTABLE_ROLES) or (press and role == "radio"):
        add("select")
    # GTK 3 lists "expand or contract" on every table cell; only an
    # expandable one can.
    expandable = raw.expandable is True and (press or any(a in acts for a in EXPAND_ACTIONS))
    opens_menu = press and (raw.native_role == "menu" or "HasPopup" in acts)
    if expandable or opens_menu:
        add("expand")
    if (press and not ("toggle" in verbs or "select" in verbs)
            and raw.native_role != "menu"):
        add("press")
    if raw.value_settable and role in _VALUE_ROLES:
        add("set_value")
    if role in ("window", "dialog") or ("Focusable" in acts and role in _TEXT_ROLES):
        add("focus")
    if role in _SCROLL_ROLES:
        add("scroll_to")

    return role, verbs.list(), states_of(raw, role)
