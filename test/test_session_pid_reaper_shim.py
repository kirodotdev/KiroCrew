"""The orphan-reaper kill paths route through the platform_compat shim.

The sweep entry point (:func:`kiro_crew.session_pid.kill_orphan_mcps`) is
gated ``IS_WINDOWS``, and the daemon it reaps is a POSIX ``start_new_session``
process, so none of these sites can fire on Windows today. They still use the
raw spellings (``os.kill``, ``signal.SIGKILL``) whose Windows failure modes
the repo documents in the cross-platform table, while sibling functions in
the same file already route through the shim ("correct on its own terms
rather than depending on a caller's early-out" — the browser-daemon probe's
own rationale).

These tests pin that routing per function, by asserting on the shim spies for
the direct-kill paths (the probe and the pid-targeted signals). The
group-targeted calls (``os.getpgid`` / ``os.killpg``) stay raw on purpose:
there is no shim spelling for a process-group signal, and the isolated-leader
predicate that guards them is POSIX group semantics.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import kiro_crew.session_pid as sp


def _gatewayd_cmdline(sock) -> bytes:  # type: ignore[no-untyped-def]
    return f"python\x00-m\x00kiro_crew.mcp_gateway.gatewayd\x00--unix={sock}".encode()


class TestGatewaydKillRoutesThroughShim:
    """_kill_orphan_gatewayd signals the pid via kill_pid, probes via pid_exists."""

    def test_term_is_sent_through_kill_pid(self) -> None:
        sent: list[tuple[int, int]] = []

        def fake_kill_pid(pid: int, sig: int) -> bool:
            sent.append((pid, sig))
            return True

        with (
            patch.object(sp.platform_compat, "kill_pid", side_effect=fake_kill_pid),
            # The daemon exits before the first liveness poll: TERM was
            # delivered, then the probe reports it gone.
            patch.object(sp.platform_compat, "pid_exists", return_value=False),
        ):
            assert sp._kill_orphan_gatewayd(900, _gatewayd_cmdline("gone.sock")) == 1
        assert (900, sp.platform_compat.SIGTERM) in sent

    def test_escalation_sigkill_goes_through_kill_pid(self) -> None:
        """Wedged daemon: TERM delivered, pid still exists past grace, shared
        pgid (not an isolated leader) — the direct SIGKILL branch fires."""
        sent: list[tuple[int, int]] = []

        def fake_kill_pid(pid: int, sig: int) -> bool:
            sent.append((pid, sig))
            return True

        for pid in (901, 903):
            with (
                patch.object(sp.platform_compat, "kill_pid", side_effect=fake_kill_pid),
                patch.object(sp.platform_compat, "pid_exists", return_value=True),
                patch.object(sp, "_GATEWAYD_TERM_GRACE_SECONDS", 0),
                # Linux path: identity recheck reads /proc/<pid>/cmdline, which
                # must still match (not a recycled PID) so the kill proceeds.
                patch.object(Path, "read_bytes", return_value=_gatewayd_cmdline("gone.sock")),
                patch("kiro_crew.session_pid.sys") as mock_sys,
                # Windows' frozen os has no getpgid/getpgrp to patch by name, so
                # create them: shared group (pgid == our pgrp) takes the direct
                # SIGKILL branch instead of killpg.
                patch.object(sp.os, "getpgid", create=True, return_value=1000),
                patch.object(sp.os, "getpgrp", create=True, return_value=1000),
            ):
                mock_sys.platform = "linux"
                assert sp._kill_orphan_gatewayd(pid, _gatewayd_cmdline("gone.sock")) == 1
        assert (901, sp.platform_compat.SIGKILL) in sent
        assert (903, sp.platform_compat.SIGKILL) in sent

    def test_liveness_probe_uses_pid_exists_not_signal_zero(self) -> None:
        """The grace loop probes through pid_exists; os.kill(pid, 0) is never
        consulted — on Windows that spelling terminates the target."""
        with (
            patch.object(sp.platform_compat, "pid_exists", return_value=False) as pe,
            patch.object(sp.platform_compat, "kill_pid", return_value=True),
            patch("kiro_crew.session_pid.os.kill") as raw_kill,
        ):
            assert sp._kill_orphan_gatewayd(902, _gatewayd_cmdline("gone.sock")) == 1
        pe.assert_called_once_with(902)
        raw_kill.assert_not_called()


class TestSweepDirectKillRoutesThroughShim:
    """kill_orphan_mcps' pid-targeted SIGKILL goes through kill_pid.

    The isolated-leader branch (killpg) is group semantics with no shim
    spelling, and its tests already exist in test_pid_lifecycle.py; this
    pins only the pid-targeted branch. The sweep gate itself
    (``IS_WINDOWS -> 0``) is pinned in test_windows_kill_probe_audit.py.
    """

    def test_shared_pgid_orphan_killed_via_kill_pid(self) -> None:
        with (
            patch("kiro_crew.session_pid.platform_compat") as pc,
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            pc.IS_WINDOWS = False
            pc.kill_pid.return_value = True
            # Same int value as signal.SIGKILL on POSIX; asserting with the
            # shim constant keeps the test tied to whatever the module sends.
            mock_sys.platform = "linux"
            # os.getpgrp/getpgid do not exist on Windows and its frozen os
            # module cannot be patched by name, so create the attributes the
            # sweep consults: our own group (1000) differs from the orphan's
            # group (the pid itself) -> shared-pgid? No: pgid==pid means the
            # orphan IS an isolated leader, which routes to killpg. To reach
            # the direct-kill branch the orphan must share OUR group.
            with (
                patch.object(sp.os, "getpgrp", create=True, return_value=1000),
                patch.object(sp.os, "getpgid", create=True, return_value=1000),
            ):
                killed = sp.kill_orphan_mcps([600])
        assert killed == 1
        pc.kill_pid.assert_called_once_with(600, pc.SIGKILL)
        pc.killpg.assert_not_called()
