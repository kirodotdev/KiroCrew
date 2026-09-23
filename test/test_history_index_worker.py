"""The session index writer must be a real separate process, and stay supervised.

Three properties, and each has its own failure mode in production:

* the indexing runs OUTSIDE this process — that is the whole point of the change,
  and a regression to a thread would be invisible except as gateway latency;
* a child that dies is replaced, and one that dies over and over is given up on
  rather than restarted forever, because search degrades to the scan path and a
  restart loop would hide that in a log;
* a child is terminated when the gateway stops, so a gateway restart does not
  leave a second writer behind competing for the same database.

The first three tests spawn real processes. That is deliberate: a mocked
``Process`` would assert the supervisor's bookkeeping while proving nothing about
spawn actually working, and spawn re-imports the module in a fresh interpreter,
which is exactly the step that breaks if the child's imports are wrong.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from kiro_crew import history_index_worker
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import INDEX_FILENAME, SessionSearchIndex
from kiro_crew.history_index_worker import SessionIndexWorkerSupervisor

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")


#: Long enough for the child's own stop check, short enough to fail fast.
_TEARDOWN_GRACE_SECS = 10


def _is_ours(child):
    """Whether this live process is one these tests spawned.

    Matched by the name the supervisor gives its child, so a stray belonging to
    another test is never touched.
    """
    return child.name.startswith("kirocrew-session-index")


@pytest.fixture(autouse=True)
def _leave_no_children():
    """Leave this pytest worker owning none of the processes these tests spawn.

    These are the only tests here that start REAL child processes, and they run
    inside a pytest-xdist worker that is itself a process. A worker that still
    owns a live child when it shuts down is a worker whose exit status nobody
    should have to reason about, and the shard runs with
    ``--max-worker-restart=0`` so a worker that dies badly stays red even when
    every test passed.

    Only children this module named are touched, so a stray belonging to another
    test is left alone. Calling ``active_children`` at all is also what reaps
    any already-finished child, which is why it runs even when the list is empty.
    """
    yield
    for child in [c for c in multiprocessing.active_children() if _is_ours(c)]:
        child.terminate()
        child.join(timeout=_TEARDOWN_GRACE_SECS)
        if child.is_alive():
            # SIGTERM was not enough. A child blocked in SQLite or waiting on the
            # per-session lock does not reach its next stop check, and a bounded
            # join that simply expires would hand a LIVE child to this worker's
            # exit -- the one outcome this fixture exists to prevent.
            child.kill()
            child.join(timeout=_TEARDOWN_GRACE_SECS)
    leftover = sorted(c.name for c in multiprocessing.active_children() if _is_ours(c))
    assert not leftover, (
        f"teardown left an indexer alive: {leftover}. The pytest worker must not own "
        f"one at exit; the shard runs --max-worker-restart=0."
    )


#: Bounded waits, so a broken child fails the test instead of hanging it.
_SPAWN_TIMEOUT_SECS = 60.0
_POLL_SECS = 0.1


def _settle(sessions, *keys):
    """Backdate transcripts past the backfill quiet window.

    ``backfill_index`` defers a session whose file changed within
    ``history_search._INDEX_QUIET_WINDOW_SECS``: it is treated as still being
    written. These tests append and expect the CHILD to index in the same
    breath, and a monkeypatch does not reach a spawned interpreter, so the
    file's own mtime is moved back instead -- the one signal the deferral reads
    that crosses the process boundary.
    """
    from kiro_crew import history_search

    settled = time.time() - history_search._INDEX_QUIET_WINDOW_SECS - 600.0
    for key in keys:
        os.utime(sessions / f"{key}.jsonl", (settled, settled))


def _seeded_sessions(tmp_path):
    """A session directory with transcripts and no index yet."""
    sessions = tmp_path / "sessions"
    log = ConversationLog(base_dir=sessions)
    log.append("alpha", "user", "a session about deployment contention")
    log.append("beta", "user", "a session about astronomy and telescopes")
    _settle(sessions, "alpha", "beta")
    return sessions


def _indexed_keys(sessions):
    """Read the index from THIS process, without writing to it."""
    index = SessionSearchIndex(sessions / ".index" / INDEX_FILENAME)
    try:
        return index.indexed_keys() if index.available else set()
    finally:
        index.close()


def _wait_until(predicate, timeout_secs=_SPAWN_TIMEOUT_SECS):
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(_POLL_SECS)
    return None


def _supervisor(sessions):
    """A supervisor whose child cycles fast enough for a test to observe it."""
    return SessionIndexWorkerSupervisor(
        sessions,
        pass_budget_secs=5.0,
        busy_pause_secs=0.1,
        idle_pause_secs=0.3,
        optimize_every_passes=10_000,
    )


def test_the_indexer_writes_from_a_separate_process(tmp_path):
    """The rows appear, and the process that wrote them is not this one."""
    sessions = _seeded_sessions(tmp_path)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    try:
        child_pid = supervisor.pid
        assert child_pid is not None
        assert child_pid != os.getpid(), "the indexer must not run in this process"

        # Wait for the WHOLE corpus, not merely for the index to become
        # non-empty: the child writes one row per session, so a check that
        # accepts any non-empty set races its second write on a busy box.
        assert _wait_until(
            lambda: {"alpha", "beta"} <= _indexed_keys(sessions)
        ), f"child did not index the corpus; got {_indexed_keys(sessions)}"
    finally:
        _stop(supervisor)


def _stop(supervisor, *, grace_secs=None):
    """Stop a supervisor the way production does: request inline, then reap.

    The gateway is on an event loop, so it calls the two halves separately --
    ``request_stop`` inline and ``reap`` in a thread. Tests go through the same
    pair rather than a wrapper no caller outside the tests would have used.
    """
    supervisor.request_stop()
    if grace_secs is None:
        supervisor.reap()
    else:
        supervisor.reap(grace_secs=grace_secs)


def _own_children():
    """Live children this module spawned, by the name the supervisor gives them.

    Portable evidence that a child has gone: ``multiprocessing`` answers it
    without a pid probe, and ``os.kill(pid, 0)`` is rejected on Windows.
    """
    return [
        c for c in multiprocessing.active_children() if c.name.startswith("kirocrew-session-index")
    ]


def test_a_sessions_dir_that_is_not_a_real_directory_starts_no_child(tmp_path, monkeypatch):
    """A path that does not exist must not be created, least of all in the CWD.

    The child receives the directory as a string and resolves it in its own
    process, whose CWD it inherited. So a relative or bogus value would be
    created wherever the parent happened to be running -- for a test run, inside
    the checkout. Pinned with the shape that actually caused it: ``str`` of an
    object that is not a path at all.
    """
    monkeypatch.chdir(tmp_path)
    before = set(os.listdir(tmp_path))

    supervisor = SessionIndexWorkerSupervisor(Path(str(object())))

    assert supervisor.start() is False
    assert supervisor.pid is None
    assert _own_children() == []
    assert set(os.listdir(tmp_path)) == before, "starting the indexer created something"


def test_the_supervisor_rejects_a_relative_sessions_dir(tmp_path, monkeypatch):
    """A relative directory is refused even when it exists.

    It resolves against the CWD of whichever process reads it, and the parent's
    CWD is not the child's contract.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sessions").mkdir()

    supervisor = SessionIndexWorkerSupervisor(Path("sessions"))

    assert supervisor.start() is False
    assert _own_children() == []


