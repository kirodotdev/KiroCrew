"""``!bot <name>`` joins a Slack thread to the Bot's own dashboard chat (``slack/bot_chat.py``).

With the Bot's chat open on the dashboard, the thread is linked to it the way Discord's
``!bot`` binds: each later message runs (or queues) on that chat, so the owner sees it on
the Bots page, and the chat's replies go back to the thread. With none open the thread
keeps the older per-thread Bot session (``test_slack_thread_bot_switch.py``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import test_slack_thread_bot_switch as _switch
from chat_test_helpers import _make_state
from slack_transport_dispatch_helpers import _MSG_TS, RecordingSlackClient
from test_slack_thread_bot_switch import (
    _KEY,
    _OTHER_KEY,
    _OTHER_TS,
    _OWNER,
    _posts,
    _send,
    _Sessions,
)

from kiro_crew.members import DM_SLOT_MODE, member_slot_key
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.slack import handler as _handler
from kiro_crew.testing.wait import await_until

# The shared fixtures, bound by assignment rather than import: pyflakes reads a test's
# ``bots`` / ``audit`` parameter as a redefinition of an imported name, not of a global.
bots = _switch.bots
audit = _switch.audit

_SECOND_TS = "1700000900.000100"


def _bot_chat(ds, bot):
    return ds.get_or_create_slot(name=member_slot_key(bot), agent=bot, mode=DM_SLOT_MODE)


@pytest.fixture()
def ds(tmp_path, monkeypatch):
    """A dashboard with Bot web-frontend's own chat open, as the Bots page leaves it."""
    state = _make_state(tmp_path)
    # The real map reads a Slack link back as the session's mirror (``get_mirror_link``).
    plain = state.sessions.get_mirror_link.side_effect

    def _mirror(key):
        held = plain(key)
        if held is not None:
            return held
        ts, ch = state.sessions.get_slack_link(key)
        return ChannelLink("slack", channel_id=ch, thread_id=ts) if ts else None

    state.sessions.get_mirror_link = MagicMock(side_effect=_mirror)
    _bot_chat(state, "web-frontend")
    monkeypatch.setattr(_handler, "_dashboard_state", state)
    return state


@pytest.fixture()
def run_chat():
    with patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock:
        yield mock


def _run(*messages):
    """Send each ``(text, thread_ts, msg_ts)`` from the owner in channel D1 (a DM)."""
    slack, sessions = RecordingSlackClient(), _Sessions()

    async def _scenario():
        for text, thread_ts, msg_ts in messages:
            await _send(slack, sessions, text, thread_ts=thread_ts, msg_ts=msg_ts)
        # Let the turn tasks the linked route started run to their (mocked) end.
        await asyncio.sleep(0)

    asyncio.run(_scenario())
    return slack, sessions


def _first(text, ts=_MSG_TS):
    return (text, None, ts)


def _reply(text, ts=_MSG_TS, n=200):
    return (text, ts, f"{ts.split('.')[0]}.000{n}")


def _user_rows(slot):
    return [m["content"] for m in slot.messages if m.get("role") == "user"]


