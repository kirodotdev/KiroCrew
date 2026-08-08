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
6. One-line metadata rewrites no longer ``fsync`` while holding the lock,
   shrinking the critical section every other writer contends on.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.history import (
    _SESSION_KEEP_LINES,
    _SESSION_MAX_BYTES,
    ConversationLog,
)


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
            raise AssertionError(
                "on-loop acquire must never sleep/poll (blocks the event loop)"
            )

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
        consolidator = ctx.Process(
            target=_mp_consolidator, args=(str(tmp_path), "k", 400)
        )
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
        assert "un-offloaded" not in [
            m["content"] for m in fresh._read_messages("k")
        ]

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
        assert "vetted-on-loop" in [
            m["content"] for m in fresh._read_messages("k")
        ]

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
            for _ in range(50):
                if any(
                    m.get("content") == "offloaded-ok"
                    for m in log.read_messages("k")
                ):
                    return
                await asyncio.sleep(0.02)

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
        assert "off-loop-fine" in [
            m["content"] for m in log.read_messages("k")
        ]

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
        assert "prod-on-loop" in [
            m["content"] for m in fresh._read_messages("k")
        ]
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
        from pathlib import Path as _P

        repo_root = _P(__file__).resolve().parents[1]
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
    def test_chain_picks_up_session_created_after_first_read(
        self, tmp_path: Path
    ) -> None:
        """A session opened under an existing tab_id AFTER the chain was first
        read must be linked in. Previously the append didn't invalidate the
        cached index, so the second session's messages vanished from
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
        assert [m["content"] for m in log.recent_chained("dashboard_chat-2-1")] == [
            "a1"
        ]

        # Second session starts WITHOUT the tab_id, then gets linked via metadata.
        log.append("dashboard_chat-2-2", "user", "b1")
        log.update_metadata("dashboard_chat-2-2", {"tab_id": "G"})

        chained = log.recent_chained("dashboard_chat-2-1", roles={"user"})
        assert [m["content"] for m in chained] == ["a1", "b1"]

    def test_no_permanent_negative_sentinel(self, tmp_path: Path) -> None:
        """A chained read for a tab_id with no dashboard siblings must not poison
        the cache: a sibling created afterwards is still discovered (the removed
        ``[]`` sentinel used to suppress every future rebuild)."""
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
        # ~11 KiB bodies: 100 messages stay under the 2 MiB cap, but appending
        # ~200 more blows it and triggers a REAL rotation whose retained tail is
        # still far larger than the offset=100 snapshot below.
        body = "x" * (11 * 1024)
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
        log.mark_consolidated(
            "k", total_at_snapshot, generation=generation_at_snapshot
        )
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

    def test_snapshot_for_consolidation_is_atomic_over_rotation(
        self, tmp_path: Path
    ) -> None:
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

        msgs, total, gen = log.snapshot_for_consolidation("k")
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

    def test_snapshot_pairs_offset_and_generation_consistently(
        self, tmp_path: Path
    ) -> None:
        """Demonstrates the exact race the snapshot fixes: the OLD two-read
        pairing can hand mark_consolidated a PRE-rotation offset with a
        POST-rotation generation, which is then applied because the stored
        generation also advanced — silently skipping retained messages. The
        atomic snapshot captures both from the SAME state so the generation it
        returns always matches the offset it returns.
        """
        body = "x" * (11 * 1024)
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
        _, total_s, gen_s = log2.snapshot_for_consolidation("k")
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
            # Let the executor task finish.
            for _ in range(50):
                if "thread" in seen:
                    break
                await asyncio.sleep(0.02)

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
            for _ in range(50):
                if "thread" in seen:
                    break
                await asyncio.sleep(0.02)

        asyncio.run(_run())
        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, (
            "update_metadata ran on the event loop thread"
        )
        assert (
            ConversationLog(base_dir=tmp_path).get_metadata("k")["title"]
            == "async-title"
        )


# ── async handler callers offload _locked ops off the event loop ─────────────
class TestOnLoopCallersOffload:
    """The audited async-path callers (``_persist_title`` behind auto-title /
    manual-title handlers, ``api_session_delete``) enter ``_locked`` via
    ``set_title`` / ``delete_session``. Running that on the event-loop thread
    lets a wedged cross-process peer freeze chat/WS/heartbeat. These wiring
    tests lock in that the ``_locked`` work is dispatched off the loop."""

    def test_persist_title_runs_set_title_off_loop(
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
        real_set_title = log.set_title

        def _spy(*args: object, **kwargs: object) -> None:
            seen["thread"] = threading.get_ident()
            real_set_title(*args, **kwargs)  # type: ignore[arg-type]

        log.set_title = _spy  # type: ignore[method-assign]
        monkeypatch.setattr(
            chat_title, "slot_history_key", lambda _slot: "dashboard:t"
        )

        state = MagicMock()
        state.conversation_log = log
        slot = MagicMock()
        slot.key = "t"
        slot.title = "My Title"

        asyncio.run(chat_title._persist_title(state, slot))

        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, "set_title ran on the event loop thread"
        assert (
            ConversationLog(base_dir=tmp_path).get_metadata("dashboard:t")["title"]
            == "My Title"
        )

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
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        monkeypatch.setattr(
            sessions_mod, "_remove_slot_for_history_key", AsyncMock()
        )

        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"key": "gone"}

        resp = asyncio.run(sessions_mod.api_session_delete(request))

        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, (
            "delete_session ran on the event loop thread"
        )
        assert b'"ok": true' in resp.body
        assert not log._path("gone").exists()


# ── Bug 6: reduced lock hold — no fsync under lock for one-line metadata ──────
class TestReducedLockHold:
    def test_update_metadata_does_not_fsync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A one-line metadata edit must not ``fsync`` while holding the
        cross-process lock — the flush dominated the critical section every
        other writer blocked on. The edit must still persist (os.replace is
        crash-atomic without a flush)."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "m")

        calls: list[int] = []
        monkeypatch.setattr("os.fsync", lambda fd: calls.append(fd))

        log.update_metadata("k", {"title": "hello", "agent": "bob"})

        assert calls == [], "metadata rewrite fsync'd while holding the lock"
        fresh = ConversationLog(base_dir=tmp_path)
        meta = fresh.get_metadata("k")
        assert meta["title"] == "hello"
        assert meta["agent"] == "bob"

    def test_mark_consolidated_does_not_fsync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mark_consolidated is likewise a one-line metadata rewrite and must
        not fsync under the lock, while still persisting the offset and every
        message."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "m1")
        log.append("k", "assistant", "m2")

        calls: list[int] = []
        monkeypatch.setattr("os.fsync", lambda fd: calls.append(fd))

        log.mark_consolidated("k", 2)

        assert calls == [], "mark_consolidated fsync'd while holding the lock"
        fresh = ConversationLog(base_dir=tmp_path)
        assert fresh.get_metadata("k")["last_consolidated"] == 2
        assert [m["content"] for m in fresh._read_messages("k")] == ["m1", "m2"]


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
        time.sleep(0.001)


def _mp_consolidator(dir_str: str, key: str, iters: int) -> None:
    log = ConversationLog(base_dir=Path(dir_str))
    for _ in range(iters):
        log.mark_consolidated(key, 1)
        time.sleep(0.0005)


# ── Bug 7: dashboard persistence save must hold _locked vs. append_off_loop ──
class TestDashboardSaveHoldsLock:
    """``_save_slot_to_history`` (dashboard persistence) does a
    read-modify-``atomic_write`` of the session JSONL. If it does NOT hold the
    same per-session ``_locked`` as :meth:`ConversationLog.append`, a concurrent
    ``append_off_loop`` — e.g. a workflow result appended to the originating
    dashboard session — can land AFTER the save snapshots the file but BEFORE it
    replaces the file, so the ``atomic_write`` silently deletes the just-appended
    (already acknowledged) message.

    This reproduces that interleaving deterministically: a competitor thread
    appends a message during the save's ``atomic_write``. With the lock the
    append serializes behind the save and survives; without it the save's
    file-replace clobbers the append.
    """

    def _make_state(self, tmp_path: Path):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.state import DashboardState

        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    def test_concurrent_append_off_loop_survives_save(self, tmp_path, monkeypatch):
        import threading

        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("locktest")
        slot.append("user", "m1")
        slot.append("assistant", "m2")
        slot.drain()
        history_key = _history_key_for(slot.key)

        # Seed the on-disk transcript with the current window.
        _save_slot_to_history(state, slot, force=True)

        # A workflow-result durable copy (exactly what inject_workflow_result
        # does via append_off_loop) appended while the NEXT save is mid-write.
        appended = "workflow-result-m3"
        go = threading.Event()
        done = threading.Event()

        def _competitor() -> None:
            # Off the event loop → append takes the patient _locked acquire path.
            go.wait(5)
            state.conversation_log.append(history_key, "assistant", appended)
            done.set()

        competitor = threading.Thread(target=_competitor)
        competitor.start()

        real_atomic_write = chat_persistence.atomic_write

        def _slow_atomic_write(path, data, **kwargs):
            # We are now INSIDE the save's critical section. If the save holds
            # _locked, the competitor's append blocks here until the save's
            # with-block exits; if it does NOT, the append lands during this
            # sleep and the real write below clobbers it.
            go.set()
            time.sleep(0.5)
            return real_atomic_write(path, data, **kwargs)

        monkeypatch.setattr(chat_persistence, "atomic_write", _slow_atomic_write)

        # Window is unchanged ([m1, m2]) so a naive save re-writes only those two
        # lines — the appended m3 exists ONLY on disk and is the loss candidate.
        _save_slot_to_history(state, slot, force=True)

        assert done.wait(10), "competitor append never completed (deadlock?)"
        competitor.join(10)

        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m.get("content") for m in fresh._read_messages(history_key)]
        assert appended in contents, (
            "dashboard save clobbered a concurrent append_off_loop message "
            f"(got {contents!r}) — _save_slot_to_history must hold _locked"
        )
        assert contents.count("m1") == 1
        assert contents.count("m2") == 1

    def test_cross_process_append_before_save_survives(self, tmp_path, monkeypatch):
        """GPT 5.6 HIGH (chat_persistence.py:793): the ``window`` snapshot is
        captured BEFORE ``_save_slot_to_history`` takes ``_locked``. A writer in
        ANOTHER process can fully append + release the lock in that gap; the save
        then acquires the lock and rewrites ``meta + frozen + window`` from the
        stale snapshot, permanently deleting the acknowledged append.

        This reproduces it WITHOUT threads: the cross-process append is committed
        to the file first, then the slot (whose in-memory window never saw it) is
        saved. The save must merge the on-disk append rather than clobber it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("staleoverwrite")
        slot.append("user", "m1")
        slot.append("assistant", "m2")
        slot.drain()
        history_key = _history_key_for(slot.key)

        # Seed the on-disk transcript with the current window ([m1, m2]).
        _save_slot_to_history(state, slot, force=True)

        # A DIFFERENT process appends an acknowledged message straight to the
        # file (append holds the same cross-process ``_locked``). This slot's
        # in-memory window never learns about it (no ``slot.append`` here), so it
        # is exactly the "landed between snapshot and lock" loss candidate.
        state.conversation_log.append(history_key, "assistant", "cross-proc-m3")

        # Next slot save: window is still [m1, m2]; a bare meta+frozen+window
        # replace would drop cross-proc-m3.
        _save_slot_to_history(state, slot, force=True)

        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m.get("content") for m in fresh._read_messages(history_key)]
        assert "cross-proc-m3" in contents, (
            "slot save deleted a cross-process append that landed between the "
            f"window snapshot and the file replace (got {contents!r})"
        )
        assert contents.count("m1") == 1
        assert contents.count("m2") == 1
        assert contents.count("cross-proc-m3") == 1

    def test_repeated_save_after_foreign_append_keeps_it(self, tmp_path, monkeypatch):
        """GPT 5.6 HIGH (chat_persistence.py:746): the frozen-prefix fast path.

        After a save preserves a cross-process append, it records the resulting
        file's ``(mtime, size, disk_older)`` in ``slot._frozen_prefix_cache``.
        The very NEXT save (window and disk both unchanged) then matches that
        cache and takes the O(window) fast path. If the fast path returns EMPTY
        foreign lines, the rebuilt ``meta + frozen + window`` payload drops the
        previously-preserved append — a cron/workflow result followed by two
        dashboard saves silently loses the transcript line.

        Reproduces the sequence save -> foreign-append -> save -> save -> save
        with NO writer between the saves, so saves 2..4 all hit the fast path,
        and asserts the foreign line survives every one exactly once.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("fastpathloss")
        slot.append("user", "m1")
        slot.append("assistant", "m2")
        slot.drain()
        history_key = _history_key_for(slot.key)

        # Seed the on-disk transcript with the current window ([m1, m2]).
        _save_slot_to_history(state, slot, force=True)

        # A cron/workflow result lands via the cross-process append path; the
        # slot's in-memory window never learns about it.
        state.conversation_log.append(history_key, "assistant", "cron-m3")

        # Save #1: slow path (disk changed since our seed write) detects and
        # preserves cron-m3, then caches the resulting file metadata + the
        # preserved foreign line.
        _save_slot_to_history(state, slot, force=True)

        # Saves #2, #3, #4: disk is byte-identical to save #1's output and the
        # window is still [m1, m2], so each hits the frozen-prefix fast path.
        # Before the fix these dropped cron-m3; now the cached foreign line is
        # re-emitted verbatim on every fast-path save.
        for _ in range(3):
            _save_slot_to_history(state, slot, force=True)

        fresh = ConversationLog(base_dir=tmp_path)
        contents = [m.get("content") for m in fresh._read_messages(history_key)]
        assert "cron-m3" in contents, (
            "fast-path save dropped a previously-preserved cross-process append "
            f"(got {contents!r}) — cached foreign lines must be re-emitted"
        )
        assert contents.count("cron-m3") == 1, (
            f"cron-m3 duplicated across repeated fast-path saves (got {contents!r})"
        )
        assert contents.count("m1") == 1
        assert contents.count("m2") == 1

    def test_slot_save_preserves_rotation_generation(self, tmp_path, monkeypatch):
        """GPT 5.6 HIGH (chat_persistence.py:793, second half): the rewritten
        metadata reconstructed a subset and omitted ``rotation_generation``, so a
        slot save AFTER a rotation reset the generation to 0 — letting a
        concurrent consolidation apply a stale offset and mark never-consolidated
        retained messages as done (undoing the rotation-generation fix).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("rotgen")
        slot.append("user", "hello")
        slot.drain()
        history_key = _history_key_for(slot.key)
        _save_slot_to_history(state, slot, force=True)

        # Simulate a rotation having bumped the generation counter in metadata.
        state.conversation_log.update_metadata(history_key, {"rotation_generation": 7})
        assert state.conversation_log.rotation_generation(history_key) == 7

        # A subsequent slot save must carry the generation forward, not reset it.
        slot.append("assistant", "world")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)

        assert state.conversation_log.rotation_generation(history_key) == 7, (
            "slot save dropped rotation_generation — a stale consolidation "
            "offset could then be applied against post-rotation messages"
        )

    def test_foreign_append_content_identity_dedup_semantics(
        self, tmp_path, monkeypatch
    ):
        """Pin the narrowed, timestamp-first foreign-append identity (GPT 5.6
        HIGH + arbiter long-term item 2).

        ``_frozen_prefix_and_foreign_appends`` classifies a disk window-region
        line as "ours" (drops it — the window re-serializes it) when EITHER its
        ``ts`` matches a window entry (in-place edit) OR — as a COUNT-BOUNDED
        tiebreak — its ``(role, content)`` matches an as-yet-unconsumed window
        entry (a same-process ``append_if_absent`` copy persisted with a fresh
        ``ts``). Because the tiebreak is bounded, each window entry absorbs AT
        MOST one disk copy: a second same-content line with a DISTINCT ts (a
        genuinely distinct event from another process, e.g. a repeated identical
        cron/workflow result) is PRESERVED as foreign rather than collapsed. This
        verifies both no-loss (the distinct event survives) and no-duplication
        (the append_if_absent fresh-ts copy is folded once, not re-appended).
        See docs/system-specs/modules/history.md.
        """
        import json

        monkeypatch.setattr(
            "kiro_crew.dashboard.state.config_dir", lambda: tmp_path
        )
        from kiro_crew.dashboard.chat_persistence import (
            _frozen_prefix_and_foreign_appends,
        )
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("dedupsem")
        history_key = _history_key_for(slot.key)
        path = state.conversation_log._path(history_key)
        path.parent.mkdir(parents=True, exist_ok=True)

        # On-disk: metadata line + window-region message lines.
        disk_lines = [
            json.dumps({"_type": "metadata", "created": "2026-01-01T00:00:00Z"}),
            # ts-match to a window entry (edited in place) → collapsed.
            json.dumps({"role": "user", "content": "same-ts", "ts": "T1"}),
            # The window's OWN persisted copy of "windowed" (ts-match) → dropped;
            # the window re-serializes it.
            json.dumps({"role": "assistant", "content": "windowed", "ts": "T2"}),
            # A genuinely DISTINCT event with identical (role, content) but a
            # different ts — the GPT 5.6 HIGH data-loss case. With the window's
            # single "windowed" budget already spent by the ts-match above, this
            # MUST be preserved as foreign, not collapsed.
            json.dumps({"role": "assistant", "content": "windowed", "ts": "TX"}),
            # An append_if_absent copy of a window message persisted with a fresh
            # ts (the window's own copy is NOT on disk at this ts) → folded into
            # the window by the bounded content tiebreak (no duplication).
            json.dumps({"role": "assistant", "content": "aif-copy", "ts": "TY"}),
            # Genuinely distinct content → preserved as foreign.
            json.dumps({"role": "assistant", "content": "distinct", "ts": "T3"}),
        ]
        path.write_text("\n".join(disk_lines) + "\n", encoding="utf-8")

        window_entries = [
            {"role": "user", "content": "same-ts", "ts": "T1"},
            {"role": "assistant", "content": "windowed", "ts": "T2"},
            {"role": "assistant", "content": "aif-copy", "ts": "TZ"},
        ]
        slot._frozen_prefix_cache = None

        _prefix, foreign, dedup_dropped = _frozen_prefix_and_foreign_appends(
            slot, path, 0, window_entries
        )
        foreign_contents = [json.loads(ln)["content"] for ln in foreign]

        # ts-match collapsed (window wins on in-place edits / its own copies).
        assert "same-ts" not in foreign_contents
        # No-loss: a distinct same-content event with a fresh ts, beyond the
        # window's single budget for that content, is preserved as foreign.
        assert foreign_contents.count("windowed") == 1, (
            "distinct same-content foreign event collapsed — the (role, content) "
            "dedup must be count-bounded/timestamp-first so two real events are "
            "not folded into one (GPT 5.6 HIGH data-loss finding)"
        )
        # No-duplication: the append_if_absent fresh-ts copy is folded once into
        # the window (its own budget absorbs it) and NOT re-appended as foreign.
        assert "aif-copy" not in foreign_contents, (
            "append_if_absent fresh-ts copy duplicated — the bounded tiebreak "
            "must fold the window's own copy"
        )
        # A genuinely-distinct foreign line is preserved (no data loss).
        assert "distinct" in foreign_contents
        # The folded fresh-ts copy is routed through the archive (item 2b) so the
        # residual ambiguous case loses no data permanently.
        dropped_contents = [json.loads(ln)["content"] for ln in dedup_dropped]
        assert dropped_contents == ["aif-copy"], (
            "fresh-ts content-tiebreak drops must be surfaced for archiving "
            f"(got {dropped_contents!r})"
        )


