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
from contextlib import asynccontextmanager
from functools import wraps
from http import HTTPStatus
from typing import Any, AsyncIterator, Awaitable, Callable, NamedTuple

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain.session import (
    MeetingSession,
    end_meeting_meta,
)
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.hooks import get_global_hook_store  # noqa: F401  (re-export for handlers)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.security import redact
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
        self.accepting_dispatches = False
        #: The session whose ingress is closed FOR INITIALIZATION, if any.
        #:
        #: ``accepting_dispatches`` alone cannot answer "should this line be
        #: refused?", because it is false for two opposite reasons: the meeting is
        #: stopping/reviewing/expired (the line has nowhere to go — refuse it, which
        #: is the gate issue #1981 added) or the meeting is STARTING and its agents
        #: are not ready yet (the line is wanted — hold it). Conflating them is what
        #: made a meeting refuse its own opening speech for ~46s.
        #:
        #: Stored as the SESSION rather than a bool so the state cannot outlive the
        #: identity it describes: every install/teardown clears it, so a stale flag
        #: can never make a later session buffer into a hold nobody drains.
        self.buffering_session: MeetingSession | None = None

    def get(self, meeting_id: str = "") -> MeetingSession | None:
        """The live session, optionally requiring it to be *meeting_id*'s."""
        session = self.session
        if session is None:
            return None
        if meeting_id and session.meeting_id != meeting_id:
            return None
        return session

    def get_for_dispatch(self, meeting_id: str) -> MeetingSession | None:
        """The matching session only while its transcript ingress is open."""
        return self.get(meeting_id) if self.accepting_dispatches else None

    def get_for_buffering(self, meeting_id: str) -> MeetingSession | None:
        """The matching session only while it is HOLDING speech through init.

        The narrow complement of :meth:`get_for_dispatch`: a caller that was just
        refused a direct fan-out asks this before answering 409. Returns None for
        every other closed-ingress reason, so stop / reviewing / expired keep
        refusing exactly as they did.
        """
        if self.accepting_dispatches:
            return None
        session = self.get(meeting_id)
        return session if session is not None and self.buffering_session is session else None

    @property
    def buffering_dispatches(self) -> bool:
        """Whether speech sent NOW would be held rather than refused.

        Reported next to ``accepting_dispatches`` on the meeting poll: the
        dashboard opens the microphone on "would speech land?", and during
        initialization the answer became yes-by-holding rather than no.
        """
        return self.buffering_session is not None and self.buffering_session is self.session

    def suspend_dispatches(
        self, session: MeetingSession | None = None, *, buffer_speech: bool = False
    ) -> None:
        """Close ingress for *session* without tearing down its agent queues.

        *buffer_speech* says WHY, and only the start path passes it: during agent
        initialization the meeting wants the speech it cannot yet deliver, so the
        dispatch endpoint holds the line instead of refusing it. Every other caller
        (stop, reviewing, an expired session, replacing an outgoing session) leaves
        it False and keeps the refusing behaviour.

        Defaults to False so a caller added later refuses by default rather than
        silently opting into a hold nothing drains.
        """
        if session is not None and self.session is session:
            self.accepting_dispatches = False
            self.buffering_session = session if buffer_speech else None

    def resume_dispatches(self, session: MeetingSession) -> None:
        """Open ingress only if *session* is still the installed session.

        Ends any hold in the same step: once direct fan-out is open, a line that
        stayed in the buffer would be delivered out of order behind live speech.
        The caller drains under this same lock acquisition.
        """
        if self.session is session:
            self.accepting_dispatches = True
            self.buffering_session = None

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
        self.accepting_dispatches = session is not None
        # Never inherited: the outgoing session's hold belongs to a meeting that is
        # gone, and leaving it set would make this session look like it were
        # buffering into a list no drain will ever reach.
        self.buffering_session = None

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
        self.accepting_dispatches = False
        self.buffering_session = None
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
        # Drop the session we DRAINED, not whatever is installed now.
        #
        # `flush_all` above is an await, and not every caller holds `START_LOCK` — the
        # expired-dispatch path in `agents.py` does not. So a concurrent start could
        # install a NEW session during the flush, and an unconditional `clear()` then
        # removed that new session instead: the meeting the user had just started went
        # live with nothing installed, and every subsequent line of its transcript was
        # dropped with a 409. Exactly the failure this method exists to prevent,
        # displaced by one meeting.
        #
        # `is`, not `==`: sessions are dataclasses and two for the same meeting id could
        # compare equal, which would let a replacement be cleared as if it were the
        # session that was drained. Identity is the question being asked.
        if self.session is previous:
            return self.clear()
        # A replacement is installed. Still cancel the outgoing session's queues — its
        # transcript was flushed a moment ago and its timers must not fire against a
        # session nobody holds — but leave the new one alone.
        if previous is not None:
            previous.cancel_all()
        return previous


