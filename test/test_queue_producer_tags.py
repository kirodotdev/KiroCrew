"""Producer tags on queued prompts that are not the person's words.

A queued entry with no ``kind`` and no actor stamp drains as the person's
message. These tests pin what the crew and peer producers get once they name
themselves, and what stays as before: a peer's message and the person's message
still cancel a pending recovery.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_delivery import QUEUE_PRODUCER_META_KEY, TURN_ACTOR_META_KEY
from kiro_crew.dashboard.chat_turn.recovery import _replay_live
from kiro_crew.dashboard.chat_utils_injected_rows import (
    _PROMISE_ONLY_CONTINUE_MSG,
    AUTOMATION_STEER_META,
    SYNTHETIC_RECOVERY_KIND,
    RecoveryPayload,
)
from kiro_crew.dashboard.recovery_replays import ReplayFamily, replays_of
from kiro_crew.dashboard.session_control_parts.delivery_containment import send_origin_meta
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_SEND_CHOSEN_KEY,
    sanitize_restored_queue,
)


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


def _queue_by(producer: str, state, slot) -> None:
    """Queue one entry as *producer*'s enqueue site does."""
    if producer == "crew":
        meta = {TURN_ACTOR_META_KEY: "crew", QUEUE_PRODUCER_META_KEY: "crew"}
        slot.enqueue_or_run_prompt("crew: next sweep", AsyncMock(), state, extra_meta=meta)
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
@pytest.mark.parametrize(("producer", "actor"), [("peer", "other"), ("crew", "crew")])
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
    [("crew", False), ("peer", True), ("person", True)],
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
    [("crew", False), ("peer", True), ("person", True)],
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


def test_the_restore_strips_a_queue_producer_mark(tmp_path):
    saved = [
        {
            "id": "q1",
            "content": "edited on disk",
            "meta": {
                QUEUE_PRODUCER_META_KEY: "crew",
                TURN_ACTOR_META_KEY: "crew",
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
