"""Writer lanes: several units written side by side, each unit by one lane at a time.

``WriterLimits.lanes`` drain loops share the buffer, and a lane HOLDS a unit while it
writes it, so no other lane and no inline write touches that unit until it is let go.
These tests pin what must survive that: no record lost or written twice, no reordering
within a unit, never two writers inside one unit at once, one slow unit not holding the
others, the retry / loss-marker / refusal rules, flush and shutdown barriers, and a
second process's write lease still refusing this one. They run against REAL session
logs on the test's data home with a real multi-worker pool.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log.errors import CODE_ALREADY_OWNED, CrewLogError
from kiro_crew.crew_log.writer import CrewLogWriter, GroupPart, WriteJob, WriterLimits
from kiro_crew.testing.wait import default_timeout, wait_until

LOGGER = "kiro_crew.crew_log.writer"
UNITS = [f"acp-lane-{n:04d}" for n in range(8)]


class _Home:
    """Real logs, a per-unit fault hook, and a record of who was inside which unit."""

    def __init__(self) -> None:
        self.handles: dict[str, Any] = {}
        self.fault: Any = None
        self.lock = threading.Lock()
        self.inside: dict[str, int] = {}
        self.max_inside_one_unit = 0
        self.writing_now = 0
        self.max_writing_now = 0

    def create(self, unit: str) -> None:
        self.handles[unit] = lg.CrewLog.create(
            lg.KIND_SESSION, unit, owner="default", agent="kirocrew"
        )

    def open_unit(self, unit: str) -> Any:
        return self.handles.get(unit)

    def entries(self, unit: str) -> list[dict]:
        path = lg.crew_log_path(lg.KIND_SESSION, unit)
        with path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()][1:]

    def steps(self, unit: str) -> list[int]:
        return [e["data"]["step"] for e in self.entries(unit) if e["type"] == "step/started"]

    def _enter(self, unit: str, steps: list[int]) -> None:
        with self.lock:
            self.inside[unit] = self.inside.get(unit, 0) + 1
            self.max_inside_one_unit = max(self.max_inside_one_unit, self.inside[unit])
            self.writing_now += 1
            self.max_writing_now = max(self.max_writing_now, self.writing_now)
        try:
            exc = None if self.fault is None else self.fault(unit, steps)
            if exc is not None:
                raise exc
        except BaseException:
            self._leave(unit)
            raise

    def _leave(self, unit: str) -> None:
        with self.lock:
            self.inside[unit] -= 1
            self.writing_now -= 1

    def job(self, unit: str, n: int, *, after: Any = None, grouped: bool = True) -> WriteJob:
        body = {"turn": 1, "step": n}

        def run() -> None:
            self._enter(unit, [n])
            try:
                self.handles[unit].append("step/started", body, src="gateway")
            finally:
                self._leave(unit)

        def commit(items: list[dict]) -> list[Any]:
            self._enter(unit, [item["data"]["step"] for item in items])
            try:
                return list(self.handles[unit].append_many(items, src="gateway"))
            finally:
                self._leave(unit)

        part = GroupPart(
            key=("append", "gateway"),
            item={"type": "step/started", "data": body},
            commit=commit,
            landed=lambda entry: None,
        )
        return WriteJob.append(run, f"step {n}", after=after, group=part if grouped else None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    h = _Home()
    for unit in UNITS:
        h.create(unit)
    yield h
    h.handles.clear()


@pytest.fixture
def pool():
    p = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-lanes")
    yield p
    p.shutdown(wait=True, cancel_futures=True)


def _writer(
    home: _Home, pool: ThreadPoolExecutor, *, hold_key: Any = None, **limits: Any
) -> CrewLogWriter:
    limits.setdefault("batch_deadline_seconds", 0.002)  # flake-ok: pause under test
    limits.setdefault("retry_backoff_seconds", 0.001)  # flake-ok: backoff under test
    return CrewLogWriter(
        home.open_unit,
        executor=lambda: pool,
        limits=WriterLimits(**limits),
        logger=logging.getLogger(LOGGER),
        hold_key=hold_key,
    )


def _queue(writer: CrewLogWriter, items: "list[tuple[str, WriteJob]]") -> None:
    """Submit on a running loop, so every job is buffered rather than written inline."""

    async def _run() -> None:
        for unit, job in items:
            assert writer.submit(unit, job)

    asyncio.run(_run())


def test_many_units_many_producers_lose_nothing_and_keep_each_units_order(home, pool):
    writer = _writer(home, pool, group_max_entries=5)
    per = 40

    def produce(t: int) -> None:
        jobs = [
            (UNITS[(t + i) % len(UNITS)], home.job(UNITS[(t + i) % len(UNITS)], t * 1000 + i))
            for i in range(per)
        ]
        if t % 2:
            _queue(writer, jobs)
        else:  # the inline path, racing the lanes
            for unit, job in jobs:
                assert writer.submit(unit, job)

    threads = [threading.Thread(target=produce, args=(t,)) for t in range(1, 7)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=default_timeout())
    assert writer.flush(timeout=default_timeout())

    written = {unit: home.steps(unit) for unit in UNITS}
    every = [s for steps in written.values() for s in steps]
    assert sorted(every) == sorted(t * 1000 + i for t in range(1, 7) for i in range(per))
    for unit, steps in written.items():
        for t in range(1, 7):
            mine = [s for s in steps if s // 1000 == t]
            assert mine == sorted(mine), f"{unit}: producer {t} reordered: {mine}"
        seqs = [e["seq"] for e in home.entries(unit)]
        assert seqs == sorted(set(seqs)), f"{unit}: seq duplicated or out of order"
    assert home.max_inside_one_unit == 1, "two writers were inside one unit at once"


def test_one_stuck_unit_does_not_hold_the_other_units(home, pool):
    writer = _writer(home, pool)
    stuck, others = UNITS[0], UNITS[1:]
    release = threading.Event()
    entered = threading.Event()

    def hold_the_first(unit: str, steps: list[int]) -> None:
        if unit == stuck:
            entered.set()
            release.wait(default_timeout())

    home.fault = hold_the_first
    landed: list[str] = []
    _queue(writer, [(stuck, home.job(stuck, 1))])
    assert entered.wait(default_timeout())
    _queue(
        writer,
        [(u, home.job(u, n, after=lambda u=u: landed.append(u))) for n in (1, 2) for u in others],
    )
    wait_until(lambda: len(landed) >= 2 * len(others), describe=lambda: landed)
    try:
        assert sorted(landed) == sorted(others * 2), "the others waited behind the stuck unit"
        assert home.steps(stuck) == []
        assert writer.stats().batch_in_flight  # the stuck write is still in flight
    finally:
        release.set()
    assert writer.flush(timeout=default_timeout())
    assert home.steps(stuck) == [1]
    assert home.max_inside_one_unit == 1


def test_a_failing_unit_retries_alone_and_its_loss_is_marked_before_later_entries(home, pool):
    writer = _writer(home, pool, max_write_attempts=2)
    bad = UNITS[0]
    all_queued = threading.Event()

    def fail_bad(unit: str, steps: list[int]) -> Exception | None:
        if unit == bad and 2 in steps:
            # Held until every job is queued, so step 3 is behind step 2 when the
            # attempts are spent, rather than queued after they already were.
            assert all_queued.wait(default_timeout())
            return OSError(errno.EIO, "disk on fire")
        return None

    home.fault = fail_bad
    _queue(writer, [(u, home.job(u, n, grouped=u != bad)) for n in (1, 2, 3) for u in UNITS])
    all_queued.set()
    assert writer.flush(timeout=default_timeout())
    home.fault = None
    _queue(writer, [(bad, home.job(bad, 4))])
    assert writer.flush(timeout=default_timeout())
    for unit in UNITS[1:]:
        assert home.steps(unit) == [1, 2, 3]
    types = [e["type"] for e in home.entries(bad)]
    # Step 2 failed every attempt; it and step 3 behind it were dropped together.
    assert home.steps(bad) == [1, 4]
    assert types == ["step/started", "write/dropped", "step/started"]
    marker = home.entries(bad)[1]["data"]
    assert marker["dropped_count"] == 2
    assert writer.stats().dropped == 2


def test_a_refusal_mid_batch_drops_only_that_entry_and_owes_a_marker(home, pool):
    writer = _writer(home, pool)
    bad = UNITS[0]

    def refuse_two(unit: str, steps: list[int]) -> Exception | None:
        if unit == bad and 2 in steps:
            return CrewLogError("lease lost", code=CODE_ALREADY_OWNED)
        return None

    home.fault = refuse_two
    _queue(writer, [(u, home.job(u, n)) for n in (1, 2, 3) for u in UNITS])
    assert writer.flush(timeout=default_timeout())
    home.fault = None
    _queue(writer, [(bad, home.job(bad, 4))])
    assert writer.flush(timeout=default_timeout())
    for unit in UNITS[1:]:
        assert home.steps(unit) == [1, 2, 3]
    # The refused group is retried one job at a time, so only step 2 is lost, and the
    # marker admitting it lands before step 3.
    assert home.steps(bad) == [1, 3, 4]
    types = [e["type"] for e in home.entries(bad)]
    assert types == ["step/started", "write/dropped", "step/started", "step/started"]
    assert writer.stats().dropped == 1


def test_shutdown_drain_writes_every_lanes_backlog(home, pool):
    writer = _writer(home, pool, batch_deadline_seconds=3600.0)  # flake-ok: never waited out
    _queue(writer, [(u, home.job(u, n)) for n in range(1, 6) for u in UNITS])
    report = writer.drain_for_shutdown(timeout=default_timeout())
    assert report.drained and report.buffered == 0 and not report.batch_in_flight
    for unit in UNITS:
        assert home.steps(unit) == [1, 2, 3, 4, 5]


def test_lanes_never_exceed_the_limit(home, pool):
    writer = _writer(home, pool, lanes=2)
    release = threading.Event()

    def hold(unit: str, steps: list[int]) -> None:
        release.wait(default_timeout())

    home.fault = hold
    _queue(writer, [(u, home.job(u, n)) for n in range(1, 4) for u in UNITS])
    try:
        # Both lanes parked inside a write, with six more units still owed: a third
        # lane would be started now if the limit did not hold.
        wait_until(lambda: home.writing_now == 2, describe=lambda: home.writing_now)
        assert len(writer._lanes) == 2
    finally:
        release.set()
    assert writer.flush(timeout=default_timeout())
    assert home.max_writing_now == 2
    for unit in UNITS:
        assert home.steps(unit) == [1, 2, 3]


_HOLD_LEASE = """
import sys
from kiro_crew import crew_log as lg
log = lg.CrewLog.create(lg.KIND_SESSION, sys.argv[1], owner="default", agent="kirocrew")
log.append("step/started", {"turn": 1, "step": 0}, src="gateway")
print("held", flush=True)
sys.stdin.read()
"""


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "win32: lock semantics differ; skipped like test_crew_log_lease.py's "
        "test_a_resume_in_another_process_cannot_close_a_live_writers_turn"
    ),
)
def test_a_second_process_owning_a_unit_still_refuses_this_writer(home, pool):
    owned = "acp-lane-owned"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LEASE, owned],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=dict(os.environ),
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        home.handles[owned] = lg.CrewLog.open(lg.KIND_SESSION, owned)
        writer = _writer(home, pool)
        _queue(writer, [(u, home.job(u, 1)) for u in [owned, *UNITS]])
        # The marker admitting the refused entry is refused too, again on each pass, so
        # the writer is never quiet for that unit; the others land regardless. Whether
        # the marker is owed or in flight at any one read is a race, so it is not read.
        wait_until(lambda: all(home.steps(u) == [1] for u in UNITS))
        wait_until(lambda: writer.stats().dropped >= 1)
        types = [e["type"] for e in home.entries(owned)]
        assert types == ["step/started"], "this process wrote into a log another one owns"
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=default_timeout())


def test_a_hung_lane_is_reported_once_while_other_lanes_keep_finishing(home, pool, caplog):
    # A zero threshold: the held write is a stall from the moment it starts.
    writer = _writer(home, pool, write_stall_secs=0.0)
    stuck = UNITS[0]
    release = threading.Event()
    entered = threading.Event()

    def hold_the_first(unit: str, steps: list[int]) -> None:
        if unit == stuck:
            entered.set()
            release.wait(default_timeout())

    home.fault = hold_the_first
    _queue(writer, [(stuck, home.job(stuck, 1))])
    assert entered.wait(default_timeout())
    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            for n in range(2, 12):
                _queue(writer, [(UNITS[1], home.job(UNITS[1], n))])
                wait_until(lambda n=n: n in home.steps(UNITS[1]))
        stalls = [r for r in caplog.records if "writer stalled" in r.getMessage()]
        assert len(stalls) == 1, [r.getMessage() for r in stalls]
    finally:
        release.set()
    assert writer.flush(timeout=default_timeout())


def _append_then_publish(
    home: _Home,
    unit: str,
    n: int,
    appended: list,
    published: list,
    gate: Any,
    done: "threading.Event | None" = None,
) -> WriteJob:
    """A work entry's two steps: the append, then the publish to the slot's order file.

    *gate* runs between them, which is where a sibling unit's lane could get in.
    """

    def run() -> None:
        home.handles[unit].append("step/started", {"turn": 1, "step": n}, src="gateway")
        appended.append(unit)
        gate(unit)
        published.append(unit)
        if done is not None:
            done.set()

    return WriteJob.append(run, f"work {n}")


@pytest.mark.parametrize("grouped", [False, True])
def test_units_of_one_slot_publish_in_the_order_they_appended(home, pool, grouped):
    """Without a shared hold key, a sibling unit's append+publish lands inside the first
    unit's pair and the order file names the older unit newest. With it, never."""
    first, second = UNITS[0], UNITS[1]
    slots = {first: "slot:board", second: "slot:board"}
    writer = _writer(home, pool, hold_key=slots.get if grouped else None)
    appended: list[str] = []
    published: list[str] = []
    second_published = threading.Event()
    first_running = threading.Event()

    def gate(unit: str) -> None:
        if unit != second:
            first_running.set()
            # Wide open for a sibling: wait until its publish is done. Ungrouped it
            # comes, so wait for it in full. Grouped it must never come; a short bound
            # there can only weaken that negative, never fail it.
            if grouped:
                second_published.wait(0.2)  # flake-ok: bounded negative, never reds the test
            else:
                assert second_published.wait(default_timeout())

    _queue(writer, [(first, _append_then_publish(home, first, 1, appended, published, gate))])
    assert first_running.wait(default_timeout())
    _queue(
        writer,
        [
            (
                second,
                _append_then_publish(home, second, 2, appended, published, gate, second_published),
            )
        ],
    )
    assert writer.flush(timeout=default_timeout())
    assert appended == [first, second]
    if grouped:
        assert published == appended
    else:
        assert published == [second, first], "the race this guards against did not occur"