class TestJoin:
    def test_the_reply_lands_in_the_bots_own_chat(self, bots, audit, ds, run_chat):
        slot = ds._slots[member_slot_key("web-frontend")]
        slack, sessions = _run(_first("!bot web-frontend"), _reply("hello"))

        assert slot._slack_linked and slot._slack_thread_ts == _MSG_TS
        assert ds.get_linked_slot(_MSG_TS) is slot
        assert _user_rows(slot) == ["hello"]
        [call] = run_chat.await_args_list
        assert call.args[1] is slot and call.args[2] == "hello"
        # No per-thread Bot session: the turn ran in the chat, not on Slack's own key.
        assert sessions.calls == []
        assert _KEY not in _handler._thread_agents
        assert any("talks to Bot *web-frontend* in its own chat" in p for p in _posts(slack))

    def test_replies_go_back_to_the_thread(self, bots, audit, ds, run_chat):
        # The chat's turns mirror to the Slack link it now holds.
        slot = ds._slots[member_slot_key("web-frontend")]
        _run(_first("!bot web-frontend"))

        key = "dashboard:" + slot.key
        assert ds.sessions.get_slack_link(key) == (_MSG_TS, "D1")

    def test_a_running_dashboard_turn_queues_the_slack_message(self, bots, audit, ds, run_chat):
        slot = ds._slots[member_slot_key("web-frontend")]
        slot.append("user", "from the dashboard", "msg msg-u")
        slot._turn_admission_reserved = True  # a dashboard turn holds the chat
        _run(_first("!bot web-frontend"), _reply("one", n=201), _reply("two", n=202))

        run_chat.assert_not_awaited()
        assert [e["content"] for e in slot._queue] == ["one", "two"]
        assert _user_rows(slot) == ["from the dashboard", "one", "two"]

    def test_naming_the_same_bot_again_keeps_the_link(self, bots, audit, ds, run_chat):
        slot = ds._slots[member_slot_key("web-frontend")]
        slack, _ = _run(_first("!bot web-frontend"), _reply("!bot web-frontend"))

        assert ds.get_linked_slot(_MSG_TS) is slot
        assert "already talks to Bot *web-frontend*" in _posts(slack)[-1]


