"""Tests for the board-column tools on the kirocrew-dashboard server.

Covers dispatch for ``chat_tag_column_list`` / ``chat_tag_column_create`` /
``chat_tag_column_move``: schema validation, id-or-name resolution of tags and
columns, the (name, tag) dedup, the id order a move sends, the identity and
channel gates, and result formatting. The HTTP helpers are patched; the
endpoints' own refusals are tested by ``test_chat_tag_column_writers.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.mcp_dashboard import _call_tool_inner, _list_tools
from kiro_crew.validation import ValidationError

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


def _rows(path: str) -> list[dict]:
    if path == "/api/chat/tags":
        return [dict(t) for t in _TAGS]
    if path == "/api/chat/tag-columns":
        return [dict(c) for c in _COLUMNS]
    if path == "/api/chat/slots":
        return [dict(s) for s in _SLOTS]
    raise AssertionError(f"unexpected GET {path}")


@pytest.fixture(autouse=True)
def _verified_caller() -> Any:
    """Board writes resolve identity strictly, like the tag vocabulary writes."""
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


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
        with patch("kiro_crew.mcp_dashboard._get", side_effect=_rows):
            out = _call_tool_inner("chat_tag_column_list", {})
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

        def rows(path: str) -> list[dict]:
            return cols if path == "/api/chat/tag-columns" else _rows(path)

        with patch("kiro_crew.mcp_dashboard._get", side_effect=rows):
            out = _call_tool_inner("chat_tag_column_list", {})
        assert "`A`  id=c1  shows all sessions" in out
        assert "tags `Active` (match any) + untagged sessions" in out

    def test_a_hand_edited_non_list_tag_ids_renders_instead_of_crashing(self) -> None:
        cols = [{"id": "c1", "name": "Odd", "tag_ids": 7, "source": "tags"}]

        def rows(path: str) -> list[dict]:
            return cols if path == "/api/chat/tag-columns" else _rows(path)

        with patch("kiro_crew.mcp_dashboard._get", side_effect=rows):
            out = _call_tool_inner("chat_tag_column_list", {})
        assert "`Odd`  id=c1  shows all sessions" in out

    def test_empty_board_points_at_create(self) -> None:
        def rows(path: str) -> list[dict]:
            return [] if path == "/api/chat/tag-columns" else _rows(path)

        with patch("kiro_crew.mcp_dashboard._get", side_effect=rows):
            out = _call_tool_inner("chat_tag_column_list", {})
        assert "chat_tag_column_create" in out

    def test_endpoint_error_is_not_reported_as_empty(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", return_value={"error": "boom"}):
            out = _call_tool_inner("chat_tag_column_list", {})
        assert out.startswith("Error:") and "boom" in out


class TestColumnCreate:
    def test_posts_one_tag_filter_under_the_verified_key(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={"id": "c00000000004", "name": "Review"},
            ) as mock_post,
        ):
            out = _call_tool_inner("chat_tag_column_create", {"name": "Review", "tag": "blocked"})
        path, body = mock_post.call_args.args
        assert path == "/api/chat/tag-columns"
        assert body == {
            "name": "Review",
            "tag_ids": ["bbbbbbbbbbbb"],
            "mode": "any",
            "ensure": True,
        }
        assert mock_post.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert "c00000000004" in out and "`Blocked`" in out

    def test_an_existing_twin_the_endpoint_returns_is_reported_as_existing(self) -> None:
        """The endpoint's ``ensure`` answers with the existing column; say so."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={"id": "c00000000001", "name": "Doing"},
            ),
        ):
            out = _call_tool_inner(
                "chat_tag_column_create", {"name": "doing", "tag": "aaaaaaaaaaaa"}
            )
        assert "already shows" in out and "c00000000001" in out

    def test_same_tag_under_a_new_name_is_a_new_column(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._post", return_value={"id": "c00000000004", "name": "Now"}
            ) as mock_post,
        ):
            _call_tool_inner("chat_tag_column_create", {"name": "Now", "tag": "Active"})
        mock_post.assert_called_once()

    def test_unknown_tag_is_refused_before_any_write(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_tag_column_create", {"name": "X", "tag": "Nope"})
        mock_post.assert_not_called()
        assert out.startswith("Error:") and "chat_tag_list" in out

    def test_name_and_tag_are_required(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_tag_column_create", {"name": "X"})
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_tag_column_create", {"tag": "Active"})

    def test_blank_name_is_refused(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
            pytest.raises(ValidationError),
        ):
            _call_tool_inner("chat_tag_column_create", {"name": "   ", "tag": "Active"})
        mock_post.assert_not_called()

    def test_the_endpoint_refusal_for_an_app_is_explained(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={"error": "apps cannot write shared tags", "code": "app_forbidden"},
            ),
        ):
            out = _call_tool_inner("chat_tag_column_create", {"name": "X", "tag": "Active"})
        assert out.startswith("Error:") and "chat_tag_column_list" in out

    def test_an_unverifiable_caller_is_refused(self) -> None:
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=None),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_tag_column_create", {"name": "X", "tag": "Active"})
        mock_post.assert_not_called()
        assert out.startswith("Error:")


