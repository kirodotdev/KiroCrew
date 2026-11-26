"""The recycle and stuck-turn notices ``DashboardState`` posts into the affected chat.

``wire_session_recycle_callback`` (``dashboard/state_parts/session_events.py``) hands
the session manager two callbacks: an async one the watchdog awaits after it recycles
a session, and a sync one its stuck-turn hook calls. Each finds the tab that displays
the session and appends one notice row there. These tests drive both callbacks
against a real ``DashboardState`` and its slot: the row they append, the sessions
with no tab or no live slot that get nothing, and a failing append or banner push
that is logged instead of raised into the watchdog. A third, sync one is called
once when the RSS recycle stops recycling a session that keeps coming back.
"""

from __future__ import annotations

import logging

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_utils
from kiro_crew.dashboard.chat_utils_injected_rows import EMPTY_TURN_NOTICE_KIND
from kiro_crew.dashboard.state import _SESSION_RECYCLED_NOTICE, stuck_turn_notice
from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_NOTICE

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


@pytest.mark.asyncio
async def test_a_recycle_tells_the_agent_its_background_work_ended_on_the_next_prompt(wired):
    """The person sees the recycle row, but a natively resumed session replays only
    the harness's own transcript, which still shows the background work as started.
    So the recycle also queues a notice for the agent's next prompt, through the same
    queue the next turn drains. A fresh tab with no conversation gets none."""
    from kiro_crew.dashboard.state_parts.injected_turns import SESSION_RESTARTED_NOTICE_PREFIX

    state, fresh, on_recycled, _on_stuck, _banners = wired
    used = state.get_or_create_slot("used")
    used.append("user", "run it in the background", "msg msg-u", broadcast=False)
    used.append("assistant", "started", "msg msg-a", broadcast=False)
    _TABS["dashboard:used"] = "used"
    try:
        await on_recycled("dashboard:t1", reason="idle")
        used._pending_subagent_failures.append("[Subagent completion event] r1 timed out")
        await on_recycled("dashboard:used", reason="runtime process is gone")
        await on_recycled("dashboard:used", reason="idle; its agent started background work")
    finally:
        _TABS.pop("dashboard:used")

    assert fresh._pending_subagent_failures == []
    # Another queued notice stays in place; two restarts before a turn queue one note.
    queued, notice = used._pending_subagent_failures
    assert queued == "[Subagent completion event] r1 timed out"
    assert notice.startswith(SESSION_RESTARTED_NOTICE_PREFIX)
    assert "runtime process is gone" in notice
    assert "spawn_run" in notice


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


def test_an_rss_hold_tells_a_used_tab_the_ceiling_and_the_setting(wired):
    # Resolved here, not at module top, so a missing module fails this test
    # alone instead of the whole file's collection.
    from kiro_crew.dashboard.state_parts.session_events import rss_hold_notice
    from kiro_crew.session_rss_rebound import RssHold

    state, slot, _on_recycled, _on_stuck, _banners = wired
    fresh = state.get_or_create_slot("fresh")
    _TABS["dashboard:fresh"] = "fresh"
    slot.append("user", "hello", "msg msg-u", broadcast=False)
    before = len(slot._messages)
    hold = RssHold(rss_mb=2100, ceiling_mb=1536, limit_mb=4200, recycles=3)
    try:
        state.sessions.on_rss_hold("dashboard:t1", hold)
        state.sessions.on_rss_hold("dashboard:fresh", hold)
    finally:
        _TABS.pop("dashboard:fresh")

    assert len(slot._messages) == before + 1
    row = slot._messages[-1]
    assert row["role"] == "assistant"
    assert row["content"] == rss_hold_notice(hold)
    for named in ("session.watchdog_rss_max_mb (1536 MB)", "Watchdog RSS Limit", "4200 MB"):
        assert named in row["content"]
    assert "recycled by the watchdog" not in row["content"]
    assert row["meta"]["kind"] == "compaction"
    assert row["meta"]["notice"] == "rss_hold"
    # Like the recycle notice, an eager-spawned tab with no conversation gets none.
    assert [r for r in fresh._messages if (r.get("meta") or {}).get("notice")] == []


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


#: The frozen "now" the expiry is judged at.
_NOW = "2026-11-13T04:27:19+00:00"

_OLD = "2026-11-13T04:25:19+00:00"
_REPLY = (("assistant", "yes", None), ("done", "", None))
# The empty-response verdict is the turn's outcome too, though no reply came.
_EMPTY = (("notice", EMPTY_TURN_NOTICE, {"kind": EMPTY_TURN_NOTICE_KIND}),)


