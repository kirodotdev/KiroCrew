"""Tests for PID tracking and orphan cleanup in session.py."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from kiro_crew import platform_compat

# These tests exercise POSIX-only process-management semantics: process-group
# APIs (os.killpg / os.getpgrp / os.getpgid), POSIX identity/age probes
# (os.getuid / os.sysconf, /proc, ps), the raw signal.SIGKILL constant, and the
# POSIX kill path of the orphan sweep (which no-ops on Windows). None of these
# have a Windows equivalent, so they are skipped on Windows. See issue #2041.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process-management semantics only; see issue #2041"
)


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
        assert "12345" in pid_file.read_text(encoding="utf-8")

    def test_track_multiple(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid

        _track_pid(111)
        _track_pid(222)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["111", "222"]

    def test_untrack_pid(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        _track_pid(111)
        _track_pid(222)
        _untrack_pid(111)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["222"]

    def test_untrack_nonexistent(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        _track_pid(111)
        _untrack_pid(999)  # should not crash
        assert "111" in pid_file.read_text(encoding="utf-8")

    @pytest.mark.parametrize("token", [None, "tok123"])
    def test_untrack_session_pid(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        token: str | None,
    ) -> None:
        """Untrack removes only the named PID's entry, in either record format.

        Pin the start token instead of inheriting it from the host: PIDs 111
        and 222 are live kernel threads on some machines (so _track writes the
        3-field ``gw:pid:token``) and absent on others (2-field ``gw:pid``),
        which would otherwise make the expected value host-dependent.
        """
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: token)
        _track_session_pid(111)
        _track_session_pid(222)
        _untrack_session_pid(111)
        gw = os.getpid()
        suffix = f":{token}" if token else ""
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{gw}:222{suffix}"]

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
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["99999:111"]

    def test_track_child_pids_with_parent(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_child_pids

        _track_child_pids({100: None, 200: None, 300: None}, parent_pid=999)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert set(lines) == {"100:999", "200:999", "300:999"}

    def test_track_child_pids_dedup(self, pid_file: Path) -> None:
        """Duplicate child:parent entries should not be written."""
        from kiro_crew.session_pid import _track_child_pids

        _track_child_pids({100: None, 200: None}, parent_pid=999)
        _track_child_pids({100: None, 300: None}, parent_pid=999)
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert sorted(lines) == ["100:999", "200:999", "300:999"]

    def test_untrack_child_pids(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _track_child_pids, _untrack_child_pids

        _track_child_pids({100: None, 200: None, 300: None}, parent_pid=999)
        _untrack_child_pids({100: None, 300: None})
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == ["200:999"]

    def test_untrack_child_pids_preserves_bare_pid(self, pid_file: Path) -> None:
        """Untracking child PIDs must not remove bare PID lines (kiro-cli parents)."""
        from kiro_crew.session_pid import _track_child_pids, _track_pid, _untrack_child_pids

        _track_pid(100)  # bare parent line
        _track_child_pids({100: None}, parent_pid=999)  # child line with same PID
        _untrack_child_pids({100: None})
        lines = pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert "100" in lines  # bare line preserved


class TestCleanupOrphanedMcpServers:
    def test_dead_child_pruned(self, pid_file: Path) -> None:
        """Dead child PIDs should be removed from the file silently."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("99999:1\n")  # child=99999, parent=1
        _cleanup_orphaned_mcp_servers()
        assert "99999" not in pid_file.read_text(encoding="utf-8")

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
        assert str(child_pid) in pid_file.read_text(encoding="utf-8")

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
        assert "77777" not in pid_file.read_text(encoding="utf-8")  # stale entry pruned

    def test_bare_pid_dead_pruned(self, pid_file: Path) -> None:
        """Dead bare PIDs should be pruned from the file."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("99999\n")

        with patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=False):
            killed = _cleanup_orphaned_mcp_servers()
        assert killed == 0
        assert "99999" not in pid_file.read_text(encoding="utf-8")

    def test_bare_pid_alive_kept(self, pid_file: Path) -> None:
        """Alive bare PIDs should be kept in the file."""
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("88888\n")

        with patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True):
            killed = _cleanup_orphaned_mcp_servers()
        assert killed == 0
        assert "88888" in pid_file.read_text(encoding="utf-8")

    def test_empty_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        pid_file.write_text("")
        assert _cleanup_orphaned_mcp_servers() == 0

    def test_no_file(self, pid_file: Path) -> None:
        from kiro_crew.session_pid import _cleanup_orphaned_mcp_servers

        assert _cleanup_orphaned_mcp_servers() == 0


class TestCleanupOrphanedSessions:
    @_POSIX_ONLY
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
        content = session_pid_file.read_text(encoding="utf-8")
        assert content == ""

    @_POSIX_ONLY
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
        # _reset_state restarts the per-process cost baseline on this object
        # (always present in production: __init__ assigns it unconditionally).
        from kiro_crew.acp.types import AcpPromptStats

        client.last_prompt_stats = AcpPromptStats()
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


# ── Untracked orphan MCP sweep tests ───────────


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

    def test_vanished_pid_logs_one_line_without_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A PID that exits between snapshot and probe logs no stack trace.

        The candidate is already gone, which is what the sweep wants, so the
        expected TOCTOU race must not emit exc_info.
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[22620]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(
                Path,
                "read_bytes",
                side_effect=FileNotFoundError(2, "No such file or directory"),
            ),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "22620" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is None
        assert "vanished before probe" in records[0].getMessage()

    def test_vanished_pid_on_macos_ps_exit_logs_no_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`ps -p <dead-pid>` exits non-zero — same race, same quiet handling."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[9140]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch(
                "kiro_crew.session_pid.subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "ps"),
            ),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "darwin"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "9140" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is None

    def test_unexpected_probe_error_keeps_traceback(self, caplog: pytest.LogCaptureFixture) -> None:
        """A genuinely unexpected probe failure still logs exc_info."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[555]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", side_effect=PermissionError(13, "denied")),
            patch("os.getpid", return_value=1),
            caplog.at_level("DEBUG", logger="kiro_crew.session_pid"),
        ):
            mock_sys.platform = "linux"
            result = find_orphan_mcp_candidates(active_pids=set())

        assert result == []
        records = [r for r in caplog.records if "555" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None

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

    def test_excludes_peer_gateway(self, tmp_path: Path) -> None:
        """Peer gateways with a LIVE socket path are never candidates.

        Age is patched above the min-age floor so the assertion depends on the
        _GATEWAY_MARKERS exclusion in _is_orphan_mcp, not on the age guard
        short-circuiting before the exclusion logic ever runs. The socket path
        must exist on disk: a gatewayd whose socket is GONE is deliberately
        sweepable via the reachability path (_is_sweepable_orphan_gatewayd).
        """
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        live_sock = tmp_path / "gw.sock"
        live_sock.write_text("")
        cmdline = (
            b"python3\x00-m\x00kiro_crew.mcp_gateway.gatewayd"
            b"\x00--socket\x00" + os.fsencode(str(live_sock))
        )
        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[360]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=cmdline),
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

        Regression guard: the upstream project's reaper lists ``builder-mcp`` (an
        internal server it manages), but the de-Amazoned fork never spawns
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
        assert "not_a_number" in pid_file.read_text(encoding="utf-8")

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
        assert "77777" not in pid_file.read_text(encoding="utf-8")


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

    def test_dead_leader_means_session_ended(self) -> None:
        """Leader gone (or PID recycled into a non-leader) -> session ended."""
        from kiro_crew.session_pid import _work_orphan_session_leader_alive

        def fake_sid(pid: int) -> int:
            return 500 if pid == 1234 else -1  # leader 500 unreadable = gone

        with patch("kiro_crew.session_pid._linux_pid_sid", side_effect=fake_sid):
            assert _work_orphan_session_leader_alive(1234) is False

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


