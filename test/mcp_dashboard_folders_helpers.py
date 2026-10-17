"""Shared rows and call helpers for the ``test_mcp_dashboard_folders*.py`` suites.

The folders and slots the in-memory dashboard serves, the verified and unverified
callers, and the helpers that run one tools/call frame through ``mcp_dashboard.TABLE``
and read back what the dashboard was asked.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

# Representative GET /api/chat/folders body — a bare JSON array (no envelope),
# each row carrying only parent_id (the human path is derived client-side).
_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": "", "history_count": 3},
    {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa", "history_count": 0},
    {"id": "cccccccccccc", "name": "Travel", "parent_id": "", "history_count": 0},
]

#: The caller slot's birth stamp — ``created`` on every real row. It rides on
#: the self-filing PATCH as ``expected_created`` so the endpoint can refuse a
#: write aimed at a slot that was recreated under the same key.
_CALLER_BORN = "2026-09-14T05:00:00.000001+00:00"

_SLOTS = [
    {
        "key": "chat-1-100",
        "title": "Backup M1",
        "folder_id": "aaaaaaaaaaaa",
        "running": True,
        "created": _CALLER_BORN,
    },
    {"key": "chat-2-200", "title": "Folder MCP", "folder_id": "bbbbbbbbbbbb"},
    {"key": "chat-3-300", "title": "Scratch", "folder_id": ""},
]

#: The caller's OWN slot, which the raw ``/api/chat/slots`` list always carries.
#: A live dashboard session is necessarily in that list, so a fixture that omits
#: it models a state production cannot reach — and one that is now refused,
#: because a ``dashboard:`` key naming an absent slot means the tab was closed
#: mid-call or the key is wrong.
#:
#: Marked non-persistent so it stays LOCATABLE (the scope resolver reads the raw
#: rows) while being filtered out of the RENDERED list — which is what the
#: tree-shape cases of the folder suites want to assert.
_CALLER_ROW = {
    "key": "chat-1-100",
    "title": "Caller",
    "folder_id": "",
    "memory_mode": "incognito",
    "created": _CALLER_BORN,
}

#: Filing another session requires an identity the gateway vouches for, so every
#: case runs as this verified dashboard session unless it names another caller.
_CALLER = Caller.strict("dashboard:chat-1-100")

#: A caller only the lenient resolver can name — what a spawned subagent's
#: process walk looks like — so every strict check refuses it.
_UNVERIFIED = Caller.unverified("dashboard:chat-1-100")


def _slots_with_caller(*extra: dict) -> list[dict]:
    """The caller's own row plus whatever the case under test needs."""
    return [dict(_CALLER_ROW), *[dict(e) for e in extra]]


def _reads(folders: list[dict] | None = None, slots: list[dict] | None = None) -> dict[str, Any]:
    """The two array endpoints every folder tool reads."""
    return {
        "GET /api/chat/folders": _FOLDERS if folders is None else folders,
        "GET /api/chat/slots": _SLOTS if slots is None else slots,
    }


def _call(
    name: str,
    args: dict[str, Any],
    routes: dict[str, Any] | None = None,
    caller: Caller = _CALLER,
) -> tuple[str, InMemoryDashboardClient]:
    """One tools/call frame against an in-memory dashboard answering ``routes``."""
    dash = InMemoryDashboardClient(_reads() if routes is None else routes)
    return TABLE.call(name, args, ToolContext(dash, caller)), dash


def _writes(dash: InMemoryDashboardClient) -> list[DashboardRequest]:
    """Every request that could change the dashboard."""
    return [r for r in dash.requests if r.method != "GET"]


def _minting_post() -> Any:
    """A folder-create route that mints a fresh id per call."""
    count = {"n": 0}

    def _post(req: DashboardRequest) -> dict:
        count["n"] += 1
        return {
            "id": f"new{count['n']:09d}",
            "name": req.body["name"],
            "parent_id": req.body["parent_id"],
        }

    return _post
