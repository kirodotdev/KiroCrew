"""``chat_session_pin_move``: reorder a pinned session with before/after anchors."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.mcp_dashboard import _call_tool_inner

_FOLDERS = [{"id": "aaaaaaaaaaaa", "name": "Work", "parent_id": ""}]

#: The caller's own row: locatable for the scope check, hidden from rendering.
_CALLER = {"key": "chat-0-000", "title": "Caller", "folder_id": "", "memory_mode": "incognito"}

_SLOTS = [
    _CALLER,
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


def _rows(path: str) -> list[dict]:
    if path == "/api/chat/folders":
        return [dict(f) for f in _FOLDERS]
    if path == "/api/chat/slots":
        return [dict(s) for s in _SLOTS]
    raise AssertionError(f"unexpected GET {path}")


@pytest.fixture(autouse=True)
def _verified_caller() -> Any:
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-0-000",
    ):
        yield


def _move(args: dict, post_result: dict | None = None) -> tuple[str, Any]:
    with (
        patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
        patch(
            "kiro_crew.mcp_dashboard._post", return_value=post_result or {"ok": True}
        ) as mock_post,
    ):
        out = _call_tool_inner("chat_session_pin_move", args)
    return out, mock_post


def test_before_anchor_sends_the_whole_pinned_order() -> None:
    out, mock_post = _move({"session": "chat-3-300", "before": "chat-2-200"})
    path, body = mock_post.call_args.args
    assert path == "/api/chat/pinned-order"
    # Ranked rows by rank, then the unranked one, with the move applied; the
    # other group's pinned row is carried so every pinned session is ranked.
    assert body == {"keys": ["chat-3-300", "chat-2-200", "chat-1-100", "chat-4-400"]}
    assert mock_post.call_args.kwargs["session_key"] == "dashboard:chat-0-000"
    assert "pinned #1 of 3" in out and "the top level" in out


def test_after_anchor_by_title() -> None:
    out, mock_post = _move({"session": "Two", "after": "One"})
    assert mock_post.call_args.args[1] == {
        "keys": ["chat-1-100", "chat-2-200", "chat-4-400", "chat-3-300"]
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
    out, mock_post = _move(args)
    assert out.startswith("Error:") and fragment in out
    mock_post.assert_not_called()


def test_an_unverifiable_caller_is_refused() -> None:
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""):
        out, mock_post = _move({"session": "chat-1-100", "before": "chat-2-200"})
    assert "cannot verify which session is calling" in out
    mock_post.assert_not_called()


def test_an_endpoint_error_is_reported() -> None:
    out, _ = _move(
        {"session": "chat-1-100", "before": "chat-2-200"},
        post_result={"error": "a session in the reorder is gone or no longer pinned"},
    )
    assert out.startswith("Error: could not reorder pinned sessions")


def test_folder_tree_lists_pinned_sessions_first_in_sidebar_order() -> None:
    with patch("kiro_crew.mcp_dashboard._get", side_effect=_rows):
        out = _call_tool_inner("chat_folder_tree", {})
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

    def _with_private(path: str) -> list[dict]:
        return [*rows, dict(private)] if path == "/api/chat/slots" else _rows(path)

    with (
        patch("kiro_crew.mcp_dashboard._get", side_effect=_with_private),
        patch("kiro_crew.mcp_dashboard._post", return_value={"ok": True}) as mock_post,
    ):
        out = _call_tool_inner(
            "chat_session_pin_move", {"session": "chat-3-300", "after": "chat-2-200"}
        )
    keys = mock_post.call_args.args[1]["keys"]
    assert keys == ["chat-2-200", "chat-3-300", "chat-9-900", "chat-1-100", "chat-4-400"]
    assert "chat-9-900" not in out and "Private" not in out
    assert "pinned #2 of 3" in out
