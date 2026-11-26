"""``/goal`` and ``/workflow`` run from a channel only when the Crew's owner sent it.

A Slack thread linked to a dashboard chat runs its messages as that chat's turns,
and the turn's slash gate reads their first word as a command. Both commands start
work that runs on after the turn (a goal loop, a workflow run), so a channel turn
runs them only when the route stamped the owner as the sender of THIS message
(``_directive_channel_senders``). Who else wrote in the thread earlier does not
matter. A dashboard turn -- the owner's or an admitted teammate's -- is not judged
here. The Slack dispatch refuses a shared user's leading ``/`` before any of this
(``shared_command_refused``); this is the dashboard's own check behind it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from chat_test_helpers import _make_state
from turn_harness import SlotSpec, TurnScript, run_turn

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_turn import slash_commands
from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce
from kiro_crew.slack import handler as slack_handler

OWNER = "U0OWNER"
SHARED = "U0SHARED"
CHANNEL = "C0LINKED"


def _channel_turn(*senders: str) -> dict[str, Any]:
    return {
        "_directive_user_origin": True,
        "_directive_channel_origin": True,
        "_directive_source_channels": (CHANNEL,),
        "_directive_channel_senders": senders,
    }


DASHBOARD_TURN = {"_directive_user_origin": True}


class _Workflows:
    def __init__(self) -> None:
        self.started: list[str] = []

    def list_definitions(self) -> list[dict[str, Any]]:
        return []

    async def start_definition(self, workflow_ref: str, **_kwargs: Any) -> dict[str, Any]:
        self.started.append(workflow_ref)
        return {"run_id": "wf_1", "slug": workflow_ref, "revision": 1}


@pytest.fixture(autouse=True)
def _owner(_floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setattr(slack_handler, "is_owner", lambda user_id: user_id == OWNER)
    cce.forget_senders()
    yield
    cce.forget_senders()


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []

    async def deliver(_state, _slot, _sessions, _key, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(chat_runner, "_deliver_linked_slack_message", deliver)
    return sent


def _thread_had(*authors: str, workflows: _Workflows | None = None):
    """The chat's past: who the Slack route already recorded writing in it."""

    def setup(ctx) -> None:
        for author in authors:
            cce.note_channel_sender(ctx.slot, author)
        if workflows is not None:
            ctx.state.workflow_service = workflows

    return setup


def _goal_armed(record) -> bool:
    return any(call.name == "add" for call in record.autonudge_calls)


