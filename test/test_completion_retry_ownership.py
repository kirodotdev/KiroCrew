"""A popped sub-agent completion keeps exactly one owner when its turn fails.

``_run_chat`` receives a popped completion's one-shot ``_on_discarded``
callback. When the turn exits before the model consumed the completion, the
callback moves onto an exact ``lifecycle_recovery`` row (or, on a direct Stop
or slot teardown, runs once). These tests pin three properties of that hand-over:

- a cancel that lands in the turn's teardown awaits still leaves the retry row
  owning the callback, because the exit guard runs the settlement the turn's
  own ``finally`` never reached;
- a user's message or a completion not yet attempted, queued behind the failed
  completion, still drains in the ordinary hand-off; the hand-off holds only
  when the next entry it would drain is already a failed completion's retry,
  so two failing completions never run each other;
- the retry replays the raw announce, so the failure notices the failed
  attempt drained go back on the slot for the next turn to deliver;
- a person's send that pauses the completion turn records no Stop, so the
  completion waits on a retry row behind their message and runs in full after
  their reply, while a Stop on the same turn still discards it.

A queued completion on a slot whose close hands its key to a recreate is retired
only once the handed-over tail committed: a failed tail leaves the callback
unfired so the manager keeps the delivery for restart recovery.

A completion turn that runs gives that ownership up as soon as the model has the
completion, at its first tool call or streamed token, so the parent's own goal
stop later in the same turn is applied instead of refused as attached child work.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_runner_coverage_helpers import _complete, _runner_state, _slot
from chat_test_helpers import _make_state
from test_autonudge_stop_auth import _FakeLoop, _FakeSvc, _install_svc
from test_slot_close_recreation_race import (
    NAME,
    _arm_running_turn,
    _reached,
    _Req,
    _slot_with_committed_and_uncommitted_rows,
)

from kiro_crew import autonudge, session_directive
from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard import responsive_chat as rc
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_utils import (
    LIFECYCLE_RECOVERY_KIND,
    SUBAGENT_COMPLETION_KIND,
    subagent_attachment_detail_async,
)
from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, LLMEvent
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import create_agent_folder
from kiro_crew.testing.wait import await_until

COMPLETION = "[Subagent completion event] the child finished"
SECOND_COMPLETION = "[Subagent completion event] another child finished"
USER_MESSAGE = "the user's next message"
#: The end of an announce longer than the copy of paused work a person's send carries.
REPORT_TAIL = "END-OF-THE-CHILD-REPORT"
LONG_COMPLETION = (
    "[Subagent completion event] the child's full report: "
    + "finding " * (rc.RECOVERY_CHARS // len("finding ") + 1)
    + REPORT_TAIL
)
QUESTION = "What is the status?"


def _state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    return state


def _failing_client(prompts: list[str] | None = None) -> AsyncMock:
    """A client whose every prompt fails terminally before any token.

    With *prompts*, each prompt the client receives is appended to it.
    """
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.set_model = AsyncMock()
    client.served_model = ""
    client.client = None

    async def _stream(msg):
        if prompts is not None:
            prompts.append(msg)
        raise AcpError("provider rejected the prompt", transient=False)
        yield  # pragma: no cover -- makes _stream an async generator

    client.stream = _stream
    client.stream_command = _stream
    return client


def _completion_rows(slot) -> list[dict]:
    return [item for item in slot._queue if item.get("kind") == LIFECYCLE_RECOVERY_KIND]


@pytest.mark.asyncio
async def test_teardown_cancel_still_transfers_the_completion_to_a_retry_row(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    drain = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain)

    async def _cancelled_teardown(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(chat_runner, "_clear_local_turn_marker", _cancelled_teardown)
    discarded = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await _run_chat(
            state,
            slot,
            COMPLETION,
            _turn_actor="subagent",
            _on_consumed=MagicMock(),
            _on_discarded=discarded,
        )

    rows = _completion_rows(slot)
    assert [row["content"] for row in rows] == [COMPLETION]
    assert rows[0]["_on_discarded"] is not None
    discarded.assert_not_called()
    # The retry is the only queued entry, so the guard's tail holds it.
    drain.assert_not_awaited()

    # The retry row is now the sole owner: removing it retires the callback once.
    assert slot.queue_remove_by_id(rows[0]["id"]) == COMPLETION
    discarded.assert_called_once_with()


@pytest.mark.asyncio
async def test_queued_user_message_drains_ahead_of_a_held_completion_retry(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    drain = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain)
    slot.queue_append("the user's next message")
    discarded = MagicMock()

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=discarded,
    )

    assert [(item["content"], item.get("kind", "")) for item in slot._queue] == [
        ("the user's next message", ""),
        (COMPLETION, LIFECYCLE_RECOVERY_KIND),
    ]
    drain.assert_awaited_once()
    discarded.assert_not_called()


@pytest.mark.asyncio
async def test_a_lone_completion_retry_is_held_not_drained(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    drain = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain)
    discarded = MagicMock()

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=discarded,
    )

    assert [row["content"] for row in _completion_rows(slot)] == [COMPLETION]
    assert len(slot._queue) == 1
    drain.assert_not_awaited()
    discarded.assert_not_called()


def _bounded_real_drain(monkeypatch, *, limit: int = 6) -> list[None]:
    """Keep the real queue drain, but end a chain that would never end on its own.

    Returns the list each started turn appends to, so a test counts dispatches.
    """
    real_drain = chat_runner._start_next_queued_turn
    started: list[None] = []

    async def _drain(*args, **kwargs):
        if len(started) >= limit:
            return False
        if not await real_drain(*args, **kwargs):
            return False
        started.append(None)
        return True

    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _drain)
    return started


async def _run_started_turns(state, rounds: int = 30) -> None:
    """Await every turn a hand-off started, and the ones those started in turn."""
    for _ in range(rounds):
        pending = [task for task in list(state._background_tasks) if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", ["nothing", "user message", "user message held by children"])
async def test_two_failed_completions_hold_their_retries_instead_of_running_each_other(
    tmp_path, monkeypatch, waiting
):
    """Each failed completion's retry queues behind the other, and neither starts it.

    The first completion fails with the second, never attempted, queued: the
    second still drains. When it fails too, the next entry is already a failed
    completion's retry, so the hand-off holds instead of sending the two rejected
    prompts round again. A user message the drain takes first still runs, and the
    retry the next hand-off starts holds again when it fails. While live children
    hold user messages, the next entry is the first one the hold releases, so a
    held message at the head does not let the retries run each other.
    """
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    prompts: list[str] = []
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(prompts), True, False))
    started = _bounded_real_drain(monkeypatch)
    if waiting == "user message held by children":
        monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *_args: True)
        slot.queue_append(USER_MESSAGE)
    first_discarded, second_discarded = MagicMock(), MagicMock()
    slot.queue_insert(
        len(slot._queue),
        SECOND_COMPLETION,
        kind=SUBAGENT_COMPLETION_KIND,
        on_consumed=MagicMock(),
        on_discarded=second_discarded,
    )
    if waiting == "user message":
        slot.queue_append(USER_MESSAGE)

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=first_discarded,
    )
    await _run_started_turns(state)

    def _sent(prompt: str) -> str:
        return next(
            label
            for label, text in (
                ("first", COMPLETION),
                ("second", SECOND_COMPLETION),
                ("user", USER_MESSAGE),
            )
            if text in prompt
        )

    queued = [(item["content"], item.get("kind", "")) for item in slot._queue]
    if waiting == "user message":
        assert [_sent(prompt) for prompt in prompts] == ["first", "second", "user", "first"]
        assert queued == [
            (SECOND_COMPLETION, LIFECYCLE_RECOVERY_KIND),
            (COMPLETION, LIFECYCLE_RECOVERY_KIND),
        ]
        assert len(started) == 3
    else:
        assert [_sent(prompt) for prompt in prompts] == ["first", "second"]
        held = [(USER_MESSAGE, "")] if waiting == "user message held by children" else []
        assert queued == held + [
            (COMPLETION, LIFECYCLE_RECOVERY_KIND),
            (SECOND_COMPLETION, LIFECYCLE_RECOVERY_KIND),
        ]
        assert len(started) == 1
    # Each held retry still owns its completion's discard callback.
    assert all(callable(row["_on_discarded"]) for row in _completion_rows(slot))
    first_discarded.assert_not_called()
    second_discarded.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_commits", [True, False])
async def test_handover_close_retires_a_queued_completion_only_after_its_tail_commits(
    tmp_path, monkeypatch, tail_commits
):
    monkeypatch.setattr(autonudge, "_INSTANCE", None)
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)
    discarded = MagicMock()
    queued_id = original.queue_insert(
        0, COMPLETION, kind=LIFECYCLE_RECOVERY_KIND, on_discarded=discarded
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    if not tail_commits:

        async def _wedged(*_args, **_kwargs):
            raise OSError("disk wedged")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _wedged)

    close = asyncio.create_task(chat_handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    state.get_or_create_slot(NAME)
    release.set()
    resp = await close

    if tail_commits:
        assert resp.status == 200
        discarded.assert_called_once_with()
    else:
        assert resp.status == 500
        discarded.assert_not_called()
        assert [item["id"] for item in original._queue] == [queued_id]
        assert original._queue[0]["_on_discarded"] is discarded


@pytest.mark.asyncio
async def test_an_enriched_completion_retry_stays_a_completion_across_a_second_failure(
    tmp_path, monkeypatch
):
    """A drained failure notice enriches ``turn.message``; the retry keeps its identity.

    The retry row queues the raw announce, so its replay mark must follow the
    raw bytes. Without it the second failure requeues the result as an ordinary
    recovery, whose discard would tombstone a completion the model never read.
    """
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    # The context-bearing arm is the one that drains pending failure notices.
    state.context_builder = MagicMock()
    state.context_builder.build_message.side_effect = lambda message, *_a, **_k: (message, None)
    slot._pending_subagent_failures = ["[Subagent delivery failed] another child timed out"]
    discarded = MagicMock()

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=discarded,
    )

    rows = _completion_rows(slot)
    assert [row["content"] for row in rows] == [COMPLETION]
    assert rows[0].get("_replays_completion") is True

    # Drain it the way the queue hand-off does and fail again.
    retry = slot._queue.pop(0)
    await _run_chat(
        state,
        slot,
        retry["content"],
        _turn_actor="subagent",
        _synthetic_recovery_turn=True,
        _replays_completion=True,
        _on_consumed=retry["_on_consumed"],
        _on_discarded=retry["_on_discarded"],
    )

    rows = _completion_rows(slot)
    assert [row["content"] for row in rows] == [COMPLETION]
    assert rows[0].get("_replays_completion") is True
    discarded.assert_not_called()


@pytest.mark.asyncio
async def test_a_steer_requeued_ahead_of_a_held_retry_still_drains(tmp_path, monkeypatch):
    """An unconsumed steer goes to the queue head at the hand-off, ahead of the retry.

    The retry's hold was decided when the retry was queued, before the steer moved,
    so it is read again there: the user's steer is now the next entry and drains.
    """
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    steer = "use the staging bucket instead"
    client = _failing_client()
    failing_stream = client.stream

    async def _stream_while_steered(msg):
        # The steer lands while the completion turn is in flight and is never consumed.
        slot._pending_steers.append(steer)
        async for chunk in failing_stream(msg):
            yield chunk  # pragma: no cover -- the failing stream raises first

    client.stream = _stream_while_steered
    client.stream_command = _stream_while_steered
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    drain = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain)

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=MagicMock(),
    )

    assert [item["content"] for item in slot._queue] == [steer, COMPLETION]
    drain.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_completion_retry_carries_back_the_failure_notices_its_attempt_drained(
    tmp_path, monkeypatch
):
    """The retry row replays the raw announce, so a drained notice goes back on the slot.

    The failed attempt drained another child's failure notice into its prompt and
    settled the notice's delivery debt, but the model never read it. Left off the
    slot, that child's failure report would count as delivered and never reach
    the parent.
    """
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    prompts: list[str] = []
    state.sessions.get_or_create = AsyncMock(return_value=(_failing_client(prompts), True, False))
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    # The context-bearing arm is the one that drains pending failure notices.
    state.context_builder = MagicMock()
    state.context_builder.build_message.side_effect = lambda message, *_a, **_k: (message, None)
    notice = "[Subagent delivery failed] another child timed out"
    slot._pending_subagent_failures = [notice]

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _on_consumed=MagicMock(),
        _on_discarded=MagicMock(),
    )

    assert notice in prompts[0]
    assert [row["content"] for row in _completion_rows(slot)] == [COMPLETION]
    assert slot._pending_subagent_failures == [notice]

    # Drained the way the queue hand-off does, the retry reads the notice again.
    retry = slot._queue.pop(0)
    await _run_chat(
        state,
        slot,
        retry["content"],
        _turn_actor="subagent",
        _synthetic_recovery_turn=True,
        _replays_completion=True,
        _on_consumed=retry["_on_consumed"],
        _on_discarded=retry["_on_discarded"],
    )

    assert len(prompts) == 2
    assert notice in prompts[1] and COMPLETION in prompts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [True, False])
async def test_a_completion_replay_vetoed_at_its_consume_seam_keeps_one_owner(
    tmp_path, monkeypatch, caplog, stopped
):
    """A replay cancelled at the consume seam returns above the turn's main try.

    The popped completion it carried still settles there: a Stop retires the
    discard callback, any other veto moves it onto a retry row. Left unsettled,
    the completion's count in the manager's ledger is never released and every
    agent-issued stop on the parent stays refused. The settlement must also run
    to its end: the retry row reports its retention once and, as the only queued
    entry, is held rather than drained straight back into the turn.
    """
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    get_or_create = AsyncMock(return_value=(_failing_client(), True, False))
    state.sessions.get_or_create = get_or_create
    drain = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain)

    async def _vetoed(_state, vetoed_slot, _claimed, _phase):
        if stopped:
            vetoed_slot._stop_generation += 1
        return True

    monkeypatch.setattr(chat_runner, "_replay_vetoed_at_consume", _vetoed)
    discarded = MagicMock()
    retained = MagicMock()

    await _run_chat(
        state,
        slot,
        COMPLETION,
        _turn_actor="subagent",
        _synthetic_recovery_turn=True,
        _replays_completion=True,
        _on_consumed=MagicMock(),
        _on_discarded=discarded,
        _on_lifecycle_recovery_retained=retained,
    )

    get_or_create.assert_not_awaited()
    assert "completion settlement failed" not in caplog.text
    if stopped:
        discarded.assert_called_once_with()
        retained.assert_not_called()
        assert _completion_rows(slot) == []
    else:
        discarded.assert_not_called()
        retained.assert_called_once_with()
        drain.assert_not_awaited()
        rows = _completion_rows(slot)
        assert [row["content"] for row in rows] == [COMPLETION]
        assert slot.queue_remove_by_id(rows[0]["id"]) == COMPLETION
        discarded.assert_called_once_with()


def _pausable_turns(tmp_path, monkeypatch, *, release_on_pause: bool) -> SimpleNamespace:
    """A slot whose first turn waits, with no output, until released, then ends cancelled.

    Every later prompt answers and ends normally. The native turn reads as live, so
    the exact-owner cancel a person's send claims (``responsive_chat.accept``) can
    pause it; with *release_on_pause* that cancel is what ends the first turn. Each
    prompt records the queue as it stood when the prompt was sent.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda *a: None)
    monkeypatch.setattr(chat_runner, "_gateway_shutdown_requested", lambda state: False)
    monkeypatch.setattr(chat_runner, "_memory_preparation_pending", lambda state: False)
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *a: False)
    state, client = _runner_state(tmp_path)
    slot = _slot("responsive-completion")
    state._slots[slot.key] = slot
    client.client = SimpleNamespace(
        pop_pending_oauth_requests=list,
        awaiting_permission=False,
        last_prompt_stats=None,
        pending_steers=[],
        supports_steer=False,
        has_active_turn=lambda: True,
    )
    client.billing_stats = MagicMock(return_value=None)
    client.has_active_turn = MagicMock(return_value=True)
    client.is_alive = MagicMock(return_value=True)
    client.supports_steer = False
    state.sessions.is_compacting = lambda key: False
    state.sessions.stop_generation = MagicMock(return_value=0)
    run = SimpleNamespace(
        state=state,
        slot=slot,
        prompts=[],
        queues=[],
        entered=asyncio.Event(),
        release=asyncio.Event(),
    )

    async def _stream(prompt):
        run.prompts.append(prompt)
        run.queues.append(
            [
                (item["content"], item.get("kind", ""), callable(item.get("_on_discarded")))
                for item in slot._queue
            ]
        )
        if len(run.prompts) == 1:
            run.entered.set()
            await asyncio.wait_for(run.release.wait(), timeout=30)
            yield _complete("cancelled")
            return
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"Reply {len(run.prompts)}.")
        yield _complete()

    client.stream = MagicMock(side_effect=_stream)

    async def _cancel_owned_turn(*_args, **_kwargs):
        if release_on_pause:
            run.release.set()
        return "acked"

    state.sessions.cancel_owned_turn = AsyncMock(side_effect=_cancel_owned_turn)
    return run


