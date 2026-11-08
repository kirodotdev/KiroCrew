"""Which thread records a crew log health metric -- the constraint, over real paths.

``test/metrics/test_crew_log_health_losses_folds.py`` pins the counters' shape. This file pins
the one property the shape was chosen FOR, and it does it over the real append,
fold and read paths rather than over the recording helpers:

*   THE APPEND PATH RECORDS NOTHING. ``eager.note_commit`` is the whole share the
    crew log's append path pays -- one ``put_nowait`` -- and a turn waits on it.
    Its loss branch increments a counter and stops there; the telemetry side
    READS that counter later, from the metrics reader's own thread. So a dropped
    wake moves ``eager.dropped_wakes()`` and makes no metrics call at all.
*   THE EAGER WORKER CLAIMS ITS OWN THREAD, which is what lets the fold record
    sites refuse every other one.
*   A DASHBOARD READ FOLDS WITHOUT RECORDING. The three fold-start branches are
    shared between eager folding and a read, and only the eager side is counted,
    so ``fold.start`` stays a count of eager folding rather than of reads.
*   EACH ORIGIN IS REACHED, for both scopes, through the real branch: a warm
    cell, a savepoint on disk, and a cold rebuild.
*   A FOLD THAT RAISES MID-APPLY counts no apply. ``fold.start`` above
    ``fold.apply`` is how a failing fold shows up.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from crew_log_fold_eager_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    WORK_SLOT,
    _isolated_home,
    _slot_log,
    _work_entry,
)

from kiro_crew.crew_log import eager
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import slot_prime
from kiro_crew.metrics import crew_log_health as health
from kiro_crew.subprocess_utf8 import UTF8_TEXT

SESSION = "acp-health"

#: The repository root, so the subprocess probe below resolves ``PYTHONPATH=src``.
_REPO_ROOT = Path(__file__).resolve().parent.parent


class _Rec:
    """Recorder double capturing counter calls, with the thread each ran on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    def counter(self, name, value=1, *, unit="1", attrs=None, **_kw):
        self.calls.append((name, dict(attrs or {}), threading.current_thread().name))

    def attrs_for(self, name: str) -> "list[dict]":
        return [a for n, a, _t in self.calls if n == name]

    def threads_for(self, name: str) -> "set[str]":
        return {t for n, _a, t in self.calls if n == name}


@pytest.fixture()
def rec(monkeypatch):
    r = _Rec()
    monkeypatch.setattr("kiro_crew.metrics.provider.get_recorder", lambda: r)
    return r


@pytest.fixture(autouse=True)
def _no_claim_left_behind():
    """No test leaves its own thread claimed as the fold worker.

    A claim that leaked would make the next case's read path record a fold, which
    is the exact mistake these tests exist to catch.
    """
    health._claim.fold_worker = False
    yield
    health._claim.fold_worker = False


# --------------------------------------------------------------------------- #
# The append path
# --------------------------------------------------------------------------- #


def test_a_dropped_wake_on_the_append_path_records_no_metric(rec, monkeypatch):
    """The append path's loss branch counts; it does not record.

    ``_enqueue`` is patched to refuse, which is what a full fold queue does to a
    wake, and ``note_commit`` is called DIRECTLY -- it is the one call the append
    path makes, so calling it is standing on that path.
    """

    def full(_wake):
        raise queue.Full

    monkeypatch.setattr(eager, "_enqueue", full)
    before = eager.dropped_wakes()
    for seq in range(1, 4):
        eager.note_commit(SESSION, "turn/started", seq)
    assert eager.dropped_wakes() == before + 3, "the count the telemetry side reads"
    assert rec.calls == [], "the append path must make no metrics call"


def test_the_append_path_never_reaches_the_metrics_module_at_all(tmp_path):
    """The strongest form, and the only one a gated no-op cannot fake.

    A recorder spy on this thread proves little: every record site returns at
    once on an unclaimed thread, so someone adding a ``note_fold_*`` call to
    ``note_commit`` would leave the spy empty and the suite green while having
    added exactly the work this PR promises not to add. So this runs the append
    path in a SUBPROCESS and asserts the metrics module was never even imported
    by it -- which no thread-local gate can satisfy.
    """
    probe = (
        "import sys\n"
        "from kiro_crew.crew_log import eager\n"
        # The fold WORKER imports the metrics module on purpose, through its
        # thread claim -- that is where recording is allowed. ``note_commit``
        # starts that worker, so leaving it live would race this probe's print
        # and fail on correct code. Stubbed to a no-op, the thread still starts
        # and the only importer left is the append path itself, which is the one
        # this probe is about.
        "eager._run = lambda: None\n"
        "eager.note_commit('acp-probe', 'turn/started', 1)\n"
        "eager.note_commit('acp-probe', 'turn/completed', 2)\n"
        "print('health' if 'kiro_crew.metrics.crew_log_health' in sys.modules else 'clean')\n"
    )
    env = dict(os.environ, PYTHONPATH="src", KIROCREW_HOME=str(tmp_path / "home"))
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", probe],
        capture_output=True,
        timeout=120,
        env=env,
        cwd=_REPO_ROOT,
        # Pins the decode to UTF-8 rather than the Windows ANSI code page, which
        # is what the subprocess-encoding gate requires; it carries text=True.
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "clean", done.stdout


