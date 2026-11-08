"""Tests for process tree tracking, recursive kill, and session cleanup."""

import asyncio
import inspect
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import runtime as rt
from kiro_crew.acp import runtime_process_tree as rpt
from kiro_crew.acp.client import (
    AcpClient,
    _direct_children,
    _get_child_pids,
    _is_our_child,
    _kill_escaped_children,
    _read_basename,
)
from kiro_crew.testing.wait import wait_until

if sys.platform != "win32":
    import signal

# ── 1. _get_child_pids: visited-set prevents infinite loops ──


class TestGetChildPidsVisitedSet:
    def test_cycle_terminates(self):
        """A→B→A cycle must not recurse infinitely."""
        call_count = 0

        def fake_direct(pid):
            nonlocal call_count
            call_count += 1
            return {1: [2], 2: [1]}.get(pid, [])

        with patch("kiro_crew.acp.client._direct_children", side_effect=fake_direct):
            result = _get_child_pids(1)
        assert result == [2]
        assert call_count <= 3

    def test_self_loop(self):
        with patch("kiro_crew.acp.client._direct_children", return_value=[42]):
            assert _get_child_pids(42) == []

    def test_diamond_deduplicates(self):
        tree = {1: [2, 3], 2: [4], 3: [4]}
        with patch("kiro_crew.acp.client._direct_children", side_effect=lambda p: tree.get(p, [])):
            assert sorted(_get_child_pids(1)) == [2, 3, 4]

    def test_none_pid(self):
        assert _get_child_pids(None) == []

    def test_no_children(self):
        with patch("kiro_crew.acp.client._direct_children", return_value=[]):
            assert _get_child_pids(999) == []

    def test_deep_chain(self):
        tree = {1: [2], 2: [3], 3: [4], 4: [5]}
        with patch("kiro_crew.acp.client._direct_children", side_effect=lambda p: tree.get(p, [])):
            assert _get_child_pids(1) == [2, 3, 4, 5]


# ── 2. _kill_escaped_children: handles dead PIDs and kills bottom-up ──


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "POSIX-only sweep: _kill_escaped_children returns immediately on Windows"
        " (kill_process_tree already walked the tree via taskkill /T), so patching"
        " os.kill / asserting a signal-order never fires. signal.SIGKILL is also"
        " undefined on Windows. The Windows no-op contract is exercised in"
        " test_kill_escaped_children_windows_noop below."
    ),
)
class TestKillEscapedChildren:
    def test_already_dead_pid(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            _kill_escaped_children({999: 100})  # should not raise

    def test_kills_verified_child(self):
        def fake_kill(pid, sig):
            if sig == 0:
                return
            assert sig == signal.SIGKILL

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_crew.acp.client._is_our_child", return_value=True),
        ):
            _kill_escaped_children({42: 100})

        # The same sweep, unpatched, on a child this test spawned and recorded.
        child = subprocess.Popen(["sleep", "30"])
        try:
            wait_until(lambda: _read_basename(child.pid) == b"sleep")
            record = (platform_compat.get_process_start_id(child.pid), b"sleep")
            _kill_escaped_children({child.pid: record})
            assert child.wait(timeout=10) == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_skips_recycled_pid(self):
        kills = []

        def fake_kill(pid, sig):
            kills.append((pid, sig))
            if sig == 0:
                return

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_crew.acp.client._is_our_child", return_value=False),
        ):
            _kill_escaped_children({42: 100})
        assert all(sig == 0 for _, sig in kills)

    def test_kills_leaf_first(self):
        killed = []

        def fake_kill(pid, sig):
            if sig == signal.SIGKILL:
                killed.append(pid)

        with (
            patch("os.kill", side_effect=fake_kill),
            patch("kiro_crew.acp.client._is_our_child", return_value=True),
        ):
            _kill_escaped_children({10: 1, 20: 2, 30: 3})
        assert killed == [30, 20, 10]


# ── 3. _is_our_child: allowlist and start-time verification ──


