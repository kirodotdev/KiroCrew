"""Route tests: answering a decision card.

A claim records one answer per decision before anything is dispatched, pinned to
the spec it was made for. An option the decision does not offer, a running agent,
a delete reservation or unreadable state refuses it, and the detail read overlays
the recorded answer, redacted.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _DEFAULT_DECISION_STATE,
    _auth_mw,
    _decision_spec,
    _final_answer,
    _make_client,
    _never_touch_the_real_state,
    _recorded_answers,
    _redirect_state,
    _slot_stub,
)


def test_normalize_spec_state_ignores_an_agent_authored_lock():
    """The agent must not be able to declare a decision locked (or unlock one it
    answered) by writing the field itself."""
    out = routes._normalize_spec_state(
        {"decisions": [{"id": "a", "title": "t", "locked": True, "answer": "x"}]}
    )
    assert out is not None
    assert out["decisions"][0]["locked"] is False


async def _mark_reserved(name: str) -> bool:
    """Stamp a delete reservation this process owns, as the delete handler does."""

    def _apply(index: dict) -> bool:
        index[name][routes._DELETING] = {"owner": routes._PROCESS_ID, "at": time.time()}
        return True

    return await routes._mutate_index(_apply)


def test_decision_fingerprint_ignores_option_order():
    """Reordering the same choices cannot make a settled question answerable again."""
    first = {
        "id": "transport",
        "title": "Inbound transport",
        "options": ["HTTPS", "WebSocket"],
    }
    reordered = {
        "id": "transport",
        "title": "Inbound transport",
        "options": ["WebSocket", "HTTPS"],
    }

    assert routes._decision_fingerprint(first) == routes._decision_fingerprint(reordered)


@pytest.mark.asyncio
async def test_reusing_an_id_for_a_different_question_does_not_inherit_the_answer(
    tmp_path, monkeypatch
):
    """The id is only one field of a decision's identity.

    An agent may legitimately reuse a short id after replacing a question. Matching the
    durable answer by id alone locks the replacement card to a choice that it never
    offered and prevents the user from answering the new question.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
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
        first = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": "Decision - Inbound transport: HTTPS",
            },
        )
        assert first.status == 200, await first.json()

        Path(spec_dir, ".spec-state.json").write_text(
            json.dumps(
                {
                    "decisions": [
                        {
                            "id": "transport",
                            "title": "Storage engine",
                            "options": ["SQLite", "PostgreSQL"],
                        }
                    ]
                }
            )
        )
        detail = await (await client.get(f"{_BASE}/specs/s")).json()
        replacement = detail["state"]["decisions"][0]

        second = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "SQLite",
                "text": "Decision - Storage engine: SQLite",
            },
        )
        assert second.status == 200, await second.json()
        if state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))

        # Re-emitting the original fingerprint must find its original immutable
        # answer. Keeping only the newest fingerprint lets this third card overwrite
        # the first record and reverse a choice the agent already consumed.
        Path(spec_dir, ".spec-state.json").write_text(json.dumps(_DEFAULT_DECISION_STATE))
        third = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "WebSocket",
                "text": "Decision - Inbound transport: WebSocket",
            },
        )
        third_payload = await third.json()
    finally:
        await client.close()

    assert replacement["locked"] is False
    assert replacement["answer"] == ""
    assert third.status == 409, third_payload
    assert third_payload.get("code") == "decision_already_answered", third_payload
    assert third_payload.get("answer") == "HTTPS", third_payload
    assert dispatched == [
        "Decision - Inbound transport: HTTPS",
        "Decision - Storage engine: SQLite",
    ]


