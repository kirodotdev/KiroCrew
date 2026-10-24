"""Coverage for ``kiro_crew.slack.gateway``: where an auto-nudge goes, and how it fires.

The ``_fire`` router and the ``_observer`` closure ``_init_autonudge`` builds, the
dashboard nudge's dispatch half, the Webex nudge adapter, and the shared
``_fire_dm_nudge`` ladder a dispatcher-routed channel joins by supplying an
adapter. Everything runs against mocked collaborators.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_coverage_helpers import (
    _DKEY,
    _loop,
    _make_orchestrator,
    _mock_dashboard_state,
    _within_lost_run,
)

from kiro_crew.autonudge import (
    AGENT_STOP_REASON,
    MANUAL_STOP_REASON,
    MONITOR_TERMINAL_REASON,
    NudgeLoop,
)
from kiro_crew.monitoring import models as monitor_models
from kiro_crew.monitoring.models import MonitorOutcome, MonitorState
from kiro_crew.slack import gateway as gw


def _closer_through(emit_ref, wake_ref):
    """Stand in for ``append_closer_if_still_applies`` on a mocked event-log service.

    The real predicate is asked against the wake view the test holds NOW, so a test
    that moves the wake between the snapshot and the write sees the closer declined,
    and the append itself goes through the test's own ``emit`` stub. A failed append
    raises, as the real service does.
    """
    from kiro_crew.eventlog.types import PROJ_WAKE

    def _append(slug, etype, data, *, still_applies, observed):
        if not still_applies({PROJ_WAKE: dict(wake_ref())}, observed):
            return None
        if not emit_ref()(slug, None, etype, data):
            raise RuntimeError("append failed")
        return object()

    return _append


# ═════════════════════════════════════════════════════════════════════════
# _init_autonudge closures: the _fire router and the _observer
# ═════════════════════════════════════════════════════════════════════════


class TestAutonudgeRouterAndObserver:
    """``_init_autonudge`` builds the key-namespace router and the WS observer."""

    async def _wire(
        self,
        orch: Any,
        *,
        existing_loops: list[NudgeLoop] | None = None,
        emit_during_start: NudgeLoop | None = None,
    ):
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with (
                patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_svc,
                patch("kiro_crew.monitoring.controller.MonitorController") as mock_controller,
            ):
                inst = MagicMock()
                inst.subscribe = MagicMock()

                async def _start():
                    if emit_during_start is not None and inst.subscribe.call_args is not None:
                        observer = inst.subscribe.call_args.args[0]
                        observer("updated", emit_during_start)

                inst.start = AsyncMock(side_effect=_start)
                inst.remove = AsyncMock()
                inst.mark_terminal_notification_delivered = AsyncMock()
                inst.list_all.return_value = existing_loops or []
                mock_svc.return_value = inst
                await orch._init_autonudge()
                inst.monitor_dispatch = mock_controller.call_args.args[1]
                inst.owner_session_id = mock_controller.call_args.kwargs["owner_session_id"]
        on_fire = mock_svc.call_args.kwargs["on_fire"]
        observer = inst.subscribe.call_args.args[0]
        return on_fire, observer, inst

    @pytest.mark.asyncio
    async def test_the_controller_is_handed_a_resolver_for_the_owner_session(self):
        """The observation recorder names the owner's crew log unit through the registry.

        No disk and no session opened: an exact registry read answers the unit the
        slot is serving on. A slot with no live session, and a gateway with no
        dashboard, answer the empty string the controller treats as a no-op.
        """
        orch = _make_orchestrator()
        orch.dashboard_state = None
        _on_fire, _observer, inst = await self._wire(orch)
        assert inst.owner_session_id(_loop("chat-1")) == ""

        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        live = SimpleNamespace(session_id="acp-77")
        ds.sessions = MagicMock()
        ds.sessions.get_provider = MagicMock(
            side_effect=lambda key: live if key in {"chat-1", "dashboard:chat-1"} else None
        )
        orch.dashboard_state = ds
        _on_fire, _observer, inst = await self._wire(orch)
        assert inst.owner_session_id(_loop("chat-1")) == "acp-77"
        assert inst.owner_session_id(_loop("chat-9")) == ""

    @pytest.mark.asyncio
    async def test_slack_key_routes_to_slack_fire(self):
        orch = _make_orchestrator()
        orch._fire_slack_nudge = AsyncMock(return_value=True)
        orch._fire_discord_nudge = AsyncMock(return_value=True)
        on_fire, _observer, _inst = await self._wire(orch)

        loop = _loop("slack:111.222")
        assert await on_fire(loop) is True
        orch._fire_slack_nudge.assert_awaited_once_with(loop)
        orch._fire_discord_nudge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discord_key_routes_to_discord_fire(self):
        orch = _make_orchestrator()
        orch._fire_slack_nudge = AsyncMock(return_value=True)
        orch._fire_discord_nudge = AsyncMock(return_value=True)
        on_fire, _observer, _inst = await self._wire(orch)

        loop = _loop(_DKEY)
        assert await on_fire(loop) is True
        orch._fire_discord_nudge.assert_awaited_once_with(loop)
        orch._fire_slack_nudge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bare_key_routes_to_dashboard_fire(self):
        orch = _make_orchestrator()
        orch._fire_dashboard_nudge = AsyncMock(return_value=True)
        on_fire, _observer, _inst = await self._wire(orch)

        loop = _loop("chat-1-1721")
        assert await on_fire(loop) is True
        orch._fire_dashboard_nudge.assert_awaited_once_with(loop)

    @pytest.mark.asyncio
    async def test_structured_envelope_is_identical_across_delivery_surfaces(self):
        orch = _make_orchestrator()
        orch._fire_slack_nudge = AsyncMock(
            return_value=monitor_models.MonitorDispatchResult.DISPATCHED
        )
        orch._fire_discord_nudge = AsyncMock(
            return_value=monitor_models.MonitorDispatchResult.DISPATCHED
        )
        orch._fire_dashboard_nudge = AsyncMock(
            return_value=monitor_models.MonitorDispatchResult.DISPATCHED
        )
        _on_fire, _observer, inst = await self._wire(orch)
        envelope = "[Monitor wake]\ncanonical facts"

        slack_loop = _loop("slack:111.222")
        discord_loop = _loop(_DKEY)
        dashboard_loop = _loop("chat-1-1721")
        assert (
            await inst.monitor_dispatch(slack_loop, envelope)
            is monitor_models.MonitorDispatchResult.DISPATCHED
        )
        assert (
            await inst.monitor_dispatch(discord_loop, envelope)
            is monitor_models.MonitorDispatchResult.DISPATCHED
        )
        assert (
            await inst.monitor_dispatch(dashboard_loop, envelope)
            is monitor_models.MonitorDispatchResult.DISPATCHED
        )

        orch._fire_slack_nudge.assert_awaited_once_with(slack_loop, envelope)
        orch._fire_discord_nudge.assert_awaited_once_with(discord_loop, envelope)
        orch._fire_dashboard_nudge.assert_awaited_once_with(dashboard_loop, envelope)

    @pytest.mark.asyncio
    async def test_unsupported_channel_namespace_retires_loop(self, monkeypatch):
        """A channel key with no fire implementation can never succeed."""
        orch = _make_orchestrator()
        monkeypatch.setattr(gw, "is_channel_key", lambda key: key.startswith("telegram:"))
        on_fire, _observer, inst = await self._wire(orch)

        assert await on_fire(_loop("telegram:42")) is False
        inst.remove.assert_awaited_once_with("loop-1", stop_reason="unsupported_channel")

    @pytest.mark.asyncio
    async def test_observer_broadcasts_loop_state(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)

        loop = _loop("chat-1-1721", cycle_count=2, created_ts=1_800_000_000.0)
        observer("armed", loop)

        topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert topic == "autonudge_state"
        assert payload["event"] == "armed"
        assert payload["slot"] == "chat-1-1721"
        assert payload["loop"]["id"] == "loop-1"
        assert payload["loop"]["cycle_count"] == 2
        assert payload["loop"]["created_ts"] == 1_800_000_000.0

    @pytest.mark.asyncio
    async def test_observer_frame_carries_the_declared_wait_s_expiry_and_release(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)

        held = _loop(
            "chat-1-1721",
            waiting_on_person=True,
            person_wait_declared=True,
            waiting_on_person_at=1_800_000_000.0,
        )
        observer("updated", held)
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["person_wait_release"] == ""
        assert payload["loop"]["person_wait_expires_at"] == 1_800_086_400.0
        assert payload["loop"]["person_wait_ends_on_budget"] is False

        released = _loop("chat-1-1721", person_wait_release="expired")
        observer("updated", released)
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["person_wait_release"] == "expired"
        assert "person_wait_expires_at" not in payload["loop"]

    @pytest.mark.asyncio
    async def test_observer_frame_carries_stopped_reason_and_deadline_for_a_plain_loop(self):
        """The dashboard caches the frame over the REST read, so a frame that
        carried these two fields only for a structured monitor blanked a plain
        loop's paused reason and countdown the moment it landed."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)

        running = _loop("chat-1-1721", next_due_ts=1_800_000_300.0)
        observer("armed", running)
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["stopped_reason"] == ""
        assert payload["loop"]["next_due_ts"] == 1_800_000_300.0
        assert payload["loop"]["monitor_outcome"] == ""
        assert payload["loop"]["monitor_kind"] == ""

        paused = _loop("chat-1-1721", active=False, stopped_reason=MANUAL_STOP_REASON)
        observer("updated", paused)
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["active"] is False
        assert payload["loop"]["stopped_reason"] == MANUAL_STOP_REASON
        assert payload["loop"]["next_due_ts"] == 0.0
        assert "monitor" not in payload["loop"]

        # A GATED prompt loop whose watch finished: the popover words Done by the
        # settled outcome, so that one scalar rides the frame while the monitor
        # record -- which names the subject -- stays withheld from this ungated
        # broadcast.
        finished = _loop(
            "chat-1-1721", active=False, stopped_reason=MONITOR_TERMINAL_REASON, gate=True
        )
        finished.monitor = MonitorState(
            kind="gh-pr",
            target="acme/widgets#7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.BLOCKED,
        )
        observer("expired", finished)
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["stopped_reason"] == MONITOR_TERMINAL_REASON
        assert payload["loop"]["monitor_outcome"] == "blocked"
        assert payload["loop"]["monitor_kind"] == "gh-pr"
        assert "monitor" not in payload["loop"]

    @pytest.mark.asyncio
    async def test_observer_frame_carries_the_agents_stop_words(self):
        """The popover shows why the agent stopped a loop, and it renders from the
        frame, so the words ride it beside the reason they explain."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)

        observer("armed", _loop("chat-1-1721"))
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["stopped_detail"] == ""

        stopped = _loop(
            "chat-1-1721",
            active=False,
            stopped_reason=AGENT_STOP_REASON,
            stopped_detail="PR merged, nothing left to do",
        )
        observer("updated", stopped)
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["stopped_reason"] == AGENT_STOP_REASON
        assert payload["loop"]["stopped_detail"] == "PR merged, nothing left to do"

    @pytest.mark.asyncio
    async def test_observer_frame_redacts_the_stop_words_it_broadcasts(self, monkeypatch):
        """The frame reaches every dashboard socket, so the agent's words go through
        the frame's redaction like every other stored string it carries."""
        from kiro_crew.slack import gateway as gateway_module

        def _redactor(value):
            return value.replace("ghp_stopword", "[redacted]") if isinstance(value, str) else value

        monkeypatch.setattr(gateway_module, "_redact_monitor_value", _redactor)
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        stopped = _loop(
            "chat-1-1721",
            active=False,
            stopped_reason=AGENT_STOP_REASON,
            stopped_detail="blocked on ghp_stopword",
        )
        observer("updated", stopped)
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["stopped_detail"] == "blocked on [redacted]"

    @pytest.mark.asyncio
    async def test_observer_frame_redacts_the_watch_kind_it_broadcasts(self, monkeypatch):
        """The kind is a stored string a hand-edited state file reads back as
        written, and this frame reaches every dashboard socket, so the scalar
        goes through the redaction the structured record goes through."""
        from kiro_crew.slack import gateway as gateway_module

        seen: list[object] = []

        def _redactor(value):
            seen.append(value)
            return "[redacted]" if value == "ghp_not-a-registry-kind" else value

        monkeypatch.setattr(gateway_module, "_redact_monitor_value", _redactor)
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        finished = _loop(
            "chat-1-1721", active=False, stopped_reason=MONITOR_TERMINAL_REASON, gate=True
        )
        finished.monitor = MonitorState(
            kind="ghp_not-a-registry-kind",
            target="acme/widgets#7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
        )
        observer("expired", finished)
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert "ghp_not-a-registry-kind" in seen
        assert payload["loop"]["monitor_kind"] == "[redacted]"
        assert payload["loop"]["monitor_outcome"] == "success"

    @pytest.mark.asyncio
    async def test_observer_logs_a_member_patrol_finished_by_its_stop_file_as_stopped(self):
        """A finish by the stop file arrives as an ``updated`` frame on a kept,
        inactive row, and the member event log has to record it as a patrol stop
        -- a plain pause is not one -- or the drawer reads ``armed`` for good,
        since the boot closer closes only a log whose row is gone."""
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PATROL_STOPPED

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        appended: list[tuple[str, str, dict]] = []
        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (fn(), True)[1]),
            patch.object(
                eventlog_hooks,
                "emit",
                lambda slug, _actor, etype, data: (appended.append((slug, etype, data)), True)[1],
            ),
        ):
            paused = _loop("member-crew-x", active=False, stopped_reason=MANUAL_STOP_REASON)
            observer("updated", paused)
            assert appended == [], "a pause is not a patrol stop"
            finished = _loop("member-crew-x", active=False, stopped_reason="stop_sentinel")
            observer("updated", finished)
        assert appended == [
            ("crew-x", PATROL_STOPPED, {"slot_key": "member-crew-x", "reason": "stop_sentinel"})
        ]

    @pytest.mark.asyncio
    async def test_observer_logs_a_member_patrol_its_agent_stopped_as_stopped(self):
        """An agent stop keeps the row deactivated and arrives as ``updated``; the
        member event log must close the patrol before the frame is broadcast, or
        the member projection keeps reading ``armed``."""
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PATROL_STOPPED

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, inst = await self._wire(orch)
        order: list[str] = []
        appended: list[tuple[str, str, dict]] = []

        def _emit(slug, _actor, etype, data):
            order.append("append")
            appended.append((slug, etype, data))
            return True

        orch.dashboard_state.broadcast_ws.side_effect = lambda *_a, **_k: order.append("publish")
        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (fn(), True)[1]),
            patch.object(eventlog_hooks, "emit", _emit),
        ):
            stopped = _loop(
                "member-crew-x",
                active=False,
                stopped_reason=AGENT_STOP_REASON,
                stopped_detail="done",
            )
            inst._activity_transition_for_observer.side_effect = [
                (True, False, ""),
                (False, False, AGENT_STOP_REASON),
            ]
            observer("updated", stopped)
            observer("updated", stopped)
        assert appended == [
            ("crew-x", PATROL_STOPPED, {"slot_key": "member-crew-x", "reason": AGENT_STOP_REASON})
        ]
        # The publish is scheduled back onto the loop after the append lands, so
        # nothing may have been broadcast ahead of it.
        assert order[0] == "append"

    @pytest.mark.asyncio
    async def test_stopped_row_edit_retries_failed_agent_stop_before_publishing(self):
        """A later edit retries a failed edge and publishes only after it lands."""
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PATROL_STOPPED, PROJ_WAKE

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, inst = await self._wire(orch)
        wake = {"patrol": "armed", "slot_key": "member-crew-x"}
        projection = MagicMock()
        projection.snapshot.side_effect = lambda _slug: {"values": {PROJ_WAKE: dict(wake)}}
        projection.append_closer_if_still_applies.side_effect = _closer_through(
            lambda: _emit, lambda: wake
        )
        outcomes = iter((False, True))
        attempts: list[str] = []
        landed: list[str] = []

        def _emit(_slug, _actor, etype, _data):
            attempts.append(etype)
            if not next(outcomes):
                return False
            landed.append(etype)
            wake.update({"patrol": "stopped", "stopped_reason": AGENT_STOP_REASON})
            return True

        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda _slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (fn(), True)[1]),
            patch.object(eventlog_hooks, "emit", _emit),
            patch("kiro_crew.eventlog.service.get_service", return_value=projection),
        ):
            stopped = _loop(
                "member-crew-x",
                active=False,
                stopped_reason=AGENT_STOP_REASON,
                stopped_detail="done",
            )
            inst._activity_transition_for_observer.side_effect = [
                (True, False, ""),
                (False, False, AGENT_STOP_REASON),
            ]
            observer("updated", stopped)
            await asyncio.sleep(0)
            orch.dashboard_state.broadcast_ws.assert_not_called()

            stopped.stopped_detail = "edited after failed append"
            observer("updated", stopped)
            await asyncio.sleep(0)

        assert attempts == [PATROL_STOPPED, PATROL_STOPPED]
        assert landed == [PATROL_STOPPED]
        orch.dashboard_state.broadcast_ws.assert_called_once()
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["stopped_detail"] == "edited after failed append"

    @pytest.mark.asyncio
    async def test_stopped_row_edit_deduplicates_landed_agent_stop(self):
        """A later edit publishes without repeating an already durable stop."""
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PATROL_STOPPED, PROJ_WAKE

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, inst = await self._wire(orch)
        wake = {"patrol": "armed", "slot_key": "member-crew-x"}
        projection = MagicMock()
        projection.snapshot.side_effect = lambda _slug: {"values": {PROJ_WAKE: dict(wake)}}
        projection.append_closer_if_still_applies.side_effect = _closer_through(
            lambda: _emit, lambda: wake
        )
        appended: list[str] = []

        def _emit(_slug, _actor, etype, _data):
            appended.append(etype)
            wake.update({"patrol": "stopped", "stopped_reason": AGENT_STOP_REASON})
            return True

        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda _slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (fn(), True)[1]),
            patch.object(eventlog_hooks, "emit", _emit),
            patch("kiro_crew.eventlog.service.get_service", return_value=projection),
        ):
            stopped = _loop(
                "member-crew-x",
                active=False,
                stopped_reason=AGENT_STOP_REASON,
                stopped_detail="done",
            )
            inst._activity_transition_for_observer.side_effect = [
                (True, False, ""),
                (False, False, AGENT_STOP_REASON),
            ]
            observer("updated", stopped)
            await asyncio.sleep(0)
            stopped.stopped_detail = "edited after landed append"
            observer("updated", stopped)
            await asyncio.sleep(0)

        assert appended == [PATROL_STOPPED]
        assert orch.dashboard_state.broadcast_ws.call_count == 2
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["stopped_detail"] == "edited after landed append"

    @pytest.mark.asyncio
    async def test_queued_stopped_row_edit_retries_after_edge_failure(self):
        """Ordered workers let a queued edit observe and repair an edge failure."""
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PATROL_STOPPED, PROJ_WAKE

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, inst = await self._wire(orch)
        queued: list[Any] = []
        wake = {"patrol": "armed", "slot_key": "member-crew-x"}
        projection = MagicMock()
        projection.snapshot.side_effect = lambda _slug: {"values": {PROJ_WAKE: dict(wake)}}
        projection.append_closer_if_still_applies.side_effect = _closer_through(
            lambda: _emit, lambda: wake
        )
        outcomes = iter((False, True))
        landed: list[str] = []

        def _emit(_slug, _actor, etype, _data):
            if not next(outcomes):
                return False
            landed.append(etype)
            wake.update({"patrol": "stopped", "stopped_reason": AGENT_STOP_REASON})
            return True

        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda _slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (queued.append(fn), True)[1]),
            patch.object(eventlog_hooks, "emit", _emit),
            patch("kiro_crew.eventlog.service.get_service", return_value=projection),
        ):
            stopped = _loop(
                "member-crew-x",
                active=False,
                stopped_reason=AGENT_STOP_REASON,
                stopped_detail="edge",
            )
            inst._activity_transition_for_observer.side_effect = [
                (True, False, ""),
                (False, False, AGENT_STOP_REASON),
            ]
            observer("updated", stopped)
            stopped.stopped_detail = "queued edit"
            observer("updated", stopped)
            assert len(queued) == 2
            orch.dashboard_state.broadcast_ws.assert_not_called()

            queued.pop(0)()
            orch.dashboard_state.broadcast_ws.assert_not_called()
            queued.pop(0)()
            await asyncio.sleep(0)

        assert landed == [PATROL_STOPPED]
        orch.dashboard_state.broadcast_ws.assert_called_once()
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["stopped_detail"] == "queued edit"

    @pytest.mark.asyncio
    async def test_failed_agent_stop_followed_by_play_leaves_projection_armed(self):
        """Play queues a start after the failed stop and publishes the active frame."""
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PATROL_STARTED, PATROL_STOPPED, PROJ_WAKE

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, inst = await self._wire(orch)
        queued: list[Any] = []
        wake = {"patrol": "armed", "slot_key": "member-crew-x"}
        projection = MagicMock()
        projection.snapshot.side_effect = lambda _slug: {"values": {PROJ_WAKE: dict(wake)}}
        projection.append_closer_if_still_applies.side_effect = _closer_through(
            lambda: _emit, lambda: wake
        )
        attempts: list[str] = []

        def _emit(_slug, _actor, etype, _data):
            attempts.append(etype)
            if etype == PATROL_STOPPED:
                return False
            wake.clear()
            wake.update({"patrol": "armed", "slot_key": "member-crew-x"})
            return True

        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda _slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (queued.append(fn), True)[1]),
            patch.object(eventlog_hooks, "emit", _emit),
            patch("kiro_crew.eventlog.service.get_service", return_value=projection),
        ):
            stopped = _loop(
                "member-crew-x",
                active=False,
                stopped_reason=AGENT_STOP_REASON,
                stopped_detail="done",
            )
            active = _loop("member-crew-x", active=True)
            inst._activity_transition_for_observer.side_effect = [
                (True, False, ""),
                (False, True, AGENT_STOP_REASON),
            ]
            observer("updated", stopped)
            observer("updated", active)
            assert len(queued) == 2

            queued.pop(0)()
            queued.pop(0)()
            await asyncio.sleep(0)

        assert attempts == [PATROL_STOPPED, PATROL_STARTED]
        assert wake == {"patrol": "armed", "slot_key": "member-crew-x"}
        orch.dashboard_state.broadcast_ws.assert_called_once()
        _topic, payload = orch.dashboard_state.broadcast_ws.call_args.args
        assert payload["loop"]["active"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("wake", "expected_appends"),
        [
            ({"patrol": "armed", "slot_key": "member-crew-x"}, 1),
            (
                {
                    "patrol": "stopped",
                    "slot_key": "member-crew-x",
                    "stopped_reason": AGENT_STOP_REASON,
                },
                0,
            ),
            ({"patrol": "armed", "slot_key": "member-crew-y"}, 0),
        ],
        ids=["same-slot-armed", "same-slot-stopped", "different-slot-armed"],
    )
    async def test_startup_reconciles_only_the_matching_armed_projection(
        self, wake, expected_appends
    ):
        """Startup closes only an armed projection owned by the retained row."""
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PATROL_STOPPED, PROJ_WAKE

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        stopped = _loop(
            "member-crew-x",
            active=False,
            stopped_reason=AGENT_STOP_REASON,
            stopped_detail="done",
        )
        projection = MagicMock()
        projection.snapshot.return_value = {"values": {PROJ_WAKE: wake}}
        projection.append_closer_if_still_applies.side_effect = _closer_through(
            lambda: _emit, lambda: wake
        )
        appended: list[tuple[str, str, dict]] = []

        def _emit(slug, _actor, etype, data):
            appended.append((slug, etype, data))
            return True

        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda _slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (fn(), True)[1]),
            patch.object(eventlog_hooks, "emit", _emit),
            patch("kiro_crew.eventlog.service.get_service", return_value=projection),
        ):
            await self._wire(orch, existing_loops=[stopped])

        assert len(appended) == expected_appends
        if appended:
            assert appended == [
                (
                    "crew-x",
                    PATROL_STOPPED,
                    {"slot_key": "member-crew-x", "reason": AGENT_STOP_REASON},
                )
            ]
        orch.dashboard_state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("create_kind", ["legacy", "structured"])
    async def test_non_directive_create_preserves_an_agent_stopped_row(self, tmp_path, create_kind):
        """Dashboard-style creates retain the stopped row and its detail."""
        from kiro_crew.autonudge import AutoNudgeService, MonitorUpdateConflict

        service = AutoNudgeService(base_dir=tmp_path)
        try:
            first = await service.add(
                slot_key="chat-1",
                message="old work",
                idle_secs=300,
            )
            await service.update(
                first.id,
                active=False,
                stopped_reason=AGENT_STOP_REASON,
                stopped_detail="finished deliberately",
            )

            with pytest.raises(MonitorUpdateConflict, match="already has an automation"):
                if create_kind == "structured":
                    await service.add_monitor(
                        slot_key="chat-1",
                        kind="github_pull_request",
                        target="https://github.com/acme/widgets/pull/7",
                        objective="review_ready",
                        cadence_secs=300,
                        budgets=monitor_models.MonitorBudgets(),
                        replace_existing=False,
                    )
                else:
                    await service.add(
                        slot_key="chat-1",
                        message="new work",
                        idle_secs=300,
                        replace_existing=False,
                    )

            kept = service.get_by_slot("chat-1")
            assert kept is first
            assert kept.active is False
            assert kept.stopped_reason == AGENT_STOP_REASON
            assert kept.stopped_detail == "finished deliberately"
        finally:
            service.stop()

    @pytest.mark.asyncio
    async def test_observer_logs_a_revived_member_patrol_as_started(self):
        """Play revives a kept agent-stop row through ``updated`` rather than add."""
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PATROL_STARTED

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, inst = await self._wire(orch)
        appended: list[tuple[str, str, dict]] = []
        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (fn(), True)[1]),
            patch.object(
                eventlog_hooks,
                "emit",
                lambda slug, _actor, etype, data: (appended.append((slug, etype, data)), True)[1],
            ),
        ):
            inst._activity_transition_for_observer.return_value = (False, True, AGENT_STOP_REASON)
            observer("updated", _loop("member-crew-x", active=True))
        assert appended == [("crew-x", PATROL_STARTED, {"slot_key": "member-crew-x"})]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "prior_reason", [MANUAL_STOP_REASON, "max_cycles", "max_runtime", "stop_sentinel"]
    )
    async def test_observer_logs_no_start_for_a_resume_the_agent_did_not_stop(self, prior_reason):
        """Only reviving an agent-stopped row logs the patrol started; every other
        resume keeps the event log as the base wrote it."""
        from kiro_crew import eventlog_hooks

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, inst = await self._wire(orch)
        appended: list[tuple[str, str, dict]] = []
        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (fn(), True)[1]),
            patch.object(
                eventlog_hooks,
                "emit",
                lambda slug, _actor, etype, data: (appended.append((slug, etype, data)), True)[1],
            ),
        ):
            inst._activity_transition_for_observer.return_value = (False, True, prior_reason)
            observer("updated", _loop("member-crew-x", active=True))
        assert appended == []

    @pytest.mark.asyncio
    async def test_observer_broadcasts_structured_state_to_owners_only(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)
        secret = "AKIAIOSFODNN7EXAMPLE"
        loop = _loop("chat-1-1721")
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
        )
        loop.monitor.last_observation = {
            "checks": {"failed": [f"deploy?token={secret}"]},
            "target": f"github.com/acme/widgets#7?token={secret}",
        }
        loop.monitor.last_provider_error = f"provider rejected token {secret}"

        observer("armed", loop)

        orch.dashboard_state.broadcast_ws.assert_not_called()
        topic, payload = orch.dashboard_state.broadcast_ws_owners.call_args.args
        assert topic == "autonudge_state"
        assert payload["loop"]["monitor"]["target"].endswith("/pull/7")
        rendered = json.dumps(payload)
        assert secret not in rendered
        assert "provider rejected token" in payload["loop"]["monitor"]["last_provider_error"]

    @pytest.mark.asyncio
    async def test_observer_expired_event_also_notifies(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock()
        _on_fire, observer, _inst = await self._wire(orch)

        loop = _loop("chat-1-1721")
        observer("expired", loop)
        orch._notify_nudge_expired.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_observer_notifies_one_structured_terminal_transition(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock()
        _on_fire, observer, _inst = await self._wire(orch)
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        observer("updated", loop)
        observer("updated", loop)
        # flake-ok: pre-existing zero yield that lets the task the observer or startup schedules run before the next assertion; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)

        orch._notify_nudge_expired.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_observer_marks_delivery_only_after_notification_persistence(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        persisted = asyncio.get_running_loop().create_future()
        orch.dashboard_state.last_notification_persist = persisted
        orch._notify_nudge_expired = MagicMock(return_value=True)
        _on_fire, observer, inst = await self._wire(orch)
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        observer("updated", loop)
        # flake-ok: pre-existing zero yield that lets the task the observer or startup schedules run before the next assertion; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)

        inst.mark_terminal_notification_delivered.assert_not_awaited()
        persisted.set_result(True)
        # flake-ok: pre-existing zero yield that lets the task the observer or startup schedules run before the next assertion; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)

        inst.mark_terminal_notification_delivered.assert_awaited_once_with(
            loop.id,
            MonitorOutcome.SUCCESS,
            2.0,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("persistence_result", [False, RuntimeError("write failed")])
    async def test_observer_retries_when_notification_persistence_fails(self, persistence_result):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        persisted = asyncio.get_running_loop().create_future()
        if isinstance(persistence_result, Exception):
            persisted.set_exception(persistence_result)
        else:
            persisted.set_result(persistence_result)
        orch.dashboard_state.last_notification_persist = persisted
        orch._notify_nudge_expired = MagicMock(return_value=True)
        _on_fire, observer, inst = await self._wire(orch)
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        observer("updated", loop)
        # flake-ok: pre-existing zero yield that lets the task the observer or startup schedules run before the next assertion; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)
        observer("updated", loop)
        # flake-ok: pre-existing zero yield that lets the task the observer or startup schedules run before the next assertion; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)

        inst.mark_terminal_notification_delivered.assert_not_awaited()
        assert orch._notify_nudge_expired.call_count == 2

    @pytest.mark.asyncio
    async def test_restart_replays_terminal_notice_without_delivery_record(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        notification_started = asyncio.Event()

        def _notify(_loop: NudgeLoop) -> bool:
            notification_started.set()
            return True

        orch._notify_nudge_expired = MagicMock(side_effect=_notify)
        persisted = asyncio.get_running_loop().create_future()
        orch.dashboard_state.last_notification_persist = persisted
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        startup = asyncio.create_task(self._wire(orch, existing_loops=[loop]))
        try:
            await _within_lost_run(
                notification_started.wait(), "startup's replay of the terminal notice"
            )
        finally:
            if not notification_started.is_set():
                startup.cancel()
        # flake-ok: pre-existing zero yield that lets the task the observer or startup schedules run before the next assertion; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)
        startup_blocked = not startup.done()

        persisted.set_result(True)
        _on_fire, observer, inst = await _within_lost_run(startup, "startup after the persist")
        # flake-ok: pre-existing zero yield that lets the task the observer or startup schedules run before the next assertion; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)

        observer("updated", loop)

        assert not startup_blocked
        orch._notify_nudge_expired.assert_called_once_with(loop)
        inst.mark_terminal_notification_delivered.assert_awaited_once_with(
            loop.id,
            MonitorOutcome.SUCCESS,
            2.0,
        )

    @pytest.mark.asyncio
    async def test_terminal_transition_during_start_is_not_lost(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock(return_value=True)
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        await self._wire(orch, emit_during_start=loop)
        # flake-ok: pre-existing zero yield that lets the task the observer or startup schedules run before the next assertion; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(0)

        orch._notify_nudge_expired.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_restart_deduplicates_terminal_notice_with_delivery_record(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock()
        loop = _loop("chat-1-1721")
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
            terminal_notification_delivered=True,
        )
        _on_fire, observer, inst = await self._wire(orch, existing_loops=[loop])

        observer("updated", loop)

        orch._notify_nudge_expired.assert_not_called()
        inst.mark_terminal_notification_delivered.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observer_does_not_repeat_a_gated_legacy_terminal_notification(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch._notify_nudge_expired = MagicMock()
        _on_fire, observer, _inst = await self._wire(orch)
        loop = _loop("slack:111.222")
        loop.gate = True
        loop.active = False
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            created_ts=1.0,
            outcome=MonitorOutcome.SUCCESS,
            stopped_at=2.0,
        )

        observer("expired", loop)
        observer("fired", loop)

        orch._notify_nudge_expired.assert_called_once_with(loop)

    @pytest.mark.asyncio
    async def test_observer_without_loop_broadcasts_nothing(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, _inst = await self._wire(orch)

        observer("stopped", None)
        orch.dashboard_state.broadcast_ws.assert_not_called()

    # ═════════════════════════════════════════════════════════════════════════
    # _fire_dashboard_nudge happy path
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_a_patrol_rearmed_between_snapshot_and_write_is_not_closed(self):
        """The reconcile's closer is re-asked against the episode it read.

        Another writer re-arms the patrol (a new ``since``) after the reconcile's
        snapshot and before its write: the stale stop must not land on the new
        episode, and the stopped row's frame still publishes because nothing of
        that stop is left to persist.
        """
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.types import PROJ_WAKE

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        _on_fire, observer, inst = await self._wire(orch)
        wake = {"patrol": "armed", "slot_key": "member-crew-x", "since": 1}
        appended: list[str] = []

        def _snapshot(_slug):
            seen = {"values": {PROJ_WAKE: dict(wake)}}
            wake["since"] = 2  # re-armed by another writer after this read
            return seen

        def _emit(_slug, _actor, etype, _data):
            appended.append(etype)
            return True

        projection = MagicMock()
        projection.snapshot.side_effect = _snapshot
        projection.append_closer_if_still_applies.side_effect = _closer_through(
            lambda: _emit, lambda: wake
        )
        with (
            patch.object(eventlog_hooks, "member_slug_for_slot", lambda _slot: "crew-x"),
            patch.object(eventlog_hooks, "submit", lambda fn: (fn(), True)[1]),
            patch.object(eventlog_hooks, "emit", _emit),
            patch("kiro_crew.eventlog.service.get_service", return_value=projection),
        ):
            stopped = _loop(
                "member-crew-x",
                active=False,
                stopped_reason=AGENT_STOP_REASON,
                stopped_detail="done",
            )
            inst._activity_transition_for_observer.side_effect = [
                (False, False, AGENT_STOP_REASON),
            ]
            observer("updated", stopped)
            await asyncio.sleep(0)

        assert appended == []
        assert wake["patrol"] == "armed"
        orch.dashboard_state.broadcast_ws.assert_called_once()


class TestFireDashboardNudgeDispatch:
    """The dispatch half of the dashboard nudge (existing tests cover the skips)."""

    @pytest.mark.asyncio
    async def test_idle_slot_appends_nudge_and_spawns_a_turn(self, monkeypatch):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = False
        slot.key = "chat-1"
        ds.get_slot.return_value = slot
        orch.dashboard_state = ds

        # _run_chat's return value is handed straight to the (patched)
        # spawn_guarded_turn, so a plain sentinel avoids creating a coroutine
        # nothing will ever await.
        task = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", MagicMock(return_value="CORO"))
        monkeypatch.setattr(gw, "spawn_guarded_turn", MagicMock(return_value=task))

        loop = _loop("chat-1", cycle_count=1)
        assert await orch._fire_dashboard_nudge(loop) is True

        role, body, css = slot.append.call_args.args
        assert role == "nudge"
        assert body.startswith("[auto-nudge cycle 2]\n")
        assert css == "msg msg-nudge"
        meta = slot.append.call_args.kwargs["meta"]
        assert meta == {"nudge": {"cycle": 2, "loop_id": "loop-1"}}
        assert slot.task is task
        assert orch._session_tasks["chat-1"] is task
        ds.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_structured_delivery_distinguishes_busy_and_unavailable(self, monkeypatch):
        busy = _make_orchestrator()
        busy_state = _mock_dashboard_state()
        busy_slot = MagicMock(running=True)
        busy_state.get_slot.return_value = busy_slot
        busy.dashboard_state = busy_state
        unavailable = _make_orchestrator()
        unavailable_state = _mock_dashboard_state()
        unavailable_state.get_slot.return_value = None
        unavailable.dashboard_state = unavailable_state
        monkeypatch.setattr(gw, "rehydrate_slot_from_history_async", AsyncMock(return_value=None))

        assert await busy._fire_dashboard_nudge(_loop("chat-1"), "[Monitor wake]") is (
            monitor_models.MonitorDispatchResult.BUSY
        )
        assert (
            await unavailable._fire_dashboard_nudge(_loop("chat-2"), "[Monitor wake]")
            is monitor_models.MonitorDispatchResult.UNAVAILABLE
        )

    @pytest.mark.asyncio
    async def test_rehydrated_slot_is_used_when_the_registry_is_cold(self, monkeypatch):
        """A get_slot miss is a cold cache, not a dead session — restore it."""
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.get_slot.return_value = None
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.remove = AsyncMock()

        restored = MagicMock()
        restored.running = False
        restored.key = "chat-9"

        async def _rehydrate(_state, _key, *, adopt_closed=False):
            assert (
                adopt_closed is True
            ), "a nudge loop must survive its slot being archived by idle cleanup"
            return restored

        monkeypatch.setattr(gw, "rehydrate_slot_from_history_async", _rehydrate)
        monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", MagicMock(return_value="CORO"))
        monkeypatch.setattr(gw, "spawn_guarded_turn", MagicMock(return_value=MagicMock()))

        assert await orch._fire_dashboard_nudge(_loop("chat-9")) is True
        orch.autonudge_svc.remove.assert_not_called()
        restored.append.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════
# _fire_webex_nudge  (adapter over the shared _fire_dm_nudge ladder)
# ═════════════════════════════════════════════════════════════════════════


_WKEY = "webex:kirocrew:direct:a@b.test"


def _webex_transport(
    *,
    authorized: bool = True,
    current_key: str | None = None,
    origin_room: str | None = None,
) -> MagicMock:
    """A Webex transport double exposing the surface the fire path uses.

    Note where authorization lives: Webex holds its allow-list on the TRANSPORT,
    where Discord holds it on the dispatcher. That difference is the whole reason
    the adapter supplies ``authorize`` instead of the ladder calling one of them.
    """
    dispatcher = MagicMock()
    dispatcher.current_session_key = MagicMock(
        return_value=current_key if current_key is not None else _WKEY
    )
    dispatcher.handle_message = AsyncMock(return_value=None)
    sessions = MagicMock()
    sessions.is_busy = MagicMock(return_value=False)
    sessions.get_origin_link = MagicMock(
        return_value=SimpleNamespace(channel_id=origin_room) if origin_room else None
    )
    dispatcher.sessions = sessions
    transport = MagicMock()
    transport.dispatcher = dispatcher
    transport.is_authorized = MagicMock(return_value=authorized)
    transport.resolve_conversation = AsyncMock(return_value="a@b.test")
    return transport


def _webex_orchestrator(transport: MagicMock | None) -> Any:
    orch = _make_orchestrator()
    ds = _mock_dashboard_state()
    ds.channel_transports = {"webex": transport} if transport is not None else {}
    orch.dashboard_state = ds
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    return orch


class TestFireWebexNudge:
    """Synthetic-injection path for a Webex DM babysit loop."""

    @pytest.mark.asyncio
    async def test_no_transport_skips_without_removing_loop(self):
        """Transport not running is transient: skip, but keep the loop armed."""
        orch = _webex_orchestrator(None)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_key_shape_retires_loop(self):
        """A key that is not ``webex:{agent}:direct:{email}`` can never route."""
        orch = _webex_orchestrator(_webex_transport())
        assert await orch._fire_webex_nudge(_loop("webex:kirocrew:space")) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1", stop_reason="unsupported_key")

    @pytest.mark.asyncio
    async def test_unauthorized_email_retires_loop(self):
        """The allow-list is re-checked at fire time because it can shrink."""
        transport = _webex_transport(authorized=False)
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        transport.is_authorized.assert_called_once_with("a@b.test")
        orch.autonudge_svc.remove.assert_awaited_once_with(
            "loop-1", stop_reason="user_not_authorized"
        )
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotated_session_retires_loop(self):
        """Firing into a rotated key would run without the loop's context."""
        transport = _webex_transport(current_key="webex:kirocrew:direct:a@b.test:gen2")
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1", stop_reason="session_rotated")
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_busy_session_skips_without_removing_loop(self):
        """A human's own turn is running, so the cycle is skipped, not queued."""
        transport = _webex_transport()
        transport.dispatcher.sessions.is_busy = MagicMock(return_value=True)
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivers_a_direct_room_inbound_without_command_parsing(self):
        transport = _webex_transport()
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is True
        transport.dispatcher.handle_message.assert_awaited_once()
        inbound = transport.dispatcher.handle_message.await_args.args[0]
        kwargs = transport.dispatcher.handle_message.await_args.kwargs
        assert kwargs == {"interpret_commands": False}
        assert inbound.person_email == "a@b.test"
        assert inbound.text.startswith("[auto-nudge cycle 1]")
        from kiro_crew.webex.transport import ROOM_DIRECT

        assert inbound.room_type == ROOM_DIRECT

    @pytest.mark.asyncio
    async def test_persisted_origin_link_wins_over_resolve_conversation(self):
        """The bind is matched by VALUE, so the nudge must reuse its spelling.

        ``resolve_conversation`` answers with the EMAIL, which delivers but is a
        second spelling of the same room. Writing that spelling would make a
        later unlink miss the binding, so a persisted link wins.
        """
        transport = _webex_transport(origin_room="ROOM_FROM_LINK")
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is True
        inbound = transport.dispatcher.handle_message.await_args.args[0]
        assert inbound.room_id == "ROOM_FROM_LINK"
        transport.resolve_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_email_is_the_first_turn_fallback_when_no_link_exists(self):
        """With no binding yet there is nothing to disagree with."""
        transport = _webex_transport(origin_room=None)
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is True
        inbound = transport.dispatcher.handle_message.await_args.args[0]
        assert inbound.room_id == "a@b.test"
        transport.resolve_conversation.assert_awaited_once_with("a@b.test")

    @pytest.mark.asyncio
    async def test_dispatch_failure_reports_false_and_keeps_the_loop(self):
        transport = _webex_transport()
        transport.dispatcher.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        orch = _webex_orchestrator(transport)
        assert await orch._fire_webex_nudge(_loop(_WKEY)) is False
        orch.autonudge_svc.remove.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# _fire_dm_nudge  (the shared ladder itself)
# ═════════════════════════════════════════════════════════════════════════


class TestDmFireSpineIsReusable:
    """A dispatcher-routed channel joins by supplying an adapter, not a ladder.

    This is the claim the extraction makes, so it is tested directly rather than
    argued: a channel the gateway has never heard of fires correctly, and
    inherits every guard, with no code beyond the five adapter members.
    """

    @staticmethod
    def _adapter() -> Any:
        return gw._DmDispatchAdapter(
            channel="zulip",
            supports_monitor=False,
            authorize=lambda transport, _dispatcher, principal: bool(
                transport.is_authorized(principal)
            ),
            resolve_conversation=(
                lambda transport, _sessions, _key, principal: transport.resolve_conversation(
                    principal
                )
            ),
            build_inbound=lambda principal, conversation_id, text: SimpleNamespace(
                who=principal, where=conversation_id, text=text
            ),
        )

    @staticmethod
    def _orchestrator(transport: MagicMock | None) -> Any:
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        ds.channel_transports = {"zulip": transport} if transport is not None else {}
        orch.dashboard_state = ds
        orch.autonudge_svc = MagicMock()
        orch.autonudge_svc.remove = AsyncMock()
        return orch

    @staticmethod
    def _transport(*, authorized: bool = True, current_key: str | None = None) -> MagicMock:
        key = "zulip:kirocrew:direct:z1"
        dispatcher = MagicMock()
        dispatcher.current_session_key = MagicMock(
            return_value=current_key if current_key is not None else key
        )
        dispatcher.handle_message = AsyncMock(return_value=None)
        sessions = MagicMock()
        sessions.is_busy = MagicMock(return_value=False)
        dispatcher.sessions = sessions
        transport = MagicMock()
        transport.dispatcher = dispatcher
        transport.is_authorized = MagicMock(return_value=authorized)
        transport.resolve_conversation = AsyncMock(return_value="STREAM1")
        return transport

    @pytest.mark.asyncio
    async def test_an_unknown_channel_delivers_with_only_an_adapter(self):
        transport = self._transport()
        orch = self._orchestrator(transport)
        loop = _loop("zulip:kirocrew:direct:z1")
        assert await orch._fire_dm_nudge(loop, self._adapter()) is True
        transport.dispatcher.handle_message.assert_awaited_once()
        inbound = transport.dispatcher.handle_message.await_args.args[0]
        assert (inbound.who, inbound.where) == ("z1", "STREAM1")
        assert inbound.text.startswith("[auto-nudge cycle 1]")

    @pytest.mark.asyncio
    async def test_the_new_channel_inherits_the_authorization_guard(self):
        transport = self._transport(authorized=False)
        orch = self._orchestrator(transport)
        loop = _loop("zulip:kirocrew:direct:z1")
        assert await orch._fire_dm_nudge(loop, self._adapter()) is False
        orch.autonudge_svc.remove.assert_awaited_once_with(
            "loop-1", stop_reason="user_not_authorized"
        )
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_new_channel_inherits_the_generation_guard(self):
        transport = self._transport(current_key="zulip:kirocrew:direct:z1:gen2")
        orch = self._orchestrator(transport)
        loop = _loop("zulip:kirocrew:direct:z1")
        assert await orch._fire_dm_nudge(loop, self._adapter()) is False
        orch.autonudge_svc.remove.assert_awaited_once_with("loop-1", stop_reason="session_rotated")
        transport.dispatcher.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_monitor_wake_on_a_monitorless_channel_delivers_nothing(self):
        """``supports_monitor=False`` refuses a wake instead of half-delivering it.

        A channel with no structured dispatch cannot report whether the wake
        landed. Delivering first and answering UNAVAILABLE afterwards would show
        the text to the reader while the controller counts the wake as
        undelivered and sends it again, so the refusal comes first.
        """
        transport = self._transport()
        orch = self._orchestrator(transport)
        loop = _loop("zulip:kirocrew:direct:z1")
        result = await orch._fire_dm_nudge(loop, self._adapter(), "wake up")
        assert result is monitor_models.MonitorDispatchResult.UNAVAILABLE
        transport.dispatcher.handle_message.assert_not_awaited()
        orch.autonudge_svc.remove.assert_not_called()