class TestIsOurChild:
    @pytest.fixture(autouse=True)
    def _force_linux(self):
        with patch("kiro_crew.acp.client.sys") as mock_sys:
            mock_sys.platform = "linux"
            yield

    def test_rejects_missing_proc(self):
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=1),
            patch("kiro_crew.acp.client._read_basename", return_value=None),
        ):
            # recorded basename was "node" but _read_basename returns None (process gone)
            assert _is_our_child(999, expected_start=1, expected_basename=b"node") is False

    def test_rejects_unknown_binary(self):
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=1),
            patch("kiro_crew.acp.client._read_basename", return_value=b"postgres"),
        ):
            # recorded basename was "node" but live binary is "postgres" (recycled)
            assert _is_our_child(999, expected_start=1, expected_basename=b"node") is False

    def test_rejects_start_time_mismatch(self):
        with patch("kiro_crew.platform_compat.get_process_start_id", return_value=200):
            assert _is_our_child(999, expected_start=100) is False

    def test_accepts_matching_kiro(self):
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=100),
            patch("kiro_crew.acp.client._read_basename", return_value=b"kiro-cli"),
        ):
            assert _is_our_child(999, expected_start=100, expected_basename=b"kiro-cli") is True

    def test_accepts_mcp_in_name(self):
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=50),
            patch("kiro_crew.acp.client._read_basename", return_value=b"builder-mcp"),
        ):
            assert _is_our_child(999, expected_start=50, expected_basename=b"builder-mcp") is True

    def test_none_start_time_denied(self):
        assert _is_our_child(999, expected_start=None) is False


# ── 4. _direct_children: /proc and pgrep fallback ──


