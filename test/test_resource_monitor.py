"""Tests for the chat resource sampler core: models and tree measurement.

The behaviours that a naive sampler gets wrong and that this task is responsible
for:

* an unreadable pid must NARROW a tree's RSS, not void it, and a wholly-unreadable
  tree must report ``None`` rather than ``0.0`` (unknown ≠ idle);
* a root pid that has vanished by walk time must drop the entry silently, never
  raise out of ``snapshot()``;
* the blocking ``/proc`` walk must run on ``subprocess_executor()``, never inline
  on the event loop.

The ``/proc`` layer is faked by monkeypatching the probes the module imports —
the same seam ``test_session_memory`` uses — so the tests are hermetic and do not
depend on the host's real process table or on the concurrently-authored runtime
registry.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from kiro_crew.acp import resource_monitor as rm
from kiro_crew.acp.runtime import _iter_descendant_pids

# Sentinel gateway self-pid, distinct from every runtime pid these tests use, so
# the gateway-self entry (task 2.3) does not collide with a runtime under test.
_GW = 999_001


class _FakeRuntime:
    """A live runtime as the sampler reads it: a pid plus identity attributes.

    Only the attributes ``_runtime_pid`` / ``_runtime_identity`` consult are
    provided, so the fake stays faithful to the structural contract without
    importing the real ``AcpRuntime``.
    """

    def __init__(
        self,
        pid: int | None,
        *,
        agent: str = "kirocrew",
        spawn_monotonic: float | None = 100.0,
    ) -> None:
        self.pid = pid
        self._agent = agent
        self._spawn_monotonic = spawn_monotonic


def _fake_linux_proc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tree: dict[int, list[int]],
    rss: dict[int, float | None],
) -> None:
    """Install a faked Linux ``/proc`` layer.

    ``tree`` maps a root pid to the pids ``_iter_descendant_pids`` returns for it
    (``[]`` models a root whose ``/proc`` entry is already gone). ``rss`` maps a
    pid to the MiB ``_get_rss_mb`` reads (``None`` models an unreadable pid).

    The gateway self-pid (task 2.3 walks the gateway tree too) is pinned to a
    sentinel with an EMPTY tree so it vanishes and emits no gateway entry — these
    2.1 tests assert on runtime entries alone. A test that wants to observe the
    gateway entry pins ``rm.os.getpid`` itself and seeds ``tree``.
    """
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm.os, "getpid", lambda: _GW)
    tree = {**tree, _GW: tree.get(_GW, [])}
    monkeypatch.setattr(
        rm, "_iter_descendant_pids", lambda pid, max_pids=None: list(tree.get(pid, [pid]))
    )
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: rss.get(pid))
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 0))


# ── data models ──────────────────────────────────────────────────────────────


def test_snapshot_defaults_are_neutral_not_zero() -> None:
    """A freshly-built snapshot must read as 'unknown host context', not as a
    host with zero memory — the host-context pass fills these later."""
    snap = rm.ResourceSnapshot(entries=[])
    assert snap.posture == "unknown"
    assert snap.available_gb == -1.0
    assert snap.host_total_gb is None
    assert snap.sampling_supported is True
    assert snap.interval_s == rm.DEFAULT_INTERVAL_S


def test_entry_carries_its_full_pid_set() -> None:
    entry = rm.EntrySample(
        kind="worker",
        session_key="",
        label="",
        agent="kirocrew",
        pid=7,
        proc_count=3,
        rss_mb=10.0,
        cpu_pct=None,
        uptime_s=5.0,
        pids=frozenset({7, 8, 9}),
    )
    assert entry.pids == frozenset({7, 8, 9})
    assert entry.slot == "" and entry.subagent_id == ""


# ── tree summing ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rss_is_summed_across_the_whole_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_linux_proc(
        monkeypatch,
        tree={7: [7, 8, 9]},
        rss={7: 100.0, 8: 20.0, 9: 5.5},
    )
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()

    assert len(snap.entries) == 1
    entry = snap.entries[0]
    assert entry.pid == 7
    assert entry.proc_count == 3
    assert entry.rss_mb == pytest.approx(125.5)
    assert entry.pids == frozenset({7, 8, 9})


@pytest.mark.asyncio
async def test_uptime_is_derived_from_spawn_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_linux_proc(monkeypatch, tree={7: [7]}, rss={7: 1.0})
    # Freeze the sampler's clock so uptime is deterministic.
    monkeypatch.setattr(rm.time, "monotonic", lambda: 250.0)
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7, spawn_monotonic=100.0)])
    snap = await sampler.snapshot()

    assert snap.entries[0].uptime_s == pytest.approx(150.0)
    assert snap.entries[0].agent == "kirocrew"


@pytest.mark.asyncio
async def test_uptime_unknown_when_spawn_time_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime with no recorded spawn time reports uptime as unknown, not 0."""
    _fake_linux_proc(monkeypatch, tree={7: [7]}, rss={7: 1.0})
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7, spawn_monotonic=None)])
    snap = await sampler.snapshot()
    assert snap.entries[0].uptime_s is None


