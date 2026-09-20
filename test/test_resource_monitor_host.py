"""Tests for the sampler's host-context, caching, and platform-degradation pass.

The behaviours this task owns, and the ways a naive implementation gets them
wrong:

* the snapshot must carry the host budget — posture, available memory, cpu count,
  host total, and the agents-slice cgroup gauge — so a per-chat figure is readable
  against the real ceiling;
* the cgroup gauge is OMITTED (both fields ``None``) on an unconstrained host, so
  the UI never renders a permanent N/A bar; it appears only when the slice's
  ``memory.max`` is a real number;
* a second ``snapshot()`` call inside the staleness window must return the FIRST
  result verbatim, never re-walk a single ``/proc`` tree, and — critically — the
  sampler must NEVER schedule a timer or background task to refresh the cache;
* a platform with no per-process sampling answers with host context only and a
  machine-readable ``sampling_supported=False``, not an empty-but-"supported"
  table.

The host readers (posture probe, ``/proc/meminfo``, the cgroup files) are faked by
monkeypatching the module seams, so the tests are hermetic and do not depend on the
host's real posture, memory, or cgroup layout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew.acp import resource_monitor as rm

_GW = 999_001


class _FakeRuntime:
    """A live runtime as the sampler reads it — pid plus identity attributes."""

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
    """Install a faked Linux ``/proc`` walk layer (mirrors the 2.1 test helper)."""
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm.os, "getpid", lambda: _GW)
    tree = {**tree, _GW: tree.get(_GW, [])}
    monkeypatch.setattr(
        rm, "_iter_descendant_pids", lambda pid, max_pids=None: list(tree.get(pid, [pid]))
    )
    monkeypatch.setattr(rm, "_get_rss_mb", lambda pid: rss.get(pid))
    monkeypatch.setattr(rm, "read_pid_stat", lambda proc_root, pid: ("R", 0.0, 0))


def _stub_host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    posture: str = "ample",
    available_gb: float = 12.0,
    mem_total_gb: float | None = 32.0,
    cgroup: tuple[float | None, float | None] = (None, None),
    cpu_count: int | None = 8,
) -> None:
    """Pin every host-context source to a known value.

    Patches the module-level readers directly so ``_host_context`` composes from
    deterministic inputs — the tests here assert on the composition and omission
    rules, not on the individual readers' /proc parsing (those have their own
    tests below).
    """
    monkeypatch.setattr(rm, "_probe_host_posture", lambda: (posture, available_gb))
    monkeypatch.setattr(rm, "_read_mem_total_gb", lambda: mem_total_gb)
    monkeypatch.setattr(rm, "_read_agents_cgroup_gb", lambda: cgroup)
    monkeypatch.setattr(rm.os, "cpu_count", lambda: cpu_count)


# ── host context composition ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_carries_host_posture_memory_and_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_linux_proc(monkeypatch, tree={}, rss={})
    _stub_host(
        monkeypatch,
        posture="tight",
        available_gb=3.5,
        mem_total_gb=16.0,
        cpu_count=4,
    )
    sampler = rm.ResourceSampler(live_runtimes=lambda: [])
    snap = await sampler.snapshot()

    assert snap.posture == "tight"
    assert snap.available_gb == pytest.approx(3.5)
    assert snap.host_total_gb == pytest.approx(16.0)
    assert snap.cpu_count == 4
    assert snap.sampling_supported is True


@pytest.mark.asyncio
async def test_host_probes_run_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host readers (posture probe, /proc/meminfo, cgroup files) are filesystem
    reads and must take the executor hop with the tree walk — never run on the
    loop thread (Req 8.3). Checked on the Linux path AND the unsupported-platform
    path, which has no tree walk to hide behind."""
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _posture() -> tuple[str, float]:
        seen.append(threading.get_ident())
        return "ample", 9.0

    _fake_linux_proc(monkeypatch, tree={}, rss={})
    monkeypatch.setattr(rm, "_probe_host_posture", _posture)
    monkeypatch.setattr(rm, "_read_mem_total_gb", lambda: None)
    monkeypatch.setattr(rm, "_read_agents_cgroup_gb", lambda: (None, None))

    snap = await rm.ResourceSampler(live_runtimes=lambda: [], interval_s=0.0).snapshot()
    assert snap.posture == "ample"
    assert seen and all(t != loop_thread for t in seen)

    seen.clear()
    monkeypatch.setattr(rm, "_sampling_supported", lambda: False)
    snap = await rm.ResourceSampler(live_runtimes=lambda: [], interval_s=0.0).snapshot()
    assert snap.sampling_supported is False
    assert snap.posture == "ample"
    assert seen and all(t != loop_thread for t in seen)


