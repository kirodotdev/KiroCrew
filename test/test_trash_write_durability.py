"""Crash-durability of the session trash WRITE path.

A staged batch is the only copy — ``move_to_trash`` MOVES files — so every step
that publishes a name or drops a source has to be ordered against the disk, not
just against the process. These tests pin that ordering the only way it can be
pinned without pulling the power: by making the sync fail and asserting the
source is still there, and by watching which descriptors and directories are
synced before the call returns.

Kept apart from ``test_session_storage.py`` because the invariant is different:
that file protects "both halves move together", this one protects "what a
returned move claims is on disk really is".
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import atomic_write as atomic_write_mod
from kiro_crew import session_storage
from kiro_crew.atomic_write import fsync_dir
from kiro_crew.session_storage import INCOMING_PREFIX, MANIFEST_NAME, SessionIndex

_NOW = 1_700_000_000.0
_DAY = 86400.0

#: Windows has no directory descriptor to open, so ``fsync_dir`` returns at its
#: documented no-op before reaching the ``fsync`` or the ``close`` these tests drive.
#: The behaviour under test genuinely does not exist there, so asserting it would only
#: assert the no-op.
_POSIX_DIR_FDS = pytest.mark.skipif(
    os.name == "nt", reason="directory descriptors do not exist on Windows"
)


@pytest.fixture(autouse=True)
def _fresh_scan_cache() -> None:
    session_storage.invalidate_scan_cache()


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point both stores at temp dirs; return (crew data home, kiro home)."""
    crew_home = tmp_path / "crew"
    kiro_home = crew_home / "kiro"
    (crew_home / "sessions" / "archive").mkdir(parents=True)
    (kiro_home / "sessions" / "cli").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    monkeypatch.setenv("KIRO_HOME", str(kiro_home))
    return crew_home, kiro_home


def _cli_half(kiro_home: Path, sid: str, *, log_bytes: int, age_days: float) -> None:
    root = kiro_home / "sessions" / "cli"
    mtime = _NOW - age_days * _DAY
    for suffix, payload in ((".json", b"{}"), (".jsonl", b"c" * log_bytes)):
        path = root / f"{sid}{suffix}"
        path.write_bytes(payload)
        os.utime(path, (mtime, mtime))


def _transcript(crew_home: Path, stem: str, *, size: int, age_days: float) -> Path:
    path = crew_home / "sessions" / f"{stem}.jsonl"
    path.write_bytes(b"t" * size)
    mtime = _NOW - age_days * _DAY
    os.utime(path, (mtime, mtime))
    return path


def _index(pairs: dict[str, str] | None = None) -> SessionIndex:
    return SessionIndex(
        stem_to_sid={stem: sid for sid, stem in (pairs or {}).items()},
        active_sids=frozenset(),
    )


def _force_cross_filesystem(monkeypatch: pytest.MonkeyPatch, *, under: Path) -> None:
    """Make ``os.rename`` report EXDEV for destinations under *under*.

    The real condition — a data home mounted apart from the kiro-cli store — cannot
    be created in a unit test, and it is the ONLY path on which the copy fallback
    runs, so the tests below would silently exercise nothing without this.
    """
    real_rename = os.rename

    def _rename(src: Any, dst: Any, **kwargs: Any) -> None:
        try:
            crosses = Path(dst).is_relative_to(under)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            crosses = False
        if crosses:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(src, dst, **kwargs)

    monkeypatch.setattr(session_storage.os, "rename", _rename)


def _is_dir_fd(fd: int) -> bool:
    """Whether *fd* is a directory descriptor, without raising on a closed one."""
    try:
        return stat.S_ISDIR(os.fstat(fd).st_mode)
    except OSError:  # pragma: no cover - defensive
        return False


@contextlib.contextmanager
def _dir_fd_faults(
    target: Path,
    *,
    fsync_error: int | None = None,
    close_error: int | None = None,
) -> Iterator[list[int]]:
    """Fail ``fsync``/``close`` for the descriptor opened on *target*, for THIS call only.

    Scoped by TIME, deliberately, and that is the third shape this took. ``os.close`` is a
    process-wide name, so a fault that outlives the call under test reaches pytest's own
    machinery: failing "any directory descriptor" broke the stdlib ``rmtree`` behind
    ``tmp_path`` cleanup, and narrowing to "the descriptor we opened on this path" still
    broke teardown, because that cleanup opens the very directory the test targeted, by
    the same path string. No predicate over descriptors separates the call from the
    teardown -- only keeping the patch installed for the duration of the call does.
    Restoring on exit means nothing after ``fsync_dir`` returns sees these wrappers.

    Yields the list of OUR descriptors that were closed, for tests that count them.
    """
    real_open, real_close, real_fsync = os.open, os.close, os.fsync
    ours: set[int] = set()
    closed: list[int] = []
    wanted = str(target)

    def _open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if str(path) == wanted and _is_dir_fd(fd):
            ours.add(fd)
        return fd

    def _fsync(fd: int) -> None:
        if fd in ours and fsync_error is not None:
            raise OSError(fsync_error, "injected")
        real_fsync(fd)

    def _close(fd: int) -> None:
        mine = fd in ours
        ours.discard(fd)
        if mine:
            closed.append(fd)
        real_close(fd)
        if mine and close_error is not None:
            raise OSError(close_error, "injected")

    os.open, os.fsync, os.close = _open, _fsync, _close  # type: ignore[assignment]
    try:
        yield closed
    finally:
        os.open, os.fsync, os.close = real_open, real_fsync, real_close  # type: ignore[assignment]


