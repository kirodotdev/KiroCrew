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

This file pins the CLI socket default, the periodic sweepers, the shutdown outstanding-work
check, the credential-change drain and rewarm, the abort frame, the SEL audit emitters, the
frame codec, the stub inbox drain, declared env and target resolution, the zombie-diagnostic
watchdog and its probes, the CLI entry points, the metric emitters, and register-PID
extraction with the connection index. Backend acquire and the respawn cases
(``_acquire_backend``, ``_respawn_backend_for_stub``) live in
``test_mcp_gatewayd_coverage_acquire_respawn.py``, and the shared doubles, ``_await_kwargs``,
``_drain_task`` and the autouse registry reset in ``mcp_gatewayd_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_gatewayd_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _await_kwargs,
    _clean_gateway_globals,
    _drain_task,
    _fake_backend,
    _FakeWriter,
    _pool_key,
)

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.hashing import hash_effective_env
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
from kiro_crew.mcp_gateway.rewriter import (
    env_sidecar_dir,
    env_sidecar_name,
    resolve_overlay_dir,
)
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.xdist_group("mcp_gateway")

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only fallback shape (Windows uses ctypes)"
)


class _FakeReader:
    """``asyncio.StreamReader`` double: hands out queued lines then raises."""

    def __init__(self, *, line: bytes = b"", exc: Optional[BaseException] = None) -> None:
        self._line = line
        self._exc = exc

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        if self._exc is not None:
            raise self._exc
        return self._line


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
        task = asyncio.create_task(gw._idle_sweeper(cast(Any, pool), 300, 0.01, stop))
        await async_wait_until(lambda: bool(pool.evict_idle.await_count))
        stop.set()
        await await_until(task)
        assert pool.evict_idle.await_count >= 1
        pool.evict_idle.assert_awaited_with(300)

    @pytest.mark.asyncio
    async def test_prefired_stop_event_never_sweeps(self):
        pool = MagicMock()
        pool.evict_idle = AsyncMock(return_value=0)
        stop = asyncio.Event()
        stop.set()
        await await_until(asyncio.create_task(gw._idle_sweeper(cast(Any, pool), 300, 0.01, stop)))
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
        await await_until(task)
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
        await await_until(task)
        pool.evict_idle.assert_not_awaited()


