"""The row a delivery writes follows its sender, on the idle and the running path.

``session_deliver.deliver`` is the session-side entry point for cron, app and
automation-wake text. The sender names its origin; the session picks idle (a row
and a turn) or running (a queued card, or a steer for a wake). These tests pin
the row each origin yields on both paths, and that each caller routes through it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from steer_requeue_helpers import _running_slot

from kiro_crew.dashboard import session_deliver as sd
from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND, MCP_APP_MESSAGE_KIND

CRON_TEXT = '[Cron notification: "nightly"]\nstill red\n[/Cron notification]'
APP_TEXT = '[MCP app message from "srv/tool"]\nhello\n[End of MCP app message]'
WAKE_TEXT = "[work-ledger wake] item=it_1 status=done"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    return st


class TestIdle:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "origin, text, role, injected",
        [
            (
                sd.cron("nightly"),
                CRON_TEXT,
                "inject",
                {"injectKind": "cron", "cronLabel": "nightly"},
            ),
            (sd.app("srv/tool"), APP_TEXT, "inject", {"injectKind": "mcp_app"}),
        ],
    )
    async def test_an_idle_slot_gets_the_origins_row_and_a_turn(
        self, state, origin, text, role, injected
    ):
        slot = state.get_or_create_slot("idle")
        run_chat = MagicMock(return_value=MagicMock())
        turn = MagicMock(name="turn")
        with (
            patch("kiro_crew.dashboard.chat_runner._run_chat", run_chat),
            patch("kiro_crew.dashboard.turn_dispatch.spawn_guarded_turn", return_value=turn),
        ):
            assert await sd.deliver(state, slot, text, origin) == sd.DELIVERED_TURN

        rows = [m for m in slot.messages if m.get("content") == text]
        assert [r["role"] for r in rows] == [role]
        assert injected.items() <= rows[0]["meta"].items()
        assert slot.task is turn
        kwargs = run_chat.call_args.kwargs
        assert kwargs["_turn_actor"] == origin.kind
        assert kwargs["_directive_user_origin"] is False
        assert kwargs["_synthetic_payload"] is (origin.kind == sd.ORIGIN_APP)

    @pytest.mark.asyncio
    async def test_an_idle_wake_is_left_to_the_autonudge_fire(self, state):
        slot = state.get_or_create_slot("idle")
        assert await sd.deliver(state, slot, WAKE_TEXT, sd.automation(3, "l1")) == sd.NOT_DELIVERED
        assert slot.messages == []


class TestRunning:
    @pytest.mark.asyncio
    async def test_a_running_slot_queues_a_cron_notification(self, state):
        slot = _running_slot(state)
        assert await sd.deliver(state, slot, CRON_TEXT, sd.cron("nightly")) == sd.DELIVERED_QUEUED
        assert [q["kind"] for q in slot._queue] == [CRON_NOTIFICATION_KIND]
        assert [m["role"] for m in slot.messages] == ["queued"]

    @pytest.mark.asyncio
    async def test_a_running_slot_queues_an_app_message_with_its_admission(self, state):
        slot = _running_slot(state)
        admission = {"queued_containment": {"x": 1}}
        out = await sd.deliver(state, slot, APP_TEXT, sd.app("srv/tool"), admission=admission)
        assert out == sd.DELIVERED_QUEUED
        entry = slot._queue[0]
        assert entry["kind"] == MCP_APP_MESSAGE_KIND
        assert entry["meta"] == {"appLabel": "srv/tool", **admission}
        assert slot.messages[0]["meta"]["kind"] == MCP_APP_MESSAGE_KIND

    @pytest.mark.asyncio
    async def test_a_full_queue_writes_nothing(self, state):
        slot = _running_slot(state)
        with patch("kiro_crew.dashboard.slot_queue_repository.MAX_LIVE_QUEUE_ENTRIES", 0):
            assert await sd.deliver(state, slot, APP_TEXT, sd.app("srv/tool")) == sd.QUEUE_FULL
        assert slot._queue == [] and slot.messages == []

    @pytest.mark.asyncio
    async def test_a_running_slot_steers_a_wake_as_a_nudge_row(self, state):
        slot = _running_slot(state)
        client = MagicMock()
        client.supports_steer = True
        client.steer = AsyncMock(return_value=True)
        slot._acp_client = client

        out = await sd.deliver(state, slot, WAKE_TEXT, sd.automation(3, "l1"), admission={})

        assert out == sd.DELIVERED_STEERED
        rows = [m for m in slot.messages if m.get("content") == WAKE_TEXT]
        assert [r["role"] for r in rows] == ["nudge"]
        assert rows[0]["meta"]["nudge"] == {"cycle": 3, "loop_id": "l1"}
        assert rows[0]["meta"]["steer"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "steer_outcome, expected",
        [("requeued", sd.DELIVERED_REQUEUED), ("unavailable", sd.NOT_DELIVERED)],
    )
    async def test_a_wake_steer_outcome_maps_to_a_delivery(self, state, steer_outcome, expected):
        slot = _running_slot(state)
        steer = AsyncMock(return_value=steer_outcome)
        with patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer):
            out = await sd.deliver(state, slot, WAKE_TEXT, sd.automation(3, "l1"))
        assert out == expected
        assert steer.await_args.kwargs["nudge_meta"] == {"cycle": 3, "loop_id": "l1"}
        assert steer.await_args.kwargs["user_origin"] is False


def test_origin_row_is_the_one_mapping():
    assert sd.origin_row(sd.automation(2, "l"))[:2] == ("nudge", "msg msg-nudge")
    assert sd.origin_row(sd.cron("c"))[0] == "inject"
    assert sd.origin_row(sd.app("a"))[0] == "inject"
    with pytest.raises(ValueError):
        sd.origin_row(sd.Origin("person"))


class TestCallersRouteThroughDeliver:
    """Each sender names its origin and leaves idle-vs-running to the session."""

    @pytest.mark.asyncio
    async def test_a_script_cron_result_is_delivered_as_cron(self):
        from test_slack_gateway_cron_exec_coverage import (
            _cron_cb,
            _job,
            _make_orchestrator,
            _mock_dashboard_state,
            _mock_slot,
        )

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        slot = _mock_slot(running=False)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2")
        deliver = AsyncMock(return_value=sd.DELIVERED_TURN)
        with patch.object(sd, "deliver", deliver):
            async with _cron_cb(orch, script_result={"status": "report", "message": "ok"}) as cb:
                await cb(job)
        origin = deliver.await_args.args[3]
        assert origin.kind == sd.ORIGIN_CRON
        assert deliver.await_args.args[1] is slot

    @pytest.mark.asyncio
    async def test_a_work_ledger_wake_is_delivered_as_automation(self):
        from test_work_ledger_wake_delivery import _ledger_loop, _orchestrator, _slot

        orch = _orchestrator()
        loop = _ledger_loop()
        slot = _slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        deliver = AsyncMock(return_value=sd.DELIVERED_STEERED)
        with (
            patch.object(sd, "deliver", deliver),
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value=WAKE_TEXT)),
        ):
            assert await orch._fire_dashboard_nudge(loop) is True
        origin = deliver.await_args.args[3]
        assert origin.kind == sd.ORIGIN_AUTOMATION
        assert origin.nudge == {"cycle": loop.cycle_count + 1, "loop_id": loop.id}
