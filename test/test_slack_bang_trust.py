"""``!trust``: the owner puts one Slack thread's session on Trust from the thread."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.messaging import session_trust
from kiro_crew.slack import handler, thread_trust

KEY = "slack:1795569220.916459"


@pytest.fixture(autouse=True)
def _clean_grants(_floor_monkeypatch: pytest.MonkeyPatch):
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
    mutate = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.apply_approval_mode", mutate)
    yield store
    mutate.assert_not_called()
    mutate.assert_not_awaited()


def _sessions(owner: str | None = None) -> MagicMock:
    sessions = MagicMock()
    sessions.get_session_for_thread.return_value = owner
    return sessions


async def _run(arg: str, *, state=None, channel="C1", key=KEY) -> str:
    return await thread_trust.handle(
        arg,
        state=state,
        session_key=key,
        channel=channel,
        reply_ts="1795569220.916459",
        user="UOWNER",
    )


@pytest.mark.asyncio
async def test_bare_trust_grants_the_thread_session():
    assert await _run("") == thread_trust.ON_TEXT
    assert session_trust.is_session_trusted(KEY)
    # No session manager is kept: nothing can write the policy sub-agents inherit.
    assert session_trust._trusted_sessions == {KEY: None}


@pytest.mark.asyncio
async def test_trust_on_is_the_same_as_bare_trust():
    assert await _run("on") == thread_trust.ON_TEXT
    assert session_trust.is_session_trusted(KEY)


@pytest.mark.asyncio
async def test_a_second_trust_says_already_on():
    await _run("")
    assert await _run("") == thread_trust.ALREADY_ON_TEXT


@pytest.mark.asyncio
async def test_trust_off_revokes_the_grant():
    await _run("")
    assert await _run("off") == thread_trust.OFF_TEXT
    assert not session_trust.is_session_trusted(KEY)


@pytest.mark.asyncio
async def test_trust_off_revokes_a_grant_masked_by_policy(monkeypatch: pytest.MonkeyPatch):
    assert await _run("") == thread_trust.ON_TEXT
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    assert await _run("off") == thread_trust.OFF_TEXT
    assert KEY not in session_trust._trusted_sessions


@pytest.mark.asyncio
async def test_trust_off_resets_the_policy_a_trust_button_grant_wrote():
    mgr = MagicMock()
    session_trust.add_trusted_session(KEY, mgr)
    mgr.set_approval_policy.reset_mock()
    assert await _run("off") == thread_trust.OFF_TEXT
    mgr.set_approval_policy.assert_called_once_with(KEY, "")


@pytest.mark.asyncio
async def test_trust_off_when_not_trusted_changes_nothing():
    assert await _run("off") == thread_trust.ALREADY_OFF_TEXT


@pytest.mark.asyncio
async def test_status_reports_without_changing():
    assert await _run("status") == thread_trust.STATUS_OFF_TEXT
    assert not session_trust.is_session_trusted(KEY)
    await _run("")
    assert await _run("status") == thread_trust.STATUS_ON_TEXT


@pytest.mark.asyncio
async def test_unknown_word_shows_usage_and_grants_nothing():
    assert await _run("forever") == thread_trust.USAGE_TEXT
    assert not session_trust.is_session_trusted(KEY)


@pytest.mark.asyncio
async def test_policy_deny_grants_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    assert await _run("") == thread_trust.POLICY_TEXT
    assert KEY not in session_trust._trusted_sessions


@pytest.mark.asyncio
async def test_approval_modes_deny_grants_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("kiro_crew.safety_override.approval_mode_permitted", lambda _m: False)
    assert await _run("") == thread_trust.POLICY_TEXT
    assert KEY not in session_trust._trusted_sessions


@pytest.mark.parametrize("which", ["get_binding", "get_thread_binding", "is_thread_quarantined"])
@pytest.mark.asyncio
async def test_code_channel_and_conductor_thread_point_at_their_own_command(_clean_grants, which):
    getattr(_clean_grants, which).return_value = object()
    assert await _run("") == thread_trust.CODE_CHANNEL_TEXT
    assert KEY not in session_trust._trusted_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["", "on", "off", "status"])
async def test_a_thread_with_a_live_linked_chat_is_pointed_at_the_picker(arg):
    # A linked chat's Trust has no per-sender rung, so a grant here would run a
    # shared user's tools without asking.
    state = MagicMock()
    state.get_linked_slot.return_value = object()
    assert await _run(arg, state=state) == thread_trust.LINKED_TEXT
    state.get_linked_slot.assert_called_once_with("1795569220.916459")
    assert not session_trust._trusted_sessions


@pytest.mark.asyncio
async def test_a_thread_owned_by_a_closed_dashboard_chat_is_pointed_at_the_picker():
    state = MagicMock()
    state.get_linked_slot.return_value = None
    assert await _run("", state=state, key="dashboard:abc") == thread_trust.LINKED_TEXT
    assert not session_trust._trusted_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_kind", ["bot", "conductor"])
async def test_a_thread_in_a_channel_with_a_bot_or_conductor_is_pointed_at_the_picker(
    monkeypatch: pytest.MonkeyPatch, handler_kind
):
    # The channel's one chat answers this thread's next message, so a grant on the
    # thread's own session would say Trust is on while every tool still asks.
    from kiro_crew.slack import channel_handler

    if handler_kind == "bot":
        monkeypatch.setattr(channel_handler, "channel_bot", lambda _c: "helper")
    else:
        monkeypatch.setattr(channel_handler, "channel_conductor", lambda _c: object())
    state = MagicMock()
    state.get_linked_slot.return_value = None
    assert await _run("", state=state) == thread_trust.LINKED_TEXT
    assert KEY not in session_trust._trusted_sessions
    # A grant made before the channel had a Bot or Conductor can still be seen and removed.
    session_trust.add_trusted_session(KEY, strict=True)
    assert await _run("status", state=state) == thread_trust.STATUS_ON_TEXT
    assert await _run("off", state=state) == thread_trust.OFF_TEXT
    assert KEY not in session_trust._trusted_sessions


@pytest.mark.asyncio
async def test_an_unlinked_thread_is_granted_with_the_dashboard_running():
    state = MagicMock()
    state.get_linked_slot.return_value = None
    assert await _run("", state=state) == thread_trust.ON_TEXT
    assert session_trust.is_session_trusted(KEY)


@pytest.mark.asyncio
async def test_off_with_a_failed_policy_reset_does_not_say_tools_ask_again():
    mgr = MagicMock()
    session_trust.add_trusted_session(KEY, mgr)
    mgr.set_approval_policy.side_effect = OSError("disk")
    assert await _run("off") == thread_trust.REVOKE_PARTIAL_TEXT
    assert not session_trust.is_session_trusted(KEY)
    thread_trust._audit.assert_called_once_with(
        "UOWNER", "error", f"session={KEY} mode=normal", "policy_reset_failed"
    )


# ── A channel-born dashboard tab on the thread's session ──


def _tab_state(trust: bool = True) -> tuple[MagicMock, MagicMock]:
    # A channel-born tab runs on the thread's own key but is not in ``_slack_to_slot``.
    slot = MagicMock(
        key="slack_1795569220_916459", linked_session_key=KEY, _trust_held=trust, _trust=trust
    )
    state = MagicMock(_slots={slot.key: slot})
    state.get_linked_slot.return_value = None
    return state, slot


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", [True, False])
async def test_trust_off_reports_the_tab_without_changing_it(grant):
    state, slot = _tab_state()
    if grant:
        session_trust.add_trusted_session(KEY)
    assert await _run("off", state=state) == thread_trust.TAB_STILL_TRUSTED_TEXT
    assert KEY not in session_trust._trusted_sessions
    assert slot._trust_held is True
    result = "set" if grant else "unchanged"
    thread_trust._audit.assert_called_once_with(
        "UOWNER", "allowed", f"session={KEY} mode=normal result={result} tab_trust=held"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", [True, False])
async def test_trust_off_reports_a_tab_masked_by_policy(monkeypatch, grant):
    state, slot = _tab_state()
    if grant:
        session_trust.add_trusted_session(KEY)
    slot._trust = False
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    assert await _run("off", state=state) == thread_trust.TAB_STILL_TRUSTED_TEXT
    assert KEY not in session_trust._trusted_sessions
    assert slot._trust_held is True
    assert slot._trust is False


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", [True, False])
async def test_trust_off_leaves_a_tab_without_trust_alone(grant):
    state, slot = _tab_state(trust=False)
    if grant:
        session_trust.add_trusted_session(KEY)
    expected = thread_trust.OFF_TEXT if grant else thread_trust.ALREADY_OFF_TEXT
    assert await _run("off", state=state) == expected
    assert KEY not in session_trust._trusted_sessions
    assert slot._trust_held is False


@pytest.mark.asyncio
async def test_status_reports_a_raw_grant_masked_by_policy(monkeypatch):
    session_trust.add_trusted_session(KEY)
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    assert not session_trust.is_session_trusted(KEY)
    assert await _run("status") == thread_trust.STATUS_ON_TEXT
    assert KEY in session_trust._trusted_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("masked", [True, False])
async def test_status_reports_only_the_tab_holds_trust(masked):
    state, slot = _tab_state()
    slot._trust = not masked
    assert await _run("status", state=state) == thread_trust.STATUS_TAB_TRUSTED_TEXT
    assert KEY not in session_trust._trusted_sessions
    assert slot._trust_held is True


@pytest.mark.parametrize("state", [None, object(), MagicMock(_slots={})])
def test_no_live_tab_holds_trust(state):
    assert not thread_trust._tab_holds_trust(state, KEY)


def test_another_sessions_tab_does_not_hold_this_threads_trust():
    state, slot = _tab_state()
    slot.linked_session_key = "slack:other"
    assert not thread_trust._tab_holds_trust(state, KEY)


def test_remove_trusted_session_uses_the_granting_manager():
    mgr = MagicMock()
    session_trust._trusted_sessions[KEY] = mgr
    session_trust.remove_trusted_session(KEY)
    assert KEY not in session_trust._trusted_sessions
    mgr.set_approval_policy.assert_called_once_with(KEY, "")


def test_remove_trusted_session_leaves_other_grants():
    session_trust._trusted_sessions["slack:other"] = None
    session_trust._trusted_sessions[KEY] = None
    session_trust.remove_trusted_session(KEY)
    assert "slack:other" in session_trust._trusted_sessions


# ── Dispatcher wiring ──


def _slack() -> MagicMock:
    slack = MagicMock()
    slack.post_message = AsyncMock()
    slack.post_blocks = AsyncMock()
    return slack


@pytest.mark.asyncio
async def test_bang_trust_posts_the_reply_in_the_thread():
    slack = _slack()
    sessions = _sessions()
    with patch.object(handler, "_dashboard_state", None):
        result = await handler._handle_slash_command(
            "!trust", slack, sessions, "C1", "t1", "m1", KEY, "UOWNER"
        )
    assert result == ""
    # Sub-agents inherit this policy whoever sent the turn: it stays unwritten.
    sessions.set_approval_policy.assert_not_called()
    slack.post_message.assert_awaited_once_with("C1", thread_trust.ON_TEXT, "t1")
    assert session_trust.is_session_trusted(KEY)


@pytest.mark.asyncio
async def test_bang_trust_sees_the_dashboard_session_that_owns_the_thread():
    slack = _slack()
    with patch.object(handler, "_dashboard_state", None):
        await handler._handle_slash_command(
            "!trust", slack, _sessions(owner="dashboard:abc"), "C1", "t1", "m1", "slack:t1", "U"
        )
    slack.post_message.assert_awaited_once_with("C1", thread_trust.LINKED_TEXT, "t1")
    assert not session_trust._trusted_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allowed, shared",
    [(True, False), (False, True), (False, False)],
    ids=["allowlisted", "shared", "stranger"],
)
@pytest.mark.parametrize("arg", ["", " on", " off", " status"])
async def test_bang_trust_is_owner_only(allowed, shared, arg):
    slack = _slack()
    with (
        patch.object(handler, "is_owner", return_value=False),
        patch.object(handler, "is_allowed_user", return_value=allowed),
        patch.object(handler, "is_shared_user", return_value=shared),
        patch.object(handler, "sel"),
    ):
        handled = await handler._route_bang_command(
            f"!trust{arg}", slack, _sessions(), "C1", "t1", "m1", KEY, "UOTHER", None
        )
    assert handled is True
    slack.post_message.assert_awaited_once_with("C1", "⛔ Owner-only command.", "t1")
    assert not session_trust.is_session_trusted(KEY)


@pytest.mark.asyncio
async def test_trust_in_one_thread_leaves_another_thread_asking():
    other = "slack:1795569999.000001"
    await _run("")
    assert session_trust.is_session_trusted(KEY)
    assert not session_trust.is_session_trusted(other)
    assert await _run("status", key=other) == thread_trust.STATUS_OFF_TEXT
    await _run("off", key=other)
    assert session_trust.is_session_trusted(KEY)


def test_a_linked_thread_hands_bang_trust_to_the_native_router():
    assert "!trust" in handler._NATIVE_BANG_COMMANDS


# ── A shared user's turn in a trusted thread still asks ──


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [True, False])
async def test_native_ladder_keeps_session_trust_for_the_owners_turns(monkeypatch, shared):
    import test_slack_ladder_held_bot_limit as ladder

    from kiro_crew import mate_held_rules
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

    monkeypatch.setattr(mate_held_rules, "held_for", lambda *a, **k: [])
    monkeypatch.setattr(handler, "sel", MagicMock())
    monkeypatch.setattr(handler, "is_shared_listed", lambda user: shared)
    prompt = AsyncMock(return_value=False)
    monkeypatch.setattr(handler, "_request_approval", prompt)
    client = MagicMock()
    client.approve_tool = AsyncMock(return_value=True)
    client.reject_tool = AsyncMock()
    event = LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="Run: ls", request_id="r1")

    await handler._answer_permission_request(ladder._rung(monkeypatch, "trust"), client, event)

    if shared:
        client.approve_tool.assert_not_awaited()
        prompt.assert_awaited_once()
    else:
        client.approve_tool.assert_awaited_once_with("r1")
        prompt.assert_not_awaited()


@pytest.mark.parametrize("shared", [True, False])
def test_transport_driver_keeps_session_trust_for_the_owners_turns(monkeypatch, shared):
    import asyncio

    from slack_transport_dispatch_helpers import (
        _MSG_TS,
        RecordingSlackClient,
        ScriptedProvider,
        _CapturingSessions,
    )

    from kiro_crew.slack import transport_dispatch

    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
    captured: list = []

    class _Recorder:
        def __init__(self, *_a, **kw) -> None:
            captured.append(kw.get("auto_approve_session"))

        async def run(self, message: str) -> str:
            return "ok"

    monkeypatch.setattr(transport_dispatch, "TurnDriver", _Recorder)
    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=RecordingSlackClient(),
            sessions=_CapturingSessions(ScriptedProvider([])),
            channel="C1",
            text="hello",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id="U_TURN",
            context_builder=None,
            conversation_log=None,
        )
    )
    assert captured, "the transport path never built a driver"
    # The predicate reads both names at call time, so these patches are what it sees.
    monkeypatch.setattr(transport_dispatch, "is_shared_listed", lambda user: shared)
    monkeypatch.setattr(transport_dispatch, "is_slack_session_trusted", lambda key: True)
    assert captured[0]() is (not shared)
