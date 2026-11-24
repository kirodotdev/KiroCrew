"""``!conductor`` and ``/kirocrew conductor`` start a Conductor thread; its locks hold.

The thread-conductor RFC's part B (``kiro_crew.slack.conductor_thread``): the owner turns
a thread into a Conductor thread, one way; only the owner's messages reach it; the words
that would change what it is are answered, never run; and all of it is inert while the
feature is off. The identity and the dispatch verdict (part A) are pinned in
``test_slack_thread_conductor_identity.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.slack import conductor_thread as ct
from kiro_crew.slack import events_slash
from kiro_crew.slack import handler as _handler
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import surface as cc_surface
from kiro_crew.slack.code_channels.models import ChannelInfo
from kiro_crew.slack.code_channels.store import (
    ROOM_IM,
    ROOM_PRIVATE,
    ROOM_PUBLIC,
    Binding,
    CodeChannelStore,
    set_store,
)

OWNER = "U0OWNER0001"
STRANGER = "U0STRANGER1"
TEAM = "T0TEAM00001"
PRIVATE = "C0PRIVATE01"
PUBLIC = "C0PUBLIC001"
DM = "D0OWNERDM01"
THREAD = "1700000000.000100"
MSG = "1700000000.000200"


def _cfg(*, on: bool = True, public: bool = False) -> Any:
    return SimpleNamespace(
        slack=SimpleNamespace(thread_conductor=on, thread_conductor_public_channels=public)
    )


class _Slack:
    """The Slack calls the start and the locks make, recorded."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str | None]] = []
        self.ephemerals: list[tuple[str, str, str, str | None]] = []
        self._channel_team: dict[str, str] = {}

    async def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        self.posts.append((channel, text, thread_ts))
        return f"1700000999.{len(self.posts):06d}"

    async def post_ephemeral(
        self, channel: str, user_id: str, text: str, blocks: Any = None, thread_ts: Any = None
    ) -> None:
        self.ephemerals.append((channel, user_id, text, thread_ts))

    async def ensure_channel_team(self, channel: str, *, retry: bool = False) -> None:
        self._channel_team.setdefault(channel, TEAM)


@pytest.fixture(autouse=True)
def _on(_floor_monkeypatch, tmp_path):
    """Owner configured, the feature on, a loaded store, no real Slack reads."""
    mp = _floor_monkeypatch
    mp.setattr(_handler, "_owner_id", OWNER)
    mp.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", True)
    mp.setattr(cc_binding, "live_config", lambda orch=None: _cfg())
    mp.setattr(cc_surface, "workspace_admitted", lambda team: team == TEAM)
    recorder = MagicMock()
    mp.setattr(_handler, "sel", lambda: recorder)
    store = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    store.load()
    set_store(store)
    ct.forget_told()
    yield recorder
    set_store(None)
    ct.forget_told()


def _bind(channel: str = PRIVATE, *, key: str = "chat-conductor", ended: bool = False) -> Binding:
    from kiro_crew.slack.code_channels.store import store

    stored = store().put_thread_binding(
        Binding(
            channel_id=channel,
            team_id=TEAM,
            session_key=key,
            anchor_ts=THREAD,
            thread=True,
            room=ROOM_PRIVATE,
        )
    )
    if ended:
        stored = store().end_thread_binding(channel, THREAD)
    return stored


def _room(
    monkeypatch,
    *,
    private: bool | None = True,
    shared: bool | None = False,
    mpim: bool | None = False,
) -> None:
    """``conversations.info`` answers this for every channel."""

    async def info(self: Any, channel: str) -> ChannelInfo:
        return ChannelInfo(
            channel_id=channel,
            is_code_channel=False,
            is_private=bool(private),
            shared=shared,
            archived=False,
            mpim=mpim,
        )

    fake = SimpleNamespace(channel_info=lambda channel: info(None, channel))
    from kiro_crew.slack.code_channels import client as cc_client

    monkeypatch.setattr(cc_client.CodeChannelsClient, "from_slack", classmethod(lambda c, s: fake))


def _refusal(slack: Any, channel: str = PRIVATE, user: str = OWNER, **kw: Any) -> tuple[str, str]:
    return asyncio.run(
        ct.refusal(
            slack, channel, THREAD, user, "slack:thread", in_thread=kw.pop("in_thread", True), **kw
        )
    )