@pytest.mark.asyncio
async def test_a_claim_for_an_absent_decision_is_refused(tmp_path, monkeypatch):
    """A stale card cannot mint a durable answer after its question disappeared."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"decisions": []})
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
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

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_not_found", payload
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_claim_is_refused_when_decision_state_is_unreadable(tmp_path, monkeypatch):
    """A failed state read cannot establish which question the stale card names."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    Path(spec_dir, ".spec-state.json").write_text("{not json")
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
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

    assert resp.status == 503, payload
    assert payload.get("code") == "decision_state_unreadable", payload
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_second_answer_to_one_decision_is_refused(tmp_path, monkeypatch):
    """The first answer dispatches; a later one for the same decision does not.

    This is the case the state file cannot catch: the agent has not written
    ``answer`` yet (its turn has not run), so nothing on disk says the decision is
    settled -- only this backend's record does."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    dispatched: list = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)

    state, _slot = _slot_stub()
    state._background_tasks = set()
    client.app["state"] = state
    await client.start_server()
    try:
        body = {"spec_dir": spec_dir, "slot_key": slot_key, "decision_id": "transport"}
        first = await client.post(
            f"{_BASE}/specs/s/message",
            json={**body, "decision_option": "HTTPS", "text": "Decision - transport: HTTPS"},
        )
        second = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                **body,
                "decision_option": "Streaming",
                "text": "Decision - transport: Streaming",
            },
        )
        payload = await second.json()
    finally:
        await client.close()

    assert first.status == 200
    assert second.status == 409, payload
    assert payload.get("code") == "decision_already_answered", payload
    # The refusal names the answer the agent actually has, so the client can show
    # the settled state rather than a bare error.
    assert payload.get("answer") == "HTTPS", payload
    assert dispatched == [
        "Decision - Inbound transport: HTTPS"
    ], "a second answer reached the agent, reversing a settled decision"


@pytest.mark.asyncio
async def test_concurrent_answers_to_one_decision_dispatch_once(tmp_path, monkeypatch):
    """The double-click race. Both requests read the decision as unanswered, so
    only an atomic claim can keep one of them out -- a client-side lock cannot,
    and neither can a check-then-write."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        body = {"spec_dir": spec_dir, "slot_key": slot_key, "decision_id": "transport"}
        results = await asyncio.gather(
            client.post(
                f"{_BASE}/specs/s/message", json={**body, "decision_option": "A", "text": "A"}
            ),
            client.post(
                f"{_BASE}/specs/s/message", json={**body, "decision_option": "B", "text": "B"}
            ),
        )
        statuses = sorted(r.status for r in results)
    finally:
        await client.close()

    assert statuses == [200, 409], statuses
    assert len(dispatched) == 1, f"both clicks reached the agent: {dispatched}"


@pytest.mark.asyncio
async def test_a_plain_message_is_never_locked(tmp_path, monkeypatch):
    """Only a decision answer is one-way. Ordinary chat keeps working, repeatedly,
    and writes nothing to the ledger."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        body = {"spec_dir": spec_dir, "slot_key": slot_key}
        one = await client.post(f"{_BASE}/specs/s/message", json={**body, "text": "looks good"})
        two = await client.post(f"{_BASE}/specs/s/message", json={**body, "text": "looks good"})
    finally:
        await client.close()

    assert (one.status, two.status) == (200, 200)
    assert len(dispatched) == 2
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_re_emitted_pending_decision_reads_as_answered(tmp_path, monkeypatch):
    """The card that came back. The agent re-writes a settled decision id with
    ``answer: null``; the detail read must still report the recorded answer and
    mark it locked, or the UI renders options for a decision that is closed."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={
            "decisions": [
                {
                    "id": "transport",
                    "title": "Inbound transport",
                    "options": ["HTTPS", "Streaming"],
                    "answer": None,
                }
            ]
        },
    )
    outcome, _held = await routes._claim_decision(
        "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )
    assert outcome == routes._CLAIM_RECORDED

    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    app["state"] = _slot_stub()[0]
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{_BASE}/specs/s")
        data = await resp.json()

    assert resp.status == 200, data
    decision = data["state"]["decisions"][0]
    assert decision["answer"] == "HTTPS", "the agent's pending re-emission won"
    assert decision["locked"] is True


@pytest.mark.asyncio
async def test_a_recorded_answer_is_redacted_on_egress(tmp_path, monkeypatch):
    """index.json is agent-writable, so the ledger is untrusted input like every
    other field -- and the overlay bypasses the state file's own scrub."""
    _redirect_state(monkeypatch, tmp_path)
    secret = "glpat-IOSFODNN7EXAMPLE"
    spec_dir, _slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "d", "title": "Which key", "answer": None}]},
    )
    routes._save_decisions(
        {
            routes._decision_key(spec_dir): {
                "name": "s",
                "answers": {"d": _final_answer("d", f"use {secret}")},
            }
        }
    )

    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    app["state"] = _slot_stub()[0]
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{_BASE}/specs/s")
        data = await resp.json()

    assert secret not in json.dumps(data["state"])


