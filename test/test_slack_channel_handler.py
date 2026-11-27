"""One Bot or one Conductor answers a whole Slack channel (``slack/channel_handler.py``).

``!channel bot <name>`` puts a channel on a Bot's own chat and ``!channel conductor on``
on one Conductor chat. A message in any thread there, new or old, reaches that one chat,
and its reply is posted in the thread the message came from: the chat's Slack link (and
a Conductor's binding) moves to the message's thread before the message is routed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import test_slack_thread_bot_switch as _switch
import test_slack_thread_conductor_runtime as _thread
from chat_test_helpers import _make_state
from slack_transport_dispatch_helpers import RecordingSlackClient
from test_slack_thread_bot_switch import _OWNER, _Sessions

from kiro_crew.members import DM_SLOT_MODE, member_slot_key
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.slack import channel_handler as ch
from kiro_crew.slack import conductor_thread as ct
from kiro_crew.slack import handler as _handler
from kiro_crew.slack import transport_dispatch
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import surface as cc_surface
from kiro_crew.slack.code_channels.models import ChannelInfo
from kiro_crew.slack.code_channels.store import (
    ROOM_PRIVATE,
    Binding,
    CodeChannelStore,
    StoreWriteError,
    set_store,
    store,
)
from kiro_crew.testing.wait import await_until, until_parked

# Bound by assignment: pyflakes reads an imported fixture name used as a parameter as
# a redefinition.
bots = _switch.bots
audit = _switch.audit
env_fixture = _thread.env_fixture
thread = _thread.thread

CHAN = "C0PRIVATE01"
TEAM = "T0TEAM00001"
STRANGER = "U0STRANGER1"
A_TS = "1700000100.000100"
B_TS = "1700000200.000100"
C_TS = "1700000300.000100"


@pytest.fixture(autouse=True)
def _store(tmp_path):
    current = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    current.load()
    set_store(current)
    ct.forget_told()
    ch._locks.clear()
    yield current
    set_store(None)
    ct.forget_told()
    ch._locks.clear()


@pytest.fixture()
def ds(tmp_path, monkeypatch, bots, audit):
    """A dashboard with Bot web-frontend's own chat open, as the Bots page leaves it."""
    state = _make_state(tmp_path)
    plain = state.sessions.get_mirror_link.side_effect

    def _mirror(key):
        held = plain(key)
        if held is not None:
            return held
        ts, chan = state.sessions.get_slack_link(key)
        return ChannelLink("slack", channel_id=chan, thread_id=ts) if ts else None

    state.sessions.get_mirror_link = MagicMock(side_effect=_mirror)
    state.get_or_create_slot(
        name=member_slot_key("web-frontend"), agent="web-frontend", mode=DM_SLOT_MODE
    )
    monkeypatch.setattr(_handler, "_dashboard_state", state)
    return state


@pytest.fixture()
def run_chat():
    with patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock:
        yield mock


@pytest.fixture()
def channel_bot(monkeypatch):
    """The channel's ``slack.channels.<id>.bot`` value, settable per test."""
    current = {"bot": "web-frontend"}
    monkeypatch.setattr(
        ch, "channel_bot", lambda channel: "" if channel.startswith("D") else current["bot"]
    )
    return current


async def _send(slack, sessions, text, *, channel=CHAN, user_id=_OWNER, thread_ts=None, msg_ts):
    await transport_dispatch.handle_message_transport(
        slack=slack,
        sessions=sessions,
        channel=channel,
        text=text,
        thread_ts=thread_ts,
        msg_ts=msg_ts,
        user_id=user_id,
        context_builder=None,
        conversation_log=None,
    )


def _run(*messages, slack=None, sessions=None):
    """Send each ``(text, thread_ts, msg_ts[, user_id])``; let started turns finish."""
    slack = slack or RecordingSlackClient()
    sessions = sessions or _Sessions()

    async def _scenario():
        for text, thread_ts, msg_ts, *user in messages:
            await _send(
                slack,
                sessions,
                text,
                thread_ts=thread_ts,
                msg_ts=msg_ts,
                user_id=user[0] if user else _OWNER,
            )
            await asyncio.sleep(0)

    asyncio.run(_scenario())
    return slack, sessions


def _posts(slack):
    return [
        (kw["text"], kw.get("thread_ts")) for (m, kw) in slack.transcript if m == "post_message"
    ]


def _user_rows(slot):
    return [m["content"] for m in slot.messages if m.get("role") == "user"]


def _bot_slot(ds):
    return ds._slots[member_slot_key("web-frontend")]


@pytest.fixture()
def options_world(ds, monkeypatch, audit):
    from kiro_crew.slack import interactions, outbound

    orch = MagicMock()
    orch.slack = RecordingSlackClient()
    orch.sessions = _Sessions()
    orch.dashboard_state = ds
    orch._handler_tasks = set()
    orch._approval_mode = "auto"
    for name in (
        "ctx_builder",
        "cron_svc",
        "conv_log",
        "consolidator",
        "subagent_mgr",
        "task_runner",
    ):
        setattr(orch, name, None)
    monkeypatch.setattr(interactions, "_orch", orch)
    monkeypatch.setattr(interactions, "sel", lambda: audit)
    monkeypatch.setattr(interactions, "options_control_is_stale", AsyncMock(return_value=False))
    monkeypatch.setattr(outbound, "_ANSWERED", {})
    return orch


async def _click_handler_option(orch, label, *, pinned=False):
    from kiro_crew.slack import interactions_options
    from kiro_crew.slack.format import build_options_blocks
    from kiro_crew.slack.outbound import encode_options_token

    token = (
        encode_options_token("dashboard:" + _bot_slot(orch.dashboard_state).key, "question")
        if pinned
        else None
    )
    blocks = build_options_blocks([label], staleness_token=token)
    action = dict(blocks[0]["elements"][0])
    if pinned:
        action["block_id"] = blocks[0]["block_id"]
    else:
        action["action_id"] = "options_choice_deploy"
        action["value"] = 'action::{"deploy": "preview"}'
    payload = {
        "user": {"id": _OWNER},
        "team": {"id": TEAM},
        "message": {"thread_ts": A_TS, "blocks": blocks},
    }
    await interactions_options._handle_options(payload, action, CHAN, "1700000100.000200")
    assert orch._handler_tasks
    await await_until(asyncio.gather(*orch._handler_tasks))
    if orch.dashboard_state._background_tasks:
        await await_until(asyncio.gather(*orch.dashboard_state._background_tasks))


