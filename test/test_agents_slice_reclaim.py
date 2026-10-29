"""The gateway drops clean page cache on the agents slice before it pins at memory.high.

The kernel charges page cache to the cgroup that read the file, so file-heavy agent
work fills the slice's ``memory.high`` with clean cache and the kernel throttles the
whole agent subtree. ``sandbox.reclaim_agents_slice_page_cache`` writes
the slice's ``memory.reclaim`` when clean cache dominates near the ceiling. These tests
run it against a fabricated cgroupfs: ``_USER_MANAGER_CGROUP_BASE`` points at a temp
tree, and a fake kernel stands in for ``memory.reclaim`` where a test needs the write
to have an effect.
"""

from __future__ import annotations

import errno
import json
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.loader as config_loader
import kiro_crew.sandbox as sb
from kiro_crew.notifications import resource_pressure

GIB = 1024**3
MIB = 1024**2

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="the slice resolver is Linux-only")
pytestmark = linux_only

_JOIN_TIMEOUT_SECS = 10.0
_UNSET = object()


class _Clock:
    """A settable ``time.monotonic`` for the slice owner."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


class _Slice:
    """A fabricated ``kirocrew-agents.slice`` directory and its memory files."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "kirocrew.slice" / sb._CGROUP_AGENTS_SLICE
        self.dir.mkdir(parents=True)
        self.high_events = 0
        # Throttle passes child scopes counted against their own limits before
        # the test starts; constant, so they never read as a slice climb.
        self.child_high_events = 7

    def set(
        self,
        *,
        high: int | str,
        current: int,
        file: int,
        anon: int,
        shmem: int = 0,
        file_dirty: int = 0,
        file_writeback: int = 0,
        swap_max: int | str | None = 0,
        reclaim_file: bool = True,
    ) -> None:
        (self.dir / "memory.high").write_text(f"{high}\n", encoding="utf-8")
        (self.dir / "memory.max").write_text("max\n", encoding="utf-8")
        self.write_usage(
            current=current,
            file=file,
            anon=anon,
            shmem=shmem,
            file_dirty=file_dirty,
            file_writeback=file_writeback,
        )
        swap = self.dir / "memory.swap.max"
        if swap_max is None:
            swap.unlink(missing_ok=True)
        else:
            swap.write_text(f"{swap_max}\n", encoding="utf-8")
        reclaim = self.dir / "memory.reclaim"
        if reclaim_file:
            reclaim.touch()
        else:
            reclaim.unlink(missing_ok=True)
        self.write_events()

    def write_usage(self, **figures: int) -> None:
        (self.dir / "memory.current").write_text(f"{figures['current']}\n", encoding="utf-8")
        (self.dir / "memory.stat").write_text(
            f"anon {figures['anon']}\n"
            f"file {figures['file']}\n"
            f"shmem {figures['shmem']}\n"
            f"file_dirty {figures['file_dirty']}\n"
            f"file_writeback {figures['file_writeback']}\n",
            encoding="utf-8",
        )

    def read_events(self, name: str) -> int:
        return sb._read_cgroup_counters(self.dir / name)["high"]

    def read(self, name: str) -> int:
        return int((self.dir / name).read_text(encoding="utf-8").strip())

    def stat(self) -> dict[str, int]:
        return sb._read_cgroup_counters(self.dir / "memory.stat")

    def write_events(self) -> None:
        """``memory.events.local`` counts the slice's own throttle passes;
        ``memory.events`` is hierarchical, so it also carries what child scopes
        counted against their own limits and is always the larger figure."""
        (self.dir / "memory.events.local").write_text(
            f"low 0\nhigh {self.high_events}\nmax 0\noom 0\noom_kill 0\n", encoding="utf-8"
        )
        total = self.high_events + self.child_high_events
        (self.dir / "memory.events").write_text(
            f"low 0\nhigh {total}\nmax 0\noom 0\noom_kill 0\n", encoding="utf-8"
        )

    def kernel_tick(self) -> None:
        """What the kernel does between two samples: count one throttle pass per
        tick while usage sits at or over memory.high."""
        high = sb.read_cgroup_int(self.dir / "memory.high")
        if high is not None and self.read("memory.current") >= high:
            self.high_events += 1000
            self.write_events()