# ── unreadable pid skip ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreadable_pid_narrows_the_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """One unreadable descendant must lower the sum, not void the whole tree."""
    _fake_linux_proc(
        monkeypatch,
        tree={7: [7, 8, 9]},
        rss={7: 100.0, 8: None, 9: 20.0},
    )
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()

    entry = snap.entries[0]
    # 8 is skipped; 7 + 9 remain. proc_count still counts all enumerated pids.
    assert entry.rss_mb == pytest.approx(120.0)
    assert entry.proc_count == 3


@pytest.mark.asyncio
async def test_whole_tree_unreadable_reports_none_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_linux_proc(
        monkeypatch,
        tree={7: [7, 8]},
        rss={7: None, 8: None},
    )
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()

    entry = snap.entries[0]
    assert entry.rss_mb is None
    # The entry is still present with its pid set — the tree exists, its memory
    # is merely unreadable.
    assert entry.proc_count == 2


# ── vanished root ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vanished_root_is_omitted_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root whose /proc entry is gone (empty descendant list) yields no entry
    and does not raise."""
    _fake_linux_proc(monkeypatch, tree={7: []}, rss={})
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()
    assert snap.entries == []


@pytest.mark.asyncio
async def test_runtime_without_a_pid_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_linux_proc(monkeypatch, tree={}, rss={})
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(None)])
    snap = await sampler.snapshot()
    assert snap.entries == []


@pytest.mark.asyncio
async def test_a_dying_pid_mid_walk_does_not_fail_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a per-root walk raises, the snapshot drops that root and keeps going for
    the healthy ones — a dying pid must never abort the whole pass."""
    monkeypatch.setattr(rm.sys, "platform", "linux")

    def _explode(pid: int, max_pids: int | None = None) -> list[int]:
        if pid == 7:
            raise OSError("vanished")
        if pid == _GW:
            return []  # gateway tree empty → no gateway entry to muddy the assert
        return [pid]

    monkeypatch.setattr(rm.os, "getpid", lambda: _GW)
    monkeypatch.setattr(rm, "_iter_descendant_pids", _explode)
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 5.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 0))

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7), _FakeRuntime(9)])
    snap = await sampler.snapshot()

    pids = {e.pid for e in snap.entries}
    assert pids == {9}


# ── shared runtime walked once ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_shared_root_pid_is_walked_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runtimes on ONE root pid must not walk the tree twice."""
    monkeypatch.setattr(rm.sys, "platform", "linux")
    calls: list[int] = []

    def _count(pid: int, max_pids: int | None = None) -> list[int]:
        if pid == _GW:
            return []  # gateway tree empty → excluded from the walk count
        calls.append(pid)
        return [pid]

    monkeypatch.setattr(rm.os, "getpid", lambda: _GW)
    monkeypatch.setattr(rm, "_iter_descendant_pids", _count)
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 0))

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7), _FakeRuntime(7)])
    await sampler.snapshot()
    assert calls.count(7) == 1


