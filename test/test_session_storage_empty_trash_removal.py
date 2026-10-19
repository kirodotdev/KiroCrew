"""Session storage: how emptying the trash removes a batch's files and directories.

Files are named by the manifest, never discovered by a walk; a staged name must be a plain
relative path; the descriptor path removes by descriptor and the coarse path through a
staging rename; and a file, directory or batch swapped in during each window pinned here
is kept, never deleted or followed.
"""

from __future__ import annotations

import json
import os
import shutil
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


class TestEmptyTrashRemoval:
    """How each delete path removes a staged batch, and the swaps it refuses."""

    @pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlink semantics")
    def test_a_file_swapped_onto_a_scanned_link_is_not_unlinked(
        self, stores: tuple[Path, Path]
    ) -> None:
        """ "It is only a link" describes what the scan saw, not what the name holds now.

        The link pass removes recorded links on the reasoning that removing one destroys
        nothing. That holds for the link that was scanned; it does not hold for whatever
        occupies the name by the time the unlink runs. A regular file moved there in
        between is data, and unlinking it would be exactly the loss the file pass's
        identity check exists to prevent -- so the link pass now demands the same thing:
        still a link, and still the one recorded.
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
        planted = holder / "dangling"
        planted.symlink_to(kiro_home / "nowhere")

        real_scan = session_storage._scan_tree
        swapped: list[bool] = []

        def scan_then_swap(dir_fd, *, device):  # type: ignore[no-untyped-def]
            out = real_scan(dir_fd, device=device)
            # After the scan has recorded the link and before the pass unlinks it: the
            # name now holds a file that nothing staged and nothing lists.
            if not swapped:
                swapped.append(True)
                planted.unlink()
                planted.write_bytes(b"ONLY COPY")
            return out

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_scan_tree", scan_then_swap)
            session_storage.empty_trash([batch.batch_id], on_skip=lambda _reason: None)

        assert swapped == [True], "the swap must land inside the window under test"
        assert planted.is_file(), "a file that took a link's name must not be unlinked"
        assert planted.read_bytes() == b"ONLY COPY"

    def test_deletes_only_what_the_manifest_names(self, stores: tuple[Path, Path]) -> None:
        """Files are named by the manifest, never discovered by walking the batch.

        A walk has to decide per entry whether to descend, and on Windows a junction
        is not a symlink -- os.path.islink reports False for one, so os.walk would
        descend into it and unlink the files it points at, outside the trash. Naming
        the files means traversal never happens. Asserted here with a symlinked
        subdirectory, the portable stand-in for that shape.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id

        outside = kiro_home.parent / "precious"
        outside.mkdir()
        victim = outside / "keep.jsonl"
        victim.write_bytes(b"NOT THE TRASH")
        # A LINKED DIRECTORY inside the batch. Note it does not trip the
        # unlisted-file guard: that guard walks for files, and a link to a
        # directory is not one -- which is precisely why the delete must not be the
        # thing that decides whether to descend.
        (staged / "link").symlink_to(outside, target_is_directory=True)

        freed = session_storage.empty_trash([batch.batch_id])

        assert freed >= 64
        assert not staged.exists(), "the batch itself is still removed"
        assert victim.read_bytes() == b"NOT THE TRASH"
        assert outside.is_dir()

    @pytest.mark.parametrize(
        "rel",
        [
            "/etc/victim",  # absolute
            "//tmp/victim",  # POSIX root of TWO slashes, whose part is "//"
            "///tmp/victim",
            "../outside",
            "cli/../../outside",
            "..",
            "",
            ".",
            "C:/windows/x",  # absolute in the other flavour
            "C:\\windows\\x",
            "..\\outside",  # a parent reference only Windows parsing sees
            "\\\\server\\share\\x",
        ],
    )
    def test_a_staged_name_that_is_not_a_plain_relative_path_is_refused(self, rel: str) -> None:
        """One table, because each shape got in by a different spelling.

        `//tmp/victim` is the one that shipped: its first component is `//`, so a
        check against `/` missed it, and an absolute path handed to `os.open` ignores
        `dir_fd` and leaves the batch entirely. The Windows spellings matter because
        the coarse path joins these names with the local `Path`.
        """
        assert session_storage._plain_parts(rel) is None

    @pytest.mark.parametrize("rel", ["cli/a.jsonl", "crew/archive/b.jsonl", "c.json"])
    def test_a_plain_staged_name_is_accepted(self, rel: str) -> None:
        assert session_storage._plain_parts(rel) is not None

    def test_the_size_read_refuses_a_rel_that_escapes_the_batch(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The coarse path's measurement must not stat outside the batch either.

        Where the platform cannot delete by descriptor the batch takes rmtree and the
        bytes come from statting the manifest's names. That read went straight to the
        filesystem, so a tampered entry made it measure - and report as freed - a file
        the delete never touched.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        outside = kiro_home / "big.jsonl"
        outside.write_bytes(b"z" * 100_000)

        listed = session_storage._manifest_rels(staged)
        honest = session_storage._listed_bytes(staged, listed)
        tampered = session_storage._listed_bytes(staged, listed + ["../../big.jsonl"])

        assert tampered == honest, "an escaping rel must contribute nothing"
        assert honest >= 16

    def test_a_stale_but_well_formed_id_is_refused(self, stores: tuple[Path, Path]) -> None:
        """Naming a batch that is not staged is a refusal, not a zero-byte success.

        `_batch_dir` does not require the directory to exist, so an id that was already
        emptied passes it. Measured on the pre-snapshot path: that id reached the delete
        and returned 0 bytes with only a log line, which is the silent outcome this PR
        exists to remove - and filtering it out of the snapshot kept it silent.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        stale = "20260101T000000-deadbeef"

        with pytest.raises(SessionStorageError, match="no longer staged"):
            session_storage.staged_targets([stale])

        # Naming a live batch alongside a stale one is refused too: the request is not
        # partly honoured, because the caller cannot see which half ran.
        with pytest.raises(SessionStorageError, match="no longer staged"):
            session_storage.staged_targets([batch.batch_id, stale])

        # And the live batch alone still resolves, with the identity of the directory it
        # names rather than the name alone.
        ids, total, identities = session_storage.staged_targets([batch.batch_id])
        assert ids == [batch.batch_id] and total > 0
        assert set(identities) == {batch.batch_id}
        staged = session_storage.trash_root() / batch.batch_id
        info = staged.lstat()
        recorded = identities[batch.batch_id]
        assert (recorded.dev, recorded.ino) == (info.st_dev, info.st_ino)
        # And the interior travels with it, because pinning the batch does not pin a
        # rename that happens inside the batch.
        if session_storage._FD_SAFE_DELETE:
            assert recorded.dirs, "the staged directories must be recorded too"

    def test_the_target_snapshot_is_serialized_against_staging(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A batch mid-staging must not be selectable, so it cannot grow before delete.

        `list_trash` does not take the mutation lock, so an unlocked read can see a
        batch whose directory and manifest header exist while its sessions are still
        moving in. Selecting that id makes the delete wait for staging to finish and
        then destroy the FINISHED batch - sessions the user never saw included.

        Asserted on the mechanism rather than by racing staging, which on a quiet
        machine would pass either way: while the snapshot is reading, a FRESH
        descriptor on the same lock file must not be able to take it, which is exactly
        what excludes a concurrent `move_to_trash`.
        """
        pytest.importorskip("fcntl")
        import fcntl

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        lock_path = session_storage.trash_root().parent / session_storage.MUTATION_LOCK_NAME
        observed: list[str] = []
        real_list = session_storage.list_trash

        def probe_then_list():  # type: ignore[no-untyped-def]
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                observed.append("free")
            except OSError:
                observed.append("held")
            finally:
                os.close(fd)
            return real_list()

        monkey = pytest.MonkeyPatch()
        monkey.setattr(session_storage, "list_trash", probe_then_list)
        try:
            ids, total, _identities = session_storage.staged_targets(None)
        finally:
            monkey.undo()

        assert observed == ["held"], "the snapshot must resolve inside the mutation lock"
        assert len(ids) == 1 and total > 0

    def test_a_nul_in_a_manifest_name_cannot_abort_a_half_done_delete(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A NUL is the one bad name that raised ValueError, not OSError.

        JSON can carry `\\u0000`, so a tampered or corrupted manifest can hold one, and
        every `os` call then raises ValueError - which the delete's `except OSError` did
        not catch. It escaped mid-loop, after earlier files were already unlinked, so an
        irreversible operation stopped halfway. Two guards now: the validator refuses the
        name, and the loop treats an unusable name as costing its own file only.
        """
        assert session_storage._plain_parts("a\x00b") is None
        assert session_storage._plain_parts("ok/a\x00b") is None

        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        manifest = staged / session_storage.MANIFEST_NAME

        # Tamper: add an entry the validator has to reject, keeping the real one.
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["files"].append({"rel": "a\x00b"})
        lines[1] = json.dumps(entry)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        skips: list[str] = []
        # The batch still empties; the unusable entry costs nothing but itself.
        session_storage.empty_trash([batch.batch_id], on_skip=skips.append)
        assert not staged.exists()
        assert skips == []

    def test_a_drive_relative_name_is_refused_even_though_it_is_not_absolute(
        self,
    ) -> None:
        """`C:.ssh/id_rsa` has no root, so an absoluteness check alone lets it through.

        Joining it onto the batch on Windows replaces the anchor - pathlib lets a
        right-hand side carrying a drive take over - and resolves it against that
        drive's working directory, so the size read would stat a file outside the batch
        and report that it exists and how big it is.
        """
        for rel in ("C:.ssh/id_rsa", "c:y", "C:/x", "C:\\x"):
            assert session_storage._plain_parts(rel) is None, rel
        # Collateral, and deliberate: Windows parsing reads a POSIX name spelled `a:b`
        # as a drive too. This store names its own files, so it never writes one, and
        # being wrong costs a batch kept as incomplete rather than an escape.
        assert session_storage._plain_parts("a:b/c") is None
        # A colon elsewhere in the name is untouched.
        assert session_storage._plain_parts("ab:c/d") == ("ab:c", "d")

    def test_the_coarse_path_reports_only_bytes_that_went_away(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Windows takes rmtree with `ignore_errors`, which can leave the batch standing.

        The freed figure has to be measured after the attempt, not before it, or a
        locked file is reported as space reclaimed while it is still on disk.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=4096, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            # Force the coarse path, and let rmtree fail the way a lock does: quietly.
            patched.setattr(session_storage, "_FD_SAFE_DELETE", False)
            patched.setattr(session_storage.shutil, "rmtree", lambda *a, **k: None)
            freed = session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert staged.is_dir(), "the batch must still be there for this to mean anything"
        assert freed == 0, "nothing went away, so nothing may be reported as freed"
        assert skips == [session_storage.SKIP_INCOMPLETE]

    def test_a_batch_that_survives_a_coarse_delete_reports_a_reason(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The coarse path re-checks the directory, because rmtree tells it nothing.

        `rmtree(ignore_errors=True)` reports nothing, so a tree it could not remove left
        the batch on the user's screen while the job said the delete had succeeded.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        monkeypatch.setattr(session_storage, "_FD_SAFE_DELETE", False)
        monkeypatch.setattr(session_storage.shutil, "rmtree", lambda *a, **k: None)
        skips: list[str] = []
        session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert (session_storage.trash_root() / batch.batch_id).is_dir()

    def test_every_staged_unlink_is_addressed_by_descriptor(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mechanism, not the outcome: no staged file is removed BY PATH.

        This is what closes the swap race, and it is not observable from the result -
        a path-based unlink deletes the same file on a quiet system. So it is asserted
        directly: every removal names a file relative to an already-open directory
        descriptor, which is a handle to the directory that was opened, not a name
        re-resolved at unlink time. `Path.unlink()` cannot satisfy this, which is why
        a resolve-then-unlink version fails this test.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        calls: list[tuple[str, int | None]] = []
        real_unlink = os.unlink

        def spy(path, *, dir_fd=None):  # type: ignore[no-untyped-def]
            calls.append((str(path), dir_fd))
            return real_unlink(path, dir_fd=dir_fd)

        monkeypatch.setattr(os, "unlink", spy)
        session_storage.empty_trash([batch.batch_id])

        assert calls, "nothing was deleted, so this asserts nothing"
        assert all(fd is not None for _name, fd in calls), calls
        assert all(os.sep not in name for name, _fd in calls), calls

    def test_the_batch_open_refuses_a_linked_ancestor(self, stores: tuple[Path, Path]) -> None:
        """A link ANYWHERE above the batch must fail the open, not be followed.

        `O_NOFOLLOW` constrains only the last component, so opening the batch by path
        left the trash root and everything above it to be re-resolved by the kernel -
        and those are writable by the same user. One swapped to a link after validation
        was followed, and the descriptor held afterwards pointed outside the trash.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = (session_storage.trash_root() / batch.batch_id).resolve()

        # The resolved path opens, which is the normal case.
        parent_fd, batch_fd = session_storage._open_absolute_nofollow(staged)
        os.close(batch_fd)
        os.close(parent_fd)

        # The SAME directory reached through a linked ancestor does not.
        alias = kiro_home.parent / "trash-alias"
        alias.symlink_to(session_storage.trash_root(), target_is_directory=True)
        with pytest.raises(OSError):
            session_storage._open_absolute_nofollow(alias / batch.batch_id)

    def test_a_batch_that_cannot_be_opened_reports_a_reason(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing to open the batch is a refusal, not a zero-byte success.

        Specific to the descriptor path: where the platform takes rmtree there is no
        open step to fail, and that path's own gap is covered by
        `test_a_batch_that_survives_a_coarse_delete_reports_a_reason`.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        real_open = os.open

        def refuse(path, flags, *a, **k):  # type: ignore[no-untyped-def]
            if str(path).endswith(batch.batch_id):
                raise PermissionError("no")
            return real_open(path, flags, *a, **k)

        monkeypatch.setattr(os, "open", refuse)
        skips: list[str] = []
        freed = session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert freed == 0
        assert skips == [session_storage.SKIP_UNREADABLE]
        assert (session_storage.trash_root() / batch.batch_id).is_dir()

    def test_a_batch_swapped_after_selection_is_kept_not_deleted(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The snapshot approves a DIRECTORY, not a name.

        The mutation lock is released between the snapshot the user acted on and the
        worker's open, so a directory moved into that name would be deleted on consent
        given for a different one - sessions the user was never shown. The identity
        travels with the id and is re-checked on the descriptor the removal addresses.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=128, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])
        assert ids == [batch.batch_id]

        staged = session_storage.trash_root() / batch.batch_id
        # Swap: the approved directory is moved aside and an impostor takes its name,
        # carrying its own manifest so every content-level check would pass.
        impostor_source = staged.parent / "impostor"
        staged.rename(impostor_source)
        shutil.copytree(impostor_source, staged)

        skips: list[str] = []
        freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert skips == [session_storage.SKIP_IDENTITY_CHANGED]
        assert freed == 0
        assert staged.is_dir(), "the impostor must be left alone, not destroyed"

    def test_a_manifest_that_lists_itself_cannot_strand_the_batch(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The manifest is not deletable as one of its own entries.

        Same defect as keeping it out of the sweep, by a different route: an entry
        reading `manifest.jsonl` passes the plain-name check, so the per-file loop
        would unlink it before the sweep proved the batch empty - and a batch with no
        readable manifest is omitted from `list_trash()`, so any file that survived
        becomes data the user can neither see nor restore.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id

        # Tamper: the manifest names itself, and one real staged file refuses to go.
        manifest = staged / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        survivor = entry["files"][0]["rel"]
        entry["files"].append({"rel": session_storage.MANIFEST_NAME})
        lines[1] = json.dumps(entry)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        real_unlink = os.unlink
        survivor_name = PurePosixPath(survivor).name

        def refuse_one(name, *, dir_fd=None):  # type: ignore[no-untyped-def]
            if str(name) == survivor_name:
                raise PermissionError("no")
            return real_unlink(name, dir_fd=dir_fd)

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "unlink", refuse_one)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert manifest.is_file(), "the manifest must survive its own entry"
        assert batch.batch_id in [b.batch_id for b in session_storage.list_trash()]

    def test_a_surviving_file_keeps_the_manifest_so_the_batch_stays_restorable(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Deleting the manifest early strands data: visible nowhere, restorable never.

        The manifest is the only thing that makes a batch restorable - `list_trash()`
        omits a batch without a readable one. Removing it before the sweep proved the
        batch empty meant a listed file that survived (unwritable staged directory, a
        file held open) left the user with data on disk they could neither see nor put
        back. So it goes last, and only if nothing else remains.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        listed = session_storage._manifest_rels(staged)
        assert listed

        real_unlink = os.unlink
        survivor = PurePosixPath(listed[0]).name

        def refuse_one(name, *, dir_fd=None):  # type: ignore[no-untyped-def]
            if str(name) == survivor:
                raise PermissionError("no")
            return real_unlink(name, dir_fd=dir_fd)

        skips: list[str] = []
        # A scoped context, NOT monkeypatch.undo(): the `stores` fixture takes the same
        # function-scoped monkeypatch, so undoing here also reverts its KIROCREW_HOME /
        # KIRO_HOME isolation - after which `list_trash()` reads the real data home
        # instead of the temporary one, and the assertion below silently measures
        # someone's actual trash.
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "unlink", refuse_one)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert (staged / session_storage.MANIFEST_NAME).is_file(), "manifest must survive"
        # And the consequence that matters: the batch is still listed, so it can be
        # restored rather than being data the user cannot reach.
        assert batch.batch_id in [b.batch_id for b in session_storage.list_trash()]

    def test_the_descriptor_path_never_removes_by_path(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No step of the descriptor delete resolves a path, including the last one.

        Finishing with `rmtree(batch)` re-resolved the whole prefix, so an ancestor
        swapped at that moment sent the removal outside the trash - the walk above it
        was pinned and the final step was not. Asserted by making any path-based
        removal fail loudly: a normal empty must not need one.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )

        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("the descriptor path must not remove by path")

        monkeypatch.setattr(session_storage.shutil, "rmtree", forbidden)
        real_rmdir = os.rmdir

        def rmdir_by_fd_only(name, *, dir_fd=None):  # type: ignore[no-untyped-def]
            # Scoped to the batch: pytest's own tmp_path teardown also calls rmdir,
            # by path, and has nothing to do with what this asserts.
            if str(name) == batch.batch_id:
                assert dir_fd is not None, f"rmdir({name!r}) resolved a path"
            return real_rmdir(name, dir_fd=dir_fd)

        monkeypatch.setattr(os, "rmdir", rmdir_by_fd_only)
        skips: list[str] = []
        freed = session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == []
        assert freed >= 32
        assert not (session_storage.trash_root() / batch.batch_id).exists()

    def test_a_batch_whose_directory_will_not_go_reports_a_reason(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The descriptor path knows from its own sweep - no second look needed.

        Its files can all go and the final `rmdir` still fail, which is the same
        outcome for the user: a batch still listed after asking for it to be destroyed.

        The removal addresses a staging name rather than the batch id -- the batch is
        renamed and re-pinned first -- so the refusal here has to recognise both, or it
        would stop simulating anything and the test would pass on a batch that was
        happily deleted.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        real_rmdir = os.rmdir
        staged_prefix = f".{batch.batch_id}.removing-"

        def refuse_rmdir(name, *, dir_fd=None):  # type: ignore[no-untyped-def]
            if name == batch.batch_id or str(name).startswith(staged_prefix):
                raise PermissionError("no")
            return real_rmdir(name, dir_fd=dir_fd)

        monkeypatch.setattr(os, "rmdir", refuse_rmdir)
        skips: list[str] = []
        freed = session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert freed >= 16, "the files still went, so the bytes are real"
        assert (session_storage.trash_root() / batch.batch_id).is_dir()

    def test_a_batch_swapped_before_its_removal_is_refused_and_keeps_its_manifest(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The batch's own name gets the same treatment its interior directories get.

        The final scan proves the batch empty by DESCRIPTOR; a removal that addressed a
        NAME instead would let a swap in between remove an empty replacement. That is the
        worst of the name-addressed removals rather than the mildest: by then the
        manifest has already been moved aside, so the real batch is left holding data
        with nothing to list it -- and the caller reports success.

        Here the manifest move is the trigger, which puts the swap exactly in that
        interval.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=16, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        root = session_storage.trash_root()
        decoy = kiro_home / "decoy"
        decoy.mkdir()
        elsewhere = root.parent / "moved-batch"
        real_rename = os.rename
        swapped: list[bool] = []

        def rename_then_swap(src, dst, *, src_dir_fd=None, dst_dir_fd=None):  # type: ignore[no-untyped-def]
            out = real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            # The manifest going aside is the last step before the batch is removed.
            if src == session_storage.MANIFEST_NAME and not swapped:
                swapped.append(True)
                (root / batch.batch_id).rename(elsewhere)
                decoy.rename(root / batch.batch_id)
            return out

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "rename", rename_then_swap)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert swapped == [True], "the swap must land inside the window under test"
        assert skips == [session_storage.SKIP_INCOMPLETE], "success must not be reported"
        # The replacement is untouched -- it was never the batch this approved -- and it is
        # left under the staging name rather than renamed back onto the batch id, because
        # that name is not ours to write to and rename would replace whatever holds it.
        debris = [p for p in root.iterdir() if p.name.startswith(f".{batch.batch_id}.removing-")]
        assert len(debris) == 1, f"the replacement must survive as staged debris, saw {debris}"
        assert debris[0].is_dir()
        # And the real batch got its manifest back, through the descriptor rather than the
        # name, so whatever it still holds stays listed and restorable.
        assert (elsewhere / session_storage.MANIFEST_NAME).is_file()

    def test_a_swapped_directory_component_is_refused_not_followed(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A staged directory that is a link is not followed into a live store.

        The outcome half of the guard above. Pre-swapped rather than raced, so it
        pins the behaviour for a batch that ALREADY holds a link where a directory
        belongs; the race itself is closed structurally by never re-resolving a path.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id

        # Where the manifest says the staged files live.
        listed = session_storage._manifest_rels(staged)
        assert listed, "the batch must list its files for this test to mean anything"
        holder = staged / PurePosixPath(listed[0]).parts[0]
        assert holder.is_dir()

        # A live store with a file of the same staged name, and the batch's own
        # directory replaced by a link to it.
        elsewhere = kiro_home / "elsewhere"
        elsewhere.mkdir()
        for rel in listed:
            victim = elsewhere / PurePosixPath(rel).name
            victim.write_bytes(b"LIVE DATA")
        shutil.rmtree(holder)
        holder.symlink_to(elsewhere, target_is_directory=True)

        session_storage.empty_trash([batch.batch_id])

        for rel in listed:
            assert (
                elsewhere / PurePosixPath(rel).name
            ).read_bytes() == b"LIVE DATA", "a swapped component must not be followed"

    def test_a_directory_renamed_into_a_staged_name_is_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """`O_NOFOLLOW` refuses a LINK; a renamed real directory is not a link.

        Pinning the batch does not pin its interior - the batch inode is unchanged by a
        rename that happens inside it - so moving a live directory onto a staged
        directory's name redirected the per-file unlinks at live files sharing a staged
        name. Each component is now admitted only as the inode its pinned parent
        reported for that name.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = (session_storage.trash_root() / batch.batch_id).resolve()
        listed = session_storage._manifest_rels(staged)
        assert listed
        holder = PurePosixPath(listed[0]).parts[0]

        parent_fd, batch_fd = session_storage._open_absolute_nofollow(staged)
        try:
            device = os.fstat(batch_fd).st_dev
            scanned = session_storage._scan_tree(batch_fd, device=device)
            dirs = scanned.dirs

            # The real staged directory opens against the scan that saw it.
            session_storage._open_chain(batch_fd, (holder,), cache={}, dirs=dirs, device=device)

            # A different REAL directory renamed into that name does not - and it is not
            # a symlink, so O_NOFOLLOW alone would have opened it. Checked against the
            # SAME map, which is the point: re-reading the parent now reports the
            # impostor, so only an identity taken before the swap can refuse it.
            impostor = kiro_home / "live-sessions"
            impostor.mkdir()
            (impostor / "keep.me").write_bytes(b"LIVE DATA")
            (staged / holder).rename(staged / "moved-aside")
            impostor.rename(staged / holder)
            with pytest.raises(OSError):
                session_storage._open_chain(batch_fd, (holder,), cache={}, dirs=dirs, device=device)
        finally:
            os.close(batch_fd)
            os.close(parent_fd)

    def test_a_directory_swapped_after_the_scan_keeps_its_files(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The outcome half: the redirected unlink does not happen at all.

        The swap is made to land in the one window that matters - after the batch was
        opened and scanned, before its files are unlinked - by doing it as the scan
        returns. What the user's approval covered is gone; what was moved in is not
        touched, and the batch is reported as kept.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        listed = session_storage._manifest_rels(staged)
        assert listed
        holder = staged / PurePosixPath(listed[0]).parts[0]

        impostor = kiro_home / "live-sessions"
        impostor.mkdir()
        for rel in listed:
            (impostor / PurePosixPath(rel).name).write_bytes(b"LIVE DATA")
        real_scan = session_storage._scan_tree
        swapped: list[bool] = []

        def scan_then_swap(dir_fd, *, device):  # type: ignore[no-untyped-def]
            tree = real_scan(dir_fd, device=device)
            # Once, right after the scan the approval is compared against, and before the
            # removal reads it. A latch rather than a scan-depth test: the scan is one call
            # per pass, so the pass number is the only thing that distinguishes them.
            if not swapped and holder.is_dir():
                swapped.append(True)
                holder.rename(staged / "moved-aside")
                impostor.rename(holder)
            return tree

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_scan_tree", scan_then_swap)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        for rel in listed:
            assert (
                holder / PurePosixPath(rel).name
            ).read_bytes() == b"LIVE DATA", "the swapped-in directory must not be touched"

    def test_a_late_file_leaves_the_batch_listed_and_restorable(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The manifest and the batch directory go together, or neither goes.

        A file created after the sweep read clear makes the final `rmdir` fail. Unlinking
        the manifest before that point turned the failure into silent loss: the batch
        disappeared from `list_trash()` with that file inside it, unreachable and
        unrestorable. The manifest is moved aside instead, and renamed back when the
        removal does not complete.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        real_remove = session_storage._remove_scanned_dirs

        def remove_then_litter(batch_fd, dirs, device, cache):  # type: ignore[no-untyped-def]
            ok = real_remove(batch_fd, dirs, device, cache)
            # Exactly the race: the directories went, and then something appeared before
            # the batch itself could be removed.
            (staged / "arrived-late.log").write_bytes(b"unaccounted")
            return ok

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_remove_scanned_dirs", remove_then_litter)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert (staged / session_storage.MANIFEST_NAME).is_file(), "manifest must be back"
        assert batch.batch_id in [b.batch_id for b in session_storage.list_trash()]
        # And no debris left behind in the trash root.
        assert [p.name for p in session_storage.trash_root().iterdir()] == [batch.batch_id]

    def test_the_coarse_path_also_refuses_a_swapped_batch(self, stores: tuple[Path, Path]) -> None:
        """Dropping the check on the weakest removal is the wrong way round.

        The coarse path has no descriptor to bind to, so it renames the batch first and
        verifies the RENAMED directory: a swap that happened before the rename is caught,
        the impostor is put back rather than destroyed, and the name finally removed is one
        that existed for microseconds. Refusing outright instead would make emptying the
        Trash impossible on that platform, which is a worse answer than a window an
        attacker has to guess their way into.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])
        recorded = identities[batch.batch_id]
        # What a swap looks like to the check: the id still resolves, the object differs.
        stale = {
            batch.batch_id: session_storage.BatchIdentity(recorded.dev, recorded.ino + 1, None)
        }

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_FD_SAFE_DELETE", False)
            freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=stale)

        assert skips == [session_storage.SKIP_IDENTITY_CHANGED]
        assert freed == 0
        # Back under the name the user saw, so it is still listed and still restorable.
        assert (session_storage.trash_root() / batch.batch_id).is_dir()
        assert [b.batch_id for b in session_storage.list_trash()] == [batch.batch_id]

    def test_the_coarse_path_still_removes_a_batch_it_approves(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The staging rename must not break the ordinary case.

        Renamed, verified, removed under the staging name - and nothing left in the trash
        root afterwards, neither the batch nor the staging directory.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_FD_SAFE_DELETE", False)
            freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert skips == []
        assert freed >= 64
        assert list(session_storage.trash_root().iterdir()) == []

    def test_an_interior_swap_during_the_handoff_is_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The approval fixes the interior too, not just the batch directory.

        This is the real window: the mutation lock is released between the snapshot the
        user acted on and the worker's open, and a map built at DELETE time records
        whatever is there by then - impostor included - so it cannot be what authorises
        the delete. No patching here; the swap simply happens between the two calls, which
        is exactly what an actor with write access to the trash gets for free.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        listed = session_storage._manifest_rels(staged)
        assert listed
        holder = staged / PurePosixPath(listed[0]).parts[0]

        # Approved: the batch as it is now, interior included.
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])

        # The handoff. A directory carrying EXACTLY the staged names takes the staged
        # directory's place - a real directory, so no link check can see it, and its
        # contents match the manifest, so the unlisted-file guard passes. That guard
        # catches the crude version of this (a directory holding anything else); the
        # identity check is what catches the crafted one.
        impostor = kiro_home / "live-sessions"
        impostor.mkdir()
        for rel in listed:
            (impostor / PurePosixPath(rel).name).write_bytes(b"LIVE DATA")
        holder.rename(kiro_home / "moved-aside")
        impostor.rename(holder)

        skips: list[str] = []
        freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert skips == [session_storage.SKIP_IDENTITY_CHANGED]
        assert freed == 0
        for rel in listed:
            assert (holder / PurePosixPath(rel).name).read_bytes() == b"LIVE DATA"

    def test_the_coarse_path_removes_a_staging_name_not_the_approved_one(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The removal must not be handed the name whose identity was checked.

        Checking a path and then handing the SAME path to `rmtree` re-resolves it, so a
        swap in between is followed. The rename removes that: by the time anything is
        removed the approved name does not exist, and the name being removed is one that
        existed for microseconds.

        Asserted structurally rather than by racing. A test that tries to land a swap
        between the check and the removal has to intercept a syscall to find that instant,
        and an earlier attempt at exactly that fired on an unrelated `stat` under a
        different Python. The observable property is "the removal is given a staging name,
        and the approved name is already gone".
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])

        real_remove = session_storage._coarse_remove
        targets: list[Path] = []

        def record(target, rels, on_progress, base_bytes):  # type: ignore[no-untyped-def]
            targets.append(target)
            assert not staged.exists(), "the batch was still sitting at its approved name"
            return real_remove(target, rels, on_progress, base_bytes)

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_FD_SAFE_DELETE", False)
            patched.setattr(session_storage, "_coarse_remove", record)
            freed = session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert skips == [] and freed >= 64
        assert len(targets) == 1
        assert targets[0] != staged, "the removal was given the name it had just checked"
        assert ".removing-" in targets[0].name and batch.batch_id in targets[0].name

    def test_a_deeply_nested_batch_does_not_break_the_walk(self, stores: tuple[Path, Path]) -> None:
        """A deep staged tree must not raise `RecursionError`.

        It is not `OSError`, so it escaped every caller that turns a failed read into a
        refusal and reached the request handler as an unexplained snapshot failure - and
        that handler would answer a named selection by deleting it unchecked. Depth is
        reachable by anything that can write into the trash, so the walk is iterative:
        deep enough still fails, but with `EMFILE`, which is an `OSError` and is handled.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = (session_storage.trash_root() / batch.batch_id).resolve()

        # Nest by descriptor with one-character names, so no long PATH is ever built and
        # the depth is limited by nothing but the fixture. 600 levels is comfortably past
        # where a recursive walk exhausts the interpreter stack and comfortably short of
        # the descriptor limit.
        depth = 600
        parent_fd, batch_fd = session_storage._open_absolute_nofollow(staged)
        fd = batch_fd
        opened = []
        try:
            for _level in range(depth):
                os.mkdir("d", dir_fd=fd)
                fd = os.open("d", session_storage._dir_open_flags(), dir_fd=fd)
                opened.append(fd)
        finally:
            session_storage._close_all(opened)
            os.close(batch_fd)
            os.close(parent_fd)

        # The snapshot walks it, and so does the delete.
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])
        assert ids == [batch.batch_id]
        recorded = identities[batch.batch_id].dirs
        assert recorded is not None and len(recorded) >= depth

        skips: list[str] = []
        session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)
        assert skips == []
        assert not (session_storage.trash_root() / batch.batch_id).exists()

    def test_the_removal_phase_refuses_a_directory_the_approval_never_named(
        self, stores: tuple[Path, Path]
    ) -> None:
        """Removing directories must not discover them, or it deletes what it finds.

        A recursive sweep enumerated children by name and descended into whatever
        answered, so a directory substituted after verification had its links and empty
        directories removed. Removal is driven by the approved map instead, and reaches
        each parent through a descriptor chain that admits only the approved inode.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        listed = session_storage._manifest_rels(staged)
        holder = staged / PurePosixPath(listed[0]).parts[0]
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])

        # A directory nobody approved takes the staged directory's place once the files
        # are gone. EMPTY, which is the case that matters: `rmdir` addresses a name and
        # succeeds on an empty directory, so without a check on the directory itself this
        # is removed on the approval of a different one.
        impostor = kiro_home / "not-approved"
        impostor.mkdir()
        real_remove = session_storage._remove_scanned_dirs

        def swap_then_remove(batch_fd, dirs, device, cache):  # type: ignore[no-untyped-def]
            holder.rename(staged.parent / "aside")
            impostor.rename(holder)
            return real_remove(batch_fd, dirs, device, cache)

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_remove_scanned_dirs", swap_then_remove)
            session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert holder.is_dir(), "a directory the approval never named must be left alone"

    def test_a_directory_swapped_after_its_identity_check_is_not_removed(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The check and the removal must not address the same name.

        The sibling test above swaps BEFORE the removal pass, which the chain check
        catches. This one swaps inside the pass -- after that check has passed and before
        the directory goes -- which is the interval a plain `rmdir` by name leaves open:
        it would remove whatever holds the name by then, on the approval of a different
        directory.

        The removal now renames the name to one nothing can predict and re-checks the
        identity there, so a swap in that interval gets the intruder moved within its own
        parent rather than deleted, and is then put back under the listed name.
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
        holder = staged / PurePosixPath(listed[0]).parts[0]
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])

        impostor = kiro_home / "not-approved"
        impostor.mkdir()
        approved_elsewhere = staged.parent / "aside"
        real_chain = session_storage._open_chain
        real_remove = session_storage._remove_scanned_dirs
        in_removal: list[bool] = []
        swapped: list[bool] = []

        def chain_then_swap(batch_fd, parts, cache, dirs, device):  # type: ignore[no-untyped-def]
            fd = real_chain(batch_fd, parts, cache=cache, dirs=dirs, device=device)
            # ONLY during the removal pass. A file's parent chain names the same key, so
            # hooking every call would swap during the FILE phase instead and end up
            # re-testing the sibling case above, which a different check already covers.
            if in_removal and parts == (holder.name,) and not swapped:
                swapped.append(True)
                holder.rename(approved_elsewhere)
                impostor.rename(holder)
            return fd

        def remove_in_pass(batch_fd, dirs, device, cache):  # type: ignore[no-untyped-def]
            in_removal.append(True)
            try:
                return real_remove(batch_fd, dirs, device, cache)
            finally:
                in_removal.clear()

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_open_chain", chain_then_swap)
            patched.setattr(session_storage, "_remove_scanned_dirs", remove_in_pass)
            session_storage.empty_trash(ids, on_skip=skips.append, expect=identities)

        assert swapped == [True], "the swap must land inside the window under test"
        assert skips == [session_storage.SKIP_INCOMPLETE]
        # The intruder survives, and it survives WHERE IT WAS MOVED TO rather than being
        # renamed back. Renaming it back looked kinder -- the user recognises the listed name
        # -- but POSIX rename replaces its destination, so an actor who swapped the directory
        # and then put something at the listed name would have had that rename destroy it.
        # Not writing to a name that is not ours beats leaving a tidy tree.
        debris = [p for p in staged.iterdir() if p.name.startswith(f".{holder.name}.removing-")]
        assert len(debris) == 1, f"the intruder must survive under the staging name, saw {debris}"
        assert debris[0].is_dir()
        assert not holder.exists(), "and the listed name is left alone, not written over"

    def test_a_directory_is_removed_under_a_name_nothing_can_predict(
        self, stores: tuple[Path, Path]
    ) -> None:
        """The name the removal addresses must not be the one the batch lists.

        The identity re-check above closes the interval between the check and the removal,
        but only because the removal addresses a name nothing else knows. Were it still the
        listed name, the interval between that re-check and the `rmdir` would be open on a
        name an actor with write access to the parent can plant at.
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
        holder = staged / PurePosixPath(listed[0]).parts[0]
        ids, _total, identities = session_storage.staged_targets([batch.batch_id])

        real_rmdir = os.rmdir
        asked: list[str] = []

        def record_rmdir(name, *args, **kwargs):  # type: ignore[no-untyped-def]
            if isinstance(name, str):
                asked.append(name)
            return real_rmdir(name, *args, **kwargs)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(os, "rmdir", record_rmdir)
            session_storage.empty_trash(ids, expect=identities)

        assert holder.name in {PurePosixPath(rel).parts[0] for rel in listed}
        assert holder.name not in asked, "the listed name must not be the one removed"
        assert any(
            n.startswith(f".{holder.name}.removing-") for n in asked
        ), f"expected a staging name for {holder.name!r}, saw {asked!r}"

    def test_a_file_swapped_after_the_scan_is_not_unlinked(self, stores: tuple[Path, Path]) -> None:
        """The leaf is a NAME, so it gets the strongest check a name can carry.

        POSIX has no unlink-by-inode - the stdlib's own `_rmtree_safe_fd` addresses names
        too - so this cannot be closed the way the directories were. What it can do is
        refuse a name that does not denote the object the pinned scan saw, which is the
        interval between that scan and the unlink. The swap here lands inside it.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        staged = session_storage.trash_root() / batch.batch_id
        listed = session_storage._manifest_rels(staged)
        assert listed
        victim = staged / PurePosixPath(listed[0])
        real_scan = session_storage._scan_tree
        swapped: list[bool] = []

        def scan_then_swap(dir_fd, *, device):  # type: ignore[no-untyped-def]
            tree = real_scan(dir_fd, device=device)
            # Once, right after the scan the approval is compared against. A latch rather
            # than a scan-depth test: the scan is one call per pass.
            if not swapped and victim.is_file():
                swapped.append(True)
                # A different file takes the staged file's name, on the same filesystem.
                replacement = kiro_home / "not-approved.jsonl"
                replacement.write_bytes(b"LIVE DATA")
                victim.unlink()
                replacement.rename(victim)
            return tree

        skips: list[str] = []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(session_storage, "_scan_tree", scan_then_swap)
            session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == [session_storage.SKIP_INCOMPLETE]
        assert victim.read_bytes() == b"LIVE DATA", "the substituted file must survive"

    def test_the_manifest_debris_name_cannot_overwrite_a_planted_file(
        self, stores: tuple[Path, Path]
    ) -> None:
        """`os.rename` replaces its destination silently, so the name must be unguessable.

        The manifest is moved to the trash root under a debris name while the batch is
        removed. A deterministic name is one an actor with write access to the trash root
        can plant a file at - and the rename would then destroy that file's only copy
        without a word, which is the exact failure this module exists to prevent.
        """
        if not session_storage._FD_SAFE_DELETE:
            pytest.skip("this platform cannot delete by descriptor; it takes rmtree")

        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=32, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        # The name a deterministic scheme would have used.
        planted = (
            session_storage.trash_root()
            / f".{batch.batch_id}.{session_storage.MANIFEST_NAME}.removing"
        )
        planted.write_bytes(b"LIVE DATA")

        skips: list[str] = []
        session_storage.empty_trash([batch.batch_id], on_skip=skips.append)

        assert skips == []
        assert not (session_storage.trash_root() / batch.batch_id).exists()
        assert planted.read_bytes() == b"LIVE DATA", "the debris rename clobbered a real file"

    def test_a_manifest_rel_that_escapes_the_batch_is_refused(
        self, stores: tuple[Path, Path]
    ) -> None:
        """A tampered manifest cannot aim the delete out of its own batch."""
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        _cli_half(kiro_home, "bbbb2222", log_bytes=8, age_days=40)
        keep = session_storage.move_to_trash(
            ["aaaa1111"], reason="a", index=_index(), now=_NOW - _DAY
        )
        drop = session_storage.move_to_trash(["bbbb2222"], reason="b", index=_index(), now=_NOW)

        # Point one of drop's entries at a file inside the OTHER batch.
        target = session_storage.trash_root() / keep.batch_id / "cli" / "aaaa1111.jsonl"
        assert target.is_file()
        manifest = session_storage.trash_root() / drop.batch_id / "manifest.jsonl"
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["files"].append({"rel": f"../{keep.batch_id}/cli/aaaa1111.jsonl"})
        lines[1] = json.dumps(entry)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

        session_storage.empty_trash([drop.batch_id])

        assert target.is_file(), "a rel outside the batch must not be deleted"