def _replies(record) -> list[str]:
    return [row["content"] for row in record.window if row["role"] == "assistant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("senders", [(SHARED,), (OWNER, SHARED)])
async def test_a_channel_goal_the_owner_did_not_send_is_not_armed(
    posted: list[str], senders: tuple[str, ...]
) -> None:
    record = await run_turn(
        TurnScript(message="/goal ship it", run_kwargs=_channel_turn(*senders)),
        slot=SlotSpec(autonudge=True),
    )
    assert not _goal_armed(record)
    [reply] = [text for text in _replies(record) if "was not run" in text]
    assert reply.startswith("`/goal` was not run: from a channel only the Crew's owner")
    assert posted == [reply]
    assert record.audits(tool_name="/goal", outcome="denied")
    assert record.provider_calls == ()


@pytest.mark.asyncio
async def test_a_channel_workflow_the_owner_did_not_send_is_not_started(
    posted: list[str],
) -> None:
    workflows = _Workflows()
    record = await run_turn(
        TurnScript(
            message="/workflow nightly go",
            run_kwargs=_channel_turn(SHARED),
            setup=_thread_had(workflows=workflows),
        )
    )
    assert workflows.started == []
    assert any(text.startswith("`/workflow` was not run") for text in _replies(record))
    assert len(posted) == 1
    assert record.audits(tool_name="/workflow", outcome="denied")


# The owner's commands, after a shared user wrote in the thread (the thread's
# past), and with no past at all (a new message after a gateway restart).
_PASTS = [pytest.param((SHARED,), id="after_a_shared_user"), pytest.param((), id="after_restart")]


@pytest.mark.asyncio
@pytest.mark.parametrize("past", _PASTS)
async def test_the_owners_channel_goal_is_armed(posted: list[str], past: tuple[str, ...]) -> None:
    record = await run_turn(
        TurnScript(
            message="/goal ship it", run_kwargs=_channel_turn(OWNER), setup=_thread_had(*past)
        ),
        slot=SlotSpec(autonudge=True),
    )
    assert _goal_armed(record)
    assert posted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("past", _PASTS)
async def test_the_owners_channel_goal_clear_runs(posted: list[str], past: tuple[str, ...]) -> None:
    record = await run_turn(
        TurnScript(
            message="/goal clear", run_kwargs=_channel_turn(OWNER), setup=_thread_had(*past)
        ),
        slot=SlotSpec(autonudge=True),
    )
    assert not any("was not run" in text for text in _replies(record))
    assert any("goal" in text.lower() for text in _replies(record))
    assert posted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("past", _PASTS)
async def test_the_owners_channel_workflow_is_started(past: tuple[str, ...]) -> None:
    workflows = _Workflows()
    await run_turn(
        TurnScript(
            message="/workflow nightly go",
            run_kwargs=_channel_turn(OWNER),
            setup=_thread_had(*past, workflows=workflows),
        )
    )
    assert workflows.started == ["nightly"]


@pytest.mark.asyncio
async def test_a_dashboard_turn_is_not_judged_by_who_wrote_in_the_channel(
    posted: list[str],
) -> None:
    # The owner's or an admitted teammate's dashboard send, in a chat a shared
    # Slack user also wrote in: the command runs.
    workflows = _Workflows()
    goal = await run_turn(
        TurnScript(message="/goal ship it", run_kwargs=DASHBOARD_TURN, setup=_thread_had(SHARED)),
        slot=SlotSpec(autonudge=True),
    )
    await run_turn(
        TurnScript(
            message="/workflow nightly",
            run_kwargs=DASHBOARD_TURN,
            setup=_thread_had(SHARED, workflows=workflows),
        )
    )
    assert _goal_armed(goal)
    assert workflows.started == ["nightly"]
    assert posted == []


def _turn(**fields: Any) -> SimpleNamespace:
    base = {
        "_directive_channel_origin": False,
        "_directive_source_channels": (),
        "_directive_channel_senders": (),
    }
    base.update(fields)
    return SimpleNamespace(**base)


_SLOT = SimpleNamespace(key="chat-x", _slack_linked=True, _slack_channel=CHANNEL)


def test_a_restored_channel_entry_has_no_sender_and_is_refused_with_why() -> None:
    # A queued channel entry restored after a restart keeps its source-channel
    # stamp, not its channel mark or its sender (provenance is not persisted).
    why = slash_commands._channel_sender_refusal(
        _turn(_directive_source_channels=(CHANNEL,)), _SLOT
    )
    assert "not kept across a restart" in why


def test_a_batch_with_an_unknown_sender_is_refused() -> None:
    turn = _turn(_directive_channel_origin=True, _directive_channel_senders=("", OWNER))
    assert slash_commands._channel_sender_refusal(turn, _SLOT)


def test_a_failing_owner_check_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_user_id: str) -> bool:
        raise RuntimeError("owner id unreadable")

    monkeypatch.setattr(slack_handler, "is_owner", broken)
    turn = _turn(_directive_channel_origin=True, _directive_channel_senders=(OWNER,))
    assert slash_commands._channel_sender_refusal(turn, _SLOT)


def test_the_sender_stamp_is_not_written_to_disk() -> None:
    from kiro_crew.dashboard.slot_queue_repository import (
        CHANNEL_SENDERS_KEY,
        channel_senders_of,
        durable_queue_entries,
        sanitize_restored_queue,
        stamp_channel_senders,
    )

    owner = SimpleNamespace(_queue=[{"id": "q1", "content": "/goal x", "kind": ""}, {"id": "q2"}])
    stamp_channel_senders(owner, "q1", (OWNER, ""))
    assert channel_senders_of(owner._queue[0]) == (OWNER,)
    assert channel_senders_of(owner._queue[1]) == ()
    assert channel_senders_of({CHANNEL_SENDERS_KEY: [OWNER]}) == ()  # not a tuple: not read
    written = json.loads(json.dumps(durable_queue_entries(owner._queue[:1])))
    assert CHANNEL_SENDERS_KEY not in written[0]
    assert channel_senders_of(sanitize_restored_queue(written)[0]) == ()