def test_the_emitters_writer_keeps_one_slots_units_on_one_lane(tmp_path, monkeypatch):
    """The installed writer takes its keys from ``writer_groups``, keyed by slot."""
    from kiro_crew.crew_log import emit_runtime, writer_groups

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    emit_runtime.reset_caches()
    try:
        writer_groups.note_unit_slot("acp-a", "chat-board")
        writer_groups.note_unit_slot("acp-b", "chat-board")
        writer = emit_runtime._writer()
        assert writer._key("acp-a") == writer._key("acp-b") == "slot:chat-board"
        assert writer._key("acp-other") == "acp-other"
    finally:
        emit_runtime.reset_caches()
    assert writer_groups.hold_key("acp-a") == ""


def test_a_unit_whose_key_changes_while_held_is_still_written_by_one_lane(home, pool):
    """The slot key of a held unit is evicted (or noted late): its next entry must still
    wait for the lane that holds the unit, or two lanes write one file out of order."""
    unit = UNITS[0]
    keys = {unit: "slot:board"}
    writer = _writer(home, pool, hold_key=keys.get)
    release = threading.Event()
    entered = threading.Event()

    def hold_step_one(u: str, steps: list[int]) -> None:
        if u == unit and 1 in steps:
            entered.set()
            release.wait(default_timeout())

    home.fault = hold_step_one
    _queue(writer, [(unit, home.job(unit, 1, grouped=False))])
    assert entered.wait(default_timeout())
    try:
        keys.clear()  # the bounded map forgot it
        _queue(writer, [(unit, home.job(unit, 2, grouped=False))])
        # A spare lane is free and the entry is owed; the hold must still stop it.
        wait_until(lambda: len(writer._lanes) >= 1)
        assert home.steps(unit) == []
    finally:
        release.set()
    assert writer.flush(timeout=default_timeout())
    assert home.steps(unit) == [1, 2]
    assert home.max_inside_one_unit == 1


