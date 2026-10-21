"""Tests for the sweeps that find orphans in the process table.

Untracked orphan MCP servers, marked launchers, orphaned work processes, and
the report-only check for an untracked managed-agent runtime: each is found by
scanning processes, not by reading a tracking file.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from non_utf8_comm import BAD_COMM, write_stat
from pid_lifecycle_helpers import (  # noqa: F401 -- fixtures are requested by name
    _POSIX_ONLY,
    pid_file_fixture,
    session_pid_file_fixture,
)


@_POSIX_ONLY
class TestKillOrphanMcps:
    """Tests for kill_orphan_mcps (kill confirmed orphans)."""

    @pytest.fixture(autouse=True)
    def _stable_root_identity(self) -> Iterator[None]:
        """Give synthetic PIDs a start token so the recycle guard passes.

        `kill_orphan_mcps` captures the root's `_pid_start_token` before the
        subtree scan and re-confirms it before signalling, so a PID whose
        identity cannot be read is skipped by design. These tests use synthetic
        PIDs that have no `/proc` entry; a stable token states the thing they
        already assume -- that the PID was not recycled mid-sweep. See
        test_orphan_mcp_subtree.TestRootRecycleGuard for the guard's own cover.
        """
        with patch("kiro_crew.session_pid._pid_start_token", return_value="tok-stable"):
            yield

    def test_uses_killpg_when_pgid_differs(self) -> None:
        """If orphan is its own group leader, kill via killpg."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=500),
            patch("os.killpg") as mock_killpg,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([500])

        assert killed == 1
        mock_killpg.assert_called_once_with(500, signal.SIGKILL)

    def test_falls_back_to_direct_kill_when_pgid_matches(self) -> None:
        """If orphan shares our pgid, use direct os.kill (not _kill_pid_tree)."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=1000),
            patch("os.kill") as mock_kill,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([600])

        assert killed == 1
        mock_kill.assert_called_once_with(600, signal.SIGKILL)

    def test_direct_kill_handles_already_dead(self) -> None:
        """ProcessLookupError on direct kill is handled gracefully."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=1000),
            patch("os.kill", side_effect=ProcessLookupError),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([600])

        assert killed == 0

    def test_respects_max_kill_cap(self) -> None:
        """Never kills more than _ORPHAN_SWEEP_MAX_KILLS in one pass."""
        from kiro_crew.session_pid import _ORPHAN_SWEEP_MAX_KILLS, kill_orphan_mcps

        pids = list(range(1000, 1000 + _ORPHAN_SWEEP_MAX_KILLS + 10))
        with (
            patch("os.getpgrp", return_value=1),
            patch("os.getpgid", side_effect=lambda pid: pid),
            patch("os.killpg"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"python3\x00kirocrew_sandbox_x.py"),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps(pids)

        assert killed == _ORPHAN_SWEEP_MAX_KILLS

    def test_handles_already_dead_process(self) -> None:
        """ProcessLookupError during kill is silently handled."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", side_effect=ProcessLookupError),
        ):
            killed = kill_orphan_mcps([700])

        assert killed == 0

    def test_skips_recycled_pid_on_reverify(self) -> None:
        """If cmdline stops matching at kill time, the PID is skipped (TOCTOU)."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"/usr/bin/bash\x00script.sh"),
            patch("os.killpg") as mock_killpg,
            patch("os.kill") as mock_kill,
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([800])

        assert killed == 0
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()

    def test_macos_subprocess_error_does_not_abort_loop(self) -> None:
        """A vanished PID raising SubprocessError on macOS must not abort
        kills for subsequent PIDs (regression: review-bot rev4).

        `ps` exits non-zero for a PID that died between find and kill, raising
        subprocess.CalledProcessError (a SubprocessError, NOT an OSError). The
        except tuple must catch it so the loop continues to the next PID.
        """
        from kiro_crew.session_pid import kill_orphan_mcps

        def mock_check_output(cmd, **kwargs):
            # cmd[-1] is the str(pid) being re-verified
            if cmd[-1] == "700":
                raise subprocess.CalledProcessError(1, cmd)
            return b"python3 /tmp/kirocrew_sandbox_x.py"

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", side_effect=lambda p: p),
            patch("os.killpg") as mock_killpg,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("subprocess.check_output", side_effect=mock_check_output),
        ):
            mock_sys.platform = "darwin"
            killed = kill_orphan_mcps([700, 701])

        # 700 vanished (SubprocessError, skipped); 701 still killed.
        assert killed == 1
        mock_killpg.assert_called_once_with(701, signal.SIGKILL)


