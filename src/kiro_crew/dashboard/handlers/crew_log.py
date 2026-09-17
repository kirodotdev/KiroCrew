"""Read routes and the push frame for a session's CREW LOG.

Two reads and one push, which is the split RFC section 5 asks for: the backend
folds and cuts pages, the frontend renders and pages and never folds (NFR-2).

- ``GET /api/sessions/{id}/crew-log?from=&to=`` -- the entries in a seq range,
  with every ``ref`` on the page resolved (FR-4).
- ``GET /api/sessions/{id}/crew-log/projection/{name}`` -- one fold's value and
  the ``seq`` it was folded through (FR-5).
- a ``session_projection`` frame per projection whose value moves, pushed when a
  session's crew log grows.

Both routes are gated on the DASHBOARD OWNER, and that is this layer answering a
question the storage layer declines to: ``resolve`` makes no authorization claim
because it has no caller identity to derive one from, and says the first caller
with a permission model owns it. These routes are that caller. A crew log holds
the session's message BODIES, redacted but whole, so the audience is the one
person the conversation belongs to -- which is also why the push goes to owner
sockets rather than every authorized one, an app token among them. The gate is
``require_owner_dashboard_request`` inside each handler; the module also stands
behind ``guard_owner_surface_routes``, which refuses a private member's scoped
caller, so a route added here later is refused rather than open by omission.

The page read and the fold read differ in one deliberate way. A FOLD passes its
vocabulary to ``iter_from``, so an entry from a newer writer stops it instead of
skewing a total nobody can see is wrong. A PAGE passes none: it renders history
for a person, where an unfamiliar line is a missing detail rather than a
corrupted answer, and refusing the whole page for one line would hide the
history in front of it. That is the posture ``crew-log-core`` states for ``page``
and ``resolve``, applied to a range read.
The storage package is imported LAZILY here, on the first call that needs it,
never at module import. The crew log is an optional subsystem behind
``KIROCREW_CREW_LOG``, this module is reachable from the dashboard's boot
path, and a gateway launched with the flag unset must not pay to load a store it
will not read -- the same split the emitter keeps, and one a test pins from a
clean interpreter.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import (
    guard_owner_surface_routes,
    require_owner_dashboard_request,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from kiro_crew.crew_log import projection as crew_log_module
    from kiro_crew.crew_log.errors import LedgerError

logger = logging.getLogger(__name__)


def _crew_log() -> "crew_log_module":
    """The projection module, loaded the first time a call actually needs it."""
    from kiro_crew.crew_log import projection

    return projection


#: The frame a growing crew log pushes. The RFC's name, kept.
FRAME = "session_projection"

#: Distinct refs a single page resolves. Each resolution opens the cited unit and
#: walks to the span, so the work is bounded per request rather than left to
#: however many citations a page happens to carry; identical refs on one page are
#: resolved once. Past the budget the entry keeps its ``ref`` and carries no
#: resolution, and the page says how many it left, so a reader is never shown a
#: silently unresolved citation.
MAX_PAGE_REFS: Final[int] = 25

#: How long a growth signal waits for its neighbours. A turn appends a burst, and
#: folding once per burst rather than once per entry is the whole reason the
#: emitter reports a drained BATCH.
COALESCE_SECONDS: Final[float] = 0.25

#: Sessions whose fold state is kept between pushes. A bounded cache, because a
#: gateway sees many sessions and each state is retained for as long as it is
#: cheaper to continue than to re-read; an evicted session simply folds from the
#: start on its next growth.
MAX_CACHED_SESSIONS: Final[int] = 32


def _bad_request(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


def _seq_param(request: web.Request, name: str) -> int | None:
    """A positive-int query parameter, ``None`` when absent, or raise ValueError."""
    raw = request.query.get(name)
    if raw is None or raw == "":
        return None
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _span(request: web.Request) -> tuple[int, int]:
    """The requested ``from``/``to`` range, clamped to one page's worth.

    ``to`` absent means one default page from ``from``. A span wider than the
    store's page cap is CLAMPED rather than refused, and the response's
    ``next_from`` is what carries the rest: a caller asking for a whole log is
    asking a reasonable question, and the answer is pages.
    """
    from kiro_crew.crew_log.store import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT

    start = _seq_param(request, "from") or 1
    end = _seq_param(request, "to")
    if end is None:
        end = start + DEFAULT_PAGE_LIMIT - 1
    if end < start:
        raise ValueError("to must be at or after from")
    return start, min(end, start + MAX_PAGE_LIMIT - 1)


def _read_page(session_id: str, start: int, end: int) -> dict[str, Any]:
    """One range of entries with their refs resolved. Blocking; runs off the loop."""
    handle = _crew_log().open_session_log(session_id)
    if handle is None:
        return {
            "session_id": session_id,
            "exists": False,
            "from": start,
            "to": end,
            "last_seq": 0,
            "entries": [],
            "next_from": None,
            "refs_unresolved": 0,
        }
    # No vocabulary: a page renders history, so an unfamiliar line is shown
    # rather than made to refuse the lines around it.
    entries = [entry for entry in handle.iter_from(start) if entry.seq <= end]
    resolutions: dict[tuple[Any, ...], dict[str, Any]] = {}
    unresolved = 0
    rows: list[dict[str, Any]] = []
    for entry in entries:
        row = entry.to_dict()
        if entry.ref is not None:
            key = (entry.ref.unit, entry.ref.id, entry.ref.from_seq, entry.ref.to_seq)
            found = resolutions.get(key)
            if found is None:
                if len(resolutions) >= MAX_PAGE_REFS:
                    unresolved += 1
                    rows.append(row)
                    continue
                outcome = handle.resolve(entry.ref)
                found = {
                    "status": outcome.status,
                    "entries": len(outcome.entries),
                    "first_seq": outcome.entries[0].seq if outcome.entries else None,
                    "last_seq": outcome.entries[-1].seq if outcome.entries else None,
                }
                resolutions[key] = found
            # The citation's VERDICT and span, not its bytes: the cited lines are
            # a page of their own unit, which this route already serves, and
            # inlining them would make one page carry up to MAX_REF_SPAN lines
            # per entry.
            row["ref_resolution"] = dict(found)
        rows.append(row)
    last_seq = handle.last_seq
    return {
        "session_id": session_id,
        "exists": True,
        "from": start,
        "to": end,
        "last_seq": last_seq,
        "entries": rows,
        "next_from": end + 1 if end < last_seq else None,
        "refs_unresolved": unresolved,
    }


async def api_session_crew_log(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/crew-log -- entries in a seq range, refs resolved."""
    denied = await require_owner_dashboard_request(request, "session_crew_log.read")
    if denied is not None:
        return denied
    from kiro_crew.crew_log.errors import LedgerError

    session_id = request.match_info.get("id", "")
    try:
        start, end = _span(request)
    except ValueError as exc:
        return _bad_request(str(exc), "bad_range")
    try:
        payload = await asyncio.to_thread(_read_page, session_id, start, end)
    except LedgerError as exc:
        return _crew_log_refusal(exc)
    return web.json_response(payload)


