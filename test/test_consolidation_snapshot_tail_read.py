"""A consolidation snapshot decodes only the rows after its marker.

The consolidator asks for the unconsolidated tail of a transcript. The snapshot
reads that tail through the transcript's row index, so a long transcript with a
short tail is not parsed whole. These tests check it returns exactly what the
whole-file read returns, and falls back to that read when the index cannot
frame the file.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.history import ConversationLog
from kiro_crew.history_projection_reads import TranscriptReadProjection

KEY = "dashboard:snapshot-tail"


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    return ConversationLog(base_dir=tmp_path / "sessions")


def _write(log: ConversationLog, rows: list[bytes], marker: int):
    path = log._path(KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"_type": "metadata", "created_at": "2026-11-01T00:00:00", "last_consolidated": marker}
    path.write_bytes(json.dumps(meta).encode() + b"\n" + b"".join(rows))
    return path


def _rows(n: int) -> list[bytes]:
    out = []
    for i in range(n):
        out.append(
            json.dumps({"role": "user", "content": f"m{i}", "ts": f"t{i:05d}"}).encode() + b"\n"
        )
        if i % 7 == 0:
            out.append(b"\n")  # blank lines are not rows
        if i % 11 == 0:
            out.append(b"not json\n")  # neither are unparseable lines
    return out


def _full_snapshot(log: ConversationLog):
    messages = log._read_messages(KEY)
    meta = log._read_metadata(KEY)
    offset = int(meta.get("last_consolidated", 0))
    return list(messages[offset:]), len(messages)


@pytest.mark.parametrize("marker", [0, 1, 250, 599, 600, 900])
def test_the_tail_read_matches_the_whole_file_read(log, monkeypatch, marker):
    _write(log, _rows(600), marker)
    expected_rows, expected_total = _full_snapshot(log)
    log._msg_cache.clear()
    calls: list[str] = []
    real = TranscriptReadProjection._read_messages

    def spy(self, key):
        calls.append(key)
        return real(self, key)

    monkeypatch.setattr(TranscriptReadProjection, "_read_messages", spy)

    rows, total, generation, sub_offset = log.snapshot_for_consolidation(KEY)

    assert (rows, total) == (expected_rows, expected_total)
    assert (generation, sub_offset) == (0, 0)
    assert calls == []  # the whole file was not parsed


def test_a_file_the_index_cannot_frame_takes_the_whole_file_read(log, monkeypatch):
    _write(log, _rows(20), 5)
    expected = _full_snapshot(log)
    log._msg_cache.clear()
    monkeypatch.setattr(TranscriptReadProjection, "read_rows_from", lambda self, key, start: None)

    rows, total, _generation, _sub = log.snapshot_for_consolidation(KEY)

    assert (rows, total) == expected


def test_a_missing_transcript_is_empty(log):
    assert log.snapshot_for_consolidation(KEY)[:2] == ([], 0)


def test_an_os_error_in_the_index_takes_the_whole_file_read(log, monkeypatch):
    _write(log, _rows(20), 5)
    expected = _full_snapshot(log)
    log._msg_cache.clear()
    real = TranscriptReadProjection._row_index
    failures = [PermissionError("denied once")]

    def flaky(self, key):
        if failures:
            raise failures.pop()
        return real(self, key)

    monkeypatch.setattr(TranscriptReadProjection, "_row_index", flaky)

    rows, total, _generation, _sub = log.snapshot_for_consolidation(KEY)

    assert (rows, total) == expected
    assert failures == []  # the index read did fail
