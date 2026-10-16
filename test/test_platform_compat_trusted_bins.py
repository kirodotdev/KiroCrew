"""Which binaries the gateway trusts: the system directories and root-owned paths.

``trusted_system_bin`` resolving only from fixed system directories, never from
``PATH``; the root-owned-path gate over every component and symlink a walk
traverses (``traversed_components``); the local ``aws`` fallback and the report
of a copy it declined; and the process parent map, which reads only a trusted
``ps``. A topic of the ``test_platform_compat`` family; see
``test_platform_compat.py``.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import pytest

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_platform_compat family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def test_parent_map_ignores_a_planted_ps_earlier_on_path(tmp_path, monkeypatch):
    """A gateway PATH can lead with agent-writable dirs, so PATH is not trusted.

    The shim below would report a bogus tree (and could run any code) if the
    lookup honored PATH.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    # The sentinel must be a number NO real process table can contain, because the
    # assertion below reads its absence as proof the shim did not run. A plausible
    # PID cannot do that job: `pid_max` is 4194304 on Linux, so a host whose counter
    # has passed 999999 has a live process with that id and the test failed with
    # "planted PATH shim was executed" while the shim had not run at all.
    unreachable_pid = 99999999999
    shim = tmp_path / "ps"
    shim.write_text(f"#!/bin/sh\necho '{unreachable_pid} {unreachable_pid - 1}'\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    parent_map = platform_compat._posix_process_parent_map()
    assert unreachable_pid not in parent_map, "planted PATH shim was executed"
    # A real snapshot still came back, so this is not passing by returning {}.
    assert os.getpid() in parent_map


def test_trusted_system_bin_rejects_a_name_not_in_system_dirs(tmp_path, monkeypatch):
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    fake = tmp_path / "definitely-not-a-system-tool"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert platform_compat.trusted_system_bin("definitely-not-a-system-tool") is None
    assert platform_compat.trusted_system_bin("ps") is not None


def test_trusted_system_bin_dirs_are_not_limited_to_fhs():
    # A distribution may keep ps/lsof/systemd-run outside /usr/{s}bin; an
    # FHS-only pin resolves nothing at all there.
    from kiro_crew import platform_compat

    fhs = {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}
    assert set(platform_compat._TRUSTED_SYSTEM_BIN_DIRS) - fhs


def test_trusted_system_bin_resolves_outside_fhs(tmp_path, monkeypatch):
    # A tool reachable only through a non-FHS pinned directory still resolves.
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    system_dir = tmp_path / "sw" / "bin"
    system_dir.mkdir(parents=True)
    tool = system_dir / "definitely-not-a-system-tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)

    monkeypatch.setattr(platform_compat, "_TRUSTED_SYSTEM_BIN_DIRS", (str(system_dir),))
    assert platform_compat.trusted_system_bin("definitely-not-a-system-tool") == str(tool)


def _root_owned_everywhere_except(*user_owned: Path):
    """An ``os.stat`` that presents the filesystem as root's, bar *user_owned*.

    Every directory on the way to a fixture -- ``/``, the temp root, ``tmp_path``
    -- answers as root-owned with no group or world write bit, so a fixture BELOW
    ``tmp_path`` can stand in for a system directory; the named paths keep their
    real ``st_uid`` so a user-owned directory is still a user-owned directory.
    Faked rather than read from ``/usr/bin``, because that directory's ownership
    is a property of the RUNNER: a sandboxed or user-namespaced host presents it
    as another uid's, and the world-writable temp root under which fixtures live
    (``/tmp``, mode 1777) puts ``S_IWOTH`` on an ancestor. Either fails the gate
    for a property of the machine rather than of the code.
    """
    real_stat = os.stat
    keep = {os.path.realpath(str(p)) for p in user_owned}

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.realpath(str(path)) in keep:
            return st
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    return fake_stat


def test_root_owned_path_accepts_a_system_dir_and_rejects_a_user_one(tmp_path, monkeypatch):
    """The gate that makes the ``/usr/local/bin`` fallback safe.

    The system directory is a fixture presented as root's through ``os.stat`` --
    see ``_root_owned_everywhere_except`` for why it is not ``/usr/bin`` -- while
    the user directory keeps its REAL ownership, so the two verdicts turn on the
    one property that separates them.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX ownership
        pytest.skip("POSIX ownership semantics")

    system_bin = tmp_path / "system" / "bin"
    system_bin.mkdir(parents=True)
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    leaf = user_dir / "aws"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", _root_owned_everywhere_except(user_dir))

    assert platform_compat._is_root_owned_path(str(system_bin)) is True
    # The user directory is the test user's, so it fails on ownership alone.
    assert platform_compat._is_root_owned_path(str(user_dir)) is False
    # ... and a root-owned leaf under it still fails, because the directory is
    # what governs replacing the file.
    assert platform_compat._is_root_owned_path(str(leaf)) is False


def test_root_owned_path_declines_a_root_leaf_under_a_writable_directory(tmp_path, monkeypatch):
    """The ancestor walk is load-bearing, not belt-and-braces.

    Replacing a file needs write on its DIRECTORY, not on the file, so a
    root-owned binary under a uid-writable directory can be swapped for anything.
    Only the walk can see that: the leaf itself passes every check.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX ownership
        pytest.skip("POSIX ownership semantics")

    leaf = tmp_path / "aws"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.realpath(str(path)) != os.path.realpath(str(leaf)):
            return st
        # Only the leaf is presented as root's; every directory above it keeps
        # the test user's real ownership.
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(leaf)) is False


