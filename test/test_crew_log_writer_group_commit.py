"""Group commit in the crew log writer: one lock, one write and one fsync per run.

A run of a unit's consecutive grouped jobs (:class:`GroupPart`) is written by ONE
``commit`` and acknowledged only once that commit returned. These tests pin the
invariants that must survive the coalescing: no record lost or written twice, no
reordering within a unit, no acknowledgement before the run is durable, and the
loss-marker and refusal rules of the single-job path. They run against REAL session
logs on the test's data home; most drive the passes on the calling thread with an
injected clock, and the concurrency test gives the writer a real one-worker pool.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import emit, emit_runtime
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.crew_log import store_segment_writes
from kiro_crew.crew_log.writer import (
    CrewLogWriter,
    GroupPart,
    WarningBudget,
    WriteJob,
    WriterLimits,
)
from kiro_crew.testing.wait import default_timeout

UNIT = "acp-group-0001"
LOGGER = "kiro_crew.crew_log.writer"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _Home:
    """Real logs, plus a fault hook consulted before every commit and solo append."""

    def __init__(self) -> None:
        self.handles: dict[str, Any] = {}
        self.fault: Any = None
        self.events: list[tuple] = []

    def create(self, unit: str = UNIT) -> None:
        self.handles[unit] = lg.CrewLog.create(
            lg.KIND_SESSION, unit, owner="default", agent="kirocrew"
        )

    def open_unit(self, unit: str) -> Any:
        return self.handles.get(unit)

    def steps(self, unit: str = UNIT) -> list[int]:
        path = lg.crew_log_path(lg.KIND_SESSION, unit)
        with path.open("r", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        return [e["data"]["step"] for e in lines[1:] if e["type"] == "step/started"]

    def types(self, unit: str = UNIT) -> list[str]:
        path = lg.crew_log_path(lg.KIND_SESSION, unit)
        with path.open("r", encoding="utf-8") as fh:
            return [json.loads(line)["type"] for line in fh if line.strip()][1:]

    def _raise_fault(self, steps: list[int]) -> None:
        exc = None if self.fault is None else self.fault(steps)
        if exc is not None:
            try:
                raise exc
            finally:
                exc = None

    def job(
        self,
        n: int,
        unit: str = UNIT,
        *,
        grouped: bool = True,
        data: Any = None,
        after: Any = None,
    ) -> WriteJob:
        body = data if data is not None else {"turn": 1, "step": n}

        def run() -> None:
            self._raise_fault([n])
            self.handles[unit].append("step/started", body, src="gateway")
            self.events.append(("solo", n))

        def commit(items: list[dict]) -> list[Any]:
            steps = [item["data"].get("step") for item in items]
            self._raise_fault(steps)
            entries = self.handles[unit].append_many(items, src="gateway")
            self.events.append(("commit", steps))
            return entries

        def landed(entry: Any) -> None:
            self.events.append(("landed", n, entry.seq))

        part = GroupPart(
            key=("append", "gateway"),
            item={"type": "step/started", "data": body},
            commit=commit,
            landed=landed,
        )
        return WriteJob.append(
            run,
            f"step {n}",
            after=after or (lambda: self.events.append(("after", n))),
            on_drop=lambda: self.events.append(("drop", n)),
            group=part if grouped else None,
        )


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    h = _Home()
    h.create()
    yield h
    h.handles.clear()


def _writer(home: _Home, clock: _Clock, **limits: Any) -> CrewLogWriter:
    logger = logging.getLogger(LOGGER)
    return CrewLogWriter(
        home.open_unit,
        clock=clock,
        sleep=clock.sleep,
        limits=WriterLimits(**limits),
        retry_delay=lambda attempts: 0.0,
        warnings=WarningBudget(logger, clock=clock),
        logger=logger,
    )


def _queue(writer: CrewLogWriter, jobs: list[WriteJob], unit: str = UNIT) -> None:
    """Submit on a running loop, so every job is buffered rather than written inline."""

    async def _run() -> None:
        for job in jobs:
            assert writer.submit(unit, job)

    asyncio.run(_run())


def _commits(home: _Home) -> list[list[int]]:
    return [e[1] for e in home.events if e[0] == "commit"]


def test_a_run_of_grouped_jobs_is_one_commit_and_acks_only_after_it(home):
    clock = _Clock()
    writer = _writer(home, clock)
    _queue(writer, [home.job(n) for n in range(1, 6)])
    assert writer.flush()
    assert _commits(home) == [[1, 2, 3, 4, 5]]
    assert home.steps() == [1, 2, 3, 4, 5]
    # Every landed/after comes after the one commit, in submission order, and each
    # landed precedes its own after.
    tail = home.events[1:]
    assert [e for e in tail if e[0] == "after"] == [("after", n) for n in range(1, 6)]
    for n in range(1, 6):
        assert tail.index(("after", n)) > next(
            i for i, e in enumerate(tail) if e[0] == "landed" and e[1] == n
        )
    seqs = [e[2] for e in home.events if e[0] == "landed"]
    assert seqs == sorted(seqs) and len(set(seqs)) == 5


def test_an_ungrouped_job_is_a_barrier_and_order_holds_across_it(home):
    clock = _Clock()
    writer = _writer(home, clock)
    jobs = [home.job(1), home.job(2), home.job(3, grouped=False), home.job(4), home.job(5)]
    _queue(writer, jobs)
    assert writer.flush()
    assert _commits(home) == [[1, 2], [4, 5]]
    assert ("solo", 3) in home.events
    assert home.steps() == [1, 2, 3, 4, 5]


def test_the_size_cap_splits_a_run_and_a_full_bucket_skips_the_window(home):
    clock = _Clock()
    writer = _writer(
        home, clock, group_max_entries=3, batch_deadline_seconds=5.0
    )  # flake-ok: fake clock
    _queue(writer, [home.job(n) for n in range(1, 8)])
    with writer._lock:
        assert writer._next_pass_delay_locked() == 0.0, "a full bucket waits out the window"
    assert writer.flush()
    # A run of one is the job's own ``run``: there is nothing to coalesce.
    assert _commits(home) == [[1, 2, 3], [4, 5, 6]]
    assert ("solo", 7) in home.events
    assert home.steps() == [1, 2, 3, 4, 5, 6, 7]


def test_a_bucket_below_the_cap_waits_the_window(home):
    clock = _Clock()
    writer = _writer(
        home, clock, group_max_entries=10, batch_deadline_seconds=5.0
    )  # flake-ok: fake clock
    _queue(writer, [home.job(1)])
    with writer._lock:
        assert writer._next_pass_delay_locked() == 5.0


def test_a_failed_commit_retains_the_whole_run_and_lands_it_once(home):
    clock = _Clock()
    writer = _writer(home, clock)
    left = {"n": 2}

    def fault(steps: list[int]) -> BaseException | None:
        if len(steps) > 1 and left["n"] > 0:
            left["n"] -= 1
            return OSError(errno.EIO, "Input/output error")
        return None

    home.fault = fault
    _queue(writer, [home.job(n) for n in range(1, 5)])
    assert writer.flush()
    assert home.steps() == [1, 2, 3, 4], "lost or duplicated across the retries"
    assert [e for e in home.events if e[0] == "after"] == [("after", n) for n in range(1, 5)]
    assert writer.stats().dropped == 0


def test_a_spent_budget_drops_the_run_and_its_marker_leads_the_next_entry(home):
    clock = _Clock()
    writer = _writer(home, clock, max_write_attempts=2)
    home.fault = lambda steps: OSError(errno.EIO, "eio") if 1 in steps else None
    _queue(writer, [home.job(1), home.job(2)])
    writer.flush()
    home.fault = None
    _queue(writer, [home.job(3)])
    assert writer.flush()
    assert home.types() == ["write/dropped", "step/started"]
    assert home.steps() == [3]
    assert writer.stats().dropped == 2
    # Dropped jobs are settled as drops, never as landed.
    assert ("drop", 1) in home.events and ("drop", 2) in home.events
    assert not any(e[0] == "landed" and e[1] in (1, 2) for e in home.events)


def test_a_refused_run_falls_back_to_one_job_at_a_time(home):
    clock = _Clock()
    writer = _writer(home, clock)

    def fault(steps: list[int]) -> BaseException | None:
        return lg.CrewLogError("refused", code=lg.CODE_BAD_DATA) if 2 in steps else None

    home.fault = fault
    _queue(writer, [home.job(n) for n in range(1, 5)])
    assert writer.flush()
    # The refused entry alone is lost, with the marker the batch path owes for it, and
    # its neighbours land in order.
    assert home.steps() == [1, 3, 4]
    assert home.types().count("write/dropped") == 1
    assert writer.stats().dropped == 1


def test_an_oversize_entry_is_refused_alone_through_the_real_store(home):
    clock = _Clock()
    writer = _writer(home, clock)
    big = {"turn": 1, "step": 2, "pad": "x" * (2 * 1024 * 1024)}
    _queue(writer, [home.job(1), home.job(2, data=big), home.job(3)])
    assert writer.flush()
    assert home.steps() == [1, 3]
    assert writer.stats().dropped == 1


def test_an_fsync_failure_rolls_the_run_back_and_no_ack_precedes_the_fsync(home, monkeypatch):
    clock = _Clock()
    writer = _writer(home, clock)
    real_fsync = store_segment_writes.os.fsync
    state = {"fail": 1, "synced": 0}
    acks_before_sync: list[int] = []

    def fsync(fd: int) -> None:
        if state["fail"]:
            state["fail"] -= 1
            raise OSError(errno.EIO, "fsync failed")
        real_fsync(fd)
        state["synced"] += 1

    monkeypatch.setattr(store_segment_writes.os, "fsync", fsync)

    def ack(n: int) -> Any:
        return lambda: None if state["synced"] else acks_before_sync.append(n)

    jobs = [home.job(n, after=ack(n)) for n in range(1, 4)]
    _queue(writer, jobs)
    assert writer.flush()
    assert acks_before_sync == []
    assert home.steps() == [1, 2, 3], "the rolled-back run must land exactly once"


def test_flush_cuts_the_batching_window(home):
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-group")
    try:
        writer = CrewLogWriter(
            home.open_unit,
            executor=lambda: pool,
            limits=WriterLimits(batch_deadline_seconds=3600.0),  # flake-ok: never waited out
            logger=logging.getLogger(LOGGER),
        )
        _queue(writer, [home.job(n) for n in range(1, 4)])
        # A flush that waited the hour-long window out would time out and answer False.
        assert writer.flush(timeout=default_timeout())
        assert home.steps() == [1, 2, 3]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def test_concurrent_submitters_lose_nothing_and_keep_each_threads_order(home):
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-group")
    try:
        writer = CrewLogWriter(
            home.open_unit,
            executor=lambda: pool,
            limits=WriterLimits(
                batch_deadline_seconds=0.005, group_max_entries=7
            ),  # flake-ok: pause under test
            logger=logging.getLogger(LOGGER),
        )
        per_thread = 12

        def produce(t: int, on_loop: bool) -> None:
            jobs = [home.job(t * 1000 + i) for i in range(per_thread)]
            if on_loop:
                _queue(writer, jobs)
            else:  # the inline path, racing the writer thread
                for job in jobs:
                    assert writer.submit(UNIT, job)

        threads = [threading.Thread(target=produce, args=(t, t % 2 == 0)) for t in range(1, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=default_timeout())
        assert writer.flush(timeout=default_timeout())
        steps = home.steps()
        assert sorted(steps) == sorted(t * 1000 + i for t in range(1, 5) for i in range(per_thread))
        for t in range(1, 5):
            mine = [s for s in steps if s // 1000 == t]
            assert mine == sorted(mine), f"thread {t} was reordered: {mine}"
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def test_the_emitters_plain_appends_share_one_commit_and_settle_after_it(tmp_path, monkeypatch):
    """End to end through ``_write``: entries queued on the loop land as one commit, in
    order, and ``on_settled`` reports each only after that commit returned."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    # Held behind a pause no burst outlasts, so all five are queued before any pass; the
    # flush below is what releases it.
    emit_runtime.reset_caches(
        limits=WriterLimits(batch_deadline_seconds=3600.0)  # flake-ok: released by flush
    )
    session = "acp-group-emit-0001"
    lg.CrewLog.create(lg.KIND_SESSION, session, owner="default", agent="kirocrew")
    calls: list[int] = []
    real_commit = emit._commit_appends

    def spy(session_id: str, src: str, items: list[dict]) -> list[Any]:
        entries = real_commit(session_id, src, items)
        calls.append(len(items))
        return entries

    monkeypatch.setattr(emit, "_commit_appends", spy)
    settled: list[tuple[int, bool, int]] = []

    async def _emit() -> None:
        for n in range(1, 6):
            emit_runtime._write(
                session,
                "step/started",
                {"turn": 1, "step": n},
                src="gateway",
                on_settled=lambda ok, n=n: settled.append((n, ok, len(calls))),
            )

    try:
        asyncio.run(_emit())
        assert emit.flush(timeout=default_timeout())
        assert calls == [5]
        assert settled == [(n, True, 1) for n in range(1, 6)]
        path = lg.crew_log_path(lg.KIND_SESSION, session)
        with path.open("r", encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()][1:]
        assert [e["data"]["step"] for e in entries] == [1, 2, 3, 4, 5]
        assert [e["seq"] for e in entries] == sorted(e["seq"] for e in entries)
    finally:
        emit_runtime.reset_caches()


