"""A chat whose session ends closes its own browser tabs, and only those.

Two chats each own an agent tab (the live hub's binding, as ``browser_ops._bind_target``
leaves it) and the relay's ledger lists both. Driven through the real ways a chat
ends -- the tab close, the idle archive, the delete of its open conversation -- with a
real ``CdpRelay`` whose upstream send is recorded instead of reaching a browser: only
the ending chat's tabs are closed, and the other chat keeps browsing on its own. The
chat's last op still holds the browser when it ends, so the tab that op opens closes
too, and the card's stream on the closing tab is stopped first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from chat_test_helpers import _make_state
from remove_slot_for_history_key_helpers import _make_slot
from remove_slot_for_history_key_helpers import _make_state as _make_delete_state
from remove_slot_for_history_key_helpers import _remove_slot_for_history_key
from slot_close_recreation_race_helpers import _Req

from kiro_crew.browser_harness import live_hub
from kiro_crew.browser_harness.relay import CdpRelay
from kiro_crew.dashboard.chat_handlers import api_chat_slots_cleanup, close_slot
from kiro_crew.dashboard.handlers import browser_live, browser_ops
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.asyncio


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[CdpRelay, list[Any]]]:
    """Chat A owns tab T-a, chat B owns T-b; every CDP request the relay sends is kept."""
    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    live_hub.reset_live_hub()
    hub = live_hub.get_live_hub()
    hub.bind_target("chat-a", "T-a")
    hub.bind_target("chat-b", "T-b")
    relay = CdpRelay()
    relay._agent_targets = ["T-a", "T-b"]
    sent: list[Any] = []

    async def send(method: str, params: Any = None, session_id: Any = None, **_kw: Any) -> Any:
        sent.append((method, params))
        return {}

    monkeypatch.setattr(relay, "send", send)
    monkeypatch.setattr(browser_live, "_current_relay", lambda: relay)
    yield relay, sent
    live_hub.reset_live_hub()


async def _end_chat_a(path: str, tmp_path: Path) -> Any:
    """End chat A by *path*; return the state whose background tasks run the close."""
    if path == "history_delete":
        state = _make_delete_state({"chat-a": _make_slot("chat-a"), "chat-b": _make_slot("chat-b")})
        state._background_tasks = set()
        await _remove_slot_for_history_key(state, "dashboard_chat-a")
        assert set(state._slots) == {"chat-b"}
        return state
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    slot = state.get_or_create_slot("chat-a")
    state.get_or_create_slot("chat-b")
    if path == "tab_close":
        await close_slot(state, slot, slot.key)
    else:
        slot.created_at = "2020-01-01T00:00:00+00:00"
        assert (await api_chat_slots_cleanup(_Req(state, slot.key))).status == 200
    assert state.get_slot("chat-a") is None and state.get_slot("chat-b") is not None
    return state


@pytest.mark.parametrize("path", ["tab_close", "idle_cleanup", "history_delete"])
async def test_ending_one_chat_closes_only_its_own_tabs(
    path: str, tmp_path: Path, browser: tuple[CdpRelay, list[Any]]
) -> None:
    relay, sent = browser
    hub = live_hub.get_live_hub()
    card = hub.get("chat-a")
    assert card is not None
    stream = _Stream()
    card.stream = stream  # type: ignore[assignment]
    async with browser_ops._OP_LOCK:  # chat A's last op is still running
        state = await _end_chat_a(path, tmp_path)
        relay._agent_targets.append("T-a2")
        hub.bind_target("chat-a", "T-a2")
    await await_until(asyncio.gather(*state._background_tasks), what="the chat's tab close")
    assert stream.stopped
    assert sent == [
        ("Target.closeTarget", {"targetId": "T-a"}),
        ("Target.closeTarget", {"targetId": "T-a2"}),
    ]
    assert relay.agent_targets() == ["T-b"]


async def test_a_chat_resumed_before_the_close_runs_keeps_its_tabs(
    tmp_path: Path, browser: tuple[CdpRelay, list[Any]]
) -> None:
    relay, sent = browser
    async with browser_ops._OP_LOCK:  # the close queues behind a running op
        state = await _end_chat_a("tab_close", tmp_path)
        state.get_or_create_slot("chat-a")  # resumed on the same key meanwhile
    await await_until(asyncio.gather(*state._background_tasks), what="the chat's tab close")
    assert sent == []
    assert relay.agent_targets() == ["T-a", "T-b"]


@pytest.mark.parametrize("path", ["tab_close", "idle_cleanup"])
async def test_closing_a_linked_channel_tab_keeps_the_channel_sessions_tabs(
    path: str, tmp_path: Path, browser: tuple[CdpRelay, list[Any]]
) -> None:
    # The Slack thread's session outlives its dashboard tab and folds onto the same
    # hub key as that tab (slack:<ts> and dashboard:slack_<ts> are both slack_<ts>).
    relay, sent = browser
    hub = live_hub.get_live_hub()
    key = browser_ops.live_session_key("slack:123")
    hub.bind_target(key, "T-s")
    relay._agent_targets.append("T-s")
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    slot = state.get_or_create_slot("slack_123")
    slot.linked_session_key = "slack:123"
    if path == "tab_close":
        await close_slot(state, slot, slot.key)
    else:
        slot.created_at = "2020-01-01T00:00:00+00:00"
        assert (await api_chat_slots_cleanup(_Req(state, slot.key))).status == 200
    assert state.get_slot("slack_123") is None
    await await_until(asyncio.gather(*state._background_tasks), what="the close's tasks")
    assert sent == []
    assert relay.agent_targets() == ["T-a", "T-b", "T-s"]


class _Stream:
    """The card's screencast on chat A's tab: only its stop is observed."""

    stopped = False

    async def stop(self) -> None:
        self.stopped = True
