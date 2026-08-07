"""Behavioural coverage for :mod:`kiro_crew.mcp_gateway.backend`.

Complements the existing focused suites (``test_mcp_gateway_wedge_ping_gate``
for heartbeat classification, ``test_mcp_gateway_apps_spool`` for the MCP Apps
interception seam, ``test_mcp_gateway_oversize`` for the spill helpers) by
driving the parts of :class:`~kiro_crew.mcp_gateway.backend.Backend` that had no
direct test:

* ``forward_from_stub`` — id rewriting, caller-identity strip/inject, the
  ``notifications/initialized`` suppression, ``notifications/cancelled``
  requestId remapping, and the dead-backend / broken-pipe error paths.
* The initialize state machine — first stub upstream, later stubs queued,
  cached replay, terminal failure, and ``prime_initialize`` on respawn.
* The stdout pump end to end against a real :class:`asyncio.StreamReader`
  (EOF, mid-line truncation, oversize-line drain, spill hook).
* ``_route_backend_line`` routing/attribution: heartbeat pongs, unknown ids,
  notification ownership, global broadcast, deny-by-default drop, and the
  server->client request recycle.
* Terminal/teardown paths: ``_broadcast_backend_gone``, ``_enqueue_to_stub``
  overflow, ``cancel_in_flight_for_stub``, ``recycle_if_idle``, ``shutdown``.
* ``spawn_backend`` / ``send_initialize`` with the subprocess seam stubbed, and
  the ``_write_json_line`` / ``_pump_stderr`` / metrics helpers.

Everything runs against in-memory pipes: no real subprocess, no network, no
sandbox, no fixed ports, no wall-clock waits (bounded waits use ``timeout=0``,
which asyncio resolves synchronously).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_caller import CALLER_META_KEY, CallerContext
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import (
    HEARTBEAT_PING_ID,
    MCP_APPS_ENV_FLAG,
    MCP_APPS_EXTENSION_KEY,
    MCP_APPS_MIME_TYPE,
    Backend,
    BackendGone,
    _inject_caller_meta,
    _inject_client_extensions,
    _is_heartbeat_id,
    _mcp_apps_enabled,
    _PendingRequest,
    _pump_stderr,
    _strip_caller_meta,
    _write_json_line,
    send_initialize,
    spawn_backend,
)
from kiro_crew.mcp_gateway.pool import PoolKey


@pytest.fixture(autouse=True)
def _no_real_metrics_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never append to the operator's real call-metrics file.

    ``backend._METRICS_PATH`` is resolved from ``MCP_GATEWAY_CALL_METRICS_PATH``
    at IMPORT time. On a machine where that variable is set, response routing
    schedules ``_emit_call_metric`` and these tests would append to that real
    path -- a host-level side effect outside ``tmp_path``, and one that only
    appears on the machines that have the variable set. Force it off for the
    whole module rather than per-test, so a newly added test cannot reintroduce
    the leak by forgetting the guard.
    """

    monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)


# --- Helpers ----------------------------------------------------------------


def _pool_key(server: str = "example-mcp") -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name="kirocrew",
        command_args_hash="cah",
        effective_env_hash="eeh",
        work_dir="/nonexistent-work-dir",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="aah",
        approval_mode="reads",
        trust_all_tools=False,
        user_identity="testuser",
        config_snapshot_hash="csh",
    )