def test_a_run_whose_rollback_also_failed_is_not_written_twice(home, monkeypatch):
    """fsync AND its rollback fail: the run's bytes stay on disk (IndeterminateAppend).
    The retry must cut them first, or every entry of the run lands twice."""
    clock = _Clock()
    writer = _writer(home, clock)
    real_fsync = store_segment_writes.os.fsync
    real_rollback = crew_log_store._rollback_append
    left = {"fsync": 1, "rollback": 1}

    def fsync(fd: int) -> None:
        if left["fsync"]:
            left["fsync"] -= 1
            raise OSError(errno.EIO, "fsync failed")
        real_fsync(fd)

    def rollback(path: Any, size: int) -> None:
        if left["rollback"]:
            left["rollback"] -= 1
            raise OSError(errno.EIO, "rollback failed")
        real_rollback(path, size)

    monkeypatch.setattr(store_segment_writes.os, "fsync", fsync)
    monkeypatch.setattr(crew_log_store, "_rollback_append", rollback)
    _queue(writer, [home.job(n) for n in range(1, 6)])
    assert writer.flush()
    assert left == {"fsync": 0, "rollback": 0}, "the injected faults never fired"
    assert home.steps() == [1, 2, 3, 4, 5]
    assert writer.stats().dropped == 0


