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
    """GET /api/sessions/{id}/crew-log/projection/{name} -- one fold and its seq.

    Serves the slot-keyed folds (:data:`projection.SLOT_PROJECTION_NAMES`) from the
    same route, addressed the same way. A caller holds a SESSION id, so the slot is
    resolved from that session's own header and the fold then joins every unit the
    slot ran under -- which is what makes the answer the slot's whole record rather
    than the part of it that happened to land in this session. A session whose slot
    cannot be proved gets the empty fold, never another slot's.
    """
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
        if name in projections.SLOT_PROJECTION_NAMES:
            # SLOT-keyed: this fold joins every unit the slot ran under, so it is
            # addressed through the slot its resolved unit's HEADER names rather than
            # folded from that one unit. Routed through the same resolution as the
            # per-unit read above, so a caller holding a slot key reaches its own
            # record either way.
            result = await asyncio.to_thread(_read_slot_fold, unit_id, name)
        else:
            result = await asyncio.to_thread(projections.read_projection, unit_id, name)
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    # ``session_id`` is what the CALLER asked about, not the unit the fold read:
    # a client polling by slot key compares this against the id it sent, and
    # answering with the resolved ACP id would both break that comparison and put
    # an internal identity on the wire. It answers neither ``resolved`` nor
    # ``writes_drained``: those exist for the surface that shows a reader the folds
    # TOGETHER, this route has no caller that reads them, and the settle they need
    # is a wait charged to every request. The resolution itself stays -- a slot-key
    # read of this route folds the session's unit like any other.
    return web.json_response({"session_id": session_id, **result.to_dict()})


