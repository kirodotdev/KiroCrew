"""Unit coverage for the ``mcp_gateway.gatewayd`` helpers that the socket-level
integration suites never reach.

The existing gatewayd tests drive the real ``_handle_connection`` loop over a
unix socket, which exercises the register/claim/recaller paths well but leaves
the daemon's supporting machinery untested: the four periodic sweepers, the
abort frame, the SEL audit emitters, the frame codec, the zombie-diagnostic
watchdog, the backend acquire/respawn helpers, and the CLI entry points.

Everything here is driven with in-memory doubles -- no socket is bound, no
subprocess is spawned, and every filesystem write lands under ``tmp_path`` or
the per-test ``KIROCREW_HOME`` that Kiro Crew's conftest pins.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.backend import Backend, BackendGone
from kiro_crew.mcp_gateway.hashing import hash_effective_env
from kiro_crew.mcp_gateway.pool import BackendPool, BackendUnavailable, PoolKey
from kiro_crew.mcp_gateway.rewriter import (
    env_sidecar_dir,
    env_sidecar_name,
    resolve_overlay_dir,
)

pytestmark = pytest.mark.xdist_group("mcp_gateway")

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only fallback shape (Windows uses ctypes)"
)


# --- doubles -----------------------------------------------------------------


class _FakeWriter:
    """``asyncio.StreamWriter`` double recording writes, optionally failing."""

    def __init__(
        self, *, fail: Optional[BaseException] = None, hang: bool = False
    ) -> None:
        self.writes: list[bytes] = []
        self.drains = 0
        self._fail = fail
        self._hang = hang

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def drain(self) -> None:
        self.drains += 1
        if self._hang:
            await asyncio.sleep(3600)
        if self._fail is not None:
            raise self._fail

    def frames(self) -> list[Any]:
        return [json.loads(p.decode("utf-8")) for p in self.writes]


class _FakeReader:
    """``asyncio.StreamReader`` double: hands out queued lines then raises."""

    def __init__(self, *, line: bytes = b"", exc: Optional[BaseException] = None) -> None:
        self._line = line
        self._exc = exc

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        if self._exc is not None:
            raise self._exc
        return self._line


def _pool_key(server: str = "demo-mcp", agent: str = "cov-agent", env_hash: str = "e" * 8) -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name=agent,
        command_args_hash="a" * 8,
        effective_env_hash=env_hash,
        work_dir="/tmp/cov",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="b" * 8,
        approval_mode="reads",
        trust_all_tools=False,
        config_snapshot_hash="c" * 8,
    )


async def _noop_pump() -> None:
    return None


def _fake_backend(key: Optional[PoolKey] = None, pid: int = 4242) -> Backend:
    """A ``Backend`` over mock pipes: alive, with an inert stdout pump."""
    proc = MagicMock()
    proc.returncode = None
    proc.pid = pid
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    backend = Backend(
        pool_key=key or _pool_key(),
        process=proc,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )
    # Never read the mock stdout: the acquire path starts the pump as a task.
    backend.run_stdout_pump = _noop_pump  # type: ignore[method-assign]
    return backend


def _await_kwargs(mock: Any) -> dict[str, Any]:
    """Keyword arguments of a mock's most recent await (fails loudly if none)."""
    assert mock.await_args is not None, "expected the mock to have been awaited"
    return dict(mock.await_args.kwargs)