@pytest.mark.asyncio
async def test_cgroup_gauge_present_when_slice_constrained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_linux_proc(monkeypatch, tree={}, rss={})
    _stub_host(monkeypatch, cgroup=(2.0, 8.0))
    sampler = rm.ResourceSampler(live_runtimes=lambda: [])
    snap = await sampler.snapshot()

    assert snap.cgroup_used_gb == pytest.approx(2.0)
    assert snap.cgroup_limit_gb == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_cgroup_gauge_omitted_on_unconstrained_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both cgroup fields stay None when the slice is absent/unconstrained —
    never a zero-limit bar the UI would render as a permanent N/A."""
    _fake_linux_proc(monkeypatch, tree={}, rss={})
    _stub_host(monkeypatch, cgroup=(None, None))
    sampler = rm.ResourceSampler(live_runtimes=lambda: [])
    snap = await sampler.snapshot()

    assert snap.cgroup_used_gb is None
    assert snap.cgroup_limit_gb is None


@pytest.mark.asyncio
async def test_host_total_none_when_meminfo_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing MemTotal reads as unknown (None), not a fabricated zero."""
    _fake_linux_proc(monkeypatch, tree={}, rss={})
    _stub_host(monkeypatch, mem_total_gb=None)
    sampler = rm.ResourceSampler(live_runtimes=lambda: [])
    snap = await sampler.snapshot()
    assert snap.host_total_gb is None


# ── caching / staleness window ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_call_inside_window_returns_cache_without_rewalking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call younger than interval_s returns the FIRST snapshot object verbatim
    and does not walk a single tree again."""
    _fake_linux_proc(monkeypatch, tree={7: [7]}, rss={7: 10.0})
    _stub_host(monkeypatch)

    walk_calls: list[int] = []
    real_walk = rm.ResourceSampler._blocking_walk

    def _spy(self, root_pids):  # type: ignore[no-untyped-def]
        walk_calls.append(len(root_pids))
        return real_walk(self, root_pids)

    monkeypatch.setattr(rm.ResourceSampler, "_blocking_walk", _spy)

    clock = {"t": 1000.0}
    monkeypatch.setattr(rm.time, "monotonic", lambda: clock["t"])

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=2.0)
    first = await sampler.snapshot()
    assert walk_calls == [2]  # runtime pid + gateway pid, one pass

    clock["t"] = 1001.0  # still inside the 2.0s window
    second = await sampler.snapshot()

    assert second is first  # cached object returned verbatim
    assert walk_calls == [2]  # no second walk


@pytest.mark.asyncio
async def test_call_after_window_resamples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_linux_proc(monkeypatch, tree={7: [7]}, rss={7: 10.0})
    _stub_host(monkeypatch)

    walk_calls: list[int] = []
    real_walk = rm.ResourceSampler._blocking_walk

    def _spy(self, root_pids):  # type: ignore[no-untyped-def]
        walk_calls.append(len(root_pids))
        return real_walk(self, root_pids)

    monkeypatch.setattr(rm.ResourceSampler, "_blocking_walk", _spy)

    clock = {"t": 1000.0}
    monkeypatch.setattr(rm.time, "monotonic", lambda: clock["t"])

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=2.0)
    first = await sampler.snapshot()
    clock["t"] = 1002.5  # past the 2.0s window
    second = await sampler.snapshot()

    assert second is not first
    assert walk_calls == [2, 2]  # a fresh walk happened


@pytest.mark.asyncio
async def test_cache_window_starts_when_the_sample_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A walk that itself takes most of ``interval_s`` must not hand back a cache
    that is already nearly stale: the window is measured from when the sample
    finished, so the next poll inside ``interval_s`` of completion is served
    from cache rather than re-walking."""
    _fake_linux_proc(monkeypatch, tree={7: [7]}, rss={7: 10.0})
    _stub_host(monkeypatch)

    clock = {"t": 1000.0}
    monkeypatch.setattr(rm.time, "monotonic", lambda: clock["t"])
    walk_calls: list[int] = []
    real_walk = rm.ResourceSampler._blocking_walk

    def _slow_walk(self, root_pids):  # type: ignore[no-untyped-def]
        walk_calls.append(len(root_pids))
        clock["t"] += 1.8  # the walk itself consumed most of the 2.0s window
        return real_walk(self, root_pids)

    monkeypatch.setattr(rm.ResourceSampler, "_blocking_walk", _slow_walk)

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=2.0)
    first = await sampler.snapshot()  # started at 1000.0, completed at 1001.8
    clock["t"] += 1.0  # 1002.8: 2.8s after the START, 1.0s after COMPLETION
    second = await sampler.snapshot()

    assert second is first
    assert walk_calls == [2]  # served from cache, no second walk


