"""Per-sender Trust: one shared user's own turns on one Slack session stop asking.

The grant lives beside the session one in ``messaging.session_trust``, bound to the
session as it ran when the grant was made (``thread_trust.session_binding``); the
Slack turn drivers read it through ``thread_trust.sender_trusted`` for a shared
user's turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from session_helpers import _mock_provider_factory, cfg_fixture  # noqa: F401 - cfg, by name

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.messaging import session_trust
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.session import SessionManager
from kiro_crew.slack import handler, shared_access, thread_trust

OWNER = "UOWNER0001"
FRIEND = "UFRIEND001"
OTHER = "UOTHER0001"
KEY = "slack:1.2"
AGENT = "kirocrew"


class _RunningProvider:
    """Stands for the provider object a running session keeps for its whole life."""


class Sessions:
    """The registry reads a binding is made of, for one thread's session."""

    def __init__(self, agent: str = AGENT, live: bool = True) -> None:
        self.agent, self.live = agent, live
        self.provider = _RunningProvider()
        self.set_approval_policy = MagicMock()

    def has_session(self, key: str) -> bool:
        return self.live

    def get_provider(self, key: str) -> _RunningProvider | None:
        return self.provider if self.live else None

    def get_agent(self, key: str) -> str:
        return self.agent

    def get_session_for_thread(self, thread_ts: str) -> None:
        return None

    def replace(self, agent: str | None = None) -> None:
        """What every removal and re-creation of the session does: a new provider."""
        self.provider = _RunningProvider()
        self.agent = self.agent if agent is None else agent


def _call(**marks) -> LLMEvent:
    """A permission request of the turn's own, from a backend that marks child calls."""
    marks = {"child_calls_marked": True, **marks}
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="Run: ls", request_id="r1", **marks)


#: What marks a helper agent's call, and a backend that cannot tell (unmarked).
_NOT_THE_TURNS_OWN = [
    {"sub_session_id": "child-1"},
    {"native_child_call": True},
    {"sub_session_id": "child-1", "native_child_call": True},
    {"child_calls_marked": False},
]
_NOT_OWN_IDS = ["child session", "native sub-agent", "both", "unmarked backend"]


def _cfg(ids: tuple[str, ...] = (FRIEND, OTHER), enabled: bool = True) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.slack = _build_slack_config(
        {"shared_access": {"enabled": enabled, "user_ids": list(ids), "verify_with_slack": False}}
    )
    return cfg


@pytest.fixture(autouse=True)
def _shared_users(_floor_monkeypatch: pytest.MonkeyPatch):
    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr(session_trust, "_trusted_sessions", {})
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: True)
    monkeypatch.setattr(thread_trust, "_audit", MagicMock())
    monkeypatch.setattr(handler, "_owner_id", OWNER)
    monkeypatch.setattr(handler, "_orch_cfg", _cfg())
    shared_access.install(None)
    for user in (FRIEND, OTHER):
        asyncio.run(shared_access.prime_shared_user(user))
        assert handler.is_shared_user(user)
    yield
    shared_access.reset()


def _grant(sessions: Sessions, sender: str = FRIEND, key: str = KEY):
    binding = thread_trust.session_binding(sessions, key)
    session_trust.add_trusted_sender(key, sender, binding)
    return binding


# ── The grant ──


def test_a_grant_covers_that_sender_on_that_session_only():
    session_trust.add_trusted_sender(KEY, FRIEND, "1:kirocrew")
    assert session_trust.is_sender_trusted(KEY, FRIEND, "1:kirocrew")
    assert not session_trust.is_sender_trusted(KEY, OTHER, "1:kirocrew")
    assert not session_trust.is_sender_trusted(KEY, OWNER, "1:kirocrew")
    assert not session_trust.is_sender_trusted("slack:other", FRIEND, "1:kirocrew")
    # Not the session grant: the owner's turns and sub-agents read that one.
    assert not session_trust.is_session_trusted(KEY)
    assert not session_trust._trusted_sessions


@pytest.mark.parametrize(
    "key, sender, binding", [("", FRIEND, "1:a"), (KEY, "", "1:a"), (KEY, FRIEND, None)]
)
def test_a_grant_needs_a_session_a_sender_and_a_binding(key, sender, binding):
    session_trust.add_trusted_sender(key, sender, binding)
    assert not session_trust._trusted_senders
    assert not session_trust.is_sender_trusted(key, sender, binding)