def test_a_committed_wake_on_the_append_path_records_no_metric(rec):
    """The happy path too: a wake that lands is one ``put_nowait``, not a metric.

    The worker is stopped first and `drain` waited on afterwards, so the
    assertion is not merely racing the worker to the recorder: either the wake
    was folded or it is still queued, and neither may have recorded.
    """
    eager.stop_for_tests()
    eager.note_commit(SESSION, "turn/started", 1)
    eager.drain(timeout=5.0)
    assert rec.calls == []


def test_the_append_path_does_not_consult_the_fold_worker_gate(rec, monkeypatch):
    """Not even the thread-local CHECK belongs on the append path.

    A call that was gated off would record nothing and still cost the append
    path a lookup, so the gate itself is spied on rather than only the recorder.
    """
    asked: "list[str]" = []
    real = health.on_fold_worker_thread

    def spy() -> bool:
        asked.append("x")
        return real()

    monkeypatch.setattr(health, "on_fold_worker_thread", spy)

    def full(_wake):
        raise queue.Full

    monkeypatch.setattr(eager, "_enqueue", full)
    eager.note_commit(SESSION, "turn/started", 1)
    assert asked == [], "the append path must not even ask which thread it is on"


def test_the_loss_the_append_path_counted_becomes_the_metrics_total(monkeypatch):
    """The count kept on the append path is what ``write.lost`` reports.

    Read through the module's own reader, so the hand-off between the two threads
    is the one production uses: the append path adds to ``eager._dropped``, the
    telemetry side folds the delta in.
    """

    def full(_wake):
        raise queue.Full

    monkeypatch.setattr(eager, "_enqueue", full)
    health.reset_for_tests()
    try:
        health.write_lost_totals()  # baseline, as the first collection is
        eager.note_commit(SESSION, "turn/started", 1)
        assert health.write_lost_totals()["wake_dropped"] == 1
    finally:
        health.reset_for_tests()


# --------------------------------------------------------------------------- #
# The eager worker's claim
# --------------------------------------------------------------------------- #


def test_the_eager_worker_claims_its_own_thread(monkeypatch):
    """``_run`` claims before it folds anything, and claims ITS thread.

    The loop is entered with the stop flag already up, so the body never runs and
    the claim is the only thing observed. The claim is restored afterwards because
    the flag is process-wide.
    """
    claimed: list[threading.Thread] = []

    def spy():
        claimed.append(threading.current_thread())

    monkeypatch.setattr(health, "claim_fold_worker_thread", spy)
    eager.stop_for_tests()
    eager._stopping.set()
    try:
        worker = threading.Thread(target=eager._run, name="crew-log-eager-fold-probe")
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
    finally:
        eager._stopping.clear()
    assert claimed == [worker], "the worker claims the thread it folds on"


def test_a_thread_that_did_not_claim_records_nothing(rec):
    """Only the claim lets a thread record, so an unclaimed one is silent."""
    assert health.on_fold_worker_thread() is False
    started = health.begin_fold_pass()
    health.note_fold_origin("slot", "cold")
    health.note_fold_pass("slot", ("work",), started)
    health.note_fold_apply("slot", "work", started)
    assert rec.calls == []


# --------------------------------------------------------------------------- #
# The real fold branches: three origins, two scopes
# --------------------------------------------------------------------------- #


def _a_slot_with_work() -> None:
    """One slot unit carrying two ``work/recorded`` entries."""
    handle = _slot_log("acp-health-slot", WORK_SLOT)
    _work_entry(handle, "it-1")
    _work_entry(handle, "it-2")


