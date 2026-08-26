"""/side steer + queue invariants — the main chat's busy-send semantics, applied
to the sidecar.

1. Steer: a message marked ``steer`` is injected into the RUNNING side turn and
   does not start a second run.
2. Fall-through: an unavailable/refused steer lands on the queue instead of
   being dropped.
3. Queue mode: without the ``steer`` flag an in-flight submit always queues.
4. Drain: the queue is drained FIFO, one entry per turn.
5. Cancel/edit: a queued entry can be removed (content echoed back for the
   composer) or rewritten in place with order preserved.
6. Bound: the queue refuses past ``MAX_SIDE_QUEUE`` rather than growing without
   limit.
7. No stranding: an entry queued after the turn already finished is drained
   immediately instead of waiting for a finally that already ran.
8. Close wins: closing the side drops the queue, and a late drain from the
   finished turn cannot resurrect it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.handlers.side import (
    api_side_close,
    api_side_open,
    api_side_queue_cancel,
    api_side_queue_edit,
    api_side_turn,
)
from kiro_crew.dashboard.side_state import MAX_SIDE_QUEUE, SideState
from kiro_crew.dashboard.ws import broadcast_side_queue, broadcast_side_result
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


class _ReadyKiroPrerequisiteService(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True


_READY_KIRO_PREREQUISITE = object.__new__(_ReadyKiroPrerequisiteService)


def _make_side_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app["kiro_prerequisite_service"] = _READY_KIRO_PREREQUISITE
    app.router.add_post("/api/chat/slots/{slot}/side/open", api_side_open)
    app.router.add_post("/api/chat/slots/{slot}/side/turn", api_side_turn)
    app.router.add_post("/api/chat/slots/{slot}/side/close", api_side_close)
    app.router.add_delete(
        "/api/chat/slots/{slot}/side/queue/{queue_id}", api_side_queue_cancel
    )
    app.router.add_patch(
        "/api/chat/slots/{slot}/side/queue/{queue_id}", api_side_queue_edit
    )
    return app


def _capture_broadcasts(state) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []

    def _record(msg_type, data):
        events.append((msg_type, data))

    # Both channels: side results broadcast to every client, while queue frames are
    # owner-only, and a caller here cares about the payload rather than the audience.
    state.broadcast_ws = _record
    state.broadcast_ws_owners = _record
    return events


def _install_gated_stream(
    state, monkeypatch, gate: asyncio.Event, *, echo_steers: bool = True
) -> list[str]:
    """Run the REAL ``_run_side_turn`` against a fake stream whose first call
    blocks on *gate*, so a turn stays genuinely in flight while the test posts.

    ``echo_steers`` mirrors a healthy backend: before the turn ends it echoes
    ``steering_consumed`` for whatever is pending, which is what settles a steer.
    Pass False to model a backend that took the write but never injected it.

    Returns the list of prompts the stream saw, in dispatch order — the drain's
    FIFO ordering is asserted against it.
    """
    seen: list[str] = []

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    state.sessions.destroy = AsyncMock()

    async def _fake_stream(
        provider, message, *, on_chunk=None, on_steer_consumed=None, **kwargs
    ):
        seen.append(message)
        if len(seen) == 1:
            await gate.wait()
        if echo_steers and on_steer_consumed is not None:
            slot = next(iter(state._slots.values()))
            pending = [e["text"] for e in (slot._side.steer_pending() if slot._side else [])]
            if pending:
                on_steer_consumed(
                    "".join(f"<user_message>\n{m}\n</user_message>" for m in pending)
                )
        return "answer"

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream
    )
    return seen


def _steerable_provider(state, *, supports: bool = True, live: bool = True) -> list[str]:
    """Register a fake isolated session provider and return the steer sink."""
    steered: list[str] = []

    async def _steer(text: str) -> bool:
        steered.append(text)
        return True

    provider = MagicMock()
    provider.supports_steer = supports
    provider.has_active_turn = MagicMock(return_value=live)
    provider.steer = _steer
    state.sessions.get_provider = MagicMock(return_value=provider)
    return steered


async def _settle(state, timeout: float = 5.0) -> None:
    """Wait for every side background task (including drained ones) to finish."""
    deadline = asyncio.get_running_loop().time() + timeout
    while state._background_tasks:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"side tasks did not settle: {state._background_tasks}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_in_flight_steer_injects_into_the_running_turn(tmp_path, monkeypatch):
    """``steer`` reaches the live turn and does NOT start a second run."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    _install_gated_stream(state, monkeypatch, gate)
    steered = _steerable_provider(state)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        first = await client.post(
            "/api/chat/slots/parent/side/turn", json={"question": "q1"}
        )
        run_id = (await first.json())["run_id"]

        resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "actually use QUIC", "steer": True},
        )
        body = await resp.json()
        assert resp.status == 200, body
        # The RPC returning is not proof of injection — no consumed echo has
        # arrived in this test — so the honest answer is "pending". Claiming
        # delivery here is what duplicated a never-consumed question.
        assert body["ok"] is True
        assert body["pending"] is True, body
        assert body.get("queued") is not True, "still in flight, not demoted"
        assert body["run_id"] == run_id, "steer must not mint a new run"
        assert steered == ["actually use QUIC"]
        assert parent._side.queue == [], "a successful steer must not also queue"

        gate.set()
        await _settle(state)

    user_frames = [
        d for t, d in events if t == "chat.side_result" and d.get("role") == "user"
    ]
    assert user_frames[-1]["steer"] is True
    assert user_frames[-1]["run_id"] == run_id
    stored = [m for m in parent._side.messages if m["role"] == "user"]
    assert stored[-1].get("steer") is True