# ── PID-recycle identity guard + cross-platform spawn grace ───────────
# Regression cover for the quit->reopen race reproduced on macOS 2026-07-29:
# a stale ``<dead_gw>:<pid>`` entry whose PID had been recycled onto a LIVE
# kiro-cli was SIGKILL'd by the startup sweep (surfacing to the user as
# "process exited (rc=None)"), because the file sweep verified only the
# cmdline and the spawn-grace window was silently Linux-only.


class TestPidStartTokenIdentityGuard:
    def test_track_session_pid_records_start_token(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Entries carry ``gw:pid:token`` so the sweep can verify identity."""
        from kiro_crew.session_pid import _track_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242:tok123"

    def test_track_session_pid_falls_back_when_token_unavailable(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No token (Windows / ps failure) → legacy 2-field entry."""
        from kiro_crew.session_pid import _track_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: None)
        _track_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == f"{os.getpid()}:4242"

    def test_track_session_pid_dedups_across_formats(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy entry must not be duplicated by a token-bearing re-track."""
        from kiro_crew.session_pid import _track_session_pid

        session_pid_file.write_text(f"{os.getpid()}:4242\n")
        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        lines = session_pid_file.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"{os.getpid()}:4242"]

    def test_untrack_session_pid_removes_token_entry(
        self, session_pid_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Untrack matches the token-bearing form, not just the legacy one."""
        from kiro_crew.session_pid import _track_session_pid, _untrack_session_pid

        monkeypatch.setattr("kiro_crew.session_pid._pid_start_token", lambda p: "tok123")
        _track_session_pid(4242)
        _untrack_session_pid(4242)
        assert session_pid_file.read_text(encoding="utf-8").strip() == ""

    def test_recycled_pid_is_pruned_not_killed(self, session_pid_file: Path) -> None:
        """THE regression: token mismatch → prune the stale entry, never kill.

        The PID is live and its cmdline matches an agent, so every pre-existing
        guard passes; only the start-token comparison catches the recycle.
        """
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        # Dead gateway (999999) : live child PID, recorded with an OLD token.
        session_pid_file.write_text("999999:99998:oldtoken\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # Live process now reports a DIFFERENT token → PID was recycled.
            patch("kiro_crew.session_pid._pid_start_token", return_value="newtoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            # Grace disabled so the ONLY thing that can save the process is the
            # identity check under test.
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert kills == [], f"recycled PID was killed: {kills}"

    def test_unreadable_token_retains_entry(self, session_pid_file: Path) -> None:
        """Unknown identity must NOT prune: pruning would leak a live orphan.

        Every sweep keys off this file, so untracking a live process on one
        transient probe failure orphans it permanently (the fail-safe stated in
        _pid_gone_or_unmanaged: "any inconclusive result retains").
        """
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        entry = "999999:99998:recorded-token"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            # Identity unreadable (probe failure) — neither match nor mismatch.
            patch("kiro_crew.session_pid._pid_start_token", return_value=None),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert kills == [], "killed a process whose identity could not be verified"

    def test_session_roots_unreadable_token_retains_entry(self, session_pid_file: Path) -> None:
        """Same fail-safe in the periodic root sweep: retain, don't kill or drop."""
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        entry = "999999:99998:recorded-token"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value=None),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert kills == [], "killed a process whose identity could not be verified"
        # Entry retained so the next sweep can retry.
        assert entry in session_pid_file.read_text(encoding="utf-8")

    @_POSIX_ONLY
    def test_session_roots_subreaper_reparent_still_killed(self, session_pid_file: Path) -> None:
        """A recorded start token that MATCHES proves identity on its own.

        Orphans do not always reparent to init: a process placed in its own
        cgroup scope by the service manager reparents to that *user manager*,
        which is a subreaper. Treating any other PPid as "the PID was recycled"
        both spares the real orphan AND drops its tracking entry, so nothing
        ever reaps it again. The token is strictly stronger evidence of identity
        than the parent, so it must not be vetoed by the PPid heuristic.
        """
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        entry = "999999:99998:sametoken"
        session_pid_file.write_text(entry + "\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            # The subreaper that adopted the orphan -- neither init(1), nor the
            # dead gateway PID, nor the -1 probe-failure sentinel.
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=7447),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert (
            99998,
            platform_compat.SIGKILL,
        ) in kills, "a token-verified orphan adopted by a subreaper was not reaped"
        # And it must not be silently untracked, which is what leaks it forever.
        assert entry not in session_pid_file.read_text(encoding="utf-8")

    @_POSIX_ONLY
    def test_matching_token_still_killed(self, session_pid_file: Path) -> None:
        """A genuine orphan (token matches) is still reaped — no regression."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("999999:99998:sametoken\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert (99998, platform_compat.SIGKILL) in kills

    @_POSIX_ONLY
    def test_legacy_entry_without_token_still_swept(self, session_pid_file: Path) -> None:
        """Back-compat: a 2-field entry keeps its old cmdline+grace behavior."""
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        session_pid_file.write_text("999999:99998\n")
        kills: list[tuple[int, int]] = []

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.pid_liveness",
                return_value=platform_compat.PID_ALIVE,
            ),
            # The owning gateway (999999) must read as DEAD or _skip_tagged
            # skips the entry and the test passes vacuously; the child is alive.
            patch(
                "kiro_crew.session_pid.platform_compat.pid_exists",
                side_effect=lambda p: p != 999999,
            ),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
            patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
            patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
        ):
            cleanup_orphaned_sessions()

        assert (99998, platform_compat.SIGKILL) in kills

    @_POSIX_ONLY
    def test_session_roots_sweep_parses_token_entry(self, session_pid_file: Path) -> None:
        """cleanup_orphaned_session_roots must not mis-prune 3-field entries.

        A ``split(":", 1)`` parse would int("99998:tok") -> ValueError and prune
        the entry, silently dropping every token-bearing line from the sweep.
        """
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        session_pid_file.write_text("999999:99998:sametoken\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            # Owning gateway dead; child alive.
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="sametoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert (99998, platform_compat.SIGKILL) in kills

    def test_session_roots_sweep_spares_recycled_pid(self, session_pid_file: Path) -> None:
        """Token mismatch in the periodic root sweep → prune, never kill."""
        from kiro_crew.session_pid import cleanup_orphaned_session_roots

        session_pid_file.write_text("999999:99998:oldtoken\n")
        kills: list[tuple[int, int]] = []

        def fake_liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
            patch("kiro_crew.session_pid._pid_start_token", return_value="newtoken"),
            patch("kiro_crew.session_pid.platform_compat.pid_liveness", side_effect=fake_liveness),
            patch("kiro_crew.session_pid.platform_compat.get_ppid", return_value=1),
            patch("kiro_crew.session_pid.platform_compat.pid_exists", return_value=True),
            patch(
                "kiro_crew.session_pid.platform_compat.kill_pid",
                side_effect=lambda p, s: kills.append((p, s)),
            ),
        ):
            cleanup_orphaned_session_roots()

        assert kills == [], f"recycled PID was killed: {kills}"


class TestSpawnGraceCrossPlatform:
    @_POSIX_ONLY
    def test_grace_applies_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: the grace window was Linux-only, so macOS never got it."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: 5.0)
        assert sp._pid_in_spawn_grace(4242) is True

    def test_old_process_not_in_grace_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: sp.SWEEP_SPAWN_GRACE_SECONDS + 1)
        assert sp._pid_in_spawn_grace(4242) is False

    @_POSIX_ONLY
    def test_unknown_age_treated_as_young(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unreadable age → safe direction (skip the kill)."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp, "_pid_age_seconds", lambda p: None)
        assert sp._pid_in_spawn_grace(4242) is True

    def test_macos_age_derived_from_start_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS age comes from the in-process start id — no subprocess/ps."""
        import time as _time

        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", False)
        start = _time.time() - 90.0
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda p: f"{start:.6f}")
        age = sp._pid_age_seconds(4242)
        assert age is not None and 85.0 <= age <= 95.0

    def test_macos_age_none_when_identity_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.sys, "platform", "darwin")
        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda p: None)
        assert sp._pid_age_seconds(4242) is None

    def test_windows_has_no_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows keeps prior behavior (no age source, sweep stays functional)."""
        import kiro_crew.session_pid as sp

        monkeypatch.setattr(sp.platform_compat, "IS_WINDOWS", True)
        assert sp._pid_in_spawn_grace(4242) is False


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: relies on fork/exec + ps for identity"
)
class TestSweepSparesLiveProcess:
    """End-to-end repro of the 2026-07-29 macOS incident with a REAL process.

    The mock-based tests above pin the decision logic; this one proves the
    whole sweep leaves an actually-running process alive. The victim is a
    short-lived ``sleep`` renamed via ``_is_managed_agent_process`` patching,
    so no kiro-cli is required and nothing user-owned is at risk.
    """

    def test_live_process_with_recycled_entry_survives(
        self, session_pid_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.session_pid import cleanup_orphaned_sessions

        monkeypatch.setattr("kiro_crew.session_pid.config_dir", lambda: tmp_path)
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # Stale entry from a DEAD gateway naming the live PID, with a token
            # that cannot match the live process (the recycle signature).
            session_pid_file.write_text(f"999999:{victim.pid}:stale-token-does-not-match\n")

            with (
                # Cmdline check passes (as it did in the real incident).
                patch("kiro_crew.session_pid._is_managed_agent_process", return_value=True),
                # The owning gateway (999999) must read as DEAD, or `_skip_tagged`
                # keeps the entry and the prune assertion below fails. Pinned rather
                # than assumed: 999999 is a perfectly ordinary live PID on a host
                # whose counter has passed it (`pid_max` is 4194304 here), which made
                # this a load-dependent flake rather than a constant failure. Only
                # that one PID is faked -- every other, the live victim included,
                # still goes to the real probe.
                patch(
                    "kiro_crew.session_pid.platform_compat.pid_exists",
                    side_effect=lambda p: p != 999999 and platform_compat.pid_exists(p),
                ),
                # Grace disabled: isolate the identity check as the sole guard.
                patch("kiro_crew.session_pid._pid_in_spawn_grace", return_value=False),
                patch("kiro_crew.session_pid._cleanup_orphaned_mcp_servers", return_value=0),
            ):
                cleanup_orphaned_sessions()

            assert victim.poll() is None, "sweep SIGKILLed a live process (the bug)"
            # And the stale entry is pruned so it can't re-trigger next boot.
            assert str(victim.pid) not in session_pid_file.read_text(encoding="utf-8")
        finally:
            victim.kill()
            victim.wait()


class TestPidFileRewriteIsAtomic:
    """A PID-file rewrite must be atomic AND must never propagate its failure.

    Atomic: ``Path.write_text`` truncates the target to zero BEFORE writing the
    kept entries. A failure — or a hard kill — inside that window leaves a SHORT
    file whose surviving content is still perfectly well-formed: nothing raised,
    nothing logged, and every dropped entry is an agent runtime that no reaper
    can ever find again, because these PID files are the ONLY record of which
    runtimes this gateway owns.

    Reported, not propagated: pruning an entry is idempotent and self-retrying,
    so a failed rewrite costs one stale line. Propagating would cost the whole
    gateway — ``cleanup_orphaned_sessions`` runs unguarded on the startup path,
    and on Windows ``replace_with_retry`` declines to retry a sharing violation
    while an event loop is running.

    These tests fail the rename and then assert the original file is untouched,
    which a truncating writer cannot satisfy because by then it has already
    destroyed the original.
    """

    @staticmethod
    def _fail_rename(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    @staticmethod
    def _assert_reported(caplog: pytest.LogCaptureFixture) -> None:
        assert any(
            r.levelno >= logging.ERROR and "Could not rewrite PID file" in r.getMessage()
            for r in caplog.records
        ), "a failed PID-file rewrite must be reported at ERROR, never silently"

    def test_write_back_failure_preserves_every_entry(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _write_back_pid_file

        original = "100:200:tokA\n101:201:tokB\n102:202:tokC\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _write_back_pid_file({"101:201:tokB"})

        # The rewrite never landed, so the ledger must still name all three
        # runtimes. A truncating writer leaves only two — and the two it leaves
        # look entirely valid, which is what makes the loss silent.
        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_session_pid_failure_preserves_every_entry(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_session_pid

        gw = os.getpid()
        original = f"{gw}:900:tokX\n{gw}:901:tokY\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_session_pid(900)

        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_pid_failure_preserves_every_entry(
        self,
        pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_pid

        original = "700\n701\n702\n"
        pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_pid(701)

        assert pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_untrack_child_pids_failure_preserves_every_entry(
        self,
        pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.session_pid import _untrack_child_pids

        original = "800:1\n801:1\n"
        pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.atomic_write.replace_with_retry", self._fail_rename)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _untrack_child_pids({801: object()})

        assert pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    @pytest.mark.asyncio
    async def test_windows_loop_sharing_violation_does_not_abort_caller(
        self,
        session_pid_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The startup path survives a Windows sharing violation on the rename.

        ``replace_with_retry`` deliberately refuses to sleep-retry while an event
        loop is running, so on Windows a scanner holding the temp file surfaces
        as an immediate ``PermissionError``. ``cleanup_orphaned_sessions`` calls
        this rewrite unguarded during gateway start, so the error escaping here
        would abort startup.
        """
        from kiro_crew.session_pid import _write_back_pid_file

        def _sharing_violation(*_a: object, **_kw: object) -> None:
            raise PermissionError(32, "The process cannot access the file")

        original = "100:200:tokA\n101:201:tokB\n"
        session_pid_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr("kiro_crew.platform_compat.IS_WINDOWS", True)
        monkeypatch.setattr("kiro_crew.atomic_write.os.replace", _sharing_violation)

        # Runs with a live event loop, which is what disables the retry.
        with caplog.at_level(logging.ERROR, logger="kiro_crew.session_pid"):
            _write_back_pid_file({"101:201:tokB"})

        assert session_pid_file.read_text(encoding="utf-8") == original
        self._assert_reported(caplog)

    def test_successful_rewrite_lands_and_leaves_no_temp_residue(
        self, session_pid_file: Path
    ) -> None:
        from kiro_crew.session_pid import _write_back_pid_file

        session_pid_file.write_text("100:200:tokA\n101:201:tokB\n", encoding="utf-8")

        _write_back_pid_file({"101:201:tokB"})

        assert session_pid_file.read_text(encoding="utf-8") == "100:200:tokA\n"
        # atomic_write's mkstemp companion must not survive the rename.
        assert not list(session_pid_file.parent.glob("*.tmp"))

    def test_no_truncating_writer_remains_in_session_pid(self) -> None:
        """Ratchet: every PID-file rewrite goes through the atomic chokepoint."""
        import kiro_crew.session_pid as sp

        source = Path(str(sp.__file__)).read_text(encoding="utf-8")
        assert ".write_text(" not in source, (
            "session_pid.py must rewrite PID files through _rewrite_pid_file(): "
            "Path.write_text truncates the file before writing, so a failure "
            "mid-write silently drops entries and leaks their runtimes until "
            "the host reboots."
        )


