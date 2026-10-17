"""Route tests: delivering a recorded answer after a crash.

A pending claim is replayed only by an explicit recover POST. Replay abandons an
answer whose question was replaced, never resends one the model may have
consumed, and finalizes consumption exactly once; a rival turn that takes the
slot mid-relay puts the delivery back to pending.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import types
from pathlib import Path
from typing import Callable

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _decision_spec,
    _make_client,
    _never_touch_the_real_state,
    _recorded_answers,
    _redirect_state,
    _slot_stub,
)


@pytest.mark.asyncio
async def test_a_claim_left_pending_by_a_crash_is_replayed_only_by_a_post(tmp_path, monkeypatch):
    """The durable write must be an outbox, not a premature delivered receipt.

    This is the exact crash window: the claim reaches disk and the process exits before
    `_dispatch_turn`. A replacement gateway's first detail poll may report that recovery
    is needed, but it must stay read-only; the authenticated recovery POST delivers that
    same pending message and then makes the answer final.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)

    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-crash",
    )
    assert outcome == routes._CLAIM_RECORDED

    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    state, _slot = _slot_stub()
    state._background_tasks = set()
    client.app["state"] = state
    await client.start_server()
    try:
        detail_resp = await client.get(f"{_BASE}/specs/s")
        detail = await detail_resp.json()
        assert detail_resp.status == 200, detail
        assert detail.get("decision_recovery_pending") is True
        assert dispatched == [], "a GET request dispatched an agent turn"
        assert [
            entry.get("delivery_id") for entry in await routes._pending_decisions(spec_dir)
        ] == ["delivery-crash"]

        recover_resp = await client.post(
            f"{_BASE}/specs/s/recover-decision",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        recovered = await recover_resp.json()
    finally:
        await client.close()

    assert recover_resp.status == 200, recovered
    assert recovered == {"ok": True}
    assert dispatched == ["Decision - Inbound transport: HTTPS"]
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))
    pending = await routes._pending_decisions(spec_dir)
    assert pending == [], "the replayed outbox entry was not marked final"

    outcome, held = await routes._claim_decision(
        "s",
        "transport",
        "Streaming",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: Streaming",
        delivery_id="delivery-later",
    )
    assert (outcome, held) == (routes._CLAIM_TAKEN, "HTTPS")


