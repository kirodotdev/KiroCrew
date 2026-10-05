"""Tests for the session tag tools on the kirocrew-dashboard server.

Covers ``chat_tag_list`` / ``chat_tag_create`` / ``chat_tag_update`` /
``chat_tag_assign`` — schema validation, id-or-name resolution, the add/remove
delta composed onto the slot's current list, the compare-and-set revision the
PUT carries, the identity gates, and result formatting. Each case is one
tools/call frame through ``mcp_dashboard.TABLE`` against an in-memory
dashboard; the endpoints themselves are tested by ``test_chat_tags.py``.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_dashboard import TABLE, _list_tools
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

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

#: Tagging another session resolves identity strictly, like filing one, so every
#: case runs as this verified caller unless it names another.
_CALLER = Caller.strict("dashboard:chat-1-100")
_UNVERIFIED = Caller.unverified("dashboard:chat-1-100")


def _reads(tags: Any = None, slots: Any = None, **writes: Any) -> dict[str, Any]:
    return {
        "GET /api/chat/tags": _TAGS if tags is None else tags,
        "GET /api/chat/slots": _SLOTS if slots is None else slots,
        "POST /api/chat/tags": writes.get("post", {"error": "unexpected create"}),
        "PATCH /api/chat/tags/{tag}": writes.get("patch", {"error": "unexpected update"}),
        "PUT /api/chat/slots/{slot}/tags": writes.get("put", {"error": "unexpected assign"}),
    }


def _call(
    name: str, args: dict[str, Any], routes: dict[str, Any] | None = None, caller: Caller = _CALLER
) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient(_reads() if routes is None else routes)
    return TABLE.call(name, args, ToolContext(dash, caller)), dash


def _writes(dash: InMemoryDashboardClient) -> list[DashboardRequest]:
    return [r for r in dash.requests if r.method != "GET"]


class TestAdvertised:
    def test_the_three_tag_tools_are_listed(self) -> None:
        names = {t["name"] for t in _list_tools()}
        assert {"chat_tag_list", "chat_tag_create", "chat_tag_update", "chat_tag_assign"} <= names


class TestTagList:
    def test_renders_every_tag_with_id_color_and_status(self) -> None:
        out, _ = _call("chat_tag_list", {})
        assert "3 tags" in out
        assert "`Active`" in out and "id=aaaaaaaaaaaa" in out and "#22c55e" in out
        assert out.count("[status]") == 2
        # Stored order, not alphabetical: what the sidebar shows.
        assert out.index("Active") < out.index("Blocked") < out.index("kirocrew")

    def test_empty_vocabulary_points_at_create(self) -> None:
        out, _ = _call("chat_tag_list", {}, _reads(tags=[]))
        assert "chat_tag_create" in out

    def test_a_malformed_persisted_order_sorts_as_zero_instead_of_crashing(self) -> None:
        """``tags.json`` is loaded verbatim, so a hand-edited row must still render."""
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "Later", "order": 1},
            {"id": "bbbbbbbbbbbb", "name": "Broken", "order": "invalid"},
            {"id": "cccccccccccc", "name": "Missing"},
        ]
        out, _ = _call("chat_tag_list", {}, _reads(tags=rows))
        assert "3 tags" in out
        assert out.index("Broken") < out.index("Later") and out.index("Missing") < out.index(
            "Later"
        )

    def test_endpoint_error_is_not_reported_as_empty(self) -> None:
        out, _ = _call("chat_tag_list", {}, _reads(tags={"error": "boom"}))
        assert out.startswith("Error:") and "boom" in out

    def test_the_read_needs_no_verified_caller(self) -> None:
        """The vocabulary names no session, so the read is not scoped to the caller."""
        out, dash = _call("chat_tag_list", {}, caller=_UNVERIFIED)
        assert "3 tags" in out
        assert [r.route for r in dash.requests] == ["GET /api/chat/tags"]


class TestTagCreate:
    def test_posts_name_color_and_status(self) -> None:
        made = {"id": "dddddddddddd", "name": "Review", "color": "#3b82f6", "status": True}
        out, dash = _call(
            "chat_tag_create",
            {"name": "Review", "color": "#3b82f6", "status": True},
            _reads(post=made),
        )
        (post,) = _writes(dash)
        assert post.path == "/api/chat/tags"
        assert post.body == {"name": "Review", "status": True, "color": "#3b82f6"}
        assert post.session_key == "dashboard:chat-1-100"
        assert "Review" in out and "dddddddddddd" in out and "status tag" in out

    def test_color_is_omitted_when_not_given(self) -> None:
        made = {"id": "dddddddddddd", "name": "Review", "color": "#6b7280"}
        _, dash = _call("chat_tag_create", {"name": "Review"}, _reads(post=made))
        assert "color" not in _writes(dash)[-1].body

    def test_malformed_color_is_refused_by_schema(self) -> None:
        out, dash = _call("chat_tag_create", {"name": "Review", "color": "red"})
        assert out == "Error: color: invalid format"
        assert dash.requests == []

    def test_name_is_required(self) -> None:
        out, dash = _call("chat_tag_create", {})
        assert out == "Error: name: required"
        assert dash.requests == []

    def test_an_existing_name_is_reported_not_duplicated(self) -> None:
        """The endpoint dedups on the lowered name; the tool reports what exists."""
        out, _ = _call("chat_tag_create", {"name": "active"}, _reads(post=dict(_TAGS[0])))
        assert "Active" in out and "aaaaaaaaaaaa" in out

    def test_an_unverifiable_caller_cannot_create(self) -> None:
        out, dash = _call("chat_tag_create", {"name": "Review"}, caller=_UNVERIFIED)
        assert out.startswith("Error:") and "cannot verify" in out
        assert _writes(dash) == []

    def test_an_apps_create_reaches_the_endpoint_and_its_refusal_is_explained(self) -> None:
        """The app rule is the endpoint's (``api_chat_tag_create``), not a second copy here."""
        refused = {"error": "apps cannot create shared tags", "code": "app_forbidden"}
        out, dash = _call(
            "chat_tag_create",
            {"name": "Review"},
            _reads(
                slots=[{"key": "chat-1-100", "title": "Radar", "app": "issue-radar"}], post=refused
            ),
        )
        (post,) = _writes(dash)
        assert post.session_key == "dashboard:chat-1-100"
        assert out.startswith("Error:") and "shared vocabulary" in out

    def test_a_credential_in_the_name_is_redacted_before_the_write(self) -> None:
        _, dash = _call(
            "chat_tag_create",
            {"name": "key AKIAIOSFODNN7EXAMPLE"},
            _reads(post={"id": "dddddddddddd", "name": "x"}),
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in _writes(dash)[-1].body["name"]

    def test_the_schema_refuses_an_overlong_name(self) -> None:
        out, dash = _call("chat_tag_create", {"name": "x" * 61})
        assert out.startswith("Error: name: exceeds max length 60")
        assert dash.requests == []


class TestTagUpdate:
    def test_patches_by_name_with_only_the_named_fields(self) -> None:
        updated = {"id": "bbbbbbbbbbbb", "name": "Waiting", "color": "#ef4444", "status": True}
        out, dash = _call(
            "chat_tag_update", {"tag": "blocked", "name": "Waiting"}, _reads(patch=updated)
        )
        (patch,) = _writes(dash)
        assert patch.path == "/api/chat/tags/bbbbbbbbbbbb"
        assert patch.body == {"name": "Waiting"}
        assert patch.session_key == "dashboard:chat-1-100"
        assert "renamed `Blocked` → `Waiting`" in out

    def test_patches_color_and_status_by_id(self) -> None:
        updated = {"id": "cccccccccccc", "name": "kirocrew", "color": "#3b82f6", "status": True}
        out, dash = _call(
            "chat_tag_update",
            {"tag": "cccccccccccc", "color": "#3b82f6", "status": True},
            _reads(patch=updated),
        )
        assert _writes(dash)[-1].body == {"color": "#3b82f6", "status": True}
        assert "color #6b7280 → #3b82f6" in out and "status tag: yes" in out

    def test_at_least_one_field_is_required(self) -> None:
        out, dash = _call("chat_tag_update", {"tag": "Blocked"})
        assert out.startswith("Error:")
        assert dash.requests == []

    def test_unknown_tag_never_reaches_the_endpoint(self) -> None:
        out, dash = _call("chat_tag_update", {"tag": "Nope", "name": "X"})
        assert out.startswith("Error:") and "no tag matches" in out
        assert _writes(dash) == []

    def test_malformed_color_is_refused_by_schema(self) -> None:
        out, dash = _call("chat_tag_update", {"tag": "Blocked", "color": "red"})
        assert out == "Error: color: invalid format"
        assert dash.requests == []

    def test_a_credential_in_the_new_name_is_redacted_before_the_write(self) -> None:
        _, dash = _call(
            "chat_tag_update",
            {"tag": "Blocked", "name": "key AKIAIOSFODNN7EXAMPLE"},
            _reads(patch={"id": "bbbbbbbbbbbb", "name": "x"}),
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in _writes(dash)[-1].body["name"]

    def test_an_unverifiable_caller_cannot_update(self) -> None:
        out, dash = _call("chat_tag_update", {"tag": "Blocked", "name": "X"}, caller=_UNVERIFIED)
        assert out.startswith("Error:") and "cannot verify" in out
        assert _writes(dash) == []

    def test_the_endpoints_app_refusal_is_explained(self) -> None:
        refused = {"error": "apps cannot write shared tags", "code": "app_forbidden"}
        out, _ = _call("chat_tag_update", {"tag": "Blocked", "name": "X"}, _reads(patch=refused))
        assert out.startswith("Error:") and "shared vocabulary" in out


class TestTagAssign:
    def test_adds_by_name_and_removes_by_id_as_a_delta_with_the_base_revision(self) -> None:
        """Tags not named are kept; the PUT names the revision the delta was composed on."""
        stored = {"ok": True, "tags": ["cccccccccccc", "bbbbbbbbbbbb"], "tags_revision": "r9"}
        out, dash = _call(
            "chat_tag_assign",
            {"session": "chat-2-200", "add": ["blocked"], "remove": ["aaaaaaaaaaaa"]},
            _reads(put=stored),
        )
        (put,) = _writes(dash)
        assert put.path == "/api/chat/slots/chat-2-200/tags"
        assert put.body == {
            "tags": ["cccccccccccc", "bbbbbbbbbbbb"],
            "base_tags_revision": "r2",
        }
        assert put.session_key == "dashboard:chat-1-100"
        assert "added `Blocked`" in out and "removed `Active`" in out
        assert "`kirocrew`, `Blocked`" in out

    def test_a_no_op_delta_writes_nothing(self) -> None:
        out, dash = _call(
            "chat_tag_assign", {"session": "chat-2-200", "add": ["Active"], "remove": ["Blocked"]}
        )
        assert _writes(dash) == []
        assert out.startswith("No change")

    def test_at_least_one_of_add_or_remove_is_required(self) -> None:
        out, dash = _call("chat_tag_assign", {"session": "chat-2-200"})
        assert out.startswith("Error:")
        assert dash.requests == []

    def test_a_tag_in_both_lists_is_refused(self) -> None:
        out, dash = _call(
            "chat_tag_assign", {"session": "chat-2-200", "add": ["Active"], "remove": ["active"]}
        )
        assert out.startswith("Error:") and "both" in out
        assert _writes(dash) == []

    def test_an_unknown_tag_fails_the_whole_call(self) -> None:
        """A delta lands whole or not at all — no partial re-labelling."""
        out, dash = _call("chat_tag_assign", {"session": "chat-2-200", "add": ["Blocked", "Nope"]})
        assert out.startswith("Error:") and "no tag matches" in out and "chat_tag_create" in out
        assert _writes(dash) == []

    def test_a_partial_name_is_not_a_match(self) -> None:
        out, dash = _call("chat_tag_assign", {"session": "chat-3-300", "add": ["Act"]})
        assert out.startswith("Error:")
        assert _writes(dash) == []

    def test_accepts_a_dashboard_session_key_and_an_exact_title(self) -> None:
        for ref in ("dashboard:chat-3-300", "scratch"):
            _, dash = _call(
                "chat_tag_assign", {"session": ref, "add": ["Active"]}, _reads(put={"ok": True})
            )
            assert _writes(dash)[-1].path == "/api/chat/slots/chat-3-300/tags"

    def test_a_missing_revision_sends_an_unconditional_write(self) -> None:
        """A row without ``tags_revision`` has nothing to compare against."""
        rows = [{"key": "chat-1-100", "title": "Caller"}, {"key": "chat-3-300", "title": "Old"}]
        _, dash = _call(
            "chat_tag_assign",
            {"session": "chat-3-300", "add": ["Active"]},
            _reads(slots=rows, put={"ok": True}),
        )
        assert "base_tags_revision" not in _writes(dash)[-1].body

    def test_a_stale_base_is_explained_as_a_retry(self) -> None:
        stale = {"error": "tags changed since the list was composed", "code": "stale_base"}
        out, _ = _call(
            "chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]}, _reads(put=stale)
        )
        assert out.startswith("Error:") and "Nothing was written" in out and "again" in out

    def test_other_endpoint_errors_are_surfaced(self) -> None:
        gone = {"error": "session was deleted or rebound", "code": "session_gone"}
        out, _ = _call(
            "chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]}, _reads(put=gone)
        )
        assert out == "Error: session was deleted or rebound"

    def test_slot_key_is_url_quoted(self) -> None:
        odd = [dict(s) for s in _SLOTS] + [{"key": "a b/c", "title": "Odd", "tags": []}]
        _, dash = _call(
            "chat_tag_assign",
            {"session": "a b/c", "add": ["Active"]},
            _reads(slots=odd, put={"ok": True}),
        )
        assert _writes(dash)[-1].path == "/api/chat/slots/a%20b%2Fc/tags"

    def test_an_unverifiable_caller_cannot_tag_another_session(self) -> None:
        out, dash = _call(
            "chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]}, caller=_UNVERIFIED
        )
        assert out.startswith("Error:") and "cannot verify" in out
        assert _writes(dash) == []

    def test_a_subagent_cannot_tag_a_session(self) -> None:
        """A subagent key matches no slot and must not read as "no app"."""
        out, dash = _call(
            "chat_tag_assign",
            {"session": "chat-3-300", "add": ["Active"]},
            caller=Caller.strict("subagent:abc"),
        )
        assert out.startswith("Error:") and "runs on behalf of whatever created it" in out
        assert _writes(dash) == []

    def test_an_app_cannot_see_or_tag_a_foreign_session(self) -> None:
        rows = [
            {"key": "chat-1-100", "title": "Radar run", "app": "issue-radar", "tags": []},
            {"key": "chat-3-300", "title": "Person's own", "app": "", "tags": []},
        ]
        out, dash = _call(
            "chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]}, _reads(slots=rows)
        )
        assert out.startswith("Error:") and "no live session" in out
        assert _writes(dash) == []

    def test_the_schema_bounds_the_delta(self) -> None:
        out, dash = _call("chat_tag_assign", {"session": "x", "add": ["a"] * 33})
        assert out.startswith("Error: add: exceeds max items")
        out, dash = _call("chat_tag_assign", {"session": "x", "add": "Active"})
        assert out.startswith("Error: add: expected list")
        assert dash.requests == []
