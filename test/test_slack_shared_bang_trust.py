"""``!trust`` from a shared user (``slack.shared_access``): their own turns only.

The owner's ``!trust`` puts the thread's session on Trust. A confirmed shared user's
puts only that sender's own turns on it: not the owner's, not another shared user's,
not a sub-agent's, and it writes no approval policy. It is bound to the session that
is running when it is made and ends when that session is replaced.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from session_helpers import _mock_provider_factory, cfg_fixture  # noqa: F401 - cfg, by name
from test_slack_sender_trust import Sessions

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.messaging import session_trust
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.session import SessionManager
from kiro_crew.slack import handler, shared_access, thread_trust

OWNER = "UOWNER0001"
FRIEND = "UFRIEND001"
OTHER = "UOTHER0001"
STRANGER = "USTRANGER1"
TS = "1.2"
KEY = "slack:1.2"
AGENT = "kirocrew"


def _cfg(ids: tuple[str, ...] = (FRIEND, OTHER)) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.slack = _build_slack_config(
        {"shared_access": {"enabled": True, "user_ids": list(ids), "verify_with_slack": False}}
    )
    return cfg


@pytest.fixture(autouse=True)
def _world(_floor_monkeypatch: pytest.MonkeyPatch):
    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr(session_trust, "_trusted_sessions", {})
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: True)
    monkeypatch.setattr("kiro_crew.safety_override.approval_mode_permitted", lambda _m: True)
    store = MagicMock()
    store.get_binding.return_value = None
    store.get_thread_binding.return_value = None
    store.is_thread_quarantined.return_value = False
    monkeypatch.setattr("kiro_crew.slack.code_channels.store.store", lambda: store)
    monkeypatch.setattr(thread_trust, "_audit", MagicMock())
    monkeypatch.setattr(handler, "sel", MagicMock())
    monkeypatch.setattr(handler, "_dashboard_state", None)
    monkeypatch.setattr(handler, "_owner_id", OWNER)
    monkeypatch.setattr(handler, "_orch_cfg", _cfg())
    shared_access.install(None)
    for user in (FRIEND, OTHER):
        asyncio.run(shared_access.prime_shared_user(user))
        assert handler.is_shared_user(user)
    yield store
    shared_access.reset()


@pytest.fixture()
def live() -> Sessions:
    """The thread's session, running on the default agent."""
    return Sessions()


async def _say(text: str, user: str, sessions, *, channel: str = "C1", key: str = KEY) -> str:
    """Type *text* as *user* through the ``!`` router; the one reply it posted."""
    slack = MagicMock()
    slack.post_message = AsyncMock()
    slack.post_blocks = AsyncMock()
    handled = await handler._route_bang_command(
        text, slack, sessions, channel, TS, "m1", key, user, None
    )
    assert handled is True
    slack.post_message.assert_awaited_once()
    posted_channel, reply, thread = slack.post_message.await_args.args
    assert (posted_channel, thread) == (channel, TS)
    return reply


def _own_call(**marks) -> LLMEvent:
    marks = {"child_calls_marked": True, **marks}
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="Run: ls", request_id="r1", **marks)


async def _asks(monkeypatch, sender: str, sessions, **marks) -> bool:
    """Whether a tool call of *sender*'s native turn on ``KEY`` reaches the prompt."""
    import test_slack_ladder_held_bot_limit as ladder

    from kiro_crew import mate_held_rules

    monkeypatch.setattr(mate_held_rules, "held_for", lambda *a, **k: [])
    prompt = AsyncMock(return_value=False)
    monkeypatch.setattr(handler, "_request_approval", prompt)
    monkeypatch.setattr(handler, "is_yolo_mode", lambda: False)
    monkeypatch.setattr(handler, "_should_auto_approve_spawn", lambda cb, ev: False)
    client = MagicMock()
    client.approve_tool = AsyncMock(return_value=True)
    client.reject_tool = AsyncMock()
    turn = ladder._turn(handler.APPROVAL_INTERACTIVE, None)
    turn.user_id = sender
    turn.sessions = sessions
    assert turn.session_key == KEY
    await handler._answer_permission_request(turn, client, _own_call(**marks))
    assert prompt.await_count + client.approve_tool.await_count == 1
    return prompt.await_count == 1


