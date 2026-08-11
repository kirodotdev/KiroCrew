"""Crew HTTP surface — the Crews dashboard page and the crews' own writes.

Registered from ``routes.register_routes`` (one import, one call) rather than by
the app manifest, so this app still has exactly ONE place that lists its routes.
It lives in its own module because ``routes.py`` is already 200KB and the crew
surface is a separate feature with its own store; the shared request plumbing
(``_key_from_request``, ``_str_field``, ``_st``, ``_require_enabled``,
``_pr_action_preamble``, ``_audit``) is imported from ``routes`` rather than
re-derived, because a second copy of a gate is how one of them eventually ships
without the check.

Routes, all under ``/api/apps/issue-radar``:

  GET    /crews?owner&repo          -> {owner,repo,provider,host, crews[], settings,
                                        counts{on_duty,working,paused}}
  POST   /crews                     -> {crew}
  GET    /crews/names?owner&repo    -> {suggestions[]}
  GET    /crew?owner&repo&id        -> {owner,repo,provider,host, crew, items[], events[],
                                        settings, skipped_numbers[], recent_skips[],
                                        counts{open}}
  PUT    /crew                      -> {crew}
  DELETE /crew                      -> {crew}          (retire — the record survives)
  PUT    /crew/work                 -> {item, event, skip}
  POST   /crew/pause                -> {crew}
  GET    /crews/settings?owner&repo -> {settings}
  PUT    /crews/settings            -> {settings}
  POST   /issue/comment             -> {comment_id, url, number}

There is deliberately NO route through which a human answers a crew, and no queue
of items a crew is holding for one. A crew that needs a human decision or a human
investigation says so on the issue, labels it with the repo's
``needs_human_label``, records the pass and releases its claim — so the place that
work waits is the issue tracker the person already reads, not a dashboard-local
inbox that only exists while this app is running.

WRITE-PERMISSION DECISION (the one this module had to make). Two tiers:

  * ``POST /issue/comment`` is a FORGE write and goes through
    ``routes._pr_action_preamble``, which is the existing gate chain used by every
    mutating pull-request route: JSON body -> owner/repo -> connected repo ->
    ``_repo_can_write`` (fail-closed: ``None`` from a transient ``gh`` failure is
    DENIED). Nothing about a crew earns a weaker gate than a human clicking the
    same button.

  * Every LOCAL crew route (crews, crew, work, pause, settings) requires the repo
    to be CONNECTED but **not** writable. Three reasons, in the order they
    mattered:
      1. Precedent: ``_handle_put_investigation`` is the same shape — per-repo
         local state, nothing reaches the forge — and is gated on connected only.
      2. ``_repo_can_write`` FAILS CLOSED, and these writes are how a crew records
         work it has ALREADY done. Gating them on a remote permission read means a
         network blip leaves a crew holding a dirty worktree with no way to persist
         its phase or its reason — losing local truth to an unrelated outage. The
         forge writes it would then attempt are each gated on their own.
      3. A read-only repo is still worth crewing: Issue Radar already degrades to
         suggest-only there, and a crew that investigates and records what it found
         without pushing is a legitimate configuration. Permissions can also be
         granted later without recreating the crew.
    The exposure this accepts is bounded and local: a user who can reach the
    dashboard can create crew records on a repo they cannot write to. Those records
    cannot mutate the repo — the first forge write refuses.

``crew_store.CrewStoreError`` maps to **409**, never 500: every raise is a
user-visible condition (a duplicate crew name, a second item trying to enter an
editing phase). "Unknown crew" is caught earlier and answered 404, because a
missing record is not a conflict.

WHO A CREW IS (the identity decision). Exactly two routes are reachable by an
AGENT — ``GET /crew`` and ``PUT /crew/work``, the pair behind the
``issue_radar_crew_read`` / ``issue_radar_crew_record`` MCP tools. For an agent
caller (loopback + ``X-Internal-Secret``, which the auth middleware marks as
``request["internal_auth"]``), owner, repo and crew id are derived from the
AUTHENTICATED SESSION KEY: ``X-Session-Key`` is matched against the crew record's
``slot_key``. They are never taken from the request. That is the whole security
property — an agent cannot NAME a repo, so it cannot reach a same-numbered issue
in another repo, and it cannot reach another crew's ledger at all (which would
also defeat the store's per-crew "one editing item" invariant). A query or body
that names a DIFFERENT owner/repo/crew is refused **403**, not quietly rewritten:
a cross-crew attempt must fail where someone can see it. The dashboard, which
authenticates with a cookie, keeps passing ``owner``/``repo``/``id`` unchanged.

Every OTHER crew route refuses an internal-secret caller outright — see
``_AGENT_REACHABLE``. This gate lives here and not in the middleware allowlist
because the allowlist cannot express it: ``dashboard.server`` entries are matched
``path == p or path.startswith(p + "/")`` and carry no method, so the ``/crew``
entry admits ``/crew/pause`` as well, and no path-only entry can separate
``GET /crew`` from ``PUT``/``DELETE /crew``. Deny-by-default HERE is what makes
"those two endpoints and only those two" true, including for any route added under
``/crew/`` later.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from functools import partial, wraps
from typing import Any

from aiohttp import web

from . import crew_runtime, crew_store, provider, routes, store

logger = logging.getLogger("kirocrew.app.issue-radar.crews")

_BASE = "/api/apps/issue-radar"

#: Phases in which the crew is NOT the actor: it is waiting on CI, on a human's
#: merge, or on a human's reply. It is never waiting on a human's DECISION — that
#: is recorded as a pass and the claim released, so no phase represents it.
#:
#: A THIRD classification of ``crew_store.PHASES``, deliberately not one of the
#: store's two — and it coincides with neither. TTL_ACTIVE is about which claims
#: age; EDITING is about worktrees. This one is about who the surface should show
#: as busy, which is a presentation question, which is why it lives in the route
#: module and not in the store.
PARKED_PHASES = frozenset({"awaiting-ci", "awaiting-merge", "awaiting-reply"})

#: The ``(method, sub-path)`` pairs an internal-secret caller — an agent running a
#: crew's MCP tools — may reach. Everything else in this module refuses one.
#:
#: EXACT method+path pairs, because that is the shape of the claim and the
#: middleware allowlist cannot make it: see the identity section of the module
#: docstring. Adding a pair here is granting an unattended agent a new capability,
#: so it is deliberately a one-line, reviewable change.
_AGENT_REACHABLE: frozenset[tuple[str, str]] = frozenset(
    {("GET", "/crew"), ("PUT", "/crew/work")}
)

#: Work-item fields ``PUT /crew/work`` forwards to ``upsert_work_item``.
#:
#: Listed explicitly so the envelope keys (owner/repo/crew_id/number/event/
#: event_kind) cannot land in the patch, and so this route's writable surface is
#: readable in one place. The store validates each field's TYPE and drops
#: unknowns, so this list is about legibility, not safety.
_WORK_PATCH_FIELDS = (
    "phase", "decision", "why", "next", "worktree", "branch", "base_sha",
    "pr_number", "claim_comment_id", "ci_state", "labels_applied",
    "outcome", "tried_approach", "tried_rejected_because",
)

#: Bound on ``GET /crew?limit=`` — the ledger is append-only and a crew that ran
#: for weeks has thousands of lines; an unbounded read is a whole file into RAM
#: and down the websocket on a page open.
_MAX_EVENTS = 500
_DEFAULT_EVENTS = 200

#: Bound on ``GET /crews/names?limit=`` — the pool is 24 names and the create
#: dialog shows a handful of chips.
_MAX_SUGGESTIONS = 24

#: Bound on ``recent_skips`` in the ``GET /crew`` payload. Its siblings' bounds
#: exist to keep a whole file out of RAM; this one exists to keep the crew's OWN
#: context small — the list is read into an agent's prompt on every resume, and
#: each entry carries free-text prose. ``skipped_numbers`` alongside it is
#: deliberately unbounded (see ``_crew_page``).
_MAX_RECENT_SKIPS = 20


# ── shared plumbing ─────────────────────────────────────────────────────────


def _crew_conflict(handler):
    """Map ``CrewStoreError`` onto 409 for EVERY crew route.

    A decorator rather than a try/except per handler: the store raises this for
    invariant violations that are ordinary user conditions (duplicate name, second
    editing item), and letting one escape would surface a legitimate refusal as a
    500 — which the frontend renders as "something broke" instead of the store's
    own message, and which a crew agent would retry forever.
    """
    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except crew_store.CrewStoreError as exc:
            return web.json_response({"error": str(exc), "code": "crew_conflict"}, status=409)

    return _wrapped


def _agent_gate(method: str, path: str, handler):
    """Refuse an internal-secret caller on every crew route but the two in
    ``_AGENT_REACHABLE``.

    Deny-by-default, and applied to the whole table rather than to the routes that
    look dangerous today: the middleware admits the entire ``/crew/`` segment to
    anything holding the internal secret, so without this an agent could pause or
    RETIRE another crew — and any route added under that segment later becomes
    agent-reachable with no edit anywhere. Refusing at the handler is the pattern
    ``api_skills_discover_install`` already uses for the same reason, and it is the
    ONLY place a method can be distinguished, which ``PUT``/``DELETE /crew`` need.

    A cookie-authenticated browser request never carries ``internal_auth``, so the
    dashboard is unaffected.
    """
    reachable = (method, path) in _AGENT_REACHABLE

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not reachable and request.get("internal_auth"):
            return web.json_response(
                {"error": f"{method} {path} is not reachable by an agent",
                 "code": "agent_route_denied"},
                status=403,
            )
        return await handler(request)

    return _wrapped


def _caller_slot_keys(request: web.Request) -> tuple[str, ...]:
    """The slot-key forms ``X-Session-Key`` could be carrying, most specific first.

    A dashboard session key is ``dashboard:<slot key>`` and a crew's slot key is
    what its record stores, so the post-colon remainder is the form that matches;
    the unsplit value is kept too, for a caller that already sends the bare key. A
    key from any other surface (``slack:``, ``cron:``, ``subagent:``) simply matches
    no crew and is answered "not a crew session".

    Stripped, because a trailing space would miss the match and be reported as "not
    a crew" — inconsistent normalization in an identity comparison (CWE-178).
    """
    raw = request.headers.get("X-Session-Key", "").strip()
    if not raw:
        return ()
    forms = [raw]
    if ":" in raw:
        forms.append(raw.split(":", 1)[1].strip())
    return tuple(dict.fromkeys(form for form in forms if form))


def _crew_for_slot(slot_keys: tuple[str, ...]) -> tuple[provider.RepoKey, dict[str, Any]] | None:
    """The crew whose ``slot_key`` is this session, searched across connected repos.

    Blocking (one directory walk plus a JSON read per crew), so callers hand it to
    ``asyncio.to_thread``. The search is repo-wide because a session key carries no
    repo: ``slot_key`` is the only field the header and the record share. First
    match in connected-repo order wins, which is deterministic — crew ids are
    ``c_<8 hex>`` from ``secrets``, so two live crews sharing one is not a case
    worth a tie-break.

    Retired crews are INCLUDED. Identifying one and then refusing it with the
    route's own ``crew_retired`` says what actually happened; reporting "this
    session is not a crew" to a crew that plainly is reads as a bug to whoever is
    looking at the transcript afterwards.

    The residual this leaves: a slot key is the only evidence of crew identity, so a
    session a human deliberately names after a crew's slot key resolves to that
    crew. It grants no privilege that human does not already hold — they can read and
    write the same ledger from the Crews page — but closing it needs the runtime to
    mark a slot as app-owned when it creates one, which is
    ``crew_runtime``/``state``'s to record, not something this lookup can derive.
    """
    for entry in store.list_connected_repos():
        owner = str(entry.get("owner") or "")
        repo = str(entry.get("repo") or "")
        if not owner or not repo:
            continue
        key = provider.key_from_parts(owner, repo, entry.get("provider"), entry.get("host"))
        for crew in crew_store.list_crews(owner, repo, routes._scope(key), include_retired=True):
            if str(crew.get("slot_key") or "") in slot_keys:
                return key, crew
    return None


def _identity_mismatch(
    key: provider.RepoKey, crew: dict[str, Any], supplied: dict[str, str]
) -> web.Response | None:
    """403 when a request named an owner, repo or crew that is not the caller's own.

    A mismatch is REFUSED rather than overwritten with the session's identity. Both
    are safe, but silently rewriting turns a cross-crew write attempt into a
    successful-looking write against a different item than the caller asked for —
    which is indistinguishable from a bug in the crew's own bookkeeping. Absent
    fields are not a mismatch: the MCP tools send nothing at all.
    """
    own = {"owner": key.owner, "repo": key.repo, "id": str(crew.get("id") or "")}
    for field, mine in own.items():
        theirs = supplied.get(field) or ""
        if theirs and theirs != mine:
            return web.json_response(
                {"error": (f"this session's crew is {own['owner']}/{own['repo']}:{own['id']} — "
                           f"{field!r} cannot name another"),
                 "code": "crew_identity_mismatch"},
                status=403,
            )
    return None


async def _session_identity(
    request: web.Request, supplied: dict[str, str]
) -> tuple[provider.RepoKey, dict[str, Any], web.Response | None]:
    """Resolve WHICH crew is calling from its authenticated session key.

    The agent-side counterpart of ``_query_preamble``/``_body_preamble``, and it
    ends with the same connected-repo gate so an agent cannot reach a repo the user
    has disconnected. ``supplied`` is whatever the caller put in its query or body:
    it is only ever COMPARED (see :func:`_identity_mismatch`), never used as the
    source of identity.
    """
    slot_keys = _caller_slot_keys(request)
    if not slot_keys:
        return provider.RepoKey(), {}, web.json_response(
            {"error": "missing X-Session-Key — a crew is identified by its session",
             "code": "missing_session"},
            status=403,
        )
    found = await asyncio.to_thread(partial(_crew_for_slot, slot_keys))
    if found is None:
        return provider.RepoKey(), {}, web.json_response(
            {"error": "this session is not an Issue Radar crew, so it has no ledger",
             "code": "not_a_crew_session"},
            status=403,
        )
    key, crew = found
    mismatch = _identity_mismatch(key, crew, supplied)
    if mismatch is not None:
        return key, crew, mismatch
    if not await asyncio.to_thread(routes._connected, key):
        return key, crew, web.json_response(
            {"error": f"{key.slug} is not connected — call /connect first",
             "code": "repo_not_connected"},
            status=404,
        )
    return key, crew, None


async def _query_preamble(
    request: web.Request,
) -> tuple[provider.RepoKey, web.Response | None]:
    """``?owner=``/``?repo=`` + the connected-repo gate, for the GET routes.

    No write gate — see the module docstring's write-permission decision.
    """
    key = routes._key_from_request(request)
    if not key.owner or not key.repo:
        return key, web.json_response(
            {"error": "missing ?owner= and ?repo=", "code": "missing_repo"}, status=400
        )
    # Synchronous config read, so off the loop — the same call every other route in
    # this app makes through asyncio.to_thread.
    if not await asyncio.to_thread(routes._connected, key):
        return key, web.json_response(
            {"error": f"{key.slug} is not connected — call /connect first",
             "code": "repo_not_connected"},
            status=404,
        )
    return key, None


def _holds_non_finite_number(value: Any) -> bool:
    """Whether *value* carries ``Infinity``, ``-Infinity`` or ``NaN`` anywhere.

    Python's ``json`` decodes those three literals by default, and it also produces
    them from a plain-looking one: ``1e309`` overflows to ``inf`` SILENTLY on the way
    in. So a body can carry a float that no field on these records can accept
    (``int(inf)`` raises ``OverflowError``, ``int(nan)`` raises ``ValueError``) and
    that no encoder can write back — ``json.dumps`` emits a bare ``Infinity``, which
    is not JSON, so storing one would leave a record the dashboard's ``JSON.parse``
    refuses to read.

    Refused WHOLE, and here rather than per field, for two reasons. The value is
    unusable for every field, so a 400 that names the body is a better answer than a
    200 that silently ignored part of the patch. And this is the one place every
    crew-route body passes through — both legs, the cookie caller's and the agent's
    — so a route added later cannot be the one that forgot. The store's own coercion
    stays as the second layer, for the values that arrive from a file or from the
    forge rather than from a request.

    Walked ITERATIVELY. A recursive walk would turn a deeply nested body into a
    ``RecursionError`` — a 500 — on the way to rejecting it.
    """
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return True
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


async def _json_object(request: web.Request) -> tuple[dict, web.Response | None]:
    """The request's JSON object, or the 400 to return instead.

    Split out of :func:`_body_preamble` so the agent write path can read a body
    without the owner/repo gate it does not use — one implementation, so the two
    paths cannot drift on which malformed payload gets which refusal.
    """
    try:
        raw = await request.json()
    except Exception:
        return {}, web.json_response(
            {"error": "request body must be JSON", "code": "invalid_json"}, status=400
        )
    if not isinstance(raw, dict):
        return {}, web.json_response(
            {"error": "request body must be a JSON object", "code": "invalid_json"}, status=400
        )
    if _holds_non_finite_number(raw):
        return {}, web.json_response(
            {
                "error": "request body must not contain Infinity, -Infinity or NaN "
                         "(a numeric literal too large to represent decodes to Infinity)",
                "code": "non_finite_number",
            },
            status=400,
        )
    return raw, None


async def _body_preamble(
    request: web.Request,
) -> tuple[dict, provider.RepoKey, web.Response | None]:
    """JSON body + owner/repo + the connected-repo gate, for the mutating routes.

    Deliberately NOT ``routes._pr_action_preamble``: that one also demands
    ``_repo_can_write``, which these local-state writes intentionally do not
    require (module docstring). Everything else — the malformed-JSON 400, the
    non-object 400, the not-connected 404 — is the same, in the same order, so a
    caller cannot tell the two preambles apart except by the gate that differs.
    """
    raw, early = await _json_object(request)
    if early is not None:
        return {}, provider.RepoKey(), early
    key = routes._key_from_body(raw)
    if not key.owner or not key.repo:
        return raw, key, web.json_response(
            {"error": "missing 'owner'/'repo'", "code": "missing_repo"}, status=400
        )
    if not await asyncio.to_thread(routes._connected, key):
        return raw, key, web.json_response(
            {"error": f"{key.slug} is not connected — call /connect first",
             "code": "repo_not_connected"},
            status=404,
        )
    return raw, key, None


async def _require_crew(
    key: provider.RepoKey, crew_id: str, *, must_be_live: bool
) -> tuple[dict, web.Response | None]:
    """Load a crew, or the response to return instead.

    An unknown crew is **404**, not the 409 that ``update_crew``'s own
    ``CrewStoreError`` would produce: a record that does not exist is not a
    conflicting write, and a 409 tells a crew agent to retry something that can
    never succeed. The residual race (the crew is retired between this read and
    the write) still surfaces as the store's 409, which is the correct answer for
    a write that lost.

    ``must_be_live`` refuses a RETIRED crew. Retiring stops the crew and releases
    its slot, so a work-item write afterwards would either vanish (nothing will ever
    pick it up) or resurrect a crew whose name is still attached to public claim
    comments. A rename or a re-retire is still allowed — those only touch the
    archived record.
    """
    if not crew_id:
        return {}, web.json_response(
            {"error": "missing 'id'", "code": "missing_crew_id"}, status=400
        )
    # A MALFORMED id is a bad request, distinct from both "unknown" (404) and the
    # store's "conflict" (409). Checked here as well as in the store because the
    # store can only raise CrewStoreError, which this app maps to 409 — and 409
    # tells a crew agent to retry an id that can never work. The store's own gate
    # is the security boundary (it stops a path escaping the crew directory); this
    # one exists to answer with the right status.
    if not crew_store.is_crew_id(crew_id):
        return {}, web.json_response(
            {"error": f"invalid crew id {crew_id!r}", "code": "invalid_crew_id"},
            status=400,
        )
    crew = await routes._st(key, crew_store.read_crew, key.owner, key.repo, crew_id)
    if crew is None:
        return {}, web.json_response(
            {"error": f"unknown crew {crew_id!r}", "code": "crew_not_found"}, status=404
        )
    if must_be_live and crew.get("retired_at"):
        return crew, web.json_response(
            {"error": f"crew {crew.get('name') or crew_id!r} is retired", "code": "crew_retired"},
            status=409,
        )
    return crew, None


def _crew_flags(crew: dict, open_items: list[dict]) -> dict[str, bool]:
    """The two per-crew booleans the chip counts sum.

    Exactly the definitions the Crews page filters on:
      ``working``   — the NEWEST open item is in a non-parked phase, i.e. the crew
                      itself is the actor right now. Newest, not any: a crew with
                      one item parked on CI and one being implemented is working,
                      and ``list_work_items`` already returns newest-progress-first.
      ``paused``    — switched off but not retired.

    There is no "needs you" flag, because a crew never holds an issue for a human:
    the one that needs a decision or an investigation is labelled and handed back to
    the issue tracker, so a person's queue is the tracker's own filter, not a
    per-crew boolean this app would have to keep true while the crew idles.

    These are NOT mutually exclusive and the counts they feed are NOT a partition:
    a paused crew with an in-flight item is counted in both. That is correct for
    chip filters, where each chip is an independent predicate rather than a slice
    of a pie — the numbers are deliberately allowed to sum past the crew count.
    """
    return {
        "working": bool(open_items) and open_items[0].get("phase") not in PARKED_PHASES,
        "paused": crew.get("enabled") is False and not crew.get("retired_at"),
    }


def _crew_status(flags: dict[str, bool]) -> str:
    """One status for the crew's status DOT.

    A dot can only be one colour, so unlike the counts this must pick, and the
    order is by what the user has to do about it: a paused crew is doing nothing
    regardless of what it holds, work in flight needs nothing, and idle is the
    absence of both.
    """
    if flags["paused"]:
        return "paused"
    if flags["working"]:
        return "working"
    return "idle"


def _crews_page(owner: str, repo: str, root: Any) -> dict[str, Any]:
    """Everything ``GET /crews`` answers, computed in ONE off-loop call.

    Not N separate ``_st`` round-trips: the counts need every crew's open work
    items, which is a directory walk plus a JSON read per item, so a hop per crew
    turns one page load into 2×N event-loop hand-offs for data that is already
    being read on the same thread.
    """
    crews = crew_store.list_crews(owner, repo, root)
    counts = {"on_duty": len(crews), "working": 0, "paused": 0}
    for crew in crews:
        open_items = crew_store.list_work_items(
            owner, repo, str(crew.get("id") or ""), root, open_only=True
        )
        flags = _crew_flags(crew, open_items)
        for name, value in flags.items():
            if value:
                counts[name] += 1
        # Additive per-crew field, derived from the same flags as the counts. It is
        # here so the phase taxonomy stays in one language: without it the frontend
        # has to re-encode PARKED_PHASES in TypeScript and the two drift the first
        # time a phase is added.
        crew["status"] = _crew_status(flags)
    return {"crews": crews, "settings": crew_store.read_settings(owner, repo, root), "counts": counts}


def _crew_page(owner: str, repo: str, crew_id: str, limit: int, root: Any) -> dict[str, Any]:
    """Everything ``GET /crew`` answers, in ONE off-loop call (see ``_crews_page``).

    ``settings`` is part of it because this response is a resuming crew's whole
    briefing: the claim TTL is the number its protocol negotiates with, and a crew
    that cannot read it has to guess at the one thing that decides whether its claim
    is still valid.

    The two skip fields are shaped by who reads them and are deliberately NOT the
    same list twice:

      * ``skipped_numbers`` is COMPLETE and unbounded. A crew tests membership
        against it before it starts investigating, and a truncated list answers
        "not skipped" for an issue that is — which is the exact re-investigation
        this index exists to prevent. Ints are ~8 bytes each; a repo would need
        tens of thousands of passes before this is worth a second round trip.
      * ``recent_skips`` carries the PROSE, which is unbounded per entry, so it is
        capped. It answers a different question — what kind of work this fleet has
        been declining lately — and the whole index is on the crew page for anyone
        who needs more.
    """
    skips = crew_store.read_skips(owner, repo, root)
    return {
        "crew": crew_store.read_crew(owner, repo, crew_id, root),
        "items": crew_store.list_work_items(owner, repo, crew_id, root),
        "events": crew_store.read_events(owner, repo, root, crew_id=crew_id, limit=limit),
        "settings": crew_store.read_settings(owner, repo, root),
        "skipped_numbers": sorted(row["number"] for row in skips.values()),
        "recent_skips": [
            {"number": row["number"], "reason": row["reason"], "scope": row["scope"]}
            for row in crew_store.recent_skips(owner, repo, root, limit=_MAX_RECENT_SKIPS)
        ],
        "counts": {"open": crew_store.open_slot_count(owner, repo, crew_id, root)},
    }


def _bounded_limit(raw: str, default: int, ceiling: int) -> int:
    """A positive ``?limit=`` clamped to ``ceiling``; anything unparseable is the
    default. A bad limit is not worth a 400 on a read route — the ceiling is what
    protects the loop, and it applies either way."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, ceiling))