class TestBestEffortSaveMarksDirty:
    """GPT 5.6 HIGH (chat_persistence.py:1087): metadata mutation endpoints
    (pin / folder / tag / mode) call ``save_slot_off_loop(..., force=True)`` with
    the default ``best_effort=True``. A lock timeout / I/O error was swallowed and
    the slot was NOT marked dirty, so the periodic flush never retried — the
    endpoint returned success but the acknowledged change was lost after restart.

    The fix marks the slot dirty on a swallowed best-effort failure so the 5s
    flush retries. ``save_slot_off_loop`` is a coroutine, so its body always runs
    under a running loop and takes the offloaded (executor) branch — that is the
    path the dashboard metadata endpoints exercise.
    """

    def _make_state(self, tmp_path: Path):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.state import DashboardState

        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    def test_offloaded_best_effort_failure_marks_dirty(self, tmp_path, monkeypatch):
        import asyncio

        from kiro_crew.dashboard import chat_persistence

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._make_state(tmp_path)
        slot = state.get_or_create_slot("beffail")
        slot.append("user", "m1")
        slot.drain()
        slot._dirty = False

        def _boom(*_a, **_k):
            raise RuntimeError("simulated lock timeout / io error")

        monkeypatch.setattr(chat_persistence, "_save_slot_to_history", _boom)

        async def _run() -> None:
            # On a running loop → offloaded (executor) branch.
            await chat_persistence.save_slot_off_loop(state, slot, force=True)

        asyncio.run(_run())

        assert slot._dirty is True, (
            "a swallowed best-effort save failure must mark the slot dirty so the "
            "periodic flush retries — otherwise the metadata change is lost"
        )


