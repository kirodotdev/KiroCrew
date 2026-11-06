"""The mid-turn ``mcp_oauth`` arm tags carded providers like the init drain does.

kiro-cli retries a signed-out server's sign-in on every turn, so a provider
with a Connections card (Linear, Notion, ...) reaches ``_on_mcp_oauth_request``
on every turn of every session. Without the ``card_owned`` tag the chat renders
its banner each time, beside the card that already shows the same Authorize
action.

The tag needs a session that can use the card's grant. The card's Connect
leaves its sign-in where kiro-cli reads it, so a KAS chat's own request for the
same server is never tagged: hiding it would leave that chat no way to sign in.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import mcp_discovery
from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _mark_mcp_oauth_completed, _on_mcp_oauth_request
from kiro_crew.dashboard.chat_utils import _expire_dead_child_oauth_meta
from kiro_crew.dashboard.state import _ChatSlot

CARDED_SLUG = "linear"
CARDED_URL = "https://mcp.linear.app/mcp/readonly"


def _own(tmp_path, monkeypatch, servers) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (path,))


class _State:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, dict]] = []

    def broadcast_ws(self, kind: str, payload: dict) -> None:
        self.broadcasts.append((kind, payload))


class _Client:
    process_instance = "child-1"

    def __init__(self, backend: str = ACP_BACKEND_KIRO) -> None:
        self.capabilities = capabilities_for(backend)

    def mcp_session_report(self):
        return None


def _turn(slot: _ChatSlot, backend: str = ACP_BACKEND_KIRO):
    return SimpleNamespace(client=_Client(backend), slot=slot, state=_State())


def _event(server_name: str):
    return SimpleNamespace(
        server_name=server_name,
        oauth_url=f"https://{server_name}.example.com/authorize?client_id=x",
        kind="mcp_oauth_request",
        runtime_global=False,
    )


@pytest.mark.asyncio
async def test_carded_provider_is_tagged_mid_turn(tmp_path, monkeypatch):
    _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": CARDED_URL}})
    slot = _ChatSlot("s1")
    await _on_mcp_oauth_request(_turn(slot), _event(CARDED_SLUG))
    assert len(slot.messages) == 1
    meta = slot.messages[0]["meta"]
    assert meta["card_owned"] is True
    # The card reads its approval URL from this message, so it must survive.
    assert meta["oauth_url"] == f"https://{CARDED_SLUG}.example.com/authorize?client_id=x"


@pytest.mark.asyncio
async def test_custom_server_is_not_tagged_mid_turn(tmp_path, monkeypatch):
    """A hand-added server has no card, so chat stays its only prompt."""
    _own(tmp_path, monkeypatch, {"my-remote": {"url": "https://mine.example.com/mcp"}})
    slot = _ChatSlot("s1")
    await _on_mcp_oauth_request(_turn(slot), _event("my-remote"))
    assert len(slot.messages) == 1
    assert "card_owned" not in slot.messages[0]["meta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False], ids=["signed-in", "sign-in-failed"])
async def test_terminal_banner_keeps_the_tag(tmp_path, monkeypatch, success):
    """Signed in or failed, the card shows the verdict, so chat keeps hiding it."""
    _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": CARDED_URL}})
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event(CARDED_SLUG))
    _mark_mcp_oauth_completed(turn.state, slot, CARDED_SLUG, success=success, error="denied")
    meta = slot.messages[0]["meta"]
    assert meta["completed" if success else "failed"] is True
    assert meta["card_owned"] is True


@pytest.mark.asyncio
async def test_next_turn_request_supersedes_and_both_stay_tagged(tmp_path, monkeypatch):
    """Each turn's re-offer retires the previous link; neither surfaces in chat."""
    _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": CARDED_URL}})
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event(CARDED_SLUG))
    await _on_mcp_oauth_request(turn, _event(CARDED_SLUG))
    assert [m["meta"].get("superseded", False) for m in slot.messages] == [True, False]
    assert _owned(slot) == [True, True]