@pytest.mark.asyncio
async def test_crash_replay_preserves_a_maximum_length_decision_option(tmp_path, monkeypatch):
    """The durable prompt is bounded without truncating the selected option."""
    title = "T" * routes._MAX_FIELD
    option = "O" * routes._MAX_FIELD
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "transport", "title": title, "options": [option]}]},
    )
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    prompt = routes._decision_answer_prompt(decision, option)
    assert len(prompt) <= routes._MAX_DECISION_PROMPT
    assert prompt.endswith(option)

    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        option,
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message=prompt,
        delivery_id="delivery-long-option",
    )
    assert outcome == routes._CLAIM_RECORDED

    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    state, _slot = _slot_stub()
    state._background_tasks = set()
    client.app["state"] = state
    await client.start_server()
    try:
        detail_resp = await client.get(f"{_BASE}/specs/s")
        detail = await detail_resp.json()
        assert detail_resp.status == 200, detail
        assert detail.get("decision_recovery_pending") is True
        assert dispatched == []
        recover_resp = await client.post(
            f"{_BASE}/specs/s/recover-decision",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        recovered = await recover_resp.json()
    finally:
        await client.close()

    assert recover_resp.status == 200, recovered
    assert recovered == {"ok": True}
    assert dispatched == [prompt]
    assert dispatched[0].endswith(option)
    source = inspect.getsource(routes._handle_message)
    assert "message=_decision_answer_prompt(current_decision, decision_option)" in source


@pytest.mark.asyncio
async def test_replay_abandons_an_answer_for_a_question_that_was_replaced(tmp_path, monkeypatch):
    """Crash recovery must revalidate the durable prompt before relaying it.

    A channel turn can replace the decision after the answer is claimed but before
    delivery. The surviving outbox row names the old fingerprint, so replay must drop
    that unconsumed row rather than inject a stale answer into the new conversation.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-stale",
    )
    assert outcome == routes._CLAIM_RECORDED
    Path(spec_dir, ".spec-state.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "id": "storage",
                        "title": "Storage engine",
                        "options": ["SQLite", "PostgreSQL"],
                    }
                ]
            }
        )
    )

    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    state, _slot = _slot_stub()
    state._background_tasks = set()
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.get(f"{_BASE}/specs/s")
        payload = await resp.json()
        assert resp.status == 200, payload
        assert payload.get("decision_recovery_pending") is True
        assert dispatched == []
        recover_resp = await client.post(
            f"{_BASE}/specs/s/recover-decision",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        recovered = await recover_resp.json()
    finally:
        await client.close()

    assert recover_resp.status == 200, recovered
    assert recovered == {"ok": False}
    assert dispatched == []
    assert await routes._pending_decisions(spec_dir) == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_replay_retains_a_relayed_answer_when_the_model_may_have_consumed_it(
    tmp_path, monkeypatch
):
    """A post-consumption crash must not erase the durable one-way decision.

    The model can consume Q1, write Q2, and then lose the gateway before the watcher
    marks Q1 final. Its persisted delivery row proves relay but not consumption, so
    the durable pre-model relay boundary is the surviving ambiguity marker. Recovery
    cannot safely resend or delete the row; retaining it is the only fail-closed answer.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-maybe-consumed",
    )
    assert outcome == routes._CLAIM_RECORDED
    assert await routes._mark_decision_relayed(
        spec_dir,
        "transport",
        fingerprint,
        "delivery-maybe-consumed",
    )
    Path(spec_dir, ".spec-state.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "id": "storage",
                        "title": "Storage engine",
                        "options": ["SQLite", "PostgreSQL"],
                    }
                ]
            }
        )
    )
    pending = (await routes._pending_decisions(spec_dir))[0]
    state, slot = _slot_stub()
    state._background_tasks = set()
    slot.messages = []
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    assert await routes._deliver_pending_decision(state, slot, spec_dir, pending) is False
    assert dispatched == []
    retained = await routes._pending_decisions(spec_dir)
    assert [entry.get("delivery_id") for entry in retained] == ["delivery-maybe-consumed"]
    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}


@pytest.mark.asyncio
async def test_replay_skips_a_stale_relay_that_precedes_a_current_answer(tmp_path, monkeypatch):
    """A retained ambiguity marker cannot permanently starve a newer answer."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    first, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and first is not None
    first_fingerprint = routes._decision_fingerprint(first)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=first_fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-stale-relay",
    )
    assert outcome == routes._CLAIM_RECORDED
    assert await routes._mark_decision_relayed(
        spec_dir,
        "transport",
        first_fingerprint,
        "delivery-stale-relay",
    )

    Path(spec_dir, ".spec-state.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "id": "storage",
                        "title": "Storage engine",
                        "options": ["SQLite", "PostgreSQL"],
                    }
                ]
            }
        )
    )
    second, usable = routes._current_decision(Path(spec_dir), "storage")
    assert usable and second is not None
    outcome, _held = await routes._claim_decision(
        "s",
        "storage",
        "SQLite",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=routes._decision_fingerprint(second),
        message="Decision - Storage engine: SQLite",
        delivery_id="delivery-current",
    )
    assert outcome == routes._CLAIM_RECORDED

    state, slot = _slot_stub()
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **_k: dispatched.append(a[2]))

    assert await routes._replay_pending_decision(
        state,
        slot,
        "s",
        routes._load_index()["s"],
    )
    assert dispatched == ["Decision - Storage engine: SQLite"]
    retained = await routes._pending_decisions(spec_dir)
    assert retained[0]["delivery_id"] == "delivery-stale-relay"


@pytest.mark.asyncio
async def test_delivery_refuses_to_start_when_the_relay_boundary_cannot_be_saved(
    tmp_path, monkeypatch
):
    """The model cannot consume an answer whose durable ambiguity marker failed."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-no-boundary",
    )
    assert outcome == routes._CLAIM_RECORDED
    pending = (await routes._pending_decisions(spec_dir))[0]
    state, slot = _slot_stub()
    state._background_tasks = set()
    dispatched: list[str] = []

    async def _fail_relay_boundary(*_args, **_kwargs):
        return False

    monkeypatch.setattr(routes, "_mark_decision_relayed", _fail_relay_boundary)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    assert await routes._deliver_pending_decision(state, slot, spec_dir, pending) is False
    assert dispatched == []
    retained = await routes._pending_decisions(spec_dir)
    assert [(entry.get("status"), entry.get("delivery_id")) for entry in retained] == [
        ("pending", "delivery-no-boundary")
    ]


