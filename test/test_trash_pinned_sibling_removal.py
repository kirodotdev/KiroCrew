"""The two sibling trash-removal paths, and the primitive they now share.

Both paths reach a point where the batch holds nothing worth keeping and has to go.
Removing it by PATH re-resolves every ancestor, so a directory above the trash swapped
to a symbolic link -- by anything running as this user, which in this product includes
an agent -- is followed and the removal lands outside the trash entirely.

The swap is set up on disk rather than simulated, and the victim carries a file, so a
test that passes says the file survived rather than that a code path was taken.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path

import pytest

from kiro_crew import pinned_fs, session_storage

_NOW = 1_700_000_000.0
_BATCH = "20231114-batch"

pytestmark = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="the pinned removal needs openat/O_NOFOLLOW; the coarse branch is tested elsewhere",
)


def _batch_with_manifest(root: Path, name: str, *, entries: list[dict[str, object]]) -> Path:
    """A trash batch whose header claims its own directory name."""
    batch = root / name
    batch.mkdir(parents=True)
    lines = [
        json.dumps(
            {
                "schema": session_storage.MANIFEST_SCHEMA,
                "batch_id": name,
                "created_at": _NOW,
                "reason": "manual",
            }
        )
    ]
    lines += [json.dumps(entry) for entry in entries]
    (batch / session_storage.MANIFEST_NAME).write_text("\n".join(lines) + "\n")
    return batch


def _approve_anything(_root_fd: int, _tree: pinned_fs.PinnedTree) -> str | None:
    """The approval a test passes when the thing under test is the removal, not the check.

    ``remove_tree_pinned`` requires the hook rather than accepting None, so a test
    exercising the mechanism itself has to STATE that it approves everything instead of
    inheriting the weaker ask-nothing mode by omission.
    """
    return None


def _identity(path: Path) -> tuple[int, int]:
    """What the caller captures before its own by-path read, and binds the removal to."""
    info = os.stat(path, follow_symlinks=False)
    return (info.st_dev, info.st_ino)


def _swap_parent_for_a_link(parent: Path, victim: Path) -> None:
    """Replace *parent* with a symbolic link to *victim*, keeping the real one aside.

    This is the ancestor swap, done exactly as an attacker would: the batch's own path
    is untouched and still spelled the same, but every component above it now resolves
    somewhere else.
    """
    parent.rename(parent.with_name(parent.name + ".real"))
    parent.symlink_to(victim, target_is_directory=True)


class TestAncestorSwapContainment:
    """Neither sibling path may delete through a swapped ancestor."""

    def test_discarding_a_restored_batch_refuses_a_swapped_ancestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The swap lands AFTER the by-path pre-screen, which is the real window.

        ``_discard_restored_batch`` reads the batch by path to decide whether it holds
        anything unlisted, and then removes it. A swap BEFORE that read is caught by the
        read itself -- it walks into the victim and finds files no manifest names. The
        window the removal has to survive on its own is the one that opens after the read
        returns, so the swap is performed at that seam.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        # What the swap points at: a directory holding a batch of the SAME name, with a
        # file in it. Nothing here is a trash batch -- there is no manifest -- which is
        # what the pinned approval asks and what a name cannot answer.
        victim = tmp_path / "victim"
        (victim / _BATCH).mkdir(parents=True)
        precious = victim / _BATCH / "precious.txt"
        precious.write_text("the only copy")

        real_unlisted = session_storage._unlisted_files

        def _screen_then_swap(target: Path) -> list[Path]:
            found = real_unlisted(target)
            _swap_parent_for_a_link(root, victim)
            return found

        monkeypatch.setattr(session_storage, "_unlisted_files", _screen_then_swap)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            # The identity comes from the caller, recorded before the restore read anything -
            # here, before the swap the scan performs below.
            session_storage._discard_restored_batch(batch, _identity(batch))

        assert precious.read_text() == "the only copy"
        assert (victim / _BATCH).is_dir()
        assert "keeping trash batch" in caplog.text
        # The real batch is untouched too: a refusal removes nothing at all.
        assert (root.with_name("trash.real") / _BATCH).is_dir()

    def test_the_empty_batch_cleanup_refuses_a_swapped_ancestor(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The same swap at the other site, reached through its own entry point.

        ``move_to_trash``'s cleanup runs when no session was staged, so it is exercised
        through :func:`_remove_emptied_batch` with that site's own wording -- the branch
        above it is a different function's control flow, not this removal's.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        # Captured BEFORE the swap, which is what the caller does: `move_to_trash` records
        # it at the moment it creates the batch, under the mutation lock.
        identity = _identity(batch)
        victim = tmp_path / "victim"
        (victim / _BATCH).mkdir(parents=True)
        precious = victim / _BATCH / "staged.jsonl"
        precious.write_text("staged, unlisted, the only copy")

        _swap_parent_for_a_link(root, victim)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            session_storage._remove_emptied_batch(batch, "the empty batch", expect=identity)

        assert precious.read_text() == "staged, unlisted, the only copy"
        assert (victim / _BATCH).is_dir()

    def test_a_victim_holding_a_forged_matching_manifest_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        """A swap target that IS a batch, with a manifest forged to name the selected one.

        This is the case the content checks alone cannot carry, and it is why an identity is
        captured at all. An actor who can write into the tree the link points at can plant a
        header naming this batch and a listing covering its contents, and every
        content-based question then answers yes about the wrong directory. An inode cannot be
        forged by writing files, so the caller records one before its own by-path read and
        the approval compares the pinned root against it.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        # Captured BEFORE the swap, which is what both callers do.
        identity = _identity(batch)
        victim = tmp_path / "victim"
        # A genuine batch directory whose manifest header names the SELECTED batch, so the
        # header check passes on it, and whose only file the manifest lists, so the
        # unlisted-file check passes too.
        forged = _batch_with_manifest(
            victim,
            _BATCH,
            entries=[{"uid": "aaaa1111", "files": [{"rel": "session.jsonl", "bytes": 4}]}],
        )
        (forged / "session.jsonl").write_text("data")

        _swap_parent_for_a_link(root, victim)

        session_storage._remove_emptied_batch(batch, "the restored batch", expect=identity)

        assert forged.is_dir()
        assert (forged / "session.jsonl").read_text() == "data"
        assert (forged / session_storage.MANIFEST_NAME).is_file()

    def test_a_swap_during_the_restore_is_refused_by_the_callers_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The anchor is taken at the TOP of the restore, so a swap anywhere inside it is caught.

        The cleanup is reached only after the restore has read the manifest and moved every
        listed file out. An identity sampled there would already be the substitute's if an
        ancestor had been swapped during any of that, and every later check would then agree
        with itself about the wrong directory. Recording it before the first read makes the
        pinned comparison refuse instead. Driven through `restore()` so the capture point under
        test is the real one.
        """
        home = tmp_path / "home"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setenv("KIRO_HOME", str(home / "kiro"))
        (home / "sessions").mkdir(parents=True)
        (home / "kiro" / "sessions" / "cli").mkdir(parents=True)
        session_storage.invalidate_scan_cache()
        live = home / "kiro" / "sessions" / "cli" / "aaaa1111.jsonl"
        live.write_text("live")
        # Old enough to be reclaimable: staging refuses a session touched recently.
        os.utime(live, (_NOW - 40 * 86400, _NOW - 40 * 86400))
        batch = session_storage.move_to_trash(
            ["aaaa1111"],
            reason="manual",
            index=session_storage.SessionIndex(stem_to_sid={}, active_sids=frozenset()),
            now=_NOW,
        )
        root = session_storage.trash_root()
        victim = tmp_path / "victim"
        (victim / batch.batch_id).mkdir(parents=True)
        precious = victim / batch.batch_id / "staged.jsonl"
        precious.write_text("the only copy")
        # A manifest that would satisfy every CONTENT question about the victim.
        (victim / batch.batch_id / session_storage.MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema": session_storage.MANIFEST_SCHEMA,
                    "batch_id": batch.batch_id,
                    "created_at": _NOW,
                    "reason": "manual",
                }
            )
            + "\n"
        )
        real_unlisted = session_storage._unlisted_files

        def _screen_then_swap(target: Path) -> list[Path]:
            found = real_unlisted(target)
            _swap_parent_for_a_link(root, victim)
            return found

        monkeypatch.setattr(session_storage, "_unlisted_files", _screen_then_swap)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.restore(batch.batch_id) == 1

        assert precious.read_text() == "the only copy", "the victim's file must survive"
        assert (victim / batch.batch_id / session_storage.MANIFEST_NAME).is_file()
        assert "not the directory that was selected" in caplog.text

    def test_an_unestablished_identity_refuses_rather_than_proceeds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No identity means nothing to bind to, so the removal is refused, not attempted.

        Proceeding would answer every remaining question about a directory reached by name
        alone, which is the state this whole path exists to get out of.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            session_storage._remove_emptied_batch(batch, "the empty batch", expect=None)

        assert batch.is_dir()
        assert (batch / session_storage.MANIFEST_NAME).is_file()
        assert "identity was never established" in caplog.text


class TestUnswappedRemovalStillHappens:
    """The containment must not turn either path into a no-op."""

    def test_a_clean_restored_batch_is_removed(self, tmp_path: Path) -> None:
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        # Interior directories left behind by a restore: emptied of files, still present.
        (batch / "cli").mkdir()
        (batch / "cli" / "nested").mkdir()

        session_storage._discard_restored_batch(batch, _identity(batch))

        assert not batch.exists()
        assert root.is_dir()

    def test_a_refused_removal_writes_nothing_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cleanup must not write to a path whose removal it just refused.

        A refusal is evidence about the PATH -- a swapped ancestor, an identity that could
        not be read, a platform with no descriptor to bind to. ``atomic_write`` REPLACES its
        destination, so tidying the manifest afterwards would land wherever the path now
        leads: answering evidence of a swap by writing through it. The entries are cleared
        by the caller BEFORE any of this, so there is nothing left for this path to write.
        """
        root = tmp_path / "trash"
        entries = [{"uid": "aaaa1111", "files": [{"rel": "cli/aaaa1111.jsonl", "bytes": 4}]}]
        batch = _batch_with_manifest(root, _BATCH, entries=entries)
        before = (batch / session_storage.MANIFEST_NAME).read_text()
        real_remove = pinned_fs.remove_dir_verified

        def _refuse_the_batch(
            parent_fd: int, name: str, *, expect: tuple[int, int]
        ) -> pinned_fs.StagedRemoval:
            if name == _BATCH:
                return pinned_fs.StagedRemoval(
                    removed=False, reason=pinned_fs.REMOVAL_FAILED, error=OSError("held open")
                )
            return real_remove(parent_fd, name, expect=expect)

        def _no_writes_here(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"the cleanup wrote by path: {args!r}")

        monkeypatch.setattr(session_storage, "atomic_write", _no_writes_here)
        # The refusal that reaches furthest: the batch directory itself will not go, so a
        # descriptor confirmed the path first and a tidying write would look safest here.
        monkeypatch.setattr(pinned_fs, "remove_dir_verified", _refuse_the_batch)
        session_storage._discard_restored_batch(batch, _identity(batch))

        assert (batch / session_storage.MANIFEST_NAME).read_text() == before

    def test_a_batch_holding_an_unlisted_file_is_kept(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The pinned approval enforces the same rule the by-path pre-screen does.

        Reached by calling the removal directly, which is what a caller whose by-path
        walk raced the arrival of the file would do.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        (batch / "orphan.jsonl").write_text("staged but never recorded")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            session_storage._remove_emptied_batch(batch, "the empty batch", expect=_identity(batch))

        assert (batch / "orphan.jsonl").read_text() == "staged but never recorded"
        assert "which nothing here may remove" in caplog.text

    def test_a_batch_holding_a_link_is_kept_rather_than_unlinked_by_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A link is the one entry a removal can only take by NAME, so it withholds too.

        `unlink_verified` compares the scanned inode and then unlinks, which is two syscalls
        with no atomic "unlink if this inode" between them. Admitting a link admits that
        window: plant one, let the approval pass it, put a file at the name in between, and the
        removal deletes the file. `empty_trash` still clears such a batch when a user asks.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        (batch / "planted").symlink_to(tmp_path / "nowhere")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            removed = session_storage._remove_emptied_batch(
                batch, "the restored batch", expect=_identity(batch)
            )

        assert removed is False
        assert (batch / "planted").is_symlink(), "nothing may be removed by name here"
        assert "link(s), which the removal could only take by name" in caplog.text

    def test_a_file_at_a_listed_path_is_kept_not_deleted(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The manifest naming a path does NOT authorise removing whatever sits there.

        Both callers arrive with the batch already empty of files -- the restore path has
        moved every listed file back out, and the staging path has a manifest with no entries
        at all. So a file at a listed path is not the listed file; it arrived at that name
        afterwards and may be the only copy of whatever it is. An earlier version of this
        test asserted the opposite, which was the hole: it let a name the manifest happens to
        mention authorise a deletion.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(
            root,
            _BATCH,
            entries=[{"uid": "aaaa1111", "files": [{"rel": "cli/aaaa1111.jsonl", "bytes": 4}]}],
        )
        (batch / "cli").mkdir()
        replanted = batch / "cli" / "aaaa1111.jsonl"
        replanted.write_text("arrived after the restore moved the real one out")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            session_storage._remove_emptied_batch(
                batch, "the restored batch", expect=_identity(batch)
            )

        assert replanted.read_text() == "arrived after the restore moved the real one out"
        assert "which nothing here may remove" in caplog.text


class TestTheManifestGoesLast:
    """A tree that will not empty must stay LISTED, which means keeping its manifest.

    `list_trash()` omits a batch with no readable manifest. So a removal that unlinks the
    manifest and then fails to remove the directory leaves data on disk that is neither
    visible nor restorable - worse than not having tried, and reported as a partial success.
    """

    def test_a_staged_file_that_will_not_go_leaves_the_manifest_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "trash"
        batch = _batch_with_manifest(
            root,
            _BATCH,
            entries=[{"uid": "aaaa1111", "files": [{"rel": "cli/aaaa1111.jsonl", "bytes": 4}]}],
        )
        (batch / "cli").mkdir()
        (batch / "cli" / "aaaa1111.jsonl").write_text("data")

        real_unlink = os.unlink

        def _refuse(path: object, **kwargs: object) -> None:
            if path == "aaaa1111.jsonl":
                raise OSError("held open")
            real_unlink(path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "unlink", _refuse)
        session_storage._remove_emptied_batch(batch, "the restored batch", expect=_identity(batch))

        assert (batch / "cli" / "aaaa1111.jsonl").read_text() == "data"
        assert (batch / session_storage.MANIFEST_NAME).is_file(), "the batch must stay listable"

    def test_a_batch_directory_that_will_not_go_gets_its_manifest_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest is moved aside BEFORE the final rmdir, so that failing must undo it.

        Put back with ``os.link``, and the debris copy RETAINED: no check available before an
        unlink can prove the name it went back to still holds a good copy, so removing the
        redundant copy is a chance to turn a stuck batch into an unlistable one. The retained
        name is reported rather than left silent.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        original = (batch / session_storage.MANIFEST_NAME).read_text()
        real_remove = pinned_fs.remove_dir_verified

        def _refuse_the_batch(
            parent_fd: int, name: str, *, expect: tuple[int, int]
        ) -> pinned_fs.StagedRemoval:
            if name == _BATCH:
                return pinned_fs.StagedRemoval(
                    removed=False,
                    reason=pinned_fs.REMOVAL_FAILED,
                    error=OSError("held open"),
                )
            return real_remove(parent_fd, name, expect=expect)

        monkeypatch.setattr(pinned_fs, "remove_dir_verified", _refuse_the_batch)
        session_storage._remove_emptied_batch(batch, "the empty batch", expect=_identity(batch))

        assert (batch / session_storage.MANIFEST_NAME).read_text() == original
        debris = [p.name for p in root.iterdir() if p.name != _BATCH]
        assert len(debris) == 1, f"the redundant copy is kept, not unlinked: {debris!r}"
        assert (root / debris[0]).read_text() == original

    def test_the_primitive_keeps_its_deferred_entry_when_the_tree_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "tree"
        root.mkdir()
        (root / "index").write_text("listing")
        (root / "stuck").write_text("x")

        real_unlink = os.unlink

        def _refuse(path: object, **kwargs: object) -> None:
            if path == "stuck":
                raise OSError("held open")
            real_unlink(path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "unlink", _refuse)
        outcome = pinned_fs.remove_tree_pinned(
            str(root), what="the tree", approve=_approve_anything, keep_until_empty="index"
        )

        assert outcome.removed is False
        assert outcome.survivors == 1
        assert (root / "index").read_text() == "listing"
        assert (root / "stuck").read_text() == "x"

    def test_the_deferred_entry_is_removed_with_the_tree_when_it_goes(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        (root / "a").mkdir(parents=True)
        (root / "a" / "leaf").write_text("x")
        (root / "index").write_text("listing")

        outcome = pinned_fs.remove_tree_pinned(
            str(root), what="the tree", approve=_approve_anything, keep_until_empty="index"
        )

        assert outcome.removed is True
        assert not root.exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == [], "no debris left behind"


class TestTheApprovalReadsOneFile:
    """The header and the listing must describe ONE manifest, bound to the scanned inode.

    Two reads is the defect: a manifest replaced in between passes the header check on the
    first file while the listing that decides which staged files may be deleted comes from
    the second, so a forged listing can name an unlisted staged file and have it destroyed.
    """

    def test_a_manifest_that_is_not_the_scanned_file_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])

        fd = os.open(str(batch), pinned_fs.dir_flags())
        try:
            seen = pinned_fs.scan_tree_pinned(fd, device=os.fstat(fd).st_dev)
            # Same name, an inode the scan did not record - which is what a manifest
            # replaced between the scan and the read looks like from here.
            forged = pinned_fs.PinnedTree(
                dirs={},
                files={
                    (session_storage.MANIFEST_NAME,): seen.files[(session_storage.MANIFEST_NAME,)]
                    + 1
                },
                links={},
            )
            assert (
                session_storage._approve_emptied_batch(batch, fd, forged)
                == session_storage.SKIP_UNREADABLE
            )
            # The real one still passes, so the check is binding rather than always-refusing.
            assert session_storage._approve_emptied_batch(batch, fd, seen) is None
        finally:
            os.close(fd)

    def test_a_manifest_that_is_not_valid_utf8_is_refused_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Undecodable bytes withhold the removal instead of escaping as an exception.

        The pinned read exists to catch a manifest replaced after the by-path pre-screen, so
        it is the one read that meets content the pre-screen never saw. ``UnicodeDecodeError``
        is a ``ValueError``, so the ``except OSError`` that turns an unreadable manifest into
        a refusal does not cover it, and on the restore path an escape would abort AFTER the
        user's files were already moved back.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        (batch / session_storage.MANIFEST_NAME).write_bytes(b'{"schema": "\xff\xfe not utf8"}\n')

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            removed = session_storage._remove_emptied_batch(
                batch, "the restored batch", expect=_identity(batch)
            )

        assert removed is False
        assert batch.is_dir(), "the batch stays, so a user can still see and act on it"
        assert "not valid UTF-8" in caplog.text

    def test_a_batch_with_no_manifest_is_refused(self, tmp_path: Path) -> None:
        batch = tmp_path / "trash" / _BATCH
        batch.mkdir(parents=True)

        fd = os.open(str(batch), pinned_fs.dir_flags())
        try:
            seen = pinned_fs.scan_tree_pinned(fd, device=os.fstat(fd).st_dev)
            assert (
                session_storage._approve_emptied_batch(batch, fd, seen)
                == session_storage.SKIP_UNREADABLE
            )
        finally:
            os.close(fd)


class TestPutBackNoClobber:
    """The undo half of "move it aside, remove the tree, put it back"."""

    def test_it_falls_back_to_a_copy_where_the_filesystem_has_no_links(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``os.link in os.supports_dir_fd`` cannot answer this, which is the whole point.

        That probe tests whether the OS accepts ``dir_fd``, not whether the MOUNT supports
        hard links - so a filesystem without them passes the probe and then refuses the call.
        A guard built on the probe fails exactly where it matters, so the recovery carries an
        ``O_CREAT | O_EXCL`` copy instead of trusting it.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        original = (batch / session_storage.MANIFEST_NAME).read_text()

        def _no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "the filesystem has no hard links")

        real_remove = pinned_fs.remove_dir_verified

        def _refuse_the_batch(
            parent_fd: int, name: str, *, expect: tuple[int, int]
        ) -> pinned_fs.StagedRemoval:
            if name == _BATCH:
                return pinned_fs.StagedRemoval(
                    removed=False, reason=pinned_fs.REMOVAL_FAILED, error=OSError("held open")
                )
            return real_remove(parent_fd, name, expect=expect)

        monkeypatch.setattr(os, "link", _no_links)
        monkeypatch.setattr(pinned_fs, "remove_dir_verified", _refuse_the_batch)
        session_storage._remove_emptied_batch(batch, "the empty batch", expect=_identity(batch))

        assert (batch / session_storage.MANIFEST_NAME).read_text() == original
        debris = [p.name for p in root.iterdir() if p.name != _BATCH]
        assert len(debris) == 1, f"the copy is kept, not unlinked: {debris!r}"
        assert (root / debris[0]).read_text() == original

    def test_a_file_that_took_the_name_is_not_overwritten(self, tmp_path: Path) -> None:
        (tmp_path / "aside").mkdir()
        (tmp_path / "aside" / "debris").write_text("the moved copy")
        (tmp_path / "tree").mkdir()
        (tmp_path / "tree" / "index").write_text("arrived while it was out")

        src = os.open(str(tmp_path / "aside"), pinned_fs.dir_flags())
        dst = os.open(str(tmp_path / "tree"), pinned_fs.dir_flags())
        try:
            ino = os.stat("debris", dir_fd=src, follow_symlinks=False).st_ino
            assert (
                pinned_fs.put_back_no_clobber(src, dst, "debris", "index", expect_ino=ino)
                == pinned_fs.PUT_BACK_NAME_TAKEN
            )
        finally:
            pinned_fs.close_all((src, dst))

        assert (tmp_path / "tree" / "index").read_text() == "arrived while it was out"
        assert (tmp_path / "aside" / "debris").read_text() == "the moved copy"

    def test_a_partial_copy_is_removed_but_only_if_it_is_still_ours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-written file must go -- and only while the name still holds the one we made.

        Half a file is worse than none: it carries some of the content and silently drops the
        rest. But the cleanup addresses a NAME, so a replacement that arrived at it in between
        is not ours to delete, and unlinking by name alone would destroy someone's only copy.
        The write is made to fail here, once with nothing else touching the name and once with
        the name swapped in the same instant, so both halves are exercised.
        """
        (tmp_path / "aside").mkdir()
        (tmp_path / "aside" / "debris").write_text("the moved copy")
        (tmp_path / "tree").mkdir()

        def _no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "the filesystem has no hard links")

        real_write = os.write
        swap: list[bool] = []

        def _fail_the_write(fd: int, data: bytes) -> int:
            if swap:
                # The race, made deterministic: something else takes the name in the same
                # instant the write fails.
                other = tmp_path / "arrived"
                other.write_text("someone else's only copy")
                os.replace(other, tmp_path / "tree" / "index")
            raise OSError(errno.EIO, "the write failed")

        src = os.open(str(tmp_path / "aside"), pinned_fs.dir_flags())
        dst = os.open(str(tmp_path / "tree"), pinned_fs.dir_flags())
        try:
            ino = os.stat("debris", dir_fd=src, follow_symlinks=False).st_ino
            monkeypatch.setattr(os, "link", _no_links)
            monkeypatch.setattr(os, "write", _fail_the_write)

            assert (
                pinned_fs.put_back_no_clobber(src, dst, "debris", "index", expect_ino=ino)
                == pinned_fs.PUT_BACK_FAILED
            )
            monkeypatch.setattr(os, "write", real_write)
            assert not (tmp_path / "tree" / "index").exists(), "our own partial file goes"

            swap.append(True)
            monkeypatch.setattr(os, "write", _fail_the_write)
            assert (
                pinned_fs.put_back_no_clobber(src, dst, "debris", "index", expect_ino=ino)
                == pinned_fs.PUT_BACK_FAILED
            )
        finally:
            monkeypatch.setattr(os, "write", real_write)
            pinned_fs.close_all((src, dst))

        assert (
            tmp_path / "tree" / "index"
        ).read_text() == "someone else's only copy", "a replacement is not ours to delete"

    def test_a_partial_copy_that_cannot_be_unlinked_is_left_empty_not_partial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The content is dropped through the DESCRIPTOR, before any name is addressed.

        The unlink that follows can only ever narrow a two-syscall window - POSIX has no
        "unlink if this inode" - so the guarantee that no partial content survives cannot rest
        on it. Here the unlink is refused outright, which is what a leftover would look like:
        the file at the name must still be empty rather than carrying half a manifest, because
        half a manifest lists some sessions and silently drops the rest.
        """
        (tmp_path / "aside").mkdir()
        (tmp_path / "aside" / "debris").write_text("entry-one\nentry-two\nentry-three\n")
        (tmp_path / "tree").mkdir()

        def _no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "the filesystem has no hard links")

        real_write = os.write

        def _write_then_fail(fd: int, data: bytes) -> int:
            real_write(fd, data[: len(data) // 2])
            raise OSError(errno.ENOSPC, "no space left on device")

        def _refuse_unlink(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EPERM, "not permitted")

        src = os.open(str(tmp_path / "aside"), pinned_fs.dir_flags())
        dst = os.open(str(tmp_path / "tree"), pinned_fs.dir_flags())
        try:
            ino = os.stat("debris", dir_fd=src, follow_symlinks=False).st_ino
            monkeypatch.setattr(os, "link", _no_links)
            monkeypatch.setattr(os, "write", _write_then_fail)
            monkeypatch.setattr(os, "unlink", _refuse_unlink)

            assert (
                pinned_fs.put_back_no_clobber(src, dst, "debris", "index", expect_ino=ino)
                == pinned_fs.PUT_BACK_FAILED
            )
        finally:
            pinned_fs.close_all((src, dst))

        left = tmp_path / "tree" / "index"
        assert left.exists(), "the refused unlink is the point of this case"
        assert left.read_bytes() == b"", f"partial content survived: {left.read_bytes()!r}"

    def test_a_swapped_source_name_is_refused_rather_than_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The moved-aside name is UNTRUSTED by the time the recovery runs.

        It is a name in a directory an actor may be able to write to, and this path only runs
        because something already failed -- so time has passed. Without an inode check and
        ``O_NOFOLLOW``, a name swapped for a symbolic link is followed and whatever it points
        at gets copied into the destination under a name the caller treats as its own. A
        credential is the obvious thing to point it at.

        Hard links are disabled so the COPY path runs, which is where the source-side guard
        is load-bearing: the link path has its own defences (``follow_symlinks=False`` plus a
        check of what landed), so with links available this swap is caught there instead and
        the source check is never reached.
        """
        (tmp_path / "aside").mkdir()
        (tmp_path / "aside" / "debris").write_text("the moved copy")
        (tmp_path / "tree").mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("a credential this code has never read")

        def _no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "the filesystem has no hard links")

        src = os.open(str(tmp_path / "aside"), pinned_fs.dir_flags())
        dst = os.open(str(tmp_path / "tree"), pinned_fs.dir_flags())
        try:
            ino = os.stat("debris", dir_fd=src, follow_symlinks=False).st_ino
            # The swap: same name, now a link pointing outside.
            os.unlink("debris", dir_fd=src)
            os.symlink(str(secret), "debris", dir_fd=src)
            monkeypatch.setattr(os, "link", _no_links)
            assert (
                pinned_fs.put_back_no_clobber(src, dst, "debris", "index", expect_ino=ino)
                == pinned_fs.PUT_BACK_FAILED
            )
        finally:
            pinned_fs.close_all((src, dst))

        assert not (tmp_path / "tree" / "index").exists(), "nothing may be placed from a swap"
        assert secret.read_text() == "a credential this code has never read"


class TestTheCoarsePlatform:
    """Where there is no descriptor to bind to, these two paths still remove the batch.

    They go through the rename-verify-remove the explicit empty already uses on this
    platform: the batch is renamed aside inside the trash root, its identity is checked
    THERE, and only the staged name is removed. Refusing outright was an earlier answer and
    it was wrong, because this branch is the whole of Windows: it left a batch behind after
    every restore and every rolled-back move, still listing sessions it no longer held.
    The residual the rename accepts is the one this platform already accepts for an empty.
    """

    def test_the_batch_is_removed_through_a_verified_staged_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        identity = _identity(batch)
        (batch / "cli").mkdir()

        monkeypatch.setattr(session_storage, "_FD_SAFE_DELETE", False)
        assert session_storage._remove_emptied_batch(batch, "the empty batch", expect=identity)

        assert not batch.exists(), "the cleanup must finish on this platform, not refuse"
        assert root.is_dir()
        assert list(root.iterdir()) == [], "no staging name may be left behind"

    def test_a_batch_that_is_not_the_selected_one_is_put_back_not_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The identity check is what the rename buys, so it is checked on its own.

        A directory answering to the batch's name that is NOT the one the caller recorded
        goes back under that name with its contents intact -- it may be somebody's only copy.
        """
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        stale_identity = (_identity(batch)[0], _identity(batch)[1] + 1)
        (batch / "keep.me").write_text("not the selected batch")

        monkeypatch.setattr(session_storage, "_FD_SAFE_DELETE", False)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            removed = session_storage._remove_emptied_batch(
                batch, "the empty batch", expect=stale_identity
            )

        assert removed is False
        assert (batch / "keep.me").read_text() == "not the selected batch"
        assert list(root.iterdir()) == [batch], "put back under the name the user saw"
        assert "not the directory that was selected" in caplog.text

    def test_no_victim_is_reachable_without_descriptors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The swap the identity check exists to catch, confirmed to reach nothing."""
        root = tmp_path / "trash"
        batch = _batch_with_manifest(root, _BATCH, entries=[])
        identity = _identity(batch)
        victim = tmp_path / "victim"
        (victim / _BATCH).mkdir(parents=True)
        precious = victim / _BATCH / "staged.jsonl"
        precious.write_text("the only copy")

        _swap_parent_for_a_link(root, victim)
        monkeypatch.setattr(session_storage, "_FD_SAFE_DELETE", False)
        session_storage._remove_emptied_batch(batch, "the restored batch", expect=identity)

        assert precious.read_text() == "the only copy"
        assert (victim / _BATCH).is_dir()
        assert not any(p.name.startswith(".") for p in victim.iterdir())


class TestScanTreePinned:
    """The traversal, and the claim the by-name ratchet's exemption rests on."""

    def test_a_direntry_from_a_descriptor_scan_stats_through_it(self, tmp_path: Path) -> None:
        """``os.scandir(<fd>)`` entries answer through the descriptor, not by path.

        The scan asks ``entry.is_symlink()`` and ``entry.is_dir(follow_symlinks=False)``
        rather than ``os.stat(name, dir_fd=fd)``, which the pinned-module ratchet would
        otherwise read as a name-based question. This is why that reading is wrong, pinned
        as behaviour: a symbolic link forces a real stat (the directory block reports only
        that it is a link), ``entry.path`` is the bare name, and the working directory is
        one where that name does not exist -- so an answer at all proves the stat went
        through the iterator's descriptor.
        """
        real = tmp_path / "elsewhere"
        real.mkdir()
        holder = tmp_path / "holder"
        holder.mkdir()
        (holder / "link").symlink_to(real, target_is_directory=True)

        fd = os.open(str(holder), pinned_fs.dir_flags())
        try:
            entry = next(iter(os.scandir(fd)))
            assert entry.path == "link"
            assert not Path.cwd().joinpath("link").exists()
            assert entry.is_symlink() is True
            assert entry.is_dir() is True
        finally:
            os.close(fd)

    def test_it_records_dirs_files_and_links_separately(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        (root / "a").mkdir(parents=True)
        (root / "a" / "leaf").write_text("x")
        (root / "top").write_text("y")
        (root / "dangling").symlink_to(tmp_path / "nowhere")

        fd = os.open(str(root), pinned_fs.dir_flags())
        try:
            tree = pinned_fs.scan_tree_pinned(fd, device=os.fstat(fd).st_dev)
        finally:
            os.close(fd)

        assert set(tree.dirs) == {("a",)}
        assert set(tree.files) == {("top",), ("a", "leaf")}
        assert set(tree.links) == {("dangling",)}

    def test_a_directory_on_another_device_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mount arriving underneath is not a link, so only the device catches it."""
        root = tmp_path / "tree"
        (root / "elsewhere").mkdir(parents=True)

        fd = os.open(str(root), pinned_fs.dir_flags())
        try:
            with pytest.raises(OSError, match="another device"):
                pinned_fs.scan_tree_pinned(fd, device=os.fstat(fd).st_dev + 1)
        finally:
            os.close(fd)


class TestRemoveDirVerified:
    """The primitive the three former inline spellings now share."""

    def test_it_removes_the_directory_it_was_given(self, tmp_path: Path) -> None:
        (tmp_path / "gone").mkdir()
        parent_fd = os.open(str(tmp_path), pinned_fs.dir_flags())
        try:
            info = os.stat(str(tmp_path / "gone"), follow_symlinks=False)
            outcome = pinned_fs.remove_dir_verified(
                parent_fd, "gone", expect=(info.st_dev, info.st_ino)
            )
        finally:
            os.close(parent_fd)

        assert outcome.removed is True
        assert outcome.staged_name is None
        assert not (tmp_path / "gone").exists()

    def test_a_swapped_name_is_refused_and_left_under_the_staging_name(
        self, tmp_path: Path
    ) -> None:
        """The swap this primitive exists for, and the rename-back that must NOT happen.

        Putting the impostor back would write to a name the refusal just proved is not
        ours -- and POSIX rename replaces its destination, so it would destroy whatever
        now answers to it. So a bystander is placed at the original name and has to
        survive.
        """
        (tmp_path / "approved").mkdir()
        approved = os.stat(str(tmp_path / "approved"), follow_symlinks=False)
        (tmp_path / "approved").rename(tmp_path / "moved-away")
        (tmp_path / "approved").mkdir()

        parent_fd = os.open(str(tmp_path), pinned_fs.dir_flags())
        try:
            outcome = pinned_fs.remove_dir_verified(
                parent_fd,
                "approved",
                expect=(approved.st_dev, approved.st_ino),
            )
        finally:
            os.close(parent_fd)

        assert outcome.removed is False
        assert outcome.reason == pinned_fs.REMOVAL_IDENTITY_CHANGED
        assert outcome.staged_name is not None
        # The impostor is under the unguessable name, and nothing was deleted.
        assert (tmp_path / outcome.staged_name).is_dir()
        assert (tmp_path / "moved-away").is_dir()

    def test_a_non_empty_directory_is_put_back(self, tmp_path: Path) -> None:
        """``rmdir`` failing is the one case the rename-back is correct for."""
        (tmp_path / "full").mkdir()
        (tmp_path / "full" / "child").write_text("x")
        info = os.stat(str(tmp_path / "full"), follow_symlinks=False)

        parent_fd = os.open(str(tmp_path), pinned_fs.dir_flags())
        try:
            outcome = pinned_fs.remove_dir_verified(
                parent_fd, "full", expect=(info.st_dev, info.st_ino)
            )
        finally:
            os.close(parent_fd)

        assert outcome.removed is False
        assert outcome.reason == pinned_fs.REMOVAL_FAILED
        # Put back, which is exactly what "no staging name to report" means.
        assert outcome.staged_name is None
        assert (tmp_path / "full" / "child").read_text() == "x"


class TestRemoveTreePinned:
    """The composition, including its two refusals."""

    def test_it_removes_a_whole_tree(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "leaf").write_text("x")
        (root / "top").write_text("y")
        (root / "link").symlink_to(tmp_path / "elsewhere")

        outcome = pinned_fs.remove_tree_pinned(
            str(root), what="the tree", approve=_approve_anything
        )

        assert outcome.removed is True
        assert not root.exists()

    def test_a_withheld_approval_removes_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        (root / "a").mkdir(parents=True)
        (root / "a" / "leaf").write_text("x")
        seen: list[int] = []

        def _withhold(root_fd: int, tree: pinned_fs.PinnedTree) -> str:
            seen.append(len(tree.files))
            return "not-mine"

        outcome = pinned_fs.remove_tree_pinned(str(root), what="the tree", approve=_withhold)

        assert outcome.removed is False
        assert outcome.reason == "not-mine"
        assert seen == [1]
        assert (root / "a" / "leaf").read_text() == "x"

    def test_a_target_that_is_a_link_is_refused(self, tmp_path: Path) -> None:
        """``O_NOFOLLOW`` on the final component, which is what a re-joined name buys."""
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "keep").write_text("x")
        (tmp_path / "as-link").symlink_to(tmp_path / "real", target_is_directory=True)

        with pytest.raises(pinned_fs.PinnedPathRefusal):
            pinned_fs.remove_tree_pinned(
                str(tmp_path / "as-link"), what="the tree", approve=_approve_anything
            )

        assert (tmp_path / "real" / "keep").read_text() == "x"

    def test_a_survivor_is_reported_rather_than_claimed_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tree that would not go must not report success.

        The unlink is made to fail so the closing scan finds the file still there. Left
        as a report rather than an exception because a caller keeps such a directory
        listed instead of claiming it was emptied.
        """
        root = tmp_path / "tree"
        root.mkdir()
        (root / "stuck").write_text("x")

        real_unlink = os.unlink

        def _refuse(path: object, **kwargs: object) -> None:
            if path == "stuck":
                raise OSError("held open")
            real_unlink(path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "unlink", _refuse)
        outcome = pinned_fs.remove_tree_pinned(
            str(root), what="the tree", approve=_approve_anything
        )

        assert outcome.removed is False
        assert outcome.survivors == 1
        assert outcome.reason == pinned_fs.REMOVAL_FAILED
        assert (root / "stuck").read_text() == "x"