def test_root_owned_path_declines_a_group_writable_component(tmp_path, monkeypatch):
    """Ownership is not enough: group/world write is writable by more than root."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX ownership
        pytest.skip("POSIX ownership semantics")

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        # Present every component as root-owned, and the leaf as group-writable,
        # so only the mode bits can decide the verdict.
        mode = st.st_mode | (stat.S_IWGRP if str(path).endswith("aws") else 0)
        return os.stat_result((mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0) + tuple(st)[6:])

    leaf = tmp_path / "aws"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)
    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(leaf)) is False


def test_root_owned_path_declines_an_acl_write_grant(tmp_path, monkeypatch):
    """Mode bits cannot express an ACL, so ownership algebra alone is incomplete.

    A root-owned ``0755`` path carrying a POSIX.1e or macOS ACL entry that grants a
    named user write passes every ``st_mode`` test while being writable by exactly
    the principal this gate defends against. ``os.access`` is what sees it, because
    the kernel evaluates ACLs and this function cannot.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX ownership
        pytest.skip("POSIX ownership semantics")

    leaf = tmp_path / "aws"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        # Everything root-owned with clean mode bits, so ONLY the access probe can
        # decide. Creating a real ACL is not portable, so the kernel's answer is
        # what gets stubbed -- the same answer it gives on a real ACL grant.
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    # The ACL arm needs faccessat; pin it so the test does not silently pass on a
    # platform where the arm is absent.
    monkeypatch.setattr(platform_compat, "_ACCESS_HONOURS_EFFECTIVE_IDS", True)

    def acl_grants_write(path, mode, *, effective_ids=False, **kw):
        # Answers True ONLY for the `effective_ids=True` form, because that is the
        # only one that evaluates a full ACL -- the bare call asks about the real
        # ids and would report this path unwritable, silently dropping the arm.
        return effective_ids and str(path) == str(leaf)

    monkeypatch.setattr(os, "access", acl_grants_write)
    assert platform_compat._is_root_owned_path(str(leaf)) is False

    # Running AS root the arm has no signal -- `os.access` answers True for
    # essentially everything there -- so the verdict is DECLINE, not accept. The
    # entry the arm would have caught grants a NON-root user write, which is exactly
    # what root must not execute, and "cannot establish" must not round to "safe" on
    # a path about to be exec'd as root. REAL or effective: a process holding either
    # id can regain it, so either one being root is enough to reach this.
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert platform_compat._is_root_owned_path(str(leaf)) is False
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert platform_compat._is_root_owned_path(str(leaf)) is False

    # And with NO ACL at all, a root REAL id still declines: that is what makes the
    # union load-bearing rather than decorative, since the effective id alone would
    # fall through to the arm, get a clean answer, and accept.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert platform_compat._is_root_owned_path(str(leaf)) is False

    # On a platform without faccessat the arm must be SKIPPED, not attempted: the
    # `effective_ids=True` form raises there, and nothing wraps this call, so the
    # exception would leave `trusted_aws_bin` and take `doctor` down with it.
    def refuse_effective_ids(path, mode, *, effective_ids=False, **kw):
        if effective_ids:
            raise NotImplementedError("faccessat unavailable on this platform")
        return False

    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(platform_compat, "_ACCESS_HONOURS_EFFECTIVE_IDS", False)
    monkeypatch.setattr(os, "access", refuse_effective_ids)
    assert platform_compat._is_root_owned_path(str(leaf)) is True