@pytest.mark.asyncio
async def test_a_paused_completion_keeps_its_owner_and_runs_in_full_after_the_reply(
    tmp_path, monkeypatch
):
    """A person's send pauses a completion turn before its first output: no Stop.

    The pause is the gateway's scheduling and records no Stop, so the completion
    keeps its owner on a retry row behind the person's message. Their turn carries
    only a bounded copy of the paused work, which here ends before the announce
    does; the retry then delivers the whole announce. Discarding it at the pause
    would settle the result as delivered with its tail never read.
    """
    run = _pausable_turns(tmp_path, monkeypatch, release_on_pause=True)
    consumed, discarded = MagicMock(), MagicMock()
    run.slot.queue_insert(
        0,
        LONG_COMPLETION,
        kind=SUBAGENT_COMPLETION_KIND,
        on_consumed=consumed,
        on_discarded=discarded,
    )
    assert await chat_runner._start_next_queued_turn(run.state, run.slot)
    await asyncio.wait_for(run.entered.wait(), timeout=30)

    response = await rc.accept(
        SimpleNamespace(app={"state": run.state}), run.slot, QUESTION, {"sendId": "status"}
    )
    assert json.loads(response.text)["responsive"] == "cancel_requested"
    await _run_started_turns(run.state)

    assert len(run.prompts) == 3
    # The person's reply ran first, on a copy of the paused work that omits the tail,
    assert run.prompts[1].endswith(QUESTION)
    assert REPORT_TAIL not in run.prompts[1]
    # while the completion waited behind it, still owning its discard callback,
    assert run.queues[1] == [(LONG_COMPLETION, LIFECYCLE_RECOVERY_KIND, True)]
    # and its retry then delivered the whole announce.
    assert LONG_COMPLETION in run.prompts[2]
    consumed.assert_called_once_with(True)
    discarded.assert_not_called()
    assert run.slot._queue == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["dashboard", "dashboard after a send", "session"])