@pytest.mark.asyncio
async def test_a_turn_that_takes_the_slot_during_relay_restores_pending(tmp_path, monkeypatch):
    """A pre-dispatch refusal cannot retain a relay the model never received."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-raced",
    )
    assert outcome == routes._CLAIM_RECORDED
    pending = (await routes._pending_decisions(spec_dir))[0]
    state, slot = _slot_stub()
    state._background_tasks = set()
    reservation = asyncio.create_task(asyncio.sleep(0))
    await reservation
    slot.task = reservation
    real_mark_relayed = routes._mark_decision_relayed

    async def _mark_then_lose_reservation(*args, **kwargs):
        marked = await real_mark_relayed(*args, **kwargs)
        rival = asyncio.create_task(asyncio.sleep(0))
        await rival
        slot.task = rival
        return marked

    monkeypatch.setattr(routes, "_mark_decision_relayed", _mark_then_lose_reservation)
    monkeypatch.setattr(
        routes,
        "_dispatch_turn",
        lambda *_args, **_kwargs: pytest.fail("the raced answer was dispatched"),
    )

    assert not await routes._deliver_pending_decision(
        state,
        slot,
        spec_dir,
        pending,
        turn_reservation=reservation,
    )
    retained = await routes._pending_decisions(spec_dir)
    assert [(entry.get("status"), entry.get("delivery_id")) for entry in retained] == [
        ("pending", "delivery-raced")
    ]


@pytest.mark.asyncio
async def test_a_channel_turn_replacing_the_question_during_claim_cannot_receive_stale_answer(
    tmp_path, monkeypatch
):
    """The slot reservation and final revalidation close the generic-chat race."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    state, slot = _slot_stub()
    state._background_tasks = set()
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    real_claim = routes._claim_decision
    reservation_seen = False

    async def _claim_while_channel_turn_replaces_question(*args, **kwargs):
        nonlocal reservation_seen
        reservation_seen = slot.task is asyncio.current_task()
        outcome = await real_claim(*args, **kwargs)
        Path(spec_dir, ".spec-state.json").write_text(
            json.dumps(
                {
                    "decisions": [
                        {
                            "id": "storage",
                            "title": "Storage engine",
                            "options": ["SQLite", "PostgreSQL"],
                        }
                    ]
                }
            )
        )

        async def _completed_channel_turn() -> None:
            return None

        rival = asyncio.create_task(_completed_channel_turn())
        await rival
        slot.task = rival
        return outcome

    monkeypatch.setattr(routes, "_claim_decision", _claim_while_channel_turn_replaces_question)
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - Inbound transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert reservation_seen, "generic chat could still observe the slot as idle"
    assert resp.status == 409, payload
    assert payload.get("code") == "decision_changed_before_delivery", payload
    assert dispatched == []
    assert await routes._pending_decisions(spec_dir) == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_message_queued_behind_a_refused_reservation_is_drained(monkeypatch):
    """A validation refusal must not strand generic chat queued behind it."""
    from kiro_crew.dashboard import chat_runner

    state, slot = _slot_stub()
    slot._queue = []
    drained = asyncio.Event()

    async def _drain(_state, _slot):
        assert _state is state
        assert _slot is slot
        drained.set()
        return True

    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _drain)

    async def _refused_request() -> None:
        reservation = routes._reserve_slot_turn(state, slot)
        assert reservation is asyncio.current_task()
        slot._queue.append({"id": "queued", "content": "continue"})

    request_task = asyncio.create_task(_refused_request())
    await request_task
    await asyncio.wait_for(drained.wait(), timeout=1)
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))