class TestChannelHandlerOptions:
    @pytest.mark.parametrize("label", ["Deploy", "!deploy", "sessions"])
    def test_unpinned_action_claims_the_bot_chat_with_payload(
        self, ds, run_chat, channel_bot, options_world, label
    ):
        slot = _bot_slot(ds)
        _run(("question A", None, A_TS), ("question B", None, B_TS))
        seen = []

        async def _turn(state, target, text, **kw):
            seen.append((target, text, target._slack_thread_ts, list(target._pending_context), kw))

        run_chat.side_effect = _turn
        asyncio.run(_click_handler_option(options_world, label))
        assert _user_rows(slot) == ["question A", "question B", label]
        assert len(seen) == 1
        target, text, thread_ts, context, kwargs = seen[0]
        assert (target, text, thread_ts) == (slot, label, A_TS)
        assert '[Action button clicked: {"deploy": "preview"}]' in str(context)
        assert kwargs["_commands_off"] is True
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (A_TS, CHAN)
        assert options_world.sessions.calls == []

    def test_busy_unpinned_action_refuses_without_moving_the_bot_chat(
        self, ds, run_chat, channel_bot, options_world
    ):
        slot = _bot_slot(ds)
        _run(("question A", None, A_TS), ("question B", None, B_TS))
        slot._turn_admission_reserved = True
        run_chat.reset_mock()
        asyncio.run(_click_handler_option(options_world, "Deploy"))
        assert (ch.BUSY_TEXT.format(name="Bot *web-frontend*"), A_TS) in _posts(options_world.slack)
        assert _user_rows(slot) == ["question A", "question B"]
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (B_TS, CHAN)
        assert ds.get_linked_slot(B_TS) is slot and ds.get_linked_slot(A_TS) is None
        assert slot.queue_depth == 0 and not slot._pending_context
        assert options_world.sessions.calls == []
        run_chat.assert_not_awaited()

    def test_pinned_option_moves_the_bot_link_before_the_turn_starts(
        self, ds, run_chat, channel_bot, options_world
    ):
        slot = _bot_slot(ds)
        _run(("question A", None, A_TS), ("question B", None, B_TS))
        seen = []

        async def _turn(state, target, text, **kw):
            seen.append((target, text, target._slack_thread_ts))
            await options_world.slack.post_message(CHAN, "answer", target._slack_thread_ts)

        run_chat.side_effect = _turn
        asyncio.run(_click_handler_option(options_world, "Deploy", pinned=True))
        assert seen == [(slot, "Deploy", A_TS)]
        assert _user_rows(slot) == ["question A", "question B", "Deploy"]
        assert ("answer", A_TS) in _posts(options_world.slack)
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (A_TS, CHAN)
        assert options_world.sessions.calls == []


# ── Channel Bot ───────────────────────────────────────────────────────────


class TestChannelBot:
    def test_a_new_thread_and_an_old_one_reach_the_same_bot_chat(self, ds, run_chat, channel_bot):
        slot = _bot_slot(ds)
        _slack, sessions = _run(
            ("first question", None, A_TS),  # a new thread
            ("second question", None, B_TS),  # another new thread
            ("back in the first", A_TS, "1700000100.000200"),  # a reply in an old thread
        )
        assert _user_rows(slot) == ["first question", "second question", "back in the first"]
        assert [c.args[1] for c in run_chat.await_args_list] == [slot, slot, slot]
        assert sessions.calls == []  # no per-thread session ran
        # Each turn started while the chat was linked to the message's own thread, so its
        # reply is posted there; the link is where the last message came from.
        assert slot._slack_thread_ts == A_TS and slot._slack_channel == CHAN
        assert ds.get_linked_slot(A_TS) is slot and ds.get_linked_slot(B_TS) is None
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (A_TS, CHAN)

    def test_each_turn_starts_linked_to_its_own_thread(self, ds, channel_bot):
        slot = _bot_slot(ds)
        seen = []

        async def _turn(state, s, text, **kw):
            seen.append((text, s._slack_thread_ts))

        with patch("kiro_crew.dashboard.chat._run_chat", side_effect=_turn):
            _run(("one", None, A_TS), ("two", None, B_TS), ("three", A_TS, "1700000100.000300"))
        assert seen == [("one", A_TS), ("two", B_TS), ("three", A_TS)]
        assert slot.queue_depth == 0

    def test_a_busy_chat_tells_another_thread_to_wait(self, ds, run_chat, channel_bot):
        slot = _bot_slot(ds)
        _run(("one", None, A_TS))
        slot._turn_admission_reserved = True  # a turn for thread A is running
        slack, _ = _run(("two", None, B_TS), ("more for A", A_TS, "1700000100.000400"))
        assert (ch.BUSY_TEXT.format(name="Bot *web-frontend*"), B_TS) in _posts(slack)
        assert slot._slack_thread_ts == A_TS  # not moved: A's reply still lands in A
        assert _user_rows(slot) == ["one", "more for A"]  # A's follow-up queued behind it
        assert [e["content"] for e in slot._queue] == ["more for A"]

    def test_a_chat_with_attached_subagents_is_busy_for_another_thread(
        self, ds, run_chat, channel_bot
    ):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = _bot_slot(ds)
        _run(("one", None, A_TS))
        ds.subagents = MagicMock()
        ds.subagents.running_agents_for.return_value = ["sub-1"]  # a sub-agent it spawned
        run_chat.reset_mock()
        slack, _ = _run(("two", None, B_TS))
        ds.subagents.running_agents_for.assert_called_with(effective_session_key(slot))
        assert _posts(slack) == [(ch.BUSY_TEXT.format(name="Bot *web-frontend*"), B_TS)]
        assert slot._slack_thread_ts == A_TS
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (A_TS, CHAN)
        run_chat.assert_not_awaited()

    def test_a_closed_bot_chat_says_so_and_runs_nothing(self, ds, run_chat, channel_bot):
        ds._slots.pop(member_slot_key("web-frontend"))
        slack, sessions = _run(("hi", None, A_TS))
        assert _posts(slack) == [(ch.BOT_CLOSED_TEXT.format(bot="web-frontend"), A_TS)]
        run_chat.assert_not_awaited()
        assert sessions.calls == []

    def test_a_bot_chat_linked_to_another_channel_is_not_taken(self, ds, run_chat, channel_bot):
        slot = _bot_slot(ds)
        ds.link_slack(slot.key, C_TS, "C0OTHER0001")
        slack, _ = _run(("hi", None, A_TS))
        assert "linked to <#C0OTHER0001>" in _posts(slack)[0][0]
        assert slot._slack_thread_ts == C_TS
        run_chat.assert_not_awaited()

    def test_a_thread_linked_to_another_chat_keeps_its_link(self, ds, run_chat, channel_bot):
        other = ds.get_or_create_slot(name="chat-other")
        ds.link_slack(other.key, A_TS, CHAN)
        _run(("for the other chat", A_TS, "1700000100.000500"))
        assert _user_rows(other) == ["for the other chat"]
        assert _user_rows(_bot_slot(ds)) == []

    @pytest.mark.parametrize("own_link", [False, True])
    def test_a_persisted_dashboard_owner_is_not_displaced(
        self, ds, run_chat, channel_bot, monkeypatch, own_link
    ):
        slot = _bot_slot(ds) if own_link else ds.get_or_create_slot(name="chat-other")
        owner = f"dashboard:{slot.key}"
        ds.link_slack(slot.key, A_TS, CHAN)
        ds._slack_to_slot.pop(A_TS)
        if not own_link:
            ds._slots.pop(slot.key)
        monkeypatch.setattr(ds.sessions, "get_session_for_thread", lambda ts: owner)
        refused = AsyncMock(return_value=True)
        monkeypatch.setattr(_handler, "closed_dashboard_owner_refused", refused)

        _run(("reply", A_TS, B_TS))

        assert ds.sessions.get_slack_link(owner) == (A_TS, CHAN)
        if own_link:
            assert ds.get_linked_slot(A_TS) is slot
            assert _user_rows(slot) == ["reply"]
            refused.assert_not_awaited()
        else:
            refused.assert_awaited_once()
            assert _bot_slot(ds)._slack_thread_ts == ""
            run_chat.assert_not_awaited()

    def test_commands_are_not_claimed(self, ds, run_chat, channel_bot):
        slack, _ = _run(("!channel", None, A_TS))
        assert _posts(slack)[-1][0].startswith("This channel is answered by Bot *web-frontend*.")
        assert _bot_slot(ds)._slack_thread_ts == ""
        run_chat.assert_not_awaited()

    def test_sessions_prose_is_claimed_but_the_keyword_is_not(self, ds, run_chat, channel_bot):
        slot = _bot_slot(ds)
        text = "sessions keep failing after the update; investigate"
        _slack, sessions = _run((text, None, A_TS), ("sessions", None, B_TS))

        assert _user_rows(slot) == [text]
        run_chat.assert_awaited_once()
        assert run_chat.await_args.args[1] is slot
        assert sessions.calls == []
        assert slot._slack_thread_ts == A_TS
        assert ds.get_linked_slot(A_TS) is slot and ds.get_linked_slot(B_TS) is None
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (A_TS, CHAN)

    def test_a_leading_mention_is_still_claimed(self, ds, run_chat, channel_bot):
        _run(("<@U0BOT00001> hello", None, A_TS))
        assert _bot_slot(ds)._slack_thread_ts == A_TS
        run_chat.assert_awaited_once()

    def test_with_no_bot_the_channel_runs_one_session_per_thread(self, ds, run_chat, channel_bot):
        channel_bot["bot"] = ""
        _slack, sessions = _run(("hi", None, A_TS))
        assert [k for k, _ in sessions.calls] == [f"slack:{A_TS}"]
        assert _user_rows(_bot_slot(ds)) == []
        run_chat.assert_not_awaited()

    def test_an_unmentioned_reply_in_an_earlier_thread_is_still_followed(
        self, ds, run_chat, channel_bot
    ):
        from kiro_crew.slack.events_inbound import _thread_follow_admits

        _run(("first", None, A_TS), ("second", None, B_TS))
        assert ds.get_linked_slot(A_TS) is None  # the link moved on to B
        sessions = MagicMock()
        sessions.has_session.return_value = False
        sessions.get_session_for_thread.return_value = None
        orch = SimpleNamespace(sessions=sessions, conv_log=None)

        def admits(thread_ts, follow=True):
            return _thread_follow_admits(
                orch,
                thread_follow=follow,
                activation="mention",
                thread_ts=thread_ts,
                text="and another thing",
                sender_id=_OWNER,
                channel=CHAN,
            )

        assert admits(A_TS) and admits(B_TS)
        assert not admits(C_TS)  # a thread the Bot never answered
        assert not admits(A_TS, follow=False)
        channel_bot["bot"] = ""
        assert not admits(A_TS)  # no handler, so no handler thread is followed

    def test_a_dm_is_never_claimed(self, ds, run_chat, channel_bot):
        assert asyncio.run(ch.may_claim("D0OWNERDM01", "hello")) is False
        slack, sessions = RecordingSlackClient(), _Sessions()
        asyncio.run(_send(slack, sessions, "hi", channel="D0OWNERDM01", msg_ts=A_TS))
        assert _user_rows(_bot_slot(ds)) == [] and sessions.calls


