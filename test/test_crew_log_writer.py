"""Interface tests for the crew log's durable writer, :mod:`kiro_crew.crew_log.writer`.

Every test builds its own :class:`CrewLogWriter` against REAL session crew logs on the
test's temporary data home, through an ``open_unit`` adapter that can inject a fault
into any append, and asserts on what the interface reports (``submit``'s verdict,
``stats()``, ``owes()``, the shutdown report) and on the entries that reached disk.

Most tests run with NO executor: every pass then runs on the calling thread inside
``flush`` or ``drain_for_shutdown``, paced by an injected clock whose ``sleep`` moves
it, so the whole retry and shutdown schedule is driven deterministically -- no real
waiting, and the same answer on any host. The tests that are about the writer THREAD
(a hung write, a parked backoff, a cancelled pass) give it a real one-worker pool and
synchronise on events.
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import emit, lease
from kiro_crew.crew_log.writer import (
    DEFAULT_LIMITS,
    CrewLogWriter,
    DrainReport,
    WarningBudget,
    WriteJob,
    WriterLimits,
)

UNIT = "acp-writer-0001"
OTHER = "acp-writer-0002"
LOGGER = "kiro_crew.crew_log.writer"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class _Clock:
    """A monotonic clock that moves only when slept on or told to."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _Units:
    """Real session crew logs on the test's data home, one handle per unit.

    ``fault(unit, entry_type, data)`` returns an exception to raise instead of an
    append, or ``None`` to let it through; it is consulted for every append made
    through ``open_unit``, the writer's own loss markers included.
    """

    def __init__(self) -> None:
        self._handles: dict[str, Any] = {}
        self.fault: Callable[[str, str, dict], BaseException | None] | None = None
        self.attempts: dict[tuple[str, str], int] = {}

    def create(self, *units: str) -> None:
        for unit in units:
            self._handles[unit] = lg.CrewLog.create(
                lg.KIND_SESSION, unit, owner="default", agent="kirocrew"
            )

    def open_unit(self, unit: str) -> "_Log | None":
        handle = self._handles.get(unit)
        return None if handle is None else _Log(self, unit, handle)

    def entries(self, unit: str) -> list[tuple[str, dict]]:
        path = lg.crew_log_path(lg.KIND_SESSION, unit)
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        return [(entry["type"], entry["data"]) for entry in lines[1:]]

    def steps(self, unit: str) -> list[int]:
        return [data["step"] for kind, data in self.entries(unit) if kind == "step/started"]

    def close(self) -> None:
        self._handles.clear()


class _Log:
    def __init__(self, units: _Units, unit: str, handle: Any) -> None:
        self._units = units
        self._unit = unit
        self._handle = handle

    def append(self, entry_type: str, data: dict, **kwargs: Any) -> Any:
        key = (self._unit, entry_type)
        self._units.attempts[key] = self._units.attempts.get(key, 0) + 1
        fault = self._units.fault
        exc = None if fault is None else fault(self._unit, entry_type, data)
        if exc is not None:
            # Cleared on the way out: a local that still names the exception would make
            # exception -> traceback -> this frame -> exception a cycle, and this frame
            # holds the handle, so its lease would wait for the collector.
            try:
                raise exc
            finally:
                exc = None
        return self._handle.append(entry_type, data, **kwargs)