def test_a_grant_read_under_another_binding_ends_for_good():
    session_trust.add_trusted_sender(KEY, FRIEND, "1:kirocrew")
    assert not session_trust.is_sender_trusted(KEY, FRIEND, "3:kirocrew")
    # Dropped, not skipped: reading the first binding again does not bring it back.
    assert not session_trust.is_sender_trusted(KEY, FRIEND, "1:kirocrew")
    assert not session_trust._trusted_senders


def test_remove_drops_one_sender_and_says_whether_there_was_a_grant():
    session_trust.add_trusted_sender(KEY, FRIEND, "1:a")
    session_trust.add_trusted_sender(KEY, OTHER, "1:a")
    assert session_trust.remove_trusted_sender(KEY, FRIEND) is True
    assert session_trust.remove_trusted_sender(KEY, FRIEND) is False
    assert session_trust.is_sender_trusted(KEY, OTHER, "1:a")
    assert session_trust.remove_trusted_sender(KEY, OTHER) is True
    assert not session_trust._trusted_senders  # no empty row is left behind


def test_drop_removes_every_sender_of_one_session():
    session_trust.add_trusted_sender(KEY, FRIEND, "1:a")
    session_trust.add_trusted_sender(KEY, OTHER, "1:a")
    session_trust.add_trusted_sender("slack:other", FRIEND, "1:a")
    assert session_trust.drop_trusted_senders(KEY) == 2
    assert session_trust.drop_trusted_senders(KEY) == 0
    assert session_trust.is_sender_trusted("slack:other", FRIEND, "1:a")


def test_keep_drops_the_senders_it_is_false_for_on_every_session():
    session_trust.add_trusted_sender(KEY, FRIEND, "1:a")
    session_trust.add_trusted_sender(KEY, OTHER, "1:a")
    session_trust.add_trusted_sender("slack:other", FRIEND, "1:a")
    assert session_trust.keep_trusted_senders(lambda sender: sender == OTHER) == 2
    assert session_trust._trusted_senders == {KEY: {OTHER: "1:a"}}


def test_a_policy_deny_refuses_the_grant_and_masks_one_already_made(monkeypatch):
    session_trust.add_trusted_sender(KEY, FRIEND, "1:a")
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    assert not session_trust.is_sender_trusted(KEY, FRIEND, "1:a")
    with pytest.raises(PermissionError):
        session_trust.add_trusted_sender(KEY, OTHER, "1:a")
    assert OTHER not in session_trust._trusted_senders[KEY]


def test_every_clear_of_session_trust_drops_the_sender_grants_too():
    manager = MagicMock()
    session_trust.add_trusted_session(KEY, manager)
    session_trust.add_trusted_sender(KEY, FRIEND, "1:a")
    session_trust.add_trusted_sender("slack:other", OTHER, "1:a")
    session_trust.clear_trusted_sessions(keep_policy={KEY})
    assert not session_trust._trusted_senders
    session_trust.add_trusted_sender(KEY, FRIEND, "1:a")
    session_trust._on_trust_denied(frozenset({"trust"}))
    assert not session_trust._trusted_senders


@pytest.mark.parametrize("run", [1, 2])
def test_the_suite_floor_starts_every_test_with_no_sender_grant(run):
    # conftest_slack._clean_sender_trust_grants: each run leaves a grant behind,
    # so whichever runs second finds it only if the floor did not clear it.
    assert not session_trust._trusted_senders
    session_trust.add_trusted_sender(KEY, FRIEND, "1:a")


# ── The binding: the session as the registry holds it ──


def test_no_running_session_has_no_binding():
    assert thread_trust.session_binding(Sessions(live=False), KEY) is None
    assert thread_trust.session_binding(None, KEY) is None
    assert thread_trust.session_binding(MagicMock(), KEY) is None  # no real answers

    class _Broken(Sessions):
        def get_provider(self, key: str):
            raise RuntimeError("registry unreadable")

    assert thread_trust.session_binding(_Broken(), KEY) is None


def test_a_binding_equals_only_the_same_running_session_on_the_same_agent():
    sessions = Sessions()
    first = thread_trust.session_binding(sessions, KEY)
    assert first == thread_trust.session_binding(sessions, KEY)
    sessions.agent = "reviewer"
    assert first != thread_trust.session_binding(sessions, KEY)
    sessions.agent = AGENT
    assert first == thread_trust.session_binding(sessions, KEY)
    sessions.replace()
    assert first != thread_trust.session_binding(sessions, KEY)
    # A session that is gone equals nothing, itself included: nothing can match it.
    del sessions
    import gc

    gc.collect()
    assert first != first
    assert first != "1:kirocrew" and first is not None