def _drop_issue_detail_cache(owner: str, repo: str, number: int, root: Any) -> None:
    """Invalidate one issue's cached detail + timeline after a comment lands.

    ``store`` has ``drop_pr_detail_cache`` but no issue equivalent yet, and adding
    one is another module's change, so this is that same one-line unlink through
    the store's own public path helper (OSError suppressed for the same reason:
    failing to invalidate a cache must not fail the write that succeeded).

    It matters more here than on the PR side. The crew's claim protocol READS the
    timeline back to confirm its own check-in comment; served a pre-comment cache
    it would conclude the claim never posted and post a second one.
    """
    with contextlib.suppress(OSError):
        store.issue_detail_cache_path(owner, repo, int(number), root).unlink(missing_ok=True)


# ── crews (list / create / names) ───────────────────────────────────────────


async def _handle_crews_list(request: web.Request) -> web.Response:
    """GET /crews?owner&repo — the Crews page's whole payload: the repo's
    non-retired crews (each with a derived ``status``), the repo-wide protocol
    settings, and the four chip counts."""
    key, early = await _query_preamble(request)
    if early is not None:
        return early
    page = await asyncio.to_thread(
        partial(_crews_page, key.owner, key.repo, routes._scope(key))
    )
    return web.json_response({**routes._identity(key), **page})


