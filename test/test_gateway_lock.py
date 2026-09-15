"""Tests for the gateway singleton lock.

Covers the P1 acceptance criteria: a second gateway on the same KIROCREW_HOME is
refused naming the holder pid, a stale lock is reclaimed, and distinct homes
both start.
"""

import os
import sys

import pytest

from kiro_crew.gateway_lock import (
    LOCK_FILENAME,
    GatewayLock,
    GatewayLockError,
    LockHolder,
    LockProbeError,
    _read_pid,
    lock_holder,
)


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


class TestLockHolder:
    """The non-destructive lock-owner oracle: a caller that must not ACQUIRE
    (``cli_perf``'s profiler target, and the CLI ``_stop``/``_restart``
    lock fallback) still needs to ask who owns a home."""

    def test_no_lock_file_reports_none(self, tmp_path):
        holder = lock_holder(tmp_path)
        assert holder == LockHolder(pid=None, alive=False, source="none")

    @pytest.mark.skipif(sys.platform != "linux", reason="relies on /proc/locks")
    def test_live_holder_is_named_and_alive(self, tmp_path):
        # /proc/locks names the acquirer only on Linux. On Windows the held
        # file cannot even be read (msvcrt.locking is mandatory), so there is
        # no recorded-pid fallback to assert either.
        held = GatewayLock(tmp_path).acquire()
        try:
            holder = lock_holder(tmp_path)
            assert holder.pid == os.getpid()
            assert holder.alive is True
            assert holder.source == "flock_owner"
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