def test_aws_bin_declined_is_none_when_the_system_copy_won(monkeypatch):
    """No decline explains anything once the resolver has succeeded.

    Reporting "the local copy was refused" while `/usr/bin/aws` is what got used
    offers that refusal as the cause of some later, unrelated failure.
    """
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: "/usr/bin/aws")
    monkeypatch.setattr(platform_compat, "_local_aws_bin_candidate", lambda: "/usr/local/bin/aws")
    monkeypatch.setattr(platform_compat, "_is_root_owned_path", lambda path: False)
    assert platform_compat.aws_bin_declined_on_ownership() is None


def test_root_owned_path_accepts_an_absolute_symlink_through_root_owned_dirs(tmp_path, monkeypatch):
    """An ABSOLUTE symlink target must resolve from ``/``, not from the current dir.

    This is the AWS installer's real layout -- ``/usr/local/bin/aws`` is an absolute
    symlink into its own versioned tree. Resolving such a target relative to where
    the walk happens to be produces a path that does not exist, the gate declines,
    and the whole fallback is dead code on exactly the hosts it was added for. So
    this is the positive case: every component root-owned, verdict True.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    versioned = tmp_path / "aws-cli" / "v2" / "bin"
    versioned.mkdir(parents=True)
    target = versioned / "aws"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    prefix = tmp_path / "bin"
    prefix.mkdir()
    entry = prefix / "aws"
    entry.symlink_to(target)  # absolute, as pathlib writes it from an absolute path

    assert os.path.isabs(os.readlink(str(entry)))

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    #
    # The group/world write bits are cleared for the same reason the uid is faked:
    # `tmp_path` sits under the temp root, and on a host whose temp root is the
    # world-writable `/tmp` (mode 1777) an ANCESTOR carries `S_IWOTH`, so the gate
    # declines for a property of the runner rather than of the code. That is what
    # made this test pass locally under a 0755 scratch root and fail on CI.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(entry)) is True

    # And a RELATIVE target resolves from the link's own directory, not from `/`.
    # `/bin -> usr/bin` is the familiar example; resetting to the root for a
    # relative target produces a path that does not exist and declines everything.
    sibling = prefix / "aws-relative"
    sibling.symlink_to(os.path.relpath(str(target), str(prefix)))
    assert not os.path.isabs(os.readlink(str(sibling)))
    assert platform_compat._is_root_owned_path(str(sibling)) is True


def test_root_owned_path_declines_a_writable_ancestor_of_a_symlinked_component(
    tmp_path, monkeypatch
):
    """A symlinked DIRECTORY component must be resolved, not walked lexically.

    ``os.stat`` follows symlinks but ``os.path.dirname`` does not, so a lexical
    parent walk over ``/usr/local/bin/aws`` where ``bin -> /opt/x/bin`` visits
    ``/usr/local`` and never ``/opt/x`` -- the directory that can replace the target
    wholesale. Only resolving component by component reaches it.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    prefix = tmp_path / "prefix"
    prefix.mkdir()
    holder = tmp_path / "holder"
    holder.mkdir()
    real_bin = holder / "bin"
    real_bin.mkdir()
    leaf = real_bin / "aws"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)
    (prefix / "bin").symlink_to(real_bin)
    entry = prefix / "bin" / "aws"

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.realpath(str(path)) == os.path.realpath(str(holder)):
            # The ONE component left as the test user's: the symlink target's own
            # parent, which no lexical walk over `entry` ever names.
            return st
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(entry)) is False


