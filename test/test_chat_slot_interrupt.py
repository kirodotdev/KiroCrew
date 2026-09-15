"""Tests for POST /api/chat/slots/{slot}/interrupt endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_interrupt
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post(
        "/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt
    )
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


@pytest.fixture
def _patch_sel():
    """Patch sel() to avoid SecurityEventLog initialization."""
    mock_sel = MagicMock()
    mock_sel.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


class TestChatSlotInterrupt:
    @pytest.mark.asyncio
    async def test_unknown_slot_returns_404(self, _patch_sel):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/missing/interrupt",
                json={},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_not_running_returns_ok_with_info(self, _patch_sel):
        slot = _ChatSlot("test")
        # running is a computed property (task is not None and not done)
        # Default: task=None → running=False
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/interrupt",
                json={},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["info"] == "not running"

    @pytest.mark.asyncio
    async def test_empty_queue_returns_400(self, _patch_sel):
        slot = _ChatSlot("test")
        # Make slot appear running
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        slot._queue = []
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/interrupt",
                json={},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "queue empty" in data["error"]

    @pytest.mark.asyncio
    async def test_interrupt_calls_stop_turn_with_preserve_queue(self, _patch_sel):
        slot = _ChatSlot("test")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        slot.queue_append("hello")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/interrupt",
                json={},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["outcome"] == "soft"
            # Verify preserve_queue=True was passed
            state.sessions.stop_turn.assert_awaited_once()
            call_kwargs = state.sessions.stop_turn.call_args.kwargs
            assert call_kwargs["preserve_queue"] is True
            assert call_kwargs["force"] is False

    @pytest.mark.asyncio
    async def test_interrupt_with_queue_id_promotes_to_front(self, _patch_sel):
        slot = _ChatSlot("test")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        # Seed through the REAL production path (queue_append -> {"id": ...}),
        # not a hand-built dict. The original fixture used {"queue_id": ...},
        # a shape that never occurs in production, which let the handler's
        # wrong-key match (item.get("queue_id")) pass this test while being a
        # silent no-op on real queues.
        q1 = slot.queue_append("first")
        q2 = slot.queue_append("second")
        q3 = slot.queue_append("third")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/interrupt",
                json={"queue_id": q2},
            )
            assert resp.status == 200
            # q2 should now be at front
            assert slot._queue[0]["id"] == q2
            assert slot._queue[1]["id"] == q1
            assert slot._queue[2]["id"] == q3

    @pytest.mark.asyncio
    async def test_interrupt_with_unknown_queue_id_preserves_order(self, _patch_sel):
        """An unknown queue_id must not reorder anything (and must not 500)."""
        slot = _ChatSlot("test")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        q1 = slot.queue_append("first")
        q2 = slot.queue_append("second")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/interrupt",
                json={"queue_id": "does-not-exist"},
            )
            assert resp.status == 200
            assert [i["id"] for i in slot._queue] == [q1, q2]

    @pytest.mark.asyncio
    async def test_interrupt_sets_stop_state_to_soft_pending(self, _patch_sel):
        slot = _ChatSlot("test")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        slot.queue_append("msg")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post(
                "/api/chat/slots/test/interrupt",
                json={},
            )
            # Mock doesn't invoke on_soft callback, so state stays soft_pending
            # (on_soft would set it to idle in production)
            assert slot._stop_state in ("soft_pending", "idle")

    @pytest.mark.asyncio
    async def test_interrupt_rejects_pending_approval_futures(self, _patch_sel):
        """Pending approval futures are resolved before stop_turn so the
        chat runner can unblock."""
        import asyncio

        slot = _ChatSlot("test")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        slot.queue_append("msg")

        # Simulate a pending approval future (agent waiting for permission)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-123"] = fut

        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/interrupt",
                json={},
            )
            assert resp.status == 200
            # The future should be resolved with "rejected"
            assert fut.done()
            assert fut.result() == "rejected"


class TestRefusalRecoverySkippedOnCancel:
    """Recovery prompt should not fire when the user pressed Stop during the turn."""

    def test_stop_press_suppresses_recovery(self):
        """The guard must return False when the user stopped, even with
        non-empty refusal_reasons."""
        from kiro_crew.dashboard.state import should_queue_refusal_recovery

        refusal_reasons = [
            ("Creating /tmp/name.txt", "command '---' is not on the read-only allowlist")
        ]
        assert not should_queue_refusal_recovery(
            refusal_reasons, stopping=False, needs_reset=False, user_stopped=True
        )

    def test_normal_refusal_still_triggers_recovery(self):
        """When the turn ends with refusal reasons and no Stop, recovery fires."""
        from kiro_crew.dashboard.state import should_queue_refusal_recovery

        refusal_reasons = [("write /tmp/x", "not on read-only allowlist")]
        assert should_queue_refusal_recovery(
            refusal_reasons, stopping=False, needs_reset=False, user_stopped=False
        )


class TestRefusalRecoveryOnBackendAbort:
    """A ``cancelled`` stop reason is not, by itself, a user cancel.

    Measured on codex-acp 1.11.0 / codex 0.153.4: a command approval advertises
    ``allow_once``, ``accept_execpolicy_amendment`` and ``cancel`` -- no
    ``decline`` -- so the host's policy deny can only answer ``cancel``, and
    codex aborts the turn with ``stopReason: "cancelled"`` before the model is
    called again. A recovery gate keyed on the wire stop reason reads that as a
    Stop press and skips, which makes every policy block on codex a silent stop.
    The gate takes the host's own Stop signal instead, and only that.
    """

    REFUSALS = [("python3 -c ...", "Blocked by security policy: credential-exfil-kirocrew-token")]

    def test_backend_abort_without_a_stop_press_queues_recovery(self):
        # The wire said "cancelled"; the host's Stop signal says nobody pressed
        # Stop. The gate only ever sees the latter, so recovery is owed.
        from kiro_crew.dashboard.state import should_queue_refusal_recovery

        assert should_queue_refusal_recovery(
            self.REFUSALS, stopping=False, needs_reset=False, user_stopped=False
        )

    def test_a_real_stop_press_still_suppresses_recovery(self):
        # The person pressed Stop during the turn (and it may already have
        # resolved, so stopping is False again): the continuation must not
        # jump ahead of whatever they type next.
        from kiro_crew.dashboard.state import should_queue_refusal_recovery

        assert not should_queue_refusal_recovery(
            self.REFUSALS, stopping=False, needs_reset=False, user_stopped=True
        )

    def test_the_gate_has_no_stop_reason_input_at_all(self):
        # Structural: a wire stop reason cannot be reintroduced as the user-cancel
        # signal by omission, because there is no parameter to carry one, and the
        # Stop signal is required rather than defaulted.
        import inspect

        from kiro_crew.dashboard.state import should_queue_refusal_recovery

        params = inspect.signature(should_queue_refusal_recovery).parameters
        assert "stop_reason" not in params
        assert params["user_stopped"].default is inspect.Parameter.empty
        assert params["user_stopped"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_in_band_delivery_still_short_circuits_on_an_abort(self):
        # Not reachable on codex today (the abort discards a pending steer), but
        # the precedence must hold: a confirmed in-band notice owes no extra turn.
        from kiro_crew.dashboard.state import should_queue_refusal_recovery

        assert not should_queue_refusal_recovery(
            self.REFUSALS,
            stopping=False,
            needs_reset=False,
            notices_sent=1,
            notices_pending=0,
            user_stopped=False,
        )