async def _handle_crew_create(request: web.Request) -> web.Response:
    """POST /crews {"owner","repo","name", ...crew fields} -> {"crew"}.

    The store enforces name uniqueness (including retired crews' names) and drops
    unknown fields, so this route validates only that a name was sent — a
    duplicate comes back as the store's own 409 message, which names the taken
    name rather than a generic conflict.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    if not routes._str_field(body, "name"):
        return web.json_response(
            {"error": "'name' is required", "code": "name_required"}, status=400
        )
    crew = await routes._st(key, crew_store.create_crew, key.owner, key.repo, body)
    routes._audit("crew_create", f"{key.slug}:{crew['id']}", "ok")
    return web.json_response({"crew": crew})


async def _handle_crew_names(request: web.Request) -> web.Response:
    """GET /crews/names?owner&repo[&limit] -> {"suggestions"} — unused galaxy
    names for the create dialog's chips. Suggestions only: the name field is free
    text, so uniqueness is enforced on create, not here."""
    key, early = await _query_preamble(request)
    if early is not None:
        return early
    limit = _bounded_limit(request.query.get("limit") or "", 6, _MAX_SUGGESTIONS)
    suggestions = await routes._st(
        key, crew_store.suggest_names, key.owner, key.repo, limit=limit
    )
    return web.json_response({"suggestions": suggestions})


# ── one crew (read / update / retire) ───────────────────────────────────────


async def _handle_crew_read(request: web.Request) -> web.Response:
    """GET /crew -> {owner,repo,provider,host,"crew","items","events","settings","counts"} —
    one crew's page: its record, all its work items (newest progress first), its
    slice of the event ledger, the repo's protocol settings, and its slot accounting.

    TWO callers with TWO identity sources (module docstring, "WHO A CREW IS"):

      * the dashboard authenticates with a cookie and passes ``?owner&repo&id``;
      * a crew agent authenticates with the internal secret and passes NOTHING —
        owner, repo and crew id come from its session key. This is the route the
        ``issue_radar_crew_read`` tool calls, and the reason that tool takes no
        arguments at all.

    The identity fields are ECHOED at the top level because the write leg's caller
    needs them and must not invent them: ``issue_radar_crew_record`` reads them out
    of this response and puts them in its PUT body, so a same-numbered issue in
    another repo can never be the item that gets overwritten.
    """
    if request.get("internal_auth"):
        key, crew, early = await _session_identity(
            request,
            {
                "owner": (request.query.get("owner") or "").strip(),
                "repo": (request.query.get("repo") or "").strip(),
                "id": (request.query.get("id") or "").strip(),
            },
        )
        if early is not None:
            return early
        crew_id = str(crew.get("id") or "")
    else:
        key, early = await _query_preamble(request)
        if early is not None:
            return early
        crew_id = (request.query.get("id") or "").strip()
        # Read routes accept a retired crew: its record, work log and ledger are kept
        # deliberately so the page still opens after retirement.
        _crew, missing = await _require_crew(key, crew_id, must_be_live=False)
        if missing is not None:
            return missing
    limit = _bounded_limit(request.query.get("limit") or "", _DEFAULT_EVENTS, _MAX_EVENTS)
    page = await asyncio.to_thread(
        partial(_crew_page, key.owner, key.repo, crew_id, limit, routes._scope(key))
    )
    return web.json_response({**routes._identity(key), **page})


def _auto_approve_armed(crew: dict[str, Any]) -> bool:
    """Whether this RECORD is what earns the crew an auto-approved unattended turn.

    Deliberately the same predicate ``crew_runtime.sync_trust`` grants on, read off
    the record rather than restated, so the grant and the revocation below cannot
    drift apart: whatever stops being true here is a grant that has to go. That is
    why it is ``unattended`` AND ``is_live`` and not ``unattended`` alone — turning
    unattended off, disabling the crew and pausing it through a patch all withdraw
    the same grant, and a check written against one field would miss the other two.
    """
    return bool(crew.get("unattended")) and crew_runtime.is_live(crew)


async def _handle_crew_update(request: web.Request) -> web.Response:
    """PUT /crew {"owner","repo","id", ...patch} -> {"crew"}.

    Merges a validated patch. A rename re-checks uniqueness in the store (409) and
    leaves ``avatar_seed`` alone, so the crew keeps its face. Allowed on a retired
    crew: correcting an archived record touches nothing live.

    A patch that withdraws auto-approval (``unattended`` true -> false, or an
    ``enabled``/``paused_reason`` patch that stops the crew) REVOKES before it
    answers, for the reason :func:`_revoke_execution` gives: persisting the flag
    reaches neither the ``SafetyOverride`` grant nor the loop, so an already-armed
    nudge firing before the watchdog's next sweep would take one whole auto-approved
    turn under a setting the human had already switched off.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    crew_id = routes._str_field(body, "id")
    before, missing = await _require_crew(key, crew_id, must_be_live=False)
    if missing is not None:
        return missing
    # Sampled from the pre-update record, because the patch is not the transition:
    # ``unattended: false`` on a crew that was already attended must not revoke
    # (there is nothing to revoke and the log line would claim a stop that never
    # happened), and a patch that omits the field must leave a live grant alone.
    was_armed = _auto_approve_armed(before)
    crew = await routes._st(key, crew_store.update_crew, key.owner, key.repo, crew_id, body)
    if was_armed and not _auto_approve_armed(crew):
        withdrawn = "unattended off" if not crew.get("unattended") else "not live"
        await _revoke_execution(request, crew, withdrawn)
    return web.json_response({"crew": crew})