class TestChannelBotCommand:
    @pytest.fixture()
    def persisted(self, monkeypatch):
        written = []
        monkeypatch.setattr(
            _handler, "_persist_channel_config", lambda channel, **kw: written.append((channel, kw))
        )
        monkeypatch.setattr(_handler, "_reload_orch_cfg", lambda: None)
        return written

    def _cmd(self, text, user=_OWNER, channel=CHAN):
        slack = RecordingSlackClient()
        asyncio.run(
            _handler._handle_slash_command(
                text, slack, MagicMock(), channel, A_TS, A_TS, f"slack:{A_TS}", user
            )
        )
        return [t for t, _ in _posts(slack)]

    def test_the_owner_sets_a_bot(self, ds, persisted):
        posts = self._cmd("!channel bot Research Bot")
        assert persisted == [(CHAN, {"bot": "research"})]
        assert "Bot *research* now answers every thread in this channel" in posts[-1]
        assert "Open its chat on the Bots page" in posts[-1]  # research has no open chat

    def test_a_bot_is_not_set_without_the_dashboard(self, ds, persisted, monkeypatch):
        # ``off`` unlinks through the dashboard state, so a Bot set without one
        # could not be cleared again.
        monkeypatch.setattr(_handler, "_dashboard_state", None)
        posts = self._cmd("!channel bot Research Bot")
        assert persisted == []
        assert posts == ["❌ A Channel Bot needs the dashboard running."]

    @pytest.mark.parametrize("command", ["bot", "agent"])
    def test_assignment_waiting_for_a_conductor_is_refused(
        self, ds, persisted, conductor_on, monkeypatch, command
    ):
        room = ct._room
        slack = _TeamSlack()

        async def scenario():
            entered, proceed = asyncio.Event(), asyncio.Event()

            async def paused_room(*args):
                entered.set()
                await await_until(proceed, what="release the Conductor room lookup")
                return await room(*args)

            monkeypatch.setattr(ct, "_room", paused_room)
            first = asyncio.create_task(
                ch.set_conductor(
                    ["!channel", "conductor", "on"],
                    slack,
                    CHAN,
                    A_TS,
                    A_TS,
                    f"slack:{A_TS}",
                    _OWNER,
                )
            )
            await await_until(entered, what="Conductor creation holds the channel lock")
            second = asyncio.create_task(
                _handler._handle_slash_command(
                    f"!channel {command} research",
                    slack,
                    MagicMock(),
                    CHAN,
                    B_TS,
                    B_TS,
                    f"slack:{B_TS}",
                    _OWNER,
                )
            )
            try:
                await until_parked(ch.lock(CHAN))
                assert persisted == []
            finally:
                proceed.set()
                await await_until(asyncio.gather(first, second), what="both handler commands")

        asyncio.run(scenario())
        assert ch.channel_conductor(CHAN) is not None
        assert persisted == []
        assert (
            "This channel has a Channel Conductor. Send `!channel conductor off` first.",
            B_TS,
        ) in _posts(slack)

    def test_two_channels_cannot_assign_the_same_bot_concurrently(self, ds, persisted, monkeypatch):
        other = "C0OTHER0001"
        slack = RecordingSlackClient()
        monkeypatch.setattr(
            ch,
            "_channel_with_bot",
            lambda bot, channel: next(
                (
                    saved
                    for saved, values in persisted
                    if saved != channel and values.get("bot") == bot
                ),
                "",
            ),
        )

        async def scenario():
            async with ch.lock(CHAN), ch.lock(other):
                tasks = [
                    asyncio.create_task(
                        ch.set_bot("!channel bot research", slack, channel, A_TS, _OWNER)
                    )
                    for channel in (CHAN, other)
                ]
                await until_parked(ch.lock(CHAN))
                await until_parked(ch.lock(other))
                assert persisted == []
            await await_until(asyncio.gather(*tasks), what="both channel Bot assignments")

        asyncio.run(scenario())
        assert len(persisted) == 1
        winner, values = persisted[0]
        assert values == {"bot": "research"}
        posts = [text for text, _ in _posts(slack)]
        assert sum("now answers every thread" in text for text in posts) == 1
        assert (
            f"Bot *research* already answers <#{winner}>. Send `!channel bot off` there first."
        ) in posts

    def test_an_unknown_bot_is_a_clear_error(self, ds, persisted):
        posts = self._cmd("!channel bot nobody")
        assert persisted == []
        assert posts[-1].startswith("❌ Unknown Bot `nobody`. Crew Bots: ")
        assert "`web-frontend`" in posts[-1]

    def test_a_non_owner_is_refused(self, ds, persisted):
        posts = self._cmd("!channel bot web-frontend", user=STRANGER)
        assert persisted == []
        assert posts and "⛔" in posts[-1]

    def test_a_dm_cannot_have_a_channel_bot(self, ds, persisted):
        posts = self._cmd("!channel bot web-frontend", channel="D0OWNERDM01")
        assert persisted == [] and posts[-1] == "A DM cannot have a Channel Bot."

    def test_off_unlinks_the_bot_chat_from_this_channel(self, ds, persisted, channel_bot):
        slot = _bot_slot(ds)
        ds.link_slack(slot.key, A_TS, CHAN)
        posts = self._cmd("!channel bot off")
        assert persisted == [(CHAN, {"bot": ""})]
        assert slot._slack_thread_ts == "" and ds.get_linked_slot(A_TS) is None
        assert posts[-1].startswith("This channel has no Bot now.")

    def test_off_clears_a_bot_without_the_dashboard(self, ds, persisted, channel_bot, monkeypatch):
        # Set while the dashboard ran; ``off`` must not depend on it to clear the channel.
        slot = _bot_slot(ds)
        key = f"dashboard:{slot.key}"

        class _Links:
            links = {key: (A_TS, CHAN)}
            flushed = 0

            def get_slack_link(self, session_key):
                return self.links.get(session_key, (None, None))

            def clear_slack_link(self, session_key):
                return self.links.pop(session_key, None) is not None

            async def aflush(self):
                type(self).flushed += 1

        monkeypatch.setattr(_handler, "_dashboard_state", None)
        slack = RecordingSlackClient()
        asyncio.run(ch.set_bot("!channel bot off", slack, CHAN, A_TS, _OWNER, _Links()))
        assert _Links.links == {} and _Links.flushed == 1  # the saved link went first
        assert persisted == [(CHAN, {"bot": ""})]
        assert _posts(slack)[-1][0].startswith("This channel has no Bot now.")

    @pytest.mark.parametrize("chat", ["open", "closed", "generation"])
    @pytest.mark.parametrize("fail_save", [False, True])
    def test_off_waits_for_durable_unlink_even_when_closed(
        self, ds, persisted, channel_bot, monkeypatch, chat, fail_save
    ):
        slot = _bot_slot(ds)
        if chat == "generation":
            from kiro_crew.members import write_dm_binding

            ds._slots.pop(slot.key)
            slot = ds.get_or_create_slot(
                name=member_slot_key("web-frontend", "web-frontend-gen2"),
                agent="web-frontend",
                mode=DM_SLOT_MODE,
            )
            write_dm_binding(
                "web-frontend",
                member="web-frontend",
                slot_key=slot.key,
                memory_store="web-frontend-gen2",
            )
        owner = f"dashboard:{slot.key}"
        ds.link_slack(slot.key, A_TS, CHAN)
        if chat != "open":
            ds._slots.pop(slot.key)
        flushed = []

        async def flush():
            assert persisted == []
            assert channel_bot["bot"] == "web-frontend"
            assert ds.sessions.get_slack_link(owner) == (None, None)
            flushed.append(True)
            if fail_save and len(flushed) == 1:
                raise OSError("disk full")

        monkeypatch.setattr(ds.sessions, "aflush", flush)
        posts = self._cmd("!channel bot off")
        if fail_save:
            assert persisted == []
            assert posts[-1] == (
                "❌ Could not unlink this channel's Bot. Try `!channel bot off` again."
            )
            posts = self._cmd("!channel bot off")
        assert len(flushed) == (2 if fail_save else 1)
        assert persisted == [(CHAN, {"bot": ""})]
        assert posts[-1].startswith("This channel has no Bot now.")

    def test_a_channel_agent_and_a_channel_bot_exclude_each_other(
        self, ds, persisted, channel_bot, monkeypatch
    ):
        posts = self._cmd("!channel agent kirocrew")
        assert (
            posts[-1]
            == "This channel is answered by Bot *web-frontend*. Send `!channel bot off` first."
        )
        channel_bot["bot"] = ""
        monkeypatch.setattr(_handler, "channel_pinned_agent", lambda channel: "kirocrew")
        posts = self._cmd("!channel bot web-frontend")
        assert "runs agent *kirocrew*" in posts[-1] and persisted == []

    def test_another_bot_is_refused_until_off_and_the_same_one_is_confirmed(
        self, ds, persisted, channel_bot
    ):
        posts = self._cmd("!channel bot research")
        assert persisted == []
        assert posts[-1] == (
            "This channel is answered by Bot *web-frontend*. Send `!channel bot off` first."
        )
        posts = self._cmd("!channel bot web-frontend")
        assert persisted == [(CHAN, {"bot": "web-frontend"})]
        assert "Bot *web-frontend* now answers every thread in this channel" in posts[-1]

    def test_channel_agent_still_takes_a_bot_key(self, ds, persisted, channel_bot):
        channel_bot["bot"] = ""
        posts = self._cmd("!channel agent web-frontend")
        assert persisted == [(CHAN, {"agent": "web-frontend"})]
        assert posts[-1] == "Agent for this channel: *web-frontend*"

    def test_bang_bot_in_a_bot_channel_is_refused(self, ds, persisted, channel_bot):
        posts = self._cmd("!bot research")
        assert (
            posts[-1]
            == "This channel is answered by Bot *web-frontend*. Send `!channel bot off` first."
        )

    def test_bare_channel_says_who_answers_and_lists_the_forms(self, ds, channel_bot):
        posts = self._cmd("!channel")
        assert posts[-1].startswith("This channel is answered by Bot *web-frontend*.")
        assert "`!channel bot <name|off>`" in posts[-1]
        assert "`!channel conductor on|off`" in posts[-1]
        assert "status" not in posts[-1]


