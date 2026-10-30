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

The list above spans the ``test_mcp_gateway_backend_coverage`` family. This file tests the
request-shaping helpers (caller and tenant metadata, client extensions, heartbeat ids), stub attach
and detach accounting, ``forward_from_stub``, the initialize state machine with its first-handshake
deadline and ``prime_initialize``, ``_broadcast_backend_gone``, inbox delivery and notification
ownership. The rest lives in the siblings:
``test_mcp_gateway_backend_coverage_route_backend_line.py`` holds the routing of each stdout frame
by ``_route_backend_line``, ``test_mcp_gateway_backend_coverage_call_metrics_logging.py`` holds the
call-metrics scoring and the failed-tool-call log line,
``test_mcp_gateway_backend_coverage_lease_settlement.py`` and
``test_mcp_gateway_backend_coverage_caller_grants_eviction.py`` hold the subscription-response
hardening (lease settlement without caller identity, then per-caller grants and rekey eviction),
``test_mcp_gateway_backend_coverage_stdio_pipes.py`` holds the stdout pump with its oversize and
stray lines, the handshake edge cases filed with them (a non-object ``capabilities``, the pre-init
reader's stray lines and a ``prime_initialize`` that finds its backend dead), the stderr drain and
the stdin writer, ``test_mcp_gateway_backend_coverage_mcp_apps.py`` holds the MCP Apps helpers, and
``test_mcp_gateway_backend_coverage_process_lifecycle.py`` holds spawn, temp containment,
``send_initialize``, cancellation, recycle, shutdown and the heartbeat. The shared fixture and
helpers live in ``mcp_gateway_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import _drain, _frames, _make_backend

from kiro_crew.mcp_caller import (
    CALLER_META_KEY,
    TENANT_META_KEY,
    CallerContext,
    tenant_nonce_from_meta,
)
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
    _inject_tenant_meta,
    _is_heartbeat_id,
    _mcp_apps_enabled,
    _PendingRequest,
    _strip_caller_meta,
)
from kiro_crew.mcp_gateway.backend_wire import _carry_tool_call_id

# --- Pure request-shaping helpers -------------------------------------------


