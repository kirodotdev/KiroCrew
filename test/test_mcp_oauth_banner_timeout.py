"""A sign-in attempt that ran out of time retires its Authorize banner.

The engine holds the OAuth callback listener open for one connect attempt only
(the server's connect timeout, 60 s by default). Once the attempt ends without the
server connecting, the banner's link sends the browser to a closed ``localhost``
port, so the dashboard withdraws it and marks the row ``attempt_ended``, which the
client does not draw.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import (
    _emit_mcp_oauth_request,
    _expire_ended_mcp_oauth_banners,
    _mark_mcp_oauth_completed,
)
from kiro_crew.dashboard.chat_utils import _prepare_messages
from kiro_crew.dashboard.state import _ChatSlot

_URL = "https://mcp.example.com/authorize?state=live&port=54804"


def _slot_with_banner(server: str = "remote") -> _ChatSlot:
    slot = _ChatSlot("s1")
    _emit_mcp_oauth_request(MagicMock(), slot, server, _URL, minted_by="child-1")
    return slot


def test_a_timed_out_attempt_withdraws_the_link_and_says_why():
    slot = _slot_with_banner()
    _expire_ended_mcp_oauth_banners(MagicMock(), slot, "remote")
    meta = slot.messages[0]["meta"]
    assert "oauth_url" not in meta
    assert meta["expired"] is True
    assert meta["expired_reason"] == "attempt_ended"
    assert "ended before it was completed" in slot.messages[0]["content"]


def test_the_withdrawal_is_broadcast_with_a_blank_url():
    """Clients merge meta, so an omitted key would leave the dead link rendered."""
    state = MagicMock()
    slot = _slot_with_banner()
    _expire_ended_mcp_oauth_banners(state, slot, "remote")
    event, payload = state.broadcast_ws.call_args.args
    assert event == "chat_message_update"
    assert payload["meta"]["oauth_url"] == ""
    assert payload["meta"]["expired_reason"] == "attempt_ended"
    assert payload["mid"] == slot.messages[0]["meta"]["mid"]


def test_another_servers_banner_keeps_its_link():
    slot = _slot_with_banner("figma")
    _expire_ended_mcp_oauth_banners(MagicMock(), slot, "remote")
    assert slot.messages[0]["meta"]["oauth_url"] == _URL


def test_a_fresh_banner_after_the_timeout_is_live():
    slot = _slot_with_banner()
    _expire_ended_mcp_oauth_banners(MagicMock(), slot, "remote")
    _emit_mcp_oauth_request(MagicMock(), slot, "remote", _URL + "&n=2", minted_by="child-1")
    assert slot.messages[-1]["meta"]["oauth_url"] == _URL + "&n=2"
    assert "expired" not in slot.messages[-1]["meta"]


def test_a_completion_that_lands_after_the_timeout_still_signs_in():
    """The attempt-ended signal and the connected snapshot race; connected wins."""
    slot = _slot_with_banner()
    _expire_ended_mcp_oauth_banners(MagicMock(), slot, "remote")
    _mark_mcp_oauth_completed(MagicMock(), slot, "remote", success=True)
    meta = slot.messages[0]["meta"]
    assert meta["completed"] is True
    assert "expired" not in meta and "expired_reason" not in meta


def test_a_failure_does_not_reopen_a_timed_out_banner():
    slot = _slot_with_banner()
    _expire_ended_mcp_oauth_banners(MagicMock(), slot, "remote")
    _mark_mcp_oauth_completed(MagicMock(), slot, "remote", success=False, error="boom")
    assert "failed" not in slot.messages[0]["meta"]


def test_the_read_gate_keeps_the_timeout_reason():
    slot = _slot_with_banner()
    _expire_ended_mcp_oauth_banners(MagicMock(), slot, "remote")
    prepared = _prepare_messages(list(slot.messages), running=False, live_child="child-1")
    assert prepared[0]["meta"]["expired_reason"] == "attempt_ended"
    assert not prepared[0]["meta"].get("oauth_url")


class _HookedClient:
    """A provider double that records the hook the dashboard installs and answers
    whether the sign-in behind a link is still running."""

    process_instance = "child-1"

    def __init__(self, live: bool | None = True) -> None:
        self.hook = None
        self.live = live

    def set_mcp_sign_in_ended_hook(self, hook) -> None:
        self.hook = hook

    def mcp_sign_in_live(self, server_name: str) -> bool | None:
        return self.live


def test_showing_a_link_installs_a_hook_that_retires_it_while_idle(monkeypatch):
    import asyncio

    from kiro_crew.dashboard.chat_turn import mcp_session

    async def approve(server_name, oauth_url):
        return None

    monkeypatch.setattr(chat_runner, "_approve_discovered_mcp_oauth_endpoint", approve)
    slot = _ChatSlot("s1")
    client = _HookedClient()
    asyncio.run(mcp_session._show_mcp_oauth_request(MagicMock(), slot, client, "remote", _URL))
    assert slot.messages[0]["meta"]["oauth_url"] == _URL
    client.hook("remote")
    assert slot.messages[0]["meta"]["expired_reason"] == "attempt_ended"


def _show(monkeypatch, client: _HookedClient, approve=None) -> _ChatSlot:
    import asyncio

    from kiro_crew.dashboard.chat_turn import mcp_session

    async def no_check(server_name, oauth_url):
        return None

    monkeypatch.setattr(chat_runner, "_approve_discovered_mcp_oauth_endpoint", approve or no_check)
    slot = _ChatSlot("s1")
    asyncio.run(mcp_session._show_mcp_oauth_request(MagicMock(), slot, client, "remote", _URL))
    return slot


def test_a_link_whose_attempt_already_ended_is_drawn_expired(monkeypatch):
    """The request waited in the queue (behind a permission prompt, or the endpoint
    check) past its attempt's end; the draw-time check retires it."""
    slot = _show(monkeypatch, _HookedClient(live=False))
    meta = slot.messages[0]["meta"]
    assert "oauth_url" not in meta
    assert meta["expired_reason"] == "attempt_ended"


