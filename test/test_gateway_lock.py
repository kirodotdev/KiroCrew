"""Tests for the gateway singleton lock.

Covers the P1 acceptance criteria: a second gateway on the same KIROCREW_HOME is
refused naming the holder pid, a stale lock is reclaimed, and distinct homes
both start.
"""

import errno
import json
import os
import stat
import sys
from itertools import islice
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import platform_compat
from kiro_crew.gateway_lock import (
    _IDENTITY_ATTEMPTS,
    _NO_DIRECTORY_LOCK_ERRNOS,
    LOCK_FILENAME,
    GatewayLock,
    GatewayLockError,
    LockHolder,
    LockProbeError,
    _directory_locks_supported,
    _DirectoryLockSupport,
    _is_same_file,
    _read_pid,
    lock_holder,
)


@pytest.fixture
def kernel_lock_home(tmp_path):
    """Keep only kernel-owner tests off a runner's incompatible overlay.

    The wrapper designates an existing tmpfs, never a new mount. Do not select
    another path based on the production lookup's result: that would hide a
    regression. Missing permissions or kernel owner evidence must still fail.
    """
    import tempfile

    root = os.environ.get("KIROCREW_LOCK_TEST_ROOT")
    if root is None:
        yield tmp_path
        return
    with tempfile.TemporaryDirectory(prefix="kc-lock-", dir=root) as home:
        yield Path(home)


@pytest.mark.parametrize("designated", [False, True])
def test_kernel_lock_home_is_scoped_and_cleaned(tmp_path, monkeypatch, designated):
    root = tmp_path / "designated"
    root.mkdir()
    if designated:
        monkeypatch.setenv("KIROCREW_LOCK_TEST_ROOT", str(root))
    else:
        monkeypatch.delenv("KIROCREW_LOCK_TEST_ROOT", raising=False)
    fixture = kernel_lock_home.__wrapped__(tmp_path)
    home = next(fixture)
    try:
        assert home.parent == root if designated else home == tmp_path
        (home / "owned").write_text("fixture data")
    finally:
        fixture.close()
    assert home.exists() is (not designated)
    assert root.is_dir()


def _collect_lock_evidence(path, pids):
    """Read only this test's inode and explicitly supplied holder processes."""
    evidence = {"pids": list(pids), "fdinfo": [], "proc_locks": [], "errors": []}

    def rows(source):
        try:
            with source.open(encoding="utf-8") as stream:
                text = stream.read(65537)
            if len(text) > 65536:
                evidence["errors"].append(f"{source}:truncated")
            return text[:65536].splitlines()
        except OSError as error:
            evidence["errors"].append(f"{source}:{type(error).__name__}")
            return []

    info = path.stat()
    key = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    evidence["stat"] = {"device": info.st_dev, "inode": info.st_ino, "key": key}
    # Do not expose mount options/sources (overlay lowerdirs can name user data).
    mounts = []
    for row in rows(Path("/proc/self/mountinfo")):
        fields = row.split()
        if len(fields) < 7 or "-" not in fields:
            continue
        mount = fields[4].replace(r"\040", " ").replace(r"\134", "\\")
        if path.resolve().is_relative_to(mount):
            mounts.append((len(mount), fields[0], fields[2], mount, fields[fields.index("-") + 1]))
    if mounts:
        _, mount_id, device, mount, filesystem = max(mounts)
        evidence["mount"] = dict(id=mount_id, device=device, path=mount, filesystem=filesystem)

    keys = {key}
    for pid in pids:
        directory = Path(f"/proc/{pid}/fd")
        try:
            descriptors = list(islice(directory.iterdir(), 129))
        except OSError as error:
            evidence["errors"].append(f"{directory}:{type(error).__name__}")
            continue
        if len(descriptors) > 128:
            evidence["errors"].append(f"{directory}:truncated")
        for fd in descriptors[:128]:
            try:
                opened = fd.stat()
            except OSError:
                continue
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                continue
            lock_rows = [
                row[:512]
                for row in rows(Path(f"/proc/{pid}/fdinfo/{fd.name}"))
                if row.startswith("lock:")
            ][:8]
            evidence["fdinfo"].append(dict(pid=pid, fd=fd.name, rows=lock_rows))
            # fdinfo can reveal the kernel device key even when stat differs.
            for row in lock_rows:
                fields = row.split()
                if len(fields) >= 7 and fields[2] == "FLOCK":
                    keys.add(fields[6])
    # Every FLOCK row on this inode, whoever the kernel names: a row naming a
    # pid the caller did not supply (a dead acquirer) is exactly the evidence a
    # failure message needs, and the key scope keeps unrelated locks out.
    for row in rows(Path("/proc/locks")):
        fields = row.split()
        if len(fields) >= 6 and fields[1] == "FLOCK" and fields[5] in keys:
            evidence["proc_locks"].append(row[:512])
            if len(evidence["proc_locks"]) == 8:
                break
    return evidence