async def _handle_crew_retire(request: web.Request) -> web.Response:
    """DELETE /crew {"owner","repo","id"} -> {"crew"} — RETIRE, not delete.

    The record, its name reservation and its work log all survive: the name still
    appears in check-in comments the crew left on the forge, so reusing it would
    make an old comment look like a live claim. Idempotent — retiring a retired
    crew re-stamps it rather than 409ing, so a double-click is not an error.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    crew_id = routes._str_field(body, "id")
    _crew, missing = await _require_crew(key, crew_id, must_be_live=False)
    if missing is not None:
        return missing
    crew = await routes._st(key, crew_store.retire_crew, key.owner, key.repo, crew_id)
    await _revoke_execution(request, crew, "retired")
    routes._audit("crew_retire", f"{key.slug}:{crew_id}", "ok")
    return web.json_response({"crew": crew})


async def _revoke_execution(
    request: web.Request, crew: dict[str, Any], reason: str
) -> None:
    """Un-arm a crew the human just stopped, BEFORE the route answers.

    Writing the record is not stopping the crew. Its autonudge loop is a live timer
    in another service and its slot still carries ``_trust``, and nothing in
    ``enabled``/``paused_reason``/``retired_at``/``unattended`` reaches either one —
    the watchdog does, on the app's poll interval. So a route that returned after the
    store write left a window in which an idle timer fires and the crew takes one
    more auto-approved, unattended turn after a human pressed stop. Revoking here
    closes it; :func:`crew_runtime.revoke_crew_execution` documents the two grants
    and stays the watchdog's backstop as well.

    Never raises. The durable state is already written and correct, so a failure to
    reach in-memory state must not turn a successful pause into a 500 — and the
    watchdog re-revokes on its next cycle.
    """
    state = request.app.get("state")
    if state is None:
        return
    try:
        await crew_runtime.revoke_crew_execution(state, crew, reason)
    except Exception:
        logger.warning(
            "crew %s: revoking execution after %s failed",
            crew.get("id"),
            reason,
            exc_info=True,
        )


# ── work items (the crews' own write path) ──────────────────────────────────


async def _handle_crew_work(request: web.Request) -> web.Response:
    """PUT /crew/work {"owner","repo","crew_id","number", ...patch, "event",
    "event_kind"} -> {"item","event"}.

    THE route a crew writes its progress through, and the one the MCP write tool
    targets. It upserts the work item AND appends one ledger line in a single
    call, which is the whole point: a phase cannot change without a logged reason,
    because there is no route that changes one without the other.

    ORDER MATTERS and is: validate the log line -> write the item -> index a skip
    -> append the line. Validating the kind and text FIRST means the store's own
    refusals, including the second-editing-item 409, all happen before anything is
    appended. Appending first would be worse: a store refusal would leave the
    ledger asserting a change that never happened, and a lie in an append-only log
    cannot be taken back.

    ORDER IS NOT ENOUGH, though, and the request is ALL-OR-NOTHING: three durable
    writes to three files cannot be made atomic by any permutation, so a later write
    that fails rolls the earlier ones back before the error goes out. That is one
    transaction under one lock, and it lives in ``crew_store.commit_work_progress``
    — the store owns the file layout and the locks, and a rollback whose target
    another writer can move while it is held is a lost update rather than a
    rollback. This route contributes the two things only a request knows: the
    fields to patch, and the prose a pass is recorded with.

    THE SKIP INVARIANT. A patch that sets ``phase`` to ``skipped`` indexes the pass
    repo-wide in the same transaction, so it is not possible to skip an issue
    without indexing it. Nothing in the app writes a phase except through this
    route, and the route cannot write one without handing the store a reason.

    ``skip_scope`` is OPTIONAL and defaults to ``other``. Requiring it would break
    every existing caller of this route for a field that is only a filter label.

    Identity follows the same two-caller rule as ``GET /crew``: an agent's
    ``owner``/``repo``/``crew_id`` come from its session, and a body that names a
    different crew is refused rather than honoured. Without that, everything the
    read leg does to keep a crew inside its own ledger would be undone one route
    later — the write is where the damage would be.
    """
    if request.get("internal_auth"):
        body, early = await _json_object(request)
        if early is not None:
            return early
        key, crew, early = await _session_identity(
            request,
            {
                "owner": routes._str_field(body, "owner"),
                "repo": routes._str_field(body, "repo"),
                "id": routes._str_field(body, "crew_id"),
            },
        )
        if early is not None:
            return early
        crew_id = str(crew.get("id") or "")
    else:
        body, key, early = await _body_preamble(request)
        if early is not None:
            return early
        crew_id = routes._str_field(body, "crew_id")

    # Reusing the PR field parser rather than copying its bound: the bound is the
    # point (the number becomes a FILENAME), and a second copy is how one of them
    # ships without it. Its out-of-range text says "pull-request number", which is
    # cosmetically wrong here and only reachable past 1e9.
    number, number_error = routes._pr_number_field(body)
    if number_error is not None:
        return number_error

    event_text, too_long = routes._pr_body_field(body, "event")
    if too_long is not None:
        return too_long
    if not event_text:
        return web.json_response(
            {"error": "'event' is required — a work-item write must say why",
             "code": "event_required"},
            status=400,
        )
    event_kind = routes._str_field(body, "event_kind")
    if event_kind not in crew_store.EVENT_KINDS:
        return web.json_response(
            {"error": f"'event_kind' must be one of {', '.join(crew_store.EVENT_KINDS)}",
             "code": "invalid_event_kind"},
            status=400,
        )

    _crew, missing = await _require_crew(key, crew_id, must_be_live=True)
    if missing is not None:
        return missing

    patch = {field: body[field] for field in _WORK_PATCH_FIELDS if field in body}
    # The route derives the pass's PROSE (only it has the request body); the store
    # owns the coupling — a non-`None` reason is what makes the transaction index
    # one, in the same call and under the same lock as the item and the line.
    skip_reason: str | None = None
    if str(patch.get("phase") or "") == "skipped":
        skip_reason = _skip_reason(body, event_text)
    committed = await routes._st(
        key,
        crew_store.commit_work_progress,
        key.owner,
        key.repo,
        crew_id,
        number,
        patch,
        event_kind,
        event_text,
        skip_reason=skip_reason,
        skip_scope=routes._str_field(body, "skip_scope"),
    )
    return web.json_response(committed)


def _skip_reason(body: dict[str, Any], event_text: str) -> str:
    """The prose the shared index stores for a pass.

    Prefers the explanation the crew wrote as an explanation — ``reason``, then the
    work item's own ``why`` — and falls back to the progress line, which is
    mandatory on any phase write and so is always present. The fallback is what
    makes the index never hold an entry with an empty reason: an indexed number
    with no ``reason`` tells the next crew to skip without telling it why, which is
    the one shape of this record that is worse than not having it.
    """
    for field in ("reason", "why"):
        text = routes._str_field(body, field)
        if text:
            return text
    return event_text


async def _handle_crew_pause(request: web.Request) -> web.Response:
    """POST /crew/pause {"owner","repo","id","paused", "reason"?} -> {"crew"}.

    ``paused`` is the request's own verb rather than a raw ``enabled`` patch, so
    the caller cannot half-express the state: pausing stores the reason, resuming
    CLEARS it. A stale reason on a running crew is worse than none — the page
    would show a live crew explaining why it is stopped.

    Pausing also REVOKES execution before answering (see :func:`_revoke_execution`);
    resuming does not re-arm here, because the watchdog relaunches a live crew on
    its next cycle and doing it twice would arm two loops for one slot.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    paused = body.get("paused")
    if not isinstance(paused, bool):
        return web.json_response(
            {"error": "'paused' must be a boolean", "code": "invalid_paused"}, status=400
        )
    crew_id = routes._str_field(body, "id")
    _crew, missing = await _require_crew(key, crew_id, must_be_live=True)
    if missing is not None:
        return missing
    crew = await routes._st(
        key,
        crew_store.set_crew_paused,
        key.owner,
        key.repo,
        crew_id,
        paused,
        routes._str_field(body, "reason"),
    )
    if paused:
        await _revoke_execution(request, crew, "paused")
    routes._audit("crew_pause", f"{key.slug}:{crew_id}:{'on' if paused else 'off'}", "ok")
    return web.json_response({"crew": crew})


