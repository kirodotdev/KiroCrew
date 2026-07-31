"""Shared request plumbing for the Meetings routes.

Holds the pieces every handler needs and nothing route-specific:

* :func:`require_enabled` — the deny-by-default authorization decorator. The app
  is ``defaultEnabled: false`` and routes are registered once at gateway
  startup, so without this a disabled app would stay callable.
* :func:`json_body` / the ``field_*`` helpers — input validation. Every value
  that reaches the filesystem or a model prompt goes through one of these.
* :data:`ACTIVE` — the single active meeting (``MAX_CONCURRENT_MEETINGS == 1``).
* :func:`error_response` — uniform error mapping, including the
  :class:`~..store.MeetingsPathError` → status translation.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from http import HTTPStatus
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain.session import MeetingSession
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.hooks import get_global_hook_store  # noqa: F401  (re-export for handlers)
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.meetings")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

MAX_BODY_BYTES = 256 * 1024


class _ActiveMeeting:
    """Module-level holder for the one live meeting.

    A plain module global would be reassigned by ``global`` statements scattered
    across four route modules; a holder keeps the mutation in one place and makes
    the "there is exactly one" invariant explicit. Safe as plain attribute access
    on the single-threaded asyncio loop.
    """

    def __init__(self) -> None:
        self.session: MeetingSession | None = None

    def get(self, meeting_id: str = "") -> MeetingSession | None:
        """The live session, optionally requiring it to be *meeting_id*'s."""
        session = self.session
        if session is None:
            return None
        if meeting_id and session.meeting_id != meeting_id:
            return None
        return session

    def set(self, session: MeetingSession | None) -> None:
        """Install *session*, replacing any current one.

        The caller MUST have drained the outgoing session first (with
        :meth:`drain_and_clear`) — replacing one that still has queued lines
        discards them, which is why the replace path is loud rather than silent:
        a leftover queue here means transcript is about to be lost, so it is
        logged with the count instead of disappearing.
        """
        previous = self.session
        if previous is not None and previous is not session:
            queued = sum(len(q.queue) for q in previous.agents.values())
            if queued:
                logger.warning(
                    "meetings: replacing session %s with %d queued line(s) still "
                    "undispatched — call drain_and_clear() before set()",
                    previous.meeting_id,
                    queued,
                )
            previous.cancel_all()
        self.session = session

    def clear(self) -> MeetingSession | None:
        """Drop the session, CANCELLING anything still queued.

        Lossy by construction — ``cancel_all`` discards pending batches — so this is
        for the paths that genuinely cannot await, and every other caller should use
        :meth:`drain_and_clear`. Kept separate rather than made private because
        ``drain_and_clear`` composes it after its flush.
        """
        previous = self.session
        if previous is not None:
            previous.cancel_all()
        self.session = None
        return previous

    async def drain_and_clear(self) -> MeetingSession | None:
        """Flush every agent's queue, THEN drop the session.

        The safe default, and what every teardown path wants. ``clear()`` alone
        cancels the pending flush timers, so a meeting torn down with a half-batch
        queued lost that transcript — its notes and tasks silently omitted whatever
        had not yet been dispatched. The expiry path (a four-hour meeting whose next
        line arrives after the session lapsed) and gateway shutdown both hit this;
        stop/pause already flushed by hand, which is exactly the kind of
        remember-to-call-it contract that gets forgotten, so the draining version is
        now the one with the obvious name.

        A flush failure must not prevent teardown — the session is going away either
        way, and a stuck agent should not wedge shutdown.
        """
        previous = self.session
        if previous is not None:
            try:
                await previous.flush_all()
            except Exception:
                logger.warning(
                    "meetings: flushing %s before teardown failed; "
                    "queued transcript may be lost",
                    previous.meeting_id,
                    exc_info=True,
                )
        return self.clear()


ACTIVE = _ActiveMeeting()

#: Serializes the check-then-install of the active meeting.
#:
#: `handle_start_meeting` reads `ACTIVE.get()` to enforce "one meeting at a time",
#: then awaits (metadata IO, then the drain) before calling `set()`. Two starts
#: interleaving in that gap BOTH pass the check, and the second silently replaces
#: the first — whose transcript then fails to dispatch with a confusing 409. An
#: asyncio lock (not threading: this guards event-loop interleaving, not threads)
#: makes the read and the install one critical section.
START_LOCK = asyncio.Lock()


# ── authorization ───────────────────────────────────────────────────────────