@pytest.mark.asyncio
async def test_the_real_registry_keeps_a_binding_for_a_sessions_life_and_no_longer(cfg):
    # The property the grant stands on, on the real session manager: every turn on
    # one session reads the same binding; remove the session and start one on the
    # same agent again, and it reads a binding equal to no earlier one.
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    assert thread_trust.session_binding(mgr, KEY) is None
    await mgr.get_or_create(KEY, agent="helper")
    mgr.release(KEY)
    first = thread_trust.session_binding(mgr, KEY)
    assert first is not None and first.agent == "helper"
    await mgr.get_or_create(KEY, agent="helper")
    mgr.release(KEY)
    assert thread_trust.session_binding(mgr, KEY) == first
    seen = [first]
    for agent in ("reviewer", "helper", "helper"):
        await mgr.remove(KEY)
        assert thread_trust.session_binding(mgr, KEY) is None
        await mgr.get_or_create(KEY, agent=agent)
        mgr.release(KEY)
        binding = thread_trust.session_binding(mgr, KEY)
        assert binding is not None and binding.agent == agent
        assert all(binding != earlier for earlier in seen)
        seen.append(binding)


@pytest.mark.asyncio
async def test_a_grant_on_the_real_registry_ends_when_the_agent_goes_away_and_comes_back(cfg):
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.get_or_create(KEY, agent="helper")
    mgr.release(KEY)
    session_trust.add_trusted_sender(KEY, FRIEND, thread_trust.session_binding(mgr, KEY))
    # Turn after turn on the same session, the grant holds.
    for _ in range(3):
        await mgr.get_or_create(KEY, agent="helper")
        mgr.release(KEY)
        assert thread_trust.sender_trusted(mgr, KEY, FRIEND, _call())
    # What `!ta`, `!bot`, `/agent` and a dashboard switch do to a running session,
    # there and back, with no turn of the grantee's in between.
    for agent in ("reviewer", "helper"):
        await mgr.remove(KEY)
        await mgr.get_or_create(KEY, agent=agent)
        mgr.release(KEY)
    assert mgr.get_agent(KEY) == "helper"
    assert not thread_trust.sender_trusted(mgr, KEY, FRIEND, _call())
    assert not session_trust._trusted_senders


# ── The Slack read ──


def test_the_slack_read_holds_for_a_confirmed_shared_user_on_the_same_session():
    sessions = Sessions()
    _grant(sessions)
    assert thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())
    assert not thread_trust.sender_trusted(sessions, KEY, OTHER, _call())


@pytest.mark.parametrize("marks", _NOT_THE_TURNS_OWN, ids=_NOT_OWN_IDS)
def test_the_slack_read_never_answers_a_helper_agents_call(marks):
    sessions = Sessions()
    _grant(sessions)
    assert not thread_trust.sender_trusted(sessions, KEY, FRIEND, _call(**marks))
    # The grant stays: the turn's own next call still runs without asking.
    assert thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())
    thread_trust._audit.assert_not_called()


@pytest.mark.parametrize("change", ["same agent", "another agent", "gone", "agent in place"])
def test_a_replaced_session_asks_and_ends_the_grant(change):
    sessions = Sessions()
    _grant(sessions)
    if change == "same agent":
        sessions.replace()
    elif change == "another agent":
        sessions.replace("reviewer")
    elif change == "gone":
        sessions.live = False
    else:
        sessions.agent = "reviewer"  # the same allocation, switched where it runs
    assert not thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())
    assert not session_trust._trusted_senders
    thread_trust._audit.assert_called_once_with(
        FRIEND, "allowed", f"session={KEY} sender={FRIEND} mode=normal reason=session_replaced"
    )


@pytest.mark.parametrize("cfg", [_cfg(ids=(OTHER,)), _cfg(enabled=False)], ids=["removed", "off"])
def test_leaving_shared_access_ends_the_grant_at_the_next_read(monkeypatch, cfg):
    sessions = Sessions()
    _grant(sessions)
    monkeypatch.setattr(handler, "_orch_cfg", cfg)
    assert not thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())
    thread_trust._audit.assert_called_once_with(
        FRIEND, "allowed", f"session={KEY} sender={FRIEND} mode=normal reason=left_shared_access"
    )
    # Dropped, not masked: being listed again does not bring it back.
    monkeypatch.setattr(handler, "_orch_cfg", _cfg())
    assert not thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())