def test_a_spawn_that_keeps_failing_gives_up_after_a_healthy_child_dies(tmp_path, monkeypatch):
    """Repeated spawn failures must exhaust the restart budget, not retry forever.

    The dangerous ordering is a child that ran healthy and THEN died, because a
    healthy death deliberately resets the rapid-restart count. If the failed
    respawn left the dead child's start time in place, every later failure would
    measure that gone process's uptime, look like another healthy death, reset the
    count again and retry at the minimum backoff indefinitely -- while a spawn
    failure means the host is short of exactly the memory and threads this module
    exists to give back.
    """
    import kiro_crew.history_index_worker as worker_module

    class _AlwaysFailsToSpawn:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise OSError("simulated EAGAIN: cannot allocate a new process")

    class _Ctx:
        Process = _AlwaysFailsToSpawn

    monkeypatch.setattr(
        worker_module.multiprocessing, "get_context", lambda _name: _Ctx(), raising=True
    )

    supervisor = _supervisor(tmp_path)
    # A child that had been up long enough to count as healthy, and has now died.
    supervisor._started_at = time.monotonic() - (worker_module._HEALTHY_UPTIME_SECS + 1.0)
    supervisor._process = None

    polls = 0
    budget = worker_module._MAX_RAPID_RESTARTS + 5
    while not supervisor.gave_up and polls < budget:
        supervisor.poll()
        polls += 1

    assert (
        supervisor.gave_up
    ), f"still restarting after {polls} failed spawns; the restart budget never ran out"
    assert supervisor.pid is None
    assert _own_children() == []