class TestOnLoopFdCleanupDeferred:
    """``_locked`` may run synchronously on the event-loop thread (an on-loop
    caller relying on the single non-blocking acquire safety net). Its cleanup
    calls ``platform_compat.release_lock`` (``flock(LOCK_UN)``) and ``os.close``,
    both ``blocking: true`` under the no-blocking-call-on-event-loop rule: a
    wedged descriptor could freeze chat/WS/heartbeat until watchdog restart.
    The cleanup must therefore be dispatched OFF the loop, never run inline on
    the loop thread — while off the loop it still runs inline."""

    def test_off_loop_cleanup_runs_inline(self, tmp_path: Path) -> None:
        import threading

        seen: list[int] = []
        real_close = os.close

        def _spy_close(fd: int) -> None:
            seen.append(threading.get_ident())
            real_close(fd)

        log = ConversationLog(base_dir=tmp_path)
        with_patch = os.close
        try:
            os.close = _spy_close  # type: ignore[assignment]
            log.append("k", "user", "seed")
        finally:
            os.close = with_patch  # type: ignore[assignment]
        # Off the loop: the sidecar-fd close ran inline on this (caller) thread.
        assert seen, "expected the lock fd to be closed"
        assert all(t == threading.get_ident() for t in seen)

    def test_on_loop_cleanup_deferred_to_worker_thread(self, tmp_path: Path) -> None:
        import asyncio
        import threading

        if not platform_compat.IS_POSIX:
            pytest.skip("advisory flock semantics are POSIX-specific")

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")  # create off-loop first

        loop_thread = threading.get_ident()
        # release_lock is called ONLY for the sidecar lock fd (never the session
        # file), so the thread it runs on identifies where _locked's descriptor
        # cleanup executed. release + os.close are one deferred callable, so an
        # off-loop release proves the close is off-loop too.
        release_threads: list[int] = []
        real_release = platform_compat.release_lock

        def _spy_release(fd: int) -> None:
            release_threads.append(threading.get_ident())
            real_release(fd)

        async def _run() -> None:
            platform_compat.release_lock = _spy_release  # type: ignore[assignment]
            try:
                # append() enters _locked ON this loop thread; the release+close
                # must be dispatched to the default executor, not run inline.
                # This deliberately exercises the low-level on-loop primitive, so
                # it bypasses the strict off-loop discipline guard.
                from kiro_crew.history import allow_on_loop_persist
                with allow_on_loop_persist():
                    log.append("k", "assistant", "on-loop-append")
                for _ in range(50):
                    if release_threads:
                        break
                    await asyncio.sleep(0.02)
            finally:
                platform_compat.release_lock = real_release  # type: ignore[assignment]

        asyncio.run(_run())

        assert release_threads, "release_lock was never called on the last exit"
        assert all(t != loop_thread for t in release_threads), (
            "descriptor release/close ran on the event-loop thread"
        )
        # The on-loop append still persisted.
        assert any(
            m.get("content") == "on-loop-append"
            for m in ConversationLog(base_dir=tmp_path)._read_messages("k")
        )


