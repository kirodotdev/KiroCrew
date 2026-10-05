"""Tests for the board-column tools on the kirocrew-dashboard server.

Covers ``chat_tag_column_list`` / ``chat_tag_column_create`` /
``chat_tag_column_move`` through their table rows: schema validation,
id-or-name resolution of tags and columns, the (name, tag) dedup, the id order
a move sends, the identity and channel gates, and result formatting. Each case
is one tools/call frame against an in-memory dashboard; the endpoints' own
refusals are tested by ``test_chat_tag_column_writers.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.mcp_dashboard import TABLE, _list_tools
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_TAGS = [
    {"id": "aaaaaaaaaaaa", "name": "Active", "color": "#22c55e", "order": 0, "status": True},
    {"id": "bbbbbbbbbbbb", "name": "Blocked", "color": "#ef4444", "order": 1, "status": True},
]

_COLUMNS = [
    {
        "id": "c00000000001",
        "name": "Doing",
        "tag_ids": ["aaaaaaaaaaaa"],
        "mode": "any",
        "order": 0,
        "source": "tags",
    },
    {
        "id": "c00000000002",
        "name": "",
        "tag_ids": [],
        "mode": "any",
        "order": 1,
        "source": "state",
        "state_key": "working",
    },
    {
        "id": "c00000000003",
        "name": "Stuck",
        "tag_ids": ["bbbbbbbbbbbb"],
        "mode": "any",
        "order": 2,
        "source": "tags",
    },
]

_SLOTS = [{"key": "chat-1-100", "title": "Caller", "tags": [], "created": "t"}]

#: Board writes resolve identity strictly, like the tag vocabulary writes.
_CALLER = Caller.strict("dashboard:chat-1-100")


def _routes(columns: Any = None, post: Any = None, put: Any = None) -> dict[str, Any]:
    return {
        "GET /api/chat/tags": _TAGS,
        "GET /api/chat/tag-columns": _COLUMNS if columns is None else columns,
        "GET /api/chat/slots": _SLOTS,
        "POST /api/chat/tag-columns": {"error": "unexpected create"} if post is None else post,
        "PUT /api/chat/tag-columns/order": {"ok": True} if put is None else put,
    }


def _call(
    name: str, args: dict[str, Any], routes: dict[str, Any] | None = None, caller: Caller = _CALLER
) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient(_routes() if routes is None else routes)
    return TABLE.call(name, args, ToolContext(dash, caller)), dash


def _writes(dash: InMemoryDashboardClient) -> list[DashboardRequest]:
    return [r for r in dash.requests if r.method != "GET"]


class TestAdvertised:
    def test_the_three_column_tools_are_listed(self) -> None:
        names = {t["name"] for t in _list_tools()}
        assert {"chat_tag_column_list", "chat_tag_column_create", "chat_tag_column_move"} <= names

    def test_no_delete_or_retag_tool_exists(self) -> None:
        """The board is the person's layout: removing or refiltering a column is theirs."""
        names = {t["name"] for t in _list_tools()}
        assert not {n for n in names if n.startswith("chat_tag_column_")} - {
            "chat_tag_column_list",
            "chat_tag_column_create",
            "chat_tag_column_move",
        }