@pytest.fixture
def dashboard(monkeypatch):
    state = SimpleNamespace(get_linked_slot=lambda ts: None)
    monkeypatch.setattr(_handler, "_dashboard_state", state)
    return state


# ── who may start one, and where ──────────────────────────────────────────


def test_the_owner_may_start_in_a_private_channel_thread(monkeypatch, dashboard):
    _room(monkeypatch, private=True)
    assert _refusal(_Slack()) == ("", ROOM_PRIVATE)


def test_the_owners_dm_needs_no_channel_reading(monkeypatch, dashboard):
    assert _refusal(_Slack(), DM) == ("", ROOM_IM)


def test_a_public_channel_needs_the_opt_in(monkeypatch, dashboard):
    _room(monkeypatch, private=False)
    why, _ = _refusal(_Slack(), PUBLIC)
    assert "public channels are off" in why
    monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: _cfg(public=True))
    assert _refusal(_Slack(), PUBLIC) == ("", ROOM_PUBLIC)


@pytest.mark.parametrize("shared", [True, None])
def test_a_shared_or_unknown_channel_is_refused(monkeypatch, dashboard, shared):
    _room(monkeypatch, shared=shared)
    assert "shared outside this workspace" in _refusal(_Slack())[0]


@pytest.mark.parametrize("channel", ["G0GROUPDM01", "C0GROUPDM01"])
@pytest.mark.parametrize("mpim", [True, None])
def test_a_group_dm_is_refused_whatever_its_id_looks_like(monkeypatch, dashboard, channel, mpim):
    """Slack reports a group DM private, with a ``G`` or ``C`` id: only ``is_mpim`` tells."""
    _room(monkeypatch, private=True, mpim=mpim)
    assert "a group DM cannot hold one" in _refusal(_Slack(), channel)[0]


def test_conversations_info_carries_the_group_dm_flag():
    from kiro_crew.slack.code_channels.client import parse_channel_info

    assert parse_channel_info("C1", {"is_mpim": True}).mpim is True
    assert parse_channel_info("C1", {"is_mpim": False}).mpim is False
    assert parse_channel_info("C1", {}).mpim is None


def test_anyone_but_the_owner_is_refused(dashboard):
    assert _refusal(_Slack(), user=STRANGER)[0] == "Owner-only command."


def test_everything_is_inert_while_the_feature_is_off(monkeypatch, dashboard):
    monkeypatch.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", False)
    assert _refusal(_Slack())[0] == ct.OFF_TEXT
    monkeypatch.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", True)
    monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: _cfg(on=False))
    assert _refusal(_Slack())[0] == ct.OFF_TEXT


def test_outside_a_thread_it_points_at_the_slash_command(dashboard):
    assert _refusal(_Slack(), in_thread=False)[0] == ct.NOT_IN_THREAD_TEXT


def test_a_code_channel_is_refused(dashboard):
    from kiro_crew.slack.code_channels.store import store

    store().put_binding(
        Binding(channel_id=PRIVATE, team_id=TEAM, session_key="chat-c", anchor_ts="9.9")
    )
    assert _refusal(_Slack())[0] == ct.CODE_CHANNEL_TEXT


def test_a_second_start_is_refused(dashboard):
    _bind()
    assert _refusal(_Slack())[0] == ct.ALREADY_TEXT


def test_a_thread_linked_to_a_dashboard_chat_is_refused(dashboard):
    dashboard.get_linked_slot = lambda ts: object()
    assert _refusal(_Slack())[0] == ct.LINKED_TEXT


def test_a_bot_thread_is_never_made_a_conductor_thread(monkeypatch, dashboard):
    monkeypatch.setitem(_handler._thread_agents, "slack:thread", "research")
    monkeypatch.setattr(_handler, "crew_bots", lambda: {"research": "Research Bot"})
    assert _refusal(_Slack())[0] == ct.BOT_THREAD_TEXT


def test_the_one_refusal_both_commands_share():
    _bind()
    assert ct.switch_refusal("!bot", PRIVATE, THREAD, "") == ct.LOCKED_TEXT
    assert ct.switch_refusal("!conductor", PRIVATE, THREAD, "") == ct.ALREADY_TEXT
    assert ct.switch_refusal("!bot", PRIVATE, "1.2", "") == ""


# ── the locks ─────────────────────────────────────────────────────────────


