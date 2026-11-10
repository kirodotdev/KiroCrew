"""Shared Slack access (``slack.shared_access``): off by default, owner-only stays owner-only.

The switch admits the Slack users the owner lists, once Slack confirms each one,
to drive work as the owner. With it off, Slack answers the owner alone exactly as
before, and the old ``slack.allowed_users`` list admits nobody either way. A shared
user answers only their own turn's tool prompts, never grants Trust, and reaches
no owner-only command, button or keyword.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_shared_access, _build_slack_config
from kiro_crew.config.sections_slack import SlackConfig, is_slack_user_id, shared_user_ids
from kiro_crew.slack import handler, interactions, shared_access
from kiro_crew.slack.handler import _pending_approvals, _PendingApproval, handle_interaction

OWNER = "UOWNER0001"
FRIEND = "UFRIEND001"
OTHER = "UOTHER0001"
TEAM = "T0000TEAM1"


class _SlackError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.response = {"error": error}


class FakeWeb:
    """``auth.test`` / ``users.info`` as Slack answers them, recording each call."""

    def __init__(self, users: dict[str, dict[str, Any]], team: str = TEAM) -> None:
        self.users = users
        self.team = team
        self.calls: list[str] = []
        self.down = False

    async def auth_test(self) -> dict[str, Any]:
        if self.down:
            raise OSError("network down")
        return {"team_id": self.team}

    async def users_info(self, user: str) -> dict[str, Any]:
        self.calls.append(user)
        if user not in self.users:
            raise _SlackError("user_not_found")
        return {"user": self.users[user]}


def _person(team: str = TEAM, **extra: Any) -> dict[str, Any]:
    return {"team_id": team, "name": "friend", "profile": {"display_name": "Friend"}, **extra}


def _cfg(enabled: bool = True, ids: tuple[str, ...] = (FRIEND,), allowed: tuple[str, ...] = ()):
    data = {
        "shared_access": {"enabled": enabled, "user_ids": list(ids)},
        "allowed_users": [{"slack_id": u, "name": ""} for u in allowed],
    }
    cfg = KiroCrewConfig()
    cfg.slack = _build_slack_config(data)
    return cfg


@pytest.fixture(autouse=True)
def _gateway(_floor_monkeypatch):
    web = FakeWeb({FRIEND: _person(), OTHER: _person()})
    shared_access.install(web)
    _floor_monkeypatch.setattr(handler, "_owner_id", OWNER)
    _floor_monkeypatch.setattr(handler, "_orch_cfg", _cfg())
    yield web
    shared_access.reset()


def _use(_floor_monkeypatch, cfg) -> None:
    _floor_monkeypatch.setattr(handler, "_orch_cfg", cfg)


# ── the setting ──────────────────────────────────────────────────────────────


def test_shared_access_is_off_and_empty_by_default() -> None:
    assert SlackConfig().shared_access.enabled is False
    assert SlackConfig().shared_access.user_ids == []
    assert _build_slack_config({}).shared_access.enabled is False


@pytest.mark.parametrize("value", ["true", 1, "yes", None, {}])
def test_only_a_literal_true_turns_it_on(value: object) -> None:
    assert _build_shared_access({"enabled": value}).enabled is False
    assert _build_shared_access({"enabled": True}).enabled is True


def test_stored_ids_are_trimmed_well_formed_and_deduped() -> None:
    raw = ["U123", " W45 ", "B999", "T111", "u12", "U123", 7, "U" + "1" * 30]
    assert shared_user_ids(raw) == ["U123", "W45"]
    assert shared_user_ids("U123") == []
    assert not is_slack_user_id("U") and is_slack_user_id("W0ABC")


# ── who is admitted ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_off_keeps_slack_owner_only(_floor_monkeypatch) -> None:
    await shared_access.prime(_cfg(), FRIEND, owner_id=OWNER)  # confirmed while on
    _use(_floor_monkeypatch, _cfg(enabled=False))
    assert handler.is_allowed_user(OWNER) is True
    assert handler.is_shared_user(FRIEND) is False
    assert handler.is_shared_user(FRIEND) is False


@pytest.mark.asyncio
async def test_switch_off_checks_nobody_with_slack(_gateway, _floor_monkeypatch) -> None:
    _use(_floor_monkeypatch, _cfg(enabled=False))
    await shared_access.prime_shared_user(FRIEND)
    assert _gateway.calls == []


@pytest.mark.asyncio
async def test_a_stale_allowed_users_entry_is_never_admitted(_floor_monkeypatch) -> None:
    _use(_floor_monkeypatch, _cfg(ids=(FRIEND,), allowed=(OTHER,)))
    await shared_access.prime_shared_user(OTHER)
    assert handler.is_allowed_user(OTHER) is False


@pytest.mark.asyncio
async def test_a_listed_person_is_admitted_once_slack_confirms_them() -> None:
    assert handler.is_shared_user(FRIEND) is False  # not checked yet
    await shared_access.prime_shared_user(FRIEND)
    assert handler.is_shared_user(FRIEND) is True
    assert handler.is_shared_user(FRIEND) is True
    assert handler.is_shared_user(OWNER) is False
    assert handler.is_owner(FRIEND) is False


@pytest.mark.parametrize(
    "user, reason",
    [
        (_person(is_bot=True), shared_access.REASON_BOT),
        (_person(deleted=True), shared_access.REASON_DEACTIVATED),
        (_person(team="T0000OTHER"), shared_access.REASON_OTHER_TEAM),
        (_person(team=""), shared_access.REASON_OTHER_TEAM),
    ],
)
@pytest.mark.asyncio
async def test_slack_must_confirm_a_person_in_the_bots_workspace(
    _gateway, user: dict, reason: str
) -> None:
    _gateway.users[FRIEND] = user
    await shared_access.prime_shared_user(FRIEND)
    assert handler.is_shared_user(FRIEND) is False
    verdict = shared_access._verdicts.get(FRIEND)
    assert verdict is not None and verdict.reason == reason


@pytest.mark.asyncio
async def test_an_unknown_id_and_an_unreachable_slack_admit_nobody(
    _gateway, _floor_monkeypatch
) -> None:
    _use(_floor_monkeypatch, _cfg(ids=("UNOBODY001", FRIEND)))
    await shared_access.prime_shared_user("UNOBODY001")
    assert shared_access._verdicts.get("UNOBODY001").reason == shared_access.REASON_NOT_FOUND
    _gateway.down = True
    await shared_access.prime_shared_user(FRIEND)
    assert shared_access._verdicts.get(FRIEND).reason == shared_access.REASON_UNVERIFIABLE
    assert handler.is_shared_user(FRIEND) is False


@pytest.mark.asyncio
async def test_the_owner_on_the_list_is_never_a_shared_user(_floor_monkeypatch) -> None:
    alias = "W" + OWNER[1:]
    _use(_floor_monkeypatch, _cfg(ids=(OWNER, alias)))
    verdict = await shared_access.check(shared_access._client, alias, owner_id=OWNER)
    assert verdict.reason == shared_access.REASON_OWNER
    assert handler.is_shared_user(OWNER) is False and handler.is_shared_user(alias) is False


@pytest.mark.asyncio
async def test_a_removal_and_a_switch_off_apply_to_the_next_event(_floor_monkeypatch) -> None:
    await shared_access.prime_shared_user(FRIEND)
    assert handler.is_shared_user(FRIEND) is True
    _use(_floor_monkeypatch, _cfg(ids=()))
    assert handler.is_shared_user(FRIEND) is False
    _use(_floor_monkeypatch, _cfg())
    assert handler.is_shared_user(FRIEND) is True
    _use(_floor_monkeypatch, _cfg(enabled=False))
    assert handler.is_shared_user(FRIEND) is False


@pytest.mark.asyncio
async def test_a_confirmation_is_asked_again_once_old(_gateway, _floor_monkeypatch) -> None:
    now = [1000.0]
    _floor_monkeypatch.setattr(shared_access, "clock", lambda: now[0])
    await shared_access.prime_shared_user(FRIEND)
    await shared_access.prime_shared_user(FRIEND)
    assert _gateway.calls == [FRIEND]
    _gateway.users[FRIEND] = _person(deleted=True)
    now[0] += shared_access.CONFIRMED_TTL_SECS
    await shared_access.prime_shared_user(FRIEND)
    assert handler.is_shared_user(FRIEND) is False


# ── tool approvals ───────────────────────────────────────────────────────────


class _Provider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


async def _confirmed_friend() -> None:
    await shared_access.prime_shared_user(FRIEND)
    assert handler.is_shared_user(FRIEND)


@pytest.mark.asyncio
async def test_a_shared_user_answers_their_own_prompt() -> None:
    await _confirmed_friend()
    prov = _Provider()
    _pending_approvals["C1:ts1"] = _PendingApproval(prov, "req-1", "s1", requester=FRIEND)
    assert await handle_interaction("C1", "ts1", "approve_tool", user_id=FRIEND) == "approve_tool"
    assert prov.approved == ["req-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", [OWNER, OTHER, ""])
async def test_a_shared_user_cannot_answer_someone_elses_prompt(requester: str) -> None:
    await _confirmed_friend()
    prov = _Provider()
    _pending_approvals["C1:ts1"] = _PendingApproval(prov, "req-1", "s1", requester=requester)
    for action in ("approve_tool", "reject_tool"):
        assert await handle_interaction("C1", "ts1", action, user_id=FRIEND) is None
    assert prov.approved == [] and prov.rejected == []
    assert "C1:ts1" in _pending_approvals  # still pending for its owner
    assert await handle_interaction("C1", "ts1", "approve_tool", user_id=OWNER) == "approve_tool"


@pytest.mark.asyncio
async def test_a_shared_user_never_grants_trust() -> None:
    await _confirmed_friend()
    prov = _Provider()
    _pending_approvals["C1:ts1"] = _PendingApproval(prov, "req-1", "s1", requester=FRIEND)
    assert await handle_interaction("C1", "ts1", "trust_tool", user_id=FRIEND) is None
    assert not handler.is_slack_session_trusted("s1")
    # A late Trust click (the prompt already answered) is refused too.
    slack = SimpleNamespace(fetch_thread_replies=AsyncMock(return_value=[{"user": FRIEND}]))
    assert (
        await handle_interaction(
            "C1", "ts-gone", "trust_tool", user_id=FRIEND, thread_ts="t1", slack=slack
        )
        is None
    )
    assert not handler.is_slack_session_trusted("t1")


@pytest.mark.asyncio
async def test_a_shared_user_cannot_answer_a_transport_prompt_they_did_not_ask(
    _floor_monkeypatch,
) -> None:
    from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX, SlackApprovalDecider

    await _confirmed_friend()
    resolved: list[str] = []
    _floor_monkeypatch.setattr(
        SlackApprovalDecider,
        "resolve_global",
        classmethod(lambda cls, key, approved, nonce="": resolved.append(key) or True),
    )
    _floor_monkeypatch.setattr(interactions, "_orch", None)
    payload = {
        "type": "block_actions",
        "user": {"id": FRIEND},
        "channel": {"id": "D1"},
        "message": {"ts": "m1"},
        "actions": [{"action_id": TOOL_APPROVE_ACTION_PREFIX + "x", "value": "s1:7"}],
    }
    await interactions.dispatch(payload)
    assert resolved == []
    _floor_monkeypatch.setitem(
        SlackApprovalDecider._REGISTRY, "s1:7", SlackApprovalDecider("s1", requester=FRIEND)
    )
    await interactions.dispatch(payload)
    assert resolved == ["s1:7"]


# ── owner-only controls ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "action, permitted",
    [
        ("approve_tool", True),
        ("reject_tool", True),
        ("mc_tool_approve_x", True),
        ("options_choice_1", True),
        ("mc_stop_confirm", True),
        ("mc_inline_stop_k", True),
        ("mc_ch_add", False),
        ("mc_ch_activation_C1", False),
        ("mc_dashboard_copy", False),
        ("mc_session_resume_x", False),
        ("allowlist_approve", False),
        ("mc_review_approve", True),
        ("mc_review_cancel", True),
        ("mc_agent_select", False),
        ("mc_allowlist_remove_U1", False),
    ],
)
def test_a_shared_user_reaches_only_their_own_buttons(action: str, permitted: bool) -> None:
    assert shared_access.shared_action_permitted(action) is permitted


@pytest.mark.asyncio
async def test_an_owner_only_button_is_refused_with_a_plain_reply(_floor_monkeypatch) -> None:
    await _confirmed_friend()
    slack = SimpleNamespace(post_ephemeral=AsyncMock())
    _floor_monkeypatch.setattr(interactions, "_orch", SimpleNamespace(slack=slack))
    ran: list[str] = []
    _floor_monkeypatch.setattr(interactions, "_handle_ch_add", AsyncMock(side_effect=ran.append))
    payload = {
        "type": "block_actions",
        "user": {"id": FRIEND},
        "channel": {"id": "C1"},
        "message": {"ts": "m1"},
        "actions": [{"action_id": "mc_ch_add"}],
    }
    await interactions.dispatch(payload)
    assert ran == []
    slack.post_ephemeral.assert_awaited_once_with(
        "C1", FRIEND, shared_access.SHARED_OWNER_ONLY_TEXT
    )


@pytest.mark.asyncio
async def test_dashboard_links_stay_the_owners() -> None:
    from kiro_crew.slack.handler import _route_bang_command

    await _confirmed_friend()
    slack = SimpleNamespace(post_message=AsyncMock())
    handled = await _route_bang_command(
        "!dashboard", slack, None, "D1", "t1", "m1", "s1", FRIEND, None  # type: ignore[arg-type]
    )
    assert handled is True
    slack.post_message.assert_awaited_once_with("D1", "⛔ Not authorized.", "t1")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["!channel off", "!yolo on", "!agent foo", "!allowlist"])
async def test_owner_commands_stay_the_owners(command: str) -> None:
    from kiro_crew.slack.handler import _route_bang_command

    await _confirmed_friend()
    slack = SimpleNamespace(post_message=AsyncMock())
    assert await _route_bang_command(
        command, slack, None, "D1", "t1", "m1", "s1", FRIEND, None  # type: ignore[arg-type]
    )
    assert slack.post_message.await_args.args[1] == "⛔ Owner-only command."


@pytest.mark.asyncio
async def test_schedules_and_task_runs_are_not_started_by_a_shared_user(
    _floor_monkeypatch,
) -> None:
    from kiro_crew.slack.handler import maybe_handle_keyword_command

    await _confirmed_friend()
    cron = AsyncMock(return_value="⏰ scheduled")
    run = AsyncMock(return_value="▶️ started")
    # `spawn list` names every subagent on the host, the owner's code channels' too.
    spawn = AsyncMock(return_value="🔹 every subagent")
    _floor_monkeypatch.setattr(handler, "_handle_cron_command", cron)
    _floor_monkeypatch.setattr(handler, "_handle_run_command", run)
    _floor_monkeypatch.setattr(handler, "_handle_spawn_command", spawn)
    slack = SimpleNamespace(post_message=AsyncMock())

    async def _keyword(user: str, text: str) -> bool:
        return await maybe_handle_keyword_command(
            text,
            slack,
            None,
            "D1",
            "t1",
            "m1",
            "s1",
            user,  # type: ignore[arg-type]
            subagent_manager=object(),  # type: ignore[arg-type]
            cron_service=object(),
            task_runner=object(),  # type: ignore[arg-type]
        )

    assert await _keyword(FRIEND, "remind me in 5 minutes") is False
    assert await _keyword(FRIEND, "run spec.md") is False
    assert await _keyword(FRIEND, "spawn list") is False
    cron.assert_not_awaited()
    run.assert_not_awaited()
    spawn.assert_not_awaited()
    assert await _keyword(OWNER, "spawn list") is True
    spawn.reset_mock(return_value=True)
    spawn.return_value = None
    assert await _keyword(OWNER, "remind me in 5 minutes") is True


@pytest.mark.asyncio
async def test_a_message_queued_by_a_shared_user_is_dropped_once_their_access_ends(
    _floor_monkeypatch,
) -> None:
    from unittest.mock import MagicMock, patch

    from kiro_crew.slack.events import _dispatch_queued, _queue_tags

    await _confirmed_friend()
    tags = _queue_tags(FRIEND, "D1", shared=True)
    assert tags.get("shared_sender") is True
    assert "shared_sender" not in _queue_tags(OWNER, "D1")

    def _orch() -> MagicMock:
        orch = MagicMock()
        orch.slack = SimpleNamespace(remove_reaction=AsyncMock())
        orch._cfg = handler._orch_cfg
        for name in ("ctx_builder", "cron_svc", "conv_log", "subagent_mgr", "task_runner"):
            setattr(orch, name, None)
        return orch

    kwargs = {"channel": "D1", "thread_ts": "t1", "sender_id": FRIEND, **tags}
    _use(_floor_monkeypatch, _cfg(ids=()))  # removed while the message waited
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
        with patch(
            "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
        ) as hmt:
            await _dispatch_queued(_orch(), "t1", "m1", "hello", dict(kwargs))
    hm.assert_not_awaited()
    hmt.assert_not_awaited()
    _use(_floor_monkeypatch, _cfg())
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
        with patch(
            "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
        ) as hmt:
            await _dispatch_queued(_orch(), "t1", "m1", "hello", dict(kwargs))
    assert hm.await_count + hmt.await_count == 1


@pytest.mark.asyncio
async def test_a_queued_message_is_kept_when_only_the_confirmation_aged_out(
    _floor_monkeypatch,
) -> None:
    from unittest.mock import MagicMock, patch

    from kiro_crew.slack.events import _dispatch_queued, _queue_tags

    now = [1000.0]
    _floor_monkeypatch.setattr(shared_access, "clock", lambda: now[0])
    await _confirmed_friend()
    kwargs = {
        "channel": "D1",
        "thread_ts": "t1",
        "sender_id": FRIEND,
        **_queue_tags(FRIEND, "D1", shared=True),
    }
    now[0] += shared_access.CONFIRMED_TTL_SECS + 1  # a long turn ran meanwhile
    orch = MagicMock()
    orch.slack = SimpleNamespace(remove_reaction=AsyncMock())
    orch._cfg = handler._orch_cfg
    for name in ("ctx_builder", "cron_svc", "conv_log", "subagent_mgr", "task_runner"):
        setattr(orch, name, None)
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
        with patch(
            "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
        ) as hmt:
            await _dispatch_queued(orch, "t1", "m1", "hello", kwargs)
    assert hm.await_count + hmt.await_count == 1


@pytest.mark.asyncio
async def test_a_removed_shared_user_cannot_act_on_their_open_review_draft(
    _floor_monkeypatch,
) -> None:
    from kiro_crew.slack.interactions_approvals import _can_act_on_review_draft

    await _confirmed_friend()
    assert _can_act_on_review_draft(FRIEND, FRIEND) is True
    assert _can_act_on_review_draft(FRIEND, OWNER) is False
    assert _can_act_on_review_draft(OWNER, FRIEND) is True
    _use(_floor_monkeypatch, _cfg(ids=()))
    assert _can_act_on_review_draft(FRIEND, FRIEND) is False


@pytest.mark.asyncio
async def test_every_modal_submission_asks_slack_again_before_its_handler_runs(
    _floor_monkeypatch,
) -> None:
    from kiro_crew.slack import interactions_context, interactions_modals

    now = [1000.0]
    _floor_monkeypatch.setattr(shared_access, "clock", lambda: now[0])
    await _confirmed_friend()
    now[0] += shared_access.CONFIRMED_TTL_SECS + 1
    seen: list[bool] = []

    async def _handler(payload: dict) -> None:
        seen.append(handler.is_shared_user(FRIEND))

    _floor_monkeypatch.setitem(interactions_context.VIEW_REGISTRY, "test_modal", _handler)
    # No orchestrator left behind by an earlier test: the check reads this test's config.
    # Patched through the facade, which reaches every module that binds it.
    from kiro_crew.slack import interactions

    _floor_monkeypatch.setattr(interactions, "_orch", None)
    payload = {"user": {"id": FRIEND}, "view": {"callback_id": "test_modal"}}
    await interactions_modals.handle_view_submission(payload)
    assert seen == [True]


@pytest.mark.asyncio
async def test_a_review_modal_outliving_the_confirmation_is_still_accepted(
    _floor_monkeypatch,
) -> None:
    from kiro_crew.slack.interactions_approvals import _can_act_on_review_draft

    now = [1000.0]
    _floor_monkeypatch.setattr(shared_access, "clock", lambda: now[0])
    await _confirmed_friend()
    now[0] += shared_access.CONFIRMED_TTL_SECS + 1
    assert _can_act_on_review_draft(FRIEND, FRIEND) is False  # stale until asked again
    await shared_access.prime_shared_user(FRIEND)
    assert _can_act_on_review_draft(FRIEND, FRIEND) is True


@pytest.mark.parametrize(
    "text, refused",
    [
        ("/tools trust-all", True),
        ("  /agent builder", True),
        ("<@UBOT0001> /model claude", True),
        ("please !incognito fix it", True),
        ("!temporary", True),
        ("fix the failing test", False),
        ("see path a/b", False),
    ],
)
@pytest.mark.asyncio
async def test_a_shared_user_sends_no_harness_command_or_privacy_modifier(
    text: str, refused: bool
) -> None:
    await _confirmed_friend()
    assert shared_access.shared_command_refused(FRIEND, text) is refused
    assert shared_access.shared_command_refused(OWNER, text) is False


@pytest.mark.asyncio
async def test_a_harness_command_from_a_shared_user_never_reaches_the_turn() -> None:
    from kiro_crew.slack.handler import _dispatch_before_turn

    await _confirmed_friend()
    slack = SimpleNamespace(post_message=AsyncMock())
    names = (
        "sessions thread_ts msg_ts context_builder cron_service conversation_log "
        "subagent_manager task_runner channel_agent target_slot_name route_pinned "
        "session_key asker_key"
    ).split()
    turn = SimpleNamespace(
        slack=slack,
        channel="D1",
        text="/tools trust-all",
        user_id=FRIEND,
        reply_ts="t1",
        interpret_commands=True,
        **{n: None for n in names},
    )
    assert await _dispatch_before_turn(turn) is False  # type: ignore[arg-type]
    slack.post_message.assert_awaited_once_with("D1", shared_access.SHARED_OWNER_ONLY_TEXT, "t1")


def test_a_listed_user_is_refused_the_owner_keywords_even_with_an_aged_confirmation(
    _floor_monkeypatch,
) -> None:
    # No confirmation at all (as if it just aged out): still on the list, so still
    # refused where a gate keeps shared users out.
    assert handler.is_shared_user(FRIEND) is False
    assert handler.is_shared_listed(FRIEND) is True
    assert handler.is_shared_listed(OWNER) is False
    _use(_floor_monkeypatch, _cfg(enabled=False))
    assert handler.is_shared_listed(FRIEND) is False


@pytest.mark.parametrize("text", ["/tools trust-all", "!temporary hi"])
def test_an_aged_confirmation_lets_no_harness_command_through(text: str) -> None:
    # Admitted while fresh, then the confirmation aged out before dispatch (a slow
    # file download): the person is still listed, so the command is still theirs
    # to be refused.
    assert handler.is_shared_user(FRIEND) is False
    assert shared_access.shared_command_refused(FRIEND, text) is True


@pytest.mark.parametrize("change", ["removed", "switched_off"])
@pytest.mark.asyncio
async def test_a_removal_mid_message_lets_no_owner_only_control_through(
    _floor_monkeypatch, change: str
) -> None:
    # Admitted as a shared user, then the owner removes them (or turns sharing
    # off) while their message is still on its way to the owner-only gates.
    await _confirmed_friend()
    assert handler.is_shared_user(FRIEND) is True
    _use(_floor_monkeypatch, _cfg(ids=()) if change == "removed" else _cfg(enabled=False))
    assert handler.is_shared_user(FRIEND) is False  # no new message is admitted
    assert handler.is_shared_listed(FRIEND) is True
    assert shared_access.shared_command_refused(FRIEND, "/tools trust-all") is True
    assert handler.is_shared_listed(OWNER) is False


# ── is_allowed_user stays owner-only; shared users are admitted only on purpose ──


@pytest.mark.asyncio
async def test_is_allowed_user_stays_owner_only_for_a_confirmed_shared_user() -> None:
    # Every reader of is_allowed_user (main's owner gate) refuses a shared user.
    await _confirmed_friend()
    assert handler.is_shared_user(FRIEND) is True
    assert handler.is_allowed_user(FRIEND) is False
    assert handler.is_allowed_user(OWNER) is True


#: The sites that admit a shared user on purpose (``is_allowed_user(x) or
#: is_shared_user(x)``). A new one is a decision, made here.
_ADMISSION_SITES = {
    "slack/agent_sessions/session_events.py",
    "slack/events.py",
    "slack/events_inbound.py",
    "slack/events_queue.py",
    "slack/handler_runtime/commands.py",
    "slack/handler_runtime/inbound.py",
    "slack/handler_runtime/permission_ladder.py",
    "slack/interactions.py",
    "slack/interactions_approvals.py",
    "slack/interactions_options.py",
    "slack/interactions_sessions.py",
}


def test_only_the_named_sites_admit_a_shared_user() -> None:
    import ast
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).parent
    callers = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in ("slack/handler_runtime/access.py", "slack/shared_access.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                via_owner_module = (
                    isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "shared_access"
                )
                if name == "is_shared_user" and not via_owner_module:
                    callers.add(rel)
    assert callers == _ADMISSION_SITES


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["native", "transport"])
async def test_the_status_keyword_shows_a_shared_user_no_host_counts(
    _floor_monkeypatch, path: str
) -> None:
    # `status` replies with the host's message, approval, session and subagent
    # counts: the owner's, so a shared user's "status" goes to the model instead.
    import importlib

    from slack_handler_helpers import FakeSessionManager

    from conftest import MockSlackClient
    from kiro_crew.slack import transport_dispatch
    from kiro_crew.stats import Stats

    await _confirmed_friend()
    _floor_monkeypatch.setattr(Stats, "summary", lambda self: "HOST-STATS")

    async def _send(user: str) -> list[str]:
        if path == "native":
            slack = MockSlackClient()
            await handler.handle_message(
                slack, FakeSessionManager(), "D1", "status", "t1", "m1", user
            )
            return [p.get("text", "") for _k, p in slack.actions]
        golden = importlib.import_module("test_slack_golden_transcript")
        rec = golden.RecordingSlackClient()
        sessions = golden.FakeSessions(golden.ScriptedProvider([golden.make_event("ok")]))
        await transport_dispatch.handle_message_transport(
            rec, sessions, "D1", "status", "t1", "m1", user
        )
        return [str(kw.get("text", "")) for _m, kw in rec.transcript]

    assert not any("HOST-STATS" in t for t in await _send(FRIEND))
    assert any("HOST-STATS" in t for t in await _send(OWNER))


@pytest.mark.asyncio
@pytest.mark.parametrize("user, keeps_queue", [(FRIEND, True), (OWNER, False)])
async def test_a_shared_users_kill_now_keeps_the_threads_queue(
    _floor_monkeypatch, user: str, keeps_queue: bool
) -> None:
    # The thread's queue holds the owner's messages too: a shared user's Kill Now
    # parks it for the successor session instead of dropping it.
    from unittest.mock import MagicMock

    await _confirmed_friend()
    stop_turn = AsyncMock(return_value="hard")
    orch = SimpleNamespace(slack=SimpleNamespace(), sessions=SimpleNamespace(stop_turn=stop_turn))
    _floor_monkeypatch.setattr(interactions, "_orch", orch)
    _floor_monkeypatch.setattr(interactions, "sel", lambda: MagicMock())
    payload = {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": "C1"},
        "message": {"ts": "m1", "thread_ts": "t1"},
        "actions": [{"action_id": "stop_kill_now", "value": "slack:t1"}],
    }
    await interactions.dispatch(payload)
    stop_turn.assert_awaited_once()
    assert stop_turn.await_args.kwargs["force"] is True
    assert stop_turn.await_args.kwargs.get("preserve_queue", False) is keeps_queue
