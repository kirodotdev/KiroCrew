"""A damaged session record: what damages it, and what the refusal says.

The chat answered "Execution memory is unavailable: session record is
unreadable" once its transcript's first (metadata) line was damaged. Two
writers could damage it: the metadata rewrite staged its temp file with one
unchecked ``os.write`` (a short write published a PREFIX of the transcript),
and the first append created the file by name and then wrote it. A first line
that was not UTF-8 was also misread as TRANSIENT, so every writer deferred and
none could ever rebuild it, and the refusal read as "try again" for a state no
retry clears. The refusal itself stays: the binding the line held is unknowable.
"""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew import execution_context as execution
from kiro_crew.execution_context import EXECUTION_CONTEXT_KEY, STRICTEST_MEMORY_MODE
from kiro_crew.history import METADATA_LINE_CORRUPT, ConversationLog
from kiro_crew.memory_stores import UnknownMemoryStore

KEY = "dashboard:damaged-record"

#: A torn JSON line (a write cut short), bytes that are not UTF-8, and the NUL
#: run a filesystem can leave after a crash.
DAMAGED_FIRST_LINES = {
    "torn-json": b'{"_type": "metadata", "created_at": "2026-10-2',
    "not-utf8": b"\xff\xfe\x00{not a record",
    "nul-run": b"\x00" * 64,
}


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    execution._LIVE_EXECUTIONS.clear()
    yield ConversationLog()
    execution._LIVE_EXECUTIONS.clear()


def _rows(data: bytes) -> list[bytes]:
    """The rows after the first line, without their line endings.

    Windows appends rows as ``\\r\\n`` and the metadata rewrite writes ``\\n``,
    so the rows are compared by content, not by their line ending.
    """
    rest = data.split(b"\n", 1)[1] if b"\n" in data else b""
    return [line.rstrip(b"\r") for line in rest.split(b"\n") if line.rstrip(b"\r")]


def _with_damaged_first_line(log: ConversationLog, first: bytes) -> list[bytes]:
    """Write a two-row transcript, then replace its first line with *first*."""
    log.append(KEY, "user", "first question")
    log.append(KEY, "assistant", "first answer")
    path = log._path(KEY)
    rows = path.read_bytes().split(b"\n", 1)[1]
    path.write_bytes(first + b"\n" + rows)
    log._meta_cache.clear()
    return _rows(first + b"\n" + rows)


@pytest.mark.parametrize("first", DAMAGED_FIRST_LINES.values(), ids=DAMAGED_FIRST_LINES)
def test_a_damaged_first_line_refuses_with_a_remedy_that_works(log, first) -> None:
    rows = _with_damaged_first_line(log, first)
    assert log.metadata_line_state(KEY) == METADATA_LINE_CORRUPT

    # Still fail-closed: the binding and privacy mode the line held are
    # unknowable, so nothing runs as an ordinary or Global chat in its place.
    # The reason names the remedy rather than a retry: no retry reads these bytes.
    with pytest.raises(UnknownMemoryStore) as refused:
        execution.read_session_execution(KEY)
    reason = execution.unavailable_reason(refused.value)
    assert reason.startswith("session record is unreadable (its first line is damaged)")
    assert "start a new chat" in reason

    # The conversation is untouched by the refusal.
    assert _rows(log._path(KEY).read_bytes()) == rows

    # And the next metadata write rebuilds the line the way every writer
    # rebuilds a corrupt one -- for bytes that are not UTF-8 too, which no
    # writer could do before -- after which the chat reads again, under the
    # strictest mode and with no binding or store.
    assert log.update_metadata_if(KEY, {"title": "kept"}, lambda _meta: True)
    assert execution.read_session_execution(KEY) is None
    metadata, readable = log.get_metadata_status(KEY)
    assert readable and metadata["title"] == "kept"
    assert metadata["memory_mode"] == STRICTEST_MEMORY_MODE
    assert EXECUTION_CONTEXT_KEY not in metadata
    assert "memory_store" not in metadata
    assert _rows(log._path(KEY).read_bytes()) == rows