@pytest.mark.asyncio
async def test_a_claim_is_pinned_to_the_spec_it_was_made_for(tmp_path, monkeypatch):
    """A delete-and-recreate in flight must not have an answer stamped onto the
    replacement's entry -- same identity pin every other mutation takes."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    wrong_dir, _ = await routes._claim_decision(
        "s", "d", "A", expect_spec_dir=str(tmp_path / "elsewhere"), expect_slot_key=slot_key
    )
    wrong_key, _ = await routes._claim_decision(
        "s", "d", "A", expect_spec_dir=spec_dir, expect_slot_key="spec-builder-s-other"
    )

    assert wrong_dir == routes._CLAIM_STALE
    assert wrong_key == routes._CLAIM_STALE
    assert await _recorded_answers(spec_dir) == {}


def test_the_claim_is_a_pending_outbox_before_delivery():
    """Source guard for the recoverable claim -> relay -> final ordering."""
    src = inspect.getsource(routes._handle_message)
    claim = src.index("_claim_decision(")
    deliver = src.index("_deliver_pending_decision(")
    assert claim < deliver, "the decision is relayed before its pending claim is durable"
    lock = src.index("async with _turn_lock(")
    assert lock < claim and lock < deliver, (
        "the claim and delivery are not both inside the turn lock, so another handler "
        "can interleave with the outbox relay"
    )
    claim_src = inspect.getsource(routes._claim_decision_locked)
    assert '"status": "pending" if delivery_id else "final"' in claim_src
    deliver_src = inspect.getsource(routes._deliver_pending_decision)
    assert "on_consumed" in deliver_src
    assert "_finalize_decision(" in deliver_src


@pytest.mark.asyncio
async def test_a_claim_is_refused_while_a_delete_is_reserved(tmp_path, monkeypatch):
    """The claim must honour the delete reservation, like every other mutation.

    Without it the claim commits (spec_dir still matches) while the pre-dispatch
    re-pin refuses, because THAT pin does honour the marker. The answer is then
    recorded and never sent -- and a delete that rolls back leaves the entry alive
    with a decision locked to an answer the agent never received, which nothing
    can re-open."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)

    async def _reserve(idx_name: str) -> None:
        def _apply(index: dict) -> bool:
            index[idx_name][routes._DELETING] = {"owner": routes._PROCESS_ID, "at": time.time()}
            return True

        await routes._mutate_index(_apply)

    await _reserve("s")
    outcome, _held = await routes._claim_decision(
        "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )

    assert outcome == routes._CLAIM_STALE
    assert (
        await _recorded_answers(spec_dir) == {}
    ), "an answer was recorded onto a spec whose delete was in flight"


@pytest.mark.asyncio
async def test_an_answer_without_its_option_is_refused(tmp_path, monkeypatch):
    """The option is what gets recorded and later rendered. A decision answer that
    carries no option cannot be recorded honestly, so it is refused rather than
    falling back to the composed prompt."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 400, payload
    assert payload.get("code") == "decision_option_required", payload
    assert dispatched == []


@pytest.mark.asyncio
async def test_the_card_shows_the_option_not_the_whole_prompt(tmp_path, monkeypatch):
    """The agent receives a canonical prompt while the card records only the choice."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={
            "decisions": [{"id": "transport", "title": "Inbound transport", "options": ["HTTPS"]}]
        },
    )
    dispatched: list = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)

    state, _slot = _slot_stub()
    state._background_tasks = set()
    client.app["state"] = state
    await client.start_server()
    try:
        client_prompt = "Decision — “Inbound transport”: HTTPS"
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
                "text": client_prompt,
            },
        )
        assert resp.status == 200, await resp.json()
        if state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))
        detail = await (await client.get(f"{_BASE}/specs/s")).json()
    finally:
        await client.close()

    assert dispatched == [
        "Decision - Inbound transport: HTTPS"
    ], "the agent must receive the server-built prompt"
    assert (
        detail["state"]["decisions"][0]["answer"] == "HTTPS"
    ), "the card renders the composed prompt instead of the chosen option"