@pytest.mark.asyncio
async def test_recovery_replays_an_unconsumed_delivery_without_a_second_chat_row(
    tmp_path, monkeypatch
):
    """A durable chat row proves display, not model consumption."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-relayed",
    )
    assert outcome == routes._CLAIM_RECORDED
    assert await routes._mark_decision_relayed(
        spec_dir,
        "transport",
        fingerprint,
        "delivery-relayed",
    )

    dispatched: list[tuple[str, bool]] = []
    consumed_callbacks: list[Callable[[bool], None]] = []

    def _dispatch(*args, **kwargs):
        dispatched.append((args[2], kwargs.get("append_user", True)))
        consumed_callbacks.append(kwargs["on_consumed"])

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    state, slot = _slot_stub()
    state._background_tasks = set()
    slot.messages = [
        {
            "role": "user",
            "content": "Decision - Inbound transport: HTTPS",
            "meta": {"spec_decision_delivery_id": "delivery-relayed"},
        }
    ]
    client.app["state"] = state
    await client.start_server()
    try:
        detail_resp = await client.get(f"{_BASE}/specs/s")
        detail = await detail_resp.json()
        assert detail_resp.status == 200, detail
        assert detail.get("decision_recovery_pending") is True
        assert dispatched == [], "a GET request dispatched an agent turn"

        recover_resp = await client.post(
            f"{_BASE}/specs/s/recover-decision",
            json={"spec_dir": spec_dir, "slot_key": slot_key},
        )
        recovered = await recover_resp.json()
    finally:
        await client.close()

    assert recover_resp.status == 200, recovered
    assert recovered == {"ok": True}
    assert dispatched == [("Decision - Inbound transport: HTTPS", False)]
    assert await routes._pending_decisions(
        spec_dir
    ), "a transcript marker finalized the answer before the model consumed it"

    async def _report_consumed() -> None:
        consumed_callbacks[0](True)

    await asyncio.create_task(_report_consumed())
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))
    assert await routes._pending_decisions(spec_dir) == []


@pytest.mark.asyncio
async def test_a_consumed_delivery_cannot_replay_during_or_after_finalization(
    tmp_path, monkeypatch
):
    """A detail poll's stale pending snapshot must not duplicate a consumed answer."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-settling",
    )
    assert outcome == routes._CLAIM_RECORDED
    stale_pending = (await routes._pending_decisions(spec_dir))[0]

    state, slot = _slot_stub()
    state._background_tasks = set()
    alias_slot = types.SimpleNamespace(running=False, messages=[], task=None)
    turns: list[asyncio.Task[None]] = []
    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        async def _consuming_turn() -> None:
            kwargs["on_consumed"](True)

        dispatched.append(args[2])
        turn = asyncio.create_task(_consuming_turn())
        slot.task = turn
        turns.append(turn)
        return turn

    finalize_started = asyncio.Event()
    allow_finalize = asyncio.Event()
    real_finalize = routes._finalize_decision

    async def _slow_finalize(*args, **kwargs):
        finalize_started.set()
        await allow_finalize.wait()
        return await real_finalize(*args, **kwargs)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    monkeypatch.setattr(routes, "_finalize_decision", _slow_finalize)

    assert await routes._deliver_pending_decision(state, slot, spec_dir, stale_pending) is True
    await turns[0]
    await finalize_started.wait()
    assert (
        await routes._deliver_pending_decision(state, alias_slot, spec_dir, stale_pending) is False
    ), "an alias slot bypassed the directory-wide in-flight settlement"

    allow_finalize.set()
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))
    assert await routes._pending_decisions(spec_dir) == []
    assert (
        await routes._deliver_pending_decision(state, alias_slot, spec_dir, stale_pending) is False
    ), "a stale pending snapshot bypassed the durable final status"
    assert dispatched == ["Decision - Inbound transport: HTTPS"]


