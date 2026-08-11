"""Unit tests for stop/kill cancel propagation.

Covers:
- Scope A: in-flight tracking, cancel notification emission on disconnect
  and on abort frame
- Scope B: refcount-0 recycle, quarantine path
- Scope C: conservative shutdown for session-sharing subagents
- Scope E: stop_turn outcome logging, late response logging
"""
from __future__ import annotations

import asyncio
import json
import signal
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.mcp_gateway import abort as abort_mod
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.backend import Backend, _PendingRequest
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey

pytestmark = pytest.mark.xdist_group("mcp_gateway")


# --- Helpers ----------------------------------------------------------------

def _make_pool_key(server: str = "test-mcp") -> PoolKey:
    return PoolKey.from_register({
        "type": "register",
        "stub_uuid": "test-stub",
        "server_name": server,
        "agent_name": "test-agent",
        "command_args_hash": "a" * 64,
        "effective_env_hash": "b" * 64,
        "work_dir": "/tmp",
        "binary_version": "deadbeef",
        "os_uid": 1000,
        "sandbox_mode": "standard",
        "autoapprove_set_hash": "c" * 64,
        "approval_mode": "interactive",
        "trust_all_tools": False,
        "user_identity": "test-user",
        "config_snapshot_hash": "d" * 64,
    })


def _make_mock_backend(pool_key: Optional[PoolKey] = None) -> Backend:
    """Create a mock Backend with controllable stdin/stdout."""
    pk = pool_key or _make_pool_key()
    proc = MagicMock()
    proc.pid = 2**22 + 12345  # safe nonexistent PID per lesson
    proc.returncode = None
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    stdin_mock = MagicMock()
    stdin_mock.write = MagicMock()
    stdin_mock.drain = AsyncMock()
    stdin_mock.close = MagicMock()

    stdout_mock = MagicMock()

    backend = Backend(
        pool_key=pk,
        process=proc,
        stdin=stdin_mock,
        stdout=stdout_mock,
        created_at=time.time(),
        last_used_at=time.time(),
    )
    return backend


# --- Scope A: cancel_in_flight_for_stub tests --------------------------------