ACTIVE = _ActiveMeeting()

#: Serializes the check-then-install of the active meeting.
#:
#: `handle_start_meeting` reads `ACTIVE.get()` to enforce "one meeting at a time",
#: then awaits (metadata IO, then the drain) before calling `set()`. Two starts
#: interleaving in that gap BOTH pass the check, and the second silently replaces
#: the first — whose transcript then fails to dispatch with a confusing 409. An
#: asyncio lock (not threading: this guards event-loop interleaving, not threads)
#: makes the read and the install one critical section.
START_LOCK = LoopBoundLock()

# Dispatch appends await worker-thread file IO, while lifecycle flushes can await
# slow agent turns. This separate admission lock protects only the short
# check/append/fan-out transaction. Lifecycle handlers close ingress under it and
# then release it before draining, so later speech is rejected promptly rather
# than waiting behind the slowest agent.
DISPATCH_LOCK = LoopBoundLock()


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
            return web.json_response(
                {"error": f"{k.APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
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


async def json_body(
    request: web.Request, *, required: bool = True, max_bytes: int = MAX_BODY_BYTES
) -> dict[str, Any]:
    """Parse and size-cap a JSON object body.

    A non-object body (list, string, number) is rejected rather than coerced —
    every handler indexes the result by key, so a list would surface as a 500.

    *max_bytes* exists for the ONE route whose body is a whole document rather than
    a short field (the minutes edit; see
    :data:`constants.MAX_MINUTES_BODY_BYTES` for the arithmetic). Raising it per
    route rather than raising the default keeps every other body small.
    """
    if request.content_length is not None and request.content_length > max_bytes:
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
    # FORBIDDEN was MISSING until the audio-import route needed it, and its absence
    # was a live bug rather than a gap: `store.contain` raises
    # `MeetingsPathError(..., status=403)` for a path that escapes the data root, and
    # without this branch that answered **400**. A containment violation reported as
    # "bad request" reads like a typo the caller can fix by retrying.
    if exc.status == HTTPStatus.FORBIDDEN:
        return web.json_response({"error": str(exc), "code": exc.code}, status=403)
    if exc.status == HTTPStatus.NOT_FOUND:
        return web.json_response({"error": str(exc), "code": exc.code}, status=404)
    if exc.status == HTTPStatus.CONFLICT:
        return web.json_response({"error": str(exc), "code": exc.code}, status=409)
    if exc.status == HTTPStatus.GONE:
        return web.json_response({"error": str(exc), "code": exc.code}, status=410)
    if exc.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
        return web.json_response({"error": str(exc), "code": exc.code}, status=413)
    # 502/503 both mean "your request was fine, the thing behind it was not", which is
    # a distinction the client acts on: retry later, versus fix the configuration.
    if exc.status == HTTPStatus.BAD_GATEWAY:
        return web.json_response({"error": str(exc), "code": exc.code}, status=502)
    if exc.status == HTTPStatus.SERVICE_UNAVAILABLE:
        return web.json_response({"error": str(exc), "code": exc.code}, status=503)
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


# ── dispatch admission, for every transcript producer ───────────────────────
#
# Two producers feed lines into a meeting — the browser's speech stream and
# broadcast bar (``agents.handle_dispatch_text``) and an imported recording
# (``audio_import.handle_import_audio``) — and both must obey the same two rules:
#
# * The live-session check, the transcript append, and the synchronous queue
#   fan-out are ONE transaction under ``DISPATCH_LOCK``, so a concurrent stop
#   followed by deletion cannot remove the meeting while a producer is awaiting
#   disk IO and then have the append recreate an orphan directory.
# * The expiry branch has SIDE EFFECTS — close admission, drain the queues, mark
#   the meeting ended on disk — and a second copy of those is a second thing that
#   has to stay correct.
#
# Extracted here rather than copied into each producer for exactly those reasons.


class Admission(NamedTuple):
    """The verdict :func:`dispatch_admission` yields while ``DISPATCH_LOCK`` is held.

    ``holding`` says which kind of admission this is: False is a live session whose
    ingress is open (fan out normally); True is a session still INITIALIZING its
    agents, admitted only because the producer opted into the hold — the line is
    wanted, so it is appended to the transcript and buffered for the drain instead
    of refused (issue #4610).
    """

    session: MeetingSession
    holding: bool


@asynccontextmanager
async def dispatch_admission(
    request: web.Request,
    meeting_id: str,
    *,
    require_session: MeetingSession | None = None,
    hold_during_init: bool = False,
) -> AsyncIterator[Admission]:
    """Admit one transcript line: yield the live session, holding ``DISPATCH_LOCK``.

    The caller's body IS the admission transaction — it runs while the lock is
    held, so its append and fan-out cannot race a lifecycle transition. No live
    session raises 409; an expired one closes admission promptly (so a later
    request fails fast instead of waiting behind agent IO), then drains and marks
    the meeting ended under ``START_LOCK`` — which still serializes the actual
    teardown with start/stop/delete — and raises 410. ``guarded`` turns both into
    the standard error bodies.

    ``hold_during_init`` opts the producer into the initialization hold: when
    ingress is closed because the meeting is STARTING (not stopping/reviewing/
    expired), the starting session is yielded with ``holding=True`` instead of
    raising 409, and the caller buffers the line for the drain. Off by default so
    a producer added later refuses rather than silently buffering; it is also
    mutually exclusive with ``require_session`` — a hold admits into a session
    whose agents never saw the producer's earlier lines, which is exactly the
    identity confusion ``require_session`` exists to refuse.

    ``require_session`` pins admission to a SPECIFIC session object. A meeting id
    is a name, not an identity: a meeting stopped and recreated with the same id
    mid-operation installs a NEW session under the OLD name, and a producer that
    was admitted against the old one must not write into its replacement (an
    imported recording landing in a meeting it was never part of). Compared with
    ``is`` — sessions are dataclasses and two for the same meeting id can compare
    equal; identity is the question being asked. The check runs BEFORE the expiry
    branch on purpose: the expiry branch has lifecycle side effects (it ends the
    live meeting on disk), and a producer holding a stale identity has no business
    tearing down a session it was never admitted to.
    """
    expired: MeetingSession | None = None
    async with DISPATCH_LOCK:
        session = ACTIVE.get_for_dispatch(meeting_id)
        if session is None:
            # The identity question FIRST, even while nothing is dispatchable: a
            # meeting stopped and recreated under the same id installs a NEW
            # session that spends its first moments initializing, and during that
            # window ``get_for_dispatch`` answers None. Answering 409
            # ``no_active_meeting`` there tells a ``require_session`` producer to
            # retry into the replacement — the exact write ``require_session``
            # exists to refuse. ``ACTIVE.get`` sees the initializing session too,
            # so the honest 410 is available before the 409 fallback.
            if require_session is not None:
                current = ACTIVE.get(meeting_id)
                if current is not None and current is not require_session:
                    raise BadRequest(
                        "the meeting session changed during the import",
                        status=410,
                        code="meeting_session_replaced",
                    )
            starting = (
                ACTIVE.get_for_buffering(meeting_id)
                if hold_during_init and require_session is None
                else None
            )
            if starting is None:
                raise BadRequest("no active meeting", status=409, code="no_active_meeting")
            yield Admission(starting, True)
            return
        if require_session is not None and session is not require_session:
            raise BadRequest(
                "the meeting session changed during the import",
                status=410,
                code="meeting_session_replaced",
            )
        if session.expired:
            ACTIVE.suspend_dispatches(session)
            expired = session
        else:
            yield Admission(session, False)
    if expired is None:
        return
    async with START_LOCK:
        if ACTIVE.get(meeting_id) is expired:
            # Drain, not cancel: a long meeting whose next line arrives after the
            # session lapsed still has whatever was queued when it went quiet, and
            # that transcript is exactly what the final notes would otherwise omit.
            await ACTIVE.drain_and_clear()
            # Then mark it ended on disk, for the same reason gateway shutdown does
            # (`routes/__init__._on_cleanup`): the live session is gone, so leaving
            # the metadata saying `active` makes the dashboard show Live and keep
            # recording into 409s. `ended` is both honest and recoverable — it is
            # the one status the user can Restart from.
            await asyncio.to_thread(end_meeting_meta, meeting_id, data_root(request))
    raise BadRequest("meeting session expired", status=410, code="meeting_session_expired")


async def dispatch_line(
    request: web.Request,
    meeting_id: str,
    text: str,
    source: str,
    *,
    chat: bool = False,
    require_session: MeetingSession | None = None,
    hold_during_init: bool = False,
) -> tuple[dict[str, Any], int, str]:
    """Persist one line, then fan it out — the whole producer transaction.

    Persist-before-fan-out is the data-integrity boundary: an accepted agent line
    cannot be absent from the transcript, whether it was spoken, typed, or
    imported. Redaction happens here, at the single entry point, so everything in
    ``transcript.jsonl`` has been scrubbed exactly once. Returns
    ``(segment, queues_accepted, line_as_dispatched)``; a full transcript raises
    413 rather than truncating an accepted row. ``require_session`` and
    ``hold_during_init`` are forwarded to :func:`dispatch_admission` — a
    multi-line producer passes the session it was admitted against so a same-id
    replacement cannot receive its lines, and only the live/typed producer opts
    into the initialization hold. A held line is appended AT ARRIVAL exactly like
    a live one, so the durable record stays in spoken order and complete even
    when the hold later overflows — the overflow then costs the agents some
    context, never the user their transcript. It reports ``queues_accepted`` of
    0, the same answer as a live dispatch that reached nobody: the line is in the
    durable transcript either way, so ``dispatched: 0`` is already the whole
    answer to "did this land with an agent yet".
    """
    async with dispatch_admission(
        request, meeting_id, require_session=require_session, hold_during_init=hold_during_init
    ) as admitted:
        transcript_text = redact(text)
        # The append creates its parent directory when needed — which is why it must
        # stay inside the admission transaction (see `dispatch_admission`).
        segment = await asyncio.to_thread(
            store.append_transcript, meeting_id, transcript_text, source, data_root(request)
        )
        if segment is None:
            raise BadRequest(
                "meeting transcript is too large", status=413, code="transcript_too_large"
            )
        line = f"{k.CHAT_PREFIX} {transcript_text}" if chat else transcript_text
        if admitted.holding:
            if not admitted.session.buffer_during_init(line):
                logger.warning(
                    "meetings: init hold for %r is full at %d line(s); "
                    "dropped the oldest and will mark the gap on drain",
                    meeting_id,
                    k.MAX_INIT_BUFFER_LINES,
                )
            accepted = 0
        else:
            accepted = admitted.session.broadcast(line)
    return segment, accepted, line


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