def _lock_failure_evidence(path, pids):
    """Assertion-message evaluation is lazy; evidence must not change its verdict."""
    try:
        return json.dumps(_collect_lock_evidence(path, tuple(pids)[:3]), sort_keys=True)[:8192]
    except Exception as error:
        return f"lock evidence unavailable: {type(error).__name__}"


@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
def test_lock_evidence_preserves_assertion_on_probe_error(tmp_path, monkeypatch, error_type):
    def broken(*_args):
        raise error_type("must not replace the original assertion")

    monkeypatch.setattr(sys.modules[__name__], "_collect_lock_evidence", broken)
    with pytest.raises(AssertionError, match="lock evidence unavailable"):
        assert False, _lock_failure_evidence(tmp_path / "gateway.lock", (os.getpid(),))


def test_lock_evidence_is_lazy_and_bounded(tmp_path, monkeypatch):
    probe = MagicMock(return_value={"rows": "x" * 20000})
    monkeypatch.setattr(sys.modules[__name__], "_collect_lock_evidence", probe)
    assert True, _lock_failure_evidence(tmp_path, (os.getpid(),))
    probe.assert_not_called()
    assert len(_lock_failure_evidence(tmp_path, (os.getpid(),))) == 8192


def test_lock_evidence_keeps_fdinfo_device_mismatch_but_not_other_locks(tmp_path, monkeypatch):
    from io import StringIO
    from types import SimpleNamespace

    path = tmp_path / "gateway.lock"
    path.write_text("42\n")
    info = path.stat()
    own_fd = Path("/proc/43/fd/7")
    other_fd = Path("/proc/43/fd/8")
    kernel_key = "ff:ff:987654"
    own_row = f"1: FLOCK ADVISORY WRITE 42 {kernel_key} 0 EOF"
    # The dead acquirer of an inherited descriptor is a pid nobody supplied.
    dead_acquirer_row = f"3: FLOCK ADVISORY WRITE 41 {kernel_key} 0 EOF"
    sources = {
        "/proc/self/mountinfo": (
            "1 0 8:0 / / rw - ext4 ignored rw\n"
            f"2 1 0:9 / {tmp_path.as_posix()} rw - overlay private-source secret-options\n"
        ),
        "/proc/43/fdinfo/7": "lock:\t" + own_row + "\n",
        "/proc/locks": (
            own_row + "\n2: FLOCK ADVISORY WRITE 99 ff:ff:999 0 EOF\n" + dead_acquirer_row + "\n"
        ),
    }
    sources = {Path(source): text for source, text in sources.items()}
    opened = []
    real_stat = Path.stat

    def fake_stat(source, *args, **kwargs):
        if source == own_fd:
            return info
        if source == other_fd:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)
        return real_stat(source, *args, **kwargs)

    def fake_open(source, **_kwargs):
        opened.append(source)
        return StringIO(sources[source])

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "stat", fake_stat)
        scoped.setattr(Path, "open", fake_open)
        scoped.setattr(Path, "iterdir", lambda _source: iter([own_fd, other_fd]))
        scoped.setattr(os, "major", lambda _device: 8, raising=False)
        scoped.setattr(os, "minor", lambda _device: 0, raising=False)
        evidence = _collect_lock_evidence(path, (43,))
    assert evidence["stat"]["key"] != kernel_key
    assert evidence["proc_locks"] == [own_row, dead_acquirer_row]
    assert evidence["fdinfo"] == [{"pid": 43, "fd": "7", "rows": ["lock:\t" + own_row]}]
    assert evidence["mount"]["filesystem"] == "overlay"
    assert "private-source" not in json.dumps(evidence)
    assert "secret-options" not in json.dumps(evidence)
    assert Path("/proc/43/fdinfo/8") not in opened
    assert all(source in sources for source in opened)


def test_acquire_creates_lock_file_with_pid(tmp_path):
    lock = GatewayLock(tmp_path).acquire()
    try:
        lock_file = tmp_path / LOCK_FILENAME
        assert lock_file.is_file()
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


def test_second_acquire_refused_and_names_holder(tmp_path):
    first = GatewayLock(tmp_path).acquire()
    try:
        with pytest.raises(GatewayLockError) as excinfo:
            GatewayLock(tmp_path).acquire()
        # flock is per open-file-description, so a second fd in the same process
        # is refused exactly as a second process would be.
        assert excinfo.value.holder_pid == os.getpid()
        assert str(tmp_path) in str(excinfo.value)
    finally:
        first.release()


def test_stale_lock_is_reclaimed(tmp_path):
    # A leftover lock file with a dead holder's pid but no held flock (the prior
    # process died -> the kernel released its lock). Acquire must succeed and
    # stamp our pid over the stale one.
    lock_file = tmp_path / LOCK_FILENAME
    lock_file.write_text("999999\n")  # pid that is not us and (effectively) dead

    lock = GatewayLock(tmp_path).acquire()
    try:
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


