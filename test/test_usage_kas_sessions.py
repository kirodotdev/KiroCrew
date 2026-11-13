"""The Usage page's session statistics count KAS session records beside kiro-cli's."""

from __future__ import annotations

import errno
import json
import os
import time
from datetime import datetime
from unittest.mock import patch

import pytest

import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.dashboard.handlers.usage_sessions as usage_sessions
from kiro_crew import platform_compat
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


def test_todays_kas_sessions_reach_the_period_summaries(tmp_path, local_tz):
    local_tz("America/Los_Angeles")
    root, cli = _sessions_root(tmp_path)
    rows = [_kas_row(row["payload"]) for row in _KAS_TURN]
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", rows)
    # The parse's clock reads the instant the rows carry, in the pinned zone.
    frozen = datetime.fromisoformat(_STAMP.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    with (
        patch.object(usage_mod, "_SESSIONS_DIR", cli),
        patch.object(usage_mod, "datetime", _Clock),
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
    assert result["unreadable_session_dirs"] == 0
    assert reads == []
    assert usage_sessions._TRANSCRIPT_COUNTS == {}


def _record_listings(monkeypatch):
    listed: list[str] = []
    real_names = platform_compat.PinnedDirectory.names

    def recording(self):
        listed.append(self.path)
        return real_names(self)

    monkeypatch.setattr(platform_compat.PinnedDirectory, "names", recording)
    return listed


def _refuse_links(path):
    # A link fails here as a link to an untrusted share fails the check on Windows.
    return None if os.path.islink(path) else os.path.realpath(path)


def _outside_with_a_record(tmp_path):
    outside = tmp_path / "outside"
    _write_jsonl(outside / "sess_x" / "messages.jsonl", _KAS_TURN)
    _write_jsonl(outside / "s.jsonl", [{"kind": "Prompt"}])
    return outside, (str(outside), os.path.realpath(outside))


def test_a_linked_record_directory_is_skipped_without_being_listed(tmp_path, monkeypatch):
    root, cli = _sessions_root(tmp_path)
    outside, targets = _outside_with_a_record(tmp_path)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    listed = _record_listings(monkeypatch)

    # With the real path check, the link fails the pin's open.
    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["total_sessions"] == 1
    assert not any(p.startswith(targets) or p.endswith("linked") for p in listed)


def test_a_refused_session_root_is_not_listed(tmp_path, monkeypatch):
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    listed = _record_listings(monkeypatch)

    def validate(path):
        return None if path == str(root) else os.path.realpath(path)

    with (
        patch.object(usage_mod, "_SESSIONS_DIR", cli),
        patch.object(usage_mod, "validate_file_path", side_effect=validate),
    ):
        result = _parse_sessions()

    assert result["total_sessions"] == 0
    assert result["unreadable_session_dirs"] == 1
    assert result["refused_transcripts"] == 0
    assert "error" not in result
    assert not any(p.startswith(str(root / "aaaa")) for p in listed)


def _fail_listing(monkeypatch, path, exc):
    real_names = platform_compat.PinnedDirectory.names

    def failing(self):
        if os.path.realpath(self.path) == os.path.realpath(path):
            raise exc
        return real_names(self)

    monkeypatch.setattr(platform_compat.PinnedDirectory, "names", failing)


def test_an_unreadable_kas_record_directory_counts_as_not_loaded(tmp_path, monkeypatch):
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    _write_jsonl(root / "bbbb" / "sess_b" / "messages.jsonl", _KAS_TURN)
    _fail_listing(monkeypatch, root / "bbbb", PermissionError("denied"))

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["total_sessions"] == 1
    assert result["unreadable_session_dirs"] == 1
    assert result["refused_transcripts"] == 0


@pytest.mark.parametrize("exc", [FileNotFoundError("gone"), NotADirectoryError("now a file")])
def test_a_kas_record_directory_gone_during_the_listing_is_skipped(tmp_path, monkeypatch, exc):
    # Removed, or replaced by a non-directory, between the root listing and its own.
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    _write_jsonl(root / "bbbb" / "sess_b" / "messages.jsonl", _KAS_TURN)
    _fail_listing(monkeypatch, root / "bbbb", exc)

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["total_sessions"] == 1
    assert result["refused_transcripts"] == 0
    assert result["unreadable_session_dirs"] == 0


def test_an_unreadable_session_root_counts_as_not_loaded(tmp_path, monkeypatch):
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    _fail_listing(monkeypatch, root, PermissionError("denied"))

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["total_sessions"] == 0
    assert result["unreadable_session_dirs"] == 1
    assert result["refused_transcripts"] == 0


def test_a_refused_cli_directory_link_is_not_listed(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    outside, targets = _outside_with_a_record(tmp_path)
    cli = root / "cli"
    cli.symlink_to(outside, target_is_directory=True)
    listed = _record_listings(monkeypatch)

    with (
        patch.object(usage_mod, "_SESSIONS_DIR", cli),
        patch.object(usage_mod, "validate_file_path", side_effect=_refuse_links),
    ):
        result = _parse_sessions()

    assert "error" not in result
    assert result["unreadable_session_dirs"] == 1
    assert result["refused_transcripts"] == 0
    assert result["all_time_sessions"] == 0
    assert not any(p.startswith(targets) for p in listed)


def test_a_home_on_an_untrusted_share_counts_as_not_loaded_without_an_error(tmp_path):
    """The path check refuses both session directories of a home on a UNC share.

    The payload then reports the refused directories instead of an ``error``, which
    would make the Usage page drop its billing along with the session counts. They
    count as directories, not transcripts: how many transcripts they hold is unknown.
    """
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(cli / "s1.jsonl", [{"kind": "Prompt"}])
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    refused_dirs = {str(root), str(cli)}

    def validate(path):
        return None if path in refused_dirs else os.path.realpath(path)

    with (
        patch.object(usage_mod, "_SESSIONS_DIR", cli),
        patch.object(usage_mod, "validate_file_path", side_effect=validate),
    ):
        result = _parse_sessions()

    assert "error" not in result
    assert "code" not in result
    assert result["unreadable_session_dirs"] == 2
    assert result["refused_transcripts"] == 0
    assert result["all_time_sessions"] == 0
    assert "today" in result


def test_an_unlistable_cli_directory_keeps_its_error_and_is_not_a_directory_count(
    tmp_path, monkeypatch
):
    root, cli = _sessions_root(tmp_path)
    _fail_listing(monkeypatch, cli, PermissionError("denied"))

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["code"] == "sessions_dir_unreadable"
    assert result["unreadable_session_dirs"] == 0
    assert result["refused_transcripts"] == 0


def test_the_cold_refresh_placeholder_has_the_parsed_shape(tmp_path):
    root, cli = _sessions_root(tmp_path)

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        parsed = _parse_sessions()

    assert usage_sessions._empty_session_summary() == parsed


def test_a_windows_special_directory_is_still_listed(tmp_path, monkeypatch):
    """A Windows cloud-files or dedup directory is a reparse point that is not a link.

    ``pin_directory`` refuses it unless the caller asks to refuse links only, so the
    Usage page would read the KAS store as empty and ``sessions/cli`` as unreadable.
    The pin here stands in for Windows: it refuses every reparse point unless
    ``links_only`` is set, the way the real Windows open does.
    """
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(cli / "s1.jsonl", [{"kind": "Prompt"}])
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    real_pin = platform_compat.pin_directory
    special = {os.path.realpath(root), os.path.realpath(cli)}

    def windows_pin(path, *, links_only=False):
        if os.path.realpath(path) in special and not links_only:
            raise NotADirectoryError(20, "not a real directory", os.fspath(path))
        return real_pin(path, links_only=links_only)

    monkeypatch.setattr(platform_compat, "pin_directory", windows_pin)
    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert "error" not in result
    assert result["total_sessions"] == 2
    assert result["refused_transcripts"] == 0


@pytest.fixture
def windows_cloud_files_pins(monkeypatch):
    """The Windows pin branch, where every directory is a cloud-files directory.

    A cloud-files directory carries a reparse point that is not a link, which the
    default Windows pin refuses and ``links_only=True`` pins. CI hosts cannot
    create such a directory. The stand-in refuses each pin without ``links_only``
    and pins the others: with the real Windows pin on Windows, and elsewhere with
    the POSIX flags, both of which refuse a link.
    """
    import sys

    import kiro_crew.platform_compat_links as links

    calls: list[bool] = []
    real_pin = links.pin_directory

    def pin(path, *, links_only=False):
        calls.append(links_only)
        if not links_only:
            raise NotADirectoryError(errno.ENOTDIR, "not a real directory", os.fspath(path))
        if sys.platform == "win32":
            # os.open cannot open a directory on Windows.
            return real_pin(path, links_only=True)
        return os.open(os.fspath(path), links.pinned_dir_flags())

    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(links, "pin_directory", pin)
    monkeypatch.setattr(platform_compat, "pin_directory", pin)
    return calls


def test_cloud_files_session_directories_are_listed_on_windows(tmp_path, windows_cloud_files_pins):
    root, cli = _sessions_root(tmp_path)
    _write_jsonl(cli / "s.jsonl", [{"kind": "Prompt", "timestamp": _STAMP}])
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert "code" not in result
    assert result["all_time_sessions"] == 2
    assert result["refused_transcripts"] == 0
    assert windows_cloud_files_pins and all(windows_cloud_files_pins)


def test_a_linked_record_directory_is_still_skipped_on_windows(
    tmp_path, monkeypatch, windows_cloud_files_pins
):
    root, cli = _sessions_root(tmp_path)
    outside, targets = _outside_with_a_record(tmp_path)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    _write_jsonl(root / "aaaa" / "sess_a" / "messages.jsonl", _KAS_TURN)
    listed = _record_listings(monkeypatch)

    with patch.object(usage_mod, "_SESSIONS_DIR", cli):
        result = _parse_sessions()

    assert result["all_time_sessions"] == 1
    assert result["refused_transcripts"] == 0
    assert not any(p.startswith(targets) or p.endswith("linked") for p in listed)