@pytest.mark.asyncio
async def test_concurrent_misses_sample_once_and_share_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two pollers that both find the cache stale must not both sample: the
    follower waits for the leader's sample and gets that snapshot verbatim. A
    second walk milliseconds after the first would write a CPU baseline over
    the leader's (an invalid delta) and could land in the cache after — and
    older than — the leader's result."""
    _fake_linux_proc(monkeypatch, tree={7: [7]}, rss={7: 10.0})
    _stub_host(monkeypatch)

    walk_started = asyncio.Event()
    release_walk = asyncio.Event()
    walk_calls: list[int] = []
    real_walk = rm.ResourceSampler._blocking_walk
    loop = asyncio.get_running_loop()

    def _slow_walk(self, root_pids):  # type: ignore[no-untyped-def]
        walk_calls.append(len(root_pids))
        loop.call_soon_threadsafe(walk_started.set)
        # Hold the executor thread until the test lets the leader finish, so the
        # follower's miss provably overlaps the leader's in-flight sample.
        asyncio.run_coroutine_threadsafe(release_walk.wait(), loop).result(timeout=5)
        return real_walk(self, root_pids)

    monkeypatch.setattr(rm.ResourceSampler, "_blocking_walk", _slow_walk)

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)], interval_s=2.0)
    leader = asyncio.create_task(sampler.snapshot())
    await walk_started.wait()
    follower = asyncio.create_task(sampler.snapshot())
    await asyncio.sleep(0)  # the follower reaches the lock and parks
    release_walk.set()

    first, second = await asyncio.gather(leader, follower)
    assert second is first  # the follower served the leader's result
    assert walk_calls == [2]  # exactly ONE walk for two overlapping misses


