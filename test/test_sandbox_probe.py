"""Tests for sandbox availability probes — distinct paths."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
from unittest.mock import mock_open, patch

import pytest

import kiro_crew.sandbox as sb
from kiro_crew.sandbox import _probe_sandbox_exec

# The namespace probe internals are Linux-only by construction: they call
# unshare(2), write /proc/<pid> identity maps, and use select.poll / os.WNOHANG,
# none of which exist on Windows. Production never reaches them off Linux —
# _probe_unshare() early-returns — so exercising them elsewhere tests nothing.
# (test_non_linux_never_probes below deliberately stays unmarked: it asserts that
# early return and mocks the platform, so it must run everywhere.)
_linux_only = pytest.mark.skipif(
    sys.platform != "linux",
    reason="namespace probe internals use Linux-only APIs (unshare, /proc "
    "identity maps, select.poll, os.WNOHANG)",
)


class _ChildExit(Exception):
    """Stands in for the probe child's ``os._exit``, which a test cannot survive."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _exit_recorder(codes: list):
    """An ``os._exit`` stand-in that records each code and unwinds instead of exiting."""

    def _fake_exit(code: int) -> None:
        codes.append(code)
        raise _ChildExit(code)

    return _fake_exit


@patch("kiro_crew.sandbox.sys")
def test_non_darwin_returns_false(mock_sys):
    mock_sys.platform = "linux"
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=False)
def test_sandbox_exec_not_found_returns_false(mock_exists, mock_sys):
    mock_sys.platform = "darwin"
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=True)
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_works(mock_run, mock_exists, mock_sys):
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    assert _probe_sandbox_exec() is True


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=True)
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_works_on_macos_26(mock_run, mock_exists, mock_sys):
    """macOS 26 (Tahoe) is NOT hard-blocked: sandbox-exec + the Seatbelt kernel
    subsystem still work there, so the probe decides empirically. A passing probe
    returns True regardless of OS version — the old ``major >= 26 -> return False``
    gate was removed after verifying the real profile compiles, runs kiro-cli, and
    enforces credential-path denies on macOS 26.5."""
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    assert _probe_sandbox_exec() is True


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=True)
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_fails_returns_false(mock_run, mock_exists, mock_sys):
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", side_effect=[True, False])
@patch("kiro_crew.sandbox.subprocess.run")
def test_missing_trusted_probe_binary_fails_closed(mock_run, mock_exists, mock_sys):
    mock_sys.platform = "darwin"

    assert _probe_sandbox_exec() is False
    mock_run.assert_not_called()
    assert mock_exists.call_count == 2


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=True)
@patch("kiro_crew.sandbox.subprocess.run", side_effect=OSError("timeout"))
def test_subprocess_exception_returns_false(mock_run, mock_exists, mock_sys):
    mock_sys.platform = "darwin"
    assert _probe_sandbox_exec() is False


def test_userns_available_delegates_to_probe(monkeypatch):
    """Public userns_available() is a stable alias for the private probe."""
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb, "_probe_unshare", lambda: True)
    assert sb.userns_available() is True
    monkeypatch.setattr(sb, "_probe_unshare", lambda: False)
    assert sb.userns_available() is False


@pytest.fixture(autouse=True)
def _reset_wsl_cache():
    """Clear the ``is_wsl`` lru_cache before AND after every test.

    ``is_wsl`` is ``@lru_cache``-decorated and process-wide, so a
    monkeypatch-derived result cached inside a test would otherwise leak into
    later tests in the same pytest-xdist worker (e.g. a future JailProvider
    test that consults ``is_wsl()`` would see a stale ``True`` on a native
    Linux host). Tearing down the cache keeps each test hermetic.
    """
    import kiro_crew.sandbox as sb

    sb.is_wsl.cache_clear()
    yield
    sb.is_wsl.cache_clear()


def test_is_wsl_false_off_linux(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "darwin")
    assert sb.is_wsl() is False