def _a_session_with_turns(unit_id: str = SESSION) -> None:
    """One session unit with a turn, which every session fold moves on."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew")
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="gateway")


def test_a_read_folds_the_same_branches_and_records_nothing(rec):
    """The read path shares every fold-start branch and is not eager folding."""
    _a_slot_with_work()
    _a_session_with_turns()
    crew_log.read_slot_projection(WORK_SLOT, "work")  # cold, then warm
    crew_log.read_slot_projection(WORK_SLOT, "work")
    crew_log.fold_session_warm(SESSION)
    crew_log.fold_session_warm(SESSION)
    assert rec.calls == [], "a read must not look like a fold the worker did"


def test_a_cold_then_warm_slot_fold_records_both_origins(rec):
    """The first pass streams every unit; the second continues the live cell.

    Driven through ``eager._advance``, which is where a pass is CLOSED: the fold
    branches only leave a mark, so going through the real eager entry point is
    the only way the mark becomes a point -- and it is the production path.
    """
    _a_slot_with_work()
    health.claim_fold_worker_thread()
    eager._advance(WORK_SLOT, "work")  # cold: handed to the background prime
    assert slot_prime.drain(timeout=30.0), "the background prime never finished"
    eager._advance(WORK_SLOT, "work")
    starts = rec.attrs_for(health.FOLD_START)
    assert {"scope": "slot", "origin": "cold"} in starts
    assert {"scope": "slot", "origin": "warm"} in starts
    assert len(starts) == 2, "one start per pass, no more"


def test_a_slot_fold_resumed_from_disk_records_the_savepoint_origin(rec):
    """A cell dropped from memory with its savepoint on disk resumes, not refolds.

    The unit is grown past the savepoint floor first, because a write is only
    earned once a fold has advanced that far: a shorter log leaves nothing on
    disk and the next pass is honestly cold.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    handle = _slot_log("acp-health-savepoint", WORK_SLOT)
    for nth in range(savepoints.MIN_ADVANCE_ENTRIES + 4):
        _work_entry(handle, f"it-{nth}")
    crew_log.read_slot_projection(WORK_SLOT, "work")  # earns and writes the savepoint
    crew_log.forget_slot_folds()  # the cell is gone, the file beside it is not
    health.claim_fold_worker_thread()
    eager._advance(WORK_SLOT, "work")
    assert rec.attrs_for(health.FOLD_START) == [{"scope": "slot", "origin": "savepoint"}]


def test_a_session_fold_resumed_from_disk_records_the_savepoint_origin(rec):
    """The session scope reaches the savepoint branch the same way the slot does."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog
    from kiro_crew.crew_log import checkpoint as savepoints

    handle = CrewLog.create(lg.KIND_SESSION, SESSION, owner="raymond", agent="kirocrew")
    for turn in range(savepoints.MIN_ADVANCE_ENTRIES + 4):
        handle.append(
            "turn/started", {"turn": turn + 1, "actor": "user", "depth": 0}, src="gateway"
        )
    crew_log.fold_session_warm(SESSION)  # earns and writes the savepoint
    crew_log.forget_session_folds(SESSION)  # the memo is gone, the file beside it is not
    health.claim_fold_worker_thread()
    eager._advance_session(SESSION)
    assert rec.attrs_for(health.FOLD_START) == [{"scope": "session", "origin": "savepoint"}]


def test_a_cold_then_warm_session_fold_records_both_origins(rec):
    """The session scope reaches its own branches, keyed by one unit's file."""
    _a_session_with_turns()
    health.claim_fold_worker_thread()
    eager._advance_session(SESSION)
    eager._advance_session(SESSION)
    starts = rec.attrs_for(health.FOLD_START)
    assert {"scope": "session", "origin": "cold"} in starts
    assert {"scope": "session", "origin": "warm"} in starts
    assert len(starts) == 2


def test_one_pass_records_exactly_one_start(rec):
    """Over-counting a start would make the cold share unreadable."""
    _a_session_with_turns()
    health.claim_fold_worker_thread()
    eager._advance_session(SESSION)
    assert len(rec.attrs_for(health.FOLD_START)) == 1


# --------------------------------------------------------------------------- #
# fold.apply over the eager advance
# --------------------------------------------------------------------------- #


def test_an_applied_slot_fold_is_counted_once_with_its_name(rec):
    """``eager._advance`` is the eager worker's slot pass."""
    _a_slot_with_work()
    health.claim_fold_worker_thread()
    eager._advance(WORK_SLOT, "work")
    assert slot_prime.drain(timeout=30.0), "the background prime never finished"
    applied = rec.attrs_for(health.FOLD_APPLY)
    assert len(applied) == 1
    assert applied[0]["scope"] == "slot"
    assert applied[0]["fold"] == "work"
    assert applied[0]["bucket"] in health.BUCKETS


def test_a_slot_fold_that_raises_mid_apply_records_its_start_and_no_apply(rec, monkeypatch):
    """A fold that died AFTER choosing its start is the failing-fold signal.

    The raise is planted past the start branch (`_persist_slot_fold` runs after
    the cold fold has been stamped), so the pass really does get far enough to
    stamp an origin. Replacing `read_slot_projection` wholesale instead would
    mean the fold never started, and the assertion would hold for the wrong
    reason.
    """
    from kiro_crew.crew_log import projection_slot_memo

    _a_slot_with_work()

    def boom(*_a, **_k):
        raise RuntimeError("fold exploded mid-apply")

    monkeypatch.setattr(projection_slot_memo, "_persist_slot_fold", boom)
    health.claim_fold_worker_thread()
    eager._advance(WORK_SLOT, "work")  # cold, so the prime folds; it swallows the raise
    assert slot_prime.drain(timeout=30.0), "the background prime never finished"
    assert rec.attrs_for(health.FOLD_START) == [{"scope": "slot", "origin": "cold"}]
    assert rec.attrs_for(health.FOLD_APPLY) == [], "nothing was applied"


