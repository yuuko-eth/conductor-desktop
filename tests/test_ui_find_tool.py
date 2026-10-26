"""ui_find wiring: executor → backend web-search hint, refs, errors; macOS helpers."""

from __future__ import annotations

import re

from conductor_desktop.core.backends.desktop import Capabilities, DesktopBackend
from conductor_desktop.core.executor import ResidentExecutor
from conductor_desktop.core.models.host_node import HostNode
from conductor_desktop.core.ui.model import UINode, UIRead


def _read():
    win = UINode("window", "AXWindow", name="Doc", children=[
        UINode("group", "AXGroup", path=(0,), children=[
            UINode("button", "AXButton", name="Save", verbs=["press"], path=(0, 0)),
        ]),
        UINode("checkbox", "AXCheckBox", name="Wrap", verbs=["toggle"], states=["checked"], path=(1,)),
    ])
    return UIRead(app="TextEdit", pid=42, window_id=9, window_title="Doc", window=win, nodes=4)


class _Backend(DesktopBackend):
    caps = Capabilities(ui=True)

    def __init__(self):
        self.calls = []

    async def ui_walk(self, title=None, depth=8, max_children=50, include_menubar=False,
                      timeout=10.0, web_search=None):
        self.calls.append({"title": title, "depth": depth, "web_search": web_search,
                           "include_menubar": include_menubar, "timeout": timeout})
        return _read()


def _executor(backend):
    ex = ResidentExecutor(HostNode(uuid="host-1", label="test-host", host_type="resident"))
    ex._desktop = backend
    return ex


async def test_text_search_passes_the_web_hint_and_walks_deeper():
    backend = _Backend()
    await _executor(backend).ui_find("Save", role="button", title="Doc", timeout=4.0)
    assert backend.calls == [{"title": "Doc", "depth": 16, "include_menubar": False, "timeout": 4.0,
                              "web_search": {"role": "button", "text": "Save"}}]


async def test_name_takes_precedence_as_web_text():
    backend = _Backend()
    await _executor(backend).ui_find("x", name="Save")
    assert backend.calls[0]["web_search"] == {"role": None, "text": "Save"}


async def test_state_only_search_skips_the_web_hint():
    backend = _Backend()
    await _executor(backend).ui_find(states=["checked"])
    assert backend.calls[0]["web_search"] is None


async def test_results_carry_refs_that_resolve_to_native_paths():
    ex = _executor(_Backend())
    result = await ex.ui_find("save")
    line = result.output.splitlines()[1]
    ref = re.search(r"\[(e\d+)\]", line).group(1)
    assert line.startswith('1. button "Save"') and ex._ui_refs.get(ref).path == (0, 0)


async def test_unnamed_result_still_gets_a_ref():
    result = await _executor(_Backend()).ui_find(role="group")
    assert re.search(r"1\. group \[e\d+\]", result.output)


async def test_errors():
    ex = _executor(_Backend())
    assert "semantic matching" in (await ex.ui_find("x", match="semantic")).error
    assert "give a query" in (await ex.ui_find()).error
    unsupported = await _executor(DesktopBackend()).ui_find("x")
    assert unsupported.error.startswith("unsupported")


def test_tool_is_registered_and_read_only(surface, kind):
    assert "ui_find" in surface()
    assert kind("ui_find") == "observing"


# --- macOS web-search helpers (fakes, no pyobjc) ----------------------------

def test_search_key_mapping():
    from conductor_desktop.core.backends.macos import _ax_search_key
    assert _ax_search_key("link") == "AXLinkSearchKey"
    assert _ax_search_key("Button") == "AXButtonSearchKey"
    assert _ax_search_key("button,link") == "AXAnyTypeSearchKey"
    assert _ax_search_key(None) == "AXAnyTypeSearchKey"


_PARENT = {"link": "p", "p": "section", "section": "web", "web": "window"}
_CHILDREN = {"web": ["banner", "section"], "section": ["h1", "p"], "p": ["text", "em", "link"]}


def _copy_attr(el, attr, _):
    if attr == "AXParent":
        return (0, _PARENT.get(el))
    if attr == "AXChildren":
        return (0, _CHILDREN.get(el))
    return (0, None)


def test_path_is_rebuilt_from_parents():
    from conductor_desktop.core.backends.macos import _ax_path_to
    assert _ax_path_to("link", "web", _copy_attr) == (1, 1, 2)


def test_path_is_none_outside_the_ancestor():
    from conductor_desktop.core.backends.macos import _ax_path_to
    assert _ax_path_to("link", "elsewhere", _copy_attr) is None
    assert _ax_path_to("link", "web", _copy_attr, max_up=2) is None