def test_is_wsl_true_via_env_distro(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    assert sb.is_wsl() is True


def test_is_wsl_true_via_env_interop(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/8_interop")
    assert sb.is_wsl() is True


def test_is_wsl_true_via_proc_version(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    version = "Linux version 5.15.0-microsoft-standard-WSL2 (gcc ...)"
    with patch("builtins.open", mock_open(read_data=version)):
        assert sb.is_wsl() is True


def test_is_wsl_false_on_native_linux(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    version = "Linux version 6.12.90-120.amzn2023.aarch64 (mockbuild@...)"
    with patch("builtins.open", mock_open(read_data=version)):
        assert sb.is_wsl() is False


def test_is_wsl_false_when_proc_version_unreadable(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    with patch("builtins.open", side_effect=OSError("no /proc")):
        assert sb.is_wsl() is False


@pytest.fixture
def pipe_fds():
    """A real pipe pair for probe-parent tests, closed on teardown.

    Real fds keep these tests off ``os.write``/``os.read`` monkeypatches, which
    would collide with pytest's own fd-level output capture.
    """
    read_fd, write_fd = os.pipe()
    yield read_fd, write_fd
    for fd in (read_fd, write_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def _scripted_steps(*reports):
    """Feed the probe parent a scripted sequence of child step reports."""
    queue = list(reports)
    return lambda _fd: queue.pop(0) if queue else None


@_linux_only
class TestProbeSplitSequence:
    """The probe must mirror the launcher's SPLIT unshare sequence.

    Regression cover for the false positive on Ubuntu >= 23.10: with
    ``kernel.apparmor_restrict_unprivileged_userns=1`` a combined
    ``unshare(NEWUSER|NEWNS)`` is satisfied atomically and succeeds, while the
    launcher's split form gets EPERM at the second call — so the old combined
    probe reported the host sandbox-capable and every real spawn then died.

    These tests drive the parent's verdict logic directly, so none of them fork
    a real process or need a restricted kernel.
    """

    def test_newns_denial_is_permanent_and_names_the_step(self, monkeypatch, pipe_fds):
        """The Ubuntu >= 23.10 shape: NEWUSER and the maps succeed, NEWNS is denied."""
        read_fd, write_fd = pipe_fds
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("U", 0), ("N", errno.EPERM)))
        monkeypatch.setattr(sb, "_probe_write_identity_maps", lambda *_a: None)

        ok, transient, reason, _remedy = sb._probe_parent_sequence(4242, read_fd, write_fd, 1000, 1000)

        assert ok is False
        assert transient is False, "an AppArmor userns denial will not clear on retry"
        assert "CLONE_NEWNS" in reason, reason
        assert "EPERM" in reason, reason

    def test_full_sequence_success_reports_ok(self, monkeypatch, pipe_fds):
        read_fd, write_fd = pipe_fds
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("U", 0), ("N", 0)))
        monkeypatch.setattr(sb, "_probe_write_identity_maps", lambda *_a: None)

        verdict = sb._probe_parent_sequence(4242, read_fd, write_fd, 1000, 1000)

        assert verdict == (True, False, "ok", "")

    def test_newuser_denial_names_newuser_not_newns(self, monkeypatch, pipe_fds):
        """A kernel with no CONFIG_USER_NS fails at the FIRST step."""
        read_fd, write_fd = pipe_fds
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("U", errno.EPERM)))

        ok, transient, reason, _remedy = sb._probe_parent_sequence(4242, read_fd, write_fd, 1000, 1000)

        assert (ok, transient) == (False, False)
        assert "CLONE_NEWUSER" in reason and "CLONE_NEWNS" not in reason, reason

    def test_max_user_namespaces_zero_stays_transient(self, monkeypatch, pipe_fds):
        """``user.max_user_namespaces=0`` denies NEWUSER with ENOSPC.

        ENOSPC is in the pre-existing transient set, so it must NOT be cached as
        a permanent "no sandbox" verdict. The step-aware reason is what makes
        this host distinguishable from an AppArmor denial.
        """
        read_fd, write_fd = pipe_fds
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("U", errno.ENOSPC)))

        ok, transient, reason, _remedy = sb._probe_parent_sequence(4242, read_fd, write_fd, 1000, 1000)

        assert (ok, transient) == (False, True)
        assert "CLONE_NEWUSER" in reason and "ENOSPC" in reason, reason

    def test_map_write_denial_is_permanent_and_names_the_file(self, monkeypatch, pipe_fds):
        read_fd, write_fd = pipe_fds
        failure = ("/proc/<pid>/uid_map write", errno.EPERM)
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("U", 0)))
        monkeypatch.setattr(sb, "_probe_write_identity_maps", lambda *_a: failure)

        ok, transient, reason, _remedy = sb._probe_parent_sequence(4242, read_fd, write_fd, 1000, 1000)

        assert (ok, transient) == (False, False)
        assert "uid_map" in reason and "EPERM" in reason, reason

    def test_vanished_child_map_write_is_transient(self, monkeypatch, pipe_fds):
        """A child that died mid-handshake is a harness failure, not a verdict."""
        read_fd, write_fd = pipe_fds
        failure = ("/proc/<pid>/setgroups write", errno.ESRCH)
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("U", 0)))
        monkeypatch.setattr(sb, "_probe_write_identity_maps", lambda *_a: failure)

        ok, transient, _reason, _remedy = sb._probe_parent_sequence(4242, read_fd, write_fd, 1000, 1000)

        assert (ok, transient) == (False, True)

    def test_silent_child_before_newns_is_transient(self, monkeypatch, pipe_fds):
        read_fd, write_fd = pipe_fds
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("U", 0), None))
        monkeypatch.setattr(sb, "_probe_write_identity_maps", lambda *_a: None)

        ok, transient, reason, _remedy = sb._probe_parent_sequence(4242, read_fd, write_fd, 1000, 1000)

        assert (ok, transient) == (False, True)
        assert "CLONE_NEWNS" in reason, reason

    def test_child_killed_after_maps_is_transient_not_permanent(self, monkeypatch, pipe_fds):
        """EPIPE on the release write means the child died — never cache that.

        Classifying it permanent would poison the backend cache and fail every
        later spawn until restart, which is the incident-2026-07-18 shape.
        """
        read_fd, _unused = pipe_fds
        dead_r, dead_w = os.pipe()
        os.close(dead_r)  # no reader left, so writing to dead_w raises EPIPE
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("U", 0)))
        monkeypatch.setattr(sb, "_probe_write_identity_maps", lambda *_a: None)
        try:
            ok, transient, reason, _remedy = sb._probe_parent_sequence(4242, read_fd, dead_w, 1000, 1000)
        finally:
            os.close(dead_w)

        assert (ok, transient) == (False, True), reason
        assert "EPIPE" in reason, reason

    def test_unexpected_step_label_is_transient(self, monkeypatch, pipe_fds):
        read_fd, write_fd = pipe_fds
        monkeypatch.setattr(sb, "_probe_read_step", _scripted_steps(("X", 0)))

        ok, transient, _reason, _remedy = sb._probe_parent_sequence(4242, read_fd, write_fd, 1000, 1000)

        assert (ok, transient) == (False, True)

    def test_a_multithreaded_child_explains_its_einval_without_reclassifying_it(
        self, monkeypatch, pipe_fds
    ):
        """The verdict is a plain EINVAL's; only the REASON gains the thread count.

        ``unshare(CLONE_NEWUSER)`` implies ``CLONE_THREAD``, so the kernel refuses it
        with EINVAL unless the caller is single-threaded. A ``fork()`` child is, until
        an ``os.register_at_fork`` handler starts a thread inside the fork -- which
        OpenTelemetry's metric exporter does on every child.

        But a kernel built without CONFIG_USER_NS returns EINVAL too, and this child
        never got far enough to tell the two apart, so calling it transient would be
        exactly as wrong as calling it permanent -- and it would additionally withhold
        the ``no_backend`` opt-in (``sandbox_allow_unsandboxed_exec``) from a host that
        really has no user namespaces. Classification and remedy therefore stay
        identical to a plain EINVAL. What the thread count buys is a reader who is not
        sent to check their kernel config.
        """
        read_fd, write_fd = pipe_fds
        monkeypatch.setattr(
            sb, "_probe_read_step", _scripted_steps((sb._PROBE_STEP_MULTITHREADED, 2))
        )
        plain = sb._probe_failure(sb._PROBE_STEP_NEWUSER, errno.EINVAL)

        ok, transient, reason, remedy = sb._probe_parent_sequence(
            4242, read_fd, write_fd, 1000, 1000
        )

        assert (ok, transient, remedy) == (plain[0], plain[1], plain[3]), reason
        assert plain[2] in reason, "the plain EINVAL reason must survive verbatim"
        assert "2 threads" in reason, reason
        assert "register_at_fork" in reason, reason

    def test_a_multithreaded_child_reports_the_count_only_with_an_einval(
        self, monkeypatch, pipe_fds
    ):
        """The unshare still RUNS -- the kernel stays the authority on the verdict."""
        read_fd, write_fd = pipe_fds
        calls: list[int] = []
        codes: list[int] = []
        # A real pipe for the report, and -1 for every other fd: this runs in the
        # pytest worker, not a fork child, so no closer may touch a live descriptor
        # -- and a path that unexpectedly READS one must fail into the child's own
        # `except BaseException` rather than block on the worker's stdin (fd 0).
        monkeypatch.setattr(sb, "_close_probe_fds", lambda *fds: None)
        monkeypatch.setattr(sb, "_close_fd_ranges", lambda ranges: None)
        monkeypatch.setattr(sb, "_probe_child_thread_count", lambda: 3)
        monkeypatch.setattr(
            sb,
            "_probe_child_unshare",
            lambda libc, flags: calls.append(flags) or errno.EINVAL,
        )
        monkeypatch.setattr(sb.os, "_exit", _exit_recorder(codes))

        with pytest.raises(_ChildExit):
            sb._probe_child_sequence(None, -1, write_fd, -1, -1, ())

        # The FIRST exit is the child's verdict. A second follows only because a test
        # cannot really leave the process here: the stand-in raises, and the child's
        # own ``except BaseException: os._exit(1)`` then runs.
        assert codes[0] == 0
        assert calls == [sb._CLONE_NEWUSER]
        assert sb._probe_read_step(read_fd) == (sb._PROBE_STEP_MULTITHREADED, 3)

    def test_a_multithreaded_child_with_another_errno_reports_it_plainly(
        self, monkeypatch, pipe_fds
    ):
        """Only EINVAL is the ambiguous one; EPERM means what it says.

        Routing every failure from a multithreaded child through the M step would bury
        the AppArmor userns denial (EPERM), whose remedy is a real and different fix.
        """
        read_fd, write_fd = pipe_fds
        monkeypatch.setattr(sb, "_close_probe_fds", lambda *fds: None)
        monkeypatch.setattr(sb, "_close_fd_ranges", lambda ranges: None)
        monkeypatch.setattr(sb, "_probe_child_thread_count", lambda: 3)
        monkeypatch.setattr(sb, "_probe_child_unshare", lambda libc, flags: errno.EPERM)
        monkeypatch.setattr(sb.os, "_exit", _exit_recorder([]))

        with pytest.raises(_ChildExit):
            sb._probe_child_sequence(None, -1, write_fd, -1, -1, ())

        assert sb._probe_read_step(read_fd) == ("U", errno.EPERM)

    def test_a_single_threaded_child_reports_its_einval_as_a_plain_U(
        self, monkeypatch, pipe_fds
    ):
        """One thread means the EINVAL really is the kernel's answer about the host."""
        read_fd, write_fd = pipe_fds
        calls: list[int] = []
        monkeypatch.setattr(sb, "_close_probe_fds", lambda *fds: None)
        monkeypatch.setattr(sb, "_close_fd_ranges", lambda ranges: None)
        monkeypatch.setattr(sb, "_probe_child_thread_count", lambda: 1)
        monkeypatch.setattr(
            sb, "_probe_child_unshare", lambda libc, flags: calls.append(flags) or errno.EINVAL
        )
        monkeypatch.setattr(sb.os, "_exit", _exit_recorder([]))

        with pytest.raises(_ChildExit):
            sb._probe_child_sequence(None, -1, write_fd, -1, -1, ())

        assert calls == [sb._CLONE_NEWUSER]
        assert sb._probe_read_step(read_fd) == ("U", errno.EINVAL)

    def test_an_undeterminable_thread_count_reports_the_errno_plainly(
        self, monkeypatch, pipe_fds
    ):
        """``/proc`` unreadable reads as 0, which must not be mistaken for "many"."""
        read_fd, write_fd = pipe_fds
        calls: list[int] = []
        monkeypatch.setattr(sb, "_close_probe_fds", lambda *fds: None)
        monkeypatch.setattr(sb, "_close_fd_ranges", lambda ranges: None)
        monkeypatch.setattr(sb, "_probe_child_thread_count", lambda: 0)
        monkeypatch.setattr(
            sb, "_probe_child_unshare", lambda libc, flags: calls.append(flags) or errno.EINVAL
        )
        monkeypatch.setattr(sb.os, "_exit", _exit_recorder([]))

        with pytest.raises(_ChildExit):
            sb._probe_child_sequence(None, -1, write_fd, -1, -1, ())

        assert calls == [sb._CLONE_NEWUSER]
        assert sb._probe_read_step(read_fd) == ("U", errno.EINVAL)

    def test_the_thread_count_is_the_task_dir_link_count_minus_two(self):
        """One stat, no allocation: the probe child must not touch the allocator.

        ``/proc/<pid>/task`` holds one subdirectory per thread, so its ``st_nlink`` is
        ``2 + threads`` (``.`` and ``..``). Reading it that way keeps the child off
        ``os.listdir``, which allocates a list and a string per task -- and the fd
        sweep is precomputed pre-fork for exactly that reason: another thread may have
        held the allocator lock at fork time and no longer exists to release it.
        """
        expected = len(os.listdir("/proc/self/task"))

        assert sb._probe_child_thread_count() == expected

    def test_an_unreadable_task_dir_reads_as_zero(self, monkeypatch):
        monkeypatch.setattr(
            sb.os, "stat", lambda *a, **k: (_ for _ in ()).throw(OSError("no /proc"))
        )

        assert sb._probe_child_thread_count() == 0


