"""Tests for process tree tracking, recursive kill, and session cleanup."""

import asyncio
import inspect
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import runtime as rt
from kiro_crew.acp.client import (
    AcpClient,
    _direct_children,
    _get_child_pids,
    _is_our_child,
    _kill_escaped_children,
)

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
    def test_proc_children_parsed(self):
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
        assert "await self.kill(" in after, "the guard does not tear the runtime down"


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

    @pytest.mark.asyncio
    async def test_cleanup_ignores_other_runs(self):
        """Sessions from other runs are not touched."""
        run = MagicMock()
        run.task_id = "abc123"
        keys = ["taskrunner:abc123:task0", "taskrunner:other:task0"]
        tr = self._make_mock_taskrunner(keys)

        from kiro_crew.taskrunner import TaskRunner

        await TaskRunner._cleanup_run_sessions(tr, run)

        # Only 1 key matches prefix "taskrunner:abc123:"
        assert tr._sessions.cancel_current.call_count == 1
        assert tr._sessions.reset.call_count == 1

    @pytest.mark.asyncio
    async def test_cleanup_handles_cancel_failure(self):
        """If cancel_current raises, reset is still called."""
        run = MagicMock()
        run.task_id = "abc123"
        keys = ["taskrunner:abc123:task0"]
        tr = self._make_mock_taskrunner(keys)
        tr._sessions.cancel_current = AsyncMock(side_effect=Exception("boom"))

        from kiro_crew.taskrunner import TaskRunner

        await TaskRunner._cleanup_run_sessions(tr, run)

        # reset still called despite cancel failure
        tr._sessions.reset.assert_awaited_once()


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
