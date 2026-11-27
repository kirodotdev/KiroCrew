"""A Claude chat on an old claude-agent-acp, refused for a held project MCP server.

Below ``CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION`` the adapter may ignore the
``disabledMcpjsonServers`` hold, so the chat is refused. When the hold is the only
reason, the refusal names the cards and the chat raises them, so the person can
allow the servers and send again instead of meeting a dead end.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.held_project_refusal import ClaudeHeldProjectServersUnroutable
from kiro_crew.dashboard.project_mcp_cards import offer_cards_for_refused_start
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE


@pytest.fixture
def folder(tmp_path, monkeypatch) -> Path:
    crew = tmp_path / "crew-home"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    settled = threading.Event()
    settled.set()
    monkeypatch.setattr(approvals, "_MIGRATION_SETTLED", settled)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps({"mcpServers": {"weather": {"command": "node", "args": ["w.js"]}}}),
        encoding="utf-8",
    )
    return repo


async def _held_client(folder: Path) -> AcpClient:
    client = AcpClient(work_dir=folder, acp_backend=ACP_BACKEND_CLAUDE)
    await client._prepare_claude_held_asks()
    assert client._claude_held_project_servers == ("weather",)
    return client


@pytest.mark.asyncio
async def test_a_hold_alone_is_refused_with_the_way_forward_for_a_surface_without_cards(
    folder,
):
    # Slack and the other channels show this text and raise no card, so it names
    # the one place that does, beside the upgrade.
    client = await _held_client(folder)

    with pytest.raises(ClaudeHeldProjectServersUnroutable) as refused:
        client._refuse_claude_session_below_floor_before_spawn("0.83.0")

    exc = refused.value
    assert exc.user_worded is True and exc.transient is False
    assert str(exc) == (
        "claude-agent-acp 0.83.0 can't hold back the .mcp.json servers you haven't "
        "allowed (0.84.0 or newer can), so this chat didn't start. Allow them in a "
        "dashboard Claude Code chat on the same folder, or upgrade: "
        "npm i -g @agentclientprotocol/claude-agent-acp"
    )
    assert "cards" not in str(exc)
    assert (exc.backend, exc.folder) == (ACP_BACKEND_CLAUDE, str(folder))

    shown = exc.with_cards_shown(every_server=True)
    assert isinstance(shown, ClaudeHeldProjectServersUnroutable) and shown.user_worded
    assert (shown.backend, shown.agent, shown.folder) == (exc.backend, exc.agent, exc.folder)
    assert "Allow them on the cards above, then send your message again." in str(shown)
    assert "npm i -g" in str(shown)


@pytest.mark.asyncio
async def test_a_hold_beside_another_need_keeps_the_upgrade_refusal(folder):
    # Allowing the server would not start this chat, so no card is named.
    client = await _held_client(folder)
    client._spec_zero_tools = True

    with pytest.raises(AcpToolGateUnroutable) as refused:
        client._refuse_claude_session_below_floor_before_spawn("0.83.0")

    assert not isinstance(refused.value, ClaudeHeldProjectServersUnroutable)
    assert "cards" not in str(refused.value)


@pytest.mark.asyncio
async def test_the_refused_start_raises_the_held_servers_cards(folder, monkeypatch):
    from kiro_crew.dashboard.handlers import change_cards_propose

    proposed: list[tuple[str, str, dict]] = []

    async def propose(_state, slot_key, sk, *, params, **_kw):
        proposed.append((slot_key, sk, params))
        return {}

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    client = await _held_client(folder)
    with pytest.raises(ClaudeHeldProjectServersUnroutable) as refused:
        client._refuse_claude_session_below_floor_before_spawn("0.83.0")
    slot = SimpleNamespace(key="chat-1", project=str(folder))

    shown = await offer_cards_for_refused_start(object(), slot, "sk-1", refused.value)

    assert "on the cards above" in str(shown)
    [(slot_key, sk, params)] = proposed
    assert (slot_key, sk, params["server"]) == ("chat-1", "sk-1", "weather")

    # Once allowed, nothing is held and the same chat starts on the old adapter.
    [server] = approvals.pending_servers(folder)
    approvals.approve(server.folder, server.file, server.name, server.fingerprint)
    await client._prepare_claude_held_asks()
    client._refuse_claude_session_below_floor_before_spawn("0.83.0")


@pytest.mark.asyncio
async def test_another_refusal_raises_no_card(folder, monkeypatch):
    from kiro_crew.dashboard.handlers import change_cards_propose

    proposed: list[object] = []

    async def propose(*_a, **_kw):
        proposed.append(1)
        return {}

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    slot = SimpleNamespace(key="chat-1", project=str(folder))

    assert (
        await offer_cards_for_refused_start(object(), slot, "sk-1", AcpToolGateUnroutable("x"))
        is None
    )
    assert await offer_cards_for_refused_start(object(), slot, "sk-1", RuntimeError("x")) is None

    assert proposed == []


@pytest.mark.asyncio
async def test_no_card_raised_keeps_the_wording_without_cards(folder, monkeypatch):
    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers import change_cards_propose

    async def refuse(*_a, **_kw):
        raise CardError(413, "card_too_large", "too large")

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", refuse)
    monkeypatch.setattr("kiro_crew.dashboard.state.append_and_surface", lambda *a, **k: None)
    client = await _held_client(folder)
    with pytest.raises(ClaudeHeldProjectServersUnroutable) as refused:
        client._refuse_claude_session_below_floor_before_spawn("0.83.0")
    slot = SimpleNamespace(key="chat-1", project=str(folder))

    assert await offer_cards_for_refused_start(object(), slot, "sk-1", refused.value) is None


@pytest.mark.asyncio
async def test_a_chat_turn_refused_for_a_held_server_shows_its_card(folder, tmp_path, monkeypatch):
    """The turn whose start the old adapter refuses raises the card, then the refusal."""
    import asyncio

    from test_chat_runner_startup_recall import _turn_state

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.handlers import change_cards_propose

    proposed: list[str] = []

    async def propose(*_a, params, **_kw):
        proposed.append(params["server"])
        return {}

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    client = await _held_client(folder)
    with pytest.raises(ClaudeHeldProjectServersUnroutable) as refused:
        client._refuse_claude_session_below_floor_before_spawn("0.83.0")
    state, _provider = _turn_state(tmp_path, monkeypatch, registered=False)
    monkeypatch.setattr(chat_runner, "prefetch_session_memory", lambda *a, **k: None)
    state.sessions.get_or_create = AsyncMock(side_effect=refused.value)
    slot = state.get_or_create_slot("old-adapter")
    slot.append("user", "hello")

    await asyncio.wait_for(chat_runner._run_chat(state, slot, "hello"), 60)

    assert proposed == ["weather"]
    rows = [str(m.get("content")) for m in slot.messages if m.get("role") == "error"]
    assert any("Allow them on the cards above" in row for row in rows), rows


@pytest.mark.asyncio
async def test_a_card_still_waiting_counts_as_shown(folder, monkeypatch):
    # A second refused send finds the first card still pending; it is drawn again.
    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers import change_cards_propose

    async def waiting(*_a, **_kw):
        raise CardError(409, "duplicate_card", "shown again at the end of the chat")

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", waiting)
    client = await _held_client(folder)
    with pytest.raises(ClaudeHeldProjectServersUnroutable) as refused:
        client._refuse_claude_session_below_floor_before_spawn("0.83.0")
    slot = SimpleNamespace(key="chat-1", project=str(folder))

    shown = await offer_cards_for_refused_start(object(), slot, "sk-1", refused.value)

    assert "on the cards above" in str(shown)


@pytest.mark.asyncio
async def test_a_server_left_with_only_a_notice_is_named_beside_the_cards(folder, monkeypatch):
    # Allowing the one card would not start the chat: the other server is still
    # held, so the text points at the notice too.
    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers import change_cards_propose

    (folder / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps(
            {
                "mcpServers": {
                    "weather": {"command": "node", "args": ["w.js"]},
                    "huge": {"command": "node", "args": ["h.js"]},
                }
            }
        ),
        encoding="utf-8",
    )

    async def propose(*_a, params, **_kw):
        if params["server"] == "huge":
            raise CardError(413, "card_too_large", "too large")
        return {}

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    monkeypatch.setattr("kiro_crew.dashboard.state.append_and_surface", lambda *a, **k: None)
    client = AcpClient(work_dir=folder, acp_backend=ACP_BACKEND_CLAUDE)
    await client._prepare_claude_held_asks()
    with pytest.raises(ClaudeHeldProjectServersUnroutable) as refused:
        client._refuse_claude_session_below_floor_before_spawn("0.83.0")
    slot = SimpleNamespace(key="chat-1", project=str(folder))

    shown = await offer_cards_for_refused_start(object(), slot, "sk-1", refused.value)

    assert "Allow or fix the servers named above, then send your message again." in str(shown)
    assert "on the cards above" not in str(shown)