@pytest.mark.asyncio
async def test_no_background_timer_or_task_is_ever_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sampler must never schedule a timer or background task to refresh the
    cache (Req 8.2): the only refresh path is a stale snapshot() call itself. We
    assert no new asyncio task or timer callback appears across a full sample."""
    _fake_linux_proc(monkeypatch, tree={7: [7]}, rss={7: 10.0})
    _stub_host(monkeypatch)

    loop = asyncio.get_running_loop()

    task_created = False
    timer_created = False

    orig_ensure_future = asyncio.ensure_future

    def _spy_ensure_future(*a, **k):  # type: ignore[no-untyped-def]
        nonlocal task_created
        task_created = True
        return orig_ensure_future(*a, **k)

    orig_call_later = loop.call_later
    orig_call_at = loop.call_at

    def _spy_call_later(*a, **k):  # type: ignore[no-untyped-def]
        nonlocal timer_created
        timer_created = True
        return orig_call_later(*a, **k)

    def _spy_call_at(*a, **k):  # type: ignore[no-untyped-def]
        nonlocal timer_created
        timer_created = True
        return orig_call_at(*a, **k)

    monkeypatch.setattr(asyncio, "ensure_future", _spy_ensure_future)
    monkeypatch.setattr(loop, "call_later", _spy_call_later)
    monkeypatch.setattr(loop, "call_at", _spy_call_at)

    before = len(asyncio.all_tasks(loop))
    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    await sampler.snapshot()
    after = len(asyncio.all_tasks(loop))

    assert task_created is False
    assert timer_created is False
    # No lingering task spawned by the sampler (the current coroutine itself is
    # already counted the same in before/after).
    assert after == before


# ── platform degradation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unsupported_platform_returns_host_context_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform with no per-process sampling answers with the host budget and a
    machine-readable flag, not an empty-but-'supported' table."""
    monkeypatch.setattr(rm, "_sampling_supported", lambda: False)
    _stub_host(monkeypatch, posture="ample", available_gb=9.0, cpu_count=2)

    walk_calls: list[int] = []
    monkeypatch.setattr(
        rm.ResourceSampler,
        "_blocking_walk",
        lambda self, pids: walk_calls.append(1) or {},  # type: ignore[func-returns-value]
    )

    sampler = rm.ResourceSampler(live_runtimes=lambda: [_FakeRuntime(7)])
    snap = await sampler.snapshot()

    assert snap.sampling_supported is False
    assert snap.entries == []
    assert snap.posture == "ample"
    assert snap.available_gb == pytest.approx(9.0)
    assert snap.cpu_count == 2
    # The unsupported path never walks a tree.
    assert walk_calls == []


@pytest.mark.asyncio
async def test_unsupported_snapshot_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rm, "_sampling_supported", lambda: False)
    _stub_host(monkeypatch)
    monkeypatch.setattr(rm.time, "monotonic", lambda: 500.0)

    sampler = rm.ResourceSampler(live_runtimes=lambda: [], interval_s=2.0)
    first = await sampler.snapshot()
    second = await sampler.snapshot()
    assert second is first


# ── host readers in isolation ────────────────────────────────────────────────


def test_read_mem_total_parses_meminfo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemFree:  100 kB\nMemTotal:  16777216 kB\nBuffers: 1 kB\n")
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm, "_MEMINFO_PATH", str(meminfo))
    # 16777216 KiB = 16 GiB.
    assert rm._read_mem_total_gb() == pytest.approx(16.0)


def test_read_mem_total_none_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    assert rm._read_mem_total_gb() is None


def test_read_mem_total_none_when_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rm.sys, "platform", "linux")
    monkeypatch.setattr(rm, "_MEMINFO_PATH", str(tmp_path / "does-not-exist"))
    assert rm._read_mem_total_gb() is None


def test_agents_cgroup_reads_current_and_max(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "memory.max").write_text(str(8 * 1024**3))  # 8 GiB
    (tmp_path / "memory.current").write_text(str(2 * 1024**3))  # 2 GiB

    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", lambda: tmp_path)
    used, limit = rm._read_agents_cgroup_gb()
    assert used == pytest.approx(2.0)
    assert limit == pytest.approx(8.0)


def test_agents_cgroup_omitted_when_slice_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", lambda: None)
    assert rm._read_agents_cgroup_gb() == (None, None)


def test_agents_cgroup_omitted_when_limit_is_max(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``memory.max`` of the literal 'max' means unconstrained — omit the whole
    gauge rather than showing an uncapped ceiling."""
    (tmp_path / "memory.max").write_text("max")
    (tmp_path / "memory.current").write_text(str(2 * 1024**3))

    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", lambda: tmp_path)
    assert rm._read_agents_cgroup_gb() == (None, None)


def test_agents_cgroup_omitted_when_limit_is_unlimited_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "memory.max").write_text(str(rm._CGROUP_UNLIMITED))
    (tmp_path / "memory.current").write_text(str(2 * 1024**3))

    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", lambda: tmp_path)
    assert rm._read_agents_cgroup_gb() == (None, None)


def test_sampling_supported_true_on_known_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    for plat in ("linux", "darwin", "win32"):
        monkeypatch.setattr(rm.sys, "platform", plat)
        assert rm._sampling_supported() is True


def test_sampling_supported_false_on_exotic_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm.sys, "platform", "aix")
    assert rm._sampling_supported() is False
