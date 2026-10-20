"""Who a request speaks for: the crew member scope and the private-memory session check.

``member_request_scope`` resolves the member (or the cron job run as one) a request
acts for, ``internal_memory_scope`` answers the same for an internal caller, and
``require_private_memory_session`` refuses a private member's request that names no
session of its own.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Mapping, NamedTuple

from aiohttp import web

from kiro_crew.dashboard.handlers._shared_access import require_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState

if TYPE_CHECKING:
    from kiro_crew.execution_context import ExecutionContext

logger = logging.getLogger("kiro_crew.dashboard.handlers._shared")


async def _audit_private_memory_denial(operation: str, error: str) -> None:
    """Record a denial without changing it or initializing SEL on the event loop."""

    def _write() -> None:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller="internal",
            operation=operation,
            outcome="denied",
            source="member_memory",
            error=error,
        )

    try:
        await asyncio.to_thread(_write)
    except Exception:
        logger.debug("SEL audit for denied member operation %s failed", operation, exc_info=True)


class MemberScope(NamedTuple):
    """Authenticated transport attribution and the captured execution target."""

    session: str | None
    verified: bool
    store: str | None
    execution: ExecutionContext | None = None


_MEMBER_SCOPE_KEY = "_member_scope"


def _cron_execution_from_registry(
    state: DashboardState | None, session: str
) -> tuple[bool | None, "ExecutionContext | None"]:
    """Resolve a code-cron's captured execution without opening its transcript.

    Script crons use the gateway's internal HTTP API, but they do not create a
    dashboard transcript before the script starts. Their stable ``cron:<id>``
    key is therefore resolved from the scheduler's in-memory job record, whose
    ``execution_context`` was captured when the job was admitted. This keeps
    built-in script helpers on the same canonical route as message crons and
    refuses a missing or malformed V2 record instead of falling back to Global.
    Legacy V1 jobs retain their explicit store through ``resolve_cron_memory``.

    ``True`` means a job was found, ``False`` means the configured registry has
    no such job, and ``None`` means this request has no scheduler registry (for
    example a reduced Slack-only surface). The first two outcomes are refused
    or routed by the caller without falling back to a transcript or Global.
    """
    if state is None or not session.startswith("cron:"):
        return None, None
    parts = session.split(":")
    job_id = parts[1] if len(parts) > 1 else ""
    if not job_id:
        return True, None
    jobs = getattr(getattr(state, "crons", None), "_jobs", None)
    if jobs is None:
        return None, None
    if not isinstance(jobs, (list, tuple, Mapping)):
        # The scheduler registry is a concrete list in production. A surface
        # that exposes only a placeholder object has no registry to consult;
        # preserve the pre-V2 directive validation path.
        return None, None
    try:
        candidates = jobs.values() if isinstance(jobs, Mapping) else jobs
        job = next((item for item in candidates if getattr(item, "id", "") == job_id), None)
    except Exception:  # noqa: BLE001 - identity resolution fails closed
        return True, None
    if job is None:
        return False, None
    try:
        from kiro_crew.execution_context import execution_from_record

        marker = object()
        execution_record = getattr(job, "execution_context", marker)
        if execution_record is marker:
            # A reduced/legacy scheduler surface may expose only the caller
            # ownership fields. It has no V2 routing authority; leave it to the
            # existing transcript/global path rather than manufacturing one.
            return None, None
        if execution_record is not None:
            execution = execution_from_record({"execution_context": execution_record})
        else:
            # A V2 schedule without its captured record is refused. Legacy V1
            # records keep their old path; this helper must not reinterpret
            # their display/store fields as a V2 identity.
            member_id = getattr(job, "member_id", "")
            memory_store = getattr(job, "memory_store", "")
            from kiro_crew.memory_stores import memory_store_version

            if not isinstance(member_id, str) or not isinstance(memory_store, str):
                return None, None
            if memory_store_version(memory_store) == 2 or (member_id and not memory_store):
                return True, None
            return None, None
        return True, execution
    except (AttributeError, OSError, ValueError):
        return True, None


async def member_request_scope(request: web.Request) -> MemberScope:
    """Authenticate the caller and read its binding off the loop, at most once per request."""
    cached = request.get(_MEMBER_SCOPE_KEY)
    if isinstance(cached, MemberScope):
        return cached
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.member_memory_auth import session_key_is_attested

    def _resolve() -> MemberScope:
        if request.get("internal_auth") is not True:
            return MemberScope(None, False, None)
        session = request.headers.get("X-Session-Key", "")
        if not isinstance(session, str):
            return MemberScope(None, False, None)
        if not session:
            return MemberScope(None, True, None)
        if not session_key_is_attested(request, session):
            # The header names an execution record this transport cannot vouch
            # for. An unverified scope is the answer the callers already turn
            # into their 409 ``member_identity_unavailable`` / 403 refusals, so
            # the record is never read on a name the caller merely asserted.
            return MemberScope(session, False, None)
        try:
            execution = read_session_execution(session)
        except (OSError, ValueError):
            if not session.startswith("cron:"):
                return MemberScope(session, False, None)
            execution = None
        if session.startswith("cron:"):
            found, cron_execution = _cron_execution_from_registry(request.app.get("state"), session)
            if found is False or (found is True and cron_execution is None):
                return MemberScope(session, False, None)
            if found is True:
                # The scheduler record is authoritative for a cron run. A
                # stale transcript must never rebind an existing job after a
                # retry, restart, or job edit.
                execution = cron_execution
        if execution is None:
            return MemberScope(session, True, "")
        return MemberScope(session, True, execution.store.legacy_name, execution)

    scope = await asyncio.to_thread(_resolve)
    try:
        request[_MEMBER_SCOPE_KEY] = scope
    except TypeError:  # a request double without item assignment: re-resolve next time
        pass
    return scope


async def internal_memory_scope(
    request: web.Request, operation: str, *, claimed_session: str | None = None
) -> tuple[str | None, web.Response | None]:
    """Capture routing from the authenticated request without opening memory."""
    if request.get("internal_auth") is not True:
        return None, None
    scope = await member_request_scope(request)
    if scope.verified and (claimed_session is None or claimed_session == scope.session):
        return scope.store or None, None
    await _audit_private_memory_denial(operation, "The execution identity is unavailable.")
    return None, web.json_response(
        {
            "error": "The execution identity is unavailable; Global memory was not used.",
            "code": "member_identity_unavailable",
        },
        status=409,
    )


def spawn_parent_claim(request: web.Request, parent_session: str) -> str | None:
    """The session a spawn route claims for ``internal_memory_scope``.

    The spawn routes default a missing ``parent_session`` to ``""``. For a caller
    without a session (no ``X-Session-Key``: only the internal secret, e.g. a
    host-side sidecar or the CLI) that empty parent is "no claim", so it is passed
    as ``None``. A caller WITH a session keeps the empty claim, which does not
    match its own session and is refused: it may not start a run detached from
    the session that owns it.
    """
    if parent_session or request.headers.get("X-Session-Key", ""):
        return parent_session
    return None


async def require_private_memory_session(
    request: web.Request, store: str, operation: str, *, session_key: str | None = None
) -> web.Response | None:
    """Require the selected target to match this call's captured execution."""
    if request.get("internal_auth") is not True:
        return await require_owner_dashboard_request(request, operation) if store else None
    scope = await member_request_scope(request)
    if scope.verified and (session_key is None or session_key == scope.session):
        if (scope.store or "") == store:
            return None
    return web.json_response(
        {
            "error": "The request does not match its execution memory target; Global was not used.",
            "code": "member_identity_unavailable",
        },
        status=409,
    )