def test_a_rebuilt_writer_does_not_write_beside_a_retired_writers_stuck_lane(home, pool):
    """``emit.reset_caches`` retires a writer whose lane may be stuck in a write. The next
    writer shares the pool and must wait for that unit, not write the file beside it."""
    unit = UNITS[0]
    retired = _writer(home, pool)
    release = threading.Event()
    entered = threading.Event()

    def hold_step_one(u: str, steps: list[int]) -> None:
        if u == unit and 1 in steps:
            entered.set()
            release.wait(default_timeout())

    home.fault = hold_step_one
    _queue(retired, [(unit, home.job(unit, 1, grouped=False))])
    assert entered.wait(default_timeout())
    try:
        assert not retired.close(timeout=0)  # the stuck write is still in flight
        # Passes on this thread, on a clock its own sleep advances: a whole flush
        # budget of passes runs at once, and none may claim the retired lane's unit.
        now = [1000.0]

        def advance(seconds: float) -> None:
            now[0] += max(seconds, 0.001)

        rebuilt = CrewLogWriter(
            home.open_unit, clock=lambda: now[0], sleep=advance, logger=logging.getLogger(LOGGER)
        )
        _queue(rebuilt, [(unit, home.job(unit, 2, grouped=False))])
        assert not rebuilt.flush(timeout=default_timeout())
        assert home.steps(unit) == []
    finally:
        release.set()
    from kiro_crew.crew_log.writer_groups import PROCESS_HOLDS

    # Step 1 lands in the file BEFORE the retired lane lets the unit go (it finishes the
    # batch's bookkeeping first). A flush in that window spends its whole fake-clock
    # budget against a held unit and returns False, so wait for the hold itself.
    wait_until(lambda: home.steps(unit) == [1] and unit not in PROCESS_HOLDS.held())
    assert rebuilt.flush(timeout=default_timeout())
    assert home.steps(unit) == [1, 2]
    assert home.max_inside_one_unit == 1


