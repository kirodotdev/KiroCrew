"""Slack Code channels through the real ``_route_message`` seams.

With ``slack.code_channels`` off the gateway must behave exactly as without the
feature; with it on, a code channel answers without a mention and a mention
that asks for multi-step work moves into a new channel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_code_channels_helpers import OWNER, SELF_USER, Env, make_env, mention

from kiro_crew.config.loader import KiroCrewConfig, MessagingConfig
from kiro_crew.slack import features
from kiro_crew.slack.code_channels.classifier import RouteDecision
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.events import SeenCache, _route_message

pytestmark = pytest.mark.asyncio


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    yield from make_env(monkeypatch, tmp_path)


def _orch(env: Env) -> MagicMock:
    orch = MagicMock()
    orch._cfg = KiroCrewConfig(messaging=MessagingConfig(use_transport=False))
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = {}
    orch.slack = env.slack
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    for name in (
        "ctx_builder",
        "cron_svc",
        "conv_log",
        "consolidator",
        "subagent_mgr",
        "task_runner",
    ):
        setattr(orch, name, None)
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    env.orch = orch
    return orch


async def _route(env: Env, event: dict[str, Any], *, is_mention: bool) -> MagicMock:
    orch = _orch(env)
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as handle:
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await _route_message(orch, event, SeenCache(), is_mention=is_mention)
            await asyncio.sleep(0)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
    return handle


def _bind(env: Env) -> None:
    env.store.put_binding(
        Binding(channel_id="C0CODE", team_id="T1", session_key="chat-9-1", anchor_ts="300.000001")
    )
    env.info("C0CODE")


def _plain(channel: str, text: str = "now add a regression test") -> dict[str, Any]:
    return {
        "type": "message",
        "user": OWNER,
        "channel": channel,
        "ts": "301.000001",
        "text": text,
        "team": "T1",
    }


async def test_toggle_off_is_identical_to_main_in_a_bound_channel(env: Env) -> None:
    _bind(env)
    env.on = False
    handle = await _route(env, _plain("C0CODE"), is_mention=False)
    # Mention-mode channel, no mention: ignored, as on main. No Slack lookups.
    handle.assert_not_called()
    assert env.routed == [] and env.api.calls == []


async def test_toggle_off_mention_takes_the_normal_path(env: Env) -> None:
    env.on = False
    handle = await _route(env, mention(), is_mention=True)
    handle.assert_called_once()
    assert env.classified == [] and env.api.calls == [] and env.sel.rows == []


@pytest.mark.parametrize("why", ["scope_missing", "latched"])
async def test_toggle_on_but_unavailable_is_identical_to_main(env: Env, why: str) -> None:
    if why == "scope_missing":
        features.record_granted_scopes(["chat:write"])
    else:
        features.latch_unavailable(features.CODE_CHANNELS, "feature_disabled")
    _bind(env)
    handle = await _route(env, _plain("C0CODE"), is_mention=False)
    handle.assert_not_called()  # mention-mode channel, no mention: ignored as on main
    handle = await _route(env, mention(), is_mention=True)
    handle.assert_called_once()  # the normal in-place answer
    assert env.routed == [] and env.classified == [] and env.api.calls == []
    assert env.slack.posts == [] and env.slack.ephemerals == []


async def test_plain_message_in_a_code_channel_reaches_its_session(env: Env) -> None:
    _bind(env)
    handle = await _route(env, _plain("C0CODE"), is_mention=False)
    handle.assert_not_called()
    assert env.routed == [("C0CODE", "now add a regression test", OWNER)]


async def test_message_addressed_to_someone_else_is_left_alone(env: Env) -> None:
    _bind(env)
    handle = await _route(env, _plain("C0CODE", "<@UALICE> can you review?"), is_mention=False)
    handle.assert_not_called()
    assert env.routed == []


def _forward(note: str, body: str = "the login test fails on main") -> dict[str, Any]:
    """A message forwarded into the channel: Slack's share attachment names its author."""
    event = _plain("C0CODE", note)
    event["attachments"] = [{"is_share": True, "text": body, "author_id": "UBOB"}]
    return event


@pytest.mark.parametrize("note", ["", "please fix this", f"<@{SELF_USER}> please fix this"])
async def test_a_forward_into_a_code_channel_reaches_its_session(env: Env, note: str) -> None:
    """The assembled forward names its author (``<@UBOB>``): who the message is
    for is read from the sender's own note, never from the forwarded content."""
    _bind(env)
    handle = await _route(env, _forward(note), is_mention=bool(note))
    handle.assert_not_called()
    [(channel, text, sender)] = env.routed
    assert (channel, sender) == ("C0CODE", OWNER)
    assert "the login test fails on main" in text