def _rows() -> list[tuple]:
    return [call.args for call in thread_trust._audit.call_args_list]


# ── What the grant covers ──


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["", " on"])
async def test_a_shared_users_trust_stops_the_prompts_of_their_own_turns_only(
    monkeypatch, live, arg
):
    assert await _say(f"!trust{arg}", FRIEND, live) == thread_trust.OWN_ON_TEXT
    assert await _asks(monkeypatch, FRIEND, live) is False
    assert await _asks(monkeypatch, OWNER, live) is True
    assert await _asks(monkeypatch, OTHER, live) is True
    # Not the session's Trust, and no approval policy: a sub-agent reads that policy.
    assert not session_trust.is_session_trusted(KEY)
    live.set_approval_policy.assert_not_called()
    assert _rows() == [
        (
            FRIEND,
            "allowed",
            f"session={KEY} sender={FRIEND} mode=trust agent={AGENT} result=set",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marks",
    [{"sub_session_id": "child-1"}, {"native_child_call": True}, {"child_calls_marked": False}],
    ids=["child session", "native sub-agent", "unmarked backend"],
)
async def test_a_helper_agents_call_in_their_own_turn_still_asks(monkeypatch, live, marks):
    await _say("!trust", FRIEND, live)
    assert await _asks(monkeypatch, FRIEND, live, **marks) is True
    # The grant is not spent: the turn's own next call runs without asking.
    assert await _asks(monkeypatch, FRIEND, live) is False


@pytest.mark.asyncio
async def test_a_second_trust_says_already_on(live):
    await _say("!trust", FRIEND, live)
    assert await _say("!trust", FRIEND, live) == thread_trust.OWN_ALREADY_ON_TEXT
    assert session_trust._trusted_senders == {
        KEY: {FRIEND: thread_trust.session_binding(live, KEY)}
    }


@pytest.mark.asyncio
async def test_each_shared_user_holds_their_own_grant(monkeypatch, live):
    await _say("!trust", FRIEND, live)
    assert await _say("!trust status", OTHER, live) == thread_trust.OWN_STATUS_OFF_TEXT
    await _say("!trust", OTHER, live)
    assert await _say("!trust off", FRIEND, live) == thread_trust.OWN_OFF_TEXT
    assert await _asks(monkeypatch, FRIEND, live) is True
    assert await _asks(monkeypatch, OTHER, live) is False


@pytest.mark.asyncio
async def test_off_removes_only_their_own_grant_while_the_owners_is_on(monkeypatch, live):
    assert await _say("!trust", OWNER, live) == thread_trust.ON_TEXT
    await _say("!trust", FRIEND, live)
    assert await _say("!trust off", FRIEND, live) == thread_trust.OWN_OFF_TEXT
    assert await _say("!trust off", FRIEND, live) == thread_trust.OWN_ALREADY_OFF_TEXT
    # The owner's grant is untouched, and it still does not cover the shared user.
    assert session_trust.is_session_trusted(KEY)
    live.set_approval_policy.assert_not_called()
    assert await _asks(monkeypatch, OWNER, live) is False
    assert await _asks(monkeypatch, FRIEND, live) is True


@pytest.mark.asyncio
async def test_status_tells_a_shared_user_their_own_state(live):
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_OFF_TEXT
    await _say("!trust", OWNER, live)
    # The owner's grant is not theirs: their turns still ask.
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_OFF_TEXT
    await _say("!trust", FRIEND, live)
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_ON_TEXT
    assert session_trust._trusted_senders == {
        KEY: {FRIEND: thread_trust.session_binding(live, KEY)}
    }


@pytest.mark.asyncio
async def test_the_owners_off_ends_every_shared_users_grant_in_the_thread(monkeypatch, live):
    await _say("!trust", FRIEND, live)
    await _say("!trust", OTHER, live)
    note = thread_trust.SENDERS_TEXT.format(count=2)
    assert await _say("!trust status", OWNER, live) == thread_trust.STATUS_OFF_TEXT + note
    assert await _say("!trust off", OWNER, live) == thread_trust.OFF_TEXT
    assert not session_trust._trusted_senders
    assert _rows()[-1] == (
        OWNER,
        "allowed",
        f"session={KEY} mode=normal senders={FRIEND},{OTHER} result=set",
    )
    assert await _asks(monkeypatch, FRIEND, live) is True
    assert await _say("!trust status", OWNER, live) == thread_trust.STATUS_OFF_TEXT
    assert await _say("!trust off", OWNER, live) == thread_trust.ALREADY_OFF_TEXT


@pytest.mark.asyncio
async def test_the_owners_trust_is_still_the_sessions_and_needs_no_running_session(monkeypatch):
    idle = Sessions(live=False)
    assert await _say("!trust", OWNER, idle) == thread_trust.ON_TEXT
    assert session_trust.is_session_trusted(KEY) and not session_trust._trusted_senders
    assert await _asks(monkeypatch, OWNER, idle) is False
    assert await _asks(monkeypatch, FRIEND, idle) is True


# ── Who may type it ──


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["", " on", " off", " status"])
async def test_a_person_who_is_not_allowed_is_refused_and_audited(live, arg):
    assert await _say(f"!trust{arg}", STRANGER, live) == "⛔ Not authorized."
    assert not session_trust._trusted_senders and not session_trust._trusted_sessions
    row = handler.sel.return_value.log_api_access.call_args.kwargs
    assert (row["caller"], row["operation"], row["outcome"], row["resources"]) == (
        STRANGER,
        "slack.allowed_command",
        "denied",
        "!trust",
    )