class _NeverRuns:
    """An executor that takes work and never runs it: a lane stays queued."""

    def submit(self, fn: Any, *args: Any) -> Any:
        from concurrent.futures import Future

        return Future()


def test_a_lane_that_close_retired_before_it_ran_writes_nothing(home):
    """A queued lane whose entry ``close()`` dropped must not drain as a stand-in that
    no barrier can see; the writer's own next lane does that work."""
    unit = UNITS[0]
    never = _NeverRuns()
    writer = CrewLogWriter(home.open_unit, executor=lambda: never, logger=logging.getLogger(LOGGER))
    _queue(writer, [(unit, home.job(unit, 1))])
    (retired_lane,) = writer._lanes
    writer.close(timeout=0)
    _queue(writer, [(unit, home.job(unit, 2))])
    writer._drain_loop(retired_lane)  # what the queued future would run
    assert home.steps(unit) == []
    assert writer.owes(unit)


def test_a_work_report_into_an_unopened_unit_gets_its_slot_key(tmp_path, monkeypatch):
    """A unit this process never opened (a conductor's unit after a restart) is keyed by
    its header slot before its work record is handed to the writer."""
    from kiro_crew.crew_log import emit, emit_runtime, writer_groups

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    emit_runtime.reset_caches()
    try:
        unit = "acp-board-after-restart"
        lg.CrewLog.create(lg.KIND_SESSION, unit, owner="default", agent="lead", slot="board-1")
        assert writer_groups.hold_key(unit) == ""
        data = {"slot": "board-1", "actor": "conductor", "by": "board-1", "action": "goal"}
        data.update({"goal": "g", "round": 1})
        assert emit.on_work_recorded(unit, data)
        assert writer_groups.hold_key(unit) == "slot:board-1"
    finally:
        emit_runtime.reset_caches()