@pytest.mark.parametrize("cfg", [_cfg(ids=(OTHER,)), _cfg(enabled=False)], ids=["removed", "off"])
def test_an_applied_config_without_the_person_drops_their_grants_with_no_turn(cfg):
    sessions = Sessions()
    _grant(sessions)
    _grant(sessions, key="slack:other")
    binding = _grant(sessions, OTHER)
    kept = 1 if cfg.slack.shared_access.enabled else 0
    assert thread_trust.drop_unlisted_senders(cfg) == 3 - kept
    assert session_trust._trusted_senders == ({KEY: {OTHER: binding}} if kept else {})
    # Listed again: the old grant is gone, on every session.
    assert thread_trust.drop_unlisted_senders(_cfg()) == 0
    assert not thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())
    assert not thread_trust.sender_trusted(sessions, "slack:other", FRIEND, _call())
    row = thread_trust._audit.call_args.args
    gone = FRIEND if kept else f"{FRIEND},{OTHER}"
    assert row[:2] == ("config", "allowed") and f"senders={gone} " in row[2]


def test_the_gateway_drops_unlisted_grants_with_each_slack_config_it_applies():
    from unittest.mock import patch

    from _hot_reload_helpers import change

    from kiro_crew.slack.gateway import GatewayOrchestrator

    boot = KiroCrewConfig()
    with patch.object(boot, "load_credentials", return_value={"KIROCREW_OWNER_ID": OWNER}):
        orch = GatewayOrchestrator(boot, no_dashboard=True, no_crons=True, no_open=True)
    sessions = Sessions()
    binding = _grant(sessions)
    _grant(sessions, OTHER)
    # A change that keeps both people listed keeps both grants.
    asyncio.run(orch._on_slack_config_change(change(_cfg(), "slack.reactions")))
    assert session_trust._trusted_senders == {KEY: {FRIEND: binding, OTHER: binding}}
    # The owner takes one person off the list: no turn of theirs is needed.
    asyncio.run(orch._on_slack_config_change(change(_cfg(ids=(OTHER,)), "slack.shared_access")))
    assert session_trust._trusted_senders == {KEY: {OTHER: binding}}
    # Put back on the list, they start without the old grant.
    asyncio.run(orch._on_slack_config_change(change(_cfg(), "slack.shared_access")))
    assert not thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())
    assert thread_trust.sender_trusted(sessions, KEY, OTHER, _call())


def test_a_grant_cleared_between_the_check_and_the_read_asks_and_does_not_raise(monkeypatch):
    # A policy deny clears every grant from another thread. Here it lands after the
    # read that says the sender holds one and before the read of its binding.
    sessions = Sessions()
    _grant(sessions)

    def cleared_meanwhile(*_a, **_k) -> bool:
        session_trust.clear_trusted_sessions()
        return True

    monkeypatch.setattr(shared_access, "still_confirmed", cleared_meanwhile)
    assert thread_trust.sender_trusted(sessions, KEY, FRIEND, _call()) is False
    assert session_trust.sender_binding(KEY, FRIEND) is None
    assert session_trust.remove_trusted_sender(KEY, FRIEND) is False
    assert thread_trust._senders() == "" and thread_trust._senders(KEY) == ""


def test_the_owner_never_reads_as_a_trusted_sender():
    sessions = Sessions()
    _grant(sessions, OWNER)
    assert not thread_trust.sender_trusted(sessions, KEY, OWNER, _call())


# ── The turn drivers ──


async def _native_call(
    monkeypatch, sender: str, sessions: Sessions, **marks
) -> tuple[MagicMock, AsyncMock]:
    """One permission request of *sender*'s native turn on ``KEY``."""
    import test_slack_ladder_held_bot_limit as ladder

    from kiro_crew import mate_held_rules

    monkeypatch.setattr(mate_held_rules, "held_for", lambda *a, **k: [])
    monkeypatch.setattr(handler, "sel", MagicMock())
    prompt = AsyncMock(return_value=False)
    monkeypatch.setattr(handler, "_request_approval", prompt)
    client = MagicMock()
    client.approve_tool = AsyncMock(return_value=True)
    client.reject_tool = AsyncMock()
    turn = ladder._turn(handler.APPROVAL_INTERACTIVE, None)
    turn.user_id = sender
    turn.sessions = sessions
    assert turn.session_key == KEY
    monkeypatch.setattr(handler, "is_yolo_mode", lambda: False)
    monkeypatch.setattr(handler, "_should_auto_approve_spawn", lambda cb, ev: False)
    await handler._answer_permission_request(turn, client, _call(**marks))
    return client, prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender, runs",
    [(FRIEND, True), (OTHER, False), (OWNER, False)],
    ids=["the grantee", "another shared user", "the owner"],
)
async def test_native_ladder_runs_only_the_grantees_own_turn(monkeypatch, sender, runs):
    sessions = Sessions()
    _grant(sessions)
    client, prompt = await _native_call(monkeypatch, sender, sessions)
    if runs:
        client.approve_tool.assert_awaited_once_with("r1")
        prompt.assert_not_awaited()
    else:
        client.approve_tool.assert_not_awaited()
        prompt.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("marks", _NOT_THE_TURNS_OWN, ids=_NOT_OWN_IDS)
