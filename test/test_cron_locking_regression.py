"""Regression tests for the cron-locking / cron.py audit fixes (cluster C).

Each test encodes the exact failure scenario from the audit so it fails against
the pre-fix code and passes against the remediation:

1. Blocking ``fcntl.flock`` on the event loop in store mutators
   → ``_file_lock`` now uses a bounded non-blocking ``try_acquire_lock`` spin.
2. Unlocked read paths (``list_jobs`` / ``get_job`` / ``run_job`` / reaper)
   racing the off-loop batch-remove worker → the loop-resident write paths
   sync under the lock; the loop-resident READ paths (``list_jobs`` /
   ``get_job``) are cache-only — they perform NO filesystem I/O at all (no
   ``read_bytes``/hash, no lock-file open), so a large store can never freeze
   the loop. Cross-process freshness is maintained off-loop by the timer tick
   and mutators; callers needing a guaranteed-fresh read use the offloaded
   ``list_jobs_async`` / ``get_job_async`` variants.
3. mtime-equality staleness in ``_sync`` losing same-second external writes,
   and (mtime_ns, size) collisions losing equal-length content rewrites
   → ``_sync`` compares a content DIGEST derived from the same bytes it parses.
4. ``_arm_timer`` cancelling the currently-running timer task
   → ``_arm_timer`` never cancels ``asyncio.current_task()``.
5. Skip-date lookahead bound coupled to schedule granularity (silently
   returning ``None`` for schedules finer than the cap was sized for)
   → the advance is bounded by a wall-clock horizon, not an iteration count.

The loop-resident scheduling paths (timer tick, reaper sweep, ``run_job``) never
take the store lock or ``_sync()`` on the event loop: the timer tick and
``run_job`` offload their locked sync + snapshot to a worker thread
(``asyncio.to_thread``) and run only the mutation-free scan/claim on the loop,
while the reaper snapshots the atomically-swapped in-memory job list cache-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.cron import (
    _MAX_SKIP_DATE_LOOKAHEAD,
    CronJob,
    CronSchedule,
    CronService,
    CronStoreBusy,
    compute_next_run_ts,
)

# ── Bug 1: blocking flock on the event loop ──


class TestFileLockNonBlocking:
    def test_file_lock_is_bounded_when_contended(self, tmp_path: Path) -> None:
        """A held lock must make ``_file_lock`` fail fast, not park forever.

        Pre-fix, ``_file_lock`` used a blocking ``fcntl.flock(LOCK_EX)``; with a
        separate fd already holding the lock this call would block the event
        loop indefinitely (the audit's failure mode). The non-blocking spin
        raises ``TimeoutError`` within the bounded timeout instead.
        """
        svc = CronService(base_dir=tmp_path)
        svc._dir.mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / ".crons.lock").open("w")
        # flock on a separate open description conflicts within one process too.
        platform_compat.acquire_lock(holder.fileno(), exclusive=True)
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                with svc._file_lock(timeout=0.3, poll=0.02):
                    pass  # pragma: no cover — never reached while contended
            elapsed = time.monotonic() - start
            # Bounded: fails near the timeout, does not park indefinitely.
            assert 0.25 <= elapsed < 5.0
        finally:
            platform_compat.release_lock(holder.fileno())
            holder.close()

    def test_file_lock_succeeds_once_released(self, tmp_path: Path) -> None:
        """Uncontended acquisition still works (and mutators still persist)."""
        svc = CronService(base_dir=tmp_path)
        with svc._file_lock(timeout=1.0):
            pass
        # A real mutator drives the same lock end-to-end.
        job = svc.add_job(name="j", message="m", every_secs=60)
        assert svc.get_job(job.id) is not None


# ── Bug 2: unlocked read paths racing the remove worker ──


class TestReadPathsLocked:
    def test_on_loop_reads_do_not_touch_the_filesystem(self, tmp_path: Path) -> None:
        """``list_jobs`` / ``get_job`` are cache-only — zero filesystem I/O.

        GPT 5.6 HIGH (cron.py:2026): the read paths ran ``_sync()`` on the
        event loop, which ``read_bytes()`` + blake2b-hashed the ENTIRE
        ``crons.json`` on every call — and ``list_jobs`` fires directly on the
        loop from the dashboard WS status push and Slack handlers, so a large
        store froze the loop with synchronous I/O. The reads are now cache-only:
        they must never open the lock file, stat, read, or hash the store.
        """
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=60)

        # Any store I/O would go through _sync / _load / the read-lock file
        # open, or a raw read of crons.json. Forbid all of them for the
        # duration of the on-loop reads.
        orig_read = Path.read_bytes

        def _no_store_read(self: Path, *a, **k):  # type: ignore[no-untyped-def]
            if self == svc._path:
                raise AssertionError("on-loop read must not read_bytes(crons.json)")
            return orig_read(self, *a, **k)

        def _boom(name: str):  # type: ignore[no-untyped-def]
            def _fn(*a, **k):  # type: ignore[no-untyped-def]
                raise AssertionError(f"on-loop read must not call {name}")
            return _fn

        with contextlib.ExitStack() as stack:
            for meth in ("_sync", "_load", "_file_lock"):
                stack.enter_context(patch.object(svc, meth, _boom(meth)))
            stack.enter_context(patch.object(Path, "read_bytes", _no_store_read))

            listed = svc.list_jobs(include_disabled=True)
            got = svc.get_job(job.id)

        assert {j.id for j in listed} == {job.id}
        assert got is not None and got.id == job.id

    def test_reads_never_block_even_while_store_lock_held(self, tmp_path: Path) -> None:
        """Cache-only reads return promptly even while the store lock is held.

        The read paths no longer touch the lock at all, so a mutator holding
        the store lock from a separate open description can never delay or
        block a read.
        """
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=60)

        svc._dir.mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / ".crons.lock").open("w")
        platform_compat.acquire_lock(holder.fileno(), exclusive=True)
        try:
            start = time.monotonic()
            listed = svc.list_jobs(include_disabled=True)  # must not raise/block
            got = svc.get_job(job.id)                      # must not raise/block
            elapsed = time.monotonic() - start
            assert elapsed < 1.0
            assert {j.id for j in listed} == {job.id}
            assert got is not None and got.id == job.id
        finally:
            platform_compat.release_lock(holder.fileno())
            holder.close()

    @pytest.mark.asyncio
    async def test_async_reads_observe_external_write_offloaded(self, tmp_path: Path) -> None:
        """``list_jobs_async`` / ``get_job_async`` pick up a cross-process write.

        The cache-only sync reads intentionally do not observe another
        process's write until the next timer refresh; the async variants
        offload a locked ``_sync()`` + snapshot to a worker thread so a caller
        that needs guaranteed freshness sees it immediately — without blocking
        the event loop (the read/hash/parse happens off-loop).
        """
        svc = CronService(base_dir=tmp_path)
        svc.add_job(name="j1", message="m", every_secs=60)

        # Simulate a second process appending a job by writing the store
        # directly and clearing our in-memory fingerprint's authority via a
        # separate service instance sharing the same directory.
        other = CronService(base_dir=tmp_path)
        other._sync()
        other.add_job(name="j2", message="m", every_secs=60)

        # The cache-only read on the first service still shows the stale view.
        assert {j.name for j in svc.list_jobs(include_disabled=True)} == {"j1"}

        # The async read offloads a locked _sync() and observes the new job.
        fresh = await svc.list_jobs_async(include_disabled=True)
        assert {j.name for j in fresh} == {"j1", "j2"}

        target = next(j for j in fresh if j.name == "j2")
        got = await svc.get_job_async(target.id)
        assert got is not None and got.name == "j2"

    @pytest.mark.asyncio
    async def test_async_read_does_not_block_loop_offloads_to_thread(self, tmp_path: Path) -> None:
        """The async read runs its store I/O in a worker thread, not on the loop."""
        svc = CronService(base_dir=tmp_path)
        svc.add_job(name="j", message="m", every_secs=60)

        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        orig_sync = svc._sync

        def _track_sync() -> None:
            seen["thread"] = threading.get_ident()
            orig_sync()

        svc._sync = _track_sync  # type: ignore[method-assign]
        await svc.list_jobs_async(include_disabled=True)
        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, "sync/read must run off the event loop"

    @pytest.mark.asyncio
    async def test_timer_tick_does_no_store_io_on_the_loop(self, tmp_path: Path) -> None:
        """The timer tick's locked ``_sync()`` runs OFF the event loop.

        GPT 5.6 HIGH (cron.py:1652, BLOCK-MERGE): ``_on_timer`` did
        ``async with self._afile_lock(): self._sync()`` directly on the loop,
        so every tick ``read_bytes()`` + blake2b-hashed the WHOLE
        ``crons.json`` on the loop — freezing chat/WS/timers/heartbeat for a
        large or slow store. The locked sync + deferred-removal drain + snapshot
        is now offloaded to a worker thread (``_tick_scan_locked`` via
        ``asyncio.to_thread``); the loop only runs the mutation-free due-scan.
        This asserts, mechanically, that no ``_sync()`` and no
        ``read_bytes(crons.json)`` ever executes on the loop thread during a
        tick — mirroring the ``list_jobs``/``get_job`` forbidden-I/O test.
        """
        svc = CronService(base_dir=tmp_path)
        # every_secs=3600 created now → NOT due, so no run task spawns and the
        # tick reduces to exactly the refresh-scan path under test.
        svc.add_job(name="j", message="m", every_secs=3600)
        svc._running = True

        loop_thread = threading.get_ident()
        sync_threads: list[int] = []
        orig_sync = svc._sync

        def _track_sync() -> None:
            sync_threads.append(threading.get_ident())
            orig_sync()

        orig_read = Path.read_bytes

        def _guard_read(self: Path, *a, **k):  # type: ignore[no-untyped-def]
            if self == svc._path and threading.get_ident() == loop_thread:
                raise AssertionError("timer tick must not read_bytes(crons.json) on the loop")
            return orig_read(self, *a, **k)

        svc._sync = _track_sync  # type: ignore[method-assign]
        with patch.object(Path, "read_bytes", _guard_read):
            await svc._on_timer()

        assert sync_threads, "the tick must still refresh the store under the lock"
        assert all(t != loop_thread for t in sync_threads), (
            "the tick's _sync() must run in a worker thread, never on the event loop"
        )
        assert not svc._executing and not svc._running_tasks, "premise: nothing due"

        svc._running = False
        if svc._timer_task and not svc._timer_task.done():
            svc._timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await svc._timer_task

    @pytest.mark.asyncio
    async def test_run_job_syncs_under_store_lock(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        ran: list[str] = []

        async def on_job(job: CronJob) -> None:
            ran.append(job.id)

        svc._on_job = on_job
        job = svc.add_job(name="j", message="m", every_secs=60)

        calls = {"n": 0}
        real = svc._file_lock

        @contextlib.contextmanager
        def spy(*a, **k):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            with real(*a, **k):
                yield

        svc._file_lock = spy  # type: ignore[method-assign, assignment]
        assert await svc.run_job(job.id) is True
        assert ran == [job.id]
        assert calls["n"] >= 1, "run_job must sync under the store lock"

    @pytest.mark.asyncio
    async def test_concurrent_batch_remove_and_reads_stay_coherent(
        self, tmp_path: Path
    ) -> None:
        """Reads racing the off-loop batch-remove worker never tear.

        ``remove_jobs`` mutates ``self._jobs`` from an ``asyncio.to_thread``
        worker under the store lock. Interleaving ``list_jobs`` / ``get_job``
        must observe a coherent list (each job fully present or fully absent),
        never a half-rebuilt one, and must not raise.
        """
        svc = CronService(base_dir=tmp_path)
        jobs = [svc.add_job(name=f"j{i}", message="m", every_secs=60) for i in range(40)]
        ids = [j.id for j in jobs]

        async def reader() -> None:
            for _ in range(200):
                listed = {j.id for j in svc.list_jobs(include_disabled=True)}
                # Coherence: every listed id is a real, known id.
                assert listed <= set(ids)
                for jid in list(listed)[:3]:
                    svc.get_job(jid)
                await asyncio.sleep(0)

        remove_task = asyncio.create_task(svc.remove_jobs(ids[::2]))
        read_tasks = [asyncio.create_task(reader()) for _ in range(4)]
        await asyncio.gather(remove_task, *read_tasks)

        remaining = {j.id for j in svc.list_jobs(include_disabled=True)}
        assert remaining == set(ids[1::2])


# ── Constructor / startup initial load: off-loop on the gateway path ──
# GPT 5.6 HIGH (BLOCK-MERGE, head c2601f20): the exhaustive read-path/timer/
# reaper sweep left ONE loop-resident blocking-I/O site — INITIALIZATION. The
# plain constructor calls _load() (a whole-file read_bytes() + blake2b hash of
# crons.json) synchronously, and the gateway constructs its CronService INSIDE
# its running async startup coroutine, so that initial load blocked the sole
# event loop (chat, WS, timers, heartbeat). The fix: loop contexts construct via
# the async factory CronService.create() (which offloads the initial _load via
# asyncio.to_thread), and start() also offloads its _load. Genuinely-sync,
# loop-less callers (CLI/MCP/apps-SDK/tests) keep the inline-loading plain
# constructor.


class TestConstructionLoadOffLoop:
    @pytest.mark.asyncio
    async def test_create_factory_loads_off_the_event_loop(self, tmp_path: Path) -> None:
        """``await CronService.create(...)`` runs the initial _load off-loop.

        Mechanical proof: no ``_load()`` and no ``read_bytes(crons.json)`` may
        execute on the event-loop thread while the factory builds the service.
        Pre-fix (plain constructor on the loop) this read+hashed the whole store
        on the loop thread.
        """
        # Seed a store on disk so the initial load has real bytes to read.
        seed = CronService(base_dir=tmp_path)
        seed.add_job(name="j", message="m", every_secs=3600)
        store_path = seed._path
        assert store_path.exists()

        loop_thread = threading.get_ident()
        load_threads: list[int] = []
        orig_load = CronService._load

        def _track_load(self: CronService, *a, **k):  # type: ignore[no-untyped-def]
            load_threads.append(threading.get_ident())
            return orig_load(self, *a, **k)

        orig_read = Path.read_bytes

        def _guard_read(self: Path, *a, **k):  # type: ignore[no-untyped-def]
            if self == store_path and threading.get_ident() == loop_thread:
                raise AssertionError(
                    "CronService.create() must not read_bytes(crons.json) on the loop"
                )
            return orig_read(self, *a, **k)

        with patch.object(CronService, "_load", _track_load), patch.object(
            Path, "read_bytes", _guard_read
        ):
            svc = await CronService.create(base_dir=tmp_path)

        assert load_threads, "the factory must still perform the initial load"
        assert all(t != loop_thread for t in load_threads), (
            "the initial _load() must run in a worker thread, never on the event loop"
        )
        # And it actually loaded the on-disk state (fresh-service invariant).
        assert {j.id for j in svc.list_jobs(include_disabled=True)} == {
            j.id for j in seed.list_jobs(include_disabled=True)
        }
        assert svc._running is False, "create() arms no timer"

    @pytest.mark.asyncio
    async def test_start_loads_off_the_event_loop(self, tmp_path: Path) -> None:
        """``await svc.start()`` also offloads its ``_load()`` to a worker."""
        svc = await CronService.create(base_dir=tmp_path)
        svc.add_job(name="j", message="m", every_secs=3600)

        loop_thread = threading.get_ident()
        load_threads: list[int] = []
        orig_load = svc._load

        def _track_load(*a, **k):  # type: ignore[no-untyped-def]
            load_threads.append(threading.get_ident())
            return orig_load(*a, **k)

        svc._load = _track_load  # type: ignore[method-assign, assignment]
        try:
            await svc.start()
            assert load_threads, "start() must load the store"
            assert all(t != loop_thread for t in load_threads), (
                "start()'s _load() must run in a worker thread, never on the loop"
            )
        finally:
            await svc.stop()

    def test_plain_constructor_loads_inline_for_sync_callers(self, tmp_path: Path) -> None:
        """Loop-less callers (CLI/MCP/apps-SDK) still get an inline initial load.

        The plain constructor must reflect on-disk state immediately without
        needing an event loop or an ``await`` — the fresh-service invariant the
        MCP/CLI processes and tests rely on.
        """
        seed = CronService(base_dir=tmp_path)
        seed.add_job(name="j", message="m", every_secs=3600)

        fresh = CronService(base_dir=tmp_path)  # no await, no loop
        assert {j.id for j in fresh.list_jobs(include_disabled=True)} == {
            j.id for j in seed.list_jobs(include_disabled=True)
        }


# ── Bug 3: mtime-equality staleness in _sync ──


class TestSyncFingerprint:
    def test_sync_detects_equal_mtime_external_write(self, tmp_path: Path) -> None:
        """An external write landing on the SAME mtime tick is still reloaded.

        Pre-fix ``_sync`` used ``mtime > self._last_mtime``; a second write in
        the same coarse mtime second had an equal mtime and was skipped,
        silently dropping the update. We force the on-disk mtime back to the
        exact nanosecond of our last load — only the size changes — and assert
        the reload still happens.
        """
        svc = CronService(base_dir=tmp_path)
        svc.add_job(name="a", message="m", every_secs=60)
        orig_ns = svc._last_mtime_ns
        orig_size = svc._last_size

        # A second process appends a job (changes the file size + content).
        other = CronService(base_dir=tmp_path)
        other._load()
        other.add_job(name="b", message="m2", every_secs=60)

        # Emulate two writes within one mtime tick: force mtime back to ours.
        os.utime(svc._path, ns=(orig_ns, orig_ns))
        st = svc._path.stat()
        assert st.st_mtime_ns == orig_ns  # equal mtime → old check would skip
        assert st.st_size != orig_size    # but the content genuinely changed

        svc._sync()
        assert {j.name for j in svc._jobs} == {"a", "b"}

    def test_sync_detects_equal_mtime_and_size_content_rewrite(self, tmp_path: Path) -> None:
        """An external write preserving BOTH mtime and byte length is reloaded.

        A (mtime_ns, size) fingerprint collides when a rewrite keeps the coarse
        timestamp and the exact length (e.g. renaming a job to an equal-length
        name). The digest-based check catches it where the tuple check could
        not, preventing the stale in-memory state from being re-saved over the
        external change.
        """
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="aaaa", message="m", every_secs=60)
        orig_ns = svc._last_mtime_ns
        orig_size = svc._last_size

        # An external process renames the job to an equal-length name, then we
        # pin mtime back to ours: same mtime_ns AND same size, different bytes.
        other = CronService(base_dir=tmp_path)
        other._load()
        other.update_job(job.id, name="bbbb")
        os.utime(svc._path, ns=(orig_ns, orig_ns))
        st = svc._path.stat()
        assert st.st_mtime_ns == orig_ns  # equal mtime
        assert st.st_size == orig_size    # AND equal size → tuple check would skip

        svc._sync()
        assert {j.name for j in svc._jobs} == {"bbbb"}

    def test_save_does_not_trigger_self_reload(self, tmp_path: Path) -> None:
        """Our own save refreshes the fingerprint so _sync is a no-op after it."""
        svc = CronService(base_dir=tmp_path)
        svc.add_job(name="a", message="m", every_secs=60)
        before = list(svc._jobs)
        svc._sync()  # must NOT reload our own just-saved write
        assert [j.id for j in svc._jobs] == [j.id for j in before]


# ── Bug 4: _arm_timer cancels the running timer task ──


class TestArmTimerNoSelfCancel:
    @pytest.mark.asyncio
    async def test_arm_timer_does_not_cancel_current_task(self, tmp_path: Path) -> None:
        """Re-arming from within the running timer task must not self-cancel.

        Pre-fix, ``_arm_timer`` blindly cancelled ``self._timer_task``; when the
        tick's own ``finally`` re-armed (``self._timer_task`` == the running
        task) this fired a CancelledError into the in-flight ``_on_timer``
        dispatch. Here the standby task re-arms and then yields — under the old
        code the yield raises CancelledError; under the fix it completes.
        """
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        result: dict[str, bool] = {}

        async def fake_tick() -> None:
            # Stand in for the running timer task re-arming from its finally.
            svc._arm_timer()
            await asyncio.sleep(0)  # old code delivers the self-cancel here
            result["completed"] = True

        task = asyncio.create_task(fake_tick())
        svc._timer_task = task  # set before any await point so identity holds

        outcome = "done"
        try:
            await task
        except asyncio.CancelledError:
            outcome = "cancelled"

        try:
            assert outcome == "done", "the running timer task was self-cancelled"
            assert result.get("completed") is True
            assert svc._timer_task is not None
            assert svc._timer_task is not task, "a fresh timer must be armed"
        finally:
            svc._running = False
            if svc._timer_task and not svc._timer_task.done():
                svc._timer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await svc._timer_task


# ── Bug 6: _arm_timer performing event-loop task work off the loop ──
# GPT 5.6 HIGH cron.py:1041 — an async mutator offloads its locked core to an
# asyncio.to_thread worker; when an external write changed the digest, that
# worker's _sync()->_load() reached _arm_timer(), which called
# asyncio.create_task() with NO running loop (RuntimeError) after possibly
# cancelling the live timer — silently stopping every scheduled job. The fix
# makes _arm_timer safe off the loop AND self-healing: it never touches asyncio
# tasks off-loop, and instead hands the (re)arm back to the bound event loop
# (captured in create()/start()) via loop.call_soon_threadsafe(self._arm_timer).
# Arming is thus owned by CronService — no caller drains a deferred arm.


class TestArmTimerOffLoopReload:
    @pytest.mark.asyncio
    async def test_arm_timer_off_loop_reschedules_on_bound_loop(
        self, tmp_path: Path
    ) -> None:
        """Called from a worker thread, _arm_timer must not raise or cancel the
        live timer inline; it hands the (re)arm back to the bound loop, which
        arms a fresh timer there. No caller-side drain is involved."""
        svc = CronService(base_dir=tmp_path)
        # create()/start() bind the loop in production; do it explicitly here.
        svc._loop = asyncio.get_running_loop()
        svc.add_job(name="a", message="m", every_secs=60)
        svc._running = True
        svc._arm_timer()
        armed = svc._timer_task
        assert armed is not None and not armed.done()

        def in_worker() -> None:
            # A thread with no running event loop, mirroring asyncio.to_thread.
            svc._arm_timer()

        await asyncio.to_thread(in_worker)

        # Reaching here proves the worker-thread _arm_timer neither raised
        # (asyncio.to_thread would propagate) nor blocked. It scheduled a re-arm
        # on the bound loop via call_soon_threadsafe; ensure that callback ran.
        await asyncio.sleep(0)
        assert svc._timer_task is not None and not svc._timer_task.done(), (
            "the bound loop must self-heal a live timer after an off-loop arm"
        )
        assert svc._timer_task is not armed, "the bound loop re-armed a fresh timer"

        svc._running = False
        if svc._timer_task and not svc._timer_task.done():
            svc._timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await svc._timer_task

    @pytest.mark.asyncio
    async def test_async_mutator_digest_reload_does_not_raise_and_stays_armed(
        self, tmp_path: Path
    ) -> None:
        """An async mutator that triggers a worker-thread reload stays healthy.

        External write changes the digest; the mutator's offloaded core
        _sync()->_load()s in the worker and reaches _arm_timer there. Pre-fix
        that raised RuntimeError (and left the timer cancelled). Post-fix the
        mutator completes, observes the external change, and the timer stays
        armed on the loop (the mutator's own on-loop arm plus the worker's
        self-healing re-arm both target the bound loop).
        """
        svc = CronService(base_dir=tmp_path)
        svc._loop = asyncio.get_running_loop()
        existing = svc.add_job(name="a", message="m", every_secs=60)
        svc._running = True
        svc._arm_timer()
        first_timer = svc._timer_task
        assert first_timer is not None and not first_timer.done()

        # A second process appends a job → changes the on-disk digest so the
        # worker's _sync() must reload (and re-arm) off the loop.
        other = CronService(base_dir=tmp_path)
        other._load()
        other.add_job(name="b", message="m2", every_secs=60)

        updated = await svc.update_job_async(existing.id, name="renamed")

        assert updated is not None and updated.name == "renamed"
        # The worker's reload observed the external add.
        assert {j.name for j in svc.list_jobs(include_disabled=True)} == {"renamed", "b"}
        # Let any loop-scheduled re-arm callback run, then confirm still armed.
        await asyncio.sleep(0)
        assert svc._timer_task is not None and not svc._timer_task.done()

        svc._running = False
        if svc._timer_task and not svc._timer_task.done():
            svc._timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await svc._timer_task


# ── GPT 5.6 HIGH (cron.py:2068) + Arbiter BLOCK item 1 — the app CronSDK ──
# surface. The app-facing CronSDK mutation API is now async and routes through
# CronService's *_async mutators, so an app hook / route handler calling it from
# the event loop performs NO blocking store-lock spin (time.sleep) on the loop
# thread. And timer arming is owned by CronService IN-SERVICE (see _arm_timer),
# so a newly registered cron's timer is (re)armed WITHOUT any caller-side drain
# (the removed _arm_after_offload / rearm_after_offload pairing that every future
# offload site would otherwise have had to remember).


class TestAppSDKMutationLoopSafety:
    @pytest.mark.asyncio
    async def test_sdk_add_job_does_no_lock_spin_on_the_loop(
        self, tmp_path: Path
    ) -> None:
        """A CronSDK mutation awaited on the loop takes ``_file_lock`` (whose
        contention spin does ``time.sleep``) only in a worker thread — never on
        the event-loop thread. Mirrors the mechanical forbidden-I/O tests."""
        from kiro_crew.apps.cron_sdk import CronSDK

        svc = await CronService.create(base_dir=tmp_path)
        await svc.start()
        real_lock = svc._file_lock
        try:
            sdk = CronSDK("demo-app", svc)
            loop_thread = threading.get_ident()

            @contextlib.contextmanager
            def guarded_lock(*a, **k):  # type: ignore[no-untyped-def]
                if threading.get_ident() == loop_thread:
                    raise AssertionError(
                        "CronSDK mutation must not take _file_lock on the event loop"
                    )
                with real_lock(*a, **k):
                    yield

            svc._file_lock = guarded_lock  # type: ignore[method-assign, assignment]

            job = await sdk.add_job_async(name="demo-app/x", message="m", every_secs=3600)
            assert job is not None and job.created_by == "app:demo-app"
            # Fully persisted through the (off-loop) locked core.
            assert any(j.id == job.id for j in svc.list_jobs(include_disabled=True))
        finally:
            svc._file_lock = real_lock  # type: ignore[method-assign, assignment]
            await svc.stop()

    @pytest.mark.asyncio
    async def test_newly_registered_cron_timer_armed_without_caller_drain(
        self, tmp_path: Path
    ) -> None:
        """Registering a cron via the async CronSDK re-arms the timer with NO
        caller-side drain call — arming is owned by CronService."""
        from kiro_crew.apps.cron_sdk import CronSDK

        svc = await CronService.create(base_dir=tmp_path)
        await svc.start()
        try:
            before = svc._timer_task
            assert before is not None
            sdk = CronSDK("demo-app", svc)

            # No rearm_after_offload / _arm_after_offload exists anymore.
            assert not hasattr(svc, "rearm_after_offload")
            assert not hasattr(svc, "_arm_after_offload")

            await sdk.add_job_async(name="demo-app/y", message="m", every_secs=3600)

            assert svc._timer_task is not None and not svc._timer_task.done()
            assert svc._timer_task is not before, "add must (re)arm the timer in-service"
        finally:
            await svc.stop()


# ── Bug 5: _MAX_SKIP_DATE_LOOKAHEAD too small for daily crons ──

class TestSkipDateLookahead:
    def test_daily_cron_with_long_skip_range_resolves(self) -> None:
        """A daily cron skipping a long consecutive range still finds a fire.

        60 consecutive skipped days exceeds the old cap of 52, so the pre-fix
        code exhausted the loop and returned None — the job never fired again.
        The bumped bound advances past the whole range to the first open day.
        """
        base = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
        now = base.timestamp()
        skip_days = 60
        skip_dates = [
            (base.date() + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(skip_days)
        ]
        job = CronJob(
            id="daily",
            name="daily",
            message="m",
            schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *"),
            timezone="UTC",
            skip_dates=skip_dates,
            created_ts=now - 3600,
        )
        # Guard the premise: the skip range is longer than the OLD cap of 52.
        assert skip_days > 52
        assert skip_days < _MAX_SKIP_DATE_LOOKAHEAD

        result = compute_next_run_ts(job, now=now)
        assert result is not None, "daily cron with a long skip range must still fire"
        got = datetime.fromtimestamp(result, tz=timezone.utc).strftime("%Y-%m-%d")
        expected = (base.date() + timedelta(days=skip_days)).strftime("%Y-%m-%d")
        assert got == expected

    def test_subdaily_cron_with_multiday_skip_resolves(self) -> None:
        """A sub-daily (*/5) cron with a multi-day skip range still resolves.

        This is the failure the wall-clock horizon fixes that an iteration cap
        could not: a */5 cron does 288 fires/day, so a fixed daily-sized cap is
        exhausted within days and the job silently returns None. Bounding by
        wall-clock time makes the schedule granularity irrelevant.
        """
        base = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
        now = base.timestamp()
        skip_days = 10  # 10 × 288 = 2880 fires — far beyond any daily-sized cap
        skip_dates = [
            (base.date() + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(skip_days)
        ]
        job = CronJob(
            id="every5",
            name="every5",
            message="m",
            schedule=CronSchedule(kind="cron", cron_expr="*/5 * * * *"),
            timezone="UTC",
            skip_dates=skip_dates,
            created_ts=now - 3600,
        )
        result = compute_next_run_ts(job, now=now)
        assert result is not None, "sub-daily cron must resolve past a multi-day skip"
        got = datetime.fromtimestamp(result, tz=timezone.utc).strftime("%Y-%m-%d")
        expected = (base.date() + timedelta(days=skip_days)).strftime("%Y-%m-%d")
        assert got == expected


# ── Bug 1 (mutator boundary): sync mutators now have a DEFINED failure ──
# contract (CronStoreBusy) and event-loop-safe async variants that offload the
# lock+save to a worker thread instead of parking the loop.


class TestMutatorContract:
    """The mutator boundary: named CronStoreBusy contract + async offload.

    Arbiter BLOCK item 1: the sync mutators surfaced a bare ``TimeoutError`` at
    every public scheduling boundary with no handling story. The fix (a) makes
    that a named, greppable ``CronStoreBusy`` (still a ``TimeoutError`` subclass
    so the reaper/timer guards keep catching it), and (b) adds ``*_async``
    variants that run the lock+save off the event loop so the gateway boundaries
    never park the loop and can translate contention into a clean retryable
    error.
    """

    def test_cronstorebusy_is_timeouterror_subclass(self) -> None:
        # Existing ``except TimeoutError`` guards (reaper sweep, timer tick,
        # read-path degrade) must keep catching the mutator failure.
        assert issubclass(CronStoreBusy, TimeoutError)

    def test_sync_mutators_raise_cronstorebusy_when_contended(self, tmp_path: Path) -> None:
        """Each sync mutator raises the named contract under lock contention."""
        svc = CronService(base_dir=tmp_path)
        existing = svc.add_job(name="j", message="m", every_secs=60)
        # Shrink the timeout so the bounded spin fails fast under the held lock.
        svc._file_lock = _bounded_lock(svc)  # type: ignore[method-assign, assignment]

        svc._dir.mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / ".crons.lock").open("w")
        platform_compat.acquire_lock(holder.fileno(), exclusive=True)
        try:
            with pytest.raises(CronStoreBusy):
                svc.add_job(name="k", message="m", every_secs=60)
            with pytest.raises(CronStoreBusy):
                svc.update_job(existing.id, name="renamed")
            with pytest.raises(CronStoreBusy):
                svc.remove_job(existing.id)
            with pytest.raises(CronStoreBusy):
                svc.enable_job(existing.id, enabled=False)
            with pytest.raises(CronStoreBusy):
                svc.ack_job(existing.id, "note")
            with pytest.raises(CronStoreBusy):
                svc.unack_job(existing.id)
        finally:
            platform_compat.release_lock(holder.fileno())
            holder.close()

    def test_async_mutators_do_not_block_loop_and_raise_cronstorebusy(
        self, tmp_path: Path
    ) -> None:
        """Async variants offload the lock+save; a contended store surfaces
        ``CronStoreBusy`` on ``await`` while the loop keeps ticking."""
        async def scenario() -> None:
            svc = CronService(base_dir=tmp_path)
            job = svc.add_job(name="j", message="m", every_secs=60)
            svc._file_lock = _bounded_lock(svc)  # type: ignore[method-assign, assignment]

            svc._dir.mkdir(parents=True, exist_ok=True)
            holder = (tmp_path / ".crons.lock").open("w")
            platform_compat.acquire_lock(holder.fileno(), exclusive=True)

            # A concurrent loop task proving the loop was never parked while the
            # mutator waited on the (offloaded) contended lock.
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                for _ in range(5):
                    await asyncio.sleep(0.02)
                    ticks += 1

            tick_task = asyncio.create_task(ticker())
            try:
                with pytest.raises(CronStoreBusy):
                    await svc.add_job_async(name="k", message="m", every_secs=60)
                with pytest.raises(CronStoreBusy):
                    await svc.update_job_async(job.id, name="renamed")
                with pytest.raises(CronStoreBusy):
                    await svc.remove_job_async(job.id)
                with pytest.raises(CronStoreBusy):
                    await svc.enable_job_async(job.id, enabled=False)
            finally:
                platform_compat.release_lock(holder.fileno())
                holder.close()
                await tick_task
            # The event loop advanced its other task while mutators waited.
            assert ticks == 5

        asyncio.run(scenario())

    def test_async_mutators_persist_when_uncontended(self, tmp_path: Path) -> None:
        """The async variants really mutate + persist through the same store."""
        async def scenario() -> None:
            svc = CronService(base_dir=tmp_path)
            job = await svc.add_job_async(name="j", message="m", every_secs=60)
            assert svc.get_job(job.id) is not None
            updated = await svc.update_job_async(job.id, name="renamed")
            assert updated is not None and updated.name == "renamed"
            assert await svc.enable_job_async(job.id, enabled=False) is True
            assert await svc.ack_job_async(job.id, "note") is True
            assert await svc.unack_job_async(job.id) is True
            assert await svc.remove_job_async(job.id) is True
            assert svc.get_job(job.id) is None

        asyncio.run(scenario())


# ── Bug 1 (result-merge boundary): the job-completion merge is the last ──
# loop-resident entry into the sync _file_lock spin. _run_job_isolated runs as
# an event-loop task; its finally offloads _merge_job_result via
# asyncio.to_thread so a contended store never parks the loop (chat, heartbeat,
# timer) for up to _FILE_LOCK_TIMEOUT_SECS.


class TestMergeResultOffLoop:
    """GPT 5.6 HIGH [BLOCK-MERGE] cron.py:1799 — _merge_job_result on the loop.

    _run_job_isolated is an event-loop task, and its result-merge entered the
    bounded sync _file_lock whose spin does time.sleep(poll) under contention,
    freezing the whole gateway loop. The fix offloads the merge to a worker
    thread via asyncio.to_thread.
    """

    @pytest.mark.asyncio
    async def test_merge_result_is_offloaded_and_keeps_loop_ticking(
        self, tmp_path: Path
    ) -> None:
        svc = CronService(base_dir=tmp_path)

        async def on_job(job: CronJob) -> None:
            return None

        svc._on_job = on_job
        svc._file_lock = _bounded_lock(svc)  # type: ignore[method-assign, assignment]
        job = svc.add_job(name="j", message="m", every_secs=60)

        # Prove the merge runs off the event loop: assert we are NOT on the
        # running loop's thread when the lock/save transaction executes.
        loop = asyncio.get_running_loop()
        merged_off_loop = {"ok": False}
        real_merge = svc._merge_job_result

        def spy_merge(j: CronJob) -> None:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            # In a worker thread there is no running loop bound to this thread.
            merged_off_loop["ok"] = running is not loop
            real_merge(j)

        svc._merge_job_result = spy_merge  # type: ignore[method-assign, assignment]

        # Contend the store lock from a separate fd so the merge would spin
        # time.sleep if it ran on the loop; a concurrent ticker proves it did
        # not park the loop.
        svc._dir.mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / ".crons.lock").open("w")
        platform_compat.acquire_lock(holder.fileno(), exclusive=True)

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        try:
            svc._executing.add(job.id)
            svc._job_run_meta[job.id] = (time.time(), "scheduled")
            # The merge will raise CronStoreBusy inside to_thread (contended);
            # _run_job_isolated swallows it (best-effort) and completes without
            # ever parking the loop.
            await svc._run_job_isolated(job)
            await tick_task
        finally:
            platform_compat.release_lock(holder.fileno())
            holder.close()

        assert merged_off_loop["ok"], "merge must run off the event loop (to_thread)"
        assert ticks == 5, "the event loop must keep ticking while the merge waits"

    @pytest.mark.asyncio
    async def test_merge_result_persists_uncontended(self, tmp_path: Path) -> None:
        """Uncontended, the offloaded merge still persists runtime state."""
        svc = CronService(base_dir=tmp_path)

        async def on_job(job: CronJob) -> None:
            return None

        svc._on_job = on_job
        job = svc.add_job(name="j", message="m", every_secs=60)
        svc._executing.add(job.id)
        svc._job_run_meta[job.id] = (time.time(), "scheduled")
        await svc._run_job_isolated(job)

        # A fresh service reading the same store sees the persisted last_run_ts.
        reloaded = CronService(base_dir=tmp_path)
        got = reloaded.get_job(job.id)
        assert got is not None and got.last_run_ts is not None
        assert got.last_status == "ok"


# ── Arbiter BLOCK item 1: deferred one-shot removal must be real ──
# _deliver_script_result caught CronStoreBusy on the fire-and-forget removal of
# a completed delete_after_run/Done job and only LOGGED "deferring" — but no
# deferral existed, so the finished job lingered ENABLED with its recurring
# schedule and re-fired (duplicate execution + duplicate user notification).
# The fix: CronService.defer_removal disables the job in memory immediately and
# queues its id; the timer tick drains the queue under the store lock (in its
# worker-thread transaction, _tick_scan_locked) before the due-scan, so the job
# is always eventually removed and never re-fires.


class TestDeferredRemoval:
    """A Done/one-shot job whose immediate removal hit a busy store is still
    removed on the next timer tick and never re-fires in the interim."""

    def test_defer_removal_disables_in_memory_immediately(self, tmp_path: Path) -> None:
        """The queued job is disabled at once so the very next due-scan skips it
        even before the drain deletes it from disk."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="done-job", message="m", every_secs=60)
        assert svc.get_job(job.id).enabled is True

        svc.defer_removal(job.id)

        assert svc.get_job(job.id).enabled is False
        assert job.id in svc._pending_removals

    @pytest.mark.asyncio
    async def test_timer_tick_drains_deferred_removal_and_never_refires(
        self, tmp_path: Path
    ) -> None:
        """The next _on_timer removes the deferred one-shot from the store and
        does NOT execute it, even though its recurring schedule is due."""
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        # An 'every' job that is already due (created in the past, never run) —
        # exactly the recurring-until-removed shape of a Done script job.
        job = svc.add_job(name="done-job", message="m", every_secs=60)
        job.created_ts = time.time() - 120
        assert svc._is_due(job, time.time()) is True, "premise: job is due"

        # Simulate the gateway's busy-store removal handing off to defer_removal.
        svc.defer_removal(job.id)

        try:
            await svc._on_timer()

            # Drained from the store (and persisted — a fresh reader agrees).
            assert svc.get_job(job.id) is None
            assert not svc._pending_removals
            assert CronService(base_dir=tmp_path).get_job(job.id) is None
            # Never re-fired: no run task spawned, nothing marked executing.
            assert job.id not in svc._executing
            assert job.id not in svc._running_tasks
        finally:
            svc._running = False
            for t in list(svc._running_tasks.values()):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            if svc._timer_task and not svc._timer_task.done():
                svc._timer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await svc._timer_task

    @pytest.mark.asyncio
    async def test_drain_survives_sync_reenable(self, tmp_path: Path) -> None:
        """Even when the tick's _sync reloads the still-on-disk (enabled) job,
        the drain removes it before the due-scan, so it cannot re-fire."""
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        job = svc.add_job(name="done-job", message="m", every_secs=60)
        job.created_ts = time.time() - 120
        svc.defer_removal(job.id)
        # Force the next _sync to actually reload (which re-derives enabled=True
        # from the persisted, still-unpaused on-disk record).
        svc._last_digest = b""

        try:
            await svc._on_timer()
            assert svc.get_job(job.id) is None
            assert job.id not in svc._executing
        finally:
            svc._running = False
            for t in list(svc._running_tasks.values()):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            if svc._timer_task and not svc._timer_task.done():
                svc._timer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await svc._timer_task

    def test_drain_atomic_swap_never_loses_concurrent_add(self, tmp_path: Path) -> None:
        """Mechanical proof of the GPT 5.6 HIGH / Arbiter item-1 race fix.

        ``_drain_pending_removals_locked`` runs in the timer tick's WORKER
        thread while ``defer_removal`` adds ids from the EVENT-LOOP thread. If
        the drain read the queue (``& present``) and then ``.clear()``-ed it as
        two separate steps, an id added in between would be erased without ever
        being deleted from disk — the completed one-shot would re-fire.

        This test injects that exact concurrent ``defer_removal`` at the
        intersection step via a ``set`` subclass whose ``__and__`` adds a new
        id to the LIVE queue attribute. Post-fix (atomic swap first, fresh set
        installed as ``self._pending_removals`` before the intersection) the
        injected id lands in the fresh queue and survives for the next tick.
        Pre-fix (read-then-``.clear()`` on the same set) it was wiped — this
        assertion fails against the old code.
        """
        svc = CronService(base_dir=tmp_path)
        keep = svc.add_job(name="drained-now", message="m", every_secs=60)
        injected = svc.add_job(name="added-mid-drain", message="m", every_secs=60)

        class _InjectingSet(set):  # type: ignore[type-arg]
            _fired = False

            def __and__(self, other):  # type: ignore[no-untyped-def]
                result = super().__and__(other)
                if not _InjectingSet._fired:
                    _InjectingSet._fired = True
                    # Model a concurrent event-loop defer_removal firing during
                    # the worker-thread drain: it targets the LIVE attribute.
                    svc._pending_removals.add(injected.id)
                return result

        svc._pending_removals = _InjectingSet({keep.id})

        svc._drain_pending_removals_locked()

        # The claimed id was removed and persisted.
        assert svc.get_job(keep.id) is None
        assert CronService(base_dir=tmp_path).get_job(keep.id) is None
        # The concurrently-added id was NOT erased — it remains queued so the
        # next tick removes it. This is the crux of the atomicity fix.
        assert injected.id in svc._pending_removals
        assert _InjectingSet._fired, "premise: the concurrent add was injected"


