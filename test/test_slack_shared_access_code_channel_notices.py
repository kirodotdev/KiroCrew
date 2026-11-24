"""A shared user (slack.shared_access) refused by a code channel is told why.

Code channels stay the owner's, except the one a shared user opened. Each refusal
below sends one ephemeral only the shared user sees, so a refused message or button
never looks broken and never names the allowlist, which is inert for them: every
button press, and their messages at most once per person per channel per window.
The agent's code-channel tools say how a channel opens, so an agent outside one
tells the person.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    CH,
    SLOT,
    _bind,
    _run,
    env_fixture,
)
from test_slack_code_channel_trust_from_slack import chan  # noqa: F401 - fixture by name
from test_slack_code_channel_worker_approvals import relay  # noqa: F401 - fixture by name

# Imported here, never under a patch: a module first imported while a name is
# patched binds the patched value for the rest of the session.
from kiro_crew.slack import events, handler, interactions  # noqa: F401
from kiro_crew.slack.code_channels import routing, shared_refusal, tools
from kiro_crew.slack.code_channels.routing import Gate
from kiro_crew.slack.handler_runtime.inbound import LinkedRoute, route_linked_thread
from kiro_crew.testing.clock import ManualClock

SHARED = "U0SHARED01"
OTHER_SHARED = "U0SHARED02"
STRANGER = "U0STRANGER"
DM = "D0DM00001"
# Read with a default so each test, not the module import, fails where it is missing.
NOTICE = getattr(shared_refusal, "OWNER_ONLY_TEXT", "<no notice text>")


def _as_shared():
    """Treat SHARED as a confirmed, listed shared user (never the owner)."""
    return (
        patch("kiro_crew.slack.handler.is_shared_user", lambda u: u == SHARED),
        patch("kiro_crew.slack.handler.is_shared_listed", lambda u: u == SHARED),
    )


def _press(env: Any, monkeypatch: pytest.MonkeyPatch, action_id: str) -> None:
    async def _prime(*_a: Any) -> None:
        return None

    monkeypatch.setattr(interactions, "_orch", SimpleNamespace(slack=env.slack))
    monkeypatch.setattr(interactions, "sel", lambda: MagicMock())
    monkeypatch.setattr(interactions, "prime_shared_user", _prime)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: False)
    monkeypatch.setattr(interactions, "is_shared_user", lambda u: u == SHARED)
    monkeypatch.setattr(interactions, "is_shared_listed", lambda u: u == SHARED)
    payload = {
        "type": "block_actions",
        "user": {"id": SHARED},
        "channel": {"id": CH},
        "message": {"ts": "300.000001"},
        "actions": [{"action_id": action_id, "value": "7"}],
    }
    asyncio.run(interactions.dispatch(payload))


def test_a_shared_users_press_in_a_code_channel_is_answered_every_time(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # approve_tool is a button a shared user may press elsewhere; in a code channel
    # the gate refuses it, and each press says why instead of looking dead.
    _bind(env)
    a, b = _as_shared()
    with a, b:
        _press(env, monkeypatch, "approve_tool")
        _press(env, monkeypatch, "approve_tool")
    assert env.slack.ephemerals == [(CH, SHARED, NOTICE), (CH, SHARED, NOTICE)]


def test_a_shared_users_messages_in_a_code_channel_get_one_notice_per_window(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind(env)
    audit = MagicMock()
    monkeypatch.setattr(events, "sel", lambda: audit)
    clock = ManualClock()
    clock.install(monkeypatch, shared_refusal)
    for _ in range(2):
        assert asyncio.run(shared_refusal.shared_message_dropped(SHARED, CH, env.slack, "5.1"))
    # Every message is refused and audited; the person is told once.
    assert audit.log_api_access.call_count == 2
    assert env.slack.ephemerals == [(CH, SHARED, NOTICE)]
    clock.advance(shared_refusal.NOTICE_WINDOW_SECS)
    assert asyncio.run(shared_refusal.shared_message_dropped(SHARED, CH, env.slack, "5.1"))
    assert env.slack.ephemerals == [(CH, SHARED, NOTICE)] * 2


def test_a_shared_users_linked_thread_into_a_code_channel_session_is_told(env: Any) -> None:
    # A plain DM thread the owner linked to the code channel's dashboard session.
    _bind(env)
    state = SimpleNamespace(get_linked_slot=lambda ts: SimpleNamespace(key=SLOT))
    a, b = _as_shared()
    with a, b, patch.object(handler, "_dashboard_state", state):
        for _ in range(2):
            outcome = asyncio.run(
                route_linked_thread(
                    "now add a regression test",
                    "slack:555.000001",
                    SHARED,
                    DM,
                    env.slack,
                    "555.000001",
                )
            )
            assert outcome is LinkedRoute.REFUSED
    assert env.slack.ephemerals == [(DM, SHARED, NOTICE)]


def test_a_shared_users_reply_routed_inside_a_code_channel_is_told(env: Any) -> None:
    _bind(env)
    a, b = _as_shared()
    with a, b:
        outcome = asyncio.run(
            route_linked_thread("go", "slack:100.000001", SHARED, CH, env.slack, "100.000001")
        )
    assert outcome is LinkedRoute.REFUSED
    assert env.slack.ephemerals == [(CH, SHARED, NOTICE)]


def _mention_in(channel: str, user: str) -> dict[str, Any]:
    return {
        "type": "app_mention",
        "user": user,
        "channel": channel,
        "ts": "301.000001",
        "team": "T1",
        "text": "<@UBOT> make the theme darker",
    }


@pytest.mark.parametrize("user, text", [(SHARED, NOTICE), (STRANGER, routing.NOT_AUTHORIZED_TEXT)])
def test_a_mention_in_a_code_channel_slacks_ui_opened_names_the_right_rule(
    env: Any, user: str, text: str
) -> None:
    # Unbound, opened by Slack's UI with us as its agent: the gate refuses anyone it
    # was not told to admit. A shared user hears the owner-only rule, never the
    # allowlist, which is inert for them; a stranger keeps the allowlist text.
    unbound = "C0UNBOUND"
    env.info(unbound)
    a, b = _as_shared()
    with a, b:
        result = asyncio.run(
            routing.inbound_gate(
                env.orch,
                _mention_in(unbound, user),
                text=_mention_in(unbound, user)["text"],
                sender_id=user,
                authorized=False,
                from_trusted_bot=False,
            )
        )
    assert result.gate is Gate.DROP
    assert env.slack.ephemerals == [(unbound, user, text)]
    assert env.routed == []


def test_an_agent_outside_a_code_channel_is_told_how_one_opens(env: Any) -> None:
    out = _run(env, tools.SET_VIEW, {"view_key": "r", "content": "<p>x</p>"})
    assert not out.ok and out.code == "not_bound"
    assert "No tool opens a code channel" in out.text
    assert "a person the owner shares Slack access with" in out.text
    assert "tell the person" in out.text
    from kiro_crew.mcp_tools.catalog.code_channels import schemas

    # How one opens: asking again in this thread is judged again and carries the
    # thread over; a top-level message does not; Slack Connect can't host one.
    guidance = (
        "No tool opens a code channel",
        "tell the person",
        "ask for it again in this thread",
        "starts with this thread",
        "A new top-level message starts without this thread",
        "Slack Connect",
        "Never promise to post into a code channel that hasn't opened",
    )
    for phrase in guidance:
        assert phrase in out.text, phrase
    for schema in schemas():
        for phrase in guidance:
            assert phrase in schema["description"], (schema["name"], phrase)


# ── The opener's own channel: what they may and may not press ────────────────


def _opened_by(env: Any, opener: str) -> Any:
    from dataclasses import replace

    binding = replace(_bind(env), opener=opener)
    env.store.replace_binding(binding)
    return binding


def _still(users: Any):
    """``shared_access.still_confirmed`` answering for *users* (read at each call)."""
    return patch(
        "kiro_crew.slack.shared_access.still_confirmed",
        lambda _cfg, user, *, owner: not owner and user in users,
    )


@contextlib.contextmanager
def _listed_and_still(users: Any):
    with patch("kiro_crew.slack.handler.is_shared_listed", lambda u: u in users), _still(users):
        yield


def _confirmed(*users: str):
    return (
        patch("kiro_crew.slack.handler.is_shared_user", lambda u: u in users),
        _listed_and_still(users),
    )


def test_the_opener_answers_a_helper_agents_card_but_never_trusts(env: Any) -> None:
    from kiro_crew.slack.code_channels import approval_picker

    binding = _opened_by(env, SHARED)
    a, b = _confirmed(SHARED, OTHER_SHARED)

    def gate(user: str, *, admit_opener: bool) -> Any:
        return asyncio.run(
            approval_picker.authorize(env.orch, binding, user, "T1", "r", admit_opener=admit_opener)
        )

    with a, b:
        # Approve / Reject (admit_opener): the opener, like the owner.
        assert gate(SHARED, admit_opener=True) is None
        # Trust this channel and the mode itself stay the owner's.
        assert gate(SHARED, admit_opener=False) == approval_picker.OWNER_ONLY_TEXT
        # Nobody else gets in: another shared user, here or anywhere.
        assert gate(OTHER_SHARED, admit_opener=True) == approval_picker.OWNER_ONLY_TEXT


def test_the_openers_channel_keeps_its_owner_only_controls(env: Any) -> None:
    from kiro_crew.slack.code_channels import agent_picker, approval_picker, wrapup
    from kiro_crew.slack.code_channels.handoff import HANDOFF_ACTION_ID
    from kiro_crew.slack.code_channels.worker_approvals import TRUST_ACTION_ID
    from kiro_crew.slack.shared_access import shared_action_permitted

    binding = _opened_by(env, SHARED)
    for action_id in (
        approval_picker.TRUST_CALL_ACTION_ID,
        approval_picker.APPROVAL_SELECT_ACTION_ID,
        approval_picker.APPROVAL_CONFIRM_ACTION_ID,
        agent_picker.AGENT_SELECT_ACTION_ID,
        TRUST_ACTION_ID,
        HANDOFF_ACTION_ID,
        wrapup.ARCHIVE_ACTION_ID,
    ):
        assert shared_action_permitted(action_id) is False, action_id
    a, b = _confirmed(SHARED)
    with a, b:
        refusal = asyncio.run(agent_picker.authorize(env.orch, binding, SHARED, "T1", "r"))
    assert refusal == agent_picker.OWNER_ONLY_TEXT


def test_the_openers_press_in_their_own_channel_is_not_refused_by_the_gate(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _opened_by(env, SHARED)
    a, b = _confirmed(SHARED)
    with a, b:
        refused = asyncio.run(shared_refusal.shared_user_refused_here(env.slack, CH, SHARED))
        other = asyncio.run(shared_refusal.shared_user_refused_here(env.slack, CH, OTHER_SHARED))
    assert refused is False and other is True


# ── The opener answers their channel session's prompts; helper agents follow ──


def _arm_prompt(channel: str = CH, ts: str = "400.000001", slot_key: str = SLOT) -> str:
    from kiro_crew.slack.handler import _linked_approvals, _LinkedApproval

    _linked_approvals[f"{channel}:{ts}"] = _LinkedApproval(
        request_id="7", session_key=f"dashboard:{slot_key}", slot_key=slot_key
    )
    return ts


@pytest.mark.parametrize(
    "who, action, channel, answered",
    [
        (SHARED, "approve_tool", CH, "approve_tool"),
        (SHARED, "reject_tool", CH, "reject_tool"),
        (SHARED, "trust_tool", CH, None),  # Trust stays the owner's
        (OTHER_SHARED, "approve_tool", CH, None),  # not the opener
        (SHARED, "approve_tool", DM, None),  # the same session's card mirrored elsewhere
    ],
)
def test_the_opener_answers_their_channel_sessions_prompts_and_no_one_else(
    env: Any, who: str, action: str, channel: str, answered: str | None
) -> None:
    from kiro_crew.slack.handler import _linked_approvals, handle_interaction

    _opened_by(env, SHARED)
    slot = SimpleNamespace(key=SLOT)
    dstate = MagicMock()
    dstate._slots = {SLOT: slot}
    dstate.resolve_slot_approval = MagicMock(return_value=True)
    ts = _arm_prompt(channel)
    a, b = _confirmed(SHARED, OTHER_SHARED)
    try:
        with a, b, patch.object(handler, "_dashboard_state", dstate):
            result = asyncio.run(handle_interaction(channel, ts, action, user_id=who))
        assert result == answered
        if answered:
            dstate.resolve_slot_approval.assert_called_once_with(
                slot, "7", action == "approve_tool", expected_future=None
            )
        else:
            dstate.resolve_slot_approval.assert_not_called()
            assert f"{channel}:{ts}" in _linked_approvals  # still the owner's to answer
    finally:
        _linked_approvals.pop(f"{channel}:{ts}", None)


def test_an_opener_removed_while_their_click_is_checked_answers_nothing(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.handler import _linked_approvals, handle_interaction

    _opened_by(env, SHARED)
    dstate = MagicMock()
    dstate._slots = {SLOT: SimpleNamespace(key=SLOT)}
    ts = _arm_prompt(CH)
    confirmed = {SHARED}
    real = shared_refusal.opener_answers_prompt

    def checked_then_removed(*a: Any) -> bool:
        out = real(*a)
        confirmed.clear()  # the owner removes them as the first check returns
        return out

    monkeypatch.setattr(shared_refusal, "opener_answers_prompt", checked_then_removed)
    monkeypatch.setattr(handler, "is_shared_user", lambda u: u in confirmed)
    monkeypatch.setattr(handler, "is_shared_listed", lambda u: u == SHARED)
    try:
        with patch.object(handler, "_dashboard_state", dstate), _still(confirmed):
            result = asyncio.run(handle_interaction(CH, ts, "approve_tool", user_id=SHARED))
        assert result is None
        dstate.resolve_slot_approval.assert_not_called()
    finally:
        _linked_approvals.pop(f"{CH}:{ts}", None)


def test_a_worker_prompt_card_in_the_openers_channel_is_not_theirs_to_answer(env: Any) -> None:
    # A prompt of any session but the channel's own (here another slot) stays refused.
    from kiro_crew.slack.handler import _linked_approvals, handle_interaction

    _opened_by(env, SHARED)
    ts = _arm_prompt(CH, slot_key="chat-9-9")
    a, b = _confirmed(SHARED)
    try:
        with a, b:
            assert asyncio.run(handle_interaction(CH, ts, "approve_tool", user_id=SHARED)) is None
    finally:
        _linked_approvals.pop(f"{CH}:{ts}", None)


def test_the_openers_slot_relinked_to_another_session_answers_nothing(env: Any) -> None:
    # The opener's own slot, but its turn ran on another session (a linked_session_key
    # restored from the editable transcript): that session's prompt is not theirs.
    from kiro_crew.slack.handler import _linked_approvals, _LinkedApproval, handle_interaction

    _opened_by(env, SHARED)
    dstate = MagicMock()
    dstate._slots = {SLOT: SimpleNamespace(key=SLOT)}
    ts = "400.000002"
    _linked_approvals[f"{CH}:{ts}"] = _LinkedApproval(
        request_id="7", session_key="slack:999.000001", slot_key=SLOT
    )
    a, b = _confirmed(SHARED)
    try:
        with a, b, patch.object(handler, "_dashboard_state", dstate):
            result = asyncio.run(handle_interaction(CH, ts, "approve_tool", user_id=SHARED))
        assert result is None
        dstate.resolve_slot_approval.assert_not_called()
        assert f"{CH}:{ts}" in _linked_approvals
    finally:
        _linked_approvals.pop(f"{CH}:{ts}", None)


@pytest.mark.parametrize("slot_key, answered", [(SLOT, None), ("chat-9-9", "approve_tool")])
def test_the_openers_session_relinked_to_a_plain_thread_is_not_theirs_as_requester(
    env: Any, slot_key: str, answered: str | None
) -> None:
    # The channel's session is relinked to an ordinary thread; its running turn is
    # still the opener's, so the card it posts there names them as requester. The
    # requester rule answers an ordinary session's card, never a code channel's.
    from kiro_crew.slack.handler import _linked_approvals, _LinkedApproval, handle_interaction
    from kiro_crew.slack.turn_requester import note_turn_requester

    _opened_by(env, SHARED)

    async def go() -> str | None:
        turn = asyncio.get_running_loop().create_future()
        slot = SimpleNamespace(key=slot_key, task=turn)
        dstate = MagicMock()
        dstate._slots = {slot_key: slot}
        dstate.resolve_slot_approval = MagicMock(return_value=True)
        note_turn_requester(turn, SHARED)  # type: ignore[arg-type]
        _linked_approvals["C0PLAIN:600.000001"] = _LinkedApproval(
            "7", f"dashboard:{slot_key}", False, slot_key, None, SHARED
        )
        with patch.object(handler, "_dashboard_state", dstate):
            return await handle_interaction("C0PLAIN", "600.000001", "approve_tool", user_id=SHARED)

    a, b = _confirmed(SHARED)
    try:
        with a, b:
            assert asyncio.run(go()) == answered
    finally:
        _linked_approvals.pop("C0PLAIN:600.000001", None)


@pytest.mark.parametrize("linked, refused", [("", False), ("dashboard:chat-other", True)])
def test_the_openers_tab_relinked_to_another_session_is_not_theirs(
    env: Any, linked: str, refused: bool
) -> None:
    # History resume can read a linked_session_key back onto the channel's tab; it
    # then runs another session, which the opener's message must not reach.
    binding = _opened_by(env, SHARED)
    tab = SimpleNamespace(key=binding.session_key, linked_session_key=linked)
    dstate = SimpleNamespace(_slots={binding.session_key: tab})
    a, b = _confirmed(SHARED)
    with a, b, patch.object(routing, "_dashboard_state", lambda: dstate):
        got = shared_refusal.shared_user_refused_for_slot(
            binding.session_key, "", sender=SHARED, channel=CH
        )
    assert got is refused


def test_the_openers_turns_follow_the_helper_agent_switch(env: Any) -> None:
    from kiro_crew.dashboard.session_control_parts import code_channel_exemption as ex

    class _Slot:
        key = SLOT
        _turn_channel_origin = True

    slot = _Slot()
    binding = _opened_by(env, SHARED)
    a, b = _confirmed(SHARED, OTHER_SHARED)
    ex.forget_senders()
    try:
        with a, b:
            ex.note_channel_sender(slot, SHARED)
            # The opener's text drives their own channel's helper agents as the owner's does.
            assert ex._sender_refusal(slot, binding) == ""
            # The owner's channel, or no binding at all: refused, as before.
            assert "someone other than the owner" in ex._sender_refusal(slot, None)
            from dataclasses import replace

            assert "someone other than the owner" in ex._sender_refusal(
                slot, replace(binding, opener="")
            )
            ex.note_channel_sender(slot, OTHER_SHARED)
            assert "someone other than the owner" in ex._sender_refusal(slot, binding)
        # The opener removed from shared access: refused at their next judgement.
        ex.forget_senders()
        ex.note_channel_sender(slot, SHARED)
        with _still(()):
            assert "someone other than the owner" in ex._sender_refusal(slot, binding)
    finally:
        ex.forget_senders()


@pytest.mark.parametrize(
    "action_id", ["kc_code_channel_worker_approve", "kc_code_channel_worker_reject"]
)
@pytest.mark.parametrize("removed_mid_click", [False, True])
def test_the_opener_answers_a_helper_agents_card_only_while_still_shared(
    relay: Any,  # noqa: F811 - the fixture imported by name
    monkeypatch: pytest.MonkeyPatch,
    action_id: str,
    removed_mid_click: bool,
) -> None:
    # Removed from shared access while the click's owner gate awaits: asked again at
    # the commit, so neither Approve nor Reject answers the helper agent's call.
    from dataclasses import replace

    from test_slack_code_channel_worker_approvals import _click as worker_click
    from test_slack_code_channel_worker_approvals import _run_relay

    from kiro_crew.slack.code_channels import agent_picker
    from kiro_crew.slack.code_channels import worker_approvals as wa
    from kiro_crew.slack.code_channels.store import store

    store().replace_binding(replace(store().get_binding(CH), opener=SHARED))
    confirmed = {SHARED}
    monkeypatch.setattr(handler, "is_shared_user", lambda u: u in confirmed)
    monkeypatch.setattr(handler, "is_shared_listed", lambda u: u == SHARED)
    real = agent_picker.authorize

    async def admitted_then_removed(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        if removed_mid_click:
            confirmed.clear()
        return out

    monkeypatch.setattr(agent_picker, "authorize", admitted_then_removed)

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        await wa.handle_click(relay.orch, worker_click(SHARED, card, action_id))
        return fut

    with _still(confirmed):
        fut = asyncio.run(go())
    assert fut.done() is (not removed_mid_click)


def test_the_openers_slash_command_in_their_channel_is_the_owners(env: Any) -> None:
    # "/agent", "/model" and the like reach the session's own command parser: a
    # shared user's stay refused in the channel they opened, as in any thread.
    from kiro_crew.slack.shared_access import SHARED_OWNER_ONLY_TEXT

    _opened_by(env, SHARED)
    a, b = _confirmed(SHARED)
    event = {"type": "message", "user": SHARED, "channel": CH, "ts": "301.000002", "team": "T1"}
    result = routing.InboundResult(Gate.DIRECTED, binding=env.store.get_binding(CH))
    with a, b:
        handled = asyncio.run(
            routing.dispatch(
                env.orch,
                result,
                event={**event, "text": "/agent kirocrew-conductor"},
                clean_text="/agent kirocrew-conductor",
                sender_id=SHARED,
                was_mention=False,
            )
        )
    assert handled is True and env.routed == []
    assert env.slack.ephemerals == [(CH, SHARED, SHARED_OWNER_ONLY_TEXT)]


@pytest.mark.parametrize(
    "typed, delivered",
    [
        ("", True),  # a captionless upload: the ingested text leads with its temp path
        ("see this", True),
        ("/agent kirocrew-conductor", False),  # a typed command stays the owner's
    ],
)
def test_the_openers_upload_is_judged_on_what_they_typed(
    env: Any, monkeypatch: pytest.MonkeyPatch, typed: str, delivered: bool
) -> None:
    _opened_by(env, SHARED)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(routing, "deliver_directed", deliver)
    ingested = f"{typed}\n/tmp/kc-inbound/shot.png" if typed else "/tmp/kc-inbound/shot.png"
    event = {"type": "message", "user": SHARED, "channel": CH, "ts": "301.000003", "team": "T1"}
    result = routing.InboundResult(Gate.DIRECTED, binding=env.store.get_binding(CH))
    a, b = _confirmed(SHARED)
    with a, b:
        handled = asyncio.run(
            routing.dispatch(
                env.orch,
                result,
                event={**event, "text": typed},
                clean_text=ingested,
                sender_id=SHARED,
                was_mention=False,
                typed_text=typed,
            )
        )
    assert handled is True
    assert deliver.await_count == (1 if delivered else 0)
    if delivered:
        assert deliver.await_args.args[3] == ingested  # the session still gets the file


def test_the_openers_admission_outlives_the_confirmations_cache_but_not_a_removal(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The channel's session asks mid-turn (a helper agent, long after the opener's
    # message), where nothing asks Slack again: the expired cache must not read as a
    # stranger, while a removal from the list ends it at once.
    from types import SimpleNamespace as NS

    from kiro_crew.slack import shared_access

    binding = _opened_by(env, SHARED)
    ids = [SHARED]
    cfg = NS(slack=NS(shared_access=NS(enabled=True, user_ids=ids)))
    # The live shared-access list, read at each call, as the config would give it.
    monkeypatch.setattr(shared_access, "_settings", lambda _cfg: (True, list(ids)))
    monkeypatch.setattr(handler, "is_owner", lambda u: False)
    verdict = shared_access.Verdict(SHARED, True, shared_access.REASON_OK, checked_at=0.0)
    monkeypatch.setitem(shared_access._verdicts, SHARED, verdict)
    monkeypatch.setattr(shared_access, "clock", lambda: shared_access.CONFIRMED_TTL_SECS * 10)
    assert shared_access.is_shared_user(cfg, SHARED, owner=False) is False  # expired
    assert shared_refusal.opener_admitted(binding, SHARED) is True
    ids.clear()  # the owner removes them
    assert shared_refusal.opener_admitted(binding, SHARED) is False
    ids.append(SHARED)
    refused = shared_access.Verdict(SHARED, False, shared_access.REASON_DEACTIVATED)
    monkeypatch.setitem(shared_access._verdicts, SHARED, refused)
    assert shared_refusal.opener_admitted(binding, SHARED) is False


def test_helper_agents_still_start_past_the_confirmations_cache(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A long turn the opener started, then an owner turn after the opener's last
    # message, both judged after the shared-user confirmation's cache expired.
    from kiro_crew.dashboard.session_control_parts import code_channel_exemption as ex
    from kiro_crew.slack import shared_access

    class _Slot:
        key = SLOT
        _turn_channel_origin = True

    slot = _Slot()
    binding = _opened_by(env, SHARED)
    # The live shared-access list, as the config would give it.
    monkeypatch.setattr(shared_access, "_settings", lambda _cfg: (True, [SHARED]))
    monkeypatch.setattr(handler, "is_owner", lambda u: u == "UOWNER")
    verdict = shared_access.Verdict(SHARED, True, shared_access.REASON_OK, checked_at=0.0)
    monkeypatch.setitem(shared_access._verdicts, SHARED, verdict)
    now = [0.0]
    monkeypatch.setattr(shared_access, "clock", lambda: now[0])
    ex.forget_senders()
    try:
        ex.note_channel_sender(slot, SHARED)
        now[0] = shared_access.CONFIRMED_TTL_SECS + 1  # the opener's turn runs on
        assert ex._sender_refusal(slot, binding) == ""
        ex.note_channel_sender(slot, "UOWNER")  # then the owner's turn
        now[0] = shared_access.CONFIRMED_TTL_SECS * 5
        assert ex._sender_refusal(slot, binding) == ""
    finally:
        ex.forget_senders()


@pytest.mark.parametrize(
    "action_id", ["kc_code_channel_worker_approve", "kc_code_channel_worker_reject"]
)
def test_an_openers_card_answer_through_a_channel_unbound_mid_click_answers_nothing(
    relay: Any,  # noqa: F811 - the fixture imported by name
    monkeypatch: pytest.MonkeyPatch,
    action_id: str,
) -> None:
    # Slack revokes the channel (its binding is dropped) while the click's owner gate
    # awaits: the opener is judged on the binding stored now, so nothing is answered.
    from dataclasses import replace

    from test_slack_code_channel_worker_approvals import _click as worker_click
    from test_slack_code_channel_worker_approvals import _run_relay

    from kiro_crew.slack.code_channels import agent_picker
    from kiro_crew.slack.code_channels import worker_approvals as wa
    from kiro_crew.slack.code_channels.store import store

    store().replace_binding(replace(store().get_binding(CH), opener=SHARED))
    monkeypatch.setattr(handler, "is_shared_user", lambda u: u == SHARED)
    monkeypatch.setattr(handler, "is_shared_listed", lambda u: u == SHARED)
    real = agent_picker.authorize

    async def admitted_then_unbound(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        store().drop_binding(CH)
        return out

    monkeypatch.setattr(agent_picker, "authorize", admitted_then_unbound)

    async def go() -> asyncio.Future:
        card, fut = await _run_relay(relay)
        await wa.handle_click(relay.orch, worker_click(SHARED, card, action_id))
        return fut

    with _still({SHARED}):
        fut = asyncio.run(go())
    assert not fut.done()


@pytest.mark.parametrize("gated", [False, True])  # a card answer; a typed message
def test_an_openers_card_answer_routed_after_their_removal_appends_nothing(
    env: Any, monkeypatch: pytest.MonkeyPatch, gated: bool
) -> None:
    # A question-card answer reaches route_linked_thread with no caller gate. The
    # opener is removed during the route's awaits (here the channel-agent read): the
    # commit point judges them again on the binding stored now and appends nothing.
    from kiro_crew.slack.handler_runtime import inbound

    _opened_by(env, SHARED)
    users = {SHARED}
    slot = SimpleNamespace(key=SLOT, window_pending=False, running=False)
    state = SimpleNamespace(get_linked_slot=lambda ts: slot, _slots={SLOT: slot}, sessions=None)
    appended: list[Any] = []

    async def mismatch_then_remove(*_a: Any) -> None:
        return None

    async def images(_state: Any, _slot: Any, text: str, **_k: Any) -> str:
        users.clear()  # the owner removes them while the route copies the images
        return text

    monkeypatch.setattr(inbound, "linked_slot_channel_mismatch", mismatch_then_remove)
    monkeypatch.setattr(handler, "store_linked_thread_images", images)
    monkeypatch.setattr(handler, "append_and_surface", lambda *a, **k: appended.append(a))
    monkeypatch.setattr(handler, "is_shared_listed", lambda u: u == SHARED)
    monkeypatch.setattr(handler, "is_shared_user", lambda u: u in users)
    audit = MagicMock()
    monkeypatch.setattr(handler, "sel", lambda: audit)
    with patch.object(handler, "_dashboard_state", state), _still(users):
        outcome = asyncio.run(
            route_linked_thread(
                "Option B",
                "slack:100.000001",
                SHARED,
                CH,
                env.slack,
                "100.000001",
                target_slot=slot,
                route_pinned=True,
                interpret_commands=False,
                # The typed message's caller gate (opener_still_admitted) refuses too.
                admit=AsyncMock(return_value=False) if gated else None,
            )
        )
    # Refused, audited and told: the card was marked answered, so a silent hold
    # would leave them thinking their answer reached the session.
    assert outcome is LinkedRoute.REFUSED and appended == []
    assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
    assert env.slack.ephemerals == [(CH, SHARED, shared_refusal.OWNER_ONLY_TEXT)]


def test_the_openers_inline_stop_in_their_own_channel_is_not_refused(env: Any) -> None:
    from kiro_crew.slack import interactions_sessions

    _opened_by(env, SHARED)
    a, b = _confirmed(SHARED, OTHER_SHARED)
    with a, b:
        mine = asyncio.run(interactions_sessions._outside_code_channel(CH, SHARED))
        theirs = asyncio.run(interactions_sessions._outside_code_channel(CH, OTHER_SHARED))
    assert mine is True and theirs is False


@pytest.mark.parametrize("removed_mid_click", [False, True])
def test_the_openers_inline_stop_is_judged_again_right_before_it_stops(
    env: Any, monkeypatch: pytest.MonkeyPatch, removed_mid_click: bool
) -> None:
    # The opener presses Stop on their own turn; the owner removes them while the
    # cosmetic "Stopping" edit awaits. The stop asks again with nothing awaited
    # before it, so a removed opener stops nothing.
    from kiro_crew.slack import events as slack_events
    from kiro_crew.slack import interactions_sessions as isess

    _opened_by(env, SHARED)
    users = {SHARED}

    shown: list[str] = []
    told: list[tuple[str, str, str]] = []

    async def update_then_remove(_ch: str, _ts: str, *, text: str) -> None:
        shown.append(text)
        if removed_mid_click:
            users.clear()

    async def ephemeral(ch: str, user: str, text: str, **_k: Any) -> None:
        told.append((ch, user, text))

    @contextlib.asynccontextmanager
    async def no_hold(*_a: Any, **_k: Any) -> Any:
        yield

    stop_turn = AsyncMock(return_value="soft")
    orch = SimpleNamespace(
        slack=SimpleNamespace(update_message=update_then_remove, post_ephemeral=ephemeral),
        sessions=SimpleNamespace(stop_turn=stop_turn),
    )
    # Through the facade: these names are shared by the interactions family.
    monkeypatch.setattr(interactions, "_orch", orch)
    monkeypatch.setattr(interactions, "sel", lambda: MagicMock())
    monkeypatch.setattr(interactions, "is_owner", lambda u: False)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: False)
    monkeypatch.setattr(interactions, "is_shared_user", lambda u: u in users)
    monkeypatch.setattr(interactions, "is_shared_listed", lambda u: u == SHARED)
    monkeypatch.setattr(handler, "is_shared_user", lambda u: u in users)
    monkeypatch.setattr(handler, "is_shared_listed", lambda u: u == SHARED)
    monkeypatch.setattr(slack_events, "slack_stop_in_flight", no_hold)
    payload = {"message": {"ts": "500.000001"}}
    with _still(users):
        asyncio.run(isess._handle_inline_stop(payload, {"value": SLOT}, CH, "500.000001", SHARED))
    assert stop_turn.await_count == (0 if removed_mid_click else 1)
    if removed_mid_click:
        # The "Stopping" it already showed is taken back, and they are told why.
        from kiro_crew.slack.shared_access import SHARED_OWNER_ONLY_TEXT

        assert shown[-1] == "⏹ Stop not applied."
        assert told == [(CH, SHARED, SHARED_OWNER_ONLY_TEXT)]
    else:
        assert told == []