async def test_native_ladder_asks_for_a_helper_agents_call_in_the_grantees_turn(monkeypatch, marks):
    sessions = Sessions()
    binding = _grant(sessions)
    client, prompt = await _native_call(monkeypatch, FRIEND, sessions, **marks)
    client.approve_tool.assert_not_awaited()
    prompt.assert_awaited_once()
    assert session_trust._trusted_senders == {KEY: {FRIEND: binding}}


@pytest.mark.asyncio
@pytest.mark.parametrize("marks", _NOT_THE_TURNS_OWN, ids=_NOT_OWN_IDS)
async def test_native_ladder_keeps_the_owners_session_grant_for_every_call(monkeypatch, marks):
    session_trust.add_trusted_session(KEY)
    client, prompt = await _native_call(monkeypatch, OWNER, Sessions(), **marks)
    client.approve_tool.assert_awaited_once_with("r1")
    prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_ladder_asks_a_shared_user_under_the_owners_session_grant(monkeypatch):
    session_trust.add_trusted_session(KEY)
    client, prompt = await _native_call(monkeypatch, FRIEND, Sessions())
    client.approve_tool.assert_not_awaited()
    prompt.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("ended_by", ["removed", "session"])
async def test_native_ladder_asks_a_grantee_whose_grant_ended(monkeypatch, ended_by):
    sessions = Sessions()
    _grant(sessions)
    if ended_by == "removed":
        monkeypatch.setattr(handler, "_orch_cfg", _cfg(ids=(OTHER,)))
    else:
        sessions.replace()
    client, prompt = await _native_call(monkeypatch, FRIEND, sessions)
    client.approve_tool.assert_not_awaited()
    prompt.assert_awaited_once()


def _transport_predicate(monkeypatch, sender: str, sessions: Sessions):
    """What the driver of *sender*'s transport turn auto-approves, and its session key.

    The predicate takes the permission event and asks the two rungs the driver asks,
    in its order: ``auto_approve_session``, then ``auto_approve_sender``.
    """
    from slack_transport_dispatch_helpers import (
        _MSG_TS,
        RecordingSlackClient,
        ScriptedProvider,
        _CapturingSessions,
    )

    from kiro_crew.slack import transport_dispatch

    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: AGENT)
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
    captured: list = []

    class _Recorder:
        def __init__(self, *_a, **kw) -> None:
            session, sender_rung = kw["auto_approve_session"], kw["auto_approve_sender"]
            captured.append(
                (
                    lambda event=None: session() or sender_rung(event or _call()),
                    kw["audit_session_key"],
                )
            )

        async def run(self, message: str) -> str:
            return "ok"

    # The registry the turn runs on answers the binding reads for this session.
    manager = _CapturingSessions(ScriptedProvider([]))
    manager.has_session = sessions.has_session
    manager.get_provider = sessions.get_provider
    manager.get_agent = sessions.get_agent
    monkeypatch.setattr(transport_dispatch, "TurnDriver", _Recorder)
    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=RecordingSlackClient(),
            sessions=manager,
            channel="C1",
            text="hello",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id=sender,
            context_builder=None,
            conversation_log=None,
        )
    )
    assert captured, "the transport path never built a driver"
    return captured[0]


@pytest.mark.parametrize(
    "sender, runs",
    [(FRIEND, True), (OTHER, False), (OWNER, False)],
    ids=["the grantee", "another shared user", "the owner"],
)
def test_transport_driver_runs_only_the_grantees_own_turn(monkeypatch, sender, runs):
    sessions = Sessions()
    predicate, key = _transport_predicate(monkeypatch, sender, sessions)
    assert predicate() is False
    _grant(sessions, key=key)
    assert predicate() is runs
    # The owner's session grant covers the owner's turn and no shared user's.
    session_trust.drop_trusted_senders(key)
    session_trust.add_trusted_session(key)
    assert predicate() is (sender == OWNER)