async def _drained_kwargs(tmp_path, monkeypatch, entries) -> dict[str, Any]:
    from kiro_crew.dashboard.slot_queue_repository import stamp_channel_senders

    state = _make_state(tmp_path)
    state.subagents = None
    slot = state.get_or_create_slot("chat-1")
    for text, senders in entries:
        qid = slot.queue_append(text, directive_user_origin=True, directive_channel_origin=True)
        stamp_channel_senders(slot, qid, senders)
    captured: dict[str, Any] = {}

    def _stub_run_chat(_state, _slot, _prompt, **kwargs):
        captured.update(kwargs)

        async def _done():
            return None

        return _done()

    def _fake_spawn(_state, _slot, coro):
        coro.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    monkeypatch.setattr(chat_runner, "_run_chat", _stub_run_chat)
    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", _fake_spawn)
    assert await chat_runner._start_next_queued_turn(state, slot) is True
    return captured


@pytest.mark.asyncio
async def test_the_drain_hands_the_entry_sender_to_the_turn(tmp_path, monkeypatch) -> None:
    captured = await _drained_kwargs(tmp_path, monkeypatch, [("/goal ship it", (OWNER,))])
    assert captured["_directive_channel_senders"] == (OWNER,)


@pytest.mark.asyncio
async def test_the_drain_marks_an_unstamped_entry_unknown(tmp_path, monkeypatch) -> None:
    captured = await _drained_kwargs(tmp_path, monkeypatch, [("/goal ship it", ())])
    assert "_directive_channel_senders" not in captured


@pytest.mark.asyncio
async def test_the_slack_route_stamps_this_messages_sender(monkeypatch) -> None:
    slot = MagicMock()
    type(slot).running = PropertyMock(return_value=False)
    slot.key, slot.agent, slot.agent_kind = "slot1", "", ""
    slot.window_pending = False
    slot._queue = []
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=slot)
    ds._background_tasks = set()
    with (
        patch.object(slack_handler, "_dashboard_state", ds),
        patch.object(slack_handler, "is_allowed_user", return_value=True),
        patch.object(slack_handler, "linked_slot_channel_mismatch", AsyncMock(return_value=None)),
        patch("kiro_crew.slack.handler_runtime.access.slack_cfg", return_value=MagicMock()),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
    ):
        routed = await slack_handler.maybe_route_linked_thread(
            "/goal ship it", "slack:t1", OWNER, CHANNEL, MagicMock(post_message=AsyncMock()), "t1"
        )
        await asyncio.sleep(0)
    assert routed is True
    assert run_chat.call_args.kwargs["_directive_channel_senders"] == (OWNER,)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/goal ship it", "/workflow nightly", "<@UBOT0001> /goal x"])
async def test_the_slack_dispatch_refuses_a_shared_users_command_before_the_link(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    # The first check: a shared user's leading ``/`` never reaches a linked chat.
    from kiro_crew.slack.shared_access import SHARED_OWNER_ONLY_TEXT

    monkeypatch.setattr(slack_handler, "is_shared_listed", lambda user_id: user_id == SHARED)
    routed = AsyncMock(return_value=True)
    monkeypatch.setattr(slack_handler, "maybe_route_linked_thread", routed)
    slack = SimpleNamespace(post_message=AsyncMock())
    names = (
        "sessions thread_ts msg_ts context_builder cron_service conversation_log "
        "subagent_manager task_runner channel_agent target_slot_name route_pinned "
        "session_key asker_key spool_text"
    ).split()
    turn = SimpleNamespace(
        slack=slack,
        channel=CHANNEL,
        text=text,
        user_id=SHARED,
        reply_ts="t1",
        interpret_commands=True,
        **{n: None for n in names},
    )
    assert await slack_handler._dispatch_before_turn(turn) is False  # type: ignore[arg-type]
    slack.post_message.assert_awaited_once_with(CHANNEL, SHARED_OWNER_ONLY_TEXT, "t1")
    routed.assert_not_awaited()