def _gate(slack: Any, text: str, user: str = OWNER, slot: Any = "live", **kw: Any) -> bool:
    linked = object() if slot == "live" else slot
    return asyncio.run(ct.gate_message(text, user, PRIVATE, slack, THREAD, linked, **kw))


def test_a_plain_thread_is_not_gated():
    assert _gate(_Slack(), "!ta research") is False


def test_the_owners_messages_reach_the_conductor():
    _bind()
    assert _gate(_Slack(), "fix the login bug and open a PR") is False
    assert _gate(_Slack(), "!stop") is False


@pytest.mark.parametrize(
    "text, reply",
    [
        ("!conductor again", ct.ALREADY_TEXT),
        ("!bot research", ct.LOCKED_TEXT),
        ("!ta kirocrew", ct.LOCKED_TEXT),
        ("!agent kirocrew", ct.LOCKED_TEXT),
        ("/agent kirocrew", ct.LOCKED_TEXT),
        ("<@U0BOT00001> /agent kirocrew", ct.LOCKED_TEXT),
        ("!link-to-dashboard", ct.ALREADY_TEXT),
    ],
)
def test_words_that_would_change_the_thread_are_answered_never_run(text, reply, _on):
    _bind()
    slack = _Slack()
    assert _gate(slack, text) is True
    assert slack.posts == [(PRIVATE, reply, THREAD)]
    assert _on.log_api_access.call_args.kwargs["outcome"] == "denied"


def test_another_members_message_is_dropped_logged_and_told_once(_on):
    _bind()
    slack = _Slack()
    assert _gate(slack, "please also do X", user=STRANGER) is True
    assert _gate(slack, "and Y", user=STRANGER) is True
    assert slack.posts == []
    assert slack.ephemerals == [(PRIVATE, STRANGER, ct.OWNER_ONLY_TEXT, THREAD)]
    assert _on.log_api_access.call_args.kwargs["error"] == "conductor thread is owner-only"


def test_a_shared_users_message_is_dropped_too(monkeypatch):
    _bind()
    monkeypatch.setattr(_handler, "is_shared_user", lambda user: user == "U0SHARED001")
    assert _gate(_Slack(), "hi", user="U0SHARED001") is True


@pytest.mark.parametrize("ended, slot", [(True, "live"), (False, None)])
def test_an_ended_thread_runs_nothing_and_never_falls_back(ended, slot):
    _bind(ended=ended)
    slack = _Slack()
    assert _gate(slack, "keep going", slot=slot) is True
    assert slack.posts == [(PRIVATE, ct.ENDED_TEXT, THREAD)]


def test_the_linked_thread_router_asks_the_gate_first(monkeypatch):
    """``route_linked_thread`` refuses a non-owner in a Conductor thread before routing."""
    _bind()
    slot = SimpleNamespace(key="chat-conductor")
    state = SimpleNamespace(get_linked_slot=lambda ts: slot)
    monkeypatch.setattr(_handler, "_dashboard_state", state)
    monkeypatch.setattr(_handler, "is_shared_listed", lambda user: False)
    slack = _Slack()
    routed = asyncio.run(
        _handler.route_linked_thread("hi", "slack:x", STRANGER, PRIVATE, slack, THREAD)
    )
    assert routed is _handler.LinkedRoute.REFUSED


def test_the_switch_words_fall_through_a_linked_thread_to_their_handlers():
    assert {"!bot", "!conductor"} <= _handler._NATIVE_BANG_COMMANDS


# ── the dashboard agent switch ────────────────────────────────────────────


def _linked_conductor_slot() -> tuple[Any, Any]:
    """The conductor's slot as the identity check sees it: linked to the thread's anchor."""
    slot = SimpleNamespace(
        key="chat-conductor", mode="", agent="kirocrew-conductor", _slack_channel=PRIVATE
    )
    state = MagicMock()
    state.get_linked_slot = lambda ts: slot if ts == THREAD else None
    return slot, state


def test_an_unlinked_slot_a_stored_row_names_is_not_locked():
    """The lock uses the strict identity: a row naming a slot not linked to its anchor
    does not lock that slot (it is no Conductor thread's session)."""
    from kiro_crew.dashboard.chat_handlers import SlotAgentSwitchCaller, switch_slot_agent

    _bind()
    slot = SimpleNamespace(key="chat-conductor", mode="", agent="x", _slack_channel=PRIVATE)
    state = MagicMock()
    state.get_linked_slot = lambda ts: None
    caller = SlotAgentSwitchCaller(app="", remote="test", owner=True, notice=False)
    with pytest.raises(AttributeError, match="_lock"):
        asyncio.run(switch_slot_agent(state, slot, slot.key, "kirocrew", "", caller))