def test_an_over_long_slot_is_not_kept_and_a_closed_session_forgets_its_key(tmp_path, monkeypatch):
    from kiro_crew.crew_log import emit, emit_runtime, writer_groups

    writer_groups.note_unit_slot("acp-long", "s" * (writer_groups.MAX_KEY_CHARS + 1))
    assert writer_groups.hold_key("acp-long") == ""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KIROCREW_CREW_LOG", raising=False)
    emit_runtime.reset_caches()
    try:
        emit.on_session_opened("acp-closing", agent="kirocrew", slot="chat-9")
        assert emit.flush()
        assert writer_groups.hold_key("acp-closing") == "slot:chat-9"
        emit.on_session_closed("acp-closing", "removed")
        assert emit.flush()
        assert writer_groups.hold_key("acp-closing") == ""
    finally:
        emit_runtime.reset_caches()


def test_a_unit_held_outside_this_writers_lanes_is_waited_for_not_left_stranded(home, pool):
    """A unit held by an inline write or another writer is not this writer's lane's to
    come back to. The lane must keep looking until the hold goes, not exit and leave the
    entry buffered with nothing left to write it."""
    from kiro_crew.crew_log.writer_groups import PROCESS_HOLDS

    unit = UNITS[0]
    writer = _writer(home, pool, hold_key=lambda u: "slot:stall-test" if u == unit else "")
    looks: list[int] = []
    main = threading.get_ident()
    claimable = writer._claimable_locked

    def counted(u: str, now: float) -> bool:
        if u == unit and threading.get_ident() != main:
            looks.append(1)  # a lane asked whether it may take this unit
        return claimable(u, now)

    writer._claimable_locked = counted  # type: ignore[method-assign]
    # Another writer holds a sibling of the slot.
    assert PROCESS_HOLDS.try_claim("stall-holder", "slot:stall-test", lambda u: "")
    try:
        _queue(writer, [(unit, home.job(unit, 1))])
        # Either the lane gave up (no lane left) or it keeps looking at the held unit.
        wait_until(lambda: not writer._lanes or len(looks) >= 3)
    finally:
        PROCESS_HOLDS.release("stall-holder")
    assert writer.flush(timeout=default_timeout()), "the entry was left with no lane"
    assert home.steps(unit) == [1]