class TestDirectChildren:
    @pytest.fixture(autouse=True)
    def _posix(self, _floor_monkeypatch):
        """Every case here takes a POSIX branch, so Windows runners pin it too."""
        _floor_monkeypatch.setattr(
            "kiro_crew.acp.client.platform_compat", SimpleNamespace(IS_WINDOWS=False)
        )

    def test_proc_children_parsed(self, monkeypatch):
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", True)
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.Path") as mock_path_cls,
        ):
            mock_sys.platform = "linux"
            mock_path = MagicMock()
            mock_path_cls.return_value = mock_path
            mock_path.is_dir.return_value = True
            child_file = MagicMock()
            child_file.exists.return_value = True
            child_file.read_text.return_value = "200 300 "
            tid = MagicMock()
            tid.__truediv__ = lambda self, x: child_file
            mock_path.iterdir.return_value = [tid]
            result = _direct_children(100)
        assert result == [200, 300]

    @staticmethod
    def _proc_with(children_files):
        """A fake /proc/<pid>/task tree: one task per entry.

        An entry is the children text, or an exception the task's read raises (a
        task that exited mid-scan).
        """
        mock_path = MagicMock()
        tids = []
        for text in children_files:
            child_file = MagicMock()
            if isinstance(text, BaseException):
                child_file.read_text.side_effect = text
            else:
                child_file.read_text.return_value = text
            tid = MagicMock()
            tid.__truediv__ = lambda self, x, f=child_file: f
            tids.append(tid)
        mock_path.iterdir.return_value = tids
        return mock_path

    @staticmethod
    def _spawner(out=b""):
        """A stand-in for the facade's subprocess module; records every spawn."""
        sub = MagicMock()
        sub.check_output.return_value = out
        return sub

    def test_an_empty_children_file_is_the_answer_without_pgrep(self, monkeypatch):
        """A leaf's empty list is authoritative: no pgrep spawn per leaf per walk."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", True)
        sub = self._spawner(b"999\n")
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.Path", return_value=self._proc_with(["", "\n"])),
            patch("kiro_crew.acp.client.subprocess_mod", sub),
        ):
            mock_sys.platform = "linux"
            assert _direct_children(100) == []
        sub.check_output.assert_not_called()

    def test_a_kernel_without_children_files_still_falls_back_to_pgrep(self, monkeypatch):
        """No children interface on this kernel, so /proc says nothing: ask pgrep."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", False)
        sub = self._spawner(b"4242\n")
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.subprocess_mod", sub),
        ):
            mock_sys.platform = "linux"
            assert _direct_children(100) == [4242]
        assert sub.check_output.call_args[0][0][:2] == ["pgrep", "-P"]

    def test_a_task_vanishing_mid_walk_keeps_the_children_already_read(self, monkeypatch):
        """A task that exits mid-scan costs neither the children read nor a pgrep."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", True)
        sub = self._spawner()
        sub.check_output.side_effect = subprocess.TimeoutExpired(["pgrep"], 2)
        proc = self._proc_with(["200", FileNotFoundError()])
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.Path", return_value=proc),
            patch("kiro_crew.acp.client.subprocess_mod", sub),
        ):
            mock_sys.platform = "linux"
            assert _direct_children(100) == [200]
        assert proc.iterdir.call_count == 2
        sub.check_output.assert_not_called()

    def test_a_pid_exiting_before_the_relist_keeps_the_first_pass(self, monkeypatch):
        """The re-list after a vanished task finds the pid gone: the first pass stands."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", True)
        sub = self._spawner(b"999\n")
        proc = self._proc_with(["200", FileNotFoundError()])
        first = proc.iterdir.return_value
        proc.iterdir.side_effect = [first, FileNotFoundError()]
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.Path", return_value=proc),
            patch("kiro_crew.acp.client.subprocess_mod", sub),
        ):
            mock_sys.platform = "linux"
            assert _direct_children(100) == [200]
        sub.check_output.assert_not_called()

    def test_an_empty_relist_keeps_the_children_the_first_pass_read(self, monkeypatch):
        """The re-list sees the parent as a zombie with empty files: the first pass's pid stays."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", True)
        sub = self._spawner(b"999\n")
        proc = self._proc_with(["200", FileNotFoundError()])
        first = proc.iterdir.return_value
        second = self._proc_with([""]).iterdir.return_value
        proc.iterdir.side_effect = [first, second]
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.Path", return_value=proc),
            patch("kiro_crew.acp.client.subprocess_mod", sub),
        ):
            mock_sys.platform = "linux"
            assert _direct_children(100) == [200]
        assert proc.iterdir.call_count == 2
        sub.check_output.assert_not_called()

    def test_a_child_read_by_both_passes_appears_once(self, monkeypatch):
        """Children found on the re-list join the first pass's, each pid listed once."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", True)
        sub = self._spawner(b"999\n")
        proc = self._proc_with(["200", FileNotFoundError()])
        first = proc.iterdir.return_value
        second = self._proc_with(["200 300"]).iterdir.return_value
        proc.iterdir.side_effect = [first, second]
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.Path", return_value=proc),
            patch("kiro_crew.acp.client.subprocess_mod", sub),
        ):
            mock_sys.platform = "linux"
            assert _direct_children(100) == [200, 300]
        sub.check_output.assert_not_called()

    def test_a_vanished_pid_is_a_leaf_without_pgrep(self, monkeypatch):
        """A pid with no /proc entry has exited: it has no children to ask pgrep about."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", True)
        sub = self._spawner(b"777\n")
        gone = MagicMock()
        gone.iterdir.side_effect = FileNotFoundError()
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.Path", return_value=gone),
            patch("kiro_crew.acp.client.subprocess_mod", sub),
        ):
            mock_sys.platform = "linux"
            assert _direct_children(100) == []
        sub.check_output.assert_not_called()

    def test_a_full_tree_walk_spawns_nothing(self, monkeypatch):
        """300 roots with 5 leaves each, walked through _get_child_pids: every pid, no spawn."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", True)
        top = 1
        roots = list(range(1000, 1300))
        tree = {top: roots}
        for i, root in enumerate(roots):
            tree[root] = [10_000 + i * 5 + j for j in range(5)]

        def fake_path(path):
            pid = int(str(path).split("/")[2])
            return self._proc_with([" ".join(map(str, tree.get(pid, [])))])

        sub = self._spawner(b"999\n")
        with (
            patch("kiro_crew.acp.client.sys") as mock_sys,
            patch("kiro_crew.acp.client.Path", side_effect=fake_path),
            patch("kiro_crew.acp.client.subprocess_mod", sub),
        ):
            mock_sys.platform = "linux"
            pids = _get_child_pids(top)
        assert len(pids) == 300 + 300 * 5
        assert len(set(pids)) == len(pids)
        assert sub.method_calls == []

    def test_the_interface_check_reads_the_real_kernel_once(self, monkeypatch):
        """The answer comes from /proc/self, never the walk's Path, and is cached."""
        monkeypatch.setattr(rpt, "_CHILDREN_INTERFACE", None)
        kernel_os = MagicMock()
        kernel_os.path.exists.return_value = True
        exists = kernel_os.path.exists
        monkeypatch.setattr(rpt, "_kernel_os", kernel_os)
        with patch("kiro_crew.acp.client.Path") as walk_path:
            assert rpt._children_interface() is True
            assert rpt._children_interface() is True
        walk_path.assert_not_called()
        exists.assert_called_once()
        assert exists.call_args[0][0].startswith("/proc/self/task/")

    @pytest.mark.skipif(sys.platform != "linux", reason="reads the real /proc children files")
    def test_a_real_leaf_process_answers_from_proc(self):
        """Against the live kernel: a childless process reads back [] with no spawn."""
        import subprocess
        from pathlib import Path

        leaf = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
        )
        try:
            if not (Path(f"/proc/{leaf.pid}/task/{leaf.pid}/children")).exists():
                pytest.skip("kernel built without /proc/<pid>/task/<tid>/children")
            sub = self._spawner(b"999\n")
            with patch("kiro_crew.acp.client.subprocess_mod", sub):
                assert _direct_children(leaf.pid) == []
            sub.check_output.assert_not_called()
        finally:
            leaf.stdin.close()
            leaf.wait(timeout=30)


