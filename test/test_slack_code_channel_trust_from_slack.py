"""Slack Code channels: approval cards in the channel, and "Trust this channel".

In a code channel the channel IS the session, so its tool-approval card posts
top-level (no ``thread_ts``), where the agent's replies go, and carries a
secondary **Trust this channel** button beside Approve (still the one primary)
and Reject. A click by the owner puts the session on Trust through the
dashboard picker's own mutation (``apply_approval_mode``) -- so the call is
approved by that function's sweep and its ``mode_change:trust`` SEL row is
written -- plus this feature's own decision row and a one-line warning in the
channel. Anyone else, or a card that is not the channel's own session's, is
refused with nothing changed. An ordinary linked thread keeps threading and
never shows the button.

Recorded Slack payloads in, recorded calls out (the agent-surface ``env``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER

# The agent-surface harness: fake Slack, dashboard, store, SEL and owner check.
from test_slack_code_channel_agent_surface import (  # noqa: F401
    CH,
    ORIGIN,
    OTHER_CH,
    SLOT,
    _bind,
    env,
)

import kiro_crew.dashboard.chat_handlers as chat_handlers
import kiro_crew.slack.handler as slack_handler
from kiro_crew import safety_override as safety_override_mod
from kiro_crew.config.loader import ACTIVATION_OFF
from kiro_crew.slack.code_channels import approval_picker
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.handler import (
    _build_approval_blocks,
    _linked_approvals,
    post_linked_approval,
)

ANCHOR = "100.000001"
REQUEST = "req-7"
OPERATION = approval_picker.OPERATION


class _Sessions:
    def __init__(self) -> None:
        self.policies: dict[str, str] = {}

    def set_approval_policy(self, key: str, policy: str) -> None:
        self.policies[key] = policy


def _slot(key: str = SLOT, channel: str = CH) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        running=True,
        linked_session_key="",
        _trust=False,
        _trust_reads=False,
        _trust_scope="",
        _approval_futures={},
        messages=[],
        _dirty=False,
        _slack_channel=channel,
        _slack_thread_ts=ANCHOR,
    )


@pytest.fixture
def chan(env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    """A bound code channel whose session is a real-shaped dashboard slot."""
    _bind(env)
    monkeypatch.setattr(approval_picker, "live_config", env.cfg)
    from kiro_crew.slack.code_channels import agent_picker

    monkeypatch.setattr(agent_picker, "live_config", env.cfg)  # the shared owner gate
    monkeypatch.setattr(slack_handler, "is_allowed_user", lambda u: u in env.allowed)
    slot = _slot()
    env.slot = slot
    env.state.linked[ANCHOR] = slot
    env.state._slots = {slot.key: slot}
    env.state.sessions = _Sessions()
    env.state._approval_futures = {}
    env.state.channel_manager = None
    env.state.broadcast_ws = lambda *a, **k: None
    env.resolved = []

    env.global_resolves = []

    def resolve_approval(request_id: str, approved: bool) -> bool:
        # Like the real one: the first pending future under this id, anywhere.
        env.global_resolves.append(request_id)
        for fut in [env.state._approval_futures.get(request_id)] + [
            s._approval_futures.get(request_id) for s in env.state._slots.values()
        ]:
            if fut is not None and not fut.done():
                fut.set_result("approved" if approved else "rejected")
                return True
        return False

    def resolve_slot_approval(
        slot: Any, request_id: str, approved: bool, *, expected_future: Any = None
    ) -> bool:
        env.resolved.append((slot.key, request_id, approved))
        fut = slot._approval_futures.get(request_id)
        if fut is None or fut is not expected_future or fut.done():
            return False
        fut.set_result("approved" if approved else "rejected")
        return True

    env.state.resolve_approval = resolve_approval
    env.state.resolve_slot_approval = resolve_slot_approval
    env.mode_rows = []
    monkeypatch.setattr(
        chat_handlers,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: env.mode_rows.append(kw)),
    )
    env.mode_permitted = True
    monkeypatch.setattr(
        safety_override_mod, "approval_mode_permitted", lambda _m: env.mode_permitted
    )
    approval_picker.reset()
    _linked_approvals.clear()
    yield env
    _linked_approvals.clear()


def _post_card(e: Any, channel: str = CH, thread: str | None = None) -> str:
    """The card the channel's session posts, through the real poster."""
    ts = asyncio.run(
        post_linked_approval(
            e.slack,
            channel,
            thread,
            REQUEST,
            f"dashboard:{SLOT}",
            "Run shell",
            "ls",
            channel_trust=thread is None,
        )
    )
    assert ts
    return ts


