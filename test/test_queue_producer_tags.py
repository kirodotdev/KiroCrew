"""Producer tags on queued prompts that are not the person's words.

A queued entry with no ``kind`` and no actor stamp drains as the person's
message. These tests pin what the workflow, crew, heartbeat and peer producers
get once they name themselves, and what stays as before: a
peer's message and the person's message still cancel a pending recovery, and a
queued workflow completion still survives a restart as a plain entry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_delivery import QUEUE_PRODUCER_META_KEY, TURN_ACTOR_META_KEY
from kiro_crew.dashboard.chat_turn.recovery import _replay_live
from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND
from kiro_crew.dashboard.chat_utils_injected_rows import (
    _PROMISE_ONLY_CONTINUE_MSG,
    AUTOMATION_STEER_META,
    SYNTHETIC_RECOVERY_KIND,
    WORKFLOW_COMPLETION_INSTRUCTION,
    WORKFLOW_COMPLETION_KIND,
    RecoveryPayload,
)
from kiro_crew.dashboard.recovery_replays import ReplayFamily, replays_of
from kiro_crew.dashboard.session_control_parts.delivery_containment import send_origin_meta
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_SEND_CHOSEN_KEY,
    sanitize_restored_queue,
)
from kiro_crew.dashboard.state import SUBAGENT_COMPLETION_PREFIX


def _workflow_prompt(name: str) -> str:
    return f"[Workflow `{name}` finished: done] Its result was just posted above. {WORKFLOW_COMPLETION_INSTRUCTION}"


def _busy(slot) -> None:
    task = MagicMock()
    task.done.return_value = False
    slot.task = task


def _idle(slot) -> None:
    slot.task = None


async def _drain(state, slot, *, merge: bool = False) -> tuple[bool, list[dict]]:
    """Run the real drain once, recording the turn it would start."""
    seen: list[dict] = []

    def _record(_state, _slot, msg, **kw):
        seen.append({"msg": msg, **kw})
        return MagicMock()

    cfg = MagicMock()
    cfg.load.return_value.dashboard.merge_queued_messages = merge
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", new=MagicMock(side_effect=_record)),
        patch.object(chat_runner, "KiroCrewConfig", cfg),
    ):
        started = await chat_runner._start_next_queued_turn(state, slot)
    return started, seen


def _queue_workflow(state, slot, name: str) -> None:
    assert (
        slot.enqueue_or_run_prompt(
            _workflow_prompt(name), AsyncMock(), state, kind=WORKFLOW_COMPLETION_KIND
        )
        is False
    )


def _queue_by(producer: str, state, slot) -> None:
    """Queue one entry as *producer*'s enqueue site does."""
    if producer == "workflow":
        _queue_workflow(state, slot, "nightly")
    elif producer == "crew":
        meta = {TURN_ACTOR_META_KEY: "crew", QUEUE_PRODUCER_META_KEY: "crew"}
        slot.enqueue_or_run_prompt("crew: next sweep", AsyncMock(), state, extra_meta=meta)
    elif producer == "heartbeat":
        meta = {TURN_ACTOR_META_KEY: "cron", QUEUE_PRODUCER_META_KEY: "heartbeat"}
        slot.enqueue_or_run_prompt("Heartbeat\n\nresult", AsyncMock(), state, extra_meta=meta)
    elif producer == "peer":
        sender = state.get_or_create_slot("conductor")
        meta = {
            **send_origin_meta(state, sender.key),
            TURN_ACTOR_META_KEY: "other",
            QUEUE_PRODUCER_META_KEY: "peer",
        }
        slot.enqueue_or_run_prompt("peer: status?", AsyncMock(), state, extra_meta=meta)
    else:
        slot.queue_append("person: wait, do not open it", directive_user_origin=True)


def _arm_continuation(state, slot) -> None:
    """Queue the promise-only continuation as the turn-end arm does."""
    from kiro_crew.dashboard.session_control import containment_meta

    slot.queue_insert(
        0,
        _PROMISE_ONLY_CONTINUE_MSG,
        kind=SYNTHETIC_RECOVERY_KIND,
        payload=RecoveryPayload.CONTINUATION,
        meta={**containment_meta(state, slot), TURN_ACTOR_META_KEY: "user"},
        directive_user_origin=True,
    )
    replays_of(slot).arm(
        ReplayFamily.CONTINUATION,
        entry_id="",
        session_key=chat_runner.effective_session_key(slot),
        stop_gen=getattr(slot, "_stop_generation", 0),
        session_stop_gen=0,
    )


