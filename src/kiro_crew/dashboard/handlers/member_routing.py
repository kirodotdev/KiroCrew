"""POST /api/member-routing/decision — record a ``select_crew`` routing decision.

The tool runs in the sandboxed mcp-core, which cannot reach a member's crew log.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings
from kiro_crew.dashboard.handlers._shared import _read_session_key, resolve_session_memory_mode
from kiro_crew.history import ConversationLog, transcript_privacy_mode
from kiro_crew.member_memory_auth import session_key_is_attested
from kiro_crew.members import record_activity
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.sel import sel
from kiro_crew.workflow_memory import WorkflowMemoryError

logger = logging.getLogger(__name__)

_UNKNOWN = "unknown"
_UNAVAILABLE = "unavailable"


def _deny(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _audit_denied(caller: str, message: str) -> None:
    try:
        sel().log_api_access(
            caller=caller,
            operation="member_routing_decision",
            outcome="denied",
            source="member_routing",
            resources="/api/member-routing",
            error=message,
        )
    except Exception:
        logger.warning("SEL audit failed for member-routing denial", exc_info=True)


async def _memory_mode(request: web.Request, session_key: str) -> str:
    # Live policy first; a private header still vetoes it, as the memory routes read both.
    try:
        mode = await resolve_session_memory_mode(request.app["state"], session_key)
    except (OSError, ValueError, WorkflowMemoryError):
        return "incognito"
    try:
        header, readable = await asyncio.to_thread(
            ConversationLog().get_metadata_status, session_key
        )
    except Exception:
        return "incognito"
    if not readable:
        return "incognito"
    return transcript_privacy_mode(header.get("memory_mode")) or mode


def _record(crew: str, session_key: str, memory_mode: str) -> bool | str:
    cfg = KiroCrewConfig.load()
    if crew not in cfg.agents:
        return _UNKNOWN
    try:
        resolve_agent_bindings(cfg, crew, validate_memory_files=False)
    except UnknownMemoryStore:
        return _UNAVAILABLE
    return record_activity(crew, session_key, memory_mode, via="select_crew")


async def api_member_routing_decision(request: web.Request) -> web.Response:
    """``{crew}``: the calling session bound *crew* through ``select_crew``."""
    sk = _read_session_key(request)
    if request.get("internal_auth") is not True:
        return _deny(403, "internal_secret_required", "forbidden")
    if not sk:
        return _deny(400, "missing_session_key", "missing X-Session-Key")
    if not await asyncio.to_thread(session_key_is_attested, request, sk):
        _audit_denied(sk, "unattested session")
        return _deny(403, "unattested_caller", "this session could not be verified")
    try:
        body: Any = await request.json()
    except Exception:
        body = {}
    crew = body.get("crew") if isinstance(body, dict) else None
    if not isinstance(crew, str) or not crew:
        return _deny(400, "missing_crew", "crew is required")
    memory_mode = await _memory_mode(request, sk)
    recorded = await asyncio.to_thread(_record, crew, sk, memory_mode)
    if recorded == _UNKNOWN:
        return _deny(404, "unknown_crew", f"unknown crew '{crew}'")
    if recorded == _UNAVAILABLE:
        return _deny(409, "crew_unavailable", f"crew '{crew}' cannot bind its memory")
    return web.json_response({"recorded": recorded})