# ── non-Linux fallback ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_darwin_walks_the_ps_table_with_the_shared_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS reads one ``ps`` table (parent map + per-pid RSS) and walks it with
    the same bounded enumerator as Linux, so a runtime's row covers its whole
    tree with per-pid readings rather than one opaque total."""
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    monkeypatch.setattr(rm.os, "getpid", lambda: 999_999)
    children = {7: [70, 71], 70: [700]}
    rss_kib = {7: 10 * 1024, 70: 20 * 1024, 71: 30 * 1024, 700: 40 * 1024}
    monkeypatch.setattr(rm, "_host_process_table", lambda: (children, rss_kib, False))
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()

    entry = snap.entries[0]
    assert entry.rss_mb == pytest.approx(100.0)
    assert entry.pids == frozenset({7, 70, 71, 700})
    assert entry.proc_count == 4
    assert entry.cpu_pct is None  # no tick source off Linux
    assert entry.truncated is False


@pytest.mark.asyncio
async def test_darwin_tree_past_the_cap_is_truncated_like_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = rm.MAX_TREE_PIDS
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    monkeypatch.setattr(rm.os, "getpid", lambda: 999_999)
    kids = list(range(10_000, 10_000 + cap + 20))
    children = {7: kids}
    rss_kib = {p: 1024 for p in [7, *kids]}
    monkeypatch.setattr(rm, "_host_process_table", lambda: (children, rss_kib, False))
    snap = await rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)]).snapshot()
    (entry,) = snap.entries
    assert entry.truncated is True
    assert len(entry.pids) == cap and 7 in entry.pids
    assert entry.rss_mb == pytest.approx(float(cap))


@pytest.mark.asyncio
async def test_darwin_without_ps_falls_back_to_the_root_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    monkeypatch.setattr(rm.os, "getpid", lambda: 999_999)
    monkeypatch.setattr(rm, "_host_process_table", lambda: None)
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: {7: 42.0}.get(pid))
    snap = await rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)]).snapshot()
    (entry,) = snap.entries
    assert entry.rss_mb == pytest.approx(42.0)
    assert entry.pids == frozenset({7})


def _fake_ps_rows(rows: list[bytes], closed: list[bool]):
    """A stand-in for ``_ps_rows``: yields *rows* and records when the consumer
    closed the generator (which is what reaps the real ``ps`` child)."""

    def _gen():
        try:
            for r in rows:
                yield r
        finally:
            closed.append(True)

    return lambda deadline: _gen()


def test_host_table_read_stops_at_the_bound_and_flags_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (bound + 1)th row is never parsed or stored; ``ps`` is stopped there;
    the table says so. A fork storm anywhere on the host therefore cannot grow
    the retained maps past ``MAX_HOST_TABLE_ROWS``."""
    bound = 5
    rows = [f"{100 + i} 1 {i + 1}".encode() for i in range(bound + 3)]
    closed: list[bool] = []
    monkeypatch.setattr(rm, "_ps_rows", _fake_ps_rows(rows, closed))

    table = rm._read_host_table(max_rows=bound)

    assert table is not None
    children, rss_kib, truncated = table
    assert truncated is True
    assert len(rss_kib) == bound
    assert 100 + bound not in rss_kib  # the row past the bound was not retained
    assert children == {1: [100, 101, 102, 103, 104]}
    assert closed == [True]  # the stream was closed, not drained


def test_host_table_read_under_the_bound_is_complete_and_not_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [b"7 1 10", b"70 7 20", b"malformed", b"71 7 x", b"700 70 40"]
    closed: list[bool] = []
    monkeypatch.setattr(rm, "_ps_rows", _fake_ps_rows(rows, closed))

    table = rm._read_host_table(max_rows=10)

    assert table == ({1: [7], 7: [70], 70: [700]}, {7: 10, 70: 20, 700: 40}, False)
    assert closed == [True]


def test_host_table_read_without_ps_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm, "_ps_rows", lambda deadline: None)
    assert rm._read_host_table() is None