class TestCancelInFlight:
    """Tests for Backend.cancel_in_flight_for_stub."""

    @pytest.mark.asyncio
    async def test_cancel_sends_notifications_for_in_flight_requests(self):
        """cancel_in_flight_for_stub sends MCP notifications/cancelled."""
        backend = _make_mock_backend()
        # Simulate in-flight requests
        backend._pending_requests = {
            "gw-1-1": _PendingRequest(stub_uuid="stub-A", original_id=1, method="tools/call"),
            "gw-1-2": _PendingRequest(stub_uuid="stub-A", original_id=2, method="tools/call"),
            "gw-1-3": _PendingRequest(stub_uuid="stub-B", original_id=3, method="tools/call"),
        }

        written: list[dict] = []

        async def fake_write(writer, msg):
            written.append(msg)

        with patch("kiro_crew.mcp_gateway.backend._write_json_line", new=fake_write):
            cancelled = await backend.cancel_in_flight_for_stub("stub-A")

        assert len(cancelled) == 2
        assert "gw-1-1" in cancelled
        assert "gw-1-2" in cancelled
        # Verify the notification format
        assert all(w["method"] == "notifications/cancelled" for w in written)
        assert written[0]["params"]["requestId"] in ("gw-1-1", "gw-1-2")

    @pytest.mark.asyncio
    async def test_cancel_returns_empty_when_no_in_flight(self):
        """No in-flight requests → empty list, no writes."""
        backend = _make_mock_backend()
        backend._pending_requests = {
            "gw-1-3": _PendingRequest(stub_uuid="stub-B", original_id=3, method="tools/call"),
        }

        with patch("kiro_crew.mcp_gateway.backend._write_json_line", new=AsyncMock()):
            cancelled = await backend.cancel_in_flight_for_stub("stub-A")

        assert cancelled == []

    @pytest.mark.asyncio
    async def test_cancel_handles_broken_pipe_gracefully(self):
        """BrokenPipeError during cancel write stops further writes."""
        backend = _make_mock_backend()
        backend._pending_requests = {
            "gw-1-1": _PendingRequest(stub_uuid="stub-A", original_id=1, method="tools/call"),
            "gw-1-2": _PendingRequest(stub_uuid="stub-A", original_id=2, method="tools/call"),
        }

        call_count = 0

        async def failing_write(writer, msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise BrokenPipeError("backend dead")

        with patch("kiro_crew.mcp_gateway.backend._write_json_line", new=failing_write):
            cancelled = await backend.cancel_in_flight_for_stub("stub-A")

        # Only gets 0 or partial depending on ordering, but should not crash
        assert len(cancelled) <= 2

    @pytest.mark.asyncio
    async def test_cancel_skips_when_backend_dead(self):
        """Dead backend → no writes attempted."""
        backend = _make_mock_backend()
        backend._dead_reason = "already dead"
        backend._pending_requests = {
            "gw-1-1": _PendingRequest(stub_uuid="stub-A", original_id=1, method="tools/call"),
        }

        with patch("kiro_crew.mcp_gateway.backend._write_json_line", new=AsyncMock()) as mock_write:
            cancelled = await backend.cancel_in_flight_for_stub("stub-A")

        assert cancelled == []
        mock_write.assert_not_called()


# --- Scope B: recycle_if_idle tests ------------------------------------------

class TestRecycleIfIdle:
    """Tests for Backend.recycle_if_idle."""

    @pytest.mark.asyncio
    async def test_recycle_kills_when_refcount_zero(self):
        """refcount 0 → SIGKILL the backend."""
        backend = _make_mock_backend()
        backend.refcount = 0
        # Set up a pid that passes the guard
        pid = 2**22 + 12345
        backend.process.pid = pid

        def fake_getpgid(p):
            if p == 0:
                return 99999  # our own pgid — must differ from target
            return pid  # target process pgid

        with patch("os.getpgid", side_effect=fake_getpgid), \
             patch("os.killpg") as mock_killpg:
            result = await backend.recycle_if_idle()

        assert result is True
        mock_killpg.assert_called_once_with(pid, signal.SIGKILL)
        assert "recycled" in (backend._dead_reason or "")

    @pytest.mark.asyncio
    async def test_recycle_quarantines_when_co_tenants(self):
        """refcount > 0 → quarantine, do not kill."""
        backend = _make_mock_backend()
        backend.refcount = 2

        with patch("os.killpg") as mock_killpg:
            result = await backend.recycle_if_idle()

        assert result is False
        assert backend.quarantined is True
        mock_killpg.assert_not_called()

    @pytest.mark.asyncio
    async def test_recycle_guards_against_pid_1(self):
        """Never kill PID 1 (init)."""
        backend = _make_mock_backend()
        backend.refcount = 0
        backend.process.pid = 1

        with patch("os.killpg") as mock_killpg, \
             patch("os.kill") as mock_kill:
            result = await backend.recycle_if_idle()

        assert result is False
        mock_killpg.assert_not_called()
        mock_kill.assert_not_called()


class TestKillPathIsPlatformCorrect:
    """The teardown paths must go through ``platform_compat``'s tree-kill
    helpers, and specifically the ``_async`` variants.

    Two independent regressions are pinned here:

    1. ``os.getpgid`` / ``os.killpg`` do not exist on Windows, and the
       ``except (ProcessLookupError, OSError)`` handlers these call sites use do
       NOT catch the resulting ``AttributeError``. Calling them raised out of
       ``recycle_if_idle`` / ``shutdown`` instead of degrading, and in
       ``shutdown`` it also skipped the ``process.kill()`` fallback so the
       backend was never killed at all.
    2. Per Mesh-2801 the ``_async`` variant is mandatory from a coroutine: the
       Windows branch spawns ``taskkill`` with a 5s timeout, which stalls the
       loop. Patching only the async symbol would let a regression back to the
       sync helper pass silently, so both symbols are pinned and the sync one
       hard-fails.

    These assert the mechanism rather than the outcome on purpose -- the
    platform-specific behaviour is unreachable on this POSIX runner, so an
    outcome-only test would keep passing after a Windows-breaking regression.
    """

    @staticmethod
    def _forbid_sync(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "sync kill helper must NOT be called from a coroutine -- "
            "the _async variant exists to keep Windows taskkill off the loop"
        )

    @pytest.mark.asyncio
    async def test_recycle_awaits_async_tree_kill_not_sync(self):
        backend = _make_mock_backend()
        backend.refcount = 0
        pid = backend.process.pid

        with (
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                new_callable=AsyncMock,
            ) as mock_async,
            patch(
                "kiro_crew.platform_compat.kill_process_tree",
                side_effect=self._forbid_sync,
            ),
            # os.killpg/os.getpgid must not be reached directly any more.
            patch("os.killpg", side_effect=self._forbid_sync),
        ):
            result = await backend.recycle_if_idle()

        assert result is True
        assert mock_async.await_count == 1
        assert mock_async.await_args.args == (pid, signal.SIGKILL)

    @pytest.mark.asyncio
    async def test_shutdown_escalation_awaits_async_tree_kill_not_sync(self):
        backend = _make_mock_backend()
        # Force the escalation branch: the process never exits on its own.
        backend.process.wait = AsyncMock(side_effect=asyncio.TimeoutError())

        with (
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                new_callable=AsyncMock,
            ) as mock_async,
            patch(
                "kiro_crew.platform_compat.kill_process_tree",
                side_effect=self._forbid_sync,
            ),
            patch("os.killpg", side_effect=self._forbid_sync),
        ):
            await backend.shutdown(timeout=0.01)

        assert mock_async.await_count == 1
        assert mock_async.await_args.args[1] == signal.SIGKILL

    @pytest.mark.asyncio
    async def test_shutdown_falls_back_to_process_kill_when_tree_kill_fails(self):
        """The fallback that the uncaught AttributeError used to skip.

        A Windows ``kill_process_tree`` failure must still reach
        ``process.kill()`` -- otherwise a backend that ignores stdin close is
        never terminated.
        """
        backend = _make_mock_backend()
        backend.process.wait = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch(
            "kiro_crew.platform_compat.kill_process_tree_async",
            new_callable=AsyncMock,
            side_effect=OSError("taskkill: access denied"),
        ):
            await backend.shutdown(timeout=0.01)

        backend.process.kill.assert_called_once()