def test_queued_work_keeps_the_slot_key_it_was_admitted_with(home, pool):
    """A session close forgets its unit's slot key while work for that unit is still
    queued. The queued work must stay fenced under the key it was admitted with, or it is
    written beside a held sibling of the slot."""
    first, sibling = UNITS[0], UNITS[1]
    keys = {first: "slot:board", sibling: "slot:board"}
    writer = _writer(home, pool, hold_key=lambda u: keys.get(u, ""))
    looks: list[int] = []
    main = threading.get_ident()
    claimable = writer._claimable_locked

    def counted(unit: str, now: float) -> bool:
        if unit == first and threading.get_ident() != main:
            looks.append(1)  # a lane asked whether it may take this unit
        return claimable(unit, now)

    writer._claimable_locked = counted  # type: ignore[method-assign]
    release = threading.Event()
    entered = threading.Event()

    def hold_the_sibling(u: str, steps: list[int]) -> None:
        if u == sibling:
            entered.set()
            release.wait(default_timeout())

    home.fault = hold_the_sibling
    _queue(writer, [(sibling, home.job(sibling, 1))])
    assert entered.wait(default_timeout())
    try:
        _queue(writer, [(first, home.job(first, 1))])
        keys.pop(first)  # the session closed; its key is forgotten
        wait_until(lambda: len(looks) >= 3 or home.steps(first) != [])
        assert home.steps(first) == [], "queued work was written beside a held sibling"
    finally:
        release.set()
    assert writer.flush(timeout=default_timeout())
    assert home.steps(first) == [1] and home.steps(sibling) == [1]


