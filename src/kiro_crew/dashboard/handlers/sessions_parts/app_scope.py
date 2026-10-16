"""What an app caller may see of the session history, and the audit of each judgement.

The transcript-ownership rule every history route applies to an app caller: an app reads,
summarises, claims or deletes only the transcripts it owns, a refusal and an allow are both
written to the SEL, and a transcript the app does not own answers as not found. Also the
late-binding ``_sel()`` every session route audits through. New per-resource rules for app
callers go here.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog, HistoryLockTimeout, transcript_lock_stems

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


#: The SEL reason for every refusal of a transcript the caller app does not own.
_NOT_TRANSCRIPT_OWNER = "app does not own this transcript"
#: The approvals list contains state-level requests, which no app may resolve.
_STATE_APPROVALS_NOT_APP_VISIBLE = "state-level approvals are not app-visible"


def _audit_app_decision(
    request_app: str, operation: str, resource: str, outcome: str, reason: str = ""
) -> None:
    """Record one app-isolation decision in the SEL, best-effort.

    A log fault never turns the caller's answer into a 500.
    """
    try:
        _sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome=outcome,
            source="app_isolation",
            resources=resource,
            error=reason,
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("SEL audit failed for %s %s", operation, outcome, exc_info=True)


def _audit_app_denial(request_app: str, operation: str, resource: str, reason: str) -> None:
    """Record one app-isolation refusal in the SEL, best-effort."""
    _audit_app_decision(request_app, operation, resource, "denied", reason)


def _audit_app_allow(request_app: str, operation: str, resource: str) -> None:
    """Record one app-isolation grant, so every ownership decision leaves a record."""
    _audit_app_decision(request_app, operation, resource, "allowed")


def _app_not_found(request_app: str, operation: str, resource: str, reason: str) -> web.Response:
    """Audit one app-isolation refusal and return the uniform 404.

    The body is the one every app refusal and every genuinely missing target
    shares, so an app cannot tell "not yours" from "does not exist"; the reason
    goes to the security-event log instead.
    """
    # Imported here: chat_handlers imports this package at module scope.
    from kiro_crew.dashboard.chat_handlers import _slot_not_found

    _audit_app_denial(request_app, operation, resource, reason)
    return _slot_not_found()


def _app_transcript_busy() -> web.Response:
    """The retryable answer when an owned read could not take the transcript lock.

    Only reached after the unlocked ownership check passed, so it says nothing
    about a transcript the app does not own.
    """
    return web.json_response(
        {"error": "This session is busy. Try again in a moment.", "code": "session_busy"},
        status=503,
    )


def _app_owns_transcript(log: ConversationLog, request_app: str, key: str) -> bool:
    """Whether the transcript *key* records *request_app* as its owner app.

    THE transcript-owner rule for app-token callers on ``/api/sessions*``: the
    ``app`` field the slot save writes on the metadata line must equal the caller.
    Identity is positive -- a transcript with no recorded app (every user, cron
    and channel session) belongs to no app, and a missing transcript to nobody.
    Blocking file IO; call it off the event loop.
    """
    if not log.has_log(key):
        return False
    return str(log.get_metadata(key).get("app") or "") == request_app


def _app_may_claim_transcript(log: ConversationLog, request_app: str, key: str) -> bool:
    """Whether a NEW slot *request_app* opens may bind to transcript *key*.

    The marker :func:`_app_owns_transcript` trusts is written by the slot save,
    so an app slot born over somebody else's transcript would rewrite it to name
    the app. The chat create, send and resume routes therefore admit a new app
    slot only onto a transcript that is missing or already the app's own.
    Blocking file IO; call it off the event loop.
    """
    return not log.has_log(key) or _app_owns_transcript(log, request_app, key)


def _app_owned_messages(log: ConversationLog, request_app: str, key: str) -> list[dict] | None:
    """*key*'s rows while *request_app* owns it, else None.

    The ownership verdict and the read run under one hold of *key*'s transcript
    locks, so a same-key delete and recreate cannot land between them and hand
    the app a transcript it does not own. Callers run the unlocked
    :func:`_app_transcript_refusal` first, so no lock sidecar is created for a
    key the app has no claim on. Blocking; off the loop. A lock timeout raises
    ``HistoryLockTimeout``.
    """
    with log.locked_stems(transcript_lock_stems(key)):
        if not _app_owns_transcript(log, request_app, key):
            return None
        return log.read_messages(key)


async def _app_transcript_refusal(
    request: web.Request, state: DashboardState, key: str, operation: str
) -> web.Response | None:
    """The uniform 404 for an app caller naming a transcript it does not own, else None."""
    request_app = str(request.get("app") or "")
    if not request_app:
        return None
    log = state.conversation_log
    if log is not None and await asyncio.to_thread(_app_owns_transcript, log, request_app, key):
        return None
    return _app_not_found(request_app, operation, f"session={key}", _NOT_TRANSCRIPT_OWNER)


def _app_owned_sessions(log: ConversationLog, request_app: str, sessions: list[dict]) -> list[dict]:
    """The rows of *sessions* whose transcript *request_app* owns. Blocking; off the loop."""
    owned: list[dict] = []
    for row in sessions:
        key = row.get("key", "")
        if not _app_owns_transcript(log, request_app, key):
            continue
        try:
            with log._locked(key):
                if not _app_owns_transcript(log, request_app, key):
                    continue
                # The unlocked catalog row may describe a foreign incarnation.
                owned.extend(log.list_sessions(keys=(key,)))
        except HistoryLockTimeout:
            continue
    return owned


def _app_bulk_history_refusal(request: web.Request, operation: str) -> web.Response | None:
    """The uniform 404 for an app caller on a whole-history route, else None.

    ``DELETE /api/sessions`` and its ``clearable/count`` act on every closed
    transcript at once, and no app owns that set, so an app is refused outright.
    """
    request_app = str(request.get("app") or "")
    if not request_app:
        return None
    return _app_not_found(request_app, operation, "sessions=*", "whole-history route")