def require_enabled(handler: Handler) -> Handler:
    """Deny every request while the app is disabled (deny-by-default).

    ``is_app_enabled`` reads ``installed.json`` synchronously, so it runs off the
    event loop (same as issue-radar's gate).
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, k.APP_NAME):
            audit("meetings.request", request.path, outcome="denied")
            return web.json_response({"error": f"{k.APP_NAME} is disabled", "code": "app_disabled"}, status=403)
        return await handler(request)

    return _wrapped


def audit(operation: str, resource: str, *, outcome: str, error: str = "") -> None:
    """SEL-audit an app-level decision. Never raises."""
    try:
        sel().log_api_access(
            caller=f"app:{k.APP_NAME}",
            operation=operation,
            outcome=outcome,
            resources=resource[:200],
            error=error[:200],
        )
    except Exception:  # pragma: no cover
        logger.exception("meetings: SEL audit failed for %s", operation)


# ── input validation ────────────────────────────────────────────────────────


class BadRequest(Exception):
    """A request body/query failed validation.

    Carries a machine-readable ``code`` as well as the HTTP status: the dashboard
    renders ``error`` verbatim into a localized UI, so the prose is advisory and the
    code is the contract (see ``test/test_error_code_contract.py``).
    """

    def __init__(self, message: str, status: int = 400, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


async def json_body(request: web.Request, *, required: bool = True) -> dict[str, Any]:
    """Parse and size-cap a JSON object body.

    A non-object body (list, string, number) is rejected rather than coerced —
    every handler indexes the result by key, so a list would surface as a 500.
    """
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise BadRequest("request body is too large", status=413)
    try:
        raw = await request.json()
    except Exception:
        if required:
            raise BadRequest("invalid JSON body") from None
        return {}
    if not isinstance(raw, dict):
        raise BadRequest("body must be a JSON object")
    return raw


def field_str(
    body: dict[str, Any],
    key: str,
    *,
    default: str = "",
    max_len: int = 1000,
    required: bool = False,
) -> str:
    """A trimmed string field. A non-string is treated as missing, not coerced.

    ``str(value)`` would stringify a list or a Mock into something that passes a
    truthiness check and then fails deeper in — turning a plainly malformed
    request into a 500 instead of a 400.
    """
    value = body.get(key)
    if not isinstance(value, str):
        if required:
            raise BadRequest(f"{key} is required")
        return default
    value = value.strip()
    if required and not value:
        raise BadRequest(f"{key} is required")
    if len(value) > max_len:
        raise BadRequest(f"{key} must be at most {max_len} characters")
    return value


def field_bool(body: dict[str, Any], key: str, *, default: bool = False) -> bool:
    """A strict boolean field. The string ``"false"`` is truthy under ``bool()``,
    so a type slip must not silently invert a mute/enable decision."""
    value = body.get(key)
    return value if isinstance(value, bool) else default


def field_int(
    body: dict[str, Any], key: str, *, default: int = 0, low: int = 0, high: int = 1_000_000
) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(value, high))


def field_str_list(
    body: dict[str, Any], key: str, *, max_items: int = 100, max_len: int = 200
) -> list[str] | None:
    """A list-of-strings field, or None when absent.

    None is meaningfully different from ``[]`` here: ``agents_enabled=[]`` means
    "no agents", while absent means "use the defaults".
    """
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise BadRequest(f"{key} must be a list of strings")
    out: list[str] = []
    for item in value[:max_items]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:max_len])
    return out


def query_int(request: web.Request, key: str, *, default: int, low: int, high: int) -> int:
    try:
        value = int(request.query.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


# ── responses ───────────────────────────────────────────────────────────────


def error_response(exc: Exception) -> web.Response:
    """Map an app exception to a JSON error response.

    Anything not explicitly mapped is re-raised, so an unexpected bug still
    surfaces as a 500 with a traceback in the log rather than a silent 400.
    """
    if not isinstance(exc, (store.MeetingsPathError, BadRequest)):
        raise exc
    # Each branch repeats the dict LITERAL against a LITERAL status, deliberately.
    # The error-code contract scanner reads `status=exc.status` as `dynamic_status`
    # (it cannot statically tell the response is even an error) and a hoisted `body`
    # variable as `opaque_body` (it cannot see the `code` inside). Only the literal
    # form proves the contract is met, so the repetition buys a checkable guarantee.
    if exc.status == HTTPStatus.NOT_FOUND:
        return web.json_response({"error": str(exc), "code": exc.code}, status=404)
    if exc.status == HTTPStatus.CONFLICT:
        return web.json_response({"error": str(exc), "code": exc.code}, status=409)
    if exc.status == HTTPStatus.GONE:
        return web.json_response({"error": str(exc), "code": exc.code}, status=410)
    if exc.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
        return web.json_response({"error": str(exc), "code": exc.code}, status=413)
    return web.json_response({"error": str(exc), "code": exc.code}, status=400)


def guarded(handler: Handler) -> Handler:
    """Wrap a handler so validation errors become 4xx instead of 500s."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        try:
            return await handler(request)
        except (store.MeetingsPathError, BadRequest) as exc:
            return error_response(exc)

    return _wrapped


def route(handler: Handler) -> Handler:
    """The standard decorator stack for every Meetings handler."""
    return require_enabled(guarded(handler))


# ── gateway wiring ──────────────────────────────────────────────────────────


def sessions_of(request: web.Request) -> Any:
    """The gateway's shared SessionManager, or None when unavailable."""
    state = request.app.get("state")
    return getattr(state, "sessions", None) if state is not None else None


def hooks_of(request: web.Request) -> Any:
    """The gateway's HookManager, so agent turns traverse the PreToolUse gate.

    ``context_builder.hooks`` is where the dashboard chat path reads it from
    (``chat_runner.py``). When it is absent (a bare test app), None makes
    ``stream_and_collect`` fall back to its always-enforced deny checks, which
    still cover deny patterns and sensitive paths.
    """
    state = request.app.get("state")
    builder = getattr(state, "context_builder", None) if state is not None else None
    return getattr(builder, "hooks", None) if builder is not None else None


def data_root(request: web.Request) -> Any:
    """Test seam: an app-scoped data root override stashed on the aiohttp app.

    Production never sets this, so ``store``'s ``root=None`` default resolves the
    real ``app_data_dir("meetings")``.
    """
    return request.app.get("_meetings_data_root")