class TestTeardownUsesPortableSignalConstant:
    """``signal.SIGKILL`` does not exist on Windows, and these teardown paths
    evaluate their signal argument BEFORE the call -- so naming it that way
    raised ``AttributeError`` from inside the very call that was supposed to be
    the portable one, and no surrounding handler catches AttributeError.

    Simulated by deleting the attribute rather than by running on Windows: the
    whole of this file is in ``conftest``'s Windows ``collect_ignore`` list, so
    a test here can never observe the platform it protects. That is exactly how
    the original defect shipped -- the mechanism tests passed on Linux, where
    ``signal.SIGKILL`` exists, while the constant was unusable on the target
    platform. Deleting the attribute reproduces the Windows namespace on the
    matrix that actually runs.
    """

    @staticmethod
    def _hide_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(signal, "SIGKILL", raising=False)
        assert not hasattr(signal, "SIGKILL"), "precondition: SIGKILL must be hidden"

    @pytest.mark.asyncio
    async def test_recycle_survives_without_signal_sigkill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._hide_sigkill(monkeypatch)
        backend = _make_mock_backend()
        backend.refcount = 0

        with patch(
            "kiro_crew.platform_compat.kill_process_tree_async",
            new_callable=AsyncMock,
        ) as mock_async:
            result = await backend.recycle_if_idle()

        assert result is True
        # The portable constant is a plain int, so it survives the deletion.
        assert mock_async.await_args.args[1] == 9

    @pytest.mark.asyncio
    async def test_shutdown_survives_without_signal_sigkill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._hide_sigkill(monkeypatch)
        backend = _make_mock_backend()
        backend.process.wait = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch(
            "kiro_crew.platform_compat.kill_process_tree_async",
            new_callable=AsyncMock,
        ) as mock_async:
            await backend.shutdown(timeout=0.01)

        assert mock_async.await_count == 1

    @pytest.mark.asyncio
    async def test_orphan_reap_survives_without_signal_sigkill(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from kiro_crew.mcp_gateway import manager as mgr

        self._hide_sigkill(monkeypatch)
        socket_path = tmp_path / "gateway.sock"
        (tmp_path / "gateway.sock.backends").write_text("111\n", encoding="utf-8")

        manager = object.__new__(mgr.GatewayManager)
        manager._spec = MagicMock()
        manager._spec.socket_path = str(socket_path)

        with patch(
            "kiro_crew.platform_compat.kill_process_tree_async",
            new_callable=AsyncMock,
        ) as mock_async:
            await manager._reap_orphaned_backends()

        assert mock_async.await_args.args == (111, 9)


class TestOrphanReapIsPlatformCorrect:
    """``_reap_orphaned_backends`` used a bare ``os.killpg`` per recorded pid.

    On Windows that raises ``AttributeError`` (uncaught by its handler), and it
    is awaited from ``_terminate_process``, so the sync helper would also spawn
    one 5s-timeout ``taskkill`` per pid directly on the loop.
    """

    @pytest.mark.asyncio
    async def test_reap_awaits_async_tree_kill_for_each_pid(self, tmp_path):
        from kiro_crew.mcp_gateway import manager as mgr

        socket_path = tmp_path / "gateway.sock"
        (tmp_path / "gateway.sock.backends").write_text("111 222\n", encoding="utf-8")

        manager = object.__new__(mgr.GatewayManager)
        manager._spec = MagicMock()
        manager._spec.socket_path = str(socket_path)

        def _forbid_sync(*_a: Any, **_kw: Any) -> None:
            raise AssertionError("sync kill_process_tree must not be called from a coroutine")

        with (
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                new_callable=AsyncMock,
            ) as mock_async,
            patch(
                "kiro_crew.platform_compat.kill_process_tree",
                side_effect=_forbid_sync,
            ),
            patch("os.killpg", side_effect=_forbid_sync),
        ):
            await manager._reap_orphaned_backends()

        assert [c.args[0] for c in mock_async.await_args_list] == [111, 222]