class TestColumnMove:
    def _move(self, args: dict[str, Any]) -> tuple[str, Any]:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._put", return_value={"ok": True}) as mock_put,
        ):
            out = _call_tool_inner("chat_tag_column_move", args)
        return out, mock_put

    def test_before_places_the_column_ahead_of_the_anchor(self) -> None:
        out, mock_put = self._move({"column": "Stuck", "before": "Doing"})
        path, body = mock_put.call_args.args
        assert path == "/api/chat/tag-columns/order"
        assert body == {
            "ids": ["c00000000003", "c00000000001", "c00000000002"],
            "base_ids": ["c00000000001", "c00000000002", "c00000000003"],
        }
        assert mock_put.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert "Moved column `Stuck` before `Doing`" in out

    def test_after_keeps_every_other_column_in_order(self) -> None:
        _out, mock_put = self._move({"column": "c00000000001", "after": "c00000000002"})
        assert mock_put.call_args.args[1]["ids"] == [
            "c00000000002",
            "c00000000001",
            "c00000000003",
        ]

    def test_a_board_changed_meanwhile_is_reported_not_overwritten(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._put",
                return_value={"error": "changed", "code": "stale_base"},
            ),
        ):
            out = _call_tool_inner("chat_tag_column_move", {"column": "Stuck", "before": "Doing"})
        assert out.startswith("Error:") and "Nothing was written" in out

    def test_already_in_place_writes_nothing(self) -> None:
        out, mock_put = self._move({"column": "Doing", "before": "c00000000002"})
        mock_put.assert_not_called()
        assert out.startswith("No change")

    def test_exactly_one_of_before_or_after(self) -> None:
        for args in ({"column": "Doing"}, {"column": "Doing", "before": "Stuck", "after": "Stuck"}):
            out, mock_put = self._move(args)
            mock_put.assert_not_called()
            assert out.startswith("Error:") and "exactly one" in out

    def test_an_empty_side_counts_as_not_given(self) -> None:
        out, mock_put = self._move({"column": "Stuck", "before": ""})
        mock_put.assert_not_called()
        assert "exactly one" in out
        out, mock_put = self._move({"column": "Stuck", "before": "", "after": "Doing"})
        assert mock_put.call_args.args[1]["ids"] == [
            "c00000000001",
            "c00000000003",
            "c00000000002",
        ]

    def test_next_to_itself_is_refused(self) -> None:
        out, mock_put = self._move({"column": "Doing", "after": "c00000000001"})
        mock_put.assert_not_called()
        assert out.startswith("Error:")

    def test_unknown_column_is_refused(self) -> None:
        out, mock_put = self._move({"column": "Nope", "after": "Doing"})
        mock_put.assert_not_called()
        assert out.startswith("Error:") and "chat_tag_column_list" in out

    def test_a_shared_name_is_refused_rather_than_guessed(self) -> None:
        dup = [dict(c) for c in _COLUMNS] + [
            {"id": "c00000000009", "name": "Stuck", "tag_ids": [], "order": 3, "source": "tags"}
        ]

        def rows(path: str) -> list[dict]:
            return dup if path == "/api/chat/tag-columns" else _rows(path)

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=rows),
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner("chat_tag_column_move", {"column": "Stuck", "before": "Doing"})
        mock_put.assert_not_called()
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
        with (
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="channel:slack:C1:1.0",
            ),
            patch(
                "kiro_crew.mcp_dashboard._refuse_tree_shaping_if_unverifiable",
                return_value=("channel:slack:C1:1.0", "", None),
            ),
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner(tool, args)
        mock_post.assert_not_called()
        mock_put.assert_not_called()
        assert "not available to channel agents" in out

    def test_the_writes_are_on_the_channel_blocklist_and_the_read_is_not(self) -> None:
        from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS

        assert "chat_tag_column_create" in CHANNEL_AGENT_BLOCKED_TOOLS
        assert "chat_tag_column_move" in CHANNEL_AGENT_BLOCKED_TOOLS
        assert "chat_tag_column_list" not in CHANNEL_AGENT_BLOCKED_TOOLS