# ── Channel Conductor ─────────────────────────────────────────────────────


@pytest.fixture()
def conductor_on(monkeypatch, bots, audit):
    """The Conductor-thread feature on, a private channel, an admitted workspace."""
    cfg = SimpleNamespace(
        slack=SimpleNamespace(thread_conductor=True, thread_conductor_public_channels=False)
    )
    monkeypatch.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", True)
    monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: cfg)
    monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: team == TEAM)
    monkeypatch.setattr(ct, "_seed", AsyncMock(return_value="[Slack Channel Conductor]"))

    async def info(channel):
        return ChannelInfo(
            channel_id=channel,
            is_code_channel=False,
            is_private=True,
            shared=False,
            archived=False,
            mpim=False,
        )

    from kiro_crew.slack.code_channels import client as cc_client

    fake = SimpleNamespace(channel_info=info)
    monkeypatch.setattr(cc_client.CodeChannelsClient, "from_slack", classmethod(lambda c, s: fake))
    return cfg


class _TeamSlack(RecordingSlackClient):
    async def ensure_channel_team(self, channel, *, retry=False):
        self._channel_team = {channel: TEAM}


def _start(ds, ts=A_TS):
    slack = _TeamSlack()
    asyncio.run(
        _handler._handle_slash_command(
            "!channel conductor on", slack, MagicMock(), CHAN, ts, ts, f"slack:{ts}", _OWNER
        )
    )
    binding = ch.channel_conductor(CHAN)
    assert binding is not None, _posts(slack)
    return slack, binding, ds._slots[binding.session_key]


@pytest.fixture()
def rollback_links(ds, monkeypatch):
    from kiro_crew.session_map import SessionMap

    links = SessionMap()
    # Exercise real link indexes and mute semantics without writes or flush tasks.
    monkeypatch.setattr(links, "_save", MagicMock())
    for method in (
        "set_slack_link",
        "get_slack_link",
        "clear_slack_link",
        "get_session_for_thread",
        "is_slack_paused",
        "set_slack_paused",
    ):
        monkeypatch.setattr(ds.sessions, method, getattr(links, method))
    return links


