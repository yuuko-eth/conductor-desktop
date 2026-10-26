"""ui_* action wiring: executor ref lookup, relocation, window cleanup; macOS window lookup."""

from __future__ import annotations

import sys

import pytest

from conductor_desktop.core.backends.desktop import Capabilities, DesktopBackend
from conductor_desktop.core.executor import ResidentExecutor
from conductor_desktop.core.models.host_node import HostNode
from conductor_desktop.core.ui.act import ActOutcome
from conductor_desktop.core.ui.model import UINode, UIRead
from conductor_desktop.core.ui.refs import Locator

ACT_TOOLS = ["ui_press", "ui_set_value", "ui_toggle", "ui_select", "ui_expand", "ui_focus", "ui_scroll_to"]


class _Backend(DesktopBackend):
    caps = Capabilities(ui=True)

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def ui_walk(self, title=None, depth=8, max_children=50, include_menubar=False,
                      timeout=10.0, web_search=None):
        win = UINode("window", "AXWindow", name="Doc", children=[
            UINode("checkbox", "AXCheckBox", name="Wrap", verbs=["toggle"], path=(0,)),
        ])
        return UIRead(app="TextEdit", pid=42, window_id=9, window_title="Doc", window=win)

    async def ui_act(self, loc, verb, params, timeout=5.0):
        self.calls.append((loc, verb, params))
        return self.outcome


async def _executor_with_ref(outcome):
    ex = ResidentExecutor(HostNode(uuid="host-1", label="test-host", host_type="resident"))
    ex._desktop = _Backend(outcome)
    await ex.ui_tree()
    return ex, "e2"  # window e1, checkbox e2


def _wrap(path=(0,)):
    return UINode("checkbox", "AXCheckBox", name="Wrap", verbs=["toggle"], path=path)


async def test_unknown_ref_is_an_error():
    ex, _ = await _executor_with_ref(ActOutcome("done"))
    result = await ex.ui_act("e999", "press")
    assert not result.success and result.error.startswith("unknown_ref: e999")


async def test_backend_gets_the_locator_and_params():
    ex, ref = await _executor_with_ref(ActOutcome("done", "verified", "now checked", node=_wrap()))
    result = await ex.ui_act(ref, "toggle", {"on": True})
    loc, verb, params = ex._desktop.calls[0]
    assert (loc.pid, loc.window_id, loc.path, verb, params) == (42, 9, (0,), "toggle", {"on": True})
    assert result.success and result.output == 'ui_toggle e2 — checkbox "Wrap" — now checked (verified)'


async def test_moved_element_relocates_the_ref():
    ex, ref = await _executor_with_ref(ActOutcome("done", "verified", "now checked", node=_wrap((3, 1))))
    result = await ex.ui_act(ref, "toggle")
    assert "ref updated" in result.output and ex._ui_refs.get(ref).path == (3, 1)


async def test_closed_window_forgets_its_refs():
    ex, ref = await _executor_with_ref(ActOutcome("stale_ref", detail="its window has closed",
                                                  window_gone=True))
    result = await ex.ui_act(ref, "toggle")
    assert not result.success and result.error.startswith("stale_ref: e2 — its window has closed.")
    assert ex._ui_refs.get(ref) is None


async def test_unsupported_without_an_actor():
    ex = ResidentExecutor(HostNode(uuid="host-1", label="test-host", host_type="resident"))
    assert (await ex.ui_act("e1", "press")).error.startswith("unsupported: ui_press")


def test_tools_are_registered_as_acting(surface, kind):
    names = surface()
    for tool in ACT_TOOLS:
        assert tool in names and kind(tool) == "acting"


def test_action_descriptions_carry_the_shared_contract(surface):
    tools = surface()
    for tool in ACT_TOOLS:
        assert "stale_ref" in tools[tool].description


@pytest.mark.skipif(sys.platform == "win32", reason="macOS helper; os.kill(pid, 0) is a POSIX probe")
def test_pid_alive():
    import os

    from conductor_desktop.core.backends.macos import _pid_alive
    assert _pid_alive(os.getpid())
    assert not _pid_alive(999_999)  # above macOS's pid ceiling


# --- macOS locator root (fakes, no pyobjc) -----------------------------------

def _loc(root="window", window_id=20185, title="Untitled 10"):
    return Locator(pid=591, window_id=window_id, root=root, path=(0,), native_role="AXTextArea",
                   name="", id="", window_title=title)


_ATTRS = {
    ("app", "AXWindows"): ["w1", "w2"],
    ("app", "AXFocusedWindow"): "w2",
    ("app", "AXMenuBar"): "bar",
    ("w2", "AXChildren"): ["toolbar", "sheet"],
    ("sheet", "AXRole"): "AXSheet",
    ("toolbar", "AXRole"): "AXToolbar",
    ("w1", "AXTitle"): "Other",
    ("w2", "AXTitle"): "Untitled 10",
}
_IDS = {"w1": 1, "w2": 20185, "sheet": 20190}


def _copy(el, attr, _):
    return (0, _ATTRS.get((el, attr)))


@pytest.mark.parametrize("loc, expected", [
    (_loc(), "w2"),
    (_loc(window_id=20190), "sheet"),
    (_loc(window_id=None, title="Other"), "w1"),
    (_loc(root="menubar"), "bar"),
    (_loc(window_id=555), None),
])
def test_locator_root(loc, expected):
    from conductor_desktop.core.backends.macos import _ax_locator_root
    assert _ax_locator_root("app", loc, _copy, window_id_of=_IDS.get) == expected


@pytest.mark.parametrize("host_type", ["adb", "wda"])
def test_phone_backends_point_at_the_mobile_tools(host_type):
    from conductor_desktop.core.executor import create_executor

    with pytest.raises(ValueError, match="CONDUCTOR_DESKTOP_MOBILE_"):
        create_executor(HostNode(label="phone", host_type=host_type))
