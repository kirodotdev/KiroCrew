"""Tests for PID tracking and orphan cleanup in session.py."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from kiro_crew import platform_compat


@pytest.fixture()
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _pid_file_path to a temp file."""
    p = tmp_path / "kiro_pids.txt"
    monkeypatch.setattr("kiro_crew.session_pid._pid_file_path", lambda: p)
    return p


@pytest.fixture()
def session_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _session_pid_file_path to a temp file."""
    p = tmp_path / "kiro_session_pids.txt"
    monkeypatch.setattr("kiro_crew.session_pid._session_pid_file_path", lambda: p)
    return p


class TestTrackUntrack:
    def test_track_pid_creates_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid

        _track_pid(12345)
        assert "12345" in pid_file.read_text()

    def test_track_multiple(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid

        _track_pid(111)
        _track_pid(222)
        lines = pid_file.read_text().strip().splitlines()
        assert lines == ["111", "222"]

    def test_untrack_pid(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        _track_pid(111)
        _track_pid(222)
        _untrack_pid(111)
        lines = pid_file.read_text().strip().splitlines()
        assert lines == ["222"]

    def test_untrack_nonexistent(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        _track_pid(111)
        _untrack_pid(999)  # should not crash
        assert "111" in pid_file.read_text()

    def test_untrack_session_pid(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        _track_session_pid(111)
        _track_session_pid(222)
        _untrack_session_pid(111)
        gw = os.getpid()
        lines = session_pid_file.read_text().strip().splitlines()
        assert lines == [f"{gw}:222"]

    def test_untrack_session_pid_missing_file(self, session_pid_file: Path) -> None:
        from kiro_crew.session_pid import _untrack_session_pid

        _untrack_session_pid(999)  # should not crash on missing file
        assert not session_pid_file.exists()

    def test_untrack_session_pid_other_gateway_untouched(self, session_pid_file: Path) -> None:
        """Untracking our PID must NOT remove other gateways' entries for same child PID."""
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        _track_session_pid(111)
        # Simulate another gateway's entry for the same child PID
        with open(session_pid_file, "a", encoding="utf-8") as f:
            f.write("99999:111\n")
        _untrack_session_pid(111)
        lines = session_pid_file.read_text().strip().splitlines()
        assert lines == ["99999:111"]

    def test_track_child_pids_with_parent(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_child_pids

        _track_child_pids({100: None, 200: None, 300: None}, parent_pid=999)
        lines = pid_file.read_text().strip().splitlines()
        assert set(lines) == {"100:999", "200:999", "300:999"}

    def test_track_child_pids_dedup(self, pid_file: Path) -> None:
        """Duplicate child:parent entries should not be written."""
        from kiro_crew.session_pid import _track_child_pids

        _track_child_pids({100: None, 200: None}, parent_pid=999)
        _track_child_pids({100: None, 300: None}, parent_pid=999)
        lines = pid_file.read_text().strip().splitlines()
        assert sorted(lines) == ["100:999", "200:999", "300:999"]

    def test_untrack_child_pids(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_child_pids, _untrack_child_pids

        _track_child_pids({100: None, 200: None, 300: None}, parent_pid=999)
        _untrack_child_pids({100: None, 300: None})
        lines = pid_file.read_text().strip().splitlines()
        assert lines == ["200:999"]

    def test_untrack_child_pids_preserves_bare_pid(self, pid_file: Path) -> None:
        """Untracking child PIDs must not remove bare PID lines (kiro-cli parents)."""
        from kiro_crew.session_pid import _track_child_pids, _track_pid, _untrack_child_pids

        _track_pid(100)  # bare parent line
        _track_child_pids({100: None}, parent_pid=999)  # child line with same PID
        _untrack_child_pids({100: None})
        lines = pid_file.read_text().strip().splitlines()
        assert "100" in lines  # bare line preserved


class TestCleanupOrphanedMcpServers:
    def test_dead_child_pruned(self, pid_file: Path) -> None:
        """Dead child PIDs should be removed from the file silently."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("99999:1\n")  # child=99999, parent=1
        _cleanup_orphaned_mcp_servers()
        assert "99999" not in pid_file.read_text()

    def test_alive_child_with_alive_parent_survives(self, pid_file: Path) -> None:
        """Child whose parent session is still alive should NOT be killed."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        my_pid = os.getpid()
        child_pid = 77777
        pid_file.write_text(f"{child_pid}:{my_pid}\n")

        def fake_pid_exists(pid: int) -> bool:
            return pid in (child_pid, my_pid)  # both alive

        with patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        assert str(child_pid) in pid_file.read_text()

    def test_alive_child_with_dead_parent_killed(self, pid_file: Path) -> None:
        """Child whose parent session died should be killed (PPid=1 confirms orphan)."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999\n")  # parent 99999 is dead

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777  # child alive, parent dead

        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.session_pid.platform_compat.kill_pid"),
            patch("kiro_crew.platform_compat.get_ppid", return_value=1),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 1

    def test_alive_child_with_dead_parent_pid_reused(self, pid_file: Path) -> None:
        """Child PID reused by unrelated process should NOT be killed."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999\n")

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777  # child alive (reused PID), parent dead

        with (
            patch("kiro_crew.session_pid.platform_compat.pid_exists", side_effect=fake_pid_exists),
            patch("kiro_crew.platform_compat.get_ppid", return_value=5555),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        assert "77777" not in pid_file.read_text()  # stale entry pruned

    def test_bare_pid_dead_pruned(self, pid_file: Path) -> None:
        """Dead bare PIDs should be pruned from the file."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("99999\n")

        with patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=False):
            killed = _cleanup_orphaned_mcp_servers()
        assert killed == 0
        assert "99999" not in pid_file.read_text()

    def test_bare_pid_alive_kept(self, pid_file: Path) -> None:
        """Alive bare PIDs should be kept in the file."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("88888\n")

        with patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True):
            killed = _cleanup_orphaned_mcp_servers()
        assert killed == 0
        assert "88888" in pid_file.read_text()

    def test_empty_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("")
        assert _cleanup_orphaned_mcp_servers() == 0

    def test_no_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        assert _cleanup_orphaned_mcp_servers() == 0


class TestCleanupOrphanedSessions:
    def test_preserves_non_kiro_pids(self, session_pid_file: Path) -> None:
        """Bug fix: non-kiro PIDs (MCP servers) must survive — not killed."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("99998\n99999\n")

        # Both PIDs must read as ALIVE so the sweep reaches the managed/kill
        # decision (the liveness gate is pid_liveness(), tri-state). Without this
        # the real os.kill(pid,0) on the fleet returns DEAD and both are pruned as
        # dead — the test would pass vacuously without exercising the kill path.
        # kill_pid is patched so _kill_pid_tree reports the managed PID killed
        # (root_killed=True -> pruned); the non-managed one is pruned via the
        # _is_managed_agent_process(False) branch.
        with (
            patch(
                "kiro_crew.session_pid._is_managed_agent_process", side_effect=lambda p: p == 99998
            ),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.session_pid.platform_compat.kill_pid"),
        ):
            cleanup_orphaned_sessions()

        # File is truncated after startup cleanup
        content = session_pid_file.read_text()
        assert content == ""

    def test_kiro_pids_killed(self, session_pid_file: Path) -> None:
        """Kiro PIDs should be SIGKILL'd."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("99998\n")

        kills: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # The sweep's liveness gate is pid_liveness() (tri-state), not pid_exists();
            # ALIVE -> falls through to the kill path. pid_exists is still patched for
            # the post-kill re-probe branch.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch("kiro_crew.session_pid.platform_compat.kill_pid", side_effect=fake_kill),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert (99998, platform_compat.SIGKILL) in kills

    def test_malformed_pid_files_deleted(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed session_pid_*.txt files (e.g. MagicMock leak) should be deleted."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        session_pid_file.write_text("")  # no kiro PIDs to kill

        # Create one valid (dead process) and one malformed pid file
        (tmp_path / "session_pid_99999.txt").write_text("sess-dead")
        (tmp_path / "session_pid_mock.get_pid().txt").write_text("sess-mock")

        with (
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            cleanup_orphaned_sessions()

        # Both should be cleaned up
        assert not (tmp_path / "session_pid_99999.txt").exists()
        assert not (tmp_path / "session_pid_mock.get_pid().txt").exists()

    def test_malformed_pid_file_unlink_oserror_continues(
        self, tmp_path: Path, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError on malformed pid file unlink should not abort the cleanup loop."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        session_pid_file.write_text("")

        # Create malformed + valid pid files
        (tmp_path / "session_pid_bad!name.txt").write_text("sess-bad")
        (tmp_path / "session_pid_99999.txt").write_text("sess-dead")

        original_unlink = Path.unlink

        def unlink_that_fails_on_bad(path_self, *a, **kw):
            if "bad!name" in path_self.name:
                raise OSError("permission denied")
            return original_unlink(path_self, *a, **kw)

        monkeypatch.setattr(Path, "unlink", unlink_that_fails_on_bad)

        with (
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            cleanup_orphaned_sessions()  # should not raise

        # bad!name still exists (unlink failed gracefully), valid one cleaned up
        assert (tmp_path / "session_pid_bad!name.txt").exists()
        assert not (tmp_path / "session_pid_99999.txt").exists()


class TestResetStateUntracksParentPid:
    def test_reset_state_untracks_parent_pid(self) -> None:
        """Verify _reset_state calls _untrack_pid with the saved PID."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient.__new__(AcpClient)
        client._process = None
        client._pid = 54321
        client._session_id = None
        client._buffer = bytearray()
        client._cancelled = False
        client._resumed = False
        client._sandbox_cleanup = None
        client._child_pids = {}
        client._stderr_lines = deque(["some error"], maxlen=20)
        client._pending_oauth_requests = []
        client._oauth_emitted_servers = set()
        mock_task = Mock()
        mock_task.done.return_value = False
        client._stderr_task = mock_task

        with patch("kiro_crew.session._untrack_pid") as mock_untrack:
            client._reset_state()

        assert client._pid is None
        assert len(client._stderr_lines) == 0
        assert client._stderr_task is None
        mock_task.cancel.assert_called_once()
        mock_untrack.assert_called_once_with(54321)


# ── Untracked orphan MCP sweep tests (Mesh-1870) ───────────


class TestFindOrphanMcpCandidates:
    """Tests for find_orphan_mcp_candidates (process-table scan)."""

    def test_excludes_pids_in_active_set(self) -> None:
        """PIDs present in active_pids are never returned as candidates."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[100, 200]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"kirocrew_sandbox_abc.py"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids={100, 200})

        assert result == []

    def test_excludes_non_kirocrew_processes(self) -> None:
        """Orphans without known MCP entrypoint markers are skipped."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[300]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path, "read_bytes", return_value=b"/usr/bin/python3\x00some_other_script.py"
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_excludes_non_entrypoint_vim_grep(self) -> None:
        """Non-Python processes mentioning kirocrew in args (e.g. vim, grep) are skipped."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[350]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"vim\x00/tmp/kirocrew_sandbox_abc.log"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_excludes_peer_gateway(self) -> None:
        """Peer gateways (gatewayd processes) are never candidates.

        Age is patched above the min-age floor so the assertion depends on the
        _GATEWAY_MARKERS exclusion in _is_orphan_mcp, not on the age guard
        short-circuiting before the exclusion logic ever runs.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[360]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                return_value=(
                    b"python3\x00-m\x00kiro_crew.mcp_gateway.gatewayd"
                    b"\x00--socket\x00/tmp/gw.sock"
                ),
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_includes_kirocrew_orphan_not_in_active(self) -> None:
        """Orphaned process with sandbox wrapper entrypoint and not in active set is a candidate."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[400]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                return_value=b"python3\x00/tmp/kirocrew_sandbox_xyz.py",
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [400]

    def test_excludes_own_pid(self) -> None:
        """The gateway's own PID is never returned."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[999]),
            patch("os.getpid", return_value=999),
        ):
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_does_not_match_builder_mcp(self) -> None:
        """builder-mcp is NOT a KiroCrew-spawned process in this public fork.

        Regression guard: the upstream MeshClaw reaper lists ``builder-mcp`` (an
        Amazon-internal server it manages), but the de-Amazoned fork never spawns
        it (the CPP companion contributes it, not the core). Reaping a user-owned
        ``builder-mcp`` orphan would SIGKILL an unrelated process, so the marker
        is deliberately absent here.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[410]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=b"builder-mcp\x00--stdio"),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=300.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []

    def test_matches_macos_space_separated_cmdline(self) -> None:
        """macOS ps output (space-separated) is correctly parsed."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        def mock_check_output(cmd, **kwargs):
            # Single combined ps call returns "<etime> <command...>"
            if "etime=" in cmd and "command=" in cmd:
                return b"   05:00 python3 /tmp/kirocrew_sandbox_xyz.py"
            return b""

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[420]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch(
                "subprocess.check_output",
                side_effect=mock_check_output,
            ),
            patch("os.getpid", return_value=1),
        ):
            mock_sys.platform = "darwin"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == [420]

    def test_skips_young_processes(self) -> None:
        """Processes younger than _ORPHAN_MIN_AGE_SECONDS are never candidates."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[450]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                return_value=b"python3\x00/tmp/kirocrew_sandbox_new.py",
            ),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=50.0),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []


class TestKillOrphanMcps:
    """Tests for kill_orphan_mcps (kill confirmed orphans)."""

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
        """If cmdline no longer matches at kill time, PID is skipped (TOCTOU)."""
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
        kills for subsequent PIDs (regression: AutoSDE rev4).

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


class TestOurOrphanPids:
    """Direct tests for _our_orphan_pids (Linux /proc and macOS ps branches)."""

    def test_linux_proc_scan_finds_init_and_subreaper_children(self) -> None:
        """Linux /proc two-pass scan: includes ppid==1 and ppid==systemd subreaper.

        Exercises the real Linux branch (systemd --user subreaper detection in
        pass 1 + PPid parsing in pass 2), not the macOS ps path.
        """
        from kiro_crew.session_pid import _our_orphan_pids

        class _FakeProcEntry:
            def __init__(self, name: str, uid: int, comm: str, ppid: str) -> None:
                self.name = name
                self._uid = uid
                self._comm = comm
                self._ppid = ppid

            def stat(self) -> MagicMock:
                return MagicMock(st_uid=self._uid)

            def __truediv__(self, child: str) -> MagicMock:
                node = MagicMock()
                if child == "comm":
                    node.read_text.return_value = self._comm + "\n"
                else:  # "status"
                    node.read_text.return_value = f"Name:\t{self._comm}\nPPid:\t{self._ppid}\n"
                return node

        my_uid = 1000
        entries = [
            _FakeProcEntry("100", my_uid, "python3", "1"),  # init-reparented
            _FakeProcEntry("200", my_uid, "bash", "50"),  # live child, excluded
            _FakeProcEntry("300", my_uid, "systemd", "1"),  # --user subreaper
            _FakeProcEntry("400", my_uid, "worker", "300"),  # child of subreaper
            _FakeProcEntry("500", 9999, "python3", "1"),  # other uid, excluded
            _FakeProcEntry("self", my_uid, "x", "1"),  # non-numeric, skipped
        ]
        proc_root = MagicMock()
        proc_root.iterdir.return_value = entries

        with (
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch("kiro_crew.session_pid.Path", return_value=proc_root),
            patch("os.getuid", return_value=my_uid),
        ):
            mock_sys.platform = "linux"
            result = _our_orphan_pids()

        assert 100 in result  # ppid == init
        assert 300 in result  # subreaper itself is ppid == init
        assert 400 in result  # ppid == detected systemd subreaper
        assert 200 not in result  # ppid is a live process, not orphaned
        assert 500 not in result  # different uid

    def test_macos_excludes_launcher_children(self) -> None:
        """ppid==launcher must NOT be reaped (regression: CR-282948194).

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


class TestLinuxPidAge:
    """Direct tests for _linux_pid_age /proc/<pid>/stat starttime parsing."""

    @staticmethod
    def _patch_proc(stat_line: str, uptime: str = "10000.0 9000.0"):
        def fake_path(p: object) -> MagicMock:
            node = MagicMock()
            if str(p).endswith("/stat"):
                node.read_text.return_value = stat_line
            elif str(p) == "/proc/uptime":
                node.read_text.return_value = uptime
            return node

        return patch("kiro_crew.session_pid.Path", side_effect=fake_path)

    def test_age_with_spaces_and_parens_in_comm(self) -> None:
        """starttime is read from field 22 even when comm contains spaces/parens.

        rfind(')') must land on the comm's closing paren so field-index math
        starts at the state field. starttime_ticks=500000, clk_tck=100 →
        5000s offset; uptime=10000s → age=5000s.
        """
        from kiro_crew.session_pid import _linux_pid_age

        # pid (comm) state ppid ... starttime(field 22 == index 19 after state)
        post_comm = "S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 0 20 0 1 500000 0 0"
        stat_line = f"1234 (my (weird) proc) {post_comm}\n"

        with self._patch_proc(stat_line), patch("os.sysconf", return_value=100):
            age = _linux_pid_age(1234, now=123456.0)

        assert age == 5000.0

    def test_malformed_stat_returns_zero(self) -> None:
        """Too-few fields → IndexError → 0.0 fail-safe (min-age guard skips)."""
        from kiro_crew.session_pid import _linux_pid_age

        with self._patch_proc("999 (proc) S 1 1\n"), patch("os.sysconf", return_value=100):
            age = _linux_pid_age(999, now=123456.0)

        assert age == 0.0


class TestIsManagedAgentProcess:
    def test_self_pid_not_managed(self) -> None:
        """Our own test PID's cmdline lacks kiro-cli/claude → not managed.

        Exercises the platform_compat.process_matches call (the real
        /proc/<pid>/cmdline read on Linux) without killing anything.
        """
        from kiro_crew.session_pid import _is_managed_agent_process

        assert _is_managed_agent_process(os.getpid()) is False


class TestSyncKillProvider:
    def test_no_pid_returns_early(self) -> None:
        """Provider with no client/_proc/_active_proc PID → early return."""
        from kiro_crew.session_pid import _sync_kill_provider

        provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        provider._client = None
        provider._proc = None
        provider._active_proc = None

        with patch("kiro_crew.session_pid.platform_compat.kill_pid") as mock_kill:
            _sync_kill_provider(provider)

        mock_kill.assert_not_called()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX SIGTERM→SIGKILL escalation; Windows uses single SIGKILL",
    )
    def test_posix_sigterm_then_sigkill(self) -> None:
        """POSIX path: real child reaped via SIGTERM→waitpid→SIGKILL loop.

        Spawns a real short-lived sleep subprocess, drives _sync_kill_provider
        through the POSIX escalation loop (kill_pid is recorded, not real, so
        the loop runs both iterations deterministically), then reaps the child.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
            provider._client = None
            provider._proc = MagicMock()
            provider._proc.returncode = None
            provider._proc.pid = proc.pid
            provider._active_proc = None

            sigs: list[int] = []

            def fake_kill(pid: int, sig: int) -> bool:
                sigs.append(sig)
                return True

            with patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=fake_kill,
            ):
                _sync_kill_provider(provider)

            # POSIX loop hits both SIGTERM and SIGKILL for our child PID
            assert sigs == [platform_compat.SIGTERM, platform_compat.SIGKILL]
        finally:
            proc.kill()
            proc.wait(timeout=5)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX SIGTERM path; Windows takes the single-SIGKILL branch",
    )
    def test_posix_already_dead_on_sigterm(self) -> None:
        """ProcessLookupError on first signal → early return (already dead)."""
        from kiro_crew.session_pid import _sync_kill_provider

        provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        provider._client = None
        provider._proc = None
        provider._active_proc = MagicMock()
        provider._active_proc.returncode = None
        provider._active_proc.pid = 99999

        sigs: list[int] = []

        def fake_kill(pid: int, sig: int) -> bool:
            sigs.append(sig)
            raise ProcessLookupError()

        with patch(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            side_effect=fake_kill,
        ):
            _sync_kill_provider(provider)

        # Loop stops after the first (SIGTERM) signal raises ProcessLookupError
        assert sigs == [platform_compat.SIGTERM]


