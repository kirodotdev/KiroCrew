"""The admission layer and the cold-start handshake see the agents slice.

Kiro Crew caps every agent process on a Linux host inside one cgroup,
``kirocrew-agents.slice``, with a ``memory.high`` the kernel throttles at. Two
things must see that ceiling, and these tests pin both:

* the memory probe behind ``resource_status`` / ``admission_check`` takes the
  slice's headroom into account alongside the CONTAINER root
  (``/sys/fs/cgroup/memory.max``, absent on a bare host) and host
  ``MemAvailable`` -- so posture reads ``tight``/``critical`` and spawns are
  refused while the kernel is throttling the agent subtree;
* the ``initialize`` handshake extends its budget while the slice is
  throttling and refuses a still-alive, slow kiro-cli at the deadline with a
  typed overload error instead of killing it and retrying into the same
  throttle.

Each test here fabricates the slice's cgroup files under a temp directory and
points ``sandbox._agents_slice_cgroup_dir`` at it; nothing reads the machine
the suite runs on.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.resource_status as rs
import kiro_crew.sandbox as sb
import kiro_crew.subagent as sa
from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.runtime import (
    _INIT_TIMEOUT_UNDER_THROTTLE,
    _REQUEST_TIMEOUT,
    AcpRequestTimeout,
    AcpRuntime,
    AcpRuntimeError,
    AcpRuntimeOverloaded,
)

GIB = 1024**3

#: Above every supported platform's pid_max, so a cleanup path that signals the
#: mocked process cannot reach a live sibling xdist worker on the runner.
_UNALLOCATABLE_PID = 99_999_999_999

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="the slice resolver is Linux-only")


def _fabricate_slice(
    tmp_path: Path,
    *,
    high: int | str,
    maximum: int | str,
    current: int,
    high_events: int = 0,
) -> Path:
    """Lay the slice out the way systemd's dash-hierarchy does and fill its files."""
    slice_dir = tmp_path / "kirocrew.slice" / sb._CGROUP_AGENTS_SLICE
    slice_dir.mkdir(parents=True)
    (slice_dir / "memory.high").write_text(f"{high}\n", encoding="utf-8")
    (slice_dir / "memory.max").write_text(f"{maximum}\n", encoding="utf-8")
    (slice_dir / "memory.current").write_text(f"{current}\n", encoding="utf-8")
    (slice_dir / "memory.events").write_text(
        f"low 0\nhigh {high_events}\nmax 0\noom 0\noom_kill 0\n", encoding="utf-8"
    )
    return slice_dir


