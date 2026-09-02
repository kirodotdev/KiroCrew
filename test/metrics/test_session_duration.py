"""``kirocrew.session.duration`` + ``kirocrew.session.started``.

Drives the REAL production helpers in ``metrics/sessions.py`` with a patched
recorder and a redirected data home, so the crumb lifecycle, the exactly-once
consumption, the end_reason enum and the crashed back-fill all live in
production code -- a change there fails these tests instead of passing green.
"""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.metrics import sessions as sess


class _CapturingRecorder:
    """Stand-in recorder that captures histogram() and counter() calls."""

    #: The crumb is only written under telemetry consent, and this double stands
    #: in for a CONSENTED recorder. ``TestConsentGate`` drives the other state.
    enabled = True

    def __init__(self) -> None:
        self.hist: list = []
        self.counters: list = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.hist.append({"name": name, "value": value, "unit": unit, "attrs": dict(attrs or {})})

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "value": value, "attrs": dict(attrs or {})})


@pytest.fixture(autouse=True)
def clean_live_registry():
    """The live-start table is process-global; leaking it across tests is ordering bugs."""
    with sess._live_lock:
        sess._live_starts.clear()
    yield
    with sess._live_lock:
        sess._live_starts.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the crumb directory and transcript store at a temp data home."""
    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def rec():
    r = _CapturingRecorder()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def _crumbs(home):
    d = home / "metrics" / "open-sessions"
    return sorted(d.glob("*.json")) if d.is_dir() else []


_UNLINK_SPELLINGS = ("_unlink(", "_unlink_generations(", "_unlink_all(")


def _unlink_is_outside_the_table_lock(src: str) -> bool:
    """True when no crumb unlink is nested inside a ``with _live_lock:`` block.

    Indentation-aware rather than a substring window: the block is the run of
    lines indented deeper than the ``with`` itself, which is what "inside" means.

    Every spelling the module uses is checked, not just the leaf ``_unlink(``: the
    end paths now hand their paths to ``_unlink_generations``, so a gate that only
    knew the leaf name would have stopped detecting anything and read as green.
    """
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "with _live_lock:":
            continue
        indent = len(line) - len(line.lstrip())
        for follower in lines[i + 1 :]:
            if not follower.strip():
                continue
            if len(follower) - len(follower.lstrip()) <= indent:
                break  # block ended
            if any(spelling in follower for spelling in _UNLINK_SPELLINGS):
                return False
    return True


def _start(key):
    """Drive ``record_session_started`` from a sync test.

    It is a coroutine because the crumb write is awaited on a worker thread -- off
    the event loop, but ordered, because the caller holds the session registry
    lock across the await. ``asyncio.run`` gives it the loop that
    ``asyncio.to_thread`` needs.
    """
    asyncio.run(sess.record_session_started(key))


def _end(key, end_reason):
    """Drive ``record_session_ended`` from a sync test.

    A coroutine for the same reason the start is: the crumb unlink is a filesystem
    syscall and must not run on the event loop, and awaiting the worker hop is the
    only shape that takes it off the loop without ever dropping it.
    """
    asyncio.run(sess.record_session_ended(key, end_reason=end_reason))


def _end_many(keys, end_reason):
    """Drive the bulk end path -- ``close_all`` and the other mass-teardown pops."""
    asyncio.run(sess.record_sessions_ended(keys, end_reason=end_reason))


def _discard(key):
    """Drive ``discard_session_start`` -- a rolled-back start, off the loop."""
    asyncio.run(sess.discard_session_start(key))


def _write_crumb(home, key, started_at, *, owner_pid=None):
    """Leave behind the crumb a CRASHED process would leave.

    Goes through the production writer, which only writes while the key's
    generation is installed, then drops the generation again -- the crash itself,
    which takes the whole in-memory table with it and is precisely why the crumb
    on disk is the only surviving evidence.

    The writer stamps THIS process as the owner, and the backfill skips crumbs
    whose owner is still alive -- correctly, since a live owner means a live
    session. So the ownership is rewritten to *owner_pid* (default: absent, i.e. a
    crumb from before ownership was recorded) to model a process that is gone.
    """
    with sess._live_lock:
        sess._live_starts[key] = started_at
    sess._write_crumb(key, started_at)
    with sess._live_lock:
        sess._live_starts.pop(key, None)
    found = _crumbs(home)
    assert found, "helper must have produced a crumb"
    payload = json.loads(found[0].read_text())
    if owner_pid is None:
        payload.pop("pid", None)
        payload.pop("start_id", None)
    else:
        payload["pid"] = owner_pid
    found[0].write_text(json.dumps(payload))


def _duration_calls(rec):
    return [c for c in rec.hist if c["name"] == "kirocrew.session.duration"]


class TestStart:
    def test_start_counts_and_leaves_a_crumb(self, home, rec):
        _start("dashboard:chat-1")
        names = [c["name"] for c in rec.counters]
        assert "kirocrew.session.started" in names
        assert len(_crumbs(home)) == 1

    def test_start_labels_the_surface(self, home, rec):
        _start("dashboard:chat-1")
        started = [c for c in rec.counters if c["name"] == "kirocrew.session.started"][-1]
        assert started["attrs"]["session_source"] == "dashboard"

    def test_crumb_is_named_by_digest_not_by_the_key(self, home, rec):
        # A session key can carry path separators and channel punctuation; a
        # key-named file would escape the directory or fail to create at all.
        _start("slack:C123/../../etc/passwd")
        crumbs = _crumbs(home)
        assert len(crumbs) == 1
        assert "passwd" not in crumbs[0].name

    def test_a_second_start_overwrites_the_first(self, home, rec):
        """Deliberate reversal, found in review round 2.

        Registry removal has no single choke point and not every remover records
        an end, so a stale entry is possible. A key re-entering the registry is a
        NEW session whose lifetime must be its own -- keeping the predecessor's
        start would report a lifetime spanning two sessions.
        """
        _start("dashboard:chat-1")
        first = sess._live_starts["dashboard:chat-1"]
        time.sleep(0.01)
        _start("dashboard:chat-1")
        assert sess._live_starts["dashboard:chat-1"] > first

    def test_empty_key_is_a_no_op(self, home, rec):
        _start("")
        assert not rec.counters
        assert not _crumbs(home)


class TestEnd:
    @staticmethod
    def _start(key, seconds_ago):
        """Register a live session the way production does, aged by seconds.

        The crumb's NAME carries its generation, so ageing the table alone would
        leave a file whose generation disagrees with the table -- a state
        production cannot reach, and one the end path would then rightly refuse to
        touch. The aged generation is therefore installed in both places.
        """
        _start(key)
        started_at = time.time() - seconds_ago
        with sess._live_lock:
            previous = sess._live_starts.get(key)
            sess._live_starts[key] = started_at
        if previous is not None:
            sess._crumb_path(key, previous).unlink(missing_ok=True)
        sess._write_crumb(key, started_at)

    def test_end_emits_the_lifetime_and_consumes_the_crumb(self, home, rec):
        self._start("dashboard:chat-1", 60)
        assert _crumbs(home), "the start must have left a crumb to consume"
        _end("dashboard:chat-1", sess.END_REASON_RESET)
        calls = _duration_calls(rec)
        assert len(calls) == 1
        assert calls[0]["unit"] == "ms"
        assert 55_000 < calls[0]["value"] < 70_000
        assert calls[0]["attrs"] == {"end_reason": "reset", "session_source": "dashboard"}
        assert _crumbs(home) == [], "the end must consume the crumb"

    def test_no_crumb_unlink_runs_on_the_event_loop(self, home, rec, monkeypatch):
        """#7537: one ``unlink`` against a slow data home parked every gateway task.

        Teardown runs on the paths a person is waiting on -- closing a tab,
        resetting a slot, shutting the gateway down -- so a stalled syscall there is
        felt as the whole gateway freezing rather than as a metrics problem. Driven
        rather than read off the source: what matters is which THREAD ends up
        holding the syscall, and only running all three end paths can show that.
        """
        threads: list[int] = []
        real = sess._unlink_all

        def _spy(paths):
            threads.append(threading.get_ident())
            real(paths)

        monkeypatch.setattr(sess, "_unlink_all", _spy)

        async def _drive():
            await sess.record_session_started("dashboard:chat-1")
            await sess.record_session_ended("dashboard:chat-1", end_reason=sess.END_REASON_RESET)
            await sess.record_session_started("dashboard:chat-2")
            await sess.record_sessions_ended(
                ["dashboard:chat-2"], end_reason=sess.END_REASON_SHUTDOWN
            )
            await sess.record_session_started("dashboard:chat-3")
            await sess.discard_session_start("dashboard:chat-3")
            return threading.get_ident()

        loop_thread = asyncio.run(_drive())
        assert len(threads) == 3, "each end path must reach the unlink exactly once"
        assert loop_thread not in threads, (
            "a crumb unlink ran on the event-loop thread, so a slow data home "
            "stalls every gateway task behind one session teardown"
        )
        assert _crumbs(home) == [], "every path must still consume its own crumb"

    def test_the_bulk_end_unlinks_the_whole_drained_set_in_one_hop(self, home, rec, monkeypatch):
        """A hop per key would put a cancellation point BETWEEN two keys.

        Every key after such a cancellation is popped but unrecorded, so its crumb
        survives and the next boot reports it as ``crashed``. On ``close_all`` -- a
        path cancellation reaches by design -- that turns one orderly shutdown into
        a burst of fabricated failures, which is worse than the on-loop unlink the
        await replaced. Counting hops rather than racing a real cancellation makes
        the discriminator deterministic: a per-key loop yields ``[1, 1, 1, 1, 1]``.
        """
        hops: list[int] = []
        real = sess._unlink_all

        def _spy(paths):
            hops.append(len(paths))
            real(paths)

        monkeypatch.setattr(sess, "_unlink_all", _spy)
        keys = [f"dashboard:chat-{i}" for i in range(1, 6)]
        for key in keys:
            self._start(key, 60)
        assert len(_crumbs(home)) == 5
        _end_many(keys, sess.END_REASON_SHUTDOWN)
        assert hops == [5], "the drained set must be consumed in one worker hop"
        assert _crumbs(home) == [], "the whole set must be consumed"
        calls = _duration_calls(rec)
        assert len(calls) == 5
        assert {c["attrs"]["end_reason"] for c in calls} == {"shutdown"}

    def test_the_bulk_end_pops_the_whole_set_under_one_lock_hold(self):
        """The pops must not be separable, or a cancellation splits the set."""
        import inspect

        src = inspect.getsource(sess.record_sessions_ended)
        body = src.split('"""', 2)[-1]
        assert body.count("with _live_lock:") == 1, "one hold, or the set is splittable"
        assert body.count("await ") == 1, "exactly one suspension point, after every pop"
        assert body.index("with _live_lock:") < body.index(
            "await "
        ), "every pop must happen before the only cancellable point"

    def test_a_cancelled_end_keeps_its_sample_and_still_consumes_the_crumb(
        self, home, rec, monkeypatch
    ):
        """A cancellation must cost neither the sample nor the crumb.

        The sample is emitted before the hop, so it is already recorded. The crumb
        is consumed either by the worker (this case, where a thread has picked the
        item up) or inline by ``_unlink_generations``' fallback (the queued case, in
        the test below).

        The worker is held on an event until after the cancel lands, because a bare
        ``unlink`` can finish first and then the task would be cancelled at its
        return instead of at its await -- a passing test that never exercised the
        window.
        """
        self._start("dashboard:chat-1", 60)
        released = threading.Event()
        real = sess._unlink_all

        def _held(paths):
            assert released.wait(10), "the test never released the crumb worker"
            real(paths)

        monkeypatch.setattr(sess, "_unlink_all", _held)

        async def _drive():
            task = asyncio.create_task(
                sess.record_session_ended("dashboard:chat-1", end_reason=sess.END_REASON_RESET)
            )
            # One step is enough to reach the executor submit inside to_thread, and
            # the worker cannot resolve the future while it waits on the event.
            await asyncio.sleep(0)
            task.cancel()
            released.set()
            try:
                await task
            except asyncio.CancelledError:
                return "raised"
            return "absorbed"

        assert asyncio.run(_drive()) == "absorbed", (
            "the end must not hand its caller an abort point between the registry "
            "pop and the caller's own post-pop cleanup"
        )
        # asyncio.run() shuts the default executor down on the way out, which joins
        # the worker -- so the unlink has landed by the time this returns.
        assert _crumbs(home) == [], (
            "a cancelled end left its crumb behind, so the next boot reports this "
            "cleanly ended session as crashed"
        )
        assert len(_duration_calls(rec)) == 1, "the sample must survive the cancellation"

    def test_a_cancelled_end_lets_its_callers_post_pop_cleanup_run(self, home, rec, monkeypatch):
        """GPT round 1 on PR #7809, against ``session_lifecycle.destroy``.

        ``destroy`` pops the registry and records the end INSIDE the registry lock,
        then opens a ``try`` whose ``finally`` deletes the session-map entry. An end
        that raised would exit ``destroy`` before that ``try`` was ever entered, so a
        permanently destroyed session would keep its mapping and could be resumed at
        the next boot -- a window the synchronous version did not have, because it
        had no suspension point there at all.

        Reproduces that control flow rather than standing up the lifecycle service
        (whose stubs this file deliberately avoids): the mandatory cleanup sits in a
        ``finally`` BELOW the end call, exactly where ``destroy`` has it.
        """
        self._start("dashboard:chat-1", 60)
        released = threading.Event()
        real = sess._unlink_all

        def _held(paths):
            assert released.wait(10), "the test never released the crumb worker"
            real(paths)

        monkeypatch.setattr(sess, "_unlink_all", _held)
        cleaned: list[str] = []

        async def _destroy_shaped():
            # The lock-held pop plus end record, where destroy() has them.
            await sess.record_session_ended(
                "dashboard:chat-1", end_reason=sess.END_REASON_DESTROYED
            )
            # The try/finally destroy() opens AFTER that block.
            try:
                await asyncio.sleep(0)
            finally:
                cleaned.append("session-map delete")

        async def _drive():
            task = asyncio.create_task(_destroy_shaped())
            await asyncio.sleep(0)
            task.cancel()
            released.set()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_drive())
        assert cleaned == ["session-map delete"], (
            "a cancellation at the crumb hop skipped the caller's mandatory "
            "post-pop cleanup, so a destroyed session keeps its map entry"
        )
        assert _crumbs(home) == [], "and the crumb must still be consumed"

    def test_a_cancelled_end_consumes_its_crumb_even_when_the_hop_never_started(self, home, rec):
        """GPT round 2 on PR #7809: submitted is not started.

        ``asyncio.to_thread`` hands the work to the default executor and suspends,
        but on a saturated pool the item is still QUEUED. Cancelling the await
        propagates through ``wrap_future`` to the underlying
        ``concurrent.futures.Future``, and cancelling a work item that has not begun
        drops it outright -- so the crumb of a cleanly ended session survives to the
        next boot and is back-filled as ``crashed``. That is the same
        dropped-unlink failure that ruled out the maintenance pool, reached by a
        different queue.

        Pins the queued case specifically: the executor is replaced with a
        single-worker pool whose one thread is occupied, so the hop provably cannot
        have started when the cancel lands.
        """
        self._start("dashboard:chat-1", 60)
        occupied = threading.Event()
        release = threading.Event()

        def _hog():
            occupied.set()
            assert release.wait(10), "the test never released the pool's only worker"

        async def _drive():
            loop = asyncio.get_running_loop()
            pool = ThreadPoolExecutor(max_workers=1)
            loop.set_default_executor(pool)
            hog = loop.run_in_executor(None, _hog)
            assert occupied.wait(5), "the pool's only worker never started"
            task = asyncio.create_task(
                sess.record_session_ended("dashboard:chat-1", end_reason=sess.END_REASON_SHUTDOWN)
            )
            # One step reaches the submit inside to_thread. The single worker is
            # busy, so the item is queued and cannot have started.
            await asyncio.sleep(0)
            task.cancel()
            outcome = "absorbed"
            try:
                await task
            except asyncio.CancelledError:
                outcome = "raised"
            release.set()
            await hog
            pool.shutdown(wait=True)
            return outcome

        assert asyncio.run(_drive()) == "absorbed"
        assert _crumbs(home) == [], (
            "the hop was cancelled before any thread picked it up, so the crumb was "
            "never consumed and the next boot reports this clean shutdown as a crash"
        )
        assert len(_duration_calls(rec)) == 1, "the sample must survive the cancellation"

    def test_the_hop_is_shielded_and_never_becomes_a_task(self):
        """Round 3 settled this line; both halves of the shape are load-bearing.

        Shielded, so a cancellation cannot reach the queued work item and drop it
        (round 2), and never unlinked inline, so a cancellation cannot put the
        syscall back on the event loop (round 3). A BARE future rather than a
        shielded ``to_thread`` coroutine, because ``shield`` would wrap a coroutine
        in a Task and ``_cancel_all_tasks`` at loop teardown would cancel it,
        dropping a still-queued item after all.
        """
        import inspect

        hop = inspect.getsource(sess._unlink_generations)
        body = hop.split('"""', 2)[-1]
        assert "run_in_executor(" in body, "the hop must be a bare future, not a Task"
        assert "asyncio.shield(" in body, "a cancellation must not reach the queued item"
        assert "to_thread(" not in body, "shield would wrap a coroutine in a cancellable Task"
        assert (
            "_unlink_all(paths)" not in body
        ), "the unlink must never run on the event loop, which is what #7537 is about"

    def test_no_end_path_hands_its_caller_a_new_abort_point(self):
        """All three absorb; the helper owns not-dropping the unlink.

        Two distinct guarantees, deliberately in two places. Whether a caller may be
        aborted is knowable only where its post-pop cleanup is known, so it lives in
        the end paths. Whether the unlink survives a cancelled hop is a property of
        the hop, so it lives in the helper.
        """
        import inspect

        for fn in (
            sess.record_session_ended,
            sess.record_sessions_ended,
            sess.discard_session_start,
        ):
            body = inspect.getsource(fn).split('"""', 2)[-1]
            assert "except BaseException" in body, (
                f"{fn.__name__} must absorb a cancellation at the crumb hop, or its "
                "caller exits before its own post-pop cleanup"
            )
        helper = inspect.getsource(sess._unlink_generations).split('"""', 2)[-1]
        assert "asyncio.shield(" in helper, (
            "the helper owns the other half: a cancelled hop must still consume the "
            "crumb, which an end path cannot do for it"
        )

    def test_a_cancelled_discard_does_not_displace_the_rollback_failure(
        self, home, rec, monkeypatch
    ):
        """Its callers re-raise the failure that started the rollback on the next line.

        A cancellation escaping here would replace that failure with one of this
        function's own making, so the discard absorbs it -- and the crumb is still
        consumed, because ``_unlink_generations`` never drops the unlink.
        """
        self._start("dashboard:chat-1", 60)
        released = threading.Event()
        real = sess._unlink_all

        def _held(paths):
            assert released.wait(10), "the test never released the crumb worker"
            real(paths)

        monkeypatch.setattr(sess, "_unlink_all", _held)

        async def _drive():
            task = asyncio.create_task(sess.discard_session_start("dashboard:chat-1"))
            await asyncio.sleep(0)
            task.cancel()
            released.set()
            try:
                await task
            except asyncio.CancelledError:
                return "raised"
            return "absorbed"

        assert asyncio.run(_drive()) == "absorbed"
        assert _crumbs(home) == [], "a rolled-back start must not leave a crumb"
        assert not _duration_calls(rec), "a session that never lived has no lifetime"

    def test_the_end_record_touches_no_disk(self):
        """It runs in the same tick as the registry pop, so it cannot block.

        Review round 2: reading the crumb here forced the call to the end of
        teardown, and a replacement session registering under the same key during
        those awaits had its start consumed by its predecessor.
        """
        import inspect

        body = inspect.getsource(sess.record_session_ended)
        assert "_live_starts.pop" in body
        assert "_read_crumb" not in body
        assert "read_text" not in body

    def test_a_second_end_emits_nothing(self, home, rec):
        """The teardown paths overlap -- the idle sweep calls reset."""
        self._start("dashboard:chat-1", 60)
        _end("dashboard:chat-1", sess.END_REASON_RESET)
        _end("dashboard:chat-1", sess.END_REASON_SHUTDOWN)
        assert len(_duration_calls(rec)) == 1

    def test_end_without_a_crumb_emits_nothing(self, home, rec):
        _end("dashboard:never-started", sess.END_REASON_RESET)
        assert not _duration_calls(rec)

    def test_unknown_end_reason_is_refused(self, home, rec):
        """An unbounded label would mint a series; the enum is the gate."""
        _write_crumb(home, "dashboard:chat-1", time.time() - 60)
        _end("dashboard:chat-1", "whatever-i-like")
        assert not _duration_calls(rec)
        assert _crumbs(home), "a refused reason must not consume the crumb"

    def test_non_positive_lifetime_is_skipped(self, home, rec):
        # A LIVE start in the future, so the end actually reaches the emit and is
        # rejected there. Planting only a crumb would return early at the pop and
        # pass without ever exercising the guard.
        self._start("dashboard:chat-1", -3600)
        _end("dashboard:chat-1", sess.END_REASON_RESET)
        assert not _duration_calls(rec)


