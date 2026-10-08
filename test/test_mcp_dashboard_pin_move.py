"""``chat_session_pin_move``: reorder a pinned session with before/after anchors."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_FOLDERS = [{"id": "aaaaaaaaaaaa", "name": "Work", "parent_id": ""}]

#: The caller's own row: locatable for the scope check, hidden from rendering.
_CALLER_ROW = {"key": "chat-0-000", "title": "Caller", "folder_id": "", "memory_mode": "incognito"}

#: Reordering the person's pins requires an identity the gateway vouches for.
_CALLER = Caller.strict("dashboard:chat-0-000")

_SLOTS = [
    _CALLER_ROW,
    # Top level: p2 ranked before p1; p3 pinned but never ranked.
    {"key": "chat-1-100", "title": "One", "folder_id": "", "pinned": True, "pin_rank": 1},
    {"key": "chat-2-200", "title": "Two", "folder_id": "", "pinned": True, "pin_rank": 0},
    {
        "key": "chat-3-300",
        "title": "Three",
        "folder_id": "",
        "pinned": True,
        "pin_rank": None,
        "last_ts": "2026-09-20T00:00:00",
        "created": "2026-09-19T00:00:00+00:00",
    },
    # Folder Work.
    {
        "key": "chat-4-400",
        "title": "Four",
        "folder_id": "aaaaaaaaaaaa",
        "pinned": True,
        "pin_rank": 2,
    },
    {"key": "chat-5-500", "title": "Five", "folder_id": "aaaaaaaaaaaa", "pinned": False},
]


def _routes(slots: list[dict] | None = None, post_result: Any = None) -> dict[str, Any]:
    return {
        "GET /api/chat/folders": [dict(f) for f in _FOLDERS],
        "GET /api/chat/slots": [dict(s) for s in (_SLOTS if slots is None else slots)],
        "POST /api/chat/pinned-order": {"ok": True} if post_result is None else post_result,
    }


def _call(
    name: str,
    args: dict,
    routes: dict[str, Any] | None = None,
    caller: Caller = _CALLER,
) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient(_routes() if routes is None else routes)
    return TABLE.call(name, args, ToolContext(dash, caller)), dash


def _posts(dash: InMemoryDashboardClient) -> list[DashboardRequest]:
    return [r for r in dash.requests if r.method == "POST"]


def _move(
    args: dict, routes: dict[str, Any] | None = None, caller: Caller = _CALLER
) -> tuple[str, list[DashboardRequest]]:
    out, dash = _call("chat_session_pin_move", args, routes, caller)
    return out, _posts(dash)


def test_before_anchor_sends_the_whole_pinned_order() -> None:
    out, posts = _move({"session": "chat-3-300", "before": "chat-2-200"})
    assert len(posts) == 1
    assert posts[0].path == "/api/chat/pinned-order"
    # Ranked rows by rank, then the unranked one, with the move applied; the
    # other group's pinned row is carried so every pinned session is ranked.
    # Each row that reports ``created`` sends it back, so a recreated key is
    # refused by the gateway instead of ranked.
    assert posts[0].body == {
        "keys": ["chat-3-300", "chat-2-200", "chat-1-100", "chat-4-400"],
        "expected_created": {"chat-3-300": "2026-09-19T00:00:00+00:00"},
    }
    assert posts[0].session_key == "dashboard:chat-0-000"
    assert "pinned #1 of 3" in out and "the top level" in out


def test_after_anchor_by_title() -> None:
    out, posts = _move({"session": "Two", "after": "One"})
    assert posts[0].body == {
        "keys": ["chat-1-100", "chat-2-200", "chat-4-400", "chat-3-300"],
        "expected_created": {"chat-3-300": "2026-09-19T00:00:00+00:00"},
    }
    assert "pinned #2 of 3" in out


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"session": "chat-1-100"}, "exactly one of `before` or `after`"),
        (
            {"session": "chat-1-100", "before": "chat-2-200", "after": "chat-3-300"},
            "exactly one of `before` or `after`",
        ),
        ({"session": "chat-5-500", "before": "chat-4-400"}, "is not pinned"),
        ({"session": "chat-4-400", "before": "chat-5-500"}, "anchor `chat-5-500` is not pinned"),
        ({"session": "chat-4-400", "before": "chat-1-100"}, "different sidebar group"),
        ({"session": "chat-1-100", "before": "chat-1-100"}, "the anchor is the session"),
    ],
)
def test_refusals_write_nothing(args: dict, fragment: str) -> None:
    out, posts = _move(args)
    assert out.startswith("Error:") and fragment in out
    assert posts == []


def test_an_unverifiable_caller_is_refused() -> None:
    out, posts = _move(
        {"session": "chat-1-100", "before": "chat-2-200"},
        caller=Caller.unverified("dashboard:chat-0-000"),
    )
    assert "cannot verify which session is calling" in out
    assert posts == []


@pytest.mark.parametrize(
    "scope",
    [
        pytest.param(None, id="unlocatable"),
        pytest.param(frozenset({"some-app"}), id="app-scoped-or-delegated"),
    ],
)
def test_a_scoped_or_unlocatable_caller_is_refused(scope: Any) -> None:
    """The tool refuses these itself, not only through the blocked-tools list."""
    with patch("kiro_crew.mcp_dashboard._caller_app_scope", return_value=scope):
        out, posts = _move({"session": "chat-1-100", "before": "chat-2-200"})
    assert out.startswith("Error:") and "app-scoped or delegated caller" in out
    assert posts == []


@pytest.mark.parametrize(
    "caller",
    [
        pytest.param("channel:slack:C1.100", id="channel-agent"),
        pytest.param("slack:1712793600.123456", id="slack-thread"),
        pytest.param("1712793600.123456", id="legacy-slack-thread"),
        pytest.param("discord:kirocrew:dm:U1", id="discord-dm"),
        pytest.param("telegram_kirocrew_dm_42", id="persisted-channel-stem"),
    ],
)
def test_a_messaging_caller_is_refused_before_any_session_is_listed(caller: str) -> None:
    """An auto-approved call never reaches the permission prompt, so the refusal is here.

    Every messaging session acts on text other people wrote, so each of its key
    forms is refused, not only a channel agent's ``channel:`` keys.
    """
    sel_obj = MagicMock()
    with patch("kiro_crew.mcp_dashboard.sel", return_value=sel_obj):
        out, dash = _call(
            "chat_session_pin_move",
            {"session": "chat-1-100", "before": "chat-2-200"},
            caller=Caller.strict(caller),
        )
    assert out.startswith("Error:") and "messaging-channel sessions" in out
    assert dash.requests == []
    assert sel_obj.log_tool_invocation.call_args.kwargs["outcome"] == "rejected_blocked_tool"


def test_an_endpoint_error_is_reported() -> None:
    out, _ = _move(
        {"session": "chat-1-100", "before": "chat-2-200"},
        routes=_routes(
            post_result={"error": "a session in the reorder is gone or no longer pinned"}
        ),
    )
    assert out.startswith("Error: could not reorder pinned sessions")


def test_folder_tree_lists_pinned_sessions_first_in_sidebar_order() -> None:
    out, _ = _call("chat_folder_tree", {})
    top = out[out.index("(unfiled") :]
    assert top.index("chat-2-200") < top.index("chat-1-100") < top.index("chat-3-300")


def test_a_private_pinned_session_keeps_its_place_and_stays_out_of_the_reply() -> None:
    """Incognito rows are hidden from the agent but not dropped from the order."""
    private = {
        "key": "chat-9-900",
        "title": "Private",
        "folder_id": "",
        "pinned": True,
        "pin_rank": 1,
        "memory_mode": "incognito",
    }
    rows = [dict(s) for s in _SLOTS]
    for row in rows:
        if row["key"] == "chat-1-100":
            row["pin_rank"] = 2
        if row["key"] == "chat-4-400":
            row["pin_rank"] = 3
    out, posts = _move(
        {"session": "chat-3-300", "after": "chat-2-200"},
        routes=_routes(slots=[*rows, dict(private)]),
    )
    keys = posts[0].body["keys"]
    assert keys == ["chat-2-200", "chat-3-300", "chat-9-900", "chat-1-100", "chat-4-400"]
    assert "chat-9-900" not in out and "Private" not in out
    assert "pinned #2 of 3" in out