def test_root_owned_path_accepts_a_real_system_binary(tmp_path, monkeypatch):
    """The gate must still say yes to an ordinary root-owned install.

    A predicate that refuses everything satisfies every rejection test above while
    making the whole fallback dead. The positive case is a REGULAR-FILE binary
    reached through plain directories (no symlink on the chain, which the
    absolute-symlink test covers): every component root-owned, verdict True.
    Presented through ``os.stat`` rather than read from ``/usr/bin/env`` -- see
    ``_root_owned_everywhere_except`` for why the real directory cannot serve.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX layout
        pytest.skip("POSIX filesystem layout")

    system_bin = tmp_path / "usr" / "bin"
    system_bin.mkdir(parents=True)
    env_bin = system_bin / "env"
    env_bin.write_bytes(b"\x7fELF-not-a-script\n")
    env_bin.chmod(0o755)

    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", _root_owned_everywhere_except())

    assert platform_compat._is_root_owned_path(str(system_bin)) is True
    assert platform_compat._is_root_owned_path(str(env_bin)) is True


def test_root_owned_path_declines_a_group_writable_directory_on_the_chain(tmp_path, monkeypatch):
    """A DIRECTORY's mode bits matter, not only its owner.

    This is the stock-Debian case: `/usr/local/bin` is root-owned there and mode
    ``2775``, so anyone in ``staff`` can replace the entry. Owner-only checking
    would accept it.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX ownership
        pytest.skip("POSIX ownership semantics")

    holder = tmp_path / "holder"
    holder.mkdir()
    leaf = holder / "aws"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        # Everything is root-owned, and the one DIRECTORY on the chain is
        # group-writable, so only the directory mode check can decide.
        mode = st.st_mode
        if os.path.realpath(str(path)) == os.path.realpath(str(holder)):
            mode |= stat.S_IWGRP
        return os.stat_result((mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0) + tuple(st)[6:])

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(leaf)) is False


def test_root_owned_path_walks_past_the_immediate_parent(tmp_path, monkeypatch):
    """The walk goes all the way up, not one level.

    A root-owned parent inside a uid-writable GRANDparent is still replaceable:
    whoever owns the grandparent can swap the parent directory wholesale. Checking
    only the immediate parent would accept it.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX ownership
        pytest.skip("POSIX ownership semantics")

    grandparent = tmp_path / "outer"
    grandparent.mkdir()
    parent = grandparent / "inner"
    parent.mkdir()
    leaf = parent / "aws"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.realpath(str(path)) == os.path.realpath(str(grandparent)):
            # The ONE component left as the test user's, two levels up from the
            # leaf, so only a full walk can reach it.
            return st
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(leaf)) is False


def test_root_owned_path_declines_a_writable_hop_in_the_middle_of_a_chain(tmp_path, monkeypatch):
    """The hop walk is load-bearing: both endpoints can be root's while a hop is not.

    ``/usr/local/bin/aws -> /tmp/link -> /usr/bin/aws`` is the shape. Checking only
    the literal path and its fully-resolved target passes it end to end: the
    literal walk visits ``/usr/local/bin``, the resolved walk visits ``/usr/bin``,
    and ``/tmp`` -- the one directory where the retarget actually happens -- is
    never looked at. Whoever can write that directory chooses what executes.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    trusted = tmp_path / "trusted"
    trusted.mkdir()
    writable = tmp_path / "writable"
    writable.mkdir()
    target = trusted / "real"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    middle = writable / "hop"
    middle.symlink_to(target)
    entry = trusted / "aws"
    entry.symlink_to(middle)

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.realpath(str(path)) == os.path.realpath(str(writable)):
            # The ONE component left as the test user's. Both ENDPOINTS of the
            # chain and every directory above them are presented as root's, so
            # nothing but the hop walk can reach this directory.
            return st
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(entry)) is False


