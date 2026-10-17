"""``chat_session_pin`` in ``mcp_dashboard``.

Covers the tool through its table row: schema validation (a real boolean, a
required session), session resolution through the scoped slot list, the
idempotent no-op, the strict identity gate, and the PATCH it sends. Each case
is one tools/call frame against an in-memory dashboard; the endpoint's own
fences are tested by ``test_chat_slot_pin_ownership.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import mcp_dashboard
from kiro_crew.mcp_dashboard import TABLE, _list_tools
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_SLOTS = [
    {"key": "chat-1-100", "title": "Caller", "pinned": False},
    {"key": "chat-2-200", "title": "Pinned one", "pinned": True},
    {"key": "chat-3-300", "title": "Scratch", "pinned": False},
]

#: Pinning another session resolves identity strictly, like tagging one.
_CALLER = Caller.strict("dashboard:chat-1-100")


def _routes(slots: Any = None, reply: Any = None) -> dict[str, Any]:
    return {
        "GET /api/chat/slots": _SLOTS if slots is None else slots,
        "PATCH /api/chat/slots/{slot}/pin": {"ok": True} if reply is None else reply,
    }


def _pin(
    args: dict[str, Any], routes: dict[str, Any] | None = None, caller: Caller = _CALLER
) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient(_routes() if routes is None else routes)
    return TABLE.call("chat_session_pin", args, ToolContext(dash, caller)), dash


def _patches(dash: InMemoryDashboardClient) -> list[DashboardRequest]:
    return dash.sent("PATCH /api/chat/slots/{slot}/pin")


def test_the_tool_is_advertised_with_both_arguments_required() -> None:
    tool = next(t for t in _list_tools() if t["name"] == "chat_session_pin")
    schema = tool["inputSchema"]
    assert set(schema["required"]) == {"session", "pinned"}
    assert schema["properties"]["pinned"]["type"] == "boolean"


class TestPin:
    def test_pins_an_unpinned_session_with_the_verified_caller(self) -> None:
        out, dash = _pin(
            {"session": "chat-3-300", "pinned": True}, _routes(reply={"ok": True, "pinned": True})
        )
        (patch,) = _patches(dash)
        assert patch.path == "/api/chat/slots/chat-3-300/pin"
        assert patch.body == {"pinned": True}
        assert patch.session_key == "dashboard:chat-1-100"
        assert out == "Pinned session `chat-3-300`."

    def test_unpins_a_pinned_session(self) -> None:
        out, dash = _pin(
            {"session": "Pinned one", "pinned": False},
            _routes(reply={"ok": True, "pinned": False}),
        )
        (patch,) = _patches(dash)
        assert (patch.path, patch.body) == ("/api/chat/slots/chat-2-200/pin", {"pinned": False})
        assert out == "Unpinned session `chat-2-200`."

    @pytest.mark.parametrize(
        "ref,pinned,word",
        [("chat-2-200", True, "already pinned"), ("chat-3-300", False, "already not pinned")],
    )
    def test_the_route_reports_no_change(self, ref, pinned, word) -> None:
        out, dash = _pin(
            {"session": ref, "pinned": pinned},
            _routes(reply={"ok": True, "pinned": pinned, "changed": False}),
        )
        assert len(_patches(dash)) == 1
        assert out.startswith("No change") and word in out

    def test_a_stale_row_does_not_skip_the_write(self) -> None:
        # The list says chat-2-200 is pinned, but it may have been unpinned
        # since: the tool must still send the PATCH and let the route decide.
        out, dash = _pin(
            {"session": "chat-2-200", "pinned": True},
            _routes(reply={"ok": True, "pinned": True, "changed": True}),
        )
        assert len(_patches(dash)) == 1
        assert out == "Pinned session `chat-2-200`."

    def test_accepts_a_dashboard_session_key(self) -> None:
        _, dash = _pin({"session": "dashboard:chat-3-300", "pinned": True})
        assert _patches(dash)[-1].path == "/api/chat/slots/chat-3-300/pin"

    def test_slot_key_is_url_quoted(self) -> None:
        odd = [dict(s) for s in _SLOTS] + [{"key": "a b/c", "title": "Odd"}]
        _, dash = _pin({"session": "a b/c", "pinned": True}, _routes(slots=odd))
        assert _patches(dash)[-1].path == "/api/chat/slots/a%20b%2Fc/pin"

    def test_endpoint_errors_are_surfaced(self) -> None:
        out, _ = _pin(
            {"session": "chat-3-300", "pinned": True},
            _routes(reply={"error": "not found", "code": "slot_not_found"}),
        )
        assert out == "Error: not found"


class TestRefusals:
    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
    def test_a_non_boolean_pinned_is_refused_by_schema(self, value) -> None:
        out, dash = _pin({"session": "chat-3-300", "pinned": value})
        assert out.startswith("Error: pinned:")
        assert dash.requests == []

    def test_both_arguments_are_required(self) -> None:
        assert _pin({"session": "chat-3-300"})[0] == "Error: pinned: required"
        assert _pin({"pinned": True})[0] == "Error: session: required"

    def test_an_unknown_session_never_reaches_the_endpoint(self) -> None:
        out, dash = _pin({"session": "Nope", "pinned": True})
        assert out.startswith("Error:") and "no live session" in out and "ARCHIVED" in out
        assert _patches(dash) == []

    def test_an_unverifiable_caller_cannot_pin(self) -> None:
        out, dash = _pin(
            {"session": "chat-3-300", "pinned": True},
            caller=Caller.unverified("dashboard:chat-1-100"),
        )
        assert out.startswith("Error:") and "cannot verify" in out
        assert dash.requests == []

    def test_a_subagent_cannot_pin(self) -> None:
        out, dash = _pin(
            {"session": "chat-3-300", "pinned": True}, caller=Caller.strict("subagent:abc")
        )
        assert out.startswith("Error:") and "runs on behalf of whatever created it" in out
        assert _patches(dash) == []

    def test_an_app_cannot_see_or_pin_a_foreign_session(self) -> None:
        mixed = [
            {"key": "chat-1-100", "title": "Radar run", "app": "issue-radar"},
            {"key": "chat-3-300", "title": "Person's own", "app": ""},
        ]
        out, dash = _pin({"session": "chat-3-300", "pinned": True}, _routes(slots=mixed))
        assert out.startswith("Error:") and "no live session" in out
        assert _patches(dash) == []

    def test_an_app_can_pin_its_own_session(self) -> None:
        own = [
            {"key": "chat-1-100", "title": "Radar run", "app": "issue-radar"},
            {"key": "chat-4-400", "title": "Radar child", "app": "issue-radar"},
        ]
        out, dash = _pin({"session": "chat-4-400", "pinned": True}, _routes(slots=own))
        assert _patches(dash)[-1].path == "/api/chat/slots/chat-4-400/pin"
        assert out == "Pinned session `chat-4-400`."

    def test_a_private_session_is_not_addressable(self) -> None:
        private = [dict(s) for s in _SLOTS] + [
            {"key": "chat-5-500", "title": "Secret", "memory_mode": "incognito"}
        ]
        out, dash = _pin({"session": "chat-5-500", "pinned": True}, _routes(slots=private))
        assert out.startswith("Error:")
        assert _patches(dash) == []


class TestPinCarriesTheResolvedGeneration:
    def test_the_resolved_created_rides_on_the_patch(self) -> None:
        rows = [dict(s) for s in _SLOTS]
        rows[2]["created"] = "2026-09-26T12:00:00+00:00"
        _, dash = _pin({"session": "chat-3-300", "pinned": True}, _routes(slots=rows))
        assert _patches(dash)[-1].body == {
            "pinned": True,
            "expected_created": "2026-09-26T12:00:00+00:00",
        }

    def test_a_row_without_created_sends_no_token(self) -> None:
        _, dash = _pin({"session": "chat-3-300", "pinned": True})
        assert "expected_created" not in _patches(dash)[-1].body

    def test_a_replaced_session_is_reported_as_nothing_written(self) -> None:
        out, _ = _pin(
            {"session": "chat-3-300", "pinned": True},
            _routes(reply={"error": "session was deleted or rebound", "code": "session_gone"}),
        )
        assert out.startswith("Error:") and "Nothing was written" in out


class TestChannelAgentsAreRefusedAtDispatch:
    """An auto-approved call never reaches the permission prompt, so the refusal is here."""

    def test_a_channel_caller_is_refused_before_any_session_is_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sel_obj = MagicMock()
        monkeypatch.setattr(mcp_dashboard, "sel", lambda: sel_obj)
        out, dash = _pin(
            {"session": "chat-3-300", "pinned": True},
            caller=Caller.strict("channel:slack:C1.100"),
        )
        assert out.startswith("Error:") and "channel agents" in out
        assert dash.requests == []
        sel_obj.log_tool_invocation.assert_called_once()
        assert sel_obj.log_tool_invocation.call_args.kwargs["outcome"] == "rejected_blocked_tool"
