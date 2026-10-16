"""The ``source_root`` barrier, and a dead live pointer that stays visible.

A link outside ``$HOME`` is readable and writable only under a recorded root
that still verifies as a project; a dead pointer is reported, not masked by the
snapshot; an over-long pointer is rejected; and every consumer of the allowed
roots shares one producer. The shared ``store`` fixture lives in
``artifacts_helpers.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from artifacts_helpers import (  # noqa: F401 -- the fixture is requested by name
    store_fixture,
)

from kiro_crew.artifacts import ArtifactNotFoundError, ArtifactStore, ArtifactValidationError

# ── source_root barrier + visible dead pointer ────────────────────────────────


@pytest.fixture
def home_store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    """A store whose data home sits INSIDE a fake ``$HOME``.

    The default ``store`` fixture is rooted at ``tmp_path/artifacts``, so its
    data-home root is ``tmp_path`` — which would make every path in the test
    tree "already allowed" and hide the barrier under test. Nesting the store
    under a fake home (mirroring production's ``~/.kiro/crew/artifacts``) leaves
    the rest of ``tmp_path`` genuinely outside every default root, standing in
    for ``/workplace/...``.
    """
    home = tmp_path / "home"
    (home / ".kiro" / "crew").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    return ArtifactStore(root=home / ".kiro" / "crew" / "artifacts")


@pytest.fixture
def project_file(tmp_path: Path) -> tuple[Path, Path]:
    """A ``/workplace``-style project root + file, outside home and the data home."""
    proj = tmp_path / "workplace" / "nrb" / "repo"
    src = proj / "docs" / "spec.md"
    src.parent.mkdir(parents=True)
    src.write_text("# live from the project", encoding="utf-8")
    # A recorded root only authorizes a read while it still VERIFIES as a
    # project, so the fixture is a real repo root rather than a bare directory.
    (proj / ".git").mkdir()
    return proj, src


class TestSourceRootBarrier:
    """``source_root`` is what makes a link outside ``$HOME`` readable at all."""

    def test_read_refused_without_recorded_root(self, home_store, project_file) -> None:
        _proj, src = project_file
        # This is the silent breakage: a project file outside $HOME is refused,
        # so the artifact would serve a stale snapshot forever.
        assert home_store._try_read_source_path(str(src)) is None

    def test_read_allowed_with_recorded_root(self, home_store, project_file) -> None:
        proj, src = project_file
        assert home_store._try_read_source_path(str(src), str(proj)) == "# live from the project"

    def test_write_refused_without_recorded_root(self, home_store, project_file) -> None:
        _proj, src = project_file
        assert home_store._try_write_source_path(str(src), "edited") is False
        assert src.read_text(encoding="utf-8") == "# live from the project"

    def test_write_allowed_with_recorded_root(self, home_store, project_file) -> None:
        proj, src = project_file
        assert home_store._try_write_source_path(str(src), "edited", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "edited"

    def test_recorded_root_does_not_widen_other_artifacts(self, home_store, tmp_path) -> None:
        # Recording one project root must not make a SIBLING directory readable.
        proj = tmp_path / "workplace" / "proj-a"
        proj.mkdir(parents=True)
        other = tmp_path / "workplace" / "proj-b" / "secret.md"
        other.parent.mkdir(parents=True)
        other.write_text("not yours", encoding="utf-8")
        assert home_store._try_read_source_path(str(other), str(proj)) is None

    def test_sensitive_path_still_refused_inside_recorded_root(
        self, home_store, tmp_path, monkeypatch
    ) -> None:
        proj = tmp_path / "workplace" / "proj"
        src = proj / ".aws" / "credentials"
        src.parent.mkdir(parents=True)
        src.write_text("SECRET", encoding="utf-8")
        from kiro_crew import artifacts as artifacts_mod

        monkeypatch.setattr(artifacts_mod, "is_sensitive_path", lambda p: p == str(src.resolve()))
        assert home_store._try_read_source_path(str(src), str(proj)) is None

    def test_forged_root_cannot_widen_the_read_boundary(self, home_store, tmp_path) -> None:
        """A persisted root is a hint, never authority.

        ``meta.json`` sits in the agent-writable data home, so a record naming
        ``source_root`` = a filesystem ancestor would otherwise hand the store
        read access to anything under it.
        """
        outside = tmp_path / "etc" / "secrets"
        outside.parent.mkdir(parents=True)
        outside.write_text("SECRET", encoding="utf-8")
        # The forged root is a real directory but is NOT a repo root and was
        # never registered as a project, so it must not authorize the read.
        assert home_store._try_read_source_path(str(outside), str(tmp_path)) is None

    def test_relocate_promotes_a_copy_to_a_live_pointer(self, home_store, project_file) -> None:
        """Relocating a copied artifact must actually attach it to the file.

        Relocate is an explicit "this artifact tracks THIS file" act. Leaving
        ``source_copy_only`` set would make it silently inert -- reads and edits
        would keep ignoring the file the user just pointed at.
        """
        proj, src = project_file
        home_store.create(
            name="Scratch",
            content="# copied",
            slug="scratch",
            kind="markdown",
            source_path="/tmp/scratch.md",
            source_copy_only=True,
        )
        relocated = home_store.relocate("scratch", str(src), str(proj))
        assert relocated.source_copy_only is False
        # And the read is now live.
        src.write_text("# externally edited", encoding="utf-8")
        got = home_store.get("scratch")
        assert got.content == "# externally edited"
        assert got.source_missing is False

    def test_write_works_without_dir_fd_via_a_by_name_atomic_replace(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """No directory-handle APIs (Windows) must not disable mirroring.

        The staged payload is renamed by NAME instead of through a pinned parent.
        That is what every editor's atomic save does, and it keeps the property
        that actually protects data: the file is either all of the old bytes or
        all of the new ones, never a half-write.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        monkeypatch.setattr(_os, "supports_dir_fd", set(), raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "new body"
        # No staging litter left behind on this path either.
        assert not list(src.parent.glob(".*kirocrew-*"))

    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "the pinned-parent re-check lives on the dir-fd path; Windows has no dir_fd "
            "support and takes the validated-descriptor path instead"
        ),
    )
    @pytest.mark.skipif(
        os.rename not in getattr(os, "supports_dir_fd", set()),
        reason="the pinned-parent re-check only exists where directory-handle APIs do",
    )
    def test_write_refuses_when_the_pinned_parent_holds_a_different_file(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """An ancestor swap must not redirect the rename onto another file.

        ``O_NOFOLLOW`` only guards the final path component, so an intermediate
        directory can be replaced between validating the file and opening its
        parent. Re-resolving the basename through the pinned directory fd and
        requiring the same ``(st_dev, st_ino)`` catches that: here the target is
        swapped for a different inode after validation, and the write is refused
        instead of overwriting the impostor.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        original_stat = _os.stat

        def lying_stat(*args, **kwargs):
            st = original_stat(*args, **kwargs)
            if kwargs.get("dir_fd") is not None:
                # Pretend the pinned parent resolved to a different inode.
                class _Fake:
                    st_dev = st.st_dev
                    st_ino = st.st_ino + 1

                return _Fake()
            return st

        monkeypatch.setattr(_os, "stat", lying_stat)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is False
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "ORIGINAL"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="os.link needs elevated privileges on Windows to make the second name",
    )
    def test_rejected_writes_do_not_leak_descriptors(self, home_store, project_file) -> None:
        """Every validation rejection must close the descriptor it opened.

        The fd deliberately outlives validation (the no-dir-fd path writes
        through it), so it cannot be closed in a blanket ``finally``. That made
        each rejected update leak one descriptor, which would eventually exhaust
        the gateway's limit.

        The subject therefore has to be a path the write gate refuses AFTER that
        open: a second hardlink on the file, which ``_pinned_replace``'s
        ``fstat`` check rejects (``st_nlink > 1``). A path OUTSIDE the approved
        root -- what this test drove before -- never gets that far:
        ``allowed_source_roots`` refuses it in ``_try_write_source_path`` itself,
        so no descriptor is opened and the leak this test names could not have
        been observed either way.

        Pairing is read off the descriptors the writer itself opened and closed,
        never a ``/proc/<pid>/fd`` census: this worker's other threads (executor
        pools, the SEL writer) open and close descriptors of their own, so a
        census moves for reasons that have nothing to do with this write.
        """
        import os as _os

        proj, src = project_file
        target = str(src.resolve())
        # A second name on the same inode makes st_nlink == 2, which the pinned
        # writer refuses -- but only once it already holds the descriptor.
        _os.link(target, str(src.with_name("second-name.md")))

        opened: list[int] = []
        closed: list[int] = []
        real_open, real_close = _os.open, _os.close

        def tracking_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if str(path) == target:
                opened.append(fd)
            return fd

        def tracking_close(fd: int, /) -> None:
            closed.append(fd)
            real_close(fd)

        # Wrapped, never stubbed -- the real syscalls still run, so what the
        # assertions below see is the writer's own cleanup. Scoped to a context
        # rather than the shared ``monkeypatch``, whose ``undo()`` would also
        # unpin the fake ``Path.home`` the ``home_store`` fixture installed.
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(_os, "open", tracking_open)
            patched.setattr(_os, "close", tracking_close)
            for _ in range(5):
                assert home_store._try_write_source_path(target, "nope", str(proj)) is False

        assert len(opened) == 5, "the refusal never reached the descriptor-pinned open"
        leaked = [fd for fd in opened if fd not in closed]
        assert leaked == [], f"a rejected update leaked its descriptor: {leaked}"
        assert src.read_text(encoding="utf-8") == "# live from the project"

    def test_write_refuses_when_an_acl_attribute_cannot_be_carried(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """Losing an ACL is a security regression, so the write is refused.

        The rename installs a NEW inode. If the owner's POSIX ACL cannot be
        reproduced on it, the replacement is protected LESS than the file it
        replaced -- so the original is left alone instead.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")

        real_list = getattr(_os, "listxattr", None)
        if real_list is None:  # pragma: no cover - platform without xattrs
            pytest.skip("no xattr support on this platform")

        monkeypatch.setattr(
            _os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False
        )
        monkeypatch.setattr(_os, "getxattr", lambda *a, **k: b"acl-bytes", raising=False)

        def refuse_setxattr(*a, **k):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(_os, "setxattr", refuse_setxattr, raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is False
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "ORIGINAL"

    def test_write_proceeds_when_only_an_informational_attribute_fails(
        self, home_store, project_file, monkeypatch
    ) -> None:
        # A user.* attribute is metadata, not protection. Failing the save over it
        # would break every linked write on a filesystem that cannot store xattrs.
        import os as _os

        proj, src = project_file
        if not hasattr(_os, "listxattr"):  # pragma: no cover
            pytest.skip("no xattr support on this platform")

        monkeypatch.setattr(_os, "listxattr", lambda *a, **k: ["user.note"], raising=False)
        monkeypatch.setattr(_os, "getxattr", lambda *a, **k: b"x", raising=False)

        def refuse_setxattr(*a, **k):
            raise OSError(95, "Operation not supported")

        monkeypatch.setattr(_os, "setxattr", refuse_setxattr, raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "new body"

    def test_write_refuses_when_the_attribute_list_cannot_be_read(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """A failed listxattr is not the same as "there are none".

        Treating a lookup failure as an empty list would install a replacement
        stripped of the owner's ACL. Only "this filesystem has no xattrs"
        (ENOTSUP/EOPNOTSUPP/ENOSYS) is safe to read as nothing-to-carry.
        """
        import errno as _errno
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        if not hasattr(_os, "listxattr"):  # pragma: no cover
            pytest.skip("no xattr support on this platform")

        def failing_list(*a, **k):
            raise OSError(_errno.EACCES, "Permission denied")

        monkeypatch.setattr(_os, "listxattr", failing_list, raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is False
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "ORIGINAL"

    def test_write_proceeds_where_the_filesystem_has_no_xattrs(
        self, home_store, project_file, monkeypatch
    ) -> None:
        # ENOTSUP means there is nothing on the source to lose, so the write goes
        # ahead -- failing here would break linked writes on tmpfs and several
        # network mounts.
        import errno as _errno
        import os as _os

        proj, src = project_file
        if not hasattr(_os, "listxattr"):  # pragma: no cover
            pytest.skip("no xattr support on this platform")

        def unsupported(*a, **k):
            raise OSError(_errno.ENOTSUP, "Operation not supported")

        monkeypatch.setattr(_os, "listxattr", unsupported, raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "new body"

    def test_write_preserves_extended_attributes(self, home_store, project_file) -> None:
        """Extended attributes must survive the replace.

        The in-place write this staging replaced preserved them for free by never
        changing the inode; a fresh inode starts with none, which would silently
        drop POSIX ACLs (stored as ``system.posix_acl_access``) and any ``user.*``
        metadata.
        """
        import os

        proj, src = project_file
        try:
            os.setxattr(str(src), "user.kirocrew_test", b"keepme")
        except (AttributeError, OSError):
            pytest.skip("filesystem or platform has no xattr support")
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "new body"
        assert os.getxattr(str(src), "user.kirocrew_test") == b"keepme"

    def test_write_survives_a_platform_without_geteuid(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """``os.geteuid`` is POSIX-only; its absence must not raise.

        On Windows the attribute does not exist, and ``AttributeError`` is not an
        ``OSError`` -- so an unguarded call would escape every handler in this
        path and surface as a 500 with the descriptor leaked and ``current.html``
        already written. Simulated by deleting the attribute.
        """
        import os as _os

        proj, src = project_file
        monkeypatch.delattr(_os, "geteuid", raising=False)
        assert home_store._try_write_source_path(str(src), "no euid here", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "no euid here"

    def test_pinned_parent_check_only_applies_where_pinning_exists(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """The inode re-check is a Linux-only EXTRA, not the safety property.

        Without directory-handle APIs there is no pinned parent to compare
        against, so the write proceeds on the by-name path. The crash-safety
        guarantee (atomic replace) is unchanged; only the anti-swap hardening is
        absent, which is the documented trade.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        monkeypatch.setattr(_os, "supports_dir_fd", set(), raising=False)
        assert home_store._try_write_source_path(str(src), "fallback body", str(proj)) is True
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "fallback body"

    def test_a_file_owned_by_someone_else_is_still_mirrored(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """Ownership does not gate the mirror.

        Replacing a file does hand the new inode to this process's user, which is
        what any editor's atomic save does to a group-shared file. Refusing
        instead meant a shared project file silently stopped tracking its
        artifact, which is the worse outcome; the write goes through and the
        permission bits and extended attributes are carried across.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        real_fstat = _os.fstat

        def fstat_foreign(fd: int):
            st = real_fstat(fd)

            class _Foreign:
                st_mode, st_nlink, st_size = st.st_mode, st.st_nlink, st.st_size
                st_dev, st_ino = st.st_dev, st.st_ino
                st_uid, st_gid = st.st_uid + 1, st.st_gid + 1

            return _Foreign()

        monkeypatch.setattr(_os, "fstat", fstat_foreign)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "new body"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="the staged rename is the dir-fd path, which Windows does not take",
    )
    def test_write_refuses_to_clobber_a_concurrent_save(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """An editor's atomic save during our staged write must win, not lose.

        ``rename()`` replaces whatever the name points at when it runs. If an IDE
        atomically saves the same file while the payload is being staged, the
        target is a NEW inode and renaming over it would silently discard content
        newer than what is being written here. The last-moment identity re-check
        turns that into a refusal.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")

        real_fsync = _os.fsync
        fired = {"done": False}

        def fsync_then_replace(fd: int) -> None:
            # Fires while the staging file is being flushed -- i.e. after the
            # first identity check and before the rename.
            real_fsync(fd)
            if not fired["done"]:
                fired["done"] = True
                newer = src.parent / "newer.md"
                newer.write_text("A NEWER SAVE FROM THE EDITOR", encoding="utf-8")
                _os.replace(str(newer), str(src))

        monkeypatch.setattr(_os, "fsync", fsync_then_replace)
        assert home_store._try_write_source_path(str(src), "our body", str(proj)) is False
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the test's os patch, and home_store's Path.home pin, before the tmp-path file is read back; moved with its test
        monkeypatch.undo()
        # The editor's newer content survived; ours was refused.
        assert src.read_text(encoding="utf-8") == "A NEWER SAVE FROM THE EDITOR"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows has no POSIX permission bits; fchmod_safe is a documented no-op there",
    )
    def test_write_preserves_the_original_file_mode(self, home_store, project_file) -> None:
        """The atomic replace must not downgrade permissions.

        Staging is created 0600; without carrying the target's mode across, a
        shared 0644 document or an 0755 script would come back 0600 and break
        every other reader.
        """
        import stat as _stat

        proj, src = project_file
        src.chmod(0o644)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "new body"
        assert _stat.S_IMODE(src.stat().st_mode) == 0o644

    def test_write_does_not_clobber_a_lookalike_staging_file(
        self, home_store, project_file
    ) -> None:
        """A sibling that merely looks like our staging file is real user data.

        A predictable staging name opened O_CREAT|O_TRUNC would have emptied it
        and then renamed it away. The name is unique per call and created
        O_EXCL, so an existing lookalike survives untouched.
        """
        proj, src = project_file
        decoy = src.parent / f".{src.name}.kirocrew-tmp"
        decoy.write_text("someone else's data", encoding="utf-8")
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "new body"
        assert decoy.read_text(encoding="utf-8") == "someone else's data"

    def test_failed_source_write_leaves_the_original_intact(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """A write that fails partway must not truncate the user's file.

        Truncating before writing meant an ENOSPC/EIO partway through left the
        project file empty or half-written with no way back.
        """
        proj, src = project_file
        original = src.read_text(encoding="utf-8")
        from kiro_crew import hooks as hooks_mod

        real_write = hooks_mod.os.write
        calls = {"n": 0}

        def exploding_write(fd: int, data: bytes) -> int:
            # Only the PAYLOAD write fails. That is what ENOSPC actually looks
            # like on the in-place path: the truncate freed exactly the space the
            # restore needs, so writing the original bytes back succeeds. Failing
            # every write instead models a disk that never recovers, which no
            # in-place scheme can survive -- and it made this test assert data
            # loss on platforms without dir-fd support (Windows), where the
            # restore write was being blown up too.
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(hooks_mod.os, "write", exploding_write)
        assert home_store._try_write_source_path(str(src), "replacement", str(proj)) is False
        monkeypatch.setattr(hooks_mod.os, "write", real_write)
        assert src.read_text(encoding="utf-8") == original
        # And no staging litter is left behind.
        assert not list(src.parent.glob(".*kirocrew-tmp"))

    def test_refused_mirror_keeps_the_edit_and_demotes_to_copy(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """A refused mirror write must not cost the user their edit.

        The write can now be declined for several legitimate reasons (read-only
        file, a concurrent save that would be clobbered, ownership we may not
        reassign, a source too large to roll back). The edit is already in
        current.html, but while the artifact still claims to be a live pointer the
        next read prefers the SOURCE -- serving the old text back and reporting
        itself clean. The artifact must take ownership of its own copy instead.
        """
        proj, src = project_file
        src.write_text("# on disk", encoding="utf-8")
        art = home_store.create(
            name="linked",
            content="# on disk",
            slug="linked-demote",
            source_path=str(src),
            source_root=str(proj),
        )
        assert art.source_copy_only is False

        monkeypatch.setattr(type(home_store), "_try_write_source_path", lambda self, *a, **k: False)
        home_store.update("linked-demote", content="# my edit")

        # The edit survived, and the source was left alone.
        assert home_store.get("linked-demote").content == "# my edit"
        assert src.read_text(encoding="utf-8") == "# on disk"
        # Demoted to copy, and PERSISTED -- otherwise every later save would
        # re-attempt the mirror and re-lose the edit.
        assert home_store.get("linked-demote").source_copy_only is True
        fresh = ArtifactStore(root=home_store.root)
        assert fresh.get("linked-demote").source_copy_only is True
        assert fresh.get("linked-demote").content == "# my edit"
        # Provenance is kept: it still records where it came from.
        assert fresh.get("linked-demote").source_path == str(src)

    def test_snapshot_never_writes_back_to_a_linked_file(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """A snapshot is a READ; it must never write out to source_path.

        The live read is bounded, so a source file larger than the bound yields a
        PREFIX. Mirroring that prefix back would truncate the user's file --
        silent, unrecoverable data loss. Writing back content that came FROM the
        file is pointless even when it fits, so the write is skipped outright.
        """
        proj, src = project_file
        home_store.create(
            name="Spec",
            content="# snapshot",
            slug="spec",
            kind="markdown",
            source_path=str(src),
            source_root=str(proj),
        )
        full = "# live from the project\n" + ("x" * 400)
        src.write_text(full, encoding="utf-8")
        # Force the bounded read to return a PREFIX, exactly as an oversized
        # file would, and prove the original survives intact.
        from kiro_crew import artifacts as artifacts_mod

        monkeypatch.setattr(artifacts_mod, "MAX_CONTENT_BYTES", 32)
        home_store.update("spec", snapshot=True)
        assert src.read_text(encoding="utf-8") == full

    def test_snapshot_of_a_copy_does_not_reimport_the_original(
        self, home_store, project_file
    ) -> None:
        """Snapshotting a copied artifact must keep the user's edits.

        The snapshot path re-reads live content so it can capture external
        changes to a LINKED file. For a copy that would overwrite the edited
        artifact with the original file's bytes.
        """
        proj, src = project_file
        home_store.create(
            name="Scratch",
            content="# original",
            slug="scratch",
            kind="markdown",
            source_path=str(src),
            source_root=str(proj),
            source_copy_only=True,
        )
        home_store.update("scratch", content="# edited in the artifact")
        snapped = home_store.update("scratch", snapshot=True)
        assert snapped.content == "# edited in the artifact"
        # And the original file is untouched by the copy's edits.
        assert src.read_text(encoding="utf-8") == "# live from the project"

    def test_get_serves_live_content_when_root_recorded(self, home_store, project_file) -> None:
        proj, src = project_file
        art = home_store.create(
            name="Spec",
            content="# snapshot",
            slug="spec",
            kind="markdown",
            source_path=str(src),
            source_root=str(proj),
        )
        assert art.source_root == str(proj)
        src.write_text("# externally edited", encoding="utf-8")
        got = home_store.get("spec")
        assert got.content == "# externally edited"
        assert got.source_missing is False

    def test_source_root_persisted_and_reloaded(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec",
            content="# snapshot",
            slug="spec",
            source_path=str(src),
            source_root=str(proj),
        )
        raw = json.loads((home_store._artifact_dir("spec") / "meta.json").read_text())
        assert raw["source_root"] == str(proj)
        assert home_store.get("spec").source_root == str(proj)

    def test_legacy_meta_without_source_root_loads_empty(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec", content="s", slug="spec", source_path=str(src), source_root=str(proj)
        )
        meta_path = home_store._artifact_dir("spec") / "meta.json"
        raw = json.loads(meta_path.read_text())
        del raw["source_root"]  # simulate an artifact written before the field existed
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        assert home_store.get("spec").source_root == ""

    def test_source_root_ignored_without_source_path(self, home_store, project_file) -> None:
        proj, _src = project_file
        art = home_store.create(name="Chat", content="x", source_root=str(proj))
        # A root with nothing to authorize is meaningless metadata.
        assert art.source_root == ""

    def test_relocate_clears_stale_source_root(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec", content="s", slug="spec", source_path=str(src), source_root=str(proj)
        )
        home_dst = Path.home() / "moved.md"
        home_dst.write_text("# moved", encoding="utf-8")
        art = home_store.relocate("spec", str(home_dst))
        # The old project root does not authorize anything about the new path.
        assert art.source_path == str(home_dst)
        assert art.source_root == ""


class TestSourceMissingIsVisible:
    """A dead live-pointer must be reported, not silently masked by the snapshot."""

    def test_deleted_source_sets_source_missing(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec",
            content="# snapshot",
            slug="spec",
            source_path=str(src),
            source_root=str(proj),
        )
        src.unlink()
        got = home_store.get("spec")
        assert got.source_missing is True
        assert got.content == "# snapshot"  # still viewable via the fallback

    def test_refused_root_sets_source_missing(self, home_store, project_file) -> None:
        # No recorded root → the read is refused → the pointer is effectively dead.
        _proj, src = project_file
        home_store.create(name="Spec", content="# snapshot", slug="spec", source_path=str(src))
        got = home_store.get("spec")
        assert got.source_missing is True
        assert got.content == "# snapshot"

    def test_live_dirty_cannot_report_clean_for_missing_source(
        self, home_store, project_file
    ) -> None:
        # The regression: live_dirty was computed against the FALLBACK, so it
        # compared the snapshot with itself and always said "in sync".
        proj, src = project_file
        home_store.create(
            name="Spec",
            # Snapshot matches the file, so the healthy baseline is genuinely clean.
            content="# live from the project",
            slug="spec",
            source_path=str(src),
            source_root=str(proj),
        )
        assert home_store.get("spec").live_dirty is False  # healthy pointer
        src.unlink()
        got = home_store.get("spec")
        assert got.source_missing is True
        assert got.live_dirty is True

    def test_healthy_pointer_leaves_flag_false(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec",
            content="# live from the project",
            slug="spec",
            source_path=str(src),
            source_root=str(proj),
        )
        assert home_store.get("spec").source_missing is False

    def test_chat_backed_artifact_never_flagged(self, store: ArtifactStore) -> None:
        store.create(name="Widget", content="<p>hi</p>", slug="w", kind="widget")
        assert store.get("w").source_missing is False

    def test_source_missing_is_not_persisted(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec", content="s", slug="spec", source_path=str(src), source_root=str(proj)
        )
        src.unlink()
        home_store.get("spec")  # computes source_missing=True
        home_store.set_pinned("spec", True)  # any metadata write
        raw = json.loads((home_store._artifact_dir("spec") / "meta.json").read_text())
        assert "source_missing" not in raw
        assert "live_dirty" not in raw

    def test_snapshot_off_dead_pointer_flags_missing(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec", content="s", slug="spec", source_path=str(src), source_root=str(proj)
        )
        src.unlink()
        art = home_store.update("spec", snapshot=True)
        assert art.source_missing is True


class TestSourcePathLengthRejected:
    """Over-long pointers are REJECTED — truncating produced a different path."""

    def test_create_rejects_overlong_source_path(self, store: ArtifactStore) -> None:
        long_path = "/" + "a" * 600
        with pytest.raises(ArtifactValidationError, match="refusing to truncate"):
            store.create(name="X", content="c", slug="x", source_path=long_path)
        # And nothing was half-created.
        with pytest.raises(ArtifactNotFoundError):
            store.get("x")

    def test_create_rejects_overlong_source_root(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError, match="source_root exceeds"):
            store.create(name="X", content="c", source_path="/p/a.md", source_root="/" + "b" * 600)

    def test_relocate_rejects_overlong_source_path(self, store: ArtifactStore) -> None:
        store.create(name="X", content="c", slug="x")
        with pytest.raises(ArtifactValidationError, match="refusing to truncate"):
            store.relocate("x", "/" + "a" * 600)

    def test_path_at_the_cap_is_accepted_unchanged(self, store: ArtifactStore) -> None:
        from kiro_crew.artifacts import MAX_SOURCE_PATH_LEN

        at_cap = "/" + "a" * (MAX_SOURCE_PATH_LEN - 1)
        art = store.create(name="X", content="c", slug="x", source_path=at_cap)
        assert art.source_path == at_cap  # never silently shortened


class TestAllowedRootsSingleProducer:
    """All three consumers of the allowed-roots set share ONE producer.

    The set had drifted into three copies (live read, live write, relocate
    handler) and the handler's copy omitted the data-home root, so relocate
    refused paths the store would then read happily.
    """

    def test_set_contains_home_data_home_and_configured_roots(
        self, home_store, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

        extra = tmp_path / "shared"
        extra.mkdir()
        cfg = KiroCrewConfig()
        cfg.publish = PublishConfig(relocate_roots=[str(extra)])
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
        roots = home_store.allowed_source_roots()
        assert Path.home().resolve() in roots
        assert (Path.home() / ".kiro" / "crew").resolve() in roots  # data home
        assert extra.resolve() in roots
        # source_root only widens when supplied AND still verifiable. A bare
        # directory is not: meta.json is agent-writable, so an unverified root
        # would let a forged record widen this boundary at will.
        assert tmp_path.resolve() not in roots
        assert tmp_path.resolve() not in home_store.allowed_source_roots(str(tmp_path))
        verifiable = tmp_path / "repo"
        (verifiable / ".git").mkdir(parents=True)
        assert verifiable.resolve() in home_store.allowed_source_roots(str(verifiable))

    def test_data_home_path_accepted_by_read_and_write(self, home_store) -> None:
        # The data-home root the relocate handler must include. Read and write must agree.
        data_home = home_store._root.resolve().parent
        target = data_home / "note.md"
        target.write_text("in the data home", encoding="utf-8")
        assert home_store._try_read_source_path(str(target)) == "in the data home"
        assert home_store._try_write_source_path(str(target), "edited") is True

    def test_no_second_copy_of_the_root_assembly_in_source(self) -> None:
        """Anti-drift pin: only ``allowed_source_roots`` may assemble the set.

        Guards against a fourth copy appearing. Both markers below exist exactly
        once in the store (inside ``allowed_source_roots``) and never in the
        handler, which must call the store instead.
        """
        import kiro_crew.artifacts as artifacts_mod
        import kiro_crew.dashboard.handlers.artifacts as handlers_mod

        here = Path(artifacts_mod.__file__).parent
        store_src = "".join(p.read_text("utf-8") for p in here.glob("artifacts*.py"))
        facade = Path(handlers_mod.__file__)
        handler_src = "".join(
            path.read_text(encoding="utf-8")
            for path in [facade, *(facade.parent / "artifacts_parts").glob("*.py")]
        )
        assert store_src.count("Path.home().resolve()") == 1
        assert store_src.count(".publish.relocate_roots") == 1
        assert "Path.home()" not in handler_src
        assert ".publish.relocate_roots" not in handler_src
        assert "allowed_source_roots()" in handler_src