# ── Untracked managed-agent runtime orphan (REPORT-ONLY, issue #2930) ──


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
        from kiro_crew.session_pid import _tracked_agent_pids

        assert _tracked_agent_pids() == set()

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
        """The regression this fixes: the leak was previously silent.

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
        exact silence issue #2930 reports.
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


# ── Orphaned playwright-cli browser daemon sweep (issue #5986) ───────────────

#: A realistic NUL-separated cliDaemon argv. playwright-core spawns the daemon
#: as ``node <...>/entry/cliDaemon.js <sessionName> [flags]`` (see
#: cli-client/session.js ``startDaemon``), so the session name is the argv
#: element immediately after the entry script.
_DAEMON_CMDLINE = (
    b"/usr/bin/node\x00"
    b"/home/u/.npm/_npx/e41f/node_modules/playwright-core/lib/entry/cliDaemon.js\x00"
    b"kc-1a2b3c4d\x00--headed"
)
_OPERATOR_DAEMON_CMDLINE = (
    b"/usr/bin/node\x00"
    b"/home/u/.npm/_npx/e41f/node_modules/playwright-core/lib/entry/cliDaemon.js\x00"
    b"chrome"
)


class TestBrowserDaemonSessionArg:
    """Structural extraction of the generated session name from daemon argv."""

    def test_extracts_generated_session_name(self) -> None:
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_DAEMON_CMDLINE) == b"kc-1a2b3c4d"

    def test_rejects_operator_named_session(self) -> None:
        """Only Kiro-Crew-generated ``kc-<8hex>`` names are ever sweepable."""
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_OPERATOR_DAEMON_CMDLINE) is None

    def test_rejects_space_joined_cmdline(self) -> None:
        """A ps-style space-joined cmdline cannot delimit argv safely."""
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(_DAEMON_CMDLINE.replace(b"\x00", b" ")) is None

    def test_rejects_non_daemon_cmdline(self) -> None:
        from kiro_crew.session_pid import _browser_daemon_session_arg

        assert _browser_daemon_session_arg(b"/usr/bin/node\x00server.js\x00kc-1a2b3c4d") is None

    def test_session_env_name_matches_launch_module(self) -> None:
        """Drift ratchet: the local constant must track the real env var."""
        from kiro_crew.browser_cli.launch import SESSION_ENV
        from kiro_crew.session_pid import _BROWSER_SESSION_ENV

        assert _BROWSER_SESSION_ENV == SESSION_ENV


class TestBrowserDaemonOrphanSweep:
    """A stranded generated-session daemon is reclaimed; a live one never is."""

    def test_dead_owner_daemon_is_a_candidate(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[800]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == [800]

    def test_live_owner_daemon_is_never_a_candidate(self) -> None:
        """The safety invariant: a live agent's browser is never reclaimed."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[801]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=True,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_operator_session_daemon_is_never_a_candidate(self) -> None:
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[802]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_OPERATOR_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"chrome"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_unmarked_daemon_is_never_a_candidate(self) -> None:
        """No ``KIROCREW_SPAWNED`` marker means we did not spawn this tree."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[803]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=False),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_young_daemon_is_never_a_candidate(self) -> None:
        """The work-class age floor applies: a fresh daemon is never raced."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[804]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=200.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-1a2b3c4d"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []

    def test_env_argv_session_mismatch_is_never_a_candidate(self) -> None:
        """argv name must be the generated name this process was exec'd with."""
        from kiro_crew.session_pid import find_orphan_mcp_candidates

        with (
            patch("kiro_crew.session_pid._our_orphan_pids", return_value=[805]),
            patch("kiro_crew.session_pid.sys") as mock_sys,
            patch.object(Path, "read_bytes", return_value=_DAEMON_CMDLINE),
            patch("os.getpid", return_value=1),
            patch("kiro_crew.session_pid._linux_pid_age", return_value=900.0),
            patch("kiro_crew.session_pid._env_has_kirocrew_marker", return_value=True),
            patch("kiro_crew.session_pid._env_value", return_value=b"kc-99999999"),
            patch(
                "kiro_crew.session_pid._browser_session_owner_alive",
                return_value=False,
            ),
        ):
            mock_sys.platform = "linux"
            assert find_orphan_mcp_candidates(active_pids=set()) == []


