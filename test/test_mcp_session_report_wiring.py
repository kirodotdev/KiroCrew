"""Wiring for the per-session MCP report: capture, publish, and invalidation.

The report's own accumulation rules are covered in
``test_mcp_session_report.py``; this covers the seams that feed and drain it —
the two transports' init drains, the dashboard publish path, and the resets that
must drop a report describing a torn-down session.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import (
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    JsonRpcMessage,
)
from kiro_crew.dashboard.chat_runner import (
    _publish_session_mcp_report,
    _record_session_mcp_event,
    _session_mcp_report,
)
from kiro_crew.dashboard.state import _ChatSlot


def _ready_frame(name: str) -> JsonRpcMessage:
    return JsonRpcMessage(method=METHOD_MCP_SERVER_INITIALIZED, params={"serverName": name})


def _failed_frame(name: str, error: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_MCP_SERVER_INIT_FAILURE, params={"serverName": name, "error": error}
    )


class TestAcpClientCapture:
    """The dedicated transport records the frames its init drain consumes."""

    @pytest.mark.asyncio
    async def test_drain_records_buffered_frames(self, tmp_path):
        # Before this, the drain reduced these frames to one log line and
        # cleared them, so a session that started without a server could not say
        # so afterwards.
        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            _ready_frame("kirocrew-core"),
            _failed_frame("slack-mcp", "spawn ENOENT"),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        payload = client.mcp_session_report().payload()
        assert payload is not None
        assert payload["ready"] == ["kirocrew-core"]
        assert payload["failed"] == ["slack-mcp"]
        assert payload["failures"] == {"slack-mcp": "spawn ENOENT"}

    @pytest.mark.asyncio
    async def test_drain_records_live_frames(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = []
        frames = [_ready_frame("github-mcp")]

        async def fake_read(timeout=2.0):
            return frames.pop(0) if frames else None

        client._read_message = fake_read

        await client._drain_notifications(duration=0.2)

        payload = client.mcp_session_report().payload()
        assert payload is not None
        assert payload["ready"] == ["github-mcp"]

    @pytest.mark.asyncio
    async def test_drain_ignores_unrelated_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            JsonRpcMessage(method="mcp/serverReady", params={"name": "legacy-shape"}),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        # Not a registration method, so it contributes nothing — the report says
        # "nothing reported" rather than inventing a server from a log-only frame.
        assert client.mcp_session_report().payload() is None

    def test_accessor_does_not_drain(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client.mcp_session_report().record_frame(_ready_frame("a"), owned=True)
        first = client.mcp_session_report().payload()
        second = client.mcp_session_report().payload()
        assert (
            first
            == second
            == {
                "configured": [],
                "ready": ["a"],
                "failed": [],
                "awaiting_auth": [],
                "failures": {},
            }
        )

    def test_reset_state_drops_the_report(self, tmp_path):
        # A replacement process re-initializes its servers and reports again;
        # carrying the old report over would present a dead session's list.
        client = AcpClient(work_dir=tmp_path)
        client.mcp_session_report().record_frame(_ready_frame("a"), owned=True)
        assert client.mcp_session_report().payload() is not None

        client._reset_state()

        assert client.mcp_session_report().payload() is None


class TestSlotState:
    def test_set_and_clear(self):
        slot = _ChatSlot("s1")
        assert slot.mcp_report_payload() is None
        assert slot.set_mcp_report({"ready": ["a"]}) is True
        assert slot.set_mcp_report({"ready": ["a"]}) is False
        assert slot.mcp_report_payload() == {"ready": ["a"]}
        assert slot.clear_mcp_report() is True
        assert slot.clear_mcp_report() is False
        assert slot.mcp_report_payload() is None

    def test_non_dict_is_stored_as_absent(self):
        slot = _ChatSlot("s1")
        assert slot.set_mcp_report("nope") is False  # type: ignore[arg-type]
        assert slot.mcp_report_payload() is None

    def test_projection_carries_the_report(self):
        slot = _ChatSlot("s1")
        assert slot.to_dict()["mcp_report"] is None
        slot.set_mcp_report({"ready": ["a"]})
        assert slot.to_dict()["mcp_report"] == {"ready": ["a"]}


class TestPublish:
    def test_publishes_and_broadcasts(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        report = McpSessionReport()
        report.begin_session([{"name": "kirocrew-core"}])
        report.record_frame(_ready_frame("kirocrew-core"), owned=True)
        acp = MagicMock()
        acp.mcp_session_report.return_value = report

        _publish_session_mcp_report(state, slot, acp)

        payload = slot.mcp_report_payload()
        assert payload is not None
        assert payload["ready"] == ["kirocrew-core"]
        state.broadcast_ws.assert_called_once_with(
            "mcp_report_update", {"slot": "s1", "mcp_report": payload}
        )

    def test_unchanged_report_broadcasts_nothing(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        report = McpSessionReport()
        report.record_frame(_ready_frame("a"), owned=True)
        acp = MagicMock()
        acp.mcp_session_report.return_value = report

        _publish_session_mcp_report(state, slot, acp)
        _publish_session_mcp_report(state, slot, acp)

        assert state.broadcast_ws.call_count == 1

    def test_object_without_the_accessor_is_a_no_op(self):
        # The dashboard reaches this duck-typed, and a foreign or placeholder
        # provider must not cost a failed publish.
        slot = _ChatSlot("s1")
        state = MagicMock()

        _publish_session_mcp_report(state, slot, object())

        assert slot.mcp_report_payload() is None
        state.broadcast_ws.assert_not_called()

    def test_none_client_is_a_no_op(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        _publish_session_mcp_report(state, slot, None)
        state.broadcast_ws.assert_not_called()

    def test_accessor_raising_is_swallowed(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        acp = MagicMock()
        acp.mcp_session_report.side_effect = RuntimeError("boom")

        _publish_session_mcp_report(state, slot, acp)

        assert slot.mcp_report_payload() is None
        state.broadcast_ws.assert_not_called()

    def test_foreign_return_value_is_refused(self):
        acp = MagicMock()
        acp.mcp_session_report.return_value = {"ready": ["a"]}
        assert _session_mcp_report(acp) is None


class TestLiveEvents:
    def test_late_initialized_event_moves_a_failed_server(self):
        # The OAuth shape: a server fails at init, the user authorizes, and the
        # server comes up mid-turn. The init drain already consumed its frames,
        # so this event is the only signal that it is now up.
        slot = _ChatSlot("s1")
        state = MagicMock()
        report = McpSessionReport()
        report.record_frame(_failed_frame("builder-mcp", "401"), owned=True)
        acp = MagicMock()
        acp.mcp_session_report.return_value = report
        slot._acp_client = acp
        _publish_session_mcp_report(state, slot, acp)

        _record_session_mcp_event(state, slot, acp, EVENT_MCP_SERVER_INITIALIZED, "builder-mcp")

        payload = slot.mcp_report_payload()
        assert payload is not None
        assert payload["ready"] == ["builder-mcp"]
        assert payload["failed"] == []
        assert payload["failures"] == {}

    def test_late_failure_event_records_its_reason(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        report = McpSessionReport()
        report.record_frame(_ready_frame("slack-mcp"), owned=True)
        acp = MagicMock()
        acp.mcp_session_report.return_value = report
        slot._acp_client = acp

        _record_session_mcp_event(
            state, slot, acp, EVENT_MCP_SERVER_INIT_FAILURE, "slack-mcp", "died later"
        )

        payload = slot.mcp_report_payload()
        assert payload is not None
        assert payload["failed"] == ["slack-mcp"]
        assert payload["failures"] == {"slack-mcp": "died later"}

    def test_event_with_no_live_session_is_a_no_op(self):
        # No provider to ask — the turn is not running one. Nothing recorded,
        # nothing pushed.
        slot = _ChatSlot("s1")
        state = MagicMock()

        _record_session_mcp_event(state, slot, None, EVENT_MCP_SERVER_INITIALIZED, "a")

        assert slot.mcp_report_payload() is None
        state.broadcast_ws.assert_not_called()

    def test_repeat_event_broadcasts_nothing(self):
        slot = _ChatSlot("s1")
        state = MagicMock()
        report = McpSessionReport()
        acp = MagicMock()
        acp.mcp_session_report.return_value = report
        slot._acp_client = acp

        _record_session_mcp_event(state, slot, acp, EVENT_MCP_SERVER_INITIALIZED, "a")
        _record_session_mcp_event(state, slot, acp, EVENT_MCP_SERVER_INITIALIZED, "a")

        assert state.broadcast_ws.call_count == 1


class TestResetInvalidation:
    """A report must never outlive the session it describes."""

    @staticmethod
    def _state(reset_result: bool) -> MagicMock:
        state = MagicMock()

        async def _reset(_key: str, *, skip_if_busy: bool = False) -> bool:
            return reset_result

        state.sessions.reset = _reset
        return state

    @pytest.mark.asyncio
    async def test_reset_drops_the_report_and_broadcasts(self):
        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        slot = _ChatSlot("s1")
        slot.set_mcp_report({"ready": ["a"]})
        state = self._state(True)

        with patch("kiro_crew.dashboard.chat_handlers._unblock_pending_waits"):
            assert await _reset_slot_session(state, slot, "dashboard:s1") is True

        assert slot.mcp_report_payload() is None
        state.broadcast_ws.assert_any_call("mcp_report_update", {"slot": "s1", "mcp_report": None})

    @pytest.mark.asyncio
    async def test_declined_reset_keeps_the_report(self):
        # skip_if_busy declined the reset, so the session described by the report
        # is still the live one. Clearing it here would blank a true answer.
        from kiro_crew.dashboard.chat_handlers import _reset_slot_session

        slot = _ChatSlot("s1")
        slot.set_mcp_report({"ready": ["a"]})
        state = self._state(False)

        with patch("kiro_crew.dashboard.chat_handlers._unblock_pending_waits"):
            assert await _reset_slot_session(state, slot, "dashboard:s1") is False

        assert slot.mcp_report_payload() == {"ready": ["a"]}
        state.broadcast_ws.assert_not_called()


class TestLiveEventOwnershipIsWired:
    """The runner must hand the event's provenance to the report.

    The report refuses an ownerless event and the transport now marks one, but
    the wire between them is its own failure point: drop the keyword here and
    both halves still look correct while every co-tenant records the frame.
    """

    @staticmethod
    def _slot_with_report():
        from kiro_crew.acp.mcp_session_report import McpSessionReport

        report = McpSessionReport()
        slot = _ChatSlot("s1")
        # The PROVIDER carries the accessor, per the LLMProvider contract. The
        # earlier shape reached `slot._acp_client` — the provider's INNER client
        # — which the shared runtime's provider does not have, so the report went
        # missing on that transport while a test built this way stayed green.
        provider = SimpleNamespace(mcp_session_report=lambda: report)
        return slot, provider, report

    def test_an_ownerless_event_does_not_reach_the_report(self):
        from kiro_crew.acp.types import EVENT_MCP_SERVER_INITIALIZED
        from kiro_crew.dashboard.chat_runner import _record_session_mcp_event

        slot, provider, report = self._slot_with_report()
        state = MagicMock()
        _record_session_mcp_event(
            state, slot, provider, EVENT_MCP_SERVER_INITIALIZED, "shared", fanout_no_owner=True
        )
        assert report.payload() is None
        state.broadcast_ws.assert_not_called()

    def test_an_owned_event_does(self):
        from kiro_crew.acp.types import EVENT_MCP_SERVER_INITIALIZED
        from kiro_crew.dashboard.chat_runner import _record_session_mcp_event

        slot, provider, report = self._slot_with_report()
        _record_session_mcp_event(MagicMock(), slot, provider, EVENT_MCP_SERVER_INITIALIZED, "mine")
        payload = report.payload()
        assert payload is not None and payload["ready"] == ["mine"]

    def test_the_roster_keeps_the_stall_clocks_ownership_rule(self):
        # Two ownership rules coexist in the same event loop and must not be
        # unified: the subagent roster feeds the idle-stall clock, whose
        # contract treats a LONE session as the owner of an ownerless frame
        # (fanout_no_owner is set only once a second queue registers), while
        # the MCP report events use the strict frame-must-name-me test.
        # Swapping the roster onto the strict test marks a lone session's
        # roster global, the clock ignores it, and an active subagent reads
        # as stalled — the exact regression a bulk rename introduced once.
        import inspect

        from kiro_crew.acp import session_handle as handle_mod

        src = inspect.getsource(handle_mod)
        roster_at = src.index("kind=EVENT_SUBAGENT_LIST,")
        roster_window = src[roster_at : roster_at + 300]
        assert "runtime_global=msg.fanout_no_owner" in roster_window, (
            "the subagent roster must keep the stall clock's lone-session-owns "
            "semantics, not the report path's strict ownership test"
        )
        assert src.count("runtime_global=not self._owns_mcp_frame(msg)") == 3, (
            "exactly the three MCP report event constructions use the strict "
            "ownership test; the roster is not one of them"
        )


class TestBothTransportsAreReached:
    """The reach must work for BOTH providers, which is what silently failed.

    The publish used to take the provider's inner ``.client``. ``AcpProvider``
    (dedicated) exposes one; ``AcpSessionProvider`` (shared runtime) does not, so
    the reach evaluated to None there and the report never reached the slot —
    the panel fell back to host-configured green dots on that whole transport,
    which is the exact defect this feature exists to remove. Every test at the
    time built its double the same wrong way, so all of them stayed green.
    """

    @staticmethod
    def _report():
        return McpSessionReport()

    def test_a_provider_with_no_inner_client_still_reports(self):
        # Shape of AcpSessionProvider: the accessor is on the provider itself and
        # there is deliberately no `.client` to reach through.
        report = self._report()
        report.begin_session([{"name": "kirocrew-core"}])
        report.record_frame(_ready_frame("kirocrew-core"), owned=True)
        provider = SimpleNamespace(mcp_session_report=lambda: report)
        assert not hasattr(provider, "client")

        slot = _ChatSlot("s1")
        state = MagicMock()
        _publish_session_mcp_report(state, slot, provider)

        payload = slot.mcp_report_payload()
        assert payload is not None, "the shared runtime's provider must be reachable"
        assert payload["ready"] == ["kirocrew-core"]

    def test_a_provider_with_an_inner_client_reports_too(self):
        # Shape of AcpProvider: it also answers the call itself, by delegating.
        report = self._report()
        report.begin_session([{"name": "kirocrew-core"}])
        report.record_frame(_ready_frame("kirocrew-core"), owned=True)
        inner = SimpleNamespace(mcp_session_report=lambda: report)
        provider = SimpleNamespace(client=inner, mcp_session_report=lambda: report)

        slot = _ChatSlot("s2")
        _publish_session_mcp_report(MagicMock(), slot, provider)

        payload = slot.mcp_report_payload()
        assert payload is not None and payload["ready"] == ["kirocrew-core"]


class TestReportFrameFloor:
    """The raw notification buffer must not carry frames across attempts either.

    ``begin_session`` clears the derived buckets, but a failed ``session/load``
    leaves its notifications in the shared buffer and the fallback
    ``session/new`` drains them afterwards — so the leak survived one layer down.
    The buffer is NOT cleared: the OAuth and config captures read it too, and an
    authorization request from the failed attempt is still one the user must
    answer. Only the report's view of it is floored.
    """

    @staticmethod
    def _client():
        client = AcpClient.__new__(AcpClient)
        client._mcp_notifications = []
        client._mcp_report = McpSessionReport()
        client._mcp_report_frame_floor = 0
        return client

    def test_the_floor_lands_above_the_failed_attempts_frames(self):
        client = self._client()
        # Two notifications buffered while the session/load attempt was in flight.
        client._mcp_notifications.extend([_ready_frame("from-load"), _ready_frame("also-load")])

        client._begin_session_report([{"name": "fresh"}])

        assert client._mcp_report_frame_floor == 2
        payload = client._mcp_report.payload()
        assert payload is not None and payload["configured"] == ["fresh"]

    def test_the_drain_gates_the_report_write_on_the_floor(self):
        # No cheap behavioural route into _drain_notifications (it needs a live
        # client), so this pins the one line the leak lives on. The OAuth and
        # config captures above it must stay UNgated.
        import inspect

        from kiro_crew.acp import client as client_mod

        src = inspect.getsource(client_mod)
        marker = "if idx >= self._mcp_report_frame_floor:"
        assert marker in src, (
            "the buffered drain records into the report without the attempt floor, "
            "so a failed session/load's frames reach the replacement session"
        )
        window = src[max(0, src.index(marker) - 600) : src.index(marker)]
        for ungated in ("_capture_oauth(msg)", "_capture_config_update(msg)"):
            assert ungated in window, f"{ungated} must run for every frame, floored or not"


class TestSerializerDropsOrphanedReport:
    """The single enforcement point: a report must describe the CURRENT session.

    Clearing at each teardown call site was the wrong shape — there are many (the
    reset funnel, the reload and reset-conversation routes, the queued discard, a
    channel handler, the cron reaper, the task runner, a project change) and a new
    one silently skips it. A liveness check was not enough either: a reset
    recreates a session under the same key, so the slot looks alive while the
    report describes the session that went. The serializer both the REST and WS
    snapshots share asks for IDENTITY, which no path can bypass.
    """

    @staticmethod
    def _state_with_slot(live_id: str | None, *, stamped: str = "sid-1"):
        from kiro_crew.dashboard.state import DashboardState

        state = DashboardState.__new__(DashboardState)
        slot = _ChatSlot("s1")
        slot.set_mcp_report({"ready": ["a"]}, stamped)
        state._slots = {"s1": slot}
        provider = None if live_id is None else SimpleNamespace(session_id=live_id)
        state.sessions = SimpleNamespace(get_provider=lambda _k: provider)
        state.serialize_slot = lambda s, **kw: s.to_dict()
        return state, slot

    def test_report_survives_while_its_own_session_is_live(self):
        state, slot = self._state_with_slot("sid-1")
        payloads = state.serialize_slots()
        assert slot.mcp_report_payload() == {"ready": ["a"]}
        assert payloads[0]["mcp_report"] == {"ready": ["a"]}

    def test_report_is_dropped_once_the_session_is_gone(self):
        state, slot = self._state_with_slot(None)
        payloads = state.serialize_slots()
        assert slot.mcp_report_payload() is None
        assert payloads[0]["mcp_report"] is None

    def test_report_is_dropped_when_the_session_was_REPLACED(self):
        # The case a liveness check cannot see, and the one a project change
        # produces: reset recreates a session under the same key, so something
        # IS alive — just not the session this payload was taken under.
        state, slot = self._state_with_slot("sid-2", stamped="sid-1")
        payloads = state.serialize_slots()
        assert slot.mcp_report_payload() is None
        assert payloads[0]["mcp_report"] is None

    def test_a_mid_turn_rebind_does_not_clear_the_live_turns_report(self):
        # A cron injection reassigns ``linked_session_key`` on a live slot with
        # no ``running`` gate. The report describes the session the in-flight
        # turn is RUNNING on; resolving the reassigned routing instead reads a
        # different provider (or none), mismatches, and clears a live session's
        # report mid-turn. The turn's own key must win — the same rule turn
        # cancellation follows.
        from kiro_crew.dashboard.state import DashboardState

        state = DashboardState.__new__(DashboardState)
        slot = _ChatSlot("s1")
        slot.set_mcp_report({"ready": ["a"]}, "sid-1")
        slot._active_turn_session_key = "dashboard:s1"
        slot.linked_session_key = "cron:job-9"  # rebound mid-turn
        state._slots = {"s1": slot}
        providers = {"dashboard:s1": SimpleNamespace(session_id="sid-1")}
        state.sessions = SimpleNamespace(get_provider=lambda k: providers.get(k))
        state.serialize_slot = lambda s, **kw: s.to_dict()
        payloads = state.serialize_slots()
        assert slot.mcp_report_payload() == {"ready": ["a"]}
        assert payloads[0]["mcp_report"] == {"ready": ["a"]}

    def test_a_published_report_survives_its_own_serialize(self):
        # The end-to-end binding, and the one a stamped-by-hand test cannot show:
        # publish must record the identity the serializer will ask for. Stamping
        # nothing there makes the serializer drop the report on EVERY snapshot —
        # the feature silently gone while both halves look correct in isolation.
        from kiro_crew.dashboard.state import DashboardState

        report = McpSessionReport()
        report.begin_session([{"name": "kirocrew-core"}])
        report.record_frame(_ready_frame("kirocrew-core"), owned=True)
        provider = SimpleNamespace(session_id="sid-live", mcp_session_report=lambda: report)

        slot = _ChatSlot("s1")
        _publish_session_mcp_report(MagicMock(), slot, provider)

        state = DashboardState.__new__(DashboardState)
        state._slots = {"s1": slot}
        state.sessions = SimpleNamespace(get_provider=lambda _k: provider)
        state.serialize_slot = lambda s, **kw: s.to_dict()
        payloads = state.serialize_slots()

        assert payloads[0]["mcp_report"] is not None, (
            "publish did not stamp the identity the serializer asks for, so the "
            "report is dropped on every snapshot"
        )
        assert payloads[0]["mcp_report"]["ready"] == ["kirocrew-core"]

    def test_a_channel_slot_is_checked_on_its_linked_key(self):
        # A channel-born slot's turns run on the channel's own session, so
        # checking the dashboard-derived key would find no provider and blank a
        # live report.
        state, slot = self._state_with_slot("sid-1")
        slot.linked_session_key = "slack:1700000000.1"
        asked: list[str] = []

        def _get(key: str):
            asked.append(key)
            return SimpleNamespace(session_id="sid-1")

        state.sessions = SimpleNamespace(get_provider=_get)
        state.serialize_slots()
        assert asked == ["slack:1700000000.1"]
        assert slot.mcp_report_payload() == {"ready": ["a"]}
