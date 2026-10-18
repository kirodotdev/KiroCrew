"""The bounded page's row index resumes after an append instead of rescanning the file."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import history, history_projection
from kiro_crew.history import ConversationLog

KEY = "dashboard:resume"
ROW_BYTES = 1_000


def _write_rows(log: ConversationLog, count: int, *, start: int = 0) -> None:
    path = log._path(KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-10-01T00:00:00+00:00"})]
    pad = "x" * ROW_BYTES
    lines.extend(
        json.dumps({"role": "tool", "content": f"row-{row} {pad}", "ts": f"t{row:08d}"})
        for row in range(start, start + count)
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log._invalidate_cache(KEY)


class _FramingMeter:
    """Counts the records and bytes the page reader frames from disk."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.records = 0
        self.bytes = 0
        real = history_projection.strict_raw_records_with_offsets

        def metered(*args, **kwargs):
            for start, end, raw in real(*args, **kwargs):
                self.records += 1
                self.bytes += end - start
                yield start, end, raw

        monkeypatch.setattr(history_projection, "strict_raw_records_with_offsets", metered)

    def reset(self) -> None:
        self.records = 0
        self.bytes = 0


@pytest.mark.parametrize("rows", [1_000, 10_000, 50_000])  # about 1, 10 and 50 MB
def test_first_page_after_an_append_reads_the_page_not_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: int
) -> None:
    # Size rotation would archive the head on the append; this test is about the
    # un-rotated file, so keep the 10 MB rotation threshold out of the way.
    monkeypatch.setattr(history, "_SESSION_MAX_BYTES", 10**12)
    log = ConversationLog(base_dir=tmp_path)
    _write_rows(log, rows)
    log.read_messages_chained_page(KEY, limit=100)  # the one full scan this process pays
    meter = _FramingMeter(monkeypatch)

    log.append(KEY, "assistant", "appended")  # drops the cached index, as every write does
    page = log.read_messages_chained_page(KEY, limit=100)

    assert page.total == rows + 1
    assert page.messages[-1]["content"] == "appended"
    assert len(page.messages) == 100
    # The new row, plus the page, plus at most one index stride of lead-in: the same
    # bound at 1 MB and at 50 MB.
    assert meter.records <= 1 + 100 + history_projection._TRANSCRIPT_PAGE_INDEX_STRIDE + 1
    assert meter.bytes < 400_000


def test_resumed_index_is_identical_to_a_fresh_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Small stride and checkpoint cap so the stride doubles during the run.
    monkeypatch.setattr(history_projection, "_TRANSCRIPT_PAGE_INDEX_STRIDE", 2)
    monkeypatch.setattr(history_projection, "_TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS", 8)
    log = ConversationLog(base_dir=tmp_path)
    _write_rows(log, 5)
    projection = log._read_projection
    projection._row_index(KEY)

    for batch in range(1, 40):
        for n in range(batch % 4 + 1):
            log.append(KEY, "user", f"b{batch}-{n}")
        resumed = projection._row_index(KEY)
        fresh_log = ConversationLog(base_dir=tmp_path)  # no retained base
        fresh = fresh_log._read_projection._row_index(KEY)
        assert (resumed.row_count, resumed.checkpoints) == (fresh.row_count, fresh.checkpoints)

    page = log.read_messages_chained_page(KEY, limit=7, before=30)
    assert page.messages == log.read_messages_chained(KEY)[page.next_before : 30]


def test_replaced_file_is_rescanned_even_when_its_tail_bytes_match(tmp_path: Path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    _write_rows(log, 200)
    assert log.read_messages_chained_page(KEY, limit=5).total == 200

    # An atomic replacement: a new inode whose first row is unreadable but whose
    # remaining bytes, up to and past the old end, equal the old file's.
    path = log._path(KEY)
    lines = path.read_bytes().splitlines(keepends=True)
    lines[1] = b"{" + b"?" * (len(lines[1]) - 3) + b"}\n"
    replacement = path.with_name("replacement.jsonl")
    replacement.write_bytes(b"".join(lines) + lines[-1])
    os.replace(replacement, path)
    log._invalidate_cache(KEY)

    page = log.read_messages_chained_page(KEY, limit=5)

    assert page.total == 200  # 199 readable rows + the repeated last row
    assert page.messages == log.read_messages_chained(KEY)[-5:]


def test_unterminated_final_row_is_not_resumed_from(tmp_path: Path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    _write_rows(log, 50)
    path = log._path(KEY)
    with open(path, "ab") as handle:
        handle.write(json.dumps({"role": "user", "content": "half", "ts": "t9"}).encode())
    log._invalidate_cache(KEY)
    assert log.read_messages_chained_page(KEY, limit=3).total == 51

    with open(path, "ab") as handle:
        handle.write(b"\n")
        handle.write(json.dumps({"role": "user", "content": "next", "ts": "t10"}).encode() + b"\n")
    log._invalidate_cache(KEY)
    page = log.read_messages_chained_page(KEY, limit=3)

    assert page.total == 52
    assert [m["content"] for m in page.messages] == ["row-49 " + "x" * ROW_BYTES, "half", "next"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows refuses to replace a file that is open for reading"
)
def test_file_replaced_after_the_resume_check_is_never_indexed_under_the_old_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    _write_rows(log, 100)
    projection = log._read_projection
    projection._row_index(KEY)
    for n in range(5):
        log.append(KEY, "user", f"tail-{n}")

    path = log._path(KEY)
    real_check = projection._handle_extends_base

    def replace_after_check(handle, base):
        verdict = real_check(handle, base)
        # A metadata rewrite lands between the check and the scan: a new inode that
        # is longer and whose first data row is unreadable.
        lines = path.read_bytes().splitlines(keepends=True)
        lines[1] = b"{" + b"?" * (len(lines[1]) - 3) + b"}\n"
        replacement = path.with_name("replacement.jsonl")
        replacement.write_bytes(b"".join(lines) + lines[-1] * 3)
        os.replace(replacement, path)
        return verdict

    monkeypatch.setattr(projection, "_handle_extends_base", replace_after_check)
    projection._row_index(KEY)
    monkeypatch.setattr(projection, "_handle_extends_base", real_check)

    log._invalidate_cache(KEY)
    log.append(KEY, "user", "after-replace")
    resumed = projection._row_index(KEY)
    fresh = ConversationLog(base_dir=tmp_path)._read_projection._row_index(KEY)

    assert (resumed.row_count, resumed.checkpoints) == (fresh.row_count, fresh.checkpoints)


def test_total_probe_hands_a_lost_revision_race_to_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    _write_rows(log, 20)
    projection = log._read_projection
    real_once = projection._read_messages_chained_page_once

    def racing_once(key, *, limit, before):
        result = real_once(key, limit=limit, before=before)
        log.append(KEY, "user", "arrives mid-read")
        return result

    monkeypatch.setattr(projection, "_read_messages_chained_page_once", racing_once)
    reads: list[str] = []
    monkeypatch.setattr(projection, "read_messages_chained", lambda key: reads.append(key) or [])

    with pytest.raises(history_projection.TranscriptRevisionChanged):
        log.read_messages_chained_page(KEY, limit=0)

    assert reads == [], "the probe decodes no rows, so it must not read the transcript"