class TestCrashedBackfill:
    def _transcript(self, home, key, mtime):
        from kiro_crew.history import SESSIONS_DIR_NAME, transcript_stem

        d = home / SESSIONS_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{transcript_stem(key)}.jsonl"
        path.write_text("{}\n")
        import os

        os.utime(path, (mtime, mtime))
        return path

    def test_leftover_crumb_becomes_a_crashed_sample(self, home, rec):
        started = time.time() - 7200
        _write_crumb(home, "dashboard:chat-1", started)
        self._transcript(home, "dashboard:chat-1", started + 3600)
        assert sess.backfill_crashed_sessions() == 1
        calls = _duration_calls(rec)
        assert len(calls) == 1
        assert calls[0]["attrs"]["end_reason"] == "crashed"
        assert 3_500_000 < calls[0]["value"] < 3_700_000

    def test_backfill_consumes_the_crumb_so_it_cannot_recur(self, home, rec):
        started = time.time() - 7200
        _write_crumb(home, "dashboard:chat-1", started)
        self._transcript(home, "dashboard:chat-1", started + 3600)
        sess.backfill_crashed_sessions()
        assert not _crumbs(home)
        assert sess.backfill_crashed_sessions() == 0
        assert len(_duration_calls(rec)) == 1

    def test_no_transcript_means_no_sample_but_the_crumb_still_goes(self, home, rec):
        _write_crumb(home, "subagent:run-1", time.time() - 7200)
        assert sess.backfill_crashed_sessions() == 0
        assert not _duration_calls(rec)
        assert not _crumbs(home)

    def test_missing_directory_is_not_an_error(self, home, rec):
        assert sess.backfill_crashed_sessions() == 0

    def test_a_cleanly_ended_session_is_never_backfilled(self, home, rec):
        started = time.time() - 7200
        _start("dashboard:chat-1")
        with sess._live_lock:
            previous = sess._live_starts.get("dashboard:chat-1")
            sess._live_starts["dashboard:chat-1"] = started
        # Age the FILE with the table: the crumb's name carries its generation.
        sess._crumb_path("dashboard:chat-1", previous).unlink(missing_ok=True)
        sess._write_crumb("dashboard:chat-1", started)
        assert _crumbs(home), "the start must have left a crumb to consume"
        self._transcript(home, "dashboard:chat-1", started + 3600)
        _end("dashboard:chat-1", sess.END_REASON_SHUTDOWN)
        assert _crumbs(home) == [], "the end must consume the crumb"
        assert sess.backfill_crashed_sessions() == 0
        calls = _duration_calls(rec)
        assert len(calls) == 1
        assert calls[0]["attrs"]["end_reason"] == "shutdown"

    def test_a_corrupt_crumb_is_consumed_here_without_emitting(self, home, rec):
        """The backfill owns this now, because it is the only reader of the files.

        The end path used to consume an unparseable crumb as a side effect of
        unlinking blind, which was the same blind unlink that could delete a live
        sibling's record. Now that an end only ever removes the generation it can
        name, the file that no session can claim is the backfill's to reap -- and
        it must still be reaped, or every boot re-walks it forever.
        """
        _write_crumb(home, "dashboard:chat-1", time.time() - 60)
        _crumbs(home)[0].write_text("not json at all")
        assert sess.backfill_crashed_sessions() == 0
        assert not _duration_calls(rec)
        assert not _crumbs(home), "a corrupt crumb must not be re-walked every boot"


