"""Session storage: restoring a trash batch, and the session's attachments.

A restore puts every half back or leaves the whole session staged, never trusts the
manifest it reads, and publishes the transcript and its reply-thread sidecar under the
transcript's lock. ``TestAttachmentsAreTheThirdHalf`` treats the session's attachments as
a third half that is measured, moved, restored and emptied with the transcript.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import threading
from pathlib import Path

import pytest
from session_storage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _DAY,
    _NOW,
    _archive_segment,
    _cli_half,
    _fresh_scan_cache,
    _index,
    _transcript,
    stores_fixture,
)

from kiro_crew import session_storage
from kiro_crew.history import ConversationLog
from kiro_crew.session_storage import SessionStorageError


def _attachment(crew_home: Path, stem: str, name: str, *, size: int, age_days: float) -> int:
    """One image in the session's attachments directory, aged with its transcript."""
    adir = crew_home / "sessions" / f"{stem}.attachments"
    adir.mkdir(exist_ok=True)
    path = adir / name
    path.write_bytes(b"i" * size)
    mtime = _NOW - age_days * _DAY
    os.utime(path, (mtime, mtime))
    return size


class TestRestoreIsAllOrNothing:
    def test_restores_every_half(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=64, age_days=40)
        _archive_segment(crew_home, "dashboard_chat-1", "20260730-211852", size=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        restored = session_storage.restore(batch.batch_id)

        assert restored == 1
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").read_bytes() == b"c" * 32
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").read_bytes() == b"t" * 64
        seg = crew_home / "sessions" / "archive" / "dashboard_chat-1__20260730-211852.jsonl"
        assert seg.read_bytes() == b"a" * 16
        assert session_storage.list_trash() == []

    def test_restore_publishes_transcript_under_history_lock(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        active_locks: set[str] = set()
        real_locked = ConversationLog._locked_stem
        real_move = session_storage._move_file_exclusive
        transcript = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        replay = kiro_home / "sessions" / "cli" / "aaaa1111.jsonl"

        @contextlib.contextmanager
        def recording_lock(log: ConversationLog, key: str):
            with real_locked(log, key):
                active_locks.add(key)
                try:
                    yield
                finally:
                    active_locks.remove(key)

        def observed_move(src: Path, dst: Path) -> bool:
            if dst == replay:
                assert active_locks == set()
            if dst == transcript:
                assert "dashboard_chat-1" in active_locks
            return real_move(src, dst)

        monkeypatch.setattr(ConversationLog, "_locked_stem", recording_lock)
        monkeypatch.setattr(session_storage, "_move_file_exclusive", observed_move)

        assert session_storage.restore(batch.batch_id) == 1
        assert transcript.is_file()

    @pytest.mark.parametrize(
        "stem",
        ["slack_1785370133.085469", "1785370133.085469"],
    )
    def test_restore_locks_both_slack_transcript_aliases(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        stem: str,
    ) -> None:
        crew_home, kiro_home = stores
        thread_ts = "1785370133.085469"
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, stem, size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": stem}),
            now=_NOW,
        )
        active_locks: set[str] = set()
        real_locked = ConversationLog._locked_stem
        real_move = session_storage._move_file_exclusive
        transcript = crew_home / "sessions" / f"{stem}.jsonl"

        @contextlib.contextmanager
        def recording_lock(log: ConversationLog, key: str):
            with real_locked(log, key):
                active_locks.add(key)
                try:
                    yield
                finally:
                    active_locks.remove(key)

        def observed_move(src: Path, dst: Path) -> bool:
            if dst == transcript:
                assert {thread_ts, f"slack_{thread_ts}"} <= active_locks
            return real_move(src, dst)

        monkeypatch.setattr(ConversationLog, "_locked_stem", recording_lock)
        monkeypatch.setattr(session_storage, "_move_file_exclusive", observed_move)

        assert session_storage.restore(batch.batch_id) == 1
        assert transcript.is_file()

    def test_waiting_canonical_append_re_resolves_after_bare_restore(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        crew_home, kiro_home = stores
        thread_ts = "1785370133.085469"
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, thread_ts, size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": thread_ts}),
            now=_NOW,
        )
        sessions_dir = crew_home / "sessions"
        log = ConversationLog(base_dir=sessions_dir)
        writer_waiting = threading.Event()
        release_writer = threading.Event()
        errors: list[BaseException] = []
        real_locked = ConversationLog._locked
        writer: threading.Thread

        @contextlib.contextmanager
        def paused_writer_lock(current_log: ConversationLog, key: str):
            if threading.current_thread() is writer:
                writer_waiting.set()
                if not release_writer.wait(timeout=5):
                    raise TimeoutError("restore never released the waiting append")
            with real_locked(current_log, key):
                yield

        def append_after_restore() -> None:
            try:
                log.append(f"slack:{thread_ts}", "user", "writer")
            except BaseException as exc:
                errors.append(exc)

        monkeypatch.setattr(ConversationLog, "_locked", paused_writer_lock)
        writer = threading.Thread(target=append_after_restore)
        started = False
        try:
            writer.start()
            started = True
            assert writer_waiting.wait(timeout=5)
            assert session_storage.restore(batch.batch_id) == 1
        finally:
            release_writer.set()
            if started:
                writer.join(timeout=5)
                assert not writer.is_alive(), "restore race worker outlived its test"

        canonical = sessions_dir / f"slack_{thread_ts}.jsonl"
        bare = sessions_dir / f"{thread_ts}.jsonl"
        assert errors == []
        assert not canonical.exists()
        assert b'"content": "writer"' in bare.read_bytes()

    def test_canonical_occupant_blocks_bare_alias_restore(
        self,
        stores: tuple[Path, Path],
    ) -> None:
        crew_home, kiro_home = stores
        thread_ts = "1785370133.085469"
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, thread_ts, size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": thread_ts}),
            now=_NOW,
        )
        canonical = crew_home / "sessions" / f"slack_{thread_ts}.jsonl"
        canonical.write_bytes(b"new canonical transcript")

        assert session_storage.restore(batch.batch_id) == 0
        assert canonical.read_bytes() == b"new canonical transcript"
        assert not (crew_home / "sessions" / f"{thread_ts}.jsonl").exists()
        assert [item.batch_id for item in session_storage.list_trash()] == [batch.batch_id]

    def test_a_full_restore_never_writes_to_the_batch_it_is_leaving(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full-restore branch writes NOTHING to the batch, and keeps its listing stale.

        Both are one decision. Every entry below the header describes a file this restore
        moved back out, so a batch the cleanup declines to remove still lists sessions that
        are not in it. Clearing them needs a write to the batch PATH, and `atomic_write`
        replaces its destination: a batch swapped to a link would send that write to another
        batch's manifest and make ITS staged sessions unlistable. A stale listing is visible
        and reversible; that is not. So the listing stays and `empty_trash` is the way out.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        def _no_writes_here(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"the full-restore path wrote by name: {args!r}")

        # Scoped to just these two patches: `monkeypatch` is the same instance
        # the `stores` fixture pins KIROCREW_HOME/KIRO_HOME with, and
        # `monkeypatch.undo()` unwinds its WHOLE stack in LIFO order, including
        # entries pushed before this test ever ran. An `undo()` here to restore
        # `atomic_write` also unpins the data home, and the `empty_trash()` call
        # below then resolves `trash_root()` against the operator's real
        # `~/.kiro/crew` and writes a real `trash/session-storage.lock`.
        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(session_storage, "atomic_write", _no_writes_here)
            # A cleanup that DECLINES, which is what makes the stale listing reachable: the
            # batch stays instead of going away with its manifest.
            scoped.setattr(session_storage, "_remove_emptied_batch", lambda *a, **k: False)

            assert session_storage.restore(batch.batch_id) == 1

            assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").read_bytes() == b"c" * 8
            kept = session_storage.list_trash()
            assert [b.batch_id for b in kept] == [batch.batch_id], "kept, not removed"
            # The accepted residual, pinned so that clearing it later is a deliberate change.
            assert kept[0].sessions == 1, "the listing goes stale rather than being rewritten"

        # And the user is not stuck with it: the explicit empty still takes the batch.
        # KIROCREW_HOME/KIRO_HOME (set by the `stores` fixture, outside the `with`
        # above) are still pinned here.
        session_storage.empty_trash()
        assert session_storage.list_trash() == []

    def test_an_occupied_half_leaves_the_whole_session_staged(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Restoring only the free half would recreate a half-session."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        # The transcript came back on its own while the session sat in the trash.
        (crew_home / "sessions" / "dashboard_chat-1.jsonl").write_bytes(b"NEWER")

        restored = session_storage.restore(batch.batch_id)

        assert restored == 0
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").read_bytes() == b"NEWER"
        # The replay half must NOT have been put back on its own.
        assert not (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").exists()
        assert session_storage.list_trash()[0].sessions == 1

    def test_partial_restore_by_uid_keeps_the_rest_staged(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111", "bbbb2222"], reason="manual", index=_index(), now=_NOW
        )

        restored = session_storage.restore(batch.batch_id, ["aaaa1111"])

        assert restored == 1
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert not (kiro_home / "sessions" / "cli" / "bbbb2222.jsonl").exists()
        assert session_storage.list_trash()[0].sessions == 1

    def test_an_origin_naming_another_session_is_refused(self, stores: tuple[Path, Path]) -> None:
        """Both paths are inside a session store, so containment cannot catch it."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        victim = kiro_home / "sessions" / "cli" / "bbbb2222.jsonl"
        batch_dir = session_storage.trash_root() / batch.batch_id
        manifest = batch_dir / session_storage.MANIFEST_NAME
        lines = manifest.read_text().splitlines()
        entry = json.loads(lines[1])
        entry["files"][0]["origin"] = str(victim)
        lines[1] = json.dumps(entry)
        manifest.write_text("\n".join(lines) + "\n")

        restored = session_storage.restore(batch.batch_id)

        # The harm this prevents: the other session's path is never written to.
        # Deriving the origin from the staged path is what guarantees it; the
        # agreement check then turns a disagreeing manifest into a refusal rather
        # than a silently ignored field.
        assert not victim.exists()
        assert restored == 0

    @pytest.mark.skipif(not hasattr(os, "O_DIRECTORY"), reason="POSIX descriptor-relative move")
    def test_a_pinned_move_of_a_planted_link_never_publishes_its_target(
        self, tmp_path: Path
    ) -> None:
        """The descriptor-relative branch is ``linkat``, which CPython asks to
        FOLLOW the source by default: a link planted in the staged trash would then
        be resolved and a hard link to its target -- a credential store, say --
        published under `.threads`. The mover must link the link itself (or refuse),
        never expose the target's bytes at the destination."""
        secret = tmp_path / "secret"
        secret.write_bytes(b"AKIA-not-for-the-agent")
        staged = tmp_path / "trash"
        staged.mkdir()
        planted = staged / "chat.json"
        planted.symlink_to(secret)
        dest_dir = tmp_path / ".threads"
        dest_dir.mkdir()
        dst = dest_dir / "chat.json"
        dir_fd = os.open(dest_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            session_storage._move_file_exclusive(planted, dst, dst_dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        if dst.exists() or dst.is_symlink():
            assert dst.is_symlink(), "the destination must never be a plain file of the target"
        assert secret.stat().st_nlink == 1, "no second name was given to the target"

    def test_an_exclusive_move_never_replaces_an_occupied_destination(self, tmp_path: Path) -> None:
        """The no-clobber guarantee itself, independent of any restore path."""
        src = tmp_path / "src.jsonl"
        src.write_bytes(b"staged")
        taken = tmp_path / "taken.jsonl"
        taken.write_bytes(b"newer generation")
        free = tmp_path / "free.jsonl"

        assert session_storage._move_file_exclusive(src, taken) is False
        assert taken.read_bytes() == b"newer generation"
        assert src.is_file(), "a refused move must not consume the staged file"

        assert session_storage._move_file_exclusive(src, free) is True
        assert free.read_bytes() == b"staged"
        assert not src.exists()

    def test_losing_the_origin_race_retains_the_entry(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused move must leave the session staged and still restorable."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        calls: list[tuple[Path, Path]] = []

        def occupied(src: Path, dst: Path) -> bool:
            # Stand in for the gateway recreating the session after the preflight.
            calls.append((src, dst))
            return False

        monkeypatch.setattr(session_storage, "_move_file_exclusive", occupied)

        assert session_storage.restore(batch.batch_id) == 0
        # Proves the move loop was reached, so this pins the lost-race branch and
        # not an earlier refusal that would return 0 for a different reason.
        assert calls, "restore never attempted the move"
        # The batch is intact, so the user can retry once the origin frees up.
        assert [b.batch_id for b in session_storage.list_trash()] == [batch.batch_id]
        staged = session_storage.trash_root() / batch.batch_id / "cli" / "aaaa1111.jsonl"
        assert staged.is_file()

    def test_a_failed_restore_stays_retryable(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-restored session would be split AND wedged against every retry."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        real_move = session_storage._move_file_exclusive
        calls = {"n": 0}

        def flaky(src: Path, dst: Path) -> bool:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated failure mid-restore")
            return real_move(src, dst)

        monkeypatch.setattr(session_storage, "_move_file_exclusive", flaky)
        assert session_storage.restore(batch.batch_id) == 0

        # The staged files are all back in the batch, so a later retry succeeds.
        monkeypatch.setattr(session_storage, "_move_file_exclusive", real_move)
        assert session_storage.restore(batch.batch_id) == 1
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()
        assert session_storage.list_trash() == []

    def test_restore_never_deletes_a_file_the_manifest_omits(
        self, stores: tuple[Path, Path]
    ) -> None:
        """An interrupted move leaves a staged file nothing lists — the only copy."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        # Simulate a crash between moving a file in and appending its manifest line.
        orphan = staged / "cli" / "cccc3333.jsonl"
        orphan.write_bytes(b"ONLY COPY")

        assert session_storage.restore(batch.batch_id) == 1

        assert orphan.read_bytes() == b"ONLY COPY"
        assert staged.is_dir()

    def test_an_unstattable_file_aborts_the_whole_session(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that cannot be sized cannot be recorded, so it cannot be skipped.

        Staging the rest would commit a manifest that omits it — exactly the split
        the rollback exists to prevent.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        target = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        real_stamp = session_storage._file_stamp

        def flaky_stamp(path: Path) -> tuple[int, float]:
            if path == target:
                raise OSError("simulated transient stat failure")
            return real_stamp(path)

        monkeypatch.setattr(session_storage, "_file_stamp", flaky_stamp)

        with pytest.raises(SessionStorageError, match="none of the selected"):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
        assert target.is_file()
        assert session_storage.list_trash() == []

    def test_a_malformed_manifest_record_blocks_its_session(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Restoring the readable files would leave the rest staged and unreferenced."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["files"].append("not-an-object")
        manifest.write_text(f"{lines[0]}\n{json.dumps(entry)}\n", encoding="utf-8")

        restored = session_storage.restore(batch.batch_id)

        assert restored == 0
        # Nothing was put back, so nothing is split.
        assert not (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").exists()
        assert not (crew_home / "sessions" / "dashboard_chat-1.jsonl").exists()
        assert session_storage.list_trash()[0].sessions == 1

    def test_unknown_batch_is_refused(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(SessionStorageError, match="no restorable batch"):
            session_storage.restore("20240101T000000-deadbeef")


class TestManifestIsUntrusted:
    """The manifest lives in an agent-writable tree, so restore must not trust it."""

    def _staged(self, kiro_home: Path) -> tuple[str, Path]:
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        return batch.batch_id, session_storage.trash_root() / batch.batch_id

    def test_an_absolute_rel_cannot_pick_up_a_file_outside_the_batch(
        self, stores: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """`Path(batch) / "/etc/x"` is `/etc/x` — joining an absolute discards the base."""
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        secret = tmp_path / "credentials"
        secret.write_bytes(b"SECRET")
        manifest = batch / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        header = lines[0]
        tampered = json.dumps(
            {
                "uid": "aaaa1111",
                "files": [
                    {
                        "rel": str(secret),
                        "origin": str(kiro_home / "sessions" / "cli" / "stolen.jsonl"),
                        "bytes": 6,
                    }
                ],
            }
        )
        manifest.write_text(f"{header}\n{tampered}\n", encoding="utf-8")

        restored = session_storage.restore(batch_id)

        assert restored == 0
        assert secret.read_bytes() == b"SECRET"
        assert not (kiro_home / "sessions" / "cli" / "stolen.jsonl").exists()

    def test_a_traversing_rel_is_refused(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        manifest = batch / session_storage.MANIFEST_NAME
        header = manifest.read_text(encoding="utf-8").splitlines()[0]
        tampered = json.dumps(
            {
                "uid": "aaaa1111",
                "files": [
                    {
                        "rel": "../../../etc/hosts",
                        "origin": str(kiro_home / "sessions" / "cli" / "x.jsonl"),
                        "bytes": 1,
                    }
                ],
            }
        )
        manifest.write_text(f"{header}\n{tampered}\n", encoding="utf-8")

        assert session_storage.restore(batch_id) == 0

    def test_an_origin_outside_the_session_stores_is_refused(
        self, stores: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Restore writes to origin, so an unconstrained origin is arbitrary write."""
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        target = tmp_path / "planted.jsonl"
        manifest = batch / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["files"] = [
            {"rel": record["rel"], "origin": str(target), "bytes": record["bytes"]}
            for record in entry["files"]
        ]
        manifest.write_text(f"{lines[0]}\n{json.dumps(entry)}\n", encoding="utf-8")

        restored = session_storage.restore(batch_id)

        assert restored == 0
        assert not target.exists()


class TestThreadSidecarRestoresUnderTheTranscriptLock:
    """The reply-thread sidecar is written under the transcript's lock, so restore
    publishes it -- and rolls it back -- under that same lock, beside the
    transcript. Published before the lock, a reply a recreated chat committed in
    between would ride the sidecar back to trash on a lost race."""

    def _sidecar(self, crew_home: Path, stem: str) -> Path:
        d = crew_home / "sessions" / ".threads"
        d.mkdir(exist_ok=True)
        p = d / f"{stem}.json"
        p.write_text(json.dumps({"version": 1, "threads": {}}), encoding="utf-8")
        aged = _NOW - 40 * _DAY
        os.utime(p, (aged, aged))
        return p

    def test_the_sidecar_appears_only_once_the_transcript_lock_is_held(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        sidecar = self._sidecar(crew_home, "dashboard_chat-1")
        transcript = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        assert not sidecar.exists() and not transcript.exists()

        seen: dict[str, bool] = {}
        real_locked = session_storage.ConversationLog.locked_stems

        @contextlib.contextmanager
        def _observing(self_log, stems):
            with real_locked(self_log, stems):
                # Nothing under the lock's protection exists before it is taken.
                seen["sidecar_before"] = sidecar.exists()
                seen["transcript_before"] = transcript.exists()
                yield
                seen["sidecar_inside"] = sidecar.exists()
                seen["transcript_inside"] = transcript.exists()

        monkeypatch.setattr(session_storage.ConversationLog, "locked_stems", _observing)
        assert session_storage.restore(batch.batch_id) == 1
        assert seen == {
            "sidecar_before": False,
            "transcript_before": False,
            "sidecar_inside": True,
            "transcript_inside": True,
        }
        assert sidecar.exists() and transcript.exists()

    def test_one_vanished_sidecar_costs_no_other_session_its_freshness(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reply writes only the sidecar, so its mtime is what keeps a threaded
        session young. Each entry is stat'ed on its own: a sidecar renamed away by
        a concurrent delete mid-scan must not empty the whole list and age every
        other threaded session by its transcript alone."""
        crew_home, _kiro_home = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _transcript(crew_home, "dashboard_chat-2", size=50, age_days=40)
        fresh = self._sidecar(crew_home, "dashboard_chat-1")
        os.utime(fresh, (_NOW, _NOW))
        vanishing = self._sidecar(crew_home, "dashboard_chat-2")
        real_scandir = os.scandir

        class _Vanishing:
            """An ``os.scandir`` iterator whose listing of the vanishing sidecar
            is the moment it disappears -- the concurrent delete, mid-scan."""

            def __init__(self, it):
                self._it = it

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._it.__exit__(*exc)

            def close(self):
                self._it.close()

            def __iter__(self):
                return self

            def __next__(self):
                entry = next(self._it)
                if entry.name == vanishing.name and vanishing.exists():
                    vanishing.unlink()
                return entry

        def _scandir(path=".", *a, **k):
            it = real_scandir(path, *a, **k)
            if isinstance(path, int) or os.fspath(path) != os.fspath(fresh.parent):
                return it
            return _Vanishing(it)

        monkeypatch.setattr(session_storage.os, "scandir", _scandir)
        by_stem = {
            u.stems[0]: u for u in session_storage.list_units(_index(), cached=False) if u.stems
        }
        assert by_stem["dashboard_chat-1"].age_days(_NOW) < 1, "the fresh sidecar still counts"
        assert by_stem["dashboard_chat-2"].age_days(_NOW) >= 40

    def test_a_sidecar_alone_is_not_restored_onto_a_recreated_chat(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A staged sidecar whose transcript is not in the batch takes the same
        transcript recheck under the lock: a chat recreated under that stem is a
        different chat, and the old replies must not be attached to it."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        sidecar = self._sidecar(crew_home, "dashboard_chat-1")
        transcript = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        staged_dir = session_storage.trash_root() / batch.batch_id / "crew"
        # The transcript never comes back from this batch (a kill mid-staging left
        # the sidecar behind on its own); the chat is then recreated.
        (staged_dir / "dashboard_chat-1.jsonl").unlink()
        manifest = session_storage.trash_root() / batch.batch_id / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["files"] = [f for f in entry["files"] if not str(f["rel"]).endswith(".jsonl")]
        assert any(str(f["rel"]).endswith(".json") for f in entry["files"]), "the sidecar stays"
        manifest.write_text(f"{lines[0]}\n{json.dumps(entry)}\n", encoding="utf-8")
        transcript.write_bytes(b"new chat")
        assert session_storage.restore(batch.batch_id) == 0
        assert not sidecar.exists(), "old replies must not land beside the new chat"
        assert (staged_dir / ".threads" / "dashboard_chat-1.json").exists()
        assert transcript.read_bytes() == b"new chat"

    def test_a_lost_race_rolls_the_sidecar_back_under_the_lock_too(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        sidecar = self._sidecar(crew_home, "dashboard_chat-1")
        transcript = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        real_locked = session_storage.ConversationLog.locked_stems

        @contextlib.contextmanager
        def _recreate_under_lock(self_log, stems):
            with real_locked(self_log, stems):
                # The chat comes back the instant the lock is taken: the restore
                # must lose the race for BOTH files and leave both staged.
                transcript.write_bytes(b"new chat")
                yield

        monkeypatch.setattr(session_storage.ConversationLog, "locked_stems", _recreate_under_lock)
        assert session_storage.restore(batch.batch_id) == 0
        assert transcript.read_bytes() == b"new chat"
        assert not sidecar.exists()
        staged = (
            session_storage.trash_root()
            / batch.batch_id
            / "crew"
            / ".threads"
            / "dashboard_chat-1.json"
        )
        assert staged.exists()

    def test_a_link_where_the_sidecar_directory_should_be_leaves_the_batch_staged(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        self._sidecar(crew_home, "dashboard_chat-1")
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        threads_dir = crew_home / "sessions" / ".threads"
        elsewhere = crew_home / "elsewhere"
        elsewhere.mkdir()
        if threads_dir.exists():
            threads_dir.rmdir()
        threads_dir.symlink_to(elsewhere, target_is_directory=True)
        assert session_storage.restore(batch.batch_id) == 0
        assert list(elsewhere.iterdir()) == []
        assert not (crew_home / "sessions" / "dashboard_chat-1.jsonl").exists()

    def test_a_link_swapped_in_under_the_lock_cannot_redirect_the_publish(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The preflight's link check has a window; the publish closes it by
        addressing the pinned `.threads` descriptor, so a link swapped in after
        preflight is refused and nothing lands under its target."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        self._sidecar(crew_home, "dashboard_chat-1")
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW
        )
        threads_dir = crew_home / "sessions" / ".threads"
        elsewhere = crew_home / "elsewhere"
        elsewhere.mkdir()
        real_locked = session_storage.ConversationLog.locked_stems

        @contextlib.contextmanager
        def _swap_under_lock(self_log, stems):
            with real_locked(self_log, stems):
                if threads_dir.exists() and not threads_dir.is_symlink():
                    threads_dir.rmdir()
                if not threads_dir.exists():
                    threads_dir.symlink_to(elsewhere, target_is_directory=True)
                yield

        monkeypatch.setattr(session_storage.ConversationLog, "locked_stems", _swap_under_lock)
        assert session_storage.restore(batch.batch_id) == 0
        assert list(elsewhere.iterdir()) == []
        assert not (crew_home / "sessions" / "dashboard_chat-1.jsonl").exists()


class TestAttachmentsAreTheThirdHalf:
    """``<stem>.attachments/`` holds the images a transcript's rows reference, so it
    is measured, moved, restored and emptied with the transcript -- leaving it
    behind would keep served images for a session the user reclaimed."""

    def test_attachment_bytes_are_the_sessions(self, stores: tuple[Path, Path]) -> None:
        crew_home, kiro_home = stores
        cli = _cli_half(kiro_home, "aaaa1111", log_bytes=100, age_days=40)
        crew = _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        img = _attachment(crew_home, "dashboard_chat-1", "abcd-shot.png", size=900, age_days=40)

        report = session_storage.measure(_index({"aaaa1111": "dashboard_chat-1"}), now=_NOW)

        assert report.total_sessions == 1
        assert report.total_bytes == cli + crew + img

    def test_a_fresh_attachment_keeps_the_session_fresh(self, stores: tuple[Path, Path]) -> None:
        """An image written hours ago is activity on the session, whatever the
        transcript's own mtime says."""
        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=50, age_days=40)
        _attachment(crew_home, "dashboard_chat-1", "abcd-shot.png", size=9, age_days=0.2)

        with pytest.raises(SessionStorageError, match="touched in the last"):
            session_storage.move_to_trash(
                ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
            )

    def test_attachments_move_restore_and_empty_with_the_session(
        self, stores: tuple[Path, Path]
    ) -> None:
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        _attachment(crew_home, "dashboard_chat-1", "abcd-shot.png", size=33, age_days=40)
        _attachment(crew_home, "dashboard_chat-1", "ef01-other.png", size=44, age_days=40)
        adir = crew_home / "sessions" / "dashboard_chat-1.attachments"

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        # Moved: the images travel under the directory's own name, and the drained
        # directory does not linger as an empty shell beside the live sessions.
        staged = session_storage.trash_root() / batch.batch_id / "crew" / adir.name
        assert (staged / "abcd-shot.png").read_bytes() == b"i" * 33
        assert (staged / "ef01-other.png").read_bytes() == b"i" * 44
        assert not adir.exists()
        entry = json.loads(
            (session_storage.trash_root() / batch.batch_id / session_storage.MANIFEST_NAME)
            .read_text(encoding="utf-8")
            .splitlines()[1]
        )
        assert str(adir / "abcd-shot.png") in {r["origin"] for r in entry["files"]}

        # Restored: the directory comes back with its files.
        assert session_storage.restore(batch.batch_id) == 1
        assert (adir / "abcd-shot.png").read_bytes() == b"i" * 33
        assert (adir / "ef01-other.png").read_bytes() == b"i" * 44
        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()

        # Emptied: nothing of the session is left anywhere.
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        freed = session_storage.empty_trash([batch.batch_id])
        assert freed >= 8 + 8 + 33 + 44
        assert not adir.exists()
        assert not (session_storage.trash_root() / batch.batch_id).exists()
        assert session_storage.list_trash() == []

    def test_the_drained_attachments_dir_is_removed_under_the_transcript_lock(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The empty-shell ``rmdir`` runs while the unit's transcript lock is held.

        A writer resuming the session creates the directory and lands its first
        image under that same lock, so an ``rmdir`` issued after the lock is
        released could take the directory between the ``mkdir`` and the file --
        the image copy fails open and the row keeps its scratch path.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        _attachment(crew_home, "dashboard_chat-1", "abcd-shot.png", size=33, age_days=40)
        adir = crew_home / "sessions" / "dashboard_chat-1.attachments"

        events: list[tuple[str, str]] = []
        real_locked_stem = ConversationLog._locked_stem

        @contextlib.contextmanager
        def recording_locked_stem(self: ConversationLog, key: str):
            events.append(("enter", key))
            try:
                with real_locked_stem(self, key):
                    yield
            finally:
                events.append(("exit", key))

        real_rmdir = os.rmdir

        def recording_rmdir(path, *args, **kwargs):
            events.append(("rmdir", str(path)))
            return real_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(ConversationLog, "_locked_stem", recording_locked_stem)
        monkeypatch.setattr(os, "rmdir", recording_rmdir)

        session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )
        assert not adir.exists()

        rmdir_at = events.index(("rmdir", str(adir)))
        before = events[:rmdir_at]
        opened = [i for i, e in enumerate(before) if e == ("enter", "dashboard_chat-1")]
        assert opened, "the transcript lock was never taken before the rmdir"
        assert ("exit", "dashboard_chat-1") not in before[
            opened[-1] :
        ], "the attachments directory was removed after the transcript lock was released"

    def test_a_foreign_entry_in_the_attachments_dir_is_not_taken(
        self, stores: tuple[Path, Path]
    ) -> None:
        """This code writes only flat files there. A subdirectory is not the
        session's and is left alone -- and so is the now non-empty directory."""
        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        _attachment(crew_home, "dashboard_chat-1", "abcd-shot.png", size=5, age_days=40)
        adir = crew_home / "sessions" / "dashboard_chat-1.attachments"
        (adir / "foreign").mkdir()
        aged = _NOW - 40 * _DAY
        os.utime(adir / "foreign", (aged, aged))

        batch = session_storage.move_to_trash(
            ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
        )

        assert batch.sessions == 1
        assert not (adir / "abcd-shot.png").exists()
        assert (adir / "foreign").is_dir()
        assert session_storage.restore(batch.batch_id) == 1
        assert (adir / "abcd-shot.png").read_bytes() == b"i" * 5

    def test_an_unreadable_attachments_dir_leaves_the_session_whole(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Files the scan cannot see cannot be moved, so nothing of the session is:
        moving the transcript alone would orphan images that are still served."""
        crew_home, _ = stores
        _transcript(crew_home, "dashboard_chat-1", size=8, age_days=40)
        _attachment(crew_home, "dashboard_chat-1", "abcd-shot.png", size=5, age_days=40)
        adir = crew_home / "sessions" / "dashboard_chat-1.attachments"
        real_scandir = os.scandir

        def refusing(path=".", *a, **k):
            if isinstance(path, (str, os.PathLike)) and Path(path) == adir:
                raise PermissionError(errno.EACCES, "denied", str(path))
            return real_scandir(path, *a, **k)

        monkeypatch.setattr(session_storage.os, "scandir", refusing)
        with pytest.raises(SessionStorageError, match="none of the selected sessions"):
            session_storage.move_to_trash(
                ["dashboard_chat-1"], reason="manual", index=_index(), now=_NOW
            )

        assert (crew_home / "sessions" / "dashboard_chat-1.jsonl").is_file()
        assert (adir / "abcd-shot.png").is_file()

    def test_a_manifest_cannot_route_an_attachment_outside_its_store(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The origin is DERIVED from the staged path, and the directory name in it
        must be a session id: a tampered record cannot name another directory."""
        assert session_storage._canonical_origin("crew/../x.attachments/a.png") is None
        assert session_storage._canonical_origin("crew/not a session.attachments/a.png") is None
        assert session_storage._canonical_origin("crew/x.attachments/a/b.png") is None
        crew_home, _ = stores
        origin = session_storage._canonical_origin("crew/dashboard_chat-1.attachments/a.png")
        assert origin == crew_home / "sessions" / "dashboard_chat-1.attachments" / "a.png"
