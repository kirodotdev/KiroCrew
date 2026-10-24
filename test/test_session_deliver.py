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
    @pytest.mark.parametrize("evict", [True, False])
    async def test_a_full_queue_drops_its_oldest_entry_only_when_asked(self, state, evict):
        slot = _running_slot(state)
        await sd.deliver(state, slot, "old report", sd.cron("nightly"))
        with patch("kiro_crew.dashboard.slot_queue_repository.MAX_LIVE_QUEUE_ENTRIES", 1):
            out = await sd.deliver(
                state, slot, "new report", sd.cron("nightly"), evict_oldest=evict
            )
        assert out == sd.DELIVERED_QUEUED
        expected = ["new report"] if evict else ["old report", "new report"]
        assert [q["content"] for q in slot._queue] == expected
        cards = [m["content"] for m in slot.messages if m["role"] == "queued"]
        assert cards == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ("autonudge_stop", "monitor_stop"))
    async def test_a_full_queue_keeps_a_retained_completion_and_releases_it_once(
        self, state, monkeypatch, tmp_path, operation
    ):
        """A full queue never evicts a sub-agent completion for a cron card.

        A sub-agent completion queued with ``on_discarded`` holds one
        manager-ledger count. Dropping it without that callback would leave the
        count held, so every later agent goal stop would be refused as terminal
        delivery. A cron eviction gives way only to another cron notification, so
        the completion stays queued with its count, the cron card is refused, and
        the agent's stop stays refused until the completion is delivered or
        discarded.
        """
        from kiro_crew.dashboard.chat_utils_slots import effective_session_key
        from kiro_crew.dashboard.session_directive_apply import apply_session_directive
        from kiro_crew.subagent import SubagentManager

        monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "subs")
        slot = _running_slot(state)
        parent = effective_session_key(slot)
        manager = SubagentManager(sessions=MagicMock(), ctx_builder=None)
        state.subagents = manager
        manager.retain_completion_delivery(parent, "child-1")
        released: list[str] = []

        def _discarded() -> None:
            released.append("child-1")
            manager.release_completion_delivery(parent, "child-1")

        slot.queue_insert(0, "child-1 result", on_discarded=_discarded)
        assert manager.terminal_delivery_inflight_for(parent) is True

        with patch("kiro_crew.dashboard.slot_queue_repository.MAX_LIVE_QUEUE_ENTRIES", 1):
            out = await sd.deliver(state, slot, CRON_TEXT, sd.cron("nightly"), evict_oldest=True)

        assert out == sd.QUEUE_FULL
        assert [q["content"] for q in slot._queue] == ["child-1 result"]
        assert released == []
        assert manager.terminal_delivery_inflight_for(parent) is True

        # Once the completion is dropped through the repository, its callback
        # releases the count exactly once and the agent's stop is honoured.
        slot.queue_discard_all()
        assert released == ["child-1"]
        assert manager.terminal_delivery_inflight_for(parent) is False
        assert manager._retained_completion_owners == {}

        class _Svc:
            def __init__(self) -> None:
                self.removed: list[str] = []

            def get_by_slot(self, _key):
                return type("_Loop", (), {"id": "loop-1", "monitor": None, "gate": False})()

            def list_all(self):
                return []

            async def remove(self, loop_id, **_kw):
                self.removed.append(loop_id)
                return True

        svc = _Svc()
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: svc)
        result = await apply_session_directive(state, slot, parent, operation, {"reason": "done"})
        assert not result.startswith("Error:"), result
        assert svc.removed == ["loop-1"]
        # A later retirement pass cannot release the count a second time.
        slot.queue_discard_all()
        assert released == ["child-1"]

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


class TestPeer:
    """A peer's text is a ``user`` row on both paths, never the human stamp."""

    @pytest.mark.asyncio
    async def test_an_idle_slot_runs_a_peer_send_as_a_user_row(self, state):
        from kiro_crew.history import HUMAN_TURN_META_KEY

        slot = state.get_or_create_slot("idle")
        ran: list[str] = []

        async def _fake_run_chat(_state, _slot, prompt, **_kw):
            ran.append(prompt)

        with patch("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat):
            out = await sd.deliver(state, slot, "peer text", sd.peer(steer=False))
            await slot.task
        assert out == sd.DELIVERED_TURN
        assert ran == ["peer text"]
        rows = [m for m in slot.messages if m.get("content") == "peer text"]
        assert [r["role"] for r in rows] == ["user"]
        assert HUMAN_TURN_META_KEY not in (rows[0].get("meta") or {})

    @pytest.mark.asyncio
    async def test_a_steer_that_finds_the_turn_ended_delivers_nothing(self, state):
        """The steer arm alone: the caller's fallback is the one delivery."""
        slot = state.get_or_create_slot("idle")
        steer = AsyncMock()
        with patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer):
            out = await sd.deliver(state, slot, "late", sd.peer(steer=True), admission={})
        assert out == sd.NOT_DELIVERED
        steer.assert_not_awaited()
        assert slot.messages == [] and slot._queue == [] and slot.task is None

    @pytest.mark.asyncio
    async def test_a_running_slot_queues_a_peer_send_that_did_not_ask_to_steer(self, state):
        slot = _running_slot(state)
        steer = AsyncMock()
        with patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer):
            out = await sd.deliver(
                state, slot, "later", sd.peer(steer=False), queue_meta={"sendOrigin": "x"}
            )
        assert out == sd.DELIVERED_QUEUED
        steer.assert_not_awaited()
        assert slot._queue[0]["content"] == "later"
        assert slot._queue[0]["meta"]["sendOrigin"] == "x"

    @pytest.mark.asyncio
    async def test_a_running_slot_steers_a_peer_send_as_a_user_row(self, state):
        slot = _running_slot(state)
        client = MagicMock()
        client.supports_steer = True
        client.steer = AsyncMock(return_value=True)
        slot._acp_client = client

        out = await sd.deliver(state, slot, "now", sd.peer(steer=True), admission={})

        assert out == sd.DELIVERED_STEERED
        rows = [m for m in slot.messages if m.get("content") == "now"]
        assert [r["role"] for r in rows] == ["user"]
        assert rows[0]["meta"]["steer"] is True
        assert "nudge" not in rows[0]["meta"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "steer_outcome, expected",
        [("requeued", sd.DELIVERED_REQUEUED), ("unavailable", sd.NOT_DELIVERED)],
    )
    async def test_a_peer_steer_outcome_maps_to_a_delivery(self, state, steer_outcome, expected):
        slot = _running_slot(state)
        steer = AsyncMock(return_value=steer_outcome)
        with patch("kiro_crew.dashboard.chat_delivery.steer_into_running_turn", steer):
            out = await sd.deliver(state, slot, "now", sd.peer(steer=True), admission={"a": 1})
        assert out == expected
        assert steer.await_args.kwargs["user_origin"] is False
        assert steer.await_args.kwargs["admission"] == {"a": 1}
        assert "nudge_meta" not in steer.await_args.kwargs


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
