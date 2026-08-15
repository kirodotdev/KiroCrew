"""Writer tests: sharding, seq, oversized refusal, loop discipline, prune."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiro_crew.events.base import parse
from kiro_crew.events.kinds import CronRegistered, SessionMessage
from kiro_crew.events.log import MAX_LINE_BYTES, EventLog, shard_name_for
from kiro_crew.platform_compat import file_lock

_TS_A = int(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
_TS_B = int(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_emit_routes_by_append_time_not_event_time(tmp_path: Path) -> None:
    # Append-time routing is what keeps the reader's forward-only watermark
    # sound: an old-dated event written today lands in TODAY's shard.
    log = EventLog(tmp_path, now_ms=lambda: _TS_B)
    old_event = CronRegistered(key="cron:a", ts_ms=_TS_A)  # event time = day A
    assert log.emit(old_event, src="test")
    assert (tmp_path / "2026-08-02.jsonl").exists()  # routed by append day B
    assert not (tmp_path / "2026-08-01.jsonl").exists()
    assert shard_name_for(_TS_A) == "2026-08-01.jsonl"


def test_seq_is_monotonic_and_seeded_from_existing_lines(tmp_path: Path) -> None:
    log = EventLog(tmp_path, now_ms=lambda: _TS_A)
    for _ in range(3):
        assert log.emit(CronRegistered(key="cron:a", ts_ms=_TS_A), src="test")
    # A fresh writer instance (new process, conceptually) continues the count.
    log2 = EventLog(tmp_path, now_ms=lambda: _TS_A)
    assert log2.emit(CronRegistered(key="cron:a", ts_ms=_TS_A), src="test")
    lines = (tmp_path / "2026-08-01.jsonl").read_text(encoding="utf-8").splitlines()
    seqs = [json.loads(ln)["seq"] for ln in lines]
    assert seqs == [0, 1, 2, 3]


def test_append_repairs_a_torn_tail_before_writing(tmp_path: Path) -> None:
    log = EventLog(tmp_path, now_ms=lambda: _TS_A)
    assert log.emit(CronRegistered(key="cron:a", ts_ms=_TS_A), src="test")
    shard = tmp_path / "2026-08-01.jsonl"
    # Simulate a writer that died mid-line: strip the trailing newline.
    torn = shard.read_bytes().rstrip(b"\n")
    shard.write_bytes(torn)
    assert log.emit(CronRegistered(key="cron:b", ts_ms=_TS_A), src="test")
    lines = shard.read_bytes().split(b"\n")
    # The torn fragment stayed isolated; the new record did not fuse with it.
    parsed_new = parse(lines[1].decode("utf-8"))
    assert parsed_new is not None and parsed_new.event.key == "cron:b"


def test_clock_rollback_clamps_to_newest_shard(tmp_path: Path) -> None:
    # Day-B writer creates the newer shard; a writer whose clock stepped back
    # to day A must NOT write behind it — forward-only shards keep the
    # reader's watermark sound.
    log_b = EventLog(tmp_path, now_ms=lambda: _TS_B)
    assert log_b.emit(CronRegistered(key="cron:b", ts_ms=_TS_B), src="test")
    log_rollback = EventLog(tmp_path, now_ms=lambda: _TS_A)
    assert log_rollback.emit(CronRegistered(key="cron:late", ts_ms=_TS_A), src="test")
    assert not (tmp_path / "2026-08-01.jsonl").exists()
    lines = (tmp_path / "2026-08-02.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # both records landed in the newest shard


def test_oversized_line_is_refused_and_file_untouched(tmp_path: Path) -> None:
    log = EventLog(tmp_path, now_ms=lambda: _TS_A)
    big = SessionMessage(
        key="chat-1", ts_ms=_TS_A, role="user", agent="a" * (MAX_LINE_BYTES + 1)
    )
    assert log.emit(big, src="test") is False
    assert not (tmp_path / "2026-08-01.jsonl").exists()


def test_emit_never_raises_on_unwritable_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "events"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    log = EventLog(blocker)
    assert log.emit(CronRegistered(key="cron:a", ts_ms=_TS_A), src="test") is False


def test_emit_inside_running_loop_offloads_the_file_write(tmp_path: Path) -> None:
    log = EventLog(tmp_path, now_ms=lambda: _TS_A)
    write_threads: list[int] = []
    original = log._append_line

    def _spy(shard: str, line: str) -> None:
        write_threads.append(threading.get_ident())
        original(shard, line)

    log._append_line = _spy  # type: ignore[method-assign]

    async def _run() -> tuple[bool, int]:
        ok = log.emit(CronRegistered(key="cron:a", ts_ms=_TS_A), src="test")
        return ok, threading.get_ident()

    ok, loop_thread = asyncio.run(_run())
    assert ok is True
    # asyncio.run shuts down the default executor, so the write has landed.
    assert (tmp_path / "2026-08-01.jsonl").exists()
    assert write_threads and write_threads[0] != loop_thread


def test_written_lines_parse_back(tmp_path: Path) -> None:
    log = EventLog(tmp_path, now_ms=lambda: _TS_A)
    event = SessionMessage(key="chat-1", ts_ms=_TS_A, role="assistant", content_chars=42)
    assert log.emit(event, src="unit")
    line = (tmp_path / "2026-08-01.jsonl").read_text(encoding="utf-8").splitlines()[0]
    parsed = parse(line)
    assert parsed is not None
    assert parsed.event == event
    assert parsed.src == "unit"


def test_prune_rejects_negative_retention(tmp_path: Path) -> None:
    (tmp_path / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")
    log = EventLog(tmp_path, now_ms=lambda: _TS_A)
    try:
        log.prune(retention_days=-1)
    except ValueError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("negative retention_days must raise")
    assert (tmp_path / "2020-01-01.jsonl").exists()


def test_invalid_shard_name_cannot_poison_append_routing(tmp_path: Path) -> None:
    # A garbage file that is shard-SHAPED but not a real date must not win
    # the newest-shard clamp (prune rightly refuses to delete it, so it
    # would hijack every append forever).
    (tmp_path / "9999-99-99.jsonl").write_text("junk\n", encoding="utf-8")
    log = EventLog(tmp_path, now_ms=lambda: _TS_A)
    assert log.emit(CronRegistered(key="cron:a", ts_ms=_TS_A), src="test")
    assert (tmp_path / "2026-08-01.jsonl").exists()  # routed by clock, not junk
    junk = (tmp_path / "9999-99-99.jsonl").read_text(encoding="utf-8")
    assert junk == "junk\n"  # untouched


def test_offloaded_emit_routes_by_append_time_clock(tmp_path: Path) -> None:
    # The shard is chosen from the clock INSIDE the writer lock (append
    # time). An emit that runs after midnight must go to the new day's
    # shard, not recreate the old one behind a reader watermark.
    clock = {"now": 1754006400000}  # 2025-08-01 00:00:00 UTC
    log = EventLog(tmp_path, now_ms=lambda: clock["now"])
    assert log.emit(CronRegistered(key="cron:a", ts_ms=clock["now"]), src="t")
    clock["now"] = 1754092800000  # 2025-08-02 00:00:00 UTC
    assert log.emit(CronRegistered(key="cron:b", ts_ms=clock["now"]), src="t")
    assert (tmp_path / "2025-08-01.jsonl").exists()
    assert (tmp_path / "2025-08-02.jsonl").exists()


def test_prune_holds_the_writer_lock(tmp_path: Path) -> None:
    # Prune must serialize with appends via the same cross-process lock;
    # with the lock already held it must block rather than delete.
    (tmp_path / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")
    log = EventLog(tmp_path)
    lock_fd = os.open(tmp_path / ".writer.lock", os.O_CREAT | os.O_WRONLY)
    try:
        with file_lock(lock_fd, exclusive=True, required=True):
            t = threading.Thread(target=lambda: log.prune(retention_days=0))
            t.start()
            t.join(timeout=0.3)
            assert t.is_alive()  # blocked on the writer lock
            assert (tmp_path / "2020-01-01.jsonl").exists()
    finally:
        os.close(lock_fd)
    t.join(timeout=5)
    assert not t.is_alive()
    assert not (tmp_path / "2020-01-01.jsonl").exists()  # proceeded after release


def test_prune_refuses_linked_events_directory(tmp_path: Path) -> None:
    # usage shards share the YYYY-MM-DD.jsonl naming: prune through a link
    # aimed at another store would delete that store's history.
    victim = tmp_path / "usage-tokens"
    victim.mkdir()
    (victim / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")
    linked = tmp_path / "events"
    linked.symlink_to(victim)
    log = EventLog(linked)
    with pytest.raises(ValueError, match="linked"):
        log.prune(retention_days=0)
    assert (victim / "2020-01-01.jsonl").exists()


def test_prune_deletes_only_out_of_window_shards(tmp_path: Path) -> None:
    (tmp_path / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    (tmp_path / f"{today}.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
    (tmp_path / "junk.jsonl").write_text("wrong name shape", encoding="utf-8")
    # Shard-shaped but not a real date: must never be deleted.
    (tmp_path / "0000-00-00.jsonl").write_text("not ours", encoding="utf-8")
    deleted = EventLog(tmp_path).prune(retention_days=30)
    assert deleted == ["2020-01-01.jsonl"]
    assert (tmp_path / f"{today}.jsonl").exists()
    assert (tmp_path / "notes.txt").exists()
    assert (tmp_path / "junk.jsonl").exists()
    assert (tmp_path / "0000-00-00.jsonl").exists()
