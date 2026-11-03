"""The macOS branch of the shared subtree walker.

macOS has no ``/proc``, so :func:`platform_compat.proc_subtree_sample` walks the
tree through ``libproc`` there. Without it every reading on a Mac is a sentinel,
the cost sweep records no run, and ``cost_samples.jsonl`` is never written.
These tests pin the branch with a fake ``libproc`` tree, check that the cost
sweep records a sample, and read the real kernel on a Mac.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

from kiro_crew import platform_compat
from kiro_crew import subagent as sa

#: 1 root + 3 children, the same shape as the ``/proc`` walker tests.
TREE = {10: [11, 12, 13], 11: [], 12: [], 13: []}
FOOTPRINT = {10: 1000 * 1024, 11: 200 * 1024, 12: 30 * 1024, 13: 4 * 1024}  # bytes
CPU_NANOS = {10: 1_000_000_000, 11: 500_000_000, 12: 250_000_000, 13: 100_000_000}


@pytest.fixture
def fake_darwin(monkeypatch):
    """Route the walker down its macOS branch over :data:`TREE`."""
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "darwin_child_pids", lambda p: TREE.get(p, []))
    monkeypatch.setattr(
        platform_compat, "_darwin_process_phys_footprint_bytes", lambda p: FOOTPRINT.get(p)
    )
    monkeypatch.setattr(platform_compat, "_darwin_process_cpu_nanos", lambda p: CPU_NANOS.get(p))
    monkeypatch.setattr(platform_compat.os, "sysconf", lambda name: 100)


class TestDarwinSubtreeWalk:
    def test_memory_and_cpu_are_summed_over_the_tree(self, fake_darwin) -> None:
        sample = platform_compat.proc_subtree_sample(10, counts=True, needles=("x",))
        assert sample.rss_kb == 1234
        # 1.85 s of CPU at 100 ticks/s, the unit the Linux path returns.
        assert sample.jiffies == 185
        assert (sample.procs, sample.matched) == (None, None)
        assert sample.pss_kb == -1, "pss is only filled when asked for"

    def test_pss_carries_the_footprint_total(self, fake_darwin) -> None:
        assert platform_compat.proc_subtree_sample(10, pss=True).pss_kb == 1234

    def test_a_dead_root_keeps_the_cpu_walk(self, monkeypatch, fake_darwin) -> None:
        monkeypatch.setattr(
            platform_compat,
            "_darwin_process_phys_footprint_bytes",
            lambda p: None if p == 10 else FOOTPRINT[p],
        )
        sample = platform_compat.proc_subtree_sample(10, pss=True)
        assert (sample.rss_kb, sample.pss_kb) == (-1, -1)
        assert sample.jiffies == 185

    def test_an_rss_only_caller_reads_no_cpu(self, monkeypatch, fake_darwin) -> None:
        def _no_cpu(_pid):
            raise AssertionError("CPU read on an RSS-only sample")

        monkeypatch.setattr(platform_compat, "_darwin_process_cpu_nanos", _no_cpu)
        sample = platform_compat.proc_subtree_sample(10, jiffies=False)
        assert (sample.rss_kb, sample.jiffies) == (1234, 0)

    def test_a_cpu_only_caller_reads_no_memory(self, monkeypatch, fake_darwin) -> None:
        def _no_memory(_pid):
            raise AssertionError("memory read on a CPU-only sample")

        monkeypatch.setattr(platform_compat, "_darwin_process_phys_footprint_bytes", _no_memory)
        sample = platform_compat.proc_subtree_sample(10, rss=False)
        assert (sample.rss_kb, sample.jiffies) == (-1, 185)

    def test_nothing_measurable_means_no_walk(self, monkeypatch, fake_darwin) -> None:
        def _no_walk(_pid):
            raise AssertionError("walked a tree with nothing to read")

        monkeypatch.setattr(platform_compat, "darwin_child_pids", _no_walk)
        monkeypatch.setattr(platform_compat, "_darwin_process_phys_footprint_bytes", lambda p: None)
        assert platform_compat.proc_subtree_sample(10, jiffies=False) == (
            platform_compat.SubtreeSample(-1, 0, None, None)
        )

    def test_cycles_and_unreadable_children_do_not_break_the_sum(
        self, monkeypatch, fake_darwin
    ) -> None:
        monkeypatch.setattr(
            platform_compat, "darwin_child_pids", lambda p: [1, 2] if p == 1 else None
        )
        monkeypatch.setattr(
            platform_compat,
            "_darwin_process_phys_footprint_bytes",
            lambda p: 100 * 1024 if p == 1 else None,
        )
        assert platform_compat.proc_subtree_sample(1, jiffies=False).rss_kb == 100

    def test_the_walk_is_bounded_by_the_shared_ceiling(self, monkeypatch, fake_darwin) -> None:
        # A chain far longer than the ceiling: the walk must stop at it.
        monkeypatch.setattr(platform_compat, "darwin_child_pids", lambda p: [p + 1])
        monkeypatch.setattr(platform_compat, "_darwin_process_phys_footprint_bytes", lambda p: 1024)
        sample = platform_compat.proc_subtree_sample(1, jiffies=False)
        assert sample.rss_kb == platform_compat._SUBTREE_MAX_PROCS


class TestTheCostSweepRecordsOnMacOS:
    """A measured Mac run reaches the learned-cost store."""

    def test_a_swept_run_writes_a_cost_sample(self, monkeypatch, fake_darwin) -> None:
        sessions = MagicMock()
        sessions.get_agent_selection.return_value = ("template", "")
        mgr = sa.SubagentManager(sessions=sessions, ctx_builder=MagicMock(), max_concurrent=16)
        info = sa.SubagentInfo(id="a1", task="t", agent="kirocrew")
        info._pid = 10
        mgr._agents = {"a1": info}
        recorded: list = []
        monkeypatch.setattr(sa, "append_cost_sample", lambda *a, **k: recorded.append((a, k)))

        mgr._sample_live_costs()
        mgr._record_cost(info)

        assert info.peak_rss_gb == pytest.approx(1234 / (1024 * 1024))
        assert len(recorded) == 1, "the run was never recorded"
        bucket, peak_gb, _cpu = recorded[0][0]
        assert (bucket, peak_gb) == ("kirocrew", info.peak_rss_gb)


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the real libproc, which only a Mac has")
class TestTheRealDarwinKernel:
    """No fakes: our own process through the real ``libproc``."""

    def test_our_own_tree_reads_positive_memory_and_cpu(self) -> None:
        sample = platform_compat.proc_subtree_sample(os.getpid(), pss=True)
        assert sample.rss_kb > 0
        assert sample.pss_kb == sample.rss_kb
        assert sample.jiffies >= 0