class TestColumnList:
    def test_renders_columns_in_board_order_with_what_each_shows(self) -> None:
        out, _ = _call("chat_tag_column_list", {})
        assert "3 columns" in out
        assert out.index("Doing") < out.index("live state `working`") < out.index("Stuck")
        assert "id=c00000000001" in out and "tags `Active`" in out
        assert "(unnamed)" in out

    def test_untagged_flag_is_labelled_as_what_it_adds(self) -> None:
        """Mirrors the board UI's ``columnMatches``: empty filter = all sessions."""
        cols = [
            {"id": "c1", "name": "A", "tag_ids": [], "include_untagged": True},
            {"id": "c2", "name": "B", "tag_ids": ["aaaaaaaaaaaa"], "include_untagged": True},
        ]
        out, _ = _call("chat_tag_column_list", {}, _routes(columns=cols))
        assert "`A`  id=c1  shows all sessions" in out
        assert "tags `Active` (match any) + untagged sessions" in out

    def test_a_hand_edited_non_list_tag_ids_renders_instead_of_crashing(self) -> None:
        cols = [{"id": "c1", "name": "Odd", "tag_ids": 7, "source": "tags"}]
        out, _ = _call("chat_tag_column_list", {}, _routes(columns=cols))
        assert "`Odd`  id=c1  shows all sessions" in out

    def test_empty_board_points_at_create(self) -> None:
        out, _ = _call("chat_tag_column_list", {}, _routes(columns=[]))
        assert "chat_tag_column_create" in out

    def test_endpoint_error_is_not_reported_as_empty(self) -> None:
        out, _ = _call("chat_tag_column_list", {}, {"GET /api/{route}": {"error": "boom"}})
        assert out.startswith("Error:") and "boom" in out


class TestColumnCreate:
    def test_posts_one_tag_filter_under_the_verified_key(self) -> None:
        out, dash = _call(
            "chat_tag_column_create",
            {"name": "Review", "tag": "blocked"},
            _routes(post={"id": "c00000000004", "name": "Review"}),
        )
        (post,) = _writes(dash)
        assert post.path == "/api/chat/tag-columns"
        assert post.body == {
            "name": "Review",
            "tag_ids": ["bbbbbbbbbbbb"],
            "mode": "any",
            "ensure": True,
        }
        assert post.session_key == "dashboard:chat-1-100"
        assert "c00000000004" in out and "`Blocked`" in out

    def test_an_existing_twin_the_endpoint_returns_is_reported_as_existing(self) -> None:
        """The endpoint's ``ensure`` answers with the existing column; say so."""
        out, _ = _call(
            "chat_tag_column_create",
            {"name": "doing", "tag": "aaaaaaaaaaaa"},
            _routes(post={"id": "c00000000001", "name": "Doing"}),
        )
        assert "already shows" in out and "c00000000001" in out

    def test_same_tag_under_a_new_name_is_a_new_column(self) -> None:
        _, dash = _call(
            "chat_tag_column_create",
            {"name": "Now", "tag": "Active"},
            _routes(post={"id": "c00000000004", "name": "Now"}),
        )
        assert len(_writes(dash)) == 1

    def test_unknown_tag_is_refused_before_any_write(self) -> None:
        out, dash = _call("chat_tag_column_create", {"name": "X", "tag": "Nope"})
        assert _writes(dash) == []
        assert out.startswith("Error:") and "chat_tag_list" in out

    def test_name_and_tag_are_required(self) -> None:
        assert _call("chat_tag_column_create", {"name": "X"})[0] == "Error: tag: required"
        assert _call("chat_tag_column_create", {"tag": "Active"})[0] == "Error: name: required"

    def test_blank_name_is_refused(self) -> None:
        out, dash = _call("chat_tag_column_create", {"name": "   ", "tag": "Active"})
        assert out == "Error: name: required (empty after sanitization)"
        assert dash.requests == []

    def test_the_endpoint_refusal_for_an_app_is_explained(self) -> None:
        out, _ = _call(
            "chat_tag_column_create",
            {"name": "X", "tag": "Active"},
            _routes(post={"error": "apps cannot write shared tags", "code": "app_forbidden"}),
        )
        assert out.startswith("Error:") and "chat_tag_column_list" in out

    def test_an_unverifiable_caller_is_refused(self) -> None:
        out, dash = _call(
            "chat_tag_column_create", {"name": "X", "tag": "Active"}, caller=Caller.unverified()
        )
        assert _writes(dash) == []
        assert out.startswith("Error:")


