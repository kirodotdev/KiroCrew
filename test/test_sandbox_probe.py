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

        ok, transient, reason, _remedy = sb._probe_unshare_once()

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

        assert sb._probe_unshare_once() == (True, False, "ok", "")
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
            sb._probe_unshare_once()
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