@pytest.mark.asyncio
async def test_a_listed_person_slack_has_not_confirmed_is_refused(monkeypatch, live):
    monkeypatch.setattr(handler, "_orch_cfg", _cfg(ids=(FRIEND, OTHER, STRANGER)))
    assert handler.is_shared_listed(STRANGER) and not handler.is_shared_user(STRANGER)
    assert await _say("!trust", STRANGER, live) == "⛔ Not authorized."
    assert not session_trust._trusted_senders


@pytest.mark.asyncio
async def test_the_command_itself_refuses_a_sender_who_is_not_a_shared_user(live):
    # Past the router (another caller of the handler): the same refusal, audited.
    reply = await thread_trust.handle(
        "",
        state=None,
        session_key=KEY,
        channel="C1",
        reply_ts=TS,
        user=STRANGER,
        shared=True,
        sessions=live,
    )
    assert reply == thread_trust.NOT_ALLOWED_TEXT
    assert not session_trust._trusted_senders
    assert _rows() == [(STRANGER, "denied", f"session={KEY}", "unauthorized sender")]


# ── What still refuses ──


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["session_trust", "approval_modes"])
@pytest.mark.parametrize("arg", ["", " on"])
async def test_both_policy_gates_refuse_a_shared_users_grant(monkeypatch, live, gate, arg):
    if gate == "session_trust":
        monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    else:
        monkeypatch.setattr("kiro_crew.safety_override.approval_mode_permitted", lambda _m: False)
    assert await _say(f"!trust{arg}", FRIEND, live) == thread_trust.POLICY_TEXT
    assert not session_trust._trusted_senders
    assert _rows()[-1][:2] == (FRIEND, "denied") and _rows()[-1][3] == "mode_disabled_by_policy"


@pytest.mark.asyncio
async def test_status_does_not_say_on_while_policy_masks_the_grant(monkeypatch, live):
    await _say("!trust", FRIEND, live)
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    assert await _asks(monkeypatch, FRIEND, live) is True
    assert await _say("!trust status", FRIEND, live) == thread_trust.POLICY_TEXT
    # The masked grant can still be removed.
    assert await _say("!trust off", FRIEND, live) == thread_trust.OWN_OFF_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["taken off the list", "session replaced"])