@pytest.fixture
def no_container_cgroup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare host: the container-root probe finds no memory controller."""
    monkeypatch.setattr(sa, "_container_cgroup_available_gb", lambda: -1.0)


@linux_only
class TestAgentsSliceHeadroom:
    def test_both_cgroup_readers_share_one_contract(self, tmp_path):
        """``subagent`` keeps its own reader so its sizing tests can fabricate
        every kernel input by patching that module's ``open``; ``sandbox`` keeps
        its own for the same reason. They must agree on every sentinel so a
        value one probe treats as "no ceiling" the other never treats as 0."""
        (tmp_path / "memory.high").write_text("4096\n", encoding="utf-8")
        (tmp_path / "memory.max").write_text("max\n", encoding="utf-8")
        (tmp_path / "garbage").write_text("not-a-number\n", encoding="utf-8")
        for name, expected in (
            ("memory.high", 4096),
            ("memory.max", None),
            ("garbage", None),
            ("absent", None),
        ):
            assert sb.read_cgroup_int(tmp_path / name) == expected
            assert sa._read_int_file(str(tmp_path / name)) == expected

    def test_lower_of_high_and_max_binds(self, tmp_path, monkeypatch, no_container_cgroup):
        _fabricate_slice(tmp_path, high=24 * GIB, maximum=26 * GIB, current=9 * GIB)
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        assert sa._agents_slice_available_gb() == pytest.approx(15.0)
        # And it is the figure the composite probe reports on a bare host.
        assert sa._cgroup_available_gb() == pytest.approx(15.0)

    def test_usage_above_the_soft_ceiling_reads_as_zero_not_negative(
        self, tmp_path, monkeypatch, no_container_cgroup
    ):
        """Under sustained throttle usage sits AT or over memory.high while the
        kernel reclaims; a negative headroom would fall below every threshold in
        an unpredictable way, zero lands squarely in the critical band."""
        _fabricate_slice(tmp_path, high=20 * GIB, maximum=26 * GIB, current=21 * GIB)
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        assert sa._agents_slice_available_gb() == 0.0

    def test_max_sentinel_on_high_falls_back_to_hard_ceiling(
        self, tmp_path, monkeypatch, no_container_cgroup
    ):
        _fabricate_slice(tmp_path, high="max", maximum=10 * GIB, current=4 * GIB)
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        assert sa._agents_slice_available_gb() == pytest.approx(6.0)

    def test_no_ceiling_at_all_does_not_constrain(self, tmp_path, monkeypatch, no_container_cgroup):
        _fabricate_slice(tmp_path, high="max", maximum="max", current=4 * GIB)
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        assert sa._agents_slice_available_gb() == -1.0
        assert sa._cgroup_available_gb() == -1.0

    def test_absent_slice_does_not_constrain(self, tmp_path, monkeypatch, no_container_cgroup):
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path / "nothing-here"))
        assert sa._agents_slice_available_gb() == -1.0
        assert sa._cgroup_available_gb() == -1.0


class TestCompositeProbePicksTheBindingCeiling:
    def test_slice_tighter_than_container(self, monkeypatch):
        monkeypatch.setattr(sa, "_container_cgroup_available_gb", lambda: 12.0)
        monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: 3.0)
        assert sa._cgroup_available_gb() == 3.0

    def test_container_tighter_than_slice(self, monkeypatch):
        monkeypatch.setattr(sa, "_container_cgroup_available_gb", lambda: 2.0)
        monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: 9.0)
        assert sa._cgroup_available_gb() == 2.0

    def test_only_the_slice_constrains(self, monkeypatch):
        monkeypatch.setattr(sa, "_container_cgroup_available_gb", lambda: -1.0)
        monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: 5.5)
        assert sa._cgroup_available_gb() == 5.5

    def test_neither_constrains(self, monkeypatch):
        monkeypatch.setattr(sa, "_container_cgroup_available_gb", lambda: -1.0)
        monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: -1.0)
        assert sa._cgroup_available_gb() == -1.0


class TestAdmissionSeesTheSlice:
    """The incident, end to end: host memory ample, slice exhausted."""

    @pytest.fixture(autouse=True)
    def _linux_bare_host_with_ample_ram(self, monkeypatch):
        monkeypatch.setattr(sa.platform_compat, "IS_LINUX", True, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_MACOS", False, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_WINDOWS", False, raising=False)
        monkeypatch.setattr(sa, "check_memory_available", lambda min_gb=0.0: (True, 19.0))
        monkeypatch.setattr(sa, "_container_cgroup_available_gb", lambda: -1.0)

    def _cfg(self):
        cfg = MagicMock()
        cfg.agent.resource_pressure_gb = 4.0
        cfg.agent.resource_critical_gb = 2.0
        cfg.agent.admission_gate = True
        return cfg

    def test_exhausted_slice_is_critical_and_refuses(self, monkeypatch):
        monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: 0.0)
        status = rs.probe(self._cfg())
        assert status.available_gb == 0.0
        assert status.posture == rs.POSTURE_CRITICAL
        decision = rs.admission_check(self._cfg())
        assert decision.admitted is False
        assert "critical" in decision.reason

    def test_slice_headroom_is_the_reported_figure(self, monkeypatch):
        monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: 3.0)
        status = rs.probe(self._cfg())
        assert status.available_gb == 3.0
        assert status.posture == rs.POSTURE_TIGHT
        assert rs.admission_check(self._cfg()).admitted is True

    def test_prewarm_population_shrinks_with_the_slice(self, monkeypatch):
        monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: 0.5)
        assert rs.prewarm_allowance(cfg=self._cfg()) == 0

    def test_unconstrained_slice_leaves_the_host_reading_alone(self, monkeypatch):
        monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: -1.0)
        assert rs.probe(self._cfg()).available_gb == 19.0


@linux_only
class TestAgentsSliceThrottling:
    def test_usage_at_or_over_high_is_throttling(self, tmp_path, monkeypatch):
        _fabricate_slice(tmp_path, high=20 * GIB, maximum=26 * GIB, current=20 * GIB)
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_PROBE_SEEN", None)
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_EDGE_AT", None)
        assert sb.agents_slice_throttling() is True

    def test_counter_climb_between_probes_is_throttling(self, tmp_path, monkeypatch):
        """Reclaim can hold usage just UNDER the line mid-episode; the kernel's
        own event counter still advances on every throttle, so two probes that
        see it move agree with the kernel even when the usage compare does not."""
        slice_dir = _fabricate_slice(
            tmp_path, high=20 * GIB, maximum=26 * GIB, current=19 * GIB, high_events=100
        )
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_PROBE_SEEN", None)
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_EDGE_AT", None)
        readings = [1000.0, 1001.0, 1002.0, 1000.0 + 1 + sb._SLICE_THROTTLE_EDGE_HOLD_SECS]

        def _clock() -> float:
            return readings.pop(0) if len(readings) > 1 else readings[0]

        monkeypatch.setattr(sb, "time", SimpleNamespace(monotonic=_clock))
        # First read only baselines.
        assert sb.agents_slice_throttling() is False
        (slice_dir / "memory.events").write_text(
            "low 0\nhigh 4200\nmax 0\noom 0\noom_kill 0\n", encoding="utf-8"
        )
        assert sb.agents_slice_throttling() is True
        # A second probe inside the hold window reads the SAME verdict even
        # though the counter is now stable: the edge is shared, not consumed.
        assert sb.agents_slice_throttling() is True
        # Once the hold expires with the counter still stable and usage under
        # the line, the reading closes.
        assert sb.agents_slice_throttling() is False

    def test_concurrent_cold_starts_share_one_counter_tick(self, tmp_path, monkeypatch):
        """Two handshakes racing one throttle tick: the probe that happens to
        read the advance must not be the only caller told the truth, or the
        other's still-alive timeout would be reaped as a plain failure."""
        slice_dir = _fabricate_slice(
            tmp_path, high=20 * GIB, maximum=26 * GIB, current=19 * GIB, high_events=7
        )
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_PROBE_SEEN", 7)
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_EDGE_AT", None)
        monkeypatch.setattr(sb, "time", SimpleNamespace(monotonic=lambda: 5000.0))
        (slice_dir / "memory.events").write_text(
            "low 0\nhigh 8\nmax 0\noom 0\noom_kill 0\n", encoding="utf-8"
        )
        first = sb.agents_slice_throttling()
        second = sb.agents_slice_throttling()
        assert (first, second) == (True, True)

    def test_no_slice_is_never_throttling(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path / "absent"))
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_PROBE_SEEN", None)
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_EDGE_AT", None)
        assert sb.agents_slice_throttling() is False

    def test_high_at_max_sentinel_uses_counter_only(self, tmp_path, monkeypatch):
        _fabricate_slice(tmp_path, high="max", maximum=26 * GIB, current=25 * GIB, high_events=3)
        monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_PROBE_SEEN", 3)
        monkeypatch.setattr(sb, "_SLICE_THROTTLE_EDGE_AT", None)
        assert sb.agents_slice_throttling() is False