async def _drain_task(task: Optional[asyncio.Task[Any]]) -> None:
    """Cancel and await a helper-created task so nothing outlives the test."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.fixture(autouse=True)
def _clean_gateway_globals():
    """gatewayd keeps process-global stub/PID registries; never leak between tests."""
    gw._STUB_PROBES.clear()
    gw._CONN_INDEX.clear()
    yield
    gw._STUB_PROBES.clear()
    gw._CONN_INDEX.clear()


# --- CLI socket default ------------------------------------------------------


class TestDefaultCliSocketPath:
    def test_prefers_xdg_runtime_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        got = gw._default_cli_socket_path()
        assert got == tmp_path / gw._DEFAULT_SOCKET_SUBDIR / gw._DEFAULT_SOCKET_NAME

    def test_falls_back_to_data_home_not_tmp(self, monkeypatch, tmp_path):
        """No /tmp tier: a Windows daemon must not create a stray C:\\tmp."""
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        got = gw._default_cli_socket_path()
        assert got == tmp_path / "mcp-gateway" / gw._DEFAULT_SOCKET_NAME


# --- periodic sweepers -------------------------------------------------------


class TestIdleSweeper:
    @pytest.mark.asyncio
    async def test_evicts_until_stop_event(self):
        pool = MagicMock()
        pool.evict_idle = AsyncMock(return_value=2)
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._idle_sweeper(cast(Any, pool), 300, 0.01, stop)
        )
        for _ in range(200):
            if pool.evict_idle.await_count:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert pool.evict_idle.await_count >= 1
        pool.evict_idle.assert_awaited_with(300)

    @pytest.mark.asyncio
    async def test_prefired_stop_event_never_sweeps(self):
        pool = MagicMock()
        pool.evict_idle = AsyncMock(return_value=0)
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(
            gw._idle_sweeper(cast(Any, pool), 300, 0.01, stop), timeout=5
        )
        pool.evict_idle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self):
        pool = MagicMock()
        pool.evict_idle = AsyncMock(return_value=0)
        stop = asyncio.Event()
        task = asyncio.create_task(gw._idle_sweeper(cast(Any, pool), 300, 30.0, stop))
        await asyncio.sleep(0)
        task.cancel()
        # The sweeper absorbs CancelledError rather than propagating it.
        await asyncio.wait_for(task, timeout=5)
        assert task.done() and not task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_event_during_the_wait_exits_without_a_final_sweep(self):
        """Shutdown must not start a fresh sweep it may not have time to finish."""
        pool = MagicMock()
        pool.evict_idle = AsyncMock(return_value=0)
        stop = asyncio.Event()
        task = asyncio.create_task(gw._idle_sweeper(cast(Any, pool), 300, 30.0, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        pool.evict_idle.assert_not_awaited()


class TestHotKeysFlushSweeper:
    @pytest.mark.asyncio
    async def test_flushes_off_the_loop(self):
        hot = MagicMock()
        hot.flush = MagicMock(return_value=True)
        hot.path = "hot-keys.json"
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._hot_keys_flush_sweeper(cast(Any, hot), 0.01, stop)
        )
        for _ in range(200):
            if hot.flush.call_count:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert hot.flush.call_count >= 1

    @pytest.mark.asyncio
    async def test_no_write_is_a_quiet_noop(self):
        hot = MagicMock()
        hot.flush = MagicMock(return_value=False)
        hot.path = "hot-keys.json"
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._hot_keys_flush_sweeper(cast(Any, hot), 0.01, stop)
        )
        for _ in range(200):
            if hot.flush.call_count:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert hot.flush.call_count >= 1

    @pytest.mark.asyncio
    async def test_stop_event_during_the_wait_skips_the_periodic_flush(self):
        """The shutdown path owns the final flush; the sweeper must not race it."""
        hot = MagicMock()
        hot.flush = MagicMock(return_value=True)
        hot.path = "hot-keys.json"
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._hot_keys_flush_sweeper(cast(Any, hot), 30.0, stop)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        hot.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self):
        hot = MagicMock()
        hot.flush = MagicMock(return_value=False)
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._hot_keys_flush_sweeper(cast(Any, hot), 30.0, stop)
        )
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait_for(task, timeout=5)
        assert task.done() and not task.cancelled()


class TestPrewarmTopupSweeper:
    @pytest.mark.asyncio
    async def test_triggers_scheduler_each_interval(self):
        calls: list[int] = []
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._prewarm_topup_sweeper(lambda: calls.append(1), 0.01, stop)
        )
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert calls

    @pytest.mark.asyncio
    async def test_prefired_stop_event_never_schedules(self):
        calls: list[int] = []
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(
            gw._prewarm_topup_sweeper(lambda: calls.append(1), 0.01, stop), timeout=5
        )
        assert calls == []

    @pytest.mark.asyncio
    async def test_stop_event_during_the_wait_skips_the_top_up(self):
        calls: list[int] = []
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._prewarm_topup_sweeper(lambda: calls.append(1), 30.0, stop)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert calls == []

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self):
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._prewarm_topup_sweeper(lambda: None, 30.0, stop)
        )
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait_for(task, timeout=5)
        assert task.done() and not task.cancelled()


class _SweeperPool:
    """Minimal BackendPool surface the heartbeat sweeper touches."""

    def __init__(
        self,
        entries: list[tuple[PoolKey, Any]],
        pids: Optional[list[int]] = None,
        reap: Optional[list[Any]] = None,
    ):
        self._entries = entries
        self._pids = pids or []
        self.deaths: list[str] = []
        self.healthy: list[str] = []
        self.evicted: list[PoolKey] = []
        self.reaped: list[Any] = []
        self._reap_payload: list[Any] = list(reap or [])

    async def snapshot(self) -> list[tuple[PoolKey, Any]]:
        return list(self._entries)

    def note_backend_death(self, digest: str, uptime: float) -> None:
        self.deaths.append(digest)

    def note_backend_healthy(self, digest: str) -> None:
        self.healthy.append(digest)

    async def evict(self, key: PoolKey, expected: Any = None) -> Any:
        self.evicted.append(key)
        return expected

    async def reap_draining(self) -> list[Any]:
        out = self._reap_payload
        self._reap_payload = []
        self.reaped.extend(out)
        return out

    def live_backend_pids(self) -> list[int]:
        return list(self._pids)


async def _run_one_heartbeat_sweep(pool: Any, pidfile: Optional[Path] = None) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(
        gw._heartbeat_sweeper(cast(Any, pool), 0.01, stop, pidfile)
    )
    for _ in range(300):
        if pool.deaths or pool.healthy or pool.evicted or pool.reaped or (
            pidfile is not None and pidfile.exists()
        ):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


class TestHeartbeatSweeper:
    @pytest.mark.asyncio
    async def test_gone_backend_is_evicted_and_charged_to_the_breaker(self):
        key = _pool_key()
        backend = _fake_backend(key)
        backend._heartbeat_once = AsyncMock(return_value="gone")  # type: ignore[method-assign]
        backend.shutdown = AsyncMock()  # type: ignore[method-assign]
        pool = _SweeperPool([(key, backend)])

        await _run_one_heartbeat_sweep(pool)

        # The sweeper may complete more than one pass before the test observes
        # it, so assert on the distinct decisions rather than the call count.
        assert set(pool.deaths) == {key.stable_hash()}
        assert set(pool.evicted) == {key}
        backend.shutdown.assert_awaited()

    @pytest.mark.asyncio
    async def test_wedged_backend_takes_the_same_recycle_path(self):
        key = _pool_key(server="wedged-mcp")
        backend = _fake_backend(key)
        backend._heartbeat_once = AsyncMock(return_value="wedged")  # type: ignore[method-assign]
        backend.shutdown = AsyncMock()  # type: ignore[method-assign]
        pool = _SweeperPool([(key, backend)])

        await _run_one_heartbeat_sweep(pool)

        assert set(pool.deaths) == {key.stable_hash()}
        assert set(pool.evicted) == {key}

    @pytest.mark.asyncio
    async def test_alive_backend_records_a_healthy_signal(self):
        key = _pool_key(server="alive-mcp")
        backend = _fake_backend(key)
        backend._heartbeat_once = AsyncMock(return_value="alive")  # type: ignore[method-assign]
        pool = _SweeperPool([(key, backend)])

        await _run_one_heartbeat_sweep(pool)

        assert set(pool.healthy) == {key.stable_hash()}
        assert pool.evicted == []

    @pytest.mark.asyncio
    async def test_idle_backend_is_left_to_the_idle_sweeper(self):
        key = _pool_key(server="idle-mcp")
        backend = _fake_backend(key)
        backend._heartbeat_once = AsyncMock(return_value="idle")  # type: ignore[method-assign]
        pool = _SweeperPool([(key, backend)], pids=[11, 12])
        pidfile = None
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._heartbeat_sweeper(cast(Any, pool), 0.01, stop, pidfile)
        )
        for _ in range(200):
            if backend._heartbeat_once.await_count:  # type: ignore[attr-defined]
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert pool.healthy == []
        assert pool.deaths == []
        assert pool.evicted == []

    @pytest.mark.asyncio
    async def test_live_backend_pids_are_persisted_out_of_band(self, tmp_path):
        """The supervising manager reads this file to killpg a wedged daemon."""
        pool = _SweeperPool([], pids=[101, 202])
        pidfile = tmp_path / "backends.pid"

        await _run_one_heartbeat_sweep(pool, pidfile)

        assert pidfile.read_text().split() == ["101", "202"]

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self):
        pool = _SweeperPool([])
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._heartbeat_sweeper(cast(Any, pool), 30.0, stop, None)
        )
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait_for(task, timeout=5)
        assert task.done() and not task.cancelled()

    @pytest.mark.asyncio
    async def test_cancellation_flushes_the_hazard_ledger(self, tmp_path):
        """A hazard observed after the last tick must survive a clean shutdown.

        The periodic flush only persists what was seen before the previous
        interval, and shutdown cancels this task — so without a flush on exit an
        observation made in the final interval is lost, and a server that
        misbehaved keeps its recommendation until it misbehaves again. Shutdown is
        the ordinary path, not the exceptional one.
        """
        from kiro_crew.mcp_gateway import hazards

        ledger = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        gw.hazards._sink = ledger  # type: ignore[attr-defined]
        try:
            ledger.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)
            assert not hazards.ledger_path(tmp_path).exists(), "nothing persisted yet"

            pool = _SweeperPool([])
            stop = asyncio.Event()
            task = asyncio.create_task(
                gw._heartbeat_sweeper(cast(Any, pool), 30.0, stop, None)
            )
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.wait_for(task, timeout=5)

            assert hazards.load_ledger(tmp_path).codes_for_name("srv") == (
                hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
            )
        finally:
            gw.hazards._sink = None  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_drained_backend_is_reaped_once_its_refcount_hits_zero(self):
        """Blue-green cutover: draining backends finish in-flight work, then go."""
        drained = _fake_backend(_pool_key(server="drained-mcp"), pid=7070)
        pool = _SweeperPool([], reap=[drained])

        await _run_one_heartbeat_sweep(pool)

        assert pool.reaped == [drained]

    @pytest.mark.asyncio
    async def test_stop_event_during_the_wait_skips_the_sweep(self):
        key = _pool_key(server="quiescing-mcp")
        backend = _fake_backend(key)
        backend._heartbeat_once = AsyncMock(return_value="alive")  # type: ignore[method-assign]
        pool = _SweeperPool([(key, backend)])
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._heartbeat_sweeper(cast(Any, pool), 30.0, stop, None)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        backend._heartbeat_once.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_crashing_transport_probe_does_not_stop_the_backend_sweep(
        self, monkeypatch
    ):
        key = _pool_key(server="probe-crash-mcp")
        backend = _fake_backend(key)
        backend._heartbeat_once = AsyncMock(return_value="alive")  # type: ignore[method-assign]
        pool = _SweeperPool([(key, backend)])
        monkeypatch.setattr(
            gw, "_probe_stub_transports", AsyncMock(side_effect=RuntimeError("probe blew up"))
        )

        await _run_one_heartbeat_sweep(pool)

        assert set(pool.healthy) == {key.stable_hash()}


class TestHasOutstandingWork:
    def test_idle_pool_has_no_undelivered_reply(self):
        assert gw._has_outstanding_work(cast(Any, _AbortPool([]))) is False

    def test_a_backend_still_owing_a_reply_blocks_the_drain(self):
        backend = MagicMock()
        backend.outstanding_work = 1
        assert gw._has_outstanding_work(cast(Any, _AbortPool([backend]))) is True

    def test_a_reply_inside_the_write_critical_section_blocks_the_drain(self):
        """Stage 4: dequeued but not yet flushed, so invisible to both the
        pending map and the inbox depth."""
        with gw._counted_stub_write():
            assert gw._has_outstanding_work(cast(Any, _AbortPool([]))) is True
        assert gw._has_outstanding_work(cast(Any, _AbortPool([]))) is False


class TestDrainAndRewarmOnCredentialChange:
    @pytest.mark.asyncio
    async def test_cutover_evicts_idle_drains_in_use_then_rewarms(self):
        pool = MagicMock()
        pool.evict_idle = AsyncMock(return_value=2)
        pool.drain_all_to_bluegreen = AsyncMock(return_value=3)
        rewarms: list[int] = []

        await gw._drain_and_rewarm_on_credential_change(
            cast(Any, pool), lambda: rewarms.append(1)
        )

        pool.evict_idle.assert_awaited_once_with(0.0, include_pinned=True)
        pool.drain_all_to_bluegreen.assert_awaited_once()
        assert rewarms == [1]

    @pytest.mark.asyncio
    async def test_failed_drain_deliberately_skips_the_rewarm(self):
        """Re-warming after a failed drain would reuse and PIN stale-credential
        backends, making them harder to evict on the next cycle."""
        pool = MagicMock()
        pool.evict_idle = AsyncMock(side_effect=RuntimeError("pool lock wedged"))
        pool.drain_all_to_bluegreen = AsyncMock(return_value=0)
        rewarms: list[int] = []

        await gw._drain_and_rewarm_on_credential_change(
            cast(Any, pool), lambda: rewarms.append(1)
        )

        assert rewarms == []


# --- abort frame -------------------------------------------------------------


class _AbortPool:
    def __init__(self, backends: list[Any]) -> None:
        self._backends_list = backends

    def all_backends(self) -> list[Any]:
        return list(self._backends_list)


class TestApplyAbort:
    @pytest.mark.asyncio
    async def test_missing_pids_is_rejected(self, monkeypatch):
        audits: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            gw, "_audit_abort_applied", lambda *a, **k: audits.append((a, k))
        )
        out = await gw._apply_abort({}, cast(Any, _AbortPool([])))
        assert out == {"type": "abort-rejected", "reason": "missing or invalid pids"}
        assert audits

    @pytest.mark.asyncio
    async def test_non_list_pids_is_rejected(self, monkeypatch):
        monkeypatch.setattr(gw, "_audit_abort_applied", lambda *a, **k: None)
        out = await gw._apply_abort({"pids": 5}, cast(Any, _AbortPool([])))
        assert out["type"] == "abort-rejected"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [[0, 1], ["7"], [True], [None], []])
    async def test_pid_list_with_no_usable_entry_is_rejected(self, monkeypatch, raw):
        """``True`` is an ``int`` subclass and pid<=1 is never a real runtime."""
        monkeypatch.setattr(gw, "_audit_abort_applied", lambda *a, **k: None)
        out = await gw._apply_abort({"pids": raw}, cast(Any, _AbortPool([])))
        assert out == {"type": "abort-rejected", "reason": "no valid pids"}

    @pytest.mark.asyncio
    async def test_cancels_in_flight_work_for_every_indexed_stub(self, monkeypatch):
        monkeypatch.setattr(gw, "_audit_abort_applied", lambda *a, **k: None)
        conn = gw._StubConn("stub-a", [909], "demo", None)
        gw._conn_index_add(conn)
        backend = MagicMock()
        backend.cancel_in_flight_for_stub = AsyncMock(return_value=["1", "2"])

        out = await gw._apply_abort(
            {"pids": [909], "reason": "session hard-stop"},
            cast(Any, _AbortPool([backend])),
        )

        assert out == {"type": "aborted", "cancelled": 2, "stubs": 1}
        backend.cancel_in_flight_for_stub.assert_awaited_once_with("stub-a")

    @pytest.mark.asyncio
    async def test_unknown_pid_cancels_nothing_but_still_succeeds(self, monkeypatch):
        monkeypatch.setattr(gw, "_audit_abort_applied", lambda *a, **k: None)
        backend = MagicMock()
        backend.cancel_in_flight_for_stub = AsyncMock(return_value=[])
        out = await gw._apply_abort({"pids": [777]}, cast(Any, _AbortPool([backend])))
        assert out == {"type": "aborted", "cancelled": 0, "stubs": 0}
        backend.cancel_in_flight_for_stub.assert_not_awaited()


# --- SEL audit emitters ------------------------------------------------------


_AUDIT_CASES = [
    ("_audit_abort_applied", ([1234], "hard-stop", "allowed"), "mcp-gateway.abort-in-flight"),
    ("_audit_pool_fallback", ("caller", "demo-mcp", "pool full"), "mcp-gateway.fallback"),
    ("_audit_pool_rejected", ("caller", "demo-mcp", "unknown target"), "mcp-gateway.ensure_backend"),
    ("_audit_prewarm_spawn", ("demo-mcp",), "mcp-gateway.prewarm-spawn"),
    (
        "_audit_reserved_stub_prefix_denied",
        ("__app_call__deadbeef",),
        "mcp-gateway.reserved-stub-prefix-denied",
    ),
]


class TestAuditEmitters:
    @pytest.mark.parametrize("fn_name,args,operation", _AUDIT_CASES)
    def test_emits_the_documented_operation(self, monkeypatch, fn_name, args, operation):
        sel = MagicMock()
        monkeypatch.setattr(gw, "SecurityEventLog", MagicMock(return_value=sel))
        getattr(gw, fn_name)(*args)
        assert sel.log_api_access.call_args.kwargs["operation"] == operation

    @pytest.mark.parametrize("fn_name,args,operation", _AUDIT_CASES)
    def test_audit_failure_never_breaks_the_caller(self, monkeypatch, fn_name, args, operation):
        monkeypatch.setattr(
            gw, "SecurityEventLog", MagicMock(side_effect=RuntimeError("sel down"))
        )
        getattr(gw, fn_name)(*args)  # must not raise

    def test_denied_abort_reports_the_reason_as_the_error(self, monkeypatch):
        sel = MagicMock()
        monkeypatch.setattr(gw, "SecurityEventLog", MagicMock(return_value=sel))
        gw._audit_abort_applied([], "no valid pids", "denied")
        kwargs = sel.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["error"] == "no valid pids"

    def test_empty_caller_is_normalised_to_unknown(self, monkeypatch):
        sel = MagicMock()
        monkeypatch.setattr(gw, "SecurityEventLog", MagicMock(return_value=sel))
        gw._audit_pool_fallback("", "demo-mcp", "pool full")
        assert sel.log_api_access.call_args.kwargs["caller"] == "unknown"


# --- frame codec -------------------------------------------------------------


class TestReadFirstFrame:
    @pytest.mark.asyncio
    async def test_parses_a_json_object(self):
        reader = _FakeReader(line=b'{"type":"register","stub_uuid":"s1"}\n')
        assert await gw._read_first_frame(cast(Any, reader)) == {
            "type": "register",
            "stub_uuid": "s1",
        }

    @pytest.mark.asyncio
    async def test_clean_eof_returns_none(self):
        reader = _FakeReader(exc=asyncio.IncompleteReadError(b"", None))
        assert await gw._read_first_frame(cast(Any, reader)) is None

    @pytest.mark.asyncio
    async def test_partial_frame_is_logged_and_dropped(self):
        reader = _FakeReader(exc=asyncio.IncompleteReadError(b'{"typ', None))
        assert await gw._read_first_frame(cast(Any, reader)) is None

    @pytest.mark.asyncio
    async def test_idle_peer_times_out(self, monkeypatch):
        monkeypatch.setattr(gw, "_REGISTER_TIMEOUT_SECS", 0.01)

        class _Idle:
            async def readuntil(self, sep: bytes = b"\n") -> bytes:
                await asyncio.sleep(3600)
                return b""

        assert await gw._read_first_frame(cast(Any, _Idle())) is None

    @pytest.mark.asyncio
    async def test_limit_overrun_returns_none(self):
        reader = _FakeReader(exc=asyncio.LimitOverrunError("too long", 1))
        assert await gw._read_first_frame(cast(Any, reader)) is None

    @pytest.mark.asyncio
    async def test_oversize_line_is_refused(self, monkeypatch):
        monkeypatch.setattr(gw, "_MAX_FRAME_BYTES", 8)
        reader = _FakeReader(line=b'{"type":"register"}\n')
        assert await gw._read_first_frame(cast(Any, reader)) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("line", [b"not json\n", b"\xff\xfe\n"])
    async def test_undecodable_or_invalid_json_returns_none(self, line):
        assert await gw._read_first_frame(cast(Any, _FakeReader(line=line))) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("line", [b"[1,2]\n", b'"hello"\n', b"7\n"])
    async def test_non_object_json_is_refused(self, line):
        assert await gw._read_first_frame(cast(Any, _FakeReader(line=line))) is None


class TestWriteJsonLine:
    @pytest.mark.asyncio
    async def test_writes_one_compact_newline_terminated_frame(self):
        writer = _FakeWriter()
        await gw._write_json_line(cast(Any, writer), {"type": "pong", "n": 1})
        assert writer.writes == [b'{"type":"pong","n":1}\n']
        assert writer.drains == 1

    @pytest.mark.asyncio
    async def test_uses_the_per_connection_write_lock_when_present(self):
        writer = _FakeWriter()
        setattr(writer, "_mc_write_lock", asyncio.Lock())
        await gw._write_json_line(cast(Any, writer), {"ok": True})
        assert writer.frames() == [{"ok": True}]

    @pytest.mark.asyncio
    async def test_peer_hangup_mid_reply_is_swallowed(self):
        writer = _FakeWriter(fail=ConnectionResetError("gone"))
        await gw._write_json_line(cast(Any, writer), {"type": "registered"})
        assert writer.writes  # the write happened; only the drain failed

    @pytest.mark.asyncio
    async def test_peer_that_stops_reading_cannot_pin_the_handler(self, monkeypatch):
        monkeypatch.setattr(gw, "_WRITE_REPLY_TIMEOUT_SECS", 0.01)
        writer = _FakeWriter(hang=True)
        await asyncio.wait_for(
            gw._write_json_line(cast(Any, writer), {"type": "registered"}), timeout=5
        )


class TestJsonRpcError:
    def test_mirrors_the_request_id(self):
        out = gw._jsonrpc_error({"id": 42, "method": "tools/call"}, "backend died")
        assert out == {
            "jsonrpc": "2.0",
            "id": 42,
            "error": {"code": -32000, "message": "backend died"},
        }

    def test_notification_without_an_id_yields_a_null_id(self):
        assert gw._jsonrpc_error({"method": "notifications/x"}, "boom")["id"] is None


class TestCallerFromRegister:
    def test_inline_fields_build_a_gateway_caller(self):
        caller = gw._caller_from_register(
            {
                "session_key": "sk-1",
                "session_type": "dashboard",
                "principal_id": "p1",
                "channel_id": "C1",
            }
        )
        assert isinstance(caller, CallerContext)
        assert (caller.session_key, caller.session_type) == ("sk-1", "dashboard")
        assert caller.from_gateway is True

    def test_nested_camel_case_caller_dict_is_accepted(self):
        caller = gw._caller_from_register(
            {"caller": {"sessionKey": "sk-2", "sessionType": "slack", "principalId": "p2"}}
        )
        assert caller is not None
        assert (caller.session_key, caller.session_type) == ("sk-2", "slack")

    def test_missing_session_key_yields_no_caller(self):
        assert gw._caller_from_register({"stub_uuid": "s"}) is None

    def test_session_type_defaults_to_unknown(self):
        caller = gw._caller_from_register({"session_key": "sk-3"})
        assert caller is not None and caller.session_type == "unknown"


# --- stub inbox drain --------------------------------------------------------


class TestDrainInboxToStub:
    @pytest.mark.asyncio
    async def test_forwards_queued_payloads(self):
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await inbox.put(b'{"id":1}\n')
        writer = _FakeWriter()
        task = asyncio.create_task(
            gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-1")
        )
        for _ in range(200):
            if writer.writes:
                break
            await asyncio.sleep(0.01)
        await _drain_task(task)
        assert writer.writes == [b'{"id":1}\n']

    @pytest.mark.asyncio
    async def test_late_reply_after_disconnect_is_dropped_not_raised(self):
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await inbox.put(b'{"id":2}\n')
        writer = _FakeWriter(fail=BrokenPipeError("stub gone"))
        await asyncio.wait_for(
            gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-2"), timeout=5
        )

    @pytest.mark.asyncio
    async def test_stub_that_stops_reading_releases_the_writer_task(self, monkeypatch):
        monkeypatch.setattr(gw, "_WRITE_REPLY_TIMEOUT_SECS", 0.01)
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await inbox.put(b'{"id":3}\n')
        writer = _FakeWriter(hang=True)
        await asyncio.wait_for(
            gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-3"), timeout=5
        )

    @pytest.mark.asyncio
    async def test_write_lock_is_honoured_and_counter_returns_to_zero(self):
        before = gw._active_stub_writes
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await inbox.put(b'{"id":4}\n')
        writer = _FakeWriter()
        setattr(writer, "_mc_write_lock", asyncio.Lock())
        task = asyncio.create_task(
            gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-4")
        )
        for _ in range(200):
            if writer.writes:
                break
            await asyncio.sleep(0.01)
        await _drain_task(task)
        assert gw._active_stub_writes == before

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        writer = _FakeWriter()
        task = asyncio.create_task(
            gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-5")
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --- declared env + target resolution ---------------------------------------


class TestDeclaredNonSecretEnv:
    def _write_sidecar(self, key: PoolKey, payload: dict[str, str]) -> Path:
        overlay = resolve_overlay_dir()
        directory = env_sidecar_dir(overlay)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / env_sidecar_name(key.agent_name, key.server_name)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_absent_sidecar_is_not_an_error(self):
        assert gw._declared_non_secret_env(_pool_key(server="never-written")) == {}

    def test_coherent_sidecar_is_forwarded(self):
        pairs = {"DEMO_REGION": "us-west-2"}
        key = _pool_key(server="coherent-mcp", env_hash=hash_effective_env(pairs))
        self._write_sidecar(key, pairs)
        assert gw._declared_non_secret_env(key) == pairs

    def test_sidecar_edited_after_the_session_started_is_refused(self):
        """The coherence gate: applying the NEW values to a backend keyed by the
        OLD hash would run co-tenants under config they never declared."""
        key = _pool_key(server="drifted-mcp", env_hash="stale" * 4)
        self._write_sidecar(key, {"DEMO_REGION": "eu-west-1"})
        assert gw._declared_non_secret_env(key) == {}

    def test_malformed_sidecar_json_is_ignored(self):
        key = _pool_key(server="badjson-mcp")
        overlay = resolve_overlay_dir()
        directory = env_sidecar_dir(overlay)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / env_sidecar_name(key.agent_name, key.server_name)).write_text(
            "{not json", encoding="utf-8"
        )
        assert gw._declared_non_secret_env(key) == {}

    def test_non_object_sidecar_is_ignored(self):
        key = _pool_key(server="array-mcp")
        self._write_sidecar(cast(Any, key), cast(Any, ["a", "b"]))
        assert gw._declared_non_secret_env(key) == {}

    def test_unreadable_config_falls_back_to_the_default_overlay_dir(self, monkeypatch):
        monkeypatch.setattr(
            gw.KiroCrewConfig, "load", classmethod(lambda cls: (_ for _ in ()).throw(OSError("nope")))
        )
        assert gw._declared_non_secret_env(_pool_key(server="cfgless-mcp")) == {}


class TestDeclaredEnvToForward:
    def test_flag_off_short_circuits_before_any_file_read(self, monkeypatch):
        monkeypatch.setattr(gw, "forward_declared_env_enabled", lambda: False)
        monkeypatch.setattr(
            gw, "_declared_non_secret_env", lambda k: {"SHOULD": "not-be-read"}
        )
        assert gw._declared_env_to_forward(_pool_key()) == {}

    def test_flag_on_delegates_to_the_sidecar_read(self, monkeypatch):
        monkeypatch.setattr(gw, "forward_declared_env_enabled", lambda: True)
        monkeypatch.setattr(gw, "_declared_non_secret_env", lambda k: {"DEMO": "1"})
        assert gw._declared_env_to_forward(_pool_key()) == {"DEMO": "1"}


class TestEnvTargetResolver:
    def test_unmapped_server_returns_none(self, monkeypatch):
        key = _pool_key(server="unmapped-mcp")
        monkeypatch.delenv("KIROCREW_MCP_TARGET_UNMAPPED_MCP", raising=False)
        monkeypatch.delenv("MC_MCP_TARGET_UNMAPPED_MCP", raising=False)
        assert gw.env_target_resolver(key) is None

    def test_whitespace_only_spec_returns_none(self, monkeypatch):
        key = _pool_key(server="blank-mcp")
        monkeypatch.setenv("KIROCREW_MCP_TARGET_BLANK_MCP", "   ")
        assert gw.env_target_resolver(key) is None

    def test_legacy_prefix_is_still_accepted(self, monkeypatch):
        key = _pool_key(server="legacy-mcp")
        monkeypatch.delenv("KIROCREW_MCP_TARGET_LEGACY_MCP", raising=False)
        monkeypatch.setenv("MC_MCP_TARGET_LEGACY_MCP", "legacy-bin --stdio")
        resolved = gw.env_target_resolver(key)
        assert resolved is not None
        command, args, env, work_dir = resolved
        assert (command, args) == ("legacy-bin", ["--stdio"])
        assert isinstance(env, dict)
        assert work_dir == key.work_dir

    def test_python_env_prefixes_are_stripped_from_spawned_env(self, monkeypatch):
        """PYTHONPATH/PYTHONHOME/PYTHONPYCACHEPREFIX must not reach a pooled
        Python-based MCP backend: the first two cause import conflicts, and
        PYTHONPYCACHEPREFIX would make the backend mirror its stdlib into the
        shared bytecode cache (see pycache_gc.py). This scrub reuses
        sandbox._PYTHON_ENV_PREFIXES rather than a hand-listed set of keys, so
        it can't silently drift from the kiro-cli/agent spawn path's scrub.
        """
        key = _pool_key(server="pyenv-mcp")
        monkeypatch.delenv("KIROCREW_MCP_TARGET_PYENV_MCP", raising=False)
        monkeypatch.setenv("MC_MCP_TARGET_PYENV_MCP", "py-backend --stdio")
        monkeypatch.setenv("PYTHONPATH", "/host/site-packages")
        monkeypatch.setenv("PYTHONHOME", "/host/python")
        monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/host/cache/pycache")
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

        resolved = gw.env_target_resolver(key)
        assert resolved is not None
        _command, _args, env, _work_dir = resolved

        for leaked_key in (
            "PYTHONPATH", "PYTHONHOME", "PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE",
        ):
            assert leaked_key not in env


# --- backend acquire / respawn ----------------------------------------------


class TestAcquireBackend:
    @pytest.mark.asyncio
    async def test_unresolvable_server_is_a_clean_rejection(self):
        pool = BackendPool(max_backends=2)
        with pytest.raises(gw._TargetUnknown) as excinfo:
            await gw._acquire_backend(pool, _pool_key(server="ghost-mcp"), lambda k: None)
        assert "ghost-mcp" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_spawn_reports_was_spawned_and_starts_the_stdout_pump(self, monkeypatch):
        pool = BackendPool(max_backends=2)
        key = _pool_key(server="spawn-mcp")
        backend = _fake_backend(key)
        monkeypatch.setattr(gw, "spawn_backend", AsyncMock(return_value=backend))
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})

        got, was_spawned = await gw._acquire_backend(
            pool, key, lambda k: ("demo-bin", ["--stdio"], {"A": "1"}, "/tmp/cov")
        )

        assert got is backend
        assert was_spawned is True
        assert backend._stdout_task is not None
        await _drain_task(backend._stdout_task)
        await pool.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_declared_env_is_merged_over_the_inherited_value(self, monkeypatch):
        pool = BackendPool(max_backends=2)
        key = _pool_key(server="declared-mcp")
        backend = _fake_backend(key)
        spawn = AsyncMock(return_value=backend)
        monkeypatch.setattr(gw, "spawn_backend", spawn)
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {"A": "declared"})

        await gw._acquire_backend(
            pool, key, lambda k: ("demo-bin", [], {"A": "inherited"}, "/tmp/cov")
        )

        assert _await_kwargs(spawn)["env"]["A"] == "declared"
        await _drain_task(backend._stdout_task)
        await pool.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_pool_reuse_reports_was_spawned_false(self, monkeypatch):
        pool = BackendPool(max_backends=2)
        key = _pool_key(server="reuse-mcp")
        backend = _fake_backend(key)
        monkeypatch.setattr(gw, "spawn_backend", AsyncMock(return_value=backend))
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})

        first, spawned_first = await gw._acquire_backend(
            pool, key, lambda k: ("demo-bin", [], {}, "/tmp/cov")
        )
        second, spawned_second = await gw._acquire_backend(
            pool, key, lambda k: ("demo-bin", [], {}, "/tmp/cov")
        )

        assert (spawned_first, spawned_second) == (True, False)
        assert first is second
        await _drain_task(backend._stdout_task)
        await pool.shutdown_all(timeout=0.1)


class TestRespawnBackendForStub:
    @pytest.mark.asyncio
    async def test_no_captured_initialize_gives_up(self):
        pool = BackendPool(max_backends=2)
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(),
            lambda k: None,
            "stub-r1",
            cast(Any, _FakeWriter()),
            None,
            old,
            None,
            None,
        )

        assert out is None
        old.detach_stub.assert_awaited_once_with("stub-r1")

    @pytest.mark.asyncio
    async def test_old_writer_task_is_cancelled_and_inbox_flushed(self):
        """Late replies for the stub's OTHER in-flight ids must still reach it,
        or kiro-cli hangs waiting on those ids forever."""
        pool = BackendPool(max_backends=2)
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        old_inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await old_inbox.put(b'{"id":9,"error":{}}\n')
        writer = _FakeWriter()
        old_task = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(),
            lambda k: None,
            "stub-r2",
            cast(Any, writer),
            None,
            old,
            old_inbox,
            cast(Any, old_task),
        )

        assert out is None
        assert old_task.done()
        assert writer.writes == [b'{"id":9,"error":{}}\n']

    @pytest.mark.asyncio
    async def test_acquire_rejection_gives_up_without_churning_spawns(self, monkeypatch):
        pool = BackendPool(max_backends=2)
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        monkeypatch.setattr(
            gw, "_acquire_backend", AsyncMock(side_effect=BackendUnavailable("breaker open"))
        )

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(),
            lambda k: None,
            "stub-r3",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is None

    @pytest.mark.asyncio
    async def test_prime_failure_gives_up_and_releases_the_reservation(self, monkeypatch):
        key = _pool_key(server="prime-fail-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        fresh = _fake_backend(key, pid=5150)
        fresh.prime_initialize = AsyncMock(  # type: ignore[method-assign]
            side_effect=BackendGone("died during prime")
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r4",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is None
        pool.unreserve.assert_called_once_with(key)

    @pytest.mark.asyncio
    async def test_successful_respawn_rebinds_the_stub_transparently(self, monkeypatch):
        key = _pool_key(server="respawn-ok-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        fresh = _fake_backend(key, pid=6161)
        fresh.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        new_inbox: asyncio.Queue[bytes] = asyncio.Queue()
        fresh.attach_stub = AsyncMock(return_value=new_inbox)  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r5",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        got_backend, got_inbox, got_task = out
        assert got_backend is fresh
        assert got_inbox is new_inbox
        pool.unreserve.assert_called_once_with(key)
        await _drain_task(got_task)

    @pytest.mark.asyncio
    async def test_a_recorded_hazard_makes_the_respawn_come_back_private(
        self, monkeypatch, tmp_path
    ):
        """The retreat's hole if the respawn inherited the shared binding blindly.

        The recycle that follows an unroutable server request comes straight back
        here, so re-pooling would hand the SAME stubs a shared backend for the
        server just observed misbehaving, and no new register happens to
        re-decide it. Also pins that a now-private respawn releases NO
        reservation: only ``pool.get_or_create`` reserves, so releasing on the
        old backend's binding instead would decrement a digest this respawn never
        reserved and drop a concurrent pooled connection's eviction protection.
        """
        from kiro_crew.mcp_gateway import hazards

        key = _pool_key(server="respawn-hazard-mcp")
        ledger = hazards.HazardLedger(tmp_path / hazards.HAZARDS_FILENAME)
        ledger.record(
            "respawn-hazard-mcp",
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
            hazards.launch_identity(
                key.command_args_hash, key.effective_env_hash, key.binary_version
            ),
        )
        monkeypatch.setattr(hazards, "_sink", ledger)

        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        assert not old.exclusive_token, "old backend must be POOLED for this case"
        fresh = _fake_backend(key, pid=6262)
        fresh.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        new_inbox: asyncio.Queue[bytes] = asyncio.Queue()
        fresh.attach_stub = AsyncMock(return_value=new_inbox)  # type: ignore[method-assign]
        acquire = AsyncMock(return_value=(fresh, True))
        monkeypatch.setattr(gw, "_acquire_backend", acquire)

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-hz1",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        assert acquire.await_args.kwargs["exclusive_stub_uuid"] == "stub-hz1"
        pool.unreserve.assert_not_called()
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_replay_skipped_when_owner_rekeyed_mid_respawn(self, monkeypatch):
        """A ``claim`` frame can retarget this connection's identity during
        the respawn's acquire/prime awaits. The captured URIs belong to the
        OLD principal — replaying them would resubscribe the old owner's
        resources onto the rekeyed stub, the exact leak
        ``evict_stub_subscriptions`` exists to prevent. The replay gate must
        recheck the live owner and skip when it changed."""
        key = _pool_key(server="respawn-rekey-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        old.resource_subscription_uris = MagicMock(  # type: ignore[method-assign]
            return_value=["file:///old-owner.txt"]
        )
        fresh = _fake_backend(key, pid=7171)
        fresh.attach_stub = AsyncMock(  # type: ignore[method-assign]
            return_value=asyncio.Queue()
        )
        fresh.replay_resource_subscriptions = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        old_caller = CallerContext(session_key="dashboard:old")
        conn = gw._StubConn("stub-r6", [], "pool", old_caller)
        # The claim lands while prime_initialize is awaited: the connection
        # identity names a NEW principal before the replay gate runs.
        fresh.prime_initialize = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda *_a, **_k: setattr(
                conn, "caller", CallerContext(session_key="dashboard:new")
            )
        )

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r6",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
            caller=old_caller,
            conn=conn,
        )

        assert out is not None  # the respawn itself still succeeds
        fresh.replay_resource_subscriptions.assert_not_awaited()
        _backend, _inbox, task = out
        await _drain_task(task)

    @pytest.mark.asyncio
    async def test_replay_proceeds_when_owner_unchanged(self, monkeypatch):
        """Control for the rekey gate: an unchanged owner still gets its
        subscriptions replayed onto the fresh backend."""
        key = _pool_key(server="respawn-stable-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        old.resource_subscription_uris = MagicMock(  # type: ignore[method-assign]
            return_value=["file:///same-owner.txt"]
        )
        fresh = _fake_backend(key, pid=7272)
        fresh.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        fresh.attach_stub = AsyncMock(  # type: ignore[method-assign]
            return_value=asyncio.Queue()
        )
        fresh.replay_resource_subscriptions = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        same_caller = CallerContext(session_key="dashboard:same")
        conn = gw._StubConn("stub-r7", [], "pool", same_caller)

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r7",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
            caller=same_caller,
            conn=conn,
        )

        assert out is not None
        fresh.replay_resource_subscriptions.assert_awaited_once_with(
            "stub-r7", ["file:///same-owner.txt"], caller=same_caller
        )
        _backend, _inbox, task = out
        await _drain_task(task)

    # --- validating the replacement's tool set (#6294) -----------------------

    @staticmethod
    def _surface_pair(*, served, published, stub="stub-r8"):
        """An old backend that already served *served* TO ``stub``, and a fresh
        one that publishes *published* — both as projected tool surfaces."""
        old = _fake_backend()
        old.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        if served is not None:
            old._served_tool_surfaces[stub] = served
        fresh = _fake_backend(_pool_key(server="respawn-surface-mcp"), pid=8383)
        fresh.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        fresh.attach_stub = AsyncMock(  # type: ignore[method-assign]
            return_value=asyncio.Queue()
        )
        fresh.probe_tool_surface = AsyncMock(return_value=published)  # type: ignore[method-assign]
        return old, fresh

    @pytest.mark.asyncio
    async def test_a_replacement_whose_tool_set_moved_is_not_adopted(
        self, monkeypatch
    ):
        """The gap this closes: priming the captured handshake proves the fresh
        process talks MCP, so without this check a server upgraded in place is
        adopted under a session still holding the DEAD process's schema."""
        key = _pool_key(server="respawn-surface-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old, fresh = self._surface_pair(
            served={"read_file": '{"type":"object"}'},
            published={"readFile": '{"type":"object"}'},
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))
        audits: list[tuple] = []
        monkeypatch.setattr(gw, "_audit_replacement_validated", lambda *a: audits.append(a))

        with pytest.raises(gw._ReplacementRefused) as excinfo:
            await gw._respawn_backend_for_stub(
                pool,
                key,
                lambda k: None,
                "stub-r8",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
                caller=CallerContext(session_key="dashboard:1"),
            )

        # The SESSION is told what changed, not just "backend gone".
        assert "tool set changed" in str(excinfo.value)
        assert "read_file" in str(excinfo.value)

        # Refused BEFORE adoption: the stub is never bound to the replacement.
        fresh.attach_stub.assert_not_awaited()
        # And the give-up still releases the reservation it took, or the digest
        # is skipped by evict_idle forever.
        pool.unreserve.assert_called_once_with(key)
        assert audits and audits[0][0] == "dashboard:1"
        assert audits[0][2] == "denied"
        assert "gone=read_file" in audits[0][3]

    @pytest.mark.asyncio
    async def test_an_unchanged_tool_set_is_adopted(self, monkeypatch):
        """Control: the check must not cost the transparent recovery when the
        replacement publishes what the session was already told."""
        key = _pool_key(server="respawn-surface-mcp")
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r9")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            key,
            lambda k: None,
            "stub-r9",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        fresh.attach_stub.assert_awaited_once_with("stub-r9")
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_a_replacement_that_cannot_be_asked_is_not_adopted(
        self, monkeypatch
    ):
        """A probe that establishes nothing is not agreement. The old backend
        answered a listing projectably, so a replacement that will not is the
        change — adopting on an unanswered probe would be the silent path again."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old, fresh = self._surface_pair(
            served={"read_file": '{"type":"object"}'}, published=None, stub="stub-r10"
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r10",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
            )

        fresh.attach_stub.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_owner_rekeyed_mid_respawn_is_not_adopted(self, monkeypatch):
        """A claim can retarget this connection during the probe. Both sides of
        the comparison belong to the CAPTURED caller, so across a rekey it
        describes a principal that no longer owns the stub — and re-probing would
        race the same way."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        # The tool set AGREES; only the owner moved.
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r14")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))
        audits: list[tuple] = []
        monkeypatch.setattr(gw, "_audit_replacement_validated", lambda *a: audits.append(a))

        captured = CallerContext(session_key="dashboard:old-owner")
        conn = gw._StubConn(
            "stub-r14", [], "pool", CallerContext(session_key="dashboard:new-owner")
        )

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r14",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
                caller=captured,
                conn=conn,
            )

        fresh.attach_stub.assert_not_awaited()
        assert audits and audits[0][2] == "denied"
        assert "retargeted mid-respawn" in audits[0][3]

    @pytest.mark.asyncio
    async def test_a_rekey_landing_after_validation_still_refuses(self, monkeypatch):
        """attach_stub and the subscription replay both await, so a claim can
        land AFTER the check beside the comparison. The re-check before the
        return is the one that closes that window; the earlier one only saves
        the attach."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r18")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        owner = CallerContext(session_key="dashboard:old-owner")
        conn = gw._StubConn("stub-r18", [], "pool", owner)

        async def _attach_then_rekey(stub_uuid):
            # The claim lands during the adoption await, past the early check.
            conn.caller = CallerContext(session_key="dashboard:new-owner")
            return asyncio.Queue()

        fresh.attach_stub = AsyncMock(  # type: ignore[method-assign]
            side_effect=_attach_then_rekey
        )
        fresh.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r18",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
                caller=owner,
                conn=conn,
            )

        # The stub it had just attached is released, or the refcount holds a stub
        # that is about to be told the adoption failed.
        fresh.attach_stub.assert_awaited_once_with("stub-r18")
        fresh.detach_stub.assert_awaited_once_with("stub-r18")

    @pytest.mark.asyncio
    async def test_an_unchanged_owner_still_adopts(self, monkeypatch):
        """Control for the rekey gate: the same owner is not a rekey, and a
        connection that cannot answer the question is not one either."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, fresh = self._surface_pair(served=same, published=dict(same), stub="stub-r15")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        owner = CallerContext(session_key="dashboard:same")

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(server="respawn-surface-mcp"),
            lambda k: None,
            "stub-r15",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
            caller=owner,
            conn=gw._StubConn("stub-r15", [], "pool", owner),
        )

        assert out is not None
        fresh.attach_stub.assert_awaited_once_with("stub-r15")
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_no_listing_ever_served_skips_the_probe_entirely(
        self, monkeypatch
    ):
        """With no claim on record there is nothing a replacement can
        contradict, so the recovery this path already performs must not become a
        failure — and the extra round-trip must not be paid either."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old, fresh = self._surface_pair(served=None, published=None, stub="stub-r11")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(server="respawn-surface-mcp"),
            lambda k: None,
            "stub-r11",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        assert out is not None
        fresh.probe_tool_surface.assert_not_awaited()
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_an_adopted_replacement_is_audited_too(self, monkeypatch):
        """Both outcomes are access decisions about which process may answer a
        live session. Recording only refusals would leave the swap this guard
        exists to make visible as a rotating log line and nothing more — and it
        must be recorded in the no-anchor case especially, which is exactly where
        nothing checked it."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        audits: list[tuple] = []
        monkeypatch.setattr(gw, "_audit_replacement_validated", lambda *a: audits.append(a))
        same = {"read_file": '{"type":"object"}'}

        for stub, served, expected in (
            ("stub-r16", same, "verified: 1 tool(s) unchanged"),
            ("stub-r17", None, "not verified"),
        ):
            old, fresh = self._surface_pair(
                served=served,
                published=dict(same) if served is not None else None,
                stub=stub,
            )
            monkeypatch.setattr(
                gw, "_acquire_backend", AsyncMock(return_value=(fresh, True))
            )

            out = await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                stub,
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
                caller=CallerContext(session_key=f"dashboard:{stub}"),
            )

            assert out is not None
            await _drain_task(out[2])

        assert [a[2] for a in audits] == ["allowed", "allowed"]
        assert "verified: 1 tool(s) unchanged" in audits[0][3]
        assert "not verified" in audits[1][3]

    @pytest.mark.asyncio
    async def test_the_validated_surface_survives_into_the_next_respawn(
        self, monkeypatch
    ):
        """The claim follows the SESSION, not the process. Without carrying it
        the replacement starts anchor-less, so a second respawn of the same stub
        adopts blindly while the client's frozen tool set is still its original
        listing — the guard would cover only the first swap in a session's life."""
        pool = BackendPool(max_backends=3)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        same = {"read_file": '{"type":"object"}'}
        old, first = self._surface_pair(served=same, published=dict(same), stub="stub-r19")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(first, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(server="respawn-surface-mcp"),
            lambda k: None,
            "stub-r19",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )
        assert out is not None
        await _drain_task(out[2])

        # The adopted backend now holds the session's claim...
        assert first.served_tool_surface("stub-r19") == same

        # ...so when IT dies, the second replacement is validated too, and a
        # drifted one is refused rather than adopted.
        second = _fake_backend(_pool_key(server="respawn-surface-mcp"), pid=9494)
        second.prime_initialize = AsyncMock()  # type: ignore[method-assign]
        second.attach_stub = AsyncMock(return_value=asyncio.Queue())  # type: ignore[method-assign]
        second.probe_tool_surface = AsyncMock(return_value={})  # type: ignore[method-assign]
        first.detach_stub = AsyncMock(return_value=0)  # type: ignore[method-assign]
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(second, True)))

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r19",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                first,
                None,
                None,
            )

        second.attach_stub.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_co_pooled_stubs_listing_is_not_this_stubs_anchor(
        self, monkeypatch
    ):
        """One backend serves several sessions. The comparison must be about the
        session being recovered, not whichever tenant listed most recently — a
        sibling's listing must neither supply nor suppress this stub's anchor."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        # Only the SIBLING was ever served a listing.
        old, fresh = self._surface_pair(
            served={"read_file": '{"type":"object"}'},
            published={"totally": '{"type":"object"}'},
            stub="stub-sibling",
        )
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        out = await gw._respawn_backend_for_stub(
            pool,
            _pool_key(server="respawn-surface-mcp"),
            lambda k: None,
            "stub-r12",
            cast(Any, _FakeWriter()),
            {"id": 0, "method": "initialize"},
            old,
            None,
            None,
        )

        # Adopted: stub-r12 holds no declaration, so the replacement's very
        # different tool set contradicts nothing it was told.
        assert out is not None
        fresh.probe_tool_surface.assert_not_awaited()
        await _drain_task(out[2])

    @pytest.mark.asyncio
    async def test_the_anchor_is_read_before_the_detach_that_prunes_it(
        self, monkeypatch
    ):
        """Real ``detach_stub`` drops the stub's anchor. Reading it after the
        detach would report "nothing was ever served" for a session that was
        told plenty, and adopt a drifted replacement."""
        pool = BackendPool(max_backends=2)
        pool.unreserve = MagicMock()  # type: ignore[method-assign]
        old, fresh = self._surface_pair(
            served={"read_file": '{"type":"object"}'},
            published={"readFile": '{"type":"object"}'},
            stub="stub-r13",
        )
        # NOT mocked: the real detach prunes the per-stub anchor.
        del old.detach_stub
        await old.attach_stub("stub-r13")
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(return_value=(fresh, True)))

        with pytest.raises(gw._ReplacementRefused):
            await gw._respawn_backend_for_stub(
                pool,
                _pool_key(server="respawn-surface-mcp"),
                lambda k: None,
                "stub-r13",
                cast(Any, _FakeWriter()),
                {"id": 0, "method": "initialize"},
                old,
                None,
                None,
            )

        fresh.attach_stub.assert_not_awaited()


# --- zombie diagnostic ------------------------------------------------------


class TestZombieDiagnosticPath:
    def test_lives_next_to_the_gatewayd_logs(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gw, "_config_dir", lambda: tmp_path)
        assert gw._zombie_diagnostic_path() == (
            tmp_path / "logs" / "gatewayd_zombie_diagnostic.jsonl"
        )


class TestCountOpenFds:
    def test_returns_a_plausible_count_on_this_platform(self):
        got = gw._count_open_fds()
        assert isinstance(got, int)
        assert got == -1 or got > 0

    @_POSIX_ONLY
    def test_returns_minus_one_when_no_source_is_available(self, monkeypatch):
        monkeypatch.setattr(
            os, "listdir", MagicMock(side_effect=OSError("no such directory"))
        )
        assert gw._count_open_fds() == -1


class TestReadRssKb:
    def test_returns_a_plausible_value_on_this_platform(self):
        got = gw._read_rss_kb()
        assert isinstance(got, int)
        assert got == -1 or got > 0

    def test_delegates_to_the_shared_current_rss_reader(self, monkeypatch):
        # The per-platform duplicate that used to live here read ru_maxrss on
        # macOS -- a peak that never falls. There is now one reader, and this
        # pins the delegation (and the bytes -> KB conversion) so a second
        # implementation cannot quietly reappear.
        monkeypatch.setattr(gw, "_proc_rss_bytes", lambda: 4096)
        assert gw._read_rss_kb() == 4

    def test_returns_minus_one_when_the_reader_cannot_measure(self, monkeypatch):
        monkeypatch.setattr(gw, "_proc_rss_bytes", lambda: 0)
        assert gw._read_rss_kb() == -1


class TestCollectTaskStacks:
    @pytest.mark.asyncio
    async def test_names_every_live_task(self):
        parked = asyncio.create_task(asyncio.Event().wait(), name="cov-parked-task")
        await asyncio.sleep(0)
        try:
            stacks = gw._collect_task_stacks()
        finally:
            await _drain_task(parked)
        names = {entry["name"] for entry in stacks}
        assert "cov-parked-task" in names
        for entry in stacks:
            assert set(entry) == {"name", "done", "cancelled", "stack"}
            assert isinstance(entry["stack"], list)


class TestSnapshotState:
    def _snapshot(self, server: Any) -> dict[str, Any]:
        return gw._snapshot_state(
            server=server, pool=BackendPool(max_backends=1), connections=set(), task_count=3
        )

    def test_reports_serving_true(self):
        server = MagicMock()
        server.is_serving.return_value = True
        snap = self._snapshot(server)
        assert snap["is_serving"] is True
        assert snap["pid"] == os.getpid()
        assert snap["task_count"] == 3
        assert snap["pool_size"] == 0
        assert snap["connections_in_flight"] == 0

    def test_absent_server_reports_unknown_rather_than_healthy(self):
        assert self._snapshot(None)["is_serving"] is None

    def test_probe_failure_reports_unknown_rather_than_healthy(self):
        server = MagicMock()
        server.is_serving.side_effect = RuntimeError("transport gone")
        assert self._snapshot(server)["is_serving"] is None


class TestWriteDiagnostic:
    def test_appends_one_jsonl_line_per_record(self, tmp_path):
        path = tmp_path / "nested" / "diag.jsonl"
        gw._write_diagnostic(path, {"tag": "probe", "n": 1})
        gw._write_diagnostic(path, {"tag": "probe", "n": 2})
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["n"] for line in lines] == [1, 2]

    def test_multiple_records_share_a_single_open(self, monkeypatch, tmp_path):
        # Records passed in one call MUST go through one open-append-close
        # cycle: a second append that lands while the first writer's handle is
        # still closing fails with a sharing violation on Windows, and the
        # never-raises contract would silently drop the record.
        path = tmp_path / "diag.jsonl"
        opens: list[str] = []
        real_open = Path.open

        def counting_open(self, *args, **kwargs):
            if self == path:
                opens.append(str(args))
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", counting_open)
        gw._write_diagnostic(path, {"tag": "probe", "n": 1}, {"tag": "zombie_detected", "n": 2})
        assert len(opens) == 1
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["tag"] for line in lines] == ["probe", "zombie_detected"]


class TestZombieDiagnostic:
    @pytest.mark.asyncio
    async def test_healthy_server_only_writes_probe_baselines(self, monkeypatch, tmp_path):
        diag = tmp_path / "diag.jsonl"
        monkeypatch.setattr(gw, "_zombie_diagnostic_path", lambda: diag)
        monkeypatch.setattr(gw, "_ZOMBIE_PROBE_INTERVAL_SECS", 0.01)
        server = MagicMock()
        server.is_serving.return_value = True
        stop = asyncio.Event()

        task = asyncio.create_task(
            gw._zombie_diagnostic(cast(Any, server), BackendPool(max_backends=1), set(), stop)
        )
        for _ in range(300):
            if diag.exists():
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        tags = {json.loads(line)["tag"] for line in diag.read_text().strip().splitlines()}
        assert tags == {"probe"}

    @pytest.mark.asyncio
    async def test_dead_accept_loop_is_dumped_and_stops_the_daemon(self, monkeypatch, tmp_path):
        diag = tmp_path / "diag.jsonl"
        monkeypatch.setattr(gw, "_zombie_diagnostic_path", lambda: diag)
        writes: list[tuple[Path, tuple[dict[str, Any], ...]]] = []

        def capture_write(path: Path, *records: dict[str, Any]) -> None:
            writes.append((path, records))

        monkeypatch.setattr(gw, "_write_diagnostic", capture_write)
        server = MagicMock()
        server.is_serving.return_value = False
        stop = asyncio.Event()
        monkeypatch.setattr(stop, "wait", AsyncMock(side_effect=asyncio.TimeoutError))

        await gw._zombie_diagnostic(
            cast(Any, server), BackendPool(max_backends=1), set(), stop
        )

        assert len(writes) == 1
        written_path, records = writes[0]
        assert written_path == diag
        assert [record["tag"] for record in records] == ["probe", "zombie_detected"]
        assert records[-1]["tag"] == "zombie_detected"
        assert isinstance(records[-1]["tasks"], list)
        assert isinstance(records[-1]["traceback"], list)
        # Setting stop_event is what lets the watchdog respawn a clean daemon.
        assert stop.is_set()

    @pytest.mark.asyncio
    async def test_zombie_dump_survives_a_windows_sharing_violation(self, monkeypatch, tmp_path):
        # Regression for the Windows write race: the probe baseline and the
        # zombie dump used to be two back-to-back open-append-close cycles,
        # and on Windows the second open can land while the first writer's
        # handle is still closing, failing with a sharing violation
        # (a PermissionError) that the never-raises writer swallows — losing
        # the zombie_detected record. Simulate that deterministically by
        # failing every open of the diagnostic file after the first: with the
        # records batched through a single open, the dump still lands; with
        # the old unserialized double-write, it is dropped and this test reds.
        diag = tmp_path / "diag.jsonl"
        monkeypatch.setattr(gw, "_zombie_diagnostic_path", lambda: diag)
        monkeypatch.setattr(gw, "_ZOMBIE_PROBE_INTERVAL_SECS", 0.01)
        server = MagicMock()
        server.is_serving.return_value = False
        stop = asyncio.Event()

        opened = []
        real_open = Path.open

        def sharing_violation_open(self, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if self == diag and "a" in mode:
                opened.append(mode)
                if len(opened) > 1:
                    raise PermissionError(
                        13, "The process cannot access the file because it is "
                        "being used by another process"
                    )
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", sharing_violation_open)
        await asyncio.wait_for(
            gw._zombie_diagnostic(cast(Any, server), BackendPool(max_backends=1), set(), stop),
            timeout=5,
        )

        records = [json.loads(line) for line in diag.read_text().strip().splitlines()]
        tags = [record["tag"] for record in records]
        assert tags == ["probe", "zombie_detected"]
        assert stop.is_set()

    @pytest.mark.asyncio
    async def test_prefired_stop_event_writes_nothing(self, monkeypatch, tmp_path):
        diag = tmp_path / "diag.jsonl"
        monkeypatch.setattr(gw, "_zombie_diagnostic_path", lambda: diag)
        monkeypatch.setattr(gw, "_ZOMBIE_PROBE_INTERVAL_SECS", 0.01)
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(
            gw._zombie_diagnostic(
                cast(Any, MagicMock()), BackendPool(max_backends=1), set(), stop
            ),
            timeout=5,
        )
        assert not diag.exists()

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gw, "_zombie_diagnostic_path", lambda: tmp_path / "d.jsonl")
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._zombie_diagnostic(
                cast(Any, MagicMock()), BackendPool(max_backends=1), set(), stop
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait_for(task, timeout=5)
        assert task.done() and not task.cancelled()


# --- CLI entry points --------------------------------------------------------


class TestBuildArgparser:
    def test_defaults_match_the_documented_daemon_shape(self):
        args = gw._build_argparser().parse_args([])
        assert args.max_backends == 20
        assert args.idle_timeout_secs == 300
        assert args.prewarm_count == 0
        assert args.credential_watch_paths == []
        assert args.socket == str(gw._default_cli_socket_path())

    def test_credential_watch_path_is_repeatable(self):
        args = gw._build_argparser().parse_args(
            ["--credential-watch-path", "a.json", "--credential-watch-path", "b.json"]
        )
        assert args.credential_watch_paths == ["a.json", "b.json"]

    def test_numeric_flags_are_parsed_as_ints(self):
        args = gw._build_argparser().parse_args(
            ["--max-backends", "3", "--idle-timeout-secs", "9", "--prewarm-count", "2"]
        )
        assert (args.max_backends, args.idle_timeout_secs, args.prewarm_count) == (3, 9, 2)

    def test_log_level_default_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("MC_GATEWAYD_LOG", "DEBUG")
        # The default is captured at parser-construction time, not at import.
        assert gw._build_argparser().parse_args([]).log_level == "DEBUG"


@pytest.fixture
def _quiet_amain(monkeypatch, tmp_path):
    """Neutralise ``_amain``'s process-level side effects (root logging config
    and the blocking sandbox warm) so only its own control flow is exercised."""
    monkeypatch.setattr(gw.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(gw, "warm_backend", lambda: None)
    return ["--socket", str(tmp_path / "cov.sock")]


class TestAmain:
    @pytest.mark.asyncio
    async def test_clean_run_returns_zero_and_forwards_parsed_args(self, _quiet_amain, monkeypatch):
        run = AsyncMock()
        monkeypatch.setattr(gw, "run_gatewayd", run)

        rc = await gw._amain(_quiet_amain + ["--max-backends", "4"])

        assert rc == 0
        assert _await_kwargs(run)["max_backends"] == 4
        assert _await_kwargs(run)["credential_watch_paths"] == []

    @pytest.mark.asyncio
    async def test_credential_watch_paths_become_path_objects(self, _quiet_amain, monkeypatch):
        run = AsyncMock()
        monkeypatch.setattr(gw, "run_gatewayd", run)

        await gw._amain(_quiet_amain + ["--credential-watch-path", "creds.json"])

        assert _await_kwargs(run)["credential_watch_paths"] == [Path("creds.json")]

    @pytest.mark.asyncio
    async def test_unhandled_exception_returns_one_instead_of_crashing(self, _quiet_amain, monkeypatch):
        monkeypatch.setattr(gw, "run_gatewayd", AsyncMock(side_effect=RuntimeError("boom")))
        assert await gw._amain(_quiet_amain) == 1

    @pytest.mark.asyncio
    async def test_sandbox_warm_exhaustion_is_survivable(self, _quiet_amain, monkeypatch):
        """Thread exhaustion must leave the cache cold, not kill the daemon."""

        def _exhausted() -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(gw, "warm_backend", _exhausted)
        monkeypatch.setattr(gw, "run_gatewayd", AsyncMock())
        assert await gw._amain(_quiet_amain) == 0

    @pytest.mark.asyncio
    async def test_loop_exception_handler_logs_both_shapes(self, _quiet_amain, monkeypatch):
        captured: list[Any] = []

        def _fake_set(handler):
            captured.append(handler)

        async def _run(*args, **kwargs):
            return None

        monkeypatch.setattr(gw, "run_gatewayd", _run)

        real_get_running_loop = asyncio.get_running_loop

        def _patched_get_running_loop():
            loop = real_get_running_loop()
            loop.set_exception_handler = _fake_set  # type: ignore[method-assign]
            return loop

        monkeypatch.setattr(gw.asyncio, "get_running_loop", _patched_get_running_loop)
        assert await gw._amain(_quiet_amain) == 0

        assert captured, "gatewayd must install a loop exception handler"
        handler = captured[0]
        loop = MagicMock()
        handler(loop, {"message": "with exc", "exception": RuntimeError("x")})
        handler(loop, {"message": "no exc"})


class TestMain:
    def test_exits_with_the_amain_return_code(self, monkeypatch):
        async def _rc() -> int:
            return 3

        monkeypatch.setattr(gw, "_amain", _rc)
        with pytest.raises(SystemExit) as excinfo:
            gw.main()
        assert excinfo.value.code == 3

    def test_keyboard_interrupt_is_a_clean_exit(self, monkeypatch):
        async def _interrupt() -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(gw, "_amain", _interrupt)
        with pytest.raises(SystemExit) as excinfo:
            gw.main()
        assert excinfo.value.code == 0


# --- metric emitters ---------------------------------------------------------


class TestMetricEmitters:
    def test_backend_acquire_metric_carries_the_warm_attribute(self, monkeypatch):
        rec = MagicMock()
        monkeypatch.setattr(gw, "get_recorder", lambda: rec)
        gw._emit_backend_acquire_metric(12.5, warm=True)
        assert rec.histogram.call_args.args[0] == "kirocrew.mcp.backend.acquire.duration"
        assert rec.histogram.call_args.kwargs["attrs"] == {"warm": True}

    def test_lazy_load_metrics_also_emit_the_acquire_histogram(self, monkeypatch):
        rec = MagicMock()
        monkeypatch.setattr(gw, "get_recorder", lambda: rec)
        gw._emit_lazy_load_metrics(33.0, warm=False)
        names = [call.args[0] for call in rec.histogram.call_args_list]
        assert "kirocrew.mcp.lazy_load.duration" in names
        assert "kirocrew.mcp.backend.acquire.duration" in names
        assert rec.counter.call_args.args[0] == "kirocrew.mcp.lazy_load.count"

    def test_telemetry_failure_never_breaks_the_hot_path(self, monkeypatch):
        monkeypatch.setattr(
            gw, "get_recorder", MagicMock(side_effect=RuntimeError("recorder down"))
        )
        gw._emit_lazy_load_metrics(1.0, warm=False)  # must not raise
        gw._emit_backend_acquire_metric(1.0, warm=True)


# --- register PID extraction / conn index -----------------------------------


class TestRegisterPids:
    @pytest.mark.parametrize(
        "register,expected",
        [
            ({"ancestor_pids": [10, 11, 12]}, [10, 11, 12]),
            ({"parent_pid": 55}, [55]),
            ({}, []),
            ({"ancestor_pids": "nope", "parent_pid": 7}, [7]),
            ({"ancestor_pids": [1, 0, -3, True, "8", None, 9]}, [9]),
            ({"parent_pid": None}, []),
        ],
    )
    def test_only_plausible_pids_survive(self, register, expected):
        assert gw._register_pids(register) == expected


class TestConnIndex:
    def test_add_indexes_every_ancestor_and_discard_removes_the_key(self):
        conn = gw._StubConn("stub-x", [31, 32], "demo", None)
        gw._conn_index_add(conn)
        assert set(gw._CONN_INDEX) == {31, 32}
        gw._conn_index_discard(conn)
        assert gw._CONN_INDEX == {}

    def test_discard_keeps_a_pid_shared_with_another_connection(self):
        first = gw._StubConn("stub-1", [41], "demo", None)
        second = gw._StubConn("stub-2", [41], "demo", None)
        gw._conn_index_add(first)
        gw._conn_index_add(second)
        gw._conn_index_discard(first)
        assert gw._CONN_INDEX[41] == {second}

    def test_discarding_an_unindexed_connection_is_a_noop(self):
        gw._conn_index_discard(gw._StubConn("stub-ghost", [51], "demo", None))
        assert gw._CONN_INDEX == {}