# ── repo-wide protocol settings ─────────────────────────────────────────────


async def _handle_crews_settings_get(request: web.Request) -> web.Response:
    """GET /crews/settings?owner&repo -> {"settings"} — the repo-wide claim
    protocol (TTL, the needs-a-human label, commit trailer), defaults filled in on
    read. Repo-wide and not per-crew: two crews negotiating with different TTLs is
    how a short-TTL crew steals a long-TTL crew's live work, and two crews labelling
    the same condition differently gives the person answering two queues to watch."""
    key, early = await _query_preamble(request)
    if early is not None:
        return early
    settings = await routes._st(key, crew_store.read_settings, key.owner, key.repo)
    return web.json_response({"settings": settings})


async def _handle_crews_settings_put(request: web.Request) -> web.Response:
    """PUT /crews/settings {"owner","repo","settings":{...}} -> {"settings"}.

    The ``{"settings": {...}}`` envelope matches the app's existing ``PUT /settings``
    so the two configuration surfaces do not need different client code.

    No ``revision`` precondition, unlike that route. There it is mandatory because
    the PUT replaces the WHOLE document, so a stale client would erase a field it
    never read. ``write_settings`` MERGES per field under the settings lock, so a
    partial patch cannot discard a key it did not send and there is nothing for an
    optimistic-concurrency check to protect.
    """
    body, key, early = await _body_preamble(request)
    if early is not None:
        return early
    patch = body.get("settings")
    if not isinstance(patch, dict):
        return web.json_response(
            {"error": "'settings' must be an object", "code": "invalid_settings"}, status=400
        )
    settings = await routes._st(key, crew_store.write_settings, key.owner, key.repo, patch)
    return web.json_response({"settings": settings})


