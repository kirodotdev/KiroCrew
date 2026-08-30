"""The ONE ``/proc`` subtree walker, and the two callers that read off it.

``mcp_gateway.pool`` and ``subagent`` each used to carry a line-for-line copy of
the same breadth-first walk over ``/proc/<pid>/task/<tid>/children``, each with
its own ``256`` process ceiling (#6096). Two copies of a walk are two copies of
its ceiling and two copies of its sentinels, which is the drift the earlier
consolidation *inside* ``subagent`` (#3970) was itself about.

So this module asserts three things:

* the walker's own contract — the per-process reads, the summed readings, the
  sentinel each reading falls back to, and the single ceiling;
* that a caller pays only for the readings it asks for, because that is what
  made sharing possible: the pool wants RSS and no CPU, the Sessions rows want
  CPU and no RSS, and an RSS-only caller with an unreadable root must not walk
  at all (the early return the pool's own copy had);
* that neither caller has a second walk any more — by name, and behaviourally,
  by installing ONE fake process tree at the shared module and watching both
  callers traverse it.
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew import subagent as sa
from kiro_crew.mcp_gateway import pool as pool_mod
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey

#: 1 root + 3 children, two of which match the caller's needles.
TREE = {10: [11, 12, 13], 11: [], 12: [], 13: []}
RSS = {10: 1000, 11: 200, 12: 30, 13: 4}
JIFFIES = {10: 100, 11: 50, 12: 25, 13: 10}
MATCHING = {11, 12}


def _fake_tree(*, needles: tuple[str, ...] = ()):
    """Patch the shared per-process reads to describe :data:`TREE`."""
    return (
        patch.object(platform_compat, "IS_LINUX", True),
        patch.object(platform_compat, "_proc_status_rss_kb", side_effect=lambda p: RSS.get(p, -1)),
        patch.object(platform_compat, "_proc_cpu_jiffies", side_effect=lambda p: JIFFIES.get(p, 0)),
        patch.object(platform_compat, "_proc_children", side_effect=lambda p: TREE.get(p, [])),
        patch.object(
            platform_compat,
            "process_matches",
            side_effect=lambda p, n: p in MATCHING and n == needles,
        ),
    )


class _FakeTree:
    """:func:`_fake_tree` as a context manager, since it patches five names."""

    def __init__(self, *, needles: tuple[str, ...] = ()) -> None:
        self._patches = _fake_tree(needles=needles)

    def __enter__(self) -> None:
        for p in self._patches:
            p.start()

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


# ── the per-process reads the walk is built from ───────────────────────────


class TestPerProcessReads:
    def test_status_rss_parses_vmrss(self) -> None:
        with patch("builtins.open", mock_open(read_data="Name:\tx\nVmRSS:\t 2048 kB\n")):
            assert platform_compat._proc_status_rss_kb(1234) == 2048

    def test_status_rss_unreadable_is_minus_one(self) -> None:
        with patch("builtins.open", side_effect=OSError):
            assert platform_compat._proc_status_rss_kb(1234) == -1

    def test_children_listing_unavailable(self) -> None:
        with patch.object(os, "listdir", side_effect=OSError):
            assert platform_compat._proc_children(1234) == []

    def test_children_parsed_per_thread(self) -> None:
        with (
            patch.object(os, "listdir", return_value=["1234"]),
            patch("builtins.open", mock_open(read_data="11 12 13")),
        ):
            assert platform_compat._proc_children(1234) == [11, 12, 13]

    def test_children_garbage_skipped(self) -> None:
        with (
            patch.object(os, "listdir", return_value=["1234"]),
            patch("builtins.open", mock_open(read_data="not-a-pid")),
        ):
            assert platform_compat._proc_children(1234) == []

    def test_jiffies_parsed_past_a_comm_with_parens(self) -> None:
        # comm with spaces + an embedded ')' — rindex must find the real close.
        # post-comm tokens: state(0) ... utime(11)=120 stime(12)=60
        stat = b"1234 (kiro cli (node)) S 2 3 4 5 6 7 8 9 10 11 120 60 0 0"
        assert platform_compat._parse_cpu_jiffies(stat) == 180

    @pytest.mark.parametrize("raw", [b"", b"no-parens-here", b"1 (x) S 1 2 3"])
    def test_malformed_jiffies_are_zero(self, raw: bytes) -> None:
        assert platform_compat._parse_cpu_jiffies(raw) == 0

    def test_jiffies_unreadable_pid_is_zero(self) -> None:
        with patch("builtins.open", side_effect=OSError):
            assert platform_compat._proc_cpu_jiffies(1234) == 0

    def test_jiffies_reads_stat(self) -> None:
        with (
            patch.object(platform_compat, "_parse_cpu_jiffies", return_value=77),
            patch("builtins.open", mock_open(read_data=b"whatever")),
        ):
            assert platform_compat._proc_cpu_jiffies(1234) == 77


# ── the walk ──────────────────────────────────────────────────────────────


class TestSubtreeWalk:
    def test_one_walk_carries_every_reading(self) -> None:
        with _FakeTree(needles=("stub",)):
            sample = platform_compat.proc_subtree_sample(10, counts=True, needles=("stub",))
        assert sample.rss_kb == sum(RSS.values())
        assert sample.jiffies == sum(JIFFIES.values())
        assert (sample.procs, sample.matched) == (4, 2)

    def test_each_process_is_visited_once(self) -> None:
        seen: list[int] = []
        with (
            _FakeTree(),
            patch.object(
                platform_compat,
                "_proc_children",
                side_effect=lambda p: (seen.append(p), TREE.get(p, []))[1],
            ),
        ):
            platform_compat.proc_subtree_sample(10, counts=True)
        assert sorted(seen) == [10, 11, 12, 13]

    def test_no_pid_is_unmeasurable_in_every_column(self) -> None:
        assert platform_compat.proc_subtree_sample(None) == platform_compat.SubtreeSample(
            -1, 0, None, None
        )
        assert platform_compat.proc_subtree_sample(0).rss_kb == -1

    def test_rss_sums_descendants(self) -> None:
        with _FakeTree():
            assert platform_compat.proc_subtree_sample(10).rss_kb == sum(RSS.values())

    def test_cycles_and_dead_children_do_not_break_the_sum(self) -> None:
        with (
            patch.object(platform_compat, "_proc_status_rss_kb", lambda p: 100 if p == 1 else -1),
            patch.object(platform_compat, "_proc_children", lambda p: [1, 2] if p == 1 else []),
        ):
            assert platform_compat.proc_subtree_sample(1, jiffies=False).rss_kb == 100

    def test_non_linux_loses_only_the_counts(self) -> None:
        """Counts are ``None`` off Linux, but RSS and CPU keep their own
        sentinels — the columns are unmeasurable in different ways."""
        with _FakeTree(), patch.object(platform_compat, "IS_LINUX", False):
            sample = platform_compat.proc_subtree_sample(10, counts=True)
        assert (sample.procs, sample.matched) == (None, None)
        assert sample.rss_kb == sum(RSS.values())
        assert sample.jiffies == sum(JIFFIES.values())

    def test_dead_root_keeps_the_cpu_walk(self) -> None:
        """An unreadable root status means RSS and the counts have nothing to
        attribute, but jiffies is consumed as a *delta*, so its walk stands."""
        with _FakeTree(), patch.object(platform_compat, "_proc_status_rss_kb", return_value=-1):
            sample = platform_compat.proc_subtree_sample(10, counts=True)
        assert sample.rss_kb == -1
        assert (sample.procs, sample.matched) == (None, None)
        assert sample.jiffies == sum(JIFFIES.values())

    def test_the_walk_is_bounded_by_one_ceiling(self) -> None:
        with (
            patch.object(platform_compat, "IS_LINUX", True),
            patch.object(platform_compat, "_proc_status_rss_kb", return_value=1024),
            patch.object(platform_compat, "_proc_cpu_jiffies", return_value=1),
            patch.object(
                platform_compat, "_proc_children", side_effect=lambda p: [p * 10, p * 10 + 1]
            ),
            patch.object(platform_compat, "process_matches", return_value=False),
        ):
            sample = platform_compat.proc_subtree_sample(2, counts=True)
        assert sample.matched == 0
        assert sample.procs is not None
        assert sample.procs < platform_compat._SUBTREE_MAX_PROCS * 3
        assert platform_compat._SUBTREE_MAX_PROCS > 0


# ── a caller pays only for what it asks for ───────────────────────────────


class TestSkippedReadingsCostNothing:
    """Sharing one walk must not make either caller slower than its own copy.

    The pool's copy read ``status`` and never ``stat``; the Sessions CPU reader
    read ``stat`` and never ``status``. Both properties have to survive, or
    consolidating would have bought one home at the price of per-process reads
    nobody surfaces.
    """

    def test_an_rss_only_caller_reads_no_stat(self) -> None:
        with (
            _FakeTree(),
            patch.object(
                platform_compat,
                "_proc_cpu_jiffies",
                side_effect=AssertionError("CPU read on an RSS-only sample"),
            ),
        ):
            sample = platform_compat.proc_subtree_sample(10, jiffies=False)
        assert sample.rss_kb == sum(RSS.values())
        assert sample.jiffies == 0

    def test_a_cpu_only_caller_reads_no_status(self) -> None:
        with (
            _FakeTree(),
            patch.object(
                platform_compat,
                "_proc_status_rss_kb",
                side_effect=AssertionError("RSS read on a CPU-only sample"),
            ),
            patch.object(
                platform_compat,
                "process_matches",
                side_effect=AssertionError("cmdline match on a CPU-only sample"),
            ),
        ):
            sample = platform_compat.proc_subtree_sample(10, rss=False, counts=False)
        assert sample.jiffies == sum(JIFFIES.values())
        assert sample.rss_kb == -1

    def test_nothing_measurable_means_no_walk_at_all(self) -> None:
        """The early return the pool's own copy had: an RSS-only sample whose
        root status is unreadable answers -1 without touching a ``children``
        file, so a dead backend costs one read rather than a tree traversal."""
        with (
            _FakeTree(),
            patch.object(platform_compat, "_proc_status_rss_kb", return_value=-1),
            patch.object(
                platform_compat,
                "_proc_children",
                side_effect=AssertionError("walked a subtree with nothing to accumulate"),
            ),
        ):
            sample = platform_compat.proc_subtree_sample(10, jiffies=False)
        assert sample == platform_compat.SubtreeSample(-1, 0, None, None)


# ── one home: neither caller carries a second walk ────────────────────────


class TestOneHome:
    #: Names the two callers defined privately before #6096. A copy coming back
    #: would restore one of them, so their absence is the ratchet.
    RETIRED = (
        "_RSS_SUBTREE_MAX_PROCS",
        "_SUBTREE_MAX_PROCS",
        "_single_proc_rss_kb",
        "_proc_children",
        "_proc_cpu_jiffies",
        "_parse_cpu_jiffies",
        "_SubtreeSample",
    )

    @pytest.mark.parametrize("name", RETIRED)
    def test_the_pool_defines_no_walker_of_its_own(self, name: str) -> None:
        assert not hasattr(pool_mod, name), f"mcp_gateway.pool re-grew {name}"

    @pytest.mark.parametrize("name", RETIRED)
    def test_subagent_defines_no_walker_of_its_own(self, name: str) -> None:
        assert not hasattr(sa, name), f"subagent re-grew {name}"

    def test_both_callers_traverse_the_same_fake_tree(self) -> None:
        """The behavioural half of the ratchet: ONE fake installed on the shared
        module is observed by both callers, which is only possible if neither
        has a walk of its own left."""
        with _FakeTree(needles=(sa.STUB_MODULE,)):
            assert pool_mod._proc_rss_kb(10) == sum(RSS.values())
            assert sa._subtree_cpu_jiffies(10) == sum(JIFFIES.values())
            sample = sa._proc_subtree_sample(10)
        assert sample.rss_kb == sum(RSS.values())
        assert (sample.procs, sample.matched) == (4, 2)

    def test_the_subagent_adapter_counts_by_the_stub_module_path(self) -> None:
        """``matched`` is the stub count on the subagent side, so the needle it
        passes must be the module path the rewriter puts on a stub launch line."""
        seen: list[tuple[str, ...]] = []
        with (
            _FakeTree(),
            patch.object(
                platform_compat,
                "process_matches",
                side_effect=lambda p, n: bool(seen.append(n)) or p in MATCHING,
            ),
        ):
            sa._proc_subtree_sample(10)
        assert seen and all(n == (sa.STUB_MODULE,) for n in seen)


# ── the pool caller ───────────────────────────────────────────────────────


def _make_pool_key(server: str = "test-server", agent: str = "test-agent") -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name=agent,
        command_args_hash="abc123",
        effective_env_hash="def456",
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="ghi789",
        approval_mode="reads",
        trust_all_tools=False,
        config_snapshot_hash="jkl012",
    )


def _make_backend(pool_key: PoolKey, *, pid: int) -> Backend:
    proc = MagicMock()
    proc.returncode = None
    proc.pid = pid
    proc.wait = AsyncMock(return_value=0)
    now = time.monotonic()
    return Backend(
        pool_key=pool_key,
        process=proc,
        stdin=MagicMock(),
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )


class TestPoolReadsTheSharedWalk:
    @pytest.mark.asyncio
    async def test_backend_rss_is_the_whole_subtree(self) -> None:
        """A pooled backend is often a thin launcher, so its row must report the
        subtree total — the reason the pool summed a tree in the first place."""
        pool = BackendPool(max_backends=4)
        key = _make_pool_key()
        await pool.add(key, _make_backend(key, pid=10))
        with _FakeTree():
            snap = await pool.metrics_snapshot_async()
        assert [row["rss_kb"] for row in snap["backends"]] == [sum(RSS.values())]

    @pytest.mark.asyncio
    async def test_the_pool_pays_for_no_cpu_reading(self) -> None:
        """The pool surfaces RSS only. Asking the shared walker for CPU too
        would charge every pooled backend a ``/proc/<pid>/stat`` read per
        process for a figure no row displays."""
        pool = BackendPool(max_backends=4)
        key = _make_pool_key()
        await pool.add(key, _make_backend(key, pid=10))
        with (
            _FakeTree(),
            patch.object(
                platform_compat,
                "_proc_cpu_jiffies",
                side_effect=AssertionError("pool metrics read CPU jiffies"),
            ),
        ):
            snap = await pool.metrics_snapshot_async()
        assert snap["backends"][0]["rss_kb"] == sum(RSS.values())

    @pytest.mark.asyncio
    async def test_an_unreadable_backend_reports_minus_one(self) -> None:
        pool = BackendPool(max_backends=4)
        key = _make_pool_key()
        await pool.add(key, _make_backend(key, pid=10))
        with _FakeTree(), patch.object(platform_compat, "_proc_status_rss_kb", return_value=-1):
            snap = await pool.metrics_snapshot_async()
        assert snap["backends"][0]["rss_kb"] == -1