def _click(user: str, ts: str, channel: str = CH) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"ts": ts},
        "actions": [{"action_id": approval_picker.TRUST_CALL_ACTION_ID, "value": REQUEST}],
    }


def _buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]


def _decisions(e: Any) -> list[dict[str, Any]]:
    return e.sel.ops(OPERATION)


# ── Where the card goes ───────────────────────────────────────────────────


def test_a_code_channel_card_posts_top_level(chan: Any) -> None:
    # What _run_chat passes as the thread for the channel's own session.
    assert cc_binding.mirror_post_thread(CH, ANCHOR) is None
    _post_card(chan, thread=cc_binding.mirror_post_thread(CH, ANCHOR))
    channel, _blocks, _text, thread_ts = chan.slack.blocks_posts[-1]
    assert channel == CH and thread_ts is None


def test_an_ordinary_linked_thread_keeps_threading_and_has_no_trust_button(chan: Any) -> None:
    thread = cc_binding.mirror_post_thread(OTHER_CH, "555.000001")
    assert thread == "555.000001"
    _post_card(chan, channel=OTHER_CH, thread=thread)
    _channel, blocks, _text, thread_ts = chan.slack.blocks_posts[-1]
    assert thread_ts == "555.000001"
    assert all(b["action_id"] != approval_picker.TRUST_CALL_ACTION_ID for b in _buttons(blocks))


@pytest.mark.parametrize(
    ("channel", "anchor", "top_level"),
    [(CH, ANCHOR, True), (OTHER_CH, "555.000001", False)],
    ids=["code-channel", "ordinary-thread"],
)
def test_a_real_turn_posts_its_card_by_the_mirror_thread_rule(
    chan: Any, channel: str, anchor: str, top_level: bool  # noqa: F811
) -> None:
    """A dashboard turn's prompt in the channel's own session posts top-level with
    Trust this channel; an ordinary linked thread keeps threading without it."""
    from slack_code_channel_turns import prompt_in_channel_session
    from turn_harness import APPROVED

    asyncio.run(
        prompt_in_channel_session(
            chan, slot_key=SLOT, channel=channel, anchor=anchor, answer=APPROVED
        )
    )
    posted, blocks, _text, thread_ts = chan.slack.blocks_posts[-1]
    assert posted == channel
    assert thread_ts == (None if top_level else anchor)
    trust = [b for b in _buttons(blocks) if b["action_id"] == approval_picker.TRUST_CALL_ACTION_ID]
    assert bool(trust) is top_level


def test_the_card_keeps_one_primary_button() -> None:
    event = SimpleNamespace(request_id=REQUEST, title="Run shell", tool_input="ls", tool_purpose="")
    buttons = _buttons(_build_approval_blocks(event, is_dm=False, channel_trust=True))  # type: ignore[arg-type]
    labels = [b["text"]["text"] for b in buttons]
    assert approval_picker.TRUST_CALL_LABEL in labels
    assert [b.get("style") for b in buttons].count("primary") == 1
    trust = next(b for b in buttons if b["action_id"] == approval_picker.TRUST_CALL_ACTION_ID)
    assert "style" not in trust


# ── "Trust this channel" ──────────────────────────────────────────────────


