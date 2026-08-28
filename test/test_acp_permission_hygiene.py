"""Permission-answer hygiene: one-shot only, unknown ids fail closed.

Kiro Crew has no grant storage. An adapter that advertises ``allow_always``
must not persist that grant; missing or foreign ``sessionId`` must not be
answered on the wrong handle.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp._dispatch import (
    permission_answerable_on_handle,
    permission_frame_session_id,
    resolve_permission_allow_id,
)
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    METHOD_REQUEST_PERMISSION,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    JsonRpcMessage,
)


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/kiro_crew/task_executor.py",
        "src/kiro_crew/task_planner.py",
        "src/kiro_crew/llm_helpers.py",
        "src/kiro_crew/slack/handler.py",
    ],
)
def test_background_hook_calls_forward_verified_mcp_identity(relative_path: str) -> None:
    """Every permission bridge must preserve the trusted MCP identity pair."""
    repo_root = Path(__file__).resolve().parents[1]
    tree = ast.parse((repo_root / relative_path).read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "on_tool_call":
            continue
        keywords = {kw.arg for kw in node.keywords if kw.arg}
        if "mcp_identity_ambiguous" not in keywords:
            continue
        checked += 1
        assert {"mcp_server_name", "mcp_tool_name"} <= keywords
    assert checked > 0


def _make_runtime():
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    return rt, reader, proc


def _register(rt: AcpRuntime, *session_ids: str) -> dict[str, asyncio.Queue]:
    queues = {sid: asyncio.Queue() for sid in session_ids}
    rt._session_queues.update(queues)
    return queues


@pytest.fixture(autouse=True)
def _stub_sel(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.sel as sel_mod

    class _StubSel:
        def log_tool_invocation(self, **kwargs):
            return None

    monkeypatch.setattr(sel_mod, "sel", lambda: _StubSel())


def test_resolve_allow_id_prefers_once() -> None:
    recorded = {"allow_once": "allow", "allow_always": "allow_always"}
    assert resolve_permission_allow_id(recorded) == "allow"
    assert resolve_permission_allow_id(recorded, always=True) == "allow"


def test_resolve_allow_id_never_persists_always() -> None:
    assert resolve_permission_allow_id({"allow_always": "allow_always"}) is None
    assert resolve_permission_allow_id({"allow_always": "allow_always"}, always=True) is None


def test_resolve_allow_id_unknown_option_fails_closed() -> None:
    recorded = {"allow_once": "allow", "allow_always": "allow_always"}
    assert resolve_permission_allow_id(recorded, option_id="allow_always") is None
    assert resolve_permission_allow_id(recorded, option_id="not-advertised") is None
    assert resolve_permission_allow_id(recorded, option_id="allow") == "allow"


def test_permission_frame_session_id_rejects_missing_and_non_string() -> None:
    assert permission_frame_session_id(None) == ""
    assert permission_frame_session_id({}) == ""
    assert permission_frame_session_id({"sessionId": ""}) == ""
    assert permission_frame_session_id({"sessionId": 12}) == ""
    assert permission_frame_session_id({"sessionId": "sA"}) == "sA"


def test_permission_answerable_on_handle() -> None:
    own = {"sessionId": "sA"}
    child = {"sessionId": "child-1"}
    other = {"sessionId": "sB"}
    registered = frozenset({"sA", "sB"})

    assert permission_answerable_on_handle(own, "sA", registered_session_ids=registered)
    assert permission_answerable_on_handle(child, "sA", registered_session_ids=registered)
    assert not permission_answerable_on_handle(other, "sA", registered_session_ids=registered)
    assert not permission_answerable_on_handle({}, "sA")
    assert not permission_answerable_on_handle(own, "")


def test_crew_mcp_forwarding_stays_unverified_on_pi() -> None:
    from kiro_crew.acp import spec_servers

    assert spec_servers.crew_mcp_forwarding_unverified(ACP_BACKEND_PI)
    assert not spec_servers.crew_mcp_forwarding_unverified(ACP_BACKEND_KIRO)
    assert not spec_servers.crew_mcp_forwarding_unverified("claude")


@pytest.mark.asyncio
async def test_client_always_true_sends_allow_once(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path)
    client._permission_options[1] = {"allow_once": "allow", "allow_always": "allow_always"}
    client._send_response = AsyncMock()
    await client.approve_tool(1, always=True)
    client._send_response.assert_awaited_once_with(
        1,
        {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "allow"}},
    )


@pytest.mark.asyncio
async def test_client_allow_always_only_cancels(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path)
    client._permission_options[2] = {"allow_always": "allow_always"}
    client._send_response = AsyncMock()
    await client.approve_tool(2)
    client._send_response.assert_awaited_once_with(
        2,
        {"outcome": {"outcome": OUTCOME_CANCELLED}},
    )


@pytest.mark.asyncio
async def test_handle_always_true_sends_allow_once() -> None:
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._permission_options[3] = {"allow_once": "allow", "allow_always": "allow_always"}
    await handle.approve_tool(3, always=True)
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["result"]["outcome"]["optionId"] == "allow"


@pytest.mark.asyncio
async def test_handle_rejects_missing_session_id() -> None:
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    msg = JsonRpcMessage.from_dict(
        {
            "id": 88,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    await handle._reject_unanswerable_permission(msg)
    if handle._audit_tasks:
        await asyncio.gather(*list(handle._audit_tasks), return_exceptions=True)
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["id"] == 88
    assert sent["result"]["outcome"]["optionId"] == "reject_once"


@pytest.mark.asyncio
async def test_handle_rejects_foreign_registered_session() -> None:
    rt, _, proc = _make_runtime()
    q = _register(rt, "sA", "sB")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    msg = JsonRpcMessage.from_dict(
        {
            "id": 89,
            "method": METHOD_REQUEST_PERMISSION,
            "params": {
                "sessionId": "sB",
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    params = msg.params if isinstance(msg.params, dict) else {}
    assert not permission_answerable_on_handle(
        params,
        handle._session_id,
        registered_session_ids=handle._registered_session_ids(),
    )
    await handle._reject_unanswerable_permission(msg)
    if handle._audit_tasks:
        await asyncio.gather(*list(handle._audit_tasks), return_exceptions=True)
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())
    assert sent["result"]["outcome"]["optionId"] == "reject_once"


@pytest.mark.asyncio
async def test_client_dispatch_skips_missing_session_id(tmp_path) -> None:
    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST

    client = AcpClient(work_dir=tmp_path)
    client._session_id = "s1"
    client.reject_tool = AsyncMock()
    perm = JsonRpcMessage(
        id=99,
        method="session/requestPermission",
        params={"toolCall": {"title": "shell"}, "options": []},
    )
    complete = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

    async def fake_prompt_loop(req_id, timeout):
        yield "permission", perm
        yield "complete", complete

    client._prompt_loop = fake_prompt_loop
    events = []
    async for ev in client._dispatch_events(req_id=1, timeout=5.0):
        events.append(ev)

    assert EVENT_PERMISSION_REQUEST not in [e.kind for e in events]
    assert EVENT_COMPLETE in [e.kind for e in events]
    client.reject_tool.assert_awaited_once_with(99)