@pytest.mark.asyncio
async def test_irreversible_consumption_finalizes_before_the_turn_finishes(tmp_path, monkeypatch):
    """A streamed token closes the crash-replay window immediately."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-irrevocable",
    )
    assert outcome == routes._CLAIM_RECORDED
    pending = (await routes._pending_decisions(spec_dir))[0]

    state, slot = _slot_stub()
    state._background_tasks = set()
    irreversible_reported = asyncio.Event()
    allow_turn_finish = asyncio.Event()
    finalize_done = asyncio.Event()
    real_finalize = routes._finalize_decision

    def _dispatch(*_args, **kwargs):
        on_irreversibly_consumed = kwargs["on_irreversibly_consumed"]

        async def _streaming_turn() -> None:
            await on_irreversibly_consumed()
            irreversible_reported.set()
            await allow_turn_finish.wait()

        turn = asyncio.create_task(_streaming_turn())
        slot.task = turn
        return turn

    async def _observe_finalize(*args, **kwargs):
        finalized = await real_finalize(*args, **kwargs)
        finalize_done.set()
        return finalized

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    monkeypatch.setattr(routes, "_finalize_decision", _observe_finalize)

    assert await routes._deliver_pending_decision(state, slot, spec_dir, pending) is True
    await irreversible_reported.wait()
    await finalize_done.wait()
    assert not allow_turn_finish.is_set()
    assert await routes._pending_decisions(spec_dir) == []

    allow_turn_finish.set()
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))


@pytest.mark.asyncio
async def test_a_failed_finalization_retries_without_replaying_the_consumed_answer(
    tmp_path, monkeypatch
):
    """A transient ledger failure keeps the process-local consumption claim."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-finalize-retry",
    )
    assert outcome == routes._CLAIM_RECORDED
    stale_pending = (await routes._pending_decisions(spec_dir))[0]

    state, slot = _slot_stub()
    state._background_tasks = set()
    alias_slot = types.SimpleNamespace(running=False, messages=[], task=None)
    turns: list[asyncio.Task[None]] = []
    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        async def _consuming_turn() -> None:
            kwargs["on_consumed"](True)

        dispatched.append(args[2])
        turn = asyncio.create_task(_consuming_turn())
        slot.task = turn
        turns.append(turn)
        return turn

    real_finalize = routes._finalize_decision
    finalize_calls = 0

    async def _fail_once_then_finalize(*args, **kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            return False
        return await real_finalize(*args, **kwargs)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    monkeypatch.setattr(routes, "_finalize_decision", _fail_once_then_finalize)

    assert await routes._deliver_pending_decision(state, slot, spec_dir, stale_pending) is True
    await turns[0]
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))
    assert await routes._pending_decisions(spec_dir)

    assert (
        await routes._deliver_pending_decision(state, alias_slot, spec_dir, stale_pending) is False
    )
    assert await routes._pending_decisions(spec_dir) == []
    assert finalize_calls == 2
    assert dispatched == ["Decision - Inbound transport: HTTPS"]


@pytest.mark.asyncio
async def test_a_retracted_consumption_report_leaves_the_delivery_pending(tmp_path, monkeypatch):
    """The first empty response reports completion, then retracts before return."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    decision, usable = routes._current_decision(Path(spec_dir), "transport")
    assert usable and decision is not None
    fingerprint = routes._decision_fingerprint(decision)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint=fingerprint,
        message="Decision - Inbound transport: HTTPS",
        delivery_id="delivery-empty",
    )
    assert outcome == routes._CLAIM_RECORDED

    state, slot = _slot_stub()
    state._background_tasks = set()
    turns: list[asyncio.Task[None]] = []

    def _dispatch(*_args, **kwargs):
        async def _empty_turn() -> None:
            kwargs["on_consumed"](True)
            kwargs["on_consumed"](False)

        turn = asyncio.create_task(_empty_turn())
        turns.append(turn)
        return turn

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    pending = (await routes._pending_decisions(spec_dir))[0]

    assert await routes._deliver_pending_decision(state, slot, spec_dir, pending) is True
    await turns[0]
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))

    assert await routes._pending_decisions(
        spec_dir
    ), "the transient completion report finalized a prompt that was requeued"


@pytest.mark.asyncio
async def test_an_undispatched_answer_is_never_reopened(tmp_path, monkeypatch):
    """The crash window, resolved in the only direction that keeps the guarantee.

    A ledger write and the dispatch it authorizes are two steps and no ordering makes
    them atomic. If the process dies in between, the answer is recorded but undelivered
    -- and it STAYS recorded, across restarts. Reopening it would be a silent reversal of
    a decision that may already have reached the agent, which is the whole defect this
    file exists to prevent; a locked card showing the user's own choice is a stall the
    user clears by saying so in the spec's chat.
    """
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    # Nothing else ran: no dispatch, no second write. A restart changes nothing, since
    # the record carries no process-scoped state that a new process could disown.
    monkeypatch.setattr(routes, "_PROCESS_ID", "9999:a-new-process")

    assert await _recorded_answers(spec_dir) == {"transport": "HTTPS"}
    outcome, held = await routes._claim_decision(
        "s", "transport", "Streaming", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )
    assert outcome == routes._CLAIM_TAKEN, "a recorded answer was reopened after a restart"
    assert held == "HTTPS"