def test_an_attempt_that_ends_during_the_endpoint_check_is_shown_expired(monkeypatch):
    client = _HookedClient(live=True)

    async def approve(server_name, oauth_url):
        client.live = False

    slot = _show(monkeypatch, client, approve)
    assert slot.messages[0]["meta"]["expired_reason"] == "attempt_ended"


def test_a_queued_request_after_a_reinstalled_hook_is_still_judged(monkeypatch):
    """Two links in one turn: the second draw reinstalls the hook, and the first
    attempt's end must not be forgotten by it -- the live check needs no record."""
    import asyncio

    from kiro_crew.dashboard.chat_turn import mcp_session

    monkeypatch.setattr(chat_runner, "_approve_discovered_mcp_oauth_endpoint", _noop_check)
    slot = _ChatSlot("s1")
    first = _HookedClient(live=True)
    asyncio.run(mcp_session._show_mcp_oauth_request(MagicMock(), slot, first, "figma", _URL))
    first.live = False
    asyncio.run(mcp_session._show_mcp_oauth_request(MagicMock(), slot, first, "remote", _URL))
    assert slot.messages[1]["meta"]["expired_reason"] == "attempt_ended"


def test_the_turn_handler_draws_through_the_hooked_path(monkeypatch):
    """A mid-turn request reaches the banner through the dispatch handler, which
    must install the idle hook and run the draw-time check like any other draw."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_turn import mcp_session

    monkeypatch.setattr(chat_runner, "_approve_discovered_mcp_oauth_endpoint", _noop_check)
    monkeypatch.setattr(chat_runner, "_record_session_mcp_event", lambda *a, **k: None)
    slot = _ChatSlot("s1")
    client = _HookedClient(live=False)
    turn = SimpleNamespace(client=client, slot=slot, state=MagicMock())
    event = SimpleNamespace(
        server_name="remote", oauth_url=_URL, kind="mcp_oauth_request", runtime_global=False
    )
    asyncio.run(mcp_session._on_mcp_oauth_request(turn, event))
    assert client.hook is not None
    assert slot.messages[0]["meta"]["expired_reason"] == "attempt_ended"


def test_a_link_drained_at_turn_start_is_judged_and_hooked(monkeypatch):
    """A consent URL buffered between turns is drawn by the pre-turn drain, which
    must install the idle hook and run the draw-time check like the turn path."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_turn import mcp_session

    monkeypatch.setattr(chat_runner, "_approve_discovered_mcp_oauth_endpoint", _noop_check)
    monkeypatch.setattr(chat_runner, "_connections_managed_mcp_names", lambda: set())
    client = _HookedClient(live=False)
    client.client = SimpleNamespace(
        pop_pending_oauth_requests=lambda: [{"serverName": "remote", "oauthUrl": _URL}]
    )
    slot = _ChatSlot("s1")
    asyncio.run(mcp_session._drain_session_init_oauth_requests(MagicMock(), slot, client))
    assert client.hook is not None
    meta = slot.messages[0]["meta"]
    assert meta["expired_reason"] == "attempt_ended"
    assert meta["child"] == "child-1"


def test_a_provider_without_crew_sign_ins_leaves_the_link_live(monkeypatch):
    slot = _show(monkeypatch, _HookedClient(live=None))
    assert slot.messages[0]["meta"]["oauth_url"] == _URL


async def _noop_check(server_name, oauth_url):
    return None