async def test_a_change_while_the_grant_is_checked_grants_nothing(monkeypatch, live, meanwhile):
    def _changes(_mode: str) -> bool:
        # What lands while the policy check runs off the loop.
        if meanwhile == "taken off the list":
            handler._orch_cfg.slack.shared_access.user_ids.remove(FRIEND)
            thread_trust.drop_unlisted_senders(handler._orch_cfg)
        else:
            live.replace()
        return True

    monkeypatch.setattr("kiro_crew.safety_override.approval_mode_permitted", _changes)
    assert await _say("!trust", FRIEND, live) == thread_trust.NOT_ALLOWED_TEXT
    assert not session_trust._trusted_senders
    assert _rows()[-1][:2] == (FRIEND, "denied")


@pytest.mark.asyncio
async def test_a_policy_deny_ends_a_grant_already_made(monkeypatch, live):
    await _say("!trust", FRIEND, live)
    session_trust._on_trust_denied(frozenset({"trust"}))
    assert await _asks(monkeypatch, FRIEND, live) is True
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_OFF_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["code channel", "conductor thread", "quarantined thread"])
@pytest.mark.parametrize("arg", ["", " off", " status"])
async def test_a_code_channel_and_a_conductor_thread_keep_their_refusal(_world, live, which, arg):
    if which == "code channel":
        _world.get_binding.return_value = object()
    elif which == "conductor thread":
        _world.get_thread_binding.return_value = object()
    else:
        _world.is_thread_quarantined.return_value = True
    assert await _say(f"!trust{arg}", FRIEND, live) == thread_trust.CODE_CHANNEL_TEXT
    assert not session_trust._trusted_senders


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["", " off", " status"])
async def test_a_thread_run_by_another_session_keeps_its_refusal(live, arg):
    live.get_session_for_thread = lambda thread_ts: "dashboard:abc"
    assert await _say(f"!trust{arg}", FRIEND, live) == thread_trust.LINKED_TEXT
    assert not session_trust._trusted_senders


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_kind", ["bot", "conductor"])
@pytest.mark.parametrize("arg", ["", " on"])
async def test_a_channel_with_a_bot_or_conductor_refuses_a_shared_users_grant(
    monkeypatch, live, handler_kind, arg
):
    # The channel's one chat answers this thread's next message, so a grant on the
    # thread's own session would say Trust is on while their tools still ask.
    from kiro_crew.slack import channel_handler

    if handler_kind == "bot":
        monkeypatch.setattr(channel_handler, "channel_bot", lambda _c: "helper")
    else:
        monkeypatch.setattr(channel_handler, "channel_conductor", lambda _c: object())
    assert await _say(f"!trust{arg}", FRIEND, live) == thread_trust.LINKED_TEXT
    assert not session_trust._trusted_senders
    # A grant made before the channel had a Bot or Conductor can still be seen and removed.
    session_trust.add_trusted_sender(KEY, FRIEND, thread_trust.session_binding(live, KEY))
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_ON_TEXT
    assert await _say("!trust off", FRIEND, live) == thread_trust.OWN_OFF_TEXT
    assert not session_trust._trusted_senders


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["code channel", "linked"])
async def test_their_own_grant_can_be_seen_and_removed_in_a_refused_thread(_world, live, which):
    # However the grant got there (the card's Trust button will make one through
    # these functions), its holder can always read it and take it off.
    key = KEY
    if which == "code channel":
        _world.get_binding.return_value = object()
    else:
        key = "dashboard:abc"
        live.get_session_for_thread = lambda thread_ts: key
    session_trust.add_trusted_sender(key, FRIEND, thread_trust.session_binding(live, key))
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_ON_TEXT
    refusal = (
        thread_trust.CODE_CHANNEL_TEXT if which == "code channel" else thread_trust.LINKED_TEXT
    )
    assert await _say("!trust", FRIEND, live) == refusal
    assert await _say("!trust status", OTHER, live) == refusal
    assert await _say("!trust off", FRIEND, live) == thread_trust.OWN_OFF_TEXT
    assert not session_trust._trusted_senders
    assert await _say("!trust off", FRIEND, live) == refusal