def test_host_table_cut_by_the_deadline_is_flagged_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that ends with the deadline sentinel (``ps`` did not finish in
    time) keeps every row it did read but is flagged like a row-bound cut: the
    unread rows may hold anyone's descendants, so totals from it are lower
    bounds, not exact -- the same guarantee the feature map states."""
    rows = [b"7 1 10", b"70 7 20", rm._STREAM_CUT]
    closed: list[bool] = []
    monkeypatch.setattr(rm, "_ps_rows", _fake_ps_rows(rows, closed))

    table = rm._read_host_table(max_rows=100)

    assert table == ({1: [7], 7: [70]}, {7: 10, 70: 20}, True)
    assert closed == [True]


@pytest.mark.skipif(
    sys.platform == "win32" or rm.platform_compat.trusted_system_bin("ps") is None,
    reason="needs a POSIX ps",
)
def test_host_table_read_against_the_real_ps_stream() -> None:
    """Live probe: the real ``ps`` stream parses, lists this very process, and a
    tiny bound cuts it short with the flag set (the child is reaped either way)."""
    full = rm._read_host_table()
    assert full is not None
    children, rss_kib, truncated = full
    assert os.getpid() in rss_kib
    assert truncated is False
    assert children  # at least one parent edge on any live host

    bounded = rm._read_host_table(max_rows=3)
    assert bounded is not None
    assert len(bounded[1]) == 3
    assert bounded[2] is True

    # An already-expired deadline: the real stream yields the cut sentinel
    # first and nothing else, and the child is reaped by the generator's close.
    cut = rm._ps_rows(time.monotonic() - 1.0)
    assert cut is not None
    try:
        assert next(cut) == rm._STREAM_CUT
        with pytest.raises(StopIteration):
            next(cut)
    finally:
        cut.close()


def test_host_table_is_memoized_across_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Many roots in one snapshot share one bounded read (the memo), so the
    cost is one ``ps`` per interval, not one per row."""
    calls: list[int] = []

    def _read(max_rows=rm.MAX_HOST_TABLE_ROWS):
        calls.append(max_rows)
        return ({}, {}, False)

    monkeypatch.setattr(rm, "_read_host_table", _read)
    monkeypatch.setattr(rm, "_host_table_cache", None)
    assert rm._host_process_table() == ({}, {}, False)
    assert rm._host_process_table() == ({}, {}, False)
    assert calls == [rm.MAX_HOST_TABLE_ROWS]


@pytest.mark.asyncio
async def test_darwin_truncated_host_table_flags_every_tree_walked_from_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table cut short at the read may be missing a descendant of any root,
    so each tree's figures are a lower bound and the row is flagged even when
    the tree itself is tiny."""
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    monkeypatch.setattr(rm.os, "getpid", lambda: 999_999)
    children = {7: [70]}
    rss_kib = {7: 1024, 70: 1024}
    monkeypatch.setattr(rm, "_host_process_table", lambda: (children, rss_kib, True))
    snap = await rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)]).snapshot()
    (entry,) = snap.entries
    assert entry.pids == frozenset({7, 70})
    assert entry.truncated is True


@pytest.mark.asyncio
async def test_non_linux_unreadable_root_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    monkeypatch.setattr(rm.os, "getpid", lambda: 999_999)
    # A ps table that does not list the root (gone) -> no entry, no phantom row.
    monkeypatch.setattr(rm, "_host_process_table", lambda: ({}, {}, False))
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: None)
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()
    assert snap.entries == []


@pytest.mark.asyncio
async def test_windows_tree_past_the_cap_is_not_summed_by_the_validated_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lineage-validated Windows sum is sized first with a bounded probe;
    an oversized tree degrades to the root's own RSS, flagged, rather than
    driving an unbounded validated walk or presenting a partial as a total."""
    monkeypatch.setattr(rm.sys, "platform", "win32")
    monkeypatch.setattr(rm.os, "getpid", lambda: 999_999)
    called: list[int] = []
    monkeypatch.setattr(rm, "_windows_tree_exceeds_cap", lambda pid: pid == 7)
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 5.0)

    def tree(pid):
        called.append(pid)
        return 500.0

    monkeypatch.setattr(rm, "_get_rss_tree_mb", tree)
    snap = await rm.ResourceSampler(
        live_runtimes=lambda: [_FakeRuntime(7), _FakeRuntime(8)]
    ).snapshot()
    by_pid = {e.pid: e for e in snap.entries}
    assert by_pid[7].truncated is True and by_pid[7].rss_mb == pytest.approx(5.0)
    assert by_pid[8].truncated is False and by_pid[8].rss_mb == pytest.approx(500.0)
    assert called == [8]  # the validated walk never ran for the oversized tree