# --- Scope A (gatewayd): abort frame handler tests ---------------------------

class TestDetachOnCancelFailure:
    """cancel_in_flight_for_stub raising must not skip detach_stub —
    otherwise the backend's refcount leaks and it can never be recycled."""

    @pytest.mark.asyncio
    async def test_detach_still_called_when_cancel_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.mcp_gateway import socketsec

        monkeypatch.setattr(socketsec, "PEER_IDENTITY_SUPPORTED", True)
        monkeypatch.setattr(
            socketsec, "check_peer_is_self",
            lambda _w: socketsec.PeerCredResult.MATCH,
        )

        class _FakeReader:
            def __init__(self, frames: list) -> None:
                self._q = [(json.dumps(f) + "\n").encode() for f in frames]

            async def readuntil(self, sep: bytes = b"\n") -> bytes:
                if not self._q:
                    raise asyncio.IncompleteReadError(b"", None)
                return self._q.pop(0)

        class _FakeWriter:
            def write(self, _b: bytes) -> None: ...
            async def drain(self) -> None: ...
            def close(self) -> None: ...
            async def wait_closed(self) -> None: ...

            def is_closing(self) -> bool:
                return False

            def get_extra_info(self, _name: str, default: Any = None) -> Any:
                return default

        class _RaisingBackend:
            supports_caller_identity = True
            quarantined = False

            def __init__(self) -> None:
                self.detach_called = False
                self._pending_requests: dict = {}

            async def attach_stub(self, _uuid: str) -> "asyncio.Queue[bytes]":
                return asyncio.Queue()

            async def detach_stub(self, _uuid: str) -> int:
                self.detach_called = True
                return 0

            async def cancel_in_flight_for_stub(self, _uuid: str) -> list:
                raise RuntimeError("dict changed size during iteration")

            async def recycle_if_idle(self) -> bool:
                return False

            async def forward_from_stub(self, *_a: Any, **_k: Any) -> None: ...

        fake_backend = _RaisingBackend()

        async def _fake_acquire(_pool: Any, _key: Any, _resolver: Any, **_kw: Any):
            return fake_backend, True

        async def _fake_drain(_inbox: Any, _writer: Any, _stub_uuid: str = "") -> None:
            await asyncio.sleep(0)

        class _FakeSEL:
            def log_api_access(self, **_kwargs: Any) -> None: ...

        class _FakePool:
            """Fork divergence: the lazy-spawn attach path releases its
            hand-out reservation via ``pool.unreserve`` in a ``finally``
            (absent upstream at this commit), so a bare ``object()`` would
            raise AttributeError. Same double as test_mcp_gateway_claim."""

            def unreserve(self, _key: object) -> None:
                pass

        monkeypatch.setattr(gw, "SecurityEventLog", _FakeSEL)
        monkeypatch.setattr(gw, "_acquire_backend", _fake_acquire)
        monkeypatch.setattr(gw, "_drain_inbox_to_stub", _fake_drain)

        register = {
            "type": "register",
            "stub_uuid": "leak-stub-0001",
            "server_name": "echo-mcp",
            "agent_name": "leak-agent",
            "command_args_hash": "0" * 64,
            "effective_env_hash": "1" * 64,
            "work_dir": "/tmp",
            "binary_version": "deadbeef",
            "os_uid": 1000,
            "sandbox_mode": "standard",
            "autoapprove_set_hash": "2" * 64,
            "approval_mode": "interactive",
            "trust_all_tools": False,
            "user_identity": "leak",
            "channel_id": "C_LEAK",
            "config_snapshot_hash": "3" * 64,
            "session_key": "dashboard:chat-leak",
            "session_type": "dashboard",
            "principal_id": "",
        }

        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "x"}}

        await asyncio.wait_for(
            gw._handle_connection(
                _FakeReader([register, call]), _FakeWriter(), pool=_FakePool(),
                resolver=object(), socket_path=Path("/tmp/leak.sock"),
                hot_keys=None,
            ),
            timeout=5.0,
        )

        # The critical cleanup must run despite the cancel failure.
        assert fake_backend.detach_called


