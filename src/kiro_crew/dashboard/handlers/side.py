"""HTTP handlers for /side: ephemeral Q&A attached to a parent slot.

Sidecar buffer on ``slot._side``; isolated ``side:{slot.key}`` LLM session;
tool calls hard-rejected via ``REJECT_ALL``. Side messages never enter
``slot.messages`` or any persistent store.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from aiohttp import web

from kiro_crew.acp.client import AcpAuthRequired
from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings
from kiro_crew.dashboard.side_context import build_side_message
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.ws import broadcast_side_result
from kiro_crew.llm_helpers import (
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    stream_and_collect,
)
from kiro_crew.security import StreamRedactor, redact
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_QUESTION_BYTES = 32_768


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _side_session_key(slot_key: str) -> str:
    """Return the isolated ACP session key for a side turn."""
    return f"side:{slot_key}"


async def _run_side_turn(
    state: DashboardState,
    slot,
    run_id: str,
    question: str,
    *,
    is_first_turn: bool,
) -> None:
    """Background task: drive one side turn and broadcast chunks over WS."""
    side_key = _side_session_key(slot.key)
    message = build_side_message(slot, question, is_first_turn=is_first_turn)
    chunks: list[str] = []
    # Rolling-buffer redactor for the live side stream. broadcast_side_result
    # already redacts each frame, but per-frame redaction alone misses a secret
    # split across streaming chunk boundaries; StreamRedactor withholds a
    # trailing credential-class run until it's confirmed safe. Mirrors
    # chat_runner._wsred so /side has the same protection as the main chat.
    _wsred = StreamRedactor()

    def _on_chunk(text: str) -> None:
        chunks.append(text)
        safe = _wsred.feed(text)
        if safe:
            broadcast_side_result(
                state,
                slot_key=slot.key,
                run_id=run_id,
                role="assistant",
                content=safe,
            )

    provider = None
    acquired_key = ""
    try:
        # Resolve the KiroCrew slot agent name (e.g. "default") to the real
        # kiro-cli agent (e.g. "kirocrew") before creating the session. Passing
        # the raw slot name straight through to get_or_create -> create_session
        # -> set_mode fails with "Mode '<name>' not found" because there is no
        # matching ~/.kiro/agents/<name>.json. Mirrors chat_runner._run_chat and
        # chat_handlers, which resolve bindings for the same reason. Best-effort:
        # a config-load failure degrades to the raw slot.agent rather than
        # crashing the turn.
        kiro_agent: str | None = None
        try:
            cfg = KiroCrewConfig.load()
            kiro_agent = resolve_agent_bindings(cfg, slot.agent or None).kiro_agent
        except Exception:
            logger.warning(
                "Side turn: failed to resolve agent bindings for slot=%s; "
                "falling back to raw slot.agent",
                slot.key,
                exc_info=True,
            )

        provider, _is_new, _resumed = await state.sessions.get_or_create(
            side_key,
            agent=kiro_agent or slot.agent or None,
        )
        acquired_key = side_key
        try:
            response_text = await stream_and_collect(
                provider,
                message,
                approval_policy=ToolApprovalPolicy.REJECT_ALL,
                on_chunk=_on_chunk,
            )
            # Redact the assembled text before it is stored/broadcast as the
            # terminal frame (which replaces the streamed deltas). Never trust
            # LLM output on an external surface. redact() applies BOTH passes —
            # redact_exfiltration_urls() then redact_credentials() (security.py)
            # — so exfil URLs and credentials are both scrubbed here.
            response_text = redact(response_text)
        except PromptBusyExhaustedError:
            logger.warning(
                "Side turn aborted (prompt busy exhausted): slot=%s run_id=%s",
                slot.key,
                run_id,
            )
            response_text = redact("".join(chunks))
            if slot._side is not None and slot._side.open and slot._side.last_run_id == run_id:
                slot._side.append_assistant(response_text)
            broadcast_side_result(
                state,
                slot_key=slot.key,
                run_id=run_id,
                role="assistant",
                content="(side conversation interrupted — please retry)",
                is_error=True,
                final=True,
            )
            return

        if not chunks:
            response_text = (
                "I tried to use a tool to answer that, but tool "
                "execution is not available in /side conversations. "
                "Let me try again using only the context I have — "
                "please rephrase your question if you'd like a "
                "different approach."
            )
            logger.info(
                "Side turn produced no text (tool rejection): " "slot=%s run_id=%s",
                slot.key,
                run_id,
            )

        if slot._side is not None and slot._side.open and slot._side.last_run_id == run_id:
            slot._side.append_assistant(response_text)

        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=run_id,
            role="assistant",
            content=response_text,
            ts=time.time(),
            final=True,
        )
    except asyncio.CancelledError:
        raise
    except AcpAuthRequired as exc:
        # A signed-out CLI is actionable, so surface its own message rather than
        # the generic failure below — the side panel has no other channel to tell
        # the user what to do. Latch the service signed-out too, so the
        # fail-closed gates stop trusting a stale ready value.
        logger.warning("Side turn auth required: slot=%s run_id=%s", slot.key, run_id)
        # Local import: chat_runner imports from this package, so a module-level
        # import would close a cycle.
        from kiro_crew.dashboard.chat_runner import _mark_kiro_signed_out

        _mark_kiro_signed_out(state)
        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=run_id,
            role="assistant",
            content=str(exc),
            is_error=True,
            final=True,
        )
    except Exception:
        logger.exception(
            "Side turn failed: slot=%s run_id=%s",
            slot.key,
            run_id,
        )
        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=run_id,
            role="assistant",
            content="(side conversation failed — see server logs)",
            is_error=True,
            final=True,
        )
    finally:
        # Identity-check run_id so a stale task from a closed-and-reopened
        # side never flips is_complete on the new state's in-flight turn.
        if slot._side is not None and slot._side.last_run_id == run_id:
            slot._side.is_complete = True
        if acquired_key:
            try:
                state.sessions.release(acquired_key)
            except Exception:
                logger.debug(
                    "Failed to release side session %s",
                    acquired_key,
                    exc_info=True,
                )


def _check_slot_ownership(
    request: web.Request,
    slot,
    operation: str,
) -> web.Response | None:
    """Return 403 if the request app can't access ``slot``; mirrors ``api_chat``.

    App Kit §5.2: dashboard users (empty ``request_app``) can access everything.
    The auth gate is upstream in ``token_auth_middleware``; this is the
    app-vs-dashboard scope check, matching ``chat_handlers.py`` and ``chat_fork.py``.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    if not slot._app:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="app cannot access unscoped slots",
        )
        return web.json_response(
            {"error": "not found"},
            status=404,
        )
    if slot._app != request_app:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="app does not own this slot",
        )
        # 404 (not 403) so a foreign/unscoped slot is indistinguishable from a
        # missing one — anti-enumeration (CWE-204); true reason logged via SEL.
        return web.json_response(
            {"error": "not found"},
            status=404,
        )
    return None


