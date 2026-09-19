"""Read routes and the push frame for a session's CREW LOG.

Two reads and one push, which is the split RFC section 5 asks for: the backend
folds and cuts pages, the frontend renders and pages and never folds (NFR-2).

- ``GET /api/sessions/{id}/crew-log?from=&to=`` -- the entries in a seq range,
  with up to ``MAX_PAGE_REFS`` of the page's refs resolved and the rest reported
  in ``refs_unresolved`` (FR-4).
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
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web

from kiro_crew.constants import env_flag_enabled
from kiro_crew.dashboard.handlers._shared import (
    guard_owner_surface_routes,
    require_owner_dashboard_request,
)

#: The variable that switches the crew log on, spelled here rather than read from
#: the emitter's ``CREW_LOG_ENV``. This module sits on the gateway's boot path and
#: importing that module to learn whether it is wanted is the very cost the flag
#: exists to avoid. A test pins this string against the emitter's own constant, so
#: the two cannot drift apart unnoticed.
CREW_LOG_ENV: Final[str] = "KIROCREW_CREW_LOG"

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from kiro_crew.crew_log.errors import CrewLogError

logger = logging.getLogger(__name__)


def _crew_log() -> ModuleType:
    """The projection module, loaded the first time a call actually needs it."""
    from kiro_crew.crew_log import projection

    return projection


def _crew_log_read() -> ModuleType:
    """The shared read module, loaded the first time a call actually needs it.

    Lazily for the same reason as :func:`_crew_log`, and it has to be a function
    rather than a module-level import for a reason a test enforces: this module is
    on the gateway's boot path and a clean interpreter importing it must load NO
    ``kiro_crew.crew_log`` submodule at all.
    """
    from kiro_crew.crew_log import read

    return read


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
    """One range of entries with their refs resolved. Blocking; runs off the loop.

    The implementation lives in :func:`kiro_crew.crew_log.read.read_page`, because
    the ``kirocrew-crew-log`` MCP server's proxy leg reads the same pages and a
    second copy is how two callers come to disagree about what a page is. This
    stays as the named seam the route calls, so the lazy load happens on the first
    read rather than at import.
    """
    return _crew_log_read().read_page(session_id, start, end)


def _unit_id(request: web.Request, given: str) -> tuple[str, bool]:
    """The crew-log UNIT a read addresses, and whether the resolver named it.

    A session's crew log is keyed by the ACP SESSION ID the turn path holds, and a
    dashboard caller holds neither: a chat surface knows its SLOT key, and the ACP
    id is not on any payload it reads (deliberately -- it is an internal identity,
    and putting it on the wire to let a client rewrite it into a path would widen
    what a client is trusted with). So a slot key is resolved here, through the one
    module that answers this question, rather than by the caller guessing.

    The resolver's answer is preferred when it has one, and the id is used verbatim
    otherwise: an ACP id is not a session KEY, so the registry lookup misses and
    answers ``UNKNOWN`` for one, which is what keeps a unit-id-addressed read
    working unchanged. Two callers on main are of that kind -- the
    ``kirocrew-crew-log`` MCP server reads a unit by id through the unit-keyed door,
    and the ``session_projection`` frame carries the unit it folded as
    ``session_id``, so anything taking an id out of a frame addresses by unit id.
    It is a registry read with no disk in it, so it stays on the loop while the
    read itself goes to a thread.

    An unresolvable key -- a slot that never ran a turn, one whose ACP session was
    torn down -- keeps the given id and reads back an empty fold. What this function
    ALSO reports is whether the resolver answered, because that is the difference
    between "this unit holds no entries" and "no unit is addressable for this id
    right now", and only the caller of the second kind can be told the truth. A
    slot whose session was reset keeps its record on disk under the retired ACP id;
    telling its reader "nothing recorded for this session" would be false.

    This does NOT fall back to the persisted session map to find that retired id,
    for two reasons that are each decisive. ``SessionMap.get`` repairs or removes an
    entry it judges stale, so consulting it would make a panel read mutate session
    state -- the exact reason ``crew_log/resolve.py`` documents for not touching it.
    And a retired unit belongs to a different session from the one this slot serves:
    presenting its totals here would imply a whole-life figure that needs the lineage
    pointer (``session/opened.data.previous``) and a fold that follows it, and this
    module has neither. So the honest move is to say what is addressable, not to
    guess.
    """
    if not given:
        return given, False
    state = request.app.get("state") if hasattr(request.app, "get") else None
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return given, False
    from kiro_crew.crew_log.resolve import unit_for_session_key

    resolved = unit_for_session_key(sessions, _session_key_of(state, given))
    return (resolved or given), bool(resolved)


def _session_key_of(state: Any, given: str) -> str:
    """The SESSION key a slot's turns run on, or *given* when it names no slot.

    A channel-born slot's turns run on the channel's own session, whose key it
    carries in ``linked_session_key`` (``slack:<ts>``) -- so the ACP provider is
    registered under THAT key, not under the slot's. The resolver does an exact
    registry lookup and its one retry is the ``dashboard:`` form, so a panel that
    sent the bare slot key would miss the provider and fold an empty record for
    every channel-linked session, with nothing to correct it: the mapping is
    stable, so it reads empty forever.

    ``effective_session_key`` is the function that owns this mapping, and its
    docstring says to use it wherever a slot's SESSION is addressed, which is what
    a crew-log read does. It is a pure attribute read -- no disk, no session-state
    mutation -- so it keeps the invariant that makes this path safe to call from a
    read. An id naming no live slot is returned unchanged, which is what an ACP
    unit id is.
    """
    get_slot = getattr(state, "get_slot", None)
    if not callable(get_slot):
        return given
    slot = get_slot(given.removeprefix("dashboard:"))
    if slot is None:
        return given
    from kiro_crew.dashboard.chat_utils import effective_session_key

    key = effective_session_key(slot)
    # A STRING or nothing: the registry lookup is keyed by one, and a partially
    # built app -- or a double standing in for one -- can answer this with an
    # object that is merely truthy. Handing that to the resolver would turn an
    # exact lookup into a guaranteed miss, which reads as "this session has no
    # log" rather than as the wiring problem it is.
    return key if isinstance(key, str) and key else given


async def api_session_crew_log(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/crew-log -- entries in a seq range, refs resolved."""
    denied = await require_owner_dashboard_request(request, "session_crew_log.read")
    if denied is not None:
        return denied
    from kiro_crew.crew_log.errors import CrewLogError

    session_id = request.match_info.get("id", "")
    try:
        start, end = _span(request)
    except ValueError as exc:
        return _bad_request(str(exc), "bad_range")
    try:
        unit_id, _ = _unit_id(request, session_id)
        payload = await asyncio.to_thread(_read_page, unit_id, start, end)
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    # The page read builds its payload around the unit it opened, so answering
    # with that would hand a slot-addressed caller back an ACP id it never sent --
    # both a broken comparison and an internal identity on the wire. Same rule as
    # the fold read below: name what the CALLER asked about.
    payload["session_id"] = session_id
    return web.json_response(payload)


