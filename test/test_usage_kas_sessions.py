"""The Usage page's session statistics count KAS session records beside kiro-cli's."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from unittest.mock import patch

import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.dashboard.handlers.usage_sessions as usage_sessions
from kiro_crew.dashboard.handlers.usage import _parse_sessions

_STAMP = "2026-04-20T12:00:00.000Z"


def _local_day(stamp: str) -> str:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")


def _write_jsonl(path, rows, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _kas_row(payload, stamp=_STAMP):
    return {"id": "row", "timestamp": stamp, "payload": payload}


_KAS_TURN = [
    _kas_row({"type": "agent_note", "content": "context"}),
    _kas_row({"type": "user", "content": "hello"}),
    _kas_row({"type": "turn_start"}),
    _kas_row({"type": "assistant", "operationType": "Reasoning", "content": "..."}),
    _kas_row({"type": "tool_call", "toolName": "read_file"}),
    _kas_row({"type": "tool_result", "content": "file"}),
    _kas_row({"type": "tool_call", "toolName": "read_file"}),
    _kas_row({"type": "tool_result", "content": "file"}),
    _kas_row({"type": "assistant", "operationType": "Say", "content": "done"}),
    _kas_row({"type": "usage_summary"}),
    _kas_row({"type": "turn_end"}),
]


def _sessions_root(tmp_path):
    root = tmp_path / "sessions"
    cli = root / "cli"
    cli.mkdir(parents=True)
    return root, cli


def test_a_kas_record_is_counted_with_its_messages_and_tool_calls(tmp_path):
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(root / "19a97a0a1eda228a" / "sess_a" / "messages.jsonl", _KAS_TURN)

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["total_sessions"] == 1
    assert result["all_time_sessions"] == 1
    # One user prompt and one Say reply; the reasoning row is not a message.
    assert result["total_messages"] == 2
    assert result["total_tool_calls"] == 2
    assert result["refused_transcripts"] == 0
    day = result["daily_history"][0]
    assert day["date"] == _local_day(_STAMP)
    assert (day["sessions"], day["messages"], day["tool_calls"]) == (1, 2, 2)


def test_kas_records_are_counted_when_the_cli_directory_does_not_exist(tmp_path):
    root = tmp_path / "sessions"
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)

    with patch.object(usage_mod, "_SESSIONS_DIR", root / "cli"):
        result = _parse_sessions()

    assert result["total_sessions"] == 1
    assert "error" not in result


def test_both_stores_are_counted_together(tmp_path):
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(
        cli / "s1.jsonl",
        [{"kind": "Prompt"}, {"kind": "AssistantMessage"}, {"kind": "ToolResults"}],
    )
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    _write_jsonl(root / "bbbb" / "sess_b" / "messages.jsonl", _KAS_TURN)

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["total_sessions"] == 3
    assert result["total_messages"] == 6
    assert result["total_tool_calls"] == 5


def test_todays_kas_sessions_reach_the_period_summaries(tmp_path):
    root, cli = _sessions_root(tmp_path)
    now = datetime.now().astimezone()
    stamp = now.isoformat()
    rows = [_kas_row(row["payload"], stamp=stamp) for row in _KAS_TURN]
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", rows)
    # Pin the parse's clock to the same instant the rows carry, so the test does
    # not depend on when it runs relative to a day, week or month boundary.
    frozen = now.replace(tzinfo=None)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    with (
        patch.object(usage_mod, "_SESSIONS_DIR", cli),
        patch("kiro_crew.dashboard.handlers.usage_sessions.datetime", _Clock),
    ):
        result = _parse_sessions()

    for period in ("today", "this_week", "this_month"):
        assert result[period] == {"sessions": 1, "messages": 2, "tool_calls": 2}, period


def test_a_kas_record_without_a_messages_file_is_not_a_failed_load(tmp_path):
    root, cli = _sessions_root(tmp_path)
    record = root / "aaaa" / "sess_a"
    record.mkdir(parents=True)
    (record / "session.json").write_text("{}")

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["total_sessions"] == 0
    assert result["all_time_sessions"] == 0
    assert result["refused_transcripts"] == 0


def test_an_old_kas_record_counts_toward_all_time_only(tmp_path):
    root, cli = _sessions_root(tmp_path)
    old = time.time() - 60 * 86400
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN, mtime=old)

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["all_time_sessions"] == 1
    assert result["total_sessions"] == 0


def test_only_sess_directories_are_read_as_kas_records(tmp_path):
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(root / "aaaa" / ".index" / "messages.jsonl", _KAS_TURN)
    (root / "stray-file").write_text("x")

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["all_time_sessions"] == 0
    assert result["refused_transcripts"] == 0


def test_a_refresh_reads_only_the_transcripts_that_changed(tmp_path, monkeypatch):
    root, cli = _sessions_root(tmp_path)
    unchanged = root / "aaaa" / "sess_a" / "messages.jsonl"
    changed = root / "bbbb" / "sess_b" / "messages.jsonl"
    _write_jsonl(unchanged, _KAS_TURN)
    _write_jsonl(changed, _KAS_TURN)
    monkeypatch.setattr(usage_sessions, "_TRANSCRIPT_COUNTS", {})
    reads: list[str] = []
    real_count = usage_sessions._count_transcript

    def counting(path, kas):
        reads.append(path.parent.name)
        return real_count(path, kas)

    monkeypatch.setattr(usage_sessions, "_count_transcript", counting)
    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        first = _parse_sessions()
        _write_jsonl(changed, _KAS_TURN + [_kas_row({"type": "user", "content": "again"})])
        reads.clear()
        second = _parse_sessions()

    assert reads == ["sess_b"]
    assert first["total_messages"] == 4
    assert second["total_messages"] == 5


def test_a_deleted_transcript_leaves_the_counts_cache(tmp_path, monkeypatch):
    root, cli = _sessions_root(tmp_path)
    record = root / "aaaa" / "sess_a" / "messages.jsonl"
    _write_jsonl(record, _KAS_TURN)
    monkeypatch.setattr(usage_sessions, "_TRANSCRIPT_COUNTS", {})

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        _parse_sessions()
        record.unlink()
        result = _parse_sessions()

    assert result["total_sessions"] == 0
    assert usage_sessions._TRANSCRIPT_COUNTS == {}


def test_a_refused_kas_path_is_not_served_from_the_counts_cache(tmp_path, monkeypatch):
    root, cli = _sessions_root(tmp_path)
    record = root / "aaaa" / "sess_a" / "messages.jsonl"
    _write_jsonl(record, _KAS_TURN)
    monkeypatch.setattr(usage_sessions, "_TRANSCRIPT_COUNTS", {})
    refuse = False

    def validate(path):
        return None if refuse and path.endswith("messages.jsonl") else path

    reads: list[str] = []
    real_count = usage_sessions._count_transcript

    def counting(path, kas):
        reads.append(path.parent.name)
        return real_count(path, kas)

    monkeypatch.setattr(usage_sessions, "_count_transcript", counting)
    with (
        patch.object(usage_mod, "_SESSIONS_DIR", cli),
        patch.object(usage_mod, "validate_file_path", side_effect=validate),
    ):
        assert _parse_sessions()["total_sessions"] == 1
        refuse = True
        reads.clear()
        result = _parse_sessions()

    assert result["total_sessions"] == 0
    assert result["refused_transcripts"] == 1
    assert reads == []
    assert usage_sessions._TRANSCRIPT_COUNTS == {}
