"""The sign-in hold: what a failed turn marks, and the send that lets the queue try again."""

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


def _held(slot, *texts, **kwargs) -> None:
    slot._last_turn_auth_required = True
    for text in texts:
        slot.queue_append(text, **kwargs)
    mark_signout_retained(slot)


class TestSignoutHoldActive:
    def test_no_failure_holds_nothing(self, slot):
        slot.queue_append("anything")
        mark_signout_retained(slot)
        assert signout_hold_active(slot) is False

    @pytest.mark.parametrize(
        "after, kind, user, held",
        [
            ("", "", False, True),
            ("sent after signing in", "", True, False),
            ("[Cron notification] digest", CRON_NOTIFICATION_KIND, False, True),
            ("from another session", "", False, True),
        ],
    )
    def test_only_a_persons_later_send_releases_it(self, slot, after, kind, user, held):
        _held(slot, "retained one", "retained two")
        if after:
            slot.queue_append(after, kind, directive_user_origin=user)
        assert signout_hold_active(slot) is held

    @pytest.mark.parametrize("actor", ["cron", "crew", "app"])
    def test_a_machine_send_does_not_release_it(self, slot, actor):
        slot._last_turn_auth_required = True
        slot.queue_append("script", meta={TURN_ACTOR_META_KEY: actor}, directive_user_origin=True)
        assert signout_hold_active(slot) is True


@pytest.mark.asyncio
async def test_the_failing_turn_marks_what_it_leaves_queued(state, slot, monkeypatch):
    slot.queue_append("behind the failing turn", directive_user_origin=True)
    slot.append("user", "the failing turn", "msg msg-u")
    _signed_out(state, monkeypatch)
    await chat_runner._run_chat(state, slot, "the failing turn", _directive_user_origin=True)
    assert [i["content"] for i in slot._queue] == ["the failing turn", "behind the failing turn"]
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
    ran = _discard_ready(state, slot, monkeypatch)
    _held(slot, "retained")
    slot.queue_append("sent after signing in", directive_user_origin=True)
    chat_runner._arm_pending_reset_retry(state, slot)
    retry = state._pending_reset_retries[slot.key]
    try:
        await async_wait_until(
            lambda: bool(ran), interval=0.001, describe=lambda: "the release to drain"
        )
    finally:
        retry[1].cancel()
    assert ran == ["retained"], "the queue did not try again, in order"


@pytest.mark.asyncio
async def test_a_landed_discard_with_only_retained_entries_drains_nothing(state, slot, monkeypatch):
    ran = _discard_ready(state, slot, monkeypatch)
    _held(slot, "retained", directive_user_origin=True)
    chat_runner._arm_pending_reset_retry(state, slot)
    retry = state._pending_reset_retries[slot.key]
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
        quiet_end=False,
    )
    try:
        await chat_runner._end_turn_tail(state, slot, turn_exit)
    finally:
        outer.cancel()
    (drain,) = seen
    return drain


@pytest.mark.asyncio
async def test_the_completion_hint_counts_a_send_queued_after_the_failure(state, slot, monkeypatch):
    from kiro_crew.dashboard import chat_utils_completion
    from kiro_crew.dashboard.chat_utils_completion import chat_done_payload

    monkeypatch.setattr(
        chat_utils_completion, "subagents_attached_async", AsyncMock(return_value=False)
    )
    _held(slot, "retained")
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
@pytest.mark.parametrize("probe", [_turn_end_drains, _unstarted_cycle_drains])
@pytest.mark.parametrize("sent_after, drains", [(False, False), (True, True)])
async def test_a_cycle_end_drains_only_once_a_send_follows_the_failure(
    state, slot, monkeypatch, probe, sent_after, drains
):
    _held(slot, "retained", directive_user_origin=True)
    if sent_after:
        slot.queue_append("sent after the failure", directive_user_origin=True)
    assert await probe(state, slot, monkeypatch) is drains