def test_trust_this_channel_approves_the_call_and_puts_the_channel_on_trust(chan: Any) -> None:
    ts = _post_card(chan)

    async def click() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures[REQUEST] = fut
        assert await approval_picker.handle_trust_call(chan.orch, _click(OWNER, ts))
        return fut

    fut = asyncio.run(click())
    assert fut.result() == "approved"
    assert chan.slot._trust is True
    assert chan.state.sessions.policies[f"dashboard:{SLOT}"] == "auto"
    # The dashboard picker's own audit row, naming the Slack user ...
    trust_rows = [r for r in chan.mode_rows if r["operation"] == "mode_change:trust"]
    assert trust_rows and trust_rows[0]["caller"] == f"slack-code-channels:{OWNER}"
    # ... and this feature's decision row.
    allowed = [r for r in _decisions(chan) if r["outcome"] == "allowed"]
    assert any("result=set" in r["resources"] for r in allowed)
    assert any("result=approved" in r["resources"] for r in allowed)
    # One warning line, top-level in the channel; the card says what happened.
    warnings = [p for p in chan.slack.posts if "*Trust*" in p[1]]
    assert len(warnings) == 1 and warnings[0][0] == CH and warnings[0][2] is None
    assert "already running" in warnings[0][1]
    assert chan.slack.updates[-1][1] == ts


def test_a_trust_click_never_answers_another_sessions_call_with_the_same_id(chan: Any) -> None:
    """Request ids are per session: the same id pending elsewhere stays pending."""
    ts = _post_card(chan)
    other = _slot(key="chat-9-1", channel=OTHER_CH)
    chan.state._slots[other.key] = other

    async def click() -> tuple[asyncio.Future, asyncio.Future, asyncio.Future]:
        loop = asyncio.get_running_loop()
        mine, theirs, background = (loop.create_future() for _ in range(3))
        chan.slot._approval_futures[REQUEST] = mine
        other._approval_futures[REQUEST] = theirs
        chan.state._approval_futures[REQUEST] = background
        assert await approval_picker.handle_trust_call(chan.orch, _click(OWNER, ts))
        return mine, theirs, background

    mine, theirs, background = asyncio.run(click())
    assert mine.result() == "approved"
    assert not theirs.done() and not background.done()
    assert other._trust is False
    assert chan.global_resolves == []


def test_an_already_trusted_channels_card_is_answered_on_its_own_slot(chan: Any) -> None:
    """The race: the card went out before the session was put on Trust."""
    ts = _post_card(chan)
    chan.slot._trust = True

    async def click() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures[REQUEST] = fut
        assert await approval_picker.handle_trust_call(chan.orch, _click(OWNER, ts))
        return fut

    fut = asyncio.run(click())
    assert fut.result() == "approved"
    assert chan.resolved == [(SLOT, REQUEST, True)]
    assert not [r for r in chan.mode_rows if r["operation"] == "mode_change:trust"]
    assert any("result=unchanged" in r["resources"] for r in _decisions(chan))
    assert _decisions(chan)[-1]["error"] == ""


@pytest.mark.parametrize(
    "case", ["non_owner", "origin_off", "other_channel_card", "unknown_card", "policy_denies"]
)
def test_a_refused_trust_click_changes_nothing(chan: Any, case: str) -> None:
    ts = _post_card(chan)
    payload = _click(OWNER, ts)
    if case == "non_owner":
        payload = _click("USTRANGER", ts)
    elif case == "origin_off":
        chan.activation[ORIGIN] = ACTIVATION_OFF
    elif case == "other_channel_card":
        # A card registered for some other session, in this channel.
        _linked_approvals[f"{CH}:{ts}"] = SimpleNamespace(
            request_id=REQUEST, session_key="dashboard:chat-9-1", trust_grantable=False
        )
    elif case == "unknown_card":
        payload = _click(OWNER, "999.000001")
    elif case == "policy_denies":
        chan.mode_permitted = False

    async def click() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures[REQUEST] = fut
        assert await approval_picker.handle_trust_call(chan.orch, payload)
        return fut

    fut = asyncio.run(click())
    assert not fut.done()
    assert chan.slot._trust is False
    assert not [r for r in chan.mode_rows if r["operation"] == "mode_change:trust"]
    assert _decisions(chan)[-1]["outcome"] in ("denied", "error")
    assert chan.slack.ephemerals
    assert not [p for p in chan.slack.posts if "*Trust*" in p[1]]