# ── 5. _snapshot_process_tree: captures full descendant tree ──


class TestSnapshotProcessTree:
    @pytest.mark.asyncio
    async def test_tracks_all_descendants(self, tmp_path):
        client = AcpClient.__new__(AcpClient)
        client._pid = 100
        client._child_pids = {}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[200, 300, 400]),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: p * 10),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
            patch("kiro_crew.session_pid._pid_start_token", side_effect=lambda p: str(p * 10)),
        ):
            await client._snapshot_process_tree()

        assert client._child_pids == {
            200: (2000, b"proc200"),
            300: (3000, b"proc300"),
            400: (4000, b"proc400"),
        }
        # Verify child:parent:start-id lines written to kiro_pids.txt
        content = (tmp_path / "kiro_pids.txt").read_text(encoding="utf-8")
        lines = {ln.strip() for ln in content.splitlines() if ln.strip()}
        assert lines == {"200:100:2000", "300:100:3000", "400:100:4000"}

    @pytest.mark.asyncio
    async def test_no_descendants_no_tracking(self):
        client = AcpClient.__new__(AcpClient)
        client._pid = 100
        client._child_pids = {}

        with patch("kiro_crew.acp.client._get_child_pids", return_value=[]):
            await client._snapshot_process_tree()

        assert client._child_pids == {}

    @pytest.mark.asyncio
    async def test_merges_early_and_late_snapshots(self, tmp_path):
        """Early snapshot from _spawn + late snapshot from _snapshot_process_tree merge."""
        client = AcpClient.__new__(AcpClient)
        client._pid = 100
        # Simulate early snapshot already captured PID 200
        client._child_pids = {200: (2000, b"node")}

        with (
            patch("kiro_crew.acp.client._get_child_pids", return_value=[200, 300]),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: p * 10),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await client._snapshot_process_tree()

        # PID 200 keeps original record, PID 300 is new
        assert client._child_pids == {200: (2000, b"node"), 300: (3000, b"proc300")}


# ── 5b. AcpRuntime._snapshot_descendants: the runtime path tracks its tree ──


def _book(root) -> set[str]:
    """The tracking file's non-empty lines."""
    return {
        ln.strip()
        for ln in (root / "kiro_pids.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    }


def _snapshot_runtime(pid: int = 100) -> rt.AcpRuntime:
    """An AcpRuntime carrying only the state _snapshot_descendants touches.

    The root-identity bracket is stubbed out here and exercised on its own in
    TestRuntimeRootIdentity, so every other case can patch the identity reader
    for the CHILDREN without also having to satisfy the root's check.
    """
    r = rt.AcpRuntime.__new__(rt.AcpRuntime)
    r._pid = pid
    r._start_time = "root-start"
    r._child_pids = {}
    r._descendant_scan_lock = asyncio.Lock()
    r._root_identity_holds = lambda: True  # type: ignore[method-assign]
    return r


class TestRuntimeSnapshotDescendants:
    """The runtime registers its spawn root; the tree under it must be tracked too.

    On a sandboxed host the registered PID is the launcher, two forks above the
    process holding the memory, so an untracked subtree that outlives the root is
    reachable by no reaper — every sweep keys off the PID files.
    """

    @pytest.mark.asyncio
    async def test_tracks_all_descendants(self, tmp_path):
        r = _snapshot_runtime()

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[200, 300]),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: p * 10),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        # The record shape session_pid verifies identity against, not a bare id.
        assert r._child_pids == {200: (2000, b"proc200"), 300: (3000, b"proc300")}
        assert _book(tmp_path) == {"200:100:2000", "300:100:3000"}

    @pytest.mark.asyncio
    async def test_a_later_session_adds_its_own_descendants(self, tmp_path):
        """Every session start forks another agent process the last scan missed."""
        r = _snapshot_runtime()
        walks = [[200], [200], [200, 400], [200, 400]]

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", side_effect=lambda pid: walks.pop(0)),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: p * 10),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()
            await r._snapshot_descendants()

        assert r._child_pids == {200: (2000, b"proc200"), 400: (4000, b"proc400")}
        assert _book(tmp_path) == {"200:100:2000", "400:100:4000"}

    @pytest.mark.asyncio
    async def test_retries_once_when_the_first_scan_is_empty(self, tmp_path, monkeypatch):
        """A scan racing a cold start sees nothing; the spawn call gets one retry."""
        monkeypatch.setattr(rt.AcpRuntime, "_DESCENDANT_RESCAN_DELAY", 0)
        r = _snapshot_runtime()
        walks = [[], [200], [200]]

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", side_effect=lambda pid: walks.pop(0)),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: p * 10),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants(retry_when_empty=True)

        assert r._child_pids == {200: (2000, b"proc200")}
        assert walks == []

    @pytest.mark.asyncio
    async def test_no_descendants_writes_nothing(self, tmp_path):
        r = _snapshot_runtime()

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[]),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert r._child_pids == {}
        assert not (tmp_path / "kiro_pids.txt").exists()

    @pytest.mark.asyncio
    async def test_a_failed_scan_never_raises(self, tmp_path):
        """Losing a snapshot must not fail the spawn or session start that called it."""
        r = _snapshot_runtime()

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", side_effect=OSError("/proc gone")),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert r._child_pids == {}

    @pytest.mark.asyncio
    async def test_no_pid_is_a_no_op(self):
        r = _snapshot_runtime()
        r._pid = None

        with patch("kiro_crew.acp.runtime._get_child_pids") as scan:
            await r._snapshot_descendants()

        scan.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_and_write_run_off_the_event_loop(self, tmp_path):
        """The scan shells out on macOS and the write takes an exclusive file lock."""
        r = _snapshot_runtime()
        loop_thread = threading.current_thread()
        scan_threads: list[threading.Thread] = []
        write_threads: list[threading.Thread] = []

        def _scan(pid):
            scan_threads.append(threading.current_thread())
            return [200]

        def _write(pids, parent_pid=0, drop=()):
            write_threads.append(threading.current_thread())
            return True

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", side_effect=_scan),
            patch("kiro_crew.acp.runtime._replace_child_pids", side_effect=_write),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: p * 10),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert scan_threads and all(t is not loop_thread for t in scan_threads)
        assert write_threads and all(t is not loop_thread for t in write_threads)