# ---------------------------------------------------------------------------
# The child's own body, exercised IN THIS PROCESS.
#
# Everything below runs in a spawned child in production, and a spawned child is
# not measured by coverage or reachable by an assertion. These call the same
# functions directly, so the loop, its failure branch and its waits are covered
# by tests that can actually see them.
# ---------------------------------------------------------------------------


@pytest.fixture
def _restore_signal_handlers():
    """Put SIGTERM and SIGINT back after a test that runs the child body.

    ``run_index_worker`` installs its own handlers, and it is the child's whole
    process in production. In-process that would leak into the rest of the run.
    """
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def test_the_child_logging_bootstrap_adds_one_handler_and_sets_the_level():
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    try:
        root.handlers = []
        history_index_worker._configure_child_logging(logging.ERROR)
        assert len(root.handlers) == 1, "a spawned child inherits no handlers"
        assert root.level == logging.ERROR
        # Called twice, it must not stack a second handler on the same stream.
        history_index_worker._configure_child_logging(logging.WARNING)
        assert len(root.handlers) == 1
        assert root.level == logging.WARNING
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)


def test_the_wait_returns_true_when_the_pause_simply_elapses():
    stopping = threading.Event()
    assert history_index_worker._wait_or_retire(stopping, 0.05) is True


def test_the_wait_returns_false_when_asked_to_stop():
    stopping = threading.Event()
    stopping.set()
    assert history_index_worker._wait_or_retire(stopping, 30.0) is False


def test_the_wait_returns_false_when_the_parent_has_gone(monkeypatch):
    """The orphan case: nobody asked this child to stop, its gateway just left.

    The check slice is shortened so this costs milliseconds. The production slice
    is what bounds how long an orphan keeps indexing; the behaviour under test is
    that the parent is re-checked DURING the pause rather than after it, which a
    long pause with a short slice demonstrates either way.
    """
    monkeypatch.setattr(history_index_worker, "_PARENT_CHECK_SECS", 0.01)
    monkeypatch.setattr(history_index_worker, "_parent_is_gone", lambda: True)
    stopping = threading.Event()

    started = time.monotonic()
    assert history_index_worker._wait_or_retire(stopping, 30.0) is False
    # Returned on the first slice, not after the 30-second pause.
    assert time.monotonic() - started < 5.0


def test_the_parent_check_says_present_when_asked_from_the_parent():
    """In this process there is no parent_process, and the ppid is not init."""
    assert history_index_worker._parent_is_gone() is False


def test_the_child_body_indexes_and_closes(tmp_path, monkeypatch, _restore_signal_handlers):
    """One pass through the real loop, in this process.

    Stopped by making the wait decline to continue, which is the same exit the
    child takes on SIGTERM.
    """
    sessions = _seeded_sessions(tmp_path)
    monkeypatch.setattr(history_index_worker, "_wait_or_retire", lambda *_a, **_k: False)

    history_index_worker.run_index_worker(str(sessions), optimize_every_passes=1)

    assert _indexed_keys(sessions), "the child body indexed nothing"


