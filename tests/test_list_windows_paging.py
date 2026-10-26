"""Filter / sort / page behavior for list_windows, and tab selection for
activate_tab — both exercised through their pure helpers (no host/CDP needed)."""

from __future__ import annotations

import json

from conductor_desktop.tools.system import _paginate_windows
from conductor_desktop.tools.web import _select_target


def _wins():
    return json.dumps([
        {"id": "1", "title": "Inbox", "process_name": "Firefox", "pid": 10, "z": 2, "focused": False},
        {"id": "2", "title": "GitHub — Chrome", "process_name": "Google Chrome", "pid": 11, "z": 0, "focused": True},
        {"id": "3", "title": "Menubar", "process_name": "SystemUIServer", "pid": 12, "z": 5, "focused": False},
        {"id": "4", "title": "Docs — Chrome", "process_name": "Google Chrome", "pid": 13, "z": 1, "focused": False},
    ])


def test_match_is_substring_over_title_and_process():
    env = json.loads(_paginate_windows(_wins(), match="chrome", sort="z", desc=False, page=1, page_size=20))
    titles = {w["title"] for w in env["windows"]}
    assert titles == {"GitHub — Chrome", "Docs — Chrome"}  # matches process_name + title
    assert env["total"] == 4 and env["matched"] == 2 and env["returned"] == 2


def test_default_sort_is_z_front_first():
    env = json.loads(_paginate_windows(_wins(), match=None, sort="z", desc=False, page=1, page_size=20))
    assert [w["id"] for w in env["windows"]] == ["2", "4", "1", "3"]  # z = 0,1,2,5


def test_sort_by_title_desc():
    env = json.loads(_paginate_windows(_wins(), match=None, sort="title", desc=True, page=1, page_size=20))
    assert next(w["title"] for w in env["windows"]) == "Menubar"


def test_paging_splits_and_reports_pages():
    p1 = json.loads(_paginate_windows(_wins(), match=None, sort="z", desc=False, page=1, page_size=2))
    p2 = json.loads(_paginate_windows(_wins(), match=None, sort="z", desc=False, page=2, page_size=2))
    assert p1["pages"] == 2 and p1["returned"] == 2 and p1["page"] == 1
    assert p2["page"] == 2 and p2["returned"] == 2
    assert [w["id"] for w in p1["windows"]] == ["2", "4"]
    assert [w["id"] for w in p2["windows"]] == ["1", "3"]


def test_page_clamped_to_last_page():
    env = json.loads(_paginate_windows(_wins(), match=None, sort="z", desc=False, page=99, page_size=2))
    assert env["page"] == env["pages"] == 2


def test_page_size_zero_returns_everything():
    env = json.loads(_paginate_windows(_wins(), match=None, sort="z", desc=False, page=1, page_size=0))
    assert env["returned"] == 4 and env["pages"] == 1


def test_match_no_hits_is_empty_not_error():
    env = json.loads(_paginate_windows(_wins(), match="zzz", sort="z", desc=False, page=1, page_size=20))
    assert env["matched"] == 0 and env["windows"] == []


def test_non_json_passes_through_as_none():
    assert _paginate_windows("not json", match=None, sort="z", desc=False, page=1, page_size=20) is None


def _wins_on_monitors():
    return json.dumps([
        {"id": "1", "title": "Left", "process_name": "Finder", "z": 0, "monitor": 0},
        {"id": "2", "title": "Right", "process_name": "Safari", "z": 1, "monitor": 1},
        {"id": "3", "title": "Right too", "process_name": "Mail", "z": 2, "monitor": 1},
    ])


def test_monitor_filter_keeps_only_that_screen():
    env = json.loads(_paginate_windows(_wins_on_monitors(), match=None, sort="z", desc=False,
                                       page=1, page_size=20, monitor=1))
    assert [w["id"] for w in env["windows"]] == ["2", "3"]
    assert env["total"] == 3 and env["matched"] == 2
    assert "note" not in env


def test_monitor_filter_combines_with_match():
    env = json.loads(_paginate_windows(_wins_on_monitors(), match="mail", sort="z", desc=False,
                                       page=1, page_size=20, monitor=1))
    assert [w["id"] for w in env["windows"]] == ["3"]


def test_monitor_filter_skipped_with_note_when_platform_lacks_monitor():
    # _wins() has no `monitor` field: don't return an empty list that would
    # read as "nothing on that screen".
    env = json.loads(_paginate_windows(_wins(), match=None, sort="z", desc=False,
                                       page=1, page_size=20, monitor=0))
    assert env["matched"] == 4
    assert "screen= ignored" in env["note"]


# ── activate_tab target selection ─────────────────────────────────────────

def _pages():
    return [
        {"id": "a", "type": "page", "title": "Dashboard", "url": "https://dash.cloudflare.com/analytics"},
        {"id": "b", "type": "page", "title": "Linode", "url": "https://cloud.linode.com/dashboard"},
        {"id": "c", "type": "page", "title": "Inbox", "url": "https://mail.google.com/"},
    ]


def test_select_defaults_to_first_page():
    assert _select_target(_pages(), tab=None, match_url=None, match_title=None)["id"] == "a"


def test_select_by_match_url():
    assert _select_target(_pages(), tab=None, match_url="linode.com", match_title=None)["id"] == "b"


def test_select_by_match_title():
    assert _select_target(_pages(), tab=None, match_url=None, match_title="inbox")["id"] == "c"


def test_select_url_and_title_and_combined():
    # title matches 'a' but url forces no overlap → AND yields no match
    assert _select_target(_pages(), tab=None, match_url="linode", match_title="Dashboard") is None


def test_select_by_legacy_tab_substring():
    assert _select_target(_pages(), tab="cloudflare", match_url=None, match_title=None)["id"] == "a"


def test_select_explicit_miss_returns_none():
    assert _select_target(_pages(), tab="nonexistent", match_url=None, match_title=None) is None