def test_an_empty_transcript_gets_a_fresh_metadata_line(log) -> None:
    # A zero-byte file (one a crash left between create and write) reads as
    # no lines, so the rewrite writes a new metadata line rather than raising.
    path = log._path(KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    log.update_metadata(KEY, {"title": "fresh"})
    first = json.loads(path.read_bytes().split(b"\n", 1)[0])
    assert first["_type"] == "metadata" and first["title"] == "fresh"
    log._meta_cache.clear()
    assert log.get_metadata_status(KEY)[1]


def test_a_first_line_that_is_not_utf8_is_corrupt_not_transient(log) -> None:
    _with_damaged_first_line(log, DAMAGED_FIRST_LINES["not-utf8"])
    # TRANSIENT tells every writer to defer and retry; no retry decodes these
    # bytes, so the line could never be rewritten and every turn refused.
    assert log.metadata_line_state(KEY) == METADATA_LINE_CORRUPT


def test_a_transient_read_failure_still_refuses(log, monkeypatch) -> None:
    log.append(KEY, "user", "hello")
    log._meta_cache.clear()
    real_open = open
    path = str(log._path(KEY))

    def flaky_open(file, *args, **kwargs):
        if str(file) == path:
            raise PermissionError("locked by another process")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    # A read that may succeed next time is not damage: the plain reason, and
    # the turn refuses fail-closed exactly as before.
    with pytest.raises(UnknownMemoryStore) as refused:
        execution.read_session_execution(KEY)
    assert execution.unavailable_reason(refused.value) == "session record is unreadable"
    monkeypatch.setattr("builtins.open", real_open)
    log._meta_cache.clear()
    assert log.get_metadata_status(KEY)[0].get("memory_mode") != STRICTEST_MEMORY_MODE


def test_a_short_write_does_not_publish_a_truncated_transcript(log, monkeypatch) -> None:
    for index in range(20):
        log.append(KEY, "user", f"row {index} " + "x" * 200)
    before = log._path(KEY).read_bytes()
    real_write = os.write

    def short_write(fd, data):
        # What a nearly full disk does: take part of the buffer, report the count.
        return real_write(fd, bytes(data)[:30])

    from kiro_crew import history_projection

    monkeypatch.setattr(history_projection.os, "write", short_write)
    log.update_metadata(KEY, {"title": "renamed"})
    monkeypatch.setattr(history_projection.os, "write", real_write)

    after = log._path(KEY).read_bytes()
    assert _rows(after) == _rows(before)
    log._meta_cache.clear()
    metadata, readable = log.get_metadata_status(KEY)
    assert readable and metadata["title"] == "renamed"
    assert json.loads(after.split(b"\n", 1)[0])["_type"] == "metadata"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_rewrite_outside_the_data_home_keeps_the_transcript_owner_only(
    tmp_path, monkeypatch
) -> None:
    # A transcript root outside the data home (an eval workspace) under a
    # permissive umask: the rewrite must keep the file owner-only, as its
    # first write made it, rather than take the umask's wider mode.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    outside = tmp_path / "workspace"
    outside.mkdir()
    external = ConversationLog(base_dir=outside)
    previous = os.umask(0o022)
    try:
        external.append(KEY, "user", "a question")
        path = external._path(KEY)
        assert path.stat().st_mode & 0o777 == 0o600
        external.update_metadata(KEY, {"title": "renamed"})
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o600
    external._meta_cache.clear()
    assert external.get_metadata_status(KEY)[0]["title"] == "renamed"


def test_a_create_that_fails_part_way_leaves_no_half_written_record(log, monkeypatch) -> None:
    from types import SimpleNamespace

    from kiro_crew import history

    def dumps(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("_type") == "metadata":
            # Text that cannot be encoded: the write fails after the file is named.
            return '{"_type": "metadata", "agent": "\udc80"}'
        return json.dumps(value, *args, **kwargs)

    # Through the facade, which carries the patch to every owner that binds it.
    monkeypatch.setattr(history, "json", SimpleNamespace(dumps=dumps, loads=json.loads))
    with pytest.raises(UnicodeEncodeError):
        log.append(KEY, "user", "hello")
    # Creating the file by name and then writing it left an empty or cut-short
    # first line behind, which the next append stacked a row after.
    assert not log._path(KEY).exists()
    monkeypatch.setattr(history, "json", json)
    log.append(KEY, "user", "hello")
    assert log.get_metadata_status(KEY)[1]
