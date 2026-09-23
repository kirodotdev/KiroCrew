"""``steer: "auto"`` at POST /api/chat: which path the answer takes, and who may ask.

The decision itself is ``test_decisions_message_steer.py``. This file pins the
wiring, and the two claims that matter are negative ones: a MANUAL Steer or Queue
never reaches the point (:class:`TestTheManualModesAreUnchanged`), and neither does
an app-authenticated send (:class:`TestOnlyTheOwnersOwnSendIsDecided`). The first
is what makes "nothing about the two shipped modes moves" checkable; the second is
the same boundary the steer branch already draws for provenance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.decisions.points import message_steer as ms

#: What the point returns for each arm, and the row the handler stamps from it.
_QUEUE = {"turn_id": "t-queue", "choice": ms.CHOICE_QUEUE, "p": 0.83, "latency_ms": 190}
_STEER = {"turn_id": "t-steer", "choice": ms.CHOICE_STEER, "p": 0.91, "latency_ms": 120}


def _record(decided):
    """The outcome row ``record_outcome`` would return for *decided*."""
    return {
        "point": ms.POINT,
        "turn_id": decided["turn_id"],
        "choice": decided["choice"],
        "baseline": ms.CHOICE_STEER,
        "p": decided["p"],
        "latency_ms": decided["latency_ms"],
    }


@pytest.fixture
def _patch_sel():
    """Patch sel() so the handler does not touch a real SecurityEventLog."""
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


@pytest.fixture
def decision(monkeypatch):
    """Install one point answer; returns the list of sends it was asked about.

    Patched at the POINT, not at ``decide``: this file is about the handler's own
    branch, and the seam's gates have their own tests. ``record_outcome`` is patched
    beside it so no day-file is written for a wiring test.
    """

    def _install(decided):
        asked: list[dict] = []

        async def _steer_or_queue(text, **kwargs):
            asked.append({"text": text, "kwargs": kwargs})
            return decided

        monkeypatch.setattr(ms, "steer_or_queue", _steer_or_queue)
        monkeypatch.setattr(
            ms,
            "record_outcome",
            lambda session_key, d: _record(d) if d else None,
        )
        return asked

    return _install


@pytest.fixture
def never_asked(monkeypatch):
    """Fail the test if the point is entered at all."""

    async def _steer_or_queue(text, **kwargs):  # pragma: no cover - must never run
        pytest.fail("the point was asked on a send that must not be decided")

    monkeypatch.setattr(ms, "steer_or_queue", _steer_or_queue)


def _running_slot(state, key="test"):
    """A slot that looks like it has a turn in flight, with a steer-capable client."""
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client_mock = MagicMock()
    client_mock.supports_steer = True
    client_mock.steer = AsyncMock(return_value=True)
    slot._acp_client = client_mock
    return slot


def _strip_of(slot, role="user"):
    """The decision record on the newest row of *role*, or ``None``."""
    for row in reversed(slot.messages):
        if row.get("role") == role:
            return (row.get("meta") or {}).get("decisions_strip")
    return None


class TestTheAnswerPicksThePath:
    @pytest.mark.asyncio
    async def test_queue_sends_the_message_to_the_queue(
        self, tmp_path, monkeypatch, _patch_sel, decision
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        asked = decision(_QUEUE)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"slot": "test", "message": "and bump the version", "steer": "auto"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        assert asked and asked[0]["text"] == "and bump the version"
        # A queue answer must not inject: nothing reaches the live client.
        slot._acp_client.steer.assert_not_awaited()
        assert slot._queue[-1]["meta"]["decisions_strip"] == _record(
            _QUEUE
        ), "the drain unions entry meta onto the row, so the receipt rides here"

    @pytest.mark.asyncio
    async def test_steer_injects_into_the_running_turn(
        self, tmp_path, monkeypatch, _patch_sel, decision
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        decision(_STEER)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "stop, wrong file", "steer": "auto"}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        slot._acp_client.steer.assert_awaited_once_with("stop, wrong file")
        assert _strip_of(slot) == _record(_STEER), "the receipt rides the persisted user row"

    @pytest.mark.asyncio
    async def test_a_refusal_takes_the_steer_path_and_stamps_nothing(
        self, tmp_path, monkeypatch, _patch_sel, decision
    ):
        """The seam off, unsampled, timed out: the composer's own default, silently."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        decision(None)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": "auto"}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        slot._acp_client.steer.assert_awaited_once_with("go left")
        assert _strip_of(slot) is None, "no decision, no receipt"

    @pytest.mark.asyncio
    async def test_a_refused_row_takes_the_path_without_a_receipt(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """``record_outcome`` returning ``None`` is a decision with no durable row."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        async def _steer_or_queue(text, **kwargs):
            return _STEER

        monkeypatch.setattr(ms, "steer_or_queue", _steer_or_queue)
        monkeypatch.setattr(ms, "record_outcome", lambda session_key, decided: None)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": "auto"}
            )
            assert resp.status == 200

        assert (
            _strip_of(slot) is None
        ), "the thumbs POST this turn id, so a receipt needs a row a verdict can join"

    @pytest.mark.asyncio
    async def test_a_steer_answer_the_client_cannot_take_still_carries_its_receipt(
        self, tmp_path, monkeypatch, _patch_sel, decision
    ):
        """Decided steer, steer UNAVAILABLE: the queue path keeps the record.

        The receipt describes the DECISION; how the delivery ended is the row's own
        ``steerState``. Withholding it here would lose the only record that a
        decision was made at all.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = None  # no live client -> cannot steer
        decision(_STEER)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": "auto"}
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        assert slot._queue[-1]["meta"]["decisions_strip"] == _record(_STEER)


class TestTheManualModesAreUnchanged:
    @pytest.mark.asyncio
    async def test_a_manual_steer_is_never_decided(
        self, tmp_path, monkeypatch, _patch_sel, never_asked
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        slot._acp_client.steer.assert_awaited_once_with("go left")
        assert _strip_of(slot) is None, "a manual steer's row keeps its exact prior shape"

    @pytest.mark.asyncio
    async def test_a_manual_queue_is_never_decided(
        self, tmp_path, monkeypatch, _patch_sel, never_asked
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": "test", "message": "later"})
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        slot._acp_client.steer.assert_not_awaited()
        assert "decisions_strip" not in (slot._queue[-1].get("meta") or {})

    def test_only_the_exact_auto_string_asks(self):
        from kiro_crew.dashboard.chat_handlers import steer_is_auto

        assert steer_is_auto("auto")
        assert steer_is_auto(" Auto ")
        assert not steer_is_auto(True), "the boolean every existing client sends is MANUAL"
        assert not steer_is_auto("automatic")
        assert not steer_is_auto("")
        assert not steer_is_auto(None)
        assert not steer_is_auto(1)


class TestTheReceiptIsTheGatewaysOwnClaim:
    @pytest.mark.asyncio
    async def test_a_request_cannot_supply_its_own_decision_receipt(
        self, tmp_path, monkeypatch, _patch_sel, never_asked
    ):
        """`meta` rides onto the persisted row, and the row's receipt carries a
        verdict control whose POST names the turn id in it -- so a caller-supplied
        one would render a decision nobody made and invite feedback about it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")

        forged = {"turn_id": "forged", "point": ms.POINT, "choice": ms.CHOICE_QUEUE, "p": 1.0}
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={
                    "slot": "test",
                    "message": "hello",
                    "meta": {"sendId": "s-1", "decisions_strip": forged},
                },
            )
            assert resp.status == 200

        assert _strip_of(slot) is None, "the receipt is server-minted only"
        row = next(r for r in reversed(slot.messages) if r.get("role") == "user")
        assert (row.get("meta") or {}).get(
            "sendId"
        ) == "s-1", "the rest of the caller's meta is untouched"

    @pytest.mark.asyncio
    async def test_a_queued_send_cannot_supply_one_either(
        self, tmp_path, monkeypatch, _patch_sel, never_asked
    ):
        """The busy branch's queue entry is the second door onto the same row: the
        drain unions entry meta onto what it appends."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "later",
                    "meta": {"decisions_strip": {"turn_id": "forged", "choice": "queue"}},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        assert "decisions_strip" not in (slot._queue[-1].get("meta") or {})

    def test_the_reserved_key_is_named_once(self):
        from kiro_crew.dashboard.chat_handlers import RESERVED_ROW_META_KEYS

        assert "decisions_strip" in RESERVED_ROW_META_KEYS


class TestTheDecisionIsFencedToItsOwnTurn:
    @pytest.mark.asyncio
    async def test_a_turn_that_ends_during_the_decision_drops_the_answer(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """The decision is a provider round-trip, so the turn it is ABOUT can end
        while it is in flight. An answer about a turn that is gone is not an answer
        about this send -- "interrupt what it is doing" names finished work, and a
        successor turn is a different subject -- so the send takes the branch's own
        default and carries no receipt."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)

        async def _steer_or_queue(text, **kwargs):
            # The successor turn, swapped in from inside the await exactly as a
            # teardown plus a new dispatch would.
            successor = MagicMock()
            successor.done.return_value = False
            slot.task = successor
            return _QUEUE

        monkeypatch.setattr(ms, "steer_or_queue", _steer_or_queue)
        monkeypatch.setattr(ms, "record_outcome", lambda session_key, d: _record(d))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": "auto"}
            )
            assert resp.status == 200
            assert (await resp.json()).get(
                "steered"
            ) is True, "the queue answer about the previous turn must not route this send"

        assert _strip_of(slot) is None, "a receipt here would misattribute the decision"

    @pytest.mark.asyncio
    async def test_the_same_turn_throughout_keeps_the_answer(
        self, tmp_path, monkeypatch, _patch_sel, decision
    ):
        """The fence must not cost the ordinary case its decision."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        decision(_QUEUE)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "and bump it", "steer": "auto"}
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        assert slot._queue[-1]["meta"]["decisions_strip"] == _record(_QUEUE)


class TestOnlyTheOwnersOwnSendIsDecided:
    @pytest.mark.asyncio
    async def test_an_app_send_is_never_decided(
        self, tmp_path, monkeypatch, _patch_sel, never_asked
    ):
        """An app has nobody watching the reply, and its text already fails closed
        into the queue -- so there is no question for the oracle to answer."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._app = "app-A"

        @web.middleware
        async def _inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, _inject_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": "auto"}
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        slot._acp_client.steer.assert_not_awaited()
        assert "decisions_strip" not in (slot._queue[-1].get("meta") or {})

    @pytest.mark.asyncio
    async def test_an_idle_slot_is_never_decided(
        self, tmp_path, monkeypatch, _patch_sel, never_asked
    ):
        """No running turn, nothing to steer INTO: the question does not apply."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.get_or_create_slot("test")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"slot": "test", "message": "go left", "steer": "auto"},
            )
            assert resp.status == 200