async def api_session_crew_log_projection(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/crew-log/projection/{name} -- one fold and its seq."""
    denied = await require_owner_dashboard_request(request, "session_crew_log.projection")
    if denied is not None:
        return denied
    from kiro_crew.crew_log.errors import LedgerError

    projections = _crew_log()
    session_id = request.match_info.get("id", "")
    name = request.match_info.get("name", "")
    try:
        projections.require_name(name)
    except LedgerError as exc:
        return _bad_request(exc.message, "unknown_projection")
    try:
        result = await asyncio.to_thread(projections.read_projection, session_id, name)
    except LedgerError as exc:
        return _crew_log_refusal(exc)
    return web.json_response({"session_id": session_id, **result.to_dict()})


def _crew_log_refusal(exc: "LedgerError") -> web.Response:
    """A storage refusal as a response, keeping the code the caller can act on."""
    from kiro_crew.crew_log.errors import CODE_INVALID_ID, CODE_UNKNOWN_ENTRY_TYPE

    code = getattr(exc, "code", "") or "crew_log_error"
    if code == CODE_INVALID_ID:
        return _bad_request(exc.message, code)
    if code == CODE_UNKNOWN_ENTRY_TYPE:
        # The reader is older than the writer, and the fold refused rather than
        # answer with a total the unknown line may have changed. 409: the request
        # is well formed and the state of the resource is what blocks it.
        return web.json_response({"error": exc.message, "code": code}, status=409)
    logger.debug("crew log read refused (%s): %s", code, exc)
    return web.json_response({"error": exc.message, "code": code}, status=422)


# --------------------------------------------------------------------------- #
# The push
# --------------------------------------------------------------------------- #


class CrewLogPublisher:
    """Folds a grown crew log off the loop and pushes what moved.

    One instance per gateway, installed at startup. It holds each watched
    session's fold state, so a growth costs a read of the entries that arrived
    rather than a read of the whole file, which is what makes pushing all five
    projections on every batch affordable.

    A frame is sent only for a projection whose ``seq`` advanced. Re-sending an
    unchanged value would spend a socket write to tell a client nothing, and the
    client's own truncate-on-reconnect rule is stated in terms of that seq.
    """

    def __init__(self, state: Any) -> None:
        self._state = state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dirty: set[str] = set()
        self._bundles: "OrderedDict[str, Any]" = OrderedDict()
        self._scheduled = False

    # -- writer thread ------------------------------------------------------ #

    def notify(self, session_id: str) -> None:
        """A session's log grew. Called on the emitter's WRITER thread.

        Does no I/O and takes no lock of its own: it hands the id to the loop and
        returns, because everything this class does afterwards -- reading the
        file, rendering, broadcasting -- belongs to the loop that owns the
        sockets, and doing any of it here would put a reader's work inside the
        writer's pass.
        """
        loop = self._loop
        if loop is None or not session_id:
            return
        try:
            loop.call_soon_threadsafe(self._mark, session_id)
        except RuntimeError:
            # The loop is closed, which happens while the gateway shuts down. A
            # push nobody can receive is not worth reporting.
            logger.debug("crew log growth for %s arrived after the loop closed", session_id)

    # -- event loop --------------------------------------------------------- #

    def _mark(self, session_id: str) -> None:
        self._dirty.add(session_id)
        if self._scheduled:
            return
        self._scheduled = True
        loop = self._loop
        if loop is not None:
            loop.call_later(COALESCE_SECONDS, self._run)

    def _run(self) -> None:
        self._scheduled = False
        loop = self._loop
        if loop is None:
            return
        task = loop.create_task(self._flush())
        # Held only so the loop keeps a reference while it runs; the callback
        # drops it and reports a failure rather than letting it be swallowed.
        task.add_done_callback(self._finished)

    def _finished(self, task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.debug("crew log publish pass failed", exc_info=error)

    async def _flush(self) -> None:
        sessions = sorted(self._dirty)
        self._dirty.clear()
        if not sessions:
            return
        # Nobody watching means nothing to push, and folding for an empty room is
        # the one cost this is free to skip. The state stays cached and the next
        # growth folds from where it is, so skipping loses no accuracy.
        if not self._watchers():
            return
        from kiro_crew.crew_log.errors import LedgerError

        for session_id in sessions:
            try:
                await self._publish(session_id)
            except LedgerError as exc:
                logger.debug("crew log fold refused for %s: %s", session_id, exc)
            except Exception:  # pragma: no cover - a push must not kill the loop
                logger.debug("crew log publish failed for %s", session_id, exc_info=True)

    def _watchers(self) -> bool:
        """Whether a dashboard user has a socket open."""
        probe = getattr(self._state, "dashboard_user_ws_count", None)
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - a probe failure is not a verdict
            return False

    async def _publish(self, session_id: str) -> None:
        projections = _crew_log()
        before = self._bundles.get(session_id)
        bundle = await asyncio.to_thread(
            projections.fold_session,
            session_id,
            projections.PROJECTION_NAMES,
            since=before,
        )
        self._bundles[session_id] = bundle
        self._bundles.move_to_end(session_id)
        while len(self._bundles) > MAX_CACHED_SESSIONS:
            self._bundles.popitem(last=False)
        for name, checkpoint in bundle.checkpoints.items():
            previous = before.checkpoints.get(name) if before is not None else None
            if previous is not None and previous.last_seq == checkpoint.last_seq:
                continue
            if checkpoint.last_seq == 0:
                continue
            value = projections.projection_of(checkpoint)
            self._state.broadcast_ws_owners(FRAME, {"session_id": session_id, **value.to_dict()})

    # -- lifecycle ---------------------------------------------------------- #

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop


_publisher: CrewLogPublisher | None = None


def install_crew_log_publisher(state: Any) -> CrewLogPublisher:
    """Register the crew-log push with the emitter, once per process.

    Returns the live publisher, and re-binds it to the running loop when it
    already exists, so a gateway restarted inside one process pushes on the loop
    that is actually serving rather than a closed one. The emitter keeps the
    listener it was given: registering a second would fold each growth twice.
    """
    global _publisher
    loop = asyncio.get_running_loop()
    if _publisher is not None:
        _publisher.bind(loop)
        return _publisher
    from kiro_crew.crew_log import emit as crew_log_emit

    _publisher = CrewLogPublisher(state)
    _publisher.bind(loop)
    crew_log_emit.add_growth_listener(_publisher.notify)
    return _publisher


guard_owner_surface_routes(globals(), member_scoped=frozenset())
