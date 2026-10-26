"""Audit log: redaction, day rotation, retention, and failing safe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conductor_desktop.audit import AuditLog, default_dir, redact, scrub_error, summarize


def _lines(directory):
    out = []
    for path in sorted(directory.glob("audit-*.jsonl")):
        out += [json.loads(line) for line in path.read_text().splitlines()]
    return out


# ── redaction ────────────────────────────────────────────────────────


def test_redacted_policy_hides_typed_text_but_keeps_the_target():
    # The whole point: an auditor sees that type_text ran and where a click
    # went, without the log becoming a transcript of typed passwords.
    out = redact({"text": "hunter2", "target": "Save button"}, "redacted")
    assert out["text"] == "<redacted:7 chars>"
    assert out["target"] == "Save button"


@pytest.mark.parametrize("name", ["text", "value", "js", "command", "password", "TOKEN"])
def test_sensitive_argument_names_are_redacted_case_insensitively(name):
    assert redact({name: "s3cret"}, "redacted")[name] == "<redacted:6 chars>"


def test_none_policy_drops_arguments_entirely():
    assert redact({"text": "hunter2", "x": 10}, "none") == {}


def test_full_policy_keeps_values_and_is_the_unsafe_one():
    assert redact({"text": "hunter2"}, "full")["text"] == "hunter2"


def test_numbers_and_bools_survive_every_policy():
    out = redact({"x": 10, "y": 20, "on": True}, "redacted")
    assert out == {"x": 10, "y": 20, "on": True}


def test_long_values_are_capped_not_stored_whole():
    out = redact({"target": "x" * 500}, "redacted")
    assert len(out["target"]) < 200 and out["target"].endswith("…")


def test_nested_dicts_redact_by_inner_key_name():
    out = redact({"params": {"value": "s3cret", "ref": "e21"}}, "redacted")
    assert out["params"]["value"] == "<redacted:6 chars>"
    assert out["params"]["ref"] == "e21"


def test_errors_do_not_repeat_a_redacted_argument():
    err = scrub_error("could not type 'hunter2' into Save", {"text": "hunter2",
                      "target": "Save"}, "redacted")
    assert err == "could not type '<redacted:7 chars>' into Save"


def test_error_scrubbing_reaches_nested_and_list_values():
    kwargs = {"params": {"value": "s3cret"}, "data": ["tok123", 5]}
    err = scrub_error("bad s3cret, bad tok123", kwargs, "none")
    assert "s3cret" not in err and "tok123" not in err


def test_full_policy_and_short_values_leave_the_error_alone():
    assert scrub_error("typed hunter2", {"text": "hunter2"}, "full") == "typed hunter2"
    assert scrub_error("a key failed", {"text": "a"}, "redacted") == "a key failed"


def test_recorded_error_is_scrubbed(tmp_path):
    a = AuditLog(enabled=True, directory=tmp_path)
    a.record(tool="web_eval", klass="acting", ok=False, elapsed=0.1,
             kwargs={"expression": "fetch(SECRET)"},
             error="JS exception in fetch(SECRET): not defined")
    a.close()
    (call,) = [e for e in _lines(tmp_path) if e["kind"] == "call"]
    assert "SECRET" not in call["error"]


# ── writing ──────────────────────────────────────────────────────────


def test_record_writes_one_json_line_per_call(tmp_path):
    a = AuditLog(enabled=True, directory=tmp_path)
    a.record(tool="click", klass="acting", ok=True, elapsed=0.25, kwargs={"x": 1})
    a.record(tool="read_screen", klass="observing", ok=False, elapsed=1.0,
             error="error: no grounder")
    a.close()
    calls = [e for e in _lines(tmp_path) if e["kind"] == "call"]
    assert [c["tool"] for c in calls] == ["click", "read_screen"]
    assert calls[0]["ok"] is True and calls[0]["ms"] == 250.0
    assert calls[1]["error"] == "error: no grounder"
    # Every line carries who wrote it, so several servers can share a file.
    assert all(c["session"] == a.session and "pid" in c for c in calls)


def test_disabled_log_writes_nothing_and_still_accepts_calls(tmp_path):
    a = AuditLog(enabled=False, directory=tmp_path)
    a.start()
    a.record(tool="click", klass="acting", ok=True, elapsed=0.1)
    assert list(tmp_path.glob("*.jsonl")) == []


def test_start_writes_a_session_line(tmp_path):
    a = AuditLog(enabled=True, directory=tmp_path)
    a.start(version="1.2.3", platform="linux-wlroots")
    a.close()
    session = [e for e in _lines(tmp_path) if e["kind"] == "session"]
    assert session and session[0]["platform"] == "linux-wlroots"


def test_file_is_named_for_the_utc_day(tmp_path):
    a = AuditLog(enabled=True, directory=tmp_path)
    a.record(tool="click", klass="acting", ok=True, elapsed=0.1)
    a.close()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (tmp_path / f"audit-{today}.jsonl").exists()


def test_an_unwritable_directory_disables_the_log_instead_of_raising(tmp_path):
    # A tool call must never fail because the audit log couldn't be written.
    blocker = tmp_path / "logs"
    blocker.write_text("I am a file, not a directory")
    a = AuditLog(enabled=True, directory=blocker)
    a.start()
    assert a.enabled is False
    a.record(tool="click", klass="acting", ok=True, elapsed=0.1)  # must not raise


def test_repeated_write_failures_disable_the_log(tmp_path):
    a = AuditLog(enabled=True, directory=tmp_path)

    def boom():
        raise OSError("disk full")

    a._stream = boom  # type: ignore[assignment]
    for _ in range(10):
        a.record(tool="click", klass="acting", ok=True, elapsed=0.1)
    assert a.enabled is False


# ── retention ────────────────────────────────────────────────────────


def _make_day(directory, days_ago: int):
    day = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    path = directory / f"audit-{day}.jsonl"
    path.write_text('{"kind":"call"}\n')
    return path


def test_prune_deletes_files_past_the_retention_window(tmp_path):
    old, recent = _make_day(tmp_path, 40), _make_day(tmp_path, 3)
    removed = AuditLog(enabled=True, directory=tmp_path, retention_days=14).prune()
    assert removed == 1
    assert not old.exists() and recent.exists()


def test_retention_zero_keeps_everything(tmp_path):
    old = _make_day(tmp_path, 400)
    assert AuditLog(enabled=True, directory=tmp_path, retention_days=0).prune() == 0
    assert old.exists()


def test_prune_leaves_files_it_does_not_recognise(tmp_path):
    _make_day(tmp_path, 40)
    stranger = tmp_path / "audit-not-a-date.jsonl"
    stranger.write_text("{}")
    AuditLog(enabled=True, directory=tmp_path, retention_days=14).prune()
    assert stranger.exists()


# ── misc ─────────────────────────────────────────────────────────────


def test_default_dir_is_hidden_and_in_the_user_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_dir() == tmp_path / ".conductor-desktop" / "logs"


def test_summarize_reports_empty_and_populated(tmp_path):
    assert "no audit files" in summarize(tmp_path)
    _make_day(tmp_path, 1)
    assert "1 file(s)" in summarize(tmp_path)