def test_the_child_body_survives_a_failing_pass(tmp_path, monkeypatch, _restore_signal_handlers):
    """A pass that raises is logged and does not escape the child.

    The failure branch takes the same wait as the success branch, so a child whose
    passes keep failing still notices a parent that has gone.
    """
    import kiro_crew.history_search as history_search

    calls = []

    def _boom(self, *, budget_secs=5.0):
        calls.append(budget_secs)
        raise RuntimeError("simulated index failure")

    monkeypatch.setattr(history_search.SessionCatalogProjection, "backfill_index", _boom)
    waits = []

    def _wait(_stopping, pause):
        waits.append(pause)
        return False

    monkeypatch.setattr(history_index_worker, "_wait_or_retire", _wait)

    history_index_worker.run_index_worker(str(_seeded_sessions(tmp_path)))

    assert calls, "the pass was never attempted"
    assert waits == [
        history_index_worker.INDEX_IDLE_PAUSE_SECS
    ], "a failed pass must take the idle pause"


def test_a_failed_first_spawn_still_retries_with_backoff(tmp_path, monkeypatch):
    """A transient failure at boot must not disable indexing for the whole run.

    The first spawn and a later death go through the same machinery, so a boot-time
    EAGAIN costs one backoff rather than the gateway's lifetime. A refusal that
    retrying cannot fix -- an absent transcript directory -- is separate: that one
    gives up inside ``start`` and is pinned by its own test.
    """
    attempts = []

    class _FailsOnce:
        def __init__(self, *args, **kwargs):
            attempts.append(1)

        def start(self):
            if len(attempts) == 1:
                raise OSError("simulated EAGAIN on the first spawn")

        def is_alive(self):
            return True

        @property
        def pid(self):
            return -1

    class _Ctx:
        Process = _FailsOnce

    monkeypatch.setattr(
        history_index_worker.multiprocessing, "get_context", lambda _name: _Ctx(), raising=True
    )

    supervisor = _supervisor(_seeded_sessions(tmp_path))

    assert supervisor.start() is False, "the first spawn was supposed to fail"
    assert supervisor.gave_up is False, "a transient spawn failure is not terminal"

    # The poll loop is what the gateway runs, and it retries.
    supervisor.poll()

    assert len(attempts) == 2, "the failed first spawn was never retried"
    assert supervisor.pid == -1


def test_stop_terminates_the_child(tmp_path):
    sessions = _seeded_sessions(tmp_path)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    child_pid = supervisor.pid
    assert child_pid is not None

    _stop(supervisor)

    assert supervisor.pid is None
    assert _own_children() == [], "the child outlived stop()"


def test_stop_is_idempotent(tmp_path):
    sessions = _seeded_sessions(tmp_path)
    supervisor = _supervisor(sessions)
    supervisor.start()
    _stop(supervisor)
    _stop(supervisor)  # must not raise on an already-reaped child
    assert supervisor.pid is None


def test_a_killed_child_is_replaced(tmp_path):
    """A crash is not the end of indexing: the supervisor starts a new child."""
    sessions = _seeded_sessions(tmp_path)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    try:
        first_pid = supervisor.pid
        assert first_pid is not None

        # Kill it the way multiprocessing does, not with a signal number:
        # ``Process.kill`` is SIGKILL on POSIX and TerminateProcess on Windows,
        # where ``signal.SIGKILL`` does not exist at all.
        supervisor._process.kill()

        # Death is observed through the supervisor rather than by probing the pid:
        # a killed child of THIS process stays a zombie until it is reaped, and a
        # zombie still answers a liveness probe. ``Process.is_alive`` is what
        # reaps it, and that is what the ``pid`` property consults.
        assert _wait_until(lambda: supervisor.pid is None), "child never observed as dead"

        second_pid = _wait_until(lambda: (supervisor.poll(), supervisor.pid)[1])
        assert second_pid is not None
        assert second_pid != first_pid
        assert supervisor.gave_up is False
    finally:
        _stop(supervisor)


def test_a_started_supervisor_does_not_start_a_second_child(tmp_path):
    sessions = _seeded_sessions(tmp_path)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    try:
        first_pid = supervisor.pid
        assert supervisor.start() is True, "start on a live child is a no-op success"
        assert supervisor.pid == first_pid
    finally:
        _stop(supervisor)