def test_the_conductors_chat_cannot_switch_to_a_non_conductor():
    from kiro_crew.dashboard.chat_handlers import SlotAgentSwitchCaller, switch_slot_agent

    _bind()
    slot, state = _linked_conductor_slot()
    caller = SlotAgentSwitchCaller(app="", remote="test", owner=True, notice=False)
    resp = asyncio.run(switch_slot_agent(state, slot, slot.key, "kirocrew", "", caller))
    assert resp.status == 409 and b"conductor_thread_agent_locked" in resp.body


def test_the_crew_mode_migration_may_move_it_to_another_conductor(monkeypatch):
    """Changing ``agent.crew_mode_agent`` moves every conductor chat; the lock lets
    a move to a conductor through, since the thread stays a Conductor thread."""
    from kiro_crew import crew_mode
    from kiro_crew.dashboard.chat_handlers import SlotAgentSwitchCaller, switch_slot_agent

    _bind()
    monkeypatch.setattr(
        crew_mode, "conductor_agents", lambda cfg=None: frozenset({"new-conductor"})
    )
    slot, state = _linked_conductor_slot()
    caller = SlotAgentSwitchCaller.for_crew_mode_migration()
    # Past every refusal the switch checks first, it takes the slot's lock, which this
    # stand-in slot does not have: reaching it proves the conductor lock let it by.
    with pytest.raises(AttributeError, match="_lock"):
        asyncio.run(switch_slot_agent(state, slot, slot.key, "new-conductor", "template", caller))


# ── the start, end to end ─────────────────────────────────────────────────


def test_the_start_binds_files_seeds_and_routes_the_goal(tmp_path, monkeypatch, _on):
    from chat_test_helpers import _make_state

    from kiro_crew.slack.code_channels.store import store

    state = _make_state(tmp_path)
    monkeypatch.setattr(_handler, "_dashboard_state", state)
    monkeypatch.setattr(ct, "_seed", AsyncMock(return_value="[Slack Conductor thread]"))
    routed = AsyncMock(return_value=_handler.LinkedRoute.ROUTED)
    monkeypatch.setattr(_handler, "route_linked_thread", routed)
    _room(monkeypatch, private=True)
    slack = _Slack()

    started, text = asyncio.run(
        ct.start(
            slack, PRIVATE, THREAD, OWNER, "fix the login bug", session_key="slack:t", msg_ts=MSG
        )
    )
    assert started and text == ct.STARTED_TEXT
    bound = store().get_thread_binding(PRIVATE, THREAD)
    assert bound is not None and bound.room == ROOM_PRIVATE and bound.team_id == TEAM
    slot = state.get_slot(bound.session_key)
    assert state.get_linked_slot(THREAD) is slot
    assert slot.folder_id  # filed in its own folder, so workers file beside it
    assert any(e.get("source") == ct.SEED_SOURCE for e in slot._pending_context)
    assert (PRIVATE, ct.STARTED_TEXT, THREAD) in slack.posts
    assert routed.await_args.args[:3] == ("fix the login bug", slot.key, OWNER)