@pytest.mark.parametrize("marks", _NOT_THE_TURNS_OWN, ids=_NOT_OWN_IDS)
def test_transport_driver_asks_for_a_helper_agents_call_in_the_grantees_turn(monkeypatch, marks):
    sessions = Sessions()
    predicate, key = _transport_predicate(monkeypatch, FRIEND, sessions)
    _grant(sessions, key=key)
    assert predicate(_call(**marks)) is False
    assert predicate(_call()) is True
    # The owner's session grant answers every call of the owner's turn, as before.
    owner, key = _transport_predicate(monkeypatch, OWNER, sessions)
    session_trust.add_trusted_session(key)
    assert owner(_call(**marks)) is True


def test_transport_driver_asks_once_the_session_is_replaced(monkeypatch):
    sessions = Sessions()
    predicate, key = _transport_predicate(monkeypatch, FRIEND, sessions)
    _grant(sessions, key=key)
    assert predicate() is True
    sessions.replace()
    assert predicate() is False
    assert not session_trust._trusted_senders


class _QuietRenderer(Renderer):
    """A renderer that shows nothing: these tests read the provider and the audit."""

    async def on_text_chunk(self, text):
        pass

    async def on_done(self, stop_reason=""):
        pass

    async def on_thinking(self, text):
        pass

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        pass

    async def on_tool_result(self, tool_call_id, tool_status):
        pass

    async def on_compaction(self, pct):
        pass

    async def on_prompt_choice(
        self, options, request_id, tool_title="", tool_purpose="", tool_input=""
    ):
        pass


def _switch(agent: str = "reviewer") -> LLMEvent:
    """The harness's report that it switched the running session's agent in place."""
    from kiro_crew.providers.base import EVENT_AGENT_SWITCHED

    assert thread_trust._AGENT_SWITCHED_EVENT == EVENT_AGENT_SWITCHED
    return LLMEvent(kind=EVENT_AGENT_SWITCHED, text=agent)


def test_an_agent_switched_in_place_ends_the_grants_on_that_session():
    sessions = Sessions()
    _grant(sessions)
    _grant(sessions, OTHER)
    other = _grant(sessions, key="slack:other")
    thread_trust.end_grants_on_turn_event(KEY, _call())  # any other event: nothing
    assert len(session_trust._trusted_senders[KEY]) == 2
    thread_trust.end_grants_on_turn_event(KEY, _switch())
    # The provider and the registry's agent are unchanged, and the grant is gone
    # for good: switching back does not bring it back.
    assert session_trust._trusted_senders == {"slack:other": {FRIEND: other}}
    thread_trust.end_grants_on_turn_event(KEY, _switch(AGENT))
    assert not thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())
    thread_trust._audit.assert_called_once_with(
        "config",
        "allowed",
        f"session={KEY} senders={FRIEND},{OTHER} mode=normal reason=agent_switched",
    )


@pytest.mark.asyncio
async def test_the_native_turn_ends_the_grant_when_the_harness_switches_agent(monkeypatch):
    # The real native turn loop: a frame that says the harness switched the agent
    # in place, then a tool call of the grantee's own turn on the same provider.
    # The call reaches the prompt.
    import inspect

    from kiro_crew.slack.handler_runtime import turn as turn_module

    source = inspect.getsource(turn_module)
    loop = source.index("async for event in continue_timed_out_calls(")
    body = source[loop : loop + 700]
    # Every event of the turn passes the hook before the kind dispatch below it.
    assert body.index("end_grants_on_turn_event(session_key, event)") < body.index(
        "if event.kind == EVENT_TEXT_CHUNK"
    )
    sessions = Sessions()
    _grant(sessions)
    thread_trust.end_grants_on_turn_event(KEY, _switch())
    client, prompt = await _native_call(monkeypatch, FRIEND, sessions)
    client.approve_tool.assert_not_awaited()
    prompt.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_turn_driver_ends_the_grant_when_the_harness_switches_agent(monkeypatch):
    from kiro_crew.messaging import driver as driver_module
    from kiro_crew.messaging.driver import TurnDriver
    from kiro_crew.messaging.transport import TransportCapabilities
    from kiro_crew.providers.base import EVENT_COMPLETE

    before, after = _call(), _call()
    after.request_id = "r2"

    class _Provider:
        approved: list = []
        rejected: list = []

        async def stream(self, message):
            for event in (before, _switch(), _switch(AGENT), after):
                yield event
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        async def approve_tool(self, request_id, *, always=False):
            self.approved.append(request_id)
            return True

        async def reject_tool(self, request_id):
            self.rejected.append(request_id)

    audit = MagicMock()
    monkeypatch.setattr(driver_module, "sel", lambda: audit)
    sessions = Sessions()
    _grant(sessions)
    provider = _Provider()
    await TurnDriver(
        provider,
        _QuietRenderer(TransportCapabilities()),
        approval_mode="interactive",
        auto_approve_session=lambda: False,
        auto_approve_sender=lambda event: thread_trust.sender_trusted(sessions, KEY, FRIEND, event),
        audit_session_key=KEY,
    ).run("hi")
    # Trusted before the switch; after it, there and back, the call asks.
    assert provider.approved == ["r1"]
    assert provider.rejected == ["r2"]
    assert not session_trust._trusted_senders
    rows = [c.kwargs for c in audit.log_api_access.call_args_list]
    assert [r["resources"] for r in rows if r.get("operation") == "sender_trust"] == [
        f"session={KEY} senders={FRIEND} mode=normal reason=agent_switched"
    ]