async def test_a_stop_on_the_completion_turn_still_discards_it(tmp_path, monkeypatch, stop):
    """A Stop is a person's decision about the work, so the completion is discarded once.

    The dashboard's Stop is recorded on the slot; a Stop on another surface moves
    only the session manager's count. A Stop that lands after a person's send paused
    the turn is still a Stop: their message runs, and the completion does not.
    """
    run = _pausable_turns(tmp_path, monkeypatch, release_on_pause=False)

    async def _stop_turn(_key, **kwargs):
        run.release.set()
        if kwargs.get("on_soft") is not None:
            await kwargs["on_soft"]()
        return "soft"

    run.state.sessions.stop_turn = AsyncMock(side_effect=_stop_turn)
    consumed, discarded = MagicMock(), MagicMock()
    run.slot.queue_insert(
        0, COMPLETION, kind=SUBAGENT_COMPLETION_KIND, on_consumed=consumed, on_discarded=discarded
    )
    assert await chat_runner._start_next_queued_turn(run.state, run.slot)
    await asyncio.wait_for(run.entered.wait(), timeout=30)

    if stop == "dashboard after a send":
        await rc.accept(
            SimpleNamespace(app={"state": run.state}), run.slot, QUESTION, {"sendId": "status"}
        )
        assert rc.turn_state(run.slot).interrupted
    if stop == "session":
        run.state.sessions.stop_generation.return_value = 1
        run.release.set()
    else:
        await chat_handlers.stop_slot_turn(run.state, run.slot)
    await _run_started_turns(run.state)

    discarded.assert_called_once_with()
    consumed.assert_not_called()
    assert _completion_rows(run.slot) == []
    if stop == "dashboard after a send":
        assert len(run.prompts) == 2 and run.prompts[1].endswith(QUESTION)
    else:
        assert len(run.prompts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first_event", ["tool call", "text token"])
@pytest.mark.parametrize("operation", ["autonudge_stop", "monitor_stop"])
@pytest.mark.parametrize("handoff", ["idle tab", "busy tab"])
async def test_an_agent_stop_in_the_turn_that_consumes_the_last_completion_is_applied(
    tmp_path, monkeypatch, handoff, operation, first_event
):
    """The parent may end its goal in the same turn that reads the last child's result.

    The tab is handed the last completion, as its own turn on an idle tab or as a
    queued row a busy tab's hand-off drains, so the manager retains it and the
    fence holds while the turn that carries it starts. That turn reports the
    completion consumed at its first tool call or first streamed token, and the
    gateway's consumer callback releases the retention there. A stop the model then
    issues in the same turn sees no attached child work and is applied, instead of
    being refused as a "terminal child report delivery" for a result it has read.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "agents")
    state, client = _runner_state(tmp_path)
    slot = _slot("chat-last-child")
    state._slots[slot.key] = slot
    parent_key = f"dashboard:{slot.key}"
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    state.subagents = manager
    svc = _FakeSvc(_FakeLoop("loop-last-child"))
    _install_svc(monkeypatch, svc)
    probes: list[str | None] = []

    async def _probe() -> None:
        probes.append(await subagent_attachment_detail_async(state, slot, parent_key, operation))

    stop_call = LLMEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="tc-stop",
        title=operation,
        tool_name=operation,
        mcp_server_name=session_directive.CORE_MCP_SERVER,
    )
    stop_result = LLMEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id="tc-stop",
        tool_output=session_directive.encode(
            operation, {"reason": "the last child reported"}, "Stop requested."
        ),
        tool_final=True,
    )
    reply = LLMEvent(kind=EVENT_TEXT_CHUNK, text="Every child has reported; the goal is met.")
    streams: list[None] = []

    async def _completion_turn(*_args, **_kwargs):
        streams.append(None)
        if len(streams) > 1:
            # Only the completion turn is scripted; any later turn just ends.
            yield _complete()
            return
        # Probed between events: the runner handles one event fully before it
        # asks for the next, so each probe reads the fence after the events
        # before it.
        await _probe()
        if first_event == "tool call":
            yield stop_call
            await _probe()
            yield stop_result
            yield reply
        else:
            yield reply
            await _probe()
            yield stop_call
            yield stop_result
        yield _complete()

    client.stream = MagicMock(side_effect=_completion_turn)

    info = SubagentInfo(id="last-child", task="t", parent_session_key=parent_key)
    info.done = True
    info.result = "the child's report"
    create_agent_folder(info.id, task="t", parent_session=parent_key)
    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.dashboard_state = state
    gateway.subagent_mgr = manager
    busy_turn = None
    if handoff == "busy tab":
        # A turn that outlasts the injection wait: the completion queues behind it.
        monkeypatch.setattr("kiro_crew.slack.gateway.INJECTION_TIMEOUT", 0.01)
        busy_turn = slot.task = asyncio.get_running_loop().create_future()
    await GatewayOrchestrator._route_subagent_to_tab(
        gateway,
        info,
        announce=COMPLETION,
        body="the child's report",
        title="last-child finished",
        sub_meta={},
        _flush_only=False,
        _injection_slot=slot,
        _injection_slot_name=slot.key,
        parent_key=parent_key,
    )
    if busy_turn is not None:
        assert [item.get("kind") for item in slot._queue] == [SUBAGENT_COMPLETION_KIND]
        # The busy turn ends and its queue hand-off starts the completion's turn.
        busy_turn.cancel()
        slot.task = None
        assert await chat_runner._start_next_queued_turn(state, slot) is True
    turn = slot.task
    assert turn is not None and turn is not busy_turn, "no completion turn was started"
    # Handed over and not yet read: the manager retains the completion.
    assert manager.terminal_delivery_inflight_for(parent_key) is True
    try:
        await await_until(turn, what="the parent's completion turn")
    finally:
        leftover = [task for task in state._background_tasks if not task.done()]
        for task in leftover:
            task.cancel()
        if leftover:
            await await_until(
                asyncio.gather(*leftover, return_exceptions=True), what="the turn's leftovers"
            )

    outcomes = [
        call.args[1]["output"]
        for call in state.broadcast_ws.call_args_list
        if call.args[:1] == ("tool_result",) and call.args[1].get("tool_call_id") == "tc-stop"
    ]
    assert len(outcomes) == 1 and "stopped on this session" in outcomes[0], outcomes
    # The fence held at the turn's start and was gone after its first event.
    assert probes == ["terminal child report delivery", None]
    assert svc.removed == ["loop-last-child"]
    assert manager.terminal_delivery_inflight_for(parent_key) is False
