"""Responsive sends preserve one message, human waits and exact turn ownership."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard import responsive_chat as rc
from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.steer_settle import settle_consumed_steers
from kiro_crew.history import HUMAN_TURN_META_KEY


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    state.owner_id = ""
    slot = state.get_or_create_slot("member-default", agent="default", mode="member")
    slot._titled = True
    state.sessions.cancel_owned_turn = AsyncMock(return_value="acked")
    state.sessions.is_compacting = lambda key: False
    monkeypatch.setattr("kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda *a: None)
    monkeypatch.setattr(chat_runner, "_gateway_shutdown_requested", lambda state: False)
    monkeypatch.setattr(chat_runner, "_memory_preparation_pending", lambda state: False)
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig()}
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    binding = {"member": "default", "slot_key": slot.key, "memory_store": ""}
    monkeypatch.setattr(chat_handlers.members_mod, "read_dm_binding_for_slot", lambda key: binding)
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.query = {"ws": "1"}
    claims = {"user": "local-app", "app": ""}
    request.get = lambda key, default=None: claims.get(key, default)
    request.__contains__.side_effect = claims.__contains__
    request.__getitem__.side_effect = claims.__getitem__
    return state, slot, request, cfg, binding, claims


def _busy(slot):
    slot.task = asyncio.get_running_loop().create_future()
    rc.begin_turn(slot, protected=False)
    slot._active_turn_session_key = rc.effective_session_key(slot)
    slot._acp_client = SimpleNamespace(awaiting_permission=False, has_active_turn=lambda: True)
    slot.append("user", "Finish the original task")
    slot.append("tool", "Operation pending; result unknown")


@pytest.mark.asyncio
async def test_owner_dm_only(fixture):
    state, slot, request, cfg, binding, claims = fixture
    assert rc.eligible(request, slot, cfg, binding)
    for patch in ({"app": "other"}, {"user": "someone-else"}, {"internal_auth": True}):
        original = dict(claims)
        claims.update(patch)
        assert not rc.eligible(request, slot, cfg, binding)
        claims.clear()
        claims.update(original)
    slot.mode = ""
    assert not rc.eligible(request, slot, cfg, binding)
    slot.mode = "member"
    assert not rc.eligible(request, slot, cfg, None)
    assert not rc.eligible(request, slot, cfg, {**binding, "member": "other"})
    slot.linked_session_key = "dashboard:unrelated"
    assert not rc.eligible(request, slot, cfg, binding)


@pytest.mark.asyncio
async def test_stable_member_identity_and_store(fixture):
    _, slot, request, cfg, binding, _ = fixture
    cfg.agents["default"].member_id = "stable-member"
    cfg.agents["default"].memory_store = "memory-member"
    assert not rc.eligible(request, slot, cfg, binding)
    binding.update(member_id="stable-member", memory_store="memory-member")
    assert rc.eligible(request, slot, cfg, binding)
    cfg.agents["default"].member_id = "replacement-member"
    assert not rc.eligible(request, slot, cfg, binding)


@pytest.mark.asyncio
async def test_a_waiting_send_evicted_from_the_window_reuses_its_written_row(fixture, monkeypatch):
    state, slot, request, *_ = fixture
    _busy(slot)
    await rc.accept(request, slot, "New question", {"sendId": "send-a"})
    entry = slot._queue[0]
    row = entry["_responsive_row"]
    monkeypatch.setattr("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", 20)
    slot._disk_window_len = len(slot.messages)
    # A long reply streams its text as many chunk rows, plus a few real ones,
    # and the capped window evicts the waiting send's row.
    for i in range(40):
        slot.append("chunk", f"piece {i}")
        if i % 10 == 0:
            slot.append("assistant", f"reply {i}")
    assert not any(existing is row for existing in slot.messages)
    assert rc.reusable_row(slot, [entry], "New question") is row
    assert rc.row_already_written(slot, entry)


@pytest.mark.asyncio
async def test_a_waiting_send_whose_row_left_the_window_without_eviction_writes_again(fixture):
    state, slot, request, *_ = fixture
    _busy(slot)
    await rc.accept(request, slot, "New question", {"sendId": "send-a"})
    entry = slot._queue[0]
    slot.messages.remove(entry["_responsive_row"])
    assert rc.reusable_row(slot, [entry], "New question") is None


@pytest.mark.asyncio
async def test_busy_send_preserves_payload_and_cancels_exact_owner_once(fixture):
    state, slot, request, *_ = fixture
    _busy(slot)
    owner = rc.turn_state(slot).owner
    meta = {"sendId": "send-a", "files": ["/work/My File.txt"], "custom": "value"}
    receipt = json.loads((await rc.accept(request, slot, "New question", meta)).text)
    assert receipt["responsive"] == "cancel_requested"
    assert len(slot._queue) == 1
    entry = slot._queue[0]
    assert entry["content"] == "New question"
    assert entry["meta"]["sendId"] == "send-a"
    assert entry["meta"]["files"] == meta["files"]
    assert "custom" not in entry["meta"]
    assert rc.priority_entry(entry)
    # Written where it was sent, once: the drain reuses this row.
    rows = [row for row in slot.messages if row["content"] == "New question"]
    assert len(rows) == 1 and rows[0]["meta"]["sendId"] == "send-a"
    assert entry["_responsive_row"] is rows[0]
    assert rc.reusable_row(slot, [entry], "New question") is rows[0]
    state.sessions.cancel_owned_turn.assert_awaited_once_with(
        rc.effective_session_key(slot), owner, wait_ack_timeout=rc.CANCEL_ACK_SECONDS
    )
    await rc.accept(request, slot, "Another question", {"sendId": "send-b"})
    assert state.sessions.cancel_owned_turn.await_count == 1
    assert [entry["content"] for entry in slot._queue] == ["New question", "Another question"]
    state.sessions.reset.assert_not_called()
    assert all("Give the useful answer" not in row["content"] for row in slot.messages)
    slot.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wait_kind", ["approval", "question", "native_permission", "compaction", "stop"]
)
async def test_pending_waits_and_compaction_survive(fixture, wait_kind):
    state, slot, request, *_ = fixture
    _busy(slot)
    pending = asyncio.get_running_loop().create_future()
    if wait_kind == "approval":
        slot._approval_futures["approval"] = pending
    elif wait_kind == "question":
        slot._question_pending["question"] = {"blocking": True}
    elif wait_kind == "native_permission":
        slot._acp_client.awaiting_permission = True
    elif wait_kind == "compaction":
        state.sessions.is_compacting = lambda key: True
    else:
        slot._stop_state = "soft_pending"
    await rc.accept(request, slot, "New question", None)
    assert not pending.done()
    assert len(slot._queue) == 1
    state.sessions.cancel_owned_turn.assert_not_called()
    if wait_kind == "question":
        assert slot._question_pending == {"question": {"blocking": True}}
    pending.cancel()
    slot.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "error", "no_turn", None])
async def test_cancel_failure_retains_message_without_reset(fixture, outcome):
    state, slot, request, *_ = fixture
    _busy(slot)
    state.sessions.cancel_owned_turn.return_value = outcome
    result = json.loads((await rc.accept(request, slot, "retained", None)).text)
    assert result["queued"] is True
    assert slot._queue[0]["content"] == "retained"
    state.sessions.reset.assert_not_called()
    slot.task.cancel()


@pytest.mark.asyncio
async def test_concurrent_burst_does_not_issue_second_cancel(fixture):
    state, slot, request, *_ = fixture
    _busy(slot)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_cancel(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=30)
        return "acked"

    state.sessions.cancel_owned_turn.side_effect = held_cancel
    first = asyncio.create_task(rc.accept(request, slot, "one", None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        await rc.accept(request, slot, "two", None)
        assert state.sessions.cancel_owned_turn.await_count == 1
        assert len(slot._queue) == 2
    finally:
        release.set()
        await asyncio.wait_for(first, timeout=30)
        slot.task.cancel()


@pytest.mark.asyncio
async def test_a_send_during_an_earlier_sends_cancel_claim_writes_its_row_below_it(fixture):
    """Send A's cancel claim awaits before A's row is written; send B, arriving
    during that await, must not write its row above A's."""
    state, slot, request, *_ = fixture
    _busy(slot)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_cancel(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=30)
        return "acked"

    state.sessions.cancel_owned_turn.side_effect = held_cancel
    first = asyncio.create_task(rc.accept(request, slot, "first", {"sendId": "send-a"}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        second = json.loads((await rc.accept(request, slot, "second", {"sendId": "send-b"})).text)
        assert second["responsive"] == "queued"
    finally:
        release.set()
        await asyncio.wait_for(first, timeout=30)
        slot.task.cancel()
    users = [row["content"] for row in slot.messages if row["role"] == "user"]
    assert users == ["Finish the original task", "first", "second"]
    assert all(rc.reusable_row(slot, [entry], entry["content"]) for entry in slot._queue)


@pytest.mark.asyncio
async def test_send_behind_a_running_reply_is_written_where_it_was_sent(fixture):
    """The person's chat draws a waiting send where it was sent, with the rest
    of the running reply below it. The transcript must say the same, or the
    turn-end reconcile and a reload move the message below text that came
    after it: the segment is cut at the send and the row written then."""
    state, slot, request, *_ = fixture
    _busy(slot)
    rc.begin_turn(slot, protected=True)
    cuts = []

    def cut(*, interrupted=True):
        cuts.append(interrupted)
        slot.append("assistant", "Reply so far")

    slot._steer_segment_cut = cut
    await rc.accept(request, slot, "Hello", {"sendId": "send-hello"})
    state.sessions.cancel_owned_turn.assert_not_called()
    assert cuts == [False], "the reply goes on, so it is not marked cut off"
    assert [row["content"] for row in slot.messages[-2:]] == ["Reply so far", "Hello"]
    row = slot.messages[-1]
    assert row["role"] == "user"
    assert row["meta"]["sendId"] == "send-hello"
    assert row["meta"][HUMAN_TURN_META_KEY] is True
    entry = slot._queue[0]
    assert rc.reusable_row(slot, [entry], "Hello") is row
    slot.task.cancel()


@pytest.mark.asyncio
async def test_idle_or_stopping_send_writes_no_row_at_send(fixture, monkeypatch):
    _, slot, request, *_ = fixture

    async def start(state, slot):
        return None

    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", start)
    await rc.accept(request, slot, "Idle hello", None)
    assert not any(row["content"] == "Idle hello" for row in slot.messages)
    _busy(slot)
    slot._stop_state = "soft_pending"
    await rc.accept(request, slot, "Stopping hello", None)
    assert not any(row["content"] == "Stopping hello" for row in slot.messages)
    slot.task.cancel()


@pytest.mark.asyncio
async def test_prioritised_answer_turn_is_not_cancelled(fixture):
    state, slot, request, *_ = fixture
    _busy(slot)
    rc.begin_turn(slot, protected=True)
    await rc.accept(request, slot, "next", None)
    state.sessions.cancel_owned_turn.assert_not_called()
    assert slot._queue[0]["content"] == "next"
    slot.task.cancel()


@pytest.mark.asyncio
async def test_idle_parent_answers_ahead_of_children_and_keeps_ordinary_queue(fixture, monkeypatch):
    state, slot, request, *_ = fixture
    seen = []
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *a: True)

    async def run(st, sl, message, **kwargs):
        seen.append((message, kwargs))

    monkeypatch.setattr(chat_runner, "_run_chat", run)
    # A pre-existing ordinary send still waits behind the child-work hold.
    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    queue_for_next_turn(state, slot, "ordinary", directive_user_origin=True)
    result = json.loads((await rc.accept(request, slot, "responsive", {"sendId": "s1"})).text)
    assert result["ok"] is True
    await asyncio.wait_for(slot.task, timeout=30)
    assert [message for message, _ in seen] == ["responsive"]
    assert seen[0][1]["_responsive_human"] is True
    assert [item["content"] for item in slot._queue] == ["ordinary"]
    rows = [row for row in slot.messages if row["role"] == "user"]
    assert len(rows) == 1 and rows[0]["meta"]["sendId"] == "s1"
    state.sessions.cancel_owned_turn.assert_not_called()


@pytest.mark.asyncio
async def test_waiting_burst_drains_as_one_turn_without_duplicate_rows(fixture, monkeypatch):
    """Several sends behind a busy crewmate are one conversation: they reach the
    model together and earn one reply, while each keeps the one bubble it was
    given where it was sent. Merging is independent of the global merge setting."""
    state, slot, request, cfg, *_ = fixture
    cfg.dashboard.merge_queued_messages = False
    _busy(slot)
    await rc.accept(request, slot, "one", {"sendId": "s1"})
    await rc.accept(request, slot, "two", {"sendId": "s2"})
    slot.task.cancel()
    slot.task = None
    seen = []
    anchors = []

    async def run(st, sl, message, **kwargs):
        seen.append(message)
        anchors.append(kwargs.get("_current_message"))

    monkeypatch.setattr(chat_runner, "_run_chat", run)
    assert await chat_runner._start_next_queued_turn(state, slot)
    await asyncio.wait_for(slot.task, timeout=30)
    assert seen == ["[2 queued messages merged]\n\none\n\ntwo"]
    assert slot._queue == []
    rows = [r for r in slot.messages if r.get("content") in ("one", "two")]
    assert [r["meta"]["sendId"] for r in rows] == ["s1", "s2"]
    assert anchors == [rows[-1]]
    assert not any("queued messages merged" in str(r.get("content")) for r in slot.messages)


@pytest.mark.asyncio
async def test_waiting_run_stops_at_a_send_without_its_row(fixture, monkeypatch):
    state, slot, request, *_ = fixture
    _busy(slot)
    await rc.accept(request, slot, "one", {"sendId": "s1"})
    await rc.accept(request, slot, "two", {"sendId": "s2"})
    slot._queue[1].pop("_responsive_row")
    slot.task.cancel()
    slot.task = None
    seen = []

    async def run(st, sl, message, **kwargs):
        seen.append(message)

    monkeypatch.setattr(chat_runner, "_run_chat", run)
    assert await chat_runner._start_next_queued_turn(state, slot)
    await asyncio.wait_for(slot.task, timeout=30)
    assert seen == ["one"]
    assert len(slot._queue) == 1


def test_cron_and_app_queue_entries_never_take_the_responsive_path():
    human = {"content": "hi", rc.RESPONSIVE_KEY: True, "_directive_user_origin": True}
    assert rc.priority_entry(human)
    for other in (
        {**human, "kind": "cron"},
        {**human, "_directive_channel_origin": "slack"},
        {**human, "_directive_user_origin": False},
        {"content": "hi", "_directive_user_origin": True},
    ):
        assert not rc.priority_entry(other)


def test_restore_cannot_mint_responsive_authority():
    raw = [
        {
            "id": "q",
            "content": "hi",
            rc.RESPONSIVE_KEY: True,
            "_directive_user_origin": True,
            "_responsive_protected": True,
            "meta": {"sendId": "s"},
        }
    ]
    restored = sanitize_restored_queue(raw)
    assert restored[0]["content"] == "hi"
    assert restored[0]["meta"]["sendId"] == "s"
    assert not rc.priority_entry(restored[0])
    assert rc.RESPONSIVE_KEY not in restored[0]


@pytest.mark.asyncio
async def test_a_restored_send_reuses_the_row_it_wrote_before_the_restart(fixture):
    """A waiting send already has its row in the transcript. After a restart
    the queue line comes back without its in-memory row link or priority; the
    drain must still find that row instead of writing the message a second
    time, and finding it must not make the entry a priority send again."""
    state, slot, request, *_ = fixture
    _busy(slot)
    rc.begin_turn(slot, protected=True)
    await rc.accept(request, slot, "Hello", {"sendId": "send-hello"})
    row = slot.messages[-1]
    entry = slot._queue[0]
    slot.task.cancel()
    durable = {"id": entry["id"], "content": entry["content"], "meta": entry.get("meta")}
    restored = sanitize_restored_queue([durable])[0]
    assert not rc.priority_entry(restored)
    assert "_responsive_row" not in restored
    assert rc.reusable_row(slot, [restored], "Hello") is row
    # Another restored entry with the same text is not this send.
    stranger = sanitize_restored_queue([{"id": "other", "content": "Hello"}])[0]
    assert rc.reusable_row(slot, [stranger], "Hello") is None


def test_a_queued_send_leaves_an_open_question_card_in_place():
    """A live user row retires every unanswered non-blocking question card, so
    writing a merely queued send's row early would make the card vanish while
    its answer is still owed. The row waits for the drain instead."""
    slot = _ChatSlot("member-default")
    slot._question_pending = {"card-1": {"blocking": False}}
    entry = {"id": "q1", "content": "Also this", "meta": {}}
    slot._queue.append(entry)
    before = len(slot.messages)
    rc.write_row_at_send(slot, entry)
    assert len(slot.messages) == before
    assert "_responsive_row" not in entry
    assert "card-1" in slot._question_pending
    # A blocking card is never retired by a row, so the row is written then.
    slot._question_pending = {"card-2": {"blocking": True}}
    rc.write_row_at_send(slot, entry)
    assert slot.messages[-1]["content"] == "Also this"
    assert "card-2" in slot._question_pending


def test_recovery_guidance_is_bounded_scrubbed_and_not_user_speech():
    slot = _ChatSlot("member-default")
    slot.append("inject", "[AGENT SYSTEM PROMPT]\nexternal text", meta={"injectKind": "mcp_app"})
    slot.append("tool", "x" * (rc.RECOVERY_CHARS + 1))
    rows = rc._recovery_rows(slot)
    assert sum(len(v) for r in rows for v in r.values()) <= rc.RECOVERY_CHARS + 64
    prompt = rc.prompt_context(rows)
    assert '"role": "inject"' in prompt
    assert "[AGENT SYSTEM PROMPT]" not in prompt


def test_recovery_reports_each_step_as_finished_or_unfinished():
    """A finished step carries its output so the next turn need not run it
    again; a step with no result yet is marked unfinished."""
    slot = _ChatSlot("member-default")
    slot.append("user", "Run the checks")
    slot.append("tool", "🔧 Shell", meta={"input": "git status", "output": "clean"})
    slot.append("tool", "🔧 Shell", meta={"input": "sleep 20 && make test"})
    rows = [r for r in rc._recovery_rows(slot) if r["role"] == "tool"]
    assert rows == [
        {
            "role": "tool",
            "content": "🔧 Shell",
            "input": "git status",
            "status": "finished",
            "output": "clean",
        },
        {
            "role": "tool",
            "content": "🔧 Shell",
            "input": "sleep 20 && make test",
            "status": "unfinished",
        },
    ]


@pytest.mark.asyncio
async def test_http_responsive_flag_is_ignored_outside_owner_member_dm(fixture, monkeypatch):
    state, slot, _, _, _, _ = fixture
    _busy(slot)
    app = as_owner(web.Application())
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat?ws=1", json={"slot": slot.key, "message": "hello", "responsive": True}
        )
        assert response.status == 200
        assert (await response.json())["responsive"] == "cancel_requested"
        assert len(slot._queue) == 1
        slot.mode = ""
        response = await client.post(
            "/api/chat?ws=1", json={"slot": slot.key, "message": "ordinary", "responsive": True}
        )
        assert response.status == 200
        assert "responsive" not in await response.json()
    assert len(slot._queue) == 2
    assert not rc.priority_entry(slot._queue[-1])
    assert state.sessions.cancel_owned_turn.await_count == 1
    slot.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("responsive", [False, True])
