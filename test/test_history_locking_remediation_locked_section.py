"""Pins what ConversationLog does inside a per-session ``_locked`` hold and on release.

Scenario 6 of the audit list in ``test_history_locking_remediation.py``: a one-line
metadata rewrite (``update_metadata``, ``mark_consolidated``, the sub-offset write)
is synced before the lock is taken, so no file ``fsync`` runs while it is held, and
a write that lands in between is kept. Off the event loop the hold closes its descriptors
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

import json
import os
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import async_wait_until

# ── Bug 6: reduced lock hold — no fsync under lock for one-line metadata ──────
#: The three hot metadata writes, each as ``(write, applied)``: *applied* checks
#: the edit on the metadata a fresh log reads back.
_HOT_METADATA_WRITES = [
    pytest.param(
        lambda log: log.mark_consolidated("k", 1),
        lambda meta: meta["last_consolidated"] == 1,
        id="mark_consolidated",
    ),
    pytest.param(
        lambda log: log.advance_consolidation_sub_offset("k", 0, 5, 0),
        lambda meta: meta["consolidation_sub_offset"] == 5,
        id="sub_offset",
    ),
    pytest.param(
        lambda log: log.update_metadata("k", {"title": "renamed"}),
        lambda meta: meta["title"] == "renamed",
        id="update_metadata",
    ),
]


def _two_rows(tmp_path: Path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    log.append("k", "user", "a")
    log.append("k", "assistant", "b")
    return log


def _record_lock_depth(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Track how many session locks this thread holds, at the lowest lock primitive."""
    import contextlib

    depth = [0]
    real = ConversationLog._locked_stem

    @contextlib.contextmanager
    def recording(self, key):
        with real(self, key):
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    monkeypatch.setattr(ConversationLog, "_locked_stem", recording)
    return depth


