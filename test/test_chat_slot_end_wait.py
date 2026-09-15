"""Tests for POST /api/chat/slots/{slot}/end-wait.

The route parks a cooperative request on the slot for the sleeping `wait` tool
to collect on its next keepalive poll. It is deliberately NOT a cancel, and it
is deliberately wait_id-scoped: a slot-scoped flag would have accepted a click
landing after the wait already elapsed, and a click from a stale tab still
showing a previous wait's countdown.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_end_wait
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE BUG (not fixed here — this file is tests only)
#
#   src/kiro_crew/dashboard/chat.py:56
#     53      api_chat_slot_delete,
#     54      api_chat_slot_detail,
#     55      api_chat_slot_followup,
#     56      api_chat_slot_end_wait,   <-- belongs on line 55, before followup
#     57      api_chat_slot_interrupt,
#
#   The re-export was appended after `api_chat_slot_followup`, but `end_wait`
#   sorts before `followup`. CI's BLOCKING import gate
#   (`isort --check-only src/kiro_crew test`, ci.yml:228) therefore fails on
#   this branch. Verified: the same file at HEAD passes
#   (`isort --check-only --settings-path <repo> ` on `git show HEAD:...` -> exit 0),
#   the working tree fails -> exit 1, and the only delta is this one added line.
#   Fix is to move line 56 above line 55. Nothing about the runtime behaviour
#   covered below changes either way.
# ─────────────────────────────────────────────────────────────────────────────


def _make_app(
    state: DashboardState, *, seen_lengths: list[int | None] | None = None
) -> web.Application:
    """Mount the real handler.

    ``seen_lengths`` collects each request's ``Content-Length`` as the server saw
    it. The body-shape tests below need that: the handler skips the parse
    entirely when ``content_length`` is falsy, so a body that never made it onto
    the wire would produce the same 400 as one that parsed to a non-object, and
    the test would pass without exercising ``request.json()`` at all.
    """
    app = web.Application()
    app["state"] = state

    async def _route(request: web.Request) -> web.Response:
        if seen_lengths is not None:
            seen_lengths.append(request.content_length)
        # Inject dashboard-owner claims so deny_non_dashboard_caller passes
        if "app" not in request:
            request["app"] = ""
        if "user" not in request:
            request["user"] = "local-app"
        return await api_chat_slot_end_wait(request)

    app.router.add_post("/api/chat/slots/{slot}/end-wait", _route)
    return app


def _mock_state(slot: _ChatSlot | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.broadcast_ws = MagicMock()
    return state


def _sleeping_slot(wait_id: str = "w1") -> _ChatSlot:
    slot = _ChatSlot("test")
    slot._wait_state = {"wait_id": wait_id, "seconds": 300, "deadline_ts": 9999.0}
    return slot


@pytest.fixture
def _patch_sel():
    """Patch sel() to avoid SecurityEventLog initialization."""
    mock_sel = MagicMock()
    mock_sel.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


class TestChatSlotEndWait:
    @pytest.mark.asyncio
    async def test_unknown_slot_returns_404(self, _patch_sel):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/missing/end-wait", json={"wait_id": "w1"}
            )
            assert resp.status == 404
            assert (await resp.json())["error"] == "not found"
        _patch_sel.log_tool_invocation.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_wait_id_returns_400(self, _patch_sel):
        slot = _sleeping_slot()
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/end-wait", json={})
            assert resp.status == 400
            assert "wait_id required" in (await resp.json())["error"]
        # A request with nothing to match must not park anything.
        assert slot._end_wait_request is None
        assert slot._wait_state is not None
        _patch_sel.log_tool_invocation.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body", [{}, {"wait_id": ""}, {"wait_id": "   "}, {"wait_id": None}]
    )
    async def test_blank_wait_id_variants_return_400(self, _patch_sel, body):
        slot = _sleeping_slot()
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/end-wait", json=body)
            assert resp.status == 400
        assert slot._end_wait_request is None

    @pytest.mark.asyncio
    async def test_empty_body_returns_400_not_500(self, _patch_sel):
        """No body at all: content_length is 0, so the parse is skipped."""
        slot = _sleeping_slot()
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/end-wait")
            assert resp.status == 400
        assert slot._end_wait_request is None

    @pytest.mark.asyncio
    async def test_malformed_body_returns_400_not_500(self, _patch_sel):
        slot = _sleeping_slot()
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/end-wait",
                data=b"{not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        assert slot._end_wait_request is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        [
            b"[]",
            b'["wait-1"]',
            b'"wait-1"',
            b"5",
            b"true",
            b"null",
        ],
        ids=[
            "json-empty-list",
            "json-list",
            "json-string",
            "json-number",
            "json-true",
            "json-null",
        ],
    )
    async def test_well_formed_non_object_body_returns_400_not_500(self, _patch_sel, raw):
        """A body that is valid JSON but not an object.

        ``read_bounded_json`` owns the shape guard: a list/str/int/bool/None
        parses cleanly but is refused as ``body_not_object`` before the handler
        ever calls ``.get`` on it, so no shape can escape as a 500.
        """
        slot = _sleeping_slot()
        state = _mock_state(slot)
        seen: list[int | None] = []
        async with TestClient(TestServer(_make_app(state, seen_lengths=seen))) as client:
            resp = await client.post(
                "/api/chat/slots/test/end-wait",
                data=raw,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status != 500
            assert resp.status == 400
            payload = await resp.json()
            assert payload["code"] == "body_not_object"

        # The body reached the handler with a length, so the parse really ran:
        # this is not the content_length==0 short circuit answering 400 for us.
        assert seen == [len(raw)]
        # Rejected, so nothing may be parked and the sleep stays in flight.
        assert slot._end_wait_request is None
        assert slot._wait_state is not None
        _patch_sel.log_tool_invocation.assert_not_called()

    @pytest.mark.asyncio
    async def test_object_body_on_the_wire_still_parks_the_request(self, _patch_sel):
        """Control for the parametrized set above: the same raw-bytes path, one
        shape different. Normalizing non-objects must not cost the object case
        its parse."""
        slot = _sleeping_slot("wait-1")
        state = _mock_state(slot)
        raw = b'{"wait_id": "wait-1"}'
        seen: list[int | None] = []
        async with TestClient(TestServer(_make_app(state, seen_lengths=seen))) as client:
            resp = await client.post(
                "/api/chat/slots/test/end-wait",
                data=raw,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            assert await resp.json() == {"ok": True}

        assert seen == [len(raw)]
        assert slot._end_wait_request == "wait-1"
        _patch_sel.log_tool_invocation.assert_called_once()

    @pytest.mark.asyncio
    async def test_mismatched_wait_id_returns_409(self, _patch_sel):
        """A stale tab quoting a previous wait's id: the click landed, the wait
        it named is gone."""
        slot = _sleeping_slot("current")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/end-wait", json={"wait_id": "previous"}
            )
            assert resp.status == 409
            assert "no such wait in flight" in (await resp.json())["error"]
        assert slot._end_wait_request is None
        assert slot._wait_state == {
            "wait_id": "current",
            "seconds": 300,
            "deadline_ts": 9999.0,
        }
        _patch_sel.log_tool_invocation.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_wait_in_flight_returns_409(self, _patch_sel):
        """The wait already elapsed before the click arrived."""
        slot = _ChatSlot("test")
        assert slot._wait_state is None
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/end-wait", json={"wait_id": "w1"}
            )
            assert resp.status == 409
        assert slot._end_wait_request is None

    @pytest.mark.asyncio
    async def test_matching_wait_id_parks_the_request(self, _patch_sel):
        slot = _sleeping_slot("w1")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/end-wait", json={"wait_id": "w1"}
            )
            assert resp.status == 200
            assert await resp.json() == {"ok": True}

        assert slot._end_wait_request == "w1"
        # Cooperative, not a cancel: the countdown stays until the tool collects
        # the request (or the turn ends), so the tab keeps rendering it.
        assert slot._wait_state is not None
        state.sessions.stop_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_matching_wait_id_is_audited(self, _patch_sel):
        slot = _sleeping_slot("w1")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/end-wait", json={"wait_id": "w1"}
            )
            assert resp.status == 200

        _patch_sel.log_tool_invocation.assert_called_once()
        kwargs = _patch_sel.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "dashboard_end_wait"
        assert kwargs["source"] == "dashboard"
        assert kwargs["outcome"] == "success"
        assert kwargs["metadata"] == {"slot": "test", "wait_id": "w1"}

    @pytest.mark.asyncio
    async def test_wait_id_is_whitespace_trimmed_before_matching(self, _patch_sel):
        slot = _sleeping_slot("w1")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/end-wait", json={"wait_id": "  w1  "}
            )
            assert resp.status == 200
        assert slot._end_wait_request == "w1"

    @pytest.mark.asyncio
    async def test_repeated_click_is_idempotent(self, _patch_sel):
        """Double-click while the tool has not polled yet: same flag, still 200."""
        slot = _sleeping_slot("w1")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            first = await client.post(
                "/api/chat/slots/test/end-wait", json={"wait_id": "w1"}
            )
            second = await client.post(
                "/api/chat/slots/test/end-wait", json={"wait_id": "w1"}
            )
            assert first.status == 200
            assert second.status == 200
        assert slot._end_wait_request == "w1"