# ── GPT 5.6 HIGH: append_if_absent collapses the workflow/cron double-append ─
class TestAppendIfAbsent:
    """``inject_workflow_result`` / ``inject_cron_result_to_dashboard`` reflect
    a result in the DIRTY in-memory slot (``slot.append``) AND schedule a
    durable disk copy. If the periodic slot save serializes the message first, a
    plain follow-up ``append`` writes it a SECOND time and it is replayed twice
    after restart. ``append_if_absent`` performs the existence check under the
    SAME per-session lock as the write, collapsing the duplicate to a no-op."""

    def test_appends_when_absent(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        assert log.append_if_absent("k", "assistant", "result-1") is True
        contents = [m.get("content") for m in log.read_messages("k")]
        assert contents.count("result-1") == 1

    def test_skips_when_present(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "assistant", "result-1")
        # A second inject of the same result (e.g. after a slot save already
        # wrote it) must NOT duplicate it.
        assert log.append_if_absent("k", "assistant", "result-1") is False
        contents = [m.get("content") for m in ConversationLog(base_dir=tmp_path).read_messages("k")]
        assert contents.count("result-1") == 1

    def test_role_sensitive(self, tmp_path: Path) -> None:
        # Same content under a different role is NOT a duplicate.
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "ping")
        assert log.append_if_absent("k", "assistant", "ping") is True
        msgs = ConversationLog(base_dir=tmp_path).read_messages("k")
        assert [(m["role"], m["content"]) for m in msgs] == [
            ("user", "ping"), ("assistant", "ping")
        ]

    def test_check_and_write_are_atomic_under_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The existence check and the append share one ``_locked`` critical
        section: two concurrent ``append_if_absent`` of the SAME (role, content)
        must serialize so exactly ONE copy is written. If the check were outside
        the lock, both would observe "absent" and both would write."""
        import threading

        if not platform_compat.IS_POSIX:
            pytest.skip("advisory flock semantics are POSIX-specific")

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "seed")
        dup = "workflow-result"

        # Slow the in-lock read of the FIRST writer so the second writer is
        # guaranteed to race the check window (it blocks on _locked until the
        # first releases, then its own check must see the first's write).
        real_read = log._read_messages
        go = threading.Event()
        slowed_once = threading.Event()

        def _slow_read(key: str) -> list[dict]:
            res = real_read(key)
            if key == "k" and not slowed_once.is_set():
                slowed_once.set()
                go.set()
                time.sleep(0.3)
            return res

        monkeypatch.setattr(log, "_read_messages", _slow_read)

        second_wrote: dict[str, bool] = {}

        def _competitor() -> None:
            go.wait(5)
            # Serialized behind the first writer's lock; its check runs AFTER
            # the first write lands, so it must observe the duplicate and skip.
            second_wrote["v"] = log.append_if_absent("k", "assistant", dup)

        t = threading.Thread(target=_competitor)
        t.start()
        first_wrote = log.append_if_absent("k", "assistant", dup)
        t.join(10)

        contents = [m.get("content") for m in ConversationLog(base_dir=tmp_path)._read_messages("k")]
        assert first_wrote is True
        assert second_wrote.get("v") is False, "second append_if_absent should have skipped"
        assert contents.count(dup) == 1, f"duplicate result on disk: {contents!r}"

    def test_off_loop_wrapper_dispatches_to_thread(self, tmp_path: Path) -> None:
        import asyncio
        import threading

        from kiro_crew.history import append_if_absent_off_loop

        log = ConversationLog(base_dir=tmp_path)
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        real = log.append_if_absent

        def _spy(*a: object, **k: object) -> bool:
            seen["thread"] = threading.get_ident()
            return real(*a, **k)  # type: ignore[arg-type]

        log.append_if_absent = _spy  # type: ignore[method-assign]

        async def _run() -> None:
            append_if_absent_off_loop(log, "k", "assistant", "hi")
            for _ in range(50):
                if "thread" in seen:
                    break
                await asyncio.sleep(0.02)

        asyncio.run(_run())
        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, "ran on the event loop thread"
        assert any(m.get("content") == "hi" for m in log.read_messages("k"))