class TestParseEtime:
    """Tests for _parse_etime (ps etime format parser)."""

    def test_minutes_seconds(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("05:30") == 330.0

    def test_hours_minutes_seconds(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("01:05:30") == 3930.0

    def test_days_hours_minutes_seconds(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("2-01:00:00") == 2 * 86400 + 3600

    def test_invalid_returns_zero(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("garbage") == 0.0

    def test_empty_returns_zero(self) -> None:
        from kiro_crew.session_pid import _parse_etime

        assert _parse_etime("") == 0.0


@_POSIX_ONLY
class TestOurOrphanPids:
    """Direct tests for _our_orphan_pids (Linux /proc and macOS ps branches)."""

    def test_linux_proc_scan_finds_init_and_subreaper_children(self, tmp_path: Path) -> None:
        """Linux /proc two-pass scan: includes ppid==1 and ppid==systemd subreaper.

        Exercises the real Linux branch (systemd --user subreaper detection in
        pass 1 + the parent read in pass 2), not the macOS ps path, over a
        fixture table whose stat and comm files are real. Pid 600's name is a
        multibyte one cut at the kernel's 15 bytes, so it is not UTF-8; its
        parent is read from stat bytes, so that orphan is listed like the rest.
        """
        from kiro_crew.session_pid import _our_orphan_pids

        my_uid = 1000
        rows = {  # pid -> (uid, comm, ppid)
            100: (my_uid, b"python3", 1),  # init-reparented
            200: (my_uid, b"bash", 50),  # live child, excluded
            300: (my_uid, b"systemd", 1),  # --user subreaper
            400: (my_uid, b"worker", 300),  # child of subreaper
            500: (9999, b"python3", 1),  # other uid, excluded
            600: (my_uid, "run_データ処理.py".encode()[:15], 1),  # name not UTF-8
        }
        for pid, (_uid, comm, ppid) in rows.items():
            (tmp_path / str(pid)).mkdir()
            (tmp_path / str(pid) / "comm").write_bytes(comm + b"\n")
            (tmp_path / str(pid) / "stat").write_bytes(b"%d (%s) S %d 0 0" % (pid, comm, ppid))
        (tmp_path / "self").mkdir()  # non-numeric, skipped

        class _Entry:
            def __init__(self, name: str) -> None:
                self.name = name

            def stat(self) -> MagicMock:
                return MagicMock(st_uid=rows[int(self.name)][0] if self.name.isdigit() else my_uid)

            def __truediv__(self, child: str) -> Path:
                return tmp_path / self.name / child

        class _Proc:
            def iterdir(self) -> list[_Entry]:
                return [_Entry(p.name) for p in sorted(tmp_path.iterdir())]

            def __truediv__(self, child: str) -> Path:
                return tmp_path / child

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("kiro_crew.session_pid.Path", return_value=_Proc()),
            patch("os.getuid", return_value=my_uid),
        ):
            mock_sys.platform = "linux"
            result = _our_orphan_pids()

        assert sorted(result) == [100, 300, 400, 600]

    def test_macos_excludes_launcher_children(self) -> None:
        """ppid==launcher must NOT be reaped (regression guard).

        Orphans reparent to init (pid 1), never back to the launcher, so a
        launcher child is a live sibling and must be excluded; only the
        init-reparented pid is returned.
        """
        from kiro_crew.session_pid import _our_orphan_pids

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch(
                "subprocess.check_output",
                return_value=b"  500    42\n  600     1\n",
            ),
            patch("os.getuid", return_value=1000),
            patch("os.getppid", return_value=42),
        ):
            mock_sys.platform = "darwin"
            result = _our_orphan_pids()

        assert 500 not in result  # launcher child — excluded after the fix
        assert 600 in result  # init-reparented orphan — included

    def test_returns_empty_on_exception(self) -> None:
        """Returns empty list on failure, does not raise."""
        from kiro_crew.session_pid import _our_orphan_pids

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("subprocess.check_output", side_effect=OSError("ps failed")),
            patch("os.getuid", return_value=1000),
        ):
            mock_sys.platform = "darwin"
            result = _our_orphan_pids()

        assert result == []


@_POSIX_ONLY
class TestLinuxPidAge:
    """Direct tests for _linux_pid_age /proc/<pid>/stat starttime parsing."""

    @staticmethod
    def _proc(tmp_path: Path, pid: int, comm: bytes, start_ticks: int) -> Path:
        write_stat(tmp_path, pid, comm=comm, start_ticks=start_ticks)
        (tmp_path / "uptime").write_text("10000.0 9000.0")
        return tmp_path

    @pytest.mark.parametrize("comm", [b"my (weird) proc", BAD_COMM], ids=["parens", "not-utf8"])
    def test_age_past_any_comm(self, tmp_path: Path, monkeypatch, comm: bytes) -> None:
        """starttime is field 22 whatever the comm holds: spaces, parens, or bytes
        that are not UTF-8. starttime_ticks=500000, clk_tck=100 → 5000s offset;
        uptime=10000s → age=5000s.
        """
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "linux")
        root = self._proc(tmp_path, 1234, comm, 500000)
        with patch("os.sysconf", return_value=100):
            assert sp._linux_pid_age(1234, root) == 5000.0

    def test_malformed_stat_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Too-few fields → 0.0 fail-safe (min-age guard skips)."""
        from kiro_crew import session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "linux")
        (tmp_path / "999").mkdir()
        (tmp_path / "999" / "stat").write_bytes(b"999 (proc) S 1 1\n")
        (tmp_path / "uptime").write_text("10000.0 9000.0")
        with patch("os.sysconf", return_value=100):
            assert sp._linux_pid_age(999, tmp_path) == 0.0


class TestIsManagedAgentProcess:
    def test_self_pid_not_managed(self) -> None:
        """Our own test PID's cmdline lacks kiro-cli/claude → not managed.

        Exercises the platform_compat.process_matches call (the real
        /proc/<pid>/cmdline read on Linux) without killing anything.
        """
        from kiro_crew.session_pid import _is_managed_agent_process

        assert _is_managed_agent_process(os.getpid()) is False


# ── Marked-launcher orphan sweep tests ───────────


class TestMarkedMcpLauncherPredicates:
    """Positive-ID sweep path for fingerprint-less MCP launchers (npx)."""

    def test_matches_npx_playwright_null_separated(self) -> None:
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        cmdline = b"npx\x00@playwright/mcp\x00--headless"
        assert _is_marked_mcp_launcher(cmdline) is True

    def test_matches_npx_playwright_space_separated(self) -> None:
        """macOS ps output is space-separated — substring match covers both."""
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        cmdline = b"/usr/local/bin/node /usr/lib/node_modules/@playwright/mcp/cli.js"
        assert _is_marked_mcp_launcher(cmdline) is True

    def test_matches_generic_start_server(self) -> None:
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        cmdline = b"/bin/sh\x00-c\x00some-launcher mcp start-server slack-mcp"
        assert _is_marked_mcp_launcher(cmdline) is True

    def test_rejects_peer_gateway(self) -> None:
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        cmdline = b"python3\x00-m\x00kiro_crew.mcp_gateway.gatewayd\x00mcp start-server"
        assert _is_marked_mcp_launcher(cmdline) is False

    def test_rejects_unrelated_process(self) -> None:
        from kiro_crew.session_pid import _is_marked_mcp_launcher

        assert _is_marked_mcp_launcher(b"vim\x00notes-about-mcp.md") is False

    def test_sweepable_requires_env_marker_for_marked_launcher(self) -> None:
        """npx cmdline WITHOUT the environ marker is NOT sweepable."""
        from kiro_crew.session_pid import _is_sweepable_orphan_mcp

        cmdline = b"npx\x00@playwright/mcp\x00--headless"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False):
            assert _is_sweepable_orphan_mcp(1234, cmdline) is False

    def test_sweepable_with_env_marker(self) -> None:
        from kiro_crew.session_pid import _is_sweepable_orphan_mcp

        cmdline = b"npx\x00@playwright/mcp\x00--headless"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_sweepable_orphan_mcp(1234, cmdline) is True

    def test_fingerprinted_cmdline_never_reads_environ(self) -> None:
        """The pre-existing marker path must not depend on the environ read."""
        from kiro_crew.session_pid import _is_sweepable_orphan_mcp

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert _is_sweepable_orphan_mcp(1, b"kirocrew_sandbox_abc\x00--stdio") is True
        mock_env.assert_not_called()


class TestEnvHasKirocrewMarker:
    """Exec-time environ positive-identity read: ``/proc`` here, ``sysctl`` on macOS.

    Per-platform arms and the macOS record parse live in
    ``test_darwin_spawn_marker.py``; this class keeps the ``/proc`` reader and
    its real-child proof.
    """

    def test_platform_without_an_environ_oracle_fails_closed(self) -> None:
        """Windows can read no same-uid environ, so the gate keeps refusing.

        macOS is deliberately NOT this case any more: it reads the same
        exec-time environment out of ``sysctl KERN_PROCARGS2``, which is what
        makes the marked-launcher sweep reachable there at all.
        """
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        with patch("kiro_crew.session_pid.sys") as mock_sys:
            mock_sys.platform = "win32"
            assert _env_has_kirocrew_marker(os.getpid()) is False

    def test_read_failure_fails_closed(self) -> None:
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", side_effect=PermissionError),
        ):
            mock_sys.platform = "linux"
            assert _env_has_kirocrew_marker(1) is False

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc is Linux-only")
    def test_real_child_with_marker(self) -> None:
        """End-to-end: a real child spawned with the marker is identified.

        Polls briefly: /proc/<pid>/environ shows the parent's environment
        until the child completes exec (production is immune — the sweep's
        min-age guard runs long after exec).
        """
        from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
        from kiro_crew.session_pid import _env_has_kirocrew_marker
        from kiro_crew.testing.wait import wait_until

        env = {**os.environ, KIROCREW_SPAWNED_ENV: KIROCREW_SPAWNED_VALUE}
        proc = subprocess.Popen(["sleep", "30"], env=env)
        try:
            wait_until(lambda: _env_has_kirocrew_marker(proc.pid) is True)
        finally:
            proc.kill()
            proc.wait()

    @pytest.mark.skipif(sys.platform != "linux", reason="/proc is Linux-only")
    def test_real_child_without_marker(self) -> None:
        from kiro_crew.constants import KIROCREW_SPAWNED_ENV
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        env = {k: v for k, v in os.environ.items() if k != KIROCREW_SPAWNED_ENV}
        proc = subprocess.Popen(["sleep", "30"], env=env)
        try:
            assert _env_has_kirocrew_marker(proc.pid) is False
        finally:
            proc.kill()
            proc.wait()


class TestMarkedLauncherSweepIntegration:
    @pytest.fixture(autouse=True)
    def _stable_root_identity(self) -> Iterator[None]:
        """See TestKillOrphanMcps._stable_root_identity."""
        with patch("kiro_crew.session_pid._pid_start_token", return_value="tok-stable"):
            yield

    """find + kill phases honor the marked-launcher positive-ID path."""

    def test_find_includes_marked_npx_orphan(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[700]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"npx\x00@playwright/mcp\x00--headless"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [700]

    def test_find_excludes_unmarked_npx_orphan(self) -> None:
        """A user's own npx process (no environ marker) is never a candidate."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[710]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"npx\x00@playwright/mcp\x00--headless"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    @_POSIX_ONLY
    def test_kill_reverify_honors_marked_launcher(self) -> None:
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=720),
            patch("os.killpg") as mock_killpg,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"npx\x00@playwright/mcp\x00--headless"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([720])

        assert killed == 1
        mock_killpg.assert_called_once_with(720, signal.SIGKILL)

    @_POSIX_ONLY
    def test_kill_reverify_skips_unmarked_launcher(self) -> None:
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpgid", return_value=730),
            patch("os.killpg") as mock_killpg,
            patch("os.kill") as mock_kill,
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"npx\x00@playwright/mcp\x00--headless"),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([730])

        assert killed == 0
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()