@pytest.mark.asyncio
async def test_an_unknown_word_shows_usage_and_grants_nothing(live):
    assert await _say("!trust please", FRIEND, live) == thread_trust.USAGE_TEXT
    assert not session_trust._trusted_senders


# ── The grant lives and ends with the running session ──


@pytest.mark.asyncio
async def test_a_grant_needs_a_running_session_to_bind_to(monkeypatch):
    idle = Sessions(live=False)
    assert await _say("!trust", FRIEND, idle) == thread_trust.NO_SESSION_TEXT
    assert not session_trust._trusted_senders
    assert _rows() == [
        (FRIEND, "denied", f"session={KEY} sender={FRIEND} mode=trust", "no running session")
    ]
    assert await _say("!trust status", FRIEND, idle) == thread_trust.OWN_STATUS_OFF_TEXT
    # Once a message has started the session, the same command grants.
    idle.live = True
    assert await _say("!trust", FRIEND, idle) == thread_trust.OWN_ON_TEXT
    assert await _asks(monkeypatch, FRIEND, idle) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["same agent", "another agent", "closed"])
async def test_a_replaced_session_ends_the_grant(monkeypatch, live, change):
    await _say("!trust", FRIEND, live)
    assert await _asks(monkeypatch, FRIEND, live) is False
    if change == "same agent":
        live.replace()
    elif change == "another agent":
        live.replace("reviewer")
    else:
        live.live = False
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_OFF_TEXT
    assert not session_trust._trusted_senders
    live.live = True
    assert await _asks(monkeypatch, FRIEND, live) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("typed_by", [FRIEND, OWNER])
async def test_a_compact_command_keeps_the_grant(monkeypatch, cfg, typed_by):
    # `!compact` runs on the real session manager's session: the same provider and
    # the same agent afterwards. The owner's Trust holds across it, and so does a
    # shared user's.
    # Not a code channel's session: the store stub would say it is.
    monkeypatch.setattr(handler, "shared_user_refused_for_session", AsyncMock(return_value=False))
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    provider, _, _ = await mgr.get_or_create(KEY, agent=AGENT)
    mgr.release(KEY)
    provider.manual_compact_unsupported_backend = None
    provider.wait_for_compaction = AsyncMock(return_value={"type": "completed", "summary": ""})
    assert "on *Trust*" in await _say("!trust", FRIEND, mgr)
    binding = thread_trust.session_binding(mgr, KEY)
    assert not await _asks(monkeypatch, FRIEND, mgr)

    slack = MagicMock()
    for name in ("post_message", "post_blocks", "add_reaction", "remove_reaction"):
        setattr(slack, name, AsyncMock())
    assert await handler._route_bang_command(
        "!compact", slack, mgr, "C1", TS, "m1", KEY, typed_by, None
    )

    provider.compact.assert_awaited_once()
    assert "✅ Context compacted." in [c.args[1] for c in slack.post_message.await_args_list]
    assert thread_trust.session_binding(mgr, KEY) == binding
    assert await _say("!trust status", FRIEND, mgr) == thread_trust.OWN_STATUS_ON_TEXT
    assert not await _asks(monkeypatch, FRIEND, mgr)
    await mgr.close_all()