class TestHotKeysFlushSweeper:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "wrote", [True, False], ids=["flushes-off-the-loop", "no-write-is-a-quiet-noop"]
    )
    async def test_flush_logs_only_a_real_write(self, caplog, wrote):
        hot = MagicMock()
        hot.flush = MagicMock(return_value=wrote)
        hot.path = "hot-keys.json"
        stop = asyncio.Event()
        with caplog.at_level("DEBUG"):
            task = asyncio.create_task(gw._hot_keys_flush_sweeper(cast(Any, hot), 0.01, stop))
            await async_wait_until(lambda: bool(hot.flush.call_count))
            stop.set()
            await await_until(task)
        assert hot.flush.call_count >= 1
        assert ("hot-keys: flushed to hot-keys.json" in caplog.text) is wrote

    @pytest.mark.asyncio
    async def test_stop_event_during_the_wait_skips_the_periodic_flush(self):
        """The shutdown path owns the final flush; the sweeper must not race it."""
        hot = MagicMock()
        hot.flush = MagicMock(return_value=True)
        hot.path = "hot-keys.json"
        stop = asyncio.Event()
        task = asyncio.create_task(gw._hot_keys_flush_sweeper(cast(Any, hot), 30.0, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await await_until(task)
        hot.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self):
        hot = MagicMock()
        hot.flush = MagicMock(return_value=False)
        stop = asyncio.Event()
        task = asyncio.create_task(gw._hot_keys_flush_sweeper(cast(Any, hot), 30.0, stop))
        await asyncio.sleep(0)
        task.cancel()
        await await_until(task)
        assert task.done() and not task.cancelled()


class TestPrewarmTopupSweeper:
    @pytest.mark.asyncio
    async def test_triggers_scheduler_each_interval(self):
        calls: list[int] = []
        stop = asyncio.Event()
        task = asyncio.create_task(gw._prewarm_topup_sweeper(lambda: calls.append(1), 0.01, stop))
        await async_wait_until(lambda: bool(calls))
        stop.set()
        await await_until(task)
        assert calls

    @pytest.mark.asyncio
    async def test_prefired_stop_event_never_schedules(self):
        calls: list[int] = []
        stop = asyncio.Event()
        stop.set()
        await await_until(
            asyncio.create_task(gw._prewarm_topup_sweeper(lambda: calls.append(1), 0.01, stop))
        )
        assert calls == []

    @pytest.mark.asyncio
    async def test_stop_event_during_the_wait_skips_the_top_up(self):
        calls: list[int] = []
        stop = asyncio.Event()
        task = asyncio.create_task(gw._prewarm_topup_sweeper(lambda: calls.append(1), 30.0, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await await_until(task)
        assert calls == []

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self):
        stop = asyncio.Event()
        task = asyncio.create_task(gw._prewarm_topup_sweeper(lambda: None, 30.0, stop))
        await asyncio.sleep(0)
        task.cancel()
        await await_until(task)
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

    def live_backend_identities(self) -> list[tuple[int, Optional[str]]]:
        return [(pid, f"start-{pid}") for pid in self._pids]


async def _run_one_heartbeat_sweep(pool: Any, pidfile: Optional[Path] = None) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(gw._heartbeat_sweeper(cast(Any, pool), 0.01, stop, pidfile))
    await async_wait_until(
        lambda: bool(
            pool.deaths
            or pool.healthy
            or pool.evicted
            or pool.reaped
            or (pidfile is not None and pidfile.exists())
        )
    )
    stop.set()
    await await_until(task)


class TestHeartbeatSweeper:
    @pytest.mark.asyncio
    async def test_gone_backend_is_evicted_and_charged_to_the_breaker(self, caplog):
        key = _pool_key()
        backend = _fake_backend(key)
        backend._heartbeat_once = AsyncMock(return_value="gone")  # type: ignore[method-assign]
        backend.shutdown = AsyncMock()  # type: ignore[method-assign]
        pool = _SweeperPool([(key, backend)])

        with caplog.at_level("WARNING"):
            await _run_one_heartbeat_sweep(pool)

        recycled = [
            r.getMessage() for r in caplog.records if "heartbeat recycled" in r.getMessage()
        ]
        assert recycled and all(
            # The fake process has not exited, so this "gone" is a broken pipe.
            m.endswith(backend.lifecycle_fields("backend_gone", "transport_closed"))
            for m in recycled
        )

        # The sweeper may complete more than one pass before the test observes
        # it, so assert on the distinct decisions rather than the call count.
        assert set(pool.deaths) == {key.stable_hash()}
        assert set(pool.evicted) == {key}
        backend.shutdown.assert_awaited()

    @pytest.mark.asyncio
    async def test_wedged_backend_takes_the_same_recycle_path(self, caplog):
        key = _pool_key(server="wedged-mcp")
        backend = _fake_backend(key)
        backend._heartbeat_once = AsyncMock(return_value="wedged")  # type: ignore[method-assign]
        backend.shutdown = AsyncMock()  # type: ignore[method-assign]
        pool = _SweeperPool([(key, backend)])

        with caplog.at_level("WARNING"):
            await _run_one_heartbeat_sweep(pool)

        recycled = [
            r.getMessage() for r in caplog.records if "heartbeat recycled" in r.getMessage()
        ]
        assert recycled and all(
            m.endswith(backend.lifecycle_fields("backend_gone", "deadline")) for m in recycled
        )

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
        task = asyncio.create_task(gw._heartbeat_sweeper(cast(Any, pool), 0.01, stop, pidfile))
        await async_wait_until(lambda: bool(backend._heartbeat_once.await_count))  # type: ignore[attr-defined]
        stop.set()
        await await_until(task)

        assert pool.healthy == []
        assert pool.deaths == []
        assert pool.evicted == []

    @pytest.mark.asyncio
    async def test_live_backend_identities_are_persisted_out_of_band(self, tmp_path):
        """The supervising manager and the next daemon read this file to reap a
        dead generation, so each pid carries the start id its backend captured
        at spawn -- never one re-read now, which could name a recycled pid."""
        pool = _SweeperPool([], pids=[101, 202])
        pidfile = tmp_path / "backends.pid"

        with patch(
            "kiro_crew.platform_compat.get_process_start_id",
            side_effect=AssertionError("the heartbeat must not re-read identities"),
        ):
            await _run_one_heartbeat_sweep(pool, pidfile)

        assert pidfile.read_text().splitlines() == ["101 start-101", "202 start-202"]

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self):
        pool = _SweeperPool([])
        stop = asyncio.Event()
        task = asyncio.create_task(gw._heartbeat_sweeper(cast(Any, pool), 30.0, stop, None))
        await asyncio.sleep(0)
        task.cancel()
        await await_until(task)
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
            task = asyncio.create_task(gw._heartbeat_sweeper(cast(Any, pool), 30.0, stop, None))
            await asyncio.sleep(0)
            task.cancel()
            await await_until(task)

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
        task = asyncio.create_task(gw._heartbeat_sweeper(cast(Any, pool), 30.0, stop, None))
        await asyncio.sleep(0.05)
        stop.set()
        await await_until(task)
        backend._heartbeat_once.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_crashing_transport_probe_does_not_stop_the_backend_sweep(self, monkeypatch):
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

        await gw._drain_and_rewarm_on_credential_change(cast(Any, pool), lambda: rewarms.append(1))

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

        await gw._drain_and_rewarm_on_credential_change(cast(Any, pool), lambda: rewarms.append(1))

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
        monkeypatch.setattr(gw, "_audit_abort_applied", lambda *a, **k: audits.append((a, k)))
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
    (
        "_audit_pool_rejected",
        ("caller", "demo-mcp", "unknown target"),
        "mcp-gateway.ensure_backend",
    ),
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
    def test_audit_failure_never_breaks_the_caller(
        self, monkeypatch, caplog, fn_name, args, operation
    ):
        sel_factory = MagicMock(side_effect=RuntimeError("sel down"))
        monkeypatch.setattr(gw, "SecurityEventLog", sel_factory)
        with caplog.at_level("DEBUG"):
            assert getattr(gw, fn_name)(*args) is None
        sel_factory.assert_called_once_with()
        assert "SEL audit emit" in caplog.text and "failed" in caplog.text

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

    @pytest.mark.asyncio
    async def test_a_frame_nested_past_the_decoder_is_refused_not_raised(self):
        """``RecursionError`` is not a ``JSONDecodeError``: the frame closes one
        connection cleanly instead of raising out of the connection handler."""
        from stray_line_helpers import too_deep_line

        assert await gw._read_first_frame(cast(Any, _FakeReader(line=too_deep_line()))) is None

    def test_the_ping_probe_reads_every_stray_frame_as_not_a_ping(self):
        from stray_line_helpers import STRAY_LINES

        for make in STRAY_LINES.values():
            assert gw._is_ping_frame(make()) is False
        assert gw._is_ping_frame(b'{"type":"ping"}\n') is True


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
        await await_until(
            asyncio.create_task(gw._write_json_line(cast(Any, writer), {"type": "registered"}))
        )
        assert writer.frames() == [{"type": "registered"}]
        assert writer.drains == 1


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
        task = asyncio.create_task(gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-1"))
        await async_wait_until(lambda: bool(writer.writes))
        await _drain_task(task)
        assert writer.writes == [b'{"id":1}\n']

    @pytest.mark.asyncio
    async def test_late_reply_after_disconnect_is_dropped_not_raised(self):
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await inbox.put(b'{"id":2}\n')
        writer = _FakeWriter(fail=BrokenPipeError("stub gone"))
        before = gw._active_stub_writes
        task = asyncio.create_task(gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-2"))
        await await_until(task)
        assert task.exception() is None
        assert writer.writes == [b'{"id":2}\n']
        assert gw._active_stub_writes == before

    @pytest.mark.asyncio
    async def test_stub_that_stops_reading_releases_the_writer_task(self, monkeypatch):
        monkeypatch.setattr(gw, "_WRITE_REPLY_TIMEOUT_SECS", 0.01)
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await inbox.put(b'{"id":3}\n')
        writer = _FakeWriter(hang=True)
        before = gw._active_stub_writes
        await await_until(
            asyncio.create_task(gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-3"))
        )
        assert writer.writes == [b'{"id":3}\n']
        assert gw._active_stub_writes == before

    @pytest.mark.asyncio
    async def test_write_lock_is_honoured_and_counter_returns_to_zero(self):
        before = gw._active_stub_writes
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        await inbox.put(b'{"id":4}\n')
        writer = _FakeWriter()
        setattr(writer, "_mc_write_lock", asyncio.Lock())
        task = asyncio.create_task(gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-4"))
        await async_wait_until(lambda: bool(writer.writes))
        await _drain_task(task)
        assert gw._active_stub_writes == before

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        inbox: asyncio.Queue[bytes] = asyncio.Queue()
        writer = _FakeWriter()
        task = asyncio.create_task(gw._drain_inbox_to_stub(inbox, cast(Any, writer), "stub-5"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --- declared env + target resolution ---------------------------------------


#: The agent these sidecars are written for. The daemon reads it off the
#: Register frame, not off the PoolKey, so a test writing a sidecar names it the
#: same way the rewriter does.
_DECLARING_AGENT = "demo-agent"


class TestDeclaredNonSecretEnv:
    def _write_sidecar(self, key: PoolKey, payload: dict[str, str]) -> Path:
        overlay = resolve_overlay_dir()
        directory = env_sidecar_dir(overlay)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / env_sidecar_name(_DECLARING_AGENT, key.server_name)
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
        (directory / env_sidecar_name(_DECLARING_AGENT, key.server_name)).write_text(
            "{not json", encoding="utf-8"
        )
        assert gw._declared_non_secret_env(key) == {}

    def test_non_object_sidecar_is_ignored(self):
        key = _pool_key(server="array-mcp")
        self._write_sidecar(cast(Any, key), cast(Any, ["a", "b"]))
        assert gw._declared_non_secret_env(key) == {}

    def test_unreadable_config_falls_back_to_the_default_overlay_dir(self, monkeypatch):
        monkeypatch.setattr(
            gw.KiroCrewConfig,
            "load",
            classmethod(lambda cls: (_ for _ in ()).throw(OSError("nope"))),
        )
        assert gw._declared_non_secret_env(_pool_key(server="cfgless-mcp")) == {}


class TestDeclaredEnvToForward:
    def test_flag_off_short_circuits_before_any_file_read(self, monkeypatch):
        monkeypatch.setattr(gw, "forward_declared_env_enabled", lambda: False)
        monkeypatch.setattr(gw, "_declared_non_secret_env", lambda k: {"SHOULD": "not-be-read"})
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

    def test_python_namespace_is_stripped_from_managed_spawn_env(self, monkeypatch):
        """Control-plane resolution and the token fence share a closed namespace rule.

        This pins the RESOLVER's output, which is what the classifier reads:
        no inherited ``PYTHON*`` key survives for a control-plane backend. The
        child's final environment is a separate step -- the spawn site re-applies
        Kiro Crew's own UTF-8 pair after the verdict -- and is pinned by the
        spawn-site tests.
        """
        key = _pool_key(server="kirocrew-core")
        monkeypatch.delenv("KIROCREW_MCP_TARGET_KIROCREW_CORE", raising=False)
        monkeypatch.setenv("MC_MCP_TARGET_KIROCREW_CORE", "kirocrew mcp-core")
        python_env = {
            "PYTHONPATH": "/host/site-packages",
            "PYTHONHOME": "/host/python",
            "PYTHONPYCACHEPREFIX": "/host/cache/pycache",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUSERBASE": "/host/user-site",
            "PYTHONSTARTUP": "/host/startup.py",
            "PYTHONEXECUTABLE": "/host/python",
            "PYTHON_FUTURE_IMPORT_ROOT": "/host/future",
        }
        for env_key, value in python_env.items():
            monkeypatch.setenv(env_key, value)

        resolved = gw.env_target_resolver(key)
        assert resolved is not None
        _command, _args, env, _work_dir = resolved

        assert not [env_key for env_key in env if env_key.upper().startswith("PYTHON")]

    def test_third_party_keeps_benign_python_env(self, monkeypatch):
        """A third-party backend keeps Python settings outside the four-key scrub."""
        key = _pool_key(server="third-party-mcp")
        monkeypatch.setenv("MC_MCP_TARGET_THIRD_PARTY_MCP", "python third_party_server.py")
        monkeypatch.setenv("PYTHONPATH", "/host/site-packages")
        monkeypatch.setenv("PYTHONHOME", "/host/python")
        monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/host/cache/pycache")
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
        monkeypatch.setenv("PYTHONUNBUFFERED", "1")

        resolved = gw.env_target_resolver(key)
        assert resolved is not None
        _command, _args, env, _work_dir = resolved

        assert env.get("PYTHONUNBUFFERED") == "1"
        for env_key in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONPYCACHEPREFIX",
            "PYTHONDONTWRITEBYTECODE",
        ):
            assert env_key not in env


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
        monkeypatch.setattr(os, "listdir", MagicMock(side_effect=OSError("no such directory")))
        assert gw._count_open_fds() == -1


class TestReadRssKb:
    def test_returns_a_plausible_value_on_this_platform(self):
        got = gw._read_rss_kb()
        assert isinstance(got, int)
        assert got == -1 or got > 0

    def test_delegates_to_the_shared_current_rss_reader(self, monkeypatch):
        # One shared reader backs this. The macOS ru_maxrss variant (a peak
        # that never falls) must not reappear, so pin the delegation and the
        # bytes -> KB conversion against a second implementation.
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

    def test_rotates_once_past_the_size_cap(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gw, "_ZOMBIE_DIAGNOSTIC_MAX_BYTES", 40)
        path = tmp_path / "diag.jsonl"
        for n in range(1, 7):
            gw._write_diagnostic(path, {"tag": "probe", "n": n})
        previous = tmp_path / "diag.jsonl.1"
        # Each line is 22 bytes: a file holds two, so every third append rotates.
        assert [json.loads(x)["n"] for x in path.read_text(encoding="utf-8").splitlines()] == [5, 6]
        assert [json.loads(x)["n"] for x in previous.read_text(encoding="utf-8").splitlines()] == [
            3,
            4,
        ]
        # One previous generation only (``.lock`` is the rotation try-lock).
        assert sorted(p.name for p in tmp_path.glob("diag.jsonl*")) == [
            "diag.jsonl",
            "diag.jsonl.1",
            "diag.jsonl.lock",
        ]

    def test_multiple_records_share_a_single_open(self, monkeypatch, tmp_path):
        # Records passed in one call MUST go through one open-append-close
        # cycle: a second append that lands while the first writer's handle is
        # still closing fails with a sharing violation on Windows, and the
        # never-raises contract would silently drop the record.
        path = tmp_path / "diag.jsonl"
        opens: list[str] = []
        real_open = builtins.open

        def counting_open(file, *args, **kwargs):
            if os.fspath(file) == os.fspath(path):
                opens.append(str(args))
            return real_open(file, *args, **kwargs)

        # The writer opens through builtins.open (it passes an owner-only opener).
        monkeypatch.setattr(builtins, "open", counting_open)
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
        await async_wait_until(lambda: diag.exists(), off_loop=True)
        stop.set()
        await await_until(task)

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

        await gw._zombie_diagnostic(cast(Any, server), BackendPool(max_backends=1), set(), stop)

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
        # The probe baseline and the zombie dump share one open-append-close
        # cycle. On Windows two back-to-back cycles can collide: the second
        # open lands while the first writer's handle is still closing and
        # fails with a sharing violation (a PermissionError) the never-raises
        # writer swallows, losing the zombie_detected record. Simulate that
        # by failing every open of the diagnostic file after the first: with
        # the records batched through a single open, the dump still lands; an
        # unserialized double-write drops it and this test reds.
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
                        13,
                        "The process cannot access the file because it is "
                        "being used by another process",
                    )
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", sharing_violation_open)
        # Wait on the watchdog's own completion signal, not the coroutine: the
        # watchdog swallows CancelledError, so on Python 3.11+ a timed-out
        # ``asyncio.wait_for(coro, ...)`` cannot raise TimeoutError -- the
        # cancellation never propagates, wait_for returns None, and the later
        # assertions fail in misleading ways (FileNotFoundError on diag.jsonl
        # or an unset stop event, depending on where the cancel landed). A
        # genuinely slow runner now fails legibly at the bounded wait below.
        task = asyncio.create_task(
            gw._zombie_diagnostic(cast(Any, server), BackendPool(max_backends=1), set(), stop)
        )
        stop_wait = asyncio.create_task(stop.wait())
        try:
            # Waiting on BOTH means a watchdog that raises before setting stop
            # surfaces its exception immediately instead of hiding behind the
            # full 30s budget.
            done, _ = await asyncio.wait(
                {task, stop_wait}, timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
            assert done, "watchdog neither set stop nor finished within 30s"
            if task in done:
                await task  # surface any exception the watchdog raised
        finally:
            # Never leak the watchdog past monkeypatch teardown: cancel and
            # drain whatever is still pending (the watchdog swallows
            # CancelledError, so the drain terminates promptly).
            task.cancel()
            stop_wait.cancel()
            await asyncio.gather(task, stop_wait, return_exceptions=True)

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
        await await_until(
            asyncio.create_task(
                gw._zombie_diagnostic(
                    cast(Any, MagicMock()), BackendPool(max_backends=1), set(), stop
                )
            )
        )
        assert not diag.exists()

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gw, "_zombie_diagnostic_path", lambda: tmp_path / "d.jsonl")
        stop = asyncio.Event()
        task = asyncio.create_task(
            gw._zombie_diagnostic(cast(Any, MagicMock()), BackendPool(max_backends=1), set(), stop)
        )
        await asyncio.sleep(0)
        task.cancel()
        await await_until(task)
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
    async def test_unhandled_exception_returns_one_instead_of_crashing(
        self, _quiet_amain, monkeypatch
    ):
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

    def test_telemetry_failure_never_breaks_the_hot_path(self, monkeypatch, caplog):
        get_recorder = MagicMock(side_effect=RuntimeError("recorder down"))
        monkeypatch.setattr(gw, "get_recorder", get_recorder)
        with caplog.at_level("DEBUG"):
            assert gw._emit_lazy_load_metrics(1.0, warm=False) is None
            assert gw._emit_backend_acquire_metric(1.0, warm=True) is None
        # lazy-load reads the recorder twice (its own emit, then backend.acquire).
        assert get_recorder.call_count == 3
        assert "lazy_load metric emit failed" in caplog.text
        assert "backend.acquire metric emit failed" in caplog.text


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