def _incoming_files(root: Path) -> list[Path]:
    return [path for path in root.rglob(f"{INCOMING_PREFIX}*")]


class TestCrossFilesystemStaging:
    """The EXDEV fallback: atomic publish, durable bytes, source dropped last."""

    def test_the_source_survives_a_copy_whose_sync_fails(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The proof that the fsync precedes the unlink, without cutting power.

        If the sync ran after the source was dropped — or never ran, which is what
        ``shutil.move`` does — a failing sync would leave the source gone. It is
        still here, so the durability step is on the near side of the unlink.
        """
        crew_home, _ = stores
        transcript = _transcript(crew_home, "dashboard_chat-1", size=64, age_days=400)
        original = transcript.read_bytes()
        dst_dir = crew_home / "elsewhere"
        dst_dir.mkdir()
        _force_cross_filesystem(monkeypatch, under=dst_dir)
        # The FILE sync only. Failing every descriptor would prove nothing about it: the
        # directory sync that follows also raises, and its own failure path withdraws the
        # publication and keeps the source -- the same observable outcome, reached without
        # the fsync under test ever running.
        real_fsync = os.fsync

        def _fail_on_files(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                real_fsync(fd)
                return
            raise OSError(errno.EIO, "no")

        monkeypatch.setattr(session_storage.os, "fsync", _fail_on_files)

        with pytest.raises(OSError):
            session_storage._move_file(transcript, dst_dir / "dashboard_chat-1.jsonl")

        assert transcript.exists()
        assert transcript.read_bytes() == original
        assert not (dst_dir / "dashboard_chat-1.jsonl").exists()
        assert _incoming_files(dst_dir) == []

    def test_an_interrupted_copy_never_appears_under_the_final_name(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copy straight to the final name is the silent-corruption case.

        ``shutil.move`` writes the destination in place, so an exit mid-copy leaves
        a short file under the name the manifest records — complete-looking and not.
        """
        crew_home, _ = stores
        transcript = _transcript(crew_home, "dashboard_chat-1", size=4096, age_days=400)
        dst_dir = crew_home / "elsewhere"
        dst_dir.mkdir()
        dst = dst_dir / "dashboard_chat-1.jsonl"
        _force_cross_filesystem(monkeypatch, under=dst_dir)

        def _half_then_die(src: Any, out: Any, length: int = 0) -> None:
            out.write(src.read(16))
            raise OSError(errno.EIO, "device fell over")

        monkeypatch.setattr(session_storage.shutil, "copyfileobj", _half_then_die)

        with pytest.raises(OSError):
            session_storage._move_file(transcript, dst)

        assert not dst.exists(), "a partial copy was published under the final name"
        assert transcript.exists()
        assert _incoming_files(dst_dir) == []

    def test_the_published_name_and_its_directory_are_durable_before_the_unlink(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bytes are not enough: the NAME reaching them has to be durable too.

        The source is what stops providing that name, so the directory sync has to
        happen while the source is still there.
        """
        crew_home, _ = stores
        transcript = _transcript(crew_home, "dashboard_chat-1", size=64, age_days=400)
        dst_dir = crew_home / "elsewhere"
        dst_dir.mkdir()
        dst = dst_dir / "dashboard_chat-1.jsonl"
        _force_cross_filesystem(monkeypatch, under=dst_dir)

        seen: list[tuple[str, bool]] = []
        real_fsync_dir = session_storage.fsync_dir

        def _spy(path: Path | str, *, best_effort: bool = False) -> None:
            seen.append((str(path), transcript.exists()))
            real_fsync_dir(path, best_effort=best_effort)

        monkeypatch.setattr(session_storage, "fsync_dir", _spy)

        session_storage._move_file(transcript, dst)

        assert dst.read_bytes() == b"t" * 64
        assert not transcript.exists()
        assert (
            str(dst_dir),
            True,
        ) in seen, "the destination directory was not synced while the source still existed"

    def test_a_cross_filesystem_restore_syncs_before_it_unlinks_the_source(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exclusive mover carries the same rule on its copy branch.

        Restore and rollback run through it, and there the destination is live
        session storage — so a lost copy is a session the user asked to get back.
        """
        crew_home, _ = stores
        staged = crew_home / "staged.jsonl"
        staged.write_bytes(b"s" * 32)
        origin = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        monkeypatch.setattr(
            session_storage.os,
            "link",
            lambda src, dst: (_ for _ in ()).throw(OSError(errno.EXDEV, "no links")),
        )
        # The FILE sync only, for the same reason as the staging mover's test: a blanket
        # failure would be satisfied by the directory sync's own rollback path.
        real_fsync = os.fsync

        def _fail_on_files(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                real_fsync(fd)
                return
            raise OSError(errno.EIO, "no")

        monkeypatch.setattr(session_storage.os, "fsync", _fail_on_files)

        with pytest.raises(OSError):
            session_storage._move_file_exclusive(staged, origin)

        assert staged.exists(), "the staged copy was dropped before it was durable"
        assert not origin.exists()

    def test_the_hard_link_restore_syncs_before_it_unlinks_the_source(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fast path has the same two-operation hazard as the copy branch.

        A hard link plus an unlink leaves two directory entries for one file and then
        removes one, and nothing orders those two operations -- unlike a same-filesystem
        rename, which is one. If the unlink reaches disk and the new name does not, the
        inode has no name left at all. So the destination is synced while the source is
        still there.
        """
        crew_home, _ = stores
        staged = crew_home / "staged.jsonl"
        staged.write_bytes(b"s" * 32)
        origin = crew_home / "sessions" / "dashboard_chat-1.jsonl"

        seen: list[tuple[str, bool]] = []
        real_fsync_dir = session_storage.fsync_dir

        def _spy(path: Path | str, *, best_effort: bool = False) -> None:
            seen.append((str(path), staged.exists()))
            real_fsync_dir(path, best_effort=best_effort)

        monkeypatch.setattr(session_storage, "fsync_dir", _spy)

        assert session_storage._move_file_exclusive(staged, origin) is True

        assert origin.read_bytes() == b"s" * 32
        assert not staged.exists()
        assert (
            str(origin.parent),
            True,
        ) in seen, "the restored name was not synced while the staged copy still existed"

    @pytest.mark.skipif(
        not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW: symlink swap needs privilege"
    )
    def test_the_copy_helper_refuses_a_symlink_instead_of_reading_its_target(
        self, tmp_path: Path
    ) -> None:
        """The copy fallback's own guarantee, independent of who calls it.

        Staging never hands it a symlink today -- both enumeration scans use
        ``is_file(follow_symlinks=False)`` -- so this pins the property AT the copy
        rather than relying on a filter several hundred lines away staying that way.
        """
        secret = tmp_path / "credentials"
        secret.write_bytes(b"SECRET-DO-NOT-COPY")
        link = tmp_path / "session.jsonl"
        link.symlink_to(secret)

        with pytest.raises(OSError) as caught:
            session_storage._open_source_no_follow(link).close()

        assert caught.value.errno in (errno.ELOOP, errno.EMLINK), caught.value
        # And a regular file still opens, or the guard would break every copy.
        plain = tmp_path / "plain.jsonl"
        plain.write_bytes(b"ok")
        with session_storage._open_source_no_follow(plain) as handle:
            assert handle.read() == b"ok"

    @pytest.mark.skipif(
        not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW: symlink swap needs privilege"
    )
    def test_a_planted_source_symlink_never_reaches_the_trash(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """End to end: a session file swapped for a link to a secret leaks nothing.

        Enumeration is what stops it -- the link is not staged at all -- and the rest of
        the session still moves. Asserting the leak AND the omission together is what
        makes this fail if either line of defence is removed.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        secret = tmp_path / "credentials"
        secret.write_bytes(b"SECRET-DO-NOT-COPY")
        # Aged too: the staging gate that compares mtimes reads THROUGH a link, so a
        # freshly written target would skip the session for an unrelated reason.
        aged = _NOW - 400 * _DAY
        os.utime(secret, (aged, aged))
        planted = kiro_home / "sessions" / "cli" / "aaaa1111.jsonl"
        planted.unlink()
        planted.symlink_to(secret)
        os.utime(planted, (aged, aged), follow_symlinks=False)
        _force_cross_filesystem(monkeypatch, under=session_storage.trash_root())

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        staged = list(session_storage._batch_dir(batch.batch_id).rglob("*"))
        leaked = [p for p in staged if p.is_file() and b"SECRET" in p.read_bytes()]
        assert leaked == [], f"the link target's bytes were copied into the trash: {leaked}"
        assert not any(
            p.name == "aaaa1111.jsonl" for p in staged
        ), "the planted link was staged rather than passed over"
        assert secret.read_bytes() == b"SECRET-DO-NOT-COPY", "the secret itself was disturbed"
        assert planted.is_symlink(), "the planted link was consumed"

    def test_a_failed_unlink_keeps_the_published_copy(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Withdrawing the destination here would be a guess that can destroy the last copy.

        A filesystem can report failure for an unlink that DID commit, and the name
        existing afterwards does not prove the ORIGINAL does -- a concurrent append
        recreates it as a new, empty file. So the published copy stays, protected by the
        unlisted-file guard, and the batch is left un-emptiable until an operator clears
        it. See ``test_the_kept_copy_is_protected_from_emptying``.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        source = _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        _force_cross_filesystem(monkeypatch, under=session_storage.trash_root())
        real_unlink = Path.unlink

        def _unlink(self: Path, missing_ok: bool = False) -> None:
            if self == source:
                # Commits, then reports failure -- and a concurrent append puts a NEW
                # file back at the same name, which is what defeats an existence check.
                real_unlink(self, missing_ok=missing_ok)
                self.write_bytes(b"fresh")
                raise OSError(errno.EIO, "reply lost")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _unlink)

        with pytest.raises(session_storage.SessionStorageError):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        kept = [
            path
            for path in session_storage.trash_root().rglob("*")
            if path.is_file() and path.read_bytes() == b"t" * 32
        ]
        assert kept, (
            "the historical bytes were deleted by the recovery path; the recreated "
            "source only holds b'fresh'"
        )

    def test_the_exclusive_mover_keeps_the_copy_when_the_unlink_fails(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Same rule on the restore direction's publish-then-drop tail."""
        src = tmp_path / "staged.jsonl"
        src.write_bytes(b"history")
        dst = tmp_path / "restored.jsonl"
        dst.write_bytes(b"history")
        real_unlink = Path.unlink

        def _unlink(self: Path, missing_ok: bool = False) -> None:
            if self == src:
                raise OSError(errno.EIO, "reply lost")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _unlink)

        with pytest.raises(OSError):
            session_storage._publish_then_drop(src, dst)

        assert dst.exists(), "the exclusive mover withdrew a copy it could not prove redundant"

    def test_the_mover_keeps_the_copy_when_the_unlink_fails(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pinned on the mover itself, which is where the decision lives.

        Whatever the caller then does with the leftover, the mover must not be the thing
        that destroys a copy it cannot prove is redundant.
        """
        src = tmp_path / "origin.jsonl"
        src.write_bytes(b"history")
        dst_dir = tmp_path / "batch"
        dst_dir.mkdir()
        dst = dst_dir / "origin.jsonl"
        real_unlink = Path.unlink

        def _unlink(self: Path, missing_ok: bool = False) -> None:
            if self == src:
                raise OSError(errno.EIO, "reply lost")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _unlink)

        with pytest.raises(OSError):
            session_storage._copy_across_filesystems(src, dst)

        assert dst.exists(), "the mover withdrew a copy it could not prove was redundant"
        assert dst.read_bytes() == b"history"

    def test_a_failed_sync_on_the_hard_link_path_withdraws_the_destination(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reporting "origin not taken" while the origin holds a file is unrecoverable.

        The manifest entry is kept and every later restore finds the origin occupied.
        """
        crew_home, _ = stores
        staged = crew_home / "staged.jsonl"
        staged.write_bytes(b"s" * 32)
        origin = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        monkeypatch.setattr(
            session_storage,
            "fsync_dir",
            lambda path, **kw: (_ for _ in ()).throw(OSError(errno.EIO, "device")),
        )

        with pytest.raises(OSError):
            session_storage._move_file_exclusive(staged, origin)

        assert staged.exists(), "the staged copy was dropped after a failed sync"
        assert not origin.exists(), "a published destination outlived a failed sync"

    def test_the_copy_branch_still_refuses_an_occupied_destination(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The added syncs must not cost the no-clobber guarantee."""
        crew_home, _ = stores
        staged = crew_home / "staged.jsonl"
        staged.write_bytes(b"s" * 32)
        origin = crew_home / "sessions" / "dashboard_chat-1.jsonl"
        origin.write_bytes(b"newer")
        monkeypatch.setattr(
            session_storage.os,
            "link",
            lambda src, dst: (_ for _ in ()).throw(OSError(errno.EXDEV, "no links")),
        )

        assert session_storage._move_file_exclusive(staged, origin) is False
        assert origin.read_bytes() == b"newer"
        assert staged.exists()

    def test_an_incoming_file_a_crash_left_behind_is_still_protected(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The deletion guard exempts NOTHING by name, this prefix included.

        A name says nothing about who wrote it. Everything else in this module's threat
        model assumes a batch can be planted in — a linked manifest, a file substituted
        at the manifest's own name — so an exemption keyed on a prefix would let
        anything able to rename a file inside a batch put a staged file beyond this
        scan's reach. The cost is that a crash's debris blocks emptying that batch until
        an operator clears it; blocking a deletion that should proceed is recoverable,
        and letting one through is not.
        """
        crew_home, _ = stores
        batch = crew_home / "trash" / "sessions" / "20260101T000000-abcd1234"
        (batch / "crew").mkdir(parents=True)
        (batch / MANIFEST_NAME).write_text(
            json.dumps({"schema": 1, "batch_id": "x", "created_at": 0, "reason": "manual"})
            + "\n"
            + json.dumps(
                {"uid": "a", "files": [{"rel": "crew/a.jsonl", "origin": "/x/a.jsonl", "bytes": 1}]}
            )
            + "\n",
            encoding="utf-8",
        )
        (batch / "crew" / "a.jsonl").write_bytes(b"a")
        leftover = batch / "crew" / f"{INCOMING_PREFIX}deadbeef"
        leftover.write_bytes(b"half a copy")

        assert session_storage._unlisted_files(batch) == [leftover]

    def test_a_file_no_entry_names_is_still_reported(self, stores: tuple[Path, Path]) -> None:
        """The carve-out is one prefix wide, not a hole in the scan."""
        crew_home, _ = stores
        batch = crew_home / "trash" / "sessions" / "20260101T000000-abcd1234"
        (batch / "crew").mkdir(parents=True)
        (batch / MANIFEST_NAME).write_text(
            json.dumps({"schema": 1, "batch_id": "x", "created_at": 0, "reason": "manual"}) + "\n",
            encoding="utf-8",
        )
        stranded = batch / "crew" / "b.jsonl"
        stranded.write_bytes(b"b")

        assert session_storage._unlisted_files(batch) == [stranded]

    def test_a_cross_filesystem_batch_is_complete_and_emptiable(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end on the copy path: every byte staged, and the batch still empties."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=128, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=64, age_days=400)
        _force_cross_filesystem(monkeypatch, under=session_storage.trash_root())

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        assert batch.sessions == 1
        staged = session_storage._batch_dir(batch.batch_id)
        assert (staged / "crew" / "dashboard_chat-1.jsonl").read_bytes() == b"t" * 64
        assert (staged / "cli" / "aaaa1111.jsonl").read_bytes() == b"c" * 128
        assert _incoming_files(staged) == []
        assert session_storage._unlisted_files(staged) == []


class TestStagedBatchDurability:
    """What a returned ``move_to_trash`` has actually put on disk."""

    def _record_fsync(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[list[tuple[int, int]], list[str]]:
        """Collect (st_dev, st_ino) of every synced descriptor and every synced dir."""
        synced: list[tuple[int, int]] = []
        dirs: list[str] = []
        real_fsync = os.fsync
        real_fsync_dir = session_storage.fsync_dir

        def _fsync(fd: int) -> None:
            info = os.fstat(fd)
            synced.append((info.st_dev, info.st_ino))
            real_fsync(fd)

        def _dir(path: Path | str, *, best_effort: bool = False) -> None:
            dirs.append(str(path))
            real_fsync_dir(path, best_effort=best_effort)

        monkeypatch.setattr(session_storage.os, "fsync", _fsync)
        monkeypatch.setattr(session_storage, "fsync_dir", _dir)
        return synced, dirs

    def test_the_manifest_is_synced_before_the_move_returns(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this the files can be staged under a manifest that never landed.

        A batch with no readable manifest is omitted from ``list_trash``, so those
        files are then reachable by neither restore nor empty — the exact loss this
        layer exists to prevent, produced by reporting success too early.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        synced, _dirs = self._record_fsync(monkeypatch)

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        manifest = session_storage._batch_dir(batch.batch_id) / MANIFEST_NAME
        info = manifest.stat()
        assert (info.st_dev, info.st_ino) in synced, "the manifest descriptor was never synced"

    def test_every_directory_holding_a_staged_name_is_synced(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A staged file's name lives in its own parent, not in the batch root.

        Deepest first, so a child's entries are durable before the entry that names
        the child.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        _dirs_synced, dirs = self._record_fsync(monkeypatch)

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        staged = session_storage._batch_dir(batch.batch_id)
        assert str(staged / "cli") in dirs
        assert str(staged / "crew") in dirs
        assert str(staged) in dirs
        # LAST occurrence, not first: a fresh staging directory is also synced on
        # creation, before anything is renamed into it, so the batch root's first
        # sync legitimately precedes a subdirectory that did not exist yet. The
        # ordering this test is about is the end-of-batch sweep, which is the run
        # these last indices fall in.
        last = {path: index for index, path in enumerate(dirs)}
        assert (
            last[str(staged / "crew")] < last[str(staged)]
        ), "the batch directory was synced before the subdirectory it names"

    def test_the_batch_directory_exists_on_disk_before_anything_moves_into_it(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The files are renamed INTO it, so a lost mkdir loses them with it."""
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        order: list[str] = []
        real_fsync_dir = session_storage.fsync_dir
        real_move = session_storage._move_file

        def _dir(path: Path | str, *, best_effort: bool = False) -> None:
            order.append(f"sync:{path}")
            real_fsync_dir(path, best_effort=best_effort)

        def _move(src: Path, dst: Path) -> None:
            order.append("move")
            real_move(src, dst)

        monkeypatch.setattr(session_storage, "fsync_dir", _dir)
        monkeypatch.setattr(session_storage, "_move_file", _move)

        session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        root = str(session_storage.trash_root())
        assert f"sync:{root}" in order
        assert order.index(f"sync:{root}") < order.index(
            "move"
        ), "files were staged before the batch directory's own entry was durable"

    def test_a_staging_subdirectory_is_durable_before_a_file_is_renamed_into_it(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rename that fills a fresh directory also gives up the file's only other name.

        Syncing the chain with the batch at the end is too late: if the rename reaches
        disk and the mkdir that created its parent does not, neither name resolves.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        order: list[str] = []
        real_fsync_dir = session_storage.fsync_dir
        real_move = session_storage._move_file

        def _dir(path: Path | str, *, best_effort: bool = False) -> None:
            order.append(f"sync:{path}")
            real_fsync_dir(path, best_effort=best_effort)

        def _move(src: Path, dst: Path) -> None:
            order.append(f"move:{dst}")
            real_move(src, dst)

        monkeypatch.setattr(session_storage, "fsync_dir", _dir)
        monkeypatch.setattr(session_storage, "_move_file", _move)

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        staged = session_storage._batch_dir(batch.batch_id)
        moves = [step for step in order if step.startswith("move:")]
        assert moves, "nothing was staged, so this test would pass vacuously"
        for step in moves:
            parent = Path(step.removeprefix("move:")).parent
            assert parent != staged, "expected a subdirectory, not the batch root"
            sync = f"sync:{parent}"
            assert sync in order, f"{parent} was never synced"
            assert order.index(sync) < order.index(step), (
                f"{parent} still held no durable entry of its own when a file was "
                f"renamed into it"
            )

    def test_a_failed_staging_directory_sync_puts_the_session_back(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing has entered the directory yet, so this rolls back rather than raising.

        Propagating would abandon a batch whose manifest has not been forced out. The
        session is left where it was, exactly as for a file that would not move.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        source = _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        real_tree = session_storage._fsync_tree
        leaves: list[Path] = []

        def _tree(root: Path, leaf: Path) -> None:
            leaves.append(leaf)
            if len(leaves) == 1:
                # The pre-loop sync of the trash root's own ancestors, which has to
                # keep working or the batch never gets far enough to stage anything.
                real_tree(root, leaf)
                return
            raise OSError(errno.EIO, "device is gone")

        monkeypatch.setattr(session_storage, "_fsync_tree", _tree)

        # Not a zero-session success: this module already refuses to report a batch
        # in which nothing moved, which is the honest answer here too.
        with pytest.raises(session_storage.SessionStorageError):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )

        assert len(leaves) > 1, "the staging-directory sync never ran"
        assert source.exists(), "the session was not put back"

    def test_an_intermediate_directory_mkdir_created_is_synced_too(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session with archive segments and no transcript never touches ``crew``.

        ``mkdir(parents=True)`` creates ``crew`` on the way to ``crew/archive``, and a
        directory's own name lives in its parent's entries — so syncing only the leaf
        leaves the entry that reaches the staged segments unrecorded.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        segment = crew_home / "sessions" / "archive" / "dashboard_chat-1__20260730-211852.jsonl"
        segment.write_bytes(b"a" * 48)
        mtime = _NOW - 400 * _DAY
        os.utime(segment, (mtime, mtime))
        _dirs_synced, dirs = self._record_fsync(monkeypatch)

        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        staged = session_storage._batch_dir(batch.batch_id)
        assert (staged / "crew" / "archive" / segment.name).exists()
        assert str(staged / "crew" / "archive") in dirs
        assert (
            str(staged / "crew") in dirs
        ), "the intermediate directory mkdir(parents=True) created was never synced"

    def test_the_drained_source_directories_are_synced_once_per_batch(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Copy-then-unlink is two operations, so a crash can leave BOTH names.

        That is a live session the user was told had been reclaimed, sitting beside a
        staged copy the manifest records as trashed. Synced once at the end rather than
        per file: the same-filesystem path is a bare rename and this is the hot path.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        _dirs_synced, dirs = self._record_fsync(monkeypatch)

        session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        assert str(crew_home / "sessions") in dirs
        assert str(kiro_home / "sessions" / "cli") in dirs
        assert (
            dirs.count(str(crew_home / "sessions")) == 1
        ), "the source directory was synced per file instead of once per batch"

    def test_nothing_that_can_raise_runs_after_a_file_has_moved(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A completed move must never reach the caller as a failure.

        The staging loop reads an exception from the mover as "this file did not
        move": it rolls the session back and omits the file from the manifest. For a
        file that HAS moved that leaves a half-staged session nothing records -- the
        exact split the rollback exists to prevent. So a failing source-directory sync
        may not be raised from inside the mover.
        """
        crew_home, _ = stores
        transcript = _transcript(crew_home, "dashboard_chat-1", size=64, age_days=400)
        dst_dir = crew_home / "elsewhere"
        dst_dir.mkdir()
        dst = dst_dir / "dashboard_chat-1.jsonl"
        _force_cross_filesystem(monkeypatch, under=dst_dir)
        real_fsync_dir = session_storage.fsync_dir

        def _fail_once_the_source_is_gone(path: Path | str, *, best_effort: bool = False) -> None:
            if not transcript.exists():
                raise OSError(errno.EIO, "device")
            real_fsync_dir(path, best_effort=best_effort)

        monkeypatch.setattr(session_storage, "fsync_dir", _fail_once_the_source_is_gone)

        session_storage._move_file(transcript, dst)

        assert dst.read_bytes() == b"t" * 64
        assert not transcript.exists()

    def test_the_metadata_is_carried_before_the_sync_that_forces_it(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``copystat`` writes the inode; the file ``fsync`` is what forces it out.

        After the sync, a power loss could publish the bytes under the temp's own 0600
        and its creation time -- and this module ages sessions by their mtime.
        """
        crew_home, _ = stores
        transcript = _transcript(crew_home, "dashboard_chat-1", size=64, age_days=400)
        dst_dir = crew_home / "elsewhere"
        dst_dir.mkdir()
        _force_cross_filesystem(monkeypatch, under=dst_dir)

        order: list[str] = []
        real_copystat = session_storage.shutil.copystat
        real_fsync = os.fsync

        def _copystat(src: Any, dst: Any, **kwargs: Any) -> None:
            order.append("copystat")
            real_copystat(src, dst, **kwargs)

        def _fsync(fd: int) -> None:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                order.append("fsync")
            real_fsync(fd)

        monkeypatch.setattr(session_storage.shutil, "copystat", _copystat)
        monkeypatch.setattr(session_storage.os, "fsync", _fsync)

        session_storage._move_file(transcript, dst_dir / "dashboard_chat-1.jsonl")

        assert order == ["copystat", "fsync"], f"metadata was carried after the sync: {order}"
        assert (dst_dir / "dashboard_chat-1.jsonl").stat().st_mtime == pytest.approx(
            _NOW - 400 * _DAY, abs=1
        )

    def test_the_first_batch_ever_syncs_the_trash_directories_it_created(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``mkdir(parents=True)`` creates ``trash/`` and ``trash/sessions/`` too.

        An unsynced ``trash/sessions`` entry takes the batch inside it, and the batch
        holds the only copies.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        root = session_storage.trash_root()
        assert not root.exists(), "this test only means anything on a first-ever batch"
        _dirs_synced, dirs = self._record_fsync(monkeypatch)

        session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=_index({"aaaa1111": "dashboard_chat-1"}),
            now=_NOW,
        )

        assert str(root) in dirs
        assert str(root.parent) in dirs, "the trash directory's own parent was never synced"
        assert str(session_storage.data_home()) in dirs


class TestRestoredManifestDurability:
    def test_rewriting_a_manifest_syncs_the_directory_that_names_it(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``fsync=True`` covers the content; the rename needs the directory.

        Otherwise a power loss can return the pre-restore manifest, whose entries
        name files restore has already moved back into live storage.
        """
        crew_home, _ = stores
        batch = crew_home / "trash" / "sessions" / "20260101T000000-abcd1234"
        batch.mkdir(parents=True)
        dirs: list[str] = []
        real_fsync_dir = session_storage.fsync_dir

        def _dir(path: Path | str, *, best_effort: bool = False) -> None:
            dirs.append(str(path))
            real_fsync_dir(path, best_effort=best_effort)

        monkeypatch.setattr(session_storage, "fsync_dir", _dir)

        session_storage._rewrite_manifest(batch, {"schema": 1}, [{"uid": "a", "files": []}])

        assert str(batch) in dirs
        assert (batch / MANIFEST_NAME).read_text(encoding="utf-8").count("\n") == 2


class TestFsyncDirDiscriminates:
    """Quiet where a directory sync cannot be expressed; loud where the device failed.

    Both halves are load-bearing. Callers unlink the only other copy right after
    calling this, so a swallowed ``EIO`` hands them a false "durable"; but Windows and
    some network mounts cannot sync a directory at all, and raising there would fail
    every write on those platforms.
    """

    def test_a_platform_without_directory_descriptors_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows has no directory descriptor to open, and that is not an error."""
        monkeypatch.setattr(atomic_write_mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("no dir fds"))
        )

        fsync_dir(tmp_path)

    def test_a_directory_that_cannot_be_opened_on_posix_is_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a platform that HAS directory fds, failing to open one is an anomaly."""
        monkeypatch.setattr(atomic_write_mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(
            os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "device"))
        )

        with pytest.raises(OSError):
            fsync_dir(tmp_path)

    def test_the_injection_does_not_outlive_the_call(self, tmp_path: Path) -> None:
        """The property three rounds of teardown errors were missing.

        Both earlier attempts left the fault installed for the whole test and tried to
        pick out the right descriptor; pytest's own cleanup went through the wrapper
        anyway. Restoring on exit is what makes that impossible, so it is pinned here
        rather than left to the next reader to rediscover.
        """
        before = (os.open, os.fsync, os.close)

        with _dir_fd_faults(tmp_path, close_error=errno.EIO):
            assert os.close is not before[2], "the fault was never installed"

        assert (os.open, os.fsync, os.close) == before, "the fault outlived its call"

    @_POSIX_DIR_FDS
    def test_a_filesystem_that_refuses_the_sync_is_quiet_and_closes_the_descriptor(
        self, tmp_path: Path
    ) -> None:
        """Some network mounts reject fsync on a directory; the fd is still ours."""
        with _dir_fd_faults(tmp_path, fsync_error=errno.EINVAL) as closed:
            fsync_dir(tmp_path)

        assert len(closed) == 1

    @_POSIX_DIR_FDS
    def test_a_device_error_is_raised_not_logged_away(self, tmp_path: Path) -> None:
        """The finding that matters: EIO means the write did not land.

        A caller about to unlink the only other copy has to hear it.
        """
        with _dir_fd_faults(tmp_path, fsync_error=errno.EIO):
            with pytest.raises(OSError):
                fsync_dir(tmp_path)

    @_POSIX_DIR_FDS
    def test_a_deferred_write_error_reported_by_close_is_raised(self, tmp_path: Path) -> None:
        """``close`` can report a write error the kernel deferred past the ``fsync``.

        For a caller whose next step is to unlink the only other copy that is the same
        signal as a failed sync, so by default it is not swallowed.
        """
        with _dir_fd_faults(tmp_path, close_error=errno.EIO):
            with pytest.raises(OSError):
                fsync_dir(tmp_path)

    @_POSIX_DIR_FDS
    def test_best_effort_survives_a_close_error_too(self, tmp_path: Path) -> None:
        """Otherwise ``best_effort`` would still raise past its caller's commit point.

        A restore that has already unlinked its staged source cannot act on this, and a
        raise there would leave the batch holding a stale manifest entry.
        """
        with _dir_fd_faults(tmp_path, close_error=errno.EIO):
            fsync_dir(tmp_path, best_effort=True)

    @_POSIX_DIR_FDS
    def test_a_close_error_does_not_mask_the_sync_error(self, tmp_path: Path) -> None:
        """The sync error names the real problem; the close error would replace it."""
        with _dir_fd_faults(tmp_path, fsync_error=errno.EIO, close_error=errno.EBADF):
            with pytest.raises(OSError) as caught:
                fsync_dir(tmp_path)

        assert caught.value.errno == errno.EIO, "the close error masked the sync error"

    @_POSIX_DIR_FDS
    def test_a_failed_directory_sync_keeps_the_source(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: the raise has to reach the mover before it drops the source."""
        crew_home, _ = stores
        transcript = _transcript(crew_home, "dashboard_chat-1", size=64, age_days=400)
        dst_dir = crew_home / "elsewhere"
        dst_dir.mkdir()
        _force_cross_filesystem(monkeypatch, under=dst_dir)
        real_fsync = os.fsync

        def _fail_on_directories(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "device")
            real_fsync(fd)

        monkeypatch.setattr(session_storage.os, "fsync", _fail_on_directories)
        dst = dst_dir / "dashboard_chat-1.jsonl"

        with pytest.raises(OSError):
            session_storage._move_file(transcript, dst)

        assert transcript.exists(), "the source was dropped after a failed directory sync"
        # And the publication is withdrawn. The caller reads the exception as "this file
        # did not move" and rolls the session back WITHOUT this file, so a surviving dst
        # would sit in the batch named by no manifest entry -- which blocks emptying that
        # batch for good.
        assert not dst.exists(), "a published destination outlived a failed sync"
        assert _incoming_files(dst_dir) == []

    def test_a_failed_manifest_sync_is_not_reported_as_a_completed_move(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch whose manifest never reached the device is not a moved batch.

        The failure is restricted to non-directory descriptors on purpose. Failing
        every ``fsync`` would prove nothing about the manifest: the directory syncs
        that follow raise on ``EIO`` too, so the test would still see an exception
        with the manifest's own sync swallowed.
        """
        crew_home, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=400)
        _transcript(crew_home, "dashboard_chat-1", size=32, age_days=400)
        real_fsync = os.fsync

        def _fail_on_files(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                real_fsync(fd)
                return
            raise OSError(errno.EIO, "device")

        monkeypatch.setattr(session_storage.os, "fsync", _fail_on_files)

        with pytest.raises(OSError):
            session_storage.move_to_trash(
                ["aaaa1111"],
                reason="manual",
                index=_index({"aaaa1111": "dashboard_chat-1"}),
                now=_NOW,
            )
