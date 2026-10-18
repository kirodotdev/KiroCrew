"""Behavioral coverage of question answers, queue identity and expanded prompts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from test_responsive_chat import _busy
from test_responsive_chat import fixture as _responsive_fixture

from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard import responsive_chat as rc
from kiro_crew.dashboard.chat_delivery import queue_entry_view
from kiro_crew.dashboard.handlers import ask_question
from kiro_crew.dashboard.state import _ChatSlot

fixture = _responsive_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_idle_stateless_card_answer_dispatches_and_retires_card(fixture, monkeypatch, native):
    state, slot, *_ = fixture
    state.deliver_ws_owners = AsyncMock(return_value=1)
    await state.post_question_card(
        slot.key, [{"question": "Which?", "options": [{"label": "First"}]}], native=native
    )
    assert slot._question_pending
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *args: True)
    ran = asyncio.Event()

    async def run(st, sl, message, **kwargs):
        assert message == "First"
        assert kwargs["_responsive_human"] is True
        ran.set()

    monkeypatch.setattr(chat_runner, "_run_chat", run)
    app = web.Application()
    app["state"] = state
    as_owner(app)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat?ws=1", json={"slot": slot.key, "message": "First", "responsive": True}
        )
        assert response.status == 200
        assert slot.task is not None, "idle card answer is stranded behind its own pending record"
        await asyncio.wait_for(slot.task, 30)
    assert ran.is_set()
    assert not slot._question_pending
    assert not slot._queue
    state.sessions.cancel_owned_turn.assert_not_called()


@pytest.mark.asyncio
async def test_live_native_card_answer_steers_with_guidance_without_cancelling(fixture):
    state, slot, *_ = fixture
    _busy(slot)
    state.deliver_ws_owners = AsyncMock(return_value=1)
    await state.post_question_card(
        slot.key, [{"question": "Which?", "options": [{"label": "First"}]}], native=True
    )
    delivered = []

    async def steer(message):
        delivered.append(message)
        chat_runner._settle_consumed_steers(slot, message, state)
        return True

    slot._acp_client.supports_steer = True
    slot._acp_client.steer = steer
    app = web.Application()
    app["state"] = state
    as_owner(app)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat?ws=1",
                json={
                    "slot": slot.key,
                    "message": "First",
                    "responsive": True,
                    "steer": True,
                },
            )
            assert response.status == 200
            assert (await response.json())["steered"] is True
        assert delivered[0].endswith("First") and delivered[0] != "First"
        assert not slot._question_pending and not slot._queue
        assert sum(row["content"] == "First" for row in slot.messages) == 1
        state.sessions.cancel_owned_turn.assert_not_called()
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_blocking_ask_answer_endpoint_preserves_future_until_answer(fixture):
    state, slot, request, *_ = fixture
    _busy(slot)
    ask_id = "a" * 32
    state.deliver_ws_owners = AsyncMock(return_value=1)
    await state.open_agent_ask(
        ask_id,
        slot.key,
        rc.effective_session_key(slot),
        [{"question": "Which?", "options": [{"label": "First"}]}],
    )
    future = state._question_futures[ask_id]
    await rc.accept(request, slot, "also check progress", None)
    assert not future.done()
    state.sessions.cancel_owned_turn.assert_not_called()
    app = web.Application()
    app["state"] = state
    as_owner(app)
    app.router.add_post("/api/ask-question/{ask_id}/answer", ask_question.api_ask_question_answer)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                f"/api/ask-question/{ask_id}/answer", json={"answers": {"Which?": "First"}}
            )
            assert response.status == 200
        assert await asyncio.wait_for(future, 30) == {"Which?": "First"}
        assert not slot._question_pending
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_actual_queue_push_pop_and_detail_carry_only_normalized_identity(
    fixture, monkeypatch
):
    state, slot, request, *_ = fixture
    state.broadcast_ws = MagicMock()
    _busy(slot)
    await rc.accept(request, slot, "one", {"sendId": "s-exact", "files": ["/work/File Name.txt"]})
    entry = slot._queue[0]
    push = next(c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push")
    assert push["meta"]["sendId"] == "s-exact"
    assert queue_entry_view(entry)["meta"]["sendId"] == "s-exact"
    slot.task.cancel()
    slot.task = None
    monkeypatch.setattr(chat_runner, "_run_chat", AsyncMock())
    assert await chat_runner._start_next_queued_turn(state, slot)
    await asyncio.wait_for(slot.task, 30)
    pop = next(c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_pop")
    assert pop["meta"] == {"sendId": "s-exact", "files": ["/work/File Name.txt"]}
    assert "sendId" not in queue_entry_view(
        {"id": "bad", "content": "one", "meta": {"sendId": ["invalid"]}}
    ).get("meta", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("responsive", [False, True])
async def test_prompts_get_keeps_same_request_guidance_and_owner(tmp_path, monkeypatch, responsive):
    from test_chat_runner_coverage import _complete, _runner_state, _slot

    from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

    state, client = _runner_state(tmp_path)
    slot = _slot("prompt-responsive")
    state._slots[slot.key] = slot
    original = "/prompts get example"
    slot.append("user", original)
    monkeypatch.setattr(
        chat_runner,
        "_expand_prompt_mention_off_loop",
        AsyncMock(return_value=("Expanded request", "ok")),
    )
    guidance = MagicMock(return_value="GATEWAY_REPLY_FIRST_TEST\n\n")
    monkeypatch.setattr(rc, "prompt_context", guidance)
    client.billing_stats = MagicMock(return_value=None)
    client.has_active_turn = MagicMock(return_value=False)
    client.is_alive = MagicMock(return_value=True)
    client.supports_steer = False
    client.client = SimpleNamespace(
        pop_pending_oauth_requests=list,
        awaiting_permission=False,
        last_prompt_stats=None,
        pending_steers=[],
        supports_steer=False,
    )
    prompts = []

    async def stream(prompt):
        prompts.append(prompt)
        assert rc.turn_state(slot).owner is asyncio.current_task()
        assert rc.turn_state(slot).protected is responsive
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Answer")
        yield _complete()

    client.stream = MagicMock(side_effect=stream)
    await asyncio.wait_for(
        chat_runner._run_chat(
            state,
            slot,
            original,
            _responsive_human=responsive,
            _responsive_protected=responsive,
            _directive_user_origin=True,
        ),
        30,
    )
    assert len(prompts) == 1 and prompts[0].endswith("Expanded request")
    assert ("GATEWAY_REPLY_FIRST_TEST" in prompts[0]) is responsive
    assert guidance.call_count == int(responsive)
    assert [row["content"] for row in slot.messages if row["role"] == "user"] == [original]


@pytest.mark.asyncio
async def test_http_priority_filters_caller_metadata_through_drain(fixture, monkeypatch):
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    state, slot, *_ = fixture
    _busy(slot)
    forbidden = {
        "commands_off": True,
        "steer_possibly_delivered": True,
        "steer_delivery_id": "forged",
        "steer_delivery_ids": ["forged"],
        "sendIds": ["forged"],
        "injectKind": "mcp_app",
        RESTORED_QUEUE_KEY: True,
        "channel_recipient": {"channel": "forged"},
        "send_origin_slot": "other-slot",
        "custom": "value",
    }
    accepted = {
        "sendId": "exact-send",
        "files": ["/work/File Name.txt"],
        "quote": {"role": "assistant", "text": "A prior answer"},
    }
    app = as_owner(web.Application())
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat?ws=1",
                json={
                    "slot": slot.key,
                    "message": "hello",
                    "responsive": True,
                    "meta": {**forbidden, **accepted},
                },
            )
            assert response.status == 200
        queued_meta = dict(slot._queue[0]["meta"])
    finally:
        slot.task.cancel()
        slot.task = None
    run = AsyncMock()
    monkeypatch.setattr(chat_runner, "_run_chat", run)
    assert await chat_runner._start_next_queued_turn(state, slot)
    await asyncio.wait_for(slot.task, 30)
    row = next(row for row in slot.messages if row["content"] == "hello")
    for metadata in (queued_meta, row["meta"]):
        assert not forbidden.keys() & metadata.keys()
        for key, value in accepted.items():
            assert metadata[key] == value
    assert run.call_args.kwargs["_directive_user_origin"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, "acked", "timeout", "error", "no_turn", "raise"])
async def test_cancel_recovery_distinguishes_no_send_from_uncertainty(fixture, outcome):
    state, slot, request, *_ = fixture
    _busy(slot)
    turn = rc.turn_state(slot)
    prior = [{"role": "user", "content": "Older unfinished work"}]
    turn.recovery = prior
    if outcome == "raise":
        state.sessions.cancel_owned_turn.side_effect = RuntimeError("cancel unavailable")
    else:
        state.sessions.cancel_owned_turn.return_value = outcome
    try:
        await rc.accept(request, slot, "next", None)
        assert turn.interrupted is (outcome is not None)
        if outcome is None:
            assert turn.recovery is prior
        else:
            assert turn.recovery is not prior
            assert turn.recovery[0]["content"] == "Finish the original task"
        assert len(slot._queue) == 1
        state.sessions.reset.assert_not_called()
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_no_send_rollback_does_not_overwrite_new_owner(fixture):
    state, slot, request, *_ = fixture
    _busy(slot)
    newer_owner = object()
    newer_recovery = [{"role": "user", "content": "New owner's work"}]

    async def cancel(*args, **kwargs):
        turn = rc.turn_state(slot)
        turn.owner = newer_owner
        turn.interrupted = True
        turn.recovery = newer_recovery

    state.sessions.cancel_owned_turn.side_effect = cancel
    try:
        await rc.accept(request, slot, "next", None)
        assert rc.turn_state(slot).owner is newer_owner
        assert rc.turn_state(slot).interrupted
        assert rc.turn_state(slot).recovery is newer_recovery
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_hard_stop_teardown_before_resolver_resumes_retained_priority(fixture, monkeypatch):
    state, slot, request, *_ = fixture
    _busy(slot)
    owner = slot.task
    await rc.accept(request, slot, "priority", None)
    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    queue_for_next_turn(state, slot, "ordinary", directive_user_origin=True)
    run = AsyncMock()
    monkeypatch.setattr(chat_runner, "_run_chat", run)

    async def stop_turn(key, **kwargs):
        assert kwargs["preserve_queue"] is True
        assert not await chat_runner._start_next_queued_turn(state, slot)
        await chat_runner._finish_queue_cycle(state, slot, allow_automatic_successor=False)
        assert slot.task is None and len(slot._queue) == 2
        run.assert_not_called()
        owner.cancel()
        await kwargs["on_hard"]()
        return "hard"

    state.sessions.stop_turn = AsyncMock(side_effect=stop_turn)
    await chat_handlers.stop_slot_turn(state, slot)
    assert slot.task is not None, "settled hard stop left the retained priority stranded"
    await asyncio.wait_for(slot.task, 30)
    assert run.call_count == 1
    assert run.call_args.args[2] == "priority"
    assert [q["content"] for q in slot._queue] == ["ordinary"]
    assert slot._stop_state == "idle"
    assert not slot._turn_admission_reserved


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["generation", "replacement", "rebind", "running", "reserved"])
async def test_stop_resolver_cannot_wake_a_different_owner(fixture, monkeypatch, fence):
    state, slot, request, *_ = fixture
    slot._stop_state = "soft_pending"
    await rc.accept(request, slot, "priority", None)
    resolver = chat_handlers._make_stop_resolver(state, slot, "hard", None)
    if fence == "generation":
        slot._stop_state = "idle"
        slot._stop_state = "soft_pending"
    elif fence == "replacement":
        state._slots[slot.key] = _ChatSlot(
            slot.key
        )  # slot-shaped stand-in so background serializer can safely access .key
    elif fence == "rebind":
        slot.linked_session_key = "dashboard:other"
    elif fence == "running":
        slot.task = asyncio.get_running_loop().create_future()
    else:
        slot._turn_admission_reserved = True
    drain = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", drain)
    try:
        await resolver()
        drain.assert_not_called()
        assert len(slot._queue) == 1
    finally:
        if slot.task is not None:
            slot.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_http_responsive_accept_notifies_human_and_observer_once(fixture, monkeypatch, busy):
    state, slot, *_ = fixture
    if busy:
        _busy(slot)
    nudge = MagicMock()
    observer = MagicMock()
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: nudge)
    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: SimpleNamespace(dashboard=SimpleNamespace(on_user_message=observer)),
    )
    monkeypatch.setattr(chat_runner, "_run_chat", AsyncMock())

    @web.middleware
    async def human_claim(request, handler):
        request["is_dashboard_user"] = True
        return await handler(request)

    app = as_owner(web.Application(middlewares=[human_claim]))
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat?ws=1",
                json={"slot": slot.key, "message": "Human input", "responsive": True},
            )
            assert response.status == 200
        if busy:
            slot.task.cancel()
            slot.task = None
            assert await chat_runner._start_next_queued_turn(state, slot)
        await asyncio.wait_for(slot.task, 30)
        nudge.notify_user_input.assert_called_once_with(slot.key, human=True)
        observer.assert_called_once_with(app, "Human input")
    finally:
        if slot.task is not None and not slot.task.done():
            slot.task.cancel()


@pytest.mark.asyncio
async def test_stop_settles_during_final_done_read_resumes_priority(fixture, monkeypatch):
    state, slot, request, *_ = fixture
    _busy(slot)
    owner = slot.task
    slot._stop_state = "soft_pending"
    await rc.accept(request, slot, "priority", None)
    resolver = chat_handlers._make_stop_resolver(state, slot, "hard", None)
    run = AsyncMock()
    monkeypatch.setattr(chat_runner, "_run_chat", run)

    async def done_payload(*args, **kwargs):
        await resolver()
        run.assert_not_called()
        return {"slot": slot.key}

    monkeypatch.setattr(chat_runner, "chat_done_payload", done_payload)
    try:
        await chat_runner._finish_queue_cycle(state, slot, allow_automatic_successor=False)
        assert slot.task is not None, "stop settled during done read but priority stayed queued"
        await asyncio.wait_for(slot.task, 30)
        assert run.call_count == 1 and not slot._queue
    finally:
        owner.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, "timeout", "error", "no_turn"])
async def test_normal_completion_after_cancel_preserves_only_uncertain_recovery(
    tmp_path, monkeypatch, outcome
):
    from test_chat_runner_coverage import _complete, _runner_state, _slot

    from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

    state, client = _runner_state(tmp_path)
    slot = _slot("responsive-normal-completion")
    state._slots[slot.key] = slot
    client.billing_stats = MagicMock(return_value=None)
    client.has_active_turn = MagicMock(return_value=True)
    client.is_alive = MagicMock(return_value=True)
    client.supports_steer = False
    client.client = SimpleNamespace(
        pop_pending_oauth_requests=list,
        awaiting_permission=False,
        last_prompt_stats=None,
        pending_steers=[],
        supports_steer=False,
        has_active_turn=lambda: True,
    )
    state.sessions.is_compacting = lambda key: False
    state.sessions.cancel_owned_turn = AsyncMock(return_value=outcome)
    entered, finish = asyncio.Event(), asyncio.Event()
    prompts = []

    async def stream(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            assert not rc.turn_state(slot).protected
            entered.set()
            await asyncio.wait_for(finish.wait(), 30)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work completed normally.")
        yield _complete()

    client.stream = MagicMock(side_effect=stream)
    original = "Original task CEDAR-23"
    slot.append("user", original)
    task = asyncio.create_task(
        chat_runner._run_chat(
            state,
            slot,
            original,
            _responsive_human=True,
            _directive_user_origin=True,
        )
    )
    slot.task = task
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.get = lambda key, default=None: default
    monkeypatch.setattr("kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda *a: None)
    try:
        await asyncio.wait_for(entered.wait(), 30)
        await rc.accept(request, slot, "New question", None)
        state.sessions.cancel_owned_turn.assert_awaited_once()
        finish.set()
        await asyncio.wait_for(task, 30)
        assert slot.task is not None and slot.task is not task
        await asyncio.wait_for(slot.task, 30)
    finally:
        finish.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30)
    assert len(prompts) == 2
    assert (
        "The work you were doing before this message (bounded transcript reference" in prompts[1]
    ) is (outcome is not None)
    assert rc.turn_state(slot).protected
    assert not rc.turn_state(slot).recovery
    state.sessions.reset.assert_not_called()
