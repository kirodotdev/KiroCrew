from __future__ import annotations

import functools
import json
import os
import sys
import time
from pathlib import Path

import pytest

from kiro_crew import history_projection
from kiro_crew.history import ConversationLog
from kiro_crew.jsonl_util import (
    OversizedRecord,
    SplitlinesBoundaryRecord,
    strict_raw_records_with_offsets,
)


def _write_transcript(
    log: ConversationLog,
    key: str,
    count: int,
    *,
    tab_id: str | None = None,
    start: int = 0,
) -> None:
    path = log._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"_type": "metadata", "created_at": "2026-09-03T00:00:00+00:00"}
    if tab_id is not None:
        metadata["tab_id"] = tab_id
    lines = [json.dumps(metadata)]
    lines.extend(
        json.dumps(
            {
                "role": "user" if row % 2 == 0 else "assistant",
                "content": f"message-{row}",
                "ts": f"2026-09-03T00:00:{row % 60:02d}+00:00",
            }
        )
        for row in range(start, start + count)
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log._invalidate_cache(key)
    log.invalidate_tab_id_cache()


def test_offset_reader_reports_exact_universal_newline_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"a":1}\r\n{"a":2}\r{"a":3}\n{"a":4}')

    with path.open("rb") as handle:
        records = list(strict_raw_records_with_offsets(handle, path))

    assert records == [
        (0, 9, b'{"a":1}\r\n'),
        (9, 17, b'{"a":2}\r'),
        (17, 25, b'{"a":3}\n'),
        (25, 32, b'{"a":4}'),
    ]
    for start, end, raw in records:
        assert path.read_bytes()[start:end] == raw