@pytest.fixture
def units(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    home = _Units()
    yield home
    home.close()
    # A write lease is released when the handle that adopted it is dropped. A failure
    # path that kept a frame holding a handle alive (an exception object, an exc_info
    # record) would leave it held here.
    assert not lease._held, f"a lease outlived its test: {sorted(lease._held)}"


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-crewlog")
    yield executor
    executor.shutdown(wait=True, cancel_futures=True)


def _writer(
    units: _Units,
    clock: _Clock | None = None,
    *,
    executor: Callable[[], Any] | None = None,
    retry_delay: Callable[[int], float] | None = None,
    on_grew: Callable[[str], None] | None = None,
    **limits: Any,
) -> CrewLogWriter:
    logger = logging.getLogger(LOGGER)
    if executor is None:
        assert clock is not None, "an inline writer is driven by an injected clock"
        return CrewLogWriter(
            units.open_unit,
            clock=clock,
            sleep=clock.sleep,
            limits=WriterLimits(**limits),
            retry_delay=retry_delay,
            on_grew=on_grew,
            warnings=WarningBudget(logger, clock=clock),
            logger=logger,
        )
    limits.setdefault("batch_deadline_seconds", 0.0)
    return CrewLogWriter(
        units.open_unit,
        executor=executor,
        limits=WriterLimits(**limits),
        retry_delay=retry_delay,
        on_grew=on_grew,
        logger=logger,
    )


def _step(
    units: _Units,
    unit: str,
    n: int,
    *,
    nbytes: int = 0,
    after: Callable[[], None] | None = None,
    on_drop: Callable[[], None] | None = None,
) -> WriteJob:
    def run() -> None:
        log = units.open_unit(unit)
        assert log is not None, f"{unit} has no log"
        log.append("step/started", {"turn": 1, "step": n}, src="gateway")

    return WriteJob.append(run, f"step {n}", nbytes=nbytes, after=after, on_drop=on_drop)


def _on_loop(fn: Callable[[], Any]) -> Any:
    """Run *fn* on a running event loop, where the writer never writes inline."""

    async def _run() -> Any:
        return fn()

    return asyncio.run(_run())


def _failing(
    match: Callable[[str, str, dict], bool], *, times: int | None = None, exc=None
) -> Callable[[str, str, dict], BaseException | None]:
    """A fault that raises for appends *match* selects, *times* times (None: always)."""
    left = {"n": times}

    def fault(unit: str, entry_type: str, data: dict) -> BaseException | None:
        if not match(unit, entry_type, data):
            return None
        if left["n"] is not None:
            if left["n"] <= 0:
                return None
            left["n"] -= 1
        return exc() if exc is not None else OSError(errno.EIO, "Input/output error")

    return fault


def _is_step(n: int | None = None) -> Callable[[str, str, dict], bool]:
    return lambda unit, kind, data: kind == "step/started" and (n is None or data["step"] == n)


def _is_marker(unit: str, kind: str, data: dict) -> bool:
    return kind == "write/dropped"


def _refusal() -> lg.CrewLogError:
    return lg.CrewLogError("refused", code=lg.CODE_BAD_DATA)


def _messages(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# --------------------------------------------------------------------------- #
# The schedule
# --------------------------------------------------------------------------- #


def test_the_retry_schedule_doubles_from_its_floor_to_its_ceiling():
    """Policy: a floor low enough that a blip costs a moment, a ceiling low enough that
    a whole attempt budget still fits inside the emitter's default bounded shutdown."""
    limits = DEFAULT_LIMITS
    floor = limits.retry_backoff_seconds
    assert limits.retry_delay(0) == floor
    assert limits.retry_delay(1) == floor
    assert limits.retry_delay(2) == floor * 2
    assert limits.retry_delay(3) == floor * 4
    assert limits.retry_delay(99) == limits.retry_backoff_max_seconds
    spent = sum(limits.retry_delay(n) for n in range(1, limits.max_write_attempts))
    shutdown_budget = inspect.signature(emit.drain_for_shutdown).parameters["timeout"].default
    assert spent < shutdown_budget


# --------------------------------------------------------------------------- #
# The inline path and the buffer
# --------------------------------------------------------------------------- #


def test_an_append_with_nothing_owed_is_written_before_submit_returns(units, clock):
    units.create(UNIT)
    writer = _writer(units, clock)
    assert writer.submit(UNIT, _step(units, UNIT, 1)) is True
    assert units.steps(UNIT) == [1]
    stats = writer.stats()
    assert (stats.buffered, stats.dropped, stats.overflowed, writer.loss_markers_owed()) == (
        0,
        0,
        0,
        0,
    )
    assert not writer.owes(UNIT)


def test_on_an_event_loop_nothing_is_written_on_the_callers_thread(units, clock):
    """The loop is never blocked: a submission there is only buffered."""
    units.create(UNIT)
    writer = _writer(units, clock)

    def _submit() -> None:
        assert writer.submit(UNIT, _step(units, UNIT, 1)) is True
        assert units.steps(UNIT) == [], "the loop thread wrote to disk"
        assert writer.stats().buffered == 1
        assert writer.owes(UNIT)

    _on_loop(_submit)
    assert writer.flush()
    assert units.steps(UNIT) == [1]


def test_a_failed_append_is_retained_and_retried_only_once_its_backoff_is_due(units, clock):
    units.create(UNIT)
    units.fault = _failing(_is_step(1), times=1)
    writer = _writer(units, clock)

    assert writer.submit(UNIT, _step(units, UNIT, 1)) is True
    assert units.steps(UNIT) == [], "a failed inline write was not retained"
    assert writer.owes(UNIT) and writer.stats().buffered == 1
    # A flush that cannot reach the retry instant does not attempt it early.
    assert writer.flush(timeout=DEFAULT_LIMITS.retry_backoff_seconds / 2) is False
    assert units.attempts[(UNIT, "step/started")] == 1
    assert writer.flush(timeout=1.0)
    assert units.steps(UNIT) == [1]
    assert writer.stats().dropped == 0


def test_entries_behind_a_retained_one_wait_and_land_in_order(units, clock):
    """The batch goes back to the FRONT: writing past a failure would put the log on disk
    in an order that never happened."""
    units.create(UNIT)
    units.fault = _failing(_is_step(1), times=2)
    writer = _writer(units, clock)
    for n in (1, 2, 3):
        assert writer.submit(UNIT, _step(units, UNIT, n))
    assert units.steps(UNIT) == [], "a later entry overtook the retained one"
    assert writer.flush(timeout=10.0)
    assert units.steps(UNIT) == [1, 2, 3]


def test_an_entry_queued_while_its_batch_fails_lands_behind_that_batch(units, pool):
    """Retention puts the failed batch back AHEAD of what producers queued while it was
    being written -- the case where back and front actually differ."""
    units.create(UNIT)
    in_write = threading.Event()
    release = threading.Event()

    def fault(unit: str, kind: str, data: dict) -> BaseException | None:
        if kind != "step/started" or data["step"] != 1 or release.is_set():
            return None
        in_write.set()
        release.wait(timeout=10.0)
        return OSError(errno.EIO, "I/O error")

    units.fault = fault
    writer = _writer(units, executor=lambda: pool, retry_delay=lambda attempts: 0.0)
    _on_loop(lambda: [writer.submit(UNIT, _step(units, UNIT, n)) for n in (1, 2, 3)])
    assert in_write.wait(timeout=10.0), "the first write never started"
    _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 4)))
    release.set()
    assert writer.flush(timeout=10.0)
    assert units.steps(UNIT) == [1, 2, 3, 4], "the retained batch went behind a later entry"


def test_one_units_backoff_does_not_hold_another_units_entries(units, clock):
    units.create(UNIT, OTHER)
    units.fault = _failing(lambda unit, kind, data: unit == UNIT and kind == "step/started")
    writer = _writer(units, clock)

    def _submit() -> None:
        writer.submit(UNIT, _step(units, UNIT, 1))
        for n in (1, 2, 3):
            writer.submit(OTHER, _step(units, OTHER, n))

    _on_loop(_submit)
    assert writer.flush(timeout=DEFAULT_LIMITS.retry_backoff_seconds / 2) is False
    assert units.steps(OTHER) == [1, 2, 3], "a wedged unit held another unit's entries"
    assert units.steps(UNIT) == []


# --------------------------------------------------------------------------- #
# The attempt budget, and loss admitted before appending resumes
# --------------------------------------------------------------------------- #


def test_a_spent_attempt_budget_drops_the_batch_and_its_marker_is_written(units, clock, caplog):
    units.create(UNIT)
    units.fault = _failing(_is_step(1))
    writer = _writer(units, clock)
    writer.submit(UNIT, _step(units, UNIT, 1, nbytes=23))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert writer.flush(timeout=60.0), "flush did not terminate against a wedged write"

    assert units.attempts[(UNIT, "step/started")] == DEFAULT_LIMITS.max_write_attempts
    stats = writer.stats()
    assert (stats.dropped, stats.buffered, writer.loss_markers_owed()) == (1, 0, 0)
    assert units.entries(UNIT) == [("write/dropped", {"dropped_count": 1, "dropped_bytes": 23})]
    assert len(_messages(caplog, "gave up on")) == 1
    assert len(_messages(caplog, "landing again")) == 1, "the recovery was not named"


