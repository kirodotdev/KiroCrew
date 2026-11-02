"""The sign-in hold: what a failed turn marks, and the send that lets the queue try again.

Each test below names one case the hold must get right.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state
from test_signout_put_back import _signed_out

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
from kiro_crew.dashboard.chat_utils_injected_rows import (
    CRON_NOTIFICATION_KIND,
    mark_signout_retained,
    signout_hold_active,
)
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


@pytest.fixture
def slot(state):
    return state.get_or_create_slot("s1")


class TestSignoutHoldActive:
    def test_no_failure_holds_nothing(self, slot):
        slot.queue_append("anything")
        mark_signout_retained(slot)

        assert signout_hold_active(slot) is False

    def test_a_retained_queue_is_held(self, slot):
        slot._last_turn_auth_required = True
        slot.queue_append("retained")
        mark_signout_retained(slot)

        assert signout_hold_active(slot) is True

    def test_a_send_behind_retained_entries_releases_it(self, slot):
        """A send queued behind retained prompts releases the hold, not
        only a send at the queue head."""
        slot._last_turn_auth_required = True
        slot.queue_append("retained one")
        slot.queue_append("retained two")
        mark_signout_retained(slot)
        slot.queue_append("sent after signing in", directive_user_origin=True)

        assert signout_hold_active(slot) is False

    def test_a_cron_arrival_does_not_release_it(self, slot):
        slot._last_turn_auth_required = True
        slot.queue_append("retained")
        mark_signout_retained(slot)
        slot.queue_append("[Cron notification] digest", CRON_NOTIFICATION_KIND)

        assert signout_hold_active(slot) is True

    @pytest.mark.parametrize("actor", ["cron", "crew"])
    def test_a_machine_send_does_not_release_it(self, slot, actor):
        slot._last_turn_auth_required = True
        slot.queue_append("from a script", meta={TURN_ACTOR_META_KEY: actor})

        assert signout_hold_active(slot) is True

    def test_an_app_send_does_not_release_it(self, slot):
        slot._last_turn_auth_required = True
        slot.queue_append("from an app", meta={TURN_ACTOR_META_KEY: "app"})

        assert signout_hold_active(slot) is True


@pytest.mark.asyncio
async def test_the_failing_turn_marks_what_it_leaves_queued(state, slot, monkeypatch):
    slot.queue_append("behind the failing turn", directive_user_origin=True)
    slot.append("user", "the failing turn", "msg msg-u")
    _signed_out(state, monkeypatch)

    await chat_runner._run_chat(state, slot, "the failing turn", _directive_user_origin=True)

    assert [item["content"] for item in slot._queue] == [
        "the failing turn",
        "behind the failing turn",
    ]
    assert signout_hold_active(slot) is True


def _discard_ready(state, slot, monkeypatch) -> list[str]:
    monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 0)
    slot._pending_discard_conversation_key = "s1"
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    ran: list[str] = []

    async def fake_run_chat(_st, _sl, msg, **_kwargs):
        ran.append(msg)

    monkeypatch.setattr(chat_runner, "_run_chat", fake_run_chat)
    return ran


@pytest.mark.asyncio
async def test_a_post_sign_in_send_behind_a_discard_drains_when_it_lands(state, slot, monkeypatch):
    """The person's send after signing in, queued behind the discard, runs
    when the discard lands rather than waiting for a second send."""
    ran = _discard_ready(state, slot, monkeypatch)
    slot._last_turn_auth_required = True
    slot.queue_append("retained")
    mark_signout_retained(slot)
    slot.queue_append("sent after signing in", directive_user_origin=True)

    chat_runner._arm_pending_reset_retry(state, slot)
    retry = chat_runner._pending_reset_retries[slot.key]
    try:
        await async_wait_until(
            lambda: bool(ran), interval=0.001, describe=lambda: "the release to drain"
        )
    finally:
        retry[1].cancel()

    assert ran == ["retained"], "the queue did not try again, in order"


@pytest.mark.asyncio
async def test_a_landed_discard_with_only_retained_entries_drains_nothing(state, slot, monkeypatch):
    """Rounds 8 and 9: the discard can land with no sign-in at all, by a
    turn's own end or by the timer, and must not drain retained prompts."""
    ran = _discard_ready(state, slot, monkeypatch)
    slot._last_turn_auth_required = True
    slot.queue_append("retained", directive_user_origin=True)
    mark_signout_retained(slot)

    chat_runner._arm_pending_reset_retry(state, slot)
    retry = chat_runner._pending_reset_retries[slot.key]
    try:
        await async_wait_until(
            lambda: slot._pending_discard_conversation_key is None,
            interval=0.001,
            describe=lambda: "the retry to land the discard",
        )
    finally:
        retry[1].cancel()

    assert ran == []
    assert [item["content"] for item in slot._queue] == ["retained"]