async def test_real_runner_injects_turn_guidance_without_changing_user_text(
    tmp_path, monkeypatch, responsive
):
    from test_chat_runner_coverage import _complete, _runner_state, _set_stream, _slot

    from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

    state, client = _runner_state(tmp_path)
    slot = _slot("responsive-runner")
    state._slots[slot.key] = slot
    original = "What is the result?"
    slot.append("user", original, meta={"sendId": "runner-send"})
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
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="The result is ready."), _complete()])
    await asyncio.wait_for(
        chat_runner._run_chat(
            state,
            slot,
            original,
            _responsive_human=responsive,
            _directive_user_origin=True,
        ),
        timeout=30,
    )
    sent = client.stream.call_args.args[0]
    assert (rc.RESPONSE_GUIDANCE in sent) is responsive
    assert sent.endswith(original)
    assert [r["content"] for r in slot.messages if r["role"] == "user"] == [original]


@pytest.mark.asyncio
async def test_cancel_rechecks_stop_and_replacement_before_rpc(fixture, monkeypatch):
    state, slot, request, *_ = fixture
    _busy(slot)
    real_enqueue = rc.queue_for_next_turn

    def enqueue_then_stop(*args, **kwargs):
        qid = real_enqueue(*args, **kwargs)
        slot._stop_state = "soft_pending"
        return qid

    monkeypatch.setattr(rc, "queue_for_next_turn", enqueue_then_stop)
    await rc.accept(request, slot, "retained after stop", None)
    assert len(slot._queue) == 1
    state.sessions.cancel_owned_turn.assert_not_called()
    slot.task.cancel()