def test_nothing_is_appended_after_a_loss_until_its_marker_lands(units, clock):
    """An entry submitted while a marker is still owed waits behind that marker."""
    units.create(UNIT)
    marker_fails = _failing(_is_marker, times=1)
    step_fails = _failing(_is_step(1))
    units.fault = lambda u, k, d: step_fails(u, k, d) or marker_fails(u, k, d)
    writer = _writer(units, clock, retry_backoff_seconds=1.0, retry_backoff_max_seconds=8.0)
    writer.submit(UNIT, _step(units, UNIT, 1))
    # Retries at +1, +3, +7, +15 and the sixth failure at +23 spends the budget; the
    # marker is tried at once, fails, and is retained until +24.
    assert writer.flush(timeout=23.5) is False
    assert writer.loss_markers_owed() == 1
    assert writer.owes(UNIT)

    writer.submit(UNIT, _step(units, UNIT, 2))
    assert units.steps(UNIT) == [], "an entry was appended ahead of the loss marker"
    assert writer.flush(timeout=10.0)
    assert [kind for kind, _ in units.entries(UNIT)] == ["write/dropped", "step/started"]
    assert units.steps(UNIT) == [2]


def test_a_wedged_filesystem_is_a_counted_loss_and_recovery_adds_none(units, clock, caplog):
    """A filesystem that never answers becomes a reported loss, never a hang."""
    units.create(UNIT)
    units.fault = _failing(lambda unit, kind, data: True)
    writer = _writer(units, clock)
    for n in (1, 2, 3):
        writer.submit(UNIT, _step(units, UNIT, n))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert writer.flush(timeout=600.0) is False, "a marker that cannot land is still owed"
    stats = writer.stats()
    # The three entries, and the marker that could not admit them.
    assert (stats.dropped, stats.buffered, writer.loss_markers_owed()) == (4, 0, 1)
    assert units.entries(UNIT) == []
    assert len(_messages(caplog, "gave up on")) == 1, "a wedged unit was named more than once"

    units.fault = None
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        writer.submit(UNIT, _step(units, UNIT, 4))
        assert writer.flush(timeout=10.0)
    assert units.entries(UNIT) == [
        ("write/dropped", {"dropped_count": 3, "dropped_bytes": 0}),
        ("step/started", {"turn": 1, "step": 4}),
    ]
    assert writer.stats().dropped == 4, "recovery counted a further loss"
    assert _messages(caplog, "landing again")


def test_a_marker_lost_to_its_own_budget_is_written_once_later(units, clock):
    """The debt of a marker that spent its budget folds forward into ONE later marker."""
    units.create(UNIT)
    marker_fails = _failing(_is_marker, times=DEFAULT_LIMITS.max_write_attempts)
    step_fails = _failing(_is_step(1))
    units.fault = lambda u, k, d: step_fails(u, k, d) or marker_fails(u, k, d)
    writer = _writer(units, clock)
    writer.submit(UNIT, _step(units, UNIT, 1, nbytes=17))
    assert writer.flush(timeout=60.0) is False
    assert writer.loss_markers_owed() == 1

    writer.submit(UNIT, _step(units, UNIT, 2))
    assert writer.flush(timeout=60.0)
    assert units.entries(UNIT) == [
        ("write/dropped", {"dropped_count": 1, "dropped_bytes": 17}),
        ("step/started", {"turn": 1, "step": 2}),
    ]


def test_loss_arriving_while_a_marker_is_retained_joins_that_marker(units, clock):
    """A retained marker claims all debt known when it is finally written."""
    units.create(UNIT)
    marker_fails = _failing(_is_marker, times=1)
    step_fails = _failing(_is_step(1))
    units.fault = lambda u, k, d: step_fails(u, k, d) or marker_fails(u, k, d)
    writer = _writer(
        units,
        clock,
        retry_backoff_seconds=1.0,
        retry_backoff_max_seconds=8.0,
        max_pending_count=1,
    )
    writer.submit(UNIT, _step(units, UNIT, 1, nbytes=5))
    assert writer.flush(timeout=23.5) is False, "the marker was meant to be retained"
    # The retained marker fills the one-entry buffer, so this append is rejected at the
    # ceiling: a second loss while the first marker waits.
    assert _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 2, nbytes=7))) is False

    assert writer.flush(timeout=10.0)
    assert units.entries(UNIT) == [("write/dropped", {"dropped_count": 2, "dropped_bytes": 12})]


def test_stats_count_a_marker_owed_from_the_map_and_from_a_retained_job(units, clock):
    """Debt lives in the pending-loss map until a marker job is built, then IN the job."""
    units.create(UNIT, OTHER)
    writer = _writer(
        units,
        clock,
        retry_backoff_seconds=1.0,
        retry_backoff_max_seconds=8.0,
        max_pending_count=0,
    )
    # A rejection records debt in the map, with no marker job built yet.
    assert _on_loop(lambda: writer.submit(OTHER, _step(units, OTHER, 1))) is False
    assert writer.loss_markers_owed() == 1

    marker_fails = _failing(lambda u, k, d: u == UNIT and _is_marker(u, k, d), times=1)
    step_fails = _failing(lambda u, k, d: u == UNIT and _is_step(1)(u, k, d))
    units.fault = lambda u, k, d: step_fails(u, k, d) or marker_fails(u, k, d)
    writer.submit(UNIT, _step(units, UNIT, 1))
    # Stop just before the sixth failure. OTHER's marker landed on the way; nothing of
    # UNIT's has been dropped yet.
    assert writer.flush(timeout=22.5) is False
    assert [kind for kind, _ in units.entries(OTHER)] == ["write/dropped"]
    assert writer.loss_markers_owed() == 0
    # The sixth failure drops the entry; its marker is tried at once, fails, and is
    # retained with the debt inside it, so the map is empty while a marker is owed.
    assert writer.flush(timeout=8.5) is False
    assert writer.loss_markers_owed() == 1, "debt riding in a retained marker was not counted"


# --------------------------------------------------------------------------- #
# A refusal is dropped, never retried
# --------------------------------------------------------------------------- #


def test_a_refused_entry_is_dropped_at_once_and_the_pass_continues_past_it(units, clock):
    units.create(UNIT)
    units.fault = _failing(_is_step(2), exc=_refusal)
    writer = _writer(units, clock)
    _on_loop(lambda: [writer.submit(UNIT, _step(units, UNIT, n, nbytes=n)) for n in (1, 2, 3)])
    assert writer.flush(timeout=10.0)
    assert units.attempts[(UNIT, "step/started")] == 3, "the refusal was retried"
    assert units.entries(UNIT) == [
        ("step/started", {"turn": 1, "step": 1}),
        ("write/dropped", {"dropped_count": 1, "dropped_bytes": 2}),
        ("step/started", {"turn": 1, "step": 3}),
    ]
    assert writer.stats().dropped == 1