async def _turn_end_drains(state, slot, monkeypatch) -> bool:
    """Run the real end-of-turn tail after a sign-in failure; return its drain verdict."""
    import asyncio

    from kiro_crew.start_priority import StartPriority

    seen: list[bool] = []

    async def capture(_st, _sl, *, drain, **_kwargs):
        seen.append(drain)

    monkeypatch.setattr(chat_runner, "_hand_off_queue", capture)
    monkeypatch.setattr(chat_runner, "schedule_eager_spawn", lambda *_a, **_kw: None)
    outer = asyncio.ensure_future(asyncio.Event().wait())
    slot.task = outer
    turn_exit = chat_runner._TurnExit()
    turn_exit.outcome = chat_runner._TurnOutcome(
        auth_required=True,
        memory_admitted=True,
        start_priority=StartPriority.BACKGROUND,
        nudge_facts={"reply_text": ""},
    )
    try:
        await chat_runner._end_turn_tail(state, slot, turn_exit)
    finally:
        outer.cancel()
    (drain,) = seen
    return drain


@pytest.mark.asyncio
async def test_the_turn_end_holds_a_queue_the_failure_marked(state, slot, monkeypatch):
    slot.queue_append("retained", directive_user_origin=True)
    mark_signout_retained(slot)

    assert await _turn_end_drains(state, slot, monkeypatch) is False


@pytest.mark.asyncio
async def test_the_turn_end_drains_a_send_queued_after_the_failure(state, slot, monkeypatch):
    slot.queue_append("retained", directive_user_origin=True)
    mark_signout_retained(slot)
    slot.queue_append("sent after the failure", directive_user_origin=True)

    assert await _turn_end_drains(state, slot, monkeypatch) is True


@pytest.mark.asyncio
async def test_the_completion_hint_counts_a_send_queued_after_the_failure(state, slot, monkeypatch):
    from kiro_crew.dashboard import chat_utils_completion
    from kiro_crew.dashboard.chat_utils_completion import chat_done_payload

    monkeypatch.setattr(
        chat_utils_completion, "subagents_attached_async", AsyncMock(return_value=False)
    )
    slot._last_turn_auth_required = True
    slot.queue_append("retained")
    mark_signout_retained(slot)
    slot.queue_append("sent after the failure", directive_user_origin=True)

    assert (await chat_done_payload(state, slot))["continuing"] is True


async def _unstarted_cycle_drains(state, slot, monkeypatch) -> bool:
    seen: list[bool] = []

    async def capture(_st, _sl, *, drain, **_kwargs):
        seen.append(drain)

    monkeypatch.setattr(chat_runner, "_hand_off_queue", capture)
    monkeypatch.setattr(chat_runner, "_memory_preparation_settled", lambda _state: True)
    await chat_runner._end_unstarted_cycle(state, slot)
    (drain,) = seen
    return drain


@pytest.mark.asyncio
async def test_the_unstarted_cycle_holds_a_queue_the_failure_marked(state, slot, monkeypatch):
    slot._last_turn_auth_required = True
    slot.queue_append("retained", directive_user_origin=True)
    mark_signout_retained(slot)

    assert await _unstarted_cycle_drains(state, slot, monkeypatch) is False


@pytest.mark.asyncio
async def test_the_unstarted_cycle_drains_a_send_queued_after_the_failure(state, slot, monkeypatch):
    slot._last_turn_auth_required = True
    slot.queue_append("retained", directive_user_origin=True)
    mark_signout_retained(slot)
    slot.queue_append("sent after the failure", directive_user_origin=True)

    assert await _unstarted_cycle_drains(state, slot, monkeypatch) is True