class TestApplyAbort:
    """Tests for gatewayd._apply_abort."""

    @pytest.mark.asyncio
    async def test_abort_rejects_missing_pids(self):
        pool = MagicMock()
        result = await gw._apply_abort({"type": "abort"}, pool)
        assert result["type"] == "abort-rejected"

    @pytest.mark.asyncio
    async def test_abort_rejects_invalid_pids(self):
        pool = MagicMock()
        result = await gw._apply_abort({"type": "abort", "pids": [0, -1, True]}, pool)
        assert result["type"] == "abort-rejected"

    @pytest.mark.asyncio
    async def test_abort_cancels_in_flight_for_indexed_stubs(self):
        """Valid abort frame → cancels in-flight work for stubs under PIDs."""
        pool = MagicMock()
        backend = _make_mock_backend()
        backend.cancel_in_flight_for_stub = AsyncMock(return_value=["gw-1-1"])
        pool.all_backends.return_value = [backend]

        test_pid = 424242
        # Set up a connection in the _CONN_INDEX
        conn = gw._StubConn("test-stub-001", [test_pid], "test-pool", None)
        gw._conn_index_add(conn)
        try:
            result = await gw._apply_abort(
                {"type": "abort", "pids": [test_pid], "reason": "test"},
                pool,
            )
            assert result["type"] == "aborted"
            assert result["cancelled"] >= 1
            backend.cancel_in_flight_for_stub.assert_called_with("test-stub-001")
        finally:
            gw._conn_index_discard(conn)


