"""A transcript's metadata line is parsed by one rule, with or without the store.

``history_metadata_line.parse_metadata_line`` is that rule. The conversation store's
cached reader uses it, and so does ``read_metadata_state``, the uncached read a process
uses when it needs one session's record and has not loaded the store: a stdio tool server
reads its session's execution record for a tool description before it loads its handlers.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_mcp_slim_entry import _child_env

from kiro_crew import execution_context, history_metadata_line
from kiro_crew.history import ConversationLog
from kiro_crew.history_files import transcript_stems
from kiro_crew.history_metadata_line import (
    METADATA_LINE_CORRUPT,
    METADATA_LINE_READABLE,
    METADATA_LINE_TRANSIENT,
    parse_metadata_line,
    read_metadata_state,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_RECORD = {"_type": "metadata", "title": "a session"}
_KEY = "dashboard:chat-1"

#: First-line bytes -> what every reader makes of them.
_LINES: dict[str, tuple[bytes, tuple[dict, str]]] = {
    "a metadata record": (json.dumps(_RECORD).encode() + b"\n", (_RECORD, METADATA_LINE_READABLE)),
    "an empty line": (b"\n", ({}, METADATA_LINE_READABLE)),
    "an empty file": (b"", ({}, METADATA_LINE_READABLE)),
    "a row that is not metadata": (b'{"role": "user"}\n', ({}, METADATA_LINE_READABLE)),
    "json that is not an object": (b"[1, 2]\n", ({}, METADATA_LINE_READABLE)),
    "bytes that are not json": (b"{torn\n", ({}, METADATA_LINE_CORRUPT)),
    "bytes that are not utf-8": (b"\xff\xfe{}\n", ({}, METADATA_LINE_CORRUPT)),
}


@pytest.mark.parametrize("case", sorted(_LINES))
def test_the_line_parses_to_its_record_and_state(case: str) -> None:
    raw, expected = _LINES[case]
    assert (
        parse_metadata_line(raw.split(b"\n", 1)[0] + (b"\n" if b"\n" in raw else b"")) == expected
    )


@pytest.mark.parametrize("case", sorted(_LINES))
def test_the_uncached_read_agrees_with_the_store(case: str, tmp_path: Path) -> None:
    raw, expected = _LINES[case]
    log = ConversationLog(tmp_path)
    log._path(_KEY).write_bytes(raw)
    assert read_metadata_state(tmp_path, transcript_stems(_KEY)) == expected
    record, readable = log.get_metadata_status(_KEY)
    state = METADATA_LINE_READABLE if readable else log.metadata_line_state(_KEY)
    assert (record, state) == expected


def test_an_absent_transcript_reads_as_no_record(tmp_path: Path) -> None:
    assert read_metadata_state(tmp_path, transcript_stems(_KEY)) == ({}, METADATA_LINE_READABLE)


def test_a_file_that_never_opens_is_transient_after_the_bounded_tries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ConversationLog(tmp_path)._path(_KEY).write_bytes(json.dumps(_RECORD).encode() + b"\n")
    opens: list[str] = []
    pauses: list[None] = []

    def _held(*_args: object, **_kwargs: object) -> object:
        opens.append("open")
        raise PermissionError("held by another process")

    monkeypatch.setattr(history_metadata_line, "open", _held, raising=False)
    monkeypatch.setattr(
        history_metadata_line, "_pause_between_attempts", lambda: pauses.append(None)
    )
    assert read_metadata_state(tmp_path, transcript_stems(_KEY)) == ({}, METADATA_LINE_TRANSIENT)
    assert len(opens) == history_metadata_line.METADATA_READ_ATTEMPTS
    assert len(pauses) == history_metadata_line.METADATA_READ_ATTEMPTS - 1


def test_a_read_that_succeeds_on_a_retry_returns_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ConversationLog(tmp_path)._path(_KEY).write_bytes(json.dumps(_RECORD).encode() + b"\n")
    attempts: list[int] = []

    def _held_once(*args: object, **kwargs: object) -> object:
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError("held by another process")
        return open(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(history_metadata_line, "open", _held_once, raising=False)
    monkeypatch.setattr(history_metadata_line, "_pause_between_attempts", lambda: None)
    assert read_metadata_state(tmp_path, transcript_stems(_KEY)) == (
        _RECORD,
        METADATA_LINE_READABLE,
    )


def test_the_store_and_the_light_reader_share_the_retry_bounds() -> None:
    from kiro_crew import history

    assert history._METADATA_READ_ATTEMPTS == history_metadata_line.METADATA_READ_ATTEMPTS
    assert history._METADATA_READ_RETRY_SECS == history_metadata_line.METADATA_READ_RETRY_SECS


# ── which file is the session's ─────────────────────────────────────────────


def _record(title: str) -> bytes:
    return json.dumps({"_type": "metadata", "title": title}).encode() + b"\n"


def test_the_light_reader_reads_the_file_the_store_resolves(tmp_path: Path) -> None:
    """A Slack thread logged under its bare timestamp keeps that file until the canonical
    one exists. Both readers follow ``transcript_stems``, canonical first."""
    key = "slack:1700000000.000100"
    log = ConversationLog(tmp_path)
    canonical = log._path(key)
    legacy = tmp_path / "1700000000.000100.jsonl"
    assert canonical != legacy

    legacy.write_bytes(_record("legacy"))
    assert log._path(key) == legacy
    assert read_metadata_state(tmp_path, transcript_stems(key))[0]["title"] == "legacy"

    canonical.write_bytes(_record("canonical"))
    assert log._path(key) == canonical
    assert read_metadata_state(tmp_path, transcript_stems(key))[0]["title"] == "canonical"


# ── the session record, with and without the store ──────────────────────────


def test_the_session_record_is_read_through_the_store_once_it_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import history

    class _Log:
        def get_metadata_status(self, key: str) -> tuple[dict, bool]:
            return {"seen": key}, True

    monkeypatch.setattr(history, "ConversationLog", _Log)
    assert execution_context._session_record_state(_KEY) == (
        {"seen": _KEY},
        METADATA_LINE_READABLE,
    )


def test_a_read_that_failed_is_never_reported_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store diagnoses a failed read with a second read. When that one finds the line
    readable again, the first read's empty record is still not a record: reporting it
    readable would read as "this session declares nothing" to a caller that must refuse."""
    from kiro_crew import history

    class _Log:
        def get_metadata_status(self, key: str) -> tuple[dict, bool]:
            return {}, False

        def metadata_line_state(self, key: str) -> str:
            return METADATA_LINE_READABLE

    monkeypatch.setattr(history, "ConversationLog", _Log)
    assert execution_context._session_record_state(_KEY) == ({}, METADATA_LINE_TRANSIENT)
    with pytest.raises(Exception) as refused:
        execution_context.read_session_execution(_KEY)
    assert (
        execution_context.unavailable_reason(refused.value)
        == execution_context._UNREADABLE_RECORD_REASON
    )


_PROBE = """
import json, sys
from pathlib import Path
from kiro_crew import execution_context
from kiro_crew.history_files import _sessions_dir, transcript_stem

key = "dashboard:chat-1"
path = _sessions_dir() / (transcript_stem(key) + ".jsonl")
path.parent.mkdir(parents=True, exist_ok=True)
absent = execution_context._session_record_state(key)
path.write_bytes(json.dumps({"_type": "metadata", "title": "a session"}).encode() + b"\\n")
present = execution_context._session_record_state(key)
path.write_bytes(b"{torn\\n")
damaged = execution_context._session_record_state(key)
print((absent, present, damaged, "kiro_crew.history" in sys.modules))
"""


def test_the_session_record_is_read_without_loading_the_store(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=_child_env(tmp_path),
        cwd=str(tmp_path),
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    absent, present, damaged, store_loaded = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert absent == ({}, METADATA_LINE_READABLE)
    assert present == (_RECORD, METADATA_LINE_READABLE)
    assert damaged == ({}, METADATA_LINE_CORRUPT)
    assert store_loaded is False