def _cancel_notices(slot) -> list[str]:
    return [
        m["content"]
        for m in slot.messages
        if m.get("role") == "notice" and "takes over" in m["content"]
    ]


@pytest.mark.asyncio
async def test_a_queued_workflow_completion_drains_as_a_gateway_inject_row(tmp_path):
    state = _state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("wf-row")
    slot._titled = True
    _busy(slot)
    _queue_workflow(state, slot, "nightly")
    _idle(slot)

    started, seen = await _drain(state, slot)

    assert started
    assert seen[0]["_turn_actor"] == "gateway"
    rows = [m for m in slot.messages if m.get("content") == _workflow_prompt("nightly")]
    assert [r["role"] for r in rows] == ["inject"]
    pops = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_pop"]
    assert pops and all(p["drain_writes_row"] for p in pops)


@pytest.mark.asyncio
async def test_an_idle_workflow_completion_writes_an_inject_row(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("wf-idle")
    run = AsyncMock(return_value=None)

    assert slot.enqueue_or_run_prompt(
        _workflow_prompt("nightly"), run, state, kind=WORKFLOW_COMPLETION_KIND
    )

    rows = [m for m in slot.messages if m.get("content") == _workflow_prompt("nightly")]
    assert [r["role"] for r in rows] == ["inject"]
    await slot.task


@pytest.mark.asyncio
async def test_completions_at_the_head_drain_as_one_turn_with_one_instruction(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("wf-batch")
    slot._titled = True
    _busy(slot)
    for name in ("a", "b", "c", "d"):
        _queue_workflow(state, slot, name)
    _idle(slot)

    _started, seen = await _drain(state, slot)

    assert len(seen) == 1 and not slot._queue
    turn_input = seen[0]["msg"]
    assert turn_input.startswith("[4 workflows finished]\n[Workflow `a` finished: done]")
    assert turn_input.count(WORKFLOW_COMPLETION_INSTRUCTION) == 1
    assert [name for name in "abcd" if f"`{name}`" in turn_input] == list("abcd")


@pytest.mark.asyncio
@pytest.mark.parametrize("merge", [False, True])
async def test_a_person_message_between_completions_keeps_its_place(tmp_path, merge):
    state = _state(tmp_path)
    slot = state.get_or_create_slot(f"wf-order-{merge}")
    slot._titled = True
    _busy(slot)
    _queue_workflow(state, slot, "a")
    slot.queue_append("person: ignore workflow b", directive_user_origin=True)
    _queue_workflow(state, slot, "b")

    order = []
    for _ in range(3):
        _idle(slot)
        _started, seen = await _drain(state, slot, merge=merge)
        order.append(seen[0]["msg"])

    assert order[0] == _workflow_prompt("a")
    assert order[1] == "person: ignore workflow b"
    assert order[2] == _workflow_prompt("b")


@pytest.mark.asyncio
async def test_the_sub_agent_hold_still_keeps_a_completion_queued(tmp_path, monkeypatch):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("wf-hold")
    slot._titled = True
    _busy(slot)
    _queue_workflow(state, slot, "a")
    slot.queue_append(f"{SUBAGENT_COMPLETION_PREFIX}\nchild done", kind=SUBAGENT_COMPLETION_KIND)
    _idle(slot)
    monkeypatch.setattr(chat_runner, "subagents_hold_user_messages", lambda *_a, **_k: True)

    _started, seen = await _drain(state, slot)

    assert seen[0]["msg"].startswith(SUBAGENT_COMPLETION_PREFIX)
    assert [q["kind"] for q in slot._queue] == [WORKFLOW_COMPLETION_KIND]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer", "actor"), [("peer", "other"), ("crew", "crew"), ("heartbeat", "cron")]
)
async def test_a_stamped_producer_runs_as_its_actor(tmp_path, producer, actor):
    state = _state(tmp_path)
    slot = state.get_or_create_slot(f"actor-{producer}")
    slot._titled = True
    _busy(slot)
    _queue_by(producer, state, slot)
    _idle(slot)

    _started, seen = await _drain(state, slot)

    assert seen[0]["_turn_actor"] == actor


@pytest.mark.asyncio
async def test_a_peer_message_does_not_merge_into_the_persons_row(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("peer-merge")
    slot._titled = True
    _busy(slot)
    slot.queue_append("person: please also fix the login bug", directive_user_origin=True)
    _queue_by("peer", state, slot)
    _idle(slot)

    _started, seen = await _drain(state, slot, merge=True)

    assert seen[0]["msg"] == "person: please also fix the login bug"
    assert [q["content"] for q in slot._queue] == ["peer: status?"]


@pytest.mark.asyncio
async def test_a_requeued_work_ledger_wake_runs_as_autonudge(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("wake")
    slot._titled = True
    _busy(slot)
    slot.queue_append(
        "wake: 2 reports", meta={AUTOMATION_STEER_META: {"cycle": 3, "loop_id": "L1"}}
    )
    _idle(slot)

    _started, seen = await _drain(state, slot)

    assert seen[0]["_turn_actor"] == "autonudge"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer", "cancels"),
    [("workflow", False), ("crew", False), ("heartbeat", False), ("peer", True), ("person", True)],
)
async def test_only_the_person_and_a_peer_cancel_the_auto_continue(tmp_path, producer, cancels):
    state = _state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot(f"purge-{producer}")
    slot._titled = True
    _busy(slot)
    _arm_continuation(state, slot)
    _queue_by(producer, state, slot)
    _idle(slot)

    _started, seen = await _drain(state, slot)

    assert bool(_cancel_notices(slot)) is cancels
    assert (seen[0]["msg"] == _PROMISE_ONLY_CONTINUE_MSG) is not cancels


@pytest.mark.parametrize("family", list(ReplayFamily))
@pytest.mark.parametrize(
    ("producer", "supersedes"),
    [("workflow", False), ("crew", False), ("heartbeat", False), ("peer", True), ("person", True)],
)
def test_only_the_person_and_a_peer_supersede_a_recovery_replay(
    tmp_path, family, producer, supersedes
):
    state = _state(tmp_path)
    slot = state.get_or_create_slot(f"replay-{family.value}-{producer}")
    _busy(slot)
    replays_of(slot).arm(
        family,
        entry_id="e1",
        session_key=chat_runner.effective_session_key(slot),
        stop_gen=getattr(slot, "_stop_generation", 0),
        session_stop_gen=0,
    )
    _queue_by(producer, state, slot)

    revocation = replays_of(slot).revalidate(family, _replay_live(state, slot))

    assert revocation.superseded is supersedes


def test_a_queued_workflow_completion_survives_a_restart_as_a_plain_entry(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("wf-durable")
    _busy(slot)
    _queue_workflow(state, slot, "nightly")

    saved = slot.durable_queue_entries()
    restored = sanitize_restored_queue(saved)

    assert [e["content"] for e in saved] == [_workflow_prompt("nightly")]
    assert "kind" not in saved[0]
    assert restored[0]["kind"] == ""
    assert "_directive_user_origin" not in restored[0]


def test_the_restore_strips_a_queue_producer_mark(tmp_path):
    saved = [
        {
            "id": "q1",
            "content": "edited on disk",
            "meta": {
                QUEUE_PRODUCER_META_KEY: "heartbeat",
                TURN_ACTOR_META_KEY: "cron",
                "sendId": "s1",
            },
        }
    ]

    restored = sanitize_restored_queue(saved)

    assert QUEUE_PRODUCER_META_KEY not in restored[0]["meta"]
    assert TURN_ACTOR_META_KEY not in restored[0]["meta"]
    assert restored[0]["meta"]["sendId"] == "s1"


@pytest.mark.asyncio
async def test_sending_a_restored_entry_with_a_forged_mark_cancels_the_auto_continue(tmp_path):
    """A restored card runs only when the person sends it, and that send is their intervention."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.chat import api_chat_slot_interrupt

    state = _state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("forged")
    slot._titled = True
    _busy(slot)
    _arm_continuation(state, slot)
    restored = sanitize_restored_queue(
        [{"id": "q1", "content": "stop", "meta": {QUEUE_PRODUCER_META_KEY: "crew"}}]
    )
    slot._queue.extend(restored)
    _idle(slot)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
    cfg = MagicMock()
    cfg.load.return_value.dashboard.merge_queued_messages = False
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()),
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        patch.object(chat_runner, "KiroCrewConfig", cfg),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/forged/interrupt", json={"queue_id": "q1"})
            assert resp.status == 200, await resp.text()

    assert _cancel_notices(slot)
    assert all(q.get("content") != _PROMISE_ONLY_CONTINUE_MSG for q in slot._queue)


@pytest.mark.asyncio
async def test_a_restored_card_whose_start_raises_is_not_left_chosen(tmp_path):
    """A failed Run now leaves the card waiting, so it must not count as the person's send."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.chat import api_chat_slot_interrupt

    state = _state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("raises")
    slot._queue.extend(sanitize_restored_queue([{"id": "q1", "content": "stop", "meta": {}}]))
    _idle(slot)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()),
        patch(
            "kiro_crew.dashboard.chat_handlers._start_next_queued_turn",
            new=AsyncMock(side_effect=RuntimeError("drain failed")),
        ),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/raises/interrupt", json={"queue_id": "q1"})
            assert resp.status == 500

    assert [q["id"] for q in slot._queue] == ["q1"]
    assert not slot._queue[0].get(RESTORED_SEND_CHOSEN_KEY)


def test_a_queued_crew_wake_carries_the_crew_stamps(tmp_path):
    from kiro_crew.apps.builtins.issue_radar.backend.crew_runtime_session import dispatch_crew_turn

    state = _state(tmp_path)
    slot = state.get_or_create_slot("crew-1")
    _busy(slot)

    assert dispatch_crew_turn(state, slot, "crew: next sweep") is False

    meta = slot._queue[0]["meta"]
    assert meta[TURN_ACTOR_META_KEY] == "crew"
    assert meta[QUEUE_PRODUCER_META_KEY] == "crew"


@pytest.mark.asyncio
async def test_a_queued_peer_send_carries_the_other_actor(tmp_path):
    from kiro_crew.dashboard.session_deliver import DELIVERED_QUEUED, Origin, _deliver_peer

    state = _state(tmp_path)
    slot = state.get_or_create_slot("peer-site")
    _busy(slot)

    outcome = await _deliver_peer(state, slot, "peer: status?", Origin(kind="peer"), None, None)

    assert outcome == DELIVERED_QUEUED
    assert slot._queue[0]["meta"][TURN_ACTOR_META_KEY] == "other"
    assert slot._queue[0]["meta"][QUEUE_PRODUCER_META_KEY] == "peer"


@pytest.mark.asyncio
async def test_a_drained_workflow_completion_writes_the_inject_class(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("wf-cls")
    slot._titled = True
    _busy(slot)
    _queue_workflow(state, slot, "nightly")
    _idle(slot)

    await _drain(state, slot)

    rows = [m for m in slot.messages if m.get("content") == _workflow_prompt("nightly")]
    assert [r["cls"] for r in rows] == ["msg msg-inject"]


@pytest.mark.asyncio
async def test_a_card_run_of_one_completion_does_not_take_the_others(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("wf-card")
    slot._titled = True
    _busy(slot)
    for name in ("a", "b"):
        _queue_workflow(state, slot, name)
    first_id = slot._queue[0]["id"]
    _idle(slot)

    seen: list[dict] = []

    def _record(_state, _slot, msg, **kw):
        seen.append({"msg": msg, **kw})
        return MagicMock()

    cfg = MagicMock()
    cfg.load.return_value.dashboard.merge_queued_messages = False
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", new=MagicMock(side_effect=_record)),
        patch.object(chat_runner, "KiroCrewConfig", cfg),
    ):
        await chat_runner._start_next_queued_turn(state, slot, required_queue_id=first_id)

    assert seen[0]["msg"] == _workflow_prompt("a")
    assert [q["content"] for q in slot._queue] == [_workflow_prompt("b")]


@pytest.mark.asyncio
async def test_a_drained_workflow_completion_keeps_the_armed_synthesis(tmp_path):
    state = _state(tmp_path)
    slot = state.get_or_create_slot("wf-synth")
    slot._titled = True
    _busy(slot)
    _queue_workflow(state, slot, "nightly")
    slot._pending_synthesis = True
    _idle(slot)

    await _drain(state, slot)

    assert slot._pending_synthesis is True