@pytest.mark.asyncio
async def test_real_turn_keeps_a_waiting_send_where_it_was_sent(fixture, tmp_path, monkeypatch):
    """A send that waits behind a turn which goes on answering (here: its
    priority attempt is already spent) sits in the transcript between the text
    streamed before it and the text after it, and the drain writes no second
    row -- the order the person's chat drew live, and what a reload reads."""
    from test_chat_runner_coverage import _complete, _runner_state, _slot

    from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

    state, client = _runner_state(tmp_path)
    slot = _slot("responsive-order")
    state._slots[slot.key] = slot
    request = SimpleNamespace(app={"state": state})
    client.client = SimpleNamespace(
        pop_pending_oauth_requests=lambda: [],
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
    state.sessions.cancel_owned_turn = AsyncMock(return_value="acked")
    entered, resume = asyncio.Event(), asyncio.Event()
    prompts = []

    async def stream(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Before the send.")
            entered.set()
            await asyncio.wait_for(resume.wait(), timeout=30)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="After the send.")
            yield _complete()
        else:
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Hello back.")
            yield _complete()

    client.stream = MagicMock(side_effect=stream)
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *a: False)
    slot.append("user", "First ask")
    original = asyncio.create_task(
        chat_runner._run_chat(
            state, slot, "First ask", _responsive_human=True, _directive_user_origin=True
        )
    )
    slot.task = original
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        rc.turn_state(slot).attempted = True  # this turn already took its one cancel
        await rc.accept(request, slot, "Hello", {"sendId": "send-hello"})
        resume.set()
        await asyncio.wait_for(original, timeout=30)
        successor = slot.task
        if successor is not None and successor is not original:
            await asyncio.wait_for(successor, timeout=30)
    finally:
        resume.set()
        if not original.done():
            original.cancel()
        await asyncio.wait_for(asyncio.gather(original, return_exceptions=True), timeout=30)
    state.sessions.cancel_owned_turn.assert_not_called()
    spoken = [
        (r["role"], r["content"]) for r in slot.messages if r["role"] in ("user", "assistant")
    ]
    assert spoken == [
        ("user", "First ask"),
        ("assistant", "Before the send."),
        ("user", "Hello"),
        ("assistant", "After the send."),
        ("assistant", "Hello back."),
    ]
    assert prompts[1].endswith("Hello")


