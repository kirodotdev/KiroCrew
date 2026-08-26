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
import logging
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


async def _settle_lease(backend: Backend, *, error: Optional[dict] = None) -> None:
    """Answer the newest forwarded subscribe/unsubscribe with the server's
    verdict (success by default), driving the lease transition its response
    confirms or refuses."""
    fid = next(
        f for f, p in reversed(list(backend._pending_requests.items()))
        if p.resource_uri
    )
    msg: dict[str, Any] = {"id": fid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = {}
    await backend._route_backend_line(_line(msg))


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


class TestFirstHandshakeDeadline:
    """The timer that closes an in-flight window a backend never resolves.

    A backend that stays alive but never answers ``initialize`` is invisible to
    every death-driven recovery path, so the deadline is the only thing that
    turns that silence into a terminal state and reclaims the process.
    """

    @staticmethod
    def _init(id_: Any = 1) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": id_, "method": "initialize",
                "params": {"capabilities": {}}}

    @staticmethod
    async def _expire_now(backend: Backend) -> None:
        """Close the in-flight window without a wall-clock wait.

        The zero-timeout coroutine is installed AS the stored task, so a disarm
        reached from inside it sees the timer's own identity -- the property
        ``test_disarm_from_inside_the_timer_does_not_self_cancel`` pins.
        """
        backend._cancel_init_deadline()
        backend._init_deadline_task = asyncio.create_task(backend._init_deadline(0))
        await backend._init_deadline_task

    @pytest.mark.asyncio
    async def test_respawn_priming_shares_the_one_handshake_bound(self) -> None:
        # Two paths drive the same handshake, so they must expire together. A
        # literal default here is how they silently came to differ.
        import inspect
        default = inspect.signature(Backend.prime_initialize).parameters["timeout"].default
        assert default == backend_mod._DEFAULT_INITIALIZE_TIMEOUT_SECS

    @pytest.mark.asyncio
    async def test_first_handshake_arms_the_deadline(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", self._init(1))
        task = backend._init_deadline_task
        assert task is not None and not task.done()
        backend._cancel_init_deadline()

    @pytest.mark.asyncio
    async def test_write_failure_does_not_arm(self) -> None:
        # No handshake ever started, so there is no window for a timer to close.
        backend = _make_backend()
        cast(Any, backend.stdin).write.side_effect = ConnectionResetError("reset")
        with pytest.raises(BackendGone):
            await backend.forward_from_stub("s1", self._init(1))
        assert backend._init_deadline_task is None

    @pytest.mark.asyncio
    async def test_arming_twice_keeps_one_timer(self) -> None:
        # A second arm must not replace the task: the displaced one would never
        # be cancelled and would outlive the window it was meant to close.
        backend = _make_backend()
        backend._init_state = "in_flight"
        backend._arm_init_deadline()
        first = backend._init_deadline_task
        backend._arm_init_deadline()
        assert backend._init_deadline_task is first
        backend._cancel_init_deadline()

    @pytest.mark.asyncio
    async def test_queued_stubs_do_not_extend_the_deadline(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", self._init(1))
        armed = backend._init_deadline_task
        await backend.forward_from_stub("s2", self._init(2))
        assert backend._init_deadline_task is armed
        backend._cancel_init_deadline()

    @pytest.mark.asyncio
    async def test_expiry_fails_init_errors_waiters_and_reaps(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", self._init(7))
        await self._expire_now(backend)
        assert backend._init_state == "failed"
        assert "initialize did not complete within 0s" in cast(str, backend._dead_reason)
        reply = await _drain(inbox)
        assert reply["id"] == 7
        assert "init failed" in reply["error"]["message"]
        # Reaped rather than left running: closing stdin is shutdown's first act.
        assert cast(Any, backend.stdin).close.called

    @pytest.mark.asyncio
    async def test_expiry_after_the_handshake_resolved_touches_nothing(self) -> None:
        # The timer may be scheduled a tick before the reply lands. A resolved
        # window is not its to close -- firing anyway would kill a healthy
        # backend every time a handshake finished near the deadline.
        backend = _make_backend()
        backend._init_state = "ready"
        await backend._init_deadline(0)
        assert backend._init_state == "ready"
        assert not cast(Any, backend.stdin).close.called
        assert backend._dead_reason is None

    @pytest.mark.asyncio
    async def test_successful_handshake_disarms_the_deadline(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", self._init(1))
        await backend._on_upstream_initialize(
            {"jsonrpc": "2.0", "id": "gw-4242-1",
             "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        )
        assert backend._init_state == "ready"
        assert backend._init_deadline_task is None

    @pytest.mark.asyncio
    async def test_failed_handshake_disarms_the_deadline(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", self._init(1))
        await backend._fail_init("upstream said no")
        assert backend._init_deadline_task is None

    @pytest.mark.asyncio
    async def test_backend_gone_disarms_the_deadline(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", self._init(1))
        await backend._broadcast_backend_gone("stdout eof")
        assert backend._init_deadline_task is None

    @pytest.mark.asyncio
    async def test_disarm_from_inside_the_timer_does_not_self_cancel(self) -> None:
        # Teardown runs inside the timer's own coroutine (the deadline calls
        # shutdown), so cancelling the current task would abort the very
        # transition being made. The expiry path must still complete.
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", self._init(1))
        await self._expire_now(backend)
        assert backend._init_state == "failed"
        assert cast(Any, backend.stdin).close.called


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
    async def test_resource_update_reaches_only_the_subscriber(self) -> None:
        """A ``notifications/resources/updated`` is attributed by URI: the stub
        whose ``resources/subscribe`` the server accepted receives it, and a
        co-pooled tenant that never subscribed receives nothing."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1  # server's own reply
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        delivered = await _drain(inbox1)
        assert delivered["method"] == "notifications/resources/updated"
        assert delivered["params"]["uri"] == "file:///watched.txt"
        assert inbox2.empty()

    @pytest.mark.asyncio
    async def test_resource_update_reaches_every_subscriber_of_the_uri(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        # s2 joined a confirmed lease, so its subscribe is answered locally.
        assert (await _drain(inbox2))["result"] == {}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///shared.txt"},
        }))
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_duplicate_subscribe_is_answered_locally_not_forwarded(self) -> None:
        """The server holds ONE subscription per URI, so only the FIRST
        subscriber's subscribe reaches it; a stub joining a confirmed lease
        receives the MCP empty result from the gateway."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 7, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 7
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 7, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 7, "result": {}}

    @pytest.mark.asyncio
    async def test_rider_parked_during_grant_gets_the_server_verdict(self) -> None:
        """A stub subscribing while the first subscribe is still in flight is
        PARKED, not told an early success: it is answered only when the
        server's verdict arrives, with that verdict."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub in ("s1", "s2"):
            await backend.forward_from_stub(stub, {
                "jsonrpc": "2.0", "id": 9, "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            })
        # Grant still in flight: the rider has NO reply yet.
        assert inbox2.empty()
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 9
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 9, "result": {}}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///shared.txt"},
        }))
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_refused_subscribe_grants_nobody(self) -> None:
        """A server refusal of the forwarded subscribe settles the forwarder
        AND every parked rider on that refusal: nobody routes, and nobody was
        told "subscribed" on the strength of a lease that never materialized
        (the refusal may be made on per-caller authorization grounds)."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub in ("s1", "s2"):
            await backend.forward_from_stub(stub, {
                "jsonrpc": "2.0", "id": 5, "method": "resources/subscribe",
                "params": {"uri": "file:///denied.txt"},
            })
        await _settle_lease(backend, error={"code": -32002, "message": "access denied"})
        refusal1 = await _drain(inbox1)
        assert refusal1["id"] == 5 and "error" in refusal1
        refusal2 = await _drain(inbox2)
        assert refusal2["id"] == 5 and refusal2["error"]["code"] == -32002
        assert backend._resource_subscriptions == {}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///denied.txt"},
        }))
        assert inbox1.empty() and inbox2.empty()

    @pytest.mark.asyncio
    async def test_update_racing_the_grant_is_dropped_not_guessed(self) -> None:
        """Routing grants land on the RESPONSE, so an update arriving while
        the grant is still in flight fails closed (dropped) rather than being
        delivered on a subscription the server has not yet accepted."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_resource_update_for_unsubscribed_uri_keeps_deny_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A URI nobody subscribed to must not be guessed at: the update is
        dropped and the unattributable-notification hazard is recorded."""
        recorded: list[str] = []

        def _capture(name: str, code: str, identity: Any) -> bool:
            recorded.append(code)
            return True

        monkeypatch.setattr(backend_mod.hazards, "record_observed", _capture)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///other.txt"},
        }))
        assert inbox1.empty() and inbox2.empty()
        assert recorded == [backend_mod.hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]

    @pytest.mark.asyncio
    async def test_resource_update_is_never_routed_by_related_request_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The URI arm is TERMINAL: a server-supplied ``_meta.relatedRequestId``
        on a resources/updated must not hand a co-tenant a URI only another
        tenant ever named. With no subscriber for the URI the update is
        dropped and the hazard recorded, even though the relatedRequestId
        resolves to a live pending request."""
        recorded: list[str] = []

        def _capture(name: str, code: str, identity: Any) -> bool:
            recorded.append(code)
            return True

        monkeypatch.setattr(backend_mod.hazards, "record_observed", _capture)
        backend = _make_backend()
        inbox_v = await backend.attach_stub("victim")
        inbox_a = await backend.attach_stub("attacker-adjacent")
        # The victim subscribes (granted), then unsubscribes (confirmed) —
        # the table is empty again.
        await backend.forward_from_stub("victim", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///victim-private.txt"},
        })
        await _settle_lease(backend)
        await backend.forward_from_stub("victim", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///victim-private.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox_v))["id"] == 1
        assert (await _drain(inbox_v))["id"] == 2
        assert backend._resource_subscriptions == {}
        # A co-tenant holds an ordinary in-flight tools/call.
        await backend.forward_from_stub("attacker-adjacent", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "t"},
        })
        fid = next(
            f for f, p in backend._pending_requests.items()
            if p.stub_uuid == "attacker-adjacent"
        )
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///victim-private.txt",
                       "_meta": {"relatedRequestId": fid}},
        }))
        assert inbox_v.empty() and inbox_a.empty()
        assert recorded == [backend_mod.hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_resource_update_delivery(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        # The last subscriber's unsubscribe releases the upstream lease …
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(upstream) == 1
        # … and routing drops the stub once the server CONFIRMS the release.
        assert backend._resource_subscriptions != {}
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        assert (await _drain(inbox1))["id"] == 1
        assert (await _drain(inbox1))["id"] == 2
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_out_of_order_release_response_keeps_replacement_grant(self) -> None:
        """A server may answer concurrent requests out of order, so a
        replacement subscribe is never forwarded BESIDE an in-flight
        release — the reverse answer order would leave routing keeping a
        subscriber whose lease the release then destroyed upstream. The
        replacement PARKS; the confirmed release drains it into a fresh
        forwarded subscribe, serialized strictly after the release, and its
        grant routes the replacement."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        # Parked, not forwarded: exactly one subscribe has gone upstream.
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/subscribe"
        ]
        assert len(upstream) == 1
        assert inbox2.empty()
        # The release confirms; the drain forwards the fresh subscribe.
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 2
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/subscribe"
        ]
        assert len(upstream) == 2
        assert inbox2.empty()  # still fail-closed until the grant
        await _settle_lease(backend)  # the fresh subscribe is granted
        assert (await _drain(inbox2))["id"] == 3
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_failed_final_unsubscribe_keeps_routing(self) -> None:
        """A refused unsubscribe means the server RETAINED the subscription:
        routing must keep the stub, or live updates are silently discarded
        while the client believes (from the error it received) that it is
        still subscribed."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend, error={"code": -32000, "message": "busy"})
        assert (await _drain(inbox1))["id"] == 1
        refusal = await _drain(inbox1)
        assert refusal["id"] == 2 and "error" in refusal
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_unsubscribe_prunes_only_the_unsubscribing_stub(self) -> None:
        """One tenant's unsubscribe must not silence a co-tenant: the routing
        entry survives AND the upstream lease is kept — the non-final
        unsubscribe is answered locally instead of being forwarded to the one
        shared server-side subscription."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        assert (await _drain(inbox2))["result"] == {}
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        # Answered locally; no unsubscribe reached the backend.
        assert (await _drain(inbox1)) == {"jsonrpc": "2.0", "id": 2, "result": {}}
        assert not [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///shared.txt"},
        }))
        assert inbox1.empty()
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_stray_unsubscribe_never_tears_down_a_co_tenants_lease(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        # s2 never subscribed; its unsubscribe must not reach the server.
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 4, "method": "resources/unsubscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        assert (await _drain(inbox2))["result"] == {}
        assert not [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///shared.txt"},
        }))
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_identity_server_gets_every_subscribe_and_grants_per_stub(self) -> None:
        """An identity-capable server authorizes per caller, so EVERY
        subscribe is forwarded (no local coalescing) and each stub routes only
        on its OWN grant — one stub's refusal neither grants it nor disturbs a
        co-tenant's accepted subscription."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub, req_id in (("s1", 1), ("s2", 2)):
            await backend.forward_from_stub(stub, {
                "jsonrpc": "2.0", "id": req_id, "method": "resources/subscribe",
                "params": {"uri": "file:///acl.txt"},
            })
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 2
        fids = {p.stub_uuid: f for f, p in backend._pending_requests.items()}
        await backend._route_backend_line(_line({"id": fids["s1"], "result": {}}))
        await backend._route_backend_line(_line({
            "id": fids["s2"], "error": {"code": -32002, "message": "denied"},
        }))
        assert (await _drain(inbox1))["id"] == 1
        refusal = await _drain(inbox2)
        assert refusal["id"] == 2 and "error" in refusal
        assert backend._resource_subscriptions == {"file:///acl.txt": {"s1"}}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///acl.txt"},
        }))
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"
        assert inbox2.empty()

    @pytest.mark.asyncio
    async def test_stub_detach_drops_its_subscriptions(self) -> None:
        """A departed stub's routing entries go with it, and — as the URI's
        last subscriber — the upstream subscription is released so the server
        does not keep firing updates nobody will receive."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await backend.detach_stub("s1")
        assert backend._resource_subscriptions == {}
        release = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(release) == 1
        assert release[0]["params"] == {"uri": "file:///watched.txt"}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert inbox2.empty()

    @pytest.mark.asyncio
    async def test_detach_keeps_upstream_lease_while_a_subscriber_remains(self) -> None:
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        assert (await _drain(inbox2))["result"] == {}
        await backend.detach_stub("s1")
        assert not [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///shared.txt"},
        }))
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_detach_of_inflight_forwarder_promotes_the_parked_rider(self) -> None:
        """When the stub whose subscribe is awaiting the server detaches, the
        first parked rider inherits the pending response: the grant settles
        the rider (with the server's verdict under the rider's id) instead of
        being dropped as unroutable and wedging the lease."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub, req_id in (("s1", 1), ("s2", 9)):
            await backend.forward_from_stub(stub, {
                "jsonrpc": "2.0", "id": req_id, "method": "resources/subscribe",
                "params": {"uri": "file:///shared.txt"},
            })
        await backend.detach_stub("s1")
        await _settle_lease(backend)
        verdict = await _drain(inbox2)
        assert verdict["id"] == 9 and verdict["result"] == {}
        assert backend._resource_subscriptions == {"file:///shared.txt": {"s2"}}

    @pytest.mark.asyncio
    async def test_orphaned_grant_is_released_not_leaked(self) -> None:
        """A grant whose forwarder detached with no rider is a lease nobody
        wants: on the server's success it is released on the spot rather than
        left firing updates into the deny-by-default drop."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.detach_stub("s1")
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        release = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(release) == 1

    @pytest.mark.asyncio
    async def test_respawn_replay_restores_routing_on_grant(self) -> None:
        """Transparent respawn replays a stub's subscriptions onto the fresh
        backend: routing returns once the server grants the replayed
        subscribe, so a live subscription survives the backend swap."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        replays = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(replays) == 1
        # No grant yet: fail-closed until the server accepts.
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert inbox1.empty()
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert (await _drain(inbox1))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_rider_after_retract_settles_on_the_orphan_grant(self) -> None:
        """Subscribe, unsubscribe before the grant, then a co-tenant
        subscribes before the response: the orphaned grant's verdict settles
        the parked rider (granted and answered) instead of dropping its
        parking and leaving the subscribe hung forever."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        # The retract answers BOTH of s1's ids: the outstanding subscribe
        # with a cancellation error, the unsubscribe with success.
        cancelled = await _drain(inbox1)
        assert cancelled["id"] == 1 and "error" in cancelled
        assert (await _drain(inbox1))["id"] == 2  # local retract reply
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        assert inbox2.empty()  # parked, not answered early
        await _settle_lease(backend)
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_replay_carries_caller_identity_on_identity_server(self) -> None:
        """On an identity-capable server the respawn replay is adjudicated as
        the same caller that held the original subscription: the replayed
        subscribe carries the connection's caller block."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions(
            "s1", ["file:///acl.txt"],
            caller=CallerContext(session_key="dashboard:abc"),
        )
        replays = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(replays) == 1
        assert CALLER_META_KEY in replays[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_replay_skipped_on_identity_server_without_caller(self) -> None:
        """Without a caller to inject, a bare replay would be adjudicated as
        the connection rather than the original caller — so it is skipped
        entirely (fail-closed) instead of risking a wrong-principal grant or
        refusal."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///acl.txt"])
        assert not [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert backend._pending_requests == {}

    @pytest.mark.asyncio
    async def test_replay_grant_for_a_detached_stub_releases_the_lease(self) -> None:
        """Detach cannot see a sentinel-owned replay pending, so the response
        arm must catch the mid-replay disconnect itself: a grant for a stub
        that is no longer attached is released, never recorded against the
        dead UUID (which would pin the lease to a stub that cannot drain it)."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        await backend.detach_stub("s1")
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        release = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(release) == 1

    @pytest.mark.asyncio
    async def test_refused_orphan_release_suppresses_the_false_hazard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Final unsubscribe in flight, the stub disconnects, and the server
        REFUSES the release: the retained subscription's updates are a
        consequence of the broker's own lease handling, so they are dropped
        without recording a hazard — while an update for a genuinely unknown
        URI still records one."""
        recorded: list[str] = []

        def _capture(name: str, code: str, identity: Any) -> bool:
            recorded.append(code)
            return True

        monkeypatch.setattr(backend_mod.hazards, "record_observed", _capture)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        # A third tenant keeps refcount above the single-client threshold
        # after s1 departs, so the control hazard below is recordable.
        await backend.attach_stub("s3")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.detach_stub("s1")
        # The server refuses the (now sentinel-owned) release.
        await _settle_lease(backend, error={"code": -32000, "message": "busy"})
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert recorded == []
        # A genuinely unknown URI still records the hazard.
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///never-named.txt"},
        }))
        assert recorded == [backend_mod.hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]

    @pytest.mark.asyncio
    async def test_repeated_same_uri_subscribes_count_toward_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rider parkings are counted individually, duplicates included:
        repeating one URI's subscribe while the grant is in flight must not
        grow the rider list unboundedly under a slow server."""
        monkeypatch.setattr(backend_mod, "_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB", 3)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        for req_id in (1, 2, 3, 4):
            await backend.forward_from_stub("s1", {
                "jsonrpc": "2.0", "id": req_id, "method": "resources/subscribe",
                "params": {"uri": "file:///same.txt"},
            })
        # 1st forwarded (in flight), 2nd and 3rd parked, 4th refused at cap.
        refusal = await _drain(inbox1)
        assert refusal["id"] == 4 and "error" in refusal
        assert len(backend._lease_pending_riders["file:///same.txt"]) == 2
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1

    @pytest.mark.asyncio
    async def test_retracted_inflight_subscribe_ids_are_answered_not_hung(self) -> None:
        """Subscribe (id 1), subscribe again (id 2, parked), then unsubscribe
        (id 3) before the grant arrives: BOTH outstanding subscribe ids
        receive a cancellation error and the unsubscribe receives success —
        no id is silently dropped from the tables to hang in the client's id
        table forever."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        for req_id in (1, 2):
            await backend.forward_from_stub("s1", {
                "jsonrpc": "2.0", "id": req_id, "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        replies = {}
        while not inbox1.empty():
            reply = await _drain(inbox1)
            replies[reply["id"]] = reply
        assert set(replies) == {1, 2, 3}
        assert "error" in replies[1] and "error" in replies[2]
        assert replies[3]["result"] == {}
        # The orphaned grant settles via the sentinel: on success it is
        # released rather than granted to the departed interest.
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        release = [f for f in _frames(backend) if f.get("method") == "resources/unsubscribe"]
        assert len(release) == 1

    @pytest.mark.asyncio
    async def test_same_stub_resubscribe_during_pending_unsubscribe_is_refused(self) -> None:
        """The releasing stub and the re-subscriber share one uuid, so a late
        out-of-order unsubscribe confirmation would erase the fresh grant.
        The resubscribe is refused locally while the unsubscribe is in
        flight; once it settles, a fresh subscribe succeeds normally."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        refusal = await _drain(inbox1)
        assert refusal["id"] == 3 and "error" in refusal
        await _settle_lease(backend)  # release confirms
        assert (await _drain(inbox1))["id"] == 2
        assert backend._resource_subscriptions == {}
        # A fresh subscribe after settlement succeeds normally.
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 4, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 4
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}

    @pytest.mark.asyncio
    async def test_subscription_cap_is_refused_locally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap precedes EVERY subscribe branch — including joining a
        co-tenant's confirmed lease — and counts in-flight grants, so a stub
        can neither queue unbounded subscribes nor keep joining URIs."""
        monkeypatch.setattr(backend_mod, "_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB", 1)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///one.txt"},
        })
        # In-flight grant already counts: a second subscribe is refused.
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
            "params": {"uri": "file:///two.txt"},
        })
        refusal = await _drain(inbox1)
        assert refusal["id"] == 2 and "error" in refusal
        upstream = [f for f in _frames(backend) if f.get("method") == "resources/subscribe"]
        assert len(upstream) == 1
        # A capped stub cannot join a co-tenant's confirmed lease either.
        await _settle_lease(backend)
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///one.txt"},
        })
        assert (await _drain(inbox2))["result"] == {}
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 4, "method": "resources/subscribe",
            "params": {"uri": "file:///one.txt"},
        })
        capped = await _drain(inbox2)
        assert capped["id"] == 4 and "error" in capped

    @pytest.mark.asyncio
    async def test_full_inbox_detach_during_fanout_does_not_break_delivery(self) -> None:
        """Fan-out iterates a COPY of the target set: a full inbox detaches
        its stub mid-fan-out (mutating the table) and the surviving subscriber
        still receives the update."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        await _settle_lease(backend)
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///shared.txt"},
        })
        assert (await _drain(inbox2))["result"] == {}
        while not inbox1.full():
            inbox1.put_nowait(b"{}")
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///shared.txt"},
        }))
        assert "s1" not in backend._stub_inboxes
        assert (await _drain(inbox2))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("params", [None, "not-a-dict", {}, {"uri": 7}, {"uri": ""}])
    async def test_malformed_subscribe_params_record_nothing(self, params: Any) -> None:
        """A subscribe without a usable ``params.uri`` string leaves the table
        untouched: the backend will reject the request anyway, and a garbage
        key could never match an incoming update's URI."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "resources/subscribe"}
        if params is not None:
            msg["params"] = params
        await backend.forward_from_stub("s1", msg)
        assert backend._resource_subscriptions == {}
        assert backend._lease_awaiting_grant == set()

    @pytest.mark.asyncio
    async def test_malformed_resource_update_is_dropped_not_broadcast(self) -> None:
        """An update without a usable URI cannot be attributed and must fall to
        the deny-by-default drop even while subscriptions exist."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox1))["id"] == 1
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": 7},
        }))
        await backend._route_backend_line(
            _line({"method": "notifications/resources/updated"}))
        assert inbox1.empty()

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


# --- subscription response hardening ----------------------------------------


def _fill_inbox(inbox: "asyncio.Queue[bytes]") -> None:
    """Fill a stub's inbox to its cap so the NEXT delivery into it trips
    the slow-stub guard and detaches the stub mid-reply."""
    while True:
        try:
            inbox.put_nowait(b"x")
        except asyncio.QueueFull:
            return


class TestSubscriptionResponseHardening:
    """Response-path invariants for the lease broker: a malformed response
    is never a grant, only one final release is in flight per URI, and every
    routing / pending-owner transition is committed BEFORE a reply is
    awaited (a reply can detach its stub, which prunes the very tables a
    late commit would then re-decide)."""

    @pytest.mark.asyncio
    async def test_malformed_subscribe_response_grants_nothing(self) -> None:
        """A response with neither ``result`` nor ``error`` is not a grant:
        routing on it would start delivering updates on a verdict the server
        never settled. The forwarder still receives the raw frame; the
        parked rider is refused, not granted."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        fid = next(f for f, p in backend._pending_requests.items() if p.resource_uri)
        await backend._route_backend_line(_line({"id": fid}))  # malformed
        assert backend._resource_subscriptions == {}
        # An UNSETTLED verdict proves nothing: the subscribe may have taken,
        # so the possibly-live lease is released rather than stranded
        # upstream firing updates that get charged to the server as hazards.
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1
        assert (await _drain(inbox1))["id"] == 1  # raw frame, no grant
        refusal = await _drain(inbox2)
        assert refusal["id"] == 2 and "error" in refusal
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert inbox1.empty() and inbox2.empty()

    @pytest.mark.asyncio
    async def test_malformed_replay_response_grants_nothing(self) -> None:
        """The sentinel-owned respawn replay applies the same success gate:
        a frame with neither ``result`` nor ``error`` grants no routing —
        and the possibly-live lease is released, not stranded."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        fid = next(f for f, p in backend._pending_requests.items() if p.resource_uri)
        await backend._route_backend_line(_line({"id": fid}))  # malformed
        assert backend._resource_subscriptions == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_refused_subscribe_response_does_not_release(self) -> None:
        """A settled REFUSAL proves the server holds no lease, so no
        gateway-originated release is issued for it."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend, error={"code": -32000, "message": "no"})
        await _drain(inbox1)
        assert backend._resource_subscriptions == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert releases == []

    @pytest.mark.asyncio
    async def test_malformed_unsubscribe_response_keeps_routing(self) -> None:
        """A malformed release response is treated as a refusal: the entry
        is kept (fail closed) instead of assuming the server released."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        fid = next(f for f, p in backend._pending_requests.items() if p.resource_uri)
        await backend._route_backend_line(_line({"id": fid}))  # malformed
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        assert "file:///watched.txt" not in backend._lease_awaiting_release

    @pytest.mark.asyncio
    async def test_repeat_final_unsubscribe_parks_behind_inflight_release(
        self,
    ) -> None:
        """A second final unsubscribe while the first's release is in flight
        is PARKED, not forwarded: forwarding it would race two responses
        against one shared awaiting-release flag, and a refusal+success pair
        would leave local routing granting a lease the server has released.
        Every waiter settles on the one in-flight release's verdict."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        assert inbox1.empty()  # parked, not answered early
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(upstream) == 1
        # The one in-flight release settles as a refusal: the server kept
        # the lease, routing keeps the stub, the waiter is told the truth,
        # and the flag is down so a later retry can release cleanly.
        await _settle_lease(backend, error={"code": -32000, "message": "no"})
        # The waiter's local reply lands first; the forwarder's raw server
        # frame is delivered afterwards by the routing path.
        waiter_reply = await _drain(inbox1)
        assert waiter_reply["id"] == 3 and "error" in waiter_reply
        raw = await _drain(inbox1)
        assert raw["id"] == 2 and "error" in raw
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s1"}}
        assert "file:///watched.txt" not in backend._lease_awaiting_release
        assert backend._lease_release_waiters == {}

    @pytest.mark.asyncio
    async def test_release_waiter_settles_on_success(self) -> None:
        """A parked final unsubscribe is answered with success — and its
        stub dropped from routing — when the in-flight release confirms."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)  # release confirmed
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        raw = await _drain(inbox1)
        assert raw["id"] == 2 and "result" in raw
        assert backend._resource_subscriptions == {}
        assert backend._lease_release_waiters == {}

    @pytest.mark.asyncio
    async def test_cotenant_final_unsubscribe_survives_releasers_detach(
        self,
    ) -> None:
        """A co-tenant subscribing while another stub's final release is in
        flight parks as a replacement; the releaser detaching (its release
        becomes sentinel-owned) must not strand that parking. The settled
        release drains it into a fresh grant, and the co-tenant's own later
        final unsubscribe releases cleanly."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # final release forwarded, in flight
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # parks as a replacement behind the release
        assert backend._lease_replacement_subscribes == {
            "file:///watched.txt": [("s2", 3)]}
        await backend.detach_stub("s1")  # release becomes sentinel-owned
        assert backend._lease_replacement_subscribes == {
            "file:///watched.txt": [("s2", 3)]}  # parking survives
        await _settle_lease(backend)  # the release confirms; drain forwards
        await _settle_lease(backend)  # the fresh subscribe is granted
        assert (await _drain(inbox2))["id"] == 3
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 4, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # s2's own final release forwards normally
        await _settle_lease(backend)
        assert (await _drain(inbox2))["id"] == 4
        assert backend._resource_subscriptions == {}
        assert backend._lease_release_waiters == {}
        assert backend._lease_replacement_subscribes == {}

    @pytest.mark.asyncio
    async def test_detached_waiter_is_pruned_not_replied(self) -> None:
        """A waiter that detaches while parked expects no further reply and
        must not linger in the waiter table."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # parked
        await backend.detach_stub("s1")
        assert backend._lease_release_waiters == {}
        await _settle_lease(backend)  # sentinel-owned release settles cleanly
        assert backend._resource_subscriptions == {}

    @pytest.mark.asyncio
    async def test_sentinel_grant_commits_routing_before_rider_replies(
        self,
    ) -> None:
        """A rider whose grant reply detaches it (full inbox) must not be
        reinserted into the routing table by a commit made after the
        replies — updates would then route at a stub that is gone."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        full_inbox = await backend.attach_stub("full")
        ok_inbox = await backend.attach_stub("ok")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # retract: the in-flight grant is now sentinel-owned
        cancelled = await _drain(inbox1)
        assert cancelled["id"] == 1 and "error" in cancelled
        assert (await _drain(inbox1))["id"] == 2  # local retract success
        await backend.forward_from_stub("full", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("ok", {
            "jsonrpc": "2.0", "id": 4, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        _fill_inbox(full_inbox)
        await _settle_lease(backend)  # grant arrives under the sentinel
        assert "full" not in backend._stub_inboxes  # detached mid-reply
        assert backend._resource_subscriptions == {"file:///watched.txt": {"ok"}}
        assert (await _drain(ok_inbox))["id"] == 4
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_grant_commits_every_rider_before_replies(self) -> None:
        """The coalesced grant commits the forwarder AND every rider before
        any reply: a mid-loop detach that empties the entry would otherwise
        delete it from the table, stranding later riders in a stale set
        alias that no longer routes."""
        backend = _make_backend()
        s1_inbox = await backend.attach_stub("s1")
        s2_inbox = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # s1 rides behind its own grant
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        _fill_inbox(s1_inbox)
        await _settle_lease(backend)  # s1's rider reply detaches s1
        assert "s1" not in backend._stub_inboxes
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        assert await _drain(s2_inbox) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert (await _drain(s2_inbox))["method"] == "notifications/resources/updated"

    @pytest.mark.asyncio
    async def test_retract_commits_promotion_before_replies(self) -> None:
        """The forwarder's retract reply can detach the retracting stub
        (full inbox). The surviving rider's promotion must already be
        committed, or detach's own promotion is overwritten with the
        sentinel and that rider's subscribe hangs unanswered forever."""
        backend = _make_backend()
        s1_inbox = await backend.attach_stub("s1")
        s2_inbox = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # rider behind s1's in-flight grant
        _fill_inbox(s1_inbox)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # retract; the error reply into the full inbox detaches s1
        assert "s1" not in backend._stub_inboxes
        p = next(p for p in backend._pending_requests.values() if p.resource_uri)
        assert p.stub_uuid == "s2" and p.original_id == 2
        await _settle_lease(backend)
        granted_reply = await _drain(s2_inbox)
        assert granted_reply["id"] == 2 and "result" in granted_reply
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}

    @pytest.mark.asyncio
    async def test_orphan_drop_log_omits_the_uri(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Resource URIs can carry tokens or presigned query parameters, so
        the orphaned-lease drop line must not persist them in the log."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        secret_uri = "https://bucket/object?sig=TOPSECRET"
        backend._orphaned_leases.add(secret_uri)
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_gateway.backend"):
            await backend._route_backend_line(_line({
                "method": "notifications/resources/updated",
                "params": {"uri": secret_uri},
            }))
        assert "orphaned lease" in caplog.text
        assert "TOPSECRET" not in caplog.text

    @pytest.mark.asyncio
    async def test_replay_and_release_failure_logs_omit_the_uri(
        self, caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The replay-failure and release-failure lines must not persist
        the URI either."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        secret_uri = "https://bucket/object?sig=TOPSECRET"
        monkeypatch.setattr(
            backend_mod, "_write_json_line",
            AsyncMock(side_effect=RuntimeError("pipe gone")),
        )
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_gateway.backend"):
            with pytest.raises(backend_mod.BackendGone):
                await backend.replay_resource_subscriptions("s1", [secret_uri])
            await backend._release_upstream_subscriptions([secret_uri])
        assert "could not replay" in caplog.text
        assert "could not release" in caplog.text
        assert "TOPSECRET" not in caplog.text

    @pytest.mark.asyncio
    async def test_refused_release_joins_replacement_locally(self) -> None:
        """When the in-flight release is REFUSED the server retained the
        lease, so a parked replacement joins the still-live entry locally —
        no second upstream subscribe."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # parks as replacement
        await _settle_lease(backend, error={"code": -32000, "message": "no"})
        assert await _drain(inbox2) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        assert backend._resource_subscriptions == {
            "file:///watched.txt": {"s1", "s2"}}
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/subscribe"
        ]
        assert len(upstream) == 1
        assert backend._lease_replacement_subscribes == {}

    @pytest.mark.asyncio
    async def test_detach_never_promotes_the_departing_stubs_own_rider(
        self,
    ) -> None:
        """A departing stub's own parked duplicate must not be promoted into
        its in-flight subscribe: the grant would record a phantom subscriber
        that blocks the release and swallows updates. The rider prune runs
        before the pending scan, so the pending converts to the sentinel and
        the granted-but-unwanted lease is released."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # s1's own duplicate parks as a rider
        await backend.detach_stub("s1")
        p = next(p for p in backend._pending_requests.values() if p.resource_uri)
        assert p.stub_uuid == backend_mod._RELEASE_STUB_SENTINEL
        assert backend._lease_pending_riders == {}
        await _settle_lease(backend)  # grant lands under the sentinel
        # Nobody wants the lease: routing stays empty and it is released.
        assert backend._resource_subscriptions == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_replacement_parks_behind_a_gateway_originated_release(
        self,
    ) -> None:
        """A detach-orphaned release is a release like any other: it must
        mark the URI in-flight so a replacement subscribe parks behind it
        instead of forwarding beside it and racing its answer order (the
        round-11 guard covered only stub-forwarded releases)."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        # Last subscriber departs WITHOUT unsubscribing: the gateway
        # originates the release itself.
        await backend.detach_stub("s1")
        assert "file:///watched.txt" in backend._lease_awaiting_release
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # parks — not forwarded beside the in-flight release
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/subscribe"
        ]
        assert len(upstream) == 1
        await _settle_lease(backend)  # the gateway release confirms
        assert "file:///watched.txt" not in backend._lease_awaiting_release
        await _settle_lease(backend)  # the drained fresh subscribe is granted
        assert (await _drain(inbox2))["id"] == 2
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}

    @pytest.mark.asyncio
    async def test_concurrent_replays_coalesce_on_one_lease(self) -> None:
        """Two identity-less replays of one URI must not put two grants in
        flight: the server holds ONE subscription, so a later final
        unsubscribe would release it while the other stub stayed routed.
        The second replay rides the first's sentinel grant, and a final
        unsubscribe afterwards only leaves the table (lease intact)."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.replay_resource_subscriptions("s1", ["file:///watched.txt"])
        await backend.replay_resource_subscriptions("s2", ["file:///watched.txt"])
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/subscribe"
        ]
        assert len(upstream) == 1  # second replay rides, never forwards
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {
            "file:///watched.txt": {"s1", "s2"}}
        # s1 leaves: with a co-tenant still routed this must NOT release
        # the server's single subscription.
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        assert (await _drain(inbox1))["id"] == 1
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert releases == []

    @pytest.mark.asyncio
    async def test_replay_joins_a_held_lease_locally(self) -> None:
        """A replay for a URI another stub already holds joins the routing
        table locally — no duplicate upstream subscribe."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.replay_resource_subscriptions("s2", ["file:///watched.txt"])
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/subscribe"
        ]
        assert len(upstream) == 1
        assert backend._resource_subscriptions == {
            "file:///watched.txt": {"s1", "s2"}}

    @pytest.mark.asyncio
    async def test_replay_parks_behind_an_inflight_release(self) -> None:
        """A replay racing an in-flight release parks as a replacement and
        is granted on the fresh sentinel subscribe after the release
        settles — never forwarded beside it."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # final release in flight
        await backend.replay_resource_subscriptions("s2", ["file:///watched.txt"])
        assert backend._lease_replacement_subscribes == {
            "file:///watched.txt": [("s2", None)]}
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/subscribe"
        ]
        assert len(upstream) == 1
        await _settle_lease(backend)  # release confirms; drain forwards
        await _settle_lease(backend)  # fresh sentinel subscribe granted
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}

    @pytest.mark.asyncio
    async def test_maintenance_pendings_carry_a_start_time(self) -> None:
        """Gateway-originated release and replay pendings must carry
        ``t_start_ms``: the heartbeat wedge scan computes each pending's age
        from it, and a zero start time reads as host-uptime old — past the
        hard ceiling it recycles a healthy backend as wedged."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend._release_upstream_subscriptions(["file:///a.txt"])
        await backend.replay_resource_subscriptions("s1", ["file:///b.txt"])
        maintenance = [
            p for p in backend._pending_requests.values() if p.resource_uri
        ]
        assert len(maintenance) == 2
        assert all(p.t_start_ms > 0 for p in maintenance)

    @pytest.mark.asyncio
    async def test_idless_subscription_frames_are_swallowed(self) -> None:
        """An id-less subscribe has no response that could ever settle the
        lease transition it would start: it must neither mutate lease state
        (later subscribers would park forever) nor be forwarded (a
        server-side subscription the broker never routes). A later valid
        subscriber proceeds normally."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # id-less
        assert backend._lease_awaiting_grant == set()
        assert _frames(backend) == []
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # id-less unsubscribe swallowed too
        assert _frames(backend) == []
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        assert (await _drain(inbox2))["id"] == 1
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_release_waiters_count_toward_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated final unsubscribes under a slow server park as waiters;
        the per-stub cap bounds that list exactly as it bounds riders."""
        monkeypatch.setattr(
            backend_mod, "_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB", 2)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        for req_id in (2, 3, 4, 5):
            await backend.forward_from_stub("s1", {
                "jsonrpc": "2.0", "id": req_id,
                "method": "resources/unsubscribe",
                "params": {"uri": "file:///watched.txt"},
            })
        # id 2 forwarded (release in flight), 3 and 4 parked, 5 refused.
        refusal = await _drain(inbox1)
        assert refusal["id"] == 5 and "error" in refusal
        assert len(backend._lease_release_waiters["file:///watched.txt"]) == 2
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(upstream) == 1

    @pytest.mark.asyncio
    async def test_orphaned_leases_are_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The orphaned-lease set is bounded: past the cap an arbitrary
        entry is evicted rather than growing the set for the backend's
        lifetime (suppression is telemetry hygiene, not correctness)."""
        monkeypatch.setattr(backend_mod, "_ORPHANED_LEASES_MAX", 2)
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        backend._orphaned_leases.update({"file:///old1", "file:///old2"})
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.detach_stub("s1")  # sentinel release forwarded
        await _settle_lease(backend, error={"code": -32000, "message": "no"})
        assert "file:///watched.txt" in backend._orphaned_leases
        assert len(backend._orphaned_leases) == 2

    @pytest.mark.asyncio
    async def test_backend_death_settles_every_parked_lease_request(
        self,
    ) -> None:
        """A rider, a release waiter, and a parked replacement each hold a
        client id only a backend response would settle. When the backend
        dies terminally, each must receive the synthetic backend-gone error
        instead of hanging in the client's id table forever, and the lease
        coordination state must clear (the routing table is kept for the
        respawn replay)."""
        backend = _make_backend()
        rider_inbox = await backend.attach_stub("rider")
        waiter_inbox = await backend.attach_stub("waiter")
        repl_inbox = await backend.attach_stub("repl")
        # A rider: parks behind rider's own in-flight grant on uri A.
        await backend.forward_from_stub("rider", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///a.txt"},
        })
        await backend.forward_from_stub("rider", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
            "params": {"uri": "file:///a.txt"},
        })  # rider parking (rider, 2)
        # A waiter + a replacement: waiter holds uri B, releases it, then
        # repeats the unsubscribe (waiter parking) while repl subscribes
        # (replacement parking).
        await backend.forward_from_stub("waiter", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///b.txt"},
        })
        await _settle_lease(backend)
        await _drain(waiter_inbox)
        await backend.forward_from_stub("waiter", {
            "jsonrpc": "2.0", "id": 4, "method": "resources/unsubscribe",
            "params": {"uri": "file:///b.txt"},
        })  # final release in flight
        await backend.forward_from_stub("waiter", {
            "jsonrpc": "2.0", "id": 5, "method": "resources/unsubscribe",
            "params": {"uri": "file:///b.txt"},
        })  # waiter parking (waiter, 5)
        await backend.forward_from_stub("repl", {
            "jsonrpc": "2.0", "id": 6, "method": "resources/subscribe",
            "params": {"uri": "file:///b.txt"},
        })  # replacement parking (repl, 6)
        await backend._broadcast_backend_gone("test teardown")
        # Every parked id answered with the synthetic error.
        rider_err = await _drain(rider_inbox)
        assert rider_err["id"] in (1, 2) and "error" in rider_err
        rider_err2 = await _drain(rider_inbox)
        assert {rider_err["id"], rider_err2["id"]} == {1, 2}
        assert "error" in rider_err2
        waiter_errs = [await _drain(waiter_inbox), await _drain(waiter_inbox)]
        assert {e["id"] for e in waiter_errs} == {4, 5}
        assert all("error" in e for e in waiter_errs)
        repl_err = await _drain(repl_inbox)
        assert repl_err["id"] == 6 and "error" in repl_err
        # Coordination state cleared; routing kept for the respawn replay.
        assert backend._lease_pending_riders == {}
        assert backend._lease_release_waiters == {}
        assert backend._lease_replacement_subscribes == {}
        assert backend._lease_awaiting_grant == set()
        assert backend._lease_awaiting_release == set()
        assert backend._resource_subscriptions == {"file:///b.txt": {"waiter"}}

    @pytest.mark.asyncio
    async def test_replay_write_failure_raises_not_swallows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A replay WRITE failure must surface as BackendGone: the
        replacement's pipe is broken, and swallowing it would report a
        successful respawn whose subscriptions are silently dark forever."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        monkeypatch.setattr(
            backend_mod, "_write_json_line",
            AsyncMock(side_effect=RuntimeError("pipe gone")),
        )
        with pytest.raises(backend_mod.BackendGone):
            await backend.replay_resource_subscriptions(
                "s1", ["file:///watched.txt"])
        assert backend._lease_awaiting_grant == set()
        assert all(
            not p.resource_uri for p in backend._pending_requests.values())

    @pytest.mark.asyncio
    async def test_partial_replay_failure_scopes_earlier_pendings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the second replay write fails, the FIRST URI's already-sent
        pending is scoped to release-on-grant: the respawn is reported
        failed, so a late success must release its lease rather than pin it
        to a stub the caller is tearing down."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        real_write = backend_mod._write_json_line
        calls = {"n": 0}

        async def write_second_fails(writer: Any, obj: Any) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("pipe gone")
            await real_write(writer, obj)

        monkeypatch.setattr(backend_mod, "_write_json_line", write_second_fails)
        with pytest.raises(backend_mod.BackendGone):
            await backend.replay_resource_subscriptions(
                "s1", ["file:///a.txt", "file:///b.txt"])
        # The first URI's pending survives but is scoped: no replay stub.
        survivors = [
            p for p in backend._pending_requests.values() if p.resource_uri
        ]
        assert len(survivors) == 1
        assert not survivors[0].replay_stub
        # Its late grant releases the lease instead of granting s1.
        monkeypatch.setattr(backend_mod, "_write_json_line", real_write)
        await _settle_lease(backend)
        assert backend._resource_subscriptions == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1

    @pytest.mark.asyncio
    async def test_identity_detach_releases_as_the_grant_caller(self) -> None:
        """On an identity-capable server a departing stub's subscription is
        released AS the caller recorded at grant time — a bare unsubscribe
        would be adjudicated as the connection, leaving the departed
        caller's upstream subscription firing forever."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)
        await _settle_lease(backend)
        await _drain(inbox1)
        assert backend._grant_callers == {
            ("file:///watched.txt", "s1"): caller_a}
        await backend.detach_stub("s1")
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]
        assert backend._grant_callers == {}
        assert backend._resource_subscriptions == {}

    @pytest.mark.asyncio
    async def test_identity_detach_skips_release_for_a_shared_caller(
        self,
    ) -> None:
        """Two stubs claimed to one session hold ONE per-caller grant
        upstream: the first stub's detach must NOT release it (the survivor
        stays routed); the last sharer's detach releases it."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        caller_a = CallerContext(session_key="dashboard:aaa")
        for stub, req_id, inbox in (("s1", 1, inbox1), ("s2", 2, inbox2)):
            await backend.forward_from_stub(stub, {
                "jsonrpc": "2.0", "id": req_id,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            }, caller=caller_a)
            await _settle_lease(backend)
            await _drain(inbox)
        await backend.detach_stub("s1")
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert releases == []  # survivor still consumes the shared grant
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        await backend.detach_stub("s2")
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1  # last sharer departs: released once
        # ... and released AS the grant caller, not as a bare unsubscribe.
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_identity_unknown_grant_caller_orphans_not_bare_release(
        self,
    ) -> None:
        """A routed identity grant with no recorded caller cannot be
        released safely (a bare unsubscribe is the wrong principal): the
        lease is knowingly retained and orphan-marked so its updates never
        charge a hazard to a blameless server."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # no caller: grant recorded with no principal
        await _settle_lease(backend)
        await _drain(inbox1)
        assert backend._grant_callers == {}
        await backend.detach_stub("s1")
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert releases == []
        assert "file:///watched.txt" in backend._orphaned_leases

    @pytest.mark.asyncio
    async def test_replayed_identity_grant_records_the_replay_caller(
        self,
    ) -> None:
        """A respawn-replayed identity grant is held by the replay's
        principal: the grant caller is recorded so the stub's later detach
        releases it as that caller instead of orphaning the lease."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.replay_resource_subscriptions(
            "s1", ["file:///watched.txt"], caller=caller_a)
        await _settle_lease(backend)
        assert backend._grant_callers == {
            ("file:///watched.txt", "s1"): caller_a}
        await backend.detach_stub("s1")
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]
        assert "file:///watched.txt" not in backend._orphaned_leases

    @pytest.mark.asyncio
    async def test_malformed_identity_grant_releases_as_the_caller(
        self,
    ) -> None:
        """An identity subscribe answered with neither result nor error may
        have taken upstream: recording nothing would strand a live
        per-caller lease. The unsettled verdict releases it as the caller
        that took it."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)
        fid = next(f for f, p in backend._pending_requests.items() if p.resource_uri)
        await backend._route_backend_line(_line({"id": fid}))  # malformed
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_shared_caller_unsubscribe_settles_locally(self) -> None:
        """Two stubs sharing one per-caller grant: either stub's explicit
        unsubscribe must settle locally (drop only that stub) — forwarding
        it would destroy the survivor's upstream subscription. The last
        sharer's unsubscribe forwards and releases."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        caller_a = CallerContext(session_key="dashboard:aaa")
        for stub, req_id, inbox in (("s1", 1, inbox1), ("s2", 2, inbox2)):
            await backend.forward_from_stub(stub, {
                "jsonrpc": "2.0", "id": req_id,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            }, caller=caller_a)
            await _settle_lease(backend)
            await _drain(inbox)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 3, "result": {}}
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert upstream == []  # settled locally, survivor keeps the lease
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        # Last sharer's unsubscribe forwards normally.
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 4, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(upstream) == 1
        await _settle_lease(backend)
        assert (await _drain(inbox2))["id"] == 4
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}

    @pytest.mark.asyncio
    async def test_unsubscribe_retracts_a_parked_replacement(self) -> None:
        """A stub that parked a replacement subscribe and then unsubscribes
        must not be resurrected by the release drain: the parking is
        retracted (its subscribe id answered with a cancellation error) and
        the settled release re-subscribes nobody."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # final release in flight
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 3, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # parks as replacement
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 4, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        })  # retracts the parking
        retracted = await _drain(inbox2)
        assert retracted["id"] == 3 and "error" in retracted
        unsub_reply = await _drain(inbox2)
        assert unsub_reply["id"] == 4 and "result" in unsub_reply
        assert backend._lease_replacement_subscribes == {}
        await _settle_lease(backend)  # the release confirms
        # Nobody is re-subscribed: exactly one upstream subscribe ever.
        upstream = [
            f for f in _frames(backend)
            if f.get("method") == "resources/subscribe"
        ]
        assert len(upstream) == 1
        assert backend._resource_subscriptions == {}

    @pytest.mark.asyncio
    async def test_evict_stub_subscriptions_releases_as_grant_caller(
        self,
    ) -> None:
        """A warm-pool claim rekeys a stub to a new session: the old
        caller's grants are evicted and released AS the grant-time caller,
        so the new owner never receives the old owner's resource-update
        URIs. The stub stays attached (rekey, not detach)."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)
        await _settle_lease(backend)
        await _drain(inbox1)
        evicted = await backend.evict_stub_subscriptions("s1")
        assert evicted == 1
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        assert "s1" in backend._stub_inboxes  # attached: rekey, not detach
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]
        # Post-eviction: an update for the old URI routes to nobody.
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt"},
        }))
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_evict_stub_subscriptions_identityless_last_subscriber(
        self,
    ) -> None:
        """Identity-less regime: eviction drops the stub from routing and
        releases the shared lease only when it was the last subscriber."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        for stub, req_id, inbox in (("s1", 1, inbox1), ("s2", 2, inbox2)):
            await backend.forward_from_stub(stub, {
                "jsonrpc": "2.0", "id": req_id,
                "method": "resources/subscribe",
                "params": {"uri": "file:///watched.txt"},
            })
            if stub == "s1":
                await _settle_lease(backend)
            await _drain(inbox)
        assert await backend.evict_stub_subscriptions("s1") == 1
        assert backend._resource_subscriptions == {"file:///watched.txt": {"s2"}}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert releases == []  # co-tenant still subscribed: no release
        assert await backend.evict_stub_subscriptions("s2") == 1
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1  # last subscriber evicted: released

    @pytest.mark.asyncio
    async def test_eviction_sentinelizes_inflight_unsubscribe(self) -> None:
        """An unsubscribe sent under the OLD owner whose response arrives
        AFTER the rekey must not deliver under the old request id into the
        rekeyed stub's stream — the new owner may have already reused that
        id for its own request. Eviction sentinelizes the pending (mirror
        of ``detach_stub``): the response settles silently."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)  # release in flight at the rekey moment
        pend = [
            (fid, p) for fid, p in backend._pending_requests.items()
            if p.method == "resources/unsubscribe" and p.stub_uuid == "s1"
        ]
        assert len(pend) == 1
        fid, p = pend[0]
        assert p.original_id == 2
        await backend.evict_stub_subscriptions("s1")
        assert p.stub_uuid == backend_mod._RELEASE_STUB_SENTINEL
        assert p.original_id is None
        # The unsubscribe response settles after the rekey: nothing may
        # reach the rekeyed stub's stream under the old id.
        await backend._route_backend_line(_line({
            "jsonrpc": "2.0", "id": fid, "result": {},
        }))
        assert inbox1.empty()

    @pytest.mark.asyncio
    async def test_eviction_retracts_inflight_subscribe(self) -> None:
        """A subscribe sent under the old owner whose grant arrives AFTER
        the rekey must not route to the new owner: eviction retracts the
        pending (id answered with a cancellation error) and the late grant
        releases the lease as the old principal."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)  # grant in flight
        assert await backend.evict_stub_subscriptions("s1") == 0
        retracted = await _drain(inbox1)
        assert retracted["id"] == 1 and "error" in retracted
        await _settle_lease(backend)  # the late grant arrives
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_eviction_commits_routing_before_first_await(self) -> None:
        """The claim path reassigns the owner then awaits the eviction: any
        frame routed during those awaits must already see the stub gone
        from the tables, so every removal commits before the first reply
        or release is awaited."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///granted.txt"},
        }, caller=caller_a)
        await _settle_lease(backend)
        await _drain(inbox1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
            "params": {"uri": "file:///inflight.txt"},
        }, caller=caller_a)  # in flight: forces a retract-reply await
        seen_at_first_await: list[dict] = []
        real_reply = backend._reply_locally

        async def spying_reply(*args: Any, **kwargs: Any) -> None:
            if not seen_at_first_await:
                seen_at_first_await.append(
                    {u: set(s) for u, s in backend._resource_subscriptions.items()}
                )
            await real_reply(*args, **kwargs)

        backend._reply_locally = spying_reply  # type: ignore[method-assign]
        try:
            await backend.evict_stub_subscriptions("s1")
        finally:
            backend._reply_locally = real_reply  # type: ignore[method-assign]
        assert seen_at_first_await == [{}]  # routing already empty at await
        assert backend._grant_callers == {}

    @pytest.mark.asyncio
    async def test_eviction_retracts_inflight_replay(self) -> None:
        """A REPLAY in flight when the stub is rekeyed targets the stub by
        its ``replay_stub`` back-reference, which the subscribe retraction
        cannot see: eviction must scope it to release-on-grant, or the old
        owner's replayed URI routes to (and its grant-caller record binds
        to) the new owner."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions("s1", ["file:///old.txt"])
        assert any(
            p.replay_stub == "s1" for p in backend._pending_requests.values()
        )  # replay grant in flight
        await backend.evict_stub_subscriptions("s1")
        assert not any(
            p.replay_stub == "s1" for p in backend._pending_requests.values()
        )
        await _settle_lease(backend)  # the late replay grant arrives
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1  # released, not routed to the new owner
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///old.txt"},
        }))
        assert inbox1.empty()  # nothing delivered to the rekeyed stub

    @pytest.mark.asyncio
    async def test_respawn_capture_includes_inflight_replay_uris(self) -> None:
        """Routing commits only on the server's grant, so a replacement that
        dies before its replay responses arrive holds those URIs in neither
        the routing table nor anywhere the NEXT respawn's capture looked:
        ``resource_subscription_uris`` must include in-flight replay URIs or
        a second respawn goes permanently dark."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.replay_resource_subscriptions(
            "s1", ["file:///pending.txt"])
        # No grant yet: the routing table is empty, the replay is in flight.
        assert backend._resource_subscriptions == {}
        assert backend.resource_subscription_uris("s1") == [
            "file:///pending.txt"
        ]

    @pytest.mark.asyncio
    async def test_respawn_capture_survives_backend_death(self) -> None:
        """The backend-gone cleanup clears the pending table — previously
        erasing an in-flight replay's only record, so a replacement dying
        before its replay responses arrived left the NEXT respawn's capture
        empty and the subscription permanently dark. Death now preserves the
        replay-target URIs for the capture. A rekey-evicted replay
        (``replay_stub`` scoped to ``""``) is correctly NOT preserved."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        await backend.attach_stub("s2")
        await backend.replay_resource_subscriptions("s1", ["file:///live.txt"])
        await backend.replay_resource_subscriptions("s2", ["file:///evicted.txt"])
        await backend.evict_stub_subscriptions("s2")  # scopes s2's replay
        await backend._broadcast_backend_gone("stdout EOF")
        assert backend._pending_requests == {}
        assert backend.resource_subscription_uris("s1") == ["file:///live.txt"]
        assert backend.resource_subscription_uris("s2") == []

    @pytest.mark.asyncio
    async def test_eviction_clears_grants_without_caller_records(self) -> None:
        """On an identity server the grant arm commits routing even when the
        accepted subscribe carried no caller metadata (no ``_grant_callers``
        record). An eviction keyed on the records alone would leave that
        entry routed — the new owner would keep receiving the old owner's
        URI. Eviction must walk the routing table: the callerless grant is
        removed and orphan-marked (retained upstream, updates dropped), not
        released as the wrong principal."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        backend.supports_caller_identity = True
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///keyless.txt"},
        })  # no caller kwarg: grant will carry no _grant_callers record
        await _settle_lease(backend)
        await _drain(inbox1)
        assert backend._resource_subscriptions == {"file:///keyless.txt": {"s1"}}
        assert backend._grant_callers == {}
        evicted = await backend.evict_stub_subscriptions("s1")
        assert evicted == 1
        assert backend._resource_subscriptions == {}
        assert "file:///keyless.txt" in backend._orphaned_leases
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert releases == []  # never released as the wrong principal
        await backend._route_backend_line(_line({
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///keyless.txt"},
        }))
        assert inbox1.empty()  # nothing routed to the rekeyed stub

    @pytest.mark.asyncio
    async def test_replay_stops_when_stub_rekeyed_mid_loop(self) -> None:
        """A multi-URI replay awaits between writes; a claim landing
        mid-loop evicts the pendings written so far, but the loop would
        keep writing the REMAINING URIs under the old owner. The replay
        must notice the rekey between writes and stop."""
        backend = _make_backend()
        await backend.attach_stub("s1")
        real_write = backend_mod._write_json_line
        calls: list[str] = []

        async def rekeying_write(stdin: Any, msg: dict) -> None:
            calls.append(msg["params"]["uri"])
            await real_write(stdin, msg)
            if len(calls) == 1:
                # The claim lands while the first write's await is live.
                await backend.evict_stub_subscriptions("s1")

        backend_mod._write_json_line = rekeying_write
        try:
            await backend.replay_resource_subscriptions(
                "s1", ["file:///a.txt", "file:///b.txt", "file:///c.txt"])
        finally:
            backend_mod._write_json_line = real_write
        assert calls == ["file:///a.txt"]  # loop stopped after the rekey

    @pytest.mark.asyncio
    async def test_nonholder_unsubscribe_settles_locally(self) -> None:
        """On an identity server a stub holding NO grant for the URI must
        have its unsubscribe answered locally: the server adjudicates by
        CALLER, so forwarding a non-holder's frame would remove a
        same-caller HOLDER's live lease while the holder stayed locally
        routed — updates silently stop."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        inbox2 = await backend.attach_stub("s2")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///held.txt"},
        }, caller=caller_a)
        await _settle_lease(backend)
        await _drain(inbox1)
        # s2 (same caller) never subscribed, nothing outstanding — its
        # unsubscribe must settle locally, not forward.
        await backend.forward_from_stub("s2", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///held.txt"},
        }, caller=caller_a)
        reply = await _drain(inbox2)
        assert reply["id"] == 2 and "error" not in reply
        forwarded = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert forwarded == []  # holder's lease untouched upstream
        assert backend._resource_subscriptions == {"file:///held.txt": {"s1"}}

    @pytest.mark.asyncio
    async def test_retracted_pendings_still_count_against_the_cap(self) -> None:
        """Sentinelizing a retracted subscribe removes it from stub-keyed
        accounting; without origin accounting a stub cycling
        subscribe/unsubscribe against an unresponsive server slips every
        cycle's pending out of its count and grows the table without
        bound. Retained sentinels must count against the originator."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        before = backend._stub_subscription_count("s1")
        for i in range(3):
            await backend.forward_from_stub("s1", {
                "jsonrpc": "2.0", "id": 100 + i,
                "method": "resources/subscribe",
                "params": {"uri": f"file:///cycle-{i}.txt"},
            }, caller=caller_a)  # server never answers
            await backend.forward_from_stub("s1", {
                "jsonrpc": "2.0", "id": 200 + i,
                "method": "resources/unsubscribe",
                "params": {"uri": f"file:///cycle-{i}.txt"},
            }, caller=caller_a)  # retract sentinelizes the pending
        assert backend._stub_subscription_count("s1") == before + 3

    @pytest.mark.asyncio
    async def test_identity_detach_sentinelizes_inflight_subscribe(
        self,
    ) -> None:
        """An identity stub detaching with a subscribe in flight must not
        drop the pending: the late grant would strand the upstream lease
        with nobody to release it. The pending converts to the sentinel and
        the grant is released as the pending's caller."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)  # grant in flight
        await backend.detach_stub("s1")
        p = next(p for p in backend._pending_requests.values() if p.resource_uri)
        assert p.stub_uuid == backend_mod._RELEASE_STUB_SENTINEL
        await _settle_lease(backend)  # the late grant arrives
        assert backend._resource_subscriptions == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_identity_unsubscribe_retracts_inflight_subscribe(
        self,
    ) -> None:
        """An identity unsubscribe racing the stub's own in-flight subscribe
        retracts it: the subscribe id is answered with a cancellation error,
        the unsubscribe settles locally, and the out-of-order late grant
        releases instead of recording routing for a stub that left."""
        backend = _make_backend()
        backend.supports_caller_identity = True
        inbox1 = await backend.attach_stub("s1")
        caller_a = CallerContext(session_key="dashboard:aaa")
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)  # grant in flight
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 2, "method": "resources/unsubscribe",
            "params": {"uri": "file:///watched.txt"},
        }, caller=caller_a)
        cancelled = await _drain(inbox1)
        assert cancelled["id"] == 1 and "error" in cancelled
        assert await _drain(inbox1) == {"jsonrpc": "2.0", "id": 2, "result": {}}
        unsubs = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert unsubs == []  # nothing granted: settled locally
        await _settle_lease(backend)  # the out-of-order grant arrives late
        assert backend._resource_subscriptions == {}
        assert backend._grant_callers == {}
        releases = [
            f for f in _frames(backend)
            if f.get("method") == "resources/unsubscribe"
        ]
        assert len(releases) == 1
        assert CALLER_META_KEY in releases[0]["params"]["_meta"]

    @pytest.mark.asyncio
    async def test_overlong_uri_refused_before_any_table_stores_it(
        self,
    ) -> None:
        """The per-stub cap bounds subscription COUNT, not bytes: an
        overlong URI is refused locally before any table retains the key,
        and the refusal does not echo the URI."""
        backend = _make_backend()
        inbox1 = await backend.attach_stub("s1")
        huge = "file:///" + "x" * (backend_mod._RESOURCE_URI_MAX_LEN + 1)
        await backend.forward_from_stub("s1", {
            "jsonrpc": "2.0", "id": 1, "method": "resources/subscribe",
            "params": {"uri": huge},
        })
        refusal = await _drain(inbox1)
        assert refusal["id"] == 1 and "error" in refusal
        assert huge not in json.dumps(refusal)
        assert backend._resource_subscriptions == {}
        assert backend._lease_awaiting_grant == set()
        assert backend._lease_pending_riders == {}
        assert all(
            not p.resource_uri for p in backend._pending_requests.values())
        assert _frames(backend) == []  # never forwarded either


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
    async def test_image_bearing_line_goes_through_the_budget_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A line matching the image probe is parse-confirmed, then the
        REWRITTEN line from the image stage is what gets routed."""
        monkeypatch.setattr(
            backend_mod, "parse_image_bearing_frame", lambda line: {"parsed": True},
        )
        monkeypatch.setattr(
            backend_mod, "rewrite_image_frame",
            lambda msg, line, server: _line({"id": "gw-1", "result": "budgeted"}),
        )
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line(
            {"id": "gw-1", "result": {"content": [{"type": "image", "data": "AA=="}]}},
        )))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["result"] == "budgeted"

    @pytest.mark.asyncio
    async def test_non_image_frame_matching_probe_skips_the_image_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe false positive (escaped text, no image blocks) must not
        occupy the image pool: the parse stage returns None and the original
        line is routed with no rewrite call."""
        rewrite_calls = []
        monkeypatch.setattr(backend_mod, "parse_image_bearing_frame", lambda line: None)
        monkeypatch.setattr(
            backend_mod, "rewrite_image_frame",
            lambda msg, line, server: rewrite_calls.append(1) or line,
        )
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line({"id": "gw-1", "result": 'has "image" text'})))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        assert (await _drain(inbox))["result"] == 'has "image" text'
        assert rewrite_calls == []

    @pytest.mark.asyncio
    async def test_image_budget_failure_routes_the_raw_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected hook failure must not take the relay down: the raw
        line is routed (per-block fail-closed lives INSIDE the hook)."""
        def _boom(line: bytes) -> dict:
            raise RuntimeError("pillow exploded")

        monkeypatch.setattr(backend_mod, "parse_image_bearing_frame", _boom)
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(_line(
            {"id": "gw-1", "result": {"content": [{"type": "image", "data": "AA=="}]}},
        )))
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        await backend.run_stdout_pump()
        routed = await _drain(inbox)
        assert routed["result"]["content"][0]["type"] == "image"

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