def test_posix_dead_holder_contention_is_retried(tmp_path, monkeypatch):
    """A dying POSIX gateway may release its flock just after our first probe."""
    from kiro_crew import gateway_lock

    lock_file = tmp_path / LOCK_FILENAME
    lock_file.write_text("999999\n", encoding="utf-8")
    attempts = 0

    def acquire_after_teardown(_fd, *, exclusive):
        nonlocal attempts
        assert exclusive is True
        attempts += 1
        return attempts > 1

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "try_acquire_lock", acquire_after_teardown)
    monkeypatch.setattr(
        platform_compat,
        "pid_liveness",
        lambda _pid: platform_compat.PID_DEAD,
    )
    monkeypatch.setattr(gateway_lock.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(GatewayLock, "_acquire_home_anchor", lambda _self: os.dup(0))

    lock = GatewayLock(tmp_path).acquire()
    try:
        assert attempts == 2
    finally:
        lock.release()


@pytest.mark.parametrize(
    "liveness",
    [platform_compat.PID_ALIVE, platform_compat.PID_UNSIGNALABLE],
)
def test_posix_contention_without_confirmed_dead_holder_is_not_retried(
    tmp_path, monkeypatch, liveness
):
    lock_file = tmp_path / LOCK_FILENAME
    lock_file.write_text("4242\n", encoding="utf-8")
    attempts = 0

    def always_busy(_fd, *, exclusive):
        nonlocal attempts
        assert exclusive is True
        attempts += 1
        return False

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "try_acquire_lock", always_busy)
    monkeypatch.setattr(platform_compat, "pid_liveness", lambda _pid: liveness)

    with pytest.raises(GatewayLockError):
        GatewayLock(tmp_path).acquire()

    assert attempts == 1


def test_posix_out_of_range_holder_is_refused_without_retry(tmp_path, monkeypatch):
    """A corrupt numeric stamp must fail closed without crashing or retrying."""
    lock_file = tmp_path / LOCK_FILENAME
    out_of_range_pid = 10**40
    lock_file.write_text(f"{out_of_range_pid}\n", encoding="utf-8")
    attempts = 0

    def always_busy(_fd, *, exclusive):
        nonlocal attempts
        assert exclusive is True
        attempts += 1
        return False

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "try_acquire_lock", always_busy)

    with pytest.raises(GatewayLockError) as excinfo:
        GatewayLock(tmp_path).acquire()

    assert attempts == 1
    assert excinfo.value.holder_pid == out_of_range_pid


def test_distinct_homes_both_acquire(tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    lock_a = GatewayLock(home_a).acquire()
    lock_b = GatewayLock(home_b).acquire()
    try:
        assert (home_a / LOCK_FILENAME).is_file()
        assert (home_b / LOCK_FILENAME).is_file()
    finally:
        lock_a.release()
        lock_b.release()


def test_release_allows_reacquire(tmp_path):
    GatewayLock(tmp_path).acquire().release()
    # Lock is free again -> a fresh acquire succeeds.
    lock = GatewayLock(tmp_path).acquire()
    lock.release()


def test_release_is_idempotent(tmp_path):
    lock = GatewayLock(tmp_path).acquire()
    lock.release()
    lock.release()  # no raise


def test_context_manager_releases(tmp_path):
    with GatewayLock(tmp_path):
        with pytest.raises(GatewayLockError):
            GatewayLock(tmp_path).acquire()
    # Block exited -> lock released -> re-acquire works.
    GatewayLock(tmp_path).acquire().release()


def test_acquire_creates_missing_home(tmp_path):
    home = tmp_path / "does" / "not" / "exist"
    lock = GatewayLock(home).acquire()
    try:
        assert (home / LOCK_FILENAME).is_file()
    finally:
        lock.release()


def test_read_pid_handles_garbage(tmp_path):
    lock_file = tmp_path / LOCK_FILENAME
    lock_file.write_text("not-a-pid")
    fd = os.open(lock_file, os.O_RDONLY)
    try:
        assert _read_pid(fd) is None
    finally:
        os.close(fd)


def test_windows_stale_pid_reclaimed_on_startup(tmp_path, monkeypatch):
    """On Windows, if the lock file holds a dead PID, the file is deleted and
    recreated before acquisition so that msvcrt.locking gets a clean file."""
    from kiro_crew import platform_compat

    # Simulate Windows platform
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    # Make pid_liveness report the stale PID as dead
    monkeypatch.setattr(platform_compat, "pid_liveness", lambda pid: platform_compat.PID_DEAD)

    # Pre-create a lock file with a fake dead PID
    lock_file = tmp_path / LOCK_FILENAME
    lock_file.write_text("999999\n")

    lock = GatewayLock(tmp_path).acquire()
    try:
        # The lock should have been acquired successfully over the stale file.
        assert lock_file.is_file()
    finally:
        lock.release()

    # Read the pid stamp only AFTER releasing: on real Windows ``msvcrt.locking``
    # is a MANDATORY byte-range lock, so a second handle opened while we hold it
    # fails with ``PermissionError`` rather than returning the contents.
    assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())