class TestChannelConductor:
    def test_on_opens_one_conductor_chat_for_the_channel(self, ds, conductor_on):
        slack, binding, slot = _start(ds)
        assert binding.channel_wide and binding.thread and binding.room == ROOM_PRIVATE
        assert binding.anchor_ts == A_TS and ds.get_linked_slot(A_TS) is slot
        assert (ct.CHANNEL_STARTED_TEXT, A_TS) in _posts(slack)

    def test_a_shared_user_cannot_start_one(self, ds, conductor_on, monkeypatch):
        # A shared user may start a Conductor thread as its opener; a channel's
        # Conductor is the owner's, so the start itself refuses anyone else.
        monkeypatch.setattr(ct, "opener_for", lambda user: user)
        slack = _TeamSlack()
        asyncio.run(
            ch.set_conductor(
                ["!channel", "conductor", "on"],
                slack,
                CHAN,
                A_TS,
                A_TS,
                f"slack:{A_TS}",
                STRANGER,
            )
        )
        assert ch.channel_conductor(CHAN) is None and store().thread_bindings() == []
        assert _posts(slack) == [("❌ Owner-only command.", A_TS)]

    def test_conductor_waiting_for_a_bot_is_refused(self, ds, conductor_on, channel_bot):
        channel_bot["bot"] = ""
        slack = _TeamSlack()

        async def scenario():
            async with ch.lock(CHAN):
                task = asyncio.create_task(
                    ch.set_conductor(
                        ["!channel", "conductor", "on"],
                        slack,
                        CHAN,
                        A_TS,
                        A_TS,
                        f"slack:{A_TS}",
                        _OWNER,
                    )
                )
                await until_parked(ch.lock(CHAN))
                channel_bot["bot"] = "research"
            await await_until(task, what="Conductor checks the channel's new Bot")

        asyncio.run(scenario())
        assert ch.channel_conductor(CHAN) is None
        assert _posts(slack) == [
            ("This channel is answered by a Bot. Send `!channel bot off` first.", A_TS)
        ]

    def test_concurrent_starts_create_only_one_channel_conductor(
        self, ds, conductor_on, monkeypatch
    ):
        room = ct._room
        calls = []
        slack = _TeamSlack()

        async def scenario():
            entered, proceed = asyncio.Event(), asyncio.Event()

            async def paused_room(*args):
                calls.append(True)
                entered.set()
                await await_until(proceed, what="release the first start's room lookup")
                return await room(*args)

            monkeypatch.setattr(ct, "_room", paused_room)

            async def start(ts):
                await ch.set_conductor(
                    ["!channel", "conductor", "on"], slack, CHAN, ts, ts, f"slack:{ts}", _OWNER
                )

            first = asyncio.create_task(start(A_TS))
            await await_until(entered, what="the first start's room lookup")
            second = asyncio.create_task(start(B_TS))
            try:
                await until_parked(ch.lock(CHAN))
                assert calls == [True]
            finally:
                proceed.set()
                await await_until(asyncio.gather(first, second), what="both channel starts")

        asyncio.run(scenario())
        bindings = store().thread_bindings()
        assert len(bindings) == 1
        assert bindings[0].anchor_ts == A_TS
        assert (ct.CHANNEL_STARTED_TEXT, A_TS) in _posts(slack)
        assert "Channel Conductor" in _posts(slack)[-1][0]

    def test_a_chat_with_a_live_worker_is_busy_for_another_thread(self, ds, conductor_on, run_chat):
        _slack, binding, slot = _start(ds)
        worker = ds.get_or_create_slot(name="chat-worker-1", agent="kirocrew-worker")
        worker._created_by = slot.key
        worker._turn_admission_reserved = True  # the worker it dispatched is running
        slack, _ = _run(("plan the release", None, B_TS))
        assert _posts(slack) == [(ch.BUSY_TEXT.format(name="The Channel Conductor"), B_TS)]
        assert slot._slack_thread_ts == A_TS and ds.get_linked_slot(B_TS) is None
        assert ch.channel_conductor(CHAN).anchor_ts == binding.anchor_ts == A_TS
        run_chat.assert_not_awaited()
        # Once the worker is idle the chat moves to the next thread that writes.
        worker._turn_admission_reserved = False
        _run(("plan the release", None, B_TS))
        assert slot._slack_thread_ts == B_TS and ch.channel_conductor(CHAN).anchor_ts == B_TS
        run_chat.assert_awaited_once()

    def test_every_thread_reaches_the_same_conductor_chat(self, ds, conductor_on, run_chat):
        from kiro_crew.dashboard.session_control_parts.caller_access import (
            code_channel_binding_of,
        )

        _slack, binding, slot = _start(ds)
        seen = []

        async def _turn(state, s, text, **kw):
            seen.append((text, s._slack_thread_ts, ch.channel_conductor(CHAN).anchor_ts))

        with patch("kiro_crew.dashboard.chat._run_chat", side_effect=_turn):
            _run(
                ("plan the release", None, B_TS),
                ("and the notes", A_TS, "1700000100.000600"),
            )
        assert seen == [("plan the release", B_TS, B_TS), ("and the notes", A_TS, A_TS)]
        assert len([s for s in ds._slots.values() if s.key.startswith("chat-")]) >= 1
        assert [b.session_key for b in store().thread_bindings()] == [binding.session_key]
        # The dispatch exemption still recognizes the chat as its channel's conductor.
        assert code_channel_binding_of(ds, slot).anchor_ts == A_TS

    def test_only_the_owners_messages_reach_it(self, ds, conductor_on, run_chat, monkeypatch):
        _slack, _binding, slot = _start(ds)
        # A shared user (slack.shared_access): heard in the channel, never by the Conductor.
        monkeypatch.setattr(_handler, "is_shared_user", lambda user: user == STRANGER)
        slack, _ = _run(("hi", None, B_TS, STRANGER), ("again", None, C_TS, STRANGER))
        run_chat.assert_not_awaited()
        assert slot._slack_thread_ts == A_TS
        told = [kw["text"] for m, kw in slack.transcript if m == "post_ephemeral"]
        assert told == [ch.OWNER_ONLY_TEXT]  # told once

    def test_off_ends_it_and_other_threads_run_on_their_own(self, ds, conductor_on, run_chat):
        slack, binding, _slot = _start(ds)
        asyncio.run(
            _handler._handle_slash_command(
                "!channel conductor off", slack, MagicMock(), CHAN, A_TS, A_TS, "slack:a", _OWNER
            )
        )
        assert ch.channel_conductor(CHAN) is None
        assert store().get_thread_binding(CHAN, A_TS) is None
        _slack, sessions = _run(("hi", None, B_TS))
        assert [k for k, _ in sessions.calls] == [f"slack:{B_TS}"]
        run_chat.assert_not_awaited()

    def test_off_in_the_anchored_thread_ends_a_closed_conductor(
        self, ds, conductor_on, run_chat, rollback_links
    ):
        _slack, binding, slot = _start(ds)
        key = "dashboard:" + slot.key
        ds._slots.pop(slot.key)  # the Conductor chat is closed; its link stays persisted
        slack, _ = _run(("!channel conductor off", A_TS, "1700000100.000700"))
        posts = [text for text, _ts in _posts(slack)]
        assert ct.ENDED_TEXT not in posts
        assert posts[-1].startswith("This channel's Conductor has ended.")
        assert ch.channel_conductor(CHAN) is None
        assert store().get_thread_binding(CHAN, A_TS) is None
        assert ds.sessions.get_slack_link(key) == (None, None)
        assert ds.sessions.get_session_for_thread(A_TS) is None
        run_chat.assert_not_awaited()

    def test_off_ends_a_conductor_without_the_dashboard(
        self, ds, conductor_on, run_chat, rollback_links, monkeypatch
    ):
        _slack, binding, slot = _start(ds)
        key = "dashboard:" + slot.key
        monkeypatch.setattr(_handler, "_dashboard_state", None)
        slack = RecordingSlackClient()
        asyncio.run(
            ch.set_conductor(
                ["!channel", "conductor", "off"],
                slack,
                CHAN,
                A_TS,
                A_TS,
                f"slack:{A_TS}",
                _OWNER,
                ds.sessions,
            )
        )
        assert _posts(slack)[-1][0].startswith("This channel's Conductor has ended.")
        assert ch.channel_conductor(CHAN) is None
        assert ds.sessions.get_slack_link(key) == (None, None)

    def test_after_off_the_anchored_thread_runs_on_its_own(
        self, ds, conductor_on, run_chat, rollback_links
    ):
        _slack, binding, slot = _start(ds)
        slack, _ = _run(("!channel conductor off", A_TS, "1700000100.000700"))
        assert _posts(slack)[-1][0].startswith("This channel's Conductor has ended.")
        assert not slot._slack_linked and ds.get_linked_slot(A_TS) is None
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (None, None)
        assert ds.sessions.get_session_for_thread(A_TS) is None
        slack, sessions = _run(("hi again", A_TS, "1700000100.000800"))
        assert ct.ENDED_TEXT not in [text for text, _ts in _posts(slack)]
        assert [k for k, _ in sessions.calls] == [f"slack:{A_TS}"]
        run_chat.assert_not_awaited()
        # A has no row: a new Channel Conductor starts there, and the old chat's key
        # stays retired, so its workers stay code-channel workers.
        slack, _ = _run(("!channel conductor on", A_TS, "1700000100.000900"), slack=_TeamSlack())
        assert (ct.CHANNEL_STARTED_TEXT, A_TS) in _posts(slack)
        new = ch.channel_conductor(CHAN)
        assert new is not None and new.anchor_ts == A_TS
        assert new.session_key != binding.session_key
        assert store().get_thread_binding(CHAN, A_TS) == new
        assert binding.session_key in store().session_keys()
        assert [b.session_key for b in store().thread_bindings()] == [new.session_key]
        new_slot = ds._slots[new.session_key]
        _run(("plan the release", None, B_TS))
        assert new_slot._slack_thread_ts == B_TS and ch.channel_conductor(CHAN).anchor_ts == B_TS
        run_chat.assert_awaited_once()
        assert run_chat.await_args.args[1] is new_slot

    def test_off_removes_the_row_and_keeps_its_session_key_retired(
        self, ds, conductor_on, run_chat, rollback_links
    ):
        _slack, binding, _slot = _start(ds)
        _run(("plan the release", None, B_TS))  # the Conductor answers B last
        assert ch.channel_conductor(CHAN).anchor_ts == B_TS
        slack, _ = _run(("!channel conductor off", B_TS, "1700000200.000700"))
        assert _posts(slack)[-1][0].startswith("This channel's Conductor has ended.")
        assert store().thread_bindings() == []
        assert store().get_thread_binding(CHAN, B_TS) is None
        assert binding.session_key in store().session_keys()
        assert not store().revoked_anchor(B_TS) and not store().revoked_anchor(A_TS)

    def test_after_off_a_new_conductor_answers_the_old_thread(
        self, ds, conductor_on, run_chat, rollback_links
    ):
        _slack, old, _slot = _start(ds)
        _run(("!channel conductor off", A_TS, "1700000100.000700"))
        # Started from another thread, the new Conductor answers a message in A.
        slack, _ = _run(("!channel conductor on", C_TS, C_TS), slack=_TeamSlack())
        assert (ct.CHANNEL_STARTED_TEXT, C_TS) in _posts(slack)
        new = ch.channel_conductor(CHAN)
        assert new.anchor_ts == C_TS and new.session_key != old.session_key
        new_slot = ds._slots[new.session_key]
        slack, sessions = _run(("back in A", A_TS, "1700000100.000800"))
        assert sessions.calls == [] and ct.ENDED_TEXT not in [t for t, _ in _posts(slack)]
        run_chat.assert_awaited_once()
        assert run_chat.await_args.args[1] is new_slot
        assert new_slot._slack_thread_ts == A_TS and ds.get_linked_slot(A_TS) is new_slot
        assert ch.channel_conductor(CHAN).anchor_ts == A_TS
        assert [b.session_key for b in store().thread_bindings()] == [new.session_key]

    def test_after_off_a_channel_bot_answers_the_old_thread(
        self, ds, conductor_on, run_chat, rollback_links, channel_bot, monkeypatch
    ):
        channel_bot["bot"] = ""
        monkeypatch.setattr(
            _handler,
            "_persist_channel_config",
            lambda channel, **kw: channel_bot.update(bot=kw["bot"]),
        )
        monkeypatch.setattr(_handler, "_reload_orch_cfg", lambda: None)
        _start(ds)
        _run(("!channel conductor off", A_TS, "1700000100.000700"))
        slack, _ = _run(("!channel bot web-frontend", C_TS, C_TS))
        assert "Bot *web-frontend* now answers every thread" in _posts(slack)[-1][0]
        slot = _bot_slot(ds)
        slack, sessions = _run(("back in A", A_TS, "1700000100.000800"))
        assert sessions.calls == [] and ct.ENDED_TEXT not in [t for t, _ in _posts(slack)]
        assert _user_rows(slot) == ["back in A"]
        run_chat.assert_awaited_once()
        assert run_chat.await_args.args[1] is slot
        assert slot._slack_thread_ts == A_TS and ds.get_linked_slot(A_TS) is slot

    def test_off_whose_unlink_is_not_saved_keeps_it_live(
        self, ds, conductor_on, run_chat, rollback_links, monkeypatch
    ):
        _slack, binding, slot = _start(ds)
        monkeypatch.setattr(ds.sessions, "aflush", AsyncMock(side_effect=OSError("disk full")))
        slack, _ = _run(("!channel conductor off", A_TS, "1700000100.000700"))
        assert _posts(slack)[-1][0] == (
            "❌ Could not unlink the Conductor's chat. Try `!channel conductor off` again."
        )
        live = ch.channel_conductor(CHAN)
        assert live is not None and live.anchor_ts == A_TS and not live.ended_at
        monkeypatch.setattr(ds.sessions, "aflush", AsyncMock())
        slack, _ = _run(("!channel conductor off", A_TS, "1700000100.000800"))
        assert _posts(slack)[-1][0].startswith("This channel's Conductor has ended.")
        assert ch.channel_conductor(CHAN) is None

    def test_a_conductor_channel_refuses_a_thread_conductor_and_a_bot(
        self, ds, conductor_on, run_chat
    ):
        _start(ds)
        for text in ("!conductor go", "!bot research", "!channel bot research"):
            slack = _TeamSlack()
            asyncio.run(
                _handler._handle_slash_command(
                    text, slack, MagicMock(), CHAN, B_TS, C_TS, f"slack:{B_TS}", _OWNER
                )
            )
            assert "Channel Conductor" in _posts(slack)[-1][0], text

    def test_the_conductor_thread_switch_still_governs_it(self, ds, conductor_on):
        conductor_on.slack.thread_conductor = False
        slack = _TeamSlack()
        asyncio.run(
            _handler._handle_slash_command(
                "!channel conductor on", slack, MagicMock(), CHAN, A_TS, A_TS, "slack:a", _OWNER
            )
        )
        assert _posts(slack)[-1][0] == f"❌ {ct.OFF_TEXT}"
        assert ch.channel_conductor(CHAN) is None

    def test_a_dm_cannot_have_one(self, ds, conductor_on):
        slack = _TeamSlack()
        asyncio.run(
            _handler._handle_slash_command(
                "!channel conductor on", slack, MagicMock(), "D0OWNERDM01", A_TS, A_TS, "s", _OWNER
            )
        )
        assert _posts(slack)[-1][0] == f"❌ {ct.NOT_IN_DM_TEXT}"