@pytest.fixture
def frozen_now(monkeypatch):
    from datetime import datetime

    from kiro_crew.dashboard.state_parts import session_events

    monkeypatch.setattr(session_events, "_utc_now", lambda: datetime.fromisoformat(_NOW))


def _unanswered(slot, ts: str) -> None:
    slot.append("user", "hello", "msg msg-u", broadcast=False)
    slot.append("assistant", "hi", "msg msg-a", broadcast=False)
    slot.append("user", "are you there?", "msg msg-u", broadcast=False)
    slot._messages[-1]["ts"] = ts


def test_an_idle_expiry_of_a_stalled_message_marks_it_failed_and_logs_its_age(
    wired, frozen_now, caplog
):
    """The sweep reset a session whose admitted turn has waited two minutes to start.

    The turn may still recover, so the row does not pin the failure: an
    OpenAI-compatible reader of that turn reports the reply it goes on to give.
    """
    from kiro_crew.dashboard import gateway_notice as gn
    from kiro_crew.dashboard.chat_turn.queue_cycle import TURN_FAILED_META
    from kiro_crew.dashboard.openai_compat import _TurnOutcome

    state, slot, _on_recycled, _on_stuck, _banners = wired
    _unanswered(slot, "2026-11-13T04:25:19+00:00")
    slot._active_turn_session_key = "dashboard:t1"
    before = len(slot._messages)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.dashboard.state"):
        state.sessions.on_idle_expired("dashboard:t1")
        # The row now answers the message, so a second expiry adds nothing.
        state.sessions.on_idle_expired("dashboard:t1")

    assert len(slot._messages) == before + 1
    row = slot._messages[-1]
    assert row["role"] == "error"
    assert TURN_FAILED_META not in row["meta"]
    assert gn.gateway_notice_meta(gn.GATEWAY_NOTICE_TURN_FAILED_BEFORE_START).items() <= (
        row["meta"].items()
    )
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "Idle sweep expired dashboard:t1 while its newest message had no reply for 120s"
    ]

    for role, text, meta in _REPLY:  # the stalled turn recovers and replies
        slot.append(role, text, "msg", meta=meta, broadcast=False)
    outcome = _TurnOutcome()
    for msg in slot._messages[before - 1 :]:  # from the opener on
        outcome.observe(msg)
    assert outcome.reason is None


@pytest.mark.parametrize(
    "key, answer, admitting, opener_ts",
    [
        pytest.param("dashboard:t1", _REPLY, "dashboard:t1", _OLD, id="answered chat"),
        pytest.param("dashboard:t1", _EMPTY, "dashboard:t1", _OLD, id="empty-response notice"),
        pytest.param("cron:nightly", (), "cron:nightly", _OLD, id="no tab"),
        pytest.param("dashboard:gone", (), "dashboard:gone", _OLD, id="slot gone"),
        # A turn still queued (fair admission, the background-turn permit) has not
        # entered admission yet, so it publishes no identity, like no turn at all.
        pytest.param("dashboard:t1", (), "", _OLD, id="no turn admitting"),
        pytest.param(
            "dashboard:t1", (), "dashboard:t1", "2026-11-13T04:27:00+00:00", id="just sent"
        ),
        pytest.param(
            "dashboard:t1", (), "dashboard:t1", "2026-11-13T04:26:19+00:00", id="a minute old"
        ),
        pytest.param("dashboard:t1", (), "dashboard:t1", "not a time", id="unparseable ts"),
        pytest.param("cron:nightly:run-7", (), "cron:nightly", _OLD, id="another session"),
    ],
)
def test_an_idle_expiry_of_a_turn_that_may_still_start_adds_nothing(
    wired, frozen_now, monkeypatch, caplog, key, answer, admitting, opener_ts
):
    """Only the turn admitting onto the reset session, waiting over a minute, is marked."""
    state, slot, _on_recycled, _on_stuck, _banners = wired
    # A cron per-run key resolves to its job's tab, whose turns run on another session.
    monkeypatch.setitem(_TABS, "cron:nightly:run-7", "t1")
    _unanswered(slot, opener_ts)
    for role, text, meta in answer:
        slot.append(role, text, "msg", meta=meta, broadcast=False)
    slot._active_turn_session_key = admitting
    before = len(slot._messages)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.dashboard.state"):
        state.sessions.on_idle_expired(key)

    assert len(slot._messages) == before
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []
