"""The KDE backend's KWin-script bridge: what it sends, and what it makes of
what comes back.

A KWin script can't return a value, so the backend reads its ``print()``
output from the journal. These tests stand in for busctl and journalctl and
pin the parts that don't need a running kwin: the reply protocol (nonce,
end marker, truncation) and the mapping from kwin's window objects to the
shape every backend reports. The live half is the smoke matrix on a
Plasma 6 host.
"""

from __future__ import annotations

import json

import pytest

from conductor_desktop.core.backends import linux_kde as kde
from conductor_desktop.core.errors import BackendError

# ── The reply protocol ─────────────────────────────────────────────

def _reply(nonce: str, records: list[tuple[str, dict]], total: int | None = None) -> str:
    lines = ["unrelated journal chatter", f"{nonce}x W not-ours"]
    lines += [f"{nonce} {kind} {json.dumps(obj)}" for kind, obj in records]
    if total is not None:
        lines.append(f"{nonce} END {total}")
    return "\n".join(lines) + "\n"


def test_only_this_call_s_records_are_read():
    # The journal holds every app's output, and a nonce-prefixed line from a
    # *different* nonce must not be mistaken for one of ours.
    text = _reply("NONCE1", [("W", {"id": "a"})], total=1)
    text += "NONCE2 W " + json.dumps({"id": "b"}) + "\nNONCE2 END 1\n"
    records, total = kde._parse_reply(text, "NONCE1")
    assert [r["id"] for r in records] == ["a"]
    assert total == 1


def test_an_unwritten_end_marker_is_not_yet_an_answer():
    # journald ingests kwin's output asynchronously, so a reply that looks
    # short may simply be early; the poll decides, not the parse.
    records, total = kde._parse_reply(_reply("N", [("W", {"id": "a"})]), "N")
    assert (len(records), total) == (1, None)


def test_an_unreadable_record_names_itself():
    with pytest.raises(BackendError, match="can't read"):
        kde._parse_reply("N W {not json\nN END 1\n", "N")


def test_an_unreadable_end_marker_names_itself():
    with pytest.raises(BackendError, match="unreadable end marker"):
        kde._parse_reply("N END not-a-number\n", "N")


def test_records_carry_their_kind():
    text = _reply("N", [("S", {"name": "HDMI-1"}), ("W", {"id": "a"})], total=2)
    records, _ = kde._parse_reply(text, "N")
    assert [r["kind"] for r in records] == ["S", "W"]


# ── Waiting for journald to catch up ───────────────────────────────

def _journal(monkeypatch, pages: list[str]):
    """Serve `pages` to successive journal reads, repeating the last."""
    reads: list[int] = []

    async def fake_journal():
        reads.append(1)
        return pages[min(len(reads) - 1, len(pages) - 1)]

    monkeypatch.setattr(kde, "_journal_text", fake_journal)
    monkeypatch.setattr(kde, "_REPLY_POLL", 0)
    return reads


async def test_the_first_read_usually_has_everything(monkeypatch):
    reads = _journal(monkeypatch, [_reply("N", [("W", {"id": "a"})], total=1)])
    assert len(await kde._await_reply("N")) == 1
    assert len(reads) == 1  # no polling on the fast path


async def test_a_late_line_is_waited_for(monkeypatch):
    _journal(monkeypatch, [
        _reply("N", [("W", {"id": "a"})]),                              # no end marker yet
        _reply("N", [("W", {"id": "a"}), ("W", {"id": "b"})], total=2),  # complete
    ])
    assert [r["id"] for r in await kde._await_reply("N")] == ["a", "b"]


async def test_a_reply_that_never_arrives_is_an_error(monkeypatch):
    # A short window list that looks complete is worse than a failure.
    _journal(monkeypatch, [""])
    monkeypatch.setattr(kde, "_REPLY_TIMEOUT", 0)
    with pytest.raises(BackendError, match="never reached the journal"):
        await kde._await_reply("N")


async def test_a_reply_that_stays_short_is_reported_as_truncated(monkeypatch):
    _journal(monkeypatch, [_reply("N", [("W", {"id": "a"})], total=4)])
    monkeypatch.setattr(kde, "_REPLY_TIMEOUT", 0)
    with pytest.raises(BackendError, match="truncated"):
        await kde._await_reply("N")


@pytest.mark.parametrize("out, want", [("i 0", 0), ("i 7\n", 7), ("i -1", -1)])
def test_script_id_is_read_from_busctl_s_reply(out, want):
    assert kde._script_id(out) == want


def test_an_unreadable_script_id_says_so():
    with pytest.raises(BackendError, match="unexpected reply"):
        kde._script_id("something went wrong")