def _uninitialized_runtime() -> tuple[AcpRuntime, MagicMock]:
    rt = AcpRuntime(work_dir="/tmp")
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    return rt, proc


class TestInitializeHandshakeUnderThrottle:
    @pytest.mark.asyncio
    async def test_unthrottled_host_keeps_the_plain_budget(self, monkeypatch):
        rt, _ = _uninitialized_runtime()
        monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: False)
        seen: dict[str, object] = {}

        async def _fake_send(method, params, timeout=None):
            seen["method"] = method
            seen["timeout"] = timeout
            return {"agentCapabilities": {}}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt._initialize_handshake({})
        assert seen["method"] == "initialize"
        assert seen["timeout"] == _REQUEST_TIMEOUT

    @pytest.mark.asyncio
    async def test_throttled_host_gets_the_extended_budget(self, monkeypatch):
        rt, _ = _uninitialized_runtime()
        monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: True)
        seen: dict[str, object] = {}

        async def _fake_send(method, params, timeout=None):
            seen["timeout"] = timeout
            return {"agentCapabilities": {}}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt._initialize_handshake({})
        assert seen["timeout"] == _INIT_TIMEOUT_UNDER_THROTTLE
        assert _INIT_TIMEOUT_UNDER_THROTTLE > _REQUEST_TIMEOUT

    def test_extended_budget_expires_before_the_startup_watchdog_reaps(self):
        """A subagent's ``info._pid`` is recorded only after ``provider.start()``
        returns, so the startup watchdog (``_is_startup_stalled_impl``: turns==0,
        ``_pid is None``, no first stream, ``_exec_started`` older than
        ``_STARTUP_TIMEOUT_SECS``) sees "no runtime" for the whole handshake.
        If the throttled budget were as long as the watchdog window, the
        reaper's ``_force_reap`` would kill the live runtime before
        ``AcpRuntimeOverloaded`` could be raised, and the caller would see a
        killed process instead of the overload verdict. Keep a margin for the
        subprocess spawn that runs between ``_exec_started`` and the handshake."""
        assert _INIT_TIMEOUT_UNDER_THROTTLE <= sa._STARTUP_TIMEOUT_SECS - 30

    @pytest.mark.asyncio
    async def test_alive_and_throttled_at_deadline_is_overload(self, monkeypatch):
        """The incident's signature: process_state=running at the deadline while
        the slice throttles. That is overload, and the error must say so."""
        rt, proc = _uninitialized_runtime()
        proc.returncode = None
        monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: True)

        async def _stall(method, params, timeout=None):
            raise AcpRequestTimeout(f"Request {method} timed out after {timeout:g}s")

        monkeypatch.setattr(rt, "_send_and_await", _stall)
        with pytest.raises(AcpRuntimeOverloaded) as raised:
            await rt._initialize_handshake({})
        assert "throttled" in str(raised.value)
        # Existing handlers keep catching it.
        assert isinstance(raised.value, AcpRequestTimeout)
        assert isinstance(raised.value, AcpRuntimeError)

    def test_overload_is_not_a_retry_verdict(self):
        """The parent timeout says "retry"; overload must not, or every retry
        layer respawns a fresh kiro-cli into the same throttled slice and the
        cold start is paid again with no more memory than the last time."""
        from kiro_crew.llm_helpers import acp_error_is_transient

        assert AcpRequestTimeout.transient is True
        assert AcpRuntimeOverloaded.transient is False
        assert acp_error_is_transient(AcpRequestTimeout("initialize timed out")) is True
        assert acp_error_is_transient(AcpRuntimeOverloaded("throttled")) is False

    @pytest.mark.asyncio
    async def test_throttle_that_begins_mid_handshake_is_still_overload(self, monkeypatch):
        """Without a still-pending request to wait on (the fake raises a bare
        timeout), a late-detected throttle can only be reported as overload."""
        rt, _ = _uninitialized_runtime()
        readings = iter([False, True])
        monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: next(readings))
        seen: dict[str, object] = {}

        async def _stall(method, params, timeout=None):
            seen["timeout"] = timeout
            raise AcpRequestTimeout("Request initialize timed out")

        monkeypatch.setattr(rt, "_send_and_await", _stall)
        with pytest.raises(AcpRuntimeOverloaded):
            await rt._initialize_handshake({})
        # Unthrottled at spawn, so the plain budget applied.
        assert seen["timeout"] == _REQUEST_TIMEOUT

    @pytest.mark.asyncio
    async def test_late_detected_throttle_keeps_the_request_pending(self, monkeypatch):
        """A fresh gateway's first probe only baselines the counter, so the
        spawn-time read misses a throttle already under way and the plain
        budget applies. When the deadline then sees the throttle with the
        process alive, the SAME initialize must be given the rest of the
        extended budget -- its late answer is the handshake, not a leak."""
        rt, _ = _uninitialized_runtime()
        readings = iter([False, True])
        monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: next(readings))
        loop = asyncio.get_running_loop()
        pending: asyncio.Future = loop.create_future()
        rt._pending_requests[7] = pending

        async def _stall(method, params, timeout=None):
            exc = AcpRequestTimeout(f"Request {method} timed out after {timeout:g}s")
            exc.req_id = 7
            exc.adopted_future = pending
            raise exc

        monkeypatch.setattr(rt, "_send_and_await", _stall)
        loop.call_later(0.01, pending.set_result, {"agentCapabilities": {"late": True}})
        result = await rt._initialize_handshake({})
        assert result == {"agentCapabilities": {"late": True}}

    @pytest.mark.asyncio
    async def test_late_detected_throttle_still_overloads_at_the_extended_deadline(
        self, monkeypatch
    ):
        rt, _ = _uninitialized_runtime()
        readings = iter([False, True])
        monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: next(readings))
        monkeypatch.setattr(runtime_mod, "_INIT_TIMEOUT_UNDER_THROTTLE", _REQUEST_TIMEOUT + 0.02)
        loop = asyncio.get_running_loop()
        pending: asyncio.Future = loop.create_future()
        rt._pending_requests[9] = pending

        async def _stall(method, params, timeout=None):
            exc = AcpRequestTimeout(f"Request {method} timed out after {timeout:g}s")
            exc.req_id = 9
            exc.adopted_future = pending
            raise exc

        monkeypatch.setattr(rt, "_send_and_await", _stall)
        with pytest.raises(AcpRuntimeOverloaded) as raised:
            await rt._initialize_handshake({})
        # The overload names the FULL budget the process was given.
        assert f"{_REQUEST_TIMEOUT + 0.02:g}s" in str(raised.value)
        # And the abandoned request is unregistered, so the reader loop has
        # nothing to resolve for it.
        assert 9 not in rt._pending_requests

    @pytest.mark.asyncio
    async def test_send_and_await_keeps_a_timed_out_initialize_registered(self, monkeypatch):
        """The extension above is only possible if the transport hands the
        still-pending request back instead of dropping it on timeout."""
        rt, proc = _uninitialized_runtime()
        proc.stdin.write = MagicMock()
        with pytest.raises(AcpRequestTimeout) as raised:
            await rt._send_and_await("initialize", {}, timeout=0.01)
        req_id = getattr(raised.value, "req_id", None)
        adopted = getattr(raised.value, "adopted_future", None)
        assert req_id is not None and adopted is not None
        assert rt._pending_requests[req_id] is adopted
        assert not adopted.done()

    @pytest.mark.asyncio
    async def test_exited_process_is_a_plain_timeout(self, monkeypatch):
        rt, proc = _uninitialized_runtime()
        proc.returncode = 137
        monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: True)

        async def _stall(method, params, timeout=None):
            raise AcpRequestTimeout("Request initialize timed out")

        monkeypatch.setattr(rt, "_send_and_await", _stall)
        with pytest.raises(AcpRequestTimeout) as raised:
            await rt._initialize_handshake({})
        assert not isinstance(raised.value, AcpRuntimeOverloaded)

    @pytest.mark.asyncio
    async def test_unthrottled_stall_is_a_plain_timeout(self, monkeypatch):
        rt, _ = _uninitialized_runtime()
        monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: False)

        async def _stall(method, params, timeout=None):
            raise AcpRequestTimeout("Request initialize timed out")

        monkeypatch.setattr(rt, "_send_and_await", _stall)
        with pytest.raises(AcpRequestTimeout) as raised:
            await rt._initialize_handshake({})
        assert not isinstance(raised.value, AcpRuntimeOverloaded)

    def test_the_spawn_path_still_kills_on_a_failed_handshake(self):
        """Placement: the helper is what spawn awaits, inside the guard that reaps
        the process -- an overload verdict must never leave a live kiro-cli behind."""
        import inspect

        src = inspect.getsource(AcpRuntime._spawn_admitted)
        assert "_initialize_handshake(client_capabilities)" in src
        before, after = src.split("_initialize_handshake(client_capabilities)", 1)
        assert "require_unchanged_derived_spec" in after
        assert "failed init handshake cleanup" in after


def test_overload_error_is_exported_from_the_runtime_module():
    assert "AcpRuntimeOverloaded" in runtime_mod.__all__
    assert runtime_mod.AcpRuntimeOverloaded is AcpRuntimeOverloaded
