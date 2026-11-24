"""``POST /api/mcp/project-servers/approve``: the ``mcp.project_server`` card's Apply.

Records one server of a folder's own MCP file as approved
(:func:`kiro_crew.project_mcp_approvals.approve`), then restarts open chats, so the
next session in that folder starts it. Hooked by the change-card middleware
(``change_card_catalog.HOOKED_ROUTES``), so a card's Apply marks the card applied.
Owner-only: an agent never reaches it, so only the person's press on the card
writes the record. The record is written only when the file still launches what
the card showed (``fingerprint``); a file changed since answers 409 and the next
session in that folder raises a card for the new launch. A record at its bounds
answers 409 too, before any chat restarts.
"""

from __future__ import annotations

import asyncio
import os
import string

from aiohttp import web

from kiro_crew.project_mcp_approvals import (
    MAX_FOLDER_CHARS,
    MAX_SERVER_NAME_CHARS,
    ApprovalStale,
    RecordFull,
    approve,
)
from kiro_crew.project_mcp_files import PROJECT_MCP_FILES


def _is_fingerprint(value: object) -> bool:
    """A sha256 hex digest, the shape :func:`project_mcp_approvals.fingerprint` returns."""
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


_HEX = frozenset(string.digits + "abcdef")


def _refuse(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def api_mcp_project_server_approve(request: web.Request) -> web.Response:
    """Body: ``{"folder", "file", "server", "fingerprint"}``."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "mcp_project_server_approve")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return _refuse(400, "invalid_json", "invalid JSON body")
    if not isinstance(body, dict):
        return _refuse(400, "body_not_object", "body must be an object")
    folder, file = body.get("folder"), body.get("file")
    server, expected = body.get("server"), body.get("fingerprint")
    if not isinstance(folder, str) or not os.path.isabs(folder) or len(folder) > MAX_FOLDER_CHARS:
        return _refuse(400, "invalid_param", "'folder' must be an absolute path")
    if file not in PROJECT_MCP_FILES:
        return _refuse(400, "invalid_param", "'file' must be a project MCP file")
    if not isinstance(server, str) or not server or len(server) > MAX_SERVER_NAME_CHARS:
        return _refuse(400, "invalid_param", "'server' must name a server")
    if not isinstance(expected, str) or not _is_fingerprint(expected):
        return _refuse(400, "invalid_param", "'fingerprint' must be a sha256 digest")
    try:
        done = await asyncio.to_thread(approve, folder, file, server, expected)
    except ApprovalStale:
        return _refuse(
            409,
            "changed",
            f"'{server}' changed in {file} after this card was shown. Start a new chat "
            "in that folder to see the new card.",
        )
    except RecordFull:
        return _refuse(
            409,
            "record_full",
            "Kiro Crew can't record more approvals. Remove servers you don't use from this "
            "folder's MCP file, or delete project folders you don't use, then try again.",
        )
    from kiro_crew.dashboard.handlers.sessions import _reset_all_sessions

    reset = int(await _reset_all_sessions(request))
    return web.json_response(
        {
            "approved": done.name,
            "folder": done.folder,
            "file": done.file,
            "sessions_reset": reset,
        }
    )