def test_a_refusal_on_the_inline_path_is_counted_but_owes_no_marker(units, clock):
    """Preserved divergence: the batch path's refusal owes a marker (above), the inline
    path's does not, though the emitter spec states the marked behaviour as intended."""
    units.create(UNIT)
    units.fault = _failing(_is_step(1), exc=_refusal)
    writer = _writer(units, clock)
    writer.submit(UNIT, _step(units, UNIT, 1))
    stats = writer.stats()
    assert (stats.dropped, writer.loss_markers_owed()) == (1, 0)
    assert not writer.owes(UNIT)
    writer.submit(UNIT, _step(units, UNIT, 2))
    assert units.entries(UNIT) == [("step/started", {"turn": 1, "step": 2})]


# --------------------------------------------------------------------------- #
# The memory ceilings count overflow and hand the job back
# --------------------------------------------------------------------------- #


def test_the_count_ceiling_rejects_at_the_tail_and_runs_none_of_the_jobs_hooks(
    units, clock, caplog
):
    units.create(UNIT)
    writer = _writer(units, clock, max_pending_count=2)
    ran: list[str] = []

    def _submit_all() -> list[bool]:
        return [
            writer.submit(
                UNIT,
                _step(
                    units,
                    UNIT,
                    n,
                    nbytes=10 * n,
                    after=lambda n=n: ran.append(f"after {n}"),
                    on_drop=lambda n=n: ran.append(f"drop {n}"),
                ),
            )
            for n in range(1, 6)
        ]

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        verdicts = _on_loop(_submit_all)
    assert verdicts == [True, True, False, False, False]
    assert ran == [], "a rejected job's hooks ran; its settlement is the caller's"
    stats = writer.stats()
    assert (stats.buffered, stats.overflowed, writer.loss_markers_owed()) == (2, 3, 1)
    assert (writer.overflowed_for(UNIT), writer.overflowed_for(OTHER)) == (3, 0)
    assert len(_messages(caplog, "buffer full")) == 1, "the overflow was named per entry"

    assert writer.flush()
    # The queued prefix kept its order, and the loss leads the unit's next batch.
    assert units.entries(UNIT) == [
        ("write/dropped", {"dropped_count": 3, "dropped_bytes": 120}),
        ("step/started", {"turn": 1, "step": 1}),
        ("step/started", {"turn": 1, "step": 2}),
    ]
    assert ran == ["after 1", "after 2"]


def test_the_byte_ceiling_spans_every_units_bucket(units, clock):
    units.create(UNIT, OTHER, "acp-writer-0003")
    writer = _writer(units, clock, max_pending_bytes=10)
    verdicts = _on_loop(
        lambda: [
            writer.submit(UNIT, _step(units, UNIT, 1, nbytes=4)),
            writer.submit(OTHER, _step(units, OTHER, 1, nbytes=4)),
            writer.submit("acp-writer-0003", _step(units, "acp-writer-0003", 1, nbytes=3)),
        ]
    )
    assert verdicts == [True, True, False], "the process total did not cap the third unit"
    assert [writer.overflowed_for(u) for u in (UNIT, OTHER, "acp-writer-0003")] == [0, 0, 1]


def test_bytes_leave_the_ceiling_exactly_once(units, clock):
    """Admission is how the byte total shows: a total that leaked upward would refuse
    what fits, and one driven negative would admit what does not."""
    third = "acp-writer-0003"
    units.create(UNIT, OTHER, third)
    writer = _writer(units, clock, max_pending_bytes=10)

    # A job dropped on the inline path was never in the total; releasing it anyway
    # would drive the total negative and loosen the ceiling. Across three units, so
    # the PROCESS total decides the last verdict: no one unit's bucket is over.
    units.fault = _failing(_is_step(1), exc=_refusal)
    writer.submit(UNIT, _step(units, UNIT, 1, nbytes=6))
    units.fault = None
    assert _on_loop(
        lambda: [
            writer.submit(OTHER, _step(units, OTHER, 2, nbytes=6)),
            writer.submit(third, _step(units, third, 3, nbytes=5)),
        ]
    ) == [True, False], "the refused inline job's bytes left a total they never entered"
    assert writer.flush()

    # A job retained from the inline path enters the total once, however many times it
    # is retained.
    units.fault = _failing(_is_step(4), times=2)
    writer.submit(UNIT, _step(units, UNIT, 4, nbytes=6))
    assert _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 5, nbytes=5))) is False
    assert writer.flush(timeout=DEFAULT_LIMITS.retry_backoff_seconds * 1.5) is False
    assert _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 6, nbytes=4))) is True
    assert writer.flush(timeout=10.0)

    # Everything landed, so the whole ceiling is free again.
    assert _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 7, nbytes=10))) is True
    assert writer.flush()
    assert (units.steps(UNIT), units.steps(OTHER), units.steps(third)) == ([4, 6, 7], [2], [])


def test_the_exempt_kinds_are_never_refused_at_the_ceiling(units, clock):
    """The file-creating record, a follow-on and a loss marker are O(1) per unit: the
    ceiling bounds payload memory and must not be able to refuse them."""
    units.create(UNIT)
    writer = _writer(units, clock, max_pending_count=0)
    ran: list[str] = []

    def _record(what: str) -> Callable[[], None]:
        def run() -> None:
            ran.append(what)
            log = units.open_unit(UNIT)
            assert log is not None
            log.append("step/started", {"turn": 1, "step": len(ran)}, src="gateway")

        return run

    verdicts = _on_loop(
        lambda: [
            writer.submit(UNIT, WriteJob.append(_record("append"), "an append")),
            writer.submit(UNIT, WriteJob.opening(_record("opening"), "an opening")),
            writer.submit(UNIT, WriteJob.follow_on(_record("follow-on"), "a follow-on")),
        ]
    )
    assert verdicts == [False, True, True]
    assert writer.flush()
    assert ran == ["opening", "follow-on"]
    assert [kind for kind, _ in units.entries(UNIT)] == [
        "write/dropped",
        "step/started",
        "step/started",
    ], "the loss marker that reports the refusal was itself refused"


def test_crossing_the_high_water_mark_is_reported_once_and_sheds_nothing(units, clock, caplog):
    units.create(UNIT)
    writer = _writer(units, clock, pending_high_water=4)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        verdicts = _on_loop(lambda: [writer.submit(UNIT, _step(units, UNIT, n)) for n in range(12)])
    assert all(verdicts)
    assert len(_messages(caplog, "past the 4 mark")) == 1
    assert writer.stats().peak_buffered == 12
    assert writer.flush()
    assert units.steps(UNIT) == list(range(12))
    assert writer.stats().dropped == 0


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #


def test_after_runs_once_when_the_append_lands_and_not_while_it_is_retried(units, clock):
    units.create(UNIT)
    units.fault = _failing(_is_step(1), times=1)
    writer = _writer(units, clock)
    ran: list[str] = []
    writer.submit(UNIT, _step(units, UNIT, 1, after=lambda: ran.append("after")))
    assert ran == [], "a retained job's after ran"
    assert writer.flush(timeout=1.0)
    assert ran == ["after"]


@pytest.mark.parametrize("cause", ["refused", "spent"])
def test_a_permanent_drop_runs_on_drop_ahead_of_after(units, clock, cause):
    units.create(UNIT)
    exc = _refusal if cause == "refused" else None
    units.fault = _failing(_is_step(1), exc=exc)
    writer = _writer(units, clock)
    ran: list[str] = []
    _on_loop(
        lambda: writer.submit(
            UNIT,
            _step(
                units,
                UNIT,
                1,
                after=lambda: ran.append("after"),
                on_drop=lambda: ran.append("drop"),
            ),
        )
    )
    assert writer.flush(timeout=60.0)
    assert ran == ["drop", "after"]


def test_a_hook_that_raises_is_reported_and_the_writer_carries_on(units, clock, caplog):
    units.create(UNIT)
    writer = _writer(units, clock)

    def _boom() -> None:
        raise RuntimeError("cleanup failed")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        writer.submit(UNIT, _step(units, UNIT, 1, after=_boom))
        writer.submit(UNIT, _step(units, UNIT, 2))
    assert units.steps(UNIT) == [1, 2]
    assert _messages(caplog, "finishing step 1")


def test_a_follow_on_dropped_with_its_batch_comes_back_once_uncounted(units, clock):
    """Waiting in its unit's bucket is how a follow-on is lost: the batch it waits in is
    dropped whole. It is submitted once more, behind the marker, and the marker counts
    only the entry that is really missing -- even at a ceiling of zero."""
    units.create(UNIT)
    units.fault = _failing(_is_step(1))
    writer = _writer(units, clock, max_pending_count=0)
    runs = {"n": 0}

    def _follow_on() -> None:
        runs["n"] += 1
        log = units.open_unit(UNIT)
        assert log is not None
        log.append("step/started", {"turn": 1, "step": 99}, src="gateway")

    writer.submit(UNIT, _step(units, UNIT, 1, nbytes=8))
    assert writer.submit(UNIT, WriteJob.follow_on(_follow_on, "a follow-on"))
    assert writer.flush(timeout=60.0)

    assert runs["n"] == 1
    assert units.entries(UNIT) == [
        ("write/dropped", {"dropped_count": 1, "dropped_bytes": 8}),
        ("step/started", {"turn": 1, "step": 99}),
    ]
    assert writer.stats().dropped == 1, "the re-submitted follow-on was counted as lost"


def test_a_re_submitted_follow_on_is_not_re_submitted_again(units, clock):
    """One extra attempt per loss: a copy that re-armed itself would follow a wedged disk
    around its retry budget for as long as the disk stayed wedged."""
    units.create(UNIT)
    units.fault = _failing(lambda unit, kind, data: kind == "step/started")
    writer = _writer(units, clock)
    runs = {"n": 0}

    def _follow_on() -> None:
        runs["n"] += 1
        raise OSError(errno.EIO, "still wedged")

    writer.submit(UNIT, _step(units, UNIT, 1))
    writer.submit(UNIT, WriteJob.follow_on(_follow_on, "a follow-on"))
    writer.flush(timeout=600.0)
    # Never attempted in the original batch (the entry ahead of it failed first), then
    # one full attempt budget as the re-submitted copy, and no more.
    assert runs["n"] == DEFAULT_LIMITS.max_write_attempts
    assert writer.flush(timeout=600.0)
    assert runs["n"] == DEFAULT_LIMITS.max_write_attempts


def test_growth_is_announced_once_per_landed_batch(units, clock):
    units.create(UNIT, OTHER)
    grew: list[str] = []
    writer = _writer(units, clock, on_grew=grew.append)
    _on_loop(lambda: [writer.submit(UNIT, _step(units, UNIT, n)) for n in (1, 2, 3)])
    assert writer.flush()
    assert grew == [UNIT]
    units.fault = _failing(
        lambda unit, kind, data: unit == OTHER and kind == "step/started", exc=_refusal
    )
    _on_loop(lambda: writer.submit(OTHER, _step(units, OTHER, 1)))
    assert writer.flush()
    # The refused batch landed nothing and said nothing; the marker batch after it is
    # growth, announced once.
    assert grew == [UNIT, OTHER]


def test_a_growth_listener_that_raises_is_reported_and_the_drain_continues(units, clock, caplog):
    units.create(UNIT)

    def _broken(unit: str) -> None:
        raise RuntimeError("listener blew up")

    writer = _writer(units, clock, on_grew=_broken)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 1)))
        assert writer.flush()
        _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 2)))
        assert writer.flush()
    assert units.steps(UNIT) == [1, 2]
    assert _messages(caplog, "growth listener")


# --------------------------------------------------------------------------- #
# A write that stalls is named by a producer
# --------------------------------------------------------------------------- #


def test_a_write_stuck_past_the_threshold_is_named_once_by_a_producer(units, clock, caplog):
    """The thread that would notice is the one blocked in the call, so the NEXT producer
    names it -- once per stall, and again for a later one."""
    units.create(UNIT, OTHER)
    writer = _writer(units, clock, write_stall_secs=30.0)

    def _other() -> WriteJob:
        return WriteJob.follow_on(lambda: None, "a later append")

    def _stalls() -> None:
        named = len(_messages(caplog, "neither returned nor failed"))
        clock.now += 29.0
        writer.submit(OTHER, _other())
        assert len(_messages(caplog, "neither returned nor failed")) == named, "named early"
        clock.now += 2.0
        writer.submit(OTHER, _other())
        writer.submit(OTHER, _other())

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        writer.submit(UNIT, WriteJob.append(_stalls, "a write that stalls"))
        assert len(_messages(caplog, "neither returned nor failed")) == 1
        assert _messages(caplog, "1 append(s) are waiting behind it")
        # The write returned, so a second stall is a new fact and is named again.
        writer.submit(UNIT, WriteJob.append(_stalls, "a second write that stalls"))
    assert len(_messages(caplog, "neither returned nor failed")) == 2
    assert writer.flush()


# --------------------------------------------------------------------------- #
# owes()
# --------------------------------------------------------------------------- #


