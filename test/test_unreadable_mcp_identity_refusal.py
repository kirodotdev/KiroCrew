"""The session handle's refusal of an unreadable MCP identity says why, to the
operator and to the model.

``AcpSessionHandle._refuse_unreadable_mcp_identity`` answers the request itself,
so no surface ever steers a notice for it, and ACP's permission answer carries
only ``outcome``/``optionId``: the backend tells the model "the user rejected
this tool call". The handle therefore steers the host notice into the turn
BEFORE its reject, and its warning and audit name the check that fired, which
differ in what they need (an over-long name is the server's spelling, a server
mismatch is two harness frames disagreeing about one call).
"""

from __future__ import annotations

import ast
import asyncio
import copy
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KAS, AcpEvent, JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_STRANDS
from kiro_crew.deny_notice import UNREADABLE_IDENTITY_REASONS
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.providers.acp_turns import _to_llm_event
from kiro_crew.testing.wait import async_wait_until, await_until

_LOGGER = "kiro_crew.acp.session_handle"
_REFUSAL = "refusing a permission request whose MCP identity is unreadable"

_TOOL_CALL = {
    "sessionUpdate": "tool_call",
    "toolCallId": "tc-mcp",
    "title": "@probefs/read_file",
    "kind": "other",
    "status": "pending",
    "rawInput": {"path": "/tmp/probe-note.txt"},
    "_meta": {"kiro": {"serverName": "probefs", "toolOrigin": "client"}},
}
_PERMISSION_META = {
    "kiro": {
        "toolId": "mcp_probefs_read_file",
        "mcpTool": {"version": 1, "identity": {"serverName": "probefs", "toolName": "read_file"}},
    }
}

#: scenario -> (tool_call ``_meta.kiro``, permission ``mcpTool.identity``, reason code).
_SCENARIOS: dict[str, tuple[dict, dict, str]] = {
    "frame_oversize": (
        {"serverName": "s" * 600, "toolOrigin": "client"},
        {"serverName": "probefs", "toolName": "read_file"},
        "tool_call_identity",
    ),
    "request_oversize": (
        {"serverName": "probefs", "toolOrigin": "client"},
        {"serverName": "probefs", "toolName": "t" * 600},
        "request_identity_over_bound",
    ),
    "request_not_string": (
        {"serverName": "probefs", "toolOrigin": "client"},
        {"serverName": "probefs", "toolName": ["read_file"]},
        "request_identity_not_string",
    ),
    "server_mismatch": (
        {"serverName": "probefs", "toolOrigin": "client"},
        {"serverName": "othersrv", "toolName": "ping"},
        "server_mismatch",
    ),
}


def _kas_handle(trace: list[tuple], *, steer_gate: asyncio.Event | None = None):
    """A KAS session handle whose runtime records every write in one ordered trace."""
    rt = MagicMock()
    rt.acp_backend = ACP_BACKEND_KAS
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    rt.supports_image_prompt = False
    steer_written = asyncio.Event()

    async def _send_request(method: str, params: dict, **_kw: Any) -> int:
        trace.append(("request", method, params))
        steer_written.set()
        if steer_gate is not None:
            await steer_gate.wait()
        return len(trace)

    async def _send_response(request_id: Any, payload: dict, **_kw: Any) -> None:
        trace.append(("response", request_id, payload))

    rt.send_request = _send_request
    rt.send_response = _send_response
    handle = AcpSessionHandle("s-1", asyncio.Queue(), rt)
    audits: list[tuple] = []
    handle._audit_handle_reject = lambda *a, **k: audits.append(a)  # type: ignore[method-assign]
    return handle, audits, steer_written