class TestRuntimeSnapshotDurability:
    """Nothing reports a descendant's exit — it is a grandchild, so there is no
    SIGCHLD to catch. Every pass re-reads the tree and writes the whole answer,
    which is what has to survive a reused pid, a failed write and a cancel."""

    @pytest.mark.asyncio
    async def test_a_pid_still_in_the_tree_is_re_read_not_remembered(self, tmp_path):
        """A recorded pid can be handed to another process OF OURS.

        Keeping the old start id would make the teardown sweep read the live
        descendant as recycled and skip it.
        """
        r = _snapshot_runtime()
        r._child_pids = {200: ("old-start", b"node")}

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[200]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="new-start"),
            patch("kiro_crew.acp.client._read_basename", return_value=b"agent-chat"),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert r._child_pids == {200: ("new-start", b"agent-chat")}
        # One line, carrying the identity captured on THIS pass under the second
        # walk's confirmation — never one the writer read for itself.
        assert _book(tmp_path) == {"200:100:new-start"}

    @pytest.mark.asyncio
    async def test_a_pid_that_left_the_tree_mid_capture_is_dropped(self, tmp_path):
        """The identity read can land on a stranger that took the number.

        Recorded as ours it is self-consistent, so every later ownership check
        passes and the teardown signals an unrelated process. The second walk is
        what refuses it.
        """
        r = _snapshot_runtime()
        walks = [[200, 999], [200]]

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", side_effect=lambda pid: walks.pop(0)),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: f"s{p}"),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert 999 not in r._child_pids
        assert r._child_pids == {200: ("s200", b"proc200")}
        assert _book(tmp_path) == {"200:100:s200"}

    @pytest.mark.asyncio
    async def test_a_capture_whose_start_id_changed_before_the_confirm_is_dropped(self, tmp_path):
        """The second walk still finds the number, but under another identity.

        The pid exited after the capture and a new descendant took it, so the
        captured identity names a process that has exited. Recorded, it makes
        the teardown read the live child as recycled and skip it. The same rule
        holds for ``AcpClient.refresh_process_tree``: both run one guarded pass.
        """
        r = _snapshot_runtime()
        reads = {200: iter(["captured", "reread"])}

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[200]),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda p: next(reads[p]),
            ),
            patch("kiro_crew.acp.client._read_basename", return_value=b"proc200"),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert r._child_pids == {}, "a capture that does not read back was recorded"
        assert not (tmp_path / "kiro_pids.txt").exists()

    @pytest.mark.asyncio
    async def test_a_live_pid_outside_the_tree_keeps_its_record(self, tmp_path):
        """The walk cannot reach a child that left the process group. Its record
        is the only handle teardown and the sweeps have on it."""
        r = _snapshot_runtime()
        r._child_pids = {200: ("s200", b"escaped")}

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[300]),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: f"s{p}"),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert r._child_pids == {200: ("s200", b"escaped"), 300: ("s300", b"proc300")}
        # 200 keeps the identity it was recorded under, not a fresh read.
        assert _book(tmp_path) == {"200:100:s200", "300:100:s300"}

    @pytest.mark.asyncio
    async def test_an_escapee_whose_identity_changed_is_dropped(self, tmp_path):
        """This is the kill-a-stranger case, and liveness alone cannot see it.

        The number is alive — as somebody else. Carrying the record forward and
        writing a freshly read token would make the sweep find live and recorded
        identity in agreement and SIGKILL an unrelated process.
        """
        r = _snapshot_runtime()
        r._child_pids = {200: ("ours", b"agent-chat")}

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[300]),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda p: "stranger" if p == 200 else f"s{p}",
            ),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert 200 not in r._child_pids
        assert _book(tmp_path) == {"300:100:s300"}

    @pytest.mark.asyncio
    async def test_a_dead_pid_leaves_the_book(self, tmp_path):
        """Nothing announces the exit, so the pass that notices removes the line."""
        r = _snapshot_runtime()
        r._child_pids = {200: ("s200", b"gone")}

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[300]),
            patch(
                "kiro_crew.platform_compat.get_process_start_id",
                side_effect=lambda p: None if p == 200 else f"s{p}",
            ),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        assert r._child_pids == {300: ("s300", b"proc300")}
        assert _book(tmp_path) == {"300:100:s300"}

    @pytest.mark.asyncio
    async def test_a_failed_write_publishes_nothing_and_the_next_pass_retries(self, tmp_path):
        """The record must never claim a line the file does not carry."""
        r = _snapshot_runtime()
        answers = [False, True]

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[200]),
            patch(
                "kiro_crew.acp.runtime._replace_child_pids",
                side_effect=lambda *a, **k: answers.pop(0),
            ),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: f"s{p}"),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()
            assert r._child_pids == {}, "a pid whose write failed must stay unpublished"

            await r._snapshot_descendants()

        assert r._child_pids == {200: ("s200", b"proc200")}
        assert answers == []

    @pytest.mark.asyncio
    async def test_a_cancellation_is_not_swallowed(self, tmp_path, monkeypatch):
        """A cancel is not a failed scan.

        It must reach the caller's cleanup guard, which owns the half-built
        runtime or session that has to be torn down.
        """
        monkeypatch.setattr(rt.AcpRuntime, "_DESCENDANT_RESCAN_DELAY", 0)
        r = _snapshot_runtime()

        async def _cancel(_delay):
            raise asyncio.CancelledError

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.runtime.asyncio.sleep", side_effect=_cancel),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            with pytest.raises(asyncio.CancelledError):
                await r._snapshot_descendants(retry_when_empty=True)

    @pytest.mark.asyncio
    async def test_one_pass_at_a_time(self, tmp_path):
        """Two overlapping passes would let an older read's answer win.

        Sessions start concurrently on a shared runtime, and each pass rewrites
        the whole block from what it read. Without the lock both passes walk
        before either writes, so the second write is built on a stale read.
        """
        r = _snapshot_runtime()
        inside = 0
        peak = 0

        def _scan(pid):
            nonlocal inside, peak
            inside += 1
            peak = max(peak, inside)
            return [200]

        def _write(pids, parent_pid=0, drop=()):
            nonlocal inside
            inside = 0
            return True

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", side_effect=_scan),
            patch("kiro_crew.acp.runtime._replace_child_pids", side_effect=_write),
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: f"s{p}"),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await asyncio.gather(r._snapshot_descendants(), r._snapshot_descendants())

        # Each pass walks twice, so one pass alone reaches 2; a second pass
        # entering before the first wrote would reach 3.
        assert peak <= 2, "a second pass walked before the first one wrote"