def test_a_unit_owes_while_anything_of_its_is_queued_retained_or_unadmitted(units, clock):
    units.create(UNIT, OTHER)
    writer = _writer(units, clock, retry_backoff_seconds=1.0, retry_backoff_max_seconds=8.0)
    assert not writer.owes(UNIT)
    _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 1)))
    assert writer.owes(UNIT) and not writer.owes(OTHER)
    assert writer.flush()
    assert not writer.owes(UNIT)

    marker_fails = _failing(_is_marker, times=1)
    step_fails = _failing(_is_step(2))
    units.fault = lambda u, k, d: step_fails(u, k, d) or marker_fails(u, k, d)
    writer.submit(UNIT, _step(units, UNIT, 2))
    assert writer.owes(UNIT), "a retained batch is owed"
    assert writer.flush(timeout=23.5) is False
    assert writer.owes(UNIT), "a loss not yet admitted is owed"
    assert writer.flush(timeout=10.0)
    assert not writer.owes(UNIT)


def test_a_running_job_is_not_told_it_owes_itself_but_sees_what_is_behind_it(units, clock):
    units.create(UNIT)
    writer = _writer(units, clock)
    seen: list[bool] = []

    def _asks() -> None:
        seen.append(writer.owes(UNIT))

    _on_loop(
        lambda: [
            writer.submit(UNIT, WriteJob.append(_asks, "first")),
            writer.submit(UNIT, WriteJob.append(_asks, "second")),
        ]
    )
    assert writer.flush()
    assert seen == [True, False]


def test_a_pass_that_stops_early_releases_what_it_never_reached(units, clock):
    """A batch retained mid-pass leaves no claim behind: once everything lands, nothing
    is owed."""
    units.create(UNIT)
    units.fault = _failing(_is_step(1), times=1)
    writer = _writer(units, clock)
    _on_loop(lambda: [writer.submit(UNIT, _step(units, UNIT, n)) for n in (1, 2, 3)])
    assert writer.flush(timeout=DEFAULT_LIMITS.retry_backoff_seconds / 2) is False
    assert writer.owes(UNIT)
    assert writer.flush(timeout=1.0)
    assert units.steps(UNIT) == [1, 2, 3]
    assert not writer.owes(UNIT), "a claim on entries the first pass never reached leaked"


# --------------------------------------------------------------------------- #
# The shutdown drain
# --------------------------------------------------------------------------- #


def test_shutdown_writes_what_is_buffered(units, clock):
    units.create(UNIT)
    writer = _writer(units, clock)
    _on_loop(lambda: [writer.submit(UNIT, _step(units, UNIT, n)) for n in (1, 2, 3)])
    assert writer.drain_for_shutdown(5.0) == DrainReport(True, 0, 0, False)
    assert units.steps(UNIT) == [1, 2, 3]


def test_shutdown_paces_a_backoff_that_outlives_its_deadline_inside_it(units, clock):
    """Clamped, never collapsed: a retry scheduled beyond the deadline is attempted one
    slice of the budget after the drain began, and later attempts a slice apart."""
    units.create(UNIT)
    attempts_at: list[float] = []

    def _fault(unit: str, kind: str, data: dict) -> BaseException | None:
        if kind != "step/started":
            return None
        attempts_at.append(clock.now)
        return OSError(errno.EIO, "busy") if len(attempts_at) < 4 else None

    units.fault = _fault
    writer = _writer(units, clock, retry_delay=lambda attempts: 3600.0)
    writer.submit(UNIT, _step(units, UNIT, 1))
    started = clock.now

    report = writer.drain_for_shutdown(3.0)

    assert report.drained, report
    assert units.steps(UNIT) == [1]
    slice_secs = 3.0 / DEFAULT_LIMITS.max_write_attempts
    offsets = [at - started for at in attempts_at]
    assert len(offsets) == 4 and offsets[0] == 0.0
    for attempt, offset in enumerate(offsets[1:], start=1):
        # Never ahead of its slice (collapsed), never past the deadline (waited out).
        assert attempt * slice_secs - 1e-9 <= offset < 3.0, (attempt, offsets)


def test_shutdown_names_a_marker_it_could_not_land(units, clock, caplog):
    units.create(UNIT)
    units.fault = _failing(lambda unit, kind, data: True)
    writer = _writer(units, clock)
    writer.submit(UNIT, _step(units, UNIT, 1))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        report = writer.drain_for_shutdown(0.5)
    assert report == DrainReport(False, 0, 1, False)
    assert _messages(caplog, "0 append(s) buffered, 1 loss marker(s) owed")


def test_shutdown_counts_a_marker_still_waiting_inside_its_retained_job(units, clock, caplog):
    """The debt rides inside a retained marker job; a count of the map alone reads zero.

    A budget no window can spend keeps the marker retained, so the report describes that
    lifecycle point and no other.
    """
    units.create(UNIT)
    refused = _failing(_is_step(1), exc=_refusal)
    marker_fails = _failing(_is_marker)
    units.fault = lambda u, k, d: refused(u, k, d) or marker_fails(u, k, d)
    writer = _writer(units, clock, max_write_attempts=10_000, second_chance_drain_seconds=0.01)
    _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 1)))
    assert writer.flush(timeout=0.01) is False
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        report = writer.drain_for_shutdown(0.01)
    assert report == DrainReport(False, 1, 1, False)
    assert _messages(caplog, "1 append(s) buffered, 1 loss marker(s) owed")


def test_a_hung_write_holds_the_backlog_and_a_bounded_shutdown_says_so(units, pool, caplog):
    """A call that never returns advances no attempt counter: the backlog grows, nothing
    is shed, and a bounded shutdown reports rather than waits."""
    units.create(UNIT)
    writer = _writer(units, executor=lambda: pool)
    entered = threading.Event()
    release = threading.Event()

    def _hangs() -> None:
        entered.set()
        release.wait(30.0)

    try:
        _on_loop(lambda: writer.submit(UNIT, WriteJob.append(_hangs, "a write that hangs")))
        assert entered.wait(20.0), "the writer never picked up the hanging job"
        _on_loop(lambda: [writer.submit(UNIT, _step(units, UNIT, n)) for n in range(64)])
        stats = writer.stats()
        assert stats.buffered == 64 and stats.dropped == 0 and stats.peak_buffered == 64
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            report = writer.drain_for_shutdown(0.2)
        assert report == DrainReport(False, 64, 0, True)
        assert _messages(caplog, "did not finish writing")
    finally:
        release.set()
    assert writer.flush(timeout=20.0)
    assert units.steps(UNIT) == list(range(64))


