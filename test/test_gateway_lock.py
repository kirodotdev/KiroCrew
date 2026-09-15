"""Tests for the gateway singleton lock.

Covers the P1 acceptance criteria: a second gateway on the same KIROCREW_HOME is
refused naming the holder pid, a stale lock is reclaimed, and distinct homes
both start.
"""

import os
import sys

import pytest

from kiro_crew import platform_compat
from kiro_crew.gateway_lock import (
    _IDENTITY_ATTEMPTS,
    LOCK_FILENAME,
    GatewayLock,
    GatewayLockError,
    LockHolder,
    LockProbeError,
    _directory_locks_supported,
    _is_same_file,
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
        if not _directory_locks_supported(tmp_path):
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
            monkeypatch.setattr(gateway_lock, "_directory_locks_supported", lambda _home: True)
            with pytest.raises(GatewayLockError):
                GatewayLock(tmp_path).acquire()
            # The same held home, reported as a filesystem that cannot lock
            # directories at all: that is no evidence, so the start goes ahead.
            monkeypatch.setattr(gateway_lock, "_directory_locks_supported", lambda _home: False)
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

    def test_support_probe_reports_a_filesystem_that_cannot_lock_directories(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(platform_compat, "try_acquire_lock", lambda fd, **k: False)
        assert _directory_locks_supported(tmp_path) is False

    def test_support_probe_reports_a_home_it_cannot_write_in(self, tmp_path):
        assert _directory_locks_supported(tmp_path / "does-not-exist") is False


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

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no home anchor on Windows")
    def test_a_deleted_lock_file_does_not_read_as_nobody(self, tmp_path):
        # The state `stop`/`restart` must not mistake for "nothing running": the
        # lock file is gone, the gateway holding the home is not.
        if not _directory_locks_supported(tmp_path):
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
