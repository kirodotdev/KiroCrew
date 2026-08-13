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
        monkeypatch.setattr(gw, "_ZOMBIE_PROBE_INTERVAL_SECS", 0.01)
        server = MagicMock()
        server.is_serving.return_value = False
        stop = asyncio.Event()

        await asyncio.wait_for(
            gw._zombie_diagnostic(cast(Any, server), BackendPool(max_backends=1), set(), stop),
            timeout=5,
        )

        records = [json.loads(line) for line in diag.read_text().strip().splitlines()]
        assert records[-1]["tag"] == "zombie_detected"
        assert isinstance(records[-1]["tasks"], list)
        assert isinstance(records[-1]["traceback"], list)
        # Setting stop_event is what lets the watchdog respawn a clean daemon.
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