class TestLockFileIdentity:
    """Deleting the lock file must not admit a second gateway.

    An ``flock`` belongs to the open file description, so unlinking the lock
    file releases nothing -- but it does make that inode unreachable by name, so
    an acquire that trusts the name alone creates a fresh inode, locks it
    unopposed, and runs as a second writer on the same home. No zombie and no
    inherited descriptor are needed; a healthy running gateway is enough. Two
    guards close it: the exclusive lock on the home DIRECTORY, which deletion
    cannot reach, and an identity check that refuses to keep a lock on an inode
    the path does not name.
    """

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS, reason="an open Windows lock file cannot be deleted"
    )
    def test_unlink_then_acquire_is_refused(self, tmp_path):
        if _directory_locks_supported(tmp_path)[0] is not _DirectoryLockSupport.SUPPORTED:
            pytest.skip("this filesystem does not implement directory locks")
        held = GatewayLock(tmp_path).acquire()
        try:
            os.unlink(tmp_path / LOCK_FILENAME)
            with pytest.raises(GatewayLockError) as excinfo:
                GatewayLock(tmp_path).acquire()
            text = str(excinfo.value)
            assert "no longer names it" in text
            assert "neither stops a gateway nor releases its lock" in text
        finally:
            held.release()

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_release_frees_the_home_anchor_so_the_home_can_be_reacquired(self, tmp_path):
        # flock is per open file description, so a leaked anchor descriptor would
        # refuse the next acquire from this very process.
        lock = GatewayLock(tmp_path).acquire()
        lock.release()
        assert lock._home_fd is None
        GatewayLock(tmp_path).acquire().release()

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows refuses to unlink a file its holder has open, so the lock path "
        "cannot be replaced under a live acquire there",
    )
    def test_a_path_replaced_mid_acquire_is_relocked_on_the_current_inode(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / LOCK_FILENAME
        opens: list[int] = []
        real_open = GatewayLock._open_lock_file

        def replace_after_first_open(lock):
            fd = real_open(lock)
            opens.append(fd)
            if len(opens) == 1:
                # The window between the open and the lock: the path is unlinked
                # and a new file is created under the same name.
                os.unlink(path)
                path.write_text("999999\n", encoding="utf-8")
            return fd

        monkeypatch.setattr(GatewayLock, "_open_lock_file", replace_after_first_open)
        lock = GatewayLock(tmp_path).acquire()
        try:
            assert len(opens) == 2  # the orphaned inode was dropped, not kept
            assert _is_same_file(lock._fd, path)
            assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            lock.release()

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows refuses to unlink a file its holder has open, so the lock path "
        "cannot be replaced under a live acquire there",
    )
    def test_acquire_refuses_a_path_replaced_on_every_attempt(self, tmp_path, monkeypatch):
        path = tmp_path / LOCK_FILENAME
        real_open = GatewayLock._open_lock_file

        def always_replace(lock):
            fd = real_open(lock)
            os.unlink(path)
            path.write_text("1\n", encoding="utf-8")
            return fd

        monkeypatch.setattr(GatewayLock, "_open_lock_file", always_replace)
        with pytest.raises(GatewayLockError, match="being replaced faster"):
            GatewayLock(tmp_path).acquire()

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_a_filesystem_without_directory_locks_still_starts(self, tmp_path, monkeypatch):
        """Missing evidence must never refuse a legitimate start."""
        from kiro_crew import gateway_lock

        anchor = os.open(tmp_path, os.O_RDONLY)
        try:
            if not platform_compat.try_acquire_lock(anchor, exclusive=True):
                pytest.skip("this filesystem does not implement directory locks")
            # A held home on a filesystem that does lock directories is a real
            # incumbent, so the start is refused.
            monkeypatch.setattr(
                gateway_lock,
                "_directory_locks_supported",
                lambda _home: (_DirectoryLockSupport.SUPPORTED, None),
            )
            with pytest.raises(GatewayLockError):
                GatewayLock(tmp_path).acquire()
            # The same held home, reported as a filesystem that cannot lock
            # directories at all: that is no evidence, so the start goes ahead.
            monkeypatch.setattr(
                gateway_lock,
                "_directory_locks_supported",
                lambda _home: (_DirectoryLockSupport.UNSUPPORTED, None),
            )
            GatewayLock(tmp_path).acquire().release()
        finally:
            platform_compat.release_lock(anchor)
            os.close(anchor)

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_a_transiently_held_anchor_is_retried_rather_than_refused(self, tmp_path, monkeypatch):
        # `lock_holder` takes the same anchor non-destructively and drops it two
        # syscalls later, so a `stop` or `restart` running beside a legitimate
        # start must not refuse that start.
        real_anchor = GatewayLock._acquire_home_anchor
        calls: list[int] = []

        def refuse_once(lock):
            calls.append(1)
            if len(calls) == 1:
                raise GatewayLockError(lock._home, None, "a probe holds the anchor")
            return real_anchor(lock)

        monkeypatch.setattr(GatewayLock, "_acquire_home_anchor", refuse_once)
        lock = GatewayLock(tmp_path).acquire()
        try:
            assert len(calls) == 2  # the refusal was retried, not raised
            assert (tmp_path / LOCK_FILENAME).read_text(encoding="utf-8").strip() == str(
                os.getpid()
            )
        finally:
            lock.release()

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_a_permanently_held_anchor_is_still_refused(self, tmp_path, monkeypatch):
        # The other half of the same discrimination: an incumbent holds the
        # anchor for its whole lifetime, so every attempt fails and the refusal
        # stands. Retrying must not soften it.
        calls: list[int] = []

        def always_refuse(lock):
            calls.append(1)
            raise GatewayLockError(lock._home, None, "an incumbent holds the anchor")

        monkeypatch.setattr(GatewayLock, "_acquire_home_anchor", always_refuse)
        with pytest.raises(GatewayLockError, match="an incumbent holds the anchor"):
            GatewayLock(tmp_path).acquire()
        assert len(calls) == _IDENTITY_ATTEMPTS

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_indeterminate_support_probe_refuses_a_held_anchor(self, tmp_path, monkeypatch):
        from kiro_crew import gateway_lock

        held = GatewayLock(tmp_path).acquire()
        try:
            os.unlink(tmp_path / LOCK_FILENAME)

            def fail_probe(*_args, **_kwargs):
                raise OSError(errno.ENOSPC, "probe has no space")

            monkeypatch.setattr(gateway_lock.tempfile, "TemporaryDirectory", fail_probe)
            with pytest.raises(GatewayLockError) as excinfo:
                GatewayLock(tmp_path).acquire()
            text = str(excinfo.value)
            assert "held or its lock support could not be determined" in text
            assert "probe has no space" in text
            assert "may be stale" not in text
            assert "kill" not in text.lower()
        finally:
            held.release()

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_indeterminate_support_probe_is_not_nobody(self, tmp_path, monkeypatch):
        from kiro_crew import gateway_lock

        held = GatewayLock(tmp_path).acquire()
        try:
            os.unlink(tmp_path / LOCK_FILENAME)

            def fail_probe(*_args, **_kwargs):
                raise OSError(errno.ENOSPC, "probe has no space")

            monkeypatch.setattr(gateway_lock.tempfile, "TemporaryDirectory", fail_probe)
            with pytest.raises(LockProbeError, match="probe has no space"):
                lock_holder(tmp_path)
        finally:
            held.release()

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_unopenable_home_refuses_acquire(self, tmp_path, monkeypatch):
        real_open = os.open

        def fail_home_open(path, flags, *args, **kwargs):
            if Path(path) == tmp_path and flags == os.O_RDONLY:
                raise OSError(errno.EMFILE, "cannot open home anchor")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", fail_home_open)
        with pytest.raises(GatewayLockError, match="cannot open home anchor"):
            GatewayLock(tmp_path).acquire()

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_unopenable_home_is_indeterminate_for_lock_holder(self, tmp_path, monkeypatch):
        real_open = os.open

        def fail_home_open(path, flags, *args, **kwargs):
            if Path(path) == tmp_path and flags == os.O_RDONLY:
                raise OSError(errno.EMFILE, "cannot open home anchor")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", fail_home_open)
        with pytest.raises(LockProbeError, match="cannot open home anchor"):
            lock_holder(tmp_path)

    @pytest.mark.skipif(
        not platform_compat.IS_WINDOWS,
        reason="pins the Windows-only property that the home-anchor exemption rests on",
    )
    def test_windows_refuses_to_unlink_a_lock_file_its_holder_has_open(self, tmp_path):
        """Windows takes no anchor, because deletion cannot orphan the rendezvous there.

        That exemption is not this module's own property: it is the share mode of
        the handle ``os.open`` asks for, which denies delete while the handle
        lives. Nothing else pins it, so a change in that behaviour would leave
        Windows with no durable rendezvous and no test to say so.
        """
        held = GatewayLock(tmp_path).acquire()
        try:
            assert held._home_fd is None
            with pytest.raises(OSError):
                os.unlink(tmp_path / LOCK_FILENAME)
            assert (tmp_path / LOCK_FILENAME).is_file()
        finally:
            held.release()

    @pytest.mark.skipif(
        sys.platform not in ("linux", "darwin"),
        reason="asserts the capability only on the two platforms that promise it",
    )
    def test_directory_locks_are_available_on_this_platform(self, tmp_path):
        """The anchor's guard tests step aside where a directory cannot be locked.

        That is the right behaviour for an exotic filesystem and the wrong signal
        for a platform: if ``flock`` on a directory descriptor stopped working on
        linux or darwin, every test above would skip and the suite would read as
        green while the guard was absent. Assert the capability itself, so that
        outcome is a red on the platform it happens to rather than a silence.
        """
        support, error = _directory_locks_supported(tmp_path)
        assert support is _DirectoryLockSupport.SUPPORTED
        assert error is None

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="os.open() refuses a directory on Windows, so the probe reads indeterminate "
        "before the lock is ever tried; neither caller consults it there",
    )
    @pytest.mark.parametrize("code", sorted(_NO_DIRECTORY_LOCK_ERRNOS))
    def test_support_probe_reports_a_filesystem_that_cannot_lock_directories(
        self, tmp_path, monkeypatch, code
    ):
        """Only ``flock`` saying "no directory locks here" may read as unsupported."""

        def refuse_as_unsupported(fd, op):
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(platform_compat.fcntl, "flock", refuse_as_unsupported)
        support, error = _directory_locks_supported(tmp_path)
        assert support is _DirectoryLockSupport.UNSUPPORTED
        assert error is None

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="os.open() refuses a directory on Windows, so the probe reads indeterminate "
        "before the lock is ever tried; neither caller consults it there",
    )
    @pytest.mark.parametrize("code", [errno.ENOLCK, errno.EIO, errno.EWOULDBLOCK])
    def test_a_lock_error_on_the_probe_is_indeterminate_not_unsupported(
        self, tmp_path, monkeypatch, code
    ):
        """A full lock table is a measurement that did not happen, not a verdict.

        ``try_acquire_lock`` folds this into the same ``False`` as "unsupported";
        read that way it would let a start past a held home, which is why the
        probe takes the flock itself.
        """

        def refuse_transiently(fd, op):
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(platform_compat.fcntl, "flock", refuse_transiently)
        support, error = _directory_locks_supported(tmp_path)
        assert support is _DirectoryLockSupport.INDETERMINATE
        assert isinstance(error, OSError) and error.errno == code

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_a_lock_error_on_the_probe_refuses_a_held_home(self, tmp_path, monkeypatch):
        """The end-to-end shape the finding names: held home + ENOLCK probe -> refused."""
        anchor = os.open(tmp_path, os.O_RDONLY)
        try:
            if not platform_compat.try_acquire_lock(anchor, exclusive=True):
                pytest.skip("this filesystem does not implement directory locks")
            real_flock = platform_compat.fcntl.flock

            def full_lock_table_on_the_probe(fd, op):
                # Only the throwaway probe directory: a directory that is not the
                # home itself. The lock FILE and the home anchor keep real flocks.
                info = os.fstat(fd)
                if stat.S_ISDIR(info.st_mode) and info.st_ino != os.fstat(anchor).st_ino:
                    raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
                return real_flock(fd, op)

            monkeypatch.setattr(platform_compat.fcntl, "flock", full_lock_table_on_the_probe)
            with pytest.raises(GatewayLockError) as excinfo:
                GatewayLock(tmp_path).acquire()
            text = str(excinfo.value)
            assert "could not be determined" in text
            assert "may be stale" not in text
            assert "kill" not in text.lower()
        finally:
            platform_compat.release_lock(anchor)
            os.close(anchor)

    def test_support_probe_reports_a_home_it_cannot_write_in(self, tmp_path):
        support, error = _directory_locks_supported(tmp_path / "does-not-exist")
        assert support is _DirectoryLockSupport.INDETERMINATE
        assert isinstance(error, OSError)


