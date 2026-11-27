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
        _register(runtime, session_id, servers)
    return runtime


def _register(runtime: AcpRuntime, session_id: str, servers: list[str], *, heard=True) -> None:
    """Register a session whose host status snapshot reported *servers* connected."""
    runtime._session_queues[session_id] = asyncio.Queue()
    runtime._open_mcp_report(session_id, [{"name": name} for name in servers])
    if heard:
        runtime._fold_mcp_frame(
            session_id,
            JsonRpcMessage(
                method=METHOD_KAS_MCP_STATUS,
                params={
                    "sessionId": session_id,
                    "servers": [{"name": name, "status": "connected"} for name in servers],
                },
            ),
        )


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
    # Without an id it may be another connection's wait: it settles no flow that has one.
    await _complete(runtime, serverName="remote", success=True)
    assert _frames(runtime, "s1") == []

    await _complete(runtime, serverName="remote", success=True, oauthFlowId="flow-1")
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


# ── what might hold the server unseen fails delivery closed ──────────────────


def _start_in_flight(runtime: AcpRuntime) -> None:
    runtime._session_inits_in_flight = 1


def _start_collector(runtime: AcpRuntime) -> None:
    runtime._start_collectors[99] = object()


def _unconfirmed_teardown(runtime: AcpRuntime) -> None:
    runtime._unconfirmed_teardowns.add("gone")


def _subagent(runtime: AcpRuntime) -> None:
    runtime._subagent_sessions.add("child")
    runtime._subagent_owner = "s1"


def _roster_overflow(runtime: AcpRuntime) -> None:
    runtime._subagent_roster_overflow = 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unseen",
    [_start_in_flight, _start_collector, _unconfirmed_teardown, _subagent, _roster_overflow],
    ids=[
        "session-new-or-load",
        "start-collector",
        "unconfirmed-teardown",
        "subagent",
        "roster-overflow",
    ],
)
async def test_an_engine_session_with_no_queue_here_blocks_delivery(monkeypatch, unseen):
    """Each could hold the server with no report here, so nobody gets the link."""
    runtime = _runtime(("s1", ["remote"]))
    replies = _answers(runtime, monkeypatch)
    unseen(runtime)

    await runtime._answer_open_external_url(5, _lazy())

    assert replies == [("error", 5, "a session this runtime cannot see may hold that MCP server")]
    assert _frames(runtime, "s1") == []


@pytest.mark.asyncio
async def test_a_capped_report_elsewhere_blocks_delivery(monkeypatch):
    runtime = _runtime(("s1", ["remote"]), ("s2", ["other"]))
    runtime._mcp_reports()["s2"]._cut["reports"] = 1
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy())

    assert [kind for kind, _id, _msg in replies] == ["error"]


@pytest.mark.asyncio
async def test_a_name_with_control_characters_is_refused(monkeypatch):
    runtime = _runtime(("s1", ["remote"]))
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy(server="remote\x07"))

    assert replies == [("error", 5, "the MCP server name is not a plain name")]


@pytest.mark.asyncio
async def test_a_connection_status_link_with_a_server_name_keeps_the_slot_rule(monkeypatch):
    runtime = _runtime(("s1", ["remote"]))
    replies = _answers(runtime, monkeypatch)
    params = {**_lazy(), "oauthCompletion": "connection-status"}

    await runtime._answer_open_external_url(5, params)
    assert replies == [("error", 5, "no MCP sign-in is in flight")]

    runtime._mcp_sign_in = ("s1", "remote")
    await runtime._answer_open_external_url(6, params)
    assert replies[-1] == ("result", 6, {"success": True})
    assert len(_frames(runtime, "s1")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("heard", [False, True], ids=["unheard", "wire-roster-only"])
async def test_a_session_that_may_hold_the_server_unseen_blocks_delivery(monkeypatch, heard):
    """No host frame yet, or the server is only on the wire Crew sent: not proof either way."""
    runtime = _runtime(("s1", ["remote"]))
    _register(runtime, "s2", [], heard=heard)
    runtime._mcp_reports()["s2"].begin_session([{"name": "remote"}])
    if heard:
        runtime._fold_mcp_frame(
            "s2",
            JsonRpcMessage(method=METHOD_KAS_MCP_STATUS, params={"sessionId": "s2", "servers": []}),
        )
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy())

    assert [kind for kind, _id, _msg in replies] == ["error"]
    assert _frames(runtime, "s1") == [] and _frames(runtime, "s2") == []


@pytest.mark.asyncio
async def test_a_name_the_report_would_cut_is_refused(monkeypatch):
    """A stored name at the cap may be a longer server's name cut short."""
    long_name = "r" * 128
    runtime = _runtime(("s1", [long_name]))
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy(server=long_name))

    assert replies == [("error", 5, "the MCP server name is not a plain name")]


@pytest.mark.asyncio
async def test_a_child_of_another_session_blocks_delivery(monkeypatch):
    runtime = _runtime(("s1", ["remote"]), ("s2", ["other"]))
    runtime._announced_children["child-of-s2"] = "s2"
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy())

    assert [kind for kind, _id, _msg in replies] == ["error"]
    assert _frames(runtime, "s1") == []