# --------------------------------------------------------------- bookkeeping


class _DeadProcess:
    """A process that was never alive, for driving the restart bookkeeping.

    The give-up path needs many consecutive rapid deaths; spawning real
    interpreters to observe it would add seconds per death and prove nothing the
    real-process tests above do not already prove about spawn.
    """

    exitcode = 1

    def is_alive(self) -> bool:
        return False

    def join(self, timeout=None) -> None:
        return None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def close(self) -> None:
        return None


def _supervisor_with_dead_children(tmp_path, monkeypatch):
    supervisor = SessionIndexWorkerSupervisor(tmp_path / "sessions")

    def fake_start() -> bool:
        if supervisor.gave_up:
            return False
        # A deliberate stand-in for a spawned process, not a SpawnProcess.
        supervisor._process = cast(Any, _DeadProcess())
        supervisor._started_at = time.monotonic()
        return True

    monkeypatch.setattr(supervisor, "start", fake_start)
    fake_start()
    return supervisor


def test_the_supervisor_gives_up_on_a_crash_looping_child(tmp_path, monkeypatch):
    """Restarting forever would hide a permanently broken indexer in a log."""
    supervisor = _supervisor_with_dead_children(tmp_path, monkeypatch)

    for _ in range(history_index_worker._MAX_RAPID_RESTARTS + 1):
        supervisor.poll()

    assert supervisor.gave_up is True
    assert supervisor.pid is None


def test_the_restart_backoff_grows(tmp_path, monkeypatch):
    """A crash loop must back off rather than respawn interpreters in a tight loop."""
    supervisor = _supervisor_with_dead_children(tmp_path, monkeypatch)

    waits = [supervisor.poll() for _ in range(3)]

    assert waits == sorted(waits), f"backoff must not shrink: {waits}"
    assert waits[-1] > waits[0]
    assert waits[-1] <= history_index_worker._RESTART_BACKOFF_MAX_SECS


def test_a_child_that_stayed_up_resets_the_backoff(tmp_path, monkeypatch):
    """One crash after a long healthy run is not a crash loop."""
    supervisor = _supervisor_with_dead_children(tmp_path, monkeypatch)
    supervisor.poll()
    assert supervisor._rapid_restarts == 1

    # Pretend the replacement child has been up well past the healthy threshold.
    supervisor._started_at = time.monotonic() - (history_index_worker._HEALTHY_UPTIME_SECS + 1)
    supervisor.poll()

    assert supervisor._rapid_restarts == 0
    assert supervisor.gave_up is False


def test_a_stopped_supervisor_will_not_start_again(tmp_path):
    """Shutdown is final: a late poll must not resurrect the child."""
    supervisor = SessionIndexWorkerSupervisor(tmp_path / "sessions")
    _stop(supervisor)
    assert supervisor.start() is False
    supervisor.poll()
    assert supervisor.pid is None


# ------------------------------------------------------------------- pacing


BUSY = 0.1
IDLE = 60.0


def _pause(report):
    return history_index_worker._pause_for(report, busy_pause_secs=BUSY, idle_pause_secs=IDLE)


@pytest.mark.parametrize(
    "report, expected, why",
    [
        # The report shape on a tree without the quiet window: no `deferred` key
        # at all. The child must not require one.
        (
            {"indexed": 0, "dropped": 0, "remaining": 0},
            IDLE,
            "caught up, no deferred key",
        ),
        (
            {"indexed": 1, "dropped": 0, "remaining": 3},
            BUSY,
            "work pending, no deferred key",
        ),
        # The report shape once the quiet window lands: `deferred` is reported
        # separately and deliberately kept OUT of `remaining`.
        (
            {"indexed": 0, "dropped": 0, "remaining": 0, "deferred": 0},
            IDLE,
            "caught up",
        ),
        (
            {"indexed": 0, "dropped": 0, "remaining": 4, "deferred": 0},
            BUSY,
            "work this loop can service",
        ),
        (
            {"indexed": 0, "dropped": 0, "remaining": 0, "deferred": 53},
            IDLE,
            "deferrals wait on the clock, not on this loop",
        ),
        (
            {"indexed": 0, "dropped": 0, "remaining": 2, "deferred": 53},
            BUSY,
            "some work is servicable now",
        ),
        # Degenerate: an empty report must not raise.
        ({}, IDLE, "no counts at all"),
    ],
)
def test_pacing_reads_whatever_backfill_reports(report, expected, why):
    assert _pause(report) == expected, why