# ── Work-process orphan sweep tests ───────────


class TestIsSweepableOrphanWork:
    """Unit tests for the work-class positive-identity predicate."""

    _PYTEST_CMDLINE = b"/usr/bin/python3\x00-m\x00pytest\x00test/\x00-x\x00-q"

    def test_marked_orphaned_old_work_process_is_sweepable(self) -> None:
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with (
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            assert _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, 700.0) is True

    def test_xdist_execnet_worker_is_sweepable(self) -> None:
        """pytest-xdist popen workers run under execnet's bootstrap cmdline."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        worker = (
            b"/repo/.venv/bin/python\x00-u\x00-c" b"\x00import sys;exec(eval(sys.stdin.readline()))"
        )
        with (
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            assert _is_sweepable_orphan_work(1234, worker, 700.0) is True

    def test_live_session_leader_blocks_sweep(self) -> None:
        """A backgrounded run whose kiro-cli session leader is ALIVE is kept.

        ``nohup pytest &`` reparents to init while the owning agent session
        still polls its log — SID still points at the live leader, so the
        sweep must leave the run alone. The environ is never read.
        """
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with (
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=True,
            ),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env,
        ):
            assert _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, 700.0) is False
        mock_env.assert_not_called()

    def test_unreadable_sid_fails_closed(self) -> None:
        """SID read failure -> assume the owner is alive -> never sweep."""
        from kiro_crew.session_pid import _work_orphan_session_leader_alive

        with patch("kiro_crew.session_pid._linux_pid_sid", return_value=-1):
            assert _work_orphan_session_leader_alive(1234) is True

    def test_self_session_leader_fails_closed(self) -> None:
        """A setsid'd coordinator (own leader) carries no ownership info -> kept."""
        from kiro_crew.session_pid import _work_orphan_session_leader_alive

        with patch("kiro_crew.session_pid._linux_pid_sid", return_value=1234):
            assert _work_orphan_session_leader_alive(1234) is True

    def test_dead_leader_means_session_ended(self, tmp_path: Path) -> None:
        """Leader gone (or PID recycled into a non-leader) -> session ended."""
        from kiro_crew.session_pid import _work_orphan_session_leader_alive

        write_stat(tmp_path, 1234, session=500)
        assert _work_orphan_session_leader_alive(1234, tmp_path) is False  # 500 gone
        write_stat(tmp_path, 500, session=7)
        assert _work_orphan_session_leader_alive(1234, tmp_path) is False  # leads another

    def test_a_present_leader_whose_stat_cannot_be_read_is_alive(self, tmp_path: Path) -> None:
        """Only a pid whose /proc entry is absent counts as a gone leader."""
        from kiro_crew.session_pid import _work_orphan_session_leader_alive

        write_stat(tmp_path, 1234, session=500)
        (tmp_path / "500").mkdir()
        assert _work_orphan_session_leader_alive(1234, tmp_path) is True

    def test_marked_detached_daemon_is_not_sweepable(self) -> None:
        """A marked process WITHOUT a test-runner shape is never swept.

        Agents deliberately leave some marked processes running past turn end
        (e.g. a preview server detached with ``start_new_session=True``).
        Those are intentional survivors — the shape gate excludes them, and
        their environ is never even read.
        """
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        daemon = b"/usr/bin/node\x00/opt/serve-sim/cli.js\x00--udid\x00ABC123"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert _is_sweepable_orphan_work(1234, daemon, 7000.0) is False
        mock_env.assert_not_called()

    def test_pytest_path_fragment_daemon_is_not_sweepable(self) -> None:
        """'pytest' inside a path ARGUMENT must not match (structural, not
        substring): ``nohup node /work/pytest-dashboard/server.js`` is a
        daemon, not a test run."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        daemon = b"/usr/bin/node\x00/work/pytest-dashboard/server.js"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert _is_sweepable_orphan_work(1234, daemon, 7000.0) is False
        mock_env.assert_not_called()

    def test_venv_pytest_console_script_is_sweepable(self) -> None:
        """argv0 basename exactly ``pytest`` (venv console script) matches."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        console = b"/repo/.venv/bin/pytest\x00test/\x00-q"
        with (
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            assert _is_sweepable_orphan_work(1234, console, 700.0) is True

    def test_bootstrap_payload_as_free_arg_is_not_sweepable(self) -> None:
        """The execnet payload only matches as the argument OF ``-c`` — the
        same bytes appearing as any other argv element do not qualify."""
        from kiro_crew.session_pid import _work_sweep_cmdline_is_test_runner

        free = b"/usr/bin/grep\x00import sys;exec(eval(sys.stdin.readline()))\x00log"
        assert _work_sweep_cmdline_is_test_runner(free) is False

    def test_young_work_process_is_not_sweepable(self) -> None:
        """Below the dedicated work floor (600s) — even marked, left alone.

        Age is checked FIRST so a young process never even has its environ
        read; the env-marker mock asserts it stays uncalled.
        """
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, 599.0) is False
        mock_env.assert_not_called()

    def test_mcp_floor_is_not_enough_for_work_class(self) -> None:
        """The 120s MCP floor must NOT admit work processes (dedicated floor)."""
        from kiro_crew.session_pid import (
            _ORPHAN_MIN_AGE_SECONDS,
            _ORPHAN_WORK_MIN_AGE_SECONDS,
            _is_sweepable_orphan_work,
        )

        assert _ORPHAN_WORK_MIN_AGE_SECONDS > _ORPHAN_MIN_AGE_SECONDS
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert (
                _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, _ORPHAN_MIN_AGE_SECONDS + 1)
                is False
            )

    def test_unmarked_work_process_is_not_sweepable(self) -> None:
        """No KIROCREW_SPAWNED environ marker — a user's own pytest is safe."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False):
            assert _is_sweepable_orphan_work(1234, self._PYTEST_CMDLINE, 700.0) is False

    def test_managed_agent_basename_is_not_sweepable(self) -> None:
        """kiro-cli/claude runtimes stay owned by their tracked-PID lifecycle."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert (
                _is_sweepable_orphan_work(1234, b"/usr/local/bin/kiro-cli\x00chat\x00--acp", 700.0)
                is False
            )
            assert _is_sweepable_orphan_work(1234, b"claude\x00--print", 700.0) is False

    def test_gateway_entrypoint_is_not_sweepable(self) -> None:
        """Agent-launched peer gateways (e.g. dev pods) are never swept."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert (
                _is_sweepable_orphan_work(
                    1234, b"python3\x00-m\x00kiro_crew.mcp_gateway.gatewayd", 700.0
                )
                is False
            )

    def test_empty_cmdline_is_not_sweepable(self) -> None:
        """Kernel threads / zombies (empty cmdline) are never candidates."""
        from kiro_crew.session_pid import _is_sweepable_orphan_work

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_sweepable_orphan_work(1234, b"", 700.0) is False


class TestWorkOrphanSweepIntegration:
    """find + kill phases honor the work-process positive-ID path."""

    _PYTEST_CMDLINE = b"/usr/bin/python3\x00-m\x00pytest\x00test/\x00-x\x00-q"

    def test_find_includes_marked_old_work_orphan(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[900]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [900]

    def test_find_excludes_young_marked_work_orphan(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[901]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_find_excludes_unmarked_work_orphan(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[902]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_find_excludes_marked_kiro_cli_orphan(self) -> None:
        """Managed agent runtime carrying the marker still isn't work-swept."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[903]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path, "read_bytes", return_value=b"/usr/local/bin/kiro-cli\x00chat\x00--acp"
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_kill_sweeps_whole_subtree_leaf_first(self) -> None:
        """Descendants (incl. grandchildren) die before parents; root last."""
        from kiro_crew.session_pid import kill_orphan_mcps

        kill_order: list[int] = []

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
            # Preorder: 910 -> [911, 912(-> 913)]; 913 is a grandchild.
            patch("kiro_crew.acp.client._get_child_pids", return_value=[911, 912, 913]),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, _sig: kill_order.append(p),
            ),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([910])

        assert killed == 4
        # Reversed preorder guarantees each process dies before its parent.
        assert kill_order == [913, 912, 911, 910]

    def test_kill_reverify_skips_now_young_or_unmarked(self) -> None:
        """Kill-phase re-verify fails closed when the marker is gone (TOCTOU)."""
        from kiro_crew.session_pid import kill_orphan_mcps

        with (
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
            patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill,
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([920])

        assert killed == 0
        mock_kill.assert_not_called()

    def test_subtree_kill_respects_global_cap(self) -> None:
        """The _ORPHAN_SWEEP_MAX_KILLS cap bounds subtree members too."""
        from kiro_crew.session_pid import kill_orphan_mcps

        kill_order: list[int] = []

        with (
            patch("kiro_crew.session_pid._ORPHAN_SWEEP_MAX_KILLS", 3),
            patch("os.getpgrp", return_value=1000),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=self._PYTEST_CMDLINE),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=700.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch(
                "kiro_crew.session_pid._work_orphan_session_leader_alive",
                return_value=False,
            ),
            patch(
                "kiro_crew.acp.client._get_child_pids",
                return_value=[931, 932, 933, 934, 935],
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, _sig: kill_order.append(p),
            ),
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([930])

        assert killed == 3
        # Deepest three descendants reaped; root survives to the next cycle.
        assert kill_order == [935, 934, 933]
        assert 930 not in kill_order


class TestSpawnedMarkerInjection:
    """Every provider/MCP spawn site injects the KIROCREW_SPAWNED marker."""

    def test_sandboxed_spawn_argv_injects_marker(self) -> None:
        from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
        from kiro_crew.sandbox import sandboxed_spawn_argv

        with (
            patch("kiro_crew.sandbox.wrap_argv", return_value=(["echo"], None)),
            patch("kiro_crew.sandbox.cgroup_scope_argv", side_effect=lambda a: a),
        ):
            _, env, _ = sandboxed_spawn_argv(["echo"], env={"PATH": "/bin"})

        assert env.get(KIROCREW_SPAWNED_ENV) == KIROCREW_SPAWNED_VALUE

    def test_spawn_site_source_registry(self, tmp_path) -> None:
        """Drift guard: the marker constant must appear at every known
        provider/MCP spawn-env build site. A new spawn site that replaces the
        inherited environment must add itself here AND inject the marker.

        Both ACP drivers build their child's environment in the one launch tail
        (``acp/launch.py``), so that file is the ACP site here, and each driver's
        child is ALSO checked to actually carry the marker: a driver that stopped
        launching through the tail would keep the file green and spawn unmarked.

        The fork is KiroACP-only, so upstream's ``providers/claude_code.py``
        spawn site is intentionally absent from this list (the module is
        deleted in the public fork)."""
        import acp_launch_capture as capture_mod

        from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KIRO
        from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE

        src_root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        spawn_sites = [
            "sandbox_spawn.py",
            "acp/launch.py",
            "mcp_gateway/backend.py",
        ]
        for rel in spawn_sites:
            content = (src_root / rel).read_text(encoding="utf-8")
            assert "KIROCREW_SPAWNED_ENV" in content, (
                f"{rel} no longer injects the KIROCREW_SPAWNED marker — "
                "escaped MCP trees from this site become unsweepable"
            )
        # The client's launch and the shared runtime's.
        for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_CODEX):
            added = capture_mod.capture(backend, tmp_path / (backend or "kiro"))["env_added"]
            assert added.get(KIROCREW_SPAWNED_ENV) == KIROCREW_SPAWNED_VALUE, (
                f"the {backend or 'kiro'} driver's child no longer carries the "
                "KIROCREW_SPAWNED marker — escaped MCP trees from it become unsweepable"
            )