# --- Scope A: abort module tests ---------------------------------------------

class TestAbortModule:
    """Tests for mcp_gateway.abort module."""

    def test_build_abort_frame(self):
        frame = abort_mod.build_abort_frame([100, 200], "test stop")
        assert frame == {
            "type": "abort",
            "pids": [100, 200],
            "reason": "test stop",
        }

    @pytest.mark.asyncio
    async def test_schedule_abort_noop_without_socket(self):
        """No socket → no-op."""
        abort_mod.schedule_abort(None, [100])
        # Should not raise

    @pytest.mark.asyncio
    async def test_schedule_abort_noop_without_valid_pids(self):
        """Empty/invalid pids → no-op."""
        abort_mod.schedule_abort("/tmp/test.sock", [0, -1])
        # Should not raise

    @pytest.mark.asyncio
    async def test_send_abort_timeout(self):
        """Timeout returns empty dict gracefully."""
        result = await abort_mod.send_abort(
            "/nonexistent/path.sock", [100], "test"
        )
        assert result == {}


# --- Scope C: conservative shutdown for session-sharing subagents -------------

class TestConservativeShutdown:
    """Tests for subagent._force_reap conservative shutdown (session-sharing)."""

    @pytest.mark.asyncio
    async def test_session_sharing_never_kills_runtime(self):
        """Session-sharing subagent reap → conservative shutdown only, NEVER SIGKILL."""
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        info = SubagentInfo(
            id="agent-001",
            task="test task",
            parent_session_key="dashboard:chat-1",
            _session_sharing=True,
            _pid=2**22 + 99999,  # safe nonexistent PID
            _shared_provider=AsyncMock(),
        )

        mgr = SubagentManager.__new__(SubagentManager)
        mgr._agents = {"agent-001": info}
        mgr._tasks = {}
        mgr._report_tasks = set()
        mgr._report_owners = {}
        # Fork adaptation: _force_reap pumps the spawn queue after freeing a
        # slot (a1933a4b, ported earlier in this branch); an empty queue makes
        # _drain_queue return immediately without touching other attrs.
        mgr._queue = []
        mgr._running_count = 1
        mgr._default_timeout = 300
        mgr._write_tombstone = MagicMock()
        mgr._record_cost = MagicMock()
        mgr._on_event = None
        mgr._on_done = None

        async def noop_fire(*a, **kw):
            pass

        mgr._fire_event = noop_fire

        with patch("os.kill") as mock_kill, \
             patch("kiro_crew.subagent.sel") as mock_sel, \
             patch("kiro_crew.subagent.Stats") as mock_stats:
            mock_sel.return_value = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_stats.return_value = MagicMock()
            mgr._sessions = MagicMock()
            mgr._sessions.release = MagicMock()
            await mgr._force_reap("agent-001", info, 400.0)

        # Must NEVER kill the shared runtime — only shutdown the provider handle
        mock_kill.assert_not_called()
        info._shared_provider.shutdown.assert_called_once()
        # SEL audit records conservative-shutdown
        audit_calls = [
            c for c in mock_sel.return_value.log_tool_invocation.call_args_list
            if c.kwargs.get("tool_name") == "smart_hard_kill"
        ]
        assert len(audit_calls) == 1
        assert audit_calls[0].kwargs["outcome"] == "conservative-shutdown"

    @pytest.mark.asyncio
    async def test_session_sharing_with_co_tenants_still_conservative(self):
        """Even with co-tenants, session-sharing → conservative shutdown (same path)."""
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        shared_pid = 2**22 + 88888
        info_a = SubagentInfo(
            id="agent-001",
            task="task A",
            parent_session_key="dashboard:chat-1",
            _session_sharing=True,
            _pid=shared_pid,
            _shared_provider=AsyncMock(),
        )
        info_b = SubagentInfo(
            id="agent-002",
            task="task B",
            parent_session_key="dashboard:chat-1",
            _session_sharing=True,
            _pid=shared_pid,
            _shared_provider=AsyncMock(),
        )

        mgr = SubagentManager.__new__(SubagentManager)
        mgr._agents = {"agent-001": info_a, "agent-002": info_b}
        mgr._tasks = {}
        mgr._report_tasks = set()
        mgr._report_owners = {}
        # Fork adaptation: see test_session_sharing_never_kills_runtime.
        mgr._queue = []
        mgr._running_count = 2
        mgr._default_timeout = 300
        mgr._write_tombstone = MagicMock()
        mgr._record_cost = MagicMock()
        mgr._on_event = None
        mgr._on_done = None

        async def noop_fire(*a, **kw):
            pass

        mgr._fire_event = noop_fire

        with patch("os.kill") as mock_kill, \
             patch("kiro_crew.subagent.sel") as mock_sel, \
             patch("kiro_crew.subagent.Stats") as mock_stats:
            mock_sel.return_value = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            mock_stats.return_value = MagicMock()
            mgr._sessions = MagicMock()
            mgr._sessions.release = MagicMock()
            await mgr._force_reap("agent-001", info_a, 400.0)

        # Should NOT kill — conservative shutdown regardless of co-tenants
        mock_kill.assert_not_called()
        info_a._shared_provider.shutdown.assert_called_once()


