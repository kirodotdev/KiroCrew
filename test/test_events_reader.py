"""Reader tests: ordering, watermark exactly-once, filters, rotation, torn tail."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.events.kinds import CronRegistered, SessionMessage
from kiro_crew.events.log import EventLog
from kiro_crew.events.reader import EventReader

_TS_A = int(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
_TS_B = int(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _seed(tmp_path: Path) -> EventLog:
    # Two calendar days of appends: a day-A writer then a day-B writer.
    log_a = EventLog(tmp_path, now_ms=lambda: _TS_A)
    assert log_a.emit(CronRegistered(key="cron:a", ts_ms=_TS_A), src="t")
    assert log_a.emit(SessionMessage(key="chat-1", ts_ms=_TS_A + 1, role="user"), src="t")
    log_b = EventLog(tmp_path, now_ms=lambda: _TS_B)
    assert log_b.emit(CronRegistered(key="cron:b", ts_ms=_TS_B), src="t")
    return log_b


def test_reads_all_shards_oldest_first_and_watermark_is_terminal(tmp_path: Path) -> None:
    _seed(tmp_path)
    reader = EventReader(tmp_path)
    items, wm = reader.read_since(None)
    assert [i.shard for i in items] == [
        "2026-08-01.jsonl",
        "2026-08-01.jsonl",
        "2026-08-02.jsonl",
    ]
    assert [i.kind for i in items] == ["cron/registered", "session/message", "cron/registered"]
    again, wm2 = reader.read_since(wm)
    assert again == []
    assert wm2 == wm


def test_empty_log_returns_none_watermark(tmp_path: Path) -> None:
    items, wm = EventReader(tmp_path).read_since(None)
    assert items == []
    assert wm is None


def test_filters_return_subset_but_consume_everything(tmp_path: Path) -> None:
    _seed(tmp_path)
    reader = EventReader(tmp_path)
    items, wm = reader.read_since(None, kind_prefix="cron/")
    assert [i.event.key for i in items] == ["cron:a", "cron:b"]
    # Filtered-out lines were still consumed: nothing left after the watermark.
    rest, _ = reader.read_since(wm)
    assert rest == []

    by_key, _ = reader.read_since(None, key="chat-1")
    assert len(by_key) == 1
    assert by_key[0].kind == "session/message"


def test_limit_resumes_exactly_once(tmp_path: Path) -> None:
    _seed(tmp_path)
    reader = EventReader(tmp_path)
    first, wm = reader.read_since(None, limit=1)
    assert len(first) == 1
    rest, wm2 = reader.read_since(wm)
    assert [i.event.key for i in first + rest] == ["cron:a", "chat-1", "cron:b"]
    final, _ = reader.read_since(wm2)
    assert final == []


def test_pruned_watermark_shard_resumes_from_next_shard(tmp_path: Path) -> None:
    log = _seed(tmp_path)
    reader = EventReader(tmp_path)
    first, wm = reader.read_since(None, limit=2)
    assert len(first) == 2 and wm is not None
    assert wm[0] == "2026-08-01.jsonl"
    # Retention removes the shard the watermark points into.
    deleted = log.prune(retention_days=0)
    assert deleted  # everything is older than a zero-day window
    # Re-write only day B, as if the log moved on after the prune.
    assert log.emit(CronRegistered(key="cron:c", ts_ms=_TS_B + 1), src="t")
    items, _ = reader.read_since(wm)
    keys = [i.event.key for i in items]
    assert "cron:a" not in keys and "chat-1" not in keys  # never re-yielded
    assert keys == ["cron:c"]


def test_torn_tail_line_is_left_for_the_next_read(tmp_path: Path) -> None:
    log = EventLog(tmp_path, now_ms=lambda: _TS_A)
    assert log.emit(CronRegistered(key="cron:a", ts_ms=_TS_A), src="t")
    shard = tmp_path / "2026-08-01.jsonl"
    whole = shard.read_bytes()
    # Simulate a writer mid-line: a second record with no trailing newline yet.
    torn = whole.rstrip(b"\n")  # reuse valid JSON bytes as the torn tail
    shard.write_bytes(whole + torn)

    reader = EventReader(tmp_path)
    items, wm = reader.read_since(None)
    assert len(items) == 1  # torn tail not consumed
    assert wm == ("2026-08-01.jsonl", len(whole))
    # The writer finishes the line; the next incremental read picks it up.
    with open(shard, "ab") as fh:
        fh.write(b"\n")
    more, _ = reader.read_since(wm)
    assert len(more) == 1
    assert more[0].event.key == "cron:a"