@pytest.mark.asyncio
async def test_steer_unavailable_falls_through_to_the_queue(tmp_path, monkeypatch):
    """A backend with no steer support must queue, never drop the text."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate)
    _steerable_provider(state, supports=False)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "fallback me", "steer": True},
        )
        body = await resp.json()
        assert resp.status == 200, body
        assert body["queued"] is True
        assert body["depth"] == 1
        assert [e["content"] for e in parent._side.queue] == ["fallback me"]

        gate.set()
        await _settle(state)

    assert "fallback me" in seen, "queued fallback never reached a turn"


@pytest.mark.asyncio
async def test_queue_mode_defers_even_when_steer_is_available(tmp_path, monkeypatch):
    """Without the flag, an in-flight submit queues — the steerable session is
    left untouched (this is the split button's Queue side)."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    _install_gated_stream(state, monkeypatch, gate)
    steered = _steerable_provider(state)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        resp = await client.post(
            "/api/chat/slots/parent/side/turn", json={"question": "later please"}
        )
        assert (await resp.json())["queued"] is True
        assert steered == [], "queue mode must not steer"
        assert [e["content"] for e in parent._side.queue] == ["later please"]

        gate.set()
        await _settle(state)


@pytest.mark.asyncio
async def test_queue_drains_fifo_one_entry_per_turn(tmp_path, monkeypatch):
    """Queued questions run in submission order after the turn completes."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q2"})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q3"})
        assert [e["content"] for e in parent._side.queue] == ["q2", "q3"]

        gate.set()
        await _settle(state)

    # First prompt carries the parent-snapshot envelope; later turns reuse the
    # session and go out bare, so they are byte-comparable.
    assert seen[1:] == ["q2", "q3"], seen
    assert parent._side.queue == []
    drained = [
        d
        for t, d in events
        if t == "chat.side_queue" and d.get("action") == "drain"
    ]
    assert [d["depth"] for d in drained] == [1, 0]


@pytest.mark.asyncio
async def test_queue_cancel_removes_the_entry_and_echoes_content(tmp_path, monkeypatch):
    """Cancel drops the entry and returns its text so the composer can restore it."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        queued = await client.post(
            "/api/chat/slots/parent/side/turn", json={"question": "drop me"}
        )
        qid = (await queued.json())["queue_id"]

        resp = await client.delete(f"/api/chat/slots/parent/side/queue/{qid}")
        body = await resp.json()
        assert resp.status == 200, body
        assert body["content"] == "drop me"
        assert body["depth"] == 0
        assert parent._side.queue == []

        missing = await client.delete(f"/api/chat/slots/parent/side/queue/{qid}")
        assert missing.status == 404

        gate.set()
        await _settle(state)

    assert "drop me" not in seen, "cancelled entry still ran"
    cancels = [
        d for t, d in events if t == "chat.side_queue" and d.get("action") == "cancel"
    ]
    assert cancels and cancels[-1]["queue_id"] == qid


@pytest.mark.asyncio
async def test_queue_edit_rewrites_in_place_preserving_order(tmp_path, monkeypatch):
    """Editing the second of two entries changes only its content."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "keep"})
        second = await client.post(
            "/api/chat/slots/parent/side/turn", json={"question": "old"}
        )
        qid = (await second.json())["queue_id"]

        resp = await client.patch(
            f"/api/chat/slots/parent/side/queue/{qid}", json={"content": "new"}
        )
        assert resp.status == 200, await resp.json()
        assert [e["content"] for e in parent._side.queue] == ["keep", "new"]

        blank = await client.patch(
            f"/api/chat/slots/parent/side/queue/{qid}", json={"content": "   "}
        )
        assert blank.status == 400

        gate.set()
        await _settle(state)

    assert seen[1:] == ["keep", "new"], seen


@pytest.mark.asyncio
async def test_queue_refuses_past_its_bound(tmp_path, monkeypatch):
    """The sidecar lives in memory on the parent slot: the queue must be bounded."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    _install_gated_stream(state, monkeypatch, gate)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        for i in range(MAX_SIDE_QUEUE):
            resp = await client.post(
                "/api/chat/slots/parent/side/turn", json={"question": f"q{i}"}
            )
            assert resp.status == 200, await resp.json()

        overflow = await client.post(
            "/api/chat/slots/parent/side/turn", json={"question": "too much"}
        )
        assert overflow.status == 429
        body = await overflow.json()
        assert str(MAX_SIDE_QUEUE) in body["error"]
        assert len(parent._side.queue) == MAX_SIDE_QUEUE

        gate.set()
        await _settle(state)