def test_root_owned_path_declines_a_symlink_loop(tmp_path, monkeypatch):
    """A cycle answers False rather than spinning: an unbounded walk is a hang."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.symlink_to(second)
    second.symlink_to(first)

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(first)) is False


def _hop_chain(tmp_path):
    """``trusted/aws -> writable/hop -> trusted/real``: the mid-chain hop shape."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    writable = tmp_path / "writable"
    writable.mkdir()
    target = trusted / "real"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    middle = writable / "hop"
    middle.symlink_to(target)
    entry = trusted / "aws"
    entry.symlink_to(middle)
    return entry, middle, writable, target


def _symlinked_component_chain(tmp_path):
    """``prefix/bin -> holder/bin``, entry ``prefix/bin/aws``: the symlinked-directory shape."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    holder = tmp_path / "holder"
    holder.mkdir()
    real_bin = holder / "bin"
    real_bin.mkdir()
    leaf = real_bin / "aws"
    leaf.write_text("#!/bin/sh\nexit 0\n")
    leaf.chmod(0o755)
    (prefix / "bin").symlink_to(real_bin)
    return prefix / "bin" / "aws", prefix, holder, leaf


def test_traversed_components_of_a_symlink_free_path_is_its_lexical_chain(tmp_path):
    """Without a symlink the walk names exactly ``resolved.parents`` plus the target.

    This is the measurement behind "strictly widening": a caller that asked its
    question over the lexical chain asks it over the very same directories here
    whenever no symlink is involved, in root-first order with the target last.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX path semantics
        pytest.skip("POSIX path semantics")

    holder = tmp_path / "holder"
    holder.mkdir()
    leaf = (holder / "aws").resolve()
    leaf.write_text("#!/bin/sh\nexit 0\n")

    assert platform_compat.traversed_components(leaf) == [*reversed(leaf.parents), leaf]
    assert platform_compat.traversed_components(str(leaf)) == [*reversed(leaf.parents), leaf]


def test_traversed_components_visits_a_hop_in_the_middle_of_a_chain(tmp_path):
    """Both endpoints' chains are named AND the directory holding the hop.

    Neither ``realpath`` then ``.parents`` nor a lexical walk over the entry names
    ``writable``; the component walk records it because it reads it. The symlinks
    themselves are absent: their mode is meaningless, and
    ``traversed_components_and_links`` reports them for a question that needs
    their owners.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    entry, middle, writable, target = _hop_chain(tmp_path)
    resolved = target.resolve()

    components = platform_compat.traversed_components(entry)

    assert components is not None
    assert components[-1] == resolved
    assert writable.resolve() in components
    assert set(resolved.parents) <= set(components), "every lexical ancestor of the target"
    assert set(entry.resolve().parents) <= set(components)
    assert middle not in components and entry not in components
    assert len(components) == len(set(components)), "each directory once"


def test_traversed_components_visits_both_sides_of_a_symlinked_directory_component(tmp_path):
    """``prefix/bin -> holder/bin``: the link's own parent AND the target's parent are named.

    A lexical walk over the entry names ``prefix`` and never ``holder``; a lexical
    walk over the collapsed path names ``holder`` and never ``prefix``. Either
    directory's owner can choose what the entry resolves to, so both are here.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    entry, prefix, holder, leaf = _symlinked_component_chain(tmp_path)

    components = platform_compat.traversed_components(entry)

    assert components is not None
    assert components[-1] == leaf.resolve()
    assert prefix.resolve() in components
    assert holder.resolve() in components
    assert (holder / "bin").resolve() in components
    assert prefix / "bin" not in components, "the symlink itself is not a component"


