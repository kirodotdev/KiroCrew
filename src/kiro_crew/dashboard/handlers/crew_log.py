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
path, and a gateway launched with the flag set to a falsy value must not pay to
load a store it will not read -- the same split the emitter keeps, and one a test pins from a
clean interpreter.
"""

# This module is the crew log handler's facade: the server, the handlers package and tests
# import and patch every name through it, as ``kiro_crew.dashboard.handlers.crew_log.<name>``.
# A read of a name an owner holds is served from that owner, and a write --
# ``monkeypatch.setattr``, ``mock.patch`` -- reaches every module holding it
# (:mod:`kiro_crew._facade_mirror`). Owners never import this module. It keeps every route
# and the push publisher. New work goes to the owner of its responsibility:
#
#   ``crew_log_scope`` -- who may read which crew log through the unit-keyed door

from __future__ import annotations

import asyncio
import functools
import importlib as _importlib
import logging
import os
import sys as _sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.constants import CREW_LOG_ENV, ENV_FALSY, crew_log_enabled, env_file_display
from kiro_crew.dashboard.handlers._shared import (
    guard_owner_surface_routes,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.crew_log_scope import (
    _UNSCOPABLE,
    LIST_CALLER_WORKSPACE_KEY,
    LIST_SCOPE_KEY,
    _authorize_crew_log_read,
    _disabled,
    _stale_grant_refusal,
    _target_class_refusal,
    _workspace_refusal,
)
from kiro_crew.work_vocab import WORK_FOLD_NAME as _WORK_FOLD

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

#: The frame about a SLOT's crew log. ONE frame kind, two shapes, and the second is
#: additive on purpose.
#:
#: ``{"slot": s}`` alone is the original and still means "re-read this slot": a growth
#: signal with no value, which is all a session-driven pass can say about folds it did not
#: run.
#:
#: ``{"slot": s, "fold": f, "revision": n, "value": {...}}`` is what an EAGER advance
#: of the ``work`` fold sends, and it carries the folded value so the client needs no
#: re-read at all. The client keeps the highest ``revision`` per (slot, fold) and
#: discards anything lower (:func:`kiro_crew.crew_log.projection.fold_slot_warm_revised`
#: mints it).
#:
#: Every OTHER slot fold's advance is sent as ``{"slot": s, "fold": f, "revision": n}``
#: with no value. The browser caches only ``work`` by fold, so for any other fold it
#: drops the value and re-reads the board either way; a valueless frame takes the
#: same path (``takeFoldedSlotProjection`` reads it as the growth signal) without
#: shipping a value per advance per tab that nothing reads.
#:
#: One frame kind rather than two, because the client already routes this one to the same
#: query key the value belongs to, and a second channel would be a second place to keep
#: that mapping correct.
SLOT_FRAME = "slot_projection"

#: Sent to a client AT SUBSCRIBE TIME, before it issues any baseline read:
#: ``{"revisions": {slot: {fold: revision}}}``, the newest revision this process has
#: published for each cell.
#:
#: WHY BEFORE THE BASELINE. A baseline read already on the wire resolves into the same
#: cache entry a pushed frame writes, and it resolves LATER, so it wins -- serving a value
#: older than one the client was already handed, with nothing left to correct it. A floor
#: established first makes that impossible: the client discards any frame at or below its
#: floor and refuses a baseline response older than it.
#:
#: A FLOOR AND NOT A VALUE. It carries revisions, never folded values: a connecting client
#: still reads its own baseline, and sending values for every slot this gateway has folded
#: would push a payload nobody asked for at exactly the moment a tab is slowest.
SLOT_SUBSCRIBED_FRAME = "slot_projection/subscribed"

#: The ``work`` advance as a delta, sent only to a tab that declared ``fold_delta.2``
#: and watches the board, INSTEAD of that board's ``slot_projection`` frame:
#: ``{v, slot, fold, kind, revision, base_revision, ops?}``. ``kind`` is ``delta``
#: (``ops`` holds ``fold_delta`` ops on ``base_revision``) or ``snapshot_needed`` (no
#: ops; re-read the board). A new frame type rather than new fields on
#: ``slot_projection``, so a bundle that predates it can never misread a delta as a
#: value. ``v`` is the frame's schema version; a tab treats one it does not know as
#: ``snapshot_needed``.
DELTA_FRAME: Final[str] = "slot_fold"
DELTA_FRAME_VERSION: Final[int] = 1
DELTA_KIND: Final[str] = "delta"
SNAPSHOT_KIND: Final[str] = "snapshot_needed"

#: Units whose owning slot is remembered. A unit's header is written once and
#: never rewritten, so the answer never goes stale; the bound only caps memory.
MAX_CACHED_SLOT_OWNERS: Final[int] = 512

#: The one slot fold a browser caches, and so the one the exporter subscribes keyed
#: for a watched board.
WORK_FOLD: Final[str] = "work"

#: Above this many boards under the watched ones, the exporter falls back to its
#: wildcard subscription rather than hold a subscription per board.
MAX_KEYED_SLOT_SUBSCRIPTIONS: Final[int] = 512

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


def _owner_served_refusal(name: str) -> web.Response:
    """The answer for a slot-keyed fold its owner serves: the same as an unregistered name."""
    return _bad_request(
        f"projection {name!r} is slot-keyed and is served by its owner, not by this route",
        "unknown_projection",
    )


def _tree_keyed_refusal(name: str) -> web.Response:
    """The answer for a tree-keyed fold on a route that addresses ONE session.

    Shared by BOTH doors that take a fold name -- the owner's cookie-gated per-session
    read and the internal per-unit one -- because the reason is the key kind and not the
    gate: either route would fold the single unit its path names and answer with it as
    the whole tree. A refusal on one door only is not fail-closed.
    """
    return _bad_request(
        f"projection {name!r} is tree-keyed and spans the logs of many sessions, "
        "so it is not served by a route that addresses one session",
        "unknown_projection",
    )


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
    pointer (``session/opened.data.previous``) and a fold that follows it. Both now
    exist -- the pointer on the entry, and ``session_tree.fold_slot_chain``, which
    walks it newest unit first, bounded, cycle-guarded and held to one slot -- but
    this function calls neither and deliberately answers the narrower question. A
    caller that does join them owes the walk's ``ended`` reason as well as its ids:
    only ``first`` reached the slot's first unit, so every other reason is a total
    over PART of a life and presenting it as the whole would be the same false
    implication in a new place. So the honest move here is still to say what is
    addressable, not to guess.
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
    if name == projections.OWNER_SERVED_SLOT_PROJECTION:
        # A slot-keyed fold its OWNER serves: the owner orders the slot's units by
        # what the crew recorded and pins the live unit last, which this route cannot
        # do, and folding the one unit it addresses would serve a part of the record
        # as the whole. Refused the way an unregistered name is.
        return _owner_served_refusal(name)
    if name in projections.TREE_PROJECTION_NAMES:
        # TREE-keyed, and REFUSED here rather than folded. This route addresses ONE
        # session, and a tree fold's population is not a property of the key: it is
        # resolved by walking what the record says -- the root's units, every worker
        # they recorded a bind for, and every board those workers conduct, to closure.
        # Folding this route's one unit would serve a single session as the whole tree,
        # which is the same part-served-as-the-whole this route already refuses for a
        # slot fold its owner serves.
        #
        # Fails CLOSED rather than folding the subset the caller addressed, because a
        # tree over "the part I can reach" is a complete-looking wrong answer. A served
        # tree read belongs with the first consumer that can bound the walk and gate
        # each unit it reaches.
        return _tree_keyed_refusal(name)
    unit_id, _ = _unit_id(request, session_id)
    published: "int | None" = None
    try:
        if name in projections.SLOT_PROJECTION_NAMES:
            # SLOT-keyed: this fold joins every unit the slot ran under, so it is
            # addressed through the slot its resolved unit's HEADER names rather than
            # folded from that one unit. Routed through the same resolution as the
            # per-unit read above, so a caller holding a slot key reaches its own
            # record either way.
            result, published = await asyncio.to_thread(_read_slot_fold_labelled, unit_id, name)
        else:
            # SESSION-keyed, which is what is LEFT: a registered name that is not the
            # owner-served slot fold, not tree-keyed and not slot-keyed. Every
            # registered fold is in exactly one key-kind set -- an import-time guard in
            # the projection module requires that -- so the three branches above
            # account for the other two kinds and nothing unkeyed reaches here.
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
    payload = {"session_id": session_id, **result.to_dict()}
    if published is not None:
        # A delta fold's snapshot label: the base a tab applies the next pushed
        # ``slot_fold`` delta on (see ``_read_slot_fold_labelled``). An extra key, so
        # an older bundle reading this route sees the response it always saw.
        payload["published_revision"] = published
    return web.json_response(payload)


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
        # The WARM memo the eager worker keeps, so a read after a wake is a lookup and
        # each fold carries the revision its pushed frames carry: the panel orders a
        # baseline against a push by that number.
        warm = await asyncio.to_thread(projections.fold_session_warm, unit_id)
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    folded = {name: warm.projection(name).to_dict() for name in projections.PROJECTION_NAMES}
    # ``session_id`` names what the CALLER asked about, the same rule the two older
    # reads follow: a client polling by slot key compares this against the id it
    # sent, and answering with the resolved ACP id would break that comparison and
    # put an internal identity on the wire.
    # Whether the gateway is recording at all. An empty fold cannot say, and the two
    # cases want different words: a session that has not recorded yet, and a gateway
    # whose flag switched recording off.
    recording = crew_log_enabled()
    payload: dict[str, Any] = {
        "session_id": session_id,
        # The unit these folds came from, beside the id the caller asked with. A pushed
        # ``session_projection`` frame names its unit, and the panel applies one only
        # when it names THIS unit: after a slot moves to a new ACP session, a late frame
        # from the old one must not land in the new one's panel.
        "unit": unit_id,
        "projections": folded,
        "resolved": resolved,
        "writes_drained": drained,
        "recording": recording,
    }
    if not recording:
        payload["flag_value"] = value = crew_log_flag_value()
        # Whether the value is one of the switch-off spellings. The panel words an
        # unrecognised one as such, so a typo is not read as the app misspelling it.
        payload["flag_recognised"] = value.lower() in ENV_FALSY
        # The ``.env`` the gateway reads, so the panel names the file to edit.
        payload["env_file"] = env_file_display()
    return web.json_response(payload)


#: The longest flag value the fold read quotes back; a longer one is cut.
_FLAG_VALUE_MAX = 40


def crew_log_flag_value() -> str:
    """The ``KIROCREW_CREW_LOG`` value that switched recording off, as the panel quotes it.

    Only this one variable, and only its printable characters up to
    :data:`_FLAG_VALUE_MAX`: the read is owner-gated, and the operator set the value
    themselves, so quoting it lets them see a typo instead of a list of spellings.
    """
    value = os.environ.get(CREW_LOG_ENV, "").strip()
    return "".join(char for char in value if char.isprintable())[:_FLAG_VALUE_MAX]


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
    return _read_slot_fold_labelled(unit_id, name)[0]


def _read_slot_fold_labelled(unit_id: str, name: str) -> "tuple[Any, int | None]":
    """:func:`_read_slot_fold` and, for a delta fold, its ``published_revision``. Blocking.

    ``published_revision`` is the base the next pushed delta applies on. The value and
    the label come from ``eager.read_labelled``, which folds outside the publisher's
    lock and checks the label under it, so the label is the revision of the value served: a publish read earlier and sent later
    reaches the tab as stale, never as a delta on this newer value. ``None`` for a fold
    that publishes no deltas, which carries no such field.
    """
    from kiro_crew.crew_log import eager, fold_delta

    projections = _crew_log()
    slot = projections.slot_of_session(unit_id)
    if name not in fold_delta.DELTA_FOLDS:
        return projections.read_slot_projection(slot, name), None
    if not slot:
        return projections.read_slot_projection(slot, name), 0
    folded, label = eager.read_labelled(
        slot, name, lambda: projections.read_slot_projection(slot, name)
    )
    return folded, label


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


def _unit_param(request: web.Request) -> str:
    """The unit id from the path, already percent-decoded by the router."""
    return (request.match_info.get("unit") or "").strip()


async def _offload_then_recheck(
    request: web.Request,
    operation: str,
    work: Callable[[], Any],
    respond: Callable[[Any], web.Response],
    *,
    unit: str = "",
    listing: bool = False,
) -> web.Response:
    """Read off the loop, re-check the grant the read suspended, then answer.

    An agent-door read that suspends owes four steps in one order: authorize, build
    the payload off the loop, re-check the grant, answer. The gate is the route's own
    first line because what it authorizes differs per route; the other three are the
    same three calls in the same order everywhere, and they live here so that a route
    cannot answer out of that order. The route hands over the read and the shaping and
    never names the payload, so "answered without re-checking" is not a shape a route
    can write -- which is a stronger guarantee than every route happening to get the
    order right.

    ``work`` is the read, already bound to its arguments, and runs in a thread.
    ``respond`` turns the payload into the response and belongs to the ROUTE, because
    what a payload means differs per route: a page with no log is a 404, a fold is
    wrapped with the unit it was folded from, a listing is returned as it stands. It
    is called only once the re-check holds, so a withdrawn grant reaches the caller as
    the refusal and the route's shaping never runs at all.

    A storage refusal is mapped here rather than per route because all three reads map
    it the same way: :func:`_crew_log_refusal` keeps the code the caller can act on.
    """
    from kiro_crew.crew_log.errors import CrewLogError

    try:
        payload = await asyncio.to_thread(work)
    except CrewLogError as exc:
        return _crew_log_refusal(exc)
    stale = await _stale_grant_refusal(request, operation, unit=unit, listing=listing)
    if stale is not None:
        return stale
    return respond(payload)


async def api_crew_log_sessions(request: web.Request) -> web.Response:
    """GET /api/crew-log/sessions -- one row per session crew log this caller may see."""
    denied = await _authorize_crew_log_read(request, "session_crew_log.list", listing=True)
    if denied is not None:
        return denied
    if not crew_log_enabled():
        return _disabled()
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

    return await _offload_then_recheck(
        request,
        "session_crew_log.list",
        functools.partial(
            _crew_log_read().list_session_units,
            slot_contains=request.query.get("slot_contains", "") or "",
            active_within_ms=active_within_secs * 1000,
            with_type_counts=request.query.get("with_type_counts") in ("1", "true", "True"),
            limit=limit,
            scope_unit=scope_unit,
            scope_slot=scope_slot,
            admit_dispatched=_admit,
        ),
        # A listing is returned as it stands: each row was admitted as it was
        # gathered, so there is nothing left to shape.
        web.json_response,
        listing=True,
    )


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
    if not crew_log_enabled():
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
    if not crew_log_enabled():
        return _disabled()
    try:
        start, end = _span(request)
    except ValueError as exc:
        return _bad_request(str(exc), "bad_range")

    def _respond(payload: Any) -> web.Response:
        if not payload.get("exists"):
            return web.json_response(
                {"error": f"no session crew log for {unit!r}", "code": "unknown_unit"}, status=404
            )
        return web.json_response(payload)

    return await _offload_then_recheck(
        request,
        "session_crew_log.read",
        functools.partial(_read_page, unit, start, end),
        _respond,
        unit=unit,
    )


async def api_crew_log_unit_projection(request: web.Request) -> web.Response:
    """GET /api/crew-log/units/{unit}/projection/{name} -- one fold and its seq."""
    unit = _unit_param(request)
    denied = await _authorize_crew_log_read(request, "session_crew_log.projection", unit=unit)
    if denied is not None:
        return denied
    if not crew_log_enabled():
        return _disabled()
    from kiro_crew.crew_log.errors import CrewLogError

    projections = _crew_log()
    name = request.match_info.get("name", "")
    try:
        projections.require_name(name)
    except CrewLogError as exc:
        return _bad_request(exc.message, "unknown_projection")
    if name == projections.OWNER_SERVED_SLOT_PROJECTION:
        # Same refusal as the per-session route: this fold is slot-keyed and served
        # by its owner; a per-unit fold of it would be a part served as the whole.
        return _owner_served_refusal(name)
    if name in projections.TREE_PROJECTION_NAMES:
        # TREE-keyed, refused here for the same reason as on the per-session route and
        # with the same helper: this route folds the ONE unit its path names, while a
        # tree fold's population is resolved by walking the record -- the root's units,
        # every worker they recorded a bind for, and every board those workers conduct,
        # to closure. Folding one unit would answer with a single session dressed as the
        # whole tree, which is a complete-looking wrong answer rather than a visibly
        # partial one.
        #
        # Stated on BOTH doors because the two gates are separate: this one admits an
        # internal caller on a forwarded session key, the other the owner's cookie, so a
        # refusal on one is not a refusal on the other.
        return _tree_keyed_refusal(name)
    if name in projections.SLOT_PROJECTION_NAMES:
        # This route folds ONE unit. A slot-keyed fold joins every unit the slot ran
        # under (and, for the work board, its bound workers' units), so one unit's
        # answer would be a partial record that reads as whole. The session route
        # resolves the slot from the unit's header and serves these names whole.
        return _bad_request(
            f"projection {name!r} is keyed by slot, not by session", "slot_projection"
        )

    def _respond(result: Any) -> web.Response:
        # The fold is named by the unit it was folded from, which the reader needs
        # and the fold itself does not carry.
        return web.json_response({"session_id": unit, **result.to_dict()})

    return await _offload_then_recheck(
        request,
        "session_crew_log.projection",
        functools.partial(projections.read_projection, unit, name),
        _respond,
        unit=unit,
    )


# --------------------------------------------------------------------------- #
# The push
# --------------------------------------------------------------------------- #


class CrewLogPublisher:
    """Pushes folded crew-log values to the dashboard's sockets.

    One instance per gateway, installed at startup. TWO SOURCES feed it, and both end
    in the same frames. The crew-log bus hands it every fold the eager worker advanced
    (:meth:`on_fold_advanced`), which is the normal path: the value is already folded
    and only needs sending. The emitter's growth listener (:meth:`notify`) is the
    BACKSTOP for a wake the eager queue dropped: a coalesced pass reads the same warm
    session memo (``fold_session_warm``), which is a lookup when the eager worker got
    there first and a short continuation when it did not.

    A session frame is sent only for a fold whose REVISION is above the last one this
    publisher sent for that (session, fold), so the two sources never send one value
    twice and an idle session sends nothing. A seq could not do this: it restarts when a
    unit is recreated under the same id, where a revision only rises.
    """

    def __init__(self, state: Any) -> None:
        self._state = state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dirty: set[str] = set()
        # The newest revision SENT per (session, fold), bounded LRU by session. What
        # stops the bus and the growth backstop sending one value twice. Touched only on
        # the loop.
        self._sent: "OrderedDict[str, dict[str, int]]" = OrderedDict()
        # Session values the bus delivered and the current window has not sent yet: the
        # newest (revision, seq, value) per (session, fold). See :meth:`_schedule_session`.
        self._pending: "dict[tuple[str, str], tuple[int, int, dict[str, Any]]]" = {}
        self._sessions_armed = False
        self._bus_disposers: "list[Callable[[], None]]" = []
        # The slot-fold subscriptions, by (key, fold); ``("*", "*")`` is the wildcard.
        # See :meth:`sync_slot_subscriptions`.
        self._slot_subs: "dict[tuple[str, str], Callable[[], None]]" = {}
        self._slot_sub_lock = threading.Lock()
        self._watch_listener_remove: "Callable[[], None] | None" = None
        self._slot_owners: "OrderedDict[str, str]" = OrderedDict()
        # The newest revision published per (slot, fold), which is what a connecting
        # client is handed as its floor. Written on the fold worker's thread and read on
        # the loop, so both sides take ``_revisions_lock``: a first write for a slot
        # adds a key, and a reader iterating the map at that moment would raise.
        # Bounded LRU by slot at ``MAX_CACHED_SLOT_OWNERS``, like its siblings: the keys
        # are slot names entries carry, so an unbounded map would grow for the life of
        # the gateway and be serialized onto every socket at accept. A slot evicted
        # here only means a new tab gets no floor for it, which is a tab that accepts
        # the first revision it sees -- correct, because nothing was pushed to it.
        self._revisions: "OrderedDict[str, dict[str, int]]" = OrderedDict()
        self._revisions_lock = threading.Lock()
        self._scheduled = False
        # A flush pass runs to completion before the next one starts. Without
        # this, a growth arriving during a slow fold would schedule a second
        # overlapping pass, and two ``_publish`` for one session would share the
        # same ``before`` bundle and race the cache write, so an older seq could
        # land and be broadcast last. When a pass finishes with more work marked,
        # it schedules the next pass itself.
        self._flushing = False
        # True while a lineage push is armed on the loop (see :meth:`_mark_tree`).
        self._tree_scheduled = False

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

    # -- fold worker thread, via the crew-log bus --------------------------- #

    def subscribe_bus(self) -> None:
        """Subscribe this publisher to the crew-log bus, replacing any earlier pair.

        TWO keyed fold subscriptions, one per scope, so each handler receives only its
        own kind of fold and the bus never hands a slot event to the session path or the
        reverse, plus one for the session tree. The disposers are KEPT: a second install in the same process (a gateway
        restarted inside one interpreter) disposes the old pair before subscribing the
        new one, so this publisher is never called twice for one event.
        """
        # boot-path import gate: the bus is imported where the publisher is installed.
        from kiro_crew.crew_log import bus as crew_log_bus

        for dispose in self._bus_disposers:
            dispose()
        self._drop_slot_subscriptions()
        # The slot subscription first, as before: the wildcard one unless every open
        # owner tab routes by watch set (see :meth:`sync_slot_subscriptions`).
        self.sync_slot_subscriptions()
        add_listener = getattr(self._state, "add_fold_watch_listener", None)
        if callable(add_listener):
            try:
                self._watch_listener_remove = add_listener(self.sync_slot_subscriptions)
            except Exception:  # pragma: no cover - a facade without the hub API
                logger.debug("fold watch listener could not be added", exc_info=True)
        self._bus_disposers = [
            crew_log_bus.subscribe(
                crew_log_bus.FOLD_ADVANCED,
                self.on_session_fold,
                scope=crew_log_bus.SCOPE_SESSION,
            ),
            # The session tree's change event: an open, a takeover, a release, a forget,
            # or the seed landing. The sidebar's nesting is pushed from here.
            crew_log_bus.subscribe(crew_log_bus.TREE_ADVANCED, self.on_tree_advanced),
        ]

    def unsubscribe_bus(self) -> None:
        """Drop every bus subscription. Idempotent."""
        disposers, self._bus_disposers = self._bus_disposers, []
        for dispose in disposers:
            dispose()
        self._drop_slot_subscriptions()

    # -- slot subscriptions: wildcard, or keyed by watched board ------------ #

    def _drop_slot_subscriptions(self) -> None:
        """Remove the slot subscriptions and the watch listener; eager folds every cell."""
        from kiro_crew.crew_log import bus as crew_log_bus

        remove, self._watch_listener_remove = self._watch_listener_remove, None
        if remove is not None:
            remove()
        with self._slot_sub_lock:
            subs, self._slot_subs = self._slot_subs, {}
            crew_log_bus.set_slot_folds_on_demand(False)
            crew_log_bus.set_delta_boards(None)
        for dispose in subs.values():
            dispose()

    def _watched_boards(self) -> "frozenset[str] | None":
        """The boards to subscribe keyed, or ``None`` for the wildcard subscription.

        ``None`` while any legacy owner socket is open (it receives every board, so
        every fold must still be rendered), while no capable socket is open (no tab
        at all is today's case, and stays today's behaviour), and whenever the answer
        cannot be computed. Otherwise the union of the watch sets plus every board
        created under a watched one, because a frame for a descendant board is routed
        to a tab watching its ancestor.
        """
        routing = getattr(self._state, "fold_watch_routing", None)
        if not callable(routing):
            return None
        try:
            legacy, capable, watched = routing()
        except Exception:
            logger.debug("fold watch routing could not be read", exc_info=True)
            return None
        if legacy or not capable:
            return None
        return self._expand_boards(watched)

    def _delta_boards(self) -> "frozenset[str] | None":
        """The boards whose ``work`` publishes should hold a delta base, or ``None``: all.

        The boards watched by an open socket that applies ``slot_fold`` deltas, plus
        every board created under one, expanded as :meth:`_watched_boards` expands its
        own set. ``None`` (hold a base for every board, the bounded default) whenever
        that cannot be computed.
        """
        union = getattr(self._state, "fold_apply_watch", None)
        if not callable(union):
            return None
        try:
            watched = union()
        except Exception:
            logger.debug("fold apply watch could not be read", exc_info=True)
            return None
        return self._expand_boards(frozenset(watched))

    def _expand_boards(self, watched: "frozenset[str]") -> "frozenset[str] | None":
        """*watched* as fold keys plus every board created under one, or ``None``."""
        if not watched:
            return frozenset()
        try:
            from kiro_crew.crew_log import session_tree_projection as tree

            if not tree.projection().seeded_for_current_store:
                # An unseeded tree names no children, which is not the same as having
                # none: fold everything until it can answer.
                return None
            roots = self._fold_cells_for(watched)
            if roots is None:
                return None
            boards: set[str] = set()
            frontier = list(roots)
            while frontier:
                slot = frontier.pop()
                if slot in boards:
                    continue
                boards.add(slot)
                if len(boards) > MAX_KEYED_SLOT_SUBSCRIPTIONS:
                    return None
                frontier.extend(tree.created_children(slot))
        except Exception:
            logger.debug("watched boards could not be resolved", exc_info=True)
            return None
        return frozenset(boards)

    def _fold_cells_for(self, watched: "frozenset[str]") -> "set[str] | None":
        """The keys a fold may name each watched board by, or ``None`` if unknown.

        A tab watches by slot key (``chat-7``, ``discord_agent_direct_123``); a fold
        names its board by session key (``dashboard:chat-7``,
        ``discord:agent:direct:123``). The channel fold is not reversible, so a live
        slot's own session key is read; a channel-shaped key no live slot resolves
        returns ``None`` and the exporter keeps its wildcard subscription.
        """
        from kiro_crew.dashboard.chat_utils_slots import effective_session_key
        from kiro_crew.dashboard.websocket_hub import board_key
        from kiro_crew.messaging.link import is_channel_session_key

        live: "dict[str, set[str]]" = {}
        for name, slot in list((getattr(self._state, "_slots", None) or {}).items()):
            try:
                session_key = effective_session_key(slot)
            except Exception:
                continue
            live.setdefault(board_key(session_key), set()).update({name, session_key})
        cells: set[str] = set()
        for board in watched:
            resolved = live.get(board, set())
            if not resolved and is_channel_session_key(board):
                return None
            cells.update({board, f"dashboard:{board}", *resolved})
        return cells

    def sync_slot_subscriptions(self) -> None:
        """Make this exporter's slot subscriptions match the open tabs. On the loop.

        WILDCARD (``scope=slot``, any key, any fold), exactly the subscription this
        exporter always held, while :meth:`_watched_boards` says ``None``. Otherwise
        ONE keyed subscription per watched board for the ``work`` fold, the only slot
        fold a browser caches, and the bus is told it may let the eager folder skip a
        cell nobody subscribes to. New subscriptions are added before old ones are
        removed, so no event falls between the two.
        """
        from kiro_crew.crew_log import bus as crew_log_bus

        boards = self._watched_boards()
        delta_boards = self._delta_boards()
        if boards is None:
            want: "set[tuple[str, str]]" = {("*", "*")}
        else:
            want = {(board, WORK_FOLD) for board in boards}
        with self._slot_sub_lock:
            have = self._slot_subs
            added: "dict[tuple[str, str], Callable[[], None]]" = {}
            for cell in want - have.keys():
                if cell == ("*", "*"):
                    added[cell] = crew_log_bus.subscribe(
                        crew_log_bus.FOLD_ADVANCED,
                        self.on_slot_fold,
                        scope=crew_log_bus.SCOPE_SLOT,
                    )
                else:
                    added[cell] = crew_log_bus.subscribe(
                        crew_log_bus.FOLD_ADVANCED,
                        self.on_slot_fold,
                        scope=crew_log_bus.SCOPE_SLOT,
                        key=cell[0],
                        fold=cell[1],
                    )
            removed = [have[cell] for cell in have.keys() - want]
            self._slot_subs = {
                **{cell: d for cell, d in have.items() if cell in want},
                **added,
            }
            # Turned on only once the keyed subscriptions exist, and off before the
            # wildcard one is the only one left, so no wanted fold is ever skipped.
            crew_log_bus.set_slot_folds_on_demand(boards is not None)
            # A delta base (a whole board render) is held only for a board some tab
            # that applies deltas watches. Narrowed or widened with the watch sets.
            crew_log_bus.set_delta_boards(delta_boards)
        for dispose in removed:
            dispose()

    def slot_subscription_cells(self) -> "frozenset[tuple[str, str]]":
        """The (key, fold) cells subscribed, ``("*", "*")`` for the wildcard. For tests."""
        with self._slot_sub_lock:
            return frozenset(self._slot_subs)

    def on_fold_advanced(self, event: Any) -> None:
        """Route one :class:`FoldAdvanced` to the handler for its scope.

        The bus subscriptions call :meth:`on_slot_fold` and :meth:`on_session_fold`
        directly, already filtered by scope; this is the entry for a caller holding an
        event of either kind.
        """
        scope = str(getattr(event, "scope", "") or "")
        if scope == "slot":
            self.on_slot_fold(event)
        elif scope == "session":
            self.on_session_fold(event)

    def on_slot_fold(self, event: Any) -> None:
        """THE WS EXPORTER for a slot fold: one frame per event, no coalesce.

        Called on the crew log's FOLD WORKER thread, synchronously inside
        ``bus.publish``. Same shape and same reason as :meth:`notify`: no I/O, no lock,
        hand the frame to the loop that owns the sockets and return. The value arrives
        already folded, so there is nothing to read.

        NOT COALESCED, which is the folder's doing rather than an omission: it drains its
        whole queue per batch and folds each (key, fold) once, so a turn's burst reaches
        this once per fold it moved.

        The event is read DEFENSIVELY -- a bus carries whatever a publisher sends, and a
        malformed one must cost this frame rather than the fan-out to the next subscriber.
        """
        unpacked = self._unpack(event)
        loop = self._loop
        if unpacked is None or loop is None:
            return
        key, fold, revision, _seq, value = unpacked
        delta = self._delta_frame(event, key, fold, revision)
        try:
            self._record_revision(key, fold, revision)
            loop.call_soon_threadsafe(self._push_fold, key, fold, revision, value, delta)
        except RuntimeError:
            logger.debug("crew log fold for %s/%s arrived after the loop closed", key, fold)

    def on_session_fold(self, event: Any) -> None:
        """THE WS EXPORTER for a session fold: held for the next coalesced send.

        Same thread and same contract as :meth:`on_slot_fold`. A SESSION fold that is
        not advertised (``class``) is not sent: its one reader asks for it by name, and a
        browser cannot draw it.
        """
        unpacked = self._unpack(event)
        loop = self._loop
        if unpacked is None or loop is None:
            return
        key, fold, revision, seq, value = unpacked
        if fold not in _crew_log().PROJECTION_NAMES:
            return
        try:
            loop.call_soon_threadsafe(self._schedule_session, key, fold, revision, seq, value)
        except RuntimeError:
            logger.debug("crew log fold for %s/%s arrived after the loop closed", key, fold)

    @staticmethod
    def _unpack(event: Any) -> "tuple[str, str, int, int, dict[str, Any]] | None":
        """``(key, fold, revision, seq, value)`` from *event*, or ``None`` if malformed."""
        key = str(getattr(event, "key", "") or "")
        fold = str(getattr(event, "fold", "") or "")
        revision = int(getattr(event, "revision", 0) or 0)
        seq = int(getattr(event, "seq", 0) or 0)
        value = getattr(event, "value", None)
        if not key or not fold or revision <= 0 or not isinstance(value, dict):
            return None
        return key, fold, revision, seq, value

    @staticmethod
    def _delta_frame(event: Any, slot: str, fold: str, revision: int) -> "dict[str, Any] | None":
        """The ``slot_fold`` data for *event* (:data:`DELTA_FRAME`), or ``None``.

        Only a ``work`` event the publisher labelled ``delta`` or ``snapshot_needed``
        has one. A ``snapshot_needed`` frame carries no ops: the tab pulls the snapshot
        over REST, which is the one read that labels a value with the base the next
        delta applies on. Ops that are not a mapping make the frame a
        ``snapshot_needed`` too, so a tab is never handed something it cannot apply.
        """
        if fold != WORK_FOLD:
            return None
        kind = getattr(event, "kind", "value")
        if kind not in (DELTA_KIND, SNAPSHOT_KIND):
            return None
        ops = getattr(event, "delta", None)
        base = int(getattr(event, "base_revision", 0) or 0)
        data: dict[str, Any] = {
            "v": DELTA_FRAME_VERSION,
            "slot": slot,
            "fold": fold,
            "kind": SNAPSHOT_KIND,
            "revision": int(revision),
            "base_revision": 0,
        }
        if kind == DELTA_KIND and base > 0 and isinstance(ops, Mapping):
            data["kind"] = DELTA_KIND
            data["base_revision"] = base
            data["ops"] = ops
        return data

    def on_tree_advanced(self, event: Any) -> None:
        """The session tree moved. Called on whichever thread changed it.

        That is the emitter's writer thread for an open or a takeover, so this does what
        :meth:`notify` does: no I/O, no lock, one hop to the loop. The event carries no
        tree -- the push re-joins the projection against the live slots on the loop.
        """
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._mark_tree)
        except RuntimeError:
            logger.debug("session tree change arrived after the loop closed")

    # -- event loop --------------------------------------------------------- #

    def _mark_tree(self) -> None:
        """Arm ONE coalesced lineage push. On the loop.

        A burst -- a lead opening six workers, a seed landing beside a turn's open -- is
        one push after :data:`COALESCE_SECONDS`, not one per change.
        """
        if self._tree_scheduled or self._loop is None:
            return
        self._tree_scheduled = True
        self._loop.call_later(COALESCE_SECONDS, self._push_tree)

    def _push_tree(self) -> None:
        """Send the slots whose ``parent`` moved. On the loop. Never raises."""
        self._tree_scheduled = False
        if self._watch_listener_remove is not None:
            # A board created under a watched one joins the keyed subscriptions.
            try:
                self.sync_slot_subscriptions()
            except Exception:  # pragma: no cover - a resync must not stop the push
                logger.debug("slot subscription resync failed", exc_info=True)
        push = getattr(self._state, "push_lineage_patch", None)
        if push is None:
            return
        try:
            push()
        except Exception:
            logger.debug("session lineage push failed", exc_info=True)

    def _push_fold(
        self,
        slot: str,
        fold: str,
        revision: int,
        value: "dict[str, Any]",
        delta: "dict[str, Any] | None" = None,
    ) -> None:
        """Broadcast one slot fold's advance. On the loop.

        Only the ``work`` fold's frame carries its value: it is the one fold the browser
        seeds into a cache by fold. Every other slot fold is sent without ``value``,
        which the browser reads as the growth signal and answers with the same
        invalidations it runs for a valued frame of that fold (see :data:`SLOT_FRAME`).
        The bus event itself is untouched, so in-process subscribers still get the
        whole value.

        An empty room is still a broadcast, which costs the hub one membership check and
        keeps this path free of a second liveness rule to keep in step with
        :meth:`_watchers`.
        """
        data: dict[str, Any] = {"slot": slot, "fold": fold, "revision": int(revision)}
        if fold == _WORK_FOLD:
            data["value"] = value
        broadcast_slot_fold = getattr(self._state, "broadcast_slot_fold", None)
        if delta is not None and callable(broadcast_slot_fold):
            # A tab that applies deltas and watches this board gets *delta* instead;
            # every other socket gets *data*, exactly as before (websocket_hub).
            broadcast_slot_fold(slot, data, delta)
            return
        self._state.broadcast_ws_owners(SLOT_FRAME, data)

    def _schedule_session(
        self, session_id: str, fold: str, revision: int, seq: int, value: "dict[str, Any]"
    ) -> None:
        """Queue one eagerly folded session value for the next coalesced send. On the loop.

        COALESCED, unlike a slot fold, because a session fold moves on EVERY entry --
        ``status`` counts them all, including each streamed ``message/chunk`` -- and the
        eager worker folds as fast as entries land. Sending each of those values would
        put a frame per chunk on every owner socket, where the panel can show one every
        :data:`COALESCE_SECONDS`. So the newest value per (session, fold) is held and the
        window sends it; the fold itself is not delayed, only the frame.
        """
        held = self._pending.get((session_id, fold))
        if held is None or revision > held[0]:
            self._pending[(session_id, fold)] = (revision, seq, value)
        if self._sessions_armed:
            return
        loop = self._loop
        if loop is None:
            return
        self._sessions_armed = True
        loop.call_later(COALESCE_SECONDS, self._drain_sessions)

    def _drain_sessions(self) -> None:
        """Send every held session value. On the loop, at the end of a window."""
        self._sessions_armed = False
        pending, self._pending = self._pending, {}
        loop = self._loop
        if loop is None or not pending:
            return
        task = loop.create_task(self._send_pending(pending))
        task.add_done_callback(_report_send_failure)

    async def _send_pending(
        self, pending: "dict[tuple[str, str], tuple[int, int, dict[str, Any]]]"
    ) -> None:
        for (session_id, fold), (revision, seq, value) in pending.items():
            await self._send_session(session_id, fold, revision, seq, value)

    async def _send_session(
        self,
        session_id: str,
        fold: str,
        revision: int,
        seq: int,
        value: "dict[str, Any]",
        slot: "str | None" = None,
    ) -> bool:
        """Broadcast one session fold's value if it is newer than the last one sent.

        *slot* is the session's slot when the caller already resolved it this pass, so a
        pass over seven folds asks the header once rather than seven times.
        """
        sent = self._sent.get(session_id)
        if sent is not None and sent.get(fold, 0) >= revision:
            return False
        if slot is None:
            slot = await self._slot_of(session_id)
        # Re-read after the await: a newer value may have been sent while the slot was
        # being resolved, and sending this one after it would put an older value last.
        sent = self._sent.get(session_id)
        if sent is not None and sent.get(fold, 0) >= revision:
            return False
        if sent is None:
            sent = self._sent[session_id] = {}
        sent[fold] = revision
        self._sent.move_to_end(session_id)
        while len(self._sent) > MAX_CACHED_SESSIONS:
            self._sent.popitem(last=False)
        self._state.broadcast_ws_owners(
            FRAME,
            {
                "session_id": session_id,
                "slot": slot,
                "name": fold,
                "seq": int(seq),
                "value": value,
                "revision": int(revision),
            },
        )
        return True

    def _record_revision(self, slot: str, fold: str, revision: int) -> None:
        """Remember *slot*'s *fold* was published at *revision*. Any thread."""
        with self._revisions_lock:
            folds = self._revisions.get(slot)
            if folds is None:
                folds = self._revisions[slot] = {}
            folds[fold] = max(folds.get(fold, 0), revision)
            self._revisions.move_to_end(slot)
            while len(self._revisions) > MAX_CACHED_SLOT_OWNERS:
                self._revisions.popitem(last=False)

    def known_revisions(self) -> "dict[str, dict[str, int]]":
        """The newest revision this process has published, per slot and fold.

        What a client is told AT SUBSCRIBE TIME, before it issues its baseline reads
        (:data:`SLOT_SUBSCRIBED_FRAME`). It is a floor and not a value: the client records
        it, then discards any frame at or below it AND refuses to let a baseline response
        that is older than it stand.

        That ordering is the point. A baseline read already on the wire resolves into the
        same cache entry a pushed frame writes, so without a floor established first the
        older response wins by arriving last, and nothing is left to correct it.

        A COPY taken under the lock, because the caller hands this to a serializer on
        the loop while the fold worker may be writing the live map.
        """
        with self._revisions_lock:
            return {slot: dict(folds) for slot, folds in self._revisions.items()}

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

        grown: dict[str, str] = {}
        for session_id in sessions:
            try:
                slot = await self._slot_of(session_id)
                if await self._publish(session_id, slot):
                    grown[session_id] = slot
            except CrewLogError as exc:
                logger.debug("crew log fold refused for %s: %s", session_id, exc)
            except Exception:  # pragma: no cover - a push must not kill the loop
                logger.debug("crew log publish failed for %s", session_id, exc_info=True)
        # One frame per slot per pass: a slot's units growing together, or a team
        # whose sessions all grew in one burst, is one re-read, not one each.
        slots: dict[str, None] = {slot: None for slot in grown.values() if slot}
        for slot in slots:
            self._state.broadcast_ws_owners(SLOT_FRAME, {"slot": slot})

    def _watchers(self) -> bool:
        """Whether a dashboard user has a socket open."""
        probe = getattr(self._state, "dashboard_user_ws_count", None)
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - a probe failure is not a verdict
            return False

    async def _publish(self, session_id: str, slot: "str | None" = None) -> bool:
        """Push every advertised fold of *session_id* that moved; whether any did.

        The growth BACKSTOP. It reads the warm session memo the eager worker writes, so
        when that worker already folded this growth the pass is a lookup and the
        revisions it finds have already been sent; when the worker's wake was dropped,
        this is the pass that folds and sends.

        "Moved" is a revision above the last one SENT, so a log removed and recreated
        under the same id -- which can come back at the same terminal seq carrying
        different values -- still pushes: the memo refuses to continue a different
        file and mints new revisions for every fold it rebuilt.
        """
        projections = _crew_log()
        warm = await asyncio.to_thread(projections.fold_session_warm, session_id)
        moved = False
        for name in projections.PROJECTION_NAMES:
            rendered = warm.projection(name)
            if rendered.revision <= 0:
                continue
            if slot is None:
                slot = await self._slot_of(session_id)
            if await self._send_session(
                session_id, name, rendered.revision, rendered.seq, rendered.value, slot
            ):
                moved = True
        return moved

    async def _slot_of(self, session_id: str) -> str:
        """The slot *session_id*'s unit belongs to, from its immutable header."""
        cached = self._slot_owners.get(session_id)
        if cached is not None:
            self._slot_owners.move_to_end(session_id)
            return cached
        slot: str = await asyncio.to_thread(_crew_log().slot_of_session, session_id)
        # An unprovable owner is not cached: the header may not be written yet.
        if slot:
            self._slot_owners[session_id] = slot
            while len(self._slot_owners) > MAX_CACHED_SLOT_OWNERS:
                self._slot_owners.popitem(last=False)
        return slot

    # -- lifecycle ---------------------------------------------------------- #

    def bind(self, loop: asyncio.AbstractEventLoop, state: Any = None) -> None:
        """Point this publisher at the loop, and the state, now serving.

        The STATE is rebound too, not just the loop. A publisher reused across a
        restart inside one process would otherwise keep broadcasting through the
        retired state -- so ``_watchers`` counts the old hub's sockets and every
        frame goes to a room nobody is in, which looks exactly like a session that
        stopped updating.

        EVERY scheduling flag belongs to the loop that is going away: a timer armed on
        it will never fire, and a flush marked in flight there will never finish. Left
        set, ``_scheduled`` makes ``_mark`` believe a pass is already coming,
        ``_flushing`` makes ``_run`` yield to a pass that does not exist, and
        ``_sessions_armed`` makes ``_schedule_session`` hold every session value for a
        window that never closes -- so the publisher goes quiet for good and the held
        map only grows. All three are cleared here.

        The WORK they guarded is kept and re-armed on this loop: the dirty set (those
        sessions did grow, the entries are on disk) and the held session values (the
        newest per cell, which no later fold may repeat). Called on *loop*, so the
        timers can be armed directly.
        """
        self._loop = loop
        if state is not None:
            self._state = state
        tree_owed = self._tree_scheduled
        self._scheduled = False
        self._flushing = False
        self._sessions_armed = False
        self._tree_scheduled = False
        if tree_owed:
            # A push armed on the retiring loop never fires; re-arm it here so a tree
            # change that landed during the swap still reaches the new sockets.
            self._tree_scheduled = True
            loop.call_later(COALESCE_SECONDS, self._push_tree)
        if self._dirty:
            self._scheduled = True
            loop.call_later(COALESCE_SECONDS, self._run)
        if self._pending:
            self._sessions_armed = True
            loop.call_later(COALESCE_SECONDS, self._drain_sessions)