class TestBrowserSessionOwnerAlive:
    """The ownership probe reads only exec-time environ, never on-disk state."""

    def test_live_peer_holding_the_session_reads_as_alive(self) -> None:
        from kiro_crew import session_pid as sp

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/900"), Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=1),
            patch.object(sp, "_env_value", return_value=b"kc-1a2b3c4d"),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True

    def test_only_the_daemons_own_tree_reads_as_dead(self) -> None:
        """Chromium children share the daemon's SID and are not owners."""
        from kiro_crew import session_pid as sp

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/900"), Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=900),
            patch.object(sp, "_env_value", return_value=b"kc-1a2b3c4d"),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is False

    def test_unreadable_peer_fails_closed_to_alive(self) -> None:
        from kiro_crew import session_pid as sp

        def _boom(pid: int, key: str) -> bytes | None:
            raise PermissionError("inconclusive")

        with (
            patch.object(sp, "sys") as mock_sys,
            patch.object(Path, "iterdir", return_value=[Path("/proc/901")]),
            patch.object(Path, "stat", return_value=Mock(st_uid=os.getuid())),
            patch.object(sp, "_linux_pid_sid", return_value=1),
            patch.object(sp, "_env_value", side_effect=_boom),
        ):
            mock_sys.platform = "linux"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True

    def test_non_linux_fails_closed_to_alive(self) -> None:
        from kiro_crew import session_pid as sp

        with patch.object(sp, "sys") as mock_sys:
            mock_sys.platform = "darwin"
            assert sp._browser_session_owner_alive(900, b"kc-1a2b3c4d") is True