@pytest.mark.asyncio
async def test_entry_queued_after_completion_is_not_stranded(tmp_path, monkeypatch):
    """A steer that loses the race to the turn's end must still get answered.

    The drain hook only runs inside a turn's ``finally``. If the turn ends while
    the steer RPC is awaiting, the fallback entry lands on a queue nothing will
    ever visit — so the queue path kicks the drain itself.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate, echo_steers=False)

    async def _steer_after_turn_ends(text: str) -> bool:
        # Let the in-flight turn finish while this RPC is suspended, exactly as a
        # slow stdin.drain() would.
        gate.set()
        await _settle(state)
        return False

    provider = MagicMock()
    provider.supports_steer = True
    provider.has_active_turn = MagicMock(return_value=True)
    provider.steer = _steer_after_turn_ends
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "late question", "steer": True},
        )
        assert (await resp.json())["queued"] is True
        await _settle(state)

    assert "late question" in seen, "entry queued past the finally was stranded"
    assert parent._side.queue == []


@pytest.mark.asyncio
async def test_close_drops_the_queue_and_a_late_drain_cannot_resurrect_it(
    tmp_path, monkeypatch
):
    """Closing mid-turn discards queued text; the finished turn's drain no-ops."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        await client.post(
            "/api/chat/slots/parent/side/turn", json={"question": "orphan"}
        )
        await client.post("/api/chat/slots/parent/side/close", json={})
        assert parent._side is None

        gate.set()
        await _settle(state)

    assert "orphan" not in seen, "close did not discard the queued entry"
    assert parent._side is None


@pytest.mark.asyncio
async def test_a_stale_task_cannot_drain_a_newer_sides_queue(tmp_path, monkeypatch):
    """The drain is identity-checked on run_id, not just on busy-ness.

    A task belonging to a superseded run can reach its ``finally`` long after a
    close/reopen replaced the sidecar. Matching only on ``is_complete`` would let
    it pop the NEW side's queue and dispatch a turn the new run never asked for.
    """
    from kiro_crew.dashboard.handlers.side import _drain_side_queue

    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True)
    parent._side.last_run_id = "run-new"
    parent._side.is_complete = True
    qid = parent._side.queue_append("belongs to the new side")

    dispatched: list[str] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side._dispatch_side_turn",
        lambda state, slot, question: dispatched.append(question) or "run-x",
    )

    _drain_side_queue(state, parent, "run-stale")

    assert dispatched == [], "a stale run drained the current side's queue"
    assert [e["id"] for e in parent._side.queue] == [qid]

    _drain_side_queue(state, parent, "run-new")
    assert dispatched == ["belongs to the new side"]


@pytest.mark.asyncio
async def test_a_steer_that_lands_after_its_run_ended_is_queued_not_claimed(
    tmp_path, monkeypatch
):
    """A steer whose RPC outlives its own turn must not be reported as delivered.

    kiro-cli accepts the write but no live generation consumes it, so claiming
    ``steered`` against whatever run is current now would leave the question
    unanswered — and attribute the bubble to a turn that never saw it.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate, echo_steers=False)

    async def _steer_after_the_run_ends(text: str) -> bool:
        # Let the in-flight turn finish while this RPC is suspended, then report
        # the write as having succeeded.
        gate.set()
        await _settle(state)
        return True

    provider = MagicMock()
    provider.supports_steer = True
    provider.has_active_turn = MagicMock(return_value=True)
    provider.steer = _steer_after_the_run_ends
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "orphan steer", "steer": True},
        )
        body = await resp.json()
        assert body.get("steered") is not True, body
        assert body["queued"] is True
        await _settle(state)

    assert "orphan steer" in seen, "the swallowed steer was never re-delivered"
    assert not any(m.get("steer") for m in parent._side.messages)


@pytest.mark.asyncio
async def test_a_steer_cannot_land_on_a_sidecar_that_was_replaced(tmp_path, monkeypatch):
    """close + reopen during the steer RPC yields a fresh, also-``open`` sidecar.

    Checking only ``open`` would attach the text to a side conversation that
    never asked for it, so the commit is bound to the sidecar OBJECT.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    _install_gated_stream(state, monkeypatch, gate)

    async def _steer_then_replace_the_sidecar(text: str) -> bool:
        parent._side = SideState(open=True)
        return True

    provider = MagicMock()
    provider.supports_steer = True
    provider.has_active_turn = MagicMock(return_value=True)
    provider.steer = _steer_then_replace_the_sidecar
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "wrong side", "steer": True},
        )
        assert resp.status == 409, await resp.json()
        assert parent._side.messages == [], "steer polluted the replacement sidecar"
        assert parent._side.queue == []

        gate.set()
        await _settle(state)