def test_chained_page_matches_full_projection(tmp_path: Path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    _write_transcript(log, "dashboard:chat-a", 120, tab_id="shared-tab", start=0)
    _write_transcript(log, "dashboard:chat-b", 140, tab_id="shared-tab", start=120)

    expected = log.read_messages_chained("dashboard:chat-b")
    newest = log.read_messages_chained_page("dashboard:chat-b", limit=100)
    older = log.read_messages_chained_page("dashboard:chat-b", limit=100, before=newest.next_before)
    oldest = log.read_messages_chained_page("dashboard:chat-b", limit=100, before=older.next_before)

    assert newest.total == 260
    assert newest.has_more is True
    assert newest.next_before == 160
    assert older.next_before == 60
    assert oldest.next_before == 0
    assert oldest.has_more is False
    assert oldest.messages + older.messages + newest.messages == expected


def test_page_index_updates_after_append(tmp_path: Path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:append"
    _write_transcript(log, key, 20)
    first = log.read_messages_chained_page(key, limit=5)
    assert [row["content"] for row in first.messages] == [f"message-{i}" for i in range(15, 20)]

    log.append(key, "assistant", "new-tail")
    second = log.read_messages_chained_page(key, limit=5)

    assert second.total == 21
    assert [row["content"] for row in second.messages] == [
        "message-16",
        "message-17",
        "message-18",
        "message-19",
        "new-tail",
    ]


def test_missing_transcript_file_is_a_stable_empty_revision(tmp_path: Path) -> None:
    """A key with no file (a slot that has not flushed) pages as total=0, no retry."""
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:never-flushed"
    assert not log._path(key).exists()

    page = log.read_messages_chained_page(key, limit=10)

    assert page.total == 0
    assert page.messages == []
    assert page.has_more is False
    assert page.revision == ((key, None, log._cache_gen(key)),)
    # The revision pins across reads, so multi-range composition stays valid too.
    again = log.read_messages_chained_page(key, limit=10, expected_revision=page.revision)
    assert again.total == 0
    assert log.read_messages_chained(key) == []


def test_rowless_chain_falls_back_to_the_key_like_the_full_reader(tmp_path: Path) -> None:
    """A tab chain none of whose members yields a row is served from the key itself.

    ``read_messages_chained`` ends in ``messages or _read_messages(key)``; the paged
    reader mirrors that terminal fallback so both report the same total.
    """
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:chat-live"
    _write_transcript(log, key, 12, tab_id="stale-tab")
    projection = log._read_projection
    # A stale chain index: the tab maps to an emptied member and not to *key*.
    _write_transcript(log, "dashboard:chat-emptied", 0, tab_id="stale-tab")
    with log._lock:
        log._tab_id_index = {"stale-tab": ["dashboard:chat-emptied"]}
    assert projection._chain_keys(key) == ["dashboard:chat-emptied"]

    page = log.read_messages_chained_page(key, limit=5)
    full = log.read_messages_chained(key)

    assert len(full) == 12
    assert page.total == 12
    assert [m["content"] for m in page.messages] == [m["content"] for m in full[-5:]]
    assert page.has_more is True


def test_unstattable_transcript_is_not_an_empty_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a missing file is empty; a stat error propagates for retry/unreadable."""
    file_stamp = history_projection.TranscriptReadProjection._file_stamp
    assert file_stamp(tmp_path / "absent.jsonl") is None

    real_stat = Path.stat
    target = tmp_path / "denied.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    def denied(self: Path, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(PermissionError):
        file_stamp(target)


def test_checkpoint_count_is_capped_and_pages_stay_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retained index is bounded; past the cap the stride doubles."""
    monkeypatch.setattr(history_projection, "_TRANSCRIPT_PAGE_INDEX_STRIDE", 4)
    monkeypatch.setattr(history_projection, "_TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS", 8)
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:capped"
    _write_transcript(log, key, 200)

    log.read_messages_chained_page(key, limit=1)
    entry = log._page_index_cache.get(key)
    assert entry is not None
    assert entry.row_count == 200
    assert len(entry.checkpoints) <= 8
    rows = [row for row, _offset in entry.checkpoints]
    assert rows[0] == 0
    stride = rows[1] - rows[0]
    assert stride > 4 and stride % 4 == 0 and (stride // 4) & (stride // 4 - 1) == 0
    assert all(b - a == stride for a, b in zip(rows, rows[1:]))

    expected = log.read_messages_chained(key)
    walked: list[dict] = []
    before: int | None = None
    while True:
        page = log.read_messages_chained_page(key, limit=7, before=before)
        walked = page.messages + walked
        if not page.has_more:
            break
        before = page.next_before
    assert walked == expected


def test_zero_limit_probe_reports_total_and_decodes_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``limit=0`` is the total/revision probe used by the bounded slot page."""
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:probe"
    _write_transcript(log, key, 300)
    log.read_messages_chained_page(key, limit=1)  # warm the sparse index

    decoded: list[bytes] = []
    original = history_projection.TranscriptReadProjection._message_record

    def counting(raw: bytes):
        decoded.append(raw)
        return original(raw)

    monkeypatch.setattr(
        history_projection.TranscriptReadProjection, "_message_record", staticmethod(counting)
    )
    probe = log.read_messages_chained_page(key, limit=0)

    assert probe.total == 300
    assert probe.messages == []
    assert decoded == []
    with pytest.raises(ValueError):
        log.read_messages_chained_page(key, limit=-1)


def test_splitlines_only_boundary_row_hands_the_read_to_the_full_reader(
    tmp_path: Path,
) -> None:
    """A raw U+2028 inside a row is one record to the framer, two to splitlines()."""
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:u2028"
    _write_transcript(log, key, 5)
    path = log._path(key)
    path.write_bytes(path.read_bytes() + b'{"role": "user", "content": "a\xe2\x80\xa8b"}\n')
    log._invalidate_cache(key)

    with pytest.raises(SplitlinesBoundaryRecord):
        log.read_messages_chained_page(key, limit=2)
    # The authoritative reader still answers; the bounded path defers to it.
    assert len(log.read_messages_chained(key)) == 5


def test_nbsp_padded_row_counts_like_the_full_reader(tmp_path: Path) -> None:
    """``str.strip`` eats NBSP; ``json.loads`` does not. Both readers must agree."""
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:nbsp"
    _write_transcript(log, key, 5)
    path = log._path(key)
    padded = "\u00a0" + json.dumps({"role": "user", "content": "padded"}) + "\u00a0\n"
    path.write_bytes(path.read_bytes() + padded.encode("utf-8"))
    log._invalidate_cache(key)

    page = log.read_messages_chained_page(key, limit=2)
    full = log.read_messages_chained(key)
    assert len(full) == 6
    assert page.total == 6
    assert page.messages[-1]["content"] == "padded"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows reports creation time as st_ctime; the ctime guard is POSIX-only",
)
def test_same_size_rewrite_with_restored_mtime_misses_the_index_cache(
    tmp_path: Path,
) -> None:
    """An external same-size rewrite that restores mtime still changes ctime."""
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:ctime"
    _write_transcript(log, key, 40)
    path = log._path(key)
    before = path.stat()
    assert log.read_messages_chained_page(key, limit=5).total == 40

    # Corrupt one row in place (same byte length), then put mtime back. No
    # ConversationLog API is used, so the invalidation generation does not move.
    data = path.read_bytes()
    target = b'{"role": "user", "content": "message-10"'
    assert data.count(target) == 1
    corrupted = data.replace(target, b'x"role": "user", "content": "message-10"')
    for _attempt in range(200):
        # Filesystem timestamps tick coarsely (ext4: jiffies); wait for a tick
        # so ctime can differ from the stamp taken before the rewrite.
        time.sleep(0.005)
        path.write_bytes(corrupted)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        if path.stat().st_ctime_ns != before.st_ctime_ns:
            break
    after = path.stat()
    assert after.st_ctime_ns != before.st_ctime_ns
    assert (after.st_mtime_ns, after.st_size, after.st_ino) == (
        before.st_mtime_ns,
        before.st_size,
        before.st_ino,
    )

    assert log.read_messages_chained_page(key, limit=5).total == 39


def test_bom_and_surrogate_rows_count_like_the_strict_full_reader(tmp_path: Path) -> None:
    """The index decodes with the same strict UTF-8 as the text-mode full reader."""
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:bom"
    _write_transcript(log, key, 5)
    path = log._path(key)
    # An encoded lone surrogate is accepted by json.loads(bytes) (surrogatepass)
    # but is not valid UTF-8 to a strict decoder.
    path.write_bytes(path.read_bytes() + b'{"role": "user", "content": "\xed\xa0\x80"}\n')
    log._invalidate_cache(key)

    with pytest.raises(UnicodeDecodeError):
        log.read_messages_chained_page(key, limit=2)
    with pytest.raises(UnicodeDecodeError):
        log.read_messages_chained(key)


def test_warm_page_decodes_only_page_plus_one_index_stride(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:large"
    _write_transcript(log, key, 10_000)
    log.read_messages_chained_page(key, limit=100)

    real = history_projection.strict_raw_records_with_offsets
    reads: list[int] = []

    def counting_records(*args, **kwargs):
        reads.append(0)
        at = len(reads) - 1
        for record in real(*args, **kwargs):
            reads[at] += 1
            yield record

    monkeypatch.setattr(history_projection, "strict_raw_records_with_offsets", counting_records)
    page = log.read_messages_chained_page(key, limit=100, before=9_000)

    assert len(page.messages) == 100
    assert reads
    assert max(reads) <= 100 + history_projection._TRANSCRIPT_PAGE_INDEX_STRIDE + 1
    assert len(log._msg_cache) == 0, "bounded paging must not retain the full parsed transcript"


def test_larger_same_inode_rewrite_rebuilds_index_from_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:larger-rewrite"
    _write_transcript(log, key, 1_000)
    log.read_messages_chained_page(key, limit=100)
    inode = log._path(key).stat().st_ino

    _write_transcript(log, key, 1_200, start=5_000)
    assert log._path(key).stat().st_ino == inode

    real = history_projection.strict_raw_records_with_offsets
    reads: list[int] = []

    def counting_records(*args, **kwargs):
        reads.append(0)
        at = len(reads) - 1
        for record in real(*args, **kwargs):
            reads[at] += 1
            yield record

    monkeypatch.setattr(history_projection, "strict_raw_records_with_offsets", counting_records)
    page = log.read_messages_chained_page(key, limit=100)

    assert page.total == 1_200
    assert [row["content"] for row in page.messages] == [
        f"message-{row}" for row in range(6_100, 6_200)
    ]
    assert reads
    assert max(reads) >= 1_200, "a changed revision must rebuild from byte zero"


def test_expected_revision_rejects_between_read_rewrite(tmp_path: Path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:revision"
    _write_transcript(log, key, 20)
    first = log.read_messages_chained_page(key, limit=5)

    _write_transcript(log, key, 8)

    with pytest.raises(history_projection.TranscriptRevisionChanged):
        log.read_messages_chained_page(
            key,
            limit=5,
            expected_revision=first.revision,
        )


def test_fallback_retries_when_chain_changes_after_full_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:chat-fallback-base"
    _write_transcript(log, key, 10, tab_id="fallback-tab")
    projection = log._read_projection

    real_once = projection._read_messages_chained_page_once

    def unstable_page_once(*args, **kwargs):
        page, indexed, keys = real_once(*args, **kwargs)
        stale = [
            (chained_key, index._replace(stamp=(0, 0, 0, 0))) for chained_key, index in indexed
        ]
        return page, stale, keys

    monkeypatch.setattr(projection, "_read_messages_chained_page_once", unstable_page_once)

    real_full = projection.read_messages_chained
    inserted = False

    def full_then_insert(chained_key: str) -> list[dict]:
        nonlocal inserted
        messages = real_full(chained_key)
        if not inserted:
            _write_transcript(
                log,
                "dashboard:chat-fallback-later",
                1,
                tab_id="fallback-tab",
                start=10,
            )
            inserted = True
        return messages

    monkeypatch.setattr(projection, "read_messages_chained", full_then_insert)

    page = log.read_messages_chained_page(key, limit=20)

    assert page.total == 11
    assert [row["content"] for row in page.messages] == [f"message-{row}" for row in range(11)]
    assert [revision_key for revision_key, _stamp, _generation in page.revision] == [
        "dashboard:chat-fallback-base",
        "dashboard:chat-fallback-later",
    ]


def test_index_scan_oserror_is_not_a_valid_empty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:index-io-error"
    _write_transcript(log, key, 3)

    def fail_scan(*_args, **_kwargs):
        raise OSError("transient read failure")

    monkeypatch.setattr(history_projection, "strict_raw_records_with_offsets", fail_scan)

    with pytest.raises(OSError, match="transient read failure"):
        log.read_messages_chained_page(key, limit=3)
    assert key not in log._page_index_cache


def test_invalid_utf8_is_not_a_valid_truncated_page(tmp_path: Path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:index-invalid-utf8"
    _write_transcript(log, key, 300)
    path = log._path(key)
    # Corrupt one byte inside row 250: past the first checkpoint stride and far
    # from the metadata line, so only the row decoder itself can catch it.
    corrupt = path.read_bytes().replace(b'"message-250"', b'"message-2\xff0"', 1)
    assert b"\xff" in corrupt
    path.write_bytes(corrupt)
    log._invalidate_cache(key)

    with pytest.raises(UnicodeDecodeError):
        log.read_messages_chained_page(key, limit=5)
    assert key not in log._page_index_cache
    # The full reader fails closed on the same file rather than skipping the row.
    with pytest.raises(UnicodeDecodeError):
        log.read_messages_chained(key)


def test_oversized_row_aborts_indexed_page_instead_of_undercounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:oversized-row"
    _write_transcript(log, key, 5)
    log.append(key, "assistant", "x" * 2_000)
    log.append(key, "user", "after-big")

    real = history_projection.strict_raw_records_with_offsets
    monkeypatch.setattr(
        history_projection,
        "strict_raw_records_with_offsets",
        functools.partial(real, cap=1_000),
    )

    with pytest.raises(OversizedRecord):
        log.read_messages_chained_page(key, limit=3)
    assert key not in log._page_index_cache
    # The authoritative reader has no per-record cap and still serves every row.
    assert [row["content"] for row in log.read_messages_chained(key)][-2:] == [
        "x" * 2_000,
        "after-big",
    ]