def test_a_refused_start_changes_nothing(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.slack.code_channels.store import store

    state = _make_state(tmp_path)
    monkeypatch.setattr(_handler, "_dashboard_state", state)
    _room(monkeypatch, private=False)
    slack = _Slack()
    started, text = asyncio.run(ct.start(slack, PUBLIC, THREAD, OWNER, "goal"))
    assert not started and "public channels are off" in text
    assert store().thread_bindings() == [] and slack.posts == []


# ── the commands ──────────────────────────────────────────────────────────


def test_bang_conductor_runs_the_start_and_says_why_not(monkeypatch):
    start = AsyncMock(return_value=(False, ct.OFF_TEXT))
    monkeypatch.setattr(ct, "start", start)
    slack = _Slack()
    asyncio.run(
        _handler._handle_slash_command(
            "!conductor ship it", slack, MagicMock(), PRIVATE, THREAD, MSG, "slack:t", OWNER
        )
    )
    assert start.await_args.args[4] == "ship it"
    assert start.await_args.kwargs["in_thread"] is True
    assert slack.posts == [(PRIVATE, f"❌ {ct.OFF_TEXT}", THREAD)]


def _slash(monkeypatch, args: str, user: str = OWNER, channel: str = PRIVATE):
    slack = _Slack()
    orch = SimpleNamespace(slack=slack)
    replies: list[str] = []

    async def respond(text: str, blocks: Any = None) -> None:
        replies.append(text)

    async def typed_in_channel() -> None:
        from kiro_crew.slack import slash_context

        slash_context.room.set((channel, TEAM))
        await events_slash._handle_conductor(orch, user, args, respond)

    asyncio.run(typed_in_channel())
    return slack, replies


def _typed(monkeypatch, slack: _Slack, *commands: tuple[str, str]) -> None:
    """Hand each ``(channel, text)`` to ``_handle_slash`` as the owner's ``/kirocrew``,
    every one before any handler task runs, then let the handlers finish."""
    monkeypatch.setattr("kiro_crew.slack.events.sel", lambda: MagicMock())
    orch = SimpleNamespace(
        slack=slack, _owner_id=OWNER, slack_command="kirocrew", _cfg=_cfg(), _handler_tasks=set()
    )

    async def typed() -> None:
        for channel, text in commands:
            payload = {
                "command": "/kirocrew",
                "text": text,
                "user_id": OWNER,
                "channel_id": channel,
                "team_id": TEAM,
            }
            await events_slash._handle_slash(orch, payload)
        # No handler task has run yet, so the commands were handled at the same time.
        assert slack.posts == []
        await asyncio.gather(*orch._handler_tasks)

    asyncio.run(typed())


def test_slash_conductor_posts_a_parent_and_starts_under_it(monkeypatch, dashboard):
    monkeypatch.setattr(ct, "refusal", AsyncMock(return_value=("", ROOM_PRIVATE)))
    start = AsyncMock(return_value=(True, ct.STARTED_TEXT))
    monkeypatch.setattr(ct, "start", start)
    slack, replies = _slash(monkeypatch, "fix the login bug")
    assert slack.posts == [(PRIVATE, "Conductor: fix the login bug", None)]
    parent = "1700000999.000001"
    assert start.await_args.args[:5] == (slack, PRIVATE, parent, OWNER, "fix the login bug")
    assert replies == [ct.STARTED_TEXT]


@pytest.mark.parametrize(
    "args, user, on, reply",
    [
        ("goal", STRANGER, True, "⛔ Owner-only command."),
        ("goal", OWNER, False, f"❌ {ct.OFF_TEXT}"),
        ("", OWNER, True, "Usage: `/kirocrew conductor <goal>`"),
    ],
)
def test_slash_conductor_refusals_post_nothing(monkeypatch, args, user, on, reply):
    monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: _cfg(on=on))
    slack, replies = _slash(monkeypatch, args, user=user)
    assert replies == [reply] and slack.posts == []


def test_slash_conductor_refused_room_posts_nothing(monkeypatch, dashboard):
    _room(monkeypatch, private=False)
    slack, replies = _slash(monkeypatch, "goal", channel=PUBLIC)
    assert slack.posts == [] and "public channels are off" in replies[0]


def test_overlapping_slash_conductors_each_post_where_they_were_typed(monkeypatch, dashboard):
    monkeypatch.setattr(ct, "refusal", AsyncMock(return_value=("", ROOM_PRIVATE)))
    monkeypatch.setattr(ct, "start", AsyncMock(return_value=(True, ct.STARTED_TEXT)))
    slack = _Slack()
    _typed(monkeypatch, slack, (PRIVATE, "conductor first"), (PUBLIC, "conductor second"))
    assert sorted(slack.posts) == [
        (PRIVATE, "Conductor: first", None),
        (PUBLIC, "Conductor: second", None),
    ]


def test_a_refused_slash_conductor_audits_the_denial(monkeypatch, dashboard, _on):
    _room(monkeypatch, private=False)
    slack = _Slack()
    _typed(monkeypatch, slack, (PUBLIC, "conductor goal"))
    why = ct.NOT_HERE_TEXT.format(
        why="public channels are off (slack.thread_conductor_public_channels)"
    )
    assert [c.kwargs for c in _on.log_api_access.call_args_list] == [
        {
            "caller": OWNER,
            "operation": ct.OPERATION,
            "outcome": "denied",
            "source": "slack",
            "resources": f"channel={PUBLIC}",
            "error": why,
        }
    ]
    assert slack.posts == []
