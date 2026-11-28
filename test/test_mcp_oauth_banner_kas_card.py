"""A KAS chat's sign-in request is card-owned only when the card signs in on KAS.

A KAS session reads remote sign-ins from Crew's grant vault, never kiro-cli's
cache. Connect writes the vault only when ``mint_kas.kas_sign_in_entry`` answers;
otherwise it mints on kiro-cli, and the chat's own banner is the one way that
KAS chat can sign in, so it must not be tagged for the render layer to hide.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_mcp_oauth_banner import CARDED_SLUG, _own, _pending

from kiro_crew import mcp_grant_kas
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.connections import mint_kas
from kiro_crew.connections import status as connections_status
from kiro_crew.dashboard.chat_runner import (
    _drain_session_init_oauth_requests,
    _on_mcp_oauth_request,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.providers.acp import AcpProvider

PREREGISTERED_SLUG = "github"
BARE = {"url": "https://mcp.notion.com/mcp"}
PUBLIC_OAUTH = {"url": "https://mcp.notion.com/mcp", "oauth": {"clientId": "public-client"}}


@pytest.fixture
def kas_chats(tmp_path, monkeypatch):
    """Chats on KAS with persistence on; returns a writer for the main spec's servers."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: agents_dir)
    monkeypatch.setattr(connections_status, "chat_backend_is_kas", lambda: True)
    monkeypatch.setattr(mcp_grant_kas, "persistence_enabled", lambda: True)

    def write_spec(servers: dict) -> None:
        (agents_dir / "kirocrew.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8"
        )
        _own(tmp_path, monkeypatch, servers)
        # The spec read this test controls is the one the predicate makes.
        assert mint_kas._configured_servers() == servers

    return write_spec


def _kas_session(*names):
    client = _pending(*names)
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    return client


async def _tag(client, server: str = CARDED_SLUG) -> bool:
    slot = _ChatSlot("s1")
    await _drain_session_init_oauth_requests(MagicMock(), slot, client)
    assert len(slot.messages) == 1, "every request is emitted, tagged or not"
    assert slot.messages[0]["meta"]["server_name"] == server
    return bool(slot.messages[0]["meta"].get("card_owned"))


@pytest.mark.asyncio
async def test_card_that_signs_in_on_kas_owns_the_kas_request(kas_chats):
    kas_chats({CARDED_SLUG: BARE})
    assert await _tag(_kas_session(CARDED_SLUG)) is True


@pytest.mark.asyncio
async def test_card_minting_on_kiro_cli_leaves_a_bot_chat_on_kas_its_banner(kas_chats, monkeypatch):
    """Ordinary chats on Kiro CLI put Bot chats on KAS while Connect mints on kiro-cli."""
    kas_chats({CARDED_SLUG: BARE})
    monkeypatch.setattr(connections_status, "chat_backend_is_kas", lambda: False)
    assert await _tag(_kas_session(CARDED_SLUG)) is False


@pytest.mark.asyncio
async def test_entry_with_an_oauth_block_leaves_the_kas_chat_its_banner(kas_chats):
    kas_chats({CARDED_SLUG: PUBLIC_OAUTH})
    assert await _tag(_kas_session(CARDED_SLUG)) is False


@pytest.mark.asyncio
async def test_persistence_off_leaves_the_kas_chat_its_banner(kas_chats, monkeypatch):
    kas_chats({CARDED_SLUG: BARE})
    monkeypatch.setattr(mcp_grant_kas, "persistence_enabled", lambda: False)
    assert await _tag(_kas_session(CARDED_SLUG)) is False


@pytest.mark.asyncio
async def test_preregistered_provider_leaves_the_kas_chat_its_banner(kas_chats):
    kas_chats({PREREGISTERED_SLUG: {"url": "https://api.githubcopilot.com/mcp/"}})
    client = _kas_session(PREREGISTERED_SLUG)
    assert await _tag(client, PREREGISTERED_SLUG) is False


@pytest.mark.asyncio
async def test_unreadable_spec_leaves_the_kas_chat_its_banner(kas_chats, monkeypatch):
    kas_chats({CARDED_SLUG: BARE})

    def _broken():
        raise OSError("spec unreadable")

    monkeypatch.setattr(mint_kas, "_configured_servers", _broken)
    assert await _tag(_kas_session(CARDED_SLUG)) is False


@pytest.mark.asyncio
async def test_non_kas_session_keeps_the_membership_tag(kas_chats, monkeypatch):
    kas_chats({CARDED_SLUG: PUBLIC_OAUTH})
    monkeypatch.setattr(connections_status, "chat_backend_is_kas", lambda: False)
    assert await _tag(_pending(CARDED_SLUG)) is True


class _KasClient:
    backend = ACP_BACKEND_KAS
    process_instance = "child-1"
    session_id = "sess-1"

    def __init__(self, names):
        self._pending = [
            {"serverName": n, "oauthUrl": f"https://{n}.example.com/authorize?client_id=x"}
            for n in names
        ]

    def pop_pending_oauth_requests(self):
        pending, self._pending = self._pending, []
        return pending


@pytest.mark.asyncio
@pytest.mark.parametrize("kas_mint", [True, False], ids=["card-on-kas", "card-on-kiro-cli"])
async def test_real_provider_reports_its_kas_backend(kas_chats, monkeypatch, kas_mint):
    kas_chats({CARDED_SLUG: BARE})
    monkeypatch.setattr(connections_status, "chat_backend_is_kas", lambda: kas_mint)
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = _KasClient([CARDED_SLUG])
    assert await _tag(provider) is kas_mint


class _MidTurnKasClient:
    capabilities = capabilities_for(ACP_BACKEND_KAS)
    process_instance = "child-1"

    def mcp_session_report(self):
        return None

    def set_mcp_sign_in_ended_hook(self, hook):
        self.ended_hook = hook

    def mcp_sign_in_ended(self, server_name):
        return False


class _State:
    def broadcast_ws(self, kind: str, payload: dict) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("kas_mint", [True, False], ids=["card-on-kas", "card-on-kiro-cli"])
async def test_mid_turn_request_is_tagged_like_the_drain(kas_chats, monkeypatch, kas_mint):
    kas_chats({CARDED_SLUG: BARE})
    monkeypatch.setattr(connections_status, "chat_backend_is_kas", lambda: kas_mint)
    slot = _ChatSlot("s1")
    turn = SimpleNamespace(client=_MidTurnKasClient(), slot=slot, state=_State())
    event = SimpleNamespace(
        server_name=CARDED_SLUG,
        oauth_url=f"https://{CARDED_SLUG}.example.com/authorize?client_id=x",
        kind="mcp_oauth_request",
        runtime_global=False,
    )
    await _on_mcp_oauth_request(turn, event)
    assert len(slot.messages) == 1
    assert bool(slot.messages[0]["meta"].get("card_owned")) is kas_mint
