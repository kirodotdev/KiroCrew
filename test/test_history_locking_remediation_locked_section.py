"""Pins what ConversationLog does inside a per-session ``_locked`` hold and on release.

Scenario 6 of the audit list in ``test_history_locking_remediation.py``: a one-line
metadata rewrite (``update_metadata``, ``mark_consolidated``) persists without an
``fsync`` while the lock is held. Off the event loop the hold closes its descriptors
inline on the caller's thread; entered on the loop thread, its cleanup
(``release_lock`` and ``os.close``) runs on a worker thread and the append still
lands. ``append_if_absent`` writes only when no message with the same role and
content exists, runs that check and the write in one hold so two racing calls write
one copy, and ``append_if_absent_off_loop`` dispatches it off the loop thread. How
the lock is acquired (cross-fd exclusivity, the bounded and on-loop acquires,
reentrancy) is pinned in ``test_history_locking_remediation.py``; the dashboard
save that relies on this lock is pinned in
``test_history_locking_remediation_dashboard_save.py``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import async_wait_until


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
                await async_wait_until(
                    lambda: bool(release_threads),
                    describe=lambda: f"release_threads={release_threads}",
                )
            finally:
                platform_compat.release_lock = real_release  # type: ignore[assignment]

        asyncio.run(_run())

        assert release_threads, "release_lock was never called on the last exit"
        assert all(
            t != loop_thread for t in release_threads
        ), "descriptor release/close ran on the event-loop thread"
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
            ("user", "ping"),
            ("assistant", "ping"),
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
                # flake-ok: pre-existing 300 ms hold inside the first writer's slowed read that widens the check window the second writer races, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep, moved verbatim; tracked for a de-flake pass
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

        contents = [
            m.get("content") for m in ConversationLog(base_dir=tmp_path)._read_messages("k")
        ]
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
            await async_wait_until(
                lambda: "thread" in seen,
                describe=lambda: f"seen={seen}",
            )

        asyncio.run(_run())
        assert seen.get("thread") is not None
        assert seen["thread"] != loop_thread, "ran on the event loop thread"
        assert any(m.get("content") == "hi" for m in log.read_messages("k"))