class TestWriterSelfCorrection:
    """The worker cannot be cancelled, so it has to clean up after itself."""

    def test_a_writer_landing_after_a_discard_leaves_no_crumb(self, home, rec):
        """Review round 9: cancelling an ``asyncio.to_thread`` does not stop it.

        The await is abandoned, the worker is not -- so the caller's rollback can
        unlink BEFORE the write lands, and the crumb the worker then writes is read
        at the next boot as a crash that never happened. The worker self-corrects
        instead of the await being made non-cancellable: it re-checks the
        generation after writing, and a discard has already popped it.
        """
        started = time.time()
        with sess._live_lock:
            sess._live_starts["dashboard:chat-1"] = started
        _discard("dashboard:chat-1")
        # The worker runs LATE, after the rollback already cleaned up.
        sess._write_crumb("dashboard:chat-1", started)
        assert not _crumbs(home), "a crumb written after a discard would backfill as crashed"

    def test_a_late_writer_never_deletes_a_successors_crumb(self, home, rec):
        """A predecessor can only ever remove its OWN file, by construction.

        An earlier attempt unlinked the path unconditionally, so a predecessor
        finishing late deleted the crumb its SUCCESSOR had just written under the
        same key -- losing that live session's later crash. Comparing the recorded
        ``started_at`` after the write only narrowed that window, and across
        PROCESSES it did nothing at all, because two processes holding one key still
        shared one filename and these are thread locks.

        The name now carries the writer and the generation, so the successor's
        record is not addressable from the predecessor's writer. The successor is
        still registered DURING the predecessor's write, the interleaving that used
        to reach the broken post-write check.
        """
        import kiro_crew.atomic_write as aw

        key = "dashboard:chat-1"
        predecessor = time.time() - 3600
        successor = time.time()
        with sess._live_lock:
            sess._live_starts[key] = predecessor
        real_write = aw.atomic_write

        def _successor_lands_mid_write(path, data, *a, **k):
            real_write(path, data, *a, **k)
            # A successor takes the key and writes ITS OWN crumb while the
            # predecessor's write is still inside its critical section.
            with sess._live_lock:
                sess._live_starts[key] = successor
            real_write(
                sess._crumb_path(key, successor),
                json.dumps({"key": key, "started_at": successor, "pid": os.getpid()}),
            )

        with patch.object(aw, "atomic_write", _successor_lands_mid_write):
            sess._write_crumb(key, predecessor)

        crumbs = _crumbs(home)
        assert len(crumbs) == 1, "the successor's crumb must survive the predecessor"
        assert json.loads(crumbs[0].read_text())["started_at"] == successor

    def test_two_generations_of_one_key_never_share_a_file(self, home, rec):
        """The premise the round-4 through round-9 findings all rested on.

        A session key is not unique across processes -- ``BACKGROUND_KEY`` is a
        fixed constant, so a ``kirocrew run`` and the gateway hold the same key at
        once, each with a live session behind it. While the filename was the key's
        digest alone they shared one file, and whichever ended first unlinked the
        other's record.
        """
        key = "_bg"
        first = time.time() - 60
        second = time.time()
        assert sess._crumb_path(key, first) != sess._crumb_path(key, second)

    def test_a_superseding_start_reaps_the_generation_it_displaced(self, home, rec):
        """Named generations make this a case that needs handling.

        Under one shared filename a second start simply overwrote the file. Now the
        displaced generation has a name of its own that the table no longer holds,
        so nothing could ever unlink it again -- it would reach the next boot and be
        reported as a crash that never happened.
        """
        key = "dashboard:chat-1"
        _start(key)
        first = sess._live_starts[key]
        time.sleep(0.01)
        _start(key)
        second = sess._live_starts[key]
        assert second > first
        assert not sess._crumb_path(key, first).exists(), "the displaced crumb must be reaped"
        assert sess._crumb_path(key, second).exists()
        assert len(_crumbs(home)) == 1

    def test_the_lock_order_is_io_then_table_everywhere(self):
        """Nested locks deadlock only if two paths disagree about the order.

        The writer takes ``_crumb_io_lock`` and then ``_live_lock`` through the
        generation check. Nothing may take them the other way round.
        """
        import inspect

        for name in (
            "record_session_ended",
            "record_sessions_ended",
            "discard_session_start",
            "_write_crumb",
        ):
            src = inspect.getsource(getattr(sess, name))
            body = src.split('"""', 2)[-1] if '"""' in src else src
            io_at = body.find("with _crumb_io_lock")
            live_at = body.find("with _live_lock")
            if io_at >= 0 and live_at >= 0:
                assert io_at < live_at, f"{name} takes the locks in the wrong order"