@pytest.mark.asyncio
async def test_real_running_turn_hands_one_message_to_same_parent(fixture, tmp_path, monkeypatch):
    from test_chat_runner_coverage import _complete, _runner_state, _slot

    from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent

    state, client = _runner_state(tmp_path)
    slot = _slot("responsive-handoff")
    state._slots[slot.key] = slot
    request = SimpleNamespace(app={"state": state})
    inner = SimpleNamespace(
        pop_pending_oauth_requests=lambda: [],
        awaiting_permission=False,
        last_prompt_stats=None,
        pending_steers=[],
        supports_steer=False,
        has_active_turn=lambda: True,
    )
    client.client = inner
    client.billing_stats = MagicMock(return_value=None)
    client.has_active_turn = MagicMock(return_value=True)
    client.is_alive = MagicMock(return_value=True)
    client.supports_steer = False
    state.sessions.is_compacting = lambda key: False
    entered, cancelled = asyncio.Event(), asyncio.Event()
    prompts = []
    owner_tasks = []

    async def stream(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            owner_tasks.append(asyncio.current_task())
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Working on the original task.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL, tool_call_id="original-call", title="Long operation"
            )
            entered.set()
            await asyncio.wait_for(cancelled.wait(), timeout=30)
            yield _complete("cancelled")
        else:
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK, text="Here is the answer; earlier work is still owed."
            )
            yield _complete()

    client.stream = MagicMock(side_effect=stream)

    async def cancel(key, owner, **kwargs):
        assert owner is owner_tasks[0]
        cancelled.set()
        return "acked"

    state.sessions.cancel_owned_turn = AsyncMock(side_effect=cancel)
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *a: True)
    slot.append("user", "Original task CEDAR-17")
    original = asyncio.create_task(
        chat_runner._run_chat(
            state,
            slot,
            "Original task CEDAR-17",
            _responsive_human=True,
            _directive_user_origin=True,
        )
    )
    slot.task = original
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        await rc.accept(request, slot, "What is the status?", {"sendId": "followup"})
        await asyncio.wait_for(original, timeout=30)
        successor = slot.task
        assert successor is not None and successor is not original
        await asyncio.wait_for(successor, timeout=30)
    finally:
        cancelled.set()
        if not original.done():
            original.cancel()
        await asyncio.wait_for(asyncio.gather(original, return_exceptions=True), timeout=30)
    assert len(prompts) == 2, "the interrupted task was automatically replayed"
    assert "Original task CEDAR-17" in prompts[1]
    assert prompts[1].endswith("What is the status?")
    assert rc.RESPONSE_GUIDANCE in prompts[1]
    assert rc.turn_state(slot).protected
    assert rc.turn_state(slot).owner is None
    assert not rc.turn_state(slot).recovery
    assert [r["content"] for r in slot.messages if r["role"] == "user"] == [
        "Original task CEDAR-17",
        "What is the status?",
    ]
    assert sum(r.get("role") == "tool" for r in slot.messages) == 1
    state.sessions.reset.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("requeue", [False, True])