def test_a_fold_that_never_started_records_nothing_at_all(rec, monkeypatch):
    """No start was chosen, so there is no pass to describe -- not even a start."""
    _a_slot_with_work()

    def boom(*_a, **_k):
        raise RuntimeError("could not even begin")

    monkeypatch.setattr(crew_log, "read_slot_projection", boom)
    health.claim_fold_worker_thread()
    eager._advance(WORK_SLOT, "work")
    assert rec.calls == []


def test_an_idle_slot_pass_records_its_start_and_no_apply(rec):
    """A pass an unchanged live cell answered advanced nothing.

    The slot scope has to reach that answer through its own idle mark, where the
    session scope reads it off `warm.changed`. Counting an apply whenever the
    fold does not raise would report three applies for the three passes below
    over an unchanging board, and the `start` above `apply` signal would say
    nothing on this scope.
    """
    _a_slot_with_work()
    health.claim_fold_worker_thread()
    eager._advance(WORK_SLOT, "work")  # the real fold, on the background prime
    assert slot_prime.drain(timeout=30.0), "the background prime never finished"
    first = len(rec.attrs_for(health.FOLD_APPLY))
    eager._advance(WORK_SLOT, "work")  # nothing changed in between
    eager._advance(WORK_SLOT, "work")
    assert first == 1, "the pass that folded counts one apply"
    assert len(rec.attrs_for(health.FOLD_APPLY)) == 1, "the idle passes count none"
    assert len(rec.attrs_for(health.FOLD_START)) == 3, "every pass counts its start"


def test_a_read_that_got_there_first_leaves_the_worker_nothing_to_apply(rec):
    """Documented consequence: `apply` counts what the WORKER's pass advanced.

    When a dashboard read reaches a growing cell before the worker, the read does
    the folding and the worker's pass behind it finds nothing changed. That pass
    is a start with no apply, which is why the two series together describe eager
    folding rather than all folding.
    """
    _a_session_with_turns()
    crew_log.fold_session_warm(SESSION)  # the read, on an unclaimed thread
    assert rec.calls == []
    health.claim_fold_worker_thread()
    eager._advance_session(SESSION)
    assert rec.attrs_for(health.FOLD_START) == [{"scope": "session", "origin": "warm"}]
    assert rec.attrs_for(health.FOLD_APPLY) == []


def test_an_applied_session_pass_counts_each_fold_that_moved(rec):
    """One walk of one file serves every session fold, so the pass is the duration."""
    _a_session_with_turns()
    health.claim_fold_worker_thread()
    eager._advance_session(SESSION)
    applied = rec.attrs_for(health.FOLD_APPLY)
    assert applied, "a unit with a turn moves at least one session fold"
    assert {a["scope"] for a in applied} == {"session"}
    assert {a["fold"] for a in applied} <= health.FOLD_LABELS
    assert len(applied) == len({a["fold"] for a in applied}), "one point per fold"


def test_a_session_pass_that_moved_nothing_counts_no_apply(rec):
    """A pass over an unchanged file applied nothing, whatever it cost to find out."""
    _a_session_with_turns()
    eager._advance_session(SESSION)  # unclaimed: warms the cell, records nothing
    assert rec.calls == []
    health.claim_fold_worker_thread()
    eager._advance_session(SESSION)
    assert rec.attrs_for(health.FOLD_APPLY) == []


def test_the_eager_advance_records_on_the_calling_thread_only(rec):
    """Every point is stamped with the thread that folded, never another.

    The cold pass is folded by the background prime and stamped with ITS thread; the
    warm pass after it is folded by the caller and stamped with the caller's.
    """
    handle = _slot_log("acp-health-slot", WORK_SLOT)
    _work_entry(handle, "it-1")
    _work_entry(handle, "it-2")
    health.claim_fold_worker_thread()
    eager._advance(WORK_SLOT, "work")
    assert slot_prime.drain(timeout=30.0), "the background prime never finished"
    primed = rec.threads_for(health.FOLD_APPLY)
    assert len(primed) == 1 and threading.current_thread().name not in primed
    assert all(name.startswith("crew-log-slot-prime") for name in primed)
    rec.calls.clear()
    _work_entry(handle, "it-3")
    eager._advance(WORK_SLOT, "work")
    assert rec.threads_for(health.FOLD_APPLY) == {threading.current_thread().name}
