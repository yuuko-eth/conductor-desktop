"""Tests for perception.deltas.windows_observer.

The module imports on every platform (ctypes.windll is only touched on the
observer thread), so the normalization runs here with the user32 and oleacc
helpers patched out: which events become which delta, the top-level and
shell filters, Layer 1, and destroy naming the window it remembered. The live
check is the smoke matrix's `deltas` scenario on a Windows host.
"""

from __future__ import annotations

import asyncio

import pytest

from conductor_desktop.core.perception.deltas import windows_observer as wo

TOP = 0x100      # a top-level app window
CHILD = 0x200    # a control inside it
TRAY = 0x300     # the taskbar

TITLES = {TOP: "notes.txt - Notepad", CHILD: "", TRAY: ""}
CLASSES = {TOP: "Notepad", CHILD: "RichEditD2DPT", TRAY: "Shell_TrayWnd"}
ROOTS = {TOP: TOP, CHILD: TOP, TRAY: TRAY}


class FakeAcc:
    def __init__(self, info=("editable text", "Text editor", "hello", 0x100000, (1, 2, 3, 4))):
        self.info = info
        self.calls = 0

    def describe(self, hwnd, id_object, id_child):
        self.calls += 1
        return self.info


@pytest.fixture
def obs(monkeypatch):
    monkeypatch.setattr(wo, "_window_text", lambda h: TITLES.get(h, ""))
    monkeypatch.setattr(wo, "_window_class", lambda h: CLASSES.get(h, ""))
    monkeypatch.setattr(wo, "_root_of", lambda h: ROOTS.get(h, 0))
    monkeypatch.setattr(wo, "_window_rect", lambda h: (0, 0, 800, 600))
    loop = asyncio.new_event_loop()
    o = wo.WindowsObserver(loop)
    o._acc = FakeAcc()
    yield o
    loop.close()


def norm(o, event, hwnd, id_object=wo.OBJID_WINDOW, id_child=wo.CHILDID_SELF):
    return o._normalize(event, hwnd, id_object, id_child)


def test_foreground_is_window_activated(obs):
    d = norm(obs, wo.EVENT_SYSTEM_FOREGROUND, TOP)
    assert d.kind == "window_state_changed"
    assert d.source == "uia"
    assert d.name == "notes.txt - Notepad"
    assert d.extra == {"cls": "Notepad", "state": "activated"}
    assert d.bbox == (0, 0, 800, 600)


def test_shell_windows_are_dropped(obs):
    assert norm(obs, wo.EVENT_SYSTEM_FOREGROUND, TRAY) is None
    assert norm(obs, wo.EVENT_OBJECT_SHOW, TRAY) is None
    assert norm(obs, wo.EVENT_OBJECT_FOCUS, TRAY, -4, 1) is None
    assert obs._acc.calls == 0


def test_show_only_counts_titled_top_level_windows(obs):
    assert norm(obs, wo.EVENT_OBJECT_SHOW, CHILD) is None
    assert norm(obs, wo.EVENT_OBJECT_SHOW, TOP, id_object=-4) is None
    d = norm(obs, wo.EVENT_OBJECT_SHOW, TOP)
    assert (d.kind, d.role, d.name) == ("node_added", "window", "notes.txt - Notepad")


def test_destroy_names_a_window_it_saw(obs):
    assert norm(obs, wo.EVENT_OBJECT_DESTROY, TOP) is None  # never seen
    norm(obs, wo.EVENT_OBJECT_SHOW, TOP)
    d = norm(obs, wo.EVENT_OBJECT_DESTROY, TOP)
    assert (d.kind, d.name) == ("node_removed", "notes.txt - Notepad")
    assert norm(obs, wo.EVENT_OBJECT_DESTROY, TOP) is None  # forgotten


def test_remembered_windows_are_bounded(obs, monkeypatch):
    monkeypatch.setattr(wo, "_SEEN_WINDOWS_MAX", 3)
    for h in range(10):
        obs._remember(h, f"w{h}")
    assert list(obs._windows) == [7, 8, 9]


def test_focus_reads_the_element(obs):
    d = norm(obs, wo.EVENT_OBJECT_FOCUS, CHILD, -4, 0)
    assert (d.kind, d.role, d.name, d.value) == (
        "focus_changed", "editable text", "Text editor", None)
    assert d.source_id == "notes.txt - Notepad:editable text:Text editor"
    assert d.bbox == (1, 2, 3, 4)


def test_value_change_keeps_short_values_only(obs):
    d = norm(obs, wo.EVENT_OBJECT_VALUECHANGE, CHILD, -4, 0)
    assert d.value == "hello"
    obs._acc.info = ("editable text", "Text editor", "x" * (wo._VALUE_KEEP_MAX + 1), 0, None)
    assert norm(obs, wo.EVENT_OBJECT_VALUECHANGE, CHILD, -4, 0).value is None


def test_state_change_carries_the_state_bits(obs):
    d = norm(obs, wo.EVENT_OBJECT_STATECHANGE, CHILD, -4, 0)
    assert (d.kind, d.value) == ("state_changed", "0x100000")


def test_chrome_value_churn_is_dropped(obs):
    obs._acc.info = ("progress bar", "Copying", "40", 0, None)
    assert norm(obs, wo.EVENT_OBJECT_VALUECHANGE, CHILD, -4, 0) is None
    assert norm(obs, wo.EVENT_OBJECT_NAMECHANGE, CHILD, -4, 0) is None
    assert norm(obs, wo.EVENT_OBJECT_FOCUS, CHILD, -4, 0).kind == "focus_changed"


def test_pointer_and_caret_are_not_elements(obs):
    assert norm(obs, wo.EVENT_OBJECT_NAMECHANGE, CHILD, wo.OBJID_CURSOR, 0) is None
    assert norm(obs, wo.EVENT_OBJECT_FOCUS, CHILD, wo.OBJID_CARET, 0) is None
    assert obs._acc.calls == 0


def test_gone_element_is_dropped(obs):
    obs._acc.info = None
    assert norm(obs, wo.EVENT_OBJECT_FOCUS, CHILD, -4, 0) is None


def test_minimize_and_dialog_and_menu(obs):
    assert norm(obs, wo.EVENT_SYSTEM_MINIMIZESTART, TOP).extra["state"] == "minimized"
    assert norm(obs, wo.EVENT_SYSTEM_MINIMIZEEND, TOP).extra["state"] == "restored"
    assert norm(obs, wo.EVENT_SYSTEM_DIALOGSTART, TOP).role == "dialog"
    assert norm(obs, wo.EVENT_SYSTEM_MENUPOPUPSTART, TOP).role == "menu"


def test_callback_never_raises(obs, monkeypatch):
    def boom(*a):
        raise RuntimeError("element went away")
    monkeypatch.setattr(obs, "_normalize", boom)
    obs._on_event(None, wo.EVENT_OBJECT_FOCUS, CHILD, -4, 0, 0, 0)


def test_hook_ranges_cover_every_mapped_event():
    covered = {e for lo, hi in wo._HOOK_RANGES for e in range(lo, hi + 1)}
    assert set(wo._ELEMENT_KINDS) <= covered
    assert {wo.EVENT_SYSTEM_FOREGROUND, wo.EVENT_OBJECT_SHOW,
            wo.EVENT_OBJECT_DESTROY, wo.EVENT_SYSTEM_DIALOGSTART} <= covered