def test_a_residue_is_cut_only_when_it_is_still_this_appends_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(crew_log_store, "_RESIDUE", {})
    path = tmp_path / "seg.jsonl"
    written = b'{"a":1}\n{"b":2}\n'
    for tail, expect in (
        (written, b"head\n"),  # the whole failed append: cut
        (written[:10], b"head\n"),  # a partial one: cut, complete lines included
        (written[:3], b"head\n"),  # only part of its first line: cut
        (written + b'{"c":3}\n', b"head\n" + written + b'{"c":3}\n'),  # more came after
        (b'{"x":9}\n', b'head\n{"x":9}\n'),  # someone else's bytes
    ):
        path.write_bytes(b"head\n" + tail)
        crew_log_store._note_residue(path, len(b"head\n"), written)
        crew_log_store._settle_residue(path)
        assert path.read_bytes() == expect
        assert path not in crew_log_store._RESIDUE


def test_residues_are_bounded_and_hold_no_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(crew_log_store, "_RESIDUE", {})
    blob = b"x" * 50_000 + b"\n"
    for n in range(crew_log_store._RESIDUE_MAX + 10):
        crew_log_store._note_residue(tmp_path / f"seg-{n}.jsonl", 0, blob)
    held = crew_log_store._RESIDUE
    assert len(held) == crew_log_store._RESIDUE_MAX
    assert tmp_path / "seg-0.jsonl" not in held, "the oldest is forgotten first"
    assert all(len(d) == 32 for _, marks in held.values() for _, d in marks)