def test_a_fleet_of_live_sessions_does_not_spin_at_the_busy_cadence():
    """The regression this guards: 53 live sessions must not pin the busy pause.

    Folding deferrals into ``remaining`` would make every pass look like it had
    work, so the child would re-stat the whole window every couple of seconds
    forever while indexing nothing.
    """
    deferred_only = {"indexed": 0, "dropped": 0, "remaining": 0, "deferred": 53}
    assert _pause(deferred_only) == IDLE


# ------------------------------------------------- non-blocking shutdown split


def test_request_stop_does_not_wait_for_the_child(tmp_path):
    """The half a coroutine may call inline must not join.

    ``request_stop`` is what the gateway's supervisor task calls on the event
    loop, so it may only do the non-blocking things: one ``waitpid`` and one
    SIGTERM. If it ever joined, a gateway shutdown would freeze the loop for
    seconds — the failure this whole change exists to remove.
    """
    sessions = _seeded_sessions(tmp_path)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    try:
        assert supervisor.pid is not None

        started = time.monotonic()
        supervisor.request_stop()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"request_stop blocked for {elapsed:.2f}s"
        # It asked, it did not reap: the process object is still held so that
        # ``reap`` can wait for it.
        assert supervisor._process is not None
    finally:
        _stop(supervisor)


def test_request_stop_then_reap_terminates_the_child(tmp_path):
    """The two halves together do what the synchronous ``stop`` does."""
    sessions = _seeded_sessions(tmp_path)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    child_pid = supervisor.pid
    assert child_pid is not None

    supervisor.request_stop()
    supervisor.reap()

    assert supervisor.pid is None
    assert supervisor._process is None
    assert _own_children() == [], "the child outlived request_stop + reap"


def test_reap_alone_still_terminates_an_unasked_child(tmp_path):
    """``reap`` terminates first, so it is safe without a prior request."""
    sessions = _seeded_sessions(tmp_path)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    child_pid = supervisor.pid
    assert child_pid is not None

    supervisor.reap()

    assert supervisor.pid is None
    assert _own_children() == [], "reap did not reap an unasked child"


def test_request_stop_and_reap_are_safe_with_no_child(tmp_path):
    supervisor = SessionIndexWorkerSupervisor(tmp_path / "sessions")
    supervisor.request_stop()  # must not raise
    supervisor.reap()  # must not raise
    assert supervisor.pid is None
    assert supervisor.start() is False, "request_stop marks the supervisor stopped"


# --------------------------------------------------------- two writers, one DB


def test_a_contended_write_waits_instead_of_failing(tmp_path):
    """Two connections writing one WAL index: the second waits, it does not fail.

    This store now has two writing processes — the child does the backfill, and
    the gateway still drops a deleted session's row on the delete path. So a
    write that meets another writer's transaction must become a WAIT rather than
    an exception, which is what ``busy_timeout`` (set in ``_ensure_open``) buys.

    ``sync`` swallows its exceptions by design (a lost row costs one scanned
    file), so a failure here would be silent. The row landing is therefore the
    only honest proof the contended write did not fail.
    """
    db_path = tmp_path / "session_index.db"
    holder = SessionSearchIndex(db_path)
    writer = SessionSearchIndex(db_path)
    assert holder.available and writer.available

    hold_secs = 0.3
    outcome: dict[str, object] = {}

    def contended_write() -> None:
        # Its own thread, so it gets its own connection (they are thread-local).
        try:
            writer.sync(
                "contended",
                mtime_ns=1,
                size=1,
                dev=1,
                ino=1,
                texts=["a write that had to wait for the other writer"],
            )
            outcome["ok"] = True
        except Exception as exc:  # pragma: no cover - would be a real regression
            outcome["error"] = repr(exc)

    try:
        # Take the database's write lock and hold it.
        conn = holder._ensure_open()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SELECT 1").fetchone()

        thread = threading.Thread(target=contended_write, name="contended-writer")
        started = time.monotonic()
        thread.start()
        time.sleep(hold_secs)
        conn.execute("COMMIT")
        thread.join(timeout=30.0)
        elapsed = time.monotonic() - started

        assert not thread.is_alive(), "the contended write never finished"
        assert "error" not in outcome, f"contended write raised: {outcome.get('error')}"
        assert elapsed >= hold_secs, "the write did not actually contend"
        # The proof: the row is there, so the write waited out the lock rather
        # than being swallowed as a failure.
        assert "contended" in writer.indexed_keys()
    finally:
        holder.close()
        writer.close()