@pytest.mark.asyncio
async def test_an_unconsumed_steer_becomes_a_queue_card_instead_of_vanishing(
    tmp_path, monkeypatch
):
    """``steer()`` returning True only proves the bytes left the process.

    The backend's ``steering_consumed`` echo is the authoritative signal. A steer
    that reaches the process but no generation — the turn ends first — would
    otherwise be reported delivered and then silently lost, so it is requeued as
    an ordinary, cancellable card.
    """
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate, echo_steers=False)
    _steerable_provider(state)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "never consumed", "steer": True},
        )
        # Accepted but unproven while the turn is live: reported as pending.
        # The point of this test is what happens NEXT — the finally must turn
        # it into a visible card rather than letting the question vanish.
        assert (await resp.json())["pending"] is True
        assert [e["text"] for e in parent._side.steer_pending()] == ["never consumed"]

        # The turn ends without the backend ever echoing steering_consumed.
        gate.set()
        await _settle(state)

    assert "never consumed" in seen, "the unconsumed steer was lost"
    assert parent._side.steer_pending() == []
    pushes = [
        d for t, d in events if t == "chat.side_queue" and d.get("action") == "push"
    ]
    assert pushes, "no queue card was broadcast for the orphaned steer"


@pytest.mark.asyncio
async def test_a_consumed_steer_is_settled_and_not_requeued(tmp_path, monkeypatch):
    """The echo proves injection, so that steer must NOT come back as a card.

    Without settling, every steer would be asked a second time.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    state.sessions.destroy = AsyncMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_steer_consumed=None, **kw):
        # The handler registers the steer, then the backend echoes it back
        # ``<user_message>``-wrapped exactly as kiro-cli does.
        parent._side.steer_register("consumed steer")
        if on_steer_consumed:
            on_steer_consumed("<user_message>\nconsumed steer\n</user_message>")
        return "answer"

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream
    )

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        await _settle(state)

    assert parent._side.steer_pending() == []
    assert parent._side.queue == [], "a consumed steer was requeued as a duplicate"


@pytest.mark.asyncio
async def test_a_failed_drain_puts_the_entry_back_instead_of_dropping_it(
    tmp_path, monkeypatch
):
    """The card is already retired on the client when the drain pops it, so a
    dispatch failure must return the text to the queue, not just report."""
    from kiro_crew.dashboard.handlers.side import _drain_side_queue

    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True)
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = True
    parent._side.queue_append("must survive")

    def _boom(*_a, **_k):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side._dispatch_side_turn", _boom
    )

    _drain_side_queue(state, parent, "run-1")

    assert [e["content"] for e in parent._side.queue] == ["must survive"]
    errors = [d for t, d in events if d.get("is_error")]
    assert errors and "back in the queue" in errors[-1]["content"]


@pytest.mark.asyncio
async def test_a_FAILED_steer_cannot_queue_onto_a_replaced_sidecar(tmp_path, monkeypatch):
    """The identity check has to cover the failure path too.

    A steer that returns False still falls through to the queue, and if a
    close+reopen landed during the RPC that queue belongs to a different side
    conversation — one that never asked for this question.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    _install_gated_stream(state, monkeypatch, gate)

    async def _steer_then_replace_the_sidecar(text: str) -> bool:
        parent._side = SideState(open=True)
        return False

    provider = MagicMock()
    provider.supports_steer = True
    provider.has_active_turn = MagicMock(return_value=True)
    provider.steer = _steer_then_replace_the_sidecar
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "wrong side", "steer": True},
        )
        assert resp.status == 409, await resp.json()
        assert parent._side.queue == [], "queued onto the replacement sidecar"
        assert parent._side.messages == []

        gate.set()
        await _settle(state)