@pytest.mark.asyncio
async def test_the_turn_driver_asks_the_sender_rung_with_the_event_and_audits_it(monkeypatch):
    from kiro_crew.messaging import driver as driver_module
    from kiro_crew.messaging.driver import TurnDriver
    from kiro_crew.messaging.transport import TransportCapabilities
    from kiro_crew.providers.base import EVENT_COMPLETE

    own, child = _call(), _call(sub_session_id="child-1")
    child.request_id = "r2"

    class _Provider:
        approved: list = []
        rejected: list = []

        async def stream(self, message):
            for event in (own, child, LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")):
                yield event

        async def approve_tool(self, request_id, *, always=False):
            self.approved.append(request_id)
            return True

        async def reject_tool(self, request_id):
            self.rejected.append(request_id)

    audit = MagicMock()
    monkeypatch.setattr(driver_module, "sel", lambda: audit)
    sessions = Sessions()
    _grant(sessions)
    provider = _Provider()
    await TurnDriver(
        provider,
        _QuietRenderer(TransportCapabilities()),
        approval_mode="interactive",
        auto_approve_session=lambda: False,
        auto_approve_sender=lambda event: thread_trust.sender_trusted(sessions, KEY, FRIEND, event),
    ).run("hi")
    # The turn's own call ran on the grant; the helper agent's call did not.
    assert provider.approved == ["r1"]
    assert provider.rejected == ["r2"]
    reasons = [c.kwargs.get("resources", "") for c in audit.log_api_access.call_args_list]
    assert any("request_id=r1" in r and "reason=sender_trust" in r for r in reasons)


# ── The conversation changes and the session stays: the grant holds, as the owner's does ──
#
# The rule: a shared user's grant never lives longer than the owner's own ``!trust``
# would in the same thread. The owner's holds across a compaction, a cleared
# conversation and a fresh conversation on the live process, and none of them
# changes who sends or which agent runs, so the sender's holds there too.


def _owner_trusts(sessions) -> None:
    """The owner's own ``!trust`` on ``KEY``: the session grant, no policy write."""
    session_trust.add_trusted_session(KEY, strict=True)
    assert session_trust.is_session_trusted(KEY)


def _both_hold(sessions, binding) -> None:
    assert session_trust.is_session_trusted(KEY)
    assert thread_trust.session_binding(sessions, KEY) == binding
    assert thread_trust.sender_trusted(sessions, KEY, FRIEND, _call())


@pytest.mark.parametrize("kind", ["compaction_status", "clear_status"])
def test_a_compaction_or_a_clear_the_turn_sees_leaves_the_grant(kind):
    from kiro_crew.providers.base import EVENT_CLEAR_STATUS, EVENT_COMPACTION_STATUS

    assert kind in (EVENT_COMPACTION_STATUS, EVENT_CLEAR_STATUS)
    sessions = Sessions()
    binding = _grant(sessions)
    _owner_trusts(sessions)
    for text in ("started", "completed", "failed"):
        thread_trust.end_grants_on_turn_event(KEY, LLMEvent(kind=kind, text=text))
    _both_hold(sessions, binding)
    thread_trust._audit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["compaction_status", "clear_status"])