class TestOneGuardedPassServesBothPaths:
    """The runtime snapshot and the client refresh run the same guarded pass, so a
    guard added to one cannot be missing from the other."""

    @pytest.mark.asyncio
    async def test_runtime_and_client_both_run_the_shared_pass(self):
        from kiro_crew.acp import runtime_process_tree
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.testing.ids import UNALLOCATABLE_PID

        modes: list[bool] = []

        async def _spy(pid, recorded, **kwargs):
            modes.append(kwargs["replace"])
            return runtime_process_tree.TreePass("empty")

        client = AcpClient.__new__(AcpClient)
        client._process = MagicMock(pid=UNALLOCATABLE_PID, returncode=None)
        client._pid = UNALLOCATABLE_PID
        client._start_time = "111"
        client._child_pids = {}

        with patch.object(runtime_process_tree, "guarded_tree_pass", new=_spy):
            await _snapshot_runtime()._snapshot_descendants()
            await client.refresh_process_tree()

        assert modes == [True, False], "a path walks its tree outside the shared pass"


class TestRuntimeRootIdentity:
    """The root's own number can be reused, and a walk from a recycled root
    enumerates a stranger's whole tree."""

    @pytest.mark.asyncio
    async def test_a_recycled_root_records_nothing(self, tmp_path):
        r = _snapshot_runtime()
        del r._root_identity_holds  # exercise the real check
        r._start_time = "spawned-then"

        with (
            patch("kiro_crew.acp.runtime._get_child_pids") as scan,
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="someone-else"),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        scan.assert_not_called()
        assert r._child_pids == {}

    @pytest.mark.asyncio
    async def test_a_root_recycled_during_the_walk_writes_nothing(self, tmp_path):
        """The identity held when the walk began and was gone before the write.

        The root can exit while the walk runs; a write keyed on the first check
        alone would persist whatever tree the number now leads. Asserting on the
        writer, not the file, is what makes deleting the second check fail this.
        """
        r = _snapshot_runtime()
        answers = iter([True, False])
        r._root_identity_holds = lambda: next(answers)  # type: ignore[method-assign]

        with (
            patch("kiro_crew.acp.runtime._get_child_pids", return_value=[200, 300]),
            patch("kiro_crew.acp.runtime._replace_child_pids") as write,
            patch("kiro_crew.platform_compat.get_process_start_id", side_effect=lambda p: p * 10),
            patch("kiro_crew.acp.client._read_basename", side_effect=lambda p: f"proc{p}".encode()),
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        write.assert_not_called()
        assert r._child_pids == {}

    @pytest.mark.asyncio
    async def test_an_unrecorded_root_identity_records_nothing(self, tmp_path):
        """Nothing to compare means the tree cannot be proven ours."""
        r = _snapshot_runtime()
        del r._root_identity_holds
        r._start_time = None

        with (
            patch("kiro_crew.acp.runtime._get_child_pids") as scan,
            patch("kiro_crew.session_pid.config_dir", return_value=tmp_path),
        ):
            await r._snapshot_descendants()

        scan.assert_not_called()
        assert r._child_pids == {}


class TestEverySessionPathSnapshots:
    """A fresh session and a resumed one fork the same processes.

    Read off the source because driving either funnel needs a live backend: the
    point is that neither path can lose the call or its teardown guard.
    """

    @pytest.mark.parametrize(
        "method,session_var",
        [("_finish_create_session", "session_id"), ("load_session", "resume_sid")],
    )
    def test_the_scan_runs_under_a_terminate_guard(self, method, session_var):
        src = inspect.getsource(getattr(rt.AcpRuntime, method))
        assert "await self._snapshot_descendants()" in src, f"{method} does not snapshot"
        # Whitespace-normalized so an indentation change cannot pass or fail it.
        flat = " ".join(src.split())
        expected = (
            "try: await self._snapshot_descendants() "
            "except BaseException: "
            f"await self.terminate_session({session_var}) raise"
        )
        guard = expected in flat
        assert guard, f"{method} does not terminate its session when the scan is cancelled"

    def test_spawn_snapshots_inside_its_cleanup_guard(self):
        """A cancelled scan after `_initialized` would leave an unowned process."""
        src = inspect.getsource(rt.AcpRuntime._spawn_admitted)
        after = " ".join(src[src.index("await self._snapshot_descendants(") :].split())
        assert after.startswith(
            "await self._snapshot_descendants(retry_when_empty=True) except BaseException:"
        ), "the spawn snapshot sits outside the cleanup guard"
        # The guard waits on one shielded cleanup task, and that task kills.
        assert "self._failed_start_cleanup()" in after, "the guard does not tear the runtime down"
        cleanup = inspect.getsource(rt.AcpRuntime._failed_start_cleanup)
        assert "await self.kill(" in cleanup, "the guard does not tear the runtime down"


# ── 6. Session cleanup on cancellation ──


class TestSessionCleanupOnCancellation:
    """Verify _cleanup_run_sessions resets all session keys for a run."""

    def _make_mock_taskrunner(self, session_keys):
        """Create a minimal mock TaskRunner with fake sessions."""
        from unittest.mock import AsyncMock

        tr = MagicMock()
        tr._sessions = MagicMock()
        tr._sessions.get_pid = MagicMock(return_value=None)
        tr._sessions._sessions = {k: MagicMock() for k in session_keys}
        tr._sessions.cancel_current = AsyncMock()
        tr._sessions.release = MagicMock()
        tr._sessions.reset = AsyncMock()
        tr._sessions.release_subagent_runtime = AsyncMock()
        tr._release_run_runtime = AsyncMock()
        return tr

    @pytest.mark.asyncio
    async def test_cleanup_resets_all_matching_keys(self):
        """All sessions with the run prefix get cancelled and reset."""
        run = MagicMock()
        run.task_id = "abc123"
        run.error = None
        keys = ["taskrunner:abc123:task0", "taskrunner:abc123:task1", "taskrunner:abc123:task2"]
        tr = self._make_mock_taskrunner(keys)

        # Import the real method and bind it
        from kiro_crew.taskrunner import TaskRunner

        cleanup = TaskRunner._cleanup_run_sessions

        await cleanup(tr, run)

        assert tr._sessions.cancel_current.call_count == 3
        assert tr._sessions.reset.call_count == 3
        for key in keys:
            # ``ends_conversation``: cancel cleanup ends every step conversation of the
            # run, so each step's sub-agent runs end with it. Asserting the whole call
            # keeps a later edit from turning this back into a process recycle, which
            # would leave a cancelled run's children going.
            tr._sessions.reset.assert_any_await(key, ends_conversation=True)
        assert run.error is None, "a cleanup whose every reset succeeded reported a failure"

    @pytest.mark.asyncio
    async def test_cleanup_ignores_other_runs(self):
        """Sessions from other runs are not touched."""
        from session_helpers import _mock_provider_factory

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager
        from kiro_crew.taskrunner import TaskRunner

        run = MagicMock()
        run.task_id = "abc123"
        run.error = None
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        keys = ["taskrunner:abc123:task0", "taskrunner:other:task0"]
        for key in keys:
            await mgr.get_or_create(key)
            mgr.release(key)
        tr = MagicMock()
        tr._sessions = mgr
        tr._release_run_runtime = AsyncMock()
        try:
            await TaskRunner._cleanup_run_sessions(tr, run)

            # Only the key under the "taskrunner:abc123:" prefix is gone.
            assert [mgr.has_session(k) for k in keys] == [False, True]
            assert run.error is None
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_handles_cancel_failure(self):
        """If cancel_current raises, reset is still called."""
        run = MagicMock()
        run.task_id = "abc123"
        run.error = None
        keys = ["taskrunner:abc123:task0"]
        tr = self._make_mock_taskrunner(keys)
        tr._sessions.cancel_current = AsyncMock(side_effect=Exception("boom"))

        from kiro_crew.taskrunner import TaskRunner

        await TaskRunner._cleanup_run_sessions(tr, run)

        # reset still called despite cancel failure
        tr._sessions.reset.assert_awaited_once()
        assert run.error is None, "a failed cancel was reported as a failed reset"


# ── 7. _track_pid / _untrack_pid file operations ──


class TestPidTracking:
    def test_track_and_untrack(self, tmp_path):
        """_track_pid appends, _untrack_pid removes."""
        from kiro_crew.session_pid import _track_pid, _untrack_pid

        pid_file = tmp_path / "pids.txt"
        with patch("kiro_crew.session_pid._pid_file_path", return_value=pid_file):
            _track_pid(100)
            _track_pid(200)
            _track_pid(300)
            assert "100" in pid_file.read_text(encoding="utf-8")
            assert "200" in pid_file.read_text(encoding="utf-8")

            _untrack_pid(200)
            content = pid_file.read_text(encoding="utf-8")
            assert "200" not in content
            assert "100" in content
            assert "300" in content


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only no-op contract")
def test_kill_escaped_children_windows_noop():
    """On Windows the sweep is a no-op — kill_process_tree already walked the
    tree via taskkill /T. Verify os.kill is never called even when the caller
    passes non-empty child_pids."""
    called = []
    with patch("os.kill", side_effect=lambda *a, **k: called.append(a)):
        _kill_escaped_children({10: 1, 20: 2, 30: 3})
    assert called == []