# ── GPT 5.6 HIGH [BLOCK-MERGE] cron.py:697 / 892: reaper & cancel persist ──
# _force_reap and cancel are event-loop coroutines that mutated the in-memory
# job and called a BARE, unlocked self._save() directly on the loop. That both
# (a) parked the loop on the bounded _file_lock spin and (b) — the lost-update
# race GPT flagged — serialized a STALE self._jobs: between a concurrent
# add_job_async/update_job_async worker's _sync and its _save, the unlocked
# terminal save re-wrote crons.json from a job list that never saw the worker's
# add, silently dropping it (API reported success; the job vanished across
# restart and other processes). The fix routes both through the locked
# worker-thread helper _merge_terminal_state_locked via asyncio.to_thread, so
# every _jobs mutation + _save participates in ONE lock transaction that
# _sync()s first.


class TestTerminalStateMergeLocked:
    """Reaper/cancel terminal-state persistence is locked, offloaded, and does
    not clobber a concurrent add/update worker."""

    @pytest.mark.asyncio
    async def test_reap_persist_does_not_drop_concurrent_add(
        self, tmp_path: Path
    ) -> None:
        """A job added by a concurrent worker/process (on disk, not yet in this
        service's in-memory snapshot) MUST survive the reaper's terminal save.

        Fails against the pre-fix bare on-loop ``self._save()`` (which
        re-serialized the stale ``self._jobs`` and dropped the concurrent add).
        """
        svc = CronService(base_dir=tmp_path)
        running = svc.add_job(name="running-job", message="m", every_secs=60)

        # A second process/worker adds another job straight to disk. This
        # service's in-memory snapshot is now stale (still only knows `running`)
        # — exactly the window an add_job_async worker occupies between its
        # _sync and _save.
        other = CronService(base_dir=tmp_path)
        added = other.add_job(name="added-by-worker", message="m", every_secs=60)
        assert {j.id for j in svc._jobs} == {running.id}, "premise: snapshot is stale"

        # Reap the running job.
        svc._executing.add(running.id)
        svc._job_run_meta[running.id] = (time.time(), "scheduled")
        await svc._force_reap(running.id, 5.0, 1)

        # A fresh reader must see BOTH jobs; the reap recorded its error state.
        reloaded = CronService(base_dir=tmp_path)
        assert (
            reloaded.get_job(added.id) is not None
        ), "the concurrently-added job must survive the reaper persist"
        reaped = reloaded.get_job(running.id)
        assert reaped is not None and reaped.last_status == "error"
        assert reaped.last_error and "Reaped" in reaped.last_error

    @pytest.mark.asyncio
    async def test_cancel_persist_does_not_drop_concurrent_add(
        self, tmp_path: Path
    ) -> None:
        """Same lost-update guarantee for the user-cancel terminal path."""
        with patch("kiro_crew.cron.cron_script.kill_running_process", return_value=False):
            svc = CronService(base_dir=tmp_path)
            running = svc.add_job(name="running-job", message="m", every_secs=60)

            other = CronService(base_dir=tmp_path)
            added = other.add_job(name="added-by-worker", message="m", every_secs=60)
            assert {j.id for j in svc._jobs} == {running.id}

            svc._executing.add(running.id)
            svc._job_run_meta[running.id] = (time.time(), "scheduled")
            ok = await svc.cancel(running.id)
            assert ok is True

        reloaded = CronService(base_dir=tmp_path)
        assert (
            reloaded.get_job(added.id) is not None
        ), "the concurrently-added job must survive the cancel persist"
        cancelled = reloaded.get_job(running.id)
        assert cancelled is not None and cancelled.last_status == "error"
        assert cancelled.last_error and "Cancelled" in cancelled.last_error

    @pytest.mark.asyncio
    async def test_terminal_merge_is_offloaded_and_keeps_loop_ticking(
        self, tmp_path: Path
    ) -> None:
        """The terminal merge runs OFF the event loop (asyncio.to_thread), so a
        contended store never parks the gateway loop.

        Mirrors ``TestMergeResultOffLoop``: hold the store lock from a separate
        fd so the merge would ``time.sleep``-spin if it ran on the loop, and
        assert a concurrent ticker keeps advancing.
        """
        svc = CronService(base_dir=tmp_path)
        svc._file_lock = _bounded_lock(svc)  # type: ignore[method-assign, assignment]
        job = svc.add_job(name="j", message="m", every_secs=60)

        loop = asyncio.get_running_loop()
        merged_off_loop = {"ok": False}
        real_merge = svc._merge_terminal_state_locked

        def spy_merge(job_id: str, **kwargs: object) -> None:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            merged_off_loop["ok"] = running is not loop
            real_merge(job_id, **kwargs)  # type: ignore[arg-type]

        svc._merge_terminal_state_locked = spy_merge  # type: ignore[method-assign, assignment]

        svc._dir.mkdir(parents=True, exist_ok=True)
        holder = (tmp_path / ".crons.lock").open("w")
        platform_compat.acquire_lock(holder.fileno(), exclusive=True)

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        try:
            svc._executing.add(job.id)
            svc._job_run_meta[job.id] = (time.time(), "scheduled")
            # The merge raises CronStoreBusy inside to_thread (store contended);
            # _force_reap swallows it (best-effort) and never parks the loop.
            await svc._force_reap(job.id, 5.0, 1)
            await tick_task
        finally:
            platform_compat.release_lock(holder.fileno())
            holder.close()

        assert merged_off_loop["ok"], "terminal merge must run off the event loop"
        assert ticks == 5, "the event loop must keep ticking while the merge waits"


def _bounded_lock(svc: CronService):  # type: ignore[no-untyped-def]
    """A ``_file_lock`` bound to a short timeout so contention tests fail fast."""
    real = type(svc)._file_lock

    def _fast(*, timeout: float = 0.3, poll: float = 0.02):  # type: ignore[no-untyped-def]
        return real(svc, timeout=0.3, poll=0.02)

    return _fast