async def test_a_forward_whose_note_is_for_someone_else_is_left_alone(env: Env) -> None:
    _bind(env)
    handle = await _route(env, _forward("<@UALICE> can you look?"), is_mention=False)
    handle.assert_not_called()
    assert env.routed == []


async def test_a_forward_of_a_message_mentioning_someone_else_still_reaches_the_session(
    env: Env,
) -> None:
    _bind(env)
    await _route(env, _forward("", body="<@UALICE> the build is red"), is_mention=False)
    assert len(env.routed) == 1


async def test_multi_step_mention_moves_into_a_new_channel(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    handle = await _route(env, mention(), is_mention=True)
    handle.assert_not_called()
    assert env.store.get_binding("C0NEW") is not None
    assert env.routed and env.routed[0][0] == "C0NEW"


async def _route_shared(env: Env, event: dict[str, Any], *, is_mention: bool) -> MagicMock:
    """:func:`_route` for a shared user (``slack.shared_access``): admitted, not the owner."""
    with (
        patch("kiro_crew.slack.events.is_shared_listed", return_value=True),
        patch("kiro_crew.slack.handler.is_shared_listed", return_value=True),
    ):
        return await _route(env, event, is_mention=is_mention)


async def test_a_shared_user_is_refused_in_a_bound_code_channel(env: Env) -> None:
    _bind(env)
    handle = await _route_shared(env, _plain("C0CODE"), is_mention=False)
    handle.assert_not_called()
    assert env.routed == []
    # Told once, privately (a member's messages are mostly not for the bot).
    from kiro_crew.slack.code_channels.shared_refusal import OWNER_ONLY_TEXT

    assert env.slack.ephemerals == [("C0CODE", OWNER, OWNER_ONLY_TEXT)]
    assert env.slack.posts == []


async def test_a_shared_users_dropped_message_in_a_code_channel_is_audited(env: Env) -> None:
    _bind(env)
    rows = MagicMock()
    with patch("kiro_crew.slack.events.sel", return_value=rows):
        handle = await _route_shared(env, _plain("C0CODE"), is_mention=False)
    handle.assert_not_called()
    denied = [
        c.kwargs for c in rows.log_api_access.call_args_list if c.kwargs.get("outcome") == "denied"
    ]
    assert denied == [
        {
            "caller": OWNER,
            "operation": "slack.message",
            "outcome": "denied",
            "source": "slack",
            "resources": "channel=C0CODE",
            "error": "code channel is owner-only",
        }
    ]


async def test_a_shared_user_cannot_answer_a_card_in_a_code_channel(env: Env) -> None:
    from kiro_crew.slack.code_channels.routing import options_answer_permitted

    _bind(env)
    assert options_answer_permitted("C0CODE", OWNER) is True
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        assert options_answer_permitted("C0CODE", OWNER) is False


async def test_a_shared_user_cannot_answer_a_card_in_a_bound_channel_with_the_feature_off(
    env: Env,
) -> None:
    from kiro_crew.slack.code_channels.routing import options_answer_permitted

    _bind(env)
    env.on = False
    assert options_answer_permitted("C0CODE", OWNER) is True
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        assert options_answer_permitted("C0CODE", OWNER) is False
        assert options_answer_permitted("C0PLAIN", OWNER) is True


async def test_the_code_channel_gate_itself_drops_a_shared_user(env: Env) -> None:
    from kiro_crew.slack.code_channels.routing import DROP, inbound_gate

    _bind(env)
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        result = await inbound_gate(
            _orch(env),
            _plain("C0CODE"),
            text="now add a regression test",
            sender_id=OWNER,
            authorized=False,
            from_trusted_bot=False,
        )
    assert result is DROP and env.routed == []


async def test_a_shared_users_mention_in_a_bound_channel_is_refused_with_the_feature_off(
    env: Env,
) -> None:
    # The owner turned Slack Code off and the binding (and its session) stayed.
    _bind(env)
    env.on = False
    event = {**_plain("C0CODE", f"<@{SELF_USER}> now add a regression test")}
    handle = await _route_shared(env, event, is_mention=True)
    handle.assert_not_called()
    assert env.routed == []


async def test_a_shared_users_linked_thread_reply_is_refused_with_no_slot_and_the_feature_off(
    env: Env,
) -> None:
    from kiro_crew.slack.handler_runtime.inbound import LinkedRoute, route_linked_thread

    _bind(env)
    env.on = False
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        outcome = await route_linked_thread(
            "now add a regression test",
            "slack:300.000001",
            OWNER,
            "C0CODE",
            env.slack,
            "300.000001",
        )
    assert outcome is LinkedRoute.REFUSED


async def test_a_revoked_channel_stays_the_owners_with_the_feature_off(env: Env) -> None:
    # Slack stopped listing the bot there; the anchor thread still leads to the session.
    from kiro_crew.slack.code_channels.routing import options_answer_permitted
    from kiro_crew.slack.handler_runtime.inbound import LinkedRoute, route_linked_thread

    _bind(env)
    env.store.drop_binding("C0CODE")
    assert env.store.was_revoked("C0CODE")
    env.on = False
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        assert options_answer_permitted("C0CODE", OWNER) is False
        outcome = await route_linked_thread(
            "now add a regression test",
            "slack:300.000001",
            OWNER,
            "C0CODE",
            env.slack,
            "300.000001",
        )
    assert outcome is LinkedRoute.REFUSED


async def test_a_shared_users_thread_stop_in_a_code_channel_is_refused(env: Env) -> None:
    from kiro_crew.slack.agent_sessions.session_events import _shared_user_in_code_channel

    _bind(env)
    env.on = False
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        assert await _shared_user_in_code_channel(OWNER, "C0CODE") is True
        assert await _shared_user_in_code_channel(OWNER, "D0DM00001") is False
    assert await _shared_user_in_code_channel(OWNER, "C0CODE") is False


async def test_a_shared_users_inline_stop_is_refused_in_a_revoked_channel_but_not_a_dm(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.interactions_sessions import _outside_code_channel

    _bind(env)
    env.store.drop_binding("C0CODE")
    assert await _outside_code_channel("C0CODE") is False
    monkeypatch.setattr(type(env.store), "unreadable", property(lambda self: True))
    assert await _outside_code_channel("D0DM00001") is True


def test_an_unreadable_store_refuses_a_shared_user_in_any_channel_that_could_be_one(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.code_channels.routing import shared_user_refused_in

    monkeypatch.setattr(type(env.store), "unreadable", property(lambda self: True))
    assert shared_user_refused_in("C0PLAIN") is True
    assert shared_user_refused_in("D0DM00001") is False


async def test_an_unconfirmed_shared_users_mention_never_opens_a_code_channel(env: Env) -> None:
    # Listed, but Slack does not confirm them (removed, or the check failed):
    # their request opens nothing, so no channel can record them as its opener.
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    handle = await _route_shared(env, mention(), is_mention=True)
    # Answered in place on the normal path, as without code channels.
    handle.assert_called_once()
    assert env.store.get_binding("C0NEW") is None and env.routed == []
    assert _calls_to(env, "agents.conversations.create") == []


# ── A shared user's own code channel (slack.shared_access, its opener) ──────

SHARED = "U0SHARED01"
OTHER_SHARED = "U0SHARED02"


def _confirmed(*users: str) -> Any:
    """*users* listed and confirmed as shared users, now (never the owner)."""
    from contextlib import ExitStack

    stack = ExitStack()
    for target in (
        "kiro_crew.slack.events.is_shared_listed",
        "kiro_crew.slack.handler.is_shared_listed",
        "kiro_crew.slack.handler.is_shared_user",
    ):
        stack.enter_context(patch(target, lambda u: u in users))
    stack.enter_context(
        patch(
            "kiro_crew.slack.shared_access.still_confirmed",
            lambda _cfg, u, *, owner: not owner and u in users,
        )
    )
    return stack


def _calls_to(env: Env, method: str) -> list[Any]:
    return [body for m, body in env.api.calls if m == method]


def _bind_opened_by(env: Env, opener: str) -> None:
    env.store.put_binding(
        Binding(
            channel_id="C0CODE",
            team_id="T1",
            session_key="chat-9-1",
            anchor_ts="300.000001",
            opener=opener,
        )
    )
    env.info("C0CODE")


async def test_a_shared_users_coding_mention_opens_their_own_code_channel(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    with _confirmed(SHARED):
        await _route(env, {**mention(), "user": SHARED}, is_mention=True)
    assert env.classified, "the classifier decides for a shared user's request too"
    binding = env.store.get_binding("C0NEW")
    assert binding is not None and binding.opener == SHARED
    assert [r.opener for r in env.store._routes.values()] == [SHARED]


async def test_the_owners_code_channel_records_no_opener(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    with _confirmed(SHARED):
        await _route(env, mention(), is_mention=True)
    binding = env.store.get_binding("C0NEW")
    assert binding is not None and binding.opener == ""


async def test_the_opener_drives_their_own_code_channel(env: Env) -> None:
    _bind_opened_by(env, SHARED)
    with _confirmed(SHARED):
        handle = await _route(env, {**_plain("C0CODE"), "user": SHARED}, is_mention=False)
    handle.assert_not_called()  # the channel's session answers, not a thread session
    assert len(env.routed) == 1 and env.slack.ephemerals == []


async def test_another_shared_user_stays_out_of_the_openers_channel(env: Env) -> None:
    from kiro_crew.slack.code_channels.shared_refusal import OWNER_ONLY_TEXT

    _bind_opened_by(env, SHARED)
    with _confirmed(SHARED, OTHER_SHARED):
        handle = await _route(env, {**_plain("C0CODE"), "user": OTHER_SHARED}, is_mention=False)
    handle.assert_not_called()
    assert env.routed == []
    assert env.slack.ephemerals == [("C0CODE", OTHER_SHARED, OWNER_ONLY_TEXT)]


async def test_an_opener_removed_while_their_message_routes_appends_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The route awaits (a window load, an image copy) before it appends: the gate it
    # asks at that commit point judges the opener again, with nothing awaited after.
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import LinkedRoute

    _bind_opened_by(env, SHARED)
    users = {SHARED}
    gates: list[bool] = []

    async def route_to_slot(_orch: Any, binding: Any, text: str, sender_id: str, **kw: Any) -> Any:
        admit = kw.get("admit")
        gates.append(admit is not None and await admit())
        users.clear()  # the owner removes them during the route's awaits
        gates.append(admit is not None and await admit())
        return LinkedRoute.HELD

    monkeypatch.setattr(routing, "_route_to_slot", route_to_slot)
    with (
        patch("kiro_crew.slack.events.is_shared_listed", lambda u: u == SHARED),
        patch("kiro_crew.slack.handler.is_shared_listed", lambda u: u == SHARED),
        patch("kiro_crew.slack.handler.is_shared_user", lambda u: u in users),
        patch(
            "kiro_crew.slack.shared_access.still_confirmed",
            lambda _cfg, u, *, owner: not owner and u in users,
        ),
    ):
        await _route(env, {**_plain("C0CODE"), "user": SHARED}, is_mention=False)
    assert gates == [True, False]


async def test_the_openers_first_turn_is_judged_at_its_commit_point(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.handler import LinkedRoute

    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    users = {SHARED}
    gates: list[bool] = []

    async def route_to_slot(_orch: Any, binding: Any, text: str, sender_id: str, **kw: Any) -> Any:
        admit = kw.get("admit")
        gates.append(admit is not None and await admit())
        users.clear()  # removed during the first turn's route
        gates.append(admit is not None and await admit())
        return LinkedRoute.HELD

    monkeypatch.setattr(routing, "_route_to_slot", route_to_slot)
    with _confirmed_live(users):
        await _route(env, {**mention(), "user": SHARED}, is_mention=True)
    assert gates == [True, False]


async def test_an_opener_removed_while_classified_opens_no_channel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.code_channels import routing

    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    users = {SHARED}
    real = routing.classify

    async def classify_then_remove(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        users.clear()
        return out

    monkeypatch.setattr(routing, "classify", classify_then_remove)
    with _confirmed_live(users):
        await _route(env, {**mention(), "user": SHARED}, is_mention=True)
    assert _calls_to(env, "agents.conversations.create") == []
    assert env.store.get_binding("C0NEW") is None


async def test_an_opener_removed_while_their_channel_binds_gets_no_session(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Removed while the bind posts its kickoff: the binding write judges the opener
    # again and takes the session back down, so nothing is bound to the channel.
    from kiro_crew.slack.code_channels import routing

    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    users = {SHARED}
    real = routing._post_kickoff

    async def kickoff_then_remove(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        users.clear()
        return out

    monkeypatch.setattr(routing, "_post_kickoff", kickoff_then_remove)
    with _confirmed_live(users):
        await _route(env, {**mention(), "user": SHARED}, is_mention=True)
    assert env.store.get_binding("C0NEW") is None and env.routed == []


async def test_an_openers_rebind_never_undoes_a_revocation_made_meanwhile(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The channel's dashboard session was closed, so the opener's message rebinds it.
    # Slack revokes the channel while the rebind posts its kickoff: the replacement
    # lands only over the binding it replaces, so the channel stays unbound.
    from kiro_crew.slack.code_channels import routing

    _bind_opened_by(env, SHARED)
    env.state.linked.clear()  # no live slot: the session was closed
    real = routing._post_kickoff

    async def kickoff_then_revoke(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        env.store.drop_binding("C0CODE")
        return out

    monkeypatch.setattr(routing, "_post_kickoff", kickoff_then_revoke)
    with _confirmed(SHARED):
        await _route(env, {**_plain("C0CODE"), "user": SHARED}, is_mention=False)
    assert env.store.get_binding("C0CODE") is None and env.store.was_revoked("C0CODE")
    assert env.routed == []


@pytest.mark.parametrize(
    "case",
    [
        "opener",
        "destination_unsaved",
        "other",
        "removed",
        "bind_failed",
        "bind_failed_no_destination",
    ],
)
async def test_the_openers_message_before_the_binding_is_stored_waits_for_it(
    env: Env, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # Slack created the channel and invited the opener; the binding is not written
    # yet (worktree cut, kickoff), or its first bind failed and they were told to
    # retry. The opener's message passes the gate (to wait for, or adopt, the
    # binding); nobody else's does, and no route of ours means no admission.
    import asyncio as aio

    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.code_channels.routing import DROP, Gate
    from kiro_crew.slack.code_channels.store import ROUTE_CODE_CHANNEL, Route

    origin, ts = "C0ORIGIN", "1700000000.000100"
    sid = routing.session_id_for(routing._self_bot_id(), "T1", origin, ts)
    env.store.record_decision(
        Route(
            session_id=sid,
            origin_channel_id=origin,
            origin_ts=ts,
            team_id="T1",
            decision=ROUTE_CODE_CHANNEL,
            opener=SHARED,
        )
    )
    if case not in ("destination_unsaved", "bind_failed_no_destination"):
        env.store.record_destination(sid, "C0NEW")  # else its write never landed
    env.info("C0NEW", origin=(origin, ts))
    if not case.startswith("bind_failed"):  # still opening in this process
        routing._creating[sid] = aio.Event()
        monkeypatch.setitem(routing._opening, "C0NEW", sid)
    sender = OTHER_SHARED if case == "other" else SHARED
    event = {**_plain("C0NEW", f"<@{SELF_USER}> also fix the header"), "user": sender}
    # Removed: still listed for the owner-only gates, not confirmed now.
    people = _confirmed_live(set()) if case == "removed" else _confirmed(SHARED, OTHER_SHARED)
    try:
        with people:
            result = await routing.inbound_gate(
                env.orch,
                event,
                text=event["text"],
                sender_id=sender,
                authorized=True,
                from_trusted_bot=False,
            )
    finally:
        routing._creating.pop(sid, None)
    if case in ("opener", "destination_unsaved", "bind_failed"):
        assert result.gate is Gate.DIRECTED and env.slack.ephemerals == []
    else:
        assert result is DROP


async def test_a_retry_after_a_failed_first_bind_keeps_the_channels_opener(env: Env) -> None:
    # The first bind failed after Slack created the channel; the opener (or the owner)
    # mentions the bot there to retry. The adopted binding keeps the route's opener.
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.code_channels.store import ROUTE_CODE_CHANNEL, Route

    origin, ts = "C0ORIGIN", "1700000000.000100"
    sid = routing.session_id_for(routing._self_bot_id(), "T1", origin, ts)
    env.store.record_decision(
        Route(
            session_id=sid,
            origin_channel_id=origin,
            origin_ts=ts,
            team_id="T1",
            decision=ROUTE_CODE_CHANNEL,
            opener=SHARED,
        )
    )
    env.store.record_destination(sid, "C0NEW")
    env.info("C0NEW", origin=(origin, ts))
    event = {**_plain("C0NEW", f"<@{SELF_USER}> retry"), "user": SHARED}
    with _confirmed(SHARED):
        await _route(env, event, is_mention=True)
    binding = env.store.get_binding("C0NEW")
    assert binding is not None, (env.slack.posts, env.slack.ephemerals, env.sel.rows[-4:])
    assert binding.opener == SHARED
    assert env.routed and env.routed[-1][2] == SHARED


@pytest.mark.parametrize("sender", [OWNER, SHARED])
async def test_an_unmentioned_message_while_the_channel_opens_waits_for_its_binding(
    env: Env, monkeypatch: pytest.MonkeyPatch, sender: str
) -> None:
    # The channel exists in Slack and its binding is being written: a plain message
    # there (no mention, as a code channel needs none) reaches the gate instead of
    # the mention-mode drop, for the owner and for the channel's opener alike.
    import asyncio as aio

    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.code_channels.store import ROUTE_CODE_CHANNEL, Route

    origin, ts = "C0ORIGIN", "1700000000.000100"
    sid = routing.session_id_for(routing._self_bot_id(), "T1", origin, ts)
    opener = SHARED if sender == SHARED else ""
    env.store.record_decision(
        Route(
            session_id=sid,
            origin_channel_id=origin,
            origin_ts=ts,
            team_id="T1",
            decision=ROUTE_CODE_CHANNEL,
            opener=opener,
        )
    )
    env.store.record_destination(sid, "C0NEW")
    env.info("C0NEW", origin=(origin, ts))
    # Still opening (the barrier and the channel are recorded); its event is already
    # set, so the message's wait for the first turn returns at once.
    routing._creating[sid] = aio.Event()
    routing._creating[sid].set()
    monkeypatch.setitem(routing._opening, "C0NEW", sid)
    gated: list[str] = []
    real = routing.inbound_gate

    async def gate(*a: Any, **k: Any) -> Any:
        gated.append(k.get("sender_id", ""))
        return await real(*a, **k)

    monkeypatch.setattr("kiro_crew.slack.events.code_channel_gate", gate)
    event = {**_plain("C0NEW", "also fix the header"), "user": sender}
    try:
        with _confirmed(SHARED):
            await _route(env, event, is_mention=False)
    finally:
        routing._creating.pop(sid, None)
    assert gated == [sender]


def _confirmed_live(users: set[str]) -> Any:
    """Like :func:`_confirmed`, read from *users* at each call (a test can remove one)."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("kiro_crew.slack.events.is_shared_listed", lambda u: u == SHARED))
    stack.enter_context(patch("kiro_crew.slack.handler.is_shared_listed", lambda u: u == SHARED))
    stack.enter_context(patch("kiro_crew.slack.handler.is_shared_user", lambda u: u in users))
    stack.enter_context(
        patch(
            "kiro_crew.slack.shared_access.still_confirmed",
            lambda _cfg, u, *, owner: not owner and u in users,
        )
    )
    return stack


@pytest.mark.parametrize("meanwhile", ["nothing", "revoked", "rebound"])
async def test_the_openers_bang_stop_is_judged_on_the_binding_stored_when_it_stops(
    env: Env, monkeypatch: pytest.MonkeyPatch, meanwhile: str
) -> None:
    from dataclasses import replace

    from kiro_crew.slack.code_channels import routing

    _bind_opened_by(env, SHARED)
    stops: list[str] = []

    async def stop_bound(_orch: Any, binding: Any, sender: str, channel: str, **_k: Any) -> str:
        stops.append(binding.session_key)
        return ""

    real = routing.recognition.classify_channel

    async def classify(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        if meanwhile == "revoked":
            env.store.drop_binding("C0CODE")  # Slack revoked it during the lookup
        elif meanwhile == "rebound":  # its closed session was remade meanwhile
            env.store.replace_binding(
                replace(env.store.get_binding("C0CODE"), session_key="chat-10-1")
            )
        return out

    monkeypatch.setattr(routing, "_stop_bound", stop_bound)
    monkeypatch.setattr(routing.recognition, "classify_channel", classify)
    with _confirmed(SHARED):
        await _route(env, {**_plain("C0CODE", "!stop"), "user": SHARED}, is_mention=False)
    assert stops == {"nothing": ["chat-9-1"], "revoked": [], "rebound": ["chat-10-1"]}[meanwhile]


@pytest.mark.parametrize("replace_it", [False, True])
def test_a_binding_write_refused_in_the_stores_lock_writes_nothing(
    env: Env, replace_it: bool
) -> None:
    from dataclasses import replace

    if replace_it:
        _bind_opened_by(env, SHARED)
        old = env.store.get_binding("C0CODE")
        new = replace(old, session_key="chat-10-1")
        assert env.store.replace_binding(new, admit=lambda: False) is None
        assert env.store.get_binding("C0CODE") == old
        stale = replace(new, session_key="chat-11-1")
        assert env.store.replace_binding(stale, expected_session="chat-10-1") is None
        assert env.store.replace_binding(new, admit=lambda: True) is not None
    else:
        new = Binding(channel_id="C0NEW", team_id="T1", session_key="chat-1-1", anchor_ts="3.1")
        assert env.store.put_binding(new, admit=lambda: False) is None
        assert env.store.get_binding("C0NEW") is None


async def test_an_openers_in_flight_retry_never_restores_a_revoked_channel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The opener's retry after a failed first bind passed the gate; Slack revokes the
    # channel (another message unbinds it) while the bind posts its kickoff. The
    # adopt's write refuses in the store's lock, so the revocation stands.
    from kiro_crew.slack.code_channels import routing
    from kiro_crew.slack.code_channels.store import ROUTE_CODE_CHANNEL, Route

    origin, ts = "C0ORIGIN", "1700000000.000100"
    sid = routing.session_id_for(routing._self_bot_id(), "T1", origin, ts)
    env.store.record_decision(
        Route(
            session_id=sid,
            origin_channel_id=origin,
            origin_ts=ts,
            team_id="T1",
            decision=ROUTE_CODE_CHANNEL,
            opener=SHARED,
        )
    )
    env.store.record_destination(sid, "C0NEW")
    env.info("C0NEW", origin=(origin, ts))
    real = routing._post_kickoff

    async def kickoff_then_revoke(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        # What drop_binding records for a channel Slack stopped listing us in.
        env.store.put_binding(
            Binding(channel_id="C0NEW", team_id="T1", session_key="chat-x", anchor_ts="9.9")
        )
        env.store.drop_binding("C0NEW")
        return out

    monkeypatch.setattr(routing, "_post_kickoff", kickoff_then_revoke)
    event = {**_plain("C0NEW", f"<@{SELF_USER}> retry"), "user": SHARED}
    with _confirmed(SHARED):
        await _route(env, event, is_mention=True)
    assert env.store.get_binding("C0NEW") is None and env.store.was_revoked("C0NEW")
    assert env.routed == []


async def test_a_shared_user_stays_out_of_the_owners_code_channel(env: Env) -> None:
    _bind_opened_by(env, "")
    with _confirmed(SHARED):
        handle = await _route(env, {**_plain("C0CODE"), "user": SHARED}, is_mention=False)
    handle.assert_not_called()
    assert env.routed == []


async def test_an_opener_removed_from_shared_access_is_cut_off(env: Env) -> None:
    # Still "listed" for the owner-only gates (an admission earlier in this process),
    # but not a confirmed shared user now: the channel does not admit them.
    _bind_opened_by(env, SHARED)
    with (
        patch("kiro_crew.slack.events.is_shared_listed", lambda u: u == SHARED),
        patch("kiro_crew.slack.handler.is_shared_listed", lambda u: u == SHARED),
        patch("kiro_crew.slack.handler.is_shared_user", lambda u: False),
        patch("kiro_crew.slack.shared_access.still_confirmed", lambda *_a, **_k: False),
    ):
        handle = await _route(env, {**_plain("C0CODE"), "user": SHARED}, is_mention=False)
    handle.assert_not_called()
    assert env.routed == []


async def test_a_thread_elsewhere_linked_to_the_openers_channel_session_is_refused(
    env: Env,
) -> None:
    from kiro_crew.slack.code_channels.routing import shared_user_refused_for_slot

    _bind_opened_by(env, SHARED)
    env.store.record_worker("chat-worker1", "chat-9-1")
    with _confirmed(SHARED):
        # In the channel itself: its own session and anchor.
        assert (
            shared_user_refused_for_slot("chat-9-1", "", sender=SHARED, channel="C0CODE") is False
        )
        assert (
            shared_user_refused_for_slot("", "300.000001", sender=SHARED, channel="C0CODE") is False
        )
        # Anywhere else (a DM thread linked or relinked to that session), or a
        # helper agent its session started, or someone else: refused.
        assert shared_user_refused_for_slot("chat-9-1", "", sender=SHARED, channel="D0DM00001")
        assert shared_user_refused_for_slot("chat-worker1", "", sender=SHARED, channel="C0CODE")
        assert shared_user_refused_for_slot("chat-9-1", "", sender=OTHER_SHARED, channel="C0CODE")
        assert shared_user_refused_for_slot("chat-9-1", "")


def test_a_binding_row_written_before_openers_reads_as_the_owners(env: Env) -> None:
    import json

    from kiro_crew.slack.code_channels.routing import shared_user_refused_in
    from kiro_crew.slack.code_channels.store import CodeChannelStore, set_store

    row = {"channel_id": "C0CODE", "team_id": "T1", "session_key": "chat-9-1", "anchor_ts": "3.1"}
    env.store.path.write_text(json.dumps({"bindings": {"C0CODE": row}}), encoding="utf-8")
    loaded = CodeChannelStore(env.store.path)
    loaded.load()
    set_store(loaded)
    binding = loaded.get_binding("C0CODE")
    assert binding is not None and binding.opener == ""
    with _confirmed(SHARED):
        assert shared_user_refused_in("C0CODE", sender=SHARED) is True


async def test_one_line_question_is_answered_in_place(env: Env) -> None:
    env.decision = RouteDecision("stay")
    handle = await _route(
        env, {**mention(), "text": f"<@{SELF_USER}> what does CI run?"}, is_mention=True
    )
    handle.assert_called_once()
    assert env.routed == []


# ── An unmentioned follow-up in a thread the bot already answers ─────────────
#
# Build 9: "how you doing" (a mention) was answered in place, then "grab an
# issue from the backlog" in the same thread, with no mention, ran in place in
# the thread session instead of reaching the classifier.

ROOT_TS = "1700000000.000100"
FOLLOW_UP = "grab an issue from the backlog and fix it"


def _follow_up(ts: str = "1700000000.000200", text: str = FOLLOW_UP) -> dict[str, Any]:
    return {
        "type": "message",
        "user": OWNER,
        "channel": "C0ORIGIN",
        "ts": ts,
        "thread_ts": ROOT_TS,
        "text": text,
        "team": "T1",
    }


async def _route_in_bot_thread(env: Env, event: dict[str, Any]) -> MagicMock:
    """Route *event* with the bot holding a session for the ROOT_TS thread."""
    orch = _orch(env)
    orch.sessions.has_session = MagicMock(side_effect=lambda ts: ts == ROOT_TS)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as handle:
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await _route_message(orch, event, SeenCache(), is_mention=False)
            await asyncio.sleep(0)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
    return handle


async def test_unmentioned_follow_up_in_a_bot_thread_is_classified(env: Env) -> None:
    env.decision = RouteDecision("stay")
    handle = await _route_in_bot_thread(env, _follow_up())
    assert env.classified == [FOLLOW_UP]
    # A stay decision answers it in place in the thread session, as before.
    handle.assert_called_once()
    assert env.routed == []
    assert "agents.conversations.create" not in env.api.methods()


async def test_follow_up_that_opens_a_code_channel_is_not_answered_in_the_thread(
    env: Env,
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    event = _follow_up()
    handle = await _route_in_bot_thread(env, event)
    handle.assert_not_called()  # the old thread session stays silent
    [(channel, seed, sender)] = env.routed
    assert channel == "C0NEW" and seed.startswith(FOLLOW_UP) and sender == OWNER
    [body] = [b for m, b in env.api.calls if m == "agents.conversations.create"]
    assert body["origin_message_ts"] == event["ts"]  # session id from the follow-up's ts
    assert ("C0ORIGIN", "Opened <#C0NEW> for this. I'll work there.", ROOT_TS) in env.slack.posts
    binding = env.store.get_binding("C0NEW")
    assert binding is not None and binding.origin_thread_ts == ROOT_TS


async def test_a_thread_that_opened_a_channel_is_not_classified_again(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    await _route_in_bot_thread(env, _follow_up())
    assert len(env.classified) == 1
    handle = await _route_in_bot_thread(env, _follow_up(ts="1700000000.000300", text="and v2"))
    assert len(env.classified) == 1  # no second model call for this thread
    handle.assert_called_once()  # answered in the thread as before
    assert len([m for m, _ in env.api.calls if m == "agents.conversations.create"]) == 1


async def test_the_opening_follow_up_redelivered_after_a_restart_is_not_answered_in_place(
    env: Env,
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    await _route_in_bot_thread(env, _follow_up())
    # A fresh dedup cache: Slack re-delivers the same event after a restart.
    handle = await _route_in_bot_thread(env, _follow_up())
    handle.assert_not_called()
    assert len(env.classified) == 1 and len(env.routed) == 1