async def test_the_turn_driver_keeps_the_grant_across_a_compaction_or_a_clear(monkeypatch, kind):
    from kiro_crew.messaging import driver as driver_module
    from kiro_crew.messaging.driver import TurnDriver
    from kiro_crew.messaging.transport import TransportCapabilities
    from kiro_crew.providers.base import EVENT_COMPLETE

    before, after = _call(), _call()
    after.request_id = "r2"

    class _Provider:
        approved: list = []
        rejected: list = []

        async def stream(self, message):
            for event in (before, LLMEvent(kind=kind, text="completed"), after):
                yield event
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        async def approve_tool(self, request_id, *, always=False):
            self.approved.append(request_id)
            return True

        async def reject_tool(self, request_id):
            self.rejected.append(request_id)

    audit = MagicMock()
    monkeypatch.setattr(driver_module, "sel", lambda: audit)
    sessions = Sessions()
    _grant(sessions)
    provider = _Provider()
    await TurnDriver(
        provider,
        _QuietRenderer(TransportCapabilities()),
        approval_mode="interactive",
        auto_approve_session=lambda: False,
        auto_approve_sender=lambda event: thread_trust.sender_trusted(sessions, KEY, FRIEND, event),
        audit_session_key=KEY,
    ).run("hi")
    # Trusted before it and after it: the same sender, the same session, the same agent.
    assert provider.approved == ["r1", "r2"]
    assert provider.rejected == []
    assert FRIEND in session_trust._trusted_senders[KEY]
    rows = [c.kwargs for c in audit.log_api_access.call_args_list]
    assert not [r for r in rows if r.get("operation") == "sender_trust"]


@pytest.mark.asyncio
async def test_a_compaction_in_place_keeps_the_grant_as_it_keeps_the_owners(cfg, monkeypatch):
    # The real session manager compacts KEY in place: the same provider object and
    # the same agent afterwards. The owner's Trust is untouched by it, and so is
    # the sender's.
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    provider, _, _ = await mgr.get_or_create(KEY, agent=AGENT)
    mgr.release(KEY)
    binding = _grant(mgr)
    _owner_trusts(mgr)

    provider.context_usage_pct = lambda: 95.0
    provider.wait_for_compaction = AsyncMock(return_value={"type": "completed", "summary": ""})
    assert await mgr.compact_if_needed(KEY) == "ok"
    # What every surface calls for a compaction its own turn saw.
    mgr.mark_needs_reinjection(KEY)

    assert mgr.get_provider(KEY) is provider
    _both_hold(mgr, binding)
    client, prompt = await _native_call(monkeypatch, FRIEND, mgr)
    client.approve_tool.assert_awaited_once()
    prompt.assert_not_awaited()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_fresh_conversation_on_the_live_process_keeps_both_grants(cfg):
    # Edit & Resend swaps the conversation and keeps the process and its provider.
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    provider, _, _ = await mgr.get_or_create(KEY, agent=AGENT)
    mgr.release(KEY)
    provider.swaps_conversation_in_place = True
    provider.session_id = "old-sid"
    provider.new_conversation = AsyncMock()
    mgr._session_map.set(KEY, "old-sid")
    binding = _grant(mgr)
    _owner_trusts(mgr)

    assert await mgr.swap_conversation(KEY) == "swapped"

    assert mgr.get_provider(KEY) is provider
    _both_hold(mgr, binding)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_clear_confirmed_in_a_dashboard_turn_keeps_both_grants():
    # The owner clears the thread's session from its dashboard tab: the provider
    # is the same one afterwards.
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_turn.session_events import _on_clear_status
    from kiro_crew.providers.base import EVENT_CLEAR_STATUS

    sessions = Sessions()
    binding = _grant(sessions)
    _owner_trusts(sessions)
    turn = SimpleNamespace(
        session_key=KEY,
        state=MagicMock(),
        client=MagicMock(),
        _this_turn_is_clear=True,
        _replay_accepted_this_turn=False,
        _replay_pending=False,
    )

    assert await _on_clear_status(turn, LLMEvent(kind=EVENT_CLEAR_STATUS)) is True

    _both_hold(sessions, binding)


# ── What still ends it: the session is replaced ──


@pytest.mark.asyncio
async def test_a_reset_on_the_real_registry_ends_the_grant(cfg):
    # A reset replaces the session: the next one has another provider, so the
    # binding the grant was made under equals nothing.
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.get_or_create(KEY, agent=AGENT)
    mgr.release(KEY)
    _grant(mgr)
    assert thread_trust.sender_trusted(mgr, KEY, FRIEND, _call())

    assert await mgr.reset(KEY) is True
    await mgr.get_or_create(KEY, agent=AGENT)
    mgr.release(KEY)

    assert not thread_trust.sender_trusted(mgr, KEY, FRIEND, _call())
    assert not session_trust._trusted_senders
    await mgr.close_all()