def test_a_hung_write_delays_another_unit_without_reordering_or_losing_it(units, pool):
    """One worker drains every unit: bucketing protects order and content, not latency."""
    units.create(UNIT, OTHER)
    writer = _writer(units, executor=lambda: pool)
    entered = threading.Event()
    release = threading.Event()

    def _hangs() -> None:
        entered.set()
        release.wait(30.0)

    try:
        _on_loop(lambda: writer.submit(UNIT, WriteJob.append(_hangs, "a write that hangs")))
        assert entered.wait(20.0)
        _on_loop(lambda: [writer.submit(OTHER, _step(units, OTHER, n)) for n in (1, 2, 3)])
        assert units.steps(OTHER) == [], "another unit wrote while the only worker was held"
        assert writer.stats().dropped == 0
    finally:
        release.set()
    assert writer.drain_for_shutdown(20.0).drained
    assert units.steps(OTHER) == [1, 2, 3]


def test_a_shutdown_wakes_a_writer_parked_on_a_long_backoff(units, pool):
    """The writer computes its pause BEFORE a shutdown asks for quiescence, so the
    shutdown has to wake it for the clamped deadline to be read at all.

    The backoff is five times the drain's deadline, so only a wake can land the entry in
    time -- and short enough that a regression costs the run one bounded pause at pool
    teardown, never a parked worker for the rest of it."""
    units.create(UNIT)
    units.fault = _failing(_is_step(1), times=1)
    writer = _writer(units, executor=lambda: pool, retry_delay=lambda attempts: 30.0)
    _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 1)))
    assert writer.flush(timeout=0.2) is False, "the failure did not park the writer"
    assert writer.drain_for_shutdown(6.0).drained, "the parked writer was never woken"
    assert units.steps(UNIT) == [1]


def test_a_cancelled_pass_does_not_read_as_a_batch_in_flight(units):
    """Shutting a pool down cancels a queued pass, which then never clears its flag. The
    barriers read the pass's future instead, so the shutdown drain writes inline."""
    units.create(UNIT)

    class _CancellingPool:
        def submit(self, fn: Callable[[], None]) -> Future:
            future: Future = Future()
            future.cancel()
            return future

    writer = _writer(units, executor=lambda: _CancellingPool())
    _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 1)))
    assert not writer.stats().batch_in_flight
    assert writer.drain_for_shutdown(5.0).drained
    assert units.steps(UNIT) == [1]


def test_a_writer_whose_pool_is_gone_still_drains_at_shutdown(units, caplog):
    units.create(UNIT)

    def _gone() -> Any:
        raise RuntimeError("cannot schedule new futures after shutdown")

    writer = _writer(units, executor=_gone)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 1)))
    assert _messages(caplog, "scheduling the crew log writer")
    assert writer.drain_for_shutdown(5.0).drained
    assert units.steps(UNIT) == [1]


def test_entries_from_a_loop_land_in_order_on_the_writer_thread(units, pool):
    units.create(UNIT, OTHER)
    writer = _writer(units, executor=lambda: pool)
    threads: set[str] = set()

    def _job(unit: str, n: int) -> WriteJob:
        def run() -> None:
            threads.add(threading.current_thread().name)
            log = units.open_unit(unit)
            assert log is not None
            log.append("step/started", {"turn": 1, "step": n}, src="gateway")

        return WriteJob.append(run, f"step {n}")

    _on_loop(
        lambda: [writer.submit(unit, _job(unit, n)) for n in range(40) for unit in (UNIT, OTHER)]
    )
    assert writer.flush(timeout=20.0)
    assert units.steps(UNIT) == list(range(40))
    assert units.steps(OTHER) == list(range(40))
    assert all(name.startswith("test-crewlog") for name in threads), threads


def test_claimed_bytes_still_count_toward_the_ceiling(units, pool):
    units.create(UNIT, OTHER)
    writer = _writer(units, executor=lambda: pool, max_pending_bytes=10)
    entered = threading.Event()
    release = threading.Event()

    def _holds() -> None:
        entered.set()
        release.wait(30.0)

    try:
        _on_loop(lambda: writer.submit(UNIT, WriteJob.append(_holds, "held", nbytes=8)))
        assert entered.wait(20.0)
        assert writer.stats().buffered == 0, "the held job is claimed, not buffered"
        assert _on_loop(lambda: writer.submit(OTHER, _step(units, OTHER, 1, nbytes=3))) is False
    finally:
        release.set()
    assert writer.flush(timeout=20.0)


# --------------------------------------------------------------------------- #
# close()
# --------------------------------------------------------------------------- #


def test_close_waits_its_bound_then_discards_what_could_not_land(units, clock):
    units.create(UNIT)
    units.fault = _failing(_is_step(1))
    writer = _writer(units, clock, retry_backoff_seconds=1.0, retry_backoff_max_seconds=8.0)
    ran: list[str] = []
    writer.submit(UNIT, _step(units, UNIT, 1, after=lambda: ran.append("after")))
    assert writer.close(timeout=2.0) is False
    stats = writer.stats()
    assert (stats.buffered, stats.dropped, writer.loss_markers_owed()) == (0, 0, 0)
    assert not writer.owes(UNIT)
    assert ran == [], "a discarded job's hooks ran"


def test_close_on_a_quiet_writer_reports_quiet(units, clock):
    units.create(UNIT)
    writer = _writer(units, clock)
    writer.submit(UNIT, _step(units, UNIT, 1))
    assert writer.close() is True
    assert units.steps(UNIT) == [1]


def test_forget_drops_a_units_overflow_tally(units, clock):
    units.create(UNIT)
    writer = _writer(units, clock, max_pending_count=0)
    _on_loop(lambda: writer.submit(UNIT, _step(units, UNIT, 1)))
    assert writer.overflowed_for(UNIT) == 1
    writer.forget(UNIT)
    assert writer.overflowed_for(UNIT) == 0
    assert writer.stats().overflowed == 1, "the process-wide count is history, not per unit"


def test_the_counters_never_scan_the_backlog(units, clock, monkeypatch):
    """``stats()`` and ``overflowed_for`` are what producers read around each record, on
    the event loop, while holding the writer's lock. They answer from counters; only
    ``loss_markers_owed()`` walks the buffered jobs.

    Kept as a call-count pin: the cost is the contract, and a wall-clock bound on a
    backlog would be a flaky test.
    """
    units.create(UNIT)
    writer = _writer(units, clock, max_pending_count=1)
    _on_loop(lambda: [writer.submit(UNIT, _step(units, UNIT, n)) for n in (1, 2)])
    scans = {"n": 0}
    real = writer._owed_loss_markers_locked

    def _counting() -> int:
        scans["n"] += 1
        return real()

    monkeypatch.setattr(writer, "_owed_loss_markers_locked", _counting)
    stats = writer.stats()
    assert (stats.buffered, stats.overflowed, writer.overflowed_for(UNIT)) == (1, 1, 1)
    assert scans["n"] == 0, "a counter read scanned the buffered jobs"
    assert writer.loss_markers_owed() == 1 and scans["n"] == 1