@pytest.mark.asyncio
async def test_a_delete_that_lands_after_the_repin_strands_no_claim(tmp_path, monkeypatch):
    """The window the in-claim reservation check does NOT cover, closed by ordering.

    A DELETE reserving after the pre-dispatch re-pin would otherwise reach a claim that
    has already committed: the dispatch is refused, and when the delete rolls back the
    spec returns with a decision locked to an answer the agent never received.
    With the claim as the last await, the same delete makes the CLAIM refuse, so
    nothing is recorded and the user can answer again."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    real_touch = routes._touch_spec
    calls: list[int] = []

    async def _touch_then_reserve(name, **kw):
        out = await real_touch(name, **kw)
        calls.append(1)
        # The second _touch_spec is the pre-dispatch re-pin; the delete reserves
        # immediately after it returns, which is the window under test.
        if len(calls) == 2:

            def _apply(index: dict) -> bool:
                index[name][routes._DELETING] = {"owner": routes._PROCESS_ID, "at": time.time()}
                return True

            await routes._mutate_index(_apply)
        return out

    monkeypatch.setattr(routes, "_touch_spec", _touch_then_reserve)

    state, _slot = _slot_stub()
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
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {}, (
        "an answer stayed recorded although its dispatch was refused -- the decision "
        "is now locked to something the agent never received"
    )


@pytest.mark.asyncio
async def test_an_answer_is_refused_while_the_agent_is_running(tmp_path, monkeypatch):
    """A running slot QUEUES the message, and Pause clears that queue by design --
    so a queued answer may never be delivered while the ledger claims it was.

    Refused instead of queued, and nothing recorded, so the user can answer again
    once the turn ends."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))
    # The gate sits BEFORE the claim, so a busy click never touches the ledger at
    # all -- it does not record-then-release, which would churn index.json (and
    # leave a lock behind if the release failed).
    claims: list = []
    real_claim = routes._claim_decision

    async def _record_claim(*a, **kw):
        claims.append(a)
        return await real_claim(*a, **kw)

    monkeypatch.setattr(routes, "_claim_decision", _record_claim)

    state, slot = _slot_stub()
    slot.running = True
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
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_agent_busy", payload
    assert dispatched == []
    assert claims == [], "a busy click reached the ledger instead of being refused first"
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_a_plain_message_still_queues_behind_a_running_turn(tmp_path, monkeypatch):
    """The busy refusal is scoped to decision answers. Ordinary chat keeps its
    queue-behind-the-turn behaviour -- that is how a user steers a working agent."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, slot = _slot_stub()
    slot.running = True
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/message",
            json={"spec_dir": spec_dir, "slot_key": slot_key, "text": "also check the auth flow"},
        )
    finally:
        await client.close()

    assert resp.status == 200
    assert dispatched == ["also check the auth flow"]


@pytest.mark.asyncio
async def test_a_delete_reserved_during_the_claim_records_nothing(tmp_path, monkeypatch):
    """The window a separate liveness hop left open: a DELETE reserving between the
    check and the write. Both now happen under ``_INDEX_LOCK`` in one hop, and
    ``_mark_deleting`` takes that same lock, so the two serialize."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert await _mark_reserved("s")

    outcome, _held = await routes._claim_decision(
        "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
    )

    assert outcome == routes._CLAIM_STALE
    assert await _recorded_answers(spec_dir) == {}