def _make_backend(
    *,
    stdout: Optional[Any] = None,
    returncode: Optional[int] = None,
) -> Backend:
    """A real :class:`Backend` over a mock process + mock stdin writer."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 4242
    proc.wait = AsyncMock(return_value=returncode if returncode is not None else 0)
    proc.kill = MagicMock()
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.close = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    return Backend(
        pool_key=_pool_key(),
        process=cast(Any, proc),
        stdin=cast(Any, stdin),
        stdout=cast(Any, stdout if stdout is not None else MagicMock()),
        created_at=now,
        last_used_at=now,
    )


def _frames(backend: Backend) -> list[dict]:
    """Decode every JSON-RPC frame written to the backend's mock stdin."""
    out: list[dict] = []
    for call in cast(Any, backend.stdin).write.call_args_list:
        for line in call.args[0].decode("utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _reader(*chunks: bytes, eof: bool = True, limit: int = 65536) -> asyncio.StreamReader:
    """A pre-filled StreamReader. Must be built inside a running loop."""
    reader = asyncio.StreamReader(limit=limit)
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


def _line(obj: Any) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


async def _drain(inbox: "asyncio.Queue[bytes]") -> dict:
    return json.loads(inbox.get_nowait().decode("utf-8"))


async def _settle(backend: Backend) -> None:
    """Await any background metric/apps tasks so nothing is GC'd mid-flight."""
    tasks = list(backend._metric_tasks) + list(backend._apps_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --- Pure request-shaping helpers -------------------------------------------


class TestFrameHelpers:
    def test_strip_caller_meta_removes_forged_block(self) -> None:
        msg: dict[str, Any] = {
            "method": "tools/call",
            "params": {"_meta": {CALLER_META_KEY: {"sessionKey": "forged"},
                                 "progressToken": "pt"}},
        }
        out = _strip_caller_meta(msg)
        assert CALLER_META_KEY not in out["params"]["_meta"]
        assert out["params"]["_meta"]["progressToken"] == "pt"
        # Original frame is not aliased/mutated.
        assert CALLER_META_KEY in msg["params"]["_meta"]

    def test_strip_caller_meta_drops_empty_meta_entirely(self) -> None:
        msg = {"method": "tools/call", "params": {"_meta": {CALLER_META_KEY: {}}}}
        out = _strip_caller_meta(msg)
        assert "_meta" not in out["params"]

    def test_strip_caller_meta_passthrough_shapes(self) -> None:
        # No params at all, non-dict params, no _meta, non-dict _meta.
        assert _strip_caller_meta({"method": "ping"}) == {"method": "ping"}
        assert _strip_caller_meta({"params": 5})["params"] == 5
        assert _strip_caller_meta({"params": {}})["params"] == {}
        assert _strip_caller_meta({"params": {"_meta": "bad"}})["params"]["_meta"] == "bad"

    def test_inject_caller_meta_synthesizes_params(self) -> None:
        out = _inject_caller_meta({"method": "tools/call"},
                                  CallerContext(session_key="dashboard:1"))
        block = out["params"]["_meta"][CALLER_META_KEY]
        assert block["sessionKey"] == "dashboard:1"

    def test_inject_caller_meta_preserves_other_meta(self) -> None:
        msg: dict[str, Any] = {
            "method": "tools/call",
            "params": {"_meta": {"progressToken": 7}, "name": "t"},
        }
        out = _inject_caller_meta(msg, CallerContext(session_key="s"))
        assert out["params"]["_meta"]["progressToken"] == 7
        assert out["params"]["name"] == "t"
        assert "_meta" in msg["params"] and CALLER_META_KEY not in msg["params"]["_meta"]

    def test_is_heartbeat_id_accepts_int_and_string(self) -> None:
        assert _is_heartbeat_id(HEARTBEAT_PING_ID)
        assert _is_heartbeat_id(str(HEARTBEAT_PING_ID))
        assert not _is_heartbeat_id("gw-1-2")
        assert not _is_heartbeat_id(None)


class TestClientExtensionInjection:
    def test_noop_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
        msg = {"method": "initialize", "params": {"capabilities": {}}}
        assert _inject_client_extensions(msg) is msg

    def test_injects_ui_extension(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        msg: dict[str, Any] = {
            "method": "initialize", "params": {"capabilities": {"roots": {}}},
        }
        out = _inject_client_extensions(msg)
        ext = out["params"]["capabilities"]["extensions"][MCP_APPS_EXTENSION_KEY]
        assert ext == {"mimeTypes": [MCP_APPS_MIME_TYPE]}
        # Pre-existing capabilities preserved; caller's frame untouched.
        assert out["params"]["capabilities"]["roots"] == {}
        assert "extensions" not in msg["params"]["capabilities"]

    def test_preserves_caller_declared_extension(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        msg = {
            "method": "initialize",
            "params": {"capabilities": {"extensions": {MCP_APPS_EXTENSION_KEY: {"mine": 1}}}},
        }
        out = _inject_client_extensions(msg)
        assert out["params"]["capabilities"]["extensions"][MCP_APPS_EXTENSION_KEY] == {"mine": 1}

    def test_non_dict_params_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        msg = {"method": "initialize", "params": None}
        assert _inject_client_extensions(msg) is msg

    def test_env_kill_switch_beats_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "off")
        assert _mcp_apps_enabled() is False
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "yes")
        assert _mcp_apps_enabled() is True


# --- Bookkeeping ------------------------------------------------------------


class TestAttachDetachAndAccounting:
    @pytest.mark.asyncio
    async def test_attach_twice_rejected(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        with pytest.raises(RuntimeError, match="already attached"):
            await backend.attach_stub("s1")
        assert backend.refcount == 1

    @pytest.mark.asyncio
    async def test_detach_clears_pending_and_init_queue(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        backend._pending_requests["gw-2"] = _PendingRequest("s2", 2, "tools/call")
        backend._init_pending = [("s1", 10), ("s2", 20)]

        assert await backend.detach_stub("s1") == 1
        assert list(backend._pending_requests) == ["gw-2"]
        assert backend._init_pending == [("s2", 20)]
        # Last detach restarts the idle clock.
        before = backend.last_used_at
        assert await backend.detach_stub("s2") == 0
        assert backend.last_used_at >= before

    @pytest.mark.asyncio
    async def test_detach_unknown_stub_is_noop(self) -> None:
        backend = _make_backend()
        assert await backend.detach_stub("ghost") == 0

    @pytest.mark.asyncio
    async def test_outstanding_work_sums_all_three_sources(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        assert backend.outstanding_work == 0
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        inbox.put_nowait(b"queued\n")
        task = asyncio.create_task(asyncio.sleep(3600))
        backend._apps_tasks.add(task)
        try:
            assert backend.outstanding_work == 3
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_properties_and_touch(self) -> None:
        backend = _make_backend()
        assert backend.pid == 4242
        assert backend.is_alive is True
        assert backend.dead_reason is None
        assert Backend._now() > 0
        backend.touch(now=123.0)
        assert backend.last_used_at == 123.0
        backend._dead_reason = "boom"
        assert backend.is_alive is False
        assert backend.dead_reason == "boom"

    def test_forward_ids_are_monotonic_and_pid_scoped(self) -> None:
        backend = _make_backend()
        assert backend._next_forward_id() == "gw-4242-1"
        assert backend._next_forward_id() == "gw-4242-2"


# --- forward_from_stub ------------------------------------------------------


class TestForwardFromStub:
    @pytest.mark.asyncio
    async def test_dead_backend_raises_backend_gone(self) -> None:
        backend = _make_backend()
        backend._dead_reason = "exit rc=1"
        with pytest.raises(BackendGone, match="exit rc=1"):
            await backend.forward_from_stub("s1", {"method": "tools/list", "id": 1})

    @pytest.mark.asyncio
    async def test_stub_initialized_notification_never_reaches_backend(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {"method": "notifications/initialized"})
        assert _frames(backend) == []

    @pytest.mark.asyncio
    async def test_request_id_rewritten_and_pending_captured(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {
            "method": "tools/call",
            "id": 77,
            "params": {
                "name": "draw",
                "arguments": {"x": 1},
                "_meta": {"progressToken": "pt-9"},
            },
        })
        (frame,) = _frames(backend)
        assert frame["id"] == "gw-4242-1"
        pending = backend._pending_requests["gw-4242-1"]
        assert (pending.stub_uuid, pending.original_id, pending.method) == (
            "s1", 77, "tools/call")
        assert pending.tool_name == "draw"
        assert pending.tool_arguments == {"x": 1}
        assert pending.progress_token == "pt-9"
        assert pending.t_start_ms > 0

    @pytest.mark.asyncio
    async def test_non_string_tool_name_and_non_dict_args_ignored(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {
            "method": "tools/call", "id": 1,
            "params": {"name": 42, "arguments": "not-a-dict"},
        })
        pending = backend._pending_requests["gw-4242-1"]
        assert pending.tool_name == ""
        assert pending.tool_arguments is None

    @pytest.mark.asyncio
    async def test_caller_identity_injected_only_when_advertised(self) -> None:
        caller = CallerContext(session_key="dashboard:abc", session_type="dashboard")
        off = _make_backend()
        await off.forward_from_stub("s1", {"method": "tools/call", "id": 1}, caller=caller)
        assert "_meta" not in _frames(off)[0].get("params", {})

        on = _make_backend()
        on.supports_caller_identity = True
        await on.forward_from_stub("s1", {"method": "tools/call", "id": 1}, caller=caller)
        meta = _frames(on)[0]["params"]["_meta"][CALLER_META_KEY]
        assert meta["sessionKey"] == "dashboard:abc"
        assert on._pending_requests["gw-4242-1"].session_key == "dashboard:abc"

    @pytest.mark.asyncio
    async def test_forged_caller_stripped_even_without_injection(self) -> None:
        """The strip is unconditional: a stub that registered without a session
        key must not be able to forge an identity on a non-tools/call method."""
        backend = _make_backend()
        await backend.forward_from_stub("s1", {
            "method": "resources/list", "id": 3,
            "params": {"_meta": {CALLER_META_KEY: {"sessionKey": "victim"}}},
        })
        assert "_meta" not in _frames(backend)[0]["params"]

    @pytest.mark.asyncio
    async def test_pure_notification_and_pure_response_pass_through(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {"method": "notifications/roots/list_changed"})
        await backend.forward_from_stub("s1", {"id": "server-req-1", "result": {"ok": True}})
        notif, response = _frames(backend)
        assert notif["method"] == "notifications/roots/list_changed"
        # A pure response keeps the backend-owned id untouched.
        assert response["id"] == "server-req-1"
        assert backend._pending_requests == {}

    @pytest.mark.asyncio
    async def test_cancelled_request_id_remapped_to_gateway_id(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {"method": "tools/call", "id": 5})
        await backend.forward_from_stub("s1", {
            "method": "notifications/cancelled",
            "params": {"requestId": 5, "reason": "user stopped"},
        })
        assert _frames(backend)[1]["params"]["requestId"] == "gw-4242-1"

    @pytest.mark.asyncio
    async def test_cancelled_for_other_stubs_request_not_remapped(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {"method": "tools/call", "id": 5})
        await backend.forward_from_stub("s2", {
            "method": "notifications/cancelled", "params": {"requestId": 5},
        })
        assert _frames(backend)[1]["params"]["requestId"] == 5

    @pytest.mark.asyncio
    async def test_cancelled_without_request_id_passes_through(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {"method": "notifications/cancelled",
                                               "params": {}})
        assert _frames(backend)[0]["params"] == {}

    @pytest.mark.asyncio
    async def test_broken_pipe_marks_backend_gone(self) -> None:
        backend = _make_backend()
        cast(Any, backend.stdin).write.side_effect = BrokenPipeError("epipe")
        with pytest.raises(BackendGone, match="stdin closed"):
            await backend.forward_from_stub("s1", {"method": "tools/list", "id": 1})
        assert backend.is_alive is False

    @pytest.mark.asyncio
    async def test_non_dict_message_is_forwarded_verbatim(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", cast(Any, ["not", "a", "dict"]))
        assert _frames(backend) == [["not", "a", "dict"]]


# --- Initialize state machine ----------------------------------------------


class TestInitializeStateMachine:
    @staticmethod
    def _init(id_: Any = 1) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": id_, "method": "initialize",
                "params": {"capabilities": {}}}

    @pytest.mark.asyncio
    async def test_first_stub_drives_upstream_handshake(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", self._init(1))
        (frame,) = _frames(backend)
        assert frame["id"] == "gw-4242-1"
        assert backend._init_state == "in_flight"
        assert backend._init_first_stub == "s1"
        assert backend._init_first_id == 1
        assert backend._pending_requests["gw-4242-1"].stub_uuid == "__init__"
        assert backend._init_pending == [("s1", 1)]

    @pytest.mark.asyncio
    async def test_second_stub_queues_while_in_flight(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", self._init(1))
        await backend.forward_from_stub("s2", self._init(2))
        # Only ONE upstream initialize.
        assert len(_frames(backend)) == 1
        assert backend._init_pending == [("s1", 1), ("s2", 2)]

    @pytest.mark.asyncio
    async def test_initialize_forged_caller_stripped_and_extensions_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        await backend.attach_stub("s1")
        msg = self._init(1)
        msg["params"]["_meta"] = {CALLER_META_KEY: {"sessionKey": "forged"}}
        await backend.forward_from_stub("s1", msg)
        params = _frames(backend)[0]["params"]
        assert "_meta" not in params
        assert MCP_APPS_EXTENSION_KEY in params["capabilities"]["extensions"]

    @pytest.mark.asyncio
    async def test_initialize_without_id_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="initialize without id"):
            await backend.forward_from_stub("s1", {"method": "initialize"})

    @pytest.mark.asyncio
    async def test_ready_state_replays_cached_result(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s2")
        backend._init_state = "ready"
        backend._init_result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        await backend.forward_from_stub("s2", self._init(99))
        # Nothing hit the backend; the stub got a synthesized reply.
        assert _frames(backend) == []
        assert await _drain(inbox) == {
            "jsonrpc": "2.0", "id": 99, "result": backend._init_result,
        }

    @pytest.mark.asyncio
    async def test_cached_replay_to_detached_stub_is_dropped(self) -> None:
        backend = _make_backend()
        backend._init_state = "ready"
        backend._init_result = {"capabilities": {}}
        await backend.forward_from_stub("ghost", self._init(1))  # no inbox
        assert _frames(backend) == []

    @pytest.mark.asyncio
    async def test_failed_state_raises_backend_gone(self) -> None:
        # No ``_dead_reason``: the backend is still "alive", so the rejection
        # must come from the initialize state machine itself.
        backend = _make_backend()
        backend._init_state = "failed"
        with pytest.raises(BackendGone, match="backend initialize failed"):
            await backend.forward_from_stub("s1", self._init(1))

    @pytest.mark.asyncio
    async def test_initialize_write_failure_marks_gone(self) -> None:
        backend = _make_backend()
        cast(Any, backend.stdin).write.side_effect = ConnectionResetError("reset")
        with pytest.raises(BackendGone, match="stdin closed"):
            await backend.forward_from_stub("s1", self._init(1))


class TestUpstreamInitializeResolution:
    @pytest.mark.asyncio
    async def test_success_caches_flushes_and_sends_initialized(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        backend._init_pending = [("s1", 1), ("s2", 2)]
        result: dict[str, Any] = {
            "capabilities": {"experimental": {"kirocrew.caller-identity": {}}}}
        await backend._on_upstream_initialize({"jsonrpc": "2.0", "id": "gw-1",
                                               "result": result})
        assert backend._init_state == "ready"
        assert backend._init_result == result
        assert backend.supports_caller_identity is True
        assert backend._init_done_event.is_set()
        # Exactly one synthetic notifications/initialized upstream.
        assert [f["method"] for f in _frames(backend)] == ["notifications/initialized"]
        assert (await _drain(inbox1))["id"] == 1
        assert (await _drain(inbox2))["id"] == 2

    @pytest.mark.asyncio
    async def test_capability_absent_leaves_flag_false(self) -> None:
        backend = _make_backend()
        await backend._on_upstream_initialize({"result": {"capabilities": {}}})
        assert backend.supports_caller_identity is False

    @pytest.mark.asyncio
    async def test_initialized_write_failure_recorded_not_raised(self) -> None:
        backend = _make_backend()
        cast(Any, backend.stdin).write.side_effect = BrokenPipeError("epipe")
        await backend._on_upstream_initialize({"result": {"capabilities": {}}})
        assert backend._init_state == "ready"
        assert "stdin closed during initialized" in (backend.dead_reason or "")

    @pytest.mark.asyncio
    async def test_error_response_fails_every_queued_stub(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._init_pending = [("s1", 7), ("ghost", 8)]
        await backend._on_upstream_initialize(
            {"jsonrpc": "2.0", "id": "gw-1", "error": {"code": -1, "message": "nope"}}
        )
        assert backend._init_state == "failed"
        assert backend._init_done_event.is_set()
        assert backend._init_pending == []
        err = await _drain(inbox)
        assert err["id"] == 7
        assert err["error"]["code"] == -32000
        assert "initialize error" in err["error"]["message"]

    @pytest.mark.asyncio
    async def test_malformed_result_fails_init(self) -> None:
        backend = _make_backend()
        await backend._on_upstream_initialize({"result": "not-a-dict"})
        assert backend._init_state == "failed"
        assert "missing/malformed result" in (backend.dead_reason or "")


class TestPrimeInitialize:
    @pytest.mark.asyncio
    async def test_ready_backend_is_a_noop(self) -> None:
        backend = _make_backend()
        backend._init_state = "ready"
        await backend.prime_initialize({"method": "initialize", "id": 1})
        assert _frames(backend) == []

    @pytest.mark.asyncio
    async def test_dead_backend_rejected(self) -> None:
        backend = _make_backend()
        backend._dead_reason = "gone"
        with pytest.raises(BackendGone, match="gone"):
            await backend.prime_initialize({"method": "initialize", "id": 1})

    @pytest.mark.asyncio
    async def test_failed_backend_rejected(self) -> None:
        backend = _make_backend()
        backend._init_state = "failed"
        with pytest.raises(BackendGone):
            await backend.prime_initialize({"method": "initialize", "id": 1})

    @pytest.mark.asyncio
    async def test_replays_captured_initialize_without_stub_delivery(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")

        async def _resolve() -> None:
            await asyncio.sleep(0)
            await backend._on_upstream_initialize({"result": {"capabilities": {}}})

        waiter = asyncio.create_task(_resolve())
        await backend.prime_initialize(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"_meta": {CALLER_META_KEY: {"sessionKey": "forged"}}}},
            timeout=5,
        )
        await waiter
        assert backend._init_state == "ready"
        # The captured frame's forged identity was stripped before the replay.
        replay = _frames(backend)[0]
        assert "_meta" not in replay["params"]
        # No stub ever saw a synthetic initialize reply.
        assert inbox.empty()

    @pytest.mark.asyncio
    async def test_concurrent_primer_waits_on_shared_event(self) -> None:
        backend = _make_backend()
        backend._init_state = "in_flight"
        backend._init_done_event.set()
        backend._init_state = "ready"
        await backend.prime_initialize({"method": "initialize", "id": 1}, timeout=5)
        # Second primer never wrote upstream.
        assert _frames(backend) == []

    @pytest.mark.asyncio
    async def test_handshake_that_never_resolves_reaps_the_backend(self) -> None:
        """timeout=0 makes asyncio.wait_for fail synchronously — no wall clock."""
        backend = _make_backend()
        with pytest.raises(BackendGone, match="initialize timed out on respawn"):
            await backend.prime_initialize({"method": "initialize", "id": 1}, timeout=0)
        assert backend._init_state == "failed"
        assert backend._init_done_event.is_set()
        cast(Any, backend.stdin).close.assert_called()

    @pytest.mark.asyncio
    async def test_resolved_but_not_ready_raises(self) -> None:
        backend = _make_backend()
        backend._init_state = "in_flight"
        backend._init_done_event.set()
        with pytest.raises(BackendGone, match="initialize failed on respawn"):
            await backend.prime_initialize({"method": "initialize", "id": 1}, timeout=5)

    @pytest.mark.asyncio
    async def test_write_failure_during_prime(self) -> None:
        backend = _make_backend()
        cast(Any, backend.stdin).write.side_effect = BrokenPipeError("epipe")
        with pytest.raises(BackendGone, match="stdin closed"):
            await backend.prime_initialize({"method": "initialize", "id": 1})


# --- Terminal broadcast -----------------------------------------------------


class TestBroadcastBackendGone:
    @pytest.mark.asyncio
    async def test_each_pending_request_gets_its_own_error(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        backend._pending_requests.update({
            "gw-1": _PendingRequest("s1", 11, "tools/call"),
            "gw-2": _PendingRequest("s2", 22, "tools/list"),
            "gw-3": _PendingRequest("ghost", 33, "tools/list"),
        })
        await backend._broadcast_backend_gone("stdout EOF")
        assert (await _drain(inbox1))["id"] == 11
        assert (await _drain(inbox2))["id"] == 22
        assert backend._pending_requests == {}

    @pytest.mark.asyncio
    async def test_init_waiters_each_get_their_own_id(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("__init__", None, "initialize")
        backend._init_pending = [("s1", 5), ("ghost", 6)]
        await backend._broadcast_backend_gone("crash")
        err = await _drain(inbox)
        assert err["id"] == 5 and "backend gone: crash" in err["error"]["message"]
        assert backend._init_pending == []

    @pytest.mark.asyncio
    async def test_in_flight_init_is_failed_and_waiters_woken(self) -> None:
        backend = _make_backend()
        backend._init_state = "in_flight"
        await backend._broadcast_backend_gone("crash")
        assert backend._init_state == "failed"
        assert backend._init_done_event.is_set()
        assert backend.dead_reason == "crash"

    @pytest.mark.asyncio
    async def test_parked_apps_future_is_failed_fast(self) -> None:
        backend = _make_backend()
        fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
        backend._pending_requests["gw-1"] = _PendingRequest(
            backend_mod._APPS_STUB_SENTINEL, None, "resources/read", apps_future=fut,
        )
        await backend._broadcast_backend_gone("crash")
        with pytest.raises(BackendGone):
            await fut

    @pytest.mark.asyncio
    async def test_already_resolved_apps_future_is_left_alone(self) -> None:
        backend = _make_backend()
        fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
        fut.set_result({"already": "done"})
        backend._pending_requests["gw-1"] = _PendingRequest(
            backend_mod._APPS_STUB_SENTINEL, None, "resources/read", apps_future=fut,
        )
        await backend._broadcast_backend_gone("crash")
        assert await fut == {"already": "done"}

    @pytest.mark.asyncio
    async def test_second_broadcast_is_a_noop(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend._broadcast_backend_gone("first")
        backend._pending_requests["gw-2"] = _PendingRequest("s1", 2, "tools/call")
        await backend._broadcast_backend_gone("second")
        assert inbox.qsize() == 1
        # The second call left the re-added pending entry alone.
        assert list(backend._pending_requests) == ["gw-2"]


# --- Inbox delivery ---------------------------------------------------------


class TestStubDelivery:
    @pytest.mark.asyncio
    async def test_full_inbox_drops_the_slow_stub(self) -> None:
        backend = _make_backend()
        inbox: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=1)
        async with backend._inbox_lock:
            backend._stub_inboxes["slow"] = inbox
            backend.refcount = 1
        assert await backend._enqueue_to_stub("slow", inbox, b"a\n") is True
        assert await backend._enqueue_to_stub("slow", inbox, b"b\n") is False
        assert backend.refcount == 0
        assert "slow" not in backend._stub_inboxes

    @pytest.mark.asyncio
    async def test_deliver_to_detached_stub_is_dropped(self) -> None:
        backend = _make_backend()
        await backend._deliver_to_stub("ghost", {"id": 1})  # must not raise

    @pytest.mark.asyncio
    async def test_broadcast_reaches_every_stub(self) -> None:
        backend = _make_backend()
        inboxes = [await backend.attach_stub(f"s{i}") for i in range(3)]
        await backend._broadcast({"method": "notifications/tools/list_changed"})
        for inbox in inboxes:
            assert (await _drain(inbox))["method"] == "notifications/tools/list_changed"


class TestNotificationOwner:
    def test_non_dict_params(self) -> None:
        backend = _make_backend()
        assert backend._notification_owner({"method": "x"}) is None
        assert backend._notification_owner({"params": "bad"}) is None

    def test_unique_progress_token_routes(self) -> None:
        backend = _make_backend()
        backend._pending_requests["gw-1"] = _PendingRequest(
            "s1", 1, "tools/call", progress_token="pt")
        assert backend._notification_owner(
            {"params": {"progressToken": "pt"}}) == "s1"

    def test_colliding_progress_token_is_unattributable(self) -> None:
        backend = _make_backend()
        backend._pending_requests.update({
            "gw-1": _PendingRequest("s1", 1, "tools/call", progress_token="pt"),
            "gw-2": _PendingRequest("s2", 2, "tools/call", progress_token="pt"),
        })
        assert backend._notification_owner({"params": {"progressToken": "pt"}}) is None

    def test_related_request_id_routes(self) -> None:
        backend = _make_backend()
        backend._pending_requests["gw-9"] = _PendingRequest("s3", 1, "tools/call")
        assert backend._notification_owner(
            {"params": {"_meta": {"relatedRequestId": "gw-9"}}}) == "s3"

    def test_init_sentinel_is_never_an_owner(self) -> None:
        backend = _make_backend()
        backend._pending_requests["gw-9"] = _PendingRequest("__init__", None, "initialize")
        assert backend._notification_owner(
            {"params": {"_meta": {"relatedRequestId": "gw-9"}}}) is None

    def test_unknown_related_request_id(self) -> None:
        backend = _make_backend()
        assert backend._notification_owner(
            {"params": {"_meta": {"relatedRequestId": "nope"}}}) is None
        assert backend._notification_owner({"params": {"_meta": "bad"}}) is None
        # A well-formed _meta that simply carries no routing token.
        assert backend._notification_owner({"params": {"_meta": {"other": 1}}}) is None


# --- _route_backend_line ----------------------------------------------------


class TestRouteBackendLine:
    @pytest.mark.asyncio
    async def test_non_json_and_non_object_lines_dropped(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(b"not json at all\n")
        await backend._route_backend_line(b"\xff\xfe binary\n")
        await backend._route_backend_line(b"[1,2,3]\n")
        assert inbox.empty()

    @pytest.mark.asyncio
    async def test_heartbeat_pong_is_swallowed(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._last_ping_response_mono = 0.0
        await backend._route_backend_line(_line({"id": HEARTBEAT_PING_ID, "result": {}}))
        assert inbox.empty()
        assert backend._last_ping_response_mono > 0.0
        # Stringified form too.
        backend._last_ping_response_mono = 0.0
        await backend._route_backend_line(
            _line({"id": str(HEARTBEAT_PING_ID), "error": {"code": -1}}))
        assert backend._last_ping_response_mono > 0.0

    @pytest.mark.asyncio
    async def test_unknown_response_id_dropped(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(_line({"id": "gw-nope", "result": {}}))
        assert inbox.empty()

    @pytest.mark.asyncio
    async def test_response_restores_original_id(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-4242-1"] = _PendingRequest("s1", 42, "tools/list")
        await backend._route_backend_line(
            _line({"jsonrpc": "2.0", "id": "gw-4242-1", "result": {"tools": []}}))
        assert await _drain(inbox) == {
            "jsonrpc": "2.0", "id": 42, "result": {"tools": []}}
        assert backend._pending_requests == {}

    @pytest.mark.asyncio
    async def test_completed_request_emits_latency_metric(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "metrics.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(path))
        backend = _make_backend()
        await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest(
            "s1", 1, "tools/call", t_start_ms=time.monotonic() * 1000.0)
        await backend._route_backend_line(_line({"id": "gw-1", "result": {}}))
        await _settle(backend)
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert record["method"] == "tools/call"
        assert record["ok"] is True
        assert record["stub"] == "s1"
        assert record["pid"] == 4242

    @pytest.mark.asyncio
    async def test_init_sentinel_response_goes_to_handshake(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("__init__", None, "initialize")
        backend._init_pending = [("s1", 1)]
        await backend._route_backend_line(
            _line({"id": "gw-1", "result": {"capabilities": {}}}))
        assert backend._init_state == "ready"
        assert (await _drain(inbox))["id"] == 1

    @pytest.mark.asyncio
    async def test_apps_sentinel_response_resolves_parked_future(self) -> None:
        backend = _make_backend()
        fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
        backend._pending_requests["gw-1"] = _PendingRequest(
            backend_mod._APPS_STUB_SENTINEL, None, "resources/read", apps_future=fut)
        await backend._route_backend_line(_line({"id": "gw-1", "result": {"contents": []}}))
        assert (await fut)["result"] == {"contents": []}

    @pytest.mark.asyncio
    async def test_resolved_apps_future_is_not_re_set(self) -> None:
        backend = _make_backend()
        fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
        fut.set_result({"first": True})
        backend._pending_requests["gw-1"] = _PendingRequest(
            backend_mod._APPS_STUB_SENTINEL, None, "resources/read", apps_future=fut)
        await backend._route_backend_line(_line({"id": "gw-1", "result": {"second": True}}))
        assert await fut == {"first": True}

    @pytest.mark.asyncio
    async def test_attributable_notification_goes_to_one_stub(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        backend._pending_requests["gw-1"] = _PendingRequest(
            "s1", 1, "tools/call", progress_token="pt")
        await backend._route_backend_line(_line({
            "method": "notifications/progress",
            "params": {"progressToken": "pt", "progress": 1},
        }))
        assert (await _drain(inbox1))["method"] == "notifications/progress"
        assert inbox2.empty()

    @pytest.mark.asyncio
    async def test_global_notification_is_broadcast(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend._route_backend_line(
            _line({"method": "notifications/tools/list_changed"}))
        assert not inbox1.empty() and not inbox2.empty()

    @pytest.mark.asyncio
    async def test_unattributable_request_scoped_notification_dropped(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend._route_backend_line(_line({
            "method": "notifications/message",
            "params": {"level": "info", "data": "tenant secret"},
        }))
        assert inbox1.empty() and inbox2.empty()

    @pytest.mark.asyncio
    async def test_server_request_routed_via_related_request_id(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        backend._pending_requests["gw-7"] = _PendingRequest("s2", 1, "tools/call")
        await backend._route_backend_line(_line({
            "id": "srv-1", "method": "sampling/createMessage",
            "params": {"_meta": {"relatedRequestId": "gw-7"}},
        }))
        assert (await _drain(inbox2))["id"] == "srv-1"
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_server_request_routed_to_single_stub(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(
            _line({"id": "srv-1", "method": "roots/list"}))
        assert (await _drain(inbox))["method"] == "roots/list"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("params", [
        "not-a-dict",
        {"_meta": "not-a-dict"},
        {"_meta": {}},
        {"_meta": {"relatedRequestId": "gw-unknown"}},
    ])
    async def test_malformed_related_request_id_falls_back_to_single_stub(
        self, params: Any
    ) -> None:
        """Every rung of the relatedRequestId lookup that cannot resolve must
        fall through to the single-stub rule rather than mis-attribute."""
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(
            _line({"id": "srv-1", "method": "roots/list", "params": params}))
        assert (await _drain(inbox))["id"] == "srv-1"

    @pytest.mark.asyncio
    async def test_unattributable_server_request_recycles_backend(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend._route_backend_line(
            _line({"id": "srv-1", "method": "elicitation/create"}))
        assert "cannot route without a cross-tenant leak" in (backend.dead_reason or "")
        assert backend._gone_broadcast is True

    @pytest.mark.asyncio
    async def test_frame_with_neither_id_nor_method_dropped(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._route_backend_line(_line({"jsonrpc": "2.0"}))
        assert inbox.empty()


# --- run_stdout_pump --------------------------------------------------------


class TestRunStdoutPump:
    @pytest.mark.asyncio
    async def test_routes_frames_then_broadcasts_on_eof(self) -> None:
        backend = _make_backend(stdout=None)
        backend.stdout = cast(Any, _reader(
            _line({"id": "gw-1", "result": {"ok": 1}}),
            _line({"method": "notifications/tools/list_changed"}),
        ))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 5, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["id"] == 5
        assert (await _drain(inbox))["method"] == "notifications/tools/list_changed"
        assert backend.dead_reason == "stdout EOF"

    @pytest.mark.asyncio
    async def test_exit_code_wins_over_generic_eof_reason(self) -> None:
        backend = _make_backend(returncode=3)
        backend.stdout = cast(Any, _reader())
        await backend.run_stdout_pump()
        assert backend.dead_reason == "exit rc=3"

    @pytest.mark.asyncio
    async def test_partial_final_line_is_reported(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(b'{"id":"gw-1","result"'))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        # The truncated frame is dropped, and the stub gets a backend-gone error.
        assert (await _drain(inbox))["error"]["code"] == -32000

    @pytest.mark.asyncio
    async def test_oversize_line_dropped_without_eating_the_next_frame(self) -> None:
        backend = _make_backend()
        oversize = b'{"id":"gw-1","result":"' + b"x" * 400 + b'"}\n'
        backend.stdout = cast(Any, _reader(
            oversize, _line({"id": "gw-2", "result": {"ok": True}}), limit=64,
        ))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests.update({
            "gw-1": _PendingRequest("s1", 1, "tools/call"),
            "gw-2": _PendingRequest("s1", 2, "tools/call"),
        })
        await backend.run_stdout_pump()
        first = await _drain(inbox)
        assert first["id"] == 1
        assert "exceeded size limit" in first["error"]["message"]
        # The frame AFTER the oversize line still arrives intact.
        assert (await _drain(inbox))["result"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_unterminated_oversize_line_at_eof_recycles(self) -> None:
        """An oversize line that never terminates: the drain loop hits EOF, the
        id cannot be recovered, so the shared backend is recycled rather than
        failing an innocent stub."""
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(b"x" * 400, limit=64))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert "unrecoverable request id" in (backend.dead_reason or "")
        err = await _drain(inbox)
        assert err["id"] == 1
        assert "unrecoverable request id" in err["error"]["message"]

    @pytest.mark.asyncio
    async def test_over_threshold_line_is_spilled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backend_mod, "RESPONSE_SPILL_THRESHOLD_BYTES", 8)
        monkeypatch.setattr(
            backend_mod, "maybe_spill_response",
            lambda line, server, threshold: _line({"id": "gw-1", "result": "spilled"}),
        )
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line({"id": "gw-1", "result": "x" * 64})))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["result"] == "spilled"

    @pytest.mark.asyncio
    async def test_spill_failure_routes_the_raw_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backend_mod, "RESPONSE_SPILL_THRESHOLD_BYTES", 8)

        def _boom(line: bytes, server: str, threshold: int) -> bytes:
            raise OSError("disk full")

        monkeypatch.setattr(backend_mod, "maybe_spill_response", _boom)
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line({"id": "gw-1", "result": "raw"})))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["result"] == "raw"

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(eof=False))
        task = asyncio.create_task(backend.run_stdout_pump())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --- _fail_oversize_request -------------------------------------------------


class TestFailOversizeRequest:
    @pytest.mark.asyncio
    async def test_complete_prefix_json_identifies_the_request(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-9"] = _PendingRequest("s1", 90, "tools/call")
        await backend._fail_oversize_request(b'{"jsonrpc":"2.0","id":"gw-9","result":{}}')
        err = await _drain(inbox)
        assert err["id"] == 90 and "exceeded size limit" in err["error"]["message"]

    @pytest.mark.asyncio
    async def test_regex_fallback_recovers_truncated_head(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-8"] = _PendingRequest("s1", 80, "tools/call")
        await backend._fail_oversize_request(
            b'{"jsonrpc":"2.0","id":"gw-8","result":{"content":"' + b"y" * 600)
        assert (await _drain(inbox))["id"] == 80

    @pytest.mark.asyncio
    async def test_brace_terminated_but_invalid_json_falls_back_to_regex(self) -> None:
        """The head looks complete (ends in ``}``) but is not valid JSON, so the
        prefix parse raises and the regex recovers the id."""
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-5"] = _PendingRequest("s1", 50, "tools/call")
        await backend._fail_oversize_request(b'{"id":"gw-5", not-valid-json}')
        assert (await _drain(inbox))["id"] == 50

    @pytest.mark.asyncio
    async def test_regex_match_that_is_not_decodable_recycles(self) -> None:
        """The regex matches a quoted id containing an invalid escape, so the
        second decode also fails and the backend is recycled."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend._fail_oversize_request(b'{"id":"\\x"}')
        assert "unrecoverable request id" in (backend.dead_reason or "")

    @pytest.mark.asyncio
    async def test_known_id_with_no_pending_entry_is_a_noop(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend._fail_oversize_request(b'{"id":"gw-unknown","result":{}}')
        assert inbox.empty()
        assert backend._gone_broadcast is False

    @pytest.mark.asyncio
    async def test_oversize_initialize_recycles_the_backend(self) -> None:
        """Failing just the one request cannot work for ``initialize``: the
        handshake could never complete. The whole backend is recycled so init
        waiters get a clean BackendGone and the done-event is woken."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        backend._init_state = "in_flight"
        backend._pending_requests["gw-7"] = _PendingRequest("__init__", None, "initialize")
        backend._init_pending = [("s1", 1)]
        await backend._fail_oversize_request(b'{"id":"gw-7","result":{}}')
        assert "oversize initialize response" in (backend.dead_reason or "")
        assert backend._init_state == "failed"
        assert backend._init_done_event.is_set()
        assert backend._init_pending == []
        assert backend._gone_broadcast is True

    @pytest.mark.asyncio
    async def test_unrecoverable_id_recycles_rather_than_guessing(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        backend._pending_requests["gw-6"] = _PendingRequest("s1", 60, "tools/call")
        await backend._fail_oversize_request(b"z" * 300)
        assert "unrecoverable request id" in (backend.dead_reason or "")
        assert backend._pending_requests == {}


# --- MCP Apps helpers -------------------------------------------------------


class TestParseUiContents:
    def test_inline_text_with_csp_and_permissions(self) -> None:
        backend = _make_backend()
        html, csp, perms = backend._parse_ui_contents([{
            "mimeType": MCP_APPS_MIME_TYPE,
            "text": "<h1>hi</h1>",
            "_meta": {"ui": {"csp": "default-src 'none'", "permissions": ["clipboard"]}},
        }])
        assert html == "<h1>hi</h1>"
        assert csp == "default-src 'none'"
        assert perms == ["clipboard"]

    def test_base64_blob_decoded(self) -> None:
        backend = _make_backend()
        blob = base64.b64encode(b"<p>b</p>").decode("ascii")
        html, csp, perms = backend._parse_ui_contents(
            [{"mimeType": MCP_APPS_MIME_TYPE, "blob": blob}])
        assert (html, csp, perms) == ("<p>b</p>", None, None)

    def test_invalid_base64_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(RuntimeError, match="invalid base64 blob"):
            backend._parse_ui_contents(
                [{"mimeType": MCP_APPS_MIME_TYPE, "blob": "!!!not-base64!!!"}])

    def test_non_object_entry_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(RuntimeError, match="is not an object"):
            backend._parse_ui_contents(["nope"])

    def test_wrong_mime_type_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(RuntimeError, match="unexpected mimeType"):
            backend._parse_ui_contents([{"mimeType": "text/plain", "text": "x"}])

    def test_neither_text_nor_blob_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(RuntimeError, match="neither text nor blob"):
            backend._parse_ui_contents([{"mimeType": MCP_APPS_MIME_TYPE}])

    def test_non_dict_meta_yields_no_csp(self) -> None:
        backend = _make_backend()
        _, csp, perms = backend._parse_ui_contents(
            [{"mimeType": MCP_APPS_MIME_TYPE, "text": "x", "_meta": {"ui": "bad"}}])
        assert csp is None and perms is None


class TestInterceptionGating:
    @pytest.mark.asyncio
    async def test_flag_off_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
        backend = _make_backend()
        backend._apps_declared_uris = {"draw": "ui://x/y.html"}
        pending = _PendingRequest("s1", 1, "tools/call", tool_name="draw")
        assert await backend._maybe_intercept_ui_result(pending, {"result": {}}) is False

    @pytest.mark.asyncio
    async def test_other_methods_and_shapes_never_intercept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        # Not a tools/call.
        assert await backend._maybe_intercept_ui_result(
            _PendingRequest("s1", 1, "resources/list"), {"result": {}}) is False
        # tools/call whose result is not an object.
        assert await backend._maybe_intercept_ui_result(
            _PendingRequest("s1", 1, "tools/call"), {"result": "text"}) is False
        # tools/call with no ui association anywhere.
        assert await backend._maybe_intercept_ui_result(
            _PendingRequest("s1", 1, "tools/call", tool_name="draw"),
            {"result": {"content": []}}) is False

    @pytest.mark.asyncio
    async def test_tools_list_harvest_records_declarations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        msg = {"result": {"tools": [{
            "name": "draw",
            "inputSchema": {"type": "object", "properties": {}},
            "_meta": {"ui": {"resourceUri": "ui://draw/app.html"}},
        }]}}
        assert await backend._maybe_intercept_ui_result(
            _PendingRequest("s1", 1, "tools/list"), msg) is False
        assert backend._apps_declared_uris == {"draw": "ui://draw/app.html"}

    @pytest.mark.asyncio
    async def test_tools_list_with_non_dict_result_leaves_map_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        backend._apps_declared_uris = {"draw": "ui://keep"}
        assert await backend._maybe_intercept_ui_result(
            _PendingRequest("s1", 1, "tools/list"), {"result": None}) is False
        assert backend._apps_declared_uris == {"draw": "ui://keep"}


class TestReadUiResource:
    @pytest.mark.asyncio
    async def test_error_reply_raises_and_clears_pending(self) -> None:
        backend = _make_backend()

        async def _answer(payload: dict[str, Any]) -> None:
            await asyncio.sleep(0)
            await backend._route_backend_line(_line(payload))

        task = asyncio.create_task(_answer(
            {"id": "gw-4242-1", "error": {"code": -1, "message": "nope"}}))
        with pytest.raises(RuntimeError, match="resources/read error"):
            await backend._read_ui_resource("ui://x/y.html")
        await task
        assert backend._pending_requests == {}
        assert _frames(backend)[0]["method"] == "resources/read"

    @pytest.mark.asyncio
    async def test_malformed_result_raises(self) -> None:
        backend = _make_backend()
        task = asyncio.create_task(_deferred_route(
            backend, {"id": "gw-4242-1", "result": "text"}))
        with pytest.raises(RuntimeError, match="malformed result"):
            await backend._read_ui_resource("ui://x/y.html")
        await task

    @pytest.mark.asyncio
    async def test_empty_contents_raises(self) -> None:
        backend = _make_backend()
        task = asyncio.create_task(_deferred_route(
            backend, {"id": "gw-4242-1", "result": {"contents": []}}))
        with pytest.raises(RuntimeError, match="no contents"):
            await backend._read_ui_resource("ui://x/y.html")
        await task

    @pytest.mark.asyncio
    async def test_contents_returned_on_success(self) -> None:
        backend = _make_backend()
        entry = {"mimeType": MCP_APPS_MIME_TYPE, "text": "<b>ok</b>"}
        task = asyncio.create_task(_deferred_route(
            backend, {"id": "gw-4242-1", "result": {"contents": [entry]}}))
        assert await backend._read_ui_resource("ui://x/y.html") == [entry]
        await task


async def _deferred_route(backend: Backend, payload: dict[str, Any]) -> None:
    await asyncio.sleep(0)
    await backend._route_backend_line(_line(payload))


# --- Cancellation / recycle / shutdown -------------------------------------


class TestCancelInFlight:
    @pytest.mark.asyncio
    async def test_sends_one_cancel_per_in_flight_request(self) -> None:
        backend = _make_backend()
        backend._pending_requests.update({
            "gw-1": _PendingRequest("s1", 1, "tools/call"),
            "gw-2": _PendingRequest("s1", 2, "tools/call"),
            "gw-3": _PendingRequest("s2", 3, "tools/call"),
        })
        assert await backend.cancel_in_flight_for_stub("s1") == ["gw-1", "gw-2"]
        frames = _frames(backend)
        assert [f["params"]["requestId"] for f in frames] == ["gw-1", "gw-2"]
        assert all(f["method"] == "notifications/cancelled" for f in frames)

    @pytest.mark.asyncio
    async def test_no_in_flight_work_writes_nothing(self) -> None:
        backend = _make_backend()
        assert await backend.cancel_in_flight_for_stub("s1") == []
        assert _frames(backend) == []

    @pytest.mark.asyncio
    async def test_dead_backend_short_circuits(self) -> None:
        backend = _make_backend()
        backend._dead_reason = "gone"
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        assert await backend.cancel_in_flight_for_stub("s1") == []

    @pytest.mark.asyncio
    async def test_broken_pipe_stops_sending(self) -> None:
        backend = _make_backend()
        cast(Any, backend.stdin).write.side_effect = BrokenPipeError("epipe")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        assert await backend.cancel_in_flight_for_stub("s1") == []

    @pytest.mark.asyncio
    async def test_many_cancels_are_summarised_in_the_log(self) -> None:
        backend = _make_backend()
        for i in range(7):
            backend._pending_requests[f"gw-{i}"] = _PendingRequest("s1", i, "tools/call")
        assert len(await backend.cancel_in_flight_for_stub("s1")) == 7


class TestRecycleIfIdle:
    @pytest.fixture(autouse=True)
    def _no_real_signals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backend_mod, "SecurityEventLog", MagicMock())
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_process_tree_async",
            AsyncMock(return_value=True))
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_pid_async", AsyncMock(return_value=True))

    @pytest.mark.asyncio
    async def test_co_tenants_present_quarantines_instead(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        assert await backend.recycle_if_idle() is False
        assert backend.quarantined is True
        assert backend.is_alive is True

    @pytest.mark.asyncio
    async def test_idle_backend_is_killed_and_audited(self) -> None:
        backend = _make_backend()
        assert await backend.recycle_if_idle() is True
        assert "recycled after last stub detached" in (backend.dead_reason or "")
        cast(Any, backend_mod.platform_compat.kill_process_tree_async).assert_awaited_once()
        cast(Any, backend_mod.SecurityEventLog).return_value.log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_failure_never_breaks_recycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sel = MagicMock()
        sel.return_value.log_api_access.side_effect = RuntimeError("sel down")
        monkeypatch.setattr(backend_mod, "SecurityEventLog", sel)
        backend = _make_backend()
        assert await backend.recycle_if_idle() is True

    @pytest.mark.asyncio
    async def test_refused_pid_reports_not_recycled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_process_tree_async",
            AsyncMock(side_effect=ValueError("refused pid")))
        backend = _make_backend()
        assert await backend.recycle_if_idle() is False
        assert backend.dead_reason is None

    @pytest.mark.asyncio
    async def test_tree_kill_failure_falls_back_to_pid_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_process_tree_async",
            AsyncMock(side_effect=ProcessLookupError()))
        pid_kill = AsyncMock(return_value=True)
        monkeypatch.setattr(backend_mod.platform_compat, "kill_pid_async", pid_kill)
        backend = _make_backend()
        assert await backend.recycle_if_idle() is True
        pid_kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_already_dead_backend_is_not_re_killed(self) -> None:
        backend = _make_backend(returncode=0)
        assert await backend.recycle_if_idle() is False
        cast(Any, backend_mod.platform_compat.kill_process_tree_async).assert_not_awaited()


class TestBackgroundTasksAndShutdown:
    @pytest.mark.asyncio
    async def test_cancel_background_tasks_clears_both_pumps(self) -> None:
        backend = _make_backend()
        backend._stdout_task = asyncio.create_task(asyncio.sleep(3600))
        backend._stderr_task = asyncio.create_task(asyncio.sleep(3600))
        await backend._cancel_background_tasks()
        assert backend._stdout_task is None and backend._stderr_task is None

    @pytest.mark.asyncio
    async def test_shutdown_of_exited_process_is_a_fast_noop(self) -> None:
        backend = _make_backend(returncode=0)
        await backend.shutdown()
        cast(Any, backend.stdin).close.assert_not_called()

    @pytest.mark.asyncio
    async def test_graceful_shutdown_closes_stdin_and_records_reason(self) -> None:
        backend = _make_backend()
        cast(Any, backend.process).wait = AsyncMock(return_value=0)
        await backend.shutdown(timeout=5)
        cast(Any, backend.stdin).close.assert_called_once()
        assert backend.dead_reason is not None
        assert backend.dead_reason.startswith("shutdown rc=")

    @pytest.mark.asyncio
    async def test_shutdown_preserves_an_existing_dead_reason(self) -> None:
        backend = _make_backend()
        backend._dead_reason = "wedged: recycled earlier"
        await backend.shutdown(timeout=5)
        assert backend.dead_reason == "wedged: recycled earlier"

    @pytest.mark.asyncio
    async def test_stdin_close_error_is_swallowed(self) -> None:
        backend = _make_backend()
        cast(Any, backend.stdin).close.side_effect = RuntimeError("already closed")
        await backend.shutdown(timeout=5)
        assert backend.dead_reason is not None

    @pytest.mark.asyncio
    async def test_timeout_escalates_to_tree_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree_kill = AsyncMock(return_value=True)
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_process_tree_async", tree_kill)
        backend = _make_backend()
        # timeout=0 makes the first wait_for fail synchronously.
        await backend.shutdown(timeout=0)
        tree_kill.assert_awaited_once()
        cast(Any, backend.process).kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_tree_kill_failure_falls_back_to_process_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_process_tree_async",
            AsyncMock(side_effect=OSError("no perm")))
        backend = _make_backend()
        await backend.shutdown(timeout=0)
        cast(Any, backend.process).kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_kill_of_vanished_pid_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_process_tree_async",
            AsyncMock(side_effect=OSError("no perm")))
        backend = _make_backend()
        cast(Any, backend.process).kill.side_effect = ProcessLookupError()
        await backend.shutdown(timeout=0)
        assert backend.dead_reason is not None


# --- Heartbeat edges not covered by the wedge suite ------------------------


class TestHeartbeatEdges:
    @pytest.mark.asyncio
    async def test_reaped_process_is_gone_with_a_synthesized_reason(self) -> None:
        backend = _make_backend(returncode=7)
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        assert await backend._heartbeat_once(time.monotonic()) == "gone"
        assert backend.dead_reason == "process exited rc=7"
        assert (await _drain(inbox))["id"] == 1

    @pytest.mark.asyncio
    async def test_oldest_pending_scan_keeps_the_first_seen_maximum(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        now = time.monotonic()
        backend._pending_requests.update({
            "gw-old": _PendingRequest("s1", 1, "tools/call",
                                      t_start_ms=(now - 30.0) * 1000.0),
            "gw-new": _PendingRequest("s1", 2, "tools/call", t_start_ms=now * 1000.0),
        })
        # Neither is old enough to be wedged, so this stays "alive" — the point
        # is that the younger entry does not displace the older one.
        assert await backend._heartbeat_once(now) == "alive"
        assert backend._warned_slow_ids == set()

    @pytest.mark.asyncio
    async def test_idle_backend_is_left_to_the_idle_sweep(self) -> None:
        backend = _make_backend()
        assert await backend._heartbeat_once(time.monotonic()) == "idle"
        assert _frames(backend) == []

    @pytest.mark.asyncio
    async def test_alive_backend_is_pinged_under_the_reserved_id(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        assert await backend._heartbeat_once(time.monotonic()) == "alive"
        (frame,) = _frames(backend)
        assert frame == {"jsonrpc": "2.0", "id": HEARTBEAT_PING_ID, "method": "ping"}

    @pytest.mark.asyncio
    async def test_ping_write_failure_is_a_liveness_failure(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest(
            "s1", 1, "tools/call", t_start_ms=time.monotonic() * 1000.0)
        cast(Any, backend.stdin).write.side_effect = ConnectionResetError("reset")
        assert await backend._heartbeat_once(time.monotonic()) == "gone"
        assert "heartbeat ping write failed" in (backend.dead_reason or "")
        assert (await _drain(inbox))["error"]["code"] == -32000

    @pytest.mark.asyncio
    async def test_warned_slow_ids_pruned_when_requests_complete(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        backend._warned_slow_ids = {"gw-stale"}
        assert await backend._heartbeat_once(time.monotonic()) == "alive"
        assert backend._warned_slow_ids == set()


# --- spawn_backend / send_initialize ---------------------------------------


class _FakeProcess:
    """Stand-in for asyncio.subprocess.Process with in-memory pipes."""

    def __init__(
        self,
        *,
        with_pipes: bool = True,
        with_stderr: bool = True,
        stderr_lines: bytes = b"",
    ) -> None:
        self.pid = 5150
        self.returncode: Optional[int] = None
        self.stdin = MagicMock() if with_pipes else None
        if self.stdin is not None:
            self.stdin.drain = AsyncMock()
        self.stdout = asyncio.StreamReader() if with_pipes else None
        self.stderr: Optional[asyncio.StreamReader] = None
        if with_stderr:
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(stderr_lines)
            self.stderr.feed_eof()
        self.killed = False

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def fake_spawn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the real subprocess spawn with an in-memory fake."""
    captured: dict[str, Any] = {}

    async def _fake_exec(program: str, *args: str, **kwargs: Any) -> _FakeProcess:
        captured["program"] = program
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        proc = _FakeProcess(
            with_pipes=captured.get("with_pipes", True),
            with_stderr=captured.get("with_stderr", True),
            stderr_lines=captured.get("stderr_lines", b""),
        )
        captured["process"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return captured


class TestSpawnBackend:
    @pytest.mark.asyncio
    async def test_spawn_marks_env_and_wires_pipes(self, fake_spawn: dict[str, Any]) -> None:
        fake_spawn["stderr_lines"] = b"boot line\n"
        backend = await spawn_backend(
            _pool_key(), "/usr/bin/example-mcp", ["--stdio"],
            {"PATH": "/usr/bin"}, "/nonexistent-work-dir",
        )
        assert fake_spawn["program"] == "/usr/bin/example-mcp"
        assert fake_spawn["args"] == ["--stdio"]
        env = fake_spawn["kwargs"]["env"]
        assert env["PATH"] == "/usr/bin"
        assert env[backend_mod.KIROCREW_SPAWNED_ENV] == backend_mod.KIROCREW_SPAWNED_VALUE
        assert fake_spawn["kwargs"]["start_new_session"] is True
        assert backend.pid == 5150
        assert backend._last_ping_response_mono > 0
        assert backend._stderr_task is not None
        await backend._stderr_task

    @pytest.mark.asyncio
    async def test_no_stderr_pipe_means_no_drain_task(
        self, fake_spawn: dict[str, Any]
    ) -> None:
        fake_spawn["with_stderr"] = False
        backend = await spawn_backend(
            _pool_key(), "cmd", [], {}, "/nonexistent-work-dir")
        assert backend._stderr_task is None

    @pytest.mark.asyncio
    async def test_missing_pipes_kills_the_child_and_raises(
        self, fake_spawn: dict[str, Any]
    ) -> None:
        fake_spawn["with_pipes"] = False
        with pytest.raises(RuntimeError, match="subprocess pipes not attached"):
            await spawn_backend(_pool_key(), "cmd", [], {}, "/nonexistent-work-dir")
        assert fake_spawn["process"].killed is True


class TestSendInitialize:
    @pytest.mark.asyncio
    async def test_success_seeds_cache_and_detects_capability(self) -> None:
        backend = _make_backend()
        result: dict[str, Any] = {
            "capabilities": {"experimental": {"kirocrew.caller-identity": {}}}}
        backend.stdout = cast(Any, _reader(
            b"backend boot noise, not json\n",
            _line([1, 2, 3]),
            _line({"id": "some-other-id", "result": {}}),
            _line({"jsonrpc": "2.0", "id": backend_mod._GATEWAY_INIT_ID,
                   "result": result}),
        ))
        assert await send_initialize(backend, timeout=5) == result
        assert backend.supports_caller_identity is True
        assert backend._init_state == "ready"
        assert backend._init_result == result
        request = _frames(backend)[0]
        assert request["method"] == "initialize"
        assert request["params"]["clientInfo"]["name"] == "kirocrew-gateway"

    @pytest.mark.asyncio
    async def test_custom_client_info_is_forwarded(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line(
            {"id": backend_mod._GATEWAY_INIT_ID, "result": {"capabilities": {}}})))
        await send_initialize(backend, client_info={"name": "probe", "version": "9"},
                              timeout=5)
        assert _frames(backend)[0]["params"]["clientInfo"] == {"name": "probe",
                                                               "version": "9"}
        assert backend.supports_caller_identity is False

    @pytest.mark.asyncio
    async def test_refuses_to_race_a_running_stdout_pump(self) -> None:
        backend = _make_backend()
        task = asyncio.create_task(asyncio.sleep(3600))
        backend._stdout_task = task
        try:
            with pytest.raises(RuntimeError, match="must run before the stdout pump"):
                await send_initialize(backend)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_error_response_raises_value_error(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line(
            {"id": backend_mod._GATEWAY_INIT_ID, "error": {"code": -1}})))
        with pytest.raises(ValueError, match="returned initialize error"):
            await send_initialize(backend, timeout=5)

    @pytest.mark.asyncio
    async def test_non_dict_result_raises_value_error(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line(
            {"id": backend_mod._GATEWAY_INIT_ID, "result": "nope"})))
        with pytest.raises(ValueError, match="missing/non-dict result"):
            await send_initialize(backend, timeout=5)

    @pytest.mark.asyncio
    async def test_eof_before_response_raises_value_error(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(b'{"partial"'))
        with pytest.raises(ValueError, match="closed stdout before initialize response"):
            await send_initialize(backend, timeout=5)


# --- Low-level helpers ------------------------------------------------------


class TestWriteJsonLine:
    @pytest.mark.asyncio
    async def test_serialises_one_newline_terminated_frame(self) -> None:
        writer = MagicMock()
        writer.drain = AsyncMock()
        await _write_json_line(cast(Any, writer), {"a": 1})
        assert writer.write.call_args.args[0] == b'{"a":1}\n'
        writer.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_per_backend_lock_is_used_when_present(self) -> None:
        backend = _make_backend()
        lock = getattr(backend.stdin, "_mc_write_lock")
        assert isinstance(lock, asyncio.Lock)
        await _write_json_line(backend.stdin, {"a": 1})
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_drain_timeout_surfaces_as_broken_pipe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backend_mod, "_WRITE_DRAIN_TIMEOUT_SECS", 0)
        writer = MagicMock()

        async def _slow_drain() -> None:
            await asyncio.sleep(3600)

        writer.drain = _slow_drain
        with pytest.raises(BrokenPipeError, match="drain timed out"):
            await _write_json_line(cast(Any, writer), {"a": 1})


class TestPumpStderr:
    @pytest.mark.asyncio
    async def test_drains_until_eof(self) -> None:
        reader = _reader(b"line one\nline two\n")
        await _pump_stderr(reader, "kirocrew:example-mcp")
        assert reader.at_eof()

    @pytest.mark.asyncio
    async def test_oversize_line_skipped_without_wedging(self) -> None:
        reader = _reader(b"x" * 400 + b"\n" + b"short\n", limit=32)
        await _pump_stderr(reader, "kirocrew:example-mcp")
        assert reader.at_eof()

    @pytest.mark.asyncio
    async def test_reader_error_ends_the_pump(self) -> None:
        reader = MagicMock()
        reader.readline = AsyncMock(side_effect=RuntimeError("closed"))
        await _pump_stderr(cast(Any, reader), "label")


class TestCallMetrics:
    @pytest.mark.asyncio
    async def test_disabled_metrics_write_nothing(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)
        await backend_mod._emit_call_metric({"method": "tools/call"})
        assert list(tmp_path.iterdir()) == []
        backend = _make_backend()
        backend._spawn_metric_task({"method": "tools/call"})
        assert backend._metric_tasks == set()

    @pytest.mark.asyncio
    async def test_enabled_metrics_append_one_json_line_per_call(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "calls.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(path))
        await backend_mod._emit_call_metric({"method": "tools/call", "dur_ms": 1.5})
        await backend_mod._emit_call_metric({"method": "tools/list", "dur_ms": 2.5})
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["method"] for line in lines] == [
            "tools/call", "tools/list"]

    def test_unwritable_path_is_silently_dropped(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A directory can never be opened for append -> OSError -> swallowed.
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(tmp_path))
        backend_mod._write_metric_line({"method": "tools/call"})

    def test_write_metric_line_returns_early_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)
        backend_mod._write_metric_line({"method": "tools/call"})

    @pytest.mark.asyncio
    async def test_spawn_metric_task_is_tracked_then_discarded(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "calls.jsonl"
        monkeypatch.setattr(backend_mod, "_METRICS_PATH", str(path))
        backend = _make_backend()
        backend._spawn_metric_task({"method": "ping"})
        assert len(backend._metric_tasks) == 1
        await _settle(backend)
        assert backend._metric_tasks == set()
        assert json.loads(path.read_text(encoding="utf-8").strip())["method"] == "ping"