class TestBackendTmpContainment:
    """Issue #5064: spawn injects a contained temp dir; shutdown reclaims it."""

    @pytest.mark.asyncio
    async def test_spawn_contains_temp_under_managed_root(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        from pathlib import Path

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        await spawn_backend(
            _pool_key(), "/usr/bin/example-mcp", [], {}, "/nonexistent-work-dir"
        )

        env = fake_spawn["kwargs"]["env"]
        root = home / "run" / "mcp-tmp"
        for key in ("TMPDIR", "TMP", "TEMP"):
            assert Path(env[key]).parent == root, key
        assert env["TMPDIR"] == env["TMP"] == env["TEMP"]
        contained = Path(env["TMPDIR"])
        assert contained.is_dir()
        # Liveness anchor for the sweep: allocation + spawn leave an owner
        # record on disk -- the sweep's ONLY state (no in-memory field).
        assert (contained / bt.OWNER_FILENAME).is_file()

    @pytest.mark.asyncio
    async def test_operator_declared_temp_wins(self, fake_spawn, monkeypatch, tmp_path) -> None:
        # A spec that sets TMPDIR deliberately points a heavy server at
        # chosen storage; containment must not trade litter for ENOSPC.
        # Declaration is the CALLER's signal (declared_temp_keys), carried
        # from the gatewayd closure that knows the declared-env set.
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        await spawn_backend(
            _pool_key(),
            "/usr/bin/example-mcp",
            [],
            {"TMPDIR": "/mnt/bigdisk/tmp"},
            "/nonexistent-work-dir",
            declared_temp_keys=("TMPDIR",),
        )

        env = fake_spawn["kwargs"]["env"]
        assert env["TMPDIR"] == "/mnt/bigdisk/tmp"
        assert not (home / "run" / "mcp-tmp").exists() or not any(
            (home / "run" / "mcp-tmp").iterdir()
        )

    @pytest.mark.asyncio
    async def test_partial_declaration_strips_competing_ambient_keys(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        # A spec declaring only TMP must actually govern the child: tempfile
        # consults TMPDIR before TMP, so leaving the daemon's ambient TMPDIR
        # in place would silently defeat the declaration (macOS always
        # exports one). Yield = declared keys kept, undeclared canonical
        # keys pruned.
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        await spawn_backend(
            _pool_key(),
            "/usr/bin/example-mcp",
            [],
            {"TMPDIR": "/var/folders/xy/ambient", "TMP": "/mnt/bigdisk/tmp"},
            "/nonexistent-work-dir",
            declared_temp_keys=("TMP",),
        )

        env = fake_spawn["kwargs"]["env"]
        assert "TMPDIR" not in env
        assert env["TMP"] == "/mnt/bigdisk/tmp"
        assert not (home / "run" / "mcp-tmp").exists() or not any(
            (home / "run" / "mcp-tmp").iterdir()
        )

    @pytest.mark.asyncio
    async def test_ambient_temp_does_not_suppress_containment(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        # Regression: the resolver folds the daemon's own inherited environ
        # into the spawn env, and macOS always exports TMPDIR (Windows: TMP /
        # TEMP). An env-membership gate read that ambient value as an operator
        # declaration and disabled containment on those platforms entirely.
        # Ambient keys must be OVERRIDDEN by the managed triple.
        from pathlib import Path

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        await spawn_backend(
            _pool_key(),
            "/usr/bin/example-mcp",
            [],
            {"TMPDIR": "/var/folders/xy/ambient", "TMP": r"C:\Users\x\tmp"},
            "/nonexistent-work-dir",
            declared_temp_keys=(),
        )

        env = fake_spawn["kwargs"]["env"]
        root = home / "run" / "mcp-tmp"
        for key in ("TMPDIR", "TMP", "TEMP"):
            assert Path(env[key]).parent == root, key
        assert Path(env["TMPDIR"]).is_dir()

    @pytest.mark.asyncio
    async def test_spawn_survives_allocation_failure(self, fake_spawn, monkeypatch) -> None:
        # Fail-open: containment is hygiene, not a spawn prerequisite. The
        # allocate step raises when the dir or its owner record cannot be
        # written (ENOSPC / inode exhaustion) -- the spawn proceeds with
        # inherited temp and nothing is left on disk to reclaim.
        from kiro_crew.mcp_gateway import backend as backend_mod

        def _boom(_digest: str):
            raise OSError("no space left on device")

        monkeypatch.setattr(backend_mod, "allocate_backend_tmp", _boom)

        await spawn_backend(
            _pool_key(), "/usr/bin/example-mcp", [], {"PATH": "/usr/bin"}, "/nonexistent-work-dir"
        )

        env = fake_spawn["kwargs"]["env"]
        assert "TMPDIR" not in env

    @pytest.mark.asyncio
    async def test_spawn_failure_reclaims_the_fresh_dir(self, monkeypatch, tmp_path) -> None:
        # Unowned-by-a-live-process dirs are never deleted by the sweeps, so
        # the spawn-failure path is the ONLY reclamation point for a dir
        # whose process never existed.
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        async def _boom(*_args, **_kwargs):
            raise OSError("exec failed")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

        with pytest.raises(OSError):
            await spawn_backend(
                _pool_key(), "/usr/bin/example-mcp", [], {}, "/nonexistent-work-dir"
            )

        root = home / "run" / "mcp-tmp"
        assert not root.exists() or list(root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_shutdown_leaves_the_dir_for_the_sweep(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        # Deliberately NO deletion on shutdown: the launcher's exit is not
        # proof its process TREE is gone (a wrapper exits while its server
        # child lives on). The sweep's dual condition (owner dead AND idle)
        # is the single deletion authority.
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        from pathlib import Path

        backend = await spawn_backend(
            _pool_key(), "/usr/bin/example-mcp", [], {}, "/nonexistent-work-dir"
        )
        tmp_dir = Path(fake_spawn["kwargs"]["env"]["TMPDIR"])
        assert tmp_dir.is_dir()

        fake_spawn["process"].returncode = 0
        await backend.shutdown()

        assert tmp_dir.is_dir(), "shutdown must not delete; the sweep owns deletion"