# ----------------------------------------------- delete vs reindex, two processes


def test_the_child_honours_the_cross_process_session_lock(tmp_path):
    """The invariant the whole design rests on, proved across the boundary.

    The index keeps a compressed copy of each session's message text, so an
    indexer that overtakes a delete leaves that text readable after the
    transcript is gone. `index_session` holds the session's own `_locked(key)`
    for the whole stat -> read -> write, and `delete_session` removes the row
    inside the same lock before unlinking. Before this change both sides were
    threads of ONE process, so the guarantee could have rested on the in-process
    RLock and nothing would have noticed; now it rests on the `flock` being
    genuinely cross-process.

    So this holds the lock HERE and asserts the child cannot index that session
    while it is held, then that it can once released. That is a direct test of
    participation rather than of a lucky interleaving: drop `_locked` from the
    child's write path and the first assertion fails immediately.

    The session is appended INSIDE the held lock (reentrant per key per thread),
    so there is no window in which the child could index it before the lock is
    taken.
    """
    sessions = _seeded_sessions(tmp_path)
    log = ConversationLog(base_dir=sessions)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    try:
        # The child is live and passing over the directory before we start.
        assert _wait_until(lambda: {"alpha", "beta"} <= _indexed_keys(sessions))

        with log._locked("gamma"):
            log.append("gamma", "user", "a session written while its lock is held")
            # Settled while the lock is still held, so the only thing keeping
            # the child off this session below is the lock itself.
            _settle(sessions, "gamma")
            # The child now sees an unindexed session it cannot lock. Give it
            # several passes (its busy pause is 0.1 s here) to try and fail.
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                assert "gamma" not in _indexed_keys(sessions), (
                    "the child indexed a session whose cross-process lock was held "
                    "elsewhere -- the delete-vs-reindex ordering is not closed"
                )
                time.sleep(_POLL_SECS)

        # Released: the same child must now pick it up, which also proves the
        # first half was a lock and not simply a child that never got there.
        assert _wait_until(
            lambda: "gamma" in _indexed_keys(sessions)
        ), "the child never indexed the session after its lock was released"
    finally:
        _stop(supervisor)
        index = log._catalog_projection._index
        if index is not None:
            index.close()


def test_a_deleted_session_does_not_come_back_while_the_child_runs(tmp_path):
    """A delete through the real path, with the indexer live throughout."""
    sessions = _seeded_sessions(tmp_path)
    log = ConversationLog(base_dir=sessions)
    supervisor = _supervisor(sessions)
    assert supervisor.start() is True
    try:
        assert _wait_until(lambda: {"alpha", "beta"} <= _indexed_keys(sessions))

        assert log.delete_session("alpha") is True

        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            assert "alpha" not in _indexed_keys(
                sessions
            ), "a deleted session's indexed text came back"
            time.sleep(_POLL_SECS)

        # The sibling is untouched: the lock is per session, not a global stall.
        assert "beta" in _indexed_keys(sessions)
        assert not (sessions / "alpha.jsonl").exists()
    finally:
        _stop(supervisor)
        index = log._catalog_projection._index
        if index is not None:
            index.close()
