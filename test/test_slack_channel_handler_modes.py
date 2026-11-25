"""A Channel Bot or Channel Conductor beside steer-by-default, ``!bot``'s join and privacy modes."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import test_slack_channel_handler as _channel
from slack_transport_dispatch_helpers import RecordingSlackClient
from test_slack_channel_handler import (
    A_TS,
    B_TS,
    C_TS,
    CHAN,
    STRANGER,
    _bot_slot,
    _posts,
    _run,
    _send,
    _Sessions,
    _start,
    _user_rows,
)

from kiro_crew.members import DM_SLOT_MODE, member_slot_key
from kiro_crew.slack import channel_handler as ch
from kiro_crew.slack import handler as _handler
from kiro_crew.slack.code_channels.store import StoreWriteError

# Assignment exposes the shared fixtures without redefining imported names.
ds = _channel.ds
channel_bot = _channel.channel_bot
conductor_on = _channel.conductor_on
run_chat = _channel.run_chat
bots = _channel.bots
audit = _channel.audit
_store = _channel._store


class TestWithSteerAndBotJoin:
    """Beside steer-by-default and the ``!bot`` join of a Bot's own chat."""

    def test_a_follow_up_in_the_answered_thread_steers_and_another_thread_waits(
        self, ds, run_chat, channel_bot
    ):
        from kiro_crew.dashboard import channel_handoff
        from kiro_crew.slack import midturn

        slot = _bot_slot(ds)
        _run(("one", None, A_TS))
        slot._turn_admission_reserved = True  # the turn for thread A is running
        slack, sessions = RecordingSlackClient(), _Sessions()
        steered = channel_handoff.ResumedBusyOutcome(channel_handoff.HANDOFF_STEERED)

        async def _scenario():
            for text, thread_ts, msg_ts in (
                ("more for A", A_TS, "1700000100.000400"),
                ("two", None, B_TS),
            ):
                midturn.set_inbound(midturn.Inbound(msg_ts, None, True))
                await _send(slack, sessions, text, thread_ts=thread_ts, msg_ts=msg_ts)

        with patch.object(
            channel_handoff, "hand_to_resumed_slot", new_callable=AsyncMock, return_value=steered
        ) as hand:
            asyncio.run(_scenario())
        hand.assert_awaited_once()
        assert hand.await_args.args[2] == "more for A"
        assert slot.queue_depth == 0  # steered into the running turn, not queued
        assert _posts(slack) == [(ch.BUSY_TEXT.format(name="Bot *web-frontend*"), B_TS)]
        assert slot._slack_thread_ts == A_TS

    def test_a_thread_joined_to_the_bots_chat_becomes_one_of_the_channels_threads(
        self, ds, run_chat, channel_bot
    ):
        slot = _bot_slot(ds)
        channel_bot["bot"] = ""
        slack, _ = _run(("!bot web-frontend", None, A_TS))
        assert ds.get_linked_slot(A_TS) is slot, _posts(slack)
        channel_bot["bot"] = "web-frontend"
        slack, sessions = _run(
            ("two", None, B_TS), ("!bot off", A_TS, "1700000100.000700"), ("back in A", A_TS, C_TS)
        )
        # The chat moved to B; ``off`` in A is refused, as every ``!bot`` in this channel.
        assert (ch.handler_refusal(CHAN), A_TS) in _posts(slack)
        assert _user_rows(slot) == ["two", "back in A"] and sessions.calls == []
        assert ds.get_linked_slot(A_TS) is slot

    @pytest.mark.parametrize("handler_kind", ["bot", "conductor"])
    def test_a_thread_joined_to_another_bots_chat_keeps_it_and_off_still_leaves(
        self, ds, conductor_on, run_chat, channel_bot, monkeypatch, handler_kind
    ):
        monkeypatch.setattr(_handler, "is_shared_user", lambda user: user == STRANGER)
        research = ds.get_or_create_slot(
            name=member_slot_key("research"), agent="research", mode=DM_SLOT_MODE
        )
        channel_bot["bot"] = ""
        _run(("!bot research", None, A_TS))
        assert ds.get_linked_slot(A_TS) is research
        if handler_kind == "bot":
            channel_bot["bot"] = "web-frontend"
            handler_chat = _bot_slot(ds)
        else:
            _slack, _binding, handler_chat = _start(ds, B_TS)
        refusal = ch.handler_refusal(CHAN)
        slack, _ = _run(
            ("for research", A_TS, "1700000100.000700"),
            ("!bot web-frontend", A_TS, "1700000100.000800"),
            ("!bot", A_TS, "1700000100.000850"),
            ("!conductor", A_TS, "1700000100.000870"),
            ("!bot off", A_TS, "1700000100.000880", STRANGER),
        )
        assert _user_rows(research) == ["for research"]
        # Starting or listing is still refused here, and leaving is still the owner's.
        assert [ts for text, ts in _posts(slack) if refusal in text] == [A_TS] * 3
        assert _posts(slack)[-1] == ("⛔ Owner-only command.", A_TS)
        assert ds.get_linked_slot(A_TS) is research
        slack, _ = _run(
            ("!bot off", A_TS, "1700000100.000900"), ("now for the channel", A_TS, C_TS)
        )
        assert any("left Bot *research*'s chat" in text for text, _ in _posts(slack))
        assert not research._slack_linked and ds.get_linked_slot(A_TS) is handler_chat
        assert _user_rows(handler_chat)[-1] == "now for the channel"

    @pytest.fixture()
    def privacy(self):
        from kiro_crew.messaging import privacy_mode

        privacy_mode.reset()
        yield privacy_mode
        privacy_mode.reset()

    @pytest.mark.parametrize("mode", ["temporary", "incognito"])
    @pytest.mark.parametrize("handler_kind", ["bot", "conductor"])
    def test_a_private_thread_stays_off_the_channels_chat(
        self, ds, conductor_on, run_chat, channel_bot, privacy, mode, handler_kind
    ):
        if handler_kind == "bot":
            handler_chat = _bot_slot(ds)
        else:
            channel_bot["bot"] = ""
            _slack, _binding, handler_chat = _start(ds)
        _run(("one", None, A_TS))
        privacy.mark(mode, f"slack:{B_TS}")  # the owner sent the modifier in thread B
        slack, sessions = _run(("keep this off the record", B_TS, "1700000200.000300"))
        # Thread B ran on its own session, where the mode holds; the chat did not move.
        assert _user_rows(handler_chat) == ["one"] and ds.get_linked_slot(B_TS) is None
        assert handler_chat._slack_thread_ts == A_TS and len(sessions.calls) == 1
        assert _posts(slack) == []
        if handler_kind == "conductor":
            assert ch.channel_conductor(CHAN).anchor_ts == A_TS

    @pytest.mark.parametrize("modifier", ["!temporary", "!incognito"])
    @pytest.mark.parametrize("commands", [True, False], ids=["typed", "click"])
    def test_a_message_that_carries_the_modifier_is_not_claimed(
        self, ds, channel_bot, privacy, modifier, commands
    ):
        text = f"what is the plan {modifier}"
        # A click's label is never read as a modifier, here or on the thread's own route.
        assert asyncio.run(ch.may_claim(CHAN, text, commands=commands, reply_ts=B_TS)) is (
            not commands
        )
        assert asyncio.run(ch.may_claim(CHAN, "what is the plan", commands=commands, reply_ts=B_TS))

    @pytest.mark.parametrize("handler_kind", ["bot", "conductor"])
    def test_a_modifier_applied_during_the_claim_keeps_the_thread_private(
        self, ds, conductor_on, run_chat, channel_bot, privacy, monkeypatch, handler_kind
    ):
        if handler_kind == "bot":
            handler_chat = _bot_slot(ds)
        else:
            channel_bot["bot"] = ""
            _slack, _binding, handler_chat = _start(ds)
        _run(("one", None, A_TS))

        async def _probe(state, probed, key, operation):
            # The owner's ``!incognito`` lands on thread B while the probe is awaited.
            privacy.mark("incognito", f"slack:{B_TS}")
            return False

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils_completion.subagents_attached_async", _probe
        )
        slack, sessions = _run(("two", None, B_TS))
        assert _user_rows(handler_chat) == ["one"] and ds.get_linked_slot(B_TS) is None
        assert handler_chat._slack_thread_ts == A_TS and len(sessions.calls) == 1
        assert _posts(slack) == []
        if handler_kind == "conductor":
            assert ch.channel_conductor(CHAN).anchor_ts == A_TS  # the binding went back

    @pytest.mark.parametrize("lands", ["move", "rollback"])
    def test_a_private_thread_is_not_linked_when_the_binding_cannot_go_back(
        self, ds, conductor_on, run_chat, channel_bot, privacy, _store, monkeypatch, lands
    ):
        channel_bot["bot"] = ""
        _slack, _binding, slot = _start(ds)
        move = _store.move_thread_binding

        def _move_then_private(channel, from_ts, to_ts):
            if to_ts == A_TS:
                if lands == "rollback":
                    privacy.mark("incognito", f"slack:{B_TS}")
                raise StoreWriteError("disk full")
            moved = move(channel, from_ts, to_ts)
            if lands == "move":
                privacy.mark("incognito", f"slack:{B_TS}")  # the modifier lands meanwhile
            else:
                slot._turn_admission_reserved = True  # a turn starts; the rollback is tried
            return moved

        monkeypatch.setattr(_store, "move_thread_binding", _move_then_private)
        slack, sessions = _run(("two", None, B_TS))
        # Fails closed: nothing runs, and the saved chat is linked to no thread.
        assert _posts(slack) == [(ch.NOT_SAVED_TEXT.format(name="The Channel Conductor"), B_TS)]
        assert ds.get_linked_slot(B_TS) is None and ds.get_linked_slot(A_TS) is None
        assert not slot._slack_linked and sessions.calls == []
        run_chat.assert_not_awaited()

    @pytest.mark.parametrize("text", ["!incognito", "!temporary the secret plan"])
    def test_a_modifier_in_the_thread_the_chat_answers_is_refused(
        self, ds, run_chat, channel_bot, privacy, text
    ):
        slot = _bot_slot(ds)
        _run(("one", None, A_TS))
        run_chat.reset_mock()
        slack, sessions = _run((text, A_TS, "1700000100.000400"))
        # Not appended to the saved chat, and not run anywhere.
        assert _posts(slack) == [(ch.PRIVATE_TEXT, A_TS)]
        assert _user_rows(slot) == ["one"] and sessions.calls == []
        assert not privacy.is_restricted(f"slack:{A_TS}")
        run_chat.assert_not_awaited()
