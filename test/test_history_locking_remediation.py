"""Regression tests for the Cluster-B ``history.py`` audit remediation.

Each test class reproduces one audited failure scenario:

1. Cross-process advisory ``flock`` covering append / read-rewrite so a writer
   in ANOTHER process (subagent, cron, CLI) can't silently lose updates.
2. ``_tab_id_index`` invalidated on append/metadata + removal of the permanent
   ``[]`` sentinel that hid sessions created after the first chained read.
3. Consolidation offset recomputed (clamped) when a rotation fired during the
   LLM await, so post-rotation messages aren't silently skipped.
4. Oversized files rotate even when they have ``<= _SESSION_KEEP_LINES`` lines
   (a few huge messages), instead of growing without bound.
5. ``delete_session`` uses ``unlink(missing_ok=True)`` and tolerates a
   concurrent removal instead of raising ``FileNotFoundError``.
6. One-line metadata rewrites do not ``fsync`` while holding the lock,
   shrinking the critical section every other writer contends on.

This file pins scenarios 1 to 5, the message-cache fill ordering against writers,
the on-loop persistence guard and its CI wiring, the off-loop helpers with their
async callers, and the Windows rename retry that only an offloaded metadata write
gets. Scenario 6, the lock's descriptor cleanup and ``append_if_absent`` are pinned
in ``test_history_locking_remediation_locked_section.py``; the dashboard slot save
and its foreign-line fold are pinned in
``test_history_locking_remediation_dashboard_save.py``.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.history import (
    _SESSION_KEEP_LINES,
    _SESSION_MAX_BYTES,
    ConversationLog,
)
from kiro_crew.testing.wait import async_wait_until


# ── Bug 1: cross-process advisory flock ──────────────────────────────────────
class TestCrossProcessLock:
    def test_locked_holds_exclusive_flock_cross_fd(self, tmp_path: Path) -> None:
        """While ``_locked`` is held, a SEPARATE fd on the sidecar lock file
        (standing in for another process) must NOT be able to grab the exclusive
        advisory lock. POSIX flock treats independent open file descriptions as
        competitors even within one process, so this faithfully models the
        cross-process contention the in-process ``threading.RLock`` cannot cover.
        """
        if not platform_compat.IS_POSIX:
            pytest.skip("advisory flock semantics are POSIX-specific")
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")
        lock_path = log._lock_path("k")

        with log._locked("k"):
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                got = platform_compat.try_acquire_lock(fd, exclusive=True)
                if got:
                    platform_compat.release_lock(fd)
                assert not got, "flock was not held across file descriptions"
            finally:
                os.close(fd)

        # Released on exit — now acquirable.
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            assert platform_compat.try_acquire_lock(fd, exclusive=True)
            platform_compat.release_lock(fd)
        finally:
            os.close(fd)

    def test_bounded_acquire_raises_and_never_writes_unlocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When another process holds the sidecar flock, a mutation must NOT
        block forever AND must NOT proceed with an unlocked write (a concurrent
        rewrite in the holder could clobber it → silent transcript loss).
        Instead it polls up to a bounded deadline, then raises
        ``HistoryLockTimeout`` and leaves the file untouched.
        """
        if not platform_compat.IS_POSIX:
            pytest.skip("advisory flock semantics are POSIX-specific")
        import kiro_crew.history as history_mod

        monkeypatch.setattr(history_mod, "_FLOCK_ACQUIRE_TIMEOUT_S", 0.2)
        monkeypatch.setattr(history_mod, "_FLOCK_POLL_INTERVAL_S", 0.02)
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")
        lock_path = log._lock_path("k")

        # Simulate another process holding the exclusive advisory lock.
        holder = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            assert platform_compat.try_acquire_lock(holder, exclusive=True)
            start = time.monotonic()
            with pytest.raises(history_mod.HistoryLockTimeout):
                log.append("k", "user", "under-contention")
            elapsed = time.monotonic() - start
            # Bounded (not unbounded), and it did fail rather than spin forever.
            assert elapsed < 2.0, f"append blocked {elapsed:.2f}s despite bounded acquire"
        finally:
            platform_compat.release_lock(holder)
            os.close(holder)

        # CRITICAL: the contended write must NOT have landed unlocked.
        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m["content"] for m in fresh._read_messages("k")]
        assert "under-contention" not in contents
        assert "seed" in contents

        # After the holder releases, a fresh mutation acquires normally.
        log.append("k", "user", "after-release")
        contents2 = [m["content"] for m in ConversationLog(base_dir=tmp_path)._read_messages("k")]
        assert "after-release" in contents2

    def test_on_loop_acquire_fails_fast_without_blocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a running asyncio loop the acquire must make exactly ONE
        non-blocking attempt and fail fast on contention — never sleep/poll,
        which would stall the sole event loop."""
        if not platform_compat.IS_POSIX:
            pytest.skip("advisory flock semantics are POSIX-specific")
        import asyncio as _asyncio

        import kiro_crew.history as history_mod

        # A long off-loop budget proves the loop path does NOT fall back to it,
        # and any _time.sleep on the loop path is a hard failure.
        monkeypatch.setattr(history_mod, "_FLOCK_ACQUIRE_TIMEOUT_S", 30.0)

        def _no_sleep_on_loop(_secs: float) -> None:
            raise AssertionError("on-loop acquire must never sleep/poll (blocks the event loop)")

        monkeypatch.setattr(history_mod._time, "sleep", _no_sleep_on_loop)
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")
        holder = os.open(str(log._lock_path("k")), os.O_RDWR | os.O_CREAT, 0o600)

        async def _run() -> float:
            assert platform_compat.try_acquire_lock(holder, exclusive=True)
            start = time.monotonic()
            # This test exercises the low-level on-loop acquire primitive
            # directly, so it deliberately bypasses the strict off-loop
            # discipline guard (which would otherwise raise OnLoopPersistError
            # before the acquire ran).
            with history_mod.allow_on_loop_persist():
                with pytest.raises(history_mod.HistoryLockTimeout):
                    # append runs synchronously on THIS loop thread.
                    log.append("k", "user", "under-contention")
            return time.monotonic() - start

        try:
            elapsed = _asyncio.run(_run())
            # Single non-blocking attempt: returns effectively immediately and
            # never used the long off-loop budget.
            assert elapsed < 1.0, f"on-loop acquire blocked ({elapsed:.2f}s)"
        finally:
            platform_compat.release_lock(holder)
            os.close(holder)

        # CRITICAL: the contended write must NOT have landed unlocked.
        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m["content"] for m in fresh._read_messages("k")]
        assert "under-contention" not in contents
        assert "seed" in contents

    def test_reentrant_same_thread_same_key(self, tmp_path: Path) -> None:
        """Nested ``_locked`` for the same key on the same thread must not
        self-deadlock (it reuses the already-held fd)."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")
        with log._locked("k"):
            with log._locked("k"):  # would hang forever without reentrancy
                log.mark_consolidated("k", 1)
        assert log.get_metadata("k")["last_consolidated"] == 1

    def test_no_lost_update_across_processes(self, tmp_path: Path) -> None:
        """A separate PROCESS hammering ``mark_consolidated`` (read-all +
        rewrite) against a live appender must not drop any appended message.

        Without the cross-process flock, the consolidator process reads N lines,
        the appender process appends line N+1, then the consolidator's
        ``os.replace`` writes back its stale N lines — silently losing N+1. The
        in-process ``threading.RLock`` cannot prevent this because the two
        actors live in different interpreters.
        """
        if not platform_compat.IS_POSIX:
            pytest.skip("cross-process flock semantics are POSIX-specific")
        ctx = multiprocessing.get_context("fork")
        _require_multiprocessing(ctx)
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")

        n = 200
        appender = ctx.Process(target=_mp_appender, args=(str(tmp_path), "k", n))
        consolidator = ctx.Process(target=_mp_consolidator, args=(str(tmp_path), "k", 400))
        consolidator.start()
        appender.start()
        appender.join(timeout=60)
        consolidator.join(timeout=60)
        assert appender.exitcode == 0
        assert consolidator.exitcode == 0

        fresh = ConversationLog(base_dir=tmp_path)
        contents = {m["content"] for m in fresh._read_messages("k")}
        missing = [f"a-{i}" for i in range(n) if f"a-{i}" not in contents]
        assert not missing, f"cross-process rewrite lost {len(missing)} appends"