async def test_http_human_steer_gets_private_guidance_and_requeues_it(
    fixture, monkeypatch, requeue
):
    state, slot, _, *_ = fixture
    _busy(slot)
    wires = []

    async def steer(message):
        wires.append(message)
        return True

    slot._acp_client.supports_steer = True
    slot._acp_client.steer = steer
    app = web.Application()
    app["state"] = state
    as_owner(app)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    text = "Answer this first"
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat?ws=1",
            json={
                "slot": slot.key,
                "message": text,
                "steer": True,
                "meta": {"sendId": "steer-send"},
            },
        )
        assert response.status == 200
        assert (await response.json())["steered"] is True
    assert wires == [rc.RESPONSE_GUIDANCE + text]
    assert slot._pending_steers == [text]
    assert [r["content"] for r in slot.messages if r["content"] == text] == [text]
    assert not any(rc.RESPONSE_GUIDANCE in r["content"] for r in slot.messages)
    state.sessions.cancel_owned_turn.assert_not_called()
    if requeue:
        chat_runner._requeue_unconsumed_steers(state, slot)
        assert slot._queue[0]["content"] == text
        assert slot._queue[0]["meta"]["sendId"] == "steer-send"
        assert rc.priority_entry(slot._queue[0])
        received = []

        async def run(st, sl, message, **kwargs):
            received.append(kwargs)

        monkeypatch.setattr(chat_runner, "_run_chat", run)
        slot.task.cancel()
        slot.task = None
        assert await chat_runner._start_next_queued_turn(state, slot)
        await asyncio.wait_for(slot.task, timeout=30)
        assert received[0]["_responsive_human"] is True
        assert sum(row.get("content") == text for row in slot.messages) == 1
    else:
        chat_runner._settle_consumed_steers(
            slot, f"<user_message>\n{wires[0]}\n</user_message>", state
        )
        assert slot._pending_steers == []
        assert slot._queue == []
        slot.task.cancel()
    assert rc.turn_state(slot).steers == {}