class TestCrossProcessOwnership:
    """Review round 7: a crumb with no owner cannot say WHOSE session it is.

    ``kirocrew run`` and the eval runner each build their own SessionManager
    against the same data home, so a gateway boot sees their crumbs too. The
    start-time cutoff only ever protected THIS process's own crumbs, so a sibling's
    live session -- started before the gateway booted -- was read as a crash: it
    both invented a sample and deleted the live crumb, losing the real crash that
    session might later suffer.
    """

    def test_a_crumb_owned_by_a_live_process_is_never_reaped(self, home, rec):
        _write_crumb(home, "dashboard:chat-1", time.time() - 7200, owner_pid=os.getpid())
        assert sess.backfill_crashed_sessions() == 0, "a live owner is not a casualty"
        assert _crumbs(home), "and its crumb must survive for its own teardown"

    @staticmethod
    def _restamp(home, start_id):
        """Give the one crumb on disk an explicit owning start identifier."""
        crumb = _crumbs(home)[0]
        payload = json.loads(crumb.read_text())
        payload["start_id"] = start_id
        crumb.write_text(json.dumps(payload))

    def test_our_own_pid_with_a_foreign_start_id_is_a_dead_predecessor(self, home, rec):
        """Review round 10: in a container the gateway is PID 1 on every restart.

        A crashed predecessor's crumb therefore arrives carrying THIS process's pid,
        and the own-pid fast path used to answer "still running" without comparing
        identities at all -- so that crumb was skipped on this boot and on every
        boot after: its crash was never emitted and its file was never cleaned. The
        other review lane read this as a coincidental pid collision, but under a
        container's fixed PID 1 it is deterministic.
        """
        started = time.time() - 7200
        _write_crumb(home, "dashboard:chat-1", started, owner_pid=os.getpid())
        self._restamp(home, "the-process-that-crashed")
        TestCrashedBackfill()._transcript(home, "dashboard:chat-1", started + 3600)
        with patch(
            "kiro_crew.platform_compat.get_process_start_id",
            side_effect=lambda pid: "this-process",
        ):
            assert sess.backfill_crashed_sessions() == 1
        assert _duration_calls(rec)[0]["attrs"]["end_reason"] == "crashed"
        assert not _crumbs(home), "and it must not be re-walked on every later boot"

    def test_our_own_pid_with_a_matching_start_id_is_still_us(self, home, rec):
        """The refinement must not start reaping this process's own live crumbs."""
        _write_crumb(home, "dashboard:chat-1", time.time() - 7200, owner_pid=os.getpid())
        self._restamp(home, "this-process")
        with patch(
            "kiro_crew.platform_compat.get_process_start_id",
            side_effect=lambda pid: "this-process",
        ):
            assert sess.backfill_crashed_sessions() == 0
        assert _crumbs(home), "a live owner's crumb must survive"

    def test_our_own_pid_with_an_unreadable_identity_fails_closed(self, home, rec):
        """Windows returns None here, and a None must never read as a mismatch."""
        _write_crumb(home, "dashboard:chat-1", time.time() - 7200, owner_pid=os.getpid())
        self._restamp(home, "written-where-it-could-be-read")
        with patch(
            "kiro_crew.platform_compat.get_process_start_id",
            side_effect=lambda pid: None,
        ):
            assert sess.backfill_crashed_sessions() == 0
        assert _crumbs(home), "an unreadable identity must not become a fabricated crash"

    def test_a_crumb_whose_owner_is_gone_is_still_claimed(self, home, rec):
        started = time.time() - 7200
        _write_crumb(home, "dashboard:chat-1", started, owner_pid=424242)
        TestCrashedBackfill()._transcript(home, "dashboard:chat-1", started + 3600)
        with patch(
            "kiro_crew.platform_compat.get_process_start_id",
            side_effect=lambda pid: None if pid == 424242 else "self",
        ):
            assert sess.backfill_crashed_sessions() == 1
        assert _duration_calls(rec)[0]["attrs"]["end_reason"] == "crashed"

    def test_an_undecidable_owner_fails_closed(self, home, rec):
        """A live pid whose identity we cannot read is left alone, not reaped.

        This is the Windows case, and it is why liveness comes from ``pid_exists``
        rather than from the start identifier: ``get_process_start_id`` returns
        None on Windows and for any process it may not introspect, and its own
        contract says a None must NOT be read as a mismatch. Reading it as one
        judged every owner dead on that platform and reaped live sibling sessions.

        Losing a real crash sample costs one data point; inventing one corrupts
        the population this instrument exists to report.
        """
        _write_crumb(home, "dashboard:chat-1", time.time() - 7200, owner_pid=424242)
        with patch("kiro_crew.platform_compat.pid_exists", return_value=True):
            with patch("kiro_crew.platform_compat.get_process_start_id", return_value=None):
                assert sess.backfill_crashed_sessions() == 0
        assert _crumbs(home), "an ambiguous crumb waits for a later boot"

    def test_a_live_pid_whose_identity_differs_is_a_recycled_pid(self, home, rec):
        """Pid reuse must not make a dead session look alive."""
        started = time.time() - 7200
        _write_crumb(home, "dashboard:chat-1", started, owner_pid=424242)
        payload = json.loads(_crumbs(home)[0].read_text())
        payload["start_id"] = "the-original-process"
        _crumbs(home)[0].write_text(json.dumps(payload))
        TestCrashedBackfill()._transcript(home, "dashboard:chat-1", started + 3600)
        with patch("kiro_crew.platform_compat.pid_exists", return_value=True):
            with patch(
                "kiro_crew.platform_compat.get_process_start_id",
                return_value="a-different-process",
            ):
                assert sess.backfill_crashed_sessions() == 1
        assert _duration_calls(rec)[0]["attrs"]["end_reason"] == "crashed"

    def test_the_writer_stamps_its_own_identity(self, home, rec):
        _start("dashboard:chat-1")
        payload = json.loads(_crumbs(home)[0].read_text())
        assert payload["pid"] == os.getpid()
        assert isinstance(payload["start_id"], str)