def test_traversed_components_and_links_names_every_symlink_the_walk_follows(tmp_path):
    """The final name's link, a hop in the middle and a symlinked directory alike.

    Each is spelled under a directory the walk read, which is where its owner's
    right to replace it is decided in a sticky directory, and the directories
    are exactly what ``traversed_components`` answers for the same path.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    (tmp_path / "hop").mkdir()
    (tmp_path / "dir").mkdir()
    entry, middle, _writable, target = _hop_chain(tmp_path / "hop")
    via_dir, prefix, _holder, _leaf = _symlinked_component_chain(tmp_path / "dir")

    def spelled(link: Path) -> Path:
        return link.parent.resolve() / link.name

    for path, expected in ((entry, [entry, middle]), (via_dir, [prefix / "bin"])):
        walk = platform_compat.traversed_components_and_links(path)

        assert walk is not None
        components, links = walk
        assert components == platform_compat.traversed_components(path)
        assert links == [spelled(link) for link in expected]
        assert all(link.parent in components for link in links)
    assert platform_compat.traversed_components_and_links(target) == (
        platform_compat.traversed_components(target),
        [],
    )


def test_traversed_components_is_none_on_a_symlink_loop(tmp_path):
    """A cycle answers ``None``, never a partial list: unknown is not a shorter walk."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.symlink_to(second)
    second.symlink_to(first)

    assert platform_compat.traversed_components(first) is None


def test_traversed_components_is_none_when_a_link_cannot_be_read(tmp_path, monkeypatch):
    """An ``OSError`` mid-walk is ``None``: the caller decides what unknown means."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    entry, _middle, _writable, _target = _hop_chain(tmp_path)

    def broken_readlink(path, *a, **kw):
        raise OSError(errno.EIO, "readlink failed")

    monkeypatch.setattr(os, "readlink", broken_readlink)

    assert platform_compat.traversed_components(entry) is None


def test_root_owned_path_is_the_root_owned_predicate_over_the_walk(tmp_path, monkeypatch):
    """The predicate is unchanged by the split: it is ``_root_owned_entry`` over the walk.

    Both evasion shapes and a tight chain agree with that composition, and the
    refusals stay refusals: the walk finds the one directory left as the test
    user's in each shape and the predicate declines it.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX ownership
        pytest.skip("POSIX ownership semantics")

    hop_entry, _middle, writable, _target = _hop_chain(tmp_path)
    component_entry, _prefix, holder, _leaf = _symlinked_component_chain(tmp_path)
    tight_dir = tmp_path / "tight"
    tight_dir.mkdir()
    tight = tight_dir / "aws"
    tight.write_text("#!/bin/sh\nexit 0\n")
    tight.chmod(0o755)
    loose = {os.path.realpath(str(writable)), os.path.realpath(str(holder))}

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.realpath(str(path)) in loose:
            return st
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)

    for entry, expected in ((hop_entry, False), (component_entry, False), (tight, True)):
        components = platform_compat.traversed_components(entry)
        assert components is not None
        composed = all(platform_compat._root_owned_entry(str(c)) for c in components)
        assert composed is expected
        assert platform_compat._is_root_owned_path(str(entry)) is expected


def test_root_owned_path_declines_a_symlink_into_a_writable_directory(tmp_path, monkeypatch):
    """The realpath pass is load-bearing too, for a reason the literal pass cannot see.

    ``os.stat`` follows a symlink, so stating the link already reads the TARGET's
    own ownership — what the literal pass never visits is the target's ancestor
    directories. A root-owned binary parked in a uid-writable directory and
    symlinked from a trusted prefix therefore passes the literal walk end to end
    and is caught only when the walk restarts from the resolved path.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX symlinks
        pytest.skip("POSIX symlink semantics")

    trusted = tmp_path / "trusted"
    trusted.mkdir()
    writable = tmp_path / "writable"
    writable.mkdir()
    target = writable / "aws"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    link = trusted / "aws"
    link.symlink_to(target)

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if os.path.realpath(str(path)) == os.path.realpath(str(writable)):
            # The ONE component left as the test user's. Everything else --
            # including the target file the link resolves to -- is presented as
            # root's, so no check other than the realpath ancestor walk can fail.
            return st
        return os.stat_result(
            (st.st_mode & ~(stat.S_IWGRP | stat.S_IWOTH), st.st_ino, st.st_dev, st.st_nlink, 0, 0)
            + tuple(st)[6:]
        )

    # The access probe reads the REAL filesystem, where these fixtures belong to
    # the test user, so it would answer False for its own reason. Stubbed to the
    # answer the fake ownership implies, leaving this test's own mechanism as the
    # only thing that can decide the verdict.
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: False)
    monkeypatch.setattr(os, "stat", fake_stat)
    assert platform_compat._is_root_owned_path(str(link)) is False


def test_trusted_aws_bin_prefers_the_trusted_system_copy(monkeypatch):
    """The local-prefix half is a FALLBACK, never a first choice."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: "/usr/bin/aws")
    assert platform_compat.trusted_aws_bin() == "/usr/bin/aws"