def test_windows_size_probe_uses_the_bounded_parent_map_walk(monkeypatch):
    pc = rm.platform_compat
    cap = rm.MAX_TREE_PIDS
    small = {c: 1 for c in range(2, 12)}  # ten children of pid 1
    storm = {c: 1 for c in range(2, 2 + cap + 5)}
    monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: small)
    assert rm._windows_tree_exceeds_cap(1) is False
    monkeypatch.setattr(pc, "_windows_process_parent_map", lambda: storm)
    assert rm._windows_tree_exceeds_cap(1) is True

    def boom():
        raise OSError("snapshot failed")

    monkeypatch.setattr(pc, "_windows_process_parent_map", boom)
    assert rm._windows_tree_exceeds_cap(1) is False


# ── executor offload seam ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_blocking_walk_runs_on_the_subprocess_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /proc walk must be offloaded to subprocess_executor(), not run inline
    on the event loop (Req 8.3). We assert the sampler hands its blocking walk to
    exactly that pool."""
    _fake_linux_proc(monkeypatch, tree={7: [7]}, rss={7: 1.0})

    the_pool = rm.subprocess_executor()
    seen_pools: list[object] = []

    real_run_in_executor = None

    class _SpyLoop:
        def __init__(self, loop: object) -> None:
            self._loop = loop

        def run_in_executor(self, executor, func, *args):  # type: ignore[no-untyped-def]
            seen_pools.append(executor)
            return real_run_in_executor(executor, func, *args)

    import asyncio as _asyncio

    loop = _asyncio.get_running_loop()
    real_run_in_executor = loop.run_in_executor
    monkeypatch.setattr(rm.asyncio, "get_running_loop", lambda: _SpyLoop(loop))

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()

    assert seen_pools == [the_pool]
    assert len(snap.entries) == 1


@pytest.mark.asyncio
async def test_snapshot_with_no_runtimes_is_empty_and_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_linux_proc(monkeypatch, tree={}, rss={})
    monkeypatch.setattr(rm.time, "time", lambda: 1234.0)
    sampler = rm.ResourceSampler(live_runtimes=lambda: [])
    snap = await sampler.snapshot()

    assert snap.entries == []
    assert snap.captured_at == 1234.0
    assert snap.interval_s == rm.DEFAULT_INTERVAL_S


def test_default_live_runtimes_degrades_to_empty_without_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the runtime registry cannot be imported, enumeration yields nothing
    rather than raising — the sampler is usable before that module lands."""
    import builtins

    real_import = builtins.__import__

    def _no_registry(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name == "kiro_crew.acp" and args and "runtime_registry" in (args[2] or ()):
            raise ImportError("registry not available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_registry)
    assert rm._default_live_runtimes() == []


@pytest.mark.asyncio
async def test_exited_root_is_omitted_not_rendered_as_a_phantom_row(monkeypatch):
    """The Linux enumeration always yields at least the root pid (a process missing
    from the map reads as 'root alone'), so a root whose every pid has neither RSS
    nor stat is GONE: it must be omitted (Req 1.6), not emitted as a row of dashes
    that then sits in the cache."""
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm.os, "getpid", lambda: 999_999)
    monkeypatch.setattr(rm, "_iter_descendant_pids", lambda pid, max_pids=None: [pid])
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: None)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: None)

    class _Rt:
        pid = 4242
        _agent = "kirocrew"
        _spawn_monotonic = 1.0

    snap = await rm.ResourceSampler(live_runtimes=lambda: [_Rt()]).snapshot()
    assert snap.entries == []