# ── the store move ────────────────────────────────────────────────────────


def _row(anchor, *, wide=True, key="chat-c"):
    return Binding(
        channel_id=CHAN,
        team_id=TEAM,
        session_key=key,
        anchor_ts=anchor,
        thread=True,
        room=ROOM_PRIVATE,
        channel_wide=wide,
    )


class TestMoveThreadBinding:
    def test_the_row_moves_whole(self, _store):
        _store.put_thread_binding(_row(A_TS))
        moved = _store.move_thread_binding(CHAN, A_TS, B_TS)
        assert moved.anchor_ts == B_TS and moved.channel_wide
        assert _store.get_thread_binding(CHAN, A_TS) is None
        assert _store.get_thread_binding(CHAN, B_TS) == moved
        reread = CodeChannelStore(path=_store.path)
        reread.load()
        assert reread.get_thread_binding(CHAN, B_TS) == moved

    def test_a_plain_conductor_thread_never_moves(self, _store):
        _store.put_thread_binding(_row(A_TS, wide=False))
        with pytest.raises(StoreWriteError):
            _store.move_thread_binding(CHAN, A_TS, B_TS)

    def test_it_never_lands_on_a_bound_thread(self, _store):
        _store.put_thread_binding(_row(A_TS))
        _store.put_thread_binding(_row(B_TS, wide=False, key="chat-other"))
        with pytest.raises(StoreWriteError):
            _store.move_thread_binding(CHAN, A_TS, B_TS)

    def test_an_ended_one_never_moves(self, _store):
        _store.put_thread_binding(_row(A_TS))
        _store.end_thread_binding(CHAN, A_TS)
        with pytest.raises(StoreWriteError):
            _store.move_thread_binding(CHAN, A_TS, B_TS)