class _FakeKernel:
    """Stands in for ``memory.reclaim``: each write drops that much clean cache."""

    def __init__(self, cg: _Slice, *, fail: int | None = None, shortfall_after: int = -1):
        self.cg = cg
        self.fail = fail
        self.shortfall_after = shortfall_after
        self.writes: list[int] = []

    def __call__(self, path: Path, nbytes: int) -> None:
        assert path == self.cg.dir / "memory.reclaim"
        if self.fail is not None:
            raise OSError(self.fail, "fake kernel refusal")
        self.writes.append(nbytes)
        stat = self.cg.stat()
        reclaimable = sb._slice_reclaimable_file(stat)
        dropped = min(nbytes, reclaimable)
        if 0 <= self.shortfall_after < len(self.writes):
            dropped = min(dropped, nbytes // 4)
        self.cg.write_usage(
            current=self.cg.read("memory.current") - dropped,
            file=stat["file"] - dropped,
            anon=stat["anon"],
            shmem=stat["shmem"],
            file_dirty=stat["file_dirty"],
            file_writeback=stat["file_writeback"],
        )
        if dropped < nbytes:
            raise OSError(errno.EAGAIN, "reclaimed less than asked")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(sb, "time", SimpleNamespace(monotonic=c.monotonic))
    return c


@pytest.fixture
def cg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> _Slice:
    base = tmp_path / "user-manager"
    base.mkdir()
    monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(base))
    monkeypatch.setattr(sb, "_SLICE_RECLAIM_DISARMED", False)
    monkeypatch.setattr(sb, "_SLICE_RECLAIM_LAST_AT", None)
    monkeypatch.setattr(sb, "_SLICE_RECLAIM_SKIP_LOGGED", set())
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 100 kB\nSwapTotal:       0 kB\n", encoding="utf-8")
    monkeypatch.setattr(sb, "_PROC_MEMINFO", str(meminfo))
    _set_config(monkeypatch, None)
    return _Slice(base)


def _set_config(
    monkeypatch: pytest.MonkeyPatch, pct: object, *, local: object = _UNSET
) -> list[int]:
    """Write ``resource_limits.slice_reclaim_trigger_pct`` = *pct* to the pinned
    data home's ``config.json`` (None: unset), and *local* to ``config.local.json``
    when given, so the value reaches the reclaim through the real loader.

    Returns a list that grows by one per config load.
    """
    base = {} if pct is None else {"resource_limits": {"slice_reclaim_trigger_pct": pct}}
    path = config_loader.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base), encoding="utf-8")
    overlay = config_loader.config_local_path()
    if local is _UNSET:
        overlay.unlink(missing_ok=True)
    else:
        overlay.write_text(
            json.dumps({"resource_limits": {"slice_reclaim_trigger_pct": local}}),
            encoding="utf-8",
        )
    config_loader._WARNED_RESOURCE_LIMIT_KEYS.discard("slice_reclaim_trigger_pct")
    loads: list[int] = []
    real_load = config_loader.KiroCrewConfig.load.__func__

    def counting_load(cls):
        loads.append(1)
        return real_load(cls)

    monkeypatch.setattr(config_loader.KiroCrewConfig, "load", classmethod(counting_load))
    return loads


def _run(expect_thread: bool = True) -> threading.Thread | None:
    worker = sb.reclaim_agents_slice_page_cache()
    if expect_thread:
        assert worker is not None
        worker.join(_JOIN_TIMEOUT_SECS)
        assert not worker.is_alive()
    else:
        assert worker is None
    return worker


def _cache_dominated(cg: _Slice, **overrides: object) -> None:
    """The measured shape from the issue, scaled: usage at the ceiling, almost
    all of it file cache."""
    figures: dict = dict(high=100 * GIB, current=100 * GIB, file=90 * GIB, anon=8 * GIB)
    figures.update(overrides)
    cg.set(**figures)