class TestLockHolder:
    """The non-destructive lock-owner oracle: a caller that must not ACQUIRE
    (``cli_perf``'s profiler target, and the CLI ``_stop``/``_restart``
    lock fallback) still needs to ask who owns a home."""

    def test_no_lock_file_reports_none(self, tmp_path):
        holder = lock_holder(tmp_path)
        assert holder == LockHolder(pid=None, alive=False, source="none")

    @pytest.mark.skipif(sys.platform != "linux", reason="relies on /proc/locks")
    def test_live_holder_is_named_and_alive(self, kernel_lock_home):
        tmp_path = kernel_lock_home
        # /proc/locks names the acquirer only on Linux. On Windows the held
        # file cannot even be read (msvcrt.locking is mandatory), so there is
        # no recorded-pid fallback to assert either.
        held = GatewayLock(tmp_path).acquire()
        try:
            holder = lock_holder(tmp_path)
            assert holder.pid == os.getpid()
            assert holder.alive is True
            assert holder.source == "flock_owner", _lock_failure_evidence(
                tmp_path / LOCK_FILENAME, (os.getpid(),)
            )
        finally:
            held.release()

    def test_released_lock_is_free_even_though_the_file_names_a_live_pid(self, tmp_path):
        # release() never clears the recorded pid, so after a clean stop the
        # file still names the last holder -- here our own, very much alive,
        # pid. A free lock must read as "nobody", not as that pid: `_stop` and
        # `_restart` SIGTERM (or refuse over) whatever this names, and
        # `cli_perf` profiles it, so a stale number reused by an unrelated
        # process would be acted on as if it were the gateway.
        held = GatewayLock(tmp_path).acquire()
        held.release()
        assert (tmp_path / LOCK_FILENAME).read_text().strip() == str(os.getpid())
        assert lock_holder(tmp_path) == LockHolder(pid=None, alive=False, source="none")

    def test_free_lock_ignores_a_reused_recorded_pid_without_proc_locks(
        self, tmp_path, monkeypatch
    ):
        # The macOS/Windows shape: no /proc/locks, the file names a pid that
        # is alive (reused by a stranger), and nothing holds the lock. The
        # held-probe is what keeps the stranger from being named.
        (tmp_path / LOCK_FILENAME).write_text("4242\n")
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: None
        )
        monkeypatch.setattr("kiro_crew.gateway_lock.platform_compat.pid_exists", lambda pid: True)
        assert lock_holder(tmp_path) == LockHolder(pid=None, alive=False, source="none")

    def test_held_probe_error_is_indeterminate_not_a_holder(self, tmp_path, monkeypatch):
        # The chain this must break: probe error read as "held" -> fall back
        # to the recorded pid -> that number reused by an unrelated live
        # process -> `stop`/`restart` signal it. An unlockable file is
        # INDETERMINATE: no LockHolder is produced, a typed error is raised.
        (tmp_path / LOCK_FILENAME).write_text("4242\n")
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.try_acquire_lock",
            lambda fd, **k: (_ for _ in ()).throw(OSError("flock unsupported")),
        )
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: None
        )
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.pid_exists", lambda pid: pid == 4242
        )
        with pytest.raises(LockProbeError) as excinfo:
            lock_holder(tmp_path)
        assert excinfo.value.path == tmp_path / LOCK_FILENAME
        assert "could not determine whether a gateway holds the lock" in str(excinfo.value)
        assert "flock unsupported" in str(excinfo.value)

    def test_probe_error_still_names_a_live_proc_locks_acquirer(self, tmp_path, monkeypatch):
        # /proc/locks naming a LIVE acquirer is positive ownership on its own;
        # the probe failing does not take that answer away.
        (tmp_path / LOCK_FILENAME).write_text("111\n")
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.try_acquire_lock",
            lambda fd, **k: (_ for _ in ()).throw(OSError("flock unsupported")),
        )
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: 222
        )
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.pid_exists", lambda pid: pid == 222
        )
        assert lock_holder(tmp_path) == LockHolder(pid=222, alive=True, source="flock_owner")

    def test_probe_error_with_a_dead_proc_locks_acquirer_is_indeterminate(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / LOCK_FILENAME).write_text("111\n")
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.try_acquire_lock",
            lambda fd, **k: (_ for _ in ()).throw(OSError("flock unsupported")),
        )
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: 222
        )
        monkeypatch.setattr("kiro_crew.gateway_lock.platform_compat.pid_exists", lambda pid: False)
        with pytest.raises(LockProbeError):
            lock_holder(tmp_path)

    def test_held_lock_whose_proc_locks_acquirer_is_dead_is_indeterminate(
        self, tmp_path, monkeypatch
    ):
        # The probe says held and /proc/locks names the acquirer, but that pid
        # is gone: a forked child carries the descriptor on. Not "nobody" --
        # restart would spawn a replacement straight into the held lock -- and
        # not a holder to signal either.
        (tmp_path / LOCK_FILENAME).write_text("111\n")
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.try_acquire_lock", lambda fd, **k: False
        )
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: 222
        )
        monkeypatch.setattr("kiro_crew.gateway_lock.platform_compat.pid_exists", lambda pid: False)
        with pytest.raises(LockProbeError, match="pid 222"):
            lock_holder(tmp_path)

    def test_held_lock_with_a_dead_recorded_pid_is_indeterminate(self, tmp_path, monkeypatch):
        # Held, no /proc/locks, and the file names a pid that is gone: somebody
        # holds the lock but nothing can name them. That is neither "nobody"
        # (stop/restart would report nothing running on a lock `kirocrew
        # gateway` refuses to start on) nor a holder to act on.
        (tmp_path / LOCK_FILENAME).write_text("4242\n")
        monkeypatch.setattr("kiro_crew.gateway_lock._lock_is_held", lambda _p: True)
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: None
        )
        monkeypatch.setattr("kiro_crew.gateway_lock.platform_compat.pid_exists", lambda pid: False)
        with pytest.raises(LockProbeError):
            lock_holder(tmp_path)

    def test_unreadable_lock_file_is_still_probed(self, tmp_path, monkeypatch):
        # The Windows shape: the gateway's mandatory lock makes the file
        # unreadable. Not being able to read it is not evidence of "nobody"; the
        # probe decides. Free -> nobody; held with no nameable holder ->
        # indeterminate.
        (tmp_path / LOCK_FILENAME).write_text("4242\n")
        real_open = os.open

        def denied(path, *a, **k):
            if str(path).endswith(LOCK_FILENAME) and a and a[0] == os.O_RDONLY:
                raise OSError("sharing violation")
            return real_open(path, *a, **k)

        monkeypatch.setattr(os, "open", denied)
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: None
        )
        monkeypatch.setattr("kiro_crew.gateway_lock._lock_is_held", lambda _p: False)
        assert lock_holder(tmp_path) == LockHolder(pid=None, alive=False, source="none")
        monkeypatch.setattr("kiro_crew.gateway_lock._lock_is_held", lambda _p: True)
        with pytest.raises(LockProbeError):
            lock_holder(tmp_path)

    def test_missing_lock_file_is_nobody(self, tmp_path):
        assert lock_holder(tmp_path) == LockHolder(pid=None, alive=False, source="none")

    @pytest.mark.parametrize("content", ["", "not-a-pid", "0\n", "-5\n"])
    def test_garbage_recorded_pid_with_no_flock_owner_is_none(self, tmp_path, monkeypatch, content):
        # No /proc/locks surface on this platform/test AND nothing plausible
        # recorded: must not fabricate a pid from garbage content. A free lock
        # is nobody; a held one with garbage for a pid is indeterminate.
        (tmp_path / LOCK_FILENAME).write_text(content)
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: None
        )
        monkeypatch.setattr("kiro_crew.gateway_lock._lock_is_held", lambda _p: False)
        assert lock_holder(tmp_path).pid is None
        monkeypatch.setattr("kiro_crew.gateway_lock._lock_is_held", lambda _p: True)
        with pytest.raises(LockProbeError):
            lock_holder(tmp_path)

    def test_recorded_pid_used_only_when_flock_owner_is_unavailable(self, tmp_path, monkeypatch):
        (tmp_path / LOCK_FILENAME).write_text("4242\n")
        monkeypatch.setattr("kiro_crew.gateway_lock._lock_is_held", lambda _p: True)
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: None
        )
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.pid_exists", lambda pid: pid == 4242
        )
        holder = lock_holder(tmp_path)
        assert holder == LockHolder(pid=4242, alive=True, source="recorded_pid")

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_a_deleted_lock_file_does_not_read_as_nobody(self, tmp_path):
        # The state `stop`/`restart` must not mistake for "nothing running": the
        # lock file is gone, the gateway holding the home is not.
        if _directory_locks_supported(tmp_path)[0] is not _DirectoryLockSupport.SUPPORTED:
            pytest.skip("this filesystem does not implement directory locks")
        held = GatewayLock(tmp_path).acquire()
        try:
            os.unlink(tmp_path / LOCK_FILENAME)
            try:
                holder = lock_holder(tmp_path)
            except LockProbeError as exc:
                # Off Linux no surface names a directory's flock owner, so the
                # answer is indeterminate rather than a pid to signal.
                assert "no longer names" in str(exc)
            else:
                assert holder == LockHolder(pid=os.getpid(), alive=True, source="home_anchor")
        finally:
            held.release()

    def test_flock_owner_outranks_a_disagreeing_recorded_pid(self, tmp_path, monkeypatch):
        # A forked inheritor keeps the flock alive under a DIFFERENT pid than
        # the one last written to the file -- the authoritative source wins.
        (tmp_path / LOCK_FILENAME).write_text("111\n")
        monkeypatch.setattr("kiro_crew.gateway_lock._lock_is_held", lambda _p: True)
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: 222
        )
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.pid_exists", lambda pid: pid == 222
        )
        holder = lock_holder(tmp_path)
        assert holder == LockHolder(pid=222, alive=True, source="flock_owner")

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_a_transiently_anchored_home_is_reprobed_and_reads_as_nobody(
        self, tmp_path, monkeypatch
    ):
        # Two non-destructive readers on an idle home (`stop` beside `cli_perf`)
        # each hold the anchor for two syscalls, so either can see the other's
        # hold. One anchored reading is a candidate, not a verdict: a later free
        # reading is positive evidence and the answer is nobody.
        readings: list[bool] = []

        def anchored_once(_home, _path):
            readings.append(True)
            return len(readings) == 1

        monkeypatch.setattr("kiro_crew.gateway_lock._home_is_anchored", anchored_once)
        monkeypatch.setattr("kiro_crew.gateway_lock.time.sleep", lambda _s: None)
        holder = lock_holder(tmp_path)
        assert holder == LockHolder(pid=None, alive=False, source="none")
        assert len(readings) > 1  # the first reading was re-probed, not believed

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_a_home_anchored_on_every_probe_is_still_indeterminate(self, tmp_path, monkeypatch):
        # The other half: an incumbent holds the anchor for its lifetime, so
        # every probe reads held. Re-probing must not soften that into nobody
        # when no surface can name the holder.
        readings: list[bool] = []

        def always_anchored(_home, _path):
            readings.append(True)
            return True

        monkeypatch.setattr("kiro_crew.gateway_lock._home_is_anchored", always_anchored)
        monkeypatch.setattr("kiro_crew.gateway_lock.time.sleep", lambda _s: None)
        monkeypatch.setattr(
            "kiro_crew.gateway_lock.platform_compat.flock_owner_pid", lambda _p: None
        )
        with pytest.raises(LockProbeError, match="no longer names"):
            lock_holder(tmp_path)
        assert len(readings) == _IDENTITY_ATTEMPTS

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_an_anchor_probe_error_propagates_without_a_retry(self, tmp_path, monkeypatch):
        # An unopenable home or an unmeasurable filesystem is a different fact
        # from a held anchor: it is not a transient another reader will release,
        # so it is raised on the attempt that produced it.
        readings: list[bool] = []

        def probe_fails(_home, path):
            readings.append(True)
            raise LockProbeError(path, OSError(errno.EMFILE, "cannot open home anchor"))

        monkeypatch.setattr("kiro_crew.gateway_lock._home_is_anchored", probe_fails)
        with pytest.raises(LockProbeError, match="cannot open home anchor"):
            lock_holder(tmp_path)
        assert len(readings) == 1