def _report_send_failure(task: "asyncio.Task[Any]") -> None:
    """Log a session send that raised. A push must never take the loop down."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.debug("crew log session push failed", exc_info=error)


_publisher: CrewLogPublisher | None = None


def live_publisher() -> "CrewLogPublisher | None":
    """The installed publisher, or ``None`` when the crew log is off or not yet installed.

    An accessor rather than the module global, so a caller reading the revision floor on
    the socket path does not reach into another module's private name.
    """
    return _publisher


def install_crew_log_publisher(state: Any) -> CrewLogPublisher | None:
    """Register the crew-log push with the emitter, once per process.

    Returns ``None`` and does nothing when the crew log is switched off. The gateway
    calls this once its listener is serving, so building the publisher never delays
    the bind, and a launch that switches the flag off does not pay for a subsystem it
    will not use at all: the flag is read from the environment here, before
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
    if not crew_log_enabled():
        return None
    loop = asyncio.get_running_loop()
    if _publisher is not None:
        _publisher.bind(loop, state)
        _publisher.subscribe_bus()
        return _publisher
    from kiro_crew.crew_log import emit as crew_log_emit

    _publisher = CrewLogPublisher(state)
    _publisher.bind(loop, state)
    crew_log_emit.add_growth_listener(_publisher.notify)
    # THE BUS SUBSCRIPTIONS, and the reason they are here rather than in the folder: this
    # is the one place the dashboard state exists, so this is where a consumer of a
    # crew-log event can be attached without the crew log naming the dashboard. The
    # growth listener says a session's file moved; the bus says a fold's value moved and
    # what it is, or that the session tree moved. A re-install swaps them through their
    # disposers.
    _publisher.subscribe_bus()
    # The bus keeps nothing for a late subscriber, and a client can read the slot list
    # before this line runs: its seed may already have landed and announced to nobody.
    # One push now re-joins the tree against the live slots, so that page still nests.
    _publisher._mark_tree()
    return _publisher


guard_owner_surface_routes(globals(), member_scoped=frozenset())


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = ("kiro_crew.dashboard.handlers.crew_log_scope",)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.crew_log_scope import (  # noqa: F401
        _LINEAGE_GRANT,
        _OUT_OF_SCOPE_REFUSAL,
        _OWNER_GRANT,
        _UNATTENDED_NAMESPACES,
        CREW_LOG_MCP_CALLER,
        TARGET_CLASS_KEY,
        _audit_crew_log_read,
        _caller_class_refusal,
        _caller_own_unit,
        _caller_session_key,
        _caller_slot,
        _caller_workspace,
        _forbidden,
        _front_of_log_is_gone,
        _list_scope,
        _live_slot,
        _live_slot_for_unit,
        _owner_session_refusal,
        _read_scope_refusal,
        _recorded_class_and_front,
        _recorded_class_refusal,
        _slot_half,
        _unit_recheck_refusal,
        crew_log_enable_hint,
    )