def test_descendant_walk_is_bounded_at_the_walk_not_after_it():
    """The enumerator's own working set (order, visited, frontier) must be held to
    ``max_pids`` however wide the tree: a runaway agent's fork storm must not
    size the gateway's memory even for the duration of one walk. Asking for
    cap+1 tells the caller from the length alone whether the tree exceeded it."""
    root = 1
    children = {root: list(range(10, 10 + 10_000))}  # 10 000 direct children
    got = _iter_descendant_pids(root, children=children, max_pids=5)
    assert got[0] == root
    assert len(got) == 5
    assert len(set(got)) == 5
    # Unbounded still walks everything; a tree under the bound is unaffected.
    assert len(_iter_descendant_pids(root, children=children)) == 10_001
    assert _iter_descendant_pids(root, children={root: [2, 3]}, max_pids=100) == [root, 3, 2]


@pytest.mark.asyncio
async def test_a_tree_past_max_tree_pids_is_flagged_truncated_and_reads_a_lower_bound(monkeypatch):
    """The sampler walks at most MAX_TREE_PIDS pids per root -- nothing past the
    bound is enumerated, read or retained -- and flags the row so its figures
    are read as a lower bound rather than a total."""
    cap = rm.MAX_TREE_PIDS
    root = 500
    tree = [root] + list(range(100_000, 100_000 + cap + 5))
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm.os, "getpid", lambda: 999_999)
    seen: dict[str, int | None] = {"max_pids": None}

    def fake_walk(pid, max_pids=None):
        seen["max_pids"] = max_pids
        return list(tree)[:max_pids] if pid == root else []

    monkeypatch.setattr(rm, "_iter_descendant_pids", fake_walk)
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 0))

    class _Rt:
        pid = root
        _agent = "kirocrew"
        _spawn_monotonic = 1.0

    snap = await rm.ResourceSampler(live_runtimes=lambda: [_Rt()]).snapshot()
    (entry,) = snap.entries
    assert seen["max_pids"] == cap + 1  # the bound is applied at the walk
    assert entry.truncated is True
    assert len(entry.pids) == cap and root in entry.pids
    assert entry.proc_count == cap
    assert entry.rss_mb == pytest.approx(float(cap))  # only the pids read


@pytest.mark.asyncio
async def test_gateway_row_is_flagged_when_any_runtime_tree_was_truncated(monkeypatch):
    """A runtime pid past that runtime's bound may still sit in the gateway walk
    and be claimed as the gateway's own; the partition is then inexact and the
    gateway row says so, while never counting a pid the gateway did not read."""
    cap = rm.MAX_TREE_PIDS
    gw, rt = 1000, 2000
    rt_tree = [rt] + list(range(50_000, 50_000 + cap + 10))  # past the bound
    gw_tree = [gw] + rt_tree[: cap - 1]  # a gateway walk that stays under it
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm.os, "getpid", lambda: gw)
    trees = {gw: gw_tree, rt: rt_tree}
    monkeypatch.setattr(
        rm, "_iter_descendant_pids", lambda pid, max_pids=None: list(trees.get(pid, []))[:max_pids]
    )
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 1.0)
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 0))

    class _Rt:
        pid = rt
        _agent = "kirocrew"
        _spawn_monotonic = 1.0

    snap = await rm.ResourceSampler(live_runtimes=lambda: [_Rt()]).snapshot()
    by_kind = {e.kind: e for e in snap.entries}
    assert by_kind["worker"].truncated is True
    assert by_kind["gateway"].truncated is True
    assert by_kind["gateway"].proc_count == len(set(gw_tree) - set(rt_tree[:cap]))