def test_the_claim_checks_liveness_and_writes_in_one_transaction():
    """Source guard: the index read and the ledger write must sit inside the SAME
    ``_INDEX_LOCK`` block, not in two hops. Split across hops, a delete reserving in
    between has its answer recorded for a spec already being torn down."""
    src = inspect.getsource(routes._claim_decision_locked)
    lock = src.index("with _INDEX_LOCK:")
    assert src.index("_spec_is_live(") > lock
    assert src.index("_save_decisions(") > lock
    assert "await" not in src, "the transaction body awaits, so it is not one hop"
    # And the async wrapper must do nothing but hand it to a worker thread.
    wrapper = inspect.getsource(routes._claim_decision)
    assert "asyncio.to_thread(" in wrapper
    assert "_read_decisions(" not in wrapper


@pytest.mark.asyncio
async def test_the_detail_read_serves_the_recorded_answer(tmp_path, monkeypatch):
    """The overlay still applies now that it happens inside the document hop."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "transport", "title": "Inbound", "options": ["HTTPS"]}]},
    )
    assert (
        await routes._claim_decision(
            "s", "transport", "HTTPS", expect_spec_dir=spec_dir, expect_slot_key=slot_key
        )
    )[0] == routes._CLAIM_RECORDED

    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    app["state"] = _slot_stub()[0]
    async with TestClient(TestServer(app)) as client:
        data = await (await client.get(f"{_BASE}/specs/s")).json()

    decision = data["state"]["decisions"][0]
    assert decision["locked"] is True
    assert decision["answer"] == "HTTPS"


@pytest.mark.asyncio
async def test_a_failed_ledger_write_is_a_named_refusal_not_a_500(tmp_path, monkeypatch):
    """A full or unwritable data home must not 500.

    A 500 carries no code the client can act on, so its optimistic lock would stay
    while NOTHING was recorded or dispatched -- a locked card for an answer the agent
    never got. The named pre-dispatch refusal is what lets the card re-open."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    def _no_space(_store: dict) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(routes, "_save_decisions", _no_space)

    state, _slot = _slot_stub()
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
                "text": "Decision - transport: HTTPS",
            },
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 503, payload
    assert payload.get("code") == "decision_record_write_failed", payload
    assert dispatched == []


def test_a_duplicate_decision_id_is_dropped():
    """The id IS the identity: the ledger is keyed on it and the overlay matches on it,
    so two cards claiming one id would both settle when either is answered -- and the
    second would display an answer chosen for the first. The FIRST occurrence wins."""
    out = routes._normalize_spec_state(
        {
            "decisions": [
                {"id": "transport", "title": "Inbound transport", "options": ["HTTPS"]},
                {"id": "transport", "title": "Something else entirely", "options": ["A"]},
            ]
        }
    )
    assert out is not None
    assert [d["title"] for d in out["decisions"]] == ["Inbound transport"]


def test_nothing_writes_the_ledger_after_the_dispatch():
    """Source guard: the claim is the LAST ledger write before the dispatch, and there
    is no write after it.

    A post-dispatch write to mark the answer delivered cannot work: its own failure is
    indistinguishable from the crash it would be detecting, so a reader could only treat
    the record as unconfirmed -- reopening a delivered answer. The absence of that write
    is the invariant, so it is worth pinning.
    """
    src = inspect.getsource(routes._handle_message)
    assert "_claim_decision(" in src
    after = src[src.index("_dispatch_turn(") :]
    for writer in ("_claim_decision", "_save_decisions", "_confirm_decision", "_forget_decisions"):
        assert writer not in after, f"{writer} runs after the dispatch"


