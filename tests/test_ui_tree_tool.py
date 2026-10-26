"""ui_tree wiring: executor → backend, refs, errors, registration, macOS targeting."""

from __future__ import annotations

import json

import pytest

from conductor_desktop.core.backends.desktop import Capabilities, DesktopBackend
from conductor_desktop.core.executor import ResidentExecutor
from conductor_desktop.core.models.host_node import HostNode
from conductor_desktop.core.ui.model import UINode, UIRead


def _read(window=True):
    win = UINode("window", "AXWindow", name="Doc", children=[
        UINode("button", "AXButton", name="Save", verbs=["press"], bounds=(1, 2, 3, 4), path=(0,)),
    ]) if window else None
    return UIRead(app="TextEdit", pid=42, window_id=9, window_title="Doc", window=win, nodes=2)


class _Backend(DesktopBackend):
    caps = Capabilities(ui=True)

    def __init__(self, make_read):
        self.make_read = make_read
        self.calls = []

    async def ui_walk(self, title=None, depth=8, max_children=50, include_menubar=False, timeout=10.0):
        self.calls.append((title, depth, max_children, include_menubar, timeout))
        return self.make_read()


def _executor(backend):
    ex = ResidentExecutor(HostNode(uuid="host-1", label="test-host", host_type="resident"))
    ex._desktop = backend
    return ex


async def test_unsupported_without_a_walker():
    result = await _executor(DesktopBackend()).ui_tree()
    assert not result.success and result.error.startswith("unsupported")


async def test_knobs_reach_the_backend():
    backend = _Backend(_read)
    await _executor(backend).ui_tree("Doc", depth=3, max_children=7, include_menubar=True, timeout=5.0)
    assert backend.calls == [("Doc", 3, 7, True, 5.0)]


async def test_outline_assigns_refs_stable_across_reads():
    ex = _executor(_Backend(_read))
    first = await ex.ui_tree()
    second = await ex.ui_tree()
    assert first.success and first.output == second.output
    assert '  button "Save" [e2] press @1,2,3,4' in first.output.splitlines()
    assert ex._ui_refs.get("e2").path == (0,)


async def test_json_format():
    result = await _executor(_Backend(_read)).ui_tree(format="json")
    assert json.loads(result.output)["window"]["children"][0]["ref"] == "e2"


async def test_no_match_is_an_error():
    result = await _executor(_Backend(lambda: None)).ui_tree("nope")
    assert not result.success and result.error == "no app or window matches 'nope'"


async def test_app_without_window_is_an_error():
    result = await _executor(_Backend(lambda: _read(window=False))).ui_tree()
    assert not result.success and "has no open window" in result.error


def test_tool_is_registered_and_read_only(surface, kind):
    names = surface()
    assert "ui_tree" in names
    assert kind("ui_tree") == "observing"


# --- macOS target selection (fakes, no pyobjc) ------------------------------

class _App:
    def localizedName(self):
        return "Terminal"

    def processIdentifier(self):
        return 579


class _Workspace:
    @classmethod
    def sharedWorkspace(cls):
        return cls()

    def runningApplications(self):
        return [_App()]

    def activeApplication(self):
        return {"NSApplicationProcessIdentifier": 579}


def _copy_attr(el, attr, _):
    if el == 579 and attr == "AXFocusedWindow":
        return (0, "focused-window")
    if el == 579 and attr == "AXWindows":
        return (0, ["maintenance — -zsh", "opensource — -zsh"])
    if attr == "AXTitle" and isinstance(el, str):
        return (0, el)
    return (0, None)


class _TwoApps:
    """Terminal (579) lists a window containing "alice" before Finder (603)'s exact "alice"."""

    WINDOWS = {579: ["alice@devbox: ~ — -zsh"], 603: ["doc-space", "alice"]}

    @classmethod
    def sharedWorkspace(cls):
        return cls()

    def runningApplications(self):
        class App:
            def __init__(self, name, pid):
                self._name, self._pid = name, pid

            def localizedName(self):
                return self._name

            def processIdentifier(self):
                return self._pid

        return [App("Terminal", 579), App("Finder", 603)]


def _two_apps_copy(el, attr, _):
    if attr == "AXWindows":
        return (0, _TwoApps.WINDOWS.get(el))
    if attr == "AXTitle" and isinstance(el, str):
        return (0, el)
    return (0, None)


def test_exact_window_title_beats_an_earlier_substring():
    from conductor_desktop.core.backends.macos import _find_ax_window
    assert _find_ax_window("alice", lambda pid: pid, _two_apps_copy, _TwoApps) == "alice"
    assert _find_ax_window("ALICE@dev", lambda pid: pid, _two_apps_copy, _TwoApps) == "alice@devbox: ~ — -zsh"


def _target(title):
    from conductor_desktop.core.backends.macos import _ui_target
    return _ui_target(title, lambda pid: pid, _copy_attr, lambda win, _: (0, 579), _Workspace)


@pytest.mark.parametrize("title, expected", [
    ("term", (579, 579, "focused-window")),       # app name → its focused window
    ("opensource", (579, 579, "opensource — -zsh")),  # window title → that window
    (None, (579, 579, "focused-window")),          # frontmost app
    ("nope", None),
])
def test_ui_target(title, expected):
    assert _target(title) == expected
