from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import history_projection
from kiro_crew.history import ConversationLog
from kiro_crew.jsonl_util import bounded_raw_records_with_offsets


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
        records = list(bounded_raw_records_with_offsets(handle, path))

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


def test_warm_page_decodes_only_page_plus_one_index_stride(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:large"
    _write_transcript(log, key, 10_000)
    log.read_messages_chained_page(key, limit=100)

    real = history_projection.bounded_raw_records_with_offsets
    reads: list[int] = []

    def counting_records(*args, **kwargs):
        reads.append(0)
        at = len(reads) - 1
        for record in real(*args, **kwargs):
            reads[at] += 1
            yield record

    monkeypatch.setattr(history_projection, "bounded_raw_records_with_offsets", counting_records)
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

    real = history_projection.bounded_raw_records_with_offsets
    reads: list[int] = []

    def counting_records(*args, **kwargs):
        reads.append(0)
        at = len(reads) - 1
        for record in real(*args, **kwargs):
            reads[at] += 1
            yield record

    monkeypatch.setattr(history_projection, "bounded_raw_records_with_offsets", counting_records)
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
        page, indexed = real_once(*args, **kwargs)
        stale = [
            (chained_key, index._replace(stamp=(0, 0, 0, 0))) for chained_key, index in indexed
        ]
        return page, stale

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

    monkeypatch.setattr(history_projection, "bounded_raw_records_with_offsets", fail_scan)

    with pytest.raises(OSError, match="transient read failure"):
        log.read_messages_chained_page(key, limit=3)
    assert key not in log._page_index_cache


def test_off_loop_full_read_warms_first_page_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = ConversationLog(base_dir=tmp_path)
    key = "dashboard:restore-warm"
    _write_transcript(log, key, 10_000)

    real = history_projection.bounded_raw_records_with_offsets
    reads: list[int] = []

    def counting_records(*args, **kwargs):
        reads.append(0)
        at = len(reads) - 1
        for record in real(*args, **kwargs):
            reads[at] += 1
            yield record

    monkeypatch.setattr(history_projection, "bounded_raw_records_with_offsets", counting_records)
    assert len(log.read_messages_chained(key)) == 10_000
    assert reads == [], "the authoritative full parse must not scan the file twice"
    entry = log._page_index_cache.get(key)
    assert entry is not None and entry.row_count == 10_000

    page = log.read_messages_chained_page(key, limit=100)

    assert len(page.messages) == 100
    assert reads
    assert max(reads) <= 100 + history_projection._TRANSCRIPT_PAGE_INDEX_STRIDE + 1
