"""Tests for the sandbox PID-namespace isolation (kill(-1) broadcast guard).

Background (2026-07-15 incident): a unit test's mocked Popen leaked a
MagicMock pid into ``os.killpg(os.getpgid(proc.pid), SIGKILL)``. The mock
coerced to 1, and ``killpg(1, sig)`` is ``kill(-1, sig)`` in libc — a
broadcast SIGKILL to every process the uid owns, which repeatedly wiped the
entire login session (systemd --user manager, SSH sessions, live gateway).

The structural fix: sandboxed subprocesses run inside a Linux PID namespace,
where ``kill(-1)``/``killpg`` can only reach namespace-local processes. The
gateway and everything else outside are unreachable by construction.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.sandbox import (
    _build_launcher_script,
    namespace_argv,
    wrap_argv,
)

_requires_linux_launcher = pytest.mark.skipif(
    sys.platform != "linux",
    reason="PID namespace launcher requires Linux (unshare via libc)",
)

pytestmark = pytest.mark.xdist_group("sandbox_pidns_e2e")


def _pidns_available() -> bool:
    """True when unprivileged user+pid namespaces work on this host."""
    if sys.platform != "linux":
        return False
    try:
        r = subprocess.run(
            ["unshare", "--user", "--pid", "--fork", "true"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


_requires_pidns = pytest.mark.skipif(
    not _pidns_available(),
    reason="unprivileged user+pid namespaces unavailable on this host",
)


class TestLauncherTemplate:
    """Template-level assertions — portable, no subprocess."""

    def test_pid_namespace_on_by_default(self):
        script = _build_launcher_script("cc")
        assert "PID_NAMESPACE = True" in script
        assert "_CLONE_NEWPID" in script

    def test_pid_namespace_can_be_disabled(self):
        script = _build_launcher_script("cc", pid_namespace=False)
        assert "PID_NAMESPACE = False" in script

    def test_template_contains_mini_init_pieces(self):
        script = _build_launcher_script("cc")
        # unshare(NEWPID) → fork → ns PID 1 mini-init → /proc remount
        assert "unshare(_CLONE_NEWPID)" in script
        assert 'mount(b"proc", b"/proc", b"proc"' in script
        # graceful degradation path when the kernel refuses NEWPID
        assert "continuing WITHOUT pid isolation" in script

    def test_template_compiles(self):
        script = _build_launcher_script("cc")
        compile(script, "<launcher>", "exec")  # SyntaxError would raise


class TestFlagPlumbing:
    """pid_namespace flows wrap_argv → namespace_argv → launcher script."""

    @_requires_linux_launcher
    def test_wrap_argv_explicit_false_disables(self):
        argv, cleanup = wrap_argv(
            [sys.executable, "-c", "pass"], mode="cc", pid_namespace=False
        )
        try:
            script = Path(argv[1]).read_text()
            assert "PID_NAMESPACE = False" in script
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)

    @_requires_linux_launcher
    def test_namespace_argv_default_enables(self):
        argv = namespace_argv([sys.executable, "-c", "pass"], "cc")
        script = Path(argv[1]).read_text()
        Path(argv[1]).unlink(missing_ok=True)
        assert "PID_NAMESPACE = True" in script


class TestPidNamespaceIsolation:
    """Live end-to-end: real namespaces, real signals. Linux + userns only."""

    @_requires_pidns
    def test_broadcast_cannot_see_outside(self):
        """kill(-1, 0) inside the sandbox must raise ESRCH — no visible targets."""
        probe = (
            "import os\n"
            "try:\n"
            "    os.kill(-1, 0)\n"
            "    print('LEAK')\n"
            "except ProcessLookupError:\n"
            "    print('ISOLATED')\n"
        )
        argv, cleanup = wrap_argv([sys.executable, "-c", probe], mode="cc")
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)
        assert "ISOLATED" in r.stdout
        assert "LEAK" not in r.stdout

    @_requires_pidns
    def test_payload_runs_as_low_ns_pid(self):
        """Payload sees a namespace-local pid (tiny number), proving the ns."""
        argv, cleanup = wrap_argv(
            [sys.executable, "-c", "import os; print('NSPID', os.getpid())"],
            mode="cc",
        )
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)
        line = next(ln for ln in r.stdout.splitlines() if ln.startswith("NSPID"))
        assert int(line.split()[1]) < 10

    @_requires_pidns
    def test_incident_replay_is_contained(self):
        """Replaying the exact 2026-07-15 killer call harms nothing outside.

        Inside the ns, kill(-1) excludes the caller and cannot signal the
        protected ns PID 1, so the broadcast finds no target at all.
        """
        probe = (
            "import os, signal\n"
            "try:\n"
            "    os.killpg(1, signal.SIGKILL)\n"
            "    print('BROADCAST-SENT')\n"
            "except (ProcessLookupError, PermissionError) as e:\n"
            "    print('CONTAINED', type(e).__name__)\n"
        )
        argv, cleanup = wrap_argv([sys.executable, "-c", probe], mode="cc")
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)
        # Either the broadcast found no targets (CONTAINED) or it only reached
        # ns-local processes; in both cases WE are alive to assert this line,
        # which is the actual guarantee under test.
        assert "CONTAINED" in r.stdout or r.returncode != 0

    @_requires_pidns
    def test_exit_code_propagates_through_mini_init(self):
        argv, cleanup = wrap_argv(
            [sys.executable, "-c", "import sys; sys.exit(42)"], mode="cc"
        )
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)
        assert r.returncode == 42

    @_requires_pidns
    def test_disabled_flag_skips_pid_namespace(self):
        """pid_namespace=False keeps legacy behavior — real host pid visible."""
        argv, cleanup = wrap_argv(
            [sys.executable, "-c", "import os; print('PID', os.getpid())"],
            mode="cc",
            pid_namespace=False,
        )
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)
        line = next(ln for ln in r.stdout.splitlines() if ln.startswith("PID"))
        assert int(line.split()[1]) > 100  # real host pid, not ns-local