# --------------------------------------------------------------------------- #
# Leases survive a failure raised inside another
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO], ids=["ENOSPC", "EIO"])
def test_a_write_error_raised_while_handling_another_holds_no_lease(units, clock, caplog, code):
    """The failure the writer keeps must not reach a frame through ANY chain.

    The injected error is raised inside an ``except`` block, so it carries the first
    exception as ``__context__`` -- and that one's traceback holds the job frames, whose
    locals hold the handle. The writer keeps only the failure's TYPE and renders the
    debug record to text, so the fixture's no-lease-held check holds even with every
    record captured at DEBUG.
    """
    units.create(UNIT)

    def _chained() -> BaseException:
        try:
            raise RuntimeError("the state the write was attempted in")
        except RuntimeError as inner:
            outer = OSError(code, "write failed")
            outer.__context__ = inner
        try:
            return outer
        finally:
            del outer

    def _fault(unit: str, kind: str, data: dict) -> BaseException | None:
        return _chained() if kind == "step/started" else None

    units.fault = _fault
    writer = _writer(units, clock)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        writer.submit(UNIT, _step(units, UNIT, 1))
        assert writer.flush(timeout=60.0)
    assert writer.stats().dropped == 1
    rendered = [
        r for r in caplog.records if r.levelno == logging.DEBUG and "failed:" in r.getMessage()
    ]
    assert rendered, "the debug arm never logged a later failure"
    assert "Traceback (most recent call last)" in rendered[0].getMessage()
    assert all(r.exc_info is None for r in caplog.records), "a record carried the exception"


# --------------------------------------------------------------------------- #
# WarningBudget
# --------------------------------------------------------------------------- #


def _budget(clock: _Clock, **kw: Any) -> WarningBudget:
    return WarningBudget(logging.getLogger(LOGGER), clock=clock, **kw)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_second_kind_of_failure_is_named_even_after_an_earlier_one(clock, caplog):
    budget = _budget(clock)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        budget.report("growth listener", RuntimeError("listener blew up"), op="growth-listener")
        budget.report("appending an entry", OSError(28, "No space left"), op="crew-append")
        budget.report("appending an entry", OSError(5, "Input/output error"), op="crew-append")
        budget.report("opening the crew log", RuntimeError("lease lost"), op="crew-log-open")
    seen = _warnings(caplog)
    assert len(seen) == 4, seen
    for needle in ("growth listener", "No space left", "Input/output error", "lease lost"):
        assert any(needle in m for m in seen), needle


def test_one_kind_repeating_is_named_once(clock, caplog):
    budget = _budget(clock)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(25):
            budget.report("appending an entry", OSError(28, "full"), op="crew-append")
    assert len(_warnings(caplog)) == 1


def test_many_units_failing_for_one_cause_are_named_once(clock, caplog):
    budget = _budget(clock)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for n in range(40):
            # Each failure names its own file, as a real OSError does: the key must not.
            budget.report(
                f"appending crew_report for crew 'store-{n}'",
                OSError(errno.ENOSPC, "No space left on device", f"store-{n}/log.jsonl"),
                op="crew-report-append",
            )
    assert len(_warnings(caplog)) == 1


def test_a_spent_budget_says_how_many_failures_it_swallowed(clock, caplog):
    budget = _budget(clock, rearm_seconds=300.0)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(7):
            budget.report("appending an entry", OSError(28, "full"), op="crew-append")
        clock.now += 299.0
        budget.report("appending an entry", OSError(28, "full"), op="crew-append")
        assert len(_warnings(caplog)) == 1, "the window had not passed yet"
        clock.now += 2.0
        budget.report("appending an entry", OSError(28, "full"), op="crew-append")
    seen = _warnings(caplog)
    assert len(seen) == 2
    assert "7 more went unreported" in seen[1], seen[1]


def test_the_first_warning_scopes_its_own_promise_to_its_kind(clock, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _budget(clock).report("appending an entry", OSError(28, "full"), op="crew-append")
    (line,) = _warnings(caplog)
    assert "of this kind" in line


def test_the_budget_map_is_bounded_oldest_kind_first(clock, caplog):
    budget = _budget(clock, max_kinds=3)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for n in range(1, 5):
            budget.report("appending an entry", OSError(n, f"errno {n}"), op="crew-append")
        # Kind 1 was evicted by kind 4, so it speaks again; kind 4 is still held.
        budget.report("appending an entry", OSError(1, "errno 1"), op="crew-append")
        budget.report("appending an entry", OSError(4, "errno 4"), op="crew-append")
    assert len(_warnings(caplog)) == 5


def test_the_kind_is_the_operation_the_class_and_the_code_never_the_unit():
    kind = WarningBudget.kind
    key = kind("crew-append", OSError(28, "full"))
    assert key == kind("crew-append", OSError(28, "full"))
    assert key != kind("crew-log-open", OSError(28, "full"))
    assert key != kind("crew-append", OSError(5, "io"))
    assert kind("crew-append", RuntimeError("x")) != kind("crew-append", ValueError("x"))
    assert kind("op", lg.CrewLogError("x", code=lg.CODE_BAD_DATA))[2] == lg.CODE_BAD_DATA
    assert "store-1" not in "".join(key)


def test_a_repeat_is_logged_at_debug_as_text_without_the_exception(clock, caplog):
    budget = _budget(clock)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for _ in range(2):
            try:
                raise OSError(28, "full")
            except OSError as exc:
                budget.report("appending an entry", exc, op="crew-append")
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug) == 1
    assert "Traceback (most recent call last)" in debug[0].getMessage()
    assert all(r.exc_info is None for r in caplog.records)


def test_the_writer_reports_through_the_budget_and_logger_it_is_given(units, clock, caplog):
    units.create(UNIT)
    named = logging.getLogger("kiro_crew.crew_log.emit")
    budget = WarningBudget(named, clock=clock)
    writer = CrewLogWriter(
        units.open_unit, clock=clock, sleep=clock.sleep, warnings=budget, logger=named
    )
    units.fault = _failing(_is_step(1))
    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
        writer.submit(UNIT, _step(units, UNIT, 1))
        assert writer.flush(timeout=60.0)
    assert {r.name for r in caplog.records} == {"kiro_crew.crew_log.emit"}
    assert _messages(caplog, "session log writes are failing")
    assert _messages(caplog, "gave up on")