def _unreadable_event(handle: AcpSessionHandle, scenario: str) -> AcpEvent:
    tool_call_kiro, identity, _reason = _SCENARIOS[scenario]
    frame = dict(_TOOL_CALL)
    frame["_meta"] = {"kiro": tool_call_kiro}
    handle._handle_update(
        JsonRpcMessage(method="session/update", params={"sessionId": "s-1", "update": frame})
    )
    meta = copy.deepcopy(_PERMISSION_META)
    meta["kiro"]["mcpTool"]["identity"] = identity
    event = handle._build_permission_event(
        JsonRpcMessage(
            id=61,
            method="session/request_permission",
            params={
                "sessionId": "s-1",
                "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "probe call"},
                "options": [
                    {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
                ],
                "_meta": meta,
            },
        )
    )
    assert event is not None and event.mcp_identity_unreadable is True, scenario
    return event


_REJECT = {"outcome": {"outcome": "selected", "optionId": "reject"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
async def test_the_refusal_steers_the_reason_before_the_reject_and_names_the_check(
    scenario: str, caplog: pytest.LogCaptureFixture
) -> None:
    trace: list[tuple] = []
    handle, audits, _ = _kas_handle(trace)
    event = _unreadable_event(handle, scenario)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert await handle._refuse_unreadable_mcp_identity(event) is True

    # The model is told the GATEWAY refused it, while the request is unanswered.
    assert [entry[0] for entry in trace] == ["request", "response"], trace
    _kind, method, params = trace[0]
    assert method == "_session/steer" and params["sessionId"] == "s-1"
    notice = params["message"]
    assert "[Kiro Crew host notice]" in notice
    assert "refused by the Kiro Crew gateway" in notice
    assert "NOT a user action" in notice
    assert "Do not retry it unchanged" in notice
    assert trace[1] == ("response", 61, _REJECT)

    # The operator's log and the audit name the check that fired.
    reason = _SCENARIOS[scenario][2]
    assert event.mcp_identity_unreadable_reason == reason
    clause = UNREADABLE_IDENTITY_REASONS[reason]
    assert clause in notice
    warnings = [r.getMessage() for r in caplog.records if _REFUSAL in r.getMessage()]
    assert len(warnings) == 1, warnings
    assert clause in warnings[0] and "[session=s-1]" in warnings[0]
    if reason == "server_mismatch":
        # Both names were in bounds: the line must not say one was too long.
        assert "longer than" not in warnings[0]
    assert audits == [(61, "mcp__unreadable", f"mcp_identity_unreadable:{reason}")]


@pytest.mark.asyncio
async def test_each_check_logs_its_own_line(caplog: pytest.LogCaptureFixture) -> None:
    lines = {}
    for scenario in _SCENARIOS:
        handle, _audits, _ = _kas_handle([])
        event = _unreadable_event(handle, scenario)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            await handle._refuse_unreadable_mcp_identity(event)
        (lines[scenario],) = [r.getMessage() for r in caplog.records if _REFUSAL in r.getMessage()]
    assert len(set(lines.values())) == len(_SCENARIOS), lines


@pytest.mark.asyncio
async def test_a_cancel_inside_the_steer_still_answers_the_request() -> None:
    """A cancelled steer skips the reject the refusal would have written, and an
    unanswered permission request wedges every later turn: the reject is still
    sent, from a strongly referenced task, and the cancellation propagates."""
    trace: list[tuple] = []
    # Never set: the steer write stays parked until the refusal is cancelled.
    gate = asyncio.Event()
    handle, _audits, steer_written = _kas_handle(trace, steer_gate=gate)
    event = _unreadable_event(handle, "server_mismatch")
    refusal = asyncio.ensure_future(handle._refuse_unreadable_mcp_identity(event))
    await async_wait_until(lambda: steer_written.is_set() or refusal.done(), describe=lambda: trace)
    assert steer_written.is_set(), f"the refusal answered without steering: {trace}"
    refusal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(refusal, what="the cancelled refusal")
    await async_wait_until(lambda: len(trace) == 2, describe=lambda: trace)
    assert trace[0][1] == "_session/steer"
    assert trace[1] == ("response", 61, _REJECT)


def test_a_strands_request_that_is_not_the_envelope_names_that_check() -> None:
    msg = JsonRpcMessage(
        id=62,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {"toolCallId": "tc-1", "status": "pending", "title": "shell"},
            "options": [{"optionId": "deny", "name": "Deny", "kind": "reject_once"}],
        },
    )
    event, _ = build_permission_event(msg, harness_backend=ACP_BACKEND_STRANDS)
    assert event is not None and event.mcp_identity_unreadable is True
    assert event.mcp_identity_unreadable_reason == "strands_identity_unclassified"


def test_a_readable_identity_carries_no_reason() -> None:
    handle, _audits, _ = _kas_handle([])
    handle._handle_update(
        JsonRpcMessage(
            method="session/update", params={"sessionId": "s-1", "update": dict(_TOOL_CALL)}
        )
    )
    event = handle._build_permission_event(
        JsonRpcMessage(
            id=63,
            method="session/request_permission",
            params={
                "sessionId": "s-1",
                "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "probe"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": _PERMISSION_META,
            },
        )
    )
    assert event is not None
    assert (event.mcp_identity_unreadable, event.mcp_identity_unreadable_reason) == (False, "")


def test_every_reason_the_builder_sets_has_a_clause() -> None:
    """The codes are written as literals where each check fires; every one of
    them must name its clause, and no clause may be left without a writer."""
    from kiro_crew.acp._dispatch_parts import permission_requests

    tree = ast.parse(Path(permission_requests.__file__).read_text(encoding="utf-8"))
    written: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_unreadable_reason" for t in node.targets):
            continue
        for leaf in ast.walk(node.value):
            if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str) and leaf.value:
                written.add(leaf.value)
    assert "server_mismatch" in written, "the scan went blind"
    assert written == set(UNREADABLE_IDENTITY_REASONS)


def _client_path_event(scenario: str) -> AcpEvent:
    """The event ``AcpClient._build_permission_event`` yields on KAS: the shared
    parser and builder, handed the unreadable-identity store only KAS gets. No
    handle refusal stands in front of it, so the consumers judge it."""
    tool_call_kiro, identity, _reason = _SCENARIOS[scenario]
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "identity_unreadable_cache": {},
    }
    parse_session_update({**_TOOL_CALL, "_meta": {"kiro": tool_call_kiro}}, **caches)
    meta = copy.deepcopy(_PERMISSION_META)
    meta["kiro"]["mcpTool"]["identity"] = identity
    msg = JsonRpcMessage(
        id=71,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "probe call"},
            "options": [{"optionId": "reject", "name": "Deny", "kind": "reject_once"}],
            "_meta": meta,
        },
    )
    event, _ = build_permission_event(msg, **caches)
    assert event is not None and event.mcp_identity_unreadable is True, scenario
    return event


