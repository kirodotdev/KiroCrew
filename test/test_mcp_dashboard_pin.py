"""``chat_session_pin`` in ``mcp_dashboard``.

Covers dispatch only: schema validation (a real boolean, a required session),
session resolution through the scoped slot list, the idempotent no-op, the
strict identity gate, and the PATCH it sends. The HTTP helpers are patched; the
endpoint's own fences are tested by ``test_chat_slot_pin_ownership.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.mcp_dashboard import _call_tool_inner, _list_tools
from kiro_crew.validation import ValidationError

_SLOTS = [
    {"key": "chat-1-100", "title": "Caller", "pinned": False},
    {"key": "chat-2-200", "title": "Pinned one", "pinned": True},
    {"key": "chat-3-300", "title": "Scratch", "pinned": False},
]


def _rows(path: str) -> list[dict]:
    if path == "/api/chat/slots":
        return [dict(s) for s in _SLOTS]
    raise AssertionError(f"unexpected GET {path}")


@pytest.fixture(autouse=True)
def _verified_caller() -> Any:
    """Pinning another session resolves identity strictly, like tagging one."""
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


def test_the_tool_is_advertised_with_both_arguments_required() -> None:
    tool = next(t for t in _list_tools() if t["name"] == "chat_session_pin")
    schema = tool["inputSchema"]
    assert set(schema["required"]) == {"session", "pinned"}
    assert schema["properties"]["pinned"]["type"] == "boolean"


class TestPin:
    def test_pins_an_unpinned_session_with_the_verified_caller(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch", return_value={"ok": True, "pinned": True}
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": True})
        path, body = mock_patch.call_args.args
        assert path == "/api/chat/slots/chat-3-300/pin"
        assert body == {"pinned": True}
        assert mock_patch.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert out == "Pinned session `chat-3-300`."

    def test_unpins_a_pinned_session(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch", return_value={"ok": True, "pinned": False}
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "Pinned one", "pinned": False})
        assert mock_patch.call_args.args == ("/api/chat/slots/chat-2-200/pin", {"pinned": False})
        assert out == "Unpinned session `chat-2-200`."

    @pytest.mark.parametrize(
        "ref,pinned,word",
        [("chat-2-200", True, "already pinned"), ("chat-3-300", False, "already not pinned")],
    )
    def test_the_route_reports_no_change(self, ref, pinned, word) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"ok": True, "pinned": pinned, "changed": False},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": ref, "pinned": pinned})
        mock_patch.assert_called_once()
        assert out.startswith("No change") and word in out

    def test_a_stale_row_does_not_skip_the_write(self) -> None:
        # The list says chat-2-200 is pinned, but it may have been unpinned
        # since: the tool must still send the PATCH and let the route decide.
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"ok": True, "pinned": True, "changed": True},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-2-200", "pinned": True})
        mock_patch.assert_called_once()
        assert out == "Pinned session `chat-2-200`."

    def test_accepts_a_dashboard_session_key(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            _call_tool_inner(
                "chat_session_pin", {"session": "dashboard:chat-3-300", "pinned": True}
            )
        assert mock_patch.call_args.args[0] == "/api/chat/slots/chat-3-300/pin"

    def test_slot_key_is_url_quoted(self) -> None:
        odd = [dict(s) for s in _SLOTS] + [{"key": "a b/c", "title": "Odd"}]
        with (
            patch("kiro_crew.mcp_dashboard._get", return_value=odd),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            _call_tool_inner("chat_session_pin", {"session": "a b/c", "pinned": True})
        assert mock_patch.call_args.args[0] == "/api/chat/slots/a%20b%2Fc/pin"

    def test_endpoint_errors_are_surfaced(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"error": "not found", "code": "slot_not_found"},
            ),
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": True})
        assert out == "Error: not found"


class TestRefusals:
    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
    def test_a_non_boolean_pinned_is_refused_by_schema(self, value) -> None:
        with patch("kiro_crew.mcp_dashboard._patch") as mock_patch:
            with pytest.raises(ValidationError):
                _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": value})
        mock_patch.assert_not_called()

    def test_both_arguments_are_required(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_session_pin", {"session": "chat-3-300"})
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_session_pin", {"pinned": True})

    def test_an_unknown_session_never_reaches_the_endpoint(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "Nope", "pinned": True})
        assert out.startswith("Error:") and "no live session" in out and "ARCHIVED" in out
        mock_patch.assert_not_called()

    def test_an_unverifiable_caller_cannot_pin(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": True})
        assert out.startswith("Error:") and "cannot verify" in out
        mock_patch.assert_not_called()

    def test_a_subagent_cannot_pin(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="subagent:abc"),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": True})
        assert out.startswith("Error:") and "runs on behalf of whatever created it" in out
        mock_patch.assert_not_called()

    def test_an_app_cannot_see_or_pin_a_foreign_session(self) -> None:
        mixed = [
            {"key": "chat-1-100", "title": "Radar run", "app": "issue-radar"},
            {"key": "chat-3-300", "title": "Person's own", "app": ""},
        ]
        with (
            patch("kiro_crew.mcp_dashboard._get", return_value=mixed),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": True})
        assert out.startswith("Error:") and "no live session" in out
        mock_patch.assert_not_called()

    def test_an_app_can_pin_its_own_session(self) -> None:
        own = [
            {"key": "chat-1-100", "title": "Radar run", "app": "issue-radar"},
            {"key": "chat-4-400", "title": "Radar child", "app": "issue-radar"},
        ]
        with (
            patch("kiro_crew.mcp_dashboard._get", return_value=own),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-4-400", "pinned": True})
        assert mock_patch.call_args.args[0] == "/api/chat/slots/chat-4-400/pin"
        assert out == "Pinned session `chat-4-400`."

    def test_a_private_session_is_not_addressable(self) -> None:
        private = [dict(s) for s in _SLOTS] + [
            {"key": "chat-5-500", "title": "Secret", "memory_mode": "incognito"}
        ]
        with (
            patch("kiro_crew.mcp_dashboard._get", return_value=private),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-5-500", "pinned": True})
        assert out.startswith("Error:")
        mock_patch.assert_not_called()


class TestPinCarriesTheResolvedGeneration:
    def test_the_resolved_created_rides_on_the_patch(self) -> None:
        rows = [dict(s) for s in _SLOTS]
        rows[2]["created"] = "2026-09-26T12:00:00+00:00"
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=lambda p: rows),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": True})
        assert mock_patch.call_args.args[1] == {
            "pinned": True,
            "expected_created": "2026-09-26T12:00:00+00:00",
        }

    def test_a_row_without_created_sends_no_token(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": True})
        assert "expected_created" not in mock_patch.call_args.args[1]

    def test_a_replaced_session_is_reported_as_nothing_written(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"error": "session was deleted or rebound", "code": "session_gone"},
            ),
        ):
            out = _call_tool_inner("chat_session_pin", {"session": "chat-3-300", "pinned": True})
        assert out.startswith("Error:") and "Nothing was written" in out