@pytest.mark.asyncio
async def test_a_link_during_an_explicit_sign_in_of_that_server_goes_to_its_slot(monkeypatch):
    """The engine tags a link opened after the handshake ``notification`` even then."""
    runtime = _runtime(("s1", ["other"]), ("s2", ["remote"]))
    replies = _answers(runtime, monkeypatch)
    runtime._mcp_sign_in = ("s2", "remote")

    await runtime._answer_open_external_url(5, _lazy())

    assert replies == [("result", 5, {"success": True})]
    assert _frames(runtime, "s1") == []
    assert len(_frames(runtime, "s2")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("s2_servers", [["remote"], ["other"]], ids=["both-hold-it", "s2-lacks-it"])
async def test_a_link_during_a_sign_in_the_holder_is_not_running_is_refused(
    monkeypatch, s2_servers
):
    """The link may be either session's, so neither gets it, nor its completion."""
    runtime = _runtime(("s1", ["remote"]), ("s2", s2_servers))
    replies = _answers(runtime, monkeypatch)
    runtime._mcp_sign_in = ("s2", "remote")

    await runtime._answer_open_external_url(5, _lazy())
    await _complete(runtime, serverName="remote", success=True, oauthFlowId="flow-1")

    assert [kind for kind, _id, _msg in replies] == ["error"]
    assert _frames(runtime, "s1") == [] and _frames(runtime, "s2") == []


# ── a completion reaches only the queue that was shown the link ──────────────


@pytest.mark.asyncio
async def test_a_completion_for_a_session_that_left_reaches_no_new_queue(monkeypatch):
    runtime = _runtime(("s1", ["remote"]))
    _answers(runtime, monkeypatch)
    await runtime._answer_open_external_url(5, _lazy())
    runtime.unregister_session("s1")
    _register(runtime, "s1", ["remote"])

    await _complete(runtime, serverName="remote", success=True, oauthFlowId="flow-1")

    assert _frames(runtime, "s1") == []


# ── a replacing link reaches the chat ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_link_in_a_flow_passes_the_handles_one_link_per_server_rule(monkeypatch):
    """The handle shows one link per server until the sign-in ends; the new one must pass."""
    from types import SimpleNamespace

    from kiro_crew.acp import session_handle_mcp

    runtime = _runtime(("s1", ["remote"]))
    _answers(runtime, monkeypatch)
    await runtime._answer_open_external_url(5, _lazy())
    await runtime._answer_open_external_url(6, _lazy(url=_URL_2))
    first, second = _frames(runtime, "s1")
    handle = SimpleNamespace(_oauth_emitted_servers=set())

    shown = [session_handle_mcp._accept_oauth_request(handle, f) for f in (first, first, second)]

    assert [s and s["oauthUrl"] for s in shown] == [_URL, None, _URL_2]


@pytest.mark.asyncio
async def test_a_server_only_on_the_wire_is_not_proof_of_holding_it(monkeypatch):
    """Crew sent it, but no host frame says this session's engine mounted it."""
    runtime = _runtime(("s1", []))
    runtime._mcp_reports()["s1"].configured = ("remote",)
    replies = _answers(runtime, monkeypatch)

    await runtime._answer_open_external_url(5, _lazy())

    assert [kind for kind, _id, _msg in replies] == ["error"]
    assert _frames(runtime, "s1") == []


# ── an open mid-session sign-in counts as live, so its card keeps its link ───


@pytest.mark.asyncio
async def test_an_open_mid_session_sign_in_is_live_until_it_ends(monkeypatch):
    """The chat withdraws a link whose sign-in ended (``mcp_sign_in_ended``)."""
    from types import SimpleNamespace

    from kiro_crew.acp.session_provider import AcpSessionProvider

    runtime = _runtime(("s1", ["remote"]), ("s2", ["other"]))
    _answers(runtime, monkeypatch)
    provider = AcpSessionProvider.__new__(AcpSessionProvider)
    provider._handle = SimpleNamespace(session_id="s1")
    provider._runtime = runtime
    assert provider.mcp_sign_in_ended("remote") is True

    await runtime._answer_open_external_url(5, _lazy())

    assert runtime.mcp_sign_in_holds("s1", "remote") is True
    assert provider.mcp_sign_in_ended("remote") is False
    assert runtime.mcp_sign_in_holds("s2", "remote") is False
    assert runtime.mcp_sign_in_holds("s1", "other") is False
    await _complete(runtime, serverName="remote", success=True, oauthFlowId="flow-1")
    assert provider.mcp_sign_in_ended("remote") is True


@pytest.mark.asyncio
async def test_a_flow_past_the_cap_is_forgotten_with_a_log_line(monkeypatch, caplog):
    from kiro_crew.acp import kas_mcp_lazy_sign_in

    runtime = _runtime(("s1", ["remote"]))
    _answers(runtime, monkeypatch)
    monkeypatch.setattr(kas_mcp_lazy_sign_in, "_FLOW_CAP", 2)
    caplog.set_level("INFO", logger="kiro_crew.acp.runtime")

    for n in range(3):
        await runtime._answer_open_external_url(n, _lazy(url=f"{_URL}-{n}", flow=f"flow-{n}"))

    assert list(runtime._mcp_lazy_sign_in_flows) == ["flow-1", "flow-2"]
    assert "forgotten past 2 open ones" in caplog.text
    assert all(len(f.url_digest) == 64 for f in runtime._mcp_lazy_sign_in_flows.values())