class TestCleanupOrphanedMcpServersExtra:
    def test_bare_pid_non_numeric_skipped(self, pid_file: Path) -> None:
        """A bare (no-colon) line that is not an int is skipped via ValueError."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("not_a_number\n")

        killed = _cleanup_orphaned_mcp_servers()

        assert killed == 0
        # Malformed bare line is left in place (continue, not pruned)
        assert "not_a_number" in pid_file.read_text()

    def test_orphan_kill_oserror_swallowed(self, pid_file: Path) -> None:
        """kill_pid raising OSError on an orphaned child is swallowed; entry pruned."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("77777:99999\n")  # parent 99999 dead

        def fake_pid_exists(pid: int) -> bool:
            return pid == 77777  # child alive, parent dead

        def fake_kill(pid: int, sig: int) -> bool:
            raise OSError("kill failed")

        with (
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=fake_pid_exists,
            ),
            patch("kiro_crew.platform_compat.get_ppid", return_value=1),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=fake_kill,
            ),
        ):
            killed = _cleanup_orphaned_mcp_servers()

        # kill raised → not counted, but the entry is still pruned
        assert killed == 0
        assert "77777" not in pid_file.read_text()


class TestPidGoneOrUnmanaged:
    """`_pid_gone_or_unmanaged` decides whether it is safe to untrack a PID.

    Safe (True) only when the process is confirmed gone. Any PID still alive or
    unsignalable returns False (retain) so a survivor of a failed teardown keeps
    its tracking entry and the orphan sweep reaps it. Fork note: routes through
    ``platform_compat.pid_liveness`` (Windows-safe) rather than raw
    ``os.kill(pid, 0)``, so — unlike upstream 33da30e6 — an EPERM/unsignalable
    PID is RETAINED (fail-safe), not untracked.

    The probe is mocked at ``platform_compat.pid_liveness`` (not a real dead
    PID): a raw ``os.kill(pid, 0)`` walk to *find* a dead PID would itself be
    the Windows-terminates-the-target footgun this fork forbids.
    """

    def test_dead_pid_is_safe_to_untrack(self) -> None:
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        with patch(
            "kiro_crew.platform_compat.pid_liveness",
            return_value=platform_compat.PID_DEAD,
        ):
            assert _pid_gone_or_unmanaged(4242) is True

    def test_live_pid_under_our_uid_is_retained(self) -> None:
        # A live PID under our uid is retained (False) regardless of whether it
        # is a managed agent: it may be an un-reaped survivor, and the periodic
        # sweep re-validates ownership before reaping. This is the fail-safe
        # direction — we never untrack something that is still alive here.
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        assert _pid_gone_or_unmanaged(os.getpid()) is False

    def test_unsignalable_pid_is_retained(self) -> None:
        # Fork divergence from upstream: pid_liveness collapses POSIX EPERM into
        # PID_UNSIGNALABLE, which we treat as "retain" (the sweep re-validates
        # ownership off the hot path). Never orphaning a live survivor is the
        # invariant; a retained-but-recycled PID is harmless.
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        with patch(
            "kiro_crew.platform_compat.pid_liveness",
            return_value=platform_compat.PID_UNSIGNALABLE,
        ):
            assert _pid_gone_or_unmanaged(4242) is False

    def test_alive_pid_is_retained(self) -> None:
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        with patch(
            "kiro_crew.platform_compat.pid_liveness",
            return_value=platform_compat.PID_ALIVE,
        ):
            assert _pid_gone_or_unmanaged(4242) is False


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
    """/proc/<pid>/environ positive-identity read."""

    def test_non_linux_fails_closed(self) -> None:
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        with patch("kiro_crew.session_pid.sys") as mock_sys:
            mock_sys.platform = "darwin"
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
        import time

        from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
        from kiro_crew.session_pid import _env_has_kirocrew_marker

        env = {**os.environ, KIROCREW_SPAWNED_ENV: KIROCREW_SPAWNED_VALUE}
        proc = subprocess.Popen(["sleep", "30"], env=env)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if _env_has_kirocrew_marker(proc.pid):
                    break
                time.sleep(0.05)
            assert _env_has_kirocrew_marker(proc.pid) is True
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
        ):
            mock_sys.platform = "linux"
            killed = kill_orphan_mcps([720])

        assert killed == 1
        mock_killpg.assert_called_once_with(720, signal.SIGKILL)

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

    def test_spawn_site_source_registry(self) -> None:
        """Drift guard: the marker constant must appear at every known
        provider/MCP spawn-env build site. A new spawn site that replaces the
        inherited environment must add itself here AND inject the marker.

        The fork is KiroACP-only, so upstream's ``providers/claude_code.py``
        spawn site is intentionally absent from this list (the module is
        deleted in the public fork)."""
        src_root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        spawn_sites = [
            "sandbox.py",
            "acp/runtime.py",
            "acp/client.py",
            "mcp_gateway/backend.py",
        ]
        for rel in spawn_sites:
            content = (src_root / rel).read_text(encoding="utf-8")
            assert "KIROCREW_SPAWNED_ENV" in content, (
                f"{rel} no longer injects the KIROCREW_SPAWNED marker — "
                "escaped MCP trees from this site become unsweepable"
            )
