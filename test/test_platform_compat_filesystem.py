"""Filesystem primitives whose behaviour differs by platform.

Directory links (a symlink or a junction), ``pin_directory``, file locks under
contention, the no-replace directory publish, the lock-file opens (one that
never truncates, the shared create-or-open, and the sweep's delete-sharing
open), and the extended-length path prefix fold. A topic of the
``test_platform_compat`` family; see ``test_platform_compat.py``.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import threading
import time
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_platform_compat family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestDirLinkShims:
    """``symlink_or_junction`` / ``is_link_or_junction`` / ``unlink_link_or_junction``.

    These run on every platform: the contract is the same everywhere (a name
    that means another directory), only the mechanism differs — a symlink on
    POSIX, a directory junction on Windows, where an ordinary account holds no
    ``SeCreateSymbolicLinkPrivilege`` and ``os.symlink`` fails with
    ``WinError 1314``.
    """

    def test_link_is_created_and_transparent(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "index.html").write_text("hi")
        link = tmp_path / "link"

        pc.symlink_or_junction(target, link)

        assert pc.is_link_or_junction(link)
        assert link.is_dir()
        assert link.resolve() == target.resolve()
        # Reads go through, and later writes to the target are visible via the
        # link — the property the dist resolver relies on for rebuild pickup.
        assert (link / "index.html").read_text(encoding="utf-8") == "hi"
        (target / "later.txt").write_text("fresh")
        assert (link / "later.txt").read_text(encoding="utf-8") == "fresh"

    def test_plain_dir_and_file_are_not_links(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        regular = tmp_path / "f.txt"
        regular.write_text("x")

        assert not pc.is_link_or_junction(plain)
        assert not pc.is_link_or_junction(regular)
        assert not pc.is_link_or_junction(tmp_path / "does-not-exist")

    def test_dangling_link_is_still_reported_as_a_link(self, tmp_path):
        """A link whose target is gone must still answer True.

        The dist resolver's replace path keys off exactly this: ``exists()``
        follows the link and is already False, so only the link-ness test can
        tell "stale link to clean up" from "nothing here".
        """
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)
        shutil.rmtree(target)

        assert pc.is_link_or_junction(link)
        assert not link.exists()

    def test_unlink_removes_the_link_and_spares_the_target(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)

        pc.unlink_link_or_junction(link)

        assert not pc.is_link_or_junction(link)
        assert not os.path.lexists(str(link))
        assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_unlink_removes_a_dangling_link(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)
        shutil.rmtree(target)

        pc.unlink_link_or_junction(link)

        assert not os.path.lexists(str(link))

    def test_unlink_refuses_a_real_directory(self, tmp_path):
        """A non-link must raise on both platforms, empty or not.

        POSIX ``os.unlink`` refuses a directory outright, so the Windows
        ``rmdir`` fallback has to be fenced to reparse points: unfenced it
        DELETES a real empty directory, so a caller that mis-detects link-ness
        loses data on Windows only while POSIX raises.
        """
        empty = tmp_path / "real-empty"
        empty.mkdir()
        full = tmp_path / "real-full"
        full.mkdir()
        (full / "keep.txt").write_text("keep")

        with pytest.raises(OSError):
            pc.unlink_link_or_junction(empty)
        with pytest.raises(OSError):
            pc.unlink_link_or_junction(full)

        assert empty.is_dir()
        assert (full / "keep.txt").read_text(encoding="utf-8") == "keep"

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="junctions exist only on Windows")
    def test_windows_link_is_usable_without_elevation(self, tmp_path):
        """Windows gets a working directory link either way it is made.

        ``symlink_or_junction`` tries ``os.symlink`` FIRST and only falls back to
        a junction, so which mechanism lands depends on whether the host holds
        ``SeCreateSymbolicLinkPrivilege`` — GitHub's runners do, an ordinary
        account does not. Asserting "junction, never symlink" would therefore
        pin the unprivileged host as if it were universal, and fail on CI.

        What matters to every caller is the same on both paths, so that is what
        is asserted: the name is a reparse point that ``is_link_or_junction``
        recognises (an ``is_symlink()``-only test does NOT see a junction, which
        is the bug this shim exists for), it is transparent to path operations,
        and ``rmtree`` refuses it — which is why ``unlink_link_or_junction``
        exists. The junction branch specifically is covered by
        ``test_junction_is_recognised_and_removable`` below.
        """
        target = tmp_path / "target"
        target.mkdir()
        (target / "f.txt").write_text("hi", encoding="utf-8")
        link = tmp_path / "link"

        pc.symlink_or_junction(target, link)

        assert pc.is_link_or_junction(link)
        assert link.is_dir()  # transparent to path operations
        assert (link / "f.txt").read_text(encoding="utf-8") == "hi"
        # rmtree refuses any directory link, which is why unlink_link_or_junction exists.
        with pytest.raises(OSError):
            shutil.rmtree(str(link))
        pc.unlink_link_or_junction(link)
        assert not link.exists()
        assert target.is_dir(), "removing the link must spare the target"

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="junctions exist only on Windows")
    def test_junction_is_recognised_and_removable(self, tmp_path):
        """A JUNCTION specifically — the form an unprivileged Windows user gets.

        Created directly via ``_winapi.CreateJunction`` rather than through the
        shim, so this covers the unprivileged branch even on a runner that holds
        the symlink privilege and would otherwise take the symlink path.
        """
        import _winapi

        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "junction"
        _winapi.CreateJunction(str(target), str(link))

        # A junction reports is_symlink() False — the whole reason the shim's
        # detector cannot be an is_symlink() test.
        assert not link.is_symlink()
        assert pc.is_link_or_junction(link)
        # 0xA0000003 = IO_REPARSE_TAG_MOUNT_POINT, spelled literally rather than
        # read from the module under test (so the assertion is independent of it)
        # and rather than via os.path.isjunction, which would couple the
        # assertion to the same stdlib helper the module itself may use.
        assert os.lstat(str(link)).st_reparse_tag == 0xA0000003
        pc.unlink_link_or_junction(link)
        assert not link.exists()
        assert target.is_dir()

    @pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX symlink mechanism")
    def test_posix_link_is_a_symlink(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"

        pc.symlink_or_junction(target, link)

        assert link.is_symlink()
        assert os.readlink(str(link)) == str(target)


class TestPinDirectory:
    """``pin_directory``: hold a directory so a child written by PATH stays put.

    Every platform: the open refuses anything that is not a real directory.
    Windows only: the held handle blocks rename/delete -- the property the
    caller relies on when a same-UID watcher could otherwise swap the directory
    for a junction between a check and a child process's open.
    """

    def test_a_real_directory_pins_and_releases(self, tmp_path):
        target = tmp_path / "dir"
        target.mkdir()
        fd = pc.pin_directory(target)
        try:
            assert fd >= 0
            assert stat.S_ISDIR(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
        # Released: the directory is ordinary again.
        target.rename(tmp_path / "moved")

    def test_a_file_at_the_name_is_refused(self, tmp_path):
        regular = tmp_path / "f.txt"
        regular.write_text("x")
        with pytest.raises(NotADirectoryError):
            pc.pin_directory(regular)

    def test_a_link_at_the_name_is_refused_not_followed(self, tmp_path):
        # A watcher's whole move is to put a link where the directory was; the
        # pin must fail on it rather than pin the link's TARGET in its place.
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        pc.symlink_or_junction(target, link)
        with pytest.raises(OSError):
            pc.pin_directory(link)

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="a held handle blocks rename only on Windows")
    def test_a_pinned_directory_cannot_be_renamed_or_removed(self, tmp_path):
        # The pin is on the DIRECTORY: its children can still be removed (the
        # caller holds its own file open for that), but the directory itself
        # can be neither renamed nor deleted until the handle is released.
        target = tmp_path / "dir"
        target.mkdir()
        fd = pc.pin_directory(target)
        try:
            with pytest.raises(OSError):
                target.rename(tmp_path / "swapped")
            with pytest.raises(OSError):
                target.rmdir()
            assert target.is_dir()
        finally:
            os.close(fd)
        target.rename(tmp_path / "swapped")
        assert (tmp_path / "swapped").is_dir()


# ---------------------------------------------------------------------------
# POSIX-branch coverage for the new platform_compat helpers. The
# tests below deliberately exercise the ``if IS_POSIX:`` / Linux ``/proc`` paths
# and the POSIX ``except`` fall-throughs that run on the Linux build fleet. The
# Windows branches (msvcrt / ctypes / wintypes / netstat / taskkill / WMI /
# OpenProcess) cannot execute here and are intentionally left to Windows CI.
# ---------------------------------------------------------------------------


class TestFileLockContention:
    def test_try_acquire_lock_fails_under_exclusive_contention(self, tmp_path):
        # flock is per open-file-description: two independent os.open() calls to
        # the same path are independent OFDs, so a second LOCK_EX|LOCK_NB on a
        # path already held exclusively raises BlockingIOError -> the helper's
        # POSIX failure branch returns False (this is what we're covering).
        lock = tmp_path / ".contend.lock"
        fd_holder = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        fd_contender = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            # Real blocking exclusive lock on the holder fd.
            pc.acquire_lock(fd_holder, exclusive=True)
            # Non-blocking exclusive acquire on the *other* OFD must fail.
            assert pc.try_acquire_lock(fd_contender, exclusive=True) is False
            # Once the holder releases, the same contender fd can take it.
            pc.release_lock(fd_holder)
            assert pc.try_acquire_lock(fd_contender, exclusive=True) is True
            pc.release_lock(fd_contender)
        finally:
            os.close(fd_holder)
            os.close(fd_contender)

    def test_shared_try_acquire_then_release_relocks(self, tmp_path):
        # Take a shared non-blocking lock, release it, and confirm an independent
        # OFD can then take an EXCLUSIVE lock -- which is only possible if the
        # shared lock was genuinely released by release_lock.
        lock = tmp_path / ".sh-release.lock"
        fd_shared = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        fd_other = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            assert pc.try_acquire_lock(fd_shared, exclusive=False) is True
            pc.release_lock(fd_shared)
            # Exclusive acquire from a separate OFD now succeeds (lock is free).
            assert pc.try_acquire_lock(fd_other, exclusive=True) is True
            pc.release_lock(fd_other)
        finally:
            os.close(fd_shared)
            os.close(fd_other)

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows LK_LOCK ceiling regression")
    def test_windows_blocking_acquire_waits_past_lk_lock_ceiling(self, tmp_path):
        # msvcrt's LK_LOCK "blocking" code gives up after ~10s with EDEADLOCK,
        # which must not be treated as "acquired".
        # A holder that keeps the lock LONGER than that ceiling must make a
        # blocking contender WAIT (until release or its own timeout) — never
        # fall through and enter the critical section unserialized at ~10s.
        #
        # Drive _win_acquire_blocking directly with an EXPLICIT timeout past the
        # ceiling: the module default is a short on-loop-safety ceiling, but the
        # bug being pinned is specifically the ~10s LK_LOCK give-up point.
        import threading

        lock = tmp_path / ".ceiling.lock"
        hold_secs = 13.0
        fd_holder = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fd_contender = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        released_at = {"t": 0.0}
        entered_at = {"t": 0.0}
        holder_ready = threading.Event()

        def _hold():
            with pc.file_lock(fd_holder, exclusive=True, required=True):
                holder_ready.set()
                # flake-ok: the holder keeps the lock past the 10 s LK_LOCK ceiling on purpose; the contender waits on the lock, not on this sleep
                time.sleep(hold_secs)
                released_at["t"] = time.monotonic()

        holder = threading.Thread(target=_hold)
        holder.start()
        try:
            assert holder_ready.wait(timeout=10.0), "holder never took the lock"
            # Blocking acquire on the OTHER fd with a timeout past the ~10s
            # ceiling: it must not succeed until the holder releases at ~13s.
            got = pc._win_acquire_blocking(fd_contender, timeout=30.0)
            entered_at["t"] = time.monotonic()
            assert got is True, "contender never acquired the lock after release"
            # It entered only AFTER the holder released — proving it waited past
            # the 10s ceiling instead of slipping through early.
            assert entered_at["t"] >= released_at["t"], (
                "contender entered the critical section before the holder "
                "released — the blocking acquire fell through the LK_LOCK ceiling"
            )
            pc.release_lock(fd_contender)
        finally:
            holder.join(timeout=20.0)
            os.close(fd_holder)
            os.close(fd_contender)

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows on-loop single-shot acquire")
    def test_windows_contended_lock_on_event_loop_fails_fast(self, tmp_path, monkeypatch):
        # On the asyncio event-loop thread a contended lock must NOT spin-sleep
        # (that freezes chat/heartbeat): _win_acquire_blocking is single-shot
        # there, so file_lock fails closed immediately instead of waiting out
        # the timeout. Pinned by what the acquire DID -- exactly one non-blocking
        # attempt on the contender and no sleep -- which fails on a second
        # attempt however fast the host is. A wall-clock bound would pass a short
        # spin and fail a starved runner that made only the one attempt.
        import asyncio
        import msvcrt

        lock = tmp_path / ".onloop.lock"
        fd_holder = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fd_contender = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        test_thread = threading.get_ident()
        attempts: list[int] = []

        def locking(fd, mode, nbytes):
            if fd == fd_contender:
                attempts.append(mode)
            return msvcrt.locking(fd, mode, nbytes)

        def sleep(secs):
            if threading.get_ident() != test_thread:
                return time.sleep(secs)
            # A poll on the loop thread: fail here rather than spin to the ceiling.
            raise AssertionError(f"the on-loop acquire slept {secs}s between attempts")

        # The lock owner reads both through this module at call time.
        monkeypatch.setattr(
            pc, "msvcrt", types.SimpleNamespace(**{**vars(msvcrt), "locking": locking})
        )
        monkeypatch.setattr(pc, "time", types.SimpleNamespace(**{**vars(time), "sleep": sleep}))

        async def _contend_on_loop():
            # Hold on THIS fd (non-blocking), then a second in-loop acquire on
            # the other fd must raise at once rather than sleep to the ceiling.
            assert pc.try_acquire_lock(fd_holder, exclusive=True) is True
            with pytest.raises(OSError):
                with pc.file_lock(fd_contender, exclusive=True):
                    pass
            pc.release_lock(fd_holder)
            assert attempts == [msvcrt.LK_NBLCK], f"on-loop acquire attempts: {attempts}"

        try:
            asyncio.run(_contend_on_loop())
        finally:
            os.close(fd_holder)
            os.close(fd_contender)


class TestMountIdentity:
    """``mount_identity``: a path's mount id, device and inode through ``statx``."""

    @pytest.fixture
    def resolves(self, tmp_path):
        if pc.mount_identity(str(tmp_path)) is None:
            pytest.skip("statx does not report a mount id here (not Linux, or Linux < 5.8)")

    @pytest.mark.usefixtures("resolves")
    def test_a_directory_and_a_descriptor_on_it_agree(self, tmp_path):
        fd = os.open(tmp_path, getattr(os, "O_PATH", 0) | os.O_DIRECTORY)
        try:
            assert pc.mount_identity("", dir_fd=fd) == pc.mount_identity(str(tmp_path))
        finally:
            os.close(fd)

    @pytest.mark.usefixtures("resolves")
    def test_a_symlink_names_its_target_and_inodes_tell_directories_apart(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (tmp_path / "link").symlink_to(a)

        ident_a = pc.mount_identity(str(a))
        assert pc.mount_identity(str(tmp_path / "link")) == ident_a
        ident_b = pc.mount_identity(str(b))
        assert ident_a is not None and ident_b is not None
        assert ident_a[:3] == ident_b[:3] and ident_a[3] == a.stat().st_ino != ident_b[3]

    def test_a_missing_path_is_unknown(self, tmp_path):
        assert pc.mount_identity(str(tmp_path / "absent")) is None

    def test_without_statx_it_is_unknown(self, monkeypatch, tmp_path):
        from kiro_crew import platform_compat_files

        monkeypatch.setattr(platform_compat_files, "_statx_call", lambda: None)
        assert pc.mount_identity(str(tmp_path)) is None

    def test_a_kernel_that_reports_no_mount_id_is_asked_once(self, monkeypatch, tmp_path):
        """Before Linux 5.8 ``statx`` succeeds without ``STATX_MNT_ID``, and it
        never will on that kernel, so the next call skips the syscall."""
        import struct

        from kiro_crew import platform_compat_files

        calls = []

        def _statx(_dirfd, _path, _flags, _mask, buf):
            calls.append(1)
            struct.pack_into("<I", buf, 0, platform_compat_files._STATX_INO)
            return 0

        monkeypatch.setattr(platform_compat_files, "_statx_mnt_id_unreported", False)
        monkeypatch.setattr(platform_compat_files, "_statx_call", lambda: _statx)

        assert pc.mount_identity(str(tmp_path)) is None
        assert pc.mount_identity(str(tmp_path)) is None
        assert len(calls) == 1


class TestPublishDirNoreplace:
    """Workspace installs must never replace a raced empty
    destination -- POSIX os.rename silently replaces an empty directory, so
    the publish goes through the no-replace rename primitive."""

    def test_publishes_into_absent_destination(self, tmp_path):
        src = tmp_path / ".ws.staging-abc"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dst = tmp_path / "ws"
        pc.publish_dir_noreplace(src, dst)
        assert (dst / "f.txt").read_text(encoding="utf-8") == "x"
        assert not src.exists()

    def test_refuses_an_existing_empty_destination(self, tmp_path):
        """The exact race: an EMPTY directory at the destination survives."""
        src = tmp_path / ".ws.staging-abc"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dst = tmp_path / "ws"
        dst.mkdir()  # a racer's just-created empty dir
        before = dst.stat().st_ino
        with pytest.raises((FileExistsError, OSError)):
            pc.publish_dir_noreplace(src, dst)
        assert dst.stat().st_ino == before, "the racer's directory was replaced"
        assert src.exists(), "the staged tree was consumed by a refused publish"

    def test_refuses_non_sibling_paths(self, tmp_path):
        src = tmp_path / "a" / ".ws.staging-abc"
        src.mkdir(parents=True)
        dst = tmp_path / "b" / "ws"
        (tmp_path / "b").mkdir()
        if pc.IS_WINDOWS:
            pytest.skip("sibling contract is POSIX-only (Windows uses os.rename)")
        with pytest.raises(ValueError):
            pc.publish_dir_noreplace(src, dst)

    def test_fallback_without_renameat2_publishes_and_still_refuses_occupied(
        self, tmp_path, monkeypatch
    ):
        """A host without renameat2 (glibc < 2.28, NFS/FUSE)
        must not crash with NotImplementedError -- the mkdir-claim fallback
        publishes into an absent destination and still refuses an existing
        one, preserving the no-replace guarantee for creation races."""
        if pc.IS_WINDOWS:
            pytest.skip("fallback is POSIX-only (Windows os.rename never replaces)")

        def _unsupported(*args, **kwargs):
            raise NotImplementedError("filesystem lacks atomic no-replace rename")

        monkeypatch.setattr(pc, "rename_noreplace", _unsupported)

        # Publishes into an absent destination.
        src = tmp_path / ".ws.staging-abc"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dst = tmp_path / "ws"
        pc.publish_dir_noreplace(src, dst)
        assert (dst / "f.txt").read_text(encoding="utf-8") == "x"
        assert not src.exists()

        # Refuses an existing EMPTY destination; nothing is consumed.
        src2 = tmp_path / ".ws2.staging-abc"
        src2.mkdir()
        dst2 = tmp_path / "ws2"
        dst2.mkdir()  # a racer's just-created empty dir
        before = dst2.stat().st_ino
        with pytest.raises(FileExistsError):
            pc.publish_dir_noreplace(src2, dst2)
        assert dst2.stat().st_ino == before, "the racer's directory was replaced"
        assert src2.exists(), "the staged tree was consumed by a refused publish"

    def test_fallback_rename_failure_drops_the_claim(self, tmp_path, monkeypatch):
        """When the fallback's rename fails, the empty mkdir
        claim is removed so a retry is not permanently blocked, and the
        staged tree is not consumed."""
        if pc.IS_WINDOWS:
            pytest.skip("fallback is POSIX-only")

        def _unsupported(*args, **kwargs):
            raise NotImplementedError("filesystem lacks atomic no-replace rename")

        monkeypatch.setattr(pc, "rename_noreplace", _unsupported)

        real_rename = os.rename

        def _failing_rename(src, dst, **kwargs):
            raise OSError(errno.EXDEV, "simulated cross-device rename failure")

        src = tmp_path / ".ws.staging-abc"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dst = tmp_path / "ws"
        monkeypatch.setattr(os, "rename", _failing_rename)
        try:
            with pytest.raises(OSError):
                pc.publish_dir_noreplace(src, dst)
        finally:
            monkeypatch.setattr(os, "rename", real_rename)
        assert not dst.exists(), (
            "the failed fallback left an orphaned empty claim that would "
            "permanently block retries"
        )
        assert (src / "f.txt").exists(), "the staged tree was consumed by a failed publish"


class TestOpenLockFile:
    """GH-9248: acquiring a lock must not truncate the file it locks."""

    def test_preserves_existing_content(self, tmp_path):
        # The defect shape: open(path, "w") truncates BEFORE the lock is
        # held, so a contending process can observe an empty lock file
        # mid-acquire. The helper must open the file without touching its
        # bytes. Content is read while the fd is OPEN but not LOCKED, and
        # again after release — on Windows msvcrt region locks are
        # MANDATORY, so a read while the lock is held answers EACCES and
        # would test the platform's locking semantics instead of the
        # helper's non-truncation property.
        lock = tmp_path / "x.lock"
        lock.write_text("holder-pid 1234")
        from kiro_crew.platform_compat import file_lock, open_lock_file

        with open_lock_file(lock) as fd:
            assert isinstance(fd, int)
            assert lock.read_text() == "holder-pid 1234"  # open did not truncate
            with file_lock(fd, exclusive=True):
                pass  # lockable with content present
        assert lock.read_text() == "holder-pid 1234"  # intact after release

    def test_creates_missing_file_and_is_lockable(self, tmp_path):
        lock = tmp_path / "sub" / "y.lock"
        lock.parent.mkdir(parents=True)
        from kiro_crew.platform_compat import flock_exclusive, open_lock_file

        with open_lock_file(lock) as fd:
            with flock_exclusive(fd):
                pass
        assert lock.exists()
        assert lock.read_bytes() == b""

    def test_non_creating_open_refuses_a_missing_file_and_creates_nothing(self, tmp_path):
        from kiro_crew.platform_compat import open_lock_file

        lock = tmp_path / "absent.lock"
        with pytest.raises(FileNotFoundError):
            with open_lock_file(lock, create=False):
                pass
        assert not lock.exists()

    def test_non_creating_open_preserves_existing_content(self, tmp_path):
        lock = tmp_path / "held.lock"
        lock.write_text("holder-pid 1234")
        from kiro_crew.platform_compat import file_lock, open_lock_file

        with open_lock_file(lock, create=False) as fd:
            assert lock.read_text() == "holder-pid 1234"  # open did not truncate
            with file_lock(fd, exclusive=True):
                pass
        assert lock.read_text() == "holder-pid 1234"

    def test_no_lock_site_opens_truncating(self):
        # CONTRACT (the work-ledger fix's test shape, applied fleet-wide): grep the source
        # tree for a truncating open whose descriptor is handed to a
        # file_lock-family acquire within the next two lines. Every site was
        # converted to open_lock_file in this change; a new offender fails
        # here with its file and line.
        import kiro_crew

        src_root = os.path.dirname(os.path.abspath(kiro_crew.__file__))
        offenders = []
        open_w = re.compile(r"""\bopen\([^)]*["']wb?["']\)""")
        acquire = re.compile(r"\b(file_lock|flock_exclusive|acquire_lock)\(\s*\w+\.fileno\(\)")
        for dirpath, _dirnames, filenames in os.walk(src_root):
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                for i, line in enumerate(lines):
                    if not open_w.search(line):
                        continue
                    window = "".join(lines[i + 1 : i + 3])
                    if acquire.search(window):
                        offenders.append(f"{path}:{i + 1}")
        assert not offenders, (
            "lock files opened truncating before the acquire (GH-9248); "
            "use platform_compat.open_lock_file: " + ", ".join(offenders)
        )


class TestStripExtendedLengthPrefix:
    r"""One lexical fold for the extended-length prefix, shared by both guards.

    ``Path.resolve()`` on a file another thread is replacing at that moment comes
    back as a prefixed path, while the directory beside it comes back plain. A
    containment comparison that reads the two spellings as-is sees the prefix
    alone as a path escape, so both guards compare through this fold.
    """

    def test_a_drive_prefixed_path_loses_the_prefix(self):
        assert pc.strip_extended_length_prefix(Path("\\\\?\\C:\\x\\y")) == Path("C:\\x\\y")

    def test_a_unc_prefixed_path_becomes_a_plain_unc_path(self):
        assert pc.strip_extended_length_prefix(Path("\\\\?\\UNC\\host\\share\\y")) == Path(
            "\\\\host\\share\\y"
        )

    def test_an_ordinary_windows_path_is_returned_unchanged(self):
        assert pc.strip_extended_length_prefix(Path("C:\\x\\y")) == Path("C:\\x\\y")

    def test_a_posix_path_is_returned_unchanged(self):
        assert pc.strip_extended_length_prefix(Path("/home/a/b")) == Path("/home/a/b")

    def test_the_fold_is_idempotent(self):
        """A path that is already plain keeps its separators intact."""
        once = pc.strip_extended_length_prefix(Path("\\\\?\\C:\\x\\y"))
        assert pc.strip_extended_length_prefix(once) == once

    def test_the_ledger_guard_shares_the_one_fold(self):
        """``session_ledger`` must call the shared helper, not keep a copy.

        Identity is the property, not equal behaviour: a second copy answers the
        same values on these inputs and still drifts when one side is edited.
        """
        from kiro_crew import session_ledger

        assert session_ledger.strip_extended_length_prefix is pc.strip_extended_length_prefix

    def test_the_workflow_guard_shares_the_one_fold(self, monkeypatch, tmp_path):
        """``workflow_memory`` must reach the same helper on its guarded path."""
        from kiro_crew import workflow_memory

        calls = []
        real = pc.strip_extended_length_prefix

        def record(path):
            calls.append(path)
            return real(path)

        monkeypatch.setattr(pc, "strip_extended_length_prefix", record)
        workflow_memory._allocator_path(tmp_path / "run-ids.json", tmp_path)
        assert calls, "workflow_memory did not reach the shared fold"


class TestOpenCreateOrExisting:
    """The one create-or-open every lock sidecar shares (SEL chain lock, decision
    log, app-deps provisioning lock): elect one creator, hand contenders the
    creator's inode, never recreate a leaf that vanished."""

    def test_absent_name_is_created_with_the_mode(self, tmp_path):
        target = tmp_path / "x.lock"
        fd = pc.open_create_or_existing(target, os.O_RDWR, 0o600)
        try:
            assert target.exists()
            if pc.IS_POSIX:
                assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        finally:
            os.close(fd)

    def test_existing_name_is_opened_not_truncated(self, tmp_path):
        target = tmp_path / "x.lock"
        target.write_bytes(b"held")
        fd = pc.open_create_or_existing(target, os.O_RDWR)
        try:
            assert os.read(fd, 8) == b"held"
        finally:
            os.close(fd)

    def test_a_missing_parent_is_the_callers_enoent(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            pc.open_create_or_existing(tmp_path / "gone" / "x.lock", os.O_RDWR)

    @pytest.mark.skipif(not pc.IS_POSIX, reason="dir_fd is a POSIX openat feature")
    def test_descriptor_relative_open_lands_under_the_pin(self, tmp_path):
        dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = pc.open_create_or_existing("x.lock", os.O_RDWR, 0o600, dir_fd=dir_fd)
            try:
                assert os.fstat(fd).st_ino == (tmp_path / "x.lock").stat().st_ino
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)

    def test_racing_creators_all_hold_one_inode(self, tmp_path):
        """Forty threads race to create the same absent name, many rounds: every
        caller comes back with a descriptor, and every descriptor names the one
        inode -- the property a nonexclusive ``O_CREAT`` does not give on Darwin."""
        for round_no in range(20):
            target = tmp_path / f"race-{round_no}.lock"
            gate = threading.Barrier(40)
            fds: list[int] = []
            errors: list[BaseException] = []
            lock = threading.Lock()

            def contend() -> None:
                try:
                    gate.wait(timeout=10)
                    fd = pc.open_create_or_existing(target, os.O_RDWR, 0o600)
                    with lock:
                        fds.append(fd)
                except BaseException as exc:  # noqa: BLE001 - recorded for the assert
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=contend) for _ in range(40)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            try:
                assert not errors, errors
                assert len(fds) == 40
                assert len({os.fstat(fd).st_ino for fd in fds}) == 1
            finally:
                for fd in fds:
                    os.close(fd)


