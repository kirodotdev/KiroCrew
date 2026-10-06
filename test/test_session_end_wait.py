"""``session_end_wait``: one session waking a worker it created from ``wait``.

Covers the three layers the verb crosses: ``session_control.end_wait_target``
(the gate, the parked request, the not-waiting reply), the keepalive handoff that
tells the woken tool who ended its sleep, and the MCP dispatch in
``mcp_dashboard``. The ``wait`` tool's own result text is covered in
``test_wait_tool_early_end.py`` beside the other early-end cases.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.handlers.sessions import _service_wait_ping


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _pair(tmp_path, *, created: bool = True):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    if created:
        target._created_by = caller.key
    return state, caller, target


def _sleeping(slot, wait_id: str = "w1"):
    slot._wait_state = {"wait_id": wait_id, "seconds": 300, "deadline_ts": 9e12}
    return slot


def _end(state, caller, target: str = "chat-2") -> dict:
    return asyncio.run(
        sc.end_wait_target(state, caller_session_key=slot_history_key(caller), target=target)
    )


class TestEndWaitTarget:
    def test_parks_the_in_flight_wait_and_names_the_caller(self, tmp_path):
        state, caller, target = _pair(tmp_path)
        _sleeping(target, "w-live")

        result = _end(state, caller)

        assert result == {"ok": True, "target": "chat-2", "ended": True, "wait_id": "w-live"}
        # The same slot field the End-wait button writes, so the tool's next
        # keepalive ping collects it exactly as it collects a click.
        assert target._end_wait_request == "w-live"
        assert target._end_wait_by == caller.key
        # Nothing about the turn itself is touched: this is not a stop.
        assert target._wait_state is not None

    def test_a_target_that_is_not_waiting_gets_info_not_an_error(self, tmp_path):
        state, caller, target = _pair(tmp_path)

        result = _end(state, caller)

        assert result["ok"] is True
        assert result["ended"] is False
        assert "not sleeping" in result["info"]
        assert target._end_wait_request is None
        assert target._end_wait_by == ""

    def test_a_contested_wait_is_not_aimed_at(self, tmp_path):
        """Two sleeps on one session key: neither can be told apart, so neither
        is ended (the button is hidden for the same reason)."""
        state, caller, target = _pair(tmp_path)
        target._wait_contested = True

        result = _end(state, caller)

        assert result["ended"] is False
        assert "two waits" in result["info"]
        assert target._end_wait_request is None

    @pytest.mark.parametrize(
        ("setup", "expected"),
        [("sleeping", "requested"), ("idle", "not_waiting"), ("contested", "contested")],
    )
    def test_the_audit_names_why_nothing_was_ended(self, tmp_path, setup, expected):
        """The SEL detail keeps a contested refusal apart from an idle target."""
        state, caller, target = _pair(tmp_path)
        if setup == "sleeping":
            _sleeping(target)
        elif setup == "contested":
            _sleeping(target)
            target._wait_contested = True

        with patch.object(sc, "_audit") as audit:
            _end(state, caller)

        assert audit.call_args.kwargs["detail"] == {"result": expected}

    def test_a_session_the_caller_did_not_create_is_refused(self, tmp_path):
        """Narrower than session_stop: an owner caller is not creator-fenced by
        authorize_target, and this verb still refuses a peer it did not open."""
        state, caller, target = _pair(tmp_path, created=False)
        _sleeping(target)

        with pytest.raises(sc.SessionControlError) as exc:
            _end(state, caller)

        assert exc.value.code == "not_creator"
        assert target._end_wait_request is None

    def test_the_shared_gate_still_applies(self, tmp_path):
        state, caller, target = _pair(tmp_path)
        target.memory_mode = "incognito"
        _sleeping(target)

        with pytest.raises(sc.SessionControlError) as exc:
            _end(state, caller)

        assert exc.value.code == "ephemeral_target"
        assert target._end_wait_request is None


class TestTheKeepaliveNamesWhoEndedIt:
    def _ping(self, state, slot, wait_id: str) -> dict:
        reply: dict = {"ok": True}
        body = {"wait_id": wait_id, "seconds": 300, "remaining": 290}
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value=slot.key):
            _service_wait_ping(state, "known", wait_id, body, reply, None)
        return reply

    def _state(self, slot):
        state = MagicMock()
        state.get_slot = MagicMock(side_effect=lambda n: slot if n == slot.key else None)
        return state

    def test_a_session_request_carries_its_requester(self, tmp_path):
        _, caller, target = _pair(tmp_path)
        state = self._state(target)
        self._ping(state, target, "w1")  # mints the wait state
        target._end_wait_request = "w1"
        target._end_wait_by = caller.key

        reply = self._ping(state, target, "w1")

        assert reply["end_wait"] == "w1"
        assert reply["end_wait_by"] == caller.key

    def test_a_button_request_reports_the_user(self, tmp_path):
        _, _, target = _pair(tmp_path)
        state = self._state(target)
        self._ping(state, target, "w1")
        target._end_wait_request = "w1"

        reply = self._ping(state, target, "w1")

        assert reply["end_wait"] == "w1"
        assert "end_wait_by" not in reply


class TestTheRoute:
    def _request(self, state, caller, *, internal: bool = True):
        request = MagicMock()
        request.app = {"state": state}
        request.path = "/api/session-control/end-wait"
        request.method = "POST"
        request.headers = {"X-Session-Key": slot_history_key(caller)}
        request.query = {}
        request.get = lambda key, default=None: (
            True if (key in ("internal_auth", "peer_verified") and internal) else default
        )

        async def _json():
            return {"target": "chat-2"}

        request.json = _json
        return request

    def test_without_the_secret_is_forbidden(self, tmp_path):
        state, caller, target = _pair(tmp_path)
        _sleeping(target)
        resp = asyncio.run(
            handlers_sc.api_session_control_end_wait(self._request(state, caller, internal=False))
        )
        assert resp.status == 403
        assert target._end_wait_request is None

    def test_the_route_reaches_the_verb(self, tmp_path):
        state, caller, target = _pair(tmp_path)
        _sleeping(target)
        resp = asyncio.run(handlers_sc.api_session_control_end_wait(self._request(state, caller)))
        assert resp.status == 200
        assert json.loads(resp.body.decode())["ended"] is True
        assert target._end_wait_request == "w1"

    def test_the_route_is_strict_internal(self):
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

        assert "/api/session-control/end-wait" in _STRICT_INTERNAL_API_PATHS


class TestMcpDispatch:
    def _call(self, reply: dict):
        from kiro_crew.mcp_dashboard import TABLE
        from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
        from kiro_crew.mcp_tools.table import Caller, ToolContext

        dash = InMemoryDashboardClient({"POST /api/session-control/end-wait": reply})
        ctx = ToolContext(dash, Caller.strict("dashboard:chat-1"))
        out = TABLE.call("session_end_wait", {"target": "chat-2"}, ctx)
        return out, dash.requests

    def test_posts_the_target_to_the_route(self):
        out, (post,) = self._call({"ok": True, "target": "chat-2", "ended": True, "wait_id": "w"})
        assert (post.path, post.body) == ("/api/session-control/end-wait", {"target": "chat-2"})
        assert post.session_key == "dashboard:chat-1"
        assert "End-wait sent to `chat-2`" in out

    def test_a_target_not_waiting_reads_as_nothing_to_end(self):
        out, _ = self._call(
            {
                "ok": True,
                "target": "chat-2",
                "ended": False,
                "info": "not sleeping in the wait tool",
            }
        )
        assert out.startswith("\u2139")
        assert "Nothing to end" in out
        assert not out.startswith("Error")

    def test_a_refusal_is_an_error(self):
        out, _ = self._call({"error": "session_end_wait reaches only sessions you created"})
        assert out.startswith("Error: could not end that session's wait")