@pytest.mark.asyncio
async def test_a_stale_consumption_echo_cannot_settle_a_replacement_sidecar(
    tmp_path, monkeypatch
):
    """An echo belongs to the turn that produced it.

    If a close+reopen swapped the sidecar mid-turn, letting the old turn's echo
    settle the NEW sidecar's pending steer would mean that steer is never
    requeued — the question disappears with no card and no answer.
    """
    from kiro_crew.dashboard.handlers.side import _run_side_turn

    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    original = SideState(open=True)
    original.last_run_id = "run-old"
    original.is_complete = False
    original.steer_register("belongs to the old turn")
    parent._side = original

    replacement = SideState(open=True)
    replacement.steer_register("belongs to the NEW side")

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    state.sessions.destroy = AsyncMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_steer_consumed=None, **kw):
        # The sidecar is replaced part-way through, then the OLD turn's backend
        # echoes a steer naming the replacement's text.
        parent._side = replacement
        if on_steer_consumed:
            on_steer_consumed("<user_message>\nbelongs to the NEW side\n</user_message>")
        return "answer"

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream
    )

    await _run_side_turn(state, parent, "run-old", "q", is_first_turn=True)

    assert [e["text"] for e in replacement.steer_pending()] == [
        "belongs to the NEW side"
    ], "a stale echo settled the replacement sidecar's steer"
    assert replacement.queue == [], "the old turn requeued onto the replacement"


@pytest.mark.asyncio
async def test_requeued_steer_broadcast_is_marked_as_a_head_insert(tmp_path, monkeypatch):
    """A requeued steer goes to the HEAD, so its frame must say so.

    Without the marker a client appends the card and shows a different next
    question than the backend will actually run.
    """
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")
    gate = asyncio.Event()
    seen = _install_gated_stream(state, monkeypatch, gate, echo_steers=False)
    _steerable_provider(state)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        # An ordinary queued entry first, then a steer that never gets consumed.
        await client.post(
            "/api/chat/slots/parent/side/turn", json={"question": "already queued"}
        )
        await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "unconsumed steer", "steer": True},
        )
        gate.set()
        await _settle(state)

    pushes = [
        d for t, d in events if t == "chat.side_queue" and d.get("action") == "push"
    ]
    ordinary = [d for d in pushes if d.get("content") == "already queued"]
    requeued = [d for d in pushes if d.get("content") == "unconsumed steer"]
    assert ordinary and not ordinary[-1].get("front"), "a tail insert claimed front"
    assert requeued and requeued[-1].get("front") is True, (
        "the requeued steer's frame did not mark its head insert"
    )
    # And the backend's own order matches what the marked frames describe: the
    # requeued steer runs before the entry queued after it.
    assert "unconsumed steer" in seen
    assert seen.index("unconsumed steer") < seen.index("already queued")


@pytest.mark.asyncio
async def test_a_steer_consumed_just_before_the_turn_ends_still_reaches_the_transcript(
    tmp_path, monkeypatch
):
    """CONSUMED outranks turn completion.

    If the backend echoes the steer AND the turn finishes before the RPC resumes,
    the question WAS injected and answered. Reporting a demotion instead would
    leave it out of the transcript with no queue card either — delivered and
    invisible, the worst of both.
    """
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    gate = asyncio.Event()
    _install_gated_stream(state, monkeypatch, gate, echo_steers=True)

    async def _steer_then_consume_and_finish(text: str) -> bool:
        # The backend takes the write, echoes consumption, and the turn ends —
        # all while this RPC is still suspended.
        gate.set()
        await _settle(state)
        return True

    provider = MagicMock()
    provider.supports_steer = True
    provider.has_active_turn = MagicMock(return_value=True)
    provider.steer = _steer_then_consume_and_finish
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "consumed late", "steer": True},
        )
        body = await resp.json()
        assert body.get("steered") is True, body
        await _settle(state)

    steered = [m for m in parent._side.messages if m.get("steer")]
    assert [m["content"] for m in steered] == ["consumed late"]
    frames = [
        d
        for t, d in events
        if t == "chat.side_result" and d.get("steer") and d.get("role") == "user"
    ]
    assert frames, "no steer frame was broadcast for a delivered question"
    assert parent._side.queue == [], "a delivered steer was also queued"


@pytest.mark.asyncio
async def test_ledger_capacity_evicts_terminal_entries_but_never_a_pending_one(tmp_path):
    """Capacity must not be resolved by either simple policy.

    Evicting the oldest entry unconditionally can drop a PENDING one, whose
    outcome nobody recorded — so the unanswered question can never be requeued.
    Refusing outright once full kills steering for the rest of the conversation.
    Terminal entries are the only safe thing to evict, and declining is correct
    only when every slot is genuinely in flight.
    """
    from kiro_crew.dashboard.side_state import (
        MAX_STEER_LEDGER,
        STEER_CONSUMED,
        STEER_PENDING,
    )

    side = SideState(open=True)

    # A pending entry survives eviction pressure; a terminal one absorbs it.
    survivor = side.steer_register("must never be evicted")
    assert survivor is not None
    for i in range(MAX_STEER_LEDGER - 1):
        sid = side.steer_register(f"filler-{i}")
        assert sid is not None
        side.steer_mark(sid, STEER_CONSUMED)

    assert len(side.steers) == MAX_STEER_LEDGER
    fresh = side.steer_register("needs room")
    assert fresh is not None, "a full ledger of terminal entries must still accept"
    assert side.steer_state(survivor) == STEER_PENDING, "evicted a pending steer"
    assert len(side.steers) == MAX_STEER_LEDGER

    # Only when EVERY slot is in flight do we decline, so the caller can fall
    # through to the bounded queue instead of accepting an unrecordable steer.
    side2 = SideState(open=True)
    for i in range(MAX_STEER_LEDGER):
        assert side2.steer_register(f"inflight-{i}") is not None
    assert side2.steer_register("one too many") is None