# ── the one forge write ─────────────────────────────────────────────────────


async def _handle_issue_comment(request: web.Request) -> web.Response:
    """POST /issue/comment {"owner","repo","number","body"} -> {"comment_id"}.

    Mirrors ``routes._handle_pull_comment``, including its permission gate: the
    shared ``_pr_action_preamble`` (JSON -> owner/repo -> connected ->
    ``_repo_can_write``, which fails closed) and the shared error taxonomy
    (403 for a provider refusal, 502 for anything else upstream). A crew posting a
    check-in comment gets no weaker gate than a human clicking Comment.

    ``add_issue_comment``, not ``add_pr_comment``: this route exists precisely
    because the crew's claim ledger lives on ISSUES, and on GitLab issues and merge
    requests are separate collections with independent numbering — the PR function
    would comment on an unrelated merge request that happens to share the number.

    Returns the comment's ``id`` because the claim protocol needs it: the crew
    stores it as ``claim_comment_id`` and EDITS that same comment on later
    check-ins rather than posting a new one.
    """
    body, key, early = await routes._pr_action_preamble(request, "issue_comment")
    if early is not None:
        return early

    number, number_error = routes._pr_number_field(body)
    if number_error is not None:
        return number_error
    text, too_long = routes._pr_body_field(body)
    if too_long is not None:
        return too_long
    if not text:
        return web.json_response(
            {"error": "'body' is required", "code": "body_required"}, status=400
        )

    target = f"{key.slug}#{number}"
    client = provider.client_for(key)
    try:
        result = await asyncio.to_thread(
            partial(
                client.add_issue_comment, key.owner, key.repo, number, text,
                **provider.call_kwargs(key),
            )
        )
    except routes.GhCliError as exc:
        return routes._pr_action_error("issue_comment", target, exc)

    await asyncio.to_thread(
        partial(_drop_issue_detail_cache, key.owner, key.repo, number, routes._scope(key))
    )
    routes._audit("issue_comment", target, "ok")
    return web.json_response({
        **routes._identity(key),
        "number": number,
        "comment_id": result.get("id"),
        "url": result.get("url"),
    })