class TestMessageCacheRewriteSerialization:
    """The cache FILL is ordered against writers, but a warm read is not, and
    an on-loop read never blocks on one."""

    @staticmethod
    def _hold(lock, acquired, release):  # type: ignore[no-untyped-def]
        """Hold *lock* from a foreign thread until *release* (bounded)."""

        def run() -> None:
            with lock:
                acquired.set()
                # Bounded even if the test body raises before releasing, so a
                # failure can never strand this thread holding the lock.
                release.wait(5)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert acquired.wait(5)
        return t

    def test_cache_fill_holds_the_writer_lock(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        lock = log._file_lock("k")
        log._invalidate_cache("k")

        seen: list[bool] = []
        real_locked = log._read_messages_locked

        def observed(
            key: str,
            gen: int | None = None,
            flock_witness: tuple[int, int] | None = None,
        ) -> list[dict]:
            # ``RLock`` exposes no owner query, and it is reentrant for THIS
            # thread — so a FOREIGN thread failing a non-blocking acquire is the
            # observable proof that the fill runs under the lock. Acquire and
            # release both happen in that thread (releasing an RLock from
            # another thread is an error).
            box: list[bool] = []

            def probe() -> None:
                got = lock.acquire(blocking=False)
                box.append(got)
                if got:
                    lock.release()

            t = threading.Thread(target=probe)
            t.start()
            t.join(5)
            seen.append(not box[0])
            return real_locked(key, gen=gen, flock_witness=flock_witness)

        log._read_messages_locked = observed  # type: ignore[method-assign]
        assert len(log._read_messages("k")) == 1
        assert seen == [True], "cache fill must run under the per-session writer lock"

    def test_warm_cache_hit_takes_no_lock(self, tmp_path: Path) -> None:
        """A hit must not even try the lock: it is served on the event loop
        while a writer can hold that RLock across a 10s flock wait."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        assert len(log._read_messages("k")) == 1  # warms the cache

        acquired, release = threading.Event(), threading.Event()
        t = self._hold(log._file_lock("k"), acquired, release)
        try:
            started = time.monotonic()
            assert len(log._read_messages("k")) == 1
            assert time.monotonic() - started < 1.0, "warm hit blocked on the writer lock"
        finally:
            release.set()
            t.join(5)

    def test_on_loop_miss_does_not_block_on_a_writer(self, tmp_path: Path) -> None:
        """A cold read reached ON the loop degrades to an unlocked fill rather
        than stalling behind a writer (LoopStallWatchdog hazard)."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log._invalidate_cache("k")

        acquired, release = threading.Event(), threading.Event()
        t = self._hold(log._file_lock("k"), acquired, release)
        try:

            async def on_loop() -> list[dict]:
                return log._read_messages("k")

            started = time.monotonic()
            assert len(asyncio.run(on_loop())) == 1
            assert time.monotonic() - started < 1.0, "on-loop fill blocked on the writer"
        finally:
            release.set()
            t.join(5)

    def test_unlocked_fill_publishes_under_unmoved_generation(self, tmp_path: Path) -> None:
        """An unlocked fill KEEPS its parse when no invalidation raced it.

        It runs exactly while a writer holds the lock, so its parse may predate
        a rewrite that restores the file's mtime. Two witnesses distinguish the
        cases: the invalidation generation (unmoved across the fill window ⇒
        no LOCAL preserved-mtime rewrite landed) and the cross-process
        flock-hold witness (this process held the flock throughout ⇒ no
        EXTERNAL writer could touch the file), so the holder here takes the
        FULL writer lock the way a real local writer does. Publishing the
        proven-valid parse spares the next reader a full re-parse. (The
        moved-generation and no-flock discards are pinned in
        test_history_cache_fill_race.py.)
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log._invalidate_cache("k")

        acquired, release = threading.Event(), threading.Event()

        def hold_writer_lock() -> None:
            with log._locked("k"):
                acquired.set()
                release.wait(5)

        t = threading.Thread(target=hold_writer_lock, daemon=True)
        t.start()
        assert acquired.wait(5)
        try:

            async def on_loop() -> list[dict]:
                return log._read_messages("k")

            assert len(asyncio.run(on_loop())) == 1, "the unlocked fill must still serve the read"
            assert log._msg_cache.get("k") is not None, (
                "an invalidation-free unlocked fill was discarded; the generation "
                "guard proves the parse valid, so dropping it re-pays the full "
                "re-parse on every contended on-loop read"
            )
        finally:
            release.set()
            t.join(5)
        # Once the writer is gone the next read is a warm hit on the kept fill.
        assert len(log._read_messages("k")) == 1
        assert log._msg_cache.get("k") is not None

    def test_off_loop_miss_waits_for_the_writer(self, tmp_path: Path) -> None:
        """Off the loop there is no watchdog to starve, so the fill waits and
        the race stays closed."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log._invalidate_cache("k")

        acquired, release = threading.Event(), threading.Event()
        entered_fill = threading.Event()
        held_box: list[bool] = []
        t = self._hold(log._file_lock("k"), acquired, release)

        def fill() -> None:
            with log._cache_fill_lock("k") as held:
                held_box.append(held)
                entered_fill.set()

        try:
            waiter = threading.Thread(target=fill, daemon=True)
            waiter.start()
            assert not entered_fill.wait(0.3), "off-loop fill did not wait for the writer"
            release.set()
            assert entered_fill.wait(5), "off-loop fill never acquired the released lock"
            waiter.join(5)
            assert held_box == [True]
        finally:
            release.set()
            t.join(5)

    def test_off_loop_miss_gives_up_rather_than_waiting_unbounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedged holder must not hang the reader: the off-loop acquire is
        capped at the writer's own ceiling and then fills unlocked."""
        import kiro_crew.history as history_mod

        monkeypatch.setattr(history_mod, "_FLOCK_ACQUIRE_TIMEOUT_S", 0.1)
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log._invalidate_cache("k")

        acquired, release = threading.Event(), threading.Event()
        t = self._hold(log._file_lock("k"), acquired, release)
        try:
            started = time.monotonic()
            with log._cache_fill_lock("k") as held:
                assert held is False, "reader claimed a lock the holder still owns"
            elapsed = time.monotonic() - started
            assert elapsed < 3.0, f"off-loop acquire was not bounded (waited {elapsed:.1f}s)"
            assert len(log._read_messages("k")) == 1
        finally:
            release.set()
            t.join(5)


# ── On-loop offload discipline: structurally enforced, not convention-only ────
class TestOnLoopPersistDiscipline:
    """The PR routes ~15 on-loop callers off the loop (append_off_loop /
    save_slot_off_loop / asyncio.to_thread) so ``_locked`` runs off-loop and
    takes the patient acquire path. Nothing structurally stopped a FUTURE
    on-loop call-site from calling a raw mutator, which works in every
    uncontended test yet silently drops the write under real contention
    (HistoryLockTimeout swallowed by best-effort callers). ``_locked`` now guards
    that: strict (pytest / dev-mode / env) RAISES ``OnLoopPersistError`` on ANY
    on-loop entry so the drift fails the suite; production logs it loudly and
    proceeds via the non-blocking safety-net acquire.
    """

    def test_raw_on_loop_mutator_raises_in_strict_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With strict enforcement on (``KIROCREW_STRICT_ON_LOOP_PERSIST=1``) a
        raw mutator call ON the event loop must raise ``OnLoopPersistError`` —
        the un-offloaded call-site fails the test instead of losing data in
        production."""
        import asyncio

        import kiro_crew.history as history_mod

        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "1")
        log = ConversationLog(base_dir=tmp_path)

        async def _run() -> None:
            # No offload: append enters _locked directly on this loop thread.
            with pytest.raises(history_mod.OnLoopPersistError):
                log.append("k", "user", "un-offloaded")

        asyncio.run(_run())
        # And the guard tripped BEFORE any write landed.
        fresh = ConversationLog(base_dir=tmp_path)
        assert "un-offloaded" not in [m["content"] for m in fresh._read_messages("k")]

    def test_metadata_mutator_on_loop_also_guarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard covers every ``_locked`` mutator, not just ``append`` —
        e.g. ``update_metadata`` / ``set_title`` / ``delete_session``."""
        import asyncio

        import kiro_crew.history as history_mod

        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "1")
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")  # off-loop seed: allowed

        async def _run() -> None:
            with pytest.raises(history_mod.OnLoopPersistError):
                log.set_title("k", "on-loop title")

        asyncio.run(_run())

    def test_dev_mode_also_enables_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KIROCREW_DEV_MODE`` (developer gateway) also turns the guard strict,
        and an explicit falsy ``KIROCREW_STRICT_ON_LOOP_PERSIST`` overrides it so
        the production-fallback path stays testable even under dev-mode."""
        import asyncio

        import kiro_crew.history as history_mod

        monkeypatch.delenv("KIROCREW_STRICT_ON_LOOP_PERSIST", raising=False)
        monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
        assert history_mod._on_loop_persist_strict() is True
        # Explicit falsy override wins over dev-mode.
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "0")
        assert history_mod._on_loop_persist_strict() is False

        log = ConversationLog(base_dir=tmp_path)
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "")
        monkeypatch.setenv("KIROCREW_DEV_MODE", "1")

        async def _run() -> None:
            with pytest.raises(history_mod.OnLoopPersistError):
                log.append("k", "user", "dev-on-loop")

        asyncio.run(_run())

    def test_allow_context_manager_suppresses_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``allow_on_loop_persist()`` is the sanctioned escape hatch for tests
        exercising the low-level primitive: even in strict mode an on-loop
        mutator inside it does NOT raise (it proceeds to the real acquire)."""
        import asyncio

        import kiro_crew.history as history_mod

        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "1")
        log = ConversationLog(base_dir=tmp_path)

        async def _run() -> None:
            with history_mod.allow_on_loop_persist():
                # Uncontended, so the single non-blocking acquire succeeds and
                # the write lands — no OnLoopPersistError.
                log.append("k", "user", "vetted-on-loop")

        asyncio.run(_run())
        fresh = ConversationLog(base_dir=tmp_path)
        assert "vetted-on-loop" in [m["content"] for m in fresh._read_messages("k")]

    def test_offloaded_call_does_not_trip_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The blessed offload helpers dispatch to a worker thread, so ``_locked``
        runs OFF the loop and the guard is a no-op — the write lands normally
        even under strict enforcement."""
        import asyncio

        from kiro_crew.history import append_off_loop

        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "1")
        log = ConversationLog(base_dir=tmp_path)

        async def _run() -> None:
            append_off_loop(log, "k", "assistant", "offloaded-ok")
            await async_wait_until(
                lambda: any(m.get("content") == "offloaded-ok" for m in log.read_messages("k")),
                describe=lambda: f"messages: {log.read_messages('k')}",
            )

        asyncio.run(_run())
        assert "offloaded-ok" in [m["content"] for m in log.read_messages("k")]

    def test_off_loop_call_never_trips_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain synchronous (off-loop) mutator — CLI / cron / subagent /
        worker thread — must never be flagged, even under strict enforcement."""
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "1")
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "off-loop-fine")  # no running loop
        assert "off-loop-fine" in [m["content"] for m in log.read_messages("k")]

    def test_production_mode_warns_and_proceeds_not_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With strict enforcement explicitly OFF (production gateway: no
        pytest/dev/env flag), an un-offloaded on-loop call must NOT raise —
        it degrades to a loud (throttled) warning + the non-blocking safety-net
        acquire, so shipping never introduces a new hard failure in the field."""
        import asyncio
        import logging

        import kiro_crew.history as history_mod

        # Force strict off (simulate a production gateway) and reset the warning
        # throttle so the diagnostic is observable.
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "0")
        monkeypatch.setattr(history_mod, "_on_loop_warn_last", 0.0)
        log = ConversationLog(base_dir=tmp_path)

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        history_mod.logger.addHandler(handler)

        async def _run() -> None:
            # Uncontended: the safety-net acquire succeeds, so this proceeds.
            log.append("k", "user", "prod-on-loop")

        try:
            asyncio.run(_run())
        finally:
            history_mod.logger.removeHandler(handler)

        # No raise, write landed, and the loud diagnostic fired.
        fresh = ConversationLog(base_dir=tmp_path)
        assert "prod-on-loop" in [m["content"] for m in fresh._read_messages("k")]
        assert any(
            "ON the event loop" in r.getMessage() for r in records
        ), "expected a loud on-loop diagnostic warning in production mode"

    def test_e2e_harness_enables_strict_mode_ci_enforcement(self) -> None:
        """CI-enforcement guard (arbiter BLOCK item 1): the on-loop persistence
        discipline must be an ENFORCED invariant, not a dev-only convention.

        Strict mode is deliberately OFF under bare unit pytest (the async harness
        legitimately drives mutators on the loop as a convenience). To catch a
        genuinely-new un-offloaded PRODUCTION call-site at PR time, the e2e gate
        — which spawns a REAL gateway subprocess and drives real chat turns —
        boots that gateway with ``KIROCREW_STRICT_ON_LOOP_PERSIST=1``, so any
        raw on-loop ``_locked`` entry raises ``OnLoopPersistError`` and fails CI
        instead of silently losing transcript data under production contention.

        This test pins that wiring: ``setup.py``'s ``E2eTestCommand`` must export
        the strict flag into the harness environment (``spawn_feature_gateway``
        inherits ``os.environ``). If someone removes it, the enforcement silently
        degrades back to a dev-only convention — so this fails deterministically
        in the fast unit CI, independent of e2e coverage.
        """
        import ast
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        setup_src = (repo_root / "setup.py").read_text(encoding="utf-8")
        tree = ast.parse(setup_src)

        # Find E2eTestCommand.run and confirm it assigns
        # env["KIROCREW_STRICT_ON_LOOP_PERSIST"] = "1" (a truthy string).
        found = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign)):
                continue
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "env"
                    and isinstance(tgt.slice, ast.Constant)
                    and tgt.slice.value == "KIROCREW_STRICT_ON_LOOP_PERSIST"
                    and isinstance(node.value, ast.Constant)
                    and str(node.value.value).strip().lower() in {"1", "true", "yes", "on"}
                ):
                    found = True
        assert found, (
            "setup.py test_e2e must export KIROCREW_STRICT_ON_LOOP_PERSIST=1 into "
            "the e2e gateway env — the on-loop persistence discipline is a "
            "CI-enforced invariant, not a dev-only convention (arbiter item 1). "
            "Do not remove this wiring without an equivalent CI enforcement."
        )


# ── Bug 2: tab_id index invalidation + no permanent [] sentinel ───────────────
class TestTabIdIndexInvalidation:
    def test_chain_picks_up_session_created_after_first_read(self, tmp_path: Path) -> None:
        """A session opened under an existing tab_id AFTER the chain was first
        read must be linked in. An append that does not invalidate the
        cached index makes the second session's messages vanish from
        ``recent_chained``.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard_chat-1-1", "user", "a1", tab_id="T")

        # First chained read builds + caches the tab_id index (T -> [A]).
        first = log.recent_chained("dashboard_chat-1-1", roles={"user"})
        assert [m["content"] for m in first] == ["a1"]

        # A second session joins tab_id T later. The append MUST invalidate the
        # cached index so the next chained read rebuilds and sees it.
        log.append("dashboard_chat-1-2", "user", "b1", tab_id="T")

        again = log.recent_chained("dashboard_chat-1-1", roles={"user"})
        assert [m["content"] for m in again] == ["a1", "b1"]

    def test_metadata_tab_id_change_invalidates_index(self, tmp_path: Path) -> None:
        """Setting a session's tab_id via update_metadata must invalidate the
        index so the session is picked into its chain on the next read."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard_chat-2-1", "user", "a1", tab_id="G")
        # Prime the index for tab_id G.
        assert [m["content"] for m in log.recent_chained("dashboard_chat-2-1")] == ["a1"]

        # Second session starts WITHOUT the tab_id, then gets linked via metadata.
        log.append("dashboard_chat-2-2", "user", "b1")
        log.update_metadata("dashboard_chat-2-2", {"tab_id": "G"})

        chained = log.recent_chained("dashboard_chat-2-1", roles={"user"})
        assert [m["content"] for m in chained] == ["a1", "b1"]

    def test_no_permanent_negative_sentinel(self, tmp_path: Path) -> None:
        """A chained read for a tab_id with no dashboard siblings must not poison
        the cache: a sibling created afterwards is still discovered (a cached
        ``[]`` sentinel would suppress every future rebuild)."""
        log = ConversationLog(base_dir=tmp_path)
        # slack-style key with a tab_id that the dashboard_chat-* glob misses,
        # so the first rebuild finds no entry for tab_id S.
        log.append("slack:123.456", "user", "s1", tab_id="S")
        first = log.recent_chained("slack:123.456", roles={"user"})
        assert [m["content"] for m in first] == ["s1"]  # single-file fallback

        # Now a dashboard session joins tab_id S. It must be discoverable.
        log.append("dashboard_chat-9-9", "user", "d1", tab_id="S")
        chained = log.recent_chained("dashboard_chat-9-9", roles={"user"})
        assert "d1" in [m["content"] for m in chained]


# ── Bug 3: recompute consolidation offset after rotation-during-await ─────────
class TestConsolidationOffsetAfterRotation:
    def test_offset_reset_when_file_shrank(self, tmp_path: Path) -> None:
        """The consolidator captures ``total`` before a (slow) LLM call. If a
        rotation truncates the file during that await, the stale absolute offset
        is in the pre-rotation numbering and is meaningless afterwards. Clamping
        it to the surviving count would mark the retained tail (which may hold
        brand-new, never-consolidated messages) as consolidated and skip them.
        mark_consolidated must instead reset to 0 and let the retained tail be
        reconsolidated — redoing a few messages is harmless; dropping any is not.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(10):
            log.append("k", "user", f"m{i}")
        total_before_llm = len(log._read_messages("k"))
        assert total_before_llm == 10

        # Rotation fires during the await: file keeps only its newest 3 messages.
        keep = log._read_messages("k")[-3:]
        log.rewrite_session("k", keep)
        assert len(log._read_messages("k")) == 3

        # Consolidator marks the STALE pre-LLM total (10 > surviving 3).
        log.mark_consolidated("k", total_before_llm)

        meta = log.get_metadata("k")
        # Reset to 0 rather than clamped to 3: the retained tail is reconsolidated
        # so nothing that survived rotation is silently marked consolidated.
        assert meta["last_consolidated"] == 0
        assert log.unconsolidated_count("k") == 3

    def test_new_message_in_retained_tail_not_skipped(self, tmp_path: Path) -> None:
        """GPT-flagged race: a NEW message arrives during the LLM await and
        rotation keeps it in the retained tail. Clamping the stale offset to the
        surviving count would mark that never-consolidated message as done and
        permanently drop it. The reset-to-0 path must keep it consolidatable.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(8):
            log.append("k", "user", f"m{i}")
        total_before_llm = len(log._read_messages("k"))  # 8 processed by the LLM

        # During the await: a brand-new message arrives, then rotation keeps the
        # newest 3 — which now INCLUDES that new, never-consolidated message.
        log.append("k", "user", "brand-new")
        keep = log._read_messages("k")[-3:]
        assert keep[-1]["content"] == "brand-new"
        log.rewrite_session("k", keep)

        log.mark_consolidated("k", total_before_llm)

        # The new message is still pending, not swallowed by a clamp.
        assert log.get_metadata("k")["last_consolidated"] == 0
        assert log.unconsolidated_count("k") == 3
        pending, _ = log.get_unconsolidated("k")
        assert any(m["content"] == "brand-new" for m in pending)

    def test_offset_unchanged_when_no_rotation(self, tmp_path: Path) -> None:
        """No rotation: offset is stored verbatim (no reset)."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(5):
            log.append("k", "user", f"m{i}")
        log.mark_consolidated("k", 3)
        assert log.get_metadata("k")["last_consolidated"] == 3
        assert log.unconsolidated_count("k") == 2

    def test_offset_equal_to_count_stored_verbatim(self, tmp_path: Path) -> None:
        """offset == current count (all consolidated, no rotation) is stored
        as-is; only a STRICTLY larger offset (file shrank) triggers the reset."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(4):
            log.append("k", "user", f"m{i}")
        log.mark_consolidated("k", 4)
        assert log.get_metadata("k")["last_consolidated"] == 4
        assert log.unconsolidated_count("k") == 0

    def test_offset_reset_when_rotation_retains_ge_offset(self, tmp_path: Path) -> None:
        """The count-only heuristic is INCOMPLETE: a rotation that RETAINS
        >= the snapshot offset leaves ``offset <= msg_count`` true, so the stale
        offset sails through and is written verbatim — but every surviving index
        shifted by the number of dropped lines, silently marking
        never-consolidated retained messages as done. The rotation GENERATION
        counter must force the reset regardless of retained count.

        Fails pre-fix: with only ``offset > msg_count`` the offset is <= the
        retained count and is stored verbatim (data-integrity failure).
        """
        log = ConversationLog(base_dir=tmp_path)
        # Bodies sized off the byte cap so 100 messages stay under it while
        # appending ~200 more blows it and triggers a REAL rotation whose
        # retained tail is still far larger than the offset=100 snapshot below.
        # The divisor keeps a full ``_SESSION_KEEP_LINES`` tail inside the cap;
        # deriving it rather than hardcoding a KiB figure is what stops a change
        # to the cap from silently leaving this test green without rotating.
        body = "x" * (_SESSION_MAX_BYTES // (_SESSION_KEEP_LINES + 50))
        for i in range(100):
            log.append("k", "user", f"{i}:{body}")
        # Consolidator snapshots BEFORE the slow LLM call.
        generation_at_snapshot = log.rotation_generation("k")
        total_at_snapshot = len(log._read_messages("k"))
        assert total_at_snapshot == 100
        assert generation_at_snapshot == 0

        # Slow LLM await: many more messages arrive and trigger a real rotation.
        for i in range(100, 300):
            log.append("k", "user", f"{i}:{body}")
        retained = len(log._read_messages("k"))
        # The rotation retained MORE messages than the snapshot offset — exactly
        # the case the count heuristic misses.
        assert retained >= total_at_snapshot
        assert log.rotation_generation("k") > generation_at_snapshot
        # last_consolidated is 0 after the rotation itself…
        assert log.get_metadata("k")["last_consolidated"] == 0

        # …the consolidator now writes back its stale pre-rotation offset with
        # the generation it snapshotted. The generation mismatch must force a
        # reset instead of applying the shifted index.
        log.mark_consolidated("k", total_at_snapshot, generation=generation_at_snapshot)
        assert log.get_metadata("k")["last_consolidated"] == 0
        assert log.unconsolidated_count("k") == retained

    def test_offset_stored_when_generation_matches(self, tmp_path: Path) -> None:
        """No rotation between snapshot and write (generation unchanged): the
        offset is stored verbatim even when supplied with a matching generation.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(6):
            log.append("k", "user", f"m{i}")
        gen = log.rotation_generation("k")
        log.mark_consolidated("k", 4, generation=gen)
        assert log.get_metadata("k")["last_consolidated"] == 4
        assert log.unconsolidated_count("k") == 2

    def test_legacy_metadata_absent_generation_reads_zero(self, tmp_path: Path) -> None:
        """Backward compatibility: a metadata line written before the
        ``rotation_generation`` field existed reads as generation 0, and a
        consolidator that snapshotted 0 stores its offset normally."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(5):
            log.append("k", "user", f"m{i}")
        # Simulate legacy metadata: strip the generation field entirely.
        path = log._path("k")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        import json as _json

        meta = _json.loads(lines[0])
        meta.pop("rotation_generation", None)
        lines[0] = _json.dumps(meta) + "\n"
        path.write_text("".join(lines), encoding="utf-8")
        log._invalidate_cache("k")

        assert log.rotation_generation("k") == 0
        log.mark_consolidated("k", 3, generation=0)
        assert log.get_metadata("k")["last_consolidated"] == 3

    def test_snapshot_for_consolidation_is_atomic_over_rotation(self, tmp_path: Path) -> None:
        """``snapshot_for_consolidation`` returns messages, total and generation
        from ONE consistent point, so the (offset, generation) pair it yields
        can never straddle a rotation.

        This is the fix for the two-separate-reads race: the OLD pairing
        (``get_unconsolidated`` then ``rotation_generation``) could read the
        offset PRE-rotation and the generation POST-rotation, so
        ``mark_consolidated`` saw a matching generation and applied the stale
        offset. We assert the snapshot's generation matches the file's actual
        generation for the same message set, and that feeding the snapshot back
        into ``mark_consolidated`` stores the offset verbatim (no phantom
        mismatch) when nothing rotated after it.

        Pre-fix analogue — reading the two values separately with a rotation
        wedged between them yields a PRE-rotation total paired with a
        POST-rotation generation; that stale offset is then applied because the
        generations (post==post) match. The snapshot closes that window.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(6):
            log.append("k", "user", f"m{i}")

        msgs, total, gen, _sub = log.snapshot_for_consolidation("k")
        assert total == 6
        assert len(msgs) == 6  # nothing consolidated yet
        assert gen == log.rotation_generation("k") == 0
        # The returned list must be an owned copy, not the shared cache object.
        assert msgs is not log._read_messages("k")

        # Feeding the atomically-captured (total, generation) back stores the
        # offset verbatim: the pair is internally consistent, so neither the
        # generation-mismatch nor the count-overflow guard trips.
        log.mark_consolidated("k", total, generation=gen)
        assert log.get_metadata("k")["last_consolidated"] == 6
        assert log.unconsolidated_count("k") == 0

    def test_snapshot_pairs_offset_and_generation_consistently(self, tmp_path: Path) -> None:
        """Demonstrates the exact race the snapshot fixes: the OLD two-read
        pairing can hand mark_consolidated a PRE-rotation offset with a
        POST-rotation generation, which is then applied because the stored
        generation also advanced — silently skipping retained messages. The
        atomic snapshot captures both from the SAME state so the generation it
        returns always matches the offset it returns.
        """
        # Sized off the byte cap for the same reason as
        # ``test_offset_reset_when_rotation_retains_ge_offset``: the race this
        # demonstrates only exists if the second batch really rotates.
        body = "x" * (_SESSION_MAX_BYTES // (_SESSION_KEEP_LINES + 50))
        log = ConversationLog(base_dir=tmp_path)
        for i in range(100):
            log.append("k", "user", f"{i}:{body}")

        # --- OLD PAIRING (simulated): read offset, then a rotation fires, then
        # read generation. This is what the pre-fix consolidator did. ---
        stale_offset = len(log._read_messages("k"))  # PRE-rotation total = 100
        for i in range(100, 300):
            log.append("k", "user", f"{i}:{body}")  # triggers a real rotation
        post_rotation_gen = log.rotation_generation("k")  # advanced
        assert post_rotation_gen > 0
        retained = len(log._read_messages("k"))
        assert retained >= stale_offset  # count fallback would miss it

        # Applying the stale offset with the POST-rotation generation: the
        # generations match (post==post), the offset <= retained count, so the
        # shifted, meaningless offset is stored verbatim — the data-loss bug.
        log.mark_consolidated("k", stale_offset, generation=post_rotation_gen)
        assert log.get_metadata("k")["last_consolidated"] == stale_offset

        # --- NEW: the atomic snapshot never produces that mismatched pair. Its
        # returned (total, generation) come from one locked read, so the
        # generation always corresponds to the messages/offset it returned. ---
        log2 = ConversationLog(base_dir=tmp_path / "other")
        for i in range(100):
            log2.append("k", "user", f"{i}:{body}")
        _, total_s, gen_s, _sub_s = log2.snapshot_for_consolidation("k")
        # The snapshot's generation matches the file state that produced total_s.
        assert gen_s == log2.rotation_generation("k")
        assert total_s == len(log2._read_messages("k"))


# ── Bug 4: rotate oversized files even with <= _SESSION_KEEP_LINES lines ──────
class TestRotateOversizedFewLines:
    def test_rotates_when_few_but_huge_lines(self, tmp_path: Path) -> None:
        """A session of a handful of very large messages exceeds the byte cap
        while having far fewer than ``_SESSION_KEEP_LINES`` lines. It must still
        rotate (drop oldest) rather than grow unbounded."""
        log = ConversationLog(base_dir=tmp_path)
        chunk = 300 * 1024  # 300 KiB per message
        count = (_SESSION_MAX_BYTES // chunk) + 4  # comfortably over the cap
        assert count <= _SESSION_KEEP_LINES  # the crux: few lines, big bytes
        big = "x" * chunk
        for i in range(count):
            log.append("k", "user", f"{big}{i}")

        path = log._path("k")
        assert path.stat().st_size <= _SESSION_MAX_BYTES, "file was never rotated"
        msgs = log._read_messages("k")
        assert 0 < len(msgs) < count, "no oldest messages were dropped"
        # Newest message survives rotation.
        assert msgs[-1]["content"].endswith(str(count - 1))

    def test_single_message_larger_than_budget_is_kept(self, tmp_path: Path) -> None:
        """A lone message bigger than the whole budget can't be split, so it is
        retained (rotation is a no-op) rather than discarded."""
        log = ConversationLog(base_dir=tmp_path)
        huge = "y" * (_SESSION_MAX_BYTES + 4096)
        log.append("k", "user", huge)
        msgs = log._read_messages("k")
        assert len(msgs) == 1
        assert msgs[0]["content"] == huge

    def test_small_files_unaffected(self, tmp_path: Path) -> None:
        """Ordinary small sessions never rotate."""
        log = ConversationLog(base_dir=tmp_path)
        for i in range(20):
            log.append("k", "user", f"m{i}")
        assert len(log._read_messages("k")) == 20

    def test_rotation_declines_when_archiving_the_dropped_lines_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Archiving is a PRECONDITION of the rewrite, not a side effect.

        The rewrite is what removes the leading lines, and until the archive
        write lands the transcript is their only copy — so an unwritable archive
        directory must leave the transcript intact rather than being logged and
        rewritten past. An oversized file is recoverable; a dropped row is not.
        """
        from kiro_crew import history as history_mod

        def _oversized(key: str) -> Path:
            log = ConversationLog(base_dir=tmp_path / key)
            chunk = _SESSION_MAX_BYTES // 4
            for i in range(6):  # over the cap, under the line cap
                log.append(key, "user", f"{i}" * chunk)
            return log

        real_archive = history_mod._archive_lines

        def _failing_archive(*_a: object, **_kw: object) -> None:
            raise OSError("archive directory is unwritable")

        monkeypatch.setattr(history_mod, "_archive_lines", _failing_archive)
        log_fail = _oversized("archive-fails")
        msgs = log_fail._read_messages("archive-fails")
        assert len(msgs) == 6, "rotation dropped rows it had nowhere to archive"
        assert msgs[0]["content"].startswith("0"), "the oldest row was deleted"

        # Positive control: with a working archive the SAME fixture does rotate,
        # so the decline above is attributable to the archive failure and not to
        # rotation having had nothing to do.
        monkeypatch.setattr(history_mod, "_archive_lines", real_archive)
        log_ok = _oversized("archive-succeeds")
        assert len(log_ok._read_messages("archive-succeeds")) < 6, (
            "the fixture does not rotate even with a healthy archive, so the "
            "decline above proves nothing"
        )


# ── Bug 5: delete_session unlink(missing_ok=True) ─────────────────────────────
class TestDeleteSessionMissingOk:
    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        assert log.delete_session("never-existed") is False

    def test_toctou_removal_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduce the window where the file is reported present by exists()
        but has been unlinked by another process before our own unlink. With
        ``missing_ok=True`` this is tolerated; the old bare ``unlink()`` raised
        ``FileNotFoundError``.
        """
        log = ConversationLog(base_dir=tmp_path)
        # Force the existence check to report True even though no file exists.
        monkeypatch.setattr("kiro_crew.history.Path.exists", lambda self: True)
        # Must not raise.
        log.delete_session("ghost")

    def test_delete_existing_returns_true(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "m")
        assert log.delete_session("k") is True
        assert not log._path("k").exists()

    def test_delete_preserves_sidecar_lock_inode(self, tmp_path: Path) -> None:
        """A deleted session MUST keep its ``.jsonl.lock`` sidecar. The sidecar
        inode is the cross-process mutex; unlinking it re-opens the lock-inode
        race (a recreating writer holds the old inode while a later acquirer
        creates a fresh sidecar inode, so two processes "hold the lock" on
        different inodes and can clobber each other). The bounded zero-byte
        orphan is the deliberate lesser evil.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "m")  # first mutation creates the sidecar
        lock_path = log._lock_path("k")
        assert lock_path.exists()
        assert log.delete_session("k") is True
        assert lock_path.exists(), "sidecar lock inode must be preserved across delete"

    def test_delete_fails_closed_under_contention(self, tmp_path: Path) -> None:
        """delete_session now runs under ``_locked``. If the cross-process lock
        cannot be acquired (a wedged holder), it must report "not removed"
        rather than unlink unlocked — the very clobber the lock prevents. Held
        from a separate fd to model another process holding the lock.
        """
        if not platform_compat.IS_POSIX:
            pytest.skip("advisory flock semantics are POSIX-specific")
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "m")
        # Shrink the acquire budget so the test doesn't wait the full deadline.
        monkeypatch_budget = 0.2
        import kiro_crew.history as history_mod

        orig = history_mod._FLOCK_ACQUIRE_TIMEOUT_S
        history_mod._FLOCK_ACQUIRE_TIMEOUT_S = monkeypatch_budget
        lock_path = log._lock_path("k")
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            assert platform_compat.try_acquire_lock(fd, exclusive=True)
            assert log.delete_session("k") is False
            assert log._path("k").exists(), "file was deleted despite contention"
        finally:
            platform_compat.release_lock(fd)
            os.close(fd)
            history_mod._FLOCK_ACQUIRE_TIMEOUT_S = orig


# ── append_off_loop: on-loop offload / off-loop inline persistence ────────────
class TestAppendOffLoop:
    def test_inline_append_when_no_running_loop(self, tmp_path: Path) -> None:
        """Off the event loop, ``append_off_loop`` persists synchronously."""
        from kiro_crew.history import append_off_loop

        log = ConversationLog(base_dir=tmp_path)
        append_off_loop(log, "k", "assistant", "hello", agent="bot")
        msgs = log.read_messages("k")
        assert any(m.get("content") == "hello" for m in msgs)

    def test_on_loop_offloads_to_thread_and_persists(self, tmp_path: Path) -> None:
        """On a running loop the append is dispatched to a worker thread (never
        run inline on the loop) and still lands. We prove the offload by
        capturing the thread the append executes on.
        """
        import asyncio
        import threading

        from kiro_crew.history import append_off_loop

        log = ConversationLog(base_dir=tmp_path)
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        real_append = log.append

        def _spy(*args: object, **kwargs: object) -> None:
            seen["thread"] = threading.get_ident()
            real_append(*args, **kwargs)  # type: ignore[arg-type]

        log.append = _spy  # type: ignore[method-assign]

        async def _run() -> None:
            append_off_loop(log, "k", "assistant", "hi")
            await async_wait_until(
                lambda: "thread" in seen,
                describe=lambda: f"seen={seen}",
            )

        asyncio.run(_run())
        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, "append ran on the event loop thread"
        assert any(m.get("content") == "hi" for m in log.read_messages("k"))


# ── update_metadata_off_loop: keep flock/os.close off the event loop ──────────
class TestUpdateMetadataOffLoop:
    """The cross-process flock acquire + ``os.close`` inside ``_locked`` are
    ``blocking: true`` under the no-blocking-call-on-event-loop rule: a wedged
    peer can stall them and freeze chat/WS/heartbeat. ``update_metadata`` (and
    ``set_title``/``delete_session``) enter ``_locked``, so synchronous on-loop
    callers must offload. ``update_metadata_off_loop`` is the sync-context
    escape hatch (mirrors ``append_off_loop``)."""

    def test_inline_update_when_no_running_loop(self, tmp_path: Path) -> None:
        """Off the event loop the update persists synchronously."""
        from kiro_crew.history import update_metadata_off_loop

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")
        update_metadata_off_loop(log, "k", {"title": "hi", "agent": "bot"})
        meta = ConversationLog(base_dir=tmp_path).get_metadata("k")
        assert meta["title"] == "hi"
        assert meta["agent"] == "bot"

    def test_on_loop_offloads_flock_ops_to_thread(self, tmp_path: Path) -> None:
        """On a running loop the flock/os.close-bearing update_metadata must run
        on a worker thread, NEVER inline on the event-loop thread, and still
        persist. We prove the offload by capturing the executing thread id."""
        import asyncio
        import threading

        from kiro_crew.history import update_metadata_off_loop

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        real_update = log.update_metadata

        def _spy(*args: object, **kwargs: object) -> None:
            seen["thread"] = threading.get_ident()
            real_update(*args, **kwargs)  # type: ignore[arg-type]

        log.update_metadata = _spy  # type: ignore[method-assign]

        async def _run() -> None:
            update_metadata_off_loop(log, "k", {"title": "async-title"})
            await async_wait_until(
                lambda: "thread" in seen,
                describe=lambda: f"seen={seen}",
            )

        asyncio.run(_run())
        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, "update_metadata ran on the event loop thread"
        assert ConversationLog(base_dir=tmp_path).get_metadata("k")["title"] == "async-title"


# ── async handler callers offload _locked ops off the event loop ─────────────
class TestOnLoopCallersOffload:
    """The audited async-path callers (``_persist_title`` behind auto-title /
    manual-title handlers, ``api_session_delete``) enter ``_locked`` via
    ``update_metadata_if`` / ``delete_session``. Running that on the event-loop
    thread lets a wedged cross-process peer freeze chat/WS/heartbeat. These
    wiring tests lock in that the ``_locked`` work is dispatched off the loop."""

    def test_persist_title_runs_update_metadata_off_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import threading
        from unittest.mock import MagicMock

        from kiro_crew.dashboard import chat_title

        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:t", "user", "seed")

        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        real_update_metadata = log.update_metadata_if

        def _spy(*args: object, **kwargs: object) -> bool:
            seen["thread"] = threading.get_ident()
            return real_update_metadata(*args, **kwargs)  # type: ignore[arg-type]

        log.update_metadata_if = _spy  # type: ignore[method-assign]
        monkeypatch.setattr(chat_title, "slot_history_key", lambda _slot: "dashboard:t")

        state = MagicMock()
        state.conversation_log = log
        slot = MagicMock()
        slot.key = "t"
        slot.title = "My Title"
        slot._title_origin = "auto"
        slot._title_refresh_mark = 8
        slot._title_low_signal = False
        slot._title_epoch = 0

        asyncio.run(chat_title._persist_title(state, slot))

        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, "update_metadata ran on the event loop thread"
        persisted = ConversationLog(base_dir=tmp_path).get_metadata("dashboard:t")
        assert persisted["title"] == "My Title"
        # Provenance + consumed refresh milestone are persisted alongside the
        # title so "a rename is final" and the refresh token budget survive a
        # reload.
        assert persisted["title_origin"] == "auto"
        assert persisted["title_refresh_mark"] == 8
        assert persisted["title_low_signal"] is False

    def test_api_session_delete_runs_delete_off_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import threading
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.handlers import sessions as sessions_mod

        log = ConversationLog(base_dir=tmp_path)
        log.append("gone", "user", "seed")

        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        real_delete = log.delete_session

        def _spy(*args: object, **kwargs: object) -> bool:
            seen["thread"] = threading.get_ident()
            return real_delete(*args, **kwargs)  # type: ignore[arg-type]

        log.delete_session = _spy  # type: ignore[method-assign]

        state = MagicMock()
        state.conversation_log = log
        state._slots = {}
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        monkeypatch.setattr(sessions_mod, "_remove_slot_for_history_key", AsyncMock())

        request = MagicMock()
        request.get = {}.get  # the dashboard user: no app claim
        request.app = {"state": state}
        request.match_info = {"key": "gone"}

        resp = asyncio.run(sessions_mod.api_session_delete(request))

        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, "delete_session ran on the event loop thread"
        assert b'"ok": true' in resp.body
        assert not log._path("gone").exists()


class TestMetadataRenameRetryNeedsTheOffload:
    """The offload is what earns a mutation the Windows rename retry.

    A one-line metadata rewrite publishes through ``replace_with_retry``. On
    Windows that rename is refused with ``PermissionError`` while any other
    handle is open on the destination -- a concurrent transcript reader, an
    indexer, an AV scanner. ``replace_with_retry`` absorbs that window, but only
    OFF the event loop: on the loop it makes exactly ONE attempt and re-raises
    rather than sleeping the sole loop (``no-blocking-call-on-event-loop``).

    So an async caller that invokes a sync mutator directly gets no retry at all,
    and a single transient reader is enough to fail its write on Windows. Every
    production caller offloads; an async TEST that mutates as a setup convenience
    has to do the same, or it inherits a Windows-only flake that no Linux run
    reproduces. These tests pin both halves of that asymmetry.
    """

    KEY = "dashboard:resume-private"

    def _seeded(self, tmp_path: Path) -> ConversationLog:
        """A transcript with a metadata line, created off the loop."""
        log = ConversationLog(base_dir=tmp_path)
        log.append(self.KEY, "user", "seed")
        return log

    @staticmethod
    def _windows_without_strict(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        # Pin the discipline check to warn-and-proceed so these tests measure the
        # rename, not an ambient strict-mode raise from the surrounding harness.
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "0")

    def test_off_loop_metadata_write_retries_past_one_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from windows_sim import replace_sharing_violation

        self._windows_without_strict(monkeypatch)
        log = self._seeded(tmp_path)
        destination = log._path(self.KEY).name

        async def _body() -> dict:
            with replace_sharing_violation(match=destination, times=1) as seen:
                await asyncio.to_thread(log.update_metadata, self.KEY, {"agent": "writer"})
            return seen

        seen = asyncio.run(_body())
        assert seen["n"] >= 2, (
            f"the rename was not retried past the violation (attempts={seen['n']}); "
            f"one attempt means the write did not take the off-loop path"
        )
        assert (
            log.get_metadata(self.KEY).get("agent") == "writer"
        ), "the retried rename did not publish the metadata"

    def test_on_loop_metadata_write_is_refused_by_the_same_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: identical contention, no offload, no retry.

        This is the asymmetry the offload exists for. If this ever stops raising,
        the on-loop branch of ``replace_with_retry`` changed and the reason an
        async caller must offload needs re-deriving.
        """
        from windows_sim import replace_sharing_violation

        self._windows_without_strict(monkeypatch)
        log = self._seeded(tmp_path)
        destination = log._path(self.KEY).name

        async def _body() -> None:
            with replace_sharing_violation(match=destination, times=1):
                log.update_metadata(self.KEY, {"agent": "writer"})

        with pytest.raises(PermissionError):
            asyncio.run(_body())

    def test_the_private_resume_test_offloads_its_transcript_setup(self) -> None:
        """Pin the async caller whose un-offloaded setup flaked on Windows.

        A direct ``state.conversation_log.<mutator>(...)`` in an async test body
        runs on the loop and so opts out of the rename retry the two tests above
        measure. The control on the offloaded form keeps an empty offender list
        meaning "converted" rather than "the calls were deleted".
        """
        import ast

        target = "test_http_resume_cannot_authorize_private_transcript"
        mutators = {
            "append",
            "append_if_absent",
            "delete_session",
            "save_slot",
            "set_title",
            "update_metadata",
        }
        source = (Path(__file__).resolve().parent / "test_member_memory_runtime.py").read_text(
            encoding="utf-8"
        )
        node = next(
            (
                n
                for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == target
            ),
            None,
        )
        assert node is not None, (
            f"{target} is gone from test_member_memory_runtime.py; move this guard "
            f"to whatever replaced it rather than deleting it"
        )

        calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
        direct = sorted(
            c.lineno
            for c in calls
            if isinstance(c.func, ast.Attribute)
            and c.func.attr in mutators
            and "conversation_log" in ast.unparse(c.func)
        )
        offloaded = [
            c
            for c in calls
            if "to_thread" in ast.unparse(c.func)
            and any("conversation_log" in ast.unparse(a) for a in c.args)
        ]

        assert direct == [], (
            f"{target} mutates the conversation log ON the event loop at line(s) "
            f"{direct}: that write gets a single rename attempt, so one transient "
            f"reader handle fails it on Windows. Wrap it in asyncio.to_thread."
        )
        assert len(offloaded) >= 2, (
            f"expected {target} to still drive its transcript setup through "
            f"asyncio.to_thread; found {len(offloaded)} offloaded mutation(s), so the "
            f"empty on-loop list above may just mean the setup was removed"
        )


# ── Multiprocessing workers (module-level so they are picklable under fork) ───
def _require_multiprocessing(ctx: "multiprocessing.context.BaseContext") -> None:
    """Skip the test when the sandbox forbids the semaphores multiprocessing
    needs (some CI/sandbox environments deny ``SemLock`` creation with
    ``PermissionError``). The flock primitive is still covered by the
    deterministic single-process ``test_locked_holds_exclusive_flock_cross_fd``.
    """
    try:
        probe = ctx.Process(target=_mp_noop)
        probe.start()
        probe.join(timeout=10)
    except (PermissionError, OSError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"multiprocessing unavailable in this environment: {exc}")


def _mp_noop() -> None:
    pass


def _mp_appender(dir_str: str, key: str, n: int) -> None:
    log = ConversationLog(base_dir=Path(dir_str))
    for i in range(n):
        log.append(key, "user", f"a-{i}")
        time.sleep(0.001)  # flake-ok: deliberate write-pacing in a subprocess stress helper


def _mp_consolidator(dir_str: str, key: str, iters: int) -> None:
    log = ConversationLog(base_dir=Path(dir_str))
    for _ in range(iters):
        log.mark_consolidated(key, 1)
        time.sleep(0.0005)  # flake-ok: deliberate write-pacing in a subprocess stress helper