@_linux_only
class TestProbeStepReports:
    """Parsing of the child's ``<step>:<errno>`` pipe report."""

    def test_parses_step_and_errno(self, pipe_fds):
        read_fd, write_fd = pipe_fds
        os.write(write_fd, b"N:1\n")
        assert sb._probe_read_step(read_fd) == ("N", 1)

    def test_closed_pipe_without_report_returns_none(self, pipe_fds):
        read_fd, write_fd = pipe_fds
        os.close(write_fd)
        assert sb._probe_read_step(read_fd) is None

    def test_junk_report_returns_none(self, pipe_fds):
        read_fd, write_fd = pipe_fds
        os.write(write_fd, b"garbage\n")
        assert sb._probe_read_step(read_fd) is None

    def test_high_numbered_fd_does_not_raise(self, pipe_fds):
        """A pipe fd past FD_SETSIZE must still be readable.

        ``select()`` raises ValueError once a descriptor reaches 1024, and a
        long-lived gateway can easily hand the probe such an fd; that exception
        would kill the background warm thread and leave wrap_argv rejecting
        every sandboxed spawn. ``poll()`` has no descriptor ceiling.
        """
        import resource

        read_fd, write_fd = pipe_fds
        high_fd = 1100
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft <= high_fd:
            if hard != resource.RLIM_INFINITY and hard <= high_fd:
                pytest.skip("cannot obtain an fd past FD_SETSIZE under this rlimit")
            resource.setrlimit(resource.RLIMIT_NOFILE, (high_fd + 64, hard))
        try:
            os.dup2(read_fd, high_fd)
        except OSError:  # pragma: no cover - environment-dependent
            pytest.skip("cannot dup to a high descriptor here")
        try:
            os.write(write_fd, b"N:0\n")
            assert sb._probe_read_step(high_fd) == ("N", 0)
        finally:
            os.close(high_fd)
            if soft <= high_fd:
                resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    def test_silent_child_times_out_rather_than_wedging(self, monkeypatch, pipe_fds):
        read_fd, _write_fd = pipe_fds
        monkeypatch.setattr(sb, "_PROBE_HANDSHAKE_TIMEOUT_SECS", 0.01)
        assert sb._probe_read_step(read_fd) is None


