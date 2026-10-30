"""The recycle and stuck-turn notices ``DashboardState`` posts into the affected chat.

``wire_session_recycle_callback`` (``dashboard/state_parts/session_events.py``) hands
the session manager two callbacks: an async one the watchdog awaits after it recycles
a session, and a sync one its stuck-turn hook calls. Each finds the tab that displays
the session and appends one notice row there. These tests drive both callbacks
against a real ``DashboardState`` and its slot: the row they append, the sessions
with no tab or no live slot that get nothing, and a failing append or banner push
that is logged instead of raised into the watchdog.
"""

from __future__ import annotations

import logging

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_utils
from kiro_crew.dashboard.state import _SESSION_RECYCLED_NOTICE, stuck_turn_notice

#: Session key -> the dashboard tab that displays it; anything else has no tab.
_TABS = {"dashboard:t1": "t1", "dashboard:gone": "gone"}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A state with one live slot ``t1`` and its two session callbacks."""
    monkeypatch.setattr(chat_utils, "dashboard_slot_key", lambda key: _TABS.get(key, ""))
    banners: list[str] = []
    monkeypatch.setattr(
        chat_utils, "_broadcast_expired_oauth_banners", lambda state, slot: banners.append(slot.key)
    )
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("t1")
    state.wire_session_recycle_callback()
    (on_recycled,) = state.sessions.set_recycle_callback.call_args.args
    return state, slot, on_recycled, state.sessions.on_stuck_turn, banners


@pytest.mark.asyncio
async def test_a_recycle_posts_its_notice_into_the_tab_and_pushes_the_banner_verdict(wired):
    state, slot, on_recycled, _on_stuck, banners = wired
    slot.append("user", "hello", "msg msg-u", broadcast=False)
    before = len(slot._messages)

    await on_recycled("dashboard:t1", reason="RSS over the limit")

    assert len(slot._messages) == before + 1
    row = slot._messages[-1]
    assert row["role"] == "assistant"
    assert row["content"] == _SESSION_RECYCLED_NOTICE.format(reason="RSS over the limit")
    assert row["meta"]["kind"] == "compaction"
    assert row["meta"]["notice"] == "session_recycled"
    assert banners == ["t1"]


@pytest.mark.asyncio
async def test_a_fresh_tab_gets_no_recycle_notice_while_a_used_tab_keeps_it(wired):
    """An eager-spawned "+ New" tab recycled before its first message
    shows no banner, even after a second recycle; a tab with a conversation
    still gets the notice. The OAuth banner verdict is pushed for both."""
    state, fresh, on_recycled, _on_stuck, banners = wired
    used = state.get_or_create_slot("used")
    used.append("user", "hello", "msg msg-u", broadcast=False)
    used.append("assistant", "hi", "msg msg-a", broadcast=False)
    _TABS["dashboard:used"] = "used"
    try:
        await on_recycled("dashboard:t1", reason="memory limit (1914MB)")
        await on_recycled("dashboard:t1", reason="memory limit (1947MB)")
        await on_recycled("dashboard:used", reason="memory limit (2074MB)")
    finally:
        _TABS.pop("dashboard:used")

    assert [r for r in fresh._messages if (r.get("meta") or {}).get("notice")] == []
    notices = [r for r in used._messages if (r.get("meta") or {}).get("notice")]
    assert [r["content"] for r in notices] == [
        _SESSION_RECYCLED_NOTICE.format(reason="memory limit (2074MB)")
    ]
    assert banners == ["t1", "t1", "used"]


def test_a_stuck_turn_posts_its_notice_into_the_tab(wired):
    _state, slot, _on_recycled, on_stuck, _banners = wired
    before = len(slot._messages)

    on_stuck("dashboard:t1", 600.0)

    assert len(slot._messages) == before + 1
    row = slot._messages[-1]
    assert row["role"] == "assistant"
    assert row["content"] == stuck_turn_notice(600.0)
    assert row["meta"]["kind"] == "compaction"
    assert row["meta"]["notice"] == "stuck_turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["cron:nightly", "dashboard:gone"], ids=["no-tab", "no-slot"])
async def test_a_session_with_no_live_tab_gets_no_notice(wired, key, caplog):
    _state, slot, on_recycled, on_stuck, banners = wired
    before = len(slot._messages)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.dashboard.state"):
        await on_recycled(key, reason="RSS over the limit")
        on_stuck(key, 600.0)

    assert len(slot._messages) == before
    assert banners == []
    assert [r for r in caplog.records if r.name == "kiro_crew.dashboard.state"] == []


@pytest.mark.asyncio
async def test_a_failing_append_or_banner_push_is_logged_not_raised(wired, monkeypatch, caplog):
    _state, slot, on_recycled, on_stuck, _banners = wired
    slot.append("user", "hello", "msg msg-u", broadcast=False)

    def _refuse(self, *args, **kwargs):
        raise RuntimeError("append refused")

    def _banner_boom(state, slot):
        raise RuntimeError("banner push refused")

    # ``_ChatSlot`` has ``__slots__``, so the method is patched on the class.
    monkeypatch.setattr(type(slot), "append", _refuse)
    monkeypatch.setattr(chat_utils, "_broadcast_expired_oauth_banners", _banner_boom)
    with caplog.at_level(logging.ERROR, logger="kiro_crew.dashboard.state"):
        await on_recycled("dashboard:t1", reason="RSS over the limit")
        on_stuck("dashboard:t1", 600.0)

    logged = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.dashboard.state"]
    assert "Failed to append recycle notice to slot t1" in logged
    assert "Failed to broadcast OAuth banner expiry for slot t1" in logged
    assert "Failed to append stuck-turn notice to slot t1" in logged