# --- Scope E: stop_turn outcome logging test ---------------------------------

class TestAbortAckLogging:
    """Tests for abort.send_abort acknowledgment handling and logging."""

    @pytest.mark.asyncio
    async def test_send_abort_logs_ack_from_real_roundtrip(self, caplog, short_sock_dir):
        """A real socket round-trip through send_abort() logs the ack produced
        by production code (not a manually-emitted log line)."""
        import logging

        socket_path = str(short_sock_dir / "gw.sock")

        async def _fake_gatewayd(reader, writer):
            frame = json.loads((await reader.readline()).decode("utf-8"))
            assert frame["type"] == "abort"
            assert frame["pids"] == [100]
            writer.write(
                json.dumps({"type": "aborted", "cancelled": 3, "stubs": 1}).encode("utf-8")
                + b"\n"
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(_fake_gatewayd, path=socket_path)
        try:
            with caplog.at_level(logging.INFO, logger=abort_mod.logger.name):
                resp = await abort_mod.send_abort(socket_path, [100], "test stop")
            assert resp == {"type": "aborted", "cancelled": 3, "stubs": 1}
            assert any(
                "abort-push acknowledged" in rec.getMessage() for rec in caplog.records
            )
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_send_abort_logs_warning_on_bad_ack(self, caplog, short_sock_dir):
        """A malformed ack from gatewayd logs a warning via production code."""
        import logging

        socket_path = str(short_sock_dir / "gw.sock")

        async def _fake_gatewayd(reader, writer):
            await reader.readline()
            writer.write(b'{"type": "unexpected"}\n')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(_fake_gatewayd, path=socket_path)
        try:
            with caplog.at_level(logging.WARNING, logger=abort_mod.logger.name):
                resp = await abort_mod.send_abort(socket_path, [100], "test stop")
            assert resp == {"type": "unexpected"}
            assert any(
                "abort-push not acknowledged" in rec.getMessage() for rec in caplog.records
            )
        finally:
            server.close()
            await server.wait_closed()


# --- Pool.all_backends test --------------------------------------------------

class TestPoolAllBackends:
    """Tests for BackendPool.all_backends."""

    @pytest.mark.asyncio
    async def test_all_backends_returns_snapshot(self):
        pool = BackendPool(max_backends=10)
        backend = _make_mock_backend()
        pk = _make_pool_key()
        async with pool._lock:
            pool._backends[pk.stable_hash()] = backend
        result = pool.all_backends()
        assert len(result) == 1
        assert result[0] is backend

    @pytest.mark.asyncio
    async def test_all_backends_empty(self):
        pool = BackendPool(max_backends=10)
        assert pool.all_backends() == []