class TestColumnMove:
    def _move(self, args: dict[str, Any], **routes: Any) -> tuple[str, list[DashboardRequest]]:
        out, dash = _call("chat_tag_column_move", args, _routes(**routes))
        return out, dash.sent("PUT /api/chat/tag-columns/order")

    def test_before_places_the_column_ahead_of_the_anchor(self) -> None:
        out, (put,) = self._move({"column": "Stuck", "before": "Doing"})
        assert put.path == "/api/chat/tag-columns/order"
        assert put.body == {
            "ids": ["c00000000003", "c00000000001", "c00000000002"],
            "base_ids": ["c00000000001", "c00000000002", "c00000000003"],
        }
        assert put.session_key == "dashboard:chat-1-100"
        assert "Moved column `Stuck` before `Doing`" in out

    def test_after_keeps_every_other_column_in_order(self) -> None:
        _out, (put,) = self._move({"column": "c00000000001", "after": "c00000000002"})
        assert put.body["ids"] == ["c00000000002", "c00000000001", "c00000000003"]

    def test_a_board_changed_meanwhile_is_reported_not_overwritten(self) -> None:
        out, _ = self._move(
            {"column": "Stuck", "before": "Doing"}, put={"error": "changed", "code": "stale_base"}
        )
        assert out.startswith("Error:") and "Nothing was written" in out

    def test_already_in_place_writes_nothing(self) -> None:
        out, puts = self._move({"column": "Doing", "before": "c00000000002"})
        assert puts == []
        assert out.startswith("No change")

    def test_exactly_one_of_before_or_after(self) -> None:
        for args in ({"column": "Doing"}, {"column": "Doing", "before": "Stuck", "after": "Stuck"}):
            out, puts = self._move(args)
            assert puts == []
            assert out.startswith("Error:") and "exactly one" in out

    def test_an_empty_side_counts_as_not_given(self) -> None:
        out, puts = self._move({"column": "Stuck", "before": ""})
        assert puts == []
        assert "exactly one" in out
        out, (put,) = self._move({"column": "Stuck", "before": "", "after": "Doing"})
        assert put.body["ids"] == ["c00000000001", "c00000000003", "c00000000002"]

    def test_next_to_itself_is_refused(self) -> None:
        out, puts = self._move({"column": "Doing", "after": "c00000000001"})
        assert puts == []
        assert out.startswith("Error:")

    def test_unknown_column_is_refused(self) -> None:
        out, puts = self._move({"column": "Nope", "after": "Doing"})
        assert puts == []
        assert out.startswith("Error:") and "chat_tag_column_list" in out

    def test_a_shared_name_is_refused_rather_than_guessed(self) -> None:
        dup = [dict(c) for c in _COLUMNS] + [
            {"id": "c00000000009", "name": "Stuck", "tag_ids": [], "order": 3, "source": "tags"}
        ]
        out, puts = self._move({"column": "Stuck", "before": "Doing"}, columns=dup)
        assert puts == []
        assert "share the name" in out


class TestChannelContainment:
    """The name blocklist covers the prompt; dispatch refuses auto-approved calls."""

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("chat_tag_column_create", {"name": "X", "tag": "Active"}),
            ("chat_tag_column_move", {"column": "Stuck", "before": "Doing"}),
        ],
    )
    def test_a_channel_caller_cannot_write_the_board(self, tool: str, args: dict) -> None:
        """The channel caller is verified and passes the tree-shaping gate (it
        names no slot and no app); the board refusal is what stops it."""
        out, dash = _call(tool, args, caller=Caller.strict("channel:slack:C1:1.0"))
        assert _writes(dash) == []
        assert "not available to channel agents" in out

    def test_the_writes_are_on_the_channel_blocklist_and_the_read_is_not(self) -> None:
        from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS

        assert "chat_tag_column_create" in CHANNEL_AGENT_BLOCKED_TOOLS
        assert "chat_tag_column_move" in CHANNEL_AGENT_BLOCKED_TOOLS
        assert "chat_tag_column_list" not in CHANNEL_AGENT_BLOCKED_TOOLS