@pytest.mark.asyncio
async def test_off_linux_gateway_row_subtracts_attributed_pids_on_darwin(monkeypatch):
    """With per-pid readings from the ps table the gateway-self pass works on
    macOS exactly as on Linux: the gateway row is its tree minus every pid a
    runtime row already claims, never the fleet's total."""
    gw, rt = 777, 42
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    monkeypatch.setattr(rm.os, "getpid", lambda: gw)
    children = {gw: [rt, 778], rt: [420]}
    rss_kib = {gw: 40 * 1024, 778: 5 * 1024, rt: 200 * 1024, 420: 100 * 1024}
    monkeypatch.setattr(rm, "_host_process_table", lambda: (children, rss_kib, False))

    class _Rt:
        pid = rt
        _agent = "claude"
        _spawn_monotonic = 1.0

    snap = await rm.ResourceSampler(live_runtimes=lambda: [_Rt()]).snapshot()
    by_kind = {e.kind: e for e in snap.entries}
    assert by_kind["gateway"].rss_mb == pytest.approx(45.0)
    assert by_kind["gateway"].pids == frozenset({gw, 778})
    assert by_kind["worker"].rss_mb == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_windows_gateway_row_is_its_own_process(monkeypatch):
    gw = 777
    monkeypatch.setattr(rm.sys, "platform", "win32")
    monkeypatch.setattr(rm.os, "getpid", lambda: gw)
    monkeypatch.setattr(rm, "_windows_tree_exceeds_cap", lambda pid: False)
    monkeypatch.setattr(rm, "_get_rss_tree_mb", lambda pid: 5000.0 if pid == gw else 300.0)
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: 40.0 if pid == gw else 999.0)

    class _Rt:
        pid = 42
        _agent = "claude"
        _spawn_monotonic = 1.0

    snap = await rm.ResourceSampler(live_runtimes=lambda: [_Rt()]).snapshot()
    by_kind = {e.kind: e for e in snap.entries}
    assert by_kind["gateway"].rss_mb == pytest.approx(40.0)
    assert by_kind["worker"].rss_mb == pytest.approx(300.0)


def test_own_children_reads_only_as_far_as_the_remaining_capacity(monkeypatch):
    """The kernel's children line for a fork-storm parent can list hundreds of
    thousands of pids; with a ``limit`` the read itself is bounded (a byte
    budget per remaining token), no token split by the cut is misparsed, and
    the caller gets at most ``limit`` pids -- so bounding the walk bounds the
    read too, not just what is kept afterwards."""
    from kiro_crew.acp import runtime as rt

    storm = " ".join(str(100_000 + i) for i in range(50_000)) + " "
    opened: list[int] = []

    class _F:
        def __init__(self, text):
            self._t = text

        def read(self, n=-1):
            opened.append(n)
            return self._t if n is None or n < 0 else self._t[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Entry:
        def __init__(self, name):
            self.name = name

    consumed: list[str] = []

    class _Scan:
        """A lazy ``scandir`` stand-in that records how far it was iterated."""

        def __init__(self, names):
            self._it = iter(names)

        def __iter__(self):
            return self

        def __next__(self):
            name = next(self._it)
            consumed.append(name)
            return _Entry(name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # A thread-heavy parent: many TIDs, the first of which lists the storm.
    monkeypatch.setattr(rt.os, "scandir", lambda path: _Scan([str(t) for t in range(1, 10_001)]))
    monkeypatch.setattr("builtins.open", lambda path, *a, **k: _F(storm))

    kids = rt._own_children(1, limit=7)
    assert len(kids) == 7
    assert kids == [100_000 + i for i in range(7)]  # no misparsed split token
    assert opened == [(7 + 1) * rt._PID_TOKEN_BYTES]  # bounded read, not the line
    # The thread directory is streamed, not listed: the bound was met after the
    # first thread, so the other 9,999 TIDs were never even iterated.
    assert consumed == ["1"]
    assert rt._own_children(1, limit=0) == []
    # Unbounded still reads the whole line.
    consumed.clear()
    opened.clear()
    monkeypatch.setattr(rt.os, "scandir", lambda path: _Scan(["1"]))
    assert len(rt._own_children(1)) == 50_000


def test_walk_passes_its_remaining_capacity_to_each_children_read(monkeypatch):
    from kiro_crew.acp import runtime as rt

    asked: list[int | None] = []

    def fake_children(pid, limit=None):
        asked.append(limit)
        return list(range(pid * 10, pid * 10 + 100))[: limit or 100]

    monkeypatch.setattr(rt, "_own_children", fake_children)
    got = rt._iter_descendant_pids(1, max_pids=6)
    assert len(got) == 6 and got[0] == 1
    assert asked[0] == 5  # root visited (1 of 6) -> five remaining
    assert all(a is not None and a <= 5 for a in asked)