class TestProbeScaffolding:
    """Probe setup/teardown: no fd leaks, no zombies, platform guard intact."""

    @_linux_only
    def test_fork_failure_reports_transient_and_closes_fds(self, monkeypatch):
        """A failed fork must still close both handshake pipes.

        Tracks the probe's OWN pipe/close calls rather than counting
        ``/proc/self/fd``: the background warm thread runs its own probe
        concurrently, so a global fd count is racy and even a patched ``os.pipe``
        sees that thread's pipes. Filtering on the calling thread makes the
        assertion deterministic, and comparing created-vs-closed sets avoids
        depending on fd state that a freed number could have had reused.
        """
        caller_thread = threading.get_ident()
        created: list[int] = []
        closed: list[int] = []
        real_pipe, real_close = os.pipe, os.close

        def tracking_pipe():
            pair = real_pipe()
            if threading.get_ident() == caller_thread:
                created.extend(pair)
            return pair

        def tracking_close(fd):
            if threading.get_ident() == caller_thread:
                closed.append(fd)
            return real_close(fd)

        def boom():
            raise OSError(errno.EAGAIN, "resource temporarily unavailable")

        monkeypatch.setattr(sb.os, "pipe", tracking_pipe)
        monkeypatch.setattr(sb.os, "close", tracking_close)
        monkeypatch.setattr(sb.os, "fork", boom)

        ok, transient, reason, _remedy = sb._probe_unshare_via_fork()

        assert (ok, transient) == (False, True)
        assert reason == "fork failed with errno 11 (EAGAIN)"
        assert created, "probe should create its handshake pipes"
        assert set(created) <= set(closed), "probe leaked a pipe fd"

    @_linux_only
    def test_parent_always_reaps_the_child(self, monkeypatch):
        """Every exit path must reap, so a probe can never leak a zombie."""
        reaped: list[int] = []
        monkeypatch.setattr(sb.os, "fork", lambda: 4242)
        monkeypatch.setattr(sb, "_probe_reap", reaped.append)
        monkeypatch.setattr(sb, "_probe_parent_sequence", lambda *_a: (True, False, "ok", ""))

        assert sb._probe_unshare_via_fork() == (True, False, "ok", "")
        assert reaped == [4242]

    @_linux_only
    def test_probe_reaps_even_when_parent_raises(self, monkeypatch):
        reaped: list[int] = []
        monkeypatch.setattr(sb.os, "fork", lambda: 4242)
        monkeypatch.setattr(sb, "_probe_reap", reaped.append)

        def boom(*_a):
            raise RuntimeError("map write blew up")

        monkeypatch.setattr(sb, "_probe_parent_sequence", boom)

        with pytest.raises(RuntimeError):
            sb._probe_unshare_via_fork()
        assert reaped == [4242]

    def test_non_linux_never_probes(self, monkeypatch):
        """``_probe_unshare`` keeps its non-Linux early return (no fork off Linux)."""
        monkeypatch.setattr(sb.sys, "platform", "darwin")
        monkeypatch.setattr(sb, "_backend", None)
        monkeypatch.setattr(
            sb, "_probe_unshare_once", lambda: pytest.fail("probed on a non-Linux host")
        )

        assert sb._probe_unshare() is False
        assert sb._last_unshare_failure == (False, "not Linux", "")


