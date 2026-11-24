"""A mid-session MCP sign-in on KAS reaches the one session holding the server.

When a connected server answers 401, KAS sends ``_kiro/openExternalUrl`` with
``serverName``, ``oauthCompletion: 'notification'`` and an ``oauthFlowId``, then one
``_kiro/mcp/oauthCompleted`` with the same id (kiro-agent
``packages/acp-type-covenant/client-capabilities/open-external-url.ts`` and
``capabilities/mcp/oauth-completed.ts``). Neither names a session. These tests pin that
the link goes only to a session that alone holds that server, that a repeated link
draws one card, and that the completion closes the card of the session shown it.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    METHOD_KAS_MCP_STATUS,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    JsonRpcMessage,
)

_URL = "https://auth.example.com/authorize?state=lazy-1"
_URL_2 = "https://auth.example.com/authorize?state=lazy-2"


def _runtime(*holders: tuple[str, list[str]]) -> AcpRuntime:
    """A KAS runtime with one registered session per (session id, servers it sent)."""
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    for session_id, servers in holders:
        runtime._session_queues[session_id] = asyncio.Queue()
        runtime._open_mcp_report(session_id, [{"name": name} for name in servers])
    return runtime


def _answers(runtime: AcpRuntime, monkeypatch) -> list[tuple]:
    answers: list[tuple] = []

    async def send_response(request_id, result):
        answers.append(("result", request_id, result))

    async def send_error(request_id, code, message):
        answers.append(("error", request_id, message))

    monkeypatch.setattr(runtime, "send_response", send_response)
    monkeypatch.setattr(runtime, "send_error", send_error)
    return answers


def _lazy(url: str = _URL, flow: str | None = "flow-1", server: str = "remote") -> dict:
    params = {"url": url, "serverName": server, "oauthCompletion": "notification"}
    if flow is not None:
        params["oauthFlowId"] = flow
    return params


def _frames(runtime: AcpRuntime, session_id: str) -> list[JsonRpcMessage]:
    queue = runtime._session_queues[session_id]
    return [queue.get_nowait() for _ in range(queue.qsize())]


@pytest.mark.asyncio
async def test_the_link_goes_to_the_one_session_holding_the_server(monkeypatch):
    runtime = _runtime(("s1", ["other"]), ("s2", ["remote"]))
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy())

    assert replies == [("result", 5, {"success": True})]
    assert _frames(runtime, "s1") == []
    [frame] = _frames(runtime, "s2")
    assert frame.method == METHOD_MCP_OAUTH_REQUEST
    assert frame.params == {"sessionId": "s2", "serverName": "remote", "oauthUrl": _URL}
    assert runtime._mcp_reports()["s2"].payload()["awaiting_auth"] == ["remote"]


@pytest.mark.asyncio
async def test_a_server_the_host_reported_counts_as_held(monkeypatch):
    """An agent-spec server Crew did not put on the wire is held once the host reports it."""
    runtime = _runtime(("s1", []))
    replies = _answers(runtime, monkeypatch)
    status = JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={"sessionId": "s1", "servers": [{"name": "remote", "status": "connected"}]},
    )
    runtime._fold_mcp_frame("s1", status)

    await runtime._answer_open_external_url(5, _lazy())

    assert replies == [("result", 5, {"success": True})]
    assert len(_frames(runtime, "s1")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "holders",
    [
        (("s1", ["remote"]), ("s2", ["remote"])),
        (("s1", ["other"]), ("s2", ["other"])),
    ],
    ids=["two-hold-it", "none-holds-it"],
)
async def test_a_link_no_single_session_holds_is_refused(monkeypatch, holders):
    runtime = _runtime(*holders)
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(6, _lazy())

    assert [kind for kind, _id, _msg in replies] == ["error"]
    assert _frames(runtime, "s1") == [] and _frames(runtime, "s2") == []


@pytest.mark.asyncio
async def test_a_session_whose_servers_are_unknown_blocks_delivery(monkeypatch):
    """A session with no report might hold the server unseen, so nobody gets the link."""
    runtime = _runtime(("s1", ["remote"]))
    runtime._session_queues["s2"] = asyncio.Queue()
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(6, _lazy())

    assert [kind for kind, _id, _msg in replies] == ["error"]
    assert _frames(runtime, "s1") == []


@pytest.mark.asyncio
async def test_a_mid_session_link_ignores_another_servers_sign_in_slot(monkeypatch):
    """The slot's server is not this link's server, so the link is not handed to it."""
    runtime = _runtime(("s1", ["a"]), ("s2", ["remote"]))
    replies = _answers(runtime, monkeypatch)
    runtime._mcp_sign_in = ("s1", "a")

    await runtime._answer_open_external_url(5, _lazy())

    assert replies == [("result", 5, {"success": True})]
    assert _frames(runtime, "s1") == []
    assert _frames(runtime, "s2")[0].params["serverName"] == "remote"


@pytest.mark.asyncio
async def test_a_link_with_no_server_name_keeps_the_slot_rule(monkeypatch):
    """An identity-provider sign-in link names no server: refused with no sign-in in flight."""
    runtime = _runtime(("s1", ["remote"]))
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, {"url": _URL, "identityProviderSignIn": True})

    assert replies == [("error", 5, "no MCP sign-in is in flight")]
    assert _frames(runtime, "s1") == []


@pytest.mark.asyncio
async def test_an_unsafe_mid_session_link_is_refused(monkeypatch):
    runtime = _runtime(("s1", ["remote"]))
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy(url="javascript:alert(1)"))

    assert [kind for kind, _id, _msg in replies] == ["error"]
    assert _frames(runtime, "s1") == []


@pytest.mark.asyncio
async def test_the_same_link_twice_in_one_flow_draws_one_card(monkeypatch):
    runtime = _runtime(("s1", ["remote"]))
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy())
    await runtime._answer_open_external_url(6, _lazy())

    assert replies == [("result", 5, {"success": True}), ("result", 6, {"success": True})]
    assert len(_frames(runtime, "s1")) == 1


@pytest.mark.asyncio
async def test_a_new_link_in_the_same_flow_is_delivered(monkeypatch):
    """The engine made a new code verifier for it, so only the newest link can finish."""
    runtime = _runtime(("s1", ["remote"]))
    _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy())
    await runtime._answer_open_external_url(6, _lazy(url=_URL_2))

    assert [f.params["oauthUrl"] for f in _frames(runtime, "s1")] == [_URL, _URL_2]


async def _complete(runtime: AcpRuntime, **params) -> None:
    from kiro_crew.acp import kas_mcp_lazy_sign_in

    await kas_mcp_lazy_sign_in.settle_mid_session_sign_in(runtime, params)


@pytest.mark.asyncio
async def test_a_completion_closes_the_card_of_the_session_shown_it(monkeypatch):
    runtime = _runtime(("s1", ["remote"]), ("s2", ["other"]))
    _answers(runtime, monkeypatch)
    await runtime._answer_open_external_url(5, _lazy())
    _frames(runtime, "s1")

    await _complete(runtime, serverName="remote", success=True, oauthFlowId="flow-1")
    await _complete(runtime, serverName="remote", success=True, oauthFlowId="flow-1")

    [frame] = _frames(runtime, "s1")
    assert frame.method == METHOD_MCP_SERVER_INITIALIZED
    assert frame.params == {"sessionId": "s1", "serverName": "remote"}
    assert _frames(runtime, "s2") == []
    payload = runtime._mcp_reports()["s1"].payload()
    assert payload["ready"] == ["remote"] and payload["awaiting_auth"] == []


@pytest.mark.asyncio
async def test_a_failed_sign_in_fails_the_card_and_leaves_the_server_awaiting(monkeypatch):
    runtime = _runtime(("s1", ["remote"]))
    _answers(runtime, monkeypatch)
    await runtime._answer_open_external_url(5, _lazy())
    _frames(runtime, "s1")

    await _complete(runtime, serverName="remote", success=False, oauthFlowId="flow-1")

    [frame] = _frames(runtime, "s1")
    assert frame.method == METHOD_MCP_SERVER_INIT_FAILURE
    assert frame.params["serverName"] == "remote"
    assert runtime._mcp_reports()["s1"].payload()["awaiting_auth"] == ["remote"]


@pytest.mark.asyncio
async def test_a_completion_matches_by_flow_id_before_server_name(monkeypatch):
    runtime = _runtime(("s1", ["remote"]))
    _answers(runtime, monkeypatch)
    await runtime._answer_open_external_url(5, _lazy())
    _frames(runtime, "s1")

    await _complete(runtime, serverName="remote", success=True, oauthFlowId="another-flow")
    assert _frames(runtime, "s1") == []

    await _complete(runtime, serverName="remote", success=True)
    assert [f.method for f in _frames(runtime, "s1")] == [METHOD_MCP_SERVER_INITIALIZED]


@pytest.mark.asyncio
async def test_a_flow_with_no_id_is_matched_by_server_name(monkeypatch):
    runtime = _runtime(("s1", ["remote"]))
    _answers(runtime, monkeypatch)
    await runtime._answer_open_external_url(5, _lazy(flow=None))
    await runtime._answer_open_external_url(6, _lazy(flow=None))
    assert len(_frames(runtime, "s1")) == 1

    await _complete(runtime, serverName="remote", success=True)
    assert [f.method for f in _frames(runtime, "s1")] == [METHOD_MCP_SERVER_INITIALIZED]


@pytest.mark.asyncio
async def test_the_reader_routes_the_link_and_its_end_to_the_holder_only():
    """Through the reader: the completion names no session, yet reaches only the holder."""
    from unittest.mock import AsyncMock, MagicMock

    from acp_runtime_helpers import (
        _drain,
        _feed,
        _RunningLoopStreamReader,
        _stop_reader,
    )

    runtime = _runtime(("s1", ["remote"]), ("s2", ["other"]))
    reader = _RunningLoopStreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    runtime._process = proc
    runtime._initialized = True
    task = asyncio.ensure_future(runtime._reader_loop())
    try:
        _feed(
            reader,
            {"jsonrpc": "2.0", "id": 11, "method": "_kiro/openExternalUrl", "params": _lazy()},
        )
        await _drain(reader)
        await asyncio.gather(*runtime._answer_tasks)
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "method": "_kiro/mcp/oauthCompleted",
                "params": {"serverName": "remote", "success": True, "oauthFlowId": "flow-1"},
            },
        )
        await _drain(reader)
    finally:
        await _stop_reader(task)

    assert [f.method for f in _frames(runtime, "s1")] == [
        METHOD_MCP_OAUTH_REQUEST,
        METHOD_MCP_SERVER_INITIALIZED,
    ]
    assert _frames(runtime, "s2") == []
    written = b"".join(call.args[0] for call in proc.stdin.write.call_args_list)
    assert b'"id": 11' in written or b'"id":11' in written
    assert b'"success"' in written
