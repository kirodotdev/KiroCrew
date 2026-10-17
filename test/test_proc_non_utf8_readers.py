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
        return session_file, child_file

    def test_a_damaged_pid_field_makes_the_tracked_snapshot_incomplete(
        self, files: tuple[Path, Path]
    ) -> None:
        session_file, child_file = files
        session_file.write_bytes(b"10:11\n")
        child_file.write_bytes(b"31:32\n\xff3:1\n")  # the second entry's pid is damaged
        assert sp._read_tracked_agent_pids() == ({11, 31}, False)
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

    def test_a_damaged_start_token_lets_the_reconciler_pass_run(
        self, files: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A damaged token is a malformed identity, as an ASCII-garbled one is:
        the snapshot stays complete and a real pass runs instead of raising."""
        session_file, child_file = files
        session_file.write_bytes(f"{os.getpid()}:5001:1".encode("ascii") + b"\xff3\n")
        child_file.write_bytes(b"")
        monkeypatch.setattr(sp, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(runtime_reconcile, "_mcp_backend_pids", lambda: set())
        monkeypatch.setattr(runtime_reconcile, "instance_slice_pids", lambda: set())
        monkeypatch.setattr(sp, "_pid_start_token", lambda pid: "123")
        monkeypatch.setattr(platform_compat, "pid_liveness", lambda pid: platform_compat.PID_ALIVE)
        assert sp._read_tracked_agent_pids() == ({5001}, True)
        reconciler = runtime_reconcile.build_reconciler(
            active_pids=lambda: set(), notify_dead=lambda pid: None
        )
        assert reconciler.run_once().supported is True

    def test_a_damaged_parent_field_keeps_the_snapshot_complete(
        self, files: tuple[Path, Path]
    ) -> None:
        """The owner field is not reapable, so damage in it costs no pass."""
        session_file, child_file = files
        session_file.write_bytes(b"")
        child_file.write_bytes(b"5002:1\xff3:77\n")
        assert sp._read_tracked_agent_pids() == ({5002}, True)

    @pytest.mark.parametrize("sweep", ["boot", "roots"])
    @pytest.mark.parametrize("gateway", [b"4\xff2", b"4x2"], ids=["non-utf8", "ascii"])
    def test_a_damaged_gateway_pid_takes_the_ascii_malformed_rule(
        self, files: tuple[Path, Path], sweep: str, gateway: bytes
    ) -> None:
        """Non-UTF-8 and ASCII damage take one rule: the row is pruned as
        malformed even while its child lives, so nothing (a recycled child pid
        included) keeps a refusal alive, and no kill is sent."""
        session_file, _child_file = files
        session_file.write_bytes(gateway + b":5001:13\n")
        with (
            patch.object(
                sp.platform_compat, "pid_liveness", return_value=platform_compat.PID_ALIVE
            ),
            patch.object(sp.platform_compat, "pid_exists", return_value=True),
            patch.object(sp.platform_compat, "kill_pid") as kill,
            patch.object(sp, "_is_managed_agent_process", return_value=True),
        ):
            if sweep == "boot":
                sp.cleanup_orphaned_sessions(narrow_with_leaders=False)
            else:
                sp.cleanup_orphaned_session_roots()
            kill.assert_not_called()
        assert session_file.read_bytes() == b""
        assert sp.retained_gateway_pids() == frozenset()

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