async def _bang_ta(monkeypatch, mgr: SessionManager, name: str, resolves: bool = True) -> None:
    """The owner's real ``!ta <name>`` on the real session manager, then the turn
    that starts the thread's session on the agent it left the thread on."""
    monkeypatch.setattr(
        handler, "_resolve_agent_name", lambda typed, project: typed if resolves else None
    )
    monkeypatch.setattr(handler, "_list_all_agent_names", lambda: "kirocrew")
    monkeypatch.setattr(handler, "memory_binding_refusal", AsyncMock(return_value=""))
    monkeypatch.setattr(handler, "_hydrate_thread_overrides", AsyncMock(return_value=None))
    slack = MagicMock(post_message=AsyncMock(), post_blocks=AsyncMock())
    await handler._handle_slash_command(f"!ta {name}", slack, mgr, "C1", TS, "m1", KEY, OWNER)
    await mgr.get_or_create(KEY, agent=handler._thread_agents.get(KEY) or AGENT)
    mgr.release(KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("away", ["reviewer", "off"])
async def test_a_thread_agent_switched_away_and_back_does_not_revive_the_grant(
    monkeypatch, cfg, away
):
    # There and back with no turn of the grantee's in between, on the real session
    # manager: `!ta` replaces the thread's session, so the grant's binding is gone.
    monkeypatch.setattr(handler, "_thread_agents", {KEY: "helper"})
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.get_or_create(KEY, agent="helper")
    mgr.release(KEY)
    await _say("!trust", FRIEND, mgr)
    await _say("!trust", OTHER, mgr)
    assert await _asks(monkeypatch, FRIEND, mgr) is False
    await _bang_ta(monkeypatch, mgr, away)
    await _bang_ta(monkeypatch, mgr, "helper")
    assert (handler._thread_agents.get(KEY), mgr.get_agent(KEY)) == ("helper", "helper")
    assert await _asks(monkeypatch, FRIEND, mgr) is True
    assert await _say("!trust status", OTHER, mgr) == thread_trust.OWN_STATUS_OFF_TEXT
    assert not session_trust._trusted_senders


@pytest.mark.asyncio
async def test_a_refused_thread_agent_switch_keeps_the_grant(monkeypatch, cfg):
    monkeypatch.setattr(handler, "_thread_agents", {})
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.get_or_create(KEY, agent=AGENT)
    mgr.release(KEY)
    await _say("!trust", FRIEND, mgr)
    await _bang_ta(monkeypatch, mgr, "nobody", resolves=False)
    assert await _asks(monkeypatch, FRIEND, mgr) is False


@pytest.mark.asyncio
async def test_a_thread_agent_switch_leaves_another_threads_grant(monkeypatch, cfg):
    other = "slack:9.9"
    monkeypatch.setattr(handler, "_thread_agents", {})
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    for key in (KEY, other):
        await mgr.get_or_create(key, agent=AGENT)
        mgr.release(key)
    session_trust.add_trusted_sender(other, FRIEND, thread_trust.session_binding(mgr, other))
    await _say("!trust", FRIEND, mgr)
    await _bang_ta(monkeypatch, mgr, "reviewer")
    assert await _asks(monkeypatch, FRIEND, mgr) is True
    assert thread_trust.sender_trusted(mgr, other, FRIEND, _own_call())


@pytest.mark.asyncio
async def test_joining_a_bots_chat_ends_the_grants_on_the_threads_own_session(monkeypatch, live):
    import contextlib

    from kiro_crew.slack import bot_chat

    slot = MagicMock(key="member:helper")
    state = MagicMock()
    state.sessions.aflush = AsyncMock()

    @contextlib.asynccontextmanager
    async def _lock(_state, _key):
        yield

    monkeypatch.setattr(bot_chat, "bot_chat_target", lambda *a: (slot, ""))
    monkeypatch.setattr("kiro_crew.dashboard.chat_slack._slack_link_lock", _lock)
    monkeypatch.setattr("kiro_crew.slack.code_channels.binding.load_store_for_links", AsyncMock())
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: "dashboard:helper"
    )
    await _say("!trust", FRIEND, live)
    session_trust.add_trusted_sender("slack:9.9", FRIEND, "another thread's binding")
    assert await bot_chat.join_bot_chat(state, "helper", "C1", TS) == (slot, "")
    state.link_slack.assert_called_once_with("member:helper", TS, "C1")
    assert session_trust._trusted_senders == {"slack:9.9": {FRIEND: "another thread's binding"}}
    assert _rows()[-1][2] == f"session={KEY} senders={FRIEND} mode=normal reason=joined_bot_chat"