async def _answer(client, state, *, spec_dir, slot_key, option, decision_id="transport"):
    """POST a decision answer, returning (status, payload)."""
    resp = await client.post(
        f"{_BASE}/specs/s/message",
        json={
            "spec_dir": spec_dir,
            "slot_key": slot_key,
            "decision_id": decision_id,
            "decision_option": option,
            "text": f"Decision - {decision_id}: {option}",
        },
    )
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_an_option_the_decision_no_longer_offers_is_refused(tmp_path, monkeypatch):
    """A card is a snapshot; the decision behind it can be re-emitted.

    Same id, revised options: a tab still showing the old render posts an option the
    replacement never offered. The claim would record it and the overlay would lock
    the card -- permanently -- to a choice the user cannot see and the agent cannot
    honour. Nothing downstream can undo that, because the ledger is never rewritten.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={"decisions": [{"id": "transport", "title": "Inbound", "options": ["gRPC", "AMQP"]}]},
    )
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        # "HTTPS" was on the card this client rendered; the decision now offers neither.
        status, payload = await _answer(
            client, state, spec_dir=spec_dir, slot_key=slot_key, option="HTTPS"
        )
    finally:
        await client.close()

    assert status == 409, f"{status}: {payload}"
    assert payload["code"] == "decision_option_not_offered", payload
    assert dispatched == [], "the stale option reached the agent"
    assert await _recorded_answers(spec_dir) == {}, (
        "the stale option was recorded, so the card is now locked to a choice the "
        "decision does not offer"
    )


@pytest.mark.asyncio
async def test_a_decision_replaced_while_the_answer_waits_for_the_lock_is_refused(
    tmp_path, monkeypatch
):
    """Validation must describe state after the preceding serialized turn.

    A request can parse a still-visible card while another turn owns the directory
    lock. If that turn replaces the question before releasing the lock, the waiting
    request must validate again inside the lock rather than deliver its stale answer.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    held = routes._turn_lock(spec_dir)
    await held.acquire()
    lock_attempted = asyncio.Event()

    class _ProbedLock:
        async def __aenter__(self):
            lock_attempted.set()
            await held.acquire()
            return held

        async def __aexit__(self, *_exc):
            held.release()

    monkeypatch.setattr(routes, "_turn_lock", lambda _key: _ProbedLock())

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        post = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/s/message",
                json={
                    "spec_dir": spec_dir,
                    "slot_key": slot_key,
                    "decision_id": "transport",
                    "decision_option": "HTTPS",
                    "text": "Decision - transport: HTTPS",
                },
            )
        )
        await lock_attempted.wait()
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
        held.release()
        resp = await post
        payload = await resp.json()
    finally:
        if held.locked():
            held.release()
        await client.close()

    assert resp.status == 409, payload
    assert payload.get("code") == "decision_not_found", payload
    assert dispatched == []
    assert await _recorded_answers(spec_dir) == {}


@pytest.mark.asyncio
async def test_an_option_the_decision_still_offers_is_accepted(tmp_path, monkeypatch):
    """The boundary: this must not refuse the ordinary answer.

    Without this, a fix that refused everything would still pass the test above.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path,
        state={
            "decisions": [{"id": "transport", "title": "Inbound", "options": ["HTTPS", "gRPC"]}]
        },
    )
    dispatched: list = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: dispatched.append(a[2]))

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        status, payload = await _answer(
            client, state, spec_dir=spec_dir, slot_key=slot_key, option="HTTPS"
        )
    finally:
        await client.close()

    assert status == 200, f"{status}: {payload}"
    assert await _recorded_answers(spec_dir) == {
        "transport": "HTTPS"
    }, "a legitimate answer was not recorded"
    assert dispatched, "a legitimate answer never reached the agent"


@pytest.mark.asyncio
async def test_a_decision_with_no_declared_options_accepts_any_answer(tmp_path, monkeypatch):
    """Carve-out one, and it is structural: nothing to violate.

    A decision that declares no options constrains nothing, so there is no offer an
    answer can fall outside of. Refusing here would break free-form decisions.
    """
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(
        tmp_path, state={"decisions": [{"id": "transport", "title": "Inbound"}]}
    )
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: None)

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        status, payload = await _answer(
            client, state, spec_dir=spec_dir, slot_key=slot_key, option="anything at all"
        )
    finally:
        await client.close()

    assert status == 200, f"{status}: {payload}"


@pytest.mark.asyncio
async def test_a_decision_absent_from_state_is_refused(tmp_path, monkeypatch):
    """A stale card cannot claim an id after the current question disappeared."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path, state={"decisions": []})
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **k: None)

    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        status, payload = await _answer(
            client, state, spec_dir=spec_dir, slot_key=slot_key, option="HTTPS"
        )
    finally:
        await client.close()

    assert status == 409, f"{status}: {payload}"
    assert payload.get("code") == "decision_not_found", payload
    assert await _recorded_answers(spec_dir) == {}