def test_trusted_aws_bin_declines_a_candidate_the_caller_could_replace(tmp_path, monkeypatch):
    """A present, executable copy under a uid-writable prefix resolves to None.

    Same answer as an absent tool, on purpose: the caller's degradation is
    "cannot ask", never "ask this binary anyway".
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    tool = tmp_path / "aws"
    # NOT a shebang script: the fallback refuses one, because a `#!` line names
    # an interpreter the ownership walk never validated. Bytes that are the
    # program stand in for the native executable AWS CLI v2 actually ships.
    tool.write_bytes(b"\x7fELF not-a-script\n")
    tool.chmod(0o755)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(platform_compat, "_LOCAL_SYSTEM_BIN_DIR", str(tmp_path))
    monkeypatch.setattr(platform_compat, "_UNTRUSTED_AWS_BIN_LOGGED", False)
    assert platform_compat.trusted_aws_bin() is None


def test_trusted_aws_bin_resolves_a_candidate_that_passes_the_gate(tmp_path, monkeypatch):
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    tool = tmp_path / "aws"
    # NOT a shebang script: the fallback refuses one, because a `#!` line names
    # an interpreter the ownership walk never validated. Bytes that are the
    # program stand in for the native executable AWS CLI v2 actually ships.
    tool.write_bytes(b"\x7fELF not-a-script\n")
    tool.chmod(0o755)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(platform_compat, "_LOCAL_SYSTEM_BIN_DIR", str(tmp_path))
    monkeypatch.setattr(platform_compat, "_is_root_owned_path", lambda path: True)
    assert platform_compat.trusted_aws_bin() == str(tool)


def test_trusted_aws_bin_is_none_when_no_copy_exists_anywhere(tmp_path, monkeypatch):
    """An open gate cannot invent a binary that is not there."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(platform_compat, "_LOCAL_SYSTEM_BIN_DIR", str(tmp_path))
    monkeypatch.setattr(platform_compat, "_is_root_owned_path", lambda path: True)
    assert platform_compat.trusted_aws_bin() is None


def test_trusted_aws_bin_declines_a_shebang_script(tmp_path, monkeypatch):
    """A `#!` wrapper hands execution to a file the ownership walk never saw.

    The interpreter is named in the script's CONTENT, so validating the script's
    PATH says nothing about it -- and a root-owned wrapper pointing at a
    user-writable interpreter is what `sudo pip install awscli` against a pyenv
    Python produces. The fallback refuses scripts rather than starting down the
    endless road of validating the interpreter, then its libraries, then its module
    search path.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    tool = tmp_path / "aws"
    tool.write_text("#!/home/someone/.pyenv/versions/3.12/bin/python\nprint(1)\n")
    tool.chmod(0o755)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(platform_compat, "_LOCAL_SYSTEM_BIN_DIR", str(tmp_path))
    # Ownership is fine; ONLY the shebang can decide the verdict.
    monkeypatch.setattr(platform_compat, "_is_root_owned_path", lambda path: True)
    monkeypatch.setattr(platform_compat, "_UNTRUSTED_AWS_BIN_LOGGED", False)
    assert platform_compat.trusted_aws_bin() is None
    # And the refusal is VISIBLE, so the caller can say "not trusted" rather than
    # "absent" -- the two resolvers must never disagree about one file.
    assert platform_compat.aws_bin_declined_on_ownership() == str(tool)

    # A candidate this cannot even READ is refused too: unreadable is not
    # shown-to-be-safe, and treating the read failure as "no shebang" would accept
    # exactly the file whose contents could not be checked.
    if os.getuid() != 0:  # pragma: no branch - root can read a 0000 file
        tool.chmod(0o000)
        monkeypatch.setattr(platform_compat, "_UNTRUSTED_AWS_BIN_LOGGED", False)
        assert platform_compat._is_native_program(str(tool)) is False
        tool.chmod(0o755)


def test_local_aws_bin_trust_is_one_predicate_for_both_callers(tmp_path, monkeypatch):
    """The resolver and the decline-reporter must never contradict each other.

    Whatever the conditions are, one of them accepting a file the other reports as
    refused would state two incompatible facts. Asserted as an invariant over both
    verdicts rather than over a particular condition, so it survives the next
    condition being added.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    tool = tmp_path / "aws"
    tool.write_bytes(b"\x7fELF not-a-script\n")
    tool.chmod(0o755)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(platform_compat, "_LOCAL_SYSTEM_BIN_DIR", str(tmp_path))
    for trusted in (True, False):
        monkeypatch.setattr(platform_compat, "_is_root_owned_path", lambda path: trusted)
        monkeypatch.setattr(platform_compat, "_UNTRUSTED_AWS_BIN_LOGGED", False)
        resolved = platform_compat.trusted_aws_bin()
        declined = platform_compat.aws_bin_declined_on_ownership()
        assert (resolved is None) is (declined is not None), (trusted, resolved, declined)