@pytest.mark.asyncio
async def test_non_member_steer_keeps_wire_text(fixture):
    from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

    state, slot, *_ = fixture
    _busy(slot)
    slot.mode = ""
    slot._acp_client.supports_steer = True
    slot._acp_client.steer = AsyncMock(return_value=True)
    await steer_into_running_turn(state, slot, "ordinary steer", user_origin=True)
    slot._acp_client.steer.assert_awaited_once_with("ordinary steer")
    slot.task.cancel()


def test_enriched_steer_echo_does_not_settle_a_wire_collision():
    slot = _ChatSlot("member-default")
    raw = "short"
    slot._steer_delivery_ids[raw] = "delivery"
    wire = rc.steer_message(slot, raw)
    assert rc.settle_steers(slot, [raw, wire], wire, settle_consumed_steers) == [raw, wire]


def test_recovery_anchor_keeps_original_task_after_a_consumed_steer():
    slot = _ChatSlot("member-default")
    opener = slot.append("user", "Original task")
    slot._turn_in_flight_prompt = dict(opener)
    slot.append("tool", "operation whose result is unknown")
    slot.append("user", "a later steer")
    rows = rc._recovery_rows(slot)
    assert [row["content"] for row in rows] == [
        "Original task",
        "operation whose result is unknown",
        "a later steer",
    ]


@pytest.mark.asyncio
async def test_stop_in_progress_retains_priority_at_drain(fixture):
    state, slot, request, *_ = fixture
    _busy(slot)
    slot._stop_state = "soft_pending"
    await rc.accept(request, slot, "retained", None)
    before = list(slot._queue)
    assert not await chat_runner._start_next_queued_turn(state, slot)
    assert slot._queue == before
    slot.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, None, "true", 1])
async def test_http_only_boolean_true_requests_responsive_mode(fixture, value):
    state, slot, *_ = fixture
    _busy(slot)
    app = web.Application()
    app["state"] = state
    as_owner(app)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat?ws=1",
            json={
                "slot": slot.key,
                "message": "keep ordinary semantics",
                "responsive": value,
            },
        )
        assert response.status == 200
        assert "responsive" not in await response.json()
    assert len(slot._queue) == 1
    assert not rc.priority_entry(slot._queue[0])
    state.sessions.cancel_owned_turn.assert_not_called()
    slot.task.cancel()
