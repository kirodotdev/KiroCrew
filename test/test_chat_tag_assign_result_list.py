"""chat_tag_assign must report the tag list the server PERSISTED, not its request.

The server filters the PUT to the live vocabulary under the write lock (e.g. a
tag deleted between the tool's GET and PUT), so an authoritative empty list
means the session has no tags. ``_run_chat_tag_assign`` used to fall back to its
own unpersisted request list (``d.get("tags") or new_tags``) and to compute
"added"/"removed" from the request, so it reported tags the session never got.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_TAGS = [
    {"id": "aaaaaaaaaaaa", "name": "Active", "color": "#22c55e", "order": 0, "status": True},
    {"id": "cccccccccccc", "name": "kirocrew", "color": "#6b7280", "order": 2, "status": False},
]
_CALLER = Caller.strict("dashboard:chat-1-100")


def _run(slot_tags: list[str], put_reply: dict[str, Any]) -> str:
    slots = [
        {"key": "chat-1-100", "title": "Caller", "tags": [], "tags_revision": "r1"},
        {"key": "chat-3-300", "title": "Scratch", "tags": slot_tags, "tags_revision": "r3"},
    ]
    dash = InMemoryDashboardClient(
        {
            "GET /api/chat/tags": _TAGS,
            "GET /api/chat/slots": slots,
            "PUT /api/chat/slots/{slot}/tags": put_reply,
        }
    )
    return TABLE.call(
        "chat_tag_assign", {"session": "chat-3-300", "add": ["Active"]}, ToolContext(dash, _CALLER)
    )


def test_empty_persisted_list_is_reported_as_none() -> None:
    # Server filtered "aaaaaaaaaaaa" (the tag was deleted between the GET and PUT).
    out = _run([], {"ok": True, "tags": [], "tags_revision": "r4"})
    assert "Tags now: none" in out, out
    assert "added `Active`" not in out, out


def test_dropped_add_is_not_reported_as_added() -> None:
    out = _run(["cccccccccccc"], {"ok": True, "tags": ["cccccccccccc"], "tags_revision": "r4"})
    assert "Tags now: `kirocrew`." in out, out
    assert "added `Active`" not in out, out