class TestOpenLockFileForSweep:
    """``open_lock_file_for_sweep`` lets the orphan-lock sweep delete a lock file
    while still holding its own verification handle on it. On POSIX an open fd
    never blocks an unlink; on Windows a CRT descriptor omits
    ``FILE_SHARE_DELETE``, so the sweep's own handle would block the delete with a
    sharing violation and residue would never be reclaimed (the shard-2 reds). The
    Windows path must open with delete-sharing.
    """

    def test_posix_opens_rdwr_without_following_links_and_allows_unlink_while_open(
        self, tmp_path, monkeypatch
    ):
        if not pc.IS_POSIX:
            pytest.skip("exercises real POSIX unlink-while-open semantics")
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        lock = tmp_path / "lock"
        lock.write_bytes(b"")
        fd = pc.open_lock_file_for_sweep(lock)
        try:
            # The whole point: the inode is deletable while this fd is open, which
            # is what lets the sweep remove residue under its own held lock.
            os.unlink(lock)
            assert not lock.exists()
            assert os.fstat(fd).st_size == 0
        finally:
            os.close(fd)

    def test_posix_refuses_a_symlink_at_the_name(self, tmp_path, monkeypatch):
        if not pc.IS_POSIX:
            pytest.skip("exercises real POSIX symlink + O_NOFOLLOW semantics")
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW unavailable")
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        target = tmp_path / "target"
        target.write_bytes(b"")
        link = tmp_path / "lock"
        os.symlink(target, link)
        with pytest.raises(OSError):
            pc.open_lock_file_for_sweep(link)

    def test_windows_createfilew_requests_delete_sharing_and_read_write(self, monkeypatch):
        # Simulate the Windows path and capture the CreateFileW arguments. The
        # share mode must include FILE_SHARE_DELETE so the subsequent unlink lands
        # while this handle (and its byte-range lock) is still held; without it a
        # sharing violation leaves genuine residue behind.
        captured: dict = {}

        def create_file(path, access, share, security, disposition, flags, template):
            captured.update(
                path=path,
                access=access,
                share=share,
                disposition=disposition,
                flags=flags,
            )
            return 0x1234

        kernel = types.SimpleNamespace(
            CreateFileW=create_file,
            CloseHandle=lambda _h: True,
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
        monkeypatch.setattr(
            pc, "msvcrt", types.SimpleNamespace(open_osfhandle=lambda _h, _flags: 77), raising=False
        )

        fd = pc.open_lock_file_for_sweep(r"C:\agents\alias.lock")

        assert fd == 77
        assert captured["share"] == pc._WIN_FILE_SHARE_READ_WRITE_DELETE
        assert captured["share"] & 0x00000004  # FILE_SHARE_DELETE bit is set
        assert captured["access"] == pc._WIN_GENERIC_READ | pc._WIN_GENERIC_WRITE
        assert captured["disposition"] == pc._WIN_OPEN_EXISTING  # never creates
        assert captured["flags"] == 0  # no OPEN_REPARSE_POINT; caller pre-checks

    def test_windows_closes_the_native_handle_when_wrapping_fails(self, monkeypatch):
        closed: list = []
        kernel = types.SimpleNamespace(
            CreateFileW=lambda *_a: 0x1234,
            CloseHandle=lambda h: closed.append(h) or True,
        )
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)

        def boom(_h, _flags):
            raise OSError("wrap failed")

        monkeypatch.setattr(pc, "msvcrt", types.SimpleNamespace(open_osfhandle=boom), raising=False)

        with pytest.raises(OSError):
            pc.open_lock_file_for_sweep(r"C:\agents\alias.lock")
        assert closed == [0x1234]