class _SteeringProvider:
    supports_refusal_steer = True

    def __init__(self) -> None:
        self.trace: list[tuple[str, Any]] = []

    async def steer(self, notice: str) -> bool:
        self.trace.append(("steer", notice))
        return True

    async def reject_tool(self, request_id: Any) -> None:
        self.trace.append(("reject", request_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
async def test_the_gate_and_the_unattended_tier_name_the_check_on_the_client_path(
    scenario: str,
) -> None:
    """The hook gate and ``_resolve_permission`` said "name over the tool-surface
    bound" for every check, and the notice told the model to fix the name."""
    event = _client_path_event(scenario)
    clause = UNREADABLE_IDENTITY_REASONS[_SCENARIOS[scenario][2]]

    decision = HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))
    assert decision.action == TOOL_DENY
    assert clause in (decision.reason or "")

    provider = _SteeringProvider()
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch("kiro_crew.sel.sel", lambda: sel_stub):
        approved = await _resolve_permission(
            provider,  # type: ignore[arg-type]
            _to_llm_event(event),
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
        )
    assert approved is False
    assert [kind for kind, _ in provider.trace] == ["steer", "reject"]
    notice = provider.trace[0][1]
    assert clause in notice and "the MCP tool identity the backend reported" in notice
    assert "failed validation" not in notice
    assert len(rows) == 1 and clause in str(rows[0].get("error"))
    if scenario == "server_mismatch":
        # Both names were in bounds: no refusal may say one was too long.
        for text in (decision.reason or "", notice, str(rows[0].get("error"))):
            assert "longer than" not in text and "tool-surface bound" not in text
