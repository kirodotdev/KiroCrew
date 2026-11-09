"""``hooks.confirm_destructive_mcp_tools``: a destructive-annotated MCP tool asks under trust.

The MCP ``destructiveHint`` annotation, read from the probe's own ``tools/list``
answer, withholds the session-trust grant so the call reaches the approval card.
It only ever adds a prompt: a tool that also says ``readOnlyHint`` is never held,
yolo is untouched, and with the flag off (the default) nothing changes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew.acp.types import EVENT_SUBAGENT_LIST
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_turn import tool_approval
from kiro_crew.hooks import HookManager, HooksConfig
from kiro_crew.mcp_discovery import McpServerInfo, _cache_probe, _probe_cache, _probe_remote
from kiro_crew.mcp_discovery_probe_state import destructive_tool_names, is_destructive_mcp_tool
from kiro_crew.providers.base import LLMEvent

_TOOLS = [
    {"name": "delete_repo", "annotations": {"destructiveHint": True}},
    {"name": "list_repos", "annotations": {"readOnlyHint": True}},
    # Contradictory: read-only wins, so this flag never asks for it.
    {"name": "peek", "annotations": {"readOnlyHint": True, "destructiveHint": True}},
    {"name": "plain"},
    {"name": "spoof", "annotations": {"destructiveHint": "true"}},
]


@pytest.fixture(autouse=True)
def _seeded_probe():
    _probe_cache.clear()
    server = McpServerInfo(
        name="gh",
        command="x",
        status="ok",
        tools=[t["name"] for t in _TOOLS],
        error="",
        destructive_tools=destructive_tool_names(_TOOLS),
    )
    _cache_probe(server)
    yield
    _probe_cache.clear()


def _event(tool: str, server: str = "gh") -> SimpleNamespace:
    return SimpleNamespace(mcp_server_name=server, tool_name=tool)


def _flag(monkeypatch, on: bool) -> None:
    # ``chat_runner`` runs the turn's helpers under its own globals, so the reader
    # is replaced there as well as in the module that defines it.
    for mod in (tool_approval, chat_runner):
        monkeypatch.setattr(
            mod,
            "_hooks_config",
            lambda: HooksConfig.from_dict({"confirm_destructive_mcp_tools": on}),
        )


def test_only_a_strict_true_destructive_hint_without_read_only_counts() -> None:
    assert destructive_tool_names(_TOOLS) == ["delete_repo"]
    assert is_destructive_mcp_tool("gh", "delete_repo")
    assert not is_destructive_mcp_tool("gh", "list_repos")
    assert not is_destructive_mcp_tool("other", "delete_repo")


def test_destructive_tool_under_trust_asks_only_when_flag_on(monkeypatch) -> None:
    _flag(monkeypatch, False)
    assert tool_approval._trust_grant_withheld(_event("delete_repo")) is False
    _flag(monkeypatch, True)
    assert tool_approval._trust_grant_withheld(_event("delete_repo")) is True


@pytest.mark.parametrize("tool", ["list_repos", "peek", "plain", "spoof"])
def test_read_only_or_unannotated_tool_never_asks_because_of_flag(monkeypatch, tool) -> None:
    _flag(monkeypatch, True)
    assert tool_approval._trust_grant_withheld(_event(tool)) is False


def test_builtin_or_unprobed_server_is_unaffected(monkeypatch) -> None:
    _flag(monkeypatch, True)
    assert tool_approval._trust_grant_withheld(_event("delete_repo", server="")) is False
    assert tool_approval._trust_grant_withheld(_event("delete_repo", server="nope")) is False


def test_flag_defaults_off_and_round_trips() -> None:
    assert HooksConfig.from_dict({}).confirm_destructive_mcp_tools is False
    assert (
        HooksConfig.from_dict(
            {"confirm_destructive_mcp_tools": "yes"}
        ).confirm_destructive_mcp_tools
        is False
    )
    on = HooksConfig.from_dict({"confirm_destructive_mcp_tools": True})
    assert on.to_dict()["confirm_destructive_mcp_tools"] is True


def test_probe_failure_keeps_the_last_destructive_set() -> None:
    _cache_probe(McpServerInfo(name="gh", command="x", status="error", tools=[], error="timeout"))
    assert is_destructive_mcp_tool("gh", "delete_repo")


# ── the trust rung of a dashboard turn ────────────────────────────────────


def _destructive_call():
    from chat_runner_coverage_helpers import _permission

    return _permission(
        title="Running: @gh/delete_repo",
        tool_input=json.dumps({"repo": "demo"}),
        tool_kind="",
        is_shell=False,
        tool_name="delete_repo",
        mcp_server_name="gh",
        mcp_identity_trusted=True,
    )


def _card_outcomes(slot) -> list:
    return [
        json.loads(m["cls"]).get("resolved") for m in slot.messages if m.get("role") == "permission"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("on,asks", [(False, False), (True, True)])
async def test_trusted_session_shows_the_card_only_with_the_flag(monkeypatch, tmp_path, on, asks):
    from chat_runner_coverage_helpers import (
        _answer_card,
        _complete,
        _drive,
        _runner_state,
        _set_stream,
        _slot,
    )

    _flag(monkeypatch, on)
    state, client = _runner_state(tmp_path)
    slot = _slot()
    slot._trust = True
    state.push_slots_update.side_effect = lambda: _answer_card(slot, "rejected")
    _set_stream(client, [_destructive_call(), _complete()])

    await _drive(state, slot)

    if asks:
        client.approve_tool.assert_not_awaited()
        assert _card_outcomes(slot) == ["rejected"]
    else:
        client.approve_tool.assert_awaited_once()
        assert _card_outcomes(slot) == []


@pytest.mark.asyncio
async def test_yolo_still_approves_a_destructive_tool(monkeypatch, tmp_path):
    from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream, _slot

    _flag(monkeypatch, True)
    state, client = _runner_state(tmp_path)
    state.enable_yolo()
    slot = _slot()
    slot._trust = True
    _set_stream(client, [_destructive_call(), _complete()])

    await _drive(state, slot)

    client.approve_tool.assert_awaited_once()
    assert _card_outcomes(slot) == []


# ── the native-crew rung ──────────────────────────────────────────────────


def _crew_active() -> LLMEvent:
    """A native sub-agent list naming one running sub-agent with a task."""
    return LLMEvent(
        kind=EVENT_SUBAGENT_LIST,
        subagents=[
            {
                "sessionId": "child-1",
                "role": "worker",
                "initialQuery": "tidy the repos",
                "status": {"type": "working"},
            }
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("on,asks", [(False, False), (True, True)])
async def test_active_crew_does_not_let_trust_skip_the_card(monkeypatch, tmp_path, on, asks):
    from chat_runner_coverage_helpers import (
        _answer_card,
        _complete,
        _drive,
        _runner_state,
        _set_stream,
        _slot,
    )

    _flag(monkeypatch, on)
    state, client = _runner_state(tmp_path)
    slot = _slot()
    slot._trust = True
    state.push_slots_update.side_effect = lambda: _answer_card(slot, "rejected")
    _set_stream(client, [_crew_active(), _destructive_call(), _complete()])

    await _drive(state, slot)

    if asks:
        client.approve_tool.assert_not_awaited()
        assert _card_outcomes(slot) == ["rejected"]
    else:
        client.approve_tool.assert_awaited_once()
        assert _card_outcomes(slot) == []


@pytest.mark.asyncio
async def test_active_crew_with_yolo_still_approves_a_destructive_tool(monkeypatch, tmp_path):
    from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream, _slot

    _flag(monkeypatch, True)
    state, client = _runner_state(tmp_path)
    state.enable_yolo()
    slot = _slot()
    slot._trust = True
    _set_stream(client, [_crew_active(), _destructive_call(), _complete()])

    await _drive(state, slot)

    client.approve_tool.assert_awaited_once()
    assert _card_outcomes(slot) == []


@pytest.mark.asyncio
async def test_active_crew_with_subagent_auto_approve_still_approves(monkeypatch, tmp_path):
    """The independent ``auto_approve_subagent_tools`` grant is not a trust grant."""
    from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream, _slot

    _flag(monkeypatch, True)
    builder = MagicMock()
    builder.hooks = HookManager(HooksConfig.from_dict({"auto_approve_subagent_tools": True}))
    builder.build_message.return_value = ("hello", None)
    state, client = _runner_state(tmp_path, context_builder=builder)
    slot = _slot()
    _set_stream(client, [_crew_active(), _destructive_call(), _complete()])

    await _drive(state, slot)

    client.approve_tool.assert_awaited_once()
    assert _card_outcomes(slot) == []


# ── the remote (HTTP) probe ───────────────────────────────────────────────


def _http_reply(body: dict | None, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.content_type = "application/json"
    resp.headers = {}
    resp.json = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
async def test_remote_probe_records_the_destructive_tool_names() -> None:
    _probe_cache.clear()
    server = McpServerInfo(name="remote-gh", url="https://example.com/mcp")
    replies = [
        _http_reply({"jsonrpc": "2.0", "id": 1, "result": {}}),
        _http_reply(None, status=202),
        _http_reply({"jsonrpc": "2.0", "id": 2, "result": {"tools": _TOOLS}}),
    ]
    session = MagicMock()
    session.post = MagicMock(side_effect=replies)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=session):
        result = await _probe_remote(server)

    try:
        assert result.status == "ok"
        assert result.destructive_tools == ["delete_repo"]
        assert is_destructive_mcp_tool("remote-gh", "delete_repo")
        assert not is_destructive_mcp_tool("remote-gh", "list_repos")
    finally:
        _probe_cache.clear()