# ── Untracked managed-agent runtime orphan (REPORT-ONLY) ──


@pytest.fixture()
def reset_untracked_report_dedup() -> Iterator[None]:
    """Clear the module-level report dedup set around each test.

    The detector keeps reported PIDs in module state so a persisting orphan is
    logged once rather than once per sweep tick; leaking that between tests
    would make assertions order-dependent.
    """
    import kiro_crew.session_pid as sp

    sp._reported_untracked_agent_pids.clear()
    yield
    sp._reported_untracked_agent_pids.clear()


def _agent_cmdline(argv0: str = "/opt/kiro/bin/kiro-cli") -> bytes:
    """A managed agent runtime cmdline in Linux /proc (NUL-separated) form."""
    return b"\x00".join([argv0.encode(), b"chat", b"--no-interactive"])


class TestTrackedAgentPids:
    """_tracked_agent_pids unions the PIDs both tracking files claim."""

    def test_no_files_yields_empty_set(self, pid_file: Path, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _read_tracked_agent_pids, _tracked_agent_pids

        assert _tracked_agent_pids() == set()
        assert _read_tracked_agent_pids() == (set(), True)

    def test_session_entry_collects_child_not_gateway_or_identity_field(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        """Only the child is reapable through a session entry.

        The gateway field names the OWNER whose death makes the entry sweepable,
        never a process reclaimed through it, and the third field is a
        start-time identity (numeric on Linux). Counting either would let a
        stale entry suppress a genuine report.
        """
        from kiro_crew.session_pid import _tracked_agent_pids

        session_pid_file.write_text("4100:4200:987654321\n", encoding="utf-8")

        assert _tracked_agent_pids() == {4200}

    def test_child_parent_entry_collects_child_not_parent(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        """``_cleanup_orphaned_mcp_servers`` kills the child, not the parent."""
        from kiro_crew.session_pid import _tracked_agent_pids

        pid_file.write_text("5100:5200\n", encoding="utf-8")

        assert _tracked_agent_pids() == {5100}

    def test_bare_line_names_its_own_process_in_either_file(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _tracked_agent_pids

        session_pid_file.write_text("5300\n", encoding="utf-8")
        pid_file.write_text("5400\n", encoding="utf-8")

        assert _tracked_agent_pids() == {5300, 5400}

    def test_both_files_union(self, pid_file: Path, session_pid_file: Path) -> None:
        """Each file contributes its own reapable field, at its own index."""
        from kiro_crew.session_pid import _tracked_agent_pids

        session_pid_file.write_text("10:11\n", encoding="utf-8")
        pid_file.write_text("20:21\n", encoding="utf-8")

        assert _tracked_agent_pids() == {11, 20}

    def test_malformed_and_non_positive_fields_are_skipped(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        """A partially-appended or hand-edited line must not raise."""
        from kiro_crew.session_pid import _tracked_agent_pids

        session_pid_file.write_text("garbage\n0:-3\n:\n77:78\n", encoding="utf-8")

        assert _tracked_agent_pids() == {78}

    def test_unreadable_file_is_tolerated(self, pid_file: Path, session_pid_file: Path) -> None:
        """Report-only: an OSError costs a log line at worst, never a raise."""
        from kiro_crew.session_pid import _tracked_agent_pids

        pid_file.write_text("31:32\n", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=OSError("boom")):
            assert _tracked_agent_pids() == set()

    def test_unreadable_non_missing_file_marks_snapshot_incomplete(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _read_tracked_agent_pids

        with patch.object(Path, "read_text", side_effect=OSError(errno.EACCES, "denied")):
            assert _read_tracked_agent_pids() == (set(), False)

    def test_undecodable_file_marks_snapshot_incomplete(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        """A stray non-UTF-8 byte is damage, not an exception out of every reaper."""
        from kiro_crew.session_pid import _read_tracked_agent_pids

        session_pid_file.write_bytes(b"10:11\n\xff\xfe\n")
        pid_file.write_text("31:32\n", encoding="utf-8")
        # Like a malformed entry: the damaged line marks the snapshot incomplete
        # and every readable line is still counted.
        assert _read_tracked_agent_pids() == ({11, 31}, False)

    def test_malformed_entry_marks_snapshot_incomplete_but_keeps_valid_pids(
        self, pid_file: Path, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _read_tracked_agent_pids

        session_pid_file.write_text("10:11\ntruncated:\n", encoding="utf-8")
        assert _read_tracked_agent_pids() == ({11}, False)


class TestIsUntrackedManagedAgentOrphan:
    """Positive identity for the report-only detector."""

    def test_untracked_marked_runtime_is_detected(self) -> None:
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(900, _agent_cmdline(), set()) is True

    def test_tracked_runtime_is_not_detected(self) -> None:
        """A PID either file claims is already reachable by a reaper."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(900, _agent_cmdline(), {900}) is False

    def test_unmarked_process_is_not_detected(self) -> None:
        """A user's own kiro-cli (no environ marker) is never reported."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False):
            assert _is_untracked_managed_agent_orphan(901, _agent_cmdline(), set()) is False

    def test_claude_runtime_basename_also_detected(self) -> None:
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert (
                _is_untracked_managed_agent_orphan(
                    902, _agent_cmdline("/usr/local/bin/claude"), set()
                )
                is True
            )

    def test_peer_gateway_is_not_detected(self) -> None:
        """A gateway/CLI entrypoint is not an agent runtime."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        cmdline = b"kiro-cli\x00-m\x00kiro_crew.mcp_gateway.gatewayd"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(903, cmdline, set()) is False

    def test_non_runtime_basename_is_not_detected(self) -> None:
        """A marked pytest orphan belongs to the work sweep, not this arm."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        cmdline = b"/venv/bin/pytest\x00-x"
        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(904, cmdline, set()) is False

    def test_empty_cmdline_is_not_detected(self) -> None:
        """Kernel thread / zombie — no argv to identify."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True):
            assert _is_untracked_managed_agent_orphan(905, b"", set()) is False

    def test_no_environ_read_when_shape_already_declines(self) -> None:
        """Cheap gates run first — the /proc environ read is the last resort."""
        from kiro_crew.session_pid import _is_untracked_managed_agent_orphan

        with patch("kiro_crew.session_pid._env_has_kirocrew_marker") as mock_env:
            assert (
                _is_untracked_managed_agent_orphan(906, b"/venv/bin/pytest\x00-x", set()) is False
            )
        mock_env.assert_not_called()


class TestUntrackedRuntimeReportIntegration:
    """find_orphan_mcp_candidates reports the orphan and terminates nothing."""

    def test_reports_at_error_and_never_returns_as_candidate(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An untracked runtime leak is reported, not silently dropped.

        Every existing reaper declines an untracked runtime, so before this arm
        the sweep produced no candidate AND no diagnostic. The report must
        appear, and the PID must stay out of ``candidates`` — this arm has no
        kill authority.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4242]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []  # report-only: nothing handed to the kill phase
        records = [r for r in caplog.records if "4242" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        message = records[0].getMessage()
        assert "kiro-cli" in message
        assert "NEITHER PID file" in message
        assert "report only" in message

    def test_argv0_control_characters_cannot_forge_log_lines(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """argv0 is set by the process itself, so it is untrusted input.

        A newline in it would otherwise forge whole lines in gateway.log and
        through /api/logs, which read as if the gateway had emitted them.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        hostile = b"\x00".join([b"/tmp/kiro-cli\nERROR forged line", b"chat"])

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4848]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=hostile),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "4848" in r.getMessage()]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "\n" not in message  # the whole report stays one line
        assert "\\nERROR forged line" in message  # escaped, not interpreted

    def test_report_is_logged_once_across_repeated_sweeps(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A persisting orphan must not re-log on every sweep tick."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4343]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            find_orphan_mcp_candidates(active_pids=set())
            find_orphan_mcp_candidates(active_pids=set())
            find_orphan_mcp_candidates(active_pids=set())

        assert len([r for r in caplog.records if "4343" in r.getMessage()]) == 1

    def test_vanished_pid_re_arms_the_report(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dedup state is scoped to PIDs still detected, so it cannot grow."""
        import kiro_crew.session_pid as sp
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            with patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4444]):
                find_orphan_mcp_candidates(active_pids=set())
            with patch("kiro_crew.session_pid._our_orphan_pids", return_value=[]):
                find_orphan_mcp_candidates(active_pids=set())
                assert sp._reported_untracked_agent_pids == set()
            with patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4444]):
                find_orphan_mcp_candidates(active_pids=set())

        assert len([r for r in caplog.records if "4444" in r.getMessage()]) == 2

    def test_tracked_runtime_is_not_reported(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A runtime the session file records is reachable — no diagnostic."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        session_pid_file.write_text("7:4545:99999\n", encoding="utf-8")

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4545]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        assert [r for r in caplog.records if "4545" in r.getMessage()] == []

    def test_recycled_owner_pid_does_not_suppress_the_report(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A stale entry's OWNER field must not shadow a real leak.

        The gateway field of a session entry and the parent field of a
        child entry name processes no reaper terminates through that entry.
        Once such an owner has died and its PID has been recycled into a leaked
        runtime, treating the field as tracked would return the sweep to the
        exact silence the report exists to prevent.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        # 4747 appears only as a dead gateway (session) and a dead parent (child).
        session_pid_file.write_text("4747:11:99999\n", encoding="utf-8")
        pid_file.write_text("12:4747\n", encoding="utf-8")

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4747]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=3600.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []  # still report-only
        assert len([r for r in caplog.records if "4747" in r.getMessage()]) == 1

    def test_young_orphan_is_not_reported(
        self,
        pid_file: Path,
        session_pid_file: Path,
        reset_untracked_report_dedup: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Below the age floor the tracking append may simply not have landed."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[4646]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_agent_cmdline()),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=5.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        assert [r for r in caplog.records if "4646" in r.getMessage()] == []
