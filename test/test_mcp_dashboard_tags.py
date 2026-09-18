"""Tests for the session tag tools on the kirocrew-dashboard server.

Covers dispatch for ``chat_tag_list`` / ``chat_tag_create`` / ``chat_tag_assign``
— schema validation, id-or-name resolution, the add/remove delta composed onto
the slot's current list, the compare-and-set revision the PUT carries, the
identity gates, and result formatting. The HTTP helpers are patched; the
endpoints themselves are tested by ``test_chat_tags.py``.
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
    {"id": "cccccccccccc", "name": "kirocrew", "color": "#6b7280", "order": 2, "status": False},
]

_SLOTS = [
    {"key": "chat-1-100", "title": "Caller", "tags": [], "tags_revision": "r1", "created": "t"},
    {
        "key": "chat-2-200",
        "title": "Tag MCP",
        "tags": ["cccccccccccc", "aaaaaaaaaaaa"],
        "tags_revision": "r2",
    },
    {"key": "chat-3-300", "title": "Scratch", "tags": [], "tags_revision": "r3"},
]


def _rows(path: str) -> list[dict]:
    if path == "/api/chat/tags":
        return [dict(t) for t in _TAGS]
    if path == "/api/chat/slots":
        return [dict(s) for s in _SLOTS]
    raise AssertionError(f"unexpected GET {path}")


@pytest.fixture(autouse=True)
def _verified_caller() -> Any:
    """Tagging another session resolves identity strictly, like filing one."""
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


class TestAdvertised:
    def test_the_three_tag_tools_are_listed(self) -> None:
        names = {t["name"] for t in _list_tools()}
        assert {"chat_tag_list", "chat_tag_create", "chat_tag_update", "chat_tag_assign"} <= names


class TestTagList:
    def test_renders_every_tag_with_id_color_and_status(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", side_effect=_rows):
            out = _call_tool_inner("chat_tag_list", {})
        assert "3 tags" in out
        assert "`Active`" in out and "id=aaaaaaaaaaaa" in out and "#22c55e" in out
        assert out.count("[status]") == 2
        # Stored order, not alphabetical: what the sidebar shows.
        assert out.index("Active") < out.index("Blocked") < out.index("kirocrew")

    def test_empty_vocabulary_points_at_create(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", return_value=[]):
            out = _call_tool_inner("chat_tag_list", {})
        assert "chat_tag_create" in out

    def test_a_malformed_persisted_order_sorts_as_zero_instead_of_crashing(self) -> None:
        """``tags.json`` is loaded verbatim, so a hand-edited row must still render."""
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "Later", "order": 1},
            {"id": "bbbbbbbbbbbb", "name": "Broken", "order": "invalid"},
            {"id": "cccccccccccc", "name": "Missing"},
        ]
        with patch("kiro_crew.mcp_dashboard._get", return_value=rows):
            out = _call_tool_inner("chat_tag_list", {})
        assert "3 tags" in out
        assert out.index("Broken") < out.index("Later") and out.index("Missing") < out.index(
            "Later"
        )

    def test_endpoint_error_is_not_reported_as_empty(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", return_value={"error": "boom"}):
            out = _call_tool_inner("chat_tag_list", {})
        assert out.startswith("Error:") and "boom" in out


class TestTagCreate:
    def test_posts_name_color_and_status(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={
                    "id": "dddddddddddd",
                    "name": "Review",
                    "color": "#3b82f6",
                    "status": True,
                },
            ) as mock_post,
        ):
            out = _call_tool_inner(
                "chat_tag_create", {"name": "Review", "color": "#3b82f6", "status": True}
            )
        path, body = mock_post.call_args.args
        assert path == "/api/chat/tags"
        assert body == {"name": "Review", "status": True, "color": "#3b82f6"}
        assert mock_post.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert "Review" in out and "dddddddddddd" in out and "status tag" in out

    def test_color_is_omitted_when_not_given(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={"id": "dddddddddddd", "name": "Review", "color": "#6b7280"},
            ) as mock_post,
        ):
            _call_tool_inner("chat_tag_create", {"name": "Review"})
        assert "color" not in mock_post.call_args.args[1]

    def test_malformed_color_is_refused_by_schema(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_tag_create", {"name": "Review", "color": "red"})

    def test_name_is_required(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_tag_create", {})

    def test_an_existing_name_is_reported_not_duplicated(self) -> None:
        """The endpoint dedups on the lowered name; the tool reports what exists."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", return_value=dict(_TAGS[0])),
        ):
            out = _call_tool_inner("chat_tag_create", {"name": "active"})
        assert "Active" in out and "aaaaaaaaaaaa" in out

    def test_an_unverifiable_caller_cannot_create(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_tag_create", {"name": "Review"})
        assert out.startswith("Error:") and "cannot verify" in out
        mock_post.assert_not_called()

    def test_an_apps_create_reaches_the_endpoint_and_its_refusal_is_explained(self) -> None:
        """The app rule is the endpoint's (``api_chat_tag_create``), not a second copy here."""

        def _mixed(path: str) -> list[dict]:
            if path == "/api/chat/tags":
                return [dict(t) for t in _TAGS]
            return [{"key": "chat-1-100", "title": "Radar", "app": "issue-radar"}]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_mixed),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={"error": "apps cannot create shared tags", "code": "app_forbidden"},
            ) as mock_post,
        ):
            out = _call_tool_inner("chat_tag_create", {"name": "Review"})
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert out.startswith("Error:") and "shared vocabulary" in out

    def test_a_credential_in_the_name_is_redacted_before_the_write(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={"id": "dddddddddddd", "name": "x"},
            ) as mock_post,
        ):
            _call_tool_inner("chat_tag_create", {"name": "key AKIAIOSFODNN7EXAMPLE"})
        assert "AKIAIOSFODNN7EXAMPLE" not in mock_post.call_args.args[1]["name"]

    def test_the_schema_refuses_an_overlong_name(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_tag_create", {"name": "x" * 61})


class TestTagUpdate:
    def test_patches_by_name_with_only_the_named_fields(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={
                    "id": "bbbbbbbbbbbb",
                    "name": "Waiting",
                    "color": "#ef4444",
                    "status": True,
                },
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_tag_update", {"tag": "blocked", "name": "Waiting"})
        path, body = mock_patch.call_args.args
        assert path == "/api/chat/tags/bbbbbbbbbbbb"
        assert body == {"name": "Waiting"}
        assert mock_patch.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert "renamed `Blocked` → `Waiting`" in out

    def test_patches_color_and_status_by_id(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={
                    "id": "cccccccccccc",
                    "name": "kirocrew",
                    "color": "#3b82f6",
                    "status": True,
                },
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_tag_update", {"tag": "cccccccccccc", "color": "#3b82f6", "status": True}
            )
        assert mock_patch.call_args.args[1] == {"color": "#3b82f6", "status": True}
        assert "color #6b7280 → #3b82f6" in out and "status tag: yes" in out

    def test_at_least_one_field_is_required(self) -> None:
        with patch("kiro_crew.mcp_dashboard._patch") as mock_patch:
            out = _call_tool_inner("chat_tag_update", {"tag": "Blocked"})
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_unknown_tag_never_reaches_the_endpoint(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_tag_update", {"tag": "Nope", "name": "X"})
        assert out.startswith("Error:") and "no tag matches" in out
        mock_patch.assert_not_called()

    def test_malformed_color_is_refused_by_schema(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_tag_update", {"tag": "Blocked", "color": "red"})

    def test_a_credential_in_the_new_name_is_redacted_before_the_write(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch", return_value={"id": "bbbbbbbbbbbb", "name": "x"}
            ) as mock_patch,
        ):
            _call_tool_inner(
                "chat_tag_update", {"tag": "Blocked", "name": "key AKIAIOSFODNN7EXAMPLE"}
            )
        assert "AKIAIOSFODNN7EXAMPLE" not in mock_patch.call_args.args[1]["name"]

    def test_an_unverifiable_caller_cannot_update(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_tag_update", {"tag": "Blocked", "name": "X"})
        assert out.startswith("Error:") and "cannot verify" in out
        mock_patch.assert_not_called()

    def test_the_endpoints_app_refusal_is_explained(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"error": "apps cannot write shared tags", "code": "app_forbidden"},
            ),
        ):
            out = _call_tool_inner("chat_tag_update", {"tag": "Blocked", "name": "X"})
        assert out.startswith("Error:") and "shared vocabulary" in out


class TestTagAssign:
    def test_adds_by_name_and_removes_by_id_as_a_delta_with_the_base_revision(self) -> None:
        """Tags not named are kept; the PUT names the revision the delta was composed on."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._put",
                return_value={
                    "ok": True,
                    "tags": ["cccccccccccc", "bbbbbbbbbbbb"],
                    "tags_revision": "r9",
                },
            ) as mock_put,
        ):
            out = _call_tool_inner(
                "chat_tag_assign",
                {"session": "chat-2-200", "add": ["blocked"], "remove": ["aaaaaaaaaaaa"]},
            )
        path, body = mock_put.call_args.args
        assert path == "/api/chat/slots/chat-2-200/tags"
        assert body == {
            "tags": ["cccccccccccc", "bbbbbbbbbbbb"],
            "base_tags_revision": "r2",
        }
        assert mock_put.call_args.kwargs["session_key"] == "dashboard:chat-1-100"
        assert "added `Blocked`" in out and "removed `Active`" in out
        assert "`kirocrew`, `Blocked`" in out

    def test_a_no_op_delta_writes_nothing(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner(
                "chat_tag_assign",
                {"session": "chat-2-200", "add": ["Active"], "remove": ["Blocked"]},
            )
        mock_put.assert_not_called()
        assert out.startswith("No change")

    def test_at_least_one_of_add_or_remove_is_required(self) -> None:
        with patch("kiro_crew.mcp_dashboard._put") as mock_put:
            out = _call_tool_inner("chat_tag_assign", {"session": "chat-2-200"})
        assert out.startswith("Error:")
        mock_put.assert_not_called()

    def test_a_tag_in_both_lists_is_refused(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner(
                "chat_tag_assign",
                {"session": "chat-2-200", "add": ["Active"], "remove": ["active"]},
            )
        assert out.startswith("Error:") and "both" in out
        mock_put.assert_not_called()

    def test_an_unknown_tag_fails_the_whole_call(self) -> None:
        """A delta lands whole or not at all — no partial re-labelling."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner(
                "chat_tag_assign", {"session": "chat-2-200", "add": ["Blocked", "Nope"]}
            )
        assert out.startswith("Error:") and "no tag matches" in out and "chat_tag_create" in out
        mock_put.assert_not_called()

    def test_a_partial_name_is_not_a_match(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner("chat_tag_assign", {"session": "chat-3-300", "add": ["Act"]})
        assert out.startswith("Error:")
        mock_put.assert_not_called()

    def test_accepts_a_dashboard_session_key_and_an_exact_title(self) -> None:
        for ref in ("dashboard:chat-3-300", "scratch"):
            with (
                patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
                patch("kiro_crew.mcp_dashboard._put", return_value={"ok": True}) as mock_put,
            ):
                _call_tool_inner("chat_tag_assign", {"session": ref, "add": ["Active"]})
            assert mock_put.call_args.args[0] == "/api/chat/slots/chat-3-300/tags"

    def test_a_missing_revision_sends_an_unconditional_write(self) -> None:
        """A row without ``tags_revision`` has nothing to compare against."""

        def _no_rev(path: str) -> list[dict]:
            if path == "/api/chat/tags":
                return [dict(t) for t in _TAGS]
            return [{"key": "chat-1-100", "title": "Caller"}, {"key": "chat-3-300", "title": "Old"}]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_no_rev),
            patch("kiro_crew.mcp_dashboard._put", return_value={"ok": True}) as mock_put,
        ):
            _call_tool_inner("chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]})
        assert "base_tags_revision" not in mock_put.call_args.args[1]

    def test_a_stale_base_is_explained_as_a_retry(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._put",
                return_value={
                    "error": "tags changed since the list was composed",
                    "code": "stale_base",
                },
            ),
        ):
            out = _call_tool_inner("chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]})
        assert out.startswith("Error:") and "Nothing was written" in out and "again" in out

    def test_other_endpoint_errors_are_surfaced(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._put",
                return_value={"error": "session was deleted or rebound", "code": "session_gone"},
            ),
        ):
            out = _call_tool_inner("chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]})
        assert out == "Error: session was deleted or rebound"

    def test_slot_key_is_url_quoted(self) -> None:
        odd = [dict(s) for s in _SLOTS] + [{"key": "a b/c", "title": "Odd", "tags": []}]

        def _get(path: str) -> list[dict]:
            return [dict(t) for t in _TAGS] if path == "/api/chat/tags" else odd

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._put", return_value={"ok": True}) as mock_put,
        ):
            _call_tool_inner("chat_tag_assign", {"session": "a b/c", "add": ["Active"]})
        assert mock_put.call_args.args[0] == "/api/chat/slots/a%20b%2Fc/tags"

    def test_an_unverifiable_caller_cannot_tag_another_session(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner("chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]})
        assert out.startswith("Error:") and "cannot verify" in out
        mock_put.assert_not_called()

    def test_a_subagent_cannot_tag_a_session(self) -> None:
        """A subagent key matches no slot and must not read as "no app"."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value="subagent:abc"),
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner("chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]})
        assert out.startswith("Error:") and "runs on behalf of whatever created it" in out
        mock_put.assert_not_called()

    def test_an_app_cannot_see_or_tag_a_foreign_session(self) -> None:
        def _mixed(path: str) -> list[dict]:
            if path == "/api/chat/tags":
                return [dict(t) for t in _TAGS]
            return [
                {"key": "chat-1-100", "title": "Radar run", "app": "issue-radar", "tags": []},
                {"key": "chat-3-300", "title": "Person's own", "app": "", "tags": []},
            ]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_mixed),
            patch("kiro_crew.mcp_dashboard._put") as mock_put,
        ):
            out = _call_tool_inner("chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]})
        assert out.startswith("Error:") and "no live session" in out
        mock_put.assert_not_called()

    def test_the_schema_bounds_the_delta(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_tag_assign", {"session": "x", "add": ["a"] * 33})
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_tag_assign", {"session": "x", "add": "Active"})
