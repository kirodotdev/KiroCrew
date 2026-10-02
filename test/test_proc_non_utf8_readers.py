"""The remaining readers answer for a process whose name is not UTF-8.

The companion of ``test_proc_non_utf8_kill_paths.py`` for the readers outside
the teardown, sweep and reclaim paths: the runtime tree's start-time guard, the
terminal title, the diagnostics recorder's thread count. A process named
:data:`non_utf8_comm.BAD_COMM` has a ``stat``, ``status`` and ``comm`` that are
not valid UTF-8, and each of these reads them without a strict decode.

The PID tracking files are here too: they hold ASCII, so a byte in one that is
not UTF-8 is damage, and every reader treats it as a malformed entry.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest
from non_utf8_comm import BAD_COMM, comm_is_settable, renamed_child

from kiro_crew import platform_compat, runtime_reconcile
from kiro_crew import session_pid as sp
from kiro_crew.acp import runtime_process_tree as tree
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.diag import recorder

live_rename = pytest.mark.skipif(not comm_is_settable(), reason="renames a real Linux process")


@live_rename
class TestALiveRenamedProcess:
    def test_its_start_time_guards_the_runtime_tree(self) -> None:
        with renamed_child() as (child, _):
            start = platform_compat.process_start_time(child)
            assert start is not None
            assert tree._get_start_time(child) == int(start)

    def test_its_terminal_title_is_a_string(self) -> None:
        with renamed_child() as (child, _):
            title = terminal._proc_comm(child)
            assert title is not None and title.startswith("run_")


def test_a_terminal_title_is_decoded_with_replace() -> None:
    with patch("builtins.open", mock_open(read_data=BAD_COMM + b"\n")):
        assert terminal._proc_comm(42) == BAD_COMM.decode("utf-8", "replace")


def test_the_recorder_reads_its_thread_count(tmp_path: Path) -> None:
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "status").write_bytes(b"Name:\t" + BAD_COMM + b"\nThreads:\t7\n")
    assert recorder._read_self_process(tmp_path)["threads"] == 7


class TestADamagedTrackingFile:
    """A byte that is not UTF-8 is a malformed entry to every reader, never a raise."""

    @pytest.fixture()
    def files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        session_file = tmp_path / "kiro_session_pids.txt"
        child_file = tmp_path / "kiro_pids.txt"
        monkeypatch.setattr(sp, "_session_pid_file_path", lambda: session_file)
        monkeypatch.setattr(sp, "_pid_file_path", lambda: child_file)
        monkeypatch.setattr(sp, "_PID_FILES_WARNED_UNDECODABLE", set(), raising=False)
        return session_file, child_file

    def test_the_tracked_snapshot_is_incomplete_and_warns_once(
        self, files: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        session_file, child_file = files
        session_file.write_bytes(b"10:11\n")
        child_file.write_bytes(b"31:32\n\xff3:1\n")  # the second entry's pid is damaged
        with caplog.at_level(logging.WARNING, logger=sp.logger.name):
            assert sp._read_tracked_agent_pids() == ({11, 31}, False)
            sp._read_tracked_agent_pids()
        warned = [r for r in caplog.records if "not valid UTF-8" in r.getMessage()]
        assert len(warned) == 1 and str(child_file) in warned[0].getMessage()
        assert sp.tracked_agent_pid_owners() == {11: 10, 31: 32}

    def test_the_boot_sweep_completes(
        self, files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session_file, child_file = files
        session_file.write_bytes(b"1:\xff\xfe\n")
        child_file.write_bytes(b"\xff:1\n")
        kill = MagicMock()
        monkeypatch.setattr(sp.platform_compat, "kill_pid", kill)
        sp.cleanup_orphaned_sessions(narrow_with_leaders=False)
        kill.assert_not_called()
        assert session_file.read_bytes() == b""  # a malformed session entry is pruned

    def test_spawn_tracking_still_records_and_rewrites_cleanly(
        self, files: tuple[Path, Path]
    ) -> None:
        _session_file, child_file = files
        child_file.write_bytes(b"\xff:1\n")
        sp._track_child_pids({os.getpid(): None}, parent_pid=1)
        assert f"{os.getpid()}:1" in child_file.read_text(encoding="utf-8", errors="replace")
        sp._untrack_child_pids({os.getpid(): None})
        child_file.read_text(encoding="utf-8")  # the rewrite is valid UTF-8

    def test_a_rewritten_file_is_not_reported_undecodable_again(
        self,
        files: tuple[Path, Path],
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rewrite stores the U+FFFD as valid UTF-8 and keeps the damaged row.

        The next process reads a valid UTF-8 file holding a malformed row, so it
        must not log that the file is not valid UTF-8.
        """
        _session_file, child_file = files
        child_file.write_bytes(b"\xff3:1\n31:32\n")
        with caplog.at_level(logging.WARNING, logger=sp.logger.name):
            sp._untrack_child_pids({31: None})
            assert [r for r in caplog.records if "not valid UTF-8" in r.getMessage()]
            assert child_file.read_bytes() == "\ufffd3:1\n".encode("utf-8")
            monkeypatch.setattr(sp, "_PID_FILES_WARNED_UNDECODABLE", set())  # a new process
            caplog.clear()
            assert sp._read_pid_file_text(child_file) == "\ufffd3:1\n"
        assert not [r for r in caplog.records if "not valid UTF-8" in r.getMessage()]

    def test_a_damaged_start_token_refuses_the_pass_and_keeps_the_row(
        self, files: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTATION TARGET: a lossy token is damage, not a recycled pid.

        No reader ``int()``-parses the start-id token, so with the pid field
        intact the decoded row still parses. Unless the snapshot reports itself
        incomplete, the reconciler compares ``1\\ufffd3`` with the live token,
        calls the live runtime a stranger and retracts the only row that tracks it.
        """
        session_file, child_file = files
        row = f"{os.getpid()}:5001:1".encode("ascii") + b"\xff3\n"
        session_file.write_bytes(row)
        child_file.write_bytes(b"")
        monkeypatch.setattr(sp, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(runtime_reconcile, "_mcp_backend_pids", lambda: set())
        monkeypatch.setattr(runtime_reconcile, "instance_slice_pids", lambda: set())
        monkeypatch.setattr(sp, "_pid_start_token", lambda pid: "123")
        monkeypatch.setattr(platform_compat, "pid_liveness", lambda pid: platform_compat.PID_ALIVE)
        assert sp._read_tracked_agent_pids() == ({5001}, False)
        reconciler = runtime_reconcile.build_reconciler(
            active_pids=lambda: set(), notify_dead=lambda pid: None
        )
        reading = reconciler.run_once()
        assert reading.supported is False
        assert session_file.read_bytes() == row

    # MUTATION TARGET for the four below: _recorded_token_is_damaged. A lossy
    # start-id token always differs from the live one, so without it each sweep
    # reads "recycled" and prunes the only row that tracks a live runtime.
    _DAMAGED_TOKEN = b"1\xff3"

    @staticmethod
    def _still_tracked(path: Path, pid: int) -> bool:
        return f":{pid}:" in path.read_bytes().decode("utf-8", "replace")

    def test_the_boot_sweep_retains_a_row_whose_token_is_damaged(
        self, files: tuple[Path, Path]
    ) -> None:
        session_file, _child_file = files
        session_file.write_bytes(b"999999:99998:" + self._DAMAGED_TOKEN + b"\n")
        kills: list[int] = []
        with (
            patch.object(sp, "_is_managed_agent_process", return_value=True),
            patch.object(sp, "_pid_start_token", return_value="123"),
            patch.object(sp, "_pid_in_spawn_grace", return_value=False),
            patch.object(sp, "_cleanup_orphaned_mcp_servers", return_value=0),
            patch.object(
                sp.platform_compat, "pid_liveness", return_value=platform_compat.PID_ALIVE
            ),
            patch.object(sp.platform_compat, "pid_exists", side_effect=lambda p: p != 999999),
            patch.object(sp.platform_compat, "kill_pid", side_effect=lambda p, s: kills.append(p)),
        ):
            sp.cleanup_orphaned_sessions(narrow_with_leaders=False)
        assert kills == []
        assert self._still_tracked(session_file, 99998)

    def test_the_kill_phase_retains_a_row_whose_token_is_damaged(
        self, files: tuple[Path, Path]
    ) -> None:
        session_file, _child_file = files
        session_file.write_bytes(f"{os.getpid()}:99998:".encode() + self._DAMAGED_TOKEN + b"\n")
        kills: list[int] = []
        with (
            patch.object(sp, "_pid_start_token", return_value="123"),
            patch.object(sp.platform_compat, "kill_pid", side_effect=lambda p, s: kills.append(p)),
        ):
            assert sp._kill_confirmed_and_writeback(os.getpid(), [99998], set()) == 0
        assert kills == []
        assert self._still_tracked(session_file, 99998)

    def test_the_child_sweep_retains_a_row_whose_token_is_damaged(
        self, files: tuple[Path, Path]
    ) -> None:
        _session_file, child_file = files
        child_file.write_bytes(b"77777:99999:" + self._DAMAGED_TOKEN + b"\n")
        kills: list[int] = []
        with (
            patch.object(sp, "_pid_start_token", return_value="123"),
            patch.object(sp, "_accepted_subreaper_pids", return_value={1}),
            patch.object(sp.platform_compat, "pid_exists", side_effect=lambda p: p == 77777),
            patch.object(sp.platform_compat, "get_ppid", return_value=1),
            patch.object(sp.platform_compat, "kill_pid", side_effect=lambda p, s: kills.append(p)),
        ):
            assert sp._cleanup_orphaned_mcp_servers() == 0
        assert kills == []
        assert "77777:99999:" in child_file.read_bytes().decode("utf-8", "replace")

    def test_the_root_sweep_retains_a_row_whose_token_is_damaged(
        self, files: tuple[Path, Path]
    ) -> None:
        session_file, _child_file = files
        session_file.write_bytes(b"999999:99998:" + self._DAMAGED_TOKEN + b"\n")
        kills: list[int] = []

        def liveness(pid: int) -> str:
            return platform_compat.PID_DEAD if pid == 999999 else platform_compat.PID_ALIVE

        with (
            patch.object(sp, "_is_managed_agent_process", return_value=True),
            patch.object(sp, "_pid_start_token", return_value="123"),
            patch.object(sp.platform_compat, "pid_liveness", side_effect=liveness),
            patch.object(sp.platform_compat, "get_ppid", return_value=1),
            patch.object(sp.platform_compat, "pid_exists", return_value=True),
            patch.object(sp.platform_compat, "kill_pid", side_effect=lambda p, s: kills.append(p)),
        ):
            sp.cleanup_orphaned_session_roots()
        assert kills == []
        assert self._still_tracked(session_file, 99998)

    def test_the_reconcilers_readers_and_retractions_answer(self, files: tuple[Path, Path]) -> None:
        """The reconciler reads the same files through the same decode.

        A damaged row is skipped, so the pass is refused only by the incomplete
        snapshot, never by a raise out of its registry read; and a retraction
        matches the text the capture saw, so it removes the confirmed row alone.
        """
        session_file, child_file = files
        session_file.write_bytes(b"1\xff:11\n10:12:tok\n")
        child_file.write_bytes(b"\xff3:1\n31:32\n")
        assert runtime_reconcile._session_pid_entry_owners() == {12: (10, "tok", "10:12:tok")}
        assert runtime_reconcile._descendant_pid_rows() == {31: ("31:32",)}
        assert sp._read_tracked_agent_pids()[1] is False
        runtime_reconcile._retract_session_rows(["10:12:tok"])
        runtime_reconcile._retract_descendant_rows(["31:32"])
        assert session_file.read_text(encoding="utf-8") == "1\ufffd:11\n"
        assert child_file.read_text(encoding="utf-8") == "\ufffd3:1\n"
