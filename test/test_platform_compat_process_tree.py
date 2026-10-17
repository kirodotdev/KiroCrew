"""A process's descendants, walked from one snapshot of the host's parent map.

``process_descendants`` and the identity walks over Linux ``/proc``, the POSIX
``ps`` snapshot and the Windows Toolhelp snapshot, the start-order comparison
that excludes an orphan under a recycled parent, ``attributed_descendants``
binding every edge to its parent's identity, and a real grandchild in its own
session. A topic of the ``test_platform_compat`` family; see
``test_platform_compat.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.testing.wait import wait_until

# Tests here read or build the macOS libc and libproc handles, so each one starts
# with both caches unbuilt and puts them back after (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestAttributedDescendants:
    """Every parent-child EDGE is attributed, not just "created after the root".

    The root-only comparison is the trap: a stale orphan sitting under a recycled
    INTERMEDIATE pid was also created after the root, so it passes that test while
    being unrelated to the tree — and on Windows it is frequently a same-user process
    the caller CAN terminate, which makes the mistake irreversible.
    """

    @staticmethod
    def _pin(monkeypatch, parent_map, tokens):
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: parent_map)
        monkeypatch.setattr(pc, "process_start_time", lambda pid: tokens.get(pid, ""))

    def test_a_real_chain_is_walked_to_the_bottom(self, monkeypatch):
        """Each generation is compared against ITS OWN parent, so depth is no barrier."""
        self._pin(
            monkeypatch,
            {20: 10, 30: 20, 40: 30},
            {10: "1000", 20: "1100", 30: "1200", 40: "1300"},
        )

        assert pc.attributed_descendants(10, "1000") == [20, 30, 40]

    def test_an_orphan_under_a_recycled_intermediate_is_excluded(self, monkeypatch):
        """The case root-only attribution gets wrong.

        30 was created BEFORE 20 — it is the leftover child of whatever held pid 20
        before 20 did — but AFTER the root, so a root comparison admits it. Comparing
        it against 20, the parent it is reached through, rejects it.
        """
        self._pin(
            monkeypatch,
            {20: 10, 30: 20},
            {10: "1000", 20: "1200", 30: "1100"},
        )

        assert pc.attributed_descendants(10, "1000") == [20]
        # Coherence check: the weaker rule really would have admitted it, so this
        # test is exercising the difference rather than restating the primitive.
        assert pc.created_after("1100", "1000") is True

    def test_the_whole_subtree_behind_a_bad_edge_is_dropped(self, monkeypatch):
        """Everything under an unattributable child is reachable only through it."""
        self._pin(
            monkeypatch,
            {20: 10, 30: 20, 40: 30},
            {10: "1000", 20: "1200", 30: "1100", 40: "9999"},
        )

        assert pc.attributed_descendants(10, "1000") == [20]

    def test_a_process_with_no_readable_identity_is_left_alone(self, monkeypatch):
        """An unreadable creation time is not a licence to guess."""
        self._pin(monkeypatch, {20: 10, 30: 10}, {10: "1000", 20: "1100"})

        assert pc.attributed_descendants(10, "1000") == [20]

    def test_a_missing_root_token_yields_nothing(self, monkeypatch):
        """With no root identity there is no edge to attribute the first hop against."""
        self._pin(monkeypatch, {20: 10}, {10: "", 20: "1100"})

        assert pc.attributed_descendants(10, "") == []


class TestProcessDescendants:
    def test_descendants_from_parent_map_sorts_siblings_and_walks_full_tree(self):
        parent_map = {
            13: 10,
            12: 11,
            11: 10,
            14: 12,
            99: 1,
            10: 14,
        }

        assert pc._descendants_from_parent_map(10, parent_map) == [11, 13, 12, 14]

    def test_linux_descendant_identities_walk_only_the_root_subtree(self, tmp_path, monkeypatch):
        proc_root = tmp_path / "proc"
        process_rows = (
            (10, 1, "100"),
            (11, 10, "110"),
            (12, 11, "120"),
            (13, 10, "130"),
            (99, 1, "990"),
        )
        children = {10: "11 13", 11: "12", 12: "", 13: "", 99: ""}
        for process, parent, start_time in process_rows:
            process_root = proc_root / str(process)
            task_root = process_root / "task" / str(process)
            task_root.mkdir(parents=True)
            fields = ["S", str(parent), *(["0"] * 17), start_time]
            (process_root / "stat").write_text(
                f"{process} (test process) {' '.join(fields)}\n",
                encoding="utf-8",
            )
            (task_root / "children").write_text(children[process], encoding="ascii")
        real_read_text = Path.read_text
        stat_reads: list[int] = []

        def _track_reads(path: Path, *args, **kwargs):
            if path.name == "stat":
                stat_reads.append(int(path.parent.name))
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _track_reads)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(
            pc.subprocess,
            "check_output",
            lambda *args, **kwargs: pytest.fail("procfs discovery must not spawn ps"),
        )

        identities = pc.process_descendant_identities(10, proc_root=proc_root)

        assert identities == [
            pc.ProcessDescendantIdentity(11, 10, "110"),
            pc.ProcessDescendantIdentity(13, 10, "130"),
            pc.ProcessDescendantIdentity(12, 11, "120"),
        ]
        assert 99 not in stat_reads

    def test_atomic_identity_walk_excludes_orphan_older_than_recycled_parent(self, monkeypatch):
        identities = {
            10: pc.ProcessStartIdentity("100", 1),
            11: pc.ProcessStartIdentity("300", 10),
            12: pc.ProcessStartIdentity("200", 11),
        }
        children = {10: [11], 11: [12], 12: []}
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(
            pc,
            "_direct_child_pids_for_identity_walk",
            lambda pid, proc_root: children[pid],
        )
        monkeypatch.setattr(
            pc,
            "get_process_start_identity",
            lambda pid, proc_root=None: identities[pid],
        )

        assert pc.process_descendant_identities(10) == [pc.ProcessDescendantIdentity(11, 10, "300")]

    @pytest.mark.parametrize(
        "replacement",
        [
            pytest.param(None, id="unreadable-parent"),
            pytest.param(
                pc.ProcessStartIdentity("recycled", 10),
                id="recycled-parent",
            ),
        ],
    )
    def test_atomic_identity_walk_parent_instability_is_inconclusive(
        self,
        monkeypatch,
        replacement,
    ):
        root = pc.ProcessStartIdentity("100", 1)
        child = pc.ProcessStartIdentity("200", 10)
        child_reads = iter((child, replacement))
        children = {10: [11], 11: [12]}
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(
            pc,
            "_direct_child_pids_for_identity_walk",
            lambda pid, proc_root: children[pid],
        )

        def _identity(pid, proc_root=None):
            if pid == 10:
                return root
            if pid == 11:
                return next(child_reads)
            return pc.ProcessStartIdentity("300", 11)

        monkeypatch.setattr(pc, "get_process_start_identity", _identity)
        monkeypatch.setattr(pc, "_posix_process_identity_map", lambda root_pid=None: None)

        assert pc.process_descendant_identities(10) is None

    @pytest.mark.parametrize(
        "late_identity",
        [
            pytest.param(None, id="vanished"),
            pytest.param(
                pc.ProcessStartIdentity("200", 99),
                id="reparented",
            ),
        ],
    )
    def test_atomic_identity_walk_child_instability_is_inconclusive(
        self,
        monkeypatch,
        late_identity,
    ):
        root = pc.ProcessStartIdentity("100", 1)
        stable_child = pc.ProcessStartIdentity("200", 10)
        children = {10: [11, 12], 11: [], 12: []}
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", True)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "linux")
        monkeypatch.setattr(
            pc,
            "_direct_child_pids_for_identity_walk",
            lambda pid, proc_root: children[pid],
        )
        monkeypatch.setattr(
            pc,
            "get_process_start_identity",
            lambda pid, proc_root=None: {
                10: root,
                11: stable_child,
                12: late_identity,
            }[pid],
        )
        monkeypatch.setattr(pc, "_posix_process_identity_map", lambda root_pid=None: None)

        assert pc.process_descendant_identities(10) is None

    def test_posix_fallback_excludes_orphan_older_than_recycled_parent(self, monkeypatch):
        snapshot = {
            10: pc._PosixProcessSnapshotRow(1, "Mon Jan  1 00:00:00 2024"),
            11: pc._PosixProcessSnapshotRow(10, "Mon Jan  1 00:00:02 2024"),
            12: pc._PosixProcessSnapshotRow(11, "Mon Jan  1 00:00:01 2024"),
        }
        snapshots = iter((snapshot, snapshot))
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "freebsd14")
        monkeypatch.setattr(pc, "_posix_process_snapshot", lambda: next(snapshots))

        assert pc.process_descendant_identities(10) == [
            pc.ProcessDescendantIdentity(
                11,
                10,
                "Mon Jan  1 00:00:02 2024",
                pc.ProcessIdentitySource.LSTART,
            )
        ]

    def test_process_start_order_is_three_way(self):
        order = pc._ProcessStartOrder

        assert pc._process_start_order("200", "100") is order.LATER
        assert pc._process_start_order("100", "200") is order.EARLIER
        assert pc._process_start_order("100", "100") is order.INCONCLUSIVE
        assert (
            pc._process_start_order(
                "Mon Jan  1 00:00:01 2024",
                "Mon Jan  1 00:00:00 2024",
            )
            is order.LATER
        )
        assert (
            pc._process_start_order(
                "Mon Jan  1 00:00:00 2024",
                "Mon Jan  1 00:00:00 2024",
            )
            is order.INCONCLUSIVE
        )
        assert pc._process_start_order("unknown", "tokens") is order.INCONCLUSIVE

    def test_created_after_wraps_the_shared_process_start_order(self, monkeypatch):
        calls = []

        def _order(child_token, parent_token):
            calls.append((child_token, parent_token))
            return pc._ProcessStartOrder.LATER

        monkeypatch.setattr(pc, "_process_start_order", _order)

        assert pc.created_after("200", "100") is True
        assert calls == [("200", "100")]

    def test_posix_fallback_same_second_edge_is_inconclusive(self, monkeypatch):
        snapshot = {
            10: pc._PosixProcessSnapshotRow(1, "Mon Jan  1 00:00:00 2024"),
            11: pc._PosixProcessSnapshotRow(10, "Mon Jan  1 00:00:00 2024"),
        }
        snapshots = iter((snapshot, snapshot))
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc.sys, "platform", "freebsd14")
        monkeypatch.setattr(pc, "_posix_process_snapshot", lambda: next(snapshots))

        assert pc.process_descendant_identities(10) is None

    def test_windows_identity_walk_excludes_orphan_older_than_recycled_parent(self, monkeypatch):
        parent_map = {10: 1, 11: 10, 12: 11}
        maps = iter((parent_map, parent_map))
        start_ids = {10: "100", 11: "300", 12: "200"}
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: next(maps))
        monkeypatch.setattr(pc, "get_process_start_id", start_ids.__getitem__)

        assert pc.process_descendant_identities(10) == [pc.ProcessDescendantIdentity(11, 10, "300")]

    def test_posix_parent_map_derives_from_the_shared_process_snapshot(self, monkeypatch):
        processes = {
            10: pc._PosixProcessSnapshotRow(1, "root"),
            11: pc._PosixProcessSnapshotRow(10, "child"),
        }
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc, "trusted_system_bin", lambda name: "/usr/bin/ps")
        monkeypatch.setattr(pc, "_posix_process_snapshot", lambda: processes)
        monkeypatch.setattr(
            pc.subprocess,
            "check_output",
            lambda *args, **kwargs: pytest.fail("the parent map must not run a second ps parser"),
        )

        assert pc._posix_process_parent_map() == {10: 1, 11: 10}

    def test_posix_fallback_requires_identity_stable_across_snapshots(self, monkeypatch):
        runs: list[list[str]] = []
        output = (
            b"10 1 Mon Jan  1 00:00:00 2024\n"
            b"11 10 Mon Jan  1 00:00:01 2024\n"
            b"12 11 Mon Jan  1 00:00:02 2024\n"
        )
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc.sys, "platform", "freebsd14")
        monkeypatch.setattr(pc, "trusted_system_bin", lambda name: "/usr/bin/ps")
        monkeypatch.setattr(
            pc,
            "get_process_start_id",
            lambda pid: pytest.fail("the fallback must not resolve every host pid"),
        )

        def _capture(argv, **kwargs):
            runs.append(list(argv))
            return output

        monkeypatch.setattr(pc.subprocess, "check_output", _capture)

        assert pc.process_descendant_identities(10) == [
            pc.ProcessDescendantIdentity(
                11,
                10,
                "Mon Jan  1 00:00:01 2024",
                pc.ProcessIdentitySource.LSTART,
            ),
            pc.ProcessDescendantIdentity(
                12,
                11,
                "Mon Jan  1 00:00:02 2024",
                pc.ProcessIdentitySource.LSTART,
            ),
        ]
        assert runs == [
            ["/usr/bin/ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "lstart="],
            ["/usr/bin/ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "lstart="],
        ]

    @pytest.mark.parametrize(
        "scenario",
        ["root-recycled", "child-reparented"],
    )
    def test_posix_fallback_rejects_root_subtree_drift(self, monkeypatch, scenario):
        before = {
            10: pc._PosixProcessSnapshotRow(1, "root-old"),
            11: pc._PosixProcessSnapshotRow(10, "child-old"),
        }
        after = {
            10: pc._PosixProcessSnapshotRow(
                1,
                "root-new" if scenario == "root-recycled" else "root-old",
            ),
            11: pc._PosixProcessSnapshotRow(
                99 if scenario == "child-reparented" else 10,
                "child-old",
            ),
        }
        snapshots = iter((before, after))
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "IS_POSIX", True)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(pc.sys, "platform", "freebsd14")
        monkeypatch.setattr(pc, "_posix_process_snapshot", lambda: next(snapshots))

        assert pc.process_descendant_identities(10) is None

    @pytest.mark.parametrize("recycled", [False, True], ids=["stable", "recycled"])
    def test_windows_descendant_identities_are_creation_time_bound(self, monkeypatch, recycled):
        parent_map = {10: 1, 11: 10}
        maps = iter((parent_map, parent_map))
        reads = {10: 0, 11: 0}

        def _start_id(pid: int) -> str:
            reads[pid] += 1
            if pid == 10:
                return "100"
            if recycled and reads[pid] > 1:
                return "300"
            return "200"

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "IS_LINUX", False)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: next(maps))
        monkeypatch.setattr(pc, "get_process_start_id", _start_id)

        identities = pc.process_descendant_identities(10)

        if recycled:
            assert identities is None
        else:
            assert identities == [pc.ProcessDescendantIdentity(11, 10, "200")]

    @pytest.mark.asyncio
    async def test_descendant_termination_handles_async_is_empty_on_posix(self):
        if pc.IS_WINDOWS:
            pytest.skip("POSIX process groups do not need retained descendants")

        assert await pc.descendant_termination_handles_async(os.getpid()) == {}

    def test_windows_parent_map_raises_when_snapshot_creation_fails(self, monkeypatch):
        class FakeCall:
            def __init__(self, result):
                self.result = result

            def __call__(self, *_args):
                return self.result

        kernel32 = types.SimpleNamespace(
            CreateToolhelp32Snapshot=FakeCall(pc.wintypes.HANDLE(-1).value),
            Process32First=FakeCall(False),
            Process32Next=FakeCall(False),
            CloseHandle=FakeCall(True),
        )
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc.ctypes,
            "windll",
            types.SimpleNamespace(kernel32=kernel32),
            raising=False,
        )

        with pytest.raises(OSError, match="process snapshot"):
            pc._windows_process_parent_map()

    def test_windows_parent_map_raises_when_initial_enumeration_fails(self, monkeypatch):
        class FakeCall:
            def __init__(self, result):
                self.result = result
                self.calls = 0

            def __call__(self, *_args):
                self.calls += 1
                return self.result

        close_handle = FakeCall(True)
        kernel32 = types.SimpleNamespace(
            CreateToolhelp32Snapshot=FakeCall(123),
            Process32First=FakeCall(False),
            Process32Next=FakeCall(False),
            CloseHandle=close_handle,
        )
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc.ctypes,
            "windll",
            types.SimpleNamespace(kernel32=kernel32),
            raising=False,
        )

        with pytest.raises(OSError, match="first process"):
            pc._windows_process_parent_map()

        assert close_handle.calls == 1

    def test_windows_parent_map_raises_when_later_enumeration_fails(self, monkeypatch):
        class FakeCall:
            def __init__(self, result):
                self.result = result
                self.calls = 0

            def __call__(self, *_args):
                self.calls += 1
                return self.result

        close_handle = FakeCall(True)
        kernel32 = types.SimpleNamespace(
            CreateToolhelp32Snapshot=FakeCall(123),
            Process32First=FakeCall(True),
            Process32Next=FakeCall(False),
            CloseHandle=close_handle,
            SetLastError=FakeCall(True),
            GetLastError=FakeCall(5),
        )
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc.ctypes,
            "windll",
            types.SimpleNamespace(kernel32=kernel32),
            raising=False,
        )
        with pytest.raises(OSError, match="process enumeration"):
            pc._windows_process_parent_map()

        assert close_handle.calls == 1

    def test_windows_descendant_lifetime_accepts_genuine_pre_exit_child(
        self,
        monkeypatch,
    ):
        parent_maps = iter(({101: 100}, {101: 100}))
        closed: list[int] = []
        identities = {
            8001: (100, 10, 20),
            9001: (101, 15, None),
        }
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: next(parent_maps))
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid, **_k: 9001)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            identities.get,
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)

        assert pc.descendant_termination_handles(100, {}, 8001) == {101: 9001}
        assert closed == []

    def test_windows_descendant_lifetime_rejects_post_exit_recycled_child(
        self,
        monkeypatch,
    ):
        parent_maps = iter(({101: 100}, {101: 100}))
        closed: list[int] = []
        identities = {
            8001: (100, 10, 20),
            9001: (101, 21, None),
        }
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: next(parent_maps))
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid, **_k: 9001)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            identities.get,
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)

        assert pc.descendant_termination_handles(100, {}, 8001) == {}
        assert closed == [9001]

    @pytest.mark.parametrize(
        "scenario",
        [
            "unopenable_live",
            "unopenable_vanished",
            "unopenable_snapshot_error",
            "partial_open_error",
            "first_identity_unreadable",
            "retained_identity_unreadable",
            "vanished_parent_live_child",
        ],
    )
    def test_windows_descendant_snapshot_must_account_for_every_candidate(
        self, monkeypatch, scenario
    ):
        first_map = {101: 100, 102: 100}
        second_map = dict(first_map)
        identities = {8001: (100, 10, None), 9001: (101, 20, None)}
        retained = {101: 9001} if scenario == "retained_identity_unreadable" else {}
        if scenario in {"unopenable_vanished", "vanished_parent_live_child"}:
            second_map.pop(102)
        if scenario == "vanished_parent_live_child":
            first_map[101] = second_map[101] = 102
        if scenario in {"first_identity_unreadable", "retained_identity_unreadable"}:
            identities.pop(9001)
            identities[9002] = (102, 30, None)
        scans = 0
        closed: list[int] = []

        def snapshot():
            nonlocal scans
            scans += 1
            if scans > 1 and scenario == "unopenable_snapshot_error":
                raise OSError("fresh snapshot unavailable")
            return first_map if scans == 1 else second_map

        def open_handle(child_pid, **_kwargs):
            if child_pid == 101:
                return 9001
            if scenario == "partial_open_error":
                raise OSError("opening failed")
            return 9002 if 9002 in identities else None

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
        monkeypatch.setattr(pc, "_open_process_termination_handle", open_handle)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", identities.get)
        # Query denial can produce False; it must not certify disappearance.
        monkeypatch.setattr(pc, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(pc, "_windows_process_query_diagnostic", lambda _pid: "identity=None")
        monkeypatch.setattr(pc, "close_process_handle", closed.append)

        if scenario == "unopenable_vanished":
            assert pc.descendant_termination_handles(100, retained, 8001) == {101: 9001}
            assert scans >= 2
            assert closed == []
        else:
            with pytest.raises(OSError):
                pc.descendant_termination_handles(100, retained, 8001)
            expected_closed = [9001] if not retained else []
            if 9002 in identities:
                expected_closed.append(9002)
            assert sorted(closed) == expected_closed
        assert 8001 not in closed
        if retained:
            assert 9001 not in closed

    @pytest.mark.parametrize("partial_open", [False, True])
    @pytest.mark.parametrize("descendants", [{}, {103: 102}, {103: 102, 104: 103}])
    def test_windows_vanished_unopened_parent_cannot_hide_new_descendants(
        self, monkeypatch, partial_open, descendants
    ):
        first_map = {102: 100, 105: 100}
        fresh_map = {105: 100, **descendants}
        retained = {105: 9005}
        identities = {8001: (100, 10, None), 9005: (105, 20, None)}
        if partial_open:
            first_map[101] = fresh_map[101] = 100
            identities[9001] = (101, 20, None)
        scans = 0
        opened: list[int] = []
        closed: list[int] = []

        def snapshot():
            nonlocal scans
            scans += 1
            assert scans <= (2 if descendants or not partial_open else 3)
            return first_map if scans == 1 else fresh_map

        def open_handle(child_pid, **_kwargs):
            opened.append(child_pid)
            assert child_pid in {101, 102}, "fresh PIDs must not acquire kill authority"
            return 9001 if child_pid == 101 else None

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
        monkeypatch.setattr(pc, "_open_process_termination_handle", open_handle)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", identities.get)
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        if descendants:
            with pytest.raises(OSError, match="vanished unopened parents") as exc:
                pc.descendant_termination_handles(100, retained, 8001)
            assert "103" in str(exc.value) and "102" in str(exc.value)
            assert closed == ([9001] if partial_open else [])
        else:
            assert pc.descendant_termination_handles(100, retained, 8001) == (
                {101: 9001} if partial_open else {}
            )
            assert closed == []
        assert opened == ([101, 102] if partial_open else [102])
        assert scans == (2 if descendants or not partial_open else 3)
        assert 8001 not in closed and 9005 not in closed

    def test_windows_vanished_unopened_parent_diagnostic_ids_are_bounded(self, monkeypatch):
        first_map = {child: 100 for child in range(101, 111)}
        fresh_map = {child + 100: child for child in first_map}
        maps = iter((first_map, fresh_map))
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: next(maps))
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid, **_k: None)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (100, 10, None))
        with pytest.raises(OSError, match="vanished unopened parents") as exc:
            pc.descendant_termination_handles(100, {}, 8001)
        message = str(exc.value)
        assert "total=10" in message
        assert "[(201, 101), (202, 102), (203, 103)]" in message
        assert "204" not in message and "104" not in message

    @pytest.mark.parametrize("outcome", ["live", "vanished", "snapshot_error"])
    def test_windows_only_unopenable_child_requires_fresh_absence(self, monkeypatch, outcome):
        scans = 0

        def snapshot():
            nonlocal scans
            scans += 1
            if scans > 1:
                if outcome == "snapshot_error":
                    raise OSError("fresh snapshot unavailable")
                if outcome == "vanished":
                    return {}
            return {101: 100}

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", snapshot)
        monkeypatch.setattr(pc, "_open_process_termination_handle", lambda _pid, **_k: None)
        monkeypatch.setattr(pc, "_windows_process_handle_identity", lambda _h: (100, 10, None))
        monkeypatch.setattr(pc, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(pc, "_windows_process_query_diagnostic", lambda _pid: "identity=None")
        if outcome == "vanished":
            assert pc.descendant_termination_handles(100, {}, 8001) == {}
        else:
            with pytest.raises(OSError):
                pc.descendant_termination_handles(100, {}, 8001)
        assert scans == 2

    @pytest.mark.parametrize(
        "scenario, expected",
        [
            ("exited", {101, 102}),
            ("retained", {102}),
            ("early_exit", {101}),
            ("equal_exit", {101}),
            ("still_live", set()),
            ("unreadable", set()),
            ("recycled_intermediate", set()),
            ("recycled_root", set()),
            ("recycled_child", {101}),
            ("changed_exit", set()),
            ("retained_early_exit", set()),
            ("changed_parent", {101}),
            ("missing_live_child", set()),
            ("stale_first_edge", {101}),
            ("snapshot_error", set()),
        ],
    )
    def test_windows_observed_chain_survives_intermediate_exit(
        self, monkeypatch, scenario, expected
    ):
        # Both handles are pinned while the first map still contains 100->101->102.
        # The second map loses 101, not the proof that 102 was its genuine child.
        first_map = {101: 100, 102: 101}
        second_map = {102: 101}
        first_ids = {8001: (100, 10, None), 9001: (101, 20, None), 9002: (102, 30, None)}
        second_ids = {**first_ids, 9001: (101, 20, 40)}
        if scenario in {"early_exit", "retained_early_exit"}:
            second_ids[9001] = (101, 20, 25)
        elif scenario == "equal_exit":
            second_ids[9001] = (101, 20, 30)
        elif scenario == "still_live":
            second_ids[9001] = first_ids[9001]
        elif scenario == "unreadable":
            second_ids.pop(9001)
        elif scenario == "recycled_intermediate":
            second_ids[9001] = (101, 21, 40)
            second_map[101] = 100
        elif scenario == "recycled_root":
            second_ids[8001] = (100, 11, None)
            second_map[101] = 100
        elif scenario == "recycled_child":
            second_ids[9002] = (102, 31, None)
            second_map[101] = 100
        elif scenario == "changed_exit":
            first_ids[9001] = (101, 20, 35)
        elif scenario == "changed_parent":
            second_map[102] = 999
        elif scenario == "missing_live_child":
            second_map.clear()
        elif scenario == "stale_first_edge":
            first_ids[9002] = second_ids[9002] = (102, 15, None)
        scans = 0
        closed: list[int] = []
        opened: list[int] = []
        retained = {101: 9001} if scenario in {"retained", "retained_early_exit"} else {}

        def parent_map():
            nonlocal scans
            scans += 1
            if scans == 2 and scenario == "snapshot_error":
                raise OSError("second snapshot failed")
            return first_map if scans == 1 else second_map

        def open_handle(child_pid, **_kwargs):
            opened.append(child_pid)
            return {101: 9001, 102: 9002}[child_pid]

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_windows_process_parent_map", parent_map)
        monkeypatch.setattr(pc, "_open_process_termination_handle", open_handle)
        monkeypatch.setattr(
            pc,
            "_windows_process_handle_identity",
            lambda handle: (first_ids if scans < 2 else second_ids).get(handle),
        )
        monkeypatch.setattr(pc, "close_process_handle", closed.append)
        if scenario in {"snapshot_error", "unreadable", "still_live", "missing_live_child"}:
            with pytest.raises(OSError):
                pc.descendant_termination_handles(100, retained, 8001)
        else:
            result = pc.descendant_termination_handles(100, retained, 8001)
            assert result == {
                child_pid: {101: 9001, 102: 9002}[child_pid] for child_pid in expected
            }
        assert opened == ([102] if retained else [101, 102])
        assert sorted(closed) == sorted(
            {101: 9001, 102: 9002}[child_pid] for child_pid in set(opened) - expected
        )
        # Neither a retained handle nor the root transfers ownership on rejection.
        assert 8001 not in closed
        if retained:
            assert 9001 not in closed

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows process handles only")
    def test_native_observed_grandchild_survives_snapshot_gap(self, monkeypatch, tmp_path):
        # Use the base interpreter, not the Windows venv redirector's extra PID.
        python = getattr(sys, "_base_executable", sys.executable)
        leaf_code = (
            "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
            "(p/'ready').write_text('ready'); deadline=time.monotonic()+30\n"
            "while not (p/'abort').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        )
        middle_code = (
            "import pathlib,subprocess,sys,time; p=pathlib.Path(sys.argv[1]); "
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[2],str(p)]); "
            "(p/'leaf').write_text(str(child.pid)); deadline=time.monotonic()+20\n"
            "while not (p/'exit').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            "if (p/'abort').exists(): child.kill(); child.wait(timeout=5)\n"
        )
        root_code = (
            "import pathlib,subprocess,sys,time; p=pathlib.Path(sys.argv[1]); "
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[2],str(p),sys.argv[3]]); "
            "(p/'middle').write_text(str(child.pid)); child.wait(timeout=25); time.sleep(30)"
        )
        root = subprocess.Popen(
            [python, "-c", root_code, str(tmp_path), middle_code, leaf_code],
            creationflags=pc.CREATE_NEW_PROCESS_GROUP,
        )
        cleanup: dict[int, int] = {}
        handles: dict[int, int] = {}
        try:
            # Pin our own objects separately so a regression dropping returned
            # handles cannot strand the native fixture's processes in teardown.
            wait_until(
                lambda: all(
                    (tmp_path / name).exists() and (tmp_path / name).stat().st_size
                    for name in ("middle", "leaf", "ready")
                ),
                timeout=10,
                describe=lambda: "owned process chain did not start",
            )
            middle = int((tmp_path / "middle").read_text())
            leaf = int((tmp_path / "leaf").read_text())
            for child_pid in (root.pid, middle, leaf):
                handle = pc._open_process_termination_handle(child_pid)
                assert handle is not None
                cleanup[child_pid] = handle
            snapshot = pc._windows_process_parent_map
            scans = 0

            def parent_map():
                nonlocal scans
                scans += 1
                if scans == 2:
                    (tmp_path / "exit").write_text("exit", encoding="utf-8")
                    wait_until(
                        lambda: not pc.process_handle_active(cleanup[middle]),
                        timeout=5,
                        describe=lambda: "owned intermediary did not exit",
                    )
                result = snapshot()
                if scans == 1:
                    assert result[middle] == root.pid and result[leaf] == middle
                else:
                    # Exit status can precede removal from Toolhelp. Wait for
                    # the snapshot gap this fixture intends to exercise.
                    def _poll_snapshot():
                        nonlocal result
                        result = snapshot()
                        return middle not in result

                    wait_until(_poll_snapshot, timeout=5)
                    assert middle not in result and result[leaf] == middle
                return result

            monkeypatch.setattr(pc, "_windows_process_parent_map", parent_map)
            handles = pc.descendant_termination_handles(root.pid, {}, cleanup[root.pid])
            assert leaf in handles
            assert pc.process_handle_active(handles[leaf])
            assert pc.terminate_process_handle(handles[leaf])
            wait_until(
                lambda: not pc.process_handle_active(cleanup[leaf]),
                timeout=5,
                describe=lambda: "retained grandchild did not terminate",
            )
        finally:
            # Kill only objects this test spawned, never services or a PID tree.
            (tmp_path / "abort").touch()
            (tmp_path / "exit").touch()
            try:
                for handle in cleanup.values():
                    if pc.process_handle_active(handle):
                        pc.terminate_process_handle(handle)
                if root.poll() is None:
                    root.kill()
                root.wait(timeout=5)
                wait_until(
                    lambda: not any(pc.process_handle_active(h) for h in cleanup.values()),
                    timeout=5,
                    describe=lambda: "owned fixture process survived cleanup",
                )
            finally:
                for handle in (*handles.values(), *cleanup.values()):
                    pc.close_process_handle(handle)

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows process handles only")
    def test_retained_handle_targets_original_windows_child(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=pc.CREATE_NEW_PROCESS_GROUP,
        )
        handles: dict[int, int] = {}
        root_handle = pc._open_process_termination_handle(os.getpid())
        assert root_handle is not None
        try:

            def _find_handle():
                handles.update(pc.descendant_termination_handles(os.getpid(), handles, root_handle))
                return child.pid in handles

            wait_until(_find_handle, timeout=5)
            assert child.pid in handles
            assert pc.terminate_process_handle(handles[child.pid]) is True
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            for handle in handles.values():
                pc.close_process_handle(handle)
            pc.close_process_handle(root_handle)


def test_process_descendants_snapshots_a_new_session_grandchild():
    """A grandchild in its OWN session is still a descendant.

    This is the case a bare ``killpg`` misses, so the walk that broadens a kill
    must be able to see it.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:  # pragma: no cover - POSIX session semantics
        pytest.skip("POSIX session semantics")

    grandchild: int | None = None
    child_code = (
        "import subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "start_new_session=True);"
        "print(c.pid,flush=True);time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        # flake-ok: the child prints its grandchild's pid first; the run's --timeout bounds a child that never does
        grandchild = int(proc.stdout.readline().strip())
        wait_until(
            lambda: grandchild in platform_compat.process_descendants(proc.pid),
            timeout=5.0,
        )
        descendants = platform_compat.process_descendants(proc.pid)
        assert grandchild in descendants
        # It is genuinely outside the parent's process group -- otherwise this
        # test would pass even without the escape it exists to describe.
        assert os.getpgid(grandchild) != os.getpgid(proc.pid)
    finally:
        for pid in (grandchild, proc.pid):
            if pid is None:
                continue
            try:
                platform_compat.kill_process_tree(pid)
            except (ProcessLookupError, OSError, ValueError):
                pass
        proc.wait(timeout=5)


def test_process_descendants_is_best_effort_on_unreadable_table(monkeypatch):
    """Introspection failure must not raise into a caller's kill path."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(
        platform_compat,
        "_posix_process_parent_map",
        lambda: (_ for _ in ()).throw(OSError("boom")),
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_process_parent_map",
        lambda: (_ for _ in ()).throw(OSError("boom")),
    )
    assert platform_compat.process_descendants(os.getpid()) == []


def test_process_descendants_refuses_reserved_pids():
    from kiro_crew import platform_compat

    assert platform_compat.process_descendants(1) == []
    assert platform_compat.process_descendants(0) == []
