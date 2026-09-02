"""HTTP routes for the per-session work ledger.

Thin mapping over :mod:`kiro_crew.session_ledger`. The security contract is
the Issue Radar crew-route one: the ledger a request touches is derived from
the CALLING SESSION's identity (``X-Session-Key``, vetted by
``_recognize_session``), never from the request body — so a session can only
ever read or write its own ledger, and raw HTTP with no recognized session
identity is refused. Restricted (incognito/temporary/guest) sessions are
refused too: a ledger is durable on-disk state, which is exactly what those
modes promise not to leave behind.

Both routes are MCP-only (no browser caller) and listed in
``server._STRICT_INTERNAL_API_PATHS`` — without that entry the internal-secret
call falls through to cookie auth and every tool call fails with 403.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew import session_ledger
from kiro_crew.dashboard.handlers._shared import _is_restricted_session

# Module-scope like memory.py's identical imports: the recognition gate and
# incognito classifier are this module's own load-bearing dependencies.
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import is_incognito_transcript
from kiro_crew.sel import sel
from kiro_crew.validation import (
    SESSION_LEDGER_RECORD_SCHEMA,
    ValidationError,
    validate_tool_args,
)

logger = logging.getLogger(__name__)


async def _resolve_ledger_key(
    request: web.Request, operation: str
) -> tuple[str, None] | tuple[None, web.Response]:
    """Vet the calling session and fold its key to the ledger spelling.

    Returns ``(key, None)`` on success or ``(None, refusal_response)``. The
    fold is :func:`session_ledger.ledger_key` — a LOSSLESS dashboard-prefix
    strip, so the spelling here matches what the nudge composer derives from a
    loop's slot key, while distinct channel session keys can never collide.
    """
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, operation, blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return None, refusal
    if _is_restricted_session(state, request):
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
            error="Ledger writes are not allowed in this session mode.",
        )
        return None, web.json_response(
            {
                "error": "The work ledger is not available in this session mode.",
                "code": "restricted_session",
            },
            status=403,
        )
    return session_ledger.ledger_key(sk), None


async def api_session_ledger_get(request: web.Request) -> web.Response:
    """GET /api/session-ledger — the calling session's state record + event tail.

    One read: the event tail rides inside the state document, so state and
    events always come from the same transaction (no torn pairing).
    """
    key, refusal = await _resolve_ledger_key(request, "session_ledger_read")
    if refusal is not None:
        return refusal
    assert key is not None
    state_record = await asyncio.to_thread(session_ledger.read_state, key)
    events = state_record.get("events", [])[-session_ledger._MAX_EVENT_TAIL :]
    return web.json_response({"state": state_record, "events": events})


async def api_session_ledger_record(request: web.Request) -> web.Response:
    """POST /api/session-ledger/record — one partial update, locked transaction."""
    key, refusal = await _resolve_ledger_key(request, "session_ledger_record")
    if refusal is not None:
        return refusal
    assert key is not None
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_body"},
            status=400,
        )
    known = {f.name for f in SESSION_LEDGER_RECORD_SCHEMA.fields}
    try:
        cleaned = validate_tool_args(
            {k: v for k, v in body.items() if k in known},
            SESSION_LEDGER_RECORD_SCHEMA,
        )
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "validation_error"}, status=400)
    artifacts = cleaned.get("artifacts")
    if artifacts is not None and not all(
        isinstance(k, str) and isinstance(v, str) for k, v in artifacts.items()
    ):
        return web.json_response(
            {
                "error": "artifacts must map strings to strings",
                "code": "artifacts_not_string_map",
            },
            status=400,
        )
    try:
        # record() takes the per-ledger file lock (bounded acquire + disk
        # I/O); off-loop so a merely-contended cross-process peer cannot
        # freeze chat/WS/heartbeat — against a live lock HOLDER the bounded
        # acquire costs at most one refused write. This does NOT bound a
        # wedged filesystem/mount: the pre-lock mkdir/os.open can still stall
        # this worker thread at the syscall (a mount-health failure mode the
        # lock deadline cannot reach), so off-loading it keeps that stall off
        # the event loop rather than promising it can't happen.
        state_record = await asyncio.to_thread(
            session_ledger.record,
            key,
            goal=cleaned.get("goal"),
            phase=cleaned.get("phase"),
            next_step=cleaned.get("next"),
            tried_approach=cleaned.get("tried_approach"),
            tried_rejected_because=cleaned.get("tried_rejected_because"),
            artifacts=artifacts,
            event=cleaned.get("event"),
            event_kind=cleaned.get("event_kind"),
        )
    except ValueError as exc:
        # The phase-requires-event discipline (and key validation) surface here.
        return web.json_response({"error": str(exc), "code": "ledger_discipline"}, status=400)
    except OSError:
        logger.warning("session ledger write failed for %s", key, exc_info=True)
        return web.json_response(
            {"error": "ledger write failed; try again", "code": "ledger_write_failed"},
            status=503,
        )
    return web.json_response({"ok": True, "state": state_record})
