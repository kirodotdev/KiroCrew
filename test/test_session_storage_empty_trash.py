"""Session storage: emptying the trash, its approval and its manifest.

``TestEmptyTrashApproval`` pins what one call may delete and what it reports: the approval
binds each batch's identity, listing and size, and a batch it cannot verify is refused
rather than dropped. ``TestEmptyTrashManifest`` pins the manifest's own step: a link at
its name is refused, not unlinked or followed, a file substituted at its name is not
destroyed, and putting it back never overwrites a manifest that arrived meanwhile.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path, PurePosixPath

import pytest
from session_storage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _DAY,
    _NOW,
    _cli_half,
    _fresh_scan_cache,
    _index,
    stores_fixture,
)

from kiro_crew import session_storage
from kiro_crew.session_storage import SessionStorageError


class TestEmptyTrashApproval:
    """What one ``empty_trash`` call may delete, and what it reports."""

    def test_frees_the_staged_bytes(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        size = _cli_half(kiro_home, "aaaa1111", log_bytes=4096, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        freed = session_storage.empty_trash()

        assert freed >= size
        assert session_storage.list_trash() == []
        assert not (session_storage.trash_root() / batch.batch_id).exists()

    def test_reports_progress_that_ends_at_what_it_returns(self, stores: tuple[Path, Path]) -> None:
        """Progress must be a running total of the WHOLE call, not per batch.

        A screen draws a bar against one denominator, so a figure that restarts on
        each batch would march to the end and then jump backwards. The last value
        reported is also the returned total, which is what lets a client stop
        polling on the callback rather than waiting for a separate confirmation.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=4096, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=2048, age_days=40)
        first = session_storage.move_to_trash(
            ["aaaa1111"], reason="a", index=_index(), now=_NOW - _DAY
        )
        second = session_storage.move_to_trash(["bbbb2222"], reason="b", index=_index(), now=_NOW)

        seen: list[int] = []
        freed = session_storage.empty_trash(
            [first.batch_id, second.batch_id], on_progress=seen.append
        )

        assert seen, "a delete that frees bytes must report at least once"
        assert seen == sorted(seen), "a running total cannot go down"
        assert seen[-1] == freed
        assert freed >= 4096 + 2048
        assert session_storage.list_trash() == []

    def test_a_refused_batch_reports_nothing_it_did_not_delete(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The refusal path must not report bytes: nothing was freed."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        (session_storage.trash_root() / batch.batch_id / "cli" / "cccc3333.jsonl").write_bytes(
            b"ONLY COPY"
        )

        seen: list[int] = []
        skips: list[str] = []
        freed = session_storage.empty_trash(
            [batch.batch_id], on_progress=seen.append, on_skip=skips.append
        )

        assert freed == 0
        assert seen == []
        # And it SAYS why. Reporting only "0 bytes freed" made a batch deliberately
        # kept indistinguishable from an empty one, with the reason in a log the
        # user cannot read.
        assert skips == [session_storage.SKIP_UNLISTED_FILES]

    def test_the_approval_binds_identity_and_size_to_one_directory(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A swap between the listing and the snapshot must not be approved.

        `list_trash()` reads each manifest by path and takes no lock, so its byte totals
        describe whatever answered to that name then. Recording the identity separately, by
        the same name, could straddle a swap -- the approval would carry the REPLACEMENT's
        identity with the original's numbers, and the delete, which checks the identity it
        was handed, would then destroy session data the user was never shown.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        root = session_storage.trash_root()
        staged = root / batch.batch_id

        # A different batch, with different contents, takes the selected one's name after
        # the listing has been read.
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        other = session_storage.move_to_trash(
            ["bbbb2222"], reason="manual", index=_index(), now=_NOW
        )
        impostor = root / other.batch_id
        real_list = session_storage.list_trash
        swapped: list[bool] = []

        def list_then_swap():  # type: ignore[no-untyped-def]
            out = real_list()
            if not swapped:
                swapped.append(True)
                staged.rename(root / "aside")
                impostor.rename(staged)
            return out

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "list_trash", list_then_swap)
            with pytest.raises(session_storage.SessionStorageError, match="cannot be verified"):
                session_storage.staged_targets([batch.batch_id])

        assert swapped == [True], "the swap must land inside the window under test"
        # The manifest header must name the batch it was read from, not just carry an
        # identity and a size describing one directory -- the impostor's. That way a
        # different batch renamed into the selected name is refused outright rather than
        # approved self-consistently under the wrong id.
        assert (staged / session_storage.MANIFEST_NAME).is_file(), "and it is left alone"

    def test_a_header_with_no_batch_id_is_refused_rather_than_waved_through(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A missing id must not read as "nothing to disagree with".

        `_write_header` always writes `batch_id` beside `schema`, and a summary is only
        returned when the schema is the current one -- so within what reaches the approval the
        id is always present, and its absence means the header was tampered with. Tolerating
        that is the fail-OPEN reading, and it is the one an actor gets to choose: strip the
        field and a swapped batch walks through the check.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME

        lines = manifest.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        del header["batch_id"]
        lines[0] = json.dumps(header)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert session_storage._approve_batch(staged) is None, "a header with no id is not proof"

    def test_a_directory_swapped_between_the_listing_and_the_open_is_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The scan that PRODUCES the approval has a check-to-use window of its own.

        `scandir` lists a name and the open happens a moment later. A rename in between means
        the inode recorded for that name belongs to the replacement -- so the approval blesses
        the impostor, and the delete, validating faithfully against that map, removes it. Every
        other guard in this module is downstream of this map, which is why it cannot be the one
        place that trusts a name.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        listed = session_storage._manifest_rels(staged)
        holder = staged / PurePosixPath(listed[0]).parts[0]

        impostor = kiro_home / "not-approved"
        impostor.mkdir()
        real_open = os.open
        swapped: list[bool] = []

        def open_after_swap(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            # The listing has already been materialised by the time the loop opens a child, so
            # swapping HERE lands between the dirent and the descriptor. Hooking `scandir`
            # instead swaps before `list(entries)` runs, which makes both sides see the
            # impostor and agree -- a test that proves nothing.
            if not swapped and path == holder.name:
                swapped.append(True)
                holder.rename(staged.parent / "aside")
                impostor.rename(holder)
            return real_open(path, flags, *args, **kwargs)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "open", open_after_swap)
            approved = session_storage._approve_batch(staged)

        assert swapped == [True], "the swap must land inside the window under test"
        assert approved is None, "an approval built on a swapped directory is not an approval"

    def test_a_listed_file_replaced_after_approval_is_not_unlinked(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The per-file check has to answer to the approval, not to the delete's own scan.

        The delete verifies each staged file's identity, but against a map its own scan
        built -- which is self-consistent and authorises nothing. A listed file replaced
        during the handoff had its replacement's inode recorded, matched, and was unlinked:
        an unapproved file, whose only copy it may be, destroyed on consent given for a
        different one. The approval now carries the file identities and the whole map is
        compared, so the batch is refused instead.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        listed = session_storage._manifest_rels(staged)
        victim = staged / PurePosixPath(listed[0])
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])

        # The approval is taken. Now a DIFFERENT file answers to the same staged name --
        # same size, so nothing but the identity gives it away.
        #
        # The replacement is created ALONGSIDE the original and renamed over it, rather than
        # unlinking and rewriting in place. Unlinking first frees the inode, and a filesystem
        # is free to hand the same number straight back to the next file -- which is exactly
        # what CI's did, so the maps matched, the delete proceeded, and the test failed
        # against correct code. Renaming a file that existed concurrently guarantees a
        # different inode.
        decoy = staged.parent / "decoy-file"
        decoy.write_bytes(b"X" * 32)
        before = victim.stat().st_ino
        os.replace(decoy, victim)
        assert victim.stat().st_ino != before, "the replacement must really be a different file"

        skips: list[str] = []
        freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert freed == 0
        assert skips == [session_storage.SKIP_IDENTITY_CHANGED]
        assert victim.read_bytes() == b"X" * 32, "the replacement must not be unlinked"

    def test_a_manifest_rewritten_to_list_more_after_approval_is_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The listing is what the delete may remove, so it must be the approved listing.

        The manifest's inode is not enough. Rewritten IN PLACE it keeps that inode, and
        every other identity check still passes because no file changed -- only which files
        the delete believes it may unlink. A file already sitting in the batch, unlisted, is
        refused today; adding it to the listing after the approval would have it deleted as
        if the user had approved it.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        listed = session_storage._manifest_rels(staged)
        holder = PurePosixPath(listed[0]).parts[0]
        # A file nobody listed, present before the approval so the identity maps record it.
        bystander = staged / holder / "not-listed.jsonl"
        bystander.write_bytes(b"ONLY COPY")
        original = manifest.read_bytes()

        ids, _total, identities = session_storage.staged_targets([batch.batch_id])
        # The approval does NOT refuse an unlisted file -- that refusal lives at delete
        # time, which is exactly why the listing has to be pinned: rewrite it and the
        # refusal never fires.
        assert ids == [batch.batch_id]

        # Now the listing is rewritten IN PLACE to claim the bystander, and the batch is
        # handed to the delete with the identity recorded a moment ago.
        lines = original.decode().splitlines()
        lines[-1] = lines[-1].replace(
            '"files": [', f'"files": [{{"rel": "{holder}/not-listed.jsonl", "bytes": 9}}, ', 1
        )
        manifest.write_bytes(("\n".join(lines) + "\n").encode())

        skips: list[str] = []
        freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert freed == 0
        assert skips == [session_storage.SKIP_IDENTITY_CHANGED]
        assert bystander.read_bytes() == b"ONLY COPY", "the newly-listed file must survive"

    def test_a_manifest_rewritten_during_the_approval_does_not_authorize_it(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The approval's own two reads must not straddle a rewrite either.

        The digest closes the gap between approval and delete. It only does so if it
        describes the same manifest the inode maps describe: taken AFTER the interior scan it
        could be a listing rewritten in between, recorded against the old maps -- which
        authorises precisely the file it exists to refuse. So it is captured first, through
        the pinned descriptor, and a rewrite after that leaves it describing the old listing,
        which the delete then refuses.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        listed = session_storage._manifest_rels(staged)
        holder = PurePosixPath(listed[0]).parts[0]
        bystander = staged / holder / "not-listed.jsonl"
        bystander.write_bytes(b"ONLY COPY")
        original = manifest.read_bytes()

        real_scan = session_storage._scan_tree
        rewritten: list[bool] = []

        def scan_then_rewrite(dir_fd, *, device):  # type: ignore[no-untyped-def]
            out = real_scan(dir_fd, device=device)
            # In place, so the manifest's own inode never changes.
            if not rewritten:
                rewritten.append(True)
                lines = original.decode().splitlines()
                lines[-1] = lines[-1].replace(
                    '"files": [',
                    f'"files": [{{"rel": "{holder}/not-listed.jsonl", "bytes": 9}}, ',
                    1,
                )
                manifest.write_bytes(("\n".join(lines) + "\n").encode())
            return out

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_scan_tree", scan_then_rewrite)
            ids, _total, identities = session_storage.staged_targets([batch.batch_id])

        assert rewritten == [True], "the rewrite must land inside the approval"
        skips: list[str] = []
        freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert freed == 0
        assert skips == [session_storage.SKIP_IDENTITY_CHANGED]
        assert bystander.read_bytes() == b"ONLY COPY", "the smuggled file must survive"

    @pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlink semantics")
    def test_a_batch_replaced_by_a_symlink_is_not_approved_as_its_target(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The approval must not resolve THROUGH the batch's own name.

        `Path.resolve()` follows the final component, so a batch directory replaced by a
        symlink resolved to its target and the walk pinned that target: the approval would
        record another directory's identity under this batch's id, and the delete, checking
        the identity it was handed, would destroy session data from outside the trash.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        # A SECOND real batch, with its own valid manifest. That matters: pointing the name
        # at a directory with no manifest would be refused for that reason instead, and the
        # test would pass whether or not the resolution is fixed.
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        other = session_storage.move_to_trash(
            ["bbbb2222"], reason="manual", index=_index(), now=_NOW
        )
        root = session_storage.trash_root()
        staged = root / batch.batch_id
        target = root / other.batch_id
        target_id = os.stat(target, follow_symlinks=False)

        # The first batch's name now points at the second batch's directory.
        staged.rename(root / "aside")
        staged.symlink_to(target)

        approved = session_storage._approve_batch(staged)

        assert approved is None, "a batch whose name is a link must not be approved at all"
        # And nothing recorded the target's identity under the first batch's id.
        if approved is not None:
            assert (approved[0].dev, approved[0].ino) != (target_id.st_dev, target_id.st_ino)

    def test_a_batch_that_cannot_be_approved_is_refused_not_dropped(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Naming a batch that cannot be verified must fail, not report a quiet success.

        Dropping it from the approved set is right -- no identity means no check. Dropping
        it SILENTLY turned "destroy this" into a success for a batch still on screen, which
        is the same bug the missing-id refusal beside it exists to prevent.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id

        real_approve = session_storage._approve_batch

        def refuse_approval(path):  # type: ignore[no-untyped-def]
            if path == staged:
                return None
            return real_approve(path)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_approve_batch", refuse_approval)
            with pytest.raises(session_storage.SessionStorageError, match="cannot be verified"):
                session_storage.staged_targets([batch.batch_id])
        assert staged.is_dir(), "and the batch is still there, which is what it will report"

    def test_the_sweep_reports_an_unverifiable_batch_instead_of_dropping_it(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Emptying everything must not report success for a batch it could not verify.

        A named selection refuses outright. The unnamed sweep cannot: one batch damaged by a
        crash mid-append would make the whole trash un-emptyable, and the delete loop skips
        rather than aborts for that reason. So the batch stays in the id list WITHOUT an
        approval, and the delete refuses an id its approval map does not name -- which turns
        a silent omission into a skip the user can read.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        real_approve = session_storage._approve_batch

        def refuse_approval(path):  # type: ignore[no-untyped-def]
            if path == staged:
                return None
            return real_approve(path)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_approve_batch", refuse_approval)
            # The SWEEP: no ids named, so this must not raise.
            ids, total, identities = session_storage.staged_targets()

        assert ids == [batch.batch_id], "the batch must still be reported to the worker"
        assert batch.batch_id not in identities, "but it carries no approval"
        assert total == 0, "and none of its bytes are promised"

        skips: list[str] = []
        freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert freed == 0
        assert skips == [session_storage.SKIP_UNREADABLE], "the user is told, not misled"
        assert staged.is_dir(), "and nothing was deleted unverified"

    def test_targets_one_batch_and_leaves_the_others(self, stores: tuple[Path, Path]) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        keep = session_storage.move_to_trash(
            ["aaaa1111"], reason="a", index=_index(), now=_NOW - _DAY
        )
        drop = session_storage.move_to_trash(["bbbb2222"], reason="b", index=_index(), now=_NOW)

        session_storage.empty_trash([drop.batch_id])

        assert [b.batch_id for b in session_storage.list_trash()] == [keep.batch_id]

    def test_refuses_a_batch_holding_files_nothing_listed(self, stores: tuple[Path, Path]) -> None:
        """A header-only batch shows zero sessions, so emptying it is not consent."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        # Simulate a crash between the move and the manifest append.
        orphan = staged / "cli" / "cccc3333.jsonl"
        orphan.write_bytes(b"ONLY COPY")

        freed = session_storage.empty_trash([batch.batch_id])

        assert freed == 0
        assert orphan.read_bytes() == b"ONLY COPY"
        assert staged.is_dir()

    def test_refuses_a_batch_id_outside_the_trash_root(self, stores: tuple[Path, Path]) -> None:
        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.empty_trash(["../../sessions"])

    def test_a_symlinked_batch_is_not_followed_out_of_the_root(
        self, stores: tuple[Path, Path], tmp_path: Path
    ) -> None:
        outside = tmp_path / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"do not delete")
        root = session_storage.trash_root()
        root.mkdir(parents=True, exist_ok=True)
        link = root / "20240101T000000-deadbeef"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        # Naming it explicitly is refused: the caller's intent cannot be honoured,
        # and succeeding silently would hide that something planted a link here.
        with pytest.raises(SessionStorageError, match="not a valid batch id"):
            session_storage.empty_trash(["20240101T000000-deadbeef"])

        # But it must not WEDGE the sweep-everything path, or one planted link
        # would make the trash permanently un-emptyable.
        session_storage.empty_trash()

        assert (outside / "keep.txt").is_file()
        assert link.is_symlink()


class TestEmptyTrashManifest:
    """The manifest's own last step, and putting it back without overwriting."""

    @pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlink semantics")
    def test_a_symlinked_manifest_is_refused_rather_than_unlinked(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A symlink at the manifest's name must stop the batch before anything is removed.

        The manifest is excluded from the FILE loop so it can go last -- it is the only
        thing that makes a batch restorable, and `list_trash()` omits a batch without a
        readable one. A symlink at that name is not a file, so it was recorded as a LINK
        instead, and the link loop unlinks every link it recorded: the manifest went early,
        and a batch that then failed to finish (an unwritable directory, a file held open)
        left the user data on disk they could neither see nor restore.

        Refusing rather than deferring is deliberate. The product writes this file with
        `atomic_write`, so a symlink here is not ours, and the entries the approval was
        computed from were read THROUGH it.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        elsewhere = staged.parent / "decoy.jsonl"
        elsewhere.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(elsewhere)

        skips: list[str] = []
        freed = session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert freed == 0
        assert skips == [session_storage.SKIP_UNREADABLE]
        # The link is still there, so the batch still LISTS: whatever survived stays
        # reachable, which is the whole point of keeping the manifest.
        assert manifest.is_symlink()
        # And what it pointed at was never followed.
        assert elsewhere.exists()

    @pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlink semantics")
    def test_the_coarse_path_refuses_a_linked_manifest_too(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal cannot live inside the descriptor branch, because both paths delete.

        On a platform without the descriptor path, `rmtree` would remove the link and leave
        any staged file it could not delete -- a locked one on Windows -- so the batch loses
        its listing and keeps its data. That is the same loss the descriptor path refuses,
        reached by the branch that has no descriptors to reason with.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        elsewhere = staged.parent / "decoy.jsonl"
        elsewhere.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(elsewhere)

        # The coarse branch, on a host that would otherwise take the descriptor one.
        monkeypatch.setattr(session_storage, "_FD_SAFE_DELETE", False)
        skips: list[str] = []
        freed = session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert freed == 0
        assert skips == [session_storage.SKIP_UNREADABLE]
        assert staged.is_dir(), "nothing may be destroyed on the strength of a linked listing"
        assert manifest.is_symlink()

    @pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlink semantics")
    def test_a_manifest_linked_after_the_path_check_is_still_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Two checks, and the second is not decoration.

        The refusal above the platform branch is computed from a PATH, so it cannot see a
        link planted after it. The descriptor path checks the same thing again from its
        pinned scan, which can. Planting the link between the two proves the second one
        earns its place rather than repeating the first.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        elsewhere = staged.parent / "decoy.jsonl"
        real_open = session_storage._open_absolute_nofollow
        planted: list[bool] = []

        def open_then_link(target):  # type: ignore[no-untyped-def]
            out = real_open(target)
            # Runs after the path check AND after the caller's unlisted-files read, so the
            # link appears in the only interval left: the one the pinned scan can still see.
            if not planted:
                planted.append(True)
                elsewhere.write_bytes(manifest.read_bytes())
                manifest.unlink()
                manifest.symlink_to(elsewhere)
            return out

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_open_absolute_nofollow", open_then_link)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert planted == [True], "the link must be planted inside the window under test"
        assert skips == [session_storage.SKIP_UNREADABLE]
        assert manifest.is_symlink(), "the link must not have been unlinked"

    def test_manifest_recovery_never_overwrites_a_manifest_that_arrived_since(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Putting the manifest back must fail rather than replace.

        The manifest is moved aside so the batch can be removed, and renamed back if that
        fails. But POSIX rename REPLACES its destination silently -- the same property the
        debris name is randomised to be safe against -- and this direction needs the
        opposite guarantee. If anything has written a `manifest.jsonl` into the batch since
        ours went aside, renaming over it destroys the only copy of a file this code has
        never read.

        Here the arriving file also makes the batch non-empty, so the removal fails on its
        own and the recovery runs for real.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        arrived = staged / session_storage.MANIFEST_NAME
        real_rename = os.rename
        planted: list[bool] = []

        def rename_then_plant(src, dst, *, src_dir_fd=None, dst_dir_fd=None):  # type: ignore[no-untyped-def]
            out = real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            if src == session_storage.MANIFEST_NAME and not planted:
                planted.append(True)
                arrived.write_bytes(b"NEW LISTING")
            return out

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "rename", rename_then_plant)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert planted == [True], "the file must arrive inside the window under test"
        assert skips == [session_storage.SKIP_INCOMPLETE]
        # The arriving file is intact: recovery refused rather than writing over it.
        assert arrived.read_bytes() == b"NEW LISTING"
        # And the batch's real manifest is still recoverable by hand, under the debris name
        # the log reports.
        assert any(
            p.name.startswith(f".{batch.batch_id}.{session_storage.MANIFEST_NAME}.removing-")
            for p in session_storage.trash_root().iterdir()
        ), "the manifest must still exist somewhere a human can put it back"

    def test_a_file_substituted_at_the_manifests_name_is_not_destroyed(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The final scan has to authenticate the survivor, not just count it.

        Everything after the post-condition treats whatever holds the manifest's name as the
        batch's own manifest: it is renamed aside and, once the batch is gone, the debris is
        unlinked. So a file substituted at that name after the first scan would be accepted
        as "nothing left but the manifest" and then destroyed -- an unapproved file, whose
        only copy it is, deleted for matching a name.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        real_remove = session_storage._remove_scanned_dirs
        swapped: list[bool] = []

        def remove_then_swap(batch_fd, dirs, device, cache):  # type: ignore[no-untyped-def]
            out = real_remove(batch_fd, dirs, device, cache)
            # After the files and directories are gone and before the post-condition scan:
            # a DIFFERENT file now answers to the manifest's name.
            if not swapped:
                swapped.append(True)
                # Written alongside and renamed over, never unlinked-then-rewritten: freeing
                # the inode lets the filesystem hand the same number back, which makes the
                # identity check pass and the test fail against correct code.
                decoy = staged.parent / "decoy-manifest"
                decoy.write_bytes(b"NOT THE MANIFEST")
                os.replace(decoy, manifest)
            return out

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_remove_scanned_dirs", remove_then_swap)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert swapped == [True], "the swap must land inside the window under test"
        assert skips == [session_storage.SKIP_INCOMPLETE], "success must not be reported"
        assert manifest.exists(), "the substitute was destroyed, which is the defect itself"
        assert manifest.read_bytes() == b"NOT THE MANIFEST", "the substitute must survive"
        assert not any(
            p.name.startswith(f".{batch.batch_id}.{session_storage.MANIFEST_NAME}.removing-")
            for p in session_storage.trash_root().iterdir()
        ), "the substitute must not even have been moved aside"

    def test_a_filesystem_without_hard_links_still_gets_its_manifest_back(
        self, stores: tuple[Path, Path]
    ) -> None:
        """`link` is not universally available, and giving up would strand the batch.

        The recovery uses `os.link` so it cannot overwrite a manifest that arrived while ours
        was aside. But a filesystem without hard links refuses `link` outright, and the
        capability probe cannot see that -- it tests whether the OS accepts `dir_fd`, not what
        the MOUNT supports. Failing there would leave the batch holding data with no manifest,
        which is the exact loss this recovery exists to prevent. So it falls back to `rename`,
        and only once the destination is known to be absent, where a rename has nothing to
        destroy.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        original = manifest.read_bytes()
        staged_prefix = f".{batch.batch_id}.removing-"
        real_rmdir = os.rmdir

        def refuse_rmdir(name, *, dir_fd=None):  # type: ignore[no-untyped-def]
            # Force the recovery path: the batch cannot go, so the manifest must come back.
            if name == batch.batch_id or str(name).startswith(staged_prefix):
                raise PermissionError("no")
            return real_rmdir(name, dir_fd=dir_fd)

        def no_hard_links(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError(errno.EPERM, "hard links not supported")

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "rmdir", refuse_rmdir)
            patched.setattr(os, "link", no_hard_links)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert manifest.exists(), "the batch was stranded with no manifest, which is the loss"
        assert manifest.read_bytes() == original, "the batch must be listable again"
        assert not any(
            p.name.startswith(f".{batch.batch_id}.{session_storage.MANIFEST_NAME}.removing-")
            for p in session_storage.trash_root().iterdir()
        ), "and the debris is cleaned up once the manifest is back"

    def test_the_fallback_still_refuses_to_overwrite_an_arriving_manifest(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The hard-link fallback must not become a way back to overwriting.

        Falling back to `rename` is only safe because the destination is checked first. With
        something at the name -- the case `link` reports as EEXIST -- the fallback must leave
        it alone even on a filesystem that has no hard links to report EEXIST with.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        arrived = staged / session_storage.MANIFEST_NAME
        staged_prefix = f".{batch.batch_id}.removing-"
        real_rmdir = os.rmdir
        real_rename = os.rename
        planted: list[bool] = []

        def refuse_rmdir(name, *, dir_fd=None):  # type: ignore[no-untyped-def]
            if name == batch.batch_id or str(name).startswith(staged_prefix):
                raise PermissionError("no")
            return real_rmdir(name, dir_fd=dir_fd)

        def rename_then_plant(src, dst, *, src_dir_fd=None, dst_dir_fd=None):  # type: ignore[no-untyped-def]
            out = real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            if src == session_storage.MANIFEST_NAME and not planted:
                planted.append(True)
                arrived.write_bytes(b"ARRIVED WHILE ASIDE")
            return out

        def no_hard_links(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError(errno.EPERM, "hard links not supported")

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "rmdir", refuse_rmdir)
            patched.setattr(os, "rename", rename_then_plant)
            patched.setattr(os, "link", no_hard_links)
            session_storage.empty_trash([batch.batch_id], on_skip=lambda _r: None)

        assert planted == [True], "the file must arrive inside the window under test"
        assert arrived.read_bytes() == b"ARRIVED WHILE ASIDE", "the fallback must not overwrite"

    def test_a_failed_copy_back_leaves_no_half_written_manifest(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A truncated manifest is worse than none at all.

        The hard-link fallback copies rather than links, so a write can fail part way. A
        manifest holding SOME of its entries lists some sessions and silently drops the rest,
        which reads as a successful listing of a smaller batch -- the user would never know
        the difference. So a failed copy removes what it wrote and reports the failure, which
        at least leaves the debris named in the log.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        staged_prefix = f".{batch.batch_id}.removing-"
        real_rmdir = os.rmdir
        real_write = os.write

        def refuse_rmdir(name, *, dir_fd=None):  # type: ignore[no-untyped-def]
            if name == batch.batch_id or str(name).startswith(staged_prefix):
                raise PermissionError("no")
            return real_rmdir(name, dir_fd=dir_fd)

        def no_hard_links(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError(errno.EPERM, "hard links not supported")

        def fail_mid_write(fd, data):  # type: ignore[no-untyped-def]
            # Let the first chunk land, then fail: that is what leaves a partial file.
            real_write(fd, data[: len(data) // 2])
            raise OSError(errno.ENOSPC, "no space left on device")

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "rmdir", refuse_rmdir)
            patched.setattr(os, "link", no_hard_links)
            patched.setattr(os, "write", fail_mid_write)
            session_storage.empty_trash([batch.batch_id], on_skip=lambda _r: None)

        assert not manifest.exists(), "a partial manifest must not be left behind"
        assert any(
            p.name.startswith(f".{batch.batch_id}.{session_storage.MANIFEST_NAME}.removing-")
            for p in session_storage.trash_root().iterdir()
        ), "and the whole manifest is still recoverable as debris"

    def test_a_file_swapped_after_the_post_condition_is_not_deleted_as_debris(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The last name-addressed step checks what LANDED, since it cannot check first.

        The post-condition verifies the manifest's inode and the rename that follows
        addresses its name -- two syscalls apart, the same interval the leaf unlink has,
        because POSIX has no rename-by-inode either. What can be checked is the result: if
        the debris is not the file that was verified, the rename moved something else, and
        the unlink that ends the successful path would destroy it. It is left as debris
        instead, with both names logged.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("needs the descriptor-bound delete path")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        root = session_storage.trash_root()
        staged = root / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME
        real_scan = session_storage._scan_tree
        calls: list[int] = []

        def scan_then_swap(dir_fd, *, device):  # type: ignore[no-untyped-def]
            out = real_scan(dir_fd, device=device)
            calls.append(1)
            # The SECOND scan is the post-condition. Swapping right after it lands between
            # the inode it verified and the rename that addresses the name.
            if len(calls) == 2:
                # Renamed over rather than unlinked and rewritten, so the inode is certainly
                # different -- see the sibling test: a freed inode can come straight back.
                decoy = root / "decoy-post"
                decoy.write_bytes(b"SOMEONE ELSES FILE")
                os.replace(decoy, manifest)
            return out

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_scan_tree", scan_then_swap)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert len(calls) >= 2, "the swap must land after the post-condition scan"
        assert skips == [session_storage.SKIP_INCOMPLETE], "success must not be reported"
        debris = [
            p
            for p in root.iterdir()
            if p.name.startswith(f".{batch.batch_id}.{session_storage.MANIFEST_NAME}.removing-")
        ]
        assert len(debris) == 1, f"the moved file must survive as debris, saw {debris!r}"
        assert debris[0].read_bytes() == b"SOMEONE ELSES FILE", "it must not be deleted"