@pytest.mark.asyncio
async def test_banner_of_a_dead_child_expires_tagged(tmp_path, monkeypatch):
    """A sign-in whose child died half way reads as expired, still card-owned."""
    _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": CARDED_URL}})
    slot = _ChatSlot("s1")
    await _on_mcp_oauth_request(_turn(slot), _event(CARDED_SLUG))
    meta = _expire_dead_child_oauth_meta("mcp_oauth", slot.messages[0]["meta"], "child-2")
    assert meta["expired"] is True
    assert "oauth_url" not in meta
    assert meta["card_owned"] is True


@pytest.mark.asyncio
async def test_lookup_failure_fails_open_mid_turn(tmp_path, monkeypatch):
    """A broken store leaves the banner untagged, so chat still shows it."""
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (tmp_path / "absent.json",))
    slot = _ChatSlot("s1")
    await _on_mcp_oauth_request(_turn(slot), _event(CARDED_SLUG))
    assert _owned(slot) == [False]


def test_only_kiro_cli_reads_the_grant_a_card_writes():
    """KAS is spawned through kiro-cli but keeps its grants in Crew's own vault."""
    readers = {b for b in ACP_BACKENDS_KNOWN if capabilities_for(b).reads_kiro_cli_mcp_grants}
    assert readers == {ACP_BACKEND_KIRO}


@pytest.mark.asyncio
async def test_kas_chat_keeps_its_banner_for_a_carded_provider(tmp_path, monkeypatch):
    """The card's grant cannot sign a KAS chat in, so its own banner is not tagged."""
    _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": CARDED_URL}})
    slot = _ChatSlot("s1")
    await _on_mcp_oauth_request(_turn(slot, ACP_BACKEND_KAS), _event(CARDED_SLUG))
    assert len(slot.messages) == 1
    meta = slot.messages[0]["meta"]
    assert "card_owned" not in meta
    assert meta["oauth_url"] == f"https://{CARDED_SLUG}.example.com/authorize?client_id=x"


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False], ids=["signed-in", "sign-in-failed"])
async def test_kas_chat_banner_stays_untagged_through_its_verdict(tmp_path, monkeypatch, success):
    """The chat is where a KAS sign-in ends, so its verdict stays visible there."""
    _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": CARDED_URL}})
    slot = _ChatSlot("s1")
    turn = _turn(slot, ACP_BACKEND_KAS)
    await _on_mcp_oauth_request(turn, _event(CARDED_SLUG))
    _mark_mcp_oauth_completed(turn.state, slot, CARDED_SLUG, success=success, error="denied")
    assert _owned(slot) == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
async def test_tagged_only_when_the_session_reads_kiro_cli_grants(tmp_path, monkeypatch, backend):
    """A backend that cannot use the card's grant keeps its banner and skips the read."""
    _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": CARDED_URL}})
    reads: list[int] = []
    real = chat_runner._connections_managed_mcp_names
    monkeypatch.setattr(
        chat_runner, "_connections_managed_mcp_names", lambda: reads.append(1) or real()
    )
    slot = _ChatSlot("s1")
    await _on_mcp_oauth_request(_turn(slot, backend), _event(CARDED_SLUG))
    usable = capabilities_for(backend).reads_kiro_cli_mcp_grants
    assert _owned(slot) == [usable]
    assert bool(reads) is usable


@pytest.mark.asyncio
async def test_provider_without_capabilities_is_not_tagged(tmp_path, monkeypatch):
    """An unknown provider shape keeps its banner, the direction that hides nothing."""
    _own(tmp_path, monkeypatch, {CARDED_SLUG: {"url": CARDED_URL}})
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    turn.client.capabilities = None
    await _on_mcp_oauth_request(turn, _event(CARDED_SLUG))
    assert _owned(slot) == [False]


def _owned(slot: _ChatSlot) -> list[bool]:
    return [bool(m["meta"].get("card_owned")) for m in slot.messages]