@pytest.mark.asyncio
async def test_a_person_taken_off_the_list_and_put_back_starts_without_the_grant(monkeypatch, live):
    await _say("!trust", FRIEND, live)
    thread_trust.drop_unlisted_senders(_cfg(ids=(OTHER,)))  # the applied config without them
    thread_trust.drop_unlisted_senders(_cfg())  # ... and the one that lists them again
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_OFF_TEXT
    assert await _asks(monkeypatch, FRIEND, live) is True


@pytest.mark.asyncio
async def test_a_grant_is_memory_only_so_a_gateway_restart_ends_it(monkeypatch, live):
    home = Path(os.environ["KIROCREW_HOME"])
    before = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
    await _say("!trust", FRIEND, live)
    # Nothing durable was written: no file in the data home, no session policy.
    assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*")) == before
    live.set_approval_policy.assert_not_called()
    # A new process starts with the module's empty map and a new session registry.
    session_trust._trusted_senders.clear()
    restarted = Sessions()
    assert await _say("!trust status", FRIEND, restarted) == thread_trust.OWN_STATUS_OFF_TEXT
    assert await _asks(monkeypatch, FRIEND, restarted) is True


# ── The card's Trust button, and a DM ──


@pytest.mark.asyncio
async def test_status_after_the_trust_button_agrees_with_their_next_tool_call(monkeypatch, live):
    from kiro_crew.slack.handler import _pending_approvals, _PendingApproval, handle_interaction

    class _Provider:
        approved: list = []

        async def approve_tool(self, request_id):
            self.approved.append(request_id)
            return True

    _pending_approvals["D1:card1"] = _PendingApproval(_Provider(), "req-1", KEY, requester=FRIEND)
    try:
        assert (
            await handle_interaction(
                "D1", "card1", "trust_tool", user_id=FRIEND, thread_ts=TS, sessions=live
            )
            == "trust_own_turns"
        )
    finally:
        _pending_approvals.pop("D1:card1", None)
    # Whatever the button grants, the status never says "on" while their calls ask.
    asks = await _asks(monkeypatch, FRIEND, live)
    expected = thread_trust.OWN_STATUS_OFF_TEXT if asks else thread_trust.OWN_STATUS_ON_TEXT
    assert await _say("!trust status", FRIEND, live, channel="D1") == expected


@pytest.mark.asyncio
async def test_status_and_off_cover_a_grant_made_through_the_sender_functions(monkeypatch, live):
    session_trust.add_trusted_sender(KEY, FRIEND, thread_trust.session_binding(live, KEY))
    assert await _say("!trust status", FRIEND, live) == thread_trust.OWN_STATUS_ON_TEXT
    assert await _asks(monkeypatch, FRIEND, live) is False
    assert await _say("!trust off", FRIEND, live) == thread_trust.OWN_OFF_TEXT
    assert await _asks(monkeypatch, FRIEND, live) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["slack:1.2", "slack:D1"], ids=["threaded DM", "flat DM"])
async def test_trust_in_a_dm_covers_the_session_the_dm_runs_on(live, key):
    assert await _say("!trust", FRIEND, live, channel="D1", key=key) == thread_trust.OWN_ON_TEXT
    assert session_trust._trusted_senders == {
        key: {FRIEND: thread_trust.session_binding(live, key)}
    }
    assert thread_trust.sender_trusted(live, key, FRIEND, _own_call())
    assert (
        await _say("!trust off", FRIEND, live, channel="D1", key=key) == thread_trust.OWN_OFF_TEXT
    )


def test_a_shared_users_replies_are_true_in_a_flat_dm_and_about_helper_agents():
    own = [
        value
        for name, value in vars(thread_trust).items()
        if name.startswith("OWN_") and isinstance(value, str)
    ]
    assert own
    # A flat DM is one session for the whole DM, so none of them says "this thread".
    assert not [text for text in own if "thread" in text.lower()]
    assert "Helper agents still ask." in thread_trust.OWN_ON_TEXT
    assert "owner" not in thread_trust.OWN_ON_TEXT