def _fd_open(fd: int) -> bool:
    """Whether *fd* refers to an open descriptor in THIS process."""
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


class TestProbeChildFdSweep:
    """The probe child must drop inherited descriptors before its first unshare.

    Regression cover for #3150: ``fork()`` copies every open descriptor — the
    ``gateway.lock`` flock fd and the dashboard listen socket included — and the
    probe child never execs, so ``O_CLOEXEC`` never fires. A child orphaned by
    its parent's death (gateway OOM-killed between fork and reap) used to keep
    the lock fd open and pin the data home.

    The range-arithmetic tests are platform-neutral; only the two tests that
    actually ``fork()`` carry the Linux gate.
    """

    def test_sweep_ranges_skip_exactly_the_keep_set(self):
        """The precomputed spans cover everything below the limit but keep."""
        ranges = sb._fd_sweep_ranges(frozenset({0, 1, 2, 5, 7}), limit=10)

        assert ranges == ((3, 5), (6, 7), (8, 10))
        covered = {fd for lo, hi in ranges for fd in range(lo, hi)}
        assert covered == set(range(10)) - {0, 1, 2, 5, 7}

    def test_sweep_ignores_keep_fds_at_or_above_the_limit(self):
        """A keep fd beyond the sweep bound must not truncate the sweep below it."""
        assert sb._fd_sweep_ranges(frozenset({0, 1, 2, 5000, -1}), limit=10) == ((3, 10),)

    def test_sweep_trusts_a_large_open_max(self, monkeypatch):
        """A big SC_OPEN_MAX is the bound, not clamped: high lock fds must close.

        ``os.closerange`` is one ``close_range(2)`` syscall on Linux >= 5.9, so
        a wide span is cheap — and silently clamping it would leave an fd at,
        say, 60000 open on a ``LimitNOFILE=65536`` host with no diagnostic.
        ``raising=False`` because ``os.sysconf`` does not exist on Windows.
        """
        monkeypatch.setattr(sb.os, "sysconf", lambda _name: 65536, raising=False)

        assert sb._fd_sweep_ranges(frozenset({0, 1, 2})) == ((3, 65536),)

    def test_sweep_falls_back_when_sysconf_is_unhelpful(self, monkeypatch):
        """Unreadable, nonsense, or absent SC_OPEN_MAX gets the fallback cap.

        The absent case is real: ``os.sysconf`` does not exist off-POSIX, and
        the helper's never-raises contract must hold everywhere it can run.
        """

        def unavailable(_name):
            raise ValueError("unrecognized configuration name")

        monkeypatch.setattr(sb.os, "sysconf", unavailable, raising=False)
        first = sb._fd_sweep_ranges(frozenset({0, 1, 2}))

        monkeypatch.setattr(sb.os, "sysconf", lambda _name: -1, raising=False)
        second = sb._fd_sweep_ranges(frozenset({0, 1, 2}))

        monkeypatch.delattr(sb.os, "sysconf", raising=False)
        third = sb._fd_sweep_ranges(frozenset({0, 1, 2}))

        assert first == second == third == ((3, sb._PROBE_CHILD_FD_SWEEP_CAP),)

    def test_close_fd_ranges_replays_exactly_the_given_spans(self, monkeypatch):
        """The child half only replays the precomputed spans, in order."""
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(sb.os, "closerange", lambda lo, hi: calls.append((lo, hi)))

        sb._close_fd_ranges(((3, 5), (8, 20)))

        assert calls == [(3, 5), (8, 20)]

    @_linux_only
    def test_child_sweeps_before_first_unshare_and_keeps_handshake_ends(self, monkeypatch):
        """``_probe_child_sequence`` runs the sweep BEFORE touching namespaces.

        Order matters: a sweep after the first unshare leaves the window where
        an orphaned child parked in the handshake still holds the lock fd. The
        sweep must replay exactly the ranges the parent precomputed — dropping
        either handshake end deadlocks the parent's read.
        """
        calls: list[tuple[str, object]] = []
        monkeypatch.setattr(
            sb,
            "_close_fd_ranges",
            lambda ranges: calls.append(("sweep", tuple(ranges))),
        )
        # This harness runs the child sequence IN the pytest worker, which has many
        # threads; a real fork child has one. Pin the count so the child takes the
        # probing path instead of reporting itself multithreaded and exiting.
        monkeypatch.setattr(sb, "_probe_child_thread_count", lambda: 1)

        def fake_unshare(_libc, flags):
            calls.append(("unshare", flags))
            return 0

        class _ChildExit(BaseException):
            """Raised by the patched ``os._exit`` so nothing runs past an exit."""

        exits: list[int] = []
        test_pid = os.getpid()
        real_exit = os._exit  # captured pre-patch; the guard must not recurse

        def fake_exit(code):
            if os.getpid() != test_pid:  # a real fork must still exit for real
                real_exit(code)  # pragma: no cover - only on an escaped fork
            exits.append(code)
            raise _ChildExit(code)

        monkeypatch.setattr(sb, "_probe_child_unshare", fake_unshare)
        monkeypatch.setattr(sb.os, "_exit", fake_exit)

        c2p_r, c2p_w = os.pipe()
        p2c_r, p2c_w = os.pipe()
        # The child closes the parent's ends (c2p_r, p2c_w); in production the
        # parent process still holds them. Without a surviving reader for c2p
        # in this single-process harness, the child's report write gets EPIPE.
        parent_c2p_r = os.dup(c2p_r)
        sweep = ((3, c2p_w), (c2p_w + 1, 64),)
        try:
            os.write(p2c_w, b"x")  # pre-release the maps handshake
            with pytest.raises(_ChildExit):
                sb._probe_child_sequence(None, c2p_r, c2p_w, p2c_r, p2c_w, sweep)
        finally:
            # _probe_child_sequence already closed c2p_r and p2c_w as its first
            # statement; re-closing them here could tear down an unrelated fd
            # that was allocated into the freed numbers meanwhile.
            for fd in (c2p_w, p2c_r, parent_c2p_r):
                try:
                    os.close(fd)
                except OSError:
                    pass

        # The success path exits 0; a failure after the second unshare would
        # first record a nonzero exit before the BaseException handler re-exits,
        # so the FIRST recorded code is the real verdict.
        assert exits[0] == 0
        assert calls[0] == ("sweep", sweep)
        assert calls[1] == ("unshare", sb._CLONE_NEWUSER)
        assert ("unshare", sb._CLONE_NEWNS) in calls

    @_linux_only
    def test_forked_child_really_drops_a_lock_shaped_fd(self, tmp_path):
        """End to end: a fd held open in the parent is closed in the swept child.

        Mirrors the leak shape exactly — a plain ``os.open`` on a lock file,
        inherited across ``fork()`` — and asserts the sweep drops it while the
        report pipe named in the keep-set survives to carry the verdict out.
        The ranges are precomputed pre-fork, as production does.
        """
        sentinel = os.open(str(tmp_path / "gateway.lock"), os.O_RDWR | os.O_CREAT)
        report_r, report_w = os.pipe()
        sweep = sb._fd_sweep_ranges(frozenset({0, 1, 2, report_w}))
        pid = os.fork()
        if pid == 0:  # pragma: no cover - forked child, exits below
            try:
                sb._close_fd_ranges(sweep)
                payload = (b"1" if _fd_open(sentinel) else b"0") + (
                    b"1" if _fd_open(report_w) else b"0"
                )
                os.write(report_w, payload)
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(report_w)
        try:
            data = os.read(report_r, 2)
        finally:
            os.close(report_r)
            os.close(sentinel)
            _, status = os.waitpid(pid, 0)

        assert os.waitstatus_to_exitcode(status) == 0
        assert data == b"01", "sentinel lock fd must close; kept report pipe must survive"


