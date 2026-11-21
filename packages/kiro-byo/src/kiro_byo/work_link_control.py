"""Optional ACP host → BYO execution correlation (no scheduling authority).

Crew owns Ticket/Assignment identity. This module only binds validated, host-
provided identifiers at an idle or guarded turn boundary and persists the
session event projection before acknowledging or running any model/tool.
"""
from __future__ import annotations

from typing import Any

from .host_work_link import validate_work_link, work_link_from_params
from .session_events import bind_work_link
from .state_io import _file_transaction_lock


def bind_explicit_work_link(server: Any, session: Any, link: dict[str, str]) -> bool:
    """Persist correlation while caller holds turn_lock and disk turn guard."""
    old_events = list(session.events)
    old_link = dict(session.host_work_link)
    try:
        changed = bind_work_link(session, link)
        if changed and not server._persist_session(session):
            raise RuntimeError("Crew work link could not be persisted")
        return changed
    except Exception:
        # Do not leave uncommitted identity in memory after a failed CAS/write.
        session.events[:] = old_events
        session.host_work_link = old_link
        raise


def handle_work_bind_request(server: Any, req_id: Any, params: dict[str, Any]) -> None:
    """Explicit, acknowledged late binding for a pooled or resumed session."""
    session_id = params.get("sessionId")
    try:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("sessionId is required")
        link = validate_work_link(params.get("workLink"))
    except (ValueError, TypeError) as exc:
        server.error(req_id, -32602, str(exc))
        return
    session = server._get_session(session_id)
    if session is None:
        server.error(req_id, -32602, "unknown session")
        return
    if not session.turn_lock.acquire(blocking=False):
        server.error(req_id, -32002, "session busy; retry work binding after the turn")
        return
    turn_guard = _file_transaction_lock(
        server._session_path(session_id).with_suffix(".turn"), timeout=0.01
    )
    acquired = False
    try:
        with server._sessions_lock:
            unavailable = (
                session.retired or session.replacing or
                server.sessions.get(session_id) is not session
            )
        if unavailable:
            raise RuntimeError("session is being replaced or closed")
        turn_guard.__enter__()
        acquired = True
        server._assert_session_current(session)
        with session.state_lock:
            if session.active_turn:
                raise RuntimeError("session has an active turn")
            changed = bind_explicit_work_link(server, session, link)
        server.result(req_id, {
            "sessionId": session_id,
            "workLink": dict(session.host_work_link),
            "changed": changed,
            "scope": "diagnostic_only",
        })
    except (RuntimeError, TimeoutError, OSError) as exc:
        server.error(req_id, -32002, str(exc))
    finally:
        if acquired:
            turn_guard.__exit__(None, None, None)
        session.turn_lock.release()


def admit_prompt_work_link(
    server: Any, req_id: Any, params: dict[str, Any], session: Any, turn_guard: Any
) -> bool:
    """Associate a new Assignment before this turn's first Run is created.

    Called after the normal prompt's local + cross-process turn locks. On
    rejection it releases BOTH locks; the caller must not start the turn.
    """
    try:
        supplied, link = work_link_from_params(params)
        if supplied:
            with session.state_lock:
                bind_explicit_work_link(server, session, link)
        return True
    except (ValueError, TypeError, RuntimeError, OSError, TimeoutError) as exc:
        turn_guard.__exit__(None, None, None)
        session.turn_lock.release()
        server.error(req_id, -32602 if isinstance(exc, (ValueError, TypeError)) else -32002, str(exc))
        return False