class TestNearbyInputs:
    def test_a_thread_with_no_bot_is_unchanged(self, bots, audit, ds, run_chat):
        slot = ds._slots[member_slot_key("web-frontend")]
        _slack, sessions = _run(_first("!bot web-frontend"), ("hi there", None, _OTHER_TS))

        assert _user_rows(slot) == []
        assert ds.get_linked_slot(_OTHER_TS) is None
        [(key, kw)] = sessions.calls
        assert key == _OTHER_KEY and "crew_agent" not in kw

    def test_a_second_thread_to_the_same_bot_is_refused(self, bots, audit, ds, run_chat):
        slot = ds._slots[member_slot_key("web-frontend")]
        slack, sessions = _run(
            _first("!bot web-frontend"),
            _first("!bot web-frontend", ts=_SECOND_TS),
            _reply("still here?", ts=_SECOND_TS),
        )

        assert ds.get_linked_slot(_MSG_TS) is slot
        assert ds.get_linked_slot(_SECOND_TS) is None
        assert any("already talking to" in p and "another Slack thread" in p for p in _posts(slack))
        # The refused thread runs as before, on its own session and the default agent.
        assert _user_rows(slot) == []
        assert [key for key, _kw in sessions.calls] == ["slack:" + _SECOND_TS]

    def test_a_chat_bound_to_another_channel_is_refused(self, bots, audit, ds, run_chat):
        slot = ds._slots[member_slot_key("web-frontend")]
        ds.sessions.set_mirror_link(
            "dashboard:" + slot.key, ChannelLink("discord", channel_id="9"), accepts_inbound=True
        )
        slack, _ = _run(_first("!bot web-frontend"))

        assert ds.get_linked_slot(_MSG_TS) is None
        assert "already talking to Discord" in _posts(slack)[-1]

    def test_off_leaves_the_chat_and_the_thread_runs_on_its_own(self, bots, audit, ds, run_chat):
        slot = ds._slots[member_slot_key("web-frontend")]
        slack, sessions = _run(
            _first("!bot web-frontend"), _reply("!bot off", n=201), _reply("after", n=202)
        )

        assert not slot._slack_linked and ds.get_linked_slot(_MSG_TS) is None
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (None, None)
        assert _user_rows(slot) == []
        assert any("left Bot *web-frontend*'s chat" in p for p in _posts(slack))
        assert [key for key, _kw in sessions.calls] == [_KEY]
        outcomes = [c.kwargs.get("outcome") for c in audit.log_tool_invocation.call_args_list]
        assert outcomes[:2] == ["bot_chat_join", "bot_chat_leave"]

    @pytest.mark.parametrize("command", ["!bot research", "!bot web-frontnd"])
    def test_another_bot_in_a_joined_thread_is_refused(self, bots, audit, ds, run_chat, command):
        # A joined thread leaves only on ``!bot off``: another name, known or not, is
        # refused before anything can unlink it or ask the memory rule.
        slot = ds._slots[member_slot_key("web-frontend")]
        _bot_chat(ds, "research")
        asked = AsyncMock(return_value=None)
        with patch.object(_handler, "memory_binding_refusal", asked):
            slack, sessions = _run(_first("!bot web-frontend"), _reply(command, n=201))

        assert ds.get_linked_slot(_MSG_TS) is slot and slot._slack_linked
        assert _posts(slack)[-1] == (
            "❌ This thread talks to Bot *web-frontend* in its own chat."
            " Send `!bot off` first, then `!bot <name>`."
        )
        assert not any("left Bot" in p for p in _posts(slack))
        assert [c.args for c in asked.await_args_list] == [(_KEY, "web-frontend")]
        outcomes = [c.kwargs.get("outcome") for c in audit.log_tool_invocation.call_args_list]
        assert "bot_chat_leave" not in outcomes
        assert sessions.calls == [] and _KEY not in _handler._thread_agents

    def test_off_then_another_bot_joins_its_chat(self, bots, audit, ds, run_chat):
        first = ds._slots[member_slot_key("web-frontend")]
        second = _bot_chat(ds, "research")
        _run(
            _first("!bot web-frontend"),
            _reply("!bot off", n=201),
            _reply("!bot research", n=202),
            _reply("hi", n=203),
        )

        assert not first._slack_linked
        assert ds.get_linked_slot(_MSG_TS) is second
        assert _user_rows(second) == ["hi"] and _user_rows(first) == []

    def test_off_runs_nothing_that_can_refuse_after_the_leave(self, bots, audit, ds, run_chat):
        # The per-thread ``off`` (``bind_bot_memory(key, None)``) can refuse; a joined
        # thread's ``!bot off`` ends at the leave, so it is never told it left and then
        # refused.
        from kiro_crew.memory_stores import UnknownMemoryStore

        refuse = AsyncMock(side_effect=UnknownMemoryStore("retains its existing history"))
        with patch.object(_handler, "bind_bot_memory", refuse):
            slack, _ = _run(_first("!bot web-frontend"), _reply("!bot off", n=201))

        assert ds.get_linked_slot(_MSG_TS) is None
        joined = next(i for i, p in enumerate(_posts(slack)) if "in its own chat" in p)
        assert _posts(slack)[joined + 1 :] == ["👋 This thread left Bot *web-frontend*'s chat."]
        refuse.assert_not_awaited()

    def test_off_names_the_threads_own_bot_that_stays(self, bots, audit, ds, run_chat, monkeypatch):
        monkeypatch.setitem(_handler._thread_agents, _KEY, "research")
        slack, _ = _run(_first("!bot web-frontend"), _reply("!bot off", n=201))

        assert ds.get_linked_slot(_MSG_TS) is None
        assert _posts(slack)[-1] == (
            "👋 This thread left Bot *web-frontend*'s chat. It still runs as *research* on its"
            " own session; `!bot off` again stops that."
        )
        assert _handler._thread_agents[_KEY] == "research"

    def test_a_shared_user_cannot_take_the_thread_out_of_the_chat(
        self, bots, audit, ds, run_chat, monkeypatch
    ):
        # ``!bot`` stays the owner's in a joined thread too: the router refuses first.
        monkeypatch.setattr(_handler, "is_shared_user", lambda uid: uid == _switch._SHARED)
        slot = ds._slots[member_slot_key("web-frontend")]
        slack, sessions = RecordingSlackClient(), _Sessions()

        async def _scenario():
            await _send(slack, sessions, "!bot web-frontend")
            for n, text in enumerate(("!bot off", "!bot research"), start=201):
                await _send(
                    slack,
                    sessions,
                    text,
                    user_id=_switch._SHARED,
                    thread_ts=_MSG_TS,
                    msg_ts=f"{_MSG_TS.split('.')[0]}.000{n}",
                )

        asyncio.run(_scenario())

        assert ds.get_linked_slot(_MSG_TS) is slot
        assert _posts(slack)[-2:] == ["⛔ Owner-only command."] * 2
        assert not any("left Bot" in p for p in _posts(slack))
        assert _user_rows(slot) == []

    def test_a_shared_users_bot_runs_on_the_thread_not_in_the_chat(
        self, bots, audit, ds, run_chat, monkeypatch
    ):
        # A shared user may run ``!bot`` (main), but only the owner links a thread to
        # the Bot's own chat: theirs keeps the per-thread Bot session.
        monkeypatch.setattr(_handler, "is_shared_user", lambda uid: uid == _switch._SHARED)
        slot = ds._slots[member_slot_key("web-frontend")]
        slack, sessions = RecordingSlackClient(), _Sessions()

        async def _scenario():
            await _send(slack, sessions, "!bot web-frontend", user_id=_switch._SHARED)

        asyncio.run(_scenario())

        assert not slot._slack_linked and ds.get_linked_slot(_MSG_TS) is None
        assert _handler._thread_agents[_KEY] == "web-frontend"
        assert not any("in its own chat" in p for p in _posts(slack))

    def test_a_thread_whose_memory_belongs_to_another_bot_is_refused(
        self, bots, audit, ds, run_chat
    ):
        held = "This thread's memory belongs to Bot 'vault'. Start a new thread."
        with patch.object(_handler, "memory_binding_refusal", AsyncMock(return_value=held)):
            slack, _ = _run(_first("!bot web-frontend"))

        assert ds.get_linked_slot(_MSG_TS) is None
        assert _posts(slack)[-1] == f"❌ {held}"

    def test_a_damaged_memory_record_is_refused_and_audited(self, bots, audit, ds, run_chat):
        from kiro_crew.memory_stores import UnknownMemoryStore

        broken = AsyncMock(side_effect=UnknownMemoryStore("no memory store 'gone'"))
        with patch.object(_handler, "memory_binding_refusal", broken):
            slack, _ = _run(_first("!bot web-frontend"))

        assert ds.get_linked_slot(_MSG_TS) is None
        assert "no memory store 'gone'" in _posts(slack)[-1]
        outcomes = [c.kwargs.get("outcome") for c in audit.log_tool_invocation.call_args_list]
        assert "agent_denied_memory_binding" in outcomes

    def test_a_join_whose_save_fails_is_taken_back_down(self, bots, audit, ds, run_chat):
        slot = ds._slots[member_slot_key("web-frontend")]
        ds.sessions.aflush = AsyncMock(side_effect=OSError("disk full"))
        slack, _ = _run(_first("!bot web-frontend"))

        assert "Could not save joining Bot *web-frontend*'s chat" in _posts(slack)[-1]
        assert not slot._slack_linked and ds.get_linked_slot(_MSG_TS) is None
        assert ds.sessions.get_slack_link("dashboard:" + slot.key) == (None, None)

    def test_off_whose_save_fails_does_not_say_it_left(self, bots, audit, ds, run_chat):
        # The join saves; the leave's save is the one that fails.
        ds.sessions.aflush = AsyncMock(side_effect=[None, OSError("read-only data home")])
        slack, _ = _run(_first("!bot web-frontend"), _reply("!bot off", n=201))

        assert "Could not save leaving Bot *web-frontend*'s chat" in _posts(slack)[-1]
        assert not any("left Bot *web-frontend*'s chat" in p for p in _posts(slack))

    def test_no_open_chat_keeps_the_per_thread_bot(self, bots, audit, ds, run_chat):
        _slack, sessions = _run(_first("!bot research"), _reply("hello"))

        assert ds.get_linked_slot(_MSG_TS) is None
        assert _handler._thread_agents[_KEY] == "research"
        assert sessions.calls[0][1]["crew_agent"] == "research"
        run_chat.assert_not_awaited()

    def test_a_pinned_channel_keeps_the_per_thread_bot(self, bots, audit, ds, run_chat):
        with patch.object(_handler, "channel_pinned_agent", return_value="kirocrew"):
            slack, sessions = RecordingSlackClient(), _Sessions()

            async def _scenario():
                await transport_send(slack, sessions, "!bot web-frontend")

            asyncio.run(_scenario())

        assert ds.get_linked_slot(_MSG_TS) is None
        assert _handler._thread_agents[_KEY] == "web-frontend"

    def test_a_flat_dm_keeps_the_per_thread_bot(self, bots, audit, ds, run_chat):
        # ``slack.dm_single_session``: the DM is ONE conversation keyed by the channel. A
        # join links one message's ts, so the next top-level message would miss the Bot.
        slot = ds._slots[member_slot_key("web-frontend")]
        slack, sessions = RecordingSlackClient(), _Sessions()

        async def _scenario():
            await _send(slack, sessions, "!bot web-frontend", dm_single_session=True)
            await _send(slack, sessions, "hello", msg_ts=_OTHER_TS, dm_single_session=True)

        asyncio.run(_scenario())

        assert not slot._slack_linked and ds.get_linked_slot(_MSG_TS) is None
        assert _handler._thread_agents["slack:D1"] == "web-frontend"
        run_chat.assert_not_awaited()
        assert sessions.calls[-1][0] == "slack:D1"
        assert sessions.calls[-1][1]["crew_agent"] == "web-frontend"

    def test_a_review_channel_keeps_the_per_thread_bot(self, bots, audit, ds, run_chat):
        # A ``review`` channel shows each reply to the owner as a draft first; a chat's
        # mirrored replies would post without it.
        from kiro_crew.config import live
        from kiro_crew.config.loader import ACTIVATION_REVIEW, ChannelConfig

        live.snapshot().slack_channels["C0PINNED"] = ChannelConfig(activation=ACTIVATION_REVIEW)
        slack, sessions = RecordingSlackClient(), _Sessions()

        async def _scenario():
            await transport_send(slack, sessions, "!bot web-frontend")

        asyncio.run(_scenario())

        assert ds.get_linked_slot(_MSG_TS) is None
        assert _handler._thread_agents[_KEY] == "web-frontend"

    def test_a_temporary_thread_keeps_the_per_thread_bot(self, bots, audit, ds, run_chat):
        # The Bot's chat is saved; a thread marked ``!temporary`` must stay off it.
        from kiro_crew.messaging import privacy_mode

        privacy_mode.mark_temporary(_KEY)
        try:
            _run(_first("!bot web-frontend"))
        finally:
            privacy_mode.reset()

        assert ds.get_linked_slot(_MSG_TS) is None
        assert _handler._thread_agents[_KEY] == "web-frontend"

    @pytest.mark.parametrize("linked_owner", [True, False], ids=["dashboard-key", "slack-key"])
    @pytest.mark.parametrize("hydrate", [False, True], ids=["in-memory", "restore-flags"])
    @pytest.mark.parametrize(
        ("modes", "open_chat", "incoming_modes"),
        [
            (("temporary",), False, ()),
            (("incognito",), False, ()),
            (("temporary", "incognito"), True, ()),
            (("temporary",), True, ()),
            ((), True, ("temporary",)),
            (("incognito",), True, ("temporary",)),
        ],
    )
    def test_leaving_carries_privacy_to_the_thread(
        self,
        bots,
        audit,
        ds,
        run_chat,
        monkeypatch,
        modes,
        open_chat,
        incoming_modes,
        linked_owner,
        hydrate,
    ):
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.session_map import SessionMap

        slot = ds._slots[member_slot_key("web-frontend")]
        if open_chat:
            _bot_chat(ds, "research")
        slack, sessions = RecordingSlackClient(), _Sessions()
        # False models native dispatch: it supplies Slack's key even when joined.
        # True follows the real link on leave, without retaining a stale fake owner.
        monkeypatch.setattr(
            sessions,
            "get_session_for_thread",
            lambda ts: (
                "dashboard:" + slot.key if linked_owner and ds.get_linked_slot(ts) is slot else None
            ),
        )

        async def _scenario():
            await _send(slack, sessions, "!bot web-frontend")
            if hydrate:
                # Model a loaded durable row with empty process-local privacy trackers.
                sm = SessionMap()
                monkeypatch.setattr(
                    sm, "_data", {"dashboard:" + slot.key: {"flags": dict.fromkeys(modes, True)}}
                )
                monkeypatch.setattr(ds.sessions, "_session_map", sm)
                privacy_mode.reset()
            else:
                for mode in modes:
                    privacy_mode.mark(mode, "dashboard:" + slot.key)
            for mode in incoming_modes:
                privacy_mode.mark(mode, _KEY)
            text, thread_ts, msg_ts = _reply("!bot off")
            await _send(slack, sessions, text, thread_ts=thread_ts, msg_ts=msg_ts)

        try:
            asyncio.run(_scenario())
            expected = modes + incoming_modes
            assert privacy_mode.is_temporary(_KEY) == ("temporary" in expected)
            assert privacy_mode.is_incognito(_KEY) == ("incognito" in expected)
            assert ds.get_linked_slot(_MSG_TS) is None
            assert not slot._slack_linked
            assert any("left Bot *web-frontend*'s chat" in p for p in _posts(slack))
            run_chat.assert_not_awaited()
        finally:
            privacy_mode.reset()

    @pytest.mark.parametrize("linked_owner", [True, False], ids=["dashboard-key", "slack-key"])
    @pytest.mark.parametrize("mode", ["temporary", "incognito"])
    def test_a_restricted_joined_thread_asking_for_another_bot_stays_joined(
        self, bots, audit, ds, run_chat, mode, linked_owner
    ):
        from kiro_crew.messaging import privacy_mode

        slot = ds._slots[member_slot_key("web-frontend")]
        _bot_chat(ds, "research")
        slack, sessions = RecordingSlackClient(), _Sessions()

        async def _scenario():
            await _send(slack, sessions, "!bot web-frontend")
            if linked_owner:
                sessions.linked_owner = "dashboard:" + slot.key
            privacy_mode.mark(mode, "dashboard:" + slot.key)
            text, thread_ts, msg_ts = _reply("!bot research")
            await _send(slack, sessions, text, thread_ts=thread_ts, msg_ts=msg_ts)

        try:
            asyncio.run(_scenario())
            assert ds.get_linked_slot(_MSG_TS) is slot
            assert "Send `!bot off` first" in _posts(slack)[-1]
            assert not any("left Bot" in p for p in _posts(slack))
            # Nothing was carried: the thread did not leave.
            assert not privacy_mode.is_temporary(_KEY) and not privacy_mode.is_incognito(_KEY)
            assert _KEY not in _handler._thread_agents
            run_chat.assert_not_awaited()
        finally:
            privacy_mode.reset()

    @pytest.mark.parametrize("refused_mode", ["temporary", "incognito"])
    def test_privacy_refusal_keeps_the_thread_joined(self, bots, audit, ds, run_chat, refused_mode):
        from kiro_crew.messaging import privacy_mode

        slot = ds._slots[member_slot_key("web-frontend")]
        slack, sessions = RecordingSlackClient(), _Sessions()
        persist = privacy_mode._persist_transcript_mode

        async def _persist(key, mode):
            if key == _KEY and mode == refused_mode:
                raise OSError("privacy header unavailable")
            await persist(key, mode)

        async def _scenario():
            await _send(slack, sessions, "!bot web-frontend")
            sessions.linked_owner = "dashboard:" + slot.key
            privacy_mode.mark_temporary(sessions.linked_owner)
            privacy_mode.mark_incognito(sessions.linked_owner)
            text, thread_ts, msg_ts = _reply("!bot off")
            with patch.object(privacy_mode, "_persist_transcript_mode", _persist):
                await _send(slack, sessions, text, thread_ts=thread_ts, msg_ts=msg_ts)

        try:
            asyncio.run(_scenario())
            assert ds.get_linked_slot(_MSG_TS) is slot
            assert sessions.calls == []
            assert not any("left Bot" in p for p in _posts(slack))
            assert privacy_mode.refusal_notice(
                refused_mode, privacy_mode.REFUSAL_PERSIST_FAILED
            ) in _posts(slack)
        finally:
            privacy_mode.reset()

    def test_a_dashboard_link_in_progress_is_seen(self, bots, audit, ds, run_chat):
        from kiro_crew.dashboard.chat_slack import link_slot_to_slack
        from kiro_crew.dashboard.chat_utils import effective_session_key

        slot = ds._slots[member_slot_key("web-frontend")]
        slack, sessions = RecordingSlackClient(), _Sessions()
        ds.slack_client = MagicMock()
        ds.owner_id = _OWNER

        async def _scenario():
            posting_anchor, join_loading = asyncio.Event(), asyncio.Event()

            async def _post_anchor(*args, **kwargs):
                posting_anchor.set()
                await await_until(join_loading, what="the Bot join's store load")
                return _SECOND_TS

            async def _load_store(_operation):
                # No yield after this signal: the join next checks the target or
                # waits for the dashboard's lock before the anchor can return.
                join_loading.set()

            ds.slack_client.post_message = AsyncMock(side_effect=_post_anchor)
            with patch("kiro_crew.slack.code_channels.binding.load_store_for_links", _load_store):
                dashboard = asyncio.create_task(
                    link_slot_to_slack(ds, slot, channel="D1", backfill=False, governed=False)
                )
                await await_until(posting_anchor, what="the dashboard's anchor post")
                result, _ = await await_until(
                    asyncio.gather(dashboard, _send(slack, sessions, "!bot web-frontend")),
                    what="the dashboard link and competing Bot join",
                )
            assert result == {"ok": True, "thread_ts": _SECOND_TS, "channel": "D1"}

        asyncio.run(_scenario())

        assert ds.get_linked_slot(_SECOND_TS) is slot and ds.get_linked_slot(_MSG_TS) is None
        assert ds.sessions.get_slack_link(effective_session_key(slot)) == (_SECOND_TS, "D1")
        assert "already talking to" in _posts(slack)[-1]
        assert not any("now talks to Bot" in p for p in _posts(slack))
        ds.sessions.aflush.assert_awaited_once()

    def test_a_join_landing_during_the_store_load_is_seen(self, bots, audit, ds, run_chat):
        # Another thread takes the chat while this join waits on the store: the check
        # runs after that wait, so this thread is refused and the first keeps the chat.
        slot = ds._slots[member_slot_key("web-frontend")]

        async def _other_thread_joins(_operation):
            ds.link_slack(slot.key, _SECOND_TS, "D1")

        with patch(
            "kiro_crew.slack.code_channels.binding.load_store_for_links", _other_thread_joins
        ):
            slack, _ = _run(_first("!bot web-frontend"))

        assert ds.get_linked_slot(_SECOND_TS) is slot and ds.get_linked_slot(_MSG_TS) is None
        assert "already talking to" in _posts(slack)[-1]


async def transport_send(slack, sessions, text):
    from kiro_crew.slack import transport_dispatch

    await transport_dispatch.handle_message_transport(
        slack=slack,
        sessions=sessions,
        channel="C0PINNED",
        text=text,
        thread_ts=None,
        msg_ts=_MSG_TS,
        user_id=_OWNER,
        context_builder=None,
        conversation_log=None,
    )