# ── registration ────────────────────────────────────────────────────────────


def register_crew_routes(app: web.Application) -> None:
    """Register the crew routes. Called from ``routes.register_routes``.

    Three wrappers, outermost first. ``routes._require_enabled`` is not optional
    and not inherited from anywhere: routes are registered ONCE at gateway startup
    and Issue Radar is ``defaultEnabled: false``, so an unwrapped handler stays
    callable while the app is switched off. ``_agent_gate`` then refuses an
    internal-secret caller on every route but the two an agent's MCP tools need.
    ``_crew_conflict`` is innermost so a store invariant surfaces as 409 rather
    than 500.
    """
    def _add(method: str, path: str, handler) -> None:
        app.router.add_route(
            method,
            f"{_BASE}{path}",
            routes._require_enabled(_agent_gate(method, path, _crew_conflict(handler))),
        )

    _add("GET", "/crews", _handle_crews_list)
    _add("POST", "/crews", _handle_crew_create)
    _add("GET", "/crews/names", _handle_crew_names)
    _add("GET", "/crews/settings", _handle_crews_settings_get)
    _add("PUT", "/crews/settings", _handle_crews_settings_put)
    _add("GET", "/crew", _handle_crew_read)
    _add("PUT", "/crew", _handle_crew_update)
    _add("DELETE", "/crew", _handle_crew_retire)
    _add("PUT", "/crew/work", _handle_crew_work)
    _add("POST", "/crew/pause", _handle_crew_pause)
    _add("POST", "/issue/comment", _handle_issue_comment)
