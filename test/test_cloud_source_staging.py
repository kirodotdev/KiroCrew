"""Unit tests for where cloud/source.py stages the source tarball before upload.

``upload_source`` hands the tarball to the AWS CLI by path, so it must sit in a directory
the launcher owns. Covers the staging directory under the data home (owner-only mode,
lockdown refusals, links and files planted at its leaf, cleanup after a failed or
interrupted build), the ancestor walk above it (writable, group-writable, sticky, ACL and
foreign-owned ancestors, relocated and symlinked homes) and the Windows arm of that walk,
driven on any host through an injected ``describe`` plus one case that reads real Windows
descriptors. What goes into the tarball, its checksum pin, the bucket, the boundaries and
the upload are in ``test_cloud_source.py``.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest
from group_privacy_helpers import _ACL_USER, _acl_xattr, _serve_acl

from conftest import make_dir_link
from kiro_crew import github_runner, platform_compat, windows_acl
from kiro_crew.cloud import aws, source
from kiro_crew.config import ensure_data_home


class TestTarballStagingDirectory:
    """Where the source tarball is built, not what goes into it.

    ``upload_source`` passes the tarball to ``s3api put-object`` as
    ``--body <path>``, so the AWS CLI re-opens it by name after this process has
    closed it. Every builder must therefore leave the file somewhere the
    launcher owns for the length of that window -- never in the process temp
    root, whose location ``tempfile`` takes from the environment and which
    carries no promise of being owner-only.
    """

    @staticmethod
    def _pin_home_and_temp_root(monkeypatch, tmp_path) -> tuple[Path, Path]:
        """Point the data home and the process temp root at two separate dirs.

        Separating them is what makes the assertions discriminating: a builder
        that honours the temp root and one that resolves the data home would
        otherwise write to the same place under pytest's ``tmp_path``.
        """
        home = tmp_path / "data-home"
        home.mkdir(mode=0o700)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        return home, temp_root

    @staticmethod
    def _tiny_targz(path: Path) -> Path:
        """A real (tiny) gzip tarball, so size and open checks see valid bytes."""
        member = path.parent / "member.txt"
        member.write_text("x\n")
        with tarfile.open(path, "w:gz") as tf:
            tf.add(member, arcname="member.txt")
        return path

    def _capture_staged_names(self, monkeypatch) -> list[str]:
        """Every path ``NamedTemporaryFile`` hands out, in call order."""
        created: list[str] = []
        real_ntf = tempfile.NamedTemporaryFile

        def _capturing(*a, **kw):
            fh = real_ntf(*a, **kw)
            created.append(fh.name)
            return fh

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", _capturing)
        return created

    def test_the_tarfile_fallback_stages_under_the_data_home(self, monkeypatch, tmp_path):
        home, temp_root = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])

        tarball = source._tar_fallback(repo)
        try:
            assert tarball.parent == home / source._STAGING_DIR_LEAF
            # The process temp root must not have been used at all.
            assert list(temp_root.iterdir()) == []
        finally:
            tarball.unlink(missing_ok=True)

    def test_the_git_archive_path_stages_both_tarballs_under_the_data_home(
        self, monkeypatch, tmp_path
    ):
        # `git archive` writes one file and `_refilter_archive` rewrites it into a
        # second: BOTH are staged, so capture every name rather than only the
        # path handed back.
        home, temp_root = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        payload = self._tiny_targz(tmp_path / "payload.tar.gz")
        real_run = subprocess.run

        def _fake_run(argv, **kw):
            if argv[:1] == ["git"] and "archive" in argv:
                # The real command writes to stdout, which is the caller's held handle.
                kw["stdout"].write(payload.read_bytes())
                kw["stdout"].flush()
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_run(argv, **kw)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        staged = self._capture_staged_names(monkeypatch)

        tarball = source._use_git_archive(tmp_path / "repo")
        try:
            assert tarball is not None, "the stubbed git archive should have succeeded"
            expected_parent = home / source._STAGING_DIR_LEAF
            assert len(staged) == 2, f"expected an archive and a re-filtered copy, got {staged}"
            assert [Path(p).parent for p in staged] == [expected_parent, expected_parent]
            assert tarball.parent == expected_parent
            assert list(temp_root.iterdir()) == []
        finally:
            if tarball is not None:
                tarball.unlink(missing_ok=True)

    def test_the_refilter_stages_under_the_data_home(self, monkeypatch, tmp_path):
        home, temp_root = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        archive = self._tiny_targz(tmp_path / "archive.tar.gz")

        filtered = None
        with archive.open("rb") as archive_fh:
            filtered = source._refilter_archive(archive_fh)
        try:
            assert filtered.parent == home / source._STAGING_DIR_LEAF
            assert list(temp_root.iterdir()) == []
        finally:
            filtered.unlink(missing_ok=True)

    def test_the_staging_directory_is_owner_only(self, monkeypatch, tmp_path):
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        staging = source._staging_dir()
        assert staging == home / source._STAGING_DIR_LEAF
        assert staging.is_dir()
        if os.name == "posix":
            assert stat.S_IMODE(staging.stat().st_mode) == 0o700

    def test_a_pre_existing_staging_directory_is_re_restricted(self, monkeypatch, tmp_path):
        # A leaf left group- or world-readable by an earlier umask must not be
        # accepted as-is: the tarball is the box's whole source tree.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        loose = home / source._STAGING_DIR_LEAF
        loose.mkdir()
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        # The loose mode IS the fixture: the assertion below is that _staging_dir
        # tightens a leaf an earlier umask left group- and world-readable. Nothing
        # is published from this temp directory.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(loose, 0o755)  # noqa: S103 - the loose mode is the fixture. lockdown-ok.
        assert stat.S_IMODE(source._staging_dir().stat().st_mode) == 0o700

    def test_a_lockdown_that_cannot_be_applied_refuses_the_build(self, monkeypatch, tmp_path):
        # restrict_dir_to_owner is fail-loud by contract. A mount that rejects the
        # change must stop the build, not warn and stage the tarball anyway.
        self._pin_home_and_temp_root(monkeypatch, tmp_path)

        def _refuse(path):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(source.platform_compat, "restrict_dir_to_owner", _refuse)
        with pytest.raises(aws.AWSError, match="owner-only"):
            source._staging_dir()

    def test_a_lockdown_that_silently_does_not_take_refuses_the_build(self, monkeypatch, tmp_path):
        # A filesystem with a fixed permission mask accepts the chmod and keeps
        # its own mode, so a successful call is not evidence of the mode. The
        # read-back is what closes that, and it is the whole point of this pin.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        leaf = home / source._STAGING_DIR_LEAF
        leaf.mkdir()
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(
            leaf, 0o757
        )  # noqa: S103 - simulating the mask a FAT/CIFS mount keeps. lockdown-ok.
        monkeypatch.setattr(source.platform_compat, "restrict_dir_to_owner", lambda path: None)
        with pytest.raises(aws.AWSError, match="reachable by other accounts"):
            source._staging_dir()

    def test_a_link_planted_at_the_staging_leaf_is_refused(self, monkeypatch, tmp_path):
        # A link at the leaf would put every tarball back outside the fence, and
        # no per-file check can see that -- fail closed instead of building.
        #
        # conftest.make_dir_link, not symlink_to: a directory symlink needs
        # SeCreateSymbolicLinkPrivilege, which an unelevated Windows shell lacks,
        # so a symlink here would skip on exactly the host whose junction branch of
        # is_link_or_junction this check depends on.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        elsewhere = tmp_path / "attacker-controlled"
        elsewhere.mkdir()
        make_dir_link(home / source._STAGING_DIR_LEAF, elsewhere)
        with pytest.raises(aws.AWSError, match="staging directory"):
            source._staging_dir()

    def test_a_regular_file_at_the_staging_leaf_is_refused_as_an_awserror(
        self, monkeypatch, tmp_path
    ):
        # mkdir(exist_ok=True) forgives an existing DIRECTORY only, so a regular
        # file at the leaf raises OSError before the resolve check below it runs.
        # cli_cloud catches AWSError/ValidationError/CloudActionDenied and nothing
        # else, so the refusal has to BE an AWSError -- otherwise 'cloud launch'
        # ends in a traceback that names no remedy. Asserting the type is the pin:
        # without the try/except this raises FileExistsError and never matches.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        leaf = home / source._STAGING_DIR_LEAF
        leaf.write_text("not a directory", encoding="utf-8")
        with pytest.raises(aws.AWSError, match="could not be created"):
            source._staging_dir()

    def test_a_linked_ancestor_above_the_data_home_is_followed(self, monkeypatch, tmp_path):
        # A link ABOVE the data home is the operator's own layout -- a symlinked
        # $HOME, a dotfile-managed ~/.kiro -- and the product documents both as
        # supported (atomic_write._link_trust_anchor, workflows.md). Refusing it
        # would refuse a launch on the DEFAULT data-home path, which is lexical
        # (Path.home() / ".kiro" / "crew"), so it is where such a link arrives.
        #
        # Driven through config_dir rather than KIROCREW_HOME: the override arrives
        # already resolved, so a link is unobservable on that path.
        #
        # What still protects the build is that _first_replaceable walks the
        # RESOLVED chain, so the ownership and mode tests land on the directories
        # that really hold the tarball. Asserting the resolved location is what
        # makes this discriminating: a staging dir merely created at the lexical
        # name would pass a bare "no exception" check.
        real = tmp_path / "real-parent"
        (real / "data-home").mkdir(mode=0o700, parents=True)
        link = tmp_path / "linked-parent"
        make_dir_link(link, real)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(source, "config_dir", lambda: link / "data-home")

        staging = source._staging_dir()

        assert staging == link / "data-home" / source._STAGING_DIR_LEAF
        assert staging.is_dir()
        assert not staging.is_symlink()
        assert staging.resolve() == (real / "data-home" / source._STAGING_DIR_LEAF).resolve()

    def test_a_data_home_relocated_through_a_link_is_followed(self, monkeypatch, tmp_path):
        # The same policy one level down: the data home ITSELF being a link is the
        # documented "data home moved onto another disk" layout, and it sits AT the
        # trust anchor rather than below it. Planting such a link needs write on its
        # parent, which is exactly what _first_replaceable refuses -- so on a chain
        # that passes those tests the link can only be the operator's own.
        real = tmp_path / "another-disk"
        real.mkdir(mode=0o700)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        holder = tmp_path / "holder"
        holder.mkdir(mode=0o700)
        make_dir_link(holder / "data-home", real)
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(source, "config_dir", lambda: holder / "data-home")

        staging = source._staging_dir()

        assert staging.is_dir()
        assert not staging.is_symlink()
        assert staging.resolve() == (real / source._STAGING_DIR_LEAF).resolve()

    def test_the_default_data_home_under_a_symlinked_home_is_accepted(self, monkeypatch, tmp_path):
        """The reviewer's scenario, through the REAL ``config_dir()``.

        ``HOME=/home/u`` with ``/home/u -> /local/home/u`` and ``KIROCREW_HOME``
        unset is an ordinary host, not an unusual one, and it is the layout this
        repo's own checkout instructions run on. The default data home is lexical,
        so ``config_dir()`` hands ``_staging_dir()`` a path whose ancestor is a
        link, and a blanket refusal there aborts ``cloud launch`` for a user who
        did nothing unusual and cannot satisfy it without repointing
        ``KIROCREW_HOME`` at a pre-resolved path.

        Driven through the real ``config_dir()`` rather than a stub, because the
        lexical default home is the whole mechanism: a stub returning an
        already-resolved path cannot show it. ``config.paths`` memoises the
        resolved home per process, so both caches are cleared alongside ``$HOME``
        or this would read whichever home the process resolved first.
        """
        from kiro_crew.config import paths as config_paths

        real_home = tmp_path / "local" / "home" / "u"
        real_home.mkdir(mode=0o700, parents=True)
        link_home = tmp_path / "home"
        link_home.mkdir()
        make_dir_link(link_home / "u", real_home)
        home = link_home / "u"
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setattr(config_paths, "_resolved_home", None)
        monkeypatch.setattr(config_paths, "_config_dir_memo", None)

        # The premise: the default home really is reached through the link.
        base = source.config_dir()
        assert base == home / ".kiro" / "crew"
        assert home.is_symlink() or source.platform_compat.is_link_or_junction(home)

        staging = source._staging_dir()

        assert staging == base / source._STAGING_DIR_LEAF
        assert staging.is_dir()
        assert not staging.is_symlink()
        assert (
            staging.resolve() == (real_home / ".kiro" / "crew" / source._STAGING_DIR_LEAF).resolve()
        )
        # And it is still locked down, which is the guarantee the leaf carries.
        if os.name == "posix":
            assert stat.S_IMODE(staging.stat().st_mode) & 0o077 == 0

    def test_a_writable_parent_of_a_relocated_home_is_refused(self, monkeypatch, tmp_path):
        # The other half of following a link at or above the data home: what the
        # guard gives up must be ONLY the link, never the hazard the link hid.
        #
        # The data home is a link onto another disk, and the loose directory is the
        # link TARGET's parent -- the directory whose write bit lets `real-home` be
        # renamed away and replaced after this process closes the tarball. That
        # directory appears on the RESOLVED chain and on no lexical one: walking the
        # path as written stats `holder/data-home`, which follows the link and reads
        # the target's own sound 0o700, then stops at `holder`. So `shared` is not
        # merely misnamed by a lexical walk, it is invisible to one, and resolving is
        # what brings the real hazard into scope at all.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "shared-real-parent"
        real_home = shared / "real-home"
        real_home.mkdir(mode=0o700, parents=True)
        holder = tmp_path / "holder"
        holder.mkdir(mode=0o700)
        make_dir_link(holder / "data-home", real_home)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(source, "config_dir", lambda: holder / "data-home")
        # Every LEXICAL component is sound, so a walk on the path as written passes.
        assert stat.S_IMODE(holder.stat().st_mode) & stat.S_IWOTH == 0
        assert stat.S_IMODE((holder / "data-home").stat().st_mode) & stat.S_IWOTH == 0
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o777)  # noqa: S103 - the shared parent is the fixture. lockdown-ok.

        with pytest.raises(aws.AWSError, match="writable by any account") as caught:
            source._staging_dir()

        # Named in its RESOLVED form, which is the directory the operator has to fix.
        assert str(shared.resolve()) in str(caught.value)

    def test_an_ancestor_others_can_write_refuses_the_build(self, monkeypatch, tmp_path):
        # Replacing a directory entry needs write on its PARENT, so a writable
        # ancestor lets the data home itself be swapped wholesale after every
        # check on the home's own mode has passed.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "shared-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o777)  # noqa: S103 - the shared parent is the fixture. lockdown-ok.
        with pytest.raises(aws.AWSError, match="writable by any account"):
            source._staging_dir()
        assert str(shared) in str(
            pytest.raises(aws.AWSError, source._staging_dir).value
        ), "the refusal should name the outermost problem, not an inner one"

    def test_a_group_writable_ancestor_is_accepted_when_the_group_is_private(
        self, monkeypatch, tmp_path
    ):
        # A host with user-private groups leaves an ordinary directory 0o775 to a
        # group holding only the operator, so refusing on the bit ALONE would refuse
        # every launch on that host. The membership decides instead, so this pin
        # states the membership rather than inheriting whatever group the test host
        # happens to give a new directory -- on a corporate host that is a shared
        # group with dozens of members, and this case would then be asserting the
        # very hole the shared-group pin below covers.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        monkeypatch.setattr(platform_compat, "_group_shared_with_another_account", lambda gid: None)
        shared = tmp_path / "group-writable-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o775)  # noqa: S103 - the user-private-group shape. lockdown-ok.
        staging = source._staging_dir()
        assert staging == home / source._STAGING_DIR_LEAF
        assert stat.S_IMODE(shared.stat().st_mode) == 0o775, "the launch rewrote the parent"

    def test_a_group_writable_ancestor_is_refused_when_the_group_is_shared(
        self, monkeypatch, tmp_path
    ):
        # The same 0o775 bit on a SHARED group hands every member of that group the
        # rename, so it is a foreign writer exactly as others-writable is. Refusing
        # only on S_IWOTH left this open: "another account can write it" covers a
        # shared group, and S_IWOTH does not.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        monkeypatch.setattr(
            platform_compat,
            "_group_shared_with_another_account",
            lambda gid: "group 'peers', shared with 3 other account(s)",
        )
        shared = tmp_path / "group-writable-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o775)  # noqa: S103 - the shared-group shape is the fixture. lockdown-ok.
        with pytest.raises(aws.AWSError) as caught:
            source._staging_dir()
        message = str(caught.value)
        assert str(shared) in message, "the refusal must name the outermost problem"
        assert "shared with 3 other account(s)" in message, message
        # A refusal the operator cannot act on is the bug this PR exists to remove.
        # The ownership test ran first, so this node is theirs or root's either way.
        assert f"chmod go-w {shared}" in message, "the refusal must name its own remedy"
        assert stat.S_IMODE(shared.stat().st_mode) == 0o775, "the launch rewrote the parent"

    def test_sticky_exempts_a_group_writable_ancestor_without_asking_the_group(
        self, monkeypatch, tmp_path
    ):
        # Sticky means an entry may be renamed only by its own owner, the directory's
        # owner, or root, and ownership is settled before the mode. So a sticky
        # group-writable ancestor is safe whatever the group holds, and the
        # membership lookup -- which reads two host databases -- must not run at all.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")

        def _must_not_run(gid):
            raise AssertionError("sticky already settles it; the group lookup ran anyway")

        monkeypatch.setattr(platform_compat, "_group_shared_with_another_account", _must_not_run)
        shared = tmp_path / "sticky-group-writable-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o1775)  # noqa: S103 - sticky + group write is the fixture. lockdown-ok.
        assert source._staging_dir() == home / source._STAGING_DIR_LEAF

    def test_a_group_writable_ancestor_asks_the_shared_group_write_question(
        self, monkeypatch, tmp_path
    ):
        # The provider-CLI resolver asks platform_compat the same question of the
        # directories a link's path reads, so the guard must ask it too rather than
        # a copy that could drift: one directory, one answer.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        asked = []

        def _admits(path, gid):
            asked.append((path, gid))
            return "an ACL entry for another account (uid 4242)"

        monkeypatch.setattr(platform_compat, "group_write_admits_another_account", _admits)
        shared = tmp_path / "group-writable-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o775)  # noqa: S103 - group-writable fixture. lockdown-ok.
        with pytest.raises(aws.AWSError) as caught:
            source._staging_dir()
        assert "writable by an ACL entry for another account (uid 4242)" in str(caught.value)
        assert (shared.resolve(), shared.stat().st_gid) in asked, asked

    def test_an_acl_entry_for_another_account_refuses_a_private_group_writable_parent(
        self, monkeypatch, tmp_path
    ):
        # The group bit of an extended-ACL directory is the ACL mask. A named entry
        # for a peer turns it on while the owning group stays private, so the
        # membership answer alone would accept a directory the peer can write.
        if not hasattr(os, "getxattr"):
            pytest.skip("POSIX ACL xattrs")
        monkeypatch.setattr(platform_compat, "_group_shared_with_another_account", lambda gid: None)
        shared = tmp_path / "acl-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o770)  # noqa: S103 - the ACL mask shape is the fixture. lockdown-ok.
        _serve_acl(monkeypatch, shared, _acl_xattr((_ACL_USER, 0o7, 4242)))
        with pytest.raises(aws.AWSError) as caught:
            source._staging_dir()
        message = str(caught.value)
        assert str(shared) in message, message
        assert "uid 4242" in message, message
        assert f"setfacl -b {shared}" in message, "the refusal must name the ACL remedy"

    def test_a_read_only_acl_entry_leaves_a_private_group_writable_parent_accepted(
        self, monkeypatch, tmp_path
    ):
        # A user-private group with a group-write umask plus a read-only entry for a
        # backup or web account is an ordinary layout. Nobody else can write it.
        if not hasattr(os, "getxattr"):
            pytest.skip("POSIX ACL xattrs")
        monkeypatch.setattr(platform_compat, "_group_shared_with_another_account", lambda gid: None)
        shared = tmp_path / "acl-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o770)  # noqa: S103 - the ACL mask shape is the fixture. lockdown-ok.
        _serve_acl(monkeypatch, shared, _acl_xattr((_ACL_USER, 0o5, 4242), group_obj=0o7))
        assert source._staging_dir() == home / source._STAGING_DIR_LEAF

    def test_a_sticky_ancestor_is_accepted(self, monkeypatch, tmp_path):
        # The sticky bit is exactly the rule that only an entry's owner may rename
        # it, so a sticky world-writable ancestor (the shape of /tmp) is not the
        # swap this refuses -- otherwise the check would reject every ordinary host.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "sticky-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o1777)  # noqa: S103 - the /tmp shape is the fixture. lockdown-ok.
        assert source._staging_dir() == home / source._STAGING_DIR_LEAF

    def test_a_foreign_owned_ancestor_is_refused_whatever_its_mode(self, monkeypatch, tmp_path):
        # A directory's OWNER can replace what is inside it whatever the mode says,
        # so an unwritable 0o755 ancestor owned by someone else is still a swap. The
        # chain here is ordinary; what makes every node foreign is the launcher's
        # own euid, which is the only half of the comparison a test can move.
        if os.name != "posix":
            pytest.skip("POSIX ownership")
        self._pin_home_and_temp_root(monkeypatch, tmp_path)
        real_uid = os.geteuid()
        monkeypatch.setattr(source.os, "geteuid", lambda: real_uid + 4242)
        with pytest.raises(aws.AWSError, match="owned by another account"):
            source._staging_dir()

    def test_sticky_does_not_exempt_a_foreign_owned_ancestor(self, monkeypatch, tmp_path):
        # The hole the ordering closes. Under the sticky bit an entry may be renamed
        # by the entry's owner, by the DIRECTORY's owner, or by root -- so a
        # foreign-owned sticky directory hands its owner the same swap, and sticky
        # must not short-circuit before ownership is judged.
        #
        # Exactly ONE node is made foreign, the sticky one. A blanket euid change
        # would make every node foreign, and then a later node's refusal would let a
        # sticky-first short-circuit pass this test.
        if os.name != "posix":
            pytest.skip("POSIX ownership")
        shared = tmp_path / "sticky-foreign-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(
            shared, 0o1777
        )  # noqa: S103 - a sticky foreign parent is the fixture. lockdown-ok.

        real_stat = Path.stat
        foreign = os.geteuid() + 4242
        target = str(shared.resolve())

        def _stat_with_one_foreign_owner(self, *a, **kw):
            info = real_stat(self, *a, **kw)
            if str(self) in (str(shared), target):
                fields = list(info)
                fields[4] = foreign  # st_uid
                return os.stat_result(tuple(fields))
            return info

        monkeypatch.setattr(Path, "stat", _stat_with_one_foreign_owner)
        with pytest.raises(aws.AWSError, match="owned by another account"):
            source._staging_dir()

    def test_ancestors_above_the_operators_home_are_out_of_scope(self, monkeypatch, tmp_path):
        # Every real chain runs through directories this account does not own: on a
        # sandboxed host '/' itself reads as an unmapped uid. Refusing there would
        # reject an ordinary container and buy nothing, so the walk stops at the
        # operator's own home.
        #
        # The foreign ancestor is CONSTRUCTED above a pinned account home, not
        # probed from the real one: a host whose '/' and '/home' are both root-owned
        # offers nothing to find, and a test that reads the operator's real home is
        # a test that passes or fails on whose machine it runs.
        if os.name != "posix":
            pytest.skip("POSIX ownership")
        above = tmp_path / "above-the-account"
        account = above / "account-home"
        home = account / "kirocrew"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: account))

        real_stat = Path.stat
        foreign = os.geteuid() + 4242
        targets = {str(above), str(above.resolve())}

        def _stat_with_one_foreign_ancestor(self, *a, **kw):
            info = real_stat(self, *a, **kw)
            if str(self) in targets:
                fields = list(info)
                fields[4] = foreign  # st_uid
                return os.stat_result(tuple(fields))
            return info

        monkeypatch.setattr(Path, "stat", _stat_with_one_foreign_ancestor)
        assert above.resolve() not in source._chain_the_launcher_owns(home)
        assert source._staging_dir() == home / source._STAGING_DIR_LEAF

    def test_a_home_inside_the_account_stops_the_walk_at_that_home(self, monkeypatch, tmp_path):
        # The scope branch, pinned where it lives rather than through whichever node
        # happens to refuse first. A home inside the operator's account is checked
        # from that home down, so the directories above it are never stat-ed.
        if os.name != "posix":
            pytest.skip("POSIX paths")
        account = tmp_path / "account-home"
        home = account / "kirocrew"
        home.mkdir(mode=0o700, parents=True)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: account))
        chain = source._chain_the_launcher_owns(home)
        assert chain[0] == account.resolve(), "the walk should start at the operator's own home"
        assert chain[-1] == home.resolve()
        assert (
            Path(account.resolve().root) not in chain
        ), "a directory above the account is out of scope"

    def test_a_home_relocated_outside_the_account_walks_its_whole_chain(
        self, monkeypatch, tmp_path
    ):
        # The premise above does not cover a data home the operator moved out of
        # their own account, which is exactly the shared-host case: there an ancestor
        # can belong to a local peer, so nothing is taken as given.
        if os.name != "posix":
            pytest.skip("POSIX paths")
        elsewhere = tmp_path / "pretend-account-home"
        elsewhere.mkdir(mode=0o700)
        home = tmp_path / "srv-shared" / "kirocrew"
        home.mkdir(mode=0o700, parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: elsewhere))
        chain = source._chain_the_launcher_owns(home)
        assert chain[0] == Path(home.resolve().root), "the walk should start at the root"
        assert chain[-1] == home.resolve()

    def test_a_peer_writable_directory_under_the_account_refuses_the_build(
        self, monkeypatch, tmp_path
    ):
        # The reachable shape of the same threat: a shared directory inside the
        # operator's own account holding the data home.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        shared = tmp_path / "shared-parent"
        home = shared / "data-home"
        home.mkdir(mode=0o700, parents=True)
        temp_root = tmp_path / "process-temp-root"
        temp_root.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(shared, 0o777)  # noqa: S103 - the peer-writable share is the fixture. lockdown-ok.
        with pytest.raises(aws.AWSError, match="writable by any account"):
            source._staging_dir()

    def test_the_data_home_is_verified_not_rewritten(self, monkeypatch, tmp_path):
        # The home belongs to the operator. A launch must not silently chmod it --
        # it refuses, and the mode it found is still there afterwards.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(home, 0o777)  # noqa: S103 - the writable home is the fixture. lockdown-ok.
        with pytest.raises(aws.AWSError, match="writable by any account"):
            source._staging_dir()
        assert stat.S_IMODE(home.stat().st_mode) == 0o777, "the launch rewrote the operator's home"

    def test_a_world_readable_but_unwritable_home_is_accepted(self, monkeypatch, tmp_path):
        # Others-WRITABLE is the swap precondition; others-readable is not, and the
        # leaf's own owner-only mode is what keeps the tarball unreadable. A launch
        # must not refuse the ordinary 0o755 a plain mkdir leaves under umask 022.
        if os.name != "posix":
            pytest.skip("POSIX mode bits")
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(home, 0o755)  # noqa: S103 - the default-umask home is the fixture. lockdown-ok.
        staging = source._staging_dir()
        assert staging == home / source._STAGING_DIR_LEAF
        assert stat.S_IMODE(staging.stat().st_mode) == 0o700

    def test_a_failed_fallback_build_leaves_no_tarball_behind(self, monkeypatch, tmp_path):
        # The staging dir lives under the data home, which no reboot clears, so a
        # build that dies mid-write must remove its own half-written file.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(source, "_git_tracked_files", lambda root: ["app.py"])
        staged = self._capture_staged_names(monkeypatch)

        def _boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(tarfile, "open", _boom)
        with pytest.raises(OSError, match="disk full"):
            source._tar_fallback(repo)

        assert staged, "NamedTemporaryFile was never called"
        leaked = [p for p in staged if Path(p).exists()]
        assert not leaked, f"failed build leaked staged tarball(s): {leaked}"
        assert list((home / source._STAGING_DIR_LEAF).iterdir()) == []

    def test_an_interrupted_git_archive_leaves_no_tarball_behind(self, monkeypatch, tmp_path):
        # An interrupt is not a fallback case: it propagates, so the cleanup the
        # fallback path does on its way out never runs. Without its own arm the
        # archive keeps a copy of the whole checkout in the data home, which no
        # reboot clears, and every aborted launch adds another.
        home, _ = self._pin_home_and_temp_root(monkeypatch, tmp_path)
        staged = self._capture_staged_names(monkeypatch)

        def _interrupted(*a, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(subprocess, "run", _interrupted)
        with pytest.raises(KeyboardInterrupt):
            source._use_git_archive(tmp_path / "repo")

        assert staged, "NamedTemporaryFile was never called"
        leaked = [p for p in staged if Path(p).exists()]
        assert not leaked, f"an interrupted archive leaked staged tarball(s): {leaked}"
        assert list((home / source._STAGING_DIR_LEAF).iterdir()) == []


class TestWindowsStagingAncestorWalk:
    """The Windows arm of the staging ancestor walk (:func:`source._first_replaceable_windows`).

    The arm reuses :func:`github_runner.check_provider_path_component_windows` per
    node, so these pins drive that shared policy by injecting a fake ``describe`` on
    the module it reads through and the launcher SID. The decision is a pure
    function of the ``ComponentSecurity`` dataclass, so it runs on the Linux CI
    runner with no real Windows host.

    Not run on a real Windows host.
    """

    _ME = "S-1-5-21-1-2-3-1001"
    _PEER = "S-1-5-21-1-2-3-1002"
    _OWNER_RIGHTS = "S-1-3-4"

    def _clean(self, owner_sid: str = _ME, **over):
        """A ``ComponentSecurity`` that passes the policy, overridable per field."""
        fields = dict(
            owner_sid=owner_sid,
            owner_name="DOMAIN\\me",
            null_dacl=False,
            writers=(),
            unparsable_ace_types=(),
            volume_is_local=True,
        )
        fields.update(over)
        return windows_acl.ComponentSecurity(**fields)

    @pytest.fixture
    def _on_windows(self, monkeypatch):
        """Pretend to be on Windows with a verifiable launcher SID."""
        monkeypatch.setattr(source.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(source.platform_compat, "current_user_sid", lambda: self._ME)

    def _serve(self, monkeypatch, by_node: dict):
        """Make ``describe`` answer from ``by_node`` keyed on resolved path.

        Patched on the module the shared policy reads through
        (``github_runner.windows_acl``). A value may be a ``ComponentSecurity`` to
        return or an exception to raise. A node not in the map gets a clean,
        launcher-owned descriptor, so a test states only the ancestor it is about.
        """

        def _describe(path):
            value = by_node.get(Path(path), self._clean())
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(github_runner.windows_acl, "describe", _describe)

    def _chain(self, monkeypatch, tmp_path):
        """A two-level chain (home / data-home) scoped by ``_chain_the_launcher_owns``."""
        home = tmp_path / "home"
        data_home = home / "data-home"
        data_home.mkdir(parents=True)
        monkeypatch.setattr(source.Path, "home", staticmethod(lambda: home))
        return home, data_home

    def test_a_clean_chain_is_accepted(self, monkeypatch, tmp_path, _on_windows):
        _, data_home = self._chain(monkeypatch, tmp_path)
        self._serve(monkeypatch, {})
        assert source._first_replaceable(data_home) is None

    def test_a_foreign_owned_ancestor_is_refused(self, monkeypatch, tmp_path, _on_windows):
        home, data_home = self._chain(monkeypatch, tmp_path)
        self._serve(monkeypatch, {home.resolve(): self._clean(owner_sid=self._PEER)})
        result = source._first_replaceable(data_home)
        assert result is not None
        node, reason = result
        assert node == home.resolve()
        assert "owned by another account" in reason

    def test_a_peer_writer_on_an_ancestor_is_refused(self, monkeypatch, tmp_path, _on_windows):
        home, data_home = self._chain(monkeypatch, tmp_path)
        peer = windows_acl.Writer(
            sid=self._PEER, name="DOMAIN\\peer", rights=("FILE_DELETE_CHILD",)
        )
        self._serve(monkeypatch, {home.resolve(): self._clean(writers=(peer,))})
        result = source._first_replaceable(data_home)
        assert result is not None
        node, reason = result
        assert node == home.resolve()
        assert self._PEER in reason and "replaced" in reason

    def test_the_launchers_own_write_is_not_a_foreign_swap(
        self, monkeypatch, tmp_path, _on_windows
    ):
        # The relaxed-mode analog of trusting ``uid``: the launcher is answerable
        # for the chain, so its own substitution-capable right on a directory it
        # owns is not a hostile writer.
        _, data_home = self._chain(monkeypatch, tmp_path)
        me = windows_acl.Writer(sid=self._ME, name="DOMAIN\\me", rights=("WRITE_DAC",))
        self._serve(monkeypatch, {data_home.resolve(): self._clean(writers=(me,))})
        assert source._first_replaceable(data_home) is None

    def test_well_known_system_sids_are_trusted(self, monkeypatch, tmp_path, _on_windows):
        # SYSTEM / Administrators / TrustedInstaller stand in for root, as both
        # owner and writer -- a principal already owning the host does not need a
        # directory to substitute the tarball.
        home, data_home = self._chain(monkeypatch, tmp_path)
        system = next(iter(windows_acl.WELL_KNOWN_TRUSTED_SIDS))
        admin = windows_acl.Writer(
            sid="S-1-5-32-544", name="BUILTIN\\Administrators", rights=("WRITE_OWNER",)
        )
        self._serve(
            monkeypatch,
            {home.resolve(): self._clean(owner_sid=system, writers=(admin,))},
        )
        assert source._first_replaceable(data_home) is None

    def test_owner_rights_from_the_data_home_lockdown_is_trusted(
        self, monkeypatch, tmp_path, _on_windows
    ):
        # ensure_data_home locks the data home with an inheritable owner-only DACL
        # whose grants are (S-1-3-4, me), so every node on this chain carries an
        # Owner Rights full-control ACE. It is this account's own lockdown, not a
        # foreign writer -- refusing on it would reject the ordinary locked-down
        # home on every Windows launch. The provider-executable walk leaves
        # trust_owner_rights off, which is why the staging walk must set it.
        _, data_home = self._chain(monkeypatch, tmp_path)
        owner_rights = windows_acl.Writer(
            sid=self._OWNER_RIGHTS, name="NT AUTHORITY\\OWNER RIGHTS", rights=("WRITE_DAC",)
        )
        self._serve(monkeypatch, {data_home.resolve(): self._clean(writers=(owner_rights,))})
        assert source._first_replaceable(data_home) is None

    def test_the_policy_is_the_shared_github_runner_check(self, monkeypatch, tmp_path, _on_windows):
        # The per-node decision must BE
        # github_runner.check_provider_path_component_windows, not a second copy. If
        # that function is not called, the walk cannot have asked the policy -- so a
        # peer-writable ancestor would wrongly pass.
        _, data_home = self._chain(monkeypatch, tmp_path)
        calls: list = []
        real = github_runner.check_provider_path_component_windows

        def _spy(path, **kw):
            calls.append((Path(path), kw.get("strict"), kw.get("trust_owner_rights")))
            return real(path, **kw)

        monkeypatch.setattr(github_runner, "check_provider_path_component_windows", _spy)
        self._serve(monkeypatch, {})
        source._first_replaceable(data_home)
        assert calls, "the shared github_runner check was never called"
        # Relaxed mode, and Owner Rights trusted on this chain.
        assert all(strict is False for _, strict, _ in calls)
        assert all(tor is True for _, _, tor in calls)

    def test_a_null_dacl_ancestor_is_refused(self, monkeypatch, tmp_path, _on_windows):
        home, data_home = self._chain(monkeypatch, tmp_path)
        self._serve(monkeypatch, {home.resolve(): self._clean(null_dacl=True)})
        result = source._first_replaceable(data_home)
        assert result is not None and "NULL DACL" in result[1]

    def test_an_unparsable_ace_type_refuses(self, monkeypatch, tmp_path, _on_windows):
        home, data_home = self._chain(monkeypatch, tmp_path)
        self._serve(monkeypatch, {home.resolve(): self._clean(unparsable_ace_types=(9,))})
        result = source._first_replaceable(data_home)
        assert result is not None and "cannot evaluate" in result[1]

    def test_a_remote_volume_ancestor_is_refused(self, monkeypatch, tmp_path, _on_windows):
        # The trusted SIDs are machine-local alias SIDs; off a local volume they
        # name a different principal, so trusting them would be unsound.
        home, data_home = self._chain(monkeypatch, tmp_path)
        self._serve(monkeypatch, {home.resolve(): self._clean(volume_is_local=False)})
        result = source._first_replaceable(data_home)
        assert result is not None and "local volume" in result[1]

    def test_an_unreadable_descriptor_fails_closed(self, monkeypatch, tmp_path, _on_windows):
        home, data_home = self._chain(monkeypatch, tmp_path)
        self._serve(
            monkeypatch,
            {home.resolve(): windows_acl.AclUnavailable("GetNamedSecurityInfo failed")},
        )
        result = source._first_replaceable(data_home)
        assert result is not None and "unreadable" in result[1]

    def test_an_unverifiable_launcher_sid_fails_closed(self, monkeypatch, tmp_path):
        # The POSIX arm cannot reach this (geteuid always answers), but the Windows
        # token read can fail, and an unverifiable launcher is not a trusted one.
        _, data_home = self._chain(monkeypatch, tmp_path)
        monkeypatch.setattr(source.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(source.platform_compat, "current_user_sid", lambda: None)
        self._serve(monkeypatch, {})
        result = source._first_replaceable(data_home)
        assert result is not None and "SID" in result[1]

    def test_a_windows_chain_with_a_peer_writable_ancestor_is_refused(
        self, monkeypatch, tmp_path, _on_windows
    ):
        # The core guarantee: a Windows chain is walked, not waved through, so a
        # peer-writable ancestor is caught rather than returning None.
        home, data_home = self._chain(monkeypatch, tmp_path)
        peer = windows_acl.Writer(sid=self._PEER, name="DOMAIN\\peer", rights=("DELETE",))
        self._serve(monkeypatch, {home.resolve(): self._clean(writers=(peer,))})
        assert source._first_replaceable(data_home) is not None

    def _relocated_chain(self, monkeypatch, tmp_path):
        """A data home relocated OUTSIDE the operator's home, so the whole resolved
        chain -- drive root included -- is in scope and ``_chain_the_launcher_owns``
        returns the anchor as its first node.

        ``Path.home()`` is pinned to a sibling of the data home (not an ancestor),
        matching the relocated shape the live test uses. Returns ``(root, data_home)``
        where ``root`` is the anchor node the walk visits first.
        """
        sibling_home = tmp_path / "elsewhere-home"
        sibling_home.mkdir()
        data_home = tmp_path / "relocated" / "data-home"
        data_home.mkdir(parents=True)
        monkeypatch.setattr(source.Path, "home", staticmethod(lambda: sibling_home))
        root = Path(data_home.resolve().anchor)
        return root, data_home

    def test_a_delete_only_grant_on_the_volume_root_is_accepted(
        self, monkeypatch, tmp_path, _on_windows
    ):
        # A volume root cannot be renamed or deleted, so a writer whose only right
        # on the root is DELETE grants no swap vector. The stock secondary-NTFS
        # ACL (Authenticated Users: Modify, which carries DELETE but not
        # FILE_DELETE_CHILD) must not refuse a data home relocated under D:\.
        root, data_home = self._relocated_chain(monkeypatch, tmp_path)
        au = windows_acl.Writer(
            sid="S-1-5-11", name="NT AUTHORITY\\Authenticated Users", rights=("DELETE",)
        )
        self._serve(monkeypatch, {root: self._clean(writers=(au,))})
        assert source._first_replaceable(data_home) is None

    def test_file_delete_child_on_the_volume_root_is_still_refused(
        self, monkeypatch, tmp_path, _on_windows
    ):
        # The root exemption is DELETE-only: FILE_DELETE_CHILD on the root is the
        # right that actually lets a peer swap a child of the root, so it refuses.
        root, data_home = self._relocated_chain(monkeypatch, tmp_path)
        peer = windows_acl.Writer(
            sid=self._PEER, name="DOMAIN\\peer", rights=("FILE_DELETE_CHILD",)
        )
        self._serve(monkeypatch, {root: self._clean(writers=(peer,))})
        result = source._first_replaceable(data_home)
        assert result is not None
        node, reason = result
        assert node == root
        assert self._PEER in reason and "replaced" in reason

    def test_a_delete_right_on_a_non_root_node_is_still_refused(
        self, monkeypatch, tmp_path, _on_windows
    ):
        # The exemption applies ONLY to the volume root. A peer with DELETE on a
        # non-root ancestor can rename that entry away, so it must still refuse --
        # the ordinary peer-writable-ancestor hazard this PR exists to catch.
        root, data_home = self._relocated_chain(monkeypatch, tmp_path)
        mid = data_home.resolve().parent
        assert mid != root  # a genuine intermediate node, not the anchor
        peer = windows_acl.Writer(sid=self._PEER, name="DOMAIN\\peer", rights=("DELETE",))
        self._serve(monkeypatch, {mid: self._clean(writers=(peer,))})
        result = source._first_replaceable(data_home)
        assert result is not None
        node, reason = result
        assert node == mid
        assert self._PEER in reason and "replaced" in reason

    def test_only_the_volume_root_is_passed_volume_root_true(
        self, monkeypatch, tmp_path, _on_windows
    ):
        # Pin that the walk marks exactly the anchor node volume_root=True and
        # every other node volume_root=False, so the exemption cannot silently
        # widen to intermediate directories.
        root, data_home = self._relocated_chain(monkeypatch, tmp_path)
        seen: dict = {}
        real = github_runner.check_provider_path_component_windows

        def _spy(path, **kw):
            seen[Path(path)] = kw.get("volume_root")
            return real(path, **kw)

        monkeypatch.setattr(github_runner, "check_provider_path_component_windows", _spy)
        self._serve(monkeypatch, {})
        source._first_replaceable(data_home)
        assert seen.get(root) is True
        assert all(flag is False for node, flag in seen.items() if node != root)

    def test_an_unexpected_describe_exception_fails_closed(
        self, monkeypatch, tmp_path, _on_windows
    ):
        # describe() can raise something other than AclUnavailable (e.g. an OSError
        # from the volume probe). The shared check only maps AclUnavailable to a
        # ValueError, so the walk must catch the rest and fail closed on the node
        # rather than let a raw exception escape the launch prologue.
        home, data_home = self._chain(monkeypatch, tmp_path)
        self._serve(monkeypatch, {home.resolve(): OSError("volume probe failed")})
        result = source._first_replaceable(data_home)
        assert result is not None
        node, reason = result
        assert node == home.resolve()
        assert "could not be checked" in reason and "OSError" in reason

    def test_the_windows_refusal_carries_the_icacls_remedy(self, monkeypatch, tmp_path):
        # Mutation guard: the refusal message on Windows must give icacls guidance,
        # not the POSIX chmod/setfacl remedy, and must break inheritance first
        # because the offending ACE on these chains is usually inherited.
        monkeypatch.setattr(source.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            source, "_first_replaceable", lambda base: (Path("D:\\crew"), "is peer-writable")
        )
        monkeypatch.setattr(source, "config_dir", lambda: tmp_path)
        with pytest.raises(aws.AWSError) as excinfo:
            source._staging_dir()
        message = str(excinfo.value)
        assert "icacls" in message and "/inheritance:d" in message
        assert "chmod" not in message and "setfacl" not in message

    @pytest.mark.skipif(sys.platform != "win32", reason="real Windows security descriptors")
    @pytest.mark.parametrize("relocated", [False, True])
    def test_a_real_data_home_passes_the_live_descriptor_walk(
        self, monkeypatch, tmp_path, relocated
    ):
        """The walk accepts a real, freshly-locked-down data home on a Windows host.

        This is the one case the synthetic-descriptor tests cannot cover: it runs
        the live ``windows_acl.describe`` read (``advapi32``) against a data home
        that ``ensure_data_home`` actually created and tightened with its
        inheritable owner-only DACL, with NOTHING mocked. It is the proof that the
        trusted set -- the launcher's SID plus Owner Rights (``S-1-3-4``) plus the
        well-known machine SIDs -- matches the entries the real lockdown and the
        real profile chain carry, rather than the author's model of them. A missing
        trusted principal would refuse the ordinary launch, and only a real
        descriptor would reveal it.

        Two shapes, because they walk different chains
        (:func:`source._chain_the_launcher_owns`): the default-shaped home nested
        under the operator's own home stops the walk at that home, while a home
        relocated outside it is walked whole, ancestors included.

        Skipped off Windows: ``describe`` raises ``AclUnavailable`` on a platform
        with no security API, so this asserts nothing there. It runs on the Windows
        CI shard, which is the only place the live read exists.
        """
        if relocated:
            # Outside the operator's profile home, so the whole resolved chain --
            # up to the drive root -- is in scope and every ancestor's real
            # descriptor is read. Path.home() is pinned to a SIBLING of the data
            # home (not an ancestor), so the home is genuinely outside it; without
            # this, on a Windows runner tmp_path sits under %TEMP% inside the real
            # profile, _chain_the_launcher_owns stops at the profile, and this
            # case would walk the same short chain as the one below.
            sibling_home = tmp_path / "elsewhere-home"
            sibling_home.mkdir()
            monkeypatch.setattr(source.Path, "home", staticmethod(lambda: sibling_home))
            base = tmp_path / "relocated-data-home"
        else:
            # Under a stand-in profile home, the default shape: the walk stops at
            # that home.
            profile = tmp_path / "profile"
            profile.mkdir()
            monkeypatch.setattr(source.Path, "home", staticmethod(lambda: profile))
            base = profile / ".kiro" / "crew"
        monkeypatch.setenv("KIROCREW_HOME", str(base))

        # Build and lock down the real data home exactly as the gateway prologue
        # does, then walk it with the live descriptor read and no mocks. For the
        # relocated shape, assert the walk reached the drive root, so a passing run
        # proves a full above-profile chain returns None rather than a short one.
        home = ensure_data_home()
        if relocated:
            chain = source._chain_the_launcher_owns(home)
            assert chain[0] == Path(
                home.resolve().anchor
            ), "relocated chain must start at the drive root"
        assert source._first_replaceable_windows(home) is None