class TestContract:
    def test_every_end_reason_constant_is_in_the_enum(self):
        """A new path must not emit a label the tests do not know about."""
        declared = {
            value
            for name, value in vars(sess).items()
            if name.startswith("END_REASON_") and isinstance(value, str)
        }
        assert declared == set(sess.END_REASONS)

    def test_the_lifecycle_module_uses_only_enum_members(self):
        """The teardown paths import their labels rather than spelling them."""
        from kiro_crew import session_lifecycle

        for name in (
            "END_REASON_RESET",
            "END_REASON_REMOVED",
            "END_REASON_UNCLAIMED",
            "END_REASON_DESTROYED",
            "END_REASON_DISCARDED",
            "END_REASON_SHUTDOWN",
        ):
            assert getattr(session_lifecycle, name) in sess.END_REASONS

    def test_the_duration_histogram_has_registered_bounds(self):
        from kiro_crew.metrics.provider import _HISTOGRAM_BUCKETS_MS

        bounds = _HISTOGRAM_BUCKETS_MS[sess.SESSION_DURATION_METRIC]
        # Minutes to days: a week-long dashboard tab must not land in +Inf, and
        # a seconds-long unclaimed session must not collapse onto bound one.
        assert bounds[0] <= 1000
        assert bounds[-1] >= 7 * 24 * 60 * 60 * 1000