async def api_session_crew_log_projections(request: web.Request) -> web.Response:
    """GET /api/sessions/{id}/crew-log/projections -- the ADVERTISED folds from one read.

    A panel shows several folds TOGETHER, and asking for them one route at a time
    means one independent resolution of the same session PER FOLD: if the slot's ACP
    session is replaced while those are in flight, some answers describe the unit
    that is going away and some the one arriving, and the reader sees a mix with no
    way to tell. Resolving once and folding once removes that window rather than
    narrowing it, and it costs less than the reads it replaces -- one pass over one
    file instead of one open of it per fold.

    It serves every ADVERTISED fold, not the subset a panel draws, because the cost
    of a fold a caller ignores is one checkpoint advanced over entries already read,
    while a per-caller subset would put the panel's list in the route and make a new
    fold a change to both. An INTERNAL fold is not served here: it is registered so
    that it shares the checkpoint and the recreated-log guard, and its one reader
    asks for it by name, so pushing it to a browser that cannot draw it would be a
    frame nothing reads.

    Each fold still carries its OWN ``seq``, because they genuinely differ: an
    entry advances the folds it belongs to and leaves the others where they were.
    What this route guarantees is that they all came from the same file at the same
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


def _read_slot_fold(unit_id: str, name: str) -> Any:
    """One slot-keyed fold for the slot *unit_id*'s header names. Blocking.

    Takes the resolved UNIT id rather than the caller's spelling, because the slot is
    read off that unit's own header -- a slot key handed straight to the header lookup
    would miss and answer with the empty fold.
    """
    projections = _crew_log()
    return projections.read_slot_projection(projections.slot_of_session(unit_id), name)


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
# and require the strict session identity the proxy forwards. One handler serving
# both would be one gate that has to be right for two callers whose identity
# arrives from different places, which is how a read widens by accident.
# The page and the fold themselves are the SAME implementations -- ``_read_page``
# and ``projection.read_projection`` -- so the two doors cannot answer differently.
#
# What the internal arm asks for is a strict session identity AND a scope: a
# session may read its own unit, the unit of any session it dispatched -- however
# many generations down -- and, if it is the owner at a dashboard tab, any unit at
# all. That third case is the one this module has always had; the second is what
# the decision added, and it exists because a conductor reading the crew logs of
# the sub-sessions it dispatched is the ordinary shape of the work rather than a
# special case.
#
# It is a DISPATCH fence, not an operator fence. The premise "sessions on a Kiro
# Crew gateway belong to one operator, so any of them may read any other" was
# considered and is not the rule here, for a reason that is about surfaces rather
# than about trust: a channel-linked session's conversation is a Slack or Telegram
# thread, so several allow-listed people read it and prompt-injectable content
# enters it. "One operator" does not reach that, and it is exactly the caller class
# an unscoped read would admit.
#
# Do NOT read the wider rule as parity with ``session_read_message`` either. That
# tool does return a peer session's transcript, but
# ``session_control.authorize_target`` is a substantially narrower gate: it refuses
# an unattended caller (``cron:``, ``taskrunner:``), an app-scoped caller or
# target, an incognito or temporary caller or target, and a channel-linked or
# channel-mirrored caller or target -- that last one explicitly because a linked
# caller's reads land in a channel thread in front of people who were never party
# to them. It also addresses only currently-open sessions and sits behind
# ``agent.session_control``. Those caller-class exclusions are MIRRORED below, and
# their constants are imported from that module rather than restated so the two
# gates cannot drift.
#
# The target side of that gate is mirrored too, and in TWO tests rather than one,
# because the question is asked about sessions that have CLOSED. The first reads
# the target's own ``session/opened`` entry, which records what kind of session the
# log belongs to; that is what makes a CLOSED child decidable, and a log that does
# not carry the record is refused rather than assumed unrestricted, so every unit
# opened before the record existed is outside a cross-session read. The second reads
# the target's live slot, which is the only thing that can see a class the session
# ACQUIRED after it opened -- a channel link added mid-conversation. Either refuses;
# neither admits on the other's behalf. Exact parity with ``authorize_target`` is
# still not the goal: that gate answers 404 for a closed session, and reading a
# finished child's recorded log is the whole point here.
#
# The fence is keyed on SLOTS, not on unit ids. A slot outlives its ACP session, so
# a gateway restart gives the same tab a new session id and therefore a new unit: a
# fence keyed on the recorded ``parent.sid`` would hand a conductor a chain naming
# one of its own earlier units and lock it out of the children it dispatched minutes
# earlier. The slot key carries its own creation stamp and is not recycled. The
# transitive walk is ``crew_log.session_tree``'s own fold rather than a second slot map
# here, so the tree this fence trusts is the tree the Sessions page draws.
#
# What it therefore refuses: a caller off the internal transport (a browser has its
# own door), a request naming another component, a caller whose session the gateway
# cannot name or cannot place as a slot, a caller in one of the mirrored classes
# reading anything but its own unit -- INCLUDING an owner's dashboard tab that is
# mirrored to a channel, because that tab republishes every turn -- a unit outside
# the caller's own dispatch tree, a target whose log does not record its class, and
# a target in one of the mirrored classes by either its record or its live slot. The
# identity requirement is the load-bearing one -- an
# operator's record of WHICH session read a log is only worth having if the
# identity was resolved strictly rather than walked out of a process tree.

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


#: Where the gate leaves the listing scope it resolved, for the listing route to
#: read. On the request rather than returned, because the gate's answer is
#: "allowed or not" for every route and only one of them needs a filter as well.
#: The ONE refusal a caller gets when a read falls outside its scope, whether the
#: request named no placeable unit at all or named a live unit in another tree. The
#: two must not be distinguishable: ``api_crew_log_resolve`` resolves the key the
#: caller named BEFORE authorizing, so a caller that could tell "no such session"
#: from "a session you may not read" could guess keys and learn which slots are
#: live -- an enumeration oracle handed to exactly the dispatched agents this fence
#: is meant to bound. The text therefore states the scope and that the request is
#: outside it, and says nothing about whether a unit was found.
_OUT_OF_SCOPE_REFUSAL: Final[str] = (
    "this read is scoped to your own unit and the crew logs of the sessions you "
    "dispatched, and this request does not fall inside that scope; the owner's own "
    "dashboard session reads any unit"
)

LIST_SCOPE_KEY: Final[str] = "crew_log_list_scope"

#: The workspace the LISTING caller was in when the gate granted it, as a VALUE rather
#: than the slot: a slot's workspace changes in place, so holding the object would make
#: every later read report the CURRENT workspace and a move would be invisible to the
#: very check that exists to catch it. Every row is compared against this one reading,
#: and the re-check compares it against the caller's workspace after the walk. Absent
#: fails CLOSED: it states no workspace, which matches no recorded one.
LIST_CALLER_WORKSPACE_KEY: Final[str] = "crew_log_list_caller_workspace"

#: Which ARM granted this read, for the re-check that runs after the payload is
#: built. A mark and never data: the re-check folds the target's class again, because
#: that fold reads the log's whole life and a restrictive move appended during the
#: read makes it answer differently. What it cannot re-derive is which target test it
#: owes -- the owner arm runs none by design -- so that is what the grant records.
#: Absent means no grant left one here, which refuses.
TARGET_CLASS_KEY: Final[str] = "crew_log_target_grant"

#: The owner arm's mark. That arm grants without any target test, so the re-check must
#: not invent one -- and it has to be able to tell that from a grant whose mark went
#: missing, which an absent value cannot express.
_OWNER_GRANT: Final[str] = "\x00owner-reads-any-unit"

#: The lineage arm's mark. Its target test IS owed, so the re-check re-runs it over a
#: freshly folded class. Two sentinels rather than a boolean because a third arm would
#: have to name itself here, and an unrecognised mark refuses.
_LINEAGE_GRANT: Final[str] = "\x00dispatcher-reads-its-child"

#: The root a listing falls back to when the gate left no scope on the request.
#: A unit id is an ACP session id, so this is not one, and it selects nothing --
#: which is the safe direction for a value that can only appear if the gate and
#: this route ever stop agreeing.
_UNSCOPABLE: Final[str] = "\x00no-scope-resolved"

#: Session-key NAMESPACES whose callers run with nobody watching. Not the same
#: spelling as ``session_control.UNATTENDED_SLOT_PREFIXES``, which names SLOT ids
#: (``cron-<job>``, ``workflow-<run>``) and so matches a key like
#: ``dashboard:cron-123``: these are the namespaces the crew log's own key
#: vocabulary uses. Both are tested below, so the class is refused whichever
#: vocabulary carries it, and the slot-id half stays imported rather than copied.
_UNATTENDED_NAMESPACES: Final[tuple[str, ...]] = ("cron:", "taskrunner:")


def _slot_half(session_key: str) -> str:
    """The SLOT half of *session_key*, or ``""`` when it has none.

    One answer for every caller here, because the two that read it -- the live-slot
    lookup and the dispatch fence -- must agree about who a caller is or one can
    place a caller the other cannot find.

    A key with NO colon is a bare slot name, not a key missing its slot: the crew
    log's own resolver states the premise (``resolve.py``, ``unit_for_session_key``)
    -- a key carrying no colon cannot already be namespaced, since every namespace
    spelling carries one -- and retries it in ``dashboard:`` form for that reason.
    Reading it as its own slot is the same premise, so a caller whose key arrives
    bare is resolved here exactly as it is resolved there. A key whose namespace is
    present but whose slot half is EMPTY (``dashboard:``) genuinely names no slot and
    answers ``""``, which every caller treats as a refusal.

    That last answer is the truthful value rather than a guard, and it is deliberately
    not pinned by a test: at every route the CLASS test runs first and refuses such a
    key for naming no live slot, so returning ``""`` and returning the whole key are
    indistinguishable from outside. A test on it would assert an incidental artifact.
    What must not happen is someone reading that as licence to fall back to the whole
    key -- it would place a caller by a name no slot has, and the only reason that is
    harmless today is the order two decisions happen to run in.
    """
    if ":" not in session_key:
        return session_key
    _namespace, _, slot = session_key.partition(":")
    return slot


def _live_slot(state: Any, session_key: str) -> Any:
    """The live slot behind *session_key*, or ``None``.

    Looked up by the key's slot half through ``state._slots``, which is the lookup
    the owner test below has always used. ``None`` means the key names no slot this
    gateway currently holds, and every caller here treats that as a refusal rather
    than as a pass: a caller that cannot be placed cannot be placed in a dispatch
    tree either.
    """
    if not session_key:
        return None
    slots = getattr(state, "_slots", None)
    lookup = getattr(slots, "get", None) if slots is not None else None
    if lookup is None:
        return None
    slot = _slot_half(session_key)
    if not slot:
        return None
    return lookup(slot)


def _live_slot_for_unit(state: Any, unit: str) -> Any:
    """The live slot whose work is currently landing in *unit*, or ``None``.

    Resolved by asking each live slot which unit it is writing to, rather than by
    trusting the slot name recorded in the log: a slot can be closed and replaced,
    so the recorded name can point at a different session than the one that wrote
    the entries. ``None`` therefore means "no OPEN session is writing to this unit",
    which is the ordinary state of a finished child's log and is why the target test
    treats it as nothing to exclude on.
    """
    from kiro_crew.crew_log.resolve import unit_for_session_key

    if not unit:
        return None
    slots = getattr(state, "_slots", None)
    values = getattr(slots, "values", None) if slots is not None else None
    if values is None:
        return None
    sessions = getattr(state, "sessions", None)
    for slot in list(values()):
        key = getattr(slot, "key", "")
        if key and unit_for_session_key(sessions, key) == unit:
            return slot
    return None


def _caller_own_unit(request: web.Request) -> str:
    """The unit the CALLING session's own work is landing in, or ``""``.

    Resolved server-side from the forwarded session key, never from the request: a
    caller that could name its own scope could name someone else's.
    """
    session_key = _caller_session_key(request)
    if not session_key:
        return ""
    from kiro_crew.crew_log.resolve import unit_for_session_key

    state = request.app.get("state")
    return unit_for_session_key(getattr(state, "sessions", None), session_key)


def _caller_slot(request_key: str) -> str:
    """The slot half of a session key, or ``""`` when it has no usable one.

    The slot is what a dispatch fence keys on, and it comes from the forwarded key
    rather than from any record: it is the caller's own identity, resolved
    server-side. A key naming no slot half yields ``""``, which refuses -- a caller
    that cannot be named as a slot cannot be found in a dispatch tree either.

    Shares :func:`_slot_half` with the live-slot lookup deliberately. A caller the
    class test can find and the fence cannot, or the reverse, is a disagreement about
    identity inside one decision, and a bare key is exactly where two separate
    spellings part: one places it and the other does not.
    """
    return _slot_half(request_key)


def _caller_class_refusal(request: web.Request, session_key: str) -> str:
    """``""`` when this caller CLASS may read a unit other than its own, else why.

    Mirrors the caller side of
    :func:`~kiro_crew.dashboard.session_control.authorize_target`. Its constants and
    its mirror test are IMPORTED rather than restated, so a change to what that gate
    excludes reaches this one instead of leaving two spellings to drift apart.

    Each class is refused for the reason that gate refuses it, and the reasons are
    about where a read LANDS rather than about how much a session is trusted: a
    scheduled run has no operator watching what it did with the content; an
    app-scoped session belongs to its app; an incognito or temporary session was
    created to leave and learn nothing; and a channel-linked or mirrored session's
    own conversation is a channel thread, so anything it reads is published to
    whoever is in that channel. A cron tab's link is exempt because it names the
    job's own run transcript and republishes to nobody -- the same exemption
    ``CRON_LINK_PREFIX`` carries there.

    This does NOT gate a read of the caller's own unit. A session reading its own
    recorded history learns nothing it did not already produce, which is why the
    scope test applies this only once a read reaches past that.
    """
    from kiro_crew.dashboard.session_control import (
        CRON_LINK_PREFIX,
        UNATTENDED_SLOT_PREFIXES,
        _has_channel_mirror,
    )

    if session_key.startswith(_UNATTENDED_NAMESPACES) or session_key.split(":", 1)[-1].startswith(
        UNATTENDED_SLOT_PREFIXES
    ):
        return (
            "unattended sessions (scheduled runs) may read their own unit but not "
            "another session's"
        )
    state = request.app.get("state")
    slot = _live_slot(state, session_key)
    if slot is None:
        return (
            "the calling session names no live slot, so it cannot be placed as this "
            "unit's dispatcher"
        )
    if getattr(slot, "_app", ""):
        return "an app-owned session may read its own unit but not another session's"
    if getattr(slot, "memory_mode", "persistent") != "persistent":
        return (
            "an incognito or temporary session may not read another session's crew "
            "log; that session is meant to leave and learn nothing"
        )
    link = getattr(slot, "linked_session_key", "")
    if link and not link.startswith(CRON_LINK_PREFIX):
        return (
            "a channel-linked session may not read another session's crew log; what "
            "it reads lands in that channel's thread"
        )
    if _has_channel_mirror(state, slot):
        return (
            "a session mirrored to a channel may not read another session's crew "
            "log; what it reads lands in front of that channel's audience"
        )
    return ""


def _front_of_log_is_gone(unit: str) -> bool:
    """Whether retention has deleted the segments this log opened with.

    Asked ONLY to word a refusal that has already been decided, so it costs one
    directory listing on the refusal path and nothing on the admitting one. It reuses
    the reader's own helper rather than re-deriving the rule: the first seq is in each
    segment's FILE NAME, which is what makes a gap at the front cheap to tell from a
    gap in the middle.
    """
    from kiro_crew.crew_log.errors import CrewLogError
    from kiro_crew.crew_log.store import KIND_SESSION, segment_first_seqs

    try:
        firsts = segment_first_seqs(KIND_SESSION, unit)
    except (OSError, CrewLogError):
        # Every failure here means the same thing: this cannot tell retention from a
        # writer fault, so it says so by declining to claim retention. The read is
        # refused either way -- only the WORDING is at stake -- which is why an id the
        # store rejects outright must not escape as a server error from a helper whose
        # whole job is choosing a sentence.
        return False
    return bool(firsts) and firsts[0] > 1


def _recorded_class_and_front(unit: str) -> tuple[dict[str, Any] | None, bool]:
    """``(recorded, front_gone)`` -- the fold and the front-trim fact, in ONE offload.

    Both are filesystem reads, and this is the only place that is already off the event
    loop. Reading the front here rather than where the refusal is worded is what keeps a
    directory listing off the loop: a handler that asked for it later would block on it,
    and the listing exists only to choose between two refusal texts.

    The front is asked ONLY when the fold came back ``None`` -- the one case whose refusal
    has to tell retention from a writer fault -- so the cost stays on the refusal path and
    an admitted read pays nothing.
    """
    from kiro_crew.crew_log.read import recorded_class

    recorded = recorded_class(unit)
    front_gone = recorded is None and bool(unit) and _front_of_log_is_gone(unit)
    return recorded, front_gone


def _recorded_class_refusal(
    recorded: dict[str, Any] | None, unit: str = "", front_gone: bool = False
) -> str:
    """``""`` when the target's OWN log says its session may be read, else why.

    The record half of :func:`_target_class_refusal`, and the half that answers for a
    session that has CLOSED. The crew log is the authoritative record of a session, so what kind
    of session a unit belongs to is read from the unit's own entries rather than
    from a live lookup that has nothing left to ask.

    ``recorded`` is the folded ``class`` projection, which is the log's whole life
    rather than its first instant: each member is held at the most restrictive value
    the log ever recorded, so a session that was published to a channel for one turn
    stays refused after the link is dropped. That is the honest reading, because the
    turn's content is still in this log.

    Two ways this refuses before looking at any class. ``None`` means nothing was
    recorded -- a log opened before the field existed, or one that cannot be read --
    and admitting on it would put every pre-existing unit back inside the fence this
    closes. ``complete`` false means the history has no BEGINNING: the fold saw the
    class move and never saw what it moved from, which retention taking the opening
    entry produces, so the earliest class the log held is unknown. Neither is
    evidence that nothing applies. Both name what is missing, because the remedy is
    not something the reader can guess.

    ``complete`` is also what DATES the log. The opening entry's ``class`` object and
    the ``session/class`` transition were declared together, so a log that states the
    first was written by a build that records the second -- which makes a complete
    history with no transitions a real account of a class that never moved, rather
    than the silence of a writer that had no way to say it moved. A log that cannot
    be dated this way is exactly the one that refuses.

    The classes are the ones ``authorize_target`` refuses, read off facts: an app
    owns the session, it keeps no memory, or its conversation is published to a
    channel.
    """
    if recorded is None:
        if front_gone:
            # RETENTION, not absence, and it gets its own text because the remedy is
            # different: nothing about this log will ever satisfy the read again, where
            # an unrecorded class is fixed by the session recording one. Saying
            # "does not record" here would send a maintainer looking for a writer bug.
            return (
                "that session's crew log no longer holds its own beginning -- retention "
                "has deleted the segments it opened with -- so the earliest kind of "
                "session it was cannot be established and it is refused rather than "
                "read from its surviving part"
            )
        return (
            "that session's crew log does not record what kind of session it is, so "
            "whether it may be read from outside cannot be established; logs opened "
            "before the class was recorded are refused rather than assumed "
            "unrestricted"
        )
    if not recorded.get("complete"):
        return (
            "that session's crew log records a change of class but not the class it "
            "started from, so the earliest kind of session it was cannot be "
            "established; a history with no beginning is refused rather than read "
            "from its surviving part"
        )
    if recorded.get("app"):
        return "an app-scoped session's crew log is not readable from outside that app"
    if recorded.get("memory") != "persistent":
        return (
            "an incognito or temporary session's crew log is not readable by another "
            "session; that session is meant to leave no trace"
        )
    if recorded.get("channel"):
        return (
            "that session's conversation is published to a channel, so reading its "
            "crew log would pull the channel's content across that boundary"
        )
    return ""


def _caller_workspace(caller_slot: Any) -> str:
    """The workspace a caller's slot states, or ``""`` when it states none.

    Read through one named function because the two doors read it at different
    MOMENTS: the per-unit door reads it live, while a listing reads it once at the gate
    and compares every row against that one reading. A slot is mutable and its
    workspace changes in place, so which moment a reading came from is the whole
    question -- and a reading taken twice from the same object is two answers.
    """
    return str(getattr(caller_slot, "workspace", "") or "")


def _workspace_refusal(caller_workspace: str, recorded: dict[str, Any] | None) -> str:
    """``""`` when *caller_workspace* owns this log, else the reason it may not read.

    A dispatch grant is derived from a RECORDED lineage, and a lineage outlives a
    WORKSPACE switch: a conductor that dispatched a child and then moved to another
    workspace still names that child in its tree, because the edge was written on the
    child and nothing rewrites it. Switching workspace is ordinary operation, so without
    this arm that grant reads a log belonging to the workspace the caller LEFT -- which
    is the boundary a workspace exists to draw, and the reason this is a boundary test
    rather than a tidiness one.

    Three states refuse, and they are one fact in three shapes: this log is not solely
    this workspace's. A recorded workspace that DIFFERS; a log whose recorded workspace
    MOVED, whose content spans two workspaces so neither owns it; and an ABSENT one,
    which no live slot can produce because a slot's workspace defaults to ``default`` --
    so silence is a log this build cannot place, and placing it by assumption is the
    thing being prevented.

    A ``recorded`` of ``None`` is NOT this arm's business. The record half already
    refuses on it, and refusing here as well would answer a caller that asked one
    question with two, telling it which arm ran.
    """
    if recorded is None:
        return ""
    if recorded.get("workspace_moved") is True:
        return (
            "that session's crew log spans more than one workspace, so no single "
            "workspace's session may read it"
        )
    owner = recorded.get("workspace")
    if not isinstance(owner, str) or not owner:
        return (
            "that session's crew log does not record which workspace it belongs to, "
            "so it cannot be read from another session"
        )
    if owner != caller_workspace:
        return (
            "that session's crew log belongs to a different workspace, and a workspace "
            "is a memory boundary"
        )
    return ""


def _target_class_refusal(
    request: web.Request,
    unit: str,
    recorded: dict[str, Any] | None,
    front_gone: bool = False,
) -> str:
    """``""`` when *unit* may be read by its dispatcher, else why. THE target test.

    One function rather than two sequential ones, because the halves are not
    independent and the order between them is not a caller's business. Reading them
    here is what makes the record half UNSKIPPABLE: a caller cannot consult the live
    half alone, which for a target with no live slot would be no test at all.

    The RECORD first, from the folded ``class`` projection: each member held at the
    most restrictive value the log ever recorded, so a session published to a channel
    for one turn stays refused after the link is dropped. That is the honest reading,
    because the turn's content is still in this log.

    Then the LIVE slot, for the one thing the record cannot cover: a session can
    acquire a channel link in the turn now in flight, whose entries are not written
    yet. A target with NO live slot is therefore fully answered by the record above,
    and that is the case this door exists for -- a conductor reading a worker that has
    finished. It is also the one place the mirror of ``authorize_target`` is
    deliberately inexact, since that gate answers 404 for a closed session.
    """
    refusal = _recorded_class_refusal(recorded, unit, front_gone)
    if refusal:
        return refusal
    from kiro_crew.dashboard.session_control import CRON_LINK_PREFIX, _has_channel_mirror

    state = request.app.get("state")
    slot = _live_slot_for_unit(state, unit)
    if slot is None:
        return ""
    if getattr(slot, "_app", ""):
        return "an app-scoped session's crew log is not readable from outside that app"
    if getattr(slot, "memory_mode", "persistent") != "persistent":
        return (
            "an incognito or temporary session's crew log is not readable by another "
            "session; that session is meant to leave no trace"
        )
    link = getattr(slot, "linked_session_key", "")
    if link and not link.startswith(CRON_LINK_PREFIX):
        return (
            "that session's conversation is a channel thread, so reading its crew log "
            "would pull the channel's content across that boundary"
        )
    if _has_channel_mirror(state, slot):
        return (
            "that session is mirrored to a channel, so reading its crew log would "
            "pull the channel's content across that boundary"
        )
    return ""


def _owner_session_refusal(request: web.Request, session_key: str) -> str:
    """``""`` when *session_key* is the owner at a dashboard tab, else the reason.

    Unchanged in substance from the rule this module shipped with, and it is what
    keeps the owner's own view of every unit working. Three conditions, each ruling
    out a caller class that is not the person at their own tab: a ``dashboard:``
    key, so a headless or channel-bound caller is refused by the namespace it
    carries rather than by a list of what it is not; no app owns it, derived by
    :func:`~kiro_crew.dashboard.token_auth.derive_caller_app` against the
    server-side registries, because an app agent granted this server arrives on the
    same transport as the person; and the session keeps persistent memory, so an
    incognito or temporary session is refused.

    Returning a reason is not by itself a refusal of the READ -- the scope test
    tries lineage next. It answers only "is this the owner's own tab", which is the
    one caller that needs no scope.
    """
    from kiro_crew.dashboard.token_auth import derive_caller_app

    if not session_key:
        return "the request carried no session identity"
    if not session_key.startswith("dashboard:"):
        return f"{session_key.split(':', 1)[0]}: is not the owner's own dashboard session"
    state = request.app.get("state")
    slot = _live_slot(state, session_key)
    if slot is None:
        return "the calling session names no live dashboard slot"
    slots = getattr(state, "_slots", None)
    jobs = getattr(getattr(state, "crons", None), "_jobs", None)
    subagents = getattr(getattr(state, "subagents", None), "_agents", None)
    if derive_caller_app(slots, session_key, jobs, subagents):
        return "an app-owned session is not the owner's own dashboard session"
    if getattr(slot, "is_restricted", False):
        return "an incognito or temporary session is not the owner's own dashboard session"
    return ""


async def _read_scope_refusal(request: web.Request, session_key: str, unit: str) -> str:
    """``""`` when *session_key* may read *unit*, else the reason it may not.

    The three ways in, tried in this order because it is cheapest first:

    #. the caller's OWN unit -- one resolver call, no slot lookup, and a session
       reading its own recorded history learns nothing it did not produce;
    #. the owner at a dashboard tab, whose caller class must ALSO allow it;
    #. a unit inside the caller's own dispatch tree, once the caller's class, the
       target's recorded class and the target's live class all allow it.

    The caller-class test gates the owner arm as well as the lineage arm, and that
    is deliberate rather than symmetry for its own sake. The exclusions are about
    where a read LANDS: an owner's own tab publishes to the person at it, but a
    dashboard session MIRRORED to a channel republishes every turn, so the same tab
    that may read every unit becomes a republisher of them. A mirrored owner tab is
    therefore held to its own unit like any other excluded caller -- one check at
    the one choke point, rather than an arm that is exempt from the rule the arm
    beside it enforces.

    The fence is keyed on SLOTS. A slot outlives its ACP session: a gateway restart
    gives the same tab a new session id and a new unit, so keying on the recorded
    ``parent.sid`` would hand a conductor a chain naming one of its own earlier
    units and lock it out of children it had dispatched minutes earlier. The recorded
    ``parent.slot`` carries the creating tab's own stamp and is not recycled, so it
    survives the restart; the sid stays on the entry as the audit citation for which
    log held the call.

    Async because the third arm touches the filesystem: it folds the store once, and
    reads the target's opening entry. Both happen in ONE offload, so a cross-unit
    request suspends once rather than per step, and the first two arms answer from
    memory and reach no ``await`` at all.

    The caller-class test is re-asserted AFTER the offload. It reads live session
    state, the ``await`` is a suspension point, and a caller can acquire a channel
    link or lose its persistent memory while the fold runs -- so the verdict this
    returns is decided from state read after the last suspension rather than before
    it.
    """
    own = _caller_own_unit(request)
    if unit and own and unit == own:
        return ""
    class_refusal = _caller_class_refusal(request, session_key)
    if not class_refusal and not _owner_session_refusal(request, session_key):
        # The owner at a dashboard tab reads any unit, which is this door's recorded
        # product decision, so this arm runs no target test at all. It says so rather
        # than leaving nothing, because the re-check must re-run exactly the tests
        # this grant ran and cannot tell "no test was owed" from "the answer is lost"
        # by looking at an absent value.
        request[TARGET_CLASS_KEY] = _OWNER_GRANT
        return ""
    if class_refusal:
        return class_refusal
    if not unit:
        return _OUT_OF_SCOPE_REFUSAL
    slot = _caller_slot(session_key)
    if not slot:
        return (
            "your session names no slot, so it cannot be placed as this unit's "
            "dispatcher; a session reads its own crew log and those of the sessions "
            "it dispatched"
        )
    from kiro_crew.crew_log.read import dispatch_view, recorded_class

    def _probe() -> tuple[Any, dict[str, Any] | None]:
        # One offload for both reads. The fold is asked to admit this unit first, so
        # the unit a request is actually about is inside the scanner's cap whatever
        # else the store holds.
        return dispatch_view((unit,)), recorded_class(unit)

    view, recorded = await asyncio.to_thread(_probe)
    # Which arm is granting, for the re-check. The fold itself is not carried: the
    # re-check folds again, because a class move appended while the payload was built
    # makes that fold answer more restrictively than this one did.
    request[TARGET_CLASS_KEY] = _LINEAGE_GRANT
    class_refusal = _caller_class_refusal(request, session_key)
    if class_refusal:
        return class_refusal
    if not view.dispatched_by(unit, slot):
        return _OUT_OF_SCOPE_REFUSAL
    # The target test, and only once the unit is known to be in this caller's tree: a
    # caller outside the tree must not be able to tell a refused class from a refused
    # scope, or the class of every unit on the host becomes readable one refusal at a
    # time.
    # The front-trim listing is asked LAST and only here: after the scope arm above has
    # had its chance to refuse, and only for a log whose fold came back ``None``. It is
    # offloaded because it is a directory read, and it only chooses between two refusal
    # texts -- so blocking the loop for it would spend gateway latency on wording.
    front_gone = False
    if recorded is None:
        front_gone = await asyncio.to_thread(_front_of_log_is_gone, unit)
    # The WORKSPACE boundary runs LAST of the target arms. It is a question about
    # OWNERSHIP, while the class arms above are about publication, and a caller refused
    # for either deserves the more specific reason -- a channel-published log says so
    # whichever workspace asks. Like those arms it is reached only once the unit is known
    # to be in this caller's tree, so a caller outside the tree learns nothing from it.
    # ``slot`` above is the caller's slot KEY, not its slot, so the live object is
    # resolved here -- the same lookup the caller test made, which already proved it
    # exists by not refusing.
    return _target_class_refusal(request, unit, recorded, front_gone) or _workspace_refusal(
        _caller_workspace(_live_slot(request.app.get("state"), session_key)), recorded
    )


def _list_scope(request: web.Request, session_key: str) -> tuple[str, str, str]:
    """``(scope_unit, scope_slot, refusal)`` for a listing by *session_key*.

    A listing ENUMERATES units, so leaving it unscoped would tell any caller which
    sessions exist on this host -- the one thing a per-unit gate cannot refuse after
    the fact. It therefore carries the same three cases the per-unit scope does,
    expressed as a filter rather than as a verdict:

    * the owner at a dashboard tab whose class also allows it gets no narrowing and
      sees every unit, which is the view this module shipped with;
    * an ordinary identified session gets its own unit plus the dispatch tree below
      its slot, so it sees its own record and the sessions it dispatched;
    * a caller in one of the excluded classes gets its own unit and nothing below
      it, because those exclusions are about reading PAST its own record and a
      listing is a read.

    A refusal is returned only when the caller has no unit of its own to scope to --
    the same condition the per-unit arm refuses on, and for the same reason: a
    caller that cannot be placed cannot be given a scope.

    The scope this returns is not the whole answer: the rows admitted by LINEAGE are
    then asked the per-unit target tests one row at a time, because a row names a
    unit's slot, model and activity and the dispatch tree alone does not bound which
    WORKSPACE those belong to. That test is deliberately NOT a blanket one -- it is
    asked only of rows this scope already admitted, so it costs one fold per unit in
    the caller's own tree rather than one per unit on the host, and the owner's
    unscoped view is never asked at all.
    """
    class_refusal = _caller_class_refusal(request, session_key)
    if not class_refusal and not _owner_session_refusal(request, session_key):
        return "", "", ""
    own = _caller_own_unit(request)
    if not own:
        return (
            "",
            "",
            "this listing is scoped to your own unit and the sessions you dispatched, "
            "and your session has no live crew log unit to scope it to",
        )
    if class_refusal:
        return own, "", ""
    return own, _caller_slot(session_key), ""


async def _authorize_crew_log_read(
    request: web.Request, operation: str, *, unit: str = "", listing: bool = False
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

    Refused rather than admitted because the browser already has its own door: the
    cookie-only ``/api/sessions/{id}/crew-log`` pair, which this prefix
    deliberately does not cover. The two doors keep separate authorization models,
    and a cookie admitted here would be a second, unaudited path into this one.

    The rule this arm applies: a valid strict session identity, and then a SCOPE --
    the caller's own unit, a unit inside the dispatch tree the caller created, or
    any unit at all for the owner at a dashboard tab. See the section comment above
    for the decision that says so, and :func:`_read_scope_refusal` for the order the
    three are tried in. A caller the gateway cannot name is refused before any of
    them, which is what keeps the audit record below meaningful.

    Every read is audited under the operation name the caller passes, which is the
    same name the browser routes use, so an operator querying
    ``session_crew_log.read`` sees every read of a page regardless of which door it
    came through. EVERY denial too, including the two that refuse a caller before
    its identity is settled -- those are the ones an operator most wants, because a
    secret-less request at an MCP-only route and a request naming another component
    are both the shape of an attempted boundary crossing, while an unidentified
    session is ordinary. ``request_origin`` is resolved FIRST so they can be
    recorded: it returns ``("dashboard", "dashboard")`` for a request with no secret
    and clamps an unrecognized component to ``"unknown-internal"``, so reading it
    early cannot let an unauthenticated caller name itself into the audit log.
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
    if not session_key:
        refusal = (
            "this read needs a session identity and the request carried none; "
            "only a session the gateway can name may read a crew log"
        )
        _audit_crew_log_read(caller, source, operation, "denied", refusal)
        return _forbidden(refusal)
    if listing:
        # A listing's scope is a FILTER, so it is resolved here rather than in the
        # route: that keeps one audit path for both arms, and it means the route
        # cannot answer a listing it never had a scope for.
        scope_unit, scope_slot, scope_refusal = _list_scope(request, session_key)
        request[LIST_SCOPE_KEY] = (scope_unit, scope_slot)
        request[LIST_CALLER_WORKSPACE_KEY] = _caller_workspace(
            _live_slot(request.app.get("state"), session_key)
        )
    else:
        scope_refusal = await _read_scope_refusal(request, session_key, unit)
    if scope_refusal:
        _audit_crew_log_read(caller, source, operation, "denied", scope_refusal)
        return _forbidden(scope_refusal)
    _audit_crew_log_read(caller, source, operation, "granted", "")
    return None


async def _stale_grant_refusal(
    request: web.Request, operation: str, *, unit: str = "", listing: bool = False
) -> web.Response | None:
    """``None`` when the grant still holds, else the refusal that overtook it.

    Called after the offload that BUILDS a payload and before that payload is
    returned. :func:`_authorize_crew_log_read` decides on state read BEFORE the read,
    and the read is a suspension point, so the target's class can move while the
    entries are being read off the loop. Without this, a cross-unit read would deliver
    entries the target appended during the read under a verdict taken before they
    existed -- a one-shot delivery of content the refreshed class governs.

    The target's class IS folded a second time, because this fold reads the log's whole
    life: a ``session/class`` move appended while the payload was being built makes the
    fold answer more restrictively than it did at the grant. The re-fold closes that
    exactly rather than narrowing it, and the exactness comes from where the class is
    RECORDED, not from when it is sampled. Every surface that commits a change to a
    session's class records the change as it commits, so a class governing any content
    in the log is stated in that log ahead of the content, and a fold taken after the
    payload read has therefore seen every class state that could govern what the
    payload carries. The fold is incremental, so on a log that did not move it consumes
    nothing. It only ever moves AWAY from permissive, so this can withdraw a grant and
    never widen one.

    What this DOES buy, stated exactly, because a wider claim would be false: the
    route never hands another session's content to a caller that is publishing AT THE
    MOMENT OF THE HANDOFF. That is an act this route performs and therefore controls.
    The scenario it stops is the one a reviewer named: a prompt to paste a dispatched
    child's log, a mirror bound while the payload read is suspended, and a reply that
    publishes the private log to the channel.

    What it does NOT buy, and this is a KNOWN residual rather than an oversight: a
    caller that was entitled at the handoff and acquires a channel link afterwards
    holds the payload in context and can publish it on a later turn. Nothing at turn
    emission inspects a turn for another session's log content -- there is no
    provenance tracking on read content anywhere in the gateway -- so no refusal here
    can claw that back. The two cases are different in kind, not merely in timing: the
    first is the gateway delivering content INTO a published session, the second is a
    session becoming published while holding content it was entitled to receive. This
    function is the last point of control over the first and has none over the second.
    Closing the second needs provenance on delivered content, which does not exist and
    is not in this change's scope.

    The mark under :data:`TARGET_CLASS_KEY` says whether a TARGET test is owed. An
    owner-granted read must not be target-tested, because an owner reads a published
    unit by entitlement, so without the mark this would refuse what the gate
    deliberately allowed. The caller test runs on every arm regardless, including the
    owner's: an owner tab that acquired a mirror during the read is publishing now.

    On the one arm that suspends AGAIN -- the lineage arm, which refolds the target off
    the loop -- the caller test is read a SECOND time, after that suspension. Answering
    on the pre-suspension reading would leave this route's own window open while it
    closed the payload-build one, which is the same stale-read class it exists to close.
    Both readings refusing is enough, so a re-read can only add a refusal.

    The dispatch edge is NOT folded again: it is recorded the same way and the store is
    append-only, so a second fold can only gain descendants -- it cannot take back a
    unit the caller was already placed above.

    A read of the caller's OWN unit is exempt, matching the first arm of
    :func:`_read_scope_refusal`: the classes govern reading PAST one's own record.

    A LISTING is re-checked too, by recomputing its scope and comparing. Its rows were
    selected under the scope the caller held at the gate, so a caller whose class
    narrowed during the read has rows in hand that its current scope would not have
    gathered. The comparison is what refuses, rather than a test of the rows, which a
    listing deliberately does not class-test.

    The denial is audited under the same operation name the grant was, so the pair
    reads in order in the audit log -- granted, then denied -- which is the true
    account of what happened rather than a grant quietly withdrawn.
    """
    session_key = _caller_session_key(request)
    caller = _caller_class_refusal(request, session_key)
    if listing:
        scope_unit, scope_slot, scope_refusal = _list_scope(request, session_key)
        if (scope_unit, scope_slot) == request.get(LIST_SCOPE_KEY, (_UNSCOPABLE, "")):
            # The scope tuple is a unit and a slot, and NEITHER moves when a caller
            # switches workspace -- so a matching tuple is not yet a matching caller. The
            # rows were admitted against the workspace read at the gate, and a caller
            # that moved while the walk ran would be handed rows from the workspace it
            # left. Same shape as the per-unit re-check below, and the same reason it
            # cannot reuse the grant's own answer.
            moved = _caller_workspace(_live_slot(request.app.get("state"), session_key)) != str(
                request.get(LIST_CALLER_WORKSPACE_KEY, "") or ""
            )
            if not moved:
                return None
            refusal = (
                "your session changed workspace while this listing was built, so the "
                "rows it names are not all this workspace's to read"
            )
        else:
            refusal = scope_refusal or caller or _OUT_OF_SCOPE_REFUSAL
    else:
        own = _caller_own_unit(request)
        if unit and own and unit == own:
            return None
        refusal = await _unit_recheck_refusal(request, unit, caller, session_key)
    if not refusal:
        return None
    from kiro_crew.dashboard.token_auth import request_origin

    source, caller = request_origin(request, what="crew log read", log=logger)
    _audit_crew_log_read(caller, source, operation, "denied", refusal)
    return _forbidden(refusal)


async def _unit_recheck_refusal(
    request: web.Request, unit: str, caller: str, session_key: str
) -> str:
    """The unit arms of :func:`_stale_grant_refusal`, split out to keep one denial tail.

    *caller* is the caller-class verdict already taken by the caller of this function,
    passed in rather than recomputed because two of the three arms suspend no further
    and one reading of live state is the whole of what they need. *session_key* is the
    key that verdict was taken under, so the one arm that DOES suspend again re-reads
    the same session rather than re-deriving which session it is.
    """
    granted = request.get(TARGET_CLASS_KEY, None)
    if granted == _OWNER_GRANT:
        # The owner arm ran no TARGET test, so neither does this: an owner reads a
        # published unit by entitlement, and re-testing the target here would refuse
        # what the gate deliberately allowed. The mark is what carries that
        # distinction past the offload. The CALLER test still runs, because an owner
        # tab that acquired a mirror while the payload was built is publishing now.
        refusal = caller
    elif granted != _LINEAGE_GRANT:
        # No mark, or one this route does not recognise. Every arm that BUILDS a
        # payload leaves one, so this is the gate and this route having stopped
        # agreeing -- and the target test must not be skipped on that disagreement.
        refusal = caller or (
            "this read cannot be checked against the grant it was made under, so "
            "it is refused rather than answered"
        )
    else:
        # The mark says the target test is OWED; the value it runs against is read
        # HERE, because the class fold can have moved restrictive while the payload
        # was built. A refreshed fold that cannot be read at all comes back
        # ``None`` and the record half refuses on it, which is the same
        # fail-closed answer the gate gives.
        refolded, front_gone = await asyncio.to_thread(_recorded_class_and_front, unit)
        # This is the ONLY arm with a second suspension, so it is the only one whose
        # incoming caller verdict predates a window. Re-read it: a mirror acquired
        # while the fold ran is exactly the state the sample exists to catch, and
        # answering on the pre-offload reading would leave this route's own smaller
        # window open while it closes the payload-build one. Either verdict refusing
        # is enough -- a caller that was publishing at ANY point across this read is
        # refused, so re-reading can only add a refusal, never withdraw one.
        # The workspace arm is re-run here for the same reason the caller test is: a
        # caller can SWITCH workspace while the payload is built, and the switch is
        # exactly what this boundary exists to catch. Re-reading can only add a refusal.
        # Ordered last, matching the gate, so the two agree on which reason a caller sees.
        caller_slot = _live_slot(request.app.get("state"), session_key)
        refusal = (
            caller
            or _caller_class_refusal(request, session_key)
            or _target_class_refusal(request, unit, refolded, front_gone)
            or _workspace_refusal(_caller_workspace(caller_slot), refolded)
        )
    return refusal


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
    """GET /api/crew-log/sessions -- one row per session crew log this caller may see."""
    denied = await _authorize_crew_log_read(request, "session_crew_log.list", listing=True)
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
    # Set by the gate, which refuses rather than leaving it unset -- so an absent
    # value is a programming error, and the fallback narrows to a unit no session
    # can have rather than widening to every unit.
    scope_unit, scope_slot = request.get(LIST_SCOPE_KEY, (_UNSCOPABLE, ""))
    # A listing discloses each row's unit, slot, model and activity. Lineage alone does
    # not bound which WORKSPACE those belong to, and a dispatch edge is written on the
    # child and never rewritten -- so a conductor that dispatched in one workspace and
    # then switched still names those children in its tree. The per-unit door already
    # decides this question; the listing asks it once per lineage row rather than
    # restating the rule, so the two doors cannot drift apart.
    #
    # Cost lands where the lists are small: only a SCOPED caller reaches this, and its
    # scope is its own dispatch tree. The unscoped owner listing -- the large one -- is
    # not narrowed at all and so is never asked.
    caller_workspace = str(request.get(LIST_CALLER_WORKSPACE_KEY, "") or "")

    def _admit(unit_id: str) -> bool:
        recorded = _crew_log_read().recorded_class(unit_id)
        # ``front_gone`` only chooses between two refusal TEXTS, and a listing shows no
        # text -- so it is not measured here, which also spares a directory read per row.
        if _target_class_refusal(request, unit_id, recorded, False):
            return False
        return not _workspace_refusal(caller_workspace, recorded)

    try:
        payload = await asyncio.to_thread(
            _crew_log_read().list_session_units,
            slot_contains=request.query.get("slot_contains", "") or "",
            active_within_ms=active_within_secs * 1000,
            with_type_counts=request.query.get("with_type_counts") in ("1", "true", "True"),
            limit=limit,
            scope_unit=scope_unit,
            scope_slot=scope_slot,
            admit_dispatched=_admit,
        )
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    stale = await _stale_grant_refusal(request, "session_crew_log.list", listing=True)
    if stale is not None:
        return stale
    return web.json_response(payload)


async def api_crew_log_resolve(request: web.Request) -> web.Response:
    """GET /api/crew-log/resolve?key= -- the unit a slot or session key is landing in."""
    # Resolve the NAMED key's unit first so the gate has something to place, then
    # authorize, then answer the request's shape. Resolving is not answering: an
    # empty key is still a 400 AFTER the gate, so a caller that never got past it
    # learns nothing about the key it named and leaves an audit row either way. A
    # key with no live session resolves to no unit, which the scope test refuses for
    # anyone but the owner -- so an unresolvable key cannot be told from an
    # out-of-scope one.
    from kiro_crew.crew_log.resolve import unit_for_session_key

    asked = (request.query.get("key") or "").strip()
    state = request.app.get("state")
    target = unit_for_session_key(getattr(state, "sessions", None), asked) if asked else ""
    denied = await _authorize_crew_log_read(request, "session_crew_log.resolve", unit=target)
    if denied is not None:
        return denied
    key = asked
    if not key:
        return _bad_request("key is required", "unresolvable_key")
    if not env_flag_enabled(CREW_LOG_ENV):
        return _disabled()
    unit = target
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
    denied = await _authorize_crew_log_read(request, "session_crew_log.read", unit=unit)
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
    stale = await _stale_grant_refusal(request, "session_crew_log.read", unit=unit)
    if stale is not None:
        return stale
    if not payload.get("exists"):
        return web.json_response(
            {"error": f"no session crew log for {unit!r}", "code": "unknown_unit"}, status=404
        )
    return web.json_response(payload)


async def api_crew_log_unit_projection(request: web.Request) -> web.Response:
    """GET /api/crew-log/units/{unit}/projection/{name} -- one fold and its seq."""
    unit = _unit_param(request)
    denied = await _authorize_crew_log_read(request, "session_crew_log.projection", unit=unit)
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
    stale = await _stale_grant_refusal(request, "session_crew_log.projection", unit=unit)
    if stale is not None:
        return stale
    return web.json_response({"session_id": unit, **result.to_dict()})


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