class TestReclaimEndsTheThrottle:
    def test_cache_at_the_ceiling_is_reclaimed_and_the_throttle_stops(
        self, cg: _Slice, clock: _Clock, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        _cache_dominated(cg, shmem=2 * GIB)
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_PROBE_SEEN", None)
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_EDGE_AT", None)

        # Pinned at the ceiling, the kernel's counter climbing: throttled.
        assert sb.agents_slice_throttling() is True
        cg.kernel_tick()
        assert sb.agents_slice_throttling() is True

        with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
            _run()

        # Down to 85% of memory.high: 15 GiB in 1 GiB chunks, never one big write.
        assert sum(kernel.writes) == 15 * GIB
        assert max(kernel.writes) == sb._SLICE_RECLAIM_CHUNK_BYTES
        assert cg.read("memory.current") == 85 * GIB
        assert cg.stat()["anon"] == 8 * GIB
        lines = [r.getMessage() for r in caplog.records if "reclaimed page cache" in r.getMessage()]
        assert len(lines) == 1
        assert f"memory.current {100 * GIB} -> {85 * GIB}" in lines[0]
        assert f"file {90 * GIB} -> {75 * GIB}" in lines[0]
        assert f"anon {8 * GIB} -> {8 * GIB}" in lines[0]

        # Under the line, the kernel stops counting throttle passes, and once the
        # held edge expires the probe reads the slice as unthrottled.
        own_before = cg.read_events("memory.events.local")
        total_before = cg.read_events("memory.events")
        cg.kernel_tick()
        assert cg.read_events("memory.events.local") == own_before
        assert cg.read_events("memory.events") == total_before > own_before
        clock.now += sb._SLICE_THROTTLE_EDGE_HOLD_SECS + 1
        assert sb.agents_slice_throttling() is False

    def test_the_real_writer_puts_the_planned_amount_in_memory_reclaim(self, cg: _Slice) -> None:
        """No fake kernel: the bytes land in the fabricated file, one chunk."""
        cg.set(high=1 * GIB, current=1 * GIB, file=900 * MIB, anon=50 * MIB)
        _run()
        expected = 1 * GIB - int(1 * GIB * sb._SLICE_RECLAIM_TARGET_FRACTION)
        assert (cg.dir / "memory.reclaim").read_text(encoding="ascii") == str(expected)

    def test_reclaim_is_capped_at_the_reclaimable_cache(
        self, cg: _Slice, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dirty, writeback and shmem pages are inside ``file`` but are not clean
        cache, so they never count toward the amount."""
        cg.set(
            high=100 * GIB,
            current=100 * GIB,
            file=60 * GIB,
            anon=40 * GIB,
            shmem=4 * GIB,
            file_dirty=3 * GIB,
            file_writeback=2 * GIB,
        )
        assert sb._slice_reclaimable_file(cg.stat()) == 51 * GIB
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        _run()
        assert sum(kernel.writes) == 15 * GIB

    def test_a_kernel_shortfall_ends_the_episode_without_disarming(
        self, cg: _Slice, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        _cache_dominated(cg)
        kernel = _FakeKernel(cg, shortfall_after=2)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.sandbox"):
            _run()
        assert len(kernel.writes) == 3
        assert sb._SLICE_RECLAIM_DISARMED is False
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_the_episode_stops_at_its_time_budget(
        self, cg: _Slice, clock: _Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _cache_dominated(cg)
        kernel = _FakeKernel(cg)

        def slow_kernel(path: Path, nbytes: int) -> None:
            kernel(path, nbytes)
            clock.now += sb._SLICE_RECLAIM_EPISODE_BUDGET_SECS / 2

        monkeypatch.setattr(sb, "_write_memory_reclaim", slow_kernel)
        _run()
        assert len(kernel.writes) == 2


class TestNoWrite:
    def _assert_no_write(self, cg: _Slice, monkeypatch: pytest.MonkeyPatch) -> _FakeKernel:
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        _run(expect_thread=False)
        assert kernel.writes == []
        return kernel

    def test_anon_dominated_near_the_ceiling(self, cg, monkeypatch) -> None:
        """Real process memory: reclaim cannot free it without swap."""
        cg.set(high=100 * GIB, current=99 * GIB, file=30 * GIB, anon=69 * GIB)
        self._assert_no_write(cg, monkeypatch)

    def test_file_share_just_under_half(self, cg, monkeypatch) -> None:
        cg.set(high=100 * GIB, current=100 * GIB, file=50 * GIB - 1, anon=50 * GIB)
        self._assert_no_write(cg, monkeypatch)

    def test_below_the_trigger(self, cg, monkeypatch) -> None:
        cg.set(high=100 * GIB, current=94 * GIB, file=90 * GIB, anon=4 * GIB)
        self._assert_no_write(cg, monkeypatch)

    def test_memory_high_is_max(self, cg, monkeypatch) -> None:
        cg.set(high="max", current=100 * GIB, file=90 * GIB, anon=8 * GIB)
        self._assert_no_write(cg, monkeypatch)

    def test_no_slice_directory(self, cg, monkeypatch) -> None:
        """cgroup v1, or the slice not materialized: nothing to read or write."""
        cg.set(high=100 * GIB, current=100 * GIB, file=90 * GIB, anon=8 * GIB)
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(cg.dir.parent.parent / "absent"))
        self._assert_no_write(cg, monkeypatch)

    def test_not_linux(self, cg, monkeypatch) -> None:
        _cache_dominated(cg)
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        with monkeypatch.context() as m:
            m.setattr(sys, "platform", "darwin")
            worker = sb.reclaim_agents_slice_page_cache()
        assert worker is None
        assert kernel.writes == []

    def test_a_slice_below_half_its_ceiling_never_reads_config(self, cg, monkeypatch) -> None:
        reads = _set_config(monkeypatch, None)
        cg.set(high=100 * GIB, current=40 * GIB, file=39 * GIB, anon=1 * GIB)
        self._assert_no_write(cg, monkeypatch)
        assert reads == []


class TestPod:
    def test_a_pod_gateway_never_reclaims_the_shared_slice(self, cg, monkeypatch) -> None:
        """A pod runs under the same user as the live gateway, so the slice it
        sees is the live one; only the gateway outside a pod reclaims it."""
        _cache_dominated(cg)
        monkeypatch.setenv("KIROCREW_POD", "1")
        TestNoWrite()._assert_no_write(cg, monkeypatch)
        monkeypatch.delenv("KIROCREW_POD")
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        _run()
        assert sum(kernel.writes) == 15 * GIB


class TestRealCgroupfsFloor:
    def test_a_refused_real_write_is_recorded_for_the_test(self) -> None:
        """The floor refuses the write AND records it, so a reclaim thread that
        swallows the refusal still fails the test that drove it."""
        import conftest_floor_services as floor

        real_path = Path("/sys/fs/cgroup/kc-floor-probe/memory.reclaim")
        with pytest.raises(AssertionError, match="real cgroupfs"):
            sb._write_memory_reclaim(real_path, 1)
        assert floor._REFUSED_REAL_RECLAIM_WRITES == [str(real_path)]
        # Drained here so this test's own teardown check passes.
        floor._REFUSED_REAL_RECLAIM_WRITES.clear()


class TestTinyHost:
    def test_a_small_ceiling_still_reclaims(self, cg, monkeypatch) -> None:
        cg.set(high=32 * MIB, current=32 * MIB, file=30 * MIB, anon=1 * MIB)
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        _run()
        assert kernel.writes == [32 * MIB - int(32 * MIB * sb._SLICE_RECLAIM_TARGET_FRACTION)]

    def test_an_amount_under_the_floor_is_skipped(self, cg, monkeypatch) -> None:
        cg.set(high=16 * MIB, current=16 * MIB, file=15 * MIB, anon=1 * MIB)
        TestNoWrite()._assert_no_write(cg, monkeypatch)


class TestSwapGuard:
    def test_host_swap_and_an_unbounded_slice_swap_skip(self, cg, monkeypatch, caplog) -> None:
        _cache_dominated(cg, swap_max="max")
        Path(sb._PROC_MEMINFO).write_text("SwapTotal:  8388604 kB\n", encoding="utf-8")
        with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
            TestNoWrite()._assert_no_write(cg, monkeypatch)
            TestNoWrite()._assert_no_write(cg, monkeypatch)
        skips = [r for r in caplog.records if "reclaim skipped" in r.getMessage()]
        assert len(skips) == 1

    def test_slice_swap_max_zero_reclaims_on_a_host_with_swap(self, cg, monkeypatch) -> None:
        _cache_dominated(cg, swap_max=0)
        Path(sb._PROC_MEMINFO).write_text("SwapTotal:  8388604 kB\n", encoding="utf-8")
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        _run()
        assert sum(kernel.writes) == 15 * GIB

    def test_no_host_swap_reclaims_without_a_swap_max_file(self, cg, monkeypatch) -> None:
        _cache_dominated(cg, swap_max=None)
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        _run()
        assert sum(kernel.writes) == 15 * GIB

    def test_unreadable_host_swap_counts_as_might_swap(self, cg, monkeypatch) -> None:
        _cache_dominated(cg, swap_max=None)
        monkeypatch.setattr(sb, "_PROC_MEMINFO", str(cg.dir / "no-such-meminfo"))
        TestNoWrite()._assert_no_write(cg, monkeypatch)


class TestDisarm:
    def test_a_kernel_without_memory_reclaim_disarms_once(self, cg, monkeypatch, caplog) -> None:
        """Normal on a pre-5.19 kernel, so it is one INFO line, not a warning."""
        _cache_dominated(cg, reclaim_file=False)
        with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
            assert sb.reclaim_agents_slice_page_cache() is None
            assert sb.reclaim_agents_slice_page_cache() is None
        lines = [r for r in caplog.records if "reclaim is off" in r.getMessage()]
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO
        assert "5.19" in lines[0].getMessage()
        assert sb._SLICE_RECLAIM_DISARMED is True

    @pytest.mark.parametrize("code", [errno.EPERM, errno.EACCES, errno.EINVAL, errno.ENOENT])
    def test_a_refused_write_disarms_once(self, cg, clock, monkeypatch, caplog, code: int) -> None:
        _cache_dominated(cg)
        kernel = _FakeKernel(cg, fail=code)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            _run()
            clock.now += sb._SLICE_RECLAIM_MIN_INTERVAL_SECS + 1
            _run(expect_thread=False)
        warnings = [r for r in caplog.records if "reclaim is off" in r.getMessage()]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert sb._SLICE_RECLAIM_DISARMED is True

    def test_a_failure_in_the_check_never_raises(self, cg, monkeypatch) -> None:
        _cache_dominated(cg)

        def boom(_path: Path) -> int:
            raise RuntimeError("boom")

        monkeypatch.setattr(sb, "_slice_reclaim_cannot_swap", boom)
        assert sb.reclaim_agents_slice_page_cache() is None


class TestConfig:
    def test_zero_turns_it_off(self, cg, monkeypatch) -> None:
        _cache_dominated(cg)
        _set_config(monkeypatch, 0)
        TestNoWrite()._assert_no_write(cg, monkeypatch)

    def test_a_lower_trigger_reclaims_below_itself(self, cg, monkeypatch) -> None:
        """Trigger 80: reclaim at 80% of memory.high, down to 70%."""
        cg.set(high=100 * GIB, current=80 * GIB, file=75 * GIB, anon=5 * GIB)
        _set_config(monkeypatch, 80)
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        _run()
        assert cg.read("memory.current") == 70 * GIB

    @pytest.mark.parametrize("pct", [30, 1, 100, -5, "95", 95.5, True])
    def test_anything_out_of_range_uses_the_default(self, monkeypatch, pct: object) -> None:
        _set_config(monkeypatch, pct)
        assert sb._slice_reclaim_trigger_pct() == sb._SLICE_RECLAIM_DEFAULT_TRIGGER_PCT

    @pytest.mark.parametrize("pct", [50, 75, 99])
    def test_the_accepted_range_is_used_as_written(self, monkeypatch, pct: int) -> None:
        _set_config(monkeypatch, pct)
        assert sb._slice_reclaim_trigger_pct() == pct

    def test_unset_is_the_default(self, monkeypatch) -> None:
        _set_config(monkeypatch, None)
        assert sb._slice_reclaim_trigger_pct() == 95

    def test_the_local_overlay_off_switch_turns_it_off(self, cg, monkeypatch) -> None:
        """``kirocrew config set --local ... 0`` writes only config.local.json; the
        base file leaves the key unset, and the overlay still wins."""
        _cache_dominated(cg)
        _set_config(monkeypatch, None, local=0)
        assert sb._slice_reclaim_trigger_pct() == 0
        TestNoWrite()._assert_no_write(cg, monkeypatch)

    def test_the_local_overlay_trigger_wins_over_the_base(self, monkeypatch) -> None:
        _set_config(monkeypatch, 90, local=70)
        assert sb._slice_reclaim_trigger_pct() == 70


class TestRateLimit:
    def test_one_episode_per_interval(self, cg, clock, monkeypatch) -> None:
        _cache_dominated(cg)
        kernel = _FakeKernel(cg)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)
        _run()
        first = len(kernel.writes)
        # The cache comes straight back.
        _cache_dominated(cg)
        clock.now += sb._SLICE_RECLAIM_MIN_INTERVAL_SECS - 1
        _run(expect_thread=False)
        assert len(kernel.writes) == first
        clock.now += 1
        _run()
        assert len(kernel.writes) == 2 * first


class TestSamplerWiring:
    def test_each_probe_tick_runs_the_reclaim_check(self, monkeypatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(resource_pressure, "check_agents_slice_pressure", lambda: None)
        monkeypatch.setattr(
            resource_pressure, "reclaim_agents_slice_page_cache", lambda: calls.append(1)
        )
        notifier = resource_pressure.ResourcePressureNotifier(
            SimpleNamespace(push=lambda _note: None), probe_fn=lambda: "status"
        )
        assert notifier._run_probe() == "status"
        assert calls == [1]

    def test_a_reclaim_failure_does_not_cost_the_sample(self, monkeypatch) -> None:
        def boom() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(resource_pressure, "check_agents_slice_pressure", lambda: None)
        monkeypatch.setattr(resource_pressure, "reclaim_agents_slice_page_cache", boom)
        notifier = resource_pressure.ResourcePressureNotifier(
            SimpleNamespace(push=lambda _note: None), probe_fn=lambda: "status"
        )
        assert notifier._run_probe() == "status"


class TestDefaultTestFloor:
    def test_a_test_that_does_not_re_arm_the_reclaim_never_reclaims(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-test floor disarms the reclaim, so a sampler test cannot depend on
        whether this host's live slice happens to sit near its memory.high."""
        assert sb._SLICE_RECLAIM_DISARMED is True
        base = tmp_path / "user-manager"
        base.mkdir()
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(base))
        monkeypatch.setattr(sb, "_SLICE_RECLAIM_LAST_AT", None)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal: 100 kB\nSwapTotal:       0 kB\n", encoding="utf-8")
        monkeypatch.setattr(sb, "_PROC_MEMINFO", str(meminfo))
        _set_config(monkeypatch, None)
        slice_ = _Slice(base)
        _cache_dominated(slice_)
        kernel = _FakeKernel(slice_)
        monkeypatch.setattr(sb, "_write_memory_reclaim", kernel)

        _run(expect_thread=False)
        assert kernel.writes == []