async def api_side_open(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/open — idempotent sidecar init."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    own = _check_slot_ownership(request, slot, "chat.side_open")
    if own is not None:
        return own

    if slot._side is None or not slot._side.open:
        slot._side = SideState(open=True, created_at=_now_iso())
        outcome = "opened"
    else:
        outcome = "reopened"

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_open",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},result={outcome}",
    )
    return web.json_response(
        {
            "ok": True,
            "open": True,
            "messages": len(slot._side.messages),
            "last_run_id": slot._side.last_run_id,
            "created_at": slot._side.created_at,
        }
    )


async def api_side_turn(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/turn — body ``{"question": str}``.

    Returns ``{ok, run_id}`` immediately and drives the LLM stream in a
    background task; chunks are broadcast on ``chat.side_result``.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    own = _check_slot_ownership(request, slot, "chat.side_turn")
    if own is not None:
        return own

    if not request.body_exists:
        return web.json_response({"error": "missing JSON body"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object"},
            status=400,
        )

    question = body.get("question")
    if not isinstance(question, str):
        return web.json_response(
            {"error": "question must be a string"},
            status=400,
        )
    question = question.strip()
    if not question:
        return web.json_response(
            {"error": "question must not be empty"},
            status=400,
        )
    if len(question.encode("utf-8")) > _MAX_QUESTION_BYTES:
        return web.json_response(
            {"error": f"question too long (max {_MAX_QUESTION_BYTES} bytes)"},
            status=400,
        )

    # Check after body parse: ``api_side_close`` may have landed during
    # ``await request.json()``.
    if slot._side is None or not slot._side.open:
        return web.json_response(
            {"error": "side conversation is not open"},
            status=409,
        )

    if slot._side.last_run_id and not slot._side.is_complete:
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="chat.side_turn",
            outcome="denied",
            source="dashboard",
            resources=(f"slot={slot.key}," f"in_flight_run_id={slot._side.last_run_id}"),
            error="side turn already in flight",
        )
        return web.json_response(
            {
                "error": (
                    "a side turn is already in flight on this slot — "
                    "wait for it to complete before submitting another"
                ),
                "in_flight_run_id": slot._side.last_run_id,
            },
            status=409,
        )

    run_id = uuid.uuid4().hex
    is_first_turn = not any(m.get("role") == "assistant" for m in slot._side.messages)
    slot._side.last_run_id = run_id
    slot._side.is_complete = False
    slot._side.append_user(question)

    broadcast_side_result(
        state,
        slot_key=slot.key,
        run_id=run_id,
        role="user",
        content=question,
    )

    task = asyncio.create_task(
        _run_side_turn(
            state,
            slot,
            run_id,
            question,
            is_first_turn=is_first_turn,
        )
    )
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_turn",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"slot={slot.key},run_id={run_id},"
            f"messages={len(slot._side.messages)},"
            f"question_len={len(question)}"
        ),
    )
    return web.json_response(
        {
            "ok": True,
            "run_id": run_id,
            "messages": len(slot._side.messages),
        }
    )


async def api_side_close(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/close — drop sidecar + destroy session."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    own = _check_slot_ownership(request, slot, "chat.side_close")
    if own is not None:
        return own

    was_open = slot._side is not None and slot._side.open
    slot._side = None

    side_key = _side_session_key(slot.key)
    try:
        await state.sessions.destroy(side_key)
    except Exception:
        logger.debug(
            "Failed to destroy side session %s",
            side_key,
            exc_info=True,
        )

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_close",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},was_open={was_open}",
    )
    return web.json_response({"ok": True, "was_open": was_open})
