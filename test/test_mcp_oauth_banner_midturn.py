"""The mid-turn ``mcp_oauth`` arm tags carded providers like the init drain does.

The turn-start sign-in offer re-requests every unauthorized server, so a
provider with a Connections card (Linear, Notion, ...) reaches
``_on_mcp_oauth_request`` on every turn of every session. Without the
``card_owned`` tag the chat renders its banner each time, beside the card that
already shows the same Authorize action.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import mcp_discovery, mcp_oauth_discovery
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

    def mcp_session_report(self):
        return None

    def set_mcp_sign_in_ended_hook(self, hook):
        self.ended_hook = hook

    def mcp_sign_in_ended(self, server_name):
        return server_name in getattr(self, "ended", ())

    def end_attempt(self, server_name):
        """What the runtime does: free the sign-in slot, then call the hook."""
        self.ended = {server_name}
        if hasattr(self, "ended_hook"):
            self.ended_hook(server_name)


def _turn(slot: _ChatSlot):
    return SimpleNamespace(client=_Client(), slot=slot, state=_State())


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
async def test_an_ended_sign_in_attempt_withdraws_its_link_and_the_next_offer_is_live(
    tmp_path, monkeypatch
):
    """A KAS attempt that ends unconnected closes its callback listener, so the link
    it showed is dead. The hook the arm installs withdraws it, open tabs
    included, and the next offer for the server draws a live card."""
    _own(tmp_path, monkeypatch, {})
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("figma"))
    turn.client.ended_hook("figma")
    ended = slot.messages[0]["meta"]
    assert (ended["expired"], ended["expired_reason"]) == (True, "attempt_ended")
    assert "oauth_url" not in ended
    kind, payload = turn.state.broadcasts[-1]
    assert (kind, payload["meta"]["oauth_url"]) == ("chat_message_update", "")
    await _on_mcp_oauth_request(turn, _event("figma"))
    assert slot.messages[1]["meta"]["oauth_url"] == _event("figma").oauth_url
    assert "superseded" not in slot.messages[0]["meta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["queued", "discovery"])
async def test_an_attempt_that_ends_before_its_row_exists_still_withdraws_it(
    tmp_path, monkeypatch, window
):
    """The attempt can end before its row exists: while its link waits in the queue
    behind a prompt the dispatch loop is answering, or while endpoint discovery
    outlasts a short connect timeout. That row must still arrive withdrawn."""
    _own(tmp_path, monkeypatch, {})
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    if window == "queued":
        turn.client.end_attempt("figma")
    else:

        async def _attempt_ends_during_discovery(server_name, oauth_url):
            turn.client.end_attempt(server_name)
            return False

        monkeypatch.setattr(
            mcp_oauth_discovery,
            "approve_discovered_oauth_endpoint",
            _attempt_ends_during_discovery,
        )
    await _on_mcp_oauth_request(turn, _event("figma"))
    ended = slot.messages[0]["meta"]
    assert (ended["expired"], ended["expired_reason"]) == (True, "attempt_ended")
    assert "oauth_url" not in ended


@pytest.mark.asyncio
async def test_a_late_success_still_turns_an_ended_attempts_card_green(tmp_path, monkeypatch):
    """The server connecting after its attempt ended is still a sign-in."""
    _own(tmp_path, monkeypatch, {})
    slot = _ChatSlot("s1")
    turn = _turn(slot)
    await _on_mcp_oauth_request(turn, _event("figma"))
    turn.client.ended_hook("figma")
    _mark_mcp_oauth_completed(turn.state, slot, "figma", success=False, error="late")
    assert "failed" not in slot.messages[0]["meta"]
    _mark_mcp_oauth_completed(turn.state, slot, "figma", success=True)
    meta = slot.messages[0]["meta"]
    assert meta["completed"] is True
    assert "expired" not in meta and "expired_reason" not in meta


@pytest.mark.asyncio
async def test_lookup_failure_fails_open_mid_turn(tmp_path, monkeypatch):
    """A broken store leaves the banner untagged, so chat still shows it."""
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (tmp_path / "absent.json",))
    slot = _ChatSlot("s1")
    await _on_mcp_oauth_request(_turn(slot), _event(CARDED_SLUG))
    assert _owned(slot) == [False]


def _owned(slot: _ChatSlot) -> list[bool]:
    return [bool(m["meta"].get("card_owned")) for m in slot.messages]