class TestDropChannelConductor:
    def test_it_removes_the_row_and_retires_its_key(self, _store):
        _store.put_thread_binding(_row(A_TS))
        dropped = _store.drop_channel_conductor(CHAN, A_TS)
        assert dropped is not None and dropped.session_key == "chat-c"
        reread = CodeChannelStore(path=_store.path)
        reread.load()
        for current in (_store, reread):
            assert current.get_thread_binding(CHAN, A_TS) is None
            assert "chat-c" in current.session_keys()
            assert not current.revoked_anchor(A_TS)
        # A has no fence: a new Channel Conductor binds there or moves there.
        assert _store.put_thread_binding(_row(A_TS, key="chat-new")).session_key == "chat-new"
        _store.drop_channel_conductor(CHAN, A_TS)
        _store.put_thread_binding(_row(B_TS, key="chat-next"))
        assert _store.move_thread_binding(CHAN, B_TS, A_TS).anchor_ts == A_TS

    def test_no_row_is_none(self, _store):
        assert _store.drop_channel_conductor(CHAN, A_TS) is None

    def test_a_plain_conductor_thread_is_refused(self, _store):
        first = _store.put_thread_binding(_row(A_TS, wide=False))
        with pytest.raises(StoreWriteError):
            _store.drop_channel_conductor(CHAN, A_TS)
        assert _store.get_thread_binding(CHAN, A_TS) == first


class TestPutThreadBinding:
    @pytest.mark.parametrize(
        "wide, ended", [(False, True), (False, False), (True, False), (True, True)]
    )
    def test_a_bound_thread_keeps_its_row(self, _store, wide, ended):
        _store.put_thread_binding(_row(A_TS, wide=wide))
        if ended:
            _store.end_thread_binding(CHAN, A_TS)
        first = _store.get_thread_binding(CHAN, A_TS)
        assert _store.put_thread_binding(_row(A_TS, key="chat-new")) == first
        assert _store.get_thread_binding(CHAN, A_TS) == first
        assert "chat-new" not in _store.session_keys()


# ── review fixes: who moves the chat, review mode, one channel per Bot ────