def _forked_child_thread_count() -> int:
    """Thread count as observed by a fresh ``os.fork()`` child of THIS process.

    ``st_nlink`` of ``/proc/self/task`` is ``2 + threads``, so the child does no
    imports and no I/O beyond one ``stat`` and one pipe write -- the same
    fork-and-count pattern as ``TestProbeRunsInAFreshProcess._EXPERIMENT``'s
    ``forked()`` helper, which inlines it because the experiment runs under
    ``-I -S`` in a disposable interpreter and cannot import this module. Reads 1
    unless an ``os.register_at_fork(after_in_child=...)`` hook armed earlier in
    this process starts a thread inside every child; -1 when the child could not
    report.
    """
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.write(w, str(max(1, os.stat("/proc/self/task").st_nlink - 2)).encode())
            os.close(w)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(w)
    try:
        data = os.read(r, 8)
    finally:
        os.close(r)
        os.waitpid(pid, 0)
    return int(data or -1)


@pytest.mark.skipif(sys.platform != "linux", reason="the userns probe is Linux-only")
class TestProbeRunsInAFreshProcess:
    """The probe child must not inherit the caller's fork hooks.

    ``unshare(CLONE_NEWUSER)`` implies ``CLONE_THREAD``, so the kernel returns
    EINVAL unless the caller's thread group holds exactly one task. An
    ``os.register_at_fork(after_in_child=...)`` handler runs INSIDE ``os.fork()``,
    so a dependency that restarts a thread in every child -- OpenTelemetry's
    metric reader does -- makes a forked probe child multithreaded before it can
    measure anything. That EINVAL is classified permanent and cached, and every
    later sandboxed spawn in the process then fails closed: one such probe cost a
    release gate 40 tests, none of them a metrics test.
    """

    #: The experiment, run in a DISPOSABLE interpreter. Arming the hook is the point
    #: of the test and CPython cannot unregister one, so doing it in the pytest worker
    #: would leave every later fork in that worker starting an unrequested thread --
    #: the exact side effect this fix exists to remove. The child takes the hook with
    #: it when it exits. Prints three counts: forked-before, forked-after, spawned.
    #: Its ``forked()`` inlines ``_forked_child_thread_count``'s fork-and-count
    #: pattern: under ``-I -S`` there is no site directory, so it cannot import
    #: this module.
    _EXPERIMENT = r"""
import os, subprocess, sys, threading, time

def forked():
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.write(w, str(max(1, os.stat("/proc/self/task").st_nlink - 2)).encode())
        os.close(w)
        os._exit(0)
    os.close(w)
    n = int(os.read(r, 8) or -1)
    os.close(r)
    os.waitpid(pid, 0)
    return n

before = forked()
os.register_at_fork(after_in_child=lambda: threading.Thread(
    target=time.sleep, args=(30,), daemon=True).start())
after = forked()
code = "import os;print(max(1, os.stat('/proc/self/task').st_nlink - 2))"
spawned = int(subprocess.run([sys.executable, "-I", "-S", "-c", code],
                             capture_output=True, text=True, check=True).stdout.strip())
print(before, after, spawned)
"""

    def test_a_fork_hook_cannot_reach_the_spawned_child(self):
        """The property the fix rests on, measured rather than argued.

        Asserted as a DIFFERENCE within one process: a bare "the spawned child has
        one thread" would pass just as well on a host where nothing armed a hook,
        which is every host until something does.
        """
        out = subprocess.run(
            [sys.executable, "-I", "-S", "-c", self._EXPERIMENT],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        before, after, spawned = (int(part) for part in out.split())

        assert before == 1, f"the experiment started with a hook already armed ({before})"
        assert after > 1, "a forked child does not inherit the fork hook; premise gone"
        assert spawned == 1, "a freshly spawned interpreter must be single-threaded"

    def test_both_paths_report_the_same_verdict_on_this_host(self):
        """Spawn and fork must agree, or the fix would be changing the answer.

        Only the reason text is compared for its leading step+errno: the spawned
        path's transient failure strings name a Popen-owned child differently, and
        that difference is not a verdict.

        The comparison only means something while the fork child that PRODUCED the
        verdict was single-threaded. An ``os.register_at_fork`` hook armed earlier
        in the same worker (test ordering under xdist decides this) starts a thread
        inside every child, and the fork path then reports the deliberate
        ``_PROBE_STEP_MULTITHREADED`` collapse instead of the kernel's verdict --
        an unknown reading, so it is skipped, not compared (see
        docs/system-specs/common/testing-conventions.md; the collapse itself is
        issue #4219's open decision).

        Two guards, because the hook-started thread is SHORT-LIVED and each fork
        races it independently: the `_forked_child_thread_count` pre-check is
        cheap and catches the steady state, but a clean pre-check child does not
        prove the verdict child was clean. The decisive check therefore reads the
        collapse off the verdict tuple itself, after the probe ran.
        """
        child_threads = _forked_child_thread_count()
        if child_threads < 0:
            pytest.fail(
                "the fork-child thread probe could not report: the forked child "
                "died before writing its /proc/self/task count -- the helper is "
                "broken on this host, which says nothing about fork hooks"
            )
        if child_threads > 1:
            pytest.skip(
                "the fork path cannot reach the kernel's verdict on this worker: a "
                f"fresh fork child reports {child_threads} thread(s) -- an "
                "os.register_at_fork hook armed earlier in this worker starts a "
                "thread inside every child, so there is no comparable verdict "
                "(the collapse is deliberate; see issue #4219)"
            )
        spawned = sb._probe_unshare_via_spawn()
        assert spawned is not None, "this host can spawn an interpreter"
        forked = sb._probe_unshare_via_fork()
        if sb._probe_reason_is_multithreaded_collapse(forked[2]):
            pytest.skip(
                "the fork child that produced the verdict came up multithreaded "
                "despite a clean pre-check: the at-fork-hook thread is short-lived "
                "and each fork races it independently, so the kernel's verdict is "
                "unobtainable from this worker's fork children (the collapse is "
                "deliberate; see issue #4219)"
            )
        assert spawned[0] == forked[0], (spawned, forked)
        assert spawned[1] == forked[1], (spawned, forked)
        assert spawned[3] == forked[3], (spawned, forked)

    def test_verdict_comparison_skips_when_fork_children_start_threaded(self, monkeypatch):
        """A hook-started thread means SKIP -- never a false red on an unlucky shard."""
        monkeypatch.setattr(
            sys.modules[__name__], "_forked_child_thread_count", lambda: 2
        )
        probed = {"n": 0}

        def _count_probe():
            probed["n"] += 1
            return None

        monkeypatch.setattr(sb, "_probe_unshare_via_spawn", _count_probe)
        with pytest.raises(pytest.skip.Exception):
            self.test_both_paths_report_the_same_verdict_on_this_host()
        assert probed["n"] == 0, "the guard must skip BEFORE probing anything"

    def test_verdict_comparison_skips_when_the_verdict_child_itself_collapsed(
        self, monkeypatch
    ):
        """A clean pre-check does not clear the verdict child -- each fork races.

        The at-fork-hook thread is short-lived, so the `_forked_child_thread_count`
        pre-check child and `_probe_unshare_via_fork`'s verdict child can disagree:
        pre-check counts 1, verdict child still comes up multithreaded and reports
        the collapse. That reading is unknown, never a red -- the skip must be
        decided off the verdict tuple itself.
        """
        monkeypatch.setattr(
            sys.modules[__name__], "_forked_child_thread_count", lambda: 1
        )
        monkeypatch.setattr(
            sb, "_probe_unshare_via_spawn",
            lambda: (False, False, "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)", "apparmor_userns"),
        )
        collapse = (
            False,
            False,
            "unshare(CLONE_NEWUSER) failed with errno 22 (EINVAL); the probe child "
            f"had 2 threads, which alone makes it return EINVAL (CLONE_NEWUSER "
            f"implies CLONE_THREAD) -- {sb._PROBE_MULTITHREADED_REASON}",
            "no_user_ns",
        )
        monkeypatch.setattr(sb, "_probe_unshare_via_fork", lambda: collapse)
        with pytest.raises(pytest.skip.Exception):
            self.test_both_paths_report_the_same_verdict_on_this_host()

    def test_verdict_comparison_still_fails_on_a_real_disagreement(self, monkeypatch):
        """The guard must not swallow a genuine disagreement on a clean shard.

        Each case differs from the spawn tuple in exactly ONE compared field, so
        the raise can only originate from that field's assertion -- a stub that
        differs in several fields at once would stay green even if all but one
        of the comparisons were deleted.
        """
        monkeypatch.setattr(
            sys.modules[__name__], "_forked_child_thread_count", lambda: 1
        )
        monkeypatch.setattr(
            sb, "_probe_unshare_via_spawn", lambda: (True, False, "ok", "")
        )
        for forked_stub in (
            (False, False, "ok", ""),  # differs only at [0]: availability verdict
            (True, True, "ok", ""),  # differs only at [1]: transient flag
            (True, False, "ok", "no_user_ns"),  # differs only at [3]: remedy token
        ):
            monkeypatch.setattr(sb, "_probe_unshare_via_fork", lambda s=forked_stub: s)
            with pytest.raises(AssertionError):
                self.test_both_paths_report_the_same_verdict_on_this_host()

    def test_verdict_comparison_fails_when_the_probe_cannot_report(self, monkeypatch):
        """A helper that cannot report is a broken helper, never a hook skip."""
        monkeypatch.setattr(
            sys.modules[__name__], "_forked_child_thread_count", lambda: -1
        )
        with pytest.raises(pytest.fail.Exception):
            try:
                self.test_both_paths_report_the_same_verdict_on_this_host()
            except pytest.skip.Exception as exc:
                # Skipped is a SIBLING of Failed, so it would escape the raises
                # block and mark this very test SKIPPED -- reporting the exact
                # regression it exists to catch as a green run.
                raise AssertionError(
                    "a probe that cannot report must fail, not skip"
                ) from exc

    def test_no_executable_falls_back_instead_of_inventing_a_verdict(self, monkeypatch):
        """An interpreter we cannot start says nothing about the host's namespaces."""
        monkeypatch.setattr(sb.sys, "executable", "")
        monkeypatch.setattr(sb, "_probe_spawn_unavailable_logged", False)
        assert sb._probe_unshare_via_spawn() is None

        monkeypatch.setattr(
            sb, "_probe_unshare_via_fork", lambda: (True, False, "fork path ran", "")
        )
        assert sb._probe_unshare_once() == (True, False, "fork path ran", "")

    def test_the_shim_imports_nothing_first_party(self):
        """It runs under ``-I -S``: no site directory, so a kiro_crew import would fail."""
        assert "kiro_crew" not in sb._PROBE_SHIM_CODE
        for line in sb._PROBE_SHIM_CODE.splitlines():
            if line.startswith(("import ", "from ")):
                assert "kiro_crew" not in line, line