class TestReviewFixes:
    """Regressions found in review -- each of these was a real defect."""

    def test_the_crumb_write_is_off_the_loop_but_still_ordered(self, home, rec):
        """Three earlier shapes each satisfied one constraint and broke the other.

        Fire-and-forget on a pool kept the loop clear but let a writer land after
        its session ended, or after a SUCCESSOR registered under the same key --
        and detecting that after the fact was itself racy, because the post-write
        check acted on the path the successor SHARES. Writing inline fixed the
        ordering and put filesystem I/O on the event loop, where a slow data home
        stalls every gateway task behind one session insertion.

        Awaiting a worker hop satisfies both: the syscalls leave the loop, and
        nothing can interleave because the caller holds the session registry lock
        across the await. This pins each half -- the hop, the await, and the
        writer taking ``_live_lock`` for the backfill it races.
        """
        import inspect

        body = inspect.getsource(sess.record_session_started)
        assert "await asyncio.to_thread(" in body, "the write must be awaited off-loop"
        assert "_write_crumb" in body, "the awaited hop must be the crumb writer"
        assert "run_in_executor(" not in body, "no loop-bound future"
        assert not hasattr(sess, "_submit"), "the fire-and-forget pool submitter must stay gone"
        writer_src = inspect.getsource(sess._write_crumb)
        assert "with _crumb_io_lock:" in writer_src, "the writer must take the I/O lock"
        assert "with _live_lock:" not in writer_src, "the table lock must not span file I/O"

        # The loop-side paths must not wait on a lock a worker holds across I/O.
        for fn in (sess.record_session_ended, sess.discard_session_start):
            body = inspect.getsource(fn)
            # Acquisition, not any mention: both docstrings discuss the I/O lock
            # precisely to record why they must never take it.
            assert "with _crumb_io_lock" not in body, f"{fn.__name__} runs on the loop"
            assert "_crumb_io_lock.acquire" not in body, f"{fn.__name__} runs on the loop"
            assert _unlink_is_outside_the_table_lock(body), (
                f"{fn.__name__} must unlink OUTSIDE the table lock, or the loop "
                "parks behind whatever holds it"
            )

        # And the end consumes whatever the start left.
        _start("dashboard:chat-1")
        assert _crumbs(home)
        _end("dashboard:chat-1", sess.END_REASON_RESET)
        assert not _crumbs(home)
        assert sess.backfill_crashed_sessions() == 0

    def test_a_later_session_under_the_same_key_still_gets_a_crumb(self, home, rec):
        """An ended key must not be suppressed for the rest of the process."""
        _end("dashboard:chat-1", sess.END_REASON_RESET)
        _start("dashboard:chat-1")
        assert len(_crumbs(home)) == 1

    def test_a_superseded_start_leaves_only_the_successors_crumb(self, home, rec):
        """Review rounds 3-5 all lived here; the inline write ended the class.

        A deferred write could land after its own session ended, or after a
        SUCCESSOR registered under the same key. Guarding it with an ended-key
        set, then with a generation compared before and after the write, both
        failed -- the post-write check acted on the path the successor SHARES, so
        a late predecessor deleted the crumb its successor had just written and
        lost that session's crash. Writing under the lock that installs the start
        removes the interleaving rather than detecting it: there is no window in
        which two starts are both mid-write.
        """
        _start("dashboard:chat-1")
        first = sess._live_starts["dashboard:chat-1"]
        time.sleep(0.01)
        _start("dashboard:chat-1")
        current = sess._live_starts["dashboard:chat-1"]
        assert current > first
        crumbs = _crumbs(home)
        assert len(crumbs) == 1, "one key is one crumb, whatever the generation"
        assert json.loads(crumbs[0].read_text())["started_at"] == current

    def test_the_end_unlinks_off_the_loop_without_ever_dropping_the_unlink(self):
        """Review round 4 rejected a POOLED unlink; #7537 rejected an inline one.

        Both rejections stand, and awaiting is the shape that survives both. Queued
        on the maintenance pool, the unlink is dropped at ``close_all`` --
        ``shutdown_maintenance_executor`` drains with ``cancel_futures=True``, so a
        cleanly ended session's crumb is left for the next boot to call
        ``crashed``. Inline on the event loop, it parks every gateway task behind
        one closing session whenever the data home is slow or network-backed.
        Awaiting a worker hop is off the loop AND never dropped.
        """
        import inspect

        for fn in (sess.record_session_ended, sess.record_sessions_ended):
            body = inspect.getsource(fn)
            assert "_submit(" not in body, "the unlink must not be handed to the pool"
            assert "await _unlink_generations(" in body, "the unlink must be awaited off-loop"
            assert "_unlink(_crumb_path(" not in body, "the unlink must not run on the loop"
        hop = inspect.getsource(sess._unlink_generations)
        assert "await asyncio.shield(" in hop, "the hop must be awaited, not fired blind"
        assert "run_in_executor(" in hop, (
            "the shielded hop must be a bare future: shielding a to_thread coroutine "
            "makes it a Task, which loop teardown cancels"
        )
        assert not hasattr(sess, "_submit"), "the fire-and-forget pool submitter must stay gone"

    def test_the_discard_also_leaves_the_loop(self):
        """#7537 named both end paths; a rollback is still a filesystem syscall.

        It absorbs a cancellation, as both end paths do, because its
        caller re-raises the failure that started the rollback on the next line --
        so a cancellation escaping here would displace that failure instead of
        adding information.
        """
        import inspect

        body = inspect.getsource(sess.discard_session_start)
        assert "await _unlink_generations(" in body, "the discard must unlink off-loop"
        assert "_unlink(_crumb_path(" not in body, "the discard must not unlink on the loop"
        assert "except BaseException" in body, "the rollback caller's failure must survive"

    def test_the_backfill_never_claims_a_crumb_from_this_process(self, home, rec):
        """The cutoff is what lets the scan run off the boot path."""
        cutoff = time.time()
        _write_crumb(home, "dashboard:chat-1", cutoff + 5)
        assert sess.backfill_crashed_sessions(cutoff) == 0
        assert _crumbs(home), "a live crumb must survive the scan"

    def test_the_backfill_still_claims_an_older_crumb_under_a_cutoff(self, home, rec):
        started = time.time() - 7200
        _write_crumb(home, "dashboard:chat-1", started)
        TestCrashedBackfill()._transcript(home, "dashboard:chat-1", started + 3600)
        assert sess.backfill_crashed_sessions(time.time()) == 1
        assert _duration_calls(rec)[0]["attrs"]["end_reason"] == "crashed"

    def test_retire_and_recycle_have_their_own_reasons(self):
        """They pop the registry directly and never route through reset."""
        assert sess.END_REASON_RETIRED in sess.END_REASONS
        assert sess.END_REASON_RECYCLED in sess.END_REASONS
        assert sess.END_REASON_RETIRED != sess.END_REASON_RESET
        assert sess.END_REASON_RECYCLED != sess.END_REASON_RESET

    def test_the_identity_retire_path_records_an_end(self):
        from kiro_crew import session_lifecycle

        found = TestTeardownPathsAreWired._reasons_recorded_by("retire_kiro_identity_sessions")
        assert found == ["END_REASON_RETIRED"]
        assert session_lifecycle.END_REASON_RETIRED in sess.END_REASONS

    def test_the_compaction_recycle_path_records_an_end(self):
        import ast
        import inspect

        from kiro_crew import session_compaction

        tree = ast.parse(inspect.getsource(session_compaction))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recycle_held":
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    fn = call.func
                    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                    if name == "record_session_ended":
                        found.append(
                            [
                                k.value.id
                                for k in call.keywords
                                if k.arg == "end_reason" and isinstance(k.value, ast.Name)
                            ]
                        )
        # Exactly one: the branch that owns the pop. The other branch's key
        # already holds a SUCCESSOR whose crumb must not be consumed here.
        assert found == [["END_REASON_RECYCLED"]]

    def test_the_boot_hook_no_longer_blocks_readiness(self):
        from pathlib import Path

        src = (Path(sess.__file__).resolve().parent.parent / "slack" / "gateway.py").read_text(
            encoding="utf-8"
        )
        i = src.index("_backfill_unclean_session_telemetry")
        window = src[i : i + 900]
        assert "asyncio.to_thread" in window, "the scan must not run on the loop"
        assert "create_task" in window, "the scan must not be awaited before readiness"
        assert "_background_tasks" in window, "the task must be tracked, not fire-and-forget"

    def test_the_crumb_write_is_never_a_loop_bound_future(self):
        """Windows CI failed with 'Event loop is closed' on the first version.

        That version used ``loop.run_in_executor``, whose future is owned by the
        running loop, so a fire-and-forget write outliving its loop raised when
        the result was set. The write is off-loop again now, but AWAITED rather
        than fired and forgotten, which is what keeps it ordered as well as
        non-blocking.
        """
        import inspect

        body = inspect.getsource(sess.record_session_started)
        assert "run_in_executor(" not in body
        assert "asyncio.to_thread" in body

    def test_a_start_writes_its_crumb_with_no_gateway_loop_of_its_own(self, home, rec):
        _start("cron:job-1")
        assert _crumbs(home), "a session start must leave a crumb"

    def test_no_crumb_is_written_without_telemetry_consent(self, home):
        """First Principles round 6: an unopted install must write nothing.

        The crumb feeds only ``kirocrew.session.duration``, which is a no-op
        without consent -- so writing one on an unopted host persists state that
        nothing can ever read, against a documented default of collecting nothing.
        Round 5 relaxed the spec sentence to permit it; that was the wrong
        direction, and this pins the behaviour instead.
        """

        class _Off:
            enabled = False

            def histogram(self, *a, **k):
                pass

            def counter(self, *a, **k):
                pass

        with patch("kiro_crew.metrics.provider.get_recorder", return_value=_Off()):
            _start("dashboard:chat-1")
        assert not _crumbs(home), "consent is off, so no crumb may reach the disk"

    def test_the_consent_check_fails_closed(self, home):
        """An unreadable consent state must not default to writing."""
        with patch("kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("boom")):
            _start("dashboard:chat-1")
        assert not _crumbs(home)


class TestEveryRegistryRemovalRecordsAnEnd:
    """A fail-closed gate over the whole source tree, not a hand-kept list.

    Review round 5 found FOUR registry removals with no end record, and the
    hand-maintained list in ``TestTeardownPathsAreWired`` could not have caught
    any of them: it enumerates six methods of one module by name, so a seventh
    path -- or any pop outside that module -- is invisible to it. That is the
    wrong shape for this invariant, because an unrecorded removal is not a lost
    sample. The start crumb survives it, and the next boot reports the session as
    ``crashed``: a failure that never happened, in the exact population the
    histogram exists to measure.

    So this walks every module that mutates the registry and requires each
    mutating function to record an end. A NEW removal path fails this test by
    default, which is the only version of this gate that stays true as the
    lifecycle grows.
    """

    #: Functions that remove from the registry WITHOUT consuming the crumb,
    #: deliberately. Each entry needs a reason, and the reason has to survive review.
    ALLOWED_SILENT: dict[str, str] = {}

    #: Modules whose ``_sessions`` is a DIFFERENT container that merely shares the
    #: attribute name, with the reason it is out of scope. Discovery is by name, so
    #: it cannot tell two containers apart on its own -- but an exemption here is
    #: NOT a permanent hole: ``test_each_unrelated_registry_exemption_still_holds``
    #: re-derives the claim, so the day such a module starts participating in the
    #: crumb lifecycle its exemption fails instead of quietly covering for it.
    UNRELATED_REGISTRIES = {
        "connections.warm": (
            "the shared warm OAuth mint's dict[int, _WarmSession], keyed by "
            "activation id -- not the session registry's dict[str, _SessionEntry], "
            "and it writes no crumb, so a removal there cannot leave one behind"
        ),
    }

    @staticmethod
    def _module_source(module_name):
        import importlib
        import inspect

        return inspect.getsource(importlib.import_module(f"kiro_crew.{module_name}"))

    @staticmethod
    def _owning_modules():
        """Every module under ``src/kiro_crew`` that mutates the session registry.

        DISCOVERED, not listed. Design review's objection to a hardcoded list was
        exact: session-metric correctness depends on every current AND FUTURE
        removal path reporting itself, and a list of four module names cannot see a
        fifth. Walking the tree means a removal added anywhere is in scope the day
        it lands, which is the only version of this gate that stays true as the
        lifecycle grows.
        """
        import kiro_crew

        root = Path(kiro_crew.__file__).resolve().parent
        found = []
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                if "_sessions" not in path.read_text(encoding="utf-8"):
                    continue
            except OSError:
                continue
            rel = path.relative_to(root).with_suffix("")
            found.append(".".join(rel.parts))
        return found

    @staticmethod
    def _removal_functions(module_name):
        """Every function in *module_name* that removes a registry entry.

        Returns ``{function name: consumes_the_crumb}``. Recognises the three
        removal spellings the tree actually uses -- ``_sessions.pop``, ``del
        _sessions[...]`` and ``_sessions.clear()`` -- so a path that switches
        spelling does not fall out of the gate. A removal consumes its crumb
        either by recording an end (a session that lived, singly or as a drained
        set) or by discarding the start (a registration rolled back before it
        became a session); both leave no crumb behind, which is what the invariant
        is actually about.
        """
        import ast
        import importlib
        import inspect

        module = importlib.import_module(f"kiro_crew.{module_name}")
        tree = ast.parse(inspect.getsource(module))
        out: dict[str, bool] = {}
        consumers = {"record_session_ended", "record_sessions_ended", "discard_session_start"}

        def _is_registry(node):
            return isinstance(node, ast.Attribute) and node.attr == "_sessions"

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            removes = records = False
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                    if name in {"pop", "clear"} and _is_registry(getattr(func, "value", None)):
                        removes = True
                    if name in consumers:
                        records = True
                elif isinstance(node, ast.Delete):
                    for target in node.targets:
                        if isinstance(target, ast.Subscript) and _is_registry(target.value):
                            removes = True
            if removes:
                out[fn.name] = records
        return out

    def test_every_registry_removal_records_an_end(self):
        silent = []
        for module_name in self._owning_modules():
            if module_name in self.UNRELATED_REGISTRIES:
                continue
            for fn_name, records in self._removal_functions(module_name).items():
                if not records and fn_name not in self.ALLOWED_SILENT:
                    silent.append(f"{module_name}.{fn_name}")
        assert not silent, (
            "these registry removals record no end, so every session they drop "
            f"is reported as crashed at the next boot: {sorted(silent)}"
        )

    def test_each_unrelated_registry_exemption_still_holds(self):
        """An exemption must void itself if the module joins the crumb lifecycle."""
        for module_name, reason in self.UNRELATED_REGISTRIES.items():
            assert reason, f"{module_name} needs a reason, not a bare exemption"
            src = self._module_source(module_name)
            assert "record_session_started" not in src, (
                f"{module_name} now writes session crumbs, so its exemption is "
                f"void and its removals must consume them. Stale reason: {reason}"
            )

    def test_the_gate_can_actually_fail(self):
        """A gate that has stopped detecting anything reads as a green signal."""
        modules = self._owning_modules()
        assert "session_lifecycle" in modules, "discovery missed the lifecycle module"
        removals = self._removal_functions("session_lifecycle")
        assert removals, "the AST walk found no registry removals at all"
        assert "drain_all_providers" in removals, "the mass-pop path must be in scope"


class TestTeardownPathsAreWired:
    """Each teardown path must record, with a reason of its own.

    A source-level gate rather than a driven one: these six methods are the
    lifecycle service's own registry pops, and standing each of them up needs a
    stub owner, provider, executor and platform layer -- which would pin the
    stubs, not the wiring. The behaviour they delegate to is driven directly
    above; what this holds is that they still delegate at all, and that no two
    paths report the same reason (which would silently merge two populations).

    Coverage that every removal records AT ALL belongs to
    ``TestEveryRegistryRemovalRecordsAnEnd`` above; this one is about the reasons
    being distinct.
    """

    EXPECTED = {
        "reset": "END_REASON_RESET",
        "remove": "END_REASON_REMOVED",
        "remove_if_unclaimed": "END_REASON_UNCLAIMED",
        "destroy": "END_REASON_DESTROYED",
        "discard_conversation": "END_REASON_DISCARDED",
        "close_all": "END_REASON_SHUTDOWN",
    }

    @staticmethod
    def _reasons_recorded_by(method_name):
        """Reasons *method_name* records, whether one key at a time or a whole set.

        Both spellings count: the mass-teardown paths pop many keys under one lock
        hold and record them with ``record_sessions_ended``, precisely so no
        cancellation can land between two of them.
        """
        import ast
        import inspect

        from kiro_crew import session_lifecycle

        recorders = {"record_session_ended", "record_sessions_ended"}
        tree = ast.parse(inspect.getsource(session_lifecycle))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name != method_name:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name not in recorders:
                    continue
                for kw in call.keywords:
                    if kw.arg == "end_reason" and isinstance(kw.value, ast.Name):
                        found.append(kw.value.id)
        return found

    @pytest.mark.parametrize("method,const", sorted(EXPECTED.items()))
    def test_the_path_records_with_its_own_reason(self, method, const):
        assert self._reasons_recorded_by(method) == [const]

    def test_no_two_paths_share_a_reason(self):
        reasons = [c for m in self.EXPECTED for c in self._reasons_recorded_by(m)]
        assert len(reasons) == len(set(reasons))

    def test_the_boot_backfill_is_wired_into_gateway_startup(self):
        from pathlib import Path

        gateway = Path(sess.__file__).resolve().parent.parent / "slack" / "gateway.py"
        src = gateway.read_text(encoding="utf-8")
        assert "backfill_crashed_sessions" in src
        # Ordering against the orphan sweep is deliberately NOT asserted any
        # more: review showed an inline pre-ready scan delays readiness in
        # proportion to accumulated crumbs, so it moved to a worker thread. What
        # replaces the ordering requirement is the process-start cutoff, which
        # TestReviewFixes pins directly -- the scan can no longer mistake a
        # session THIS process opened for a casualty of the last one.
        assert "_telemetry_backfill_cutoff" in src

    def test_the_registry_insertions_record_a_start(self):
        from pathlib import Path

        root = Path(sess.__file__).resolve().parent.parent
        for rel in ("session_allocation.py", "session_background.py"):
            src = (root / rel).read_text(encoding="utf-8")
            assert "record_session_started" in src, rel