def test_aws_bin_declined_names_the_copy_the_gate_refused(tmp_path, monkeypatch):
    """A declined copy must be reportable, so "not trusted" is not told as "absent".

    Debian policy has ``/usr/local`` subdirectories ``root:staff`` mode ``2775``,
    so on a stock Debian or Ubuntu host the gate declines by default. A caller
    that could only see ``None`` would tell those operators to install a tool they
    already have.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    tool = tmp_path / "aws"
    # NOT a shebang script: the fallback refuses one, because a `#!` line names
    # an interpreter the ownership walk never validated. Bytes that are the
    # program stand in for the native executable AWS CLI v2 actually ships.
    tool.write_bytes(b"\x7fELF not-a-script\n")
    tool.chmod(0o755)
    monkeypatch.setattr(platform_compat, "_LOCAL_SYSTEM_BIN_DIR", str(tmp_path))
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    # tmp_path is the test user's, so the gate declines on ownership.
    assert platform_compat.aws_bin_declined_on_ownership() == str(tool)


def test_aws_bin_declined_is_none_when_there_is_no_copy(tmp_path, monkeypatch):
    """An absent tool is not a declined one -- nothing was refused."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    monkeypatch.setattr(platform_compat, "_LOCAL_SYSTEM_BIN_DIR", str(tmp_path))
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    assert platform_compat.aws_bin_declined_on_ownership() is None


def test_aws_bin_declined_is_silent_about_a_copy_the_gate_accepts(tmp_path, monkeypatch):
    """The accepted case must report NOTHING, or the caller slanders a trusted copy."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX lookup
        pytest.skip("POSIX binary resolution")

    tool = tmp_path / "aws"
    # NOT a shebang script: the fallback refuses one, because a `#!` line names
    # an interpreter the ownership walk never validated. Bytes that are the
    # program stand in for the native executable AWS CLI v2 actually ships.
    tool.write_bytes(b"\x7fELF not-a-script\n")
    tool.chmod(0o755)
    monkeypatch.setattr(platform_compat, "_LOCAL_SYSTEM_BIN_DIR", str(tmp_path))
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(platform_compat, "_is_root_owned_path", lambda path: True)
    assert platform_compat.aws_bin_declined_on_ownership() is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "asserts the POSIX degradation path: neutering trusted_system_bin only "
        "disarms _posix_process_parent_map, while process_descendants on Windows "
        "goes through the Win32 snapshot and still reports this process's real "
        "live children -- so the == [] assertion depends on whether the xdist "
        "worker happens to have a subprocess alive at that instant"
    ),
)
def test_parent_map_is_empty_when_no_trusted_ps_exists(monkeypatch):
    """No trusted binary must degrade to best-effort, never fall back to PATH."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
    assert platform_compat._posix_process_parent_map() == {}
    assert platform_compat.process_descendants(os.getpid()) == []