def test_a_bare_id_freeze_upgrades_when_the_slot_is_noted_again(home, pool):
    """A unit admitted while its slot was unknown (or forgotten) is frozen under its bare
    id. Once the slot is noted again, its queued work must not be claimed beside a held
    sibling of that slot."""
    first, sibling = UNITS[0], UNITS[1]
    keys = {sibling: "slot:board"}  # the first unit's slot is not known yet
    writer = _writer(home, pool, hold_key=lambda u: keys.get(u, ""))
    looks: list[int] = []
    main = threading.get_ident()
    claimable = writer._claimable_locked

    def counted(unit: str, now: float) -> bool:
        if unit == first and threading.get_ident() != main:
            looks.append(1)
        return claimable(unit, now)

    writer._claimable_locked = counted  # type: ignore[method-assign]
    release = threading.Event()
    entered = threading.Event()

    def hold_the_sibling(u: str, steps: list[int]) -> None:
        if u == sibling:
            entered.set()
            release.wait(default_timeout())

    home.fault = hold_the_sibling
    _queue(writer, [(sibling, home.job(sibling, 1))])
    assert entered.wait(default_timeout())
    try:
        # Admitted with no slot known, so frozen under its bare id. A stand-in hold on
        # the unit keeps every lane off it until its slot has been noted again.
        from kiro_crew.crew_log.writer_groups import PROCESS_HOLDS

        assert PROCESS_HOLDS.try_claim(first, first, lambda u: "")
        try:
            _queue(writer, [(first, home.job(first, 1))])
            assert writer._frozen[first] == first
            keys[first] = "slot:board"  # on_work_recorded notes the slot again
        finally:
            PROCESS_HOLDS.release(first)
        wait_until(lambda: len(looks) >= 3 or home.steps(first) != [])
        assert home.steps(first) == [], "a bare-id freeze was written beside its slot sibling"
    finally:
        release.set()
    assert writer.flush(timeout=default_timeout())
    assert home.steps(first) == [1] and home.steps(sibling) == [1]


def test_two_units_of_one_known_slot_are_never_held_together_in_any_event_order():
    """The Holds invariant over every order of note, forget, freeze, claim and release
    for two units of one slot: a claim succeeds beside a held sibling only when the
    slot is unknown for at least one of them at that moment."""
    import itertools

    from kiro_crew.crew_log.writer_groups import Holds

    ops = ["noteA", "noteB", "forgetA", "freezeA", "freezeB", "claimA", "claimB", "releaseA"]
    for order in itertools.permutations(ops):
        keys: dict[str, str] = {}
        writer = CrewLogWriter(lambda unit: None, hold_key=lambda u: keys.get(u, ""))
        writer._held = Holds()
        held: set[str] = set()

        def known(u: str) -> bool:
            return bool(keys.get(u)) or writer._frozen.get(u, "").startswith("slot:")

        for op in order:
            unit = op[-1]
            other = "B" if unit == "A" else "A"
            if op.startswith("note"):
                keys[unit] = "slot:S"
            elif op.startswith("forget"):
                keys.pop(unit, None)
            elif op.startswith("freeze"):
                writer._freeze_locked(unit)
            elif op.startswith("release"):
                writer._held.release(unit)
                held.discard(unit)
            elif unit not in held:
                both_known = known(unit) and known(other)
                if writer._held.try_claim(unit, writer._key(unit), writer._key):
                    held.add(unit)
                    assert not (other in held and both_known), order