# ── busctl arguments are data, never options ───────────────────────

def test_the_scripting_calls_put_their_arguments_after_a_separator():
    assert "--" in kde._SCRIPTING
    assert kde._SCRIPTING.index("--") < kde._SCRIPTING.index("call")


# ── kwin's windows, in the shape every backend reports ─────────────

def _records(**overrides) -> list[dict]:
    window = {
        "kind": "W", "id": "uuid-1", "title": "notes.txt — Kate",
        "process_name": "org.kde.kate", "pid": 42, "bounds": [10, 20, 300, 400],
        "state": "normal", "desktop": 0, "focused": True, "output": "HDMI-A-1",
        "z": 1, "layer": "normal", "sticky": False, "modal": False,
        "parent_window_id": None,
    }
    window.update(overrides)
    return [{"kind": "S", "index": 0, "name": "HDMI-A-1", "bounds": [0, 0, 1920, 1080],
             "scale": 1.0, "primary": True}, window]


def test_a_window_is_attributed_to_its_output():
    (win,) = kde._window_dicts(_records(), None)
    assert win["monitor"] == 0
    assert win["bounds"] == [10, 20, 300, 400]
    assert win["z"] == 1


def test_a_window_on_an_unknown_output_has_no_monitor():
    (win,) = kde._window_dicts(_records(output="GONE-1"), None)
    assert win["monitor"] == -1


@pytest.mark.parametrize("modal, parent, modality", [
    (False, None, None),
    # EWMH semantics, the same ones linux_x11 reports.
    (True, None, "app"),
    (True, "uuid-parent", "window"),
])
def test_modality_follows_the_transient_parent(modal, parent, modality):
    (win,) = kde._window_dicts(_records(modal=modal, parent_window_id=parent), None)
    assert win["modality"] == modality
    assert win["parent_window_id"] == parent


def test_a_window_on_every_desktop_belongs_to_no_single_one():
    # kwin reports an empty desktop list for a sticky window; the script sends
    # -1, and a workspace index of None is what the scene graph expects.
    (win,) = kde._window_dicts(_records(desktop=-1, sticky=True), None)
    assert win["desktop"] == -1
    assert win["workspace"] == {"global_index": None, "per_output": None}
    assert win["sticky"] is True


def test_the_desktop_filter_selects_one_virtual_desktop():
    records = [*_records(), dict(_records()[1], id="uuid-2", desktop=1)]
    assert [w["id"] for w in kde._window_dicts(records, None)] == ["uuid-1", "uuid-2"]
    assert [w["id"] for w in kde._window_dicts(records, 1)] == ["uuid-2"]
    assert kde._window_dicts(records, 3) == []


def test_screens_come_back_in_the_common_shape():
    (screen,) = kde._screen_dicts(_records())
    assert screen == {"index": 0, "name": "HDMI-A-1", "bounds": [0, 0, 1920, 1080],
                      "scale": 1.0, "primary": True}


# ── focus_window matches like every other backend ──────────────────

@pytest.fixture
def windows(monkeypatch):
    listed = [
        {"id": "uuid-a", "title": "notes.txt — Kate", "pid": 42},
        {"id": "uuid-b", "title": "Wi-Fi — System Settings", "pid": 99},
    ]

    async def fake_list(desktop=None):
        return listed

    activated: list[str] = []

    async def fake_script(body, timeout=None):
        activated.append(body)
        return [{"kind": "F", "activated": True}]

    monkeypatch.setattr(kde, "list_windows", fake_list)
    monkeypatch.setattr(kde, "_run_kwin_script", fake_script)
    return activated


async def test_focus_matches_a_title_case_insensitively(windows):
    assert await kde.focus_window("KATE") is True
    assert json.dumps("uuid-a") in windows[0]


async def test_focus_matches_a_pid(windows):
    assert await kde.focus_window(pid=99) is True
    assert json.dumps("uuid-b") in windows[0]


async def test_focus_without_a_match_is_false_not_an_error(windows):
    assert await kde.focus_window("nothing here") is False
    assert windows == []  # nothing was run against kwin


async def test_focus_needs_something_to_match_on(windows):
    assert await kde.focus_window() is False
    assert windows == []


async def test_a_window_id_reaches_the_script_as_a_json_literal(windows):
    # The id is interpolated into JavaScript that kwin executes, so it goes in
    # quoted, never raw — the same rule as the script quoting elsewhere.
    await kde.focus_window("Kate")
    assert '"uuid-a"' in windows[0]
    assert "WANT_ID" not in windows[0]