@pytest.mark.asyncio
async def test_an_unproven_steer_is_not_committed_so_it_cannot_duplicate(tmp_path):
    """A steer is only in the transcript once PROVEN consumed.

    The old shape committed on "the RPC returned and the turn is still running",
    which is not proof. When the model never consumed it, the turn's finally
    requeued the entry and the queue ran it as a real turn — so the same question
    appeared twice: once as the optimistic chip, once as the genuine submission.
    """
    from kiro_crew.dashboard.side_state import STEER_PENDING

    side = SideState(open=True)
    side.last_run_id = "run-1"
    sid = side.steer_register("please also cover TLS")
    assert sid is not None
    assert side.steer_state(sid) == STEER_PENDING

    # Nothing is in the transcript yet: registration is not delivery.
    assert [m for m in side.messages if m.get("steer")] == []

    # The echo never names this text, so it stays unproven and settles nothing.
    settled = side.steer_settle(
        "<user_message>\nsomething unrelated\n</user_message>"
    )
    assert settled == [], "an echo that does not name the steer must settle nothing"
    assert [m for m in side.messages if m.get("steer")] == []

    # A proving echo settles it AND is the thing that returns it for rendering,
    # so there is exactly one writer and therefore exactly one transcript row.
    echo = "<user_message>\nplease also cover TLS\n</user_message>"
    settled = side.steer_settle(echo)
    assert [e["text"] for e in settled] == ["please also cover TLS"]
    assert side.steer_settle(echo) == [], (
        "a replayed echo must not settle the same steer twice"
    )


@pytest.mark.asyncio
async def test_the_head_insert_is_bounded_like_every_other_queue_write(tmp_path):
    """`queue_insert_front` used to be unbounded, so repeated unconsumed steers
    could grow the queue past MAX_SIDE_QUEUE without limit."""
    from kiro_crew.dashboard.side_state import MAX_SIDE_QUEUE

    side = SideState(open=True)
    for i in range(MAX_SIDE_QUEUE):
        assert side.queue_append(f"q-{i}") is not None
    assert len(side.queue) == MAX_SIDE_QUEUE

    assert side.queue_insert_front("one too many") is None, (
        "a full queue must refuse a head-insert, not grow past the bound"
    )
    assert len(side.queue) == MAX_SIDE_QUEUE


@pytest.mark.asyncio
async def test_a_steer_is_declined_when_its_requeue_could_not_fit(tmp_path):
    """Acceptance reserves a slot per in-flight steer.

    A pending steer may be requeued at the head when the turn ends. Accepting one
    with no room for that requeue would drop a question already reported as
    delivered, so the refusal belongs at acceptance — where the caller can fall
    through to the queue and the user still sees an answer.
    """
    from kiro_crew.dashboard.side_state import MAX_SIDE_QUEUE

    side = SideState(open=True)
    # One slot left.
    for i in range(MAX_SIDE_QUEUE - 1):
        assert side.queue_append(f"q-{i}") is not None
    assert side.queue_headroom() == 1
    assert side.steer_can_accept() is True

    # That last slot is now reserved by an in-flight steer.
    assert side.steer_register("in flight") is not None
    assert side.steer_can_accept() is False, (
        "the only free slot is reserved for the pending steer's requeue"
    )