async def api_session_crew_log_projection(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/crew-log/projection/{name} -- one fold and its seq."""
    denied = await require_owner_dashboard_request(request, "session_crew_log.projection")
    if denied is not None:
        return denied
    from kiro_crew.crew_log.errors import CrewLogError

    projections = _crew_log()
    session_id = request.match_info.get("id", "")
    name = request.match_info.get("name", "")
    try:
        projections.require_name(name)
    except CrewLogError as exc:
        return _bad_request(exc.message, "unknown_projection")
    unit_id, _ = _unit_id(request, session_id)
    try:
        result = await asyncio.to_thread(projections.read_projection, unit_id, name)
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    # ``session_id`` is what the CALLER asked about, not the unit the fold read:
    # a client polling by slot key compares this against the id it sent, and
    # answering with the resolved ACP id would both break that comparison and put
    # an internal identity on the wire. It answers neither ``resolved`` nor
    # ``writes_drained``: those exist for the surface that shows a reader five folds
    # at once, this route has no caller that reads them, and the settle they need
    # is a wait charged to every request. The resolution itself stays -- a slot-key
    # read of this route folds the session's unit like any other.
    return web.json_response({"session_id": session_id, **result.to_dict()})


async def api_session_crew_log_projections(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/crew-log/projections -- every fold from one read.

    A panel shows the five folds TOGETHER, and asking for them one route at a time
    means five independent resolutions of the same session: if the slot's ACP
    session is replaced while those are in flight, some answers describe the unit
    that is going away and some the one arriving, and the reader sees a mix with no
    way to tell. Resolving once and folding once removes that window rather than
    narrowing it, and it costs less than the five reads it replaces -- one pass over
    one file instead of five opens of it.

    Each fold still carries its OWN ``seq``, because they genuinely differ: an
    entry advances the folds it belongs to and leaves the others where they were.
    What this route guarantees is that all five came from the same file at the same
    moment, which is the part a caller cannot reconstruct for itself.
    """
    denied = await require_owner_dashboard_request(request, "session_crew_log.projections")
    if denied is not None:
        return denied
    from kiro_crew.crew_log.errors import CrewLogError

    projections = _crew_log()
    session_id = request.match_info.get("id", "")
    unit_id, resolved = _unit_id(request, session_id)
    drained = await asyncio.to_thread(_settle_writes)
    try:
        bundle = await asyncio.to_thread(
            projections.fold_session, unit_id, projections.PROJECTION_NAMES
        )
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    folded = {
        name: projections.projection_of(checkpoint).to_dict()
        for name, checkpoint in bundle.checkpoints.items()
    }
    # ``session_id`` names what the CALLER asked about, the same rule the two older
    # reads follow: a client polling by slot key compares this against the id it
    # sent, and answering with the resolved ACP id would break that comparison and
    # put an internal identity on the wire.
    return web.json_response(
        {
            "session_id": session_id,
            "projections": folded,
            "resolved": resolved,
            "writes_drained": drained,
        }
    )


#: How long a fold read waits for the writer to owe nothing before folding anyway.
#: The emitter hands an append to a queue and returns, so a turn can END with its
#: last entries still owed -- and a refresh triggered by that turn's end would then
#: fold a file the turn has not finished writing and present the result as current.
#: Short on purpose: a reader is waiting, and a read that misses the drain says so
#: rather than blocking until it cannot.
_SETTLE_SECONDS: Final[float] = 0.5


def _settle_writes() -> bool:
    """Whether the crew-log writer owes nothing, after waiting briefly for that.

    ``emit.flush`` is the emitter's own answer for "a caller that must read the
    file it just wrote", and it is global rather than per session DELIBERATELY: a
    batch the writer has already claimed is absent from the per-session queue and
    cannot be seen there (``_inline_claimable_locked`` documents exactly this), so
    a predicate scoped to one session would report quiet in the one case that
    matters. False is not an error -- it means the fold may be behind the record,
    and the answer carries that so a reader is not shown a stale value dressed as
    a current one.

    Imported here rather than at module scope: a gateway that never turned the
    crew log on does not import the emitter, and a read route is not the place to
    change that. With the flag off there is nothing queued, so this answers True
    without waiting.
    """
    from kiro_crew.crew_log import emit

    return emit.flush(timeout=_SETTLE_SECONDS)


def _crew_log_refusal(exc: "CrewLogError") -> web.Response:
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
# The agent's door: unit-keyed reads the kirocrew-crew-log MCP server proxies
# --------------------------------------------------------------------------- #
#
# A SECOND prefix rather than a second gate on the two routes above, and the
# distinction is the authorization model, not the bytes. Those routes are keyed by
# the session id the SPA already holds and are gated on the owner's cookie; these
# are keyed by a crew log UNIT, are reachable over the internal-secret transport,
# and scope every read against the session key the proxy forwards. One handler
# serving both would be one gate that has to be right for two callers whose
# identity arrives from different places, which is how a read widens by accident.
# The page and the fold themselves are the SAME implementations -- ``_read_page``
# and ``projection.read_projection`` -- so the two doors cannot answer differently.

#: The component name this module's internal door recognizes on
#: ``X-Internal-Caller``. Spelled here rather than imported from
#: :mod:`kiro_crew.mcp_crew_log`, because that module is an MCP stdio server and
#: this one is on the gateway's boot path; a test pins the two together so they
#: cannot drift.
CREW_LOG_MCP_CALLER: Final[str] = "kirocrew-crew-log"

#: How to switch the crew log on, quoted in the refusal a disabled read earns. An
#: agent that reads ``crew_log_disabled`` should not have to be told separately.
CREW_LOG_ENABLE_HINT: Final[str] = (
    f"set {CREW_LOG_ENV}=1 in ~/.kiro/crew/.env and restart the gateway"
)


def _forbidden(reason: str) -> web.Response:
    """A 403 in this module's own vocabulary, with the reason the agent can act on."""
    return web.json_response({"error": reason, "code": "forbidden"}, status=403)


def _disabled() -> web.Response:
    """The refusal a read earns while the crew log is switched off.

    422 rather than 404: the route exists and the request is well formed, and what
    blocks it is the state of the subsystem. The agent learns the flag state from
    THIS, which is why the MCP server registers its tools whether or not the flag
    is on -- a missing tool would read as a Kiro Crew that cannot do this at all.
    """
    return web.json_response(
        {
            "error": f"the crew log is switched off; {CREW_LOG_ENABLE_HINT}",
            "code": "crew_log_disabled",
        },
        status=422,
    )


def _caller_session_key(request: web.Request) -> str:
    """The session key the internal proxy forwarded, or ``""``.

    The agent cannot forge it: it does not build the request, and the MCP request
    helpers set it from the calling session's own strictly-resolved context rather
    than from tool arguments.
    """
    return (request.headers.get("X-Session-Key") or "").strip()


def _owner_session_refusal(request: web.Request, session_key: str) -> str:
    """``""`` when *session_key* is the owner at a dashboard tab, else the reason.

    Three conditions, and each rules out a caller class that must not read another
    session's crew log:

    * a ``dashboard:`` key, so a headless or channel-bound caller is refused by
      the namespace it carries rather than by a list of what it is not;
    * no app owns it, derived by :func:`~kiro_crew.dashboard.token_auth.derive_caller_app`
      against the server-side registries -- an app agent granted this server
      arrives on the same transport as the person and is otherwise
      indistinguishable;
    * the session keeps persistent memory, so an incognito or temporary session is
      refused. Those sessions are the ones the person asked to leave no trace, and
      a read that hands one every other session's history is the opposite of that.
    """
    from kiro_crew.dashboard.token_auth import derive_caller_app

    if not session_key:
        return (
            "this read needs a session identity and the request carried none; "
            "only the owner's own dashboard session may read another unit"
        )
    if not session_key.startswith("dashboard:"):
        return (
            f"{session_key.split(':', 1)[0]}: sessions may read their own unit but not "
            "another's; that read is the owner's dashboard session only"
        )
    state = request.app.get("state")
    slots = getattr(state, "_slots", None)
    lookup = getattr(slots, "get", None) if slots is not None else None
    slot = lookup(session_key.split(":", 1)[1]) if lookup is not None else None
    if slot is None:
        return (
            "the calling session names no live dashboard slot, so it cannot be "
            "placed as the owner's own tab"
        )
    jobs = getattr(getattr(state, "crons", None), "_jobs", None)
    subagents = getattr(getattr(state, "subagents", None), "_agents", None)
    if derive_caller_app(slots, session_key, jobs, subagents):
        return "an app-owned session may read its own unit but not another's"
    if getattr(slot, "is_restricted", False):
        return (
            "an incognito or temporary session may not read another session's "
            "crew log; that session is meant to leave and learn nothing"
        )
    return ""


async def _authorize_crew_log_read(
    request: web.Request, operation: str, *, self_scope: bool
) -> web.Response | None:
    """``None`` when the caller may read, else the refusal. Internal transport only.

    The header check below is LOAD-BEARING, not a re-assert of the transport. Being
    on ``_STRICT_INTERNAL_API_PATHS`` does not mean the secret was checked: for a
    LOOPBACK request carrying no ``X-Internal-Secret``, ``token_auth_middleware``
    falls through to ordinary cookie auth and calls this handler on success
    (``token_auth.py``, "No secret header (browser request)"). Strict membership
    decides only the NON-loopback case -- hard deny, where a mixed path would get
    the cookie fall-through -- and with ``local_only=False`` a strict path is
    reclassified mixed anyway. So a same-machine tab, and a forwarded one once
    remote access is on, both arrive here with a valid cookie and no secret; this
    branch is the only thing that refuses them.

    Refused rather than admitted because the browser already has its own door for
    the only log it should read: the cookie-only ``/api/sessions/{id}/crew-log``
    pair, which this prefix deliberately does not cover. Admitting a cookie here
    would make the owner-dashboard test below a second authorization path into
    ANOTHER session's recorded history, reachable from any authenticated page.

    The rule the internal arm applies: a read of the caller's OWN unit needs only
    its forwarded session identity (the gate ``session_ledger_read`` uses), and any
    wider read needs the owner at a dashboard tab.

    Every read is audited under the operation name the caller passes, which is the
    same name the browser routes use, so an operator querying
    ``session_crew_log.read`` sees every read of a page regardless of which door it
    came through. EVERY denial too, including the two that refuse a caller before
    its identity is settled -- those are the ones an operator most wants, because a
    secret-less request at an MCP-only route and a request naming another component
    are both the shape of an attempted boundary crossing, while a refused session
    class is ordinary. ``request_origin`` is resolved FIRST so they can be recorded:
    it returns ``("dashboard", "dashboard")`` for a request with no secret and
    clamps an unrecognized component to ``"unknown-internal"``, so reading it early
    cannot let an unauthenticated caller name itself into the audit log.
    """
    from kiro_crew.dashboard.token_auth import request_origin

    source, caller = request_origin(request, what="crew log read", log=logger)
    if request.headers.get("X-Internal-Secret") is None:
        refusal = (
            "these reads are internal-transport only; a browser reads its own "
            "crew log through /api/sessions/{id}/crew-log"
        )
        _audit_crew_log_read(caller, source, operation, "denied", refusal)
        return _forbidden(refusal)
    if caller != CREW_LOG_MCP_CALLER:
        refusal = f"this route serves {CREW_LOG_MCP_CALLER}; the request named {caller!r}"
        _audit_crew_log_read(caller, source, operation, "denied", refusal)
        return _forbidden(refusal)
    session_key = _caller_session_key(request)
    if not self_scope:
        refusal = _owner_session_refusal(request, session_key)
        if refusal:
            _audit_crew_log_read(caller, source, operation, "denied", refusal)
            return _forbidden(refusal)
    elif not session_key:
        refusal = "a read of your own unit needs a session identity and none arrived"
        _audit_crew_log_read(caller, source, operation, "denied", refusal)
        return _forbidden(refusal)
    _audit_crew_log_read(caller, source, operation, "granted", "")
    return None


def _audit_crew_log_read(
    caller: str, source: str, operation: str, outcome: str, error: str
) -> None:
    """SEL for one internal read, under the SAME action name the browser uses.

    The browser arm is audited inside ``require_owner_dashboard_request``, which
    records a DENIAL only. This arm records both, because a granted read over the
    internal transport is the event an operator asked about -- "what did the agent
    read" has no other answer -- while a granted browser read is the person looking
    at their own panel.
    """
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source=source,
            error=error,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _unit_param(request: web.Request) -> str:
    """The unit id from the path, already percent-decoded by the router."""
    return (request.match_info.get("unit") or "").strip()


async def api_crew_log_sessions(request: web.Request) -> web.Response:
    """GET /api/crew-log/sessions -- one row per session crew log on this host."""
    denied = await _authorize_crew_log_read(request, "session_crew_log.list", self_scope=False)
    if denied is not None:
        return denied
    if not env_flag_enabled(CREW_LOG_ENV):
        return _disabled()
    from kiro_crew.crew_log.errors import CrewLogError

    try:
        limit = int(request.query.get("limit") or 50)
        active_within_secs = int(request.query.get("active_within_secs") or 0)
    except ValueError as exc:
        return _bad_request(str(exc), "bad_range")
    if limit < 1 or active_within_secs < 0:
        return _bad_request("limit must be at least 1 and a window cannot be negative", "bad_range")
    try:
        payload = await asyncio.to_thread(
            _crew_log_read().list_session_units,
            slot_contains=request.query.get("slot_contains", "") or "",
            active_within_ms=active_within_secs * 1000,
            with_type_counts=request.query.get("with_type_counts") in ("1", "true", "True"),
            limit=limit,
        )
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    return web.json_response(payload)


async def api_crew_log_resolve(request: web.Request) -> web.Response:
    """GET /api/crew-log/resolve?key= -- the unit a slot or session key is landing in."""
    key = (request.query.get("key") or "").strip()
    # Authorize BEFORE answering the request's shape. An empty key is a 400, and
    # returning it first tells a caller the route refuses it for a reason other than
    # being unwelcome, and leaves no audit row for a probe that never got past the
    # gate. ``self_scope`` needs ``key`` read, which is why it is parsed above -- but
    # an empty key equals no caller's session key, so it resolves ``self_scope`` to
    # False and the request is held to the owner test, which is the stricter side.
    self_scope = bool(key) and key == _caller_session_key(request)
    denied = await _authorize_crew_log_read(
        request, "session_crew_log.resolve", self_scope=self_scope
    )
    if denied is not None:
        return denied
    if not key:
        return _bad_request("key is required", "unresolvable_key")
    if not env_flag_enabled(CREW_LOG_ENV):
        return _disabled()
    from kiro_crew.crew_log.resolve import unit_for_session_key

    state = request.app.get("state")
    unit = unit_for_session_key(getattr(state, "sessions", None), key)
    if not unit:
        # The resolver's own vocabulary: a key with no LIVE ACP session is
        # unresolvable rather than unknown, and the difference matters to the
        # caller -- a slot that has never run a turn, or whose session was torn
        # down, has no unit to name and will have a different one next turn.
        return web.json_response(
            {
                "error": (
                    f"{key!r} has no live ACP session, so no crew log unit is "
                    "receiving its work right now"
                ),
                "code": "unresolvable_key",
            },
            status=404,
        )
    return web.json_response({"key": key, "unit": unit})


async def api_crew_log_unit_page(request: web.Request) -> web.Response:
    """GET /api/crew-log/units/{unit}/page -- entries in a seq range, refs resolved."""
    unit = _unit_param(request)
    denied = await _authorize_crew_log_read(
        request, "session_crew_log.read", self_scope=_reads_own_unit(request, unit)
    )
    if denied is not None:
        return denied
    if not env_flag_enabled(CREW_LOG_ENV):
        return _disabled()
    from kiro_crew.crew_log.errors import CrewLogError

    try:
        start, end = _span(request)
    except ValueError as exc:
        return _bad_request(str(exc), "bad_range")
    try:
        payload = await asyncio.to_thread(_read_page, unit, start, end)
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    if not payload.get("exists"):
        return web.json_response(
            {"error": f"no session crew log for {unit!r}", "code": "unknown_unit"}, status=404
        )
    return web.json_response(payload)


async def api_crew_log_unit_projection(request: web.Request) -> web.Response:
    """GET /api/crew-log/units/{unit}/projection/{name} -- one fold and its seq."""
    unit = _unit_param(request)
    denied = await _authorize_crew_log_read(
        request, "session_crew_log.projection", self_scope=_reads_own_unit(request, unit)
    )
    if denied is not None:
        return denied
    if not env_flag_enabled(CREW_LOG_ENV):
        return _disabled()
    from kiro_crew.crew_log.errors import CrewLogError

    projections = _crew_log()
    name = request.match_info.get("name", "")
    try:
        projections.require_name(name)
    except CrewLogError as exc:
        return _bad_request(exc.message, "unknown_projection")
    try:
        result = await asyncio.to_thread(projections.read_projection, unit, name)
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    return web.json_response({"session_id": unit, **result.to_dict()})


def _reads_own_unit(request: web.Request, unit: str) -> bool:
    """Whether *unit* is the one the CALLING session's own work is landing in.

    Resolved server-side from the forwarded session key, never from the request:
    a caller that could name its own scope could name someone else's. An empty
    unit, or a key with no live ACP session, answers False -- so the read falls to
    the owner rule rather than being waved through as "self".
    """
    if not unit:
        return False
    from kiro_crew.crew_log.resolve import unit_for_session_key

    session_key = _caller_session_key(request)
    if not session_key:
        return False
    state = request.app.get("state")
    return unit_for_session_key(getattr(state, "sessions", None), session_key) == unit


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
        # A flush pass runs to completion before the next one starts. Without
        # this, a growth arriving during a slow fold would schedule a second
        # overlapping pass, and two ``_publish`` for one session would share the
        # same ``before`` bundle and race the cache write, so an older seq could
        # land and be broadcast last. When a pass finishes with more work marked,
        # it schedules the next pass itself.
        self._flushing = False

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
        # A pass is already running. It will re-schedule when it finishes if the
        # dirty set is non-empty, so starting a second, overlapping pass here is
        # exactly the race that would let an older seq land last.
        if self._flushing:
            return
        self._flushing = True
        task = loop.create_task(self._flush())
        # Held only so the loop keeps a reference while it runs; the callback
        # drops it and reports a failure rather than letting it be swallowed.
        task.add_done_callback(self._finished)

    def _finished(self, task: "asyncio.Task[None]") -> None:
        self._flushing = False
        # A growth that arrived mid-flush left the dirty set non-empty and found
        # ``_scheduled`` still true (so it did not re-arm the timer); pick it up
        # now that this pass is done, on the next coalesce tick.
        if self._dirty and not self._scheduled:
            self._scheduled = True
            loop = self._loop
            if loop is not None:
                loop.call_later(COALESCE_SECONDS, self._run)
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
        from kiro_crew.crew_log.errors import CrewLogError

        for session_id in sessions:
            try:
                await self._publish(session_id)
            except CrewLogError as exc:
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
        # A seq is only comparable WITHIN one file. ``fold_session`` refuses to
        # reuse a bundle whose origin does not match the file and rebuilds from the
        # start, so a log removed and recreated can come back with the same
        # terminal seq and entirely different values. Comparing seqs alone would
        # read that as "nothing moved" and suppress every frame, leaving each
        # client holding the retired file's projection with no later growth able to
        # dislodge it. When the origin changes, every projection is new.
        rebuilt = before is None or before.origin != bundle.origin
        for name, checkpoint in bundle.checkpoints.items():
            previous = before.checkpoints.get(name) if before is not None else None
            if not rebuilt and previous is not None and previous.last_seq == checkpoint.last_seq:
                continue
            if checkpoint.last_seq == 0:
                continue
            value = projections.projection_of(checkpoint)
            self._state.broadcast_ws_owners(FRAME, {"session_id": session_id, **value.to_dict()})

    # -- lifecycle ---------------------------------------------------------- #

    def bind(self, loop: asyncio.AbstractEventLoop, state: Any = None) -> None:
        """Point this publisher at the loop, and the state, now serving.

        The STATE is rebound too, not just the loop. A publisher reused across a
        restart inside one process would otherwise keep broadcasting through the
        retired state -- so ``_watchers`` counts the old hub's sockets and every
        frame goes to a room nobody is in, which looks exactly like a session that
        stopped updating.

        Scheduling flags belong to the loop that is going away: a timer armed on it
        will never fire, and a flush marked in flight there will never finish. Left
        set, ``_scheduled`` makes ``_mark`` believe a pass is already coming and
        ``_flushing`` makes ``_run`` yield to a pass that does not exist, so the
        publisher goes quiet for good. The dirty set is KEPT -- those sessions did
        grow, the entries are on disk, and the next pass folds them forward.
        """
        self._loop = loop
        if state is not None:
            self._state = state
        self._scheduled = False
        self._flushing = False


_publisher: CrewLogPublisher | None = None


def install_crew_log_publisher(state: Any) -> CrewLogPublisher | None:
    """Register the crew-log push with the emitter, once per process.

    Returns ``None`` and does nothing when the crew log is switched off. This runs
    on the gateway's boot path, so a launch without the flag must not pay for a
    subsystem it will not use: the flag is read from the environment here, before
    the emitter is imported and before a publisher is built. Importing the emitter
    to ask it whether it is enabled would be the cost itself, which is why the
    variable's name is spelled out below rather than read from that module.

    Returns the live publisher, and re-points it at the running loop AND the state
    now serving when it already exists, so a gateway restarted inside one process
    pushes on the loop that is actually serving, through the hub that actually
    holds the sockets, rather than a closed loop and a retired state. The emitter
    keeps the listener it was given: registering a second would fold each growth
    twice.
    """
    global _publisher
    if not env_flag_enabled(CREW_LOG_ENV):
        return None
    loop = asyncio.get_running_loop()
    if _publisher is not None:
        _publisher.bind(loop, state)
        return _publisher
    from kiro_crew.crew_log import emit as crew_log_emit

    _publisher = CrewLogPublisher(state)
    _publisher.bind(loop, state)
    crew_log_emit.add_growth_listener(_publisher.notify)
    return _publisher


guard_owner_surface_routes(globals(), member_scoped=frozenset())
