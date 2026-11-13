"""An answered OPTIONS card keeps its "✅ <who> chose <X>" line when the next turn starts.

A card posted top-level -- not in its conversation's thread, as a code channel's own
session posts its replies -- carries no ``thread_ts``. The click handler then falls
back to the card's own ts as the thread, which no session owns, so forgetting the
card's record by the thread's owners missed it. The answer's own turn start then
drained the record and struck every choice through over the answer the click had just
rendered.

Two guards, each pinned here on its own: the click forgets the control by its message
(channel + ts) whatever key it was filed under, and an expiry never edits a control
whose answer is already on screen.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from slack_options_lifecycle_helpers import _recs

from kiro_crew.slack import interactions, outbound
from kiro_crew.slack.format import OPTIONS_ACTION_PREFIX, build_options_blocks
from kiro_crew.slack.outbound import PostedOptions

_CHANNEL = "C-room"
_THREAD = "1700000000.000100"
_CARD = "1700000000.000200"
_CHOICES = ["Nudge the stuck chat again", "Stop the other chat"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-7-1700000000")
    state.link_slack(slot.key, _THREAD, _CHANNEL)

    slack = MagicMock()
    slack.update_message = AsyncMock()
    slack.post_blocks = AsyncMock(return_value="new_ts")
    slack.delete_message = AsyncMock()
    slack.post_ephemeral = AsyncMock()
    state.slack_client = slack

    orch = MagicMock()
    orch.slack = slack
    orch.dashboard_state = state
    orch._handler_tasks = set()
    monkeypatch.setattr(interactions, "_orch", orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: uid == "U1")
    monkeypatch.setattr(interactions, "handle_message", AsyncMock())
    monkeypatch.setattr(outbound, "_ANSWERED", {})
    return state, slack, effective_session_key(slot)


def _post_card(state, session_key: str, *, thread_ts: str | None) -> dict:
    """Record the card under the session that posted it; return a click on its first choice."""
    from kiro_crew.dashboard.chat_utils import remember_slack_options

    blocks = build_options_blocks(_CHOICES)
    remember_slack_options(
        state,
        session_key,
        PostedOptions(channel=_CHANNEL, ts=_CARD, choices=tuple(_CHOICES), blocks=tuple(blocks)),
    )
    message: dict = {"ts": _CARD, "blocks": blocks}
    if thread_ts:
        message["thread_ts"] = thread_ts
    button = next(
        el
        for b in blocks
        if b.get("type") == "actions"
        for el in b["elements"]
        if el.get("action_id", "").startswith(OPTIONS_ACTION_PREFIX)
    )
    return {
        "type": "block_actions",
        "user": {"id": "U1"},
        "team": {"id": "T1"},
        "channel": {"id": _CHANNEL},
        "message": message,
        "actions": [dict(button)],
    }


def _card_edits(slack) -> list:
    return [c for c in slack.update_message.await_args_list if c.args[1] == _CARD]


def _text_of(call) -> str:
    return " ".join(
        el.get("text", "")
        for b in call.kwargs["blocks"]
        if b.get("type") in ("context", "section")
        for el in (b.get("elements") or [b.get("text") or {}])
        if isinstance(el, dict)
    )


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class TestAnAnsweredCardSurvivesTheNextTurn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("thread_ts", [None, _THREAD], ids=["top-level", "threaded"])
    async def test_the_answer_turns_start_keeps_the_chose_line(self, world, thread_ts):
        from kiro_crew.dashboard.chat_utils import expire_slack_options

        state, slack, session_key = world
        await interactions.dispatch(_post_card(state, session_key, thread_ts=thread_ts))
        await _settle()
        assert "chose" in _text_of(_card_edits(slack)[-1])

        # The answer's own turn starts and drains the session's outstanding controls.
        await expire_slack_options(state, session_key)

        text = _text_of(_card_edits(slack)[-1])
        assert "chose" in text and "~" not in text, "the turn start struck the answer: " + text
        assert len(_card_edits(slack)) == 1, "nothing may edit the card after its answer"

    @pytest.mark.asyncio
    async def test_a_top_level_click_forgets_the_record_wherever_it_is_filed(self, world):
        state, _, session_key = world
        await interactions.dispatch(_post_card(state, session_key, thread_ts=None))
        await _settle()
        assert _recs(state, session_key) == (), "the answered card's record outlived the click"


class TestTheExpiryNeverWritesOverAnAnswer:
    @pytest.mark.asyncio
    async def test_an_expiry_over_an_answered_card_keeps_the_chose_line(self, world, monkeypatch):
        """Whatever leaves the record behind, the expiry must not strike the answer."""
        from kiro_crew.dashboard.chat_utils import expire_slack_options

        state, slack, session_key = world
        # Leave the record in place, as any forget that misses its key would.
        monkeypatch.setattr(interactions, "_forget_options_control", MagicMock())
        await interactions.dispatch(_post_card(state, session_key, thread_ts=_THREAD))
        await _settle()
        assert _recs(state, session_key), "precondition: the record survived the click"

        await expire_slack_options(state, session_key)

        edits = _card_edits(slack)
        assert len(edits) == 1 and "chose" in _text_of(edits[0]), [_text_of(c) for c in edits]
        assert _recs(state, session_key) == (), "the spent record is dropped, not retried"

    @pytest.mark.asyncio
    async def test_a_claim_whose_buttons_are_still_on_screen_is_still_struck(self, world):
        """An answer whose original survived (a fallback whose delete failed) is not settled."""
        from kiro_crew.dashboard.chat_utils import expire_slack_options

        state, slack, session_key = world
        _post_card(state, session_key, thread_ts=_THREAD)
        assert outbound.claim_options_answer(_CHANNEL, _CARD)  # claimed, never settled

        await expire_slack_options(state, session_key)

        edits = _card_edits(slack)
        assert len(edits) == 1 and "~" in _text_of(edits[0]), "the live buttons must be spent"

    @pytest.mark.asyncio
    async def test_a_stale_click_whose_strike_failed_is_still_struck_later(
        self, world, monkeypatch
    ):
        """A refused stale click settles the control only once its strike landed."""
        from slack_sdk.errors import SlackApiError

        from kiro_crew.dashboard.chat_utils import expire_slack_options

        state, slack, session_key = world

        async def _stale(*_a, **_k):
            return True

        monkeypatch.setattr(interactions, "options_control_is_stale", _stale)
        payload = _post_card(state, session_key, thread_ts=_THREAD)
        slack.update_message.side_effect = SlackApiError("ratelimited", MagicMock(status_code=429))
        await interactions.dispatch(payload)
        await _settle()
        interactions.handle_message.assert_not_called()
        assert not outbound.options_answer_settled(_CHANNEL, _CARD), "nothing was struck"

        slack.update_message.side_effect = None
        await expire_slack_options(state, session_key)

        edits = _card_edits(slack)
        assert "~" in _text_of(edits[-1]), "the buttons still on screen must be spent"
        assert _recs(state, session_key) == ()