def test_every_queued_steer_response_hands_back_a_correlation_handle():
    """A queued steer's card holds REDACTED content, so a response that reports the
    text was queued without a handle leaves the submitter unable to restore its own raw
    text — a cancel then returns the scrubbed copy permanently.

    Asserted over every such return in the steer block rather than one branch: the
    round-20 fix covered `pending` and missed `demoted`, and a per-branch test would
    have missed it again.
    """
    import ast
    import inspect

    from kiro_crew.dashboard.handlers import side as side_mod

    tree = ast.parse(inspect.getsource(side_mod.api_side_turn))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Dict)):
            continue
        keys = {
            k.value
            for k in call.args[0].keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        # Only responses that CLAIM the text was queued need a handle.
        if "queued" not in keys:
            continue
        if not ({"queue_id", "steer_id"} & keys):
            offenders.append(sorted(keys))

    assert not offenders, (
        "these queued responses hand back no correlation handle, so the submitter "
        f"cannot restore its raw text: {offenders}"
    )


@pytest.mark.asyncio
async def test_a_requeued_steer_card_names_the_steer_it_came_from(tmp_path, monkeypatch):
    """The card gets a brand-new id, and its broadcast content is redacted.

    Without the originating ledger id the submitting client has no handle to match its
    own RAW text, so cancelling a credential-bearing steer would restore the scrubbed
    rendering — permanently corrupted.
    """
    from kiro_crew.dashboard.handlers.side import _requeue_unconsumed_side_steers

    state = _make_state(tmp_path)
    frames = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    side = SideState(open=True)
    side.last_run_id = "run-1"
    sid = side.steer_register("deploy with AKIAIOSFODNN7EXAMPLE")
    parent._side = side

    _requeue_unconsumed_side_steers(state, parent, "run-1", side)

    pushes = [d for _t, d in frames if isinstance(d, dict) and d.get("action") == "push"]
    assert pushes, "the unconsumed steer produced no card"
    assert pushes[-1].get("steer_id") == sid, (
        f"the card must name its steer so raw text can be matched: {pushes[-1]}"
    )


@pytest.mark.asyncio
async def test_a_new_submit_does_not_overtake_a_queue_on_an_idle_side(tmp_path, monkeypatch):
    """An auth hold leaves the side idle with questions still waiting.

    Dispatching a fresh submit straight away would run the NEWEST question first and
    leave the earlier ones behind it. The new one joins the tail and the head goes
    next, which is also what restarts a held queue.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    side = SideState(open=True)
    side.last_run_id = "run-done"
    side.is_complete = True
    side.queue_append("asked first")
    parent._side = side

    dispatched: list[str] = []

    def _fake_dispatch(_state, _slot, question):
        dispatched.append(question)
        return "run-next"

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side._dispatch_side_turn", _fake_dispatch
    )

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/parent/side/turn", json={"question": "asked second"}
        )
        body = await resp.json()

    assert body.get("queued") is True, f"a waiting queue must not be overtaken: {body}"
    assert dispatched == ["asked first"], (
        f"the HEAD should have gone next, not the new submit: {dispatched}"
    )


@pytest.mark.asyncio
async def test_an_auth_failure_holds_the_queue_instead_of_draining_it(tmp_path, monkeypatch):
    """A signed-out CLI is not a per-question failure.

    Every queued question would hit the same wall, so draining spends the whole queue
    on identical errors and leaves nothing to resume once the user signs in. The main
    chat already decided this in ``_run_chat`` ("hold the queue intact instead");
    ``/side`` must not disagree. An unconsumed steer is still requeued — it was never
    delivered either — so only the DISPATCH is withheld.
    """
    from kiro_crew.acp.client import AcpAuthRequired
    from kiro_crew.dashboard.handlers.side import _run_side_turn

    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    side = SideState(open=True)
    side.last_run_id = "run-auth"
    side.is_complete = False
    side.queue_append("still want to know this")
    side.queue_append("and this")
    side.steer_register("never delivered")
    parent._side = side

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    state.sessions.destroy = AsyncMock()

    async def _auth_fails(provider, message, *, on_chunk=None, on_steer_consumed=None, **kw):
        raise AcpAuthRequired("kiro-cli is not signed in")

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect", _auth_fails
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._mark_kiro_signed_out", lambda *_a, **_k: None
    )

    await _run_side_turn(state, parent, "run-auth", "q", is_first_turn=True)

    texts = [e["content"] for e in side.queue]
    assert "still want to know this" in texts, "a queued question was spent on the auth error"
    assert "and this" in texts, "a queued question was spent on the auth error"
    # The unconsumed steer is recovered to the HEAD, where a resume finds it first.
    assert texts[0] == "never delivered", f"steer not requeued at the head: {texts}"


@pytest.mark.asyncio
async def test_an_ordinary_submit_cannot_spend_a_slot_reserved_for_a_steer(tmp_path):
    """A reservation only holds if EVERY writer honours it.

    Reserving at steer acceptance while `queue_append` measured raw length let 20
    ordinary submissions fill the queue under an in-flight steer — and that steer's
    requeue then had nowhere to land, losing a question already reported as
    delivered.
    """
    from kiro_crew.dashboard.side_state import MAX_SIDE_QUEUE

    side = SideState(open=True)
    sid = side.steer_register("in flight")
    assert sid is not None

    # One slot is reserved, so only MAX-1 ordinary entries fit.
    for i in range(MAX_SIDE_QUEUE - 1):
        assert side.queue_append(f"q-{i}") is not None, f"entry {i} should fit"
    assert side.queue_append("one too many") is None, (
        "the last slot belongs to the pending steer's requeue"
    )

    # And that reserved slot really is available to the requeue.
    assert side.queue_insert_front("recovered steer") is not None
    assert len(side.queue) == MAX_SIDE_QUEUE


@pytest.mark.asyncio
async def test_queue_mutations_require_an_open_side(tmp_path):
    """Cancel/edit on a closed side is a 409, not a silent success."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        cancel = await client.delete("/api/chat/slots/parent/side/queue/abc")
        assert cancel.status == 409
        edit = await client.patch(
            "/api/chat/slots/parent/side/queue/abc", json={"content": "x"}
        )
        assert edit.status == 409


class TestSideQueueBroadcastScope:
    """The queue frame carries user prose, so it stays inside the owner boundary.

    `_check_slot_ownership` answers 404 when an app asks about a slot it does not own; a
    broadcast that ignored that split would leak the same content the HTTP layer refuses.
    """

    @staticmethod
    def _ws():
        ws = MagicMock()
        ws.closed = False
        ws.send_str = AsyncMock()
        return ws

    def test_a_non_owner_socket_never_receives_queued_text(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        owner, app_socket = self._ws(), self._ws()
        state.register_ws(owner, owner=True)
        state.register_ws(app_socket)

        broadcast_side_queue(
            state,
            slot_key="chat-1",
            action="push",
            queue_id="q1",
            content="my private question",
        )

        owner_payloads = [c.args[0] for c in owner.send_str.call_args_list]
        assert any("my private question" in p for p in owner_payloads), owner_payloads
        assert app_socket.send_str.call_args_list == []

    def test_a_non_owner_socket_never_receives_a_side_result(self, tmp_path) -> None:
        # Same boundary as the queue frame: the steer echo carries the owner's own text.
        state = _make_state(tmp_path)
        owner, app_socket = self._ws(), self._ws()
        state.register_ws(owner, owner=True)
        state.register_ws(app_socket)

        broadcast_side_result(
            state,
            slot_key="chat-1",
            run_id="run-1",
            role="assistant",
            content="the owner's steer text",
        )

        assert any("steer text" in c.args[0] for c in owner.send_str.call_args_list)
        assert app_socket.send_str.call_args_list == []

    def test_the_frame_still_reaches_the_dashboard(self, tmp_path) -> None:
        # Guards the fix from being "safe" by broadcasting to nobody.
        state = _make_state(tmp_path)
        owner = self._ws()
        state.register_ws(owner, owner=True)

        broadcast_side_queue(state, slot_key="chat-1", action="cancel", queue_id="q1")

        assert owner.send_str.call_count == 1


class TestConcurrentCloseDuringBodyRead:
    """`_side` must not be resolved across the body await.

    `_resolve_side_queue_slot` guarantees `_side` at entry, but that guarantee does not survive
    a suspension point: `/side/close` clears it. Reading the body first is what keeps these
    handlers correct, so this drives the exact interleaving rather than trusting the ordering to
    stay put.
    """

    class _StubRequest:
        """Only what these handlers touch, with a `json()` that closes the side mid-request."""

        def __init__(self, app, slot_key: str, queue_id: str, on_json) -> None:
            self.app = app
            self.match_info = {"slot": slot_key, "queue_id": queue_id}
            self._on_json = on_json
            self.headers: dict[str, str] = {}
            self.body_exists = True

        def get(self, key: str, default=None):
            return default

        async def json(self):
            self._on_json()
            return {"client": "tab-1", "content": "edited"}

    @staticmethod
    def _app_with_queued_entry(tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("parent")
        slot._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
        slot._side.queue_append("a queued question")
        app = web.Application()
        app["state"] = state
        return app, slot

    @pytest.mark.asyncio
    async def test_cancel_survives_a_close_during_the_body_read(self, tmp_path) -> None:
        app, slot = self._app_with_queued_entry(tmp_path)
        queue_id = slot._side.queue[0]["id"]

        def close_it() -> None:
            slot._side = None

        req = self._StubRequest(app, "parent", queue_id, close_it)
        resp = await api_side_queue_cancel(req)  # type: ignore[arg-type]

        # The body is read before any state is resolved, so the close is seen as a closed side
        # (a clean 4xx) rather than crashing after the entry was already removed.
        assert resp.status < 500, resp.status

    @pytest.mark.asyncio
    async def test_edit_survives_a_close_during_the_body_read(self, tmp_path) -> None:
        app, slot = self._app_with_queued_entry(tmp_path)
        queue_id = slot._side.queue[0]["id"]

        def close_it() -> None:
            slot._side = None

        req = self._StubRequest(app, "parent", queue_id, close_it)
        resp = await api_side_queue_edit(req)  # type: ignore[arg-type]

        assert resp.status < 500, resp.status