class TestFrameHelpers:
    def test_strip_caller_meta_removes_forged_block(self) -> None:
        msg: dict[str, Any] = {
            "method": "tools/call",
            "params": {"_meta": {CALLER_META_KEY: {"sessionKey": "forged"}, "progressToken": "pt"}},
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
        out = _inject_caller_meta(
            {"method": "tools/call"}, CallerContext(session_key="dashboard:1")
        )
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

    def test_carry_tool_call_id_promotes_progress_token(self) -> None:
        """The gateway carries the request's progressToken (kiro-cli's ACP
        toolCallId) onto the caller so the authored block emits it as the
        always-present correlation key."""
        msg: dict[str, Any] = {
            "method": "tools/call",
            "params": {"_meta": {"progressToken": "toolu_9"}, "name": "t"},
        }
        carried = _carry_tool_call_id(CallerContext(session_key="s"), msg)
        assert carried.tool_call_id == "toolu_9"
        block = _inject_caller_meta(msg, carried)["params"]["_meta"][CALLER_META_KEY]
        assert block["toolCallId"] == "toolu_9"

    def test_carry_tool_call_id_is_noop_without_a_token(self) -> None:
        """No progressToken: the caller is returned unchanged, so the backend's
        own progressToken fallback (empty here) still applies."""
        caller = CallerContext(session_key="s")
        msg: dict[str, Any] = {"method": "tools/call", "params": {"name": "t"}}
        assert _carry_tool_call_id(caller, msg) is caller
        # A numeric token coerces to str, matching the backend reader.
        msg_num: dict[str, Any] = {
            "method": "tools/call",
            "params": {"_meta": {"progressToken": 42}, "name": "t"},
        }
        assert _carry_tool_call_id(caller, msg_num).tool_call_id == "42"

    def test_strip_caller_meta_also_removes_a_forged_TENANT_block(self) -> None:
        """The nonce decides which namespace an unnamed co-tenant lands in.

        A stub allowed to supply its own would pick a PEER's namespace — a chosen
        collision rather than an accidental one — so the nonce is stripped on the
        same trust boundary as the identity, by the same function, on every
        forwarded frame.
        """
        msg: dict[str, Any] = {
            "method": "tools/call",
            "params": {
                "_meta": {
                    TENANT_META_KEY: {"schemaVersion": 1, "nonce": "peer-nonce"},
                    "progressToken": "pt",
                }
            },
        }
        out = _strip_caller_meta(msg)
        assert TENANT_META_KEY not in out["params"]["_meta"]
        assert out["params"]["_meta"]["progressToken"] == "pt"
        assert TENANT_META_KEY in msg["params"]["_meta"]

    def test_strip_caller_meta_removes_BOTH_blocks_at_once(self) -> None:
        """One forged block must not shield the other."""
        msg: dict[str, Any] = {
            "method": "tools/call",
            "params": {
                "_meta": {
                    CALLER_META_KEY: {"sessionKey": "forged"},
                    TENANT_META_KEY: {"schemaVersion": 1, "nonce": "forged"},
                }
            },
        }
        out = _strip_caller_meta(msg)
        assert "_meta" not in out["params"]

    def test_inject_tenant_meta_carries_the_nonce_without_an_identity(self) -> None:
        """The shape an UNNAMED co-tenant receives.

        A nonce block and no caller block: the separator arrives, the identity does
        not, and ``CallerContext.from_meta`` must still find nothing — otherwise a
        connection name would be laundered into a session identity.
        """
        out = _inject_tenant_meta({"method": "tools/call"}, "n0nce")
        meta = out["params"]["_meta"]
        assert meta[TENANT_META_KEY]["nonce"] == "n0nce"
        assert CALLER_META_KEY not in meta
        assert CallerContext.from_meta(meta) is None
        assert tenant_nonce_from_meta(meta) == "n0nce"

    def test_inject_tenant_meta_preserves_other_meta(self) -> None:
        msg: dict[str, Any] = {
            "method": "tools/call",
            "params": {"_meta": {"progressToken": 7}, "name": "t"},
        }
        out = _inject_tenant_meta(msg, "n0nce")
        assert out["params"]["_meta"]["progressToken"] == 7
        assert out["params"]["name"] == "t"
        assert TENANT_META_KEY not in msg["params"]["_meta"]

    def test_is_heartbeat_id_accepts_int_and_string(self) -> None:
        assert _is_heartbeat_id(HEARTBEAT_PING_ID)
        assert _is_heartbeat_id(str(HEARTBEAT_PING_ID))
        assert not _is_heartbeat_id("gw-1-2")
        assert not _is_heartbeat_id(None)


class TestClientExtensionInjection:
    def test_injects_ui_extension(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        msg: dict[str, Any] = {
            "method": "initialize",
            "params": {"capabilities": {"roots": {}}},
        }
        out = _inject_client_extensions(msg)
        ext = out["params"]["capabilities"]["extensions"][MCP_APPS_EXTENSION_KEY]
        assert ext == {"mimeTypes": [MCP_APPS_MIME_TYPE]}
        # Pre-existing capabilities preserved; caller's frame untouched.
        assert out["params"]["capabilities"]["roots"] == {}
        assert "extensions" not in msg["params"]["capabilities"]

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
    async def test_the_per_stub_agent_is_kept_per_stub_and_pruned_on_detach(self) -> None:
        """Each attached stub's own agent, which an app render is stamped with.

        Per stub and not a key field: the agent is not a pool dimension, so a
        shared backend has one entry per attached stub and a PoolKey field could
        hold none of them. The prune matters because the value is a governance
        identity -- a render spooled after a detach must name no agent (which
        refuses) rather than a gone stub's.
        """
        backend = _make_backend()
        await backend.attach_stub("s1", agent="gpu-dev")
        await backend.attach_stub("s2", agent="kirocrew")
        assert backend.agent_for_stub("s1") == "gpu-dev"
        assert backend.agent_for_stub("s2") == "kirocrew"

        await backend.detach_stub("s2")
        assert backend.agent_for_stub("s2") == ""
        assert backend.agent_for_stub("s1") == "gpu-dev", "a detach took a sibling's agent"

    @pytest.mark.asyncio
    async def test_a_stub_that_declares_no_agent_records_none(self) -> None:
        """An ephemeral app-call stub has no session behind it, so it names no
        agent -- and ``""`` is the refusing answer downstream, never a default."""
        backend = _make_backend()
        await backend.attach_stub("__app_call__abc")
        assert backend.agent_for_stub("__app_call__abc") == ""
        assert backend.agent_for_stub("never-attached") == ""

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
    @pytest.mark.parametrize("with_caller", [True, False])
    async def test_only_admitted_gateway_caller_reaches_backend(self, with_caller) -> None:
        backend = _make_backend()
        backend.supports_caller_identity = True
        caller = (
            None
            if not with_caller
            else CallerContext(
                session_key="reviewer",
                from_gateway=True,
            )
        )
        msg = {
            "method": "tools/call",
            "id": 23,
            "params": {
                "name": "memory_recall",
                "_meta": {
                    CALLER_META_KEY: {
                        "schemaVersion": 1,
                        "sessionKey": "victim",
                        "memberMemoryProof": "forged.signature",
                    },
                    "progressToken": "visible-progress",
                },
            },
        }
        await backend.forward_from_stub("s1", msg, caller=caller)
        meta = _frames(backend)[0]["params"]["_meta"]
        parsed = CallerContext.from_meta(meta)
        if caller is None:
            assert parsed is None
        else:
            assert parsed is not None
            assert parsed.session_key == "reviewer"
        assert "forged.signature" not in json.dumps(meta)
        assert meta["progressToken"] == "visible-progress"
        assert "forged.signature" in json.dumps(msg)

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
        await backend.forward_from_stub(
            "s1",
            {
                "method": "tools/call",
                "id": 77,
                "params": {
                    "name": "draw",
                    "arguments": {"x": 1},
                    "_meta": {"progressToken": "pt-9"},
                },
            },
        )
        (frame,) = _frames(backend)
        assert frame["id"] == "gw-4242-1"
        pending = backend._pending_requests["gw-4242-1"]
        assert (pending.stub_uuid, pending.original_id, pending.method) == ("s1", 77, "tools/call")
        assert pending.tool_name == "draw"
        assert pending.tool_arguments == {"x": 1}
        assert pending.progress_token == "pt-9"
        assert pending.t_start_ms > 0

    @pytest.mark.asyncio
    async def test_non_string_tool_name_and_non_dict_args_ignored(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub(
            "s1",
            {
                "method": "tools/call",
                "id": 1,
                "params": {"name": 42, "arguments": "not-a-dict"},
            },
        )
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
        await backend.forward_from_stub(
            "s1",
            {
                "method": "resources/list",
                "id": 3,
                "params": {"_meta": {CALLER_META_KEY: {"sessionKey": "victim"}}},
            },
        )
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
        await backend.forward_from_stub(
            "s1",
            {
                "method": "notifications/cancelled",
                "params": {"requestId": 5, "reason": "user stopped"},
            },
        )
        assert _frames(backend)[1]["params"]["requestId"] == "gw-4242-1"

    @pytest.mark.asyncio
    async def test_cancelled_for_other_stubs_request_not_remapped(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {"method": "tools/call", "id": 5})
        await backend.forward_from_stub(
            "s2",
            {
                "method": "notifications/cancelled",
                "params": {"requestId": 5},
            },
        )
        assert _frames(backend)[1]["params"]["requestId"] == 5

    @pytest.mark.asyncio
    async def test_cancelled_without_request_id_passes_through(self) -> None:
        backend = _make_backend()
        await backend.forward_from_stub("s1", {"method": "notifications/cancelled", "params": {}})
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
        return {"jsonrpc": "2.0", "id": id_, "method": "initialize", "params": {"capabilities": {}}}

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
            "jsonrpc": "2.0",
            "id": 99,
            "result": backend._init_result,
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
            "capabilities": {"experimental": {"kirocrew.caller-identity": {}}}
        }
        await backend._on_upstream_initialize({"jsonrpc": "2.0", "id": "gw-1", "result": result})
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
        return {"jsonrpc": "2.0", "id": id_, "method": "initialize", "params": {"capabilities": {}}}

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

    def test_windows_floor_lifts_the_posix_measured_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Windows host left on the POSIX-measured 10s default reaps a backend
        # whose cold start was about to finish, then respawns it -- the crash
        # loop. The floor gives the handshake room instead.
        monkeypatch.setattr(backend_mod.platform_compat, "IS_WINDOWS", True)
        effective = backend_mod._effective_initialize_timeout_secs(
            backend_mod._DEFAULT_INITIALIZE_TIMEOUT_SECS
        )
        assert effective == backend_mod._WINDOWS_INITIALIZE_TIMEOUT_FLOOR_SECS
        assert effective > backend_mod._DEFAULT_INITIALIZE_TIMEOUT_SECS

    def test_windows_floor_never_lowers_an_explicit_higher_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An operator who deliberately raised the deadline (slow runtime, remote
        # resolution) must keep their value; the floor is a floor, not a clamp.
        monkeypatch.setattr(backend_mod.platform_compat, "IS_WINDOWS", True)
        higher = backend_mod._WINDOWS_INITIALIZE_TIMEOUT_FLOOR_SECS + 45.0
        assert backend_mod._effective_initialize_timeout_secs(higher) == higher

    def test_posix_initialize_timeout_is_passed_through_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # POSIX cold starts are what the 10s default was measured against, so
        # the floor must not touch them -- it is a Windows-only lift.
        monkeypatch.setattr(backend_mod.platform_compat, "IS_WINDOWS", False)
        assert (
            backend_mod._effective_initialize_timeout_secs(
                backend_mod._DEFAULT_INITIALIZE_TIMEOUT_SECS
            )
            == backend_mod._DEFAULT_INITIALIZE_TIMEOUT_SECS
        )
        assert backend_mod._effective_initialize_timeout_secs(3.0) == 3.0

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
            {
                "jsonrpc": "2.0",
                "id": "gw-4242-1",
                "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
            }
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
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"_meta": {CALLER_META_KEY: {"sessionKey": "forged"}}},
            },
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
        backend._pending_requests.update(
            {
                "gw-1": _PendingRequest("s1", 11, "tools/call"),
                "gw-2": _PendingRequest("s2", 22, "tools/list"),
                "gw-3": _PendingRequest("ghost", 33, "tools/list"),
            }
        )
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
            backend_mod._APPS_STUB_SENTINEL,
            None,
            "resources/read",
            apps_future=fut,
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
            backend_mod._APPS_STUB_SENTINEL,
            None,
            "resources/read",
            apps_future=fut,
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
        live = await backend.attach_stub("live")
        await backend._deliver_to_stub("ghost", {"id": 1})
        assert live.empty()
        assert set(backend._stub_inboxes) == {"live"}

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
            "s1", 1, "tools/call", progress_token="pt"
        )
        assert backend._notification_owner({"params": {"progressToken": "pt"}}) == "s1"

    def test_colliding_progress_token_is_unattributable(self) -> None:
        backend = _make_backend()
        backend._pending_requests.update(
            {
                "gw-1": _PendingRequest("s1", 1, "tools/call", progress_token="pt"),
                "gw-2": _PendingRequest("s2", 2, "tools/call", progress_token="pt"),
            }
        )
        assert backend._notification_owner({"params": {"progressToken": "pt"}}) is None

    def test_related_request_id_routes(self) -> None:
        backend = _make_backend()
        backend._pending_requests["gw-9"] = _PendingRequest("s3", 1, "tools/call")
        assert (
            backend._notification_owner({"params": {"_meta": {"relatedRequestId": "gw-9"}}}) == "s3"
        )

    def test_init_sentinel_is_never_an_owner(self) -> None:
        backend = _make_backend()
        backend._pending_requests["gw-9"] = _PendingRequest("__init__", None, "initialize")
        assert (
            backend._notification_owner({"params": {"_meta": {"relatedRequestId": "gw-9"}}}) is None
        )

    def test_unknown_related_request_id(self) -> None:
        backend = _make_backend()
        assert (
            backend._notification_owner({"params": {"_meta": {"relatedRequestId": "nope"}}}) is None
        )
        assert backend._notification_owner({"params": {"_meta": "bad"}}) is None
        # A well-formed _meta that simply carries no routing token.
        assert backend._notification_owner({"params": {"_meta": {"other": 1}}}) is None