class TestGuards:
    def test_a_sender_the_route_would_not_hear_moves_nothing(self, ds, run_chat, channel_bot):
        _run(("hi", None, A_TS, STRANGER))
        assert _bot_slot(ds)._slack_thread_ts == ""
        run_chat.assert_not_awaited()

    def test_a_review_channel_is_not_handed_to_the_chat(
        self, ds, run_chat, channel_bot, monkeypatch
    ):
        monkeypatch.setattr(ch, "_activation", lambda channel: ch.REVIEW)
        slack, sessions = _run(("hi", None, A_TS))
        assert _posts(slack) == [(ch.REVIEW_TEXT, A_TS)]
        assert _bot_slot(ds)._slack_thread_ts == "" and sessions.calls == []
        run_chat.assert_not_awaited()

    def test_a_bot_another_channel_has_is_refused(self, ds, monkeypatch):
        from kiro_crew.config import KiroCrewConfig

        cfg = SimpleNamespace(slack_channels={"C0OTHER0001": SimpleNamespace(bot="research")})
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda c, *a, **k: cfg))
        assert ch._channel_with_bot("research", CHAN) == "C0OTHER0001"
        assert ch._channel_with_bot("research", "C0OTHER0001") == ""

    @pytest.mark.parametrize("conductor", [True, False], ids=["conductor", "bot"])
    @pytest.mark.parametrize("paused", [False, True], ids=["unmuted", "muted"])
    def test_a_failed_link_moves_the_conductor_binding_back(
        self, ds, conductor_on, channel_bot, rollback_links, monkeypatch, conductor, paused
    ):
        if conductor:
            channel_bot["bot"] = ""
            _slack, binding, slot = _start(ds)
            name = "The Channel Conductor"
        else:
            slot = _bot_slot(ds)
            ds.link_slack(slot.key, A_TS, CHAN)
            name = "Bot *web-frontend*"
        key = "dashboard:" + slot.key
        ds.sessions.set_slack_paused(key, paused)
        flush = AsyncMock(side_effect=OSError("disk full"))
        monkeypatch.setattr(ds.sessions, "aflush", flush)

        slack, _ = _run(("hi", None, B_TS))

        assert (ch.NOT_SAVED_TEXT.format(name=name), B_TS) in _posts(slack)
        if conductor:
            assert ch.channel_conductor(CHAN).anchor_ts == A_TS
        assert slot._slack_linked
        assert (slot._slack_thread_ts, slot._slack_channel) == (A_TS, CHAN)
        assert ds.get_linked_slot(A_TS) is slot and ds.get_linked_slot(B_TS) is None
        assert ds.sessions.get_slack_link(key) == (A_TS, CHAN)
        assert ds.sessions.get_session_for_thread(A_TS) == key
        assert ds.sessions.get_session_for_thread(B_TS) is None
        assert ds.sessions.is_slack_paused(key) is paused
        flush.assert_awaited_once()

    @pytest.mark.parametrize("conductor", [True, False], ids=["conductor", "bot"])
    def test_a_muted_chat_stays_muted_when_it_moves_thread(
        self, ds, conductor_on, channel_bot, rollback_links, monkeypatch, run_chat, conductor
    ):
        if conductor:
            channel_bot["bot"] = ""
            _slack, _binding, slot = _start(ds)
        else:
            slot = _bot_slot(ds)
            ds.link_slack(slot.key, A_TS, CHAN)
        key = "dashboard:" + slot.key
        ds.sessions.set_slack_paused(key, True)
        monkeypatch.setattr(ds.sessions, "aflush", AsyncMock())

        _run(("hi", None, B_TS))

        assert ds.sessions.get_slack_link(key) == (B_TS, CHAN)
        if conductor:
            assert ch.channel_conductor(CHAN).anchor_ts == B_TS
        assert ds.sessions.is_slack_paused(key)
        run_chat.assert_awaited_once()

    @pytest.mark.parametrize(
        "change", ["new-link", "unlink", "closing", "slot-owner", "session-owner", "undo-failed"]
    )
    def test_failed_move_does_not_restore_over_changed_state(
        self, ds, conductor_on, rollback_links, monkeypatch, change
    ):
        _slack, binding, slot = _start(ds)
        key = "dashboard:" + slot.key
        move_binding = store().move_thread_binding

        def _undo(channel, from_ts, to_ts):
            if from_ts == B_TS:
                # Changes while the binding undo is off the loop must survive rollback.
                if change == "new-link":
                    ds._link_slack_persisted(slot, slot.key, C_TS, CHAN)
                elif change == "closing":
                    slot._closing = 1
                elif change == "slot-owner":
                    ds._slack_to_slot[A_TS] = "other-chat"
                elif change == "session-owner":
                    ds.sessions.set_slack_link("dashboard:other-chat", A_TS, CHAN)
                elif change == "undo-failed":
                    raise StoreWriteError("disk full")
            return move_binding(channel, from_ts, to_ts)

        async def _fail():
            if change == "unlink":
                # An unlink that happened during the failed save must not be reversed.
                ds.sessions.clear_slack_link(key)
            raise OSError("disk full")

        monkeypatch.setattr(store(), "move_thread_binding", _undo)
        monkeypatch.setattr(ds.sessions, "aflush", _fail)
        slack = RecordingSlackClient()
        assert not asyncio.run(
            ch._move(ds, slot, key, binding, CHAN, B_TS, slack, "The Channel Conductor")
        )
        expected = (C_TS, CHAN) if change == "new-link" else (None, None)
        assert ds.sessions.get_slack_link(key) == expected
        assert ds._slack_to_slot.get(A_TS) != slot.key
        if change == "new-link":
            assert ds.get_linked_slot(C_TS) is slot
        elif change == "slot-owner":
            assert ds._slack_to_slot[A_TS] == "other-chat"
        elif change == "session-owner":
            assert ds.sessions.get_session_for_thread(A_TS) == "dashboard:other-chat"
        elif change == "undo-failed":
            assert not slot._slack_linked
        assert ch.channel_conductor(CHAN).anchor_ts == (B_TS if change == "undo-failed" else A_TS)

    def test_anchor_handoff_refused_keeps_the_previous_link(
        self, ds, conductor_on, rollback_links, monkeypatch
    ):
        _slack, binding, slot = _start(ds)
        key = "dashboard:" + slot.key
        ds.sessions.set_slack_paused(key, True)
        monkeypatch.setattr(
            cc_binding,
            "check_anchor_handoff",
            MagicMock(side_effect=cc_binding.AnchorHandoffRefused(B_TS)),
        )
        flush = AsyncMock()
        monkeypatch.setattr(ds.sessions, "aflush", flush)
        slack = RecordingSlackClient()
        assert not asyncio.run(
            ch._move(ds, slot, key, binding, CHAN, B_TS, slack, "The Channel Conductor")
        )
        assert slot._slack_linked
        assert (slot._slack_thread_ts, slot._slack_channel) == (A_TS, CHAN)
        assert ds.get_linked_slot(A_TS) is slot
        assert ds.sessions.get_slack_link(key) == (A_TS, CHAN)
        assert ds.sessions.is_slack_paused(key)
        assert ch.channel_conductor(CHAN).anchor_ts == A_TS
        flush.assert_not_awaited()

    def test_a_turn_started_during_the_subagent_probe_keeps_its_thread(
        self, ds, run_chat, channel_bot, monkeypatch
    ):
        slot = _bot_slot(ds)
        _run(("one", None, A_TS))
        run_chat.reset_mock()

        async def _probe(state, probed, key, operation):
            # The dashboard starts a turn in the Bot's chat while the probe is awaited.
            probed._turn_admission_reserved = True
            return False

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils_completion.subagents_attached_async", _probe
        )
        slack, _ = _run(("two", None, B_TS))
        assert _posts(slack) == [(ch.BUSY_TEXT.format(name="Bot *web-frontend*"), B_TS)]
        assert slot._slack_thread_ts == A_TS and ds.get_linked_slot(B_TS) is None
        run_chat.assert_not_awaited()

    @pytest.mark.parametrize("undo_saved", [True, False], ids=["undone", "undo-failed"])
    def test_a_turn_started_while_the_binding_moves_keeps_its_thread(
        self, ds, conductor_on, run_chat, _store, monkeypatch, undo_saved
    ):
        _slack, binding, slot = _start(ds)
        move = _store.move_thread_binding

        def _move_then_turn(channel, from_ts, to_ts):
            if to_ts == A_TS and not undo_saved:
                raise StoreWriteError("disk full")
            moved = move(channel, from_ts, to_ts)
            if to_ts == B_TS:
                slot._turn_admission_reserved = True  # a dashboard turn starts meanwhile
            return moved

        monkeypatch.setattr(_store, "move_thread_binding", _move_then_turn)
        slack, _ = _run(("plan the release", None, B_TS))
        if undo_saved:
            assert _posts(slack) == [(ch.BUSY_TEXT.format(name="The Channel Conductor"), B_TS)]
            assert ch.channel_conductor(CHAN).anchor_ts == binding.anchor_ts == A_TS
            assert slot._slack_thread_ts == A_TS and ds.get_linked_slot(B_TS) is None
            run_chat.assert_not_awaited()
        else:
            # The link follows a binding that could not go back: the owner-only gate
            # stays on the thread the chat is linked to.
            assert ch.channel_conductor(CHAN).anchor_ts == B_TS
            assert slot._slack_thread_ts == B_TS and ds.get_linked_slot(A_TS) is None

    def test_a_review_channel_leaves_a_thread_its_own_route(
        self, ds, run_chat, channel_bot, monkeypatch
    ):
        other = ds.get_or_create_slot(name="chat-other")
        ds.link_slack(other.key, A_TS, CHAN)
        slot = _bot_slot(ds)
        ds.link_slack(slot.key, B_TS, CHAN)
        monkeypatch.setattr(ch, "_activation", lambda channel: ch.REVIEW)
        slack, _ = _run(
            ("for the other chat", A_TS, "1700000100.000500"),
            ("for the bot", B_TS, "1700000200.000500"),
        )
        assert _user_rows(other) == ["for the other chat"]
        # The Bot's own linked thread is still refused: its replies skip the draft.
        assert _posts(slack) == [(ch.REVIEW_TEXT, B_TS)]
        assert _user_rows(slot) == []

    @pytest.mark.parametrize("change", ["taken", "closed", "linked-elsewhere"])
    @pytest.mark.parametrize("conductor", [False, True], ids=["bot", "conductor"])
    def test_what_changed_during_the_subagent_probe_stops_the_move(
        self, ds, conductor_on, run_chat, channel_bot, monkeypatch, change, conductor
    ):
        if conductor:
            channel_bot["bot"] = ""
            _slack, _binding, slot = _start(ds)
            name = "The Channel Conductor"
        else:
            slot = _bot_slot(ds)
            _run(("one", None, A_TS))
            name = "Bot *web-frontend*"
        run_chat.reset_mock()
        other = ds.get_or_create_slot(name="chat-other")

        async def _probe(state, probed, key, operation):
            # The dashboard takes no channel lock: it acts while the probe is awaited.
            if change == "taken":
                ds.link_slack(other.key, B_TS, CHAN)
            elif change == "closed":
                ds._slots.pop(probed.key)
            else:
                ds.link_slack(probed.key, C_TS, "C0OTHER0001")
            return False

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils_completion.subagents_attached_async", _probe
        )
        slack, _ = _run(("two", None, B_TS))
        if conductor:
            assert ch.channel_conductor(CHAN).anchor_ts == A_TS  # the binding went back
        if change == "taken":
            # The thread keeps the chat it was just linked to, which gets the message.
            assert ds.get_linked_slot(B_TS) is other and _user_rows(other) == ["two"]
            assert _posts(slack) == [] and _user_rows(slot)[-1:] != ["two"]
        else:
            assert _posts(slack) == [(ch.NOT_SAVED_TEXT.format(name=name), B_TS)]
            assert ds.get_linked_slot(B_TS) is None
            run_chat.assert_not_awaited()