class TestReducedLockHold:
    """The flush for a one-line metadata edit happens before the session lock is taken.

    The replacement carries every row, so it is synced (an unsynced file renamed
    over the old one can come back empty after a power loss). But the flush grows
    with the transcript and every other writer of the session waits on that lock,
    so it is staged and synced first, and the lock covers only the check that the
    file is unchanged and the rename.
    """

    @pytest.mark.parametrize(("write", "applied"), _HOT_METADATA_WRITES)
    def test_no_file_fsync_while_the_session_lock_is_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write, applied
    ) -> None:
        import stat

        from kiro_crew import history_rewrite

        log = _two_rows(tmp_path)
        depth = _record_lock_depth(monkeypatch)
        file_syncs: list[int] = []  # lock depth at each regular-file fsync
        dir_syncs: list[tuple[Path, int, bool]] = []  # (directory, lock depth, best_effort)
        real_fsync = os.fsync
        real_fsync_dir = history_rewrite.fsync_dir

        def recording_fsync(fd: int) -> None:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                file_syncs.append(depth[0])
            real_fsync(fd)

        def recording_fsync_dir(path, *, best_effort: bool = False) -> None:
            dir_syncs.append((Path(path), depth[0], best_effort))
            real_fsync_dir(path, best_effort=best_effort)

        monkeypatch.setattr("os.fsync", recording_fsync)
        monkeypatch.setattr(history_rewrite, "fsync_dir", recording_fsync_dir)

        write(log)

        assert file_syncs == [0], f"file fsyncs at lock depths {file_syncs}"
        # The rename is made durable after the lock is released, best effort.
        assert dir_syncs == [(log._path("k").parent, 0, True)], f"directory syncs {dir_syncs}"
        fresh = ConversationLog(base_dir=tmp_path)
        assert applied(fresh.get_metadata("k"))
        assert [m["content"] for m in fresh._read_messages("k")] == ["a", "b"]

    @pytest.mark.parametrize(("write", "applied"), _HOT_METADATA_WRITES)
    @pytest.mark.parametrize("interleaved", ["append", "metadata edit"])
    def test_a_write_between_the_sync_and_the_rename_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write, applied, interleaved
    ) -> None:
        """Another writer lands after the staged copy was synced, before the lock.

        The staged copy does not match the file, so it is discarded and the edit
        is recomputed under the lock: the other writer's row or field survives.
        """
        from kiro_crew import history_rewrite

        log = _two_rows(tmp_path)
        real_stage = history_rewrite.stage_replacement
        fired: list[str] = []

        def stage_then_interleave(path, content, **kwargs):
            staged = real_stage(path, content, **kwargs)
            if not fired:
                fired.append(staged)
                if interleaved == "append":
                    log.append("k", "user", "late")
                else:
                    log.update_metadata("k", {"agent": "other"})
            return staged

        monkeypatch.setattr(history_rewrite, "stage_replacement", stage_then_interleave)

        write(log)

        assert fired, "the interleaving never ran: the write did not stage outside the lock"
        assert not Path(fired[0]).exists(), "the stale staged copy was left behind"
        fresh = ConversationLog(base_dir=tmp_path)
        meta = fresh.get_metadata("k")
        assert applied(meta)
        rows = [m["content"] for m in fresh._read_messages("k")]
        if interleaved == "append":
            assert rows == ["a", "b", "late"]
        else:
            assert rows == ["a", "b"]
            assert meta["agent"] == "other"

    @pytest.mark.parametrize(("write", "applied"), _HOT_METADATA_WRITES)
    def test_the_fallback_frees_the_stale_staged_copy_before_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write, applied
    ) -> None:
        """The locked rebuild never needs room for two whole-transcript copies.

        When an append lands after staging, the stale staged file is removed
        before the locked rewrite writes its own copy, so a disk with room for
        one replacement still takes the edit.
        """
        from kiro_crew import history_rewrite

        log = _two_rows(tmp_path)
        real_stage = history_rewrite.stage_replacement
        real_rewrite = history_rewrite.rewrite_transcript
        staged_names: list[str] = []
        stale_present_at_rewrite: list[bool] = []

        def stage_then_append(path, content, **kwargs):
            staged = real_stage(path, content, **kwargs)
            if not staged_names:
                staged_names.append(staged)
                log.append("k", "user", "late")
            return staged

        def recording_rewrite(path, content):
            stale_present_at_rewrite.append(Path(staged_names[0]).exists())
            real_rewrite(path, content)

        monkeypatch.setattr(history_rewrite, "stage_replacement", stage_then_append)
        monkeypatch.setattr(history_rewrite, "rewrite_transcript", recording_rewrite)

        write(log)

        assert stale_present_at_rewrite == [False], "the stale staged copy was still on disk"
        fresh = ConversationLog(base_dir=tmp_path)
        assert applied(fresh.get_metadata("k"))
        assert [m["content"] for m in fresh._read_messages("k")] == ["a", "b", "late"]

    @pytest.mark.parametrize(("write", "applied"), _HOT_METADATA_WRITES)
    def test_a_same_length_edit_that_restores_the_mtime_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write, applied
    ) -> None:
        """Only the bytes tell this edit apart from the snapshot.

        Another writer edits a row in place to the same length and restores the
        mtime, so size, mtime and inode all still match the unlocked snapshot.
        The staged copy must be discarded and the edit rebuilt under the lock,
        or the other writer's row is lost.
        """
        from kiro_crew import history_rewrite

        log = _two_rows(tmp_path)
        path = log._path("k")
        staged_names: list[str] = []
        published: list[str] = []
        real_stage = history_rewrite.stage_replacement
        real_publish = history_rewrite.publish_replacement

        def stage_then_edit_in_place(target, content, **kwargs):
            staged = real_stage(target, content, **kwargs)
            staged_names.append(staged)
            st = os.stat(path)
            data = path.read_bytes()
            edited = data.replace(b'"b"', b'"z"', 1)
            assert len(edited) == len(data) and edited != data
            with open(path, "r+b") as handle:
                handle.write(edited)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            after = os.stat(path)
            assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
                st.st_size,
                st.st_mtime_ns,
                st.st_ino,
            )
            return staged

        def recording_publish(staged, target):
            published.append(staged)
            real_publish(staged, target)

        monkeypatch.setattr(history_rewrite, "stage_replacement", stage_then_edit_in_place)
        monkeypatch.setattr(history_rewrite, "publish_replacement", recording_publish)

        write(log)

        assert staged_names, "the write did not stage outside the lock"
        assert published == [], "the staged copy was published over a changed file"
        assert not Path(staged_names[0]).exists(), "the stale staged copy was left behind"
        fresh = ConversationLog(base_dir=tmp_path)
        assert applied(fresh.get_metadata("k"))
        assert [m["content"] for m in fresh._read_messages("k")] == ["a", "z"]

    @pytest.mark.parametrize(("write", "applied"), _HOT_METADATA_WRITES)
    def test_a_key_that_moves_before_the_lock_is_rewritten_at_its_new_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write, applied
    ) -> None:
        """The staged copy is published only to the path it was staged for.

        A restore or migration can change which file a key resolves to. The new
        path here is a hard link to the old one, so its bytes match the unlocked
        snapshot: only the path comparison can tell the staged copy belongs to
        the old name.
        """
        import contextlib

        log = _two_rows(tmp_path)
        old = log._path("k")
        moved = tmp_path / "moved.jsonl"
        try:
            os.link(old, moved)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            pytest.skip(f"hard links unavailable here: {exc}")
        before = old.read_bytes()
        state = {"locked": False}
        real_path = ConversationLog._path
        real_locked = ConversationLog._locked

        def resolving_path(self, key):
            if state["locked"] and key == "k":
                return moved
            return real_path(self, key)

        @contextlib.contextmanager
        def moving_locked(self, key):
            with real_locked(self, key):
                state["locked"] = True
                try:
                    yield
                finally:
                    state["locked"] = False

        monkeypatch.setattr(ConversationLog, "_path", resolving_path)
        monkeypatch.setattr(ConversationLog, "_locked", moving_locked)

        write(log)

        assert old.read_bytes() == before, "the edit was published to the stale path"
        lines = moved.read_text(encoding="utf-8").splitlines()
        assert applied(json.loads(lines[0]))
        assert [json.loads(line)["content"] for line in lines[1:]] == ["a", "b"]

    @pytest.mark.parametrize(("write", "applied"), _HOT_METADATA_WRITES)
    def test_a_crlf_transcript_keeps_one_line_ending(self, tmp_path: Path, write, applied) -> None:
        """A transcript written with CRLF endings is edited as text, not as raw lines.

        On Windows a whole-transcript replace written from text gets CRLF endings,
        so the next metadata edit reads one. The rows must come back unchanged
        with a single line-ending style and no doubled carriage return.
        """
        path = _two_rows(tmp_path)._path("k")
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        log = ConversationLog(base_dir=tmp_path)

        write(log)

        data = path.read_bytes()
        assert b"\r\r" not in data
        endings = {
            b"\r\n" if line.endswith(b"\r\n") else b"\n" for line in data.splitlines(keepends=True)
        }
        assert len(endings) == 1, f"mixed line endings: {data!r}"
        fresh = ConversationLog(base_dir=tmp_path)
        assert applied(fresh.get_metadata("k"))
        assert [m["content"] for m in fresh._read_messages("k")] == ["a", "b"]

    @pytest.mark.parametrize(("write", "applied"), _HOT_METADATA_WRITES)
    def test_an_interrupt_as_the_publish_starts_removes_the_staged_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write, applied
    ) -> None:
        """A KeyboardInterrupt in the handoff to publish_replacement leaks nothing.

        The interrupt is raised as the publish is entered, before its own ``try``,
        so only the writer's cleanup around the call can remove the staged file,
        a whole copy of the transcript. The transcript keeps its old bytes.
        """
        from kiro_crew import history_rewrite

        log = _two_rows(tmp_path)
        path = log._path("k")
        before = path.read_bytes()
        handed: list[str] = []

        def interrupted_publish(staged, target):
            handed.append(staged)
            raise KeyboardInterrupt

        monkeypatch.setattr(history_rewrite, "publish_replacement", interrupted_publish)

        with pytest.raises(KeyboardInterrupt):
            write(log)

        assert handed, "the write never reached the publish"
        assert not Path(handed[0]).exists(), "the staged copy was left behind"
        assert sorted(tmp_path.rglob("*.tmp")) == []
        assert path.read_bytes() == before

    @pytest.mark.parametrize(("write", "applied"), _HOT_METADATA_WRITES)
    def test_an_interrupt_after_the_rename_keeps_the_published_edit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write, applied
    ) -> None:
        """Cleanup that still owns the staged name after the rename removes nothing.

        The interrupt is raised once the rename has happened, before the publish
        returns. The writer's cleanup then targets a name the rename already
        moved, so the published edit and both rows survive.
        """
        from kiro_crew import history_rewrite

        log = _two_rows(tmp_path)
        real_publish = history_rewrite.publish_replacement
        handed: list[str] = []

        def publish_then_interrupt(staged, target):
            handed.append(staged)
            real_publish(staged, target)
            raise KeyboardInterrupt

        monkeypatch.setattr(history_rewrite, "publish_replacement", publish_then_interrupt)

        with pytest.raises(KeyboardInterrupt):
            write(log)

        assert handed, "the write never reached the publish"
        assert sorted(tmp_path.rglob("*.tmp")) == []
        fresh = ConversationLog(base_dir=tmp_path)
        assert applied(fresh.get_metadata("k"))
        assert [m["content"] for m in fresh._read_messages("k")] == ["a", "b"]

    def test_update_metadata_still_writes_bytes(self, tmp_path: Path) -> None:
        log = _two_rows(tmp_path)
        log.update_metadata("k", {"title": "renamed"})
        # No newline translation on Windows.
        assert b"\r\n" not in log._path("k").read_bytes()

    def test_update_metadata_still_upserts_a_missing_transcript(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.update_metadata("new", {"title": "t"})
        meta = ConversationLog(base_dir=tmp_path).get_metadata("new")
        assert meta["title"] == "t"
        assert "created_at" in meta


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
