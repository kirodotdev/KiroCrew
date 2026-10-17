"""Tests for the AppArmor profile that attaches to the desktop launcher.

The attached path must be safe to grant, a takeover of it is refused, the
ownership check names the service account, and install, status and uninstall
stay fail-soft and honest.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _clear_sudo_user,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
    durable_dir_fixture,
)

# The whole feature is Linux-only (one Ubuntu kernel restriction), but the backend
# test suite also runs on Windows, where these POSIX paths are not absolute, do
# not resolve, and render with backslashes.
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX path semantics; the feature is Linux-only"
)


def _trust_ancestors_above(monkeypatch, base, *, extra=None):
    """Pin ancestor owners while preserving fixture ownership and permission bits."""
    owners = dict.fromkeys(base.resolve().parents, 0)
    owners.update(extra or {})
    real_stat = Path.stat

    def fake_stat(self, **kwargs):
        info = real_stat(self, **kwargs)
        if self not in owners:
            return info

        class AncestorStat:
            st_uid = owners[self]

            def __getattr__(self, name):
                return getattr(info, name)

        return AncestorStat()

    monkeypatch.setattr(Path, "stat", fake_stat)


class TestLauncherExecPathIsSafeToAttach:
    """An attachment is a permission grant keyed on a path.

    These are the two rules the whole direct-launch feature rests on: the path
    must not be substitutable by another local user, and it must not be shared
    with unrelated programs. Everything else in the launcher profile is cosmetic
    by comparison, so each rejection is pinned here.
    """

    def test_rejects_a_relative_path(self):
        from kiro_crew.service import apparmor as aa

        resolved, problem = aa.validate_exec_path("kirocrew.AppImage")

        assert resolved is None
        assert "absolute" in problem

    def test_rejects_an_empty_path(self):
        from kiro_crew.service import apparmor as aa

        assert aa.validate_exec_path("   ")[0] is None

    def test_rejects_a_path_that_does_not_exist(self, tmp_path):
        from kiro_crew.service import apparmor as aa

        resolved, problem = aa.validate_exec_path(str(tmp_path / "nope.AppImage"))

        assert resolved is None
        assert "could not be resolved" in problem

    def test_rejects_a_directory(self, tmp_path):
        from kiro_crew.service import apparmor as aa

        resolved, problem = aa.validate_exec_path(str(tmp_path))

        assert resolved is None
        assert "not a regular file" in problem

    @posix_only
    def test_rejects_a_world_writable_location(self, tmp_path, monkeypatch):
        """/tmp and friends: another local user could put their file there.

        The constant is monkeypatched rather than writing to the real /tmp so the
        assertion holds on macOS too, where /tmp resolves to /private/tmp.
        """
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", (str(tmp_path) + "/",))

        resolved, problem = aa.validate_exec_path(str(app))

        assert resolved is None
        assert "any local user can" in problem
        assert "Move" in problem, "must tell the user what to do instead"

    def test_rejects_the_appimage_runtime_mount(self):
        """/tmp/.mount_XXXXXX is a fresh random path every launch."""
        from kiro_crew.service import apparmor as aa

        assert "/tmp/" in aa._UNSAFE_EXEC_PARENTS

    @pytest.mark.parametrize(
        "path",
        [
            "/usr/bin/python3",
            "/usr/bin/python3.12",
            "/bin/sh",
            "/bin/bash",
            "/usr/bin/node",
            "/usr/local/bin/node",
            "/usr/bin/perl",
            "/usr/bin/env",
            "/bin/busybox",
        ],
    )
    def test_shared_interpreters_are_recognised(self, path):
        """Attaching here would grant userns to every program that runs it."""
        from kiro_crew.service import apparmor as aa

        assert aa._SHARED_INTERPRETER_RE.match(path), path

    @pytest.mark.parametrize(
        "path",
        [
            "/home/user/AppImages/kirocrew.AppImage",
            "/opt/KiroCrew/kirocrew",
            "/usr/bin/kirocrew-desktop",
            "/usr/local/bin/pythonish-app",
        ],
    )
    def test_real_application_paths_are_not_mistaken_for_interpreters(self, path):
        from kiro_crew.service import apparmor as aa

        assert not aa._SHARED_INTERPRETER_RE.match(path), path

    @posix_only
    def test_rejects_a_shared_interpreter_end_to_end(self):
        """/bin/sh exists on every POSIX host, and resolves to a shell either way."""
        from kiro_crew.service import apparmor as aa

        resolved, problem = aa.validate_exec_path("/bin/sh")

        assert resolved is None
        assert "shared system interpreter" in problem
        assert "service install" in problem, "must name the supported alternative"

    @posix_only
    def test_resolves_before_validating_so_a_symlink_cannot_smuggle_a_grant(self, tmp_path):
        """A link in a safe directory pointing at a shared interpreter.

        Validating the given path instead of the resolved one would write an
        attachment that the kernel matches against /bin/sh — a host-wide grant
        reached through a name that looks harmless.
        """
        from kiro_crew.service import apparmor as aa

        link = tmp_path / "kirocrew.AppImage"
        link.symlink_to("/bin/sh")

        resolved, problem = aa.validate_exec_path(str(link))

        assert resolved is None
        assert "shared system interpreter" in problem

    @pytest.mark.parametrize("bad", ["star*", "quest?", "brack[et]", "brace{x}", 'quo"te'])
    def test_rejects_glob_metacharacters(self, durable_dir, bad):
        """AppArmor reads an attachment as a glob even inside quotes."""
        from kiro_crew.service import apparmor as aa

        app = durable_dir / f"{bad}.AppImage"
        try:
            app.write_text("#!/bin/sh\n")
        except OSError:
            pytest.skip("filesystem rejects this name")

        resolved, problem = aa.validate_exec_path(str(app))

        assert resolved is None
        assert "glob syntax" in problem

    @posix_only
    def test_accepts_a_durable_path_with_a_space(self, durable_dir):
        """A space is fine — the rendered attachment is quoted."""
        from kiro_crew.service import apparmor as aa

        app = durable_dir / "Kiro Crew.AppImage"
        app.write_text("#!/bin/sh\n")

        resolved, problem = aa.validate_exec_path(str(app))

        assert problem == ""
        assert resolved == app.resolve()


@posix_only
class TestATakeoverOfTheAttachedPathIsRefused:
    """The prefix denylist is a message; filesystem modes are the guarantee.

    A world-writable directory outside the denylist — `/srv/shared` at 0777, a
    group-writable `/opt/apps`, a permissive network mount — would sail past a
    prefix check, and an attachment there lets any local user drop in their own
    executable and inherit the userns grant. These tests pin that refusal.
    """

    def test_a_world_writable_directory_outside_the_denylist_is_refused(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.service import apparmor as aa

        shared = tmp_path / "shared"
        shared.mkdir()
        app = shared / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        os.chmod(shared, 0o777)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the lax mode IS the fixture, not the behaviour under test: this stages a world-writable directory outside the prefix denylist precisely so the assertion below can prove validate_exec_path() refuses to attach an AppArmor userns grant there. Removing it deletes this regression test.  # noqa: E501  # fmt: skip
        # Empty the denylist so this can only be caught by the mode walk — the
        # whole point of the finding is that a prefix list does not cover it.
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())

        resolved, problem = aa.validate_exec_path(str(app))

        assert resolved is None
        assert "world-writable" in problem
        assert str(shared) in problem, "must name the offending component"

    def test_a_group_writable_file_is_refused(self, tmp_path, monkeypatch):
        """Group members could replace the binary that receives the grant."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o775)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- group-writable IS the fixture: the test proves a file group members could replace is refused as an attachment target.  # noqa: E501  # fmt: skip
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())
        monkeypatch.setattr(aa, "_substitutable_by_others", aa._substitutable_by_others)

        problem = aa._substitutable_by_others(app.resolve())

        assert problem is not None
        assert "group- or world-writable" in problem

    def test_a_writable_ancestor_is_enough_to_refuse(self, tmp_path, monkeypatch):
        """Renaming a writable parent re-points the same absolute path."""
        from kiro_crew.service import apparmor as aa

        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        app = inner / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- tight leaf; the writable ANCESTOR below is what this test exercises.  # noqa: E501  # fmt: skip
        os.chmod(inner, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- tight leaf; the writable ANCESTOR below is what this test exercises.  # noqa: E501  # fmt: skip
        os.chmod(outer, 0o777)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the writable ancestor IS the fixture: renaming a world-writable parent re-points the same absolute path at an attacker's file, so the test proves the mode walk climbs to / instead of checking the leaf alone.  # noqa: E501  # fmt: skip

        problem = aa._substitutable_by_others(app.resolve())

        assert problem is not None
        assert str(outer) in problem

    def test_a_root_owned_system_binary_is_refused(self):
        """Pins the reviewed refusal behaviour.

        The shared-interpreter regex is a BLOCKLIST and blocklists leak: it names
        python, perl, ruby, node and the shells, but not java, mono, dotnet, php,
        lua, wine, R or qemu-*. `--path /usr/bin/java` would have granted
        unprivileged userns to every Java process on the host. Requiring the target
        to be owned by the caller closes the whole class rather than adding names
        to the list.
        """
        from kiro_crew.service import apparmor as aa

        target = Path("/usr/bin/env")  # root-owned on every POSIX host
        if not target.exists():
            pytest.skip("need /usr/bin/env")
        target_uid = target.stat().st_uid
        if target_uid == os.getuid():
            pytest.skip("need a binary not owned by the test user")
        if target_uid != 0:
            pytest.skip("need a root-owned binary; this host has uid %d" % target_uid)

        problem = aa._substitutable_by_others(target.resolve())

        assert problem is not None
        assert "owned by root" in problem
        assert "shared with every user" in problem

    @pytest.mark.parametrize(
        "shared",
        ["/usr/bin/java", "/usr/bin/mono", "/usr/bin/php", "/usr/lib/jvm/bin/java"],
    )
    def test_shared_runtimes_absent_from_the_blocklist_are_still_refused(
        self, shared, monkeypatch, tmp_path
    ):
        """The ownership rule covers what the interpreter regex never listed."""
        from kiro_crew.service import apparmor as aa

        stand_in = tmp_path / Path(shared).name
        stand_in.write_text("#!/bin/sh\n")
        assert not aa._SHARED_INTERPRETER_RE.match(
            shared
        ), f"{shared} is not on the blocklist, which is the point"

        # Same shape as the real thing: root-owned, so not ours.
        class RootStat:
            st_uid = 0
            st_mode = stand_in.stat().st_mode

        monkeypatch.setattr(Path, "stat", lambda self, **_k: RootStat())

        assert aa._substitutable_by_others(stand_in) is not None

    def test_a_file_you_own_under_a_tight_chain_is_accepted(self, tmp_path, monkeypatch):
        """Positive control: an AppImage you downloaded is owned by you."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- an executable must be executable; the point of this test is that a user-owned 0755 file under a tight chain is ACCEPTED.  # noqa: E501  # fmt: skip
        # The ancestor chain of a pytest tmp_path is 0700 on macOS but includes
        # /tmp on Linux, so only assert the ownership half here; the mode walk has
        # its own tests above. On an NFS host the mount roots stat as uid 65534
        # (nobody), which the ownership walk would refuse; simulate the normal
        # root-owned chain above tmp_path so this positive control is not a
        # test-environment artifact (the fixture's own file keeps its real stat).
        _trust_ancestors_above(monkeypatch, tmp_path)
        problem = aa._substitutable_by_others(app)

        assert problem is None or "world-writable" in problem, problem
        assert problem is None or "owned by" not in problem

    def test_tmp_is_refused_outright(self):
        """/tmp is caught twice over: root-owned AND world-writable.

        Ownership is checked first, so that is the reason reported. Either is
        disqualifying; what matters is that the most obvious wrong answer a user
        could give is refused.
        """
        from kiro_crew.service import apparmor as aa

        tmp_uid = Path("/tmp").stat().st_uid
        if tmp_uid not in (0, os.getuid()):
            pytest.skip("/tmp owned by uid %d (not root or current user)" % tmp_uid)

        problem = aa._substitutable_by_others(Path("/tmp"))

        assert problem is not None
        assert "owned by root" in problem or "world-writable" in problem

    def test_a_file_owned_by_another_user_is_refused(self, tmp_path, monkeypatch):
        """The owner of the file chooses which binary gets the grant."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        real = app.stat()

        class ForeignStat:
            st_uid = 4242
            st_mode = real.st_mode

        monkeypatch.setattr(Path, "stat", lambda self, **_k: ForeignStat())

        problem = aa._substitutable_by_others(app)

        assert problem is not None
        assert "uid 4242" in problem
        assert "not by you" in problem

    @staticmethod
    def _tight_except(monkeypatch, base, loose):
        """Every component reads as mode-tight (no ``0o022``) except *loose* (``0o777``).

        Ancestors above *base* read as root's, the way :func:`_trust_ancestors_above`
        pins them, so a temp root owned by a third account on the host does not
        decide the verdict; the fixtures under *base* keep their real owner.
        """
        # ``os.path.realpath`` rather than ``Path.resolve``: a non-strict resolve
        # calls ``stat`` on its result, which would re-enter this fake.
        loose_real = os.path.realpath(loose)
        ancestors = {str(parent) for parent in Path(os.path.realpath(base)).parents}
        real_stat = Path.stat

        def fake_stat(self, **kwargs):
            info = real_stat(self, **kwargs)
            real = os.path.realpath(self)
            mode = info.st_mode | 0o022 if real == loose_real else info.st_mode & ~0o022
            uid = 0 if real in ancestors else info.st_uid
            return os.stat_result(
                (mode, info.st_ino, info.st_dev, info.st_nlink, uid) + tuple(info)[5:]
            )

        monkeypatch.setattr(Path, "stat", fake_stat)

    def test_a_writable_hop_in_the_middle_of_a_chain_is_refused(self, tmp_path, monkeypatch):
        """``trusted/app -> loose/hop -> trusted/real``: the hop's directory is walked.

        Both endpoints' chains are tight; only the directory holding the hop is
        writable, and a lexical chain over the collapsed path never names it.
        """
        from kiro_crew.service import apparmor as aa

        trusted = tmp_path / "trusted"
        trusted.mkdir()
        loose = tmp_path / "loose"
        loose.mkdir()
        real = trusted / "real.AppImage"
        real.write_text("#!/bin/sh\n")
        hop = loose / "hop"
        hop.symlink_to(real)
        app = trusted / "kirocrew.AppImage"
        app.symlink_to(hop)
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())
        self._tight_except(monkeypatch, tmp_path, loose)

        resolved, problem = aa.validate_exec_path(str(app))

        assert resolved is None
        assert "group- or world-writable" in problem
        assert str(loose.resolve()) in problem, "must name the offending component"

    def test_a_writable_parent_of_a_symlinked_component_is_refused(self, tmp_path, monkeypatch):
        """``prefix/bin -> holder/bin``: ``prefix`` holds the link and is walked.

        The collapsed path's chain runs ``holder/bin``, ``holder``, ... and never
        names ``prefix``, whose owner can re-point ``bin`` wholesale.
        """
        from kiro_crew.service import apparmor as aa

        prefix = tmp_path / "prefix"
        prefix.mkdir()
        real_bin = tmp_path / "holder" / "bin"
        real_bin.mkdir(parents=True)
        leaf = real_bin / "kirocrew.AppImage"
        leaf.write_text("#!/bin/sh\n")
        (prefix / "bin").symlink_to(real_bin)
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())
        self._tight_except(monkeypatch, tmp_path, prefix)

        resolved, problem = aa.validate_exec_path(str(prefix / "bin" / "kirocrew.AppImage"))

        assert resolved is None
        assert "group- or world-writable" in problem
        assert str(prefix.resolve()) in problem, "must name the offending component"

    def test_a_chain_through_tight_directories_is_still_accepted(self, tmp_path, monkeypatch):
        """Strictly a widening: the hop shape with every directory tight is accepted."""
        from kiro_crew.service import apparmor as aa

        trusted = tmp_path / "trusted"
        trusted.mkdir()
        hops = tmp_path / "hops"
        hops.mkdir()
        real = trusted / "real.AppImage"
        real.write_text("#!/bin/sh\n")
        hop = hops / "hop"
        hop.symlink_to(real)
        app = trusted / "kirocrew.AppImage"
        app.symlink_to(hop)
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())
        self._tight_except(monkeypatch, tmp_path, tmp_path / "nothing-is-loose")

        resolved, problem = aa.validate_exec_path(str(app))

        assert problem == ""
        assert resolved == real.resolve()

    def test_a_walk_that_cannot_be_enumerated_is_refused(self, tmp_path, monkeypatch):
        """``None`` from the walk is a refusal that says so, never a shorter chain."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        monkeypatch.setattr(aa.platform_compat, "traversed_components", lambda _path: None)

        problem = aa._substitutable_by_others(app.resolve(), candidate=app)

        assert problem is not None
        assert "could not be inspected" in problem


@posix_only
class TestExpectedUidOverride:
    """The systemd service case checks ownership against the SERVICE
    account, not the installer process's own uid — a different account when
    ``kirocrew service install`` itself runs as root or under ``sudo``."""

    def test_a_file_owned_by_the_expected_uid_is_accepted(self, tmp_path, monkeypatch):
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        real_uid = app.stat().st_uid

        # Simulate the normal root-owned ancestor chain above tmp_path; on an
        # NFS host the real mount roots stat as uid 65534 (nobody) and would
        # wrongly fail this accept case (see _trust_ancestors_above).
        _trust_ancestors_above(monkeypatch, tmp_path)
        problem = aa._substitutable_by_others(app, expected_uid=real_uid)

        assert problem is None or "world-writable" in problem, problem
        assert problem is None or "owned by" not in problem

    def test_a_file_owned_by_the_installer_but_not_the_expected_account_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The critical case: the venv script IS owned by
        whoever is running this Python process (e.g. root, under ``sudo
        kirocrew service install``), but that is not the account the SERVICE
        runs as -- checking against the installer's own uid would wrongly
        accept an attachment that grants a different human's process the
        namespace capability."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        installer_uid = app.stat().st_uid
        monkeypatch.setattr(os, "getuid", lambda: installer_uid, raising=False)
        # Simulate the normal root-owned ancestor chain above tmp_path so the
        # accept-half below is not defeated by this NFS host's nobody-owned
        # (uid 65534) mount roots; the fixture's own file keeps its real owner.
        _trust_ancestors_above(monkeypatch, tmp_path)

        # Accepted against the installer's own uid (legacy AppImage
        # semantics): the OWNERSHIP rule must not fire. Assert only that half —
        # the ancestor mode walk legitimately flags ``/tmp`` on Linux CI, as
        # ``test_a_file_you_own_under_a_tight_chain_is_accepted`` documents.
        problem_own = aa._substitutable_by_others(app)
        assert problem_own is None or "world-writable" in problem_own, problem_own
        assert problem_own is None or "owned by" not in problem_own
        # ...but refused once an expected_uid names a DIFFERENT account, even
        # though the file's real owner never changed. Ownership is checked
        # before the mode walk, so this message is deterministic.
        problem = aa._substitutable_by_others(app, expected_uid=installer_uid + 1)

        assert problem is not None
        assert f"uid {installer_uid}" in problem
        assert "not by the expected account" in problem

    def test_a_foreign_owned_ancestor_is_refused(self, tmp_path, monkeypatch):
        """A directory's OWNER can rename or
        replace what is inside it regardless of the 0o022 mode bits, so a
        tight-mode ancestor owned by a THIRD account (not root, not the
        expected owner) still makes the whole path substitutable — the mode
        walk alone must not accept it."""
        from kiro_crew.service import apparmor as aa

        foreign = tmp_path / "foreign"
        foreign.mkdir()
        app = foreign / "kirocrew"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        os.chmod(foreign, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- deliberately TIGHT against the 0o022 walk; the foreign OWNER below is what this test exercises.  # noqa: E501  # fmt: skip
        real_uid = app.stat().st_uid
        foreign_resolved = foreign.resolve()
        real_stat = Path.stat

        def fake_stat(self, **kwargs):
            info = real_stat(self, **kwargs)
            if self == foreign_resolved:

                class ForeignStat:
                    st_uid = real_uid + 7
                    st_mode = info.st_mode

                return ForeignStat()
            return info

        monkeypatch.setattr(Path, "stat", fake_stat)

        problem = aa._substitutable_by_others(app, expected_uid=real_uid)

        assert problem is not None
        assert f"owned by uid {real_uid + 7}" in problem
        assert "rename or replace" in problem

    def test_a_root_owned_ancestor_stays_trusted(self, tmp_path, monkeypatch):
        """The system chain (/, /home, /opt) is root-owned; the ancestor
        ownership rule must not reject it — root can already edit
        /etc/apparmor.d directly, so refusing root-owned ancestors would
        reject every real install for no gain."""
        from kiro_crew.service import apparmor as aa

        parent = tmp_path / "sys"
        parent.mkdir()
        app = parent / "kirocrew"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        os.chmod(parent, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        real_uid = app.stat().st_uid
        parent_resolved = parent.resolve()

        # Pin the fixture's own parent to root (the property under test: a
        # root-owned ancestor stays trusted). The same helper also reports the
        # ancestors ABOVE tmp_path as root-owned, so this host's nobody-owned
        # (uid 65534) NFS mount roots do not defeat the assertion.
        _trust_ancestors_above(monkeypatch, tmp_path, extra={parent_resolved: 0})

        problem = aa._substitutable_by_others(app, expected_uid=real_uid)

        # The mode walk may still flag /tmp on Linux CI (an ancestor outside
        # this fixture); assert only that the OWNERSHIP rules did not fire.
        assert problem is None or "owned by" not in problem, problem

    def test_validate_exec_path_forwards_expected_uid(self, tmp_path, monkeypatch):
        """End-to-end through the public entry point, not just the private
        ownership helper -- a regression that stops threading the kwarg
        through validate_exec_path would not be caught by the unit test above
        alone."""
        from kiro_crew.service import apparmor as aa

        app = tmp_path / "kirocrew"
        app.write_text("#!/bin/sh\n")
        os.chmod(app, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        real_uid = app.stat().st_uid
        # On Linux CI ``tmp_path`` lives under ``/tmp``, which the prefix
        # denylist refuses before ownership is ever consulted. Neutralise ONLY
        # that rule; the ownership check under test runs unstubbed, and it
        # fires before the mode walk so the message below is deterministic.
        monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())

        resolved, problem = aa.validate_exec_path(str(app), expected_uid=real_uid + 1)

        assert resolved is None
        assert "not by the expected account" in problem


class TestLauncherProfileRendering:
    """The rendered profile must grant one permission and attach to one path."""

    def _render(self, path="/home/u/Apps/kirocrew.AppImage", abi="4.0"):
        from kiro_crew.service import apparmor as aa

        return aa.render_launcher_profile(abi, Path(path))

    def test_grants_only_userns(self):
        """The rule body must contain `userns,` and nothing else.

        Asserted against the extracted body rather than the file text: the header
        comment legitimately discusses mount namespaces and credential paths, so
        a substring search over the whole profile would be testing the prose.
        """
        text = self._render()

        body = text.split("{", 1)[1].rsplit("}", 1)[0]
        rules = [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith(("#", "include "))
        ]

        assert rules == ["userns,"], rules

    @posix_only
    def test_attaches_to_the_given_path_in_quotes(self):
        from kiro_crew.service import apparmor as aa

        text = self._render("/home/u/My Apps/kirocrew.AppImage")

        assert (
            f'profile {aa.LAUNCHER_PROFILE_NAME} "/home/u/My Apps/kirocrew.AppImage" '
            "flags=(unconfined)" in text
        )

    def test_omits_the_abi_line_when_the_host_ships_none(self):
        """Declaring an abi file that is absent makes the profile fail to load."""
        text = self._render(abi=None)

        assert "abi <abi/" not in text
        assert "include <tunables/global>" in text

    def test_keeps_a_local_override_include(self):
        from kiro_crew.service import apparmor as aa

        assert f"include if exists <local/{aa.LAUNCHER_PROFILE_NAME}>" in self._render()

    def test_explains_that_moving_the_file_breaks_it(self):
        """The one failure mode the kernel gives no error for."""
        text = self._render()

        assert "Moving or renaming" in text
        assert "kirocrew sandbox status" in text

    @posix_only
    def test_round_trips_through_the_attachment_parser(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        profile = tmp_path / aa.LAUNCHER_PROFILE_NAME
        profile.write_text(self._render("/home/u/Apps/kirocrew.AppImage"))
        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", profile)

        assert aa.installed_attachment() == "/home/u/Apps/kirocrew.AppImage"

    def test_attachment_parser_returns_none_when_not_installed(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", tmp_path / "absent")

        assert aa.installed_attachment() is None


@posix_only
class TestLauncherStatusTellsTheTruth:
    """A stale attachment must not be reported as a working setup."""

    @staticmethod
    def _restricted(monkeypatch, aa):
        monkeypatch.setattr(aa, "apparmor_is_active", lambda: True)
        monkeypatch.setattr(aa, "userns_restricted", lambda: True)

    def test_unaffected_host_is_reported_as_fine(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "apparmor_is_active", lambda: True)
        monkeypatch.setattr(aa, "userns_restricted", lambda: False)

        ok, detail = aa.launcher_status("/home/u/Apps/kirocrew.AppImage")

        assert ok is True
        assert "does not restrict" in detail

    def test_missing_profile_on_an_appimage_launch_names_the_command(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        self._restricted(monkeypatch, aa)
        monkeypatch.setattr(aa, "installed_attachment", lambda: None)

        ok, detail = aa.launcher_status("/home/u/Apps/kirocrew.AppImage")

        assert ok is False
        assert "kirocrew sandbox install-profile" in detail

    def test_missing_profile_without_an_appimage_points_at_the_service(self, monkeypatch):
        """A foreground gateway has no safe path to attach to."""
        from kiro_crew.service import apparmor as aa

        self._restricted(monkeypatch, aa)
        monkeypatch.setattr(aa, "installed_attachment", lambda: None)
        monkeypatch.setattr(aa, "default_exec_path", lambda: None)

        ok, detail = aa.launcher_status(None)

        assert ok is False
        assert "kirocrew service install" in detail

    def test_a_moved_appimage_is_reported_as_not_covered(self, monkeypatch, durable_dir):
        """The kernel reports nothing here — the profile simply never matches."""
        from kiro_crew.service import apparmor as aa

        app = durable_dir / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        self._restricted(monkeypatch, aa)
        monkeypatch.setattr(aa, "installed_attachment", lambda: "/old/place/kirocrew.AppImage")

        ok, detail = aa.launcher_status(str(app))

        assert ok is False
        assert "/old/place/kirocrew.AppImage" in detail
        assert "does not apply" in detail
        assert "re-point" in detail

    def test_a_matching_attachment_is_reported_as_covered(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        app = durable_dir / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        self._restricted(monkeypatch, aa)
        monkeypatch.setattr(aa, "installed_attachment", lambda: str(app.resolve()))

        ok, detail = aa.launcher_status(str(app))

        assert ok is True
        assert str(app.resolve()) in detail


@posix_only
class TestLauncherInstallIsFailSoftAndHonest:
    """Same contract as the service profile: never raise, never overclaim."""

    @staticmethod
    def _app(durable_dir):
        app = durable_dir / "kirocrew.AppImage"
        app.write_text("#!/bin/sh\n")
        return app

    @staticmethod
    def _writers():
        writes: list[tuple[str, str]] = []
        runs: list[tuple[str, ...]] = []
        return writes, runs, (lambda t, d: writes.append((t, str(d)))), (lambda *a: runs.append(a))

    def _ready(self, monkeypatch, aa):
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        monkeypatch.setattr(aa, "detect_abi", lambda: "4.0")
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (True, ""))
        monkeypatch.setattr(aa, "conflicting_attachment", lambda _p: None)
        monkeypatch.setattr(aa, "verify_enforcement", lambda *_a: (True, None))

    def test_skips_cleanly_on_a_host_that_does_not_need_it(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (False, "no restriction here"))

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert outcome.changed is False
        assert outcome.ok is True, "a skip is not a failure"
        assert writes == [] and runs == []

    def test_explains_itself_when_there_is_nothing_to_attach_to(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))
        monkeypatch.setattr(aa, "default_exec_path", lambda: None)

        outcome = aa.install_launcher(write, run, lambda *_a: (0, ""), 1000, 1000, None)

        assert outcome.ok is False
        assert "$APPIMAGE" in outcome.message
        assert "service install" in outcome.message
        assert writes == []

    def test_refuses_an_unsafe_path_without_touching_the_host(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        monkeypatch.setattr(aa, "should_install", lambda: (True, "restricted"))

        outcome = aa.install_launcher(write, run, lambda *_a: (0, ""), 1000, 1000, "/bin/sh")

        assert outcome.ok is False
        assert outcome.changed is False
        assert writes == [] and runs == []

    def test_refuses_a_profile_that_does_not_compile(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        monkeypatch.setattr(aa, "validate", lambda _p, _t: (False, "syntax error"))

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert outcome.ok is False
        assert "did NOT compile" in outcome.message
        assert writes == [] and runs == []

    def test_a_sudo_failure_warns_and_never_raises(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        self._ready(monkeypatch, aa)

        def boom(*_a, **_k):
            raise RuntimeError("sudo: a password is required")

        outcome = aa.install_launcher(
            boom,
            lambda *_a: None,
            lambda *_a: (0, ""),
            1000,
            1000,
            str(self._app(durable_dir)),
        )

        assert outcome.ok is False
        assert "fail closed" in outcome.message

    def test_does_not_claim_success_when_enforcement_is_unconfirmed(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        monkeypatch.setattr(aa, "verify_enforcement", lambda *_a: (False, "probe still fails"))

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert outcome.changed is True, "the file WAS written"
        assert outcome.ok is False
        assert "Not claiming" in outcome.message

    def test_verifies_enforcement_against_the_launcher_profile_by_name(
        self, monkeypatch, durable_dir
    ):
        """Verifying the service profile instead would prove the wrong thing."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        seen: list[str] = []
        monkeypatch.setattr(
            aa,
            "verify_enforcement",
            lambda _c, _u, _g, name: (seen.append(name), (True, None))[1],
        )

        aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert seen == [aa.LAUNCHER_PROFILE_NAME]

    def test_success_writes_the_profile_loads_it_and_says_to_restart(
        self, monkeypatch, durable_dir
    ):
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        app = self._app(durable_dir)

        outcome = aa.install_launcher(write, run, lambda *_a: (0, ""), 1000, 1000, str(app))

        assert outcome.ok is True and outcome.changed is True
        assert len(writes) == 1
        assert writes[0][1] == str(aa.LAUNCHER_PROFILE_PATH)
        assert f'"{app.resolve()}"' in writes[0][0]
        assert runs == [("/usr/sbin/apparmor_parser", "-r", "-W", str(aa.LAUNCHER_PROFILE_PATH))]
        assert "Restart the app" in outcome.message

    def test_warns_about_a_conflicting_hand_written_profile(self, monkeypatch, durable_dir):
        """The workaround people find first attaches to the same AppImage."""
        from kiro_crew.service import apparmor as aa

        writes, runs, write, run = self._writers()
        self._ready(monkeypatch, aa)
        monkeypatch.setattr(aa, "conflicting_attachment", lambda _p: "/etc/apparmor.d/kirocrew")

        outcome = aa.install_launcher(
            write, run, lambda *_a: (0, ""), 1000, 1000, str(self._app(durable_dir))
        )

        assert outcome.ok is True, "a conflict is a warning, not a failure"
        assert "/etc/apparmor.d/kirocrew" in outcome.message
        assert "ambiguous" in outcome.message

    def test_uninstall_is_a_silent_noop_when_nothing_is_installed(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", durable_dir / "absent")

        outcome = aa.uninstall_launcher(lambda *_a: None)

        assert outcome.changed is False
        assert outcome.message == ""

    def test_uninstall_unloads_then_removes(self, monkeypatch, durable_dir):
        from kiro_crew.service import apparmor as aa

        profile = durable_dir / aa.LAUNCHER_PROFILE_NAME
        profile.write_text("profile\n")
        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", profile)
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        runs: list[tuple[str, ...]] = []

        outcome = aa.uninstall_launcher(lambda *a: runs.append(a))

        assert outcome.changed is True
        assert runs[0] == ("/usr/sbin/apparmor_parser", "-R", str(profile))
        assert runs[1] == ("rm", "-f", str(profile))

    def test_default_exec_path_reads_appimage(self, monkeypatch):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setenv("APPIMAGE", "/home/u/Apps/kirocrew.AppImage")
        assert aa.default_exec_path() == "/home/u/Apps/kirocrew.AppImage"

        monkeypatch.setenv("APPIMAGE", "   ")
        assert aa.default_exec_path() is None

        monkeypatch.delenv("APPIMAGE", raising=False)
        assert aa.default_exec_path() is None


#: The AppImage path the attachment scan matches on, POSIX-flavoured on purpose.
#: ``conflicting_attachment`` interpolates the path into a literal needle, and on
#: Windows a plain ``Path("/opt/x")`` renders with BACKSLASHES — so the needle
#: would never match the forward-slash profile text and the test would assert a
#: platform artefact instead of the matching logic. AppArmor is Linux-only, so
#: PurePosixPath is also what production actually passes here.
_APPIMAGE = PurePosixPath("/opt/KiroCrew.AppImage")


class TestAppArmorConflictingAttachment:
    """Two profiles claiming one attachment is an ambiguous load.

    A hand-written profile attached to the same AppImage is the workaround people
    find first, so it is common rather than exotic — and the caller can only warn
    about it if this finds it. Best effort by design: a literal scan, no policy
    parsing, and an unreadable file is skipped rather than failing the install.
    """

    @staticmethod
    def _dir(monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", tmp_path / aa.LAUNCHER_PROFILE_NAME)
        return aa

    def test_no_other_profile_means_no_conflict(self, monkeypatch, tmp_path):
        aa = self._dir(monkeypatch, tmp_path)
        assert aa.conflicting_attachment(_APPIMAGE) is None

    def test_our_own_profile_is_never_the_conflict(self, monkeypatch, tmp_path):
        aa = self._dir(monkeypatch, tmp_path)
        (tmp_path / aa.LAUNCHER_PROFILE_NAME).write_text('"/opt/KiroCrew.AppImage" {}')
        assert aa.conflicting_attachment(_APPIMAGE) is None

    @pytest.mark.parametrize(
        "body",
        [
            '"/opt/KiroCrew.AppImage" flags=(attach_disconnected) {}',
            "profile local /opt/KiroCrew.AppImage {}",
            "profile local\n/opt/KiroCrew.AppImage",
        ],
    )
    def test_each_attachment_spelling_is_found(self, body: str, monkeypatch, tmp_path):
        aa = self._dir(monkeypatch, tmp_path)
        other = tmp_path / "local-kirocrew"
        other.write_text(body)
        assert aa.conflicting_attachment(_APPIMAGE) == str(other)

    def test_a_profile_for_another_path_is_not_a_conflict(self, monkeypatch, tmp_path):
        aa = self._dir(monkeypatch, tmp_path)
        (tmp_path / "other").write_text('"/opt/SomethingElse.AppImage" {}')
        assert aa.conflicting_attachment(_APPIMAGE) is None

    def test_a_subdirectory_is_skipped(self, monkeypatch, tmp_path):
        # /etc/apparmor.d has abstractions/ and tunables/ subdirectories; reading
        # one as a file would raise, and this scan must not fail the install.
        aa = self._dir(monkeypatch, tmp_path)
        (tmp_path / "abstractions").mkdir()
        assert aa.conflicting_attachment(_APPIMAGE) is None

    def test_an_unreadable_file_is_skipped_not_fatal(self, monkeypatch, tmp_path):
        aa = self._dir(monkeypatch, tmp_path)
        (tmp_path / "unreadable").write_text("x")
        good = tmp_path / "zz-real"
        good.write_text('"/opt/KiroCrew.AppImage" {}')
        real_read = Path.read_text

        def _read(self, *a, **kw):
            if self.name == "unreadable":
                raise OSError("EACCES")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _read)
        assert aa.conflicting_attachment(_APPIMAGE) == str(good)

    def test_a_missing_directory_is_not_an_error(self, monkeypatch, tmp_path):
        aa = self._dir(monkeypatch, tmp_path / "absent")
        assert aa.conflicting_attachment(_APPIMAGE) is None


class TestAppArmorLauncherUninstall:
    """Unloading is idempotent and never raises.

    An uninstall that fails hard leaves the user with a half-removed profile and
    no way to finish; the file is what matters, so a failed UNLOAD is logged and
    the removal still runs.
    """

    def test_nothing_to_do_when_no_profile_is_installed(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", tmp_path / "absent")
        outcome = aa.uninstall_launcher(lambda *a: None)
        assert (outcome.changed, outcome.ok) == (False, True)

    def test_it_unloads_then_removes(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        path = tmp_path / "kirocrew-launcher"
        path.write_text("profile")
        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", path)
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        calls: list[tuple] = []

        outcome = aa.uninstall_launcher(lambda *a: calls.append(a))

        assert calls == [
            ("/usr/sbin/apparmor_parser", "-R", str(path)),
            ("rm", "-f", str(path)),
        ]
        assert outcome.changed is True and outcome.ok is True

    def test_a_failed_unload_still_removes_the_file(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        path = tmp_path / "kirocrew-launcher"
        path.write_text("profile")
        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", path)
        monkeypatch.setattr(aa, "parser_path", lambda: "/usr/sbin/apparmor_parser")
        calls: list[tuple] = []

        def _sudo(*a):
            if a[1] == "-R":
                raise RuntimeError("kernel said no")
            calls.append(a)

        outcome = aa.uninstall_launcher(_sudo)

        assert calls == [("rm", "-f", str(path))]
        assert outcome.ok is True

    def test_a_failed_removal_is_reported(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        path = tmp_path / "kirocrew-launcher"
        path.write_text("profile")
        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", path)
        monkeypatch.setattr(aa, "parser_path", lambda: None)

        def _sudo(*a):
            raise RuntimeError("read-only /etc")

        outcome = aa.uninstall_launcher(_sudo)

        assert outcome.ok is False
        assert "read-only /etc" in outcome.message

    def test_no_parser_skips_the_unload_and_still_removes(self, monkeypatch, tmp_path):
        from kiro_crew.service import apparmor as aa

        path = tmp_path / "kirocrew-launcher"
        path.write_text("profile")
        monkeypatch.setattr(aa, "LAUNCHER_PROFILE_PATH", path)
        monkeypatch.setattr(aa, "parser_path", lambda: None)
        calls: list[tuple] = []

        outcome = aa.uninstall_launcher(lambda *a: calls.append(a))

        assert calls == [("rm", "-f", str(path))]
        assert outcome.changed is True