def test_trust_click_with_the_feature_off_is_not_ours(chan: Any) -> None:
    ts = _post_card(chan)
    chan.on = False
    assert asyncio.run(approval_picker.handle_trust_call(chan.orch, _click(OWNER, ts))) is False
    assert chan.slot._trust is False


# ── Routing from the interactive dispatcher ───────────────────────────────


def _dispatch(chan: Any, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[str]:
    from kiro_crew.slack import interactions

    routed: list[str] = []

    async def trust_call(_orch: Any, p: dict[str, Any]) -> bool:
        routed.append(p["user"]["id"])
        return True

    monkeypatch.setattr(approval_picker, "handle_trust_call", trust_call)
    monkeypatch.setattr(interactions, "_orch", chan.orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: u in chan.allowed)
    monkeypatch.setattr(interactions, "sel", lambda: chan.sel)
    asyncio.run(interactions.dispatch(payload))
    return routed


def test_the_dispatcher_routes_an_owners_trust_click(chan: Any, monkeypatch: Any) -> None:
    assert _dispatch(chan, monkeypatch, _click(OWNER, "300.000001")) == [OWNER]


def test_the_dispatcher_refuses_a_non_owner_before_the_trust_handler(
    chan: Any, monkeypatch: Any
) -> None:
    assert _dispatch(chan, monkeypatch, _click("USTRANGER", "300.000001")) == []
    assert chan.slack.ephemerals and chan.slack.ephemerals[-1][1] == "USTRANGER"
    denied = chan.sel.ops("slack.interactive")
    assert denied and denied[-1]["outcome"] == "denied"


def test_trust_on_a_spent_channel_card_changes_no_mode(chan: Any, monkeypatch: Any) -> None:
    """The prompt is answered while the click's policy check is in flight."""
    ts = _post_card(chan)

    async def click() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures[REQUEST] = fut

        def permitted(_mode: str) -> bool:
            fut.get_loop().call_soon_threadsafe(fut.set_result, "rejected")
            import time

            time.sleep(0.05)
            return True

        monkeypatch.setattr(safety_override_mod, "approval_mode_permitted", permitted)
        assert await approval_picker.handle_trust_call(chan.orch, _click(OWNER, ts))
        return fut

    fut = asyncio.run(click())
    assert fut.result() == "rejected"
    assert chan.slot._trust is False
    assert not [r for r in chan.mode_rows if r["operation"] == "mode_change:trust"]
    assert chan.slack.ephemerals[-1][2] == approval_picker.NOT_LIVE_TEXT


def test_trust_on_a_card_whose_prompt_is_gone_changes_no_mode(chan: Any) -> None:
    ts = _post_card(chan)  # no future registered: the prompt was already answered
    assert asyncio.run(approval_picker.handle_trust_call(chan.orch, _click(OWNER, ts)))
    assert chan.slot._trust is False
    assert _decisions(chan)[-1]["outcome"] == "denied"


def test_a_channel_card_spent_inside_the_mode_change_grants_nothing(
    chan: Any, monkeypatch: Any
) -> None:
    """The prompt is answered during apply_approval_mode's own await (a declared override)."""
    import time

    ts = _post_card(chan)

    async def click() -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        chan.slot._approval_futures[REQUEST] = fut

        def spend() -> None:
            fut.get_loop().call_soon_threadsafe(fut.set_result, "rejected")
            time.sleep(0.05)

        override = SimpleNamespace(
            is_declared=True, deactivate=lambda _who: spend(), is_active=lambda: False
        )
        monkeypatch.setattr(chat_handlers, "safety_override", lambda: override)
        assert await approval_picker.handle_trust_call(chan.orch, _click(OWNER, ts))
        return fut

    fut = asyncio.run(click())
    assert fut.result() == "rejected"
    assert chan.slot._trust is False
    assert chan.state.sessions.policies.get(f"dashboard:{SLOT}", "") == ""
    assert chan.slack.ephemerals[-1][2] == approval_picker.NOT_LIVE_TEXT
