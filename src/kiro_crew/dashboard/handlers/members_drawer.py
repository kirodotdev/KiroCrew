"""The reads the Crewmates page opens for one member at a time.

The drawer's folded projections, its recent activity timeline, the crewmate's
read-only notes (its briefing and the system-owned upkeep text beside them), and the
recap the cold-start welcome greets with. Each read names the exact crew: the projections
and briefing reads refuse a slug two crews share, and the activity and recap reads keep to
the named crew's own records.

Served through :mod:`kiro_crew.dashboard.handlers.members`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

import kiro_crew.dashboard.handlers as _h
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members_access import (
    _audit_owner_read,
    _deny_app_caller,
    _member_names_for_slug,
    _sel,
)
from kiro_crew.dashboard.handlers.members_roster import (
    _SEQ_NO_LOG,
    _SEQ_UNATTRIBUTABLE,
    _absence_is_confirmed,
    _logged_slugs,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.members import MemberSlugError

logger = logging.getLogger("kiro_crew.dashboard.handlers.members")


#: Activity entries returned to the drawer. Bounds the payload and the JSONL
#: scan alike; the log itself rotates at ~256KiB so this is a display cap,
#: not a durability boundary.
_ACTIVITY_LIMIT = 50
# How many log envelopes one backwards page reads. Larger than the display limit
# because the log is shared: config, binding, rules, message, slot and patrol
# events all sit between one member's activity records, so a page the size of the
# display cap would usually need several round trips to fill it. This bounds the
# allocation per page, which is the point -- it is not a cap on the answer.
_ACTIVITY_PAGE = 500


def _parse_activity_ts(raw: str) -> float:
    """Epoch seconds from an activity record's ISO-8601 ``ts``, or 0.0.

    ``record_activity`` writes ``%Y-%m-%dT%H:%M:%SZ`` (UTC, second
    precision); tolerate a ``+00:00`` suffix too since ``fromisoformat``
    accepts it and hand-edited logs exist. Anything that is not a string in
    that shape — including a numeric epoch from a foreign writer — reads as
    unplaceable (0.0) rather than crashing the endpoint: the log is
    append-only from multiple processes and tolerant reads are its contract.
    """
    if not isinstance(raw, str) or not raw:
        return 0.0
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


async def api_member_projections(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/projections?member=<name> — one member's folded views.

    What the detail drawer mounts on. The roster list carries only the view a list
    ROW paints; the drawer paints the activity timeline, the patrol state and the
    driven-slot list, and it is open for exactly one member at a time, so it reads
    them here instead of every row carrying three views nothing on it reads.

    Serves the WHOLE snapshot rather than a named subset: the drawer reads three
    views today, the projection set is extensible (an app contributes its own
    ``<app>/<name>`` key), and a per-key allowlist here would silently withhold a
    view the moment one is added. The block is the same shape the roster ships and
    the same shape a ``member_projection`` frame carries, so the client seeds it
    through one code path.

    ``member`` (query, REQUIRED) is the exact crew name, for the same reason the
    activity read requires it: a slug is a lossy fold, two names can share one log,
    and a whole-member projection served on the wrong name renders one member's work
    as another's. The name is checked against the log's own header, and a mismatch
    answers 409 rather than a blank body -- the drawer has to be able to say WHY it
    is empty.

    A member with no log answers an empty BASELINE (``asOfSeq`` 0, no values),
    which is exactly what the roster sends for the same member, and the read does
    not create one: opening a drawer is not an event in that member's life. The
    negative sentinel is kept for a slug that cannot be attributed, because the
    client stops applying live frames to those and a member about to be written
    about for the first time must keep receiving them.

    Redacted through the projection chain before it leaves, the same as the roster
    block, the ``/history`` read and the WS push.
    """
    denied = await _deny_app_caller(request, "members.projections")
    if denied is not None:
        return denied
    owner_denied = await require_owner_dashboard_request(request, "members.projections.read")
    if owner_denied is not None:
        return owner_denied
    _audit_owner_read(request, "members.projections.read")
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    member = request.query.get("member", "")
    # Same eligibility bar as the activity, rules and briefing reads: this route
    # reconciles the named member's config into its log, so a stored name that
    # cannot reach a model is refused here. Display names are free-form text; the
    # identifier grammar is not the test.
    if not members_mod.is_dispatchable_member_name(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # The member has to OWN the slug, not merely fail to collide with it. Three
    # separate questions, and the same order the briefing read asks them in: does
    # this name derive this slug, does the name exist in config, and is it the only
    # name deriving the slug. Without the first, `?member=` is free to name a
    # SECOND member while the path names a log: a log whose header holds the slug
    # placeholder passes the header check below for any name, and the reconcile
    # would then append the named member's config fields into another member's log
    # and record the pass as done. The header check cannot stand in for this --
    # it asks which member the log RECORDS, and the placeholder records nobody.
    try:
        if members_mod.member_slug(member, cfg) != slug:
            return web.json_response(
                {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
            )
    except MemberSlugError:
        return web.json_response(
            {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
        )
    if member not in cfg.agents:
        return web.json_response(
            {"error": "no crew member for this slug", "code": "member_not_found"}, status=404
        )
    # Two rows folding to one slug is the case the roster blanks for BOTH of them,
    # and it has to blank here too: neither member can be told apart in a slug-keyed
    # log, so serving either one's views under this slug is a guess.
    # `_member_names_for_slug` is the addressability question, which is the one being
    # asked -- whether this ROUTE can name a single member behind the slug.
    if _member_names_for_slug(cfg, slug) != [member]:
        return web.json_response(
            {"error": "member slug is shared", "code": "member_slug_shared"}, status=409
        )

    def _read() -> dict | None:
        from kiro_crew.eventlog.service import _redact_projection_value, get_service

        svc = get_service()
        if slug not in _logged_slugs(svc):
            if _absence_is_confirmed(slug):
                # Empty baseline, for the reason the roster read gives at the same
                # exit: a member with no log is attributable, so its frames must
                # still land.
                return {"asOfSeq": _SEQ_NO_LOG, "values": {}}
            if slug not in _logged_slugs(svc):
                # The listing omits a log whose header it cannot prove, and a fresh
                # listing still omits this one, so this is a failed READ of the one
                # member the request is about. Raising takes the route's own 500,
                # which the drawer renders as an error instead of painting an
                # affirmative "nothing scheduled" over state it could not read.
                raise OSError(f"member log for {slug!r} is present but cannot be proved")
            # The store proves it now -- a log created since the first listing --
            # so it reads like any other.
        logged_name = svc.logged_name(slug)
        if logged_name is not None and logged_name not in (member, slug):
            return None
        snap = svc.snapshot(slug)
        # A PURE read, and deliberately no config reconcile. The reconcile compares a
        # config this request loaded against the folded roster and appends what
        # differs, so a save that lands between the load and the snapshot is undone by
        # an append carrying the older values. The roster read is where that
        # comparison belongs -- it runs for every logged member on every poll, and the
        # correcting append raises the log's sequence, so the corrected value outranks
        # an uncorrected one under the client's higher-seq-wins rule. Doing it here as
        # well buys one member's correction and costs a second writer on a read path.
        if not isinstance(snap, dict):
            return {"asOfSeq": _SEQ_UNATTRIBUTABLE, "values": {}}
        return {
            "asOfSeq": snap.get("asOfSeq", _SEQ_UNATTRIBUTABLE),
            "values": _redact_projection_value(snap.get("values", {})),
        }

    try:
        block = await asyncio.to_thread(_read)
    except Exception:
        logger.debug("member projections read failed for %r", slug, exc_info=True)
        return web.json_response(
            {"error": "could not read member projections", "code": "member_projections_failed"},
            status=500,
        )
    if block is None:
        return web.json_response(
            {"error": "member slug logs another member", "code": "member_slug_foreign"}, status=409
        )
    return web.json_response(block)


async def api_member_activity(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/activity — a member's recent activity pointers.

    Feeds the detail drawer's "recent activity" timeline and its derived
    counts. Entries come from the member's own append-only pointer log
    (``members.record_activity``), so everything here is REAL recorded
    signal — the drawer omits a stat rather than fabricating one.

    Response entries carry an allowlist of fields only: ``ts`` (epoch
    seconds), ``via`` (how the member was engaged — ``chat`` is a session
    the user opened with it, ``select_crew`` is a routing decision), and
    ``project``. Session keys stay out of the payload: the drawer renders
    what happened, not handles into other sessions.

    ``member`` (query, REQUIRED) is the exact crew name. Slugification is
    lossy — two distinct names can share one slug and therefore one log
    file — and each record carries the exact name precisely so attribution
    stays recoverable. Filtering here (BEFORE the display limit) is what
    keeps a colliding slug's drawer from rendering the other member's
    events; making the parameter required makes the mixed read impossible
    by construction rather than a caller obligation.
    """
    denied = await _deny_app_caller(request, "members.activity")
    if denied is not None:
        return denied
    owner_denied = await require_owner_dashboard_request(request, "members.activity.read")
    if owner_denied is not None:
        return owner_denied
    _audit_owner_read(request, "members.activity.read")
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    member = request.query.get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )

    # Source the records from the member's append-only log: ACTIVITY_RECORD
    # events for THIS exact member, unwrapped to the record dict each carries.
    #
    # The member filter and the timestamp parse run INSIDE this read, before any
    # cap, because a log's newest N envelopes are not N of one member's activity
    # records. The same log also carries config, binding, rules, message, slot and
    # patrol events, and a colliding slug's log carries another exact name's
    # records as well -- so capping the envelope read first and filtering second
    # drops activity that is well inside the window the drawer promises.
    #
    # But the read is PAGED rather than asked for the whole log. `history` with
    # `limit=None` materialises every event in the lifetime file into a list and
    # reverses it, which is bounded only while the log still fits the retained tail
    # (`MAX_RETAINED_EVENTS`); past that the allocation is the file's whole length,
    # on a request path, for a response that shows `_ACTIVITY_LIMIT` rows. The log
    # has no rotation, so outgrowing the tail is ordinary ageing rather than an
    # extreme input. Paging keeps the filter where it has to be AND keeps the
    # allocation bounded: walk backwards a page at a time and stop as soon as one
    # more than the display cap has matched, which is all `capped` needs to know.
    def _read_activity_records() -> list[tuple[float, int, dict]]:
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc = get_service()
        out: list[tuple[float, int, dict]] = []
        before: int | None = None
        while True:
            page = svc.history(slug, before=before, limit=_ACTIVITY_PAGE)
            if not page:
                break
            for event in page:
                if event.get("type") != ACTIVITY_RECORD:
                    continue
                record = event.get("data")
                if not isinstance(record, dict):
                    continue
                if record.get("member") != member:
                    # A colliding slug's log holds records for another exact name;
                    # they belong to that member's drawer, not this one's.
                    continue
                ts = _parse_activity_ts(record.get("ts", ""))
                if ts <= 0:
                    # A record without a readable timestamp cannot be placed on a
                    # timeline; skip it rather than sorting garbage to the top.
                    continue
                # ``seq`` is the log's own append order, which is what the former
                # read index stood in for -- and it stays the same number whatever
                # slice this read returns, so same-second ties break identically.
                out.append((ts, int(event.get("seq", 0)), record))
            if len(out) > _ACTIVITY_LIMIT:
                # One past the display cap is enough: the sort below can only trim
                # the oldest tail, and `capped` is a boolean, not a total.
                break
            if len(page) < _ACTIVITY_PAGE:
                break  # the log is exhausted
            oldest = page[-1].get("seq")  # pages come newest-first
            if not isinstance(oldest, int) or (before is not None and oldest >= before):
                # No usable cursor, or one that did not move: stop rather than
                # re-read the same page for ever.
                break
            before = oldest
        return out

    entries = await asyncio.to_thread(_read_activity_records)

    def _sanitize(text: str) -> str:
        # Same redaction chain the roster's message preview uses: a project
        # value is an operator-supplied path that can embed a credential or
        # presigned URL, and this response is a network boundary. Run it on
        # the FULL value (nothing here truncates, so order is trivial today,
        # but keeping the shared chain means a future cap cannot split a
        # token past the patterns).
        text, _ = _h.redact_exfiltration_urls(text)
        text, _ = _h.redact_credentials(text)
        return text

    rows: list[tuple[float, int, dict]] = []
    for ts, seq, entry in entries:
        rows.append(
            (
                ts,
                seq,
                {
                    "ts": ts,
                    "via": entry.get("via", "") or "chat",
                    "project": _sanitize(str(entry.get("project", "") or "")),
                },
            )
        )
    # Newest first — the drawer renders top-down and the newest event is the
    # one the user opened the drawer to see. The log's ts is second-precision,
    # so append order (the read index) breaks same-second ties: without it two
    # events in one second would render oldest-first at the top. The display
    # cap applies AFTER the member filter and the sort, so it can only ever
    # trim the oldest tail — never another member's share of a shared log.
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    capped = len(rows) > _ACTIVITY_LIMIT
    return web.json_response(
        {
            "slug": slug,
            "member": member,
            # `capped` tells the drawer its derived counters are floors, not
            # totals, once the window is saturated — it renders "N+" instead
            # of asserting an exact count it cannot know.
            "capped": capped,
            "entries": [r[2] for r in rows[:_ACTIVITY_LIMIT]],
        }
    )


async def api_member_briefing(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/briefing?member=<name> — a crewmate's own notes, read-only.

    Feeds the Crewmates page panel's Notes tab. The briefing
    (``members/<slug>/briefing.md``) is the crewmate's self-maintained standing
    notes — an AGENT-written file, curated by the crewmate for its future self.
    This endpoint reads and never writes, and the panel offers no editor for
    the file: the dashboard's file viewer reads through a redacting path and
    its Save writes the buffer back, so any in-dashboard edit of an
    agent-written file could replace a secret the crewmate wrote in the
    meantime with its placeholder. The notes are edited where the crewmate
    writes them, outside the dashboard.

    The text comes from :func:`members.read_member_briefing_bounded` and
    inherits its total contract: a missing or unreadable file reads as ``""``
    (the normal state of a fresh crewmate — never a 404), and content past
    ``MEMBER_BRIEFING_MAX_CHARS`` is cut at the cap with a visible marker
    (:func:`members.cap_member_briefing`, applied AFTER redaction so the cap
    cannot split a token past the patterns), which the panel renders as-is so
    the human sees the same overflow the crewmate is shown. ``supported`` is :func:`members.member_briefing_supported`:
    on platforms without ``O_NOFOLLOW`` and the pinned ancestor walk the read
    fails closed to ``""`` and the panel explains that from the flag rather
    than presenting an empty briefing as "no notes yet". ``updated_ts`` is the
    file's own mtime (epoch seconds) or ``null`` when there is no file.
    ``upkeep`` is the SYSTEM-owned text beside it (:func:`dashboard_upkeep.view`):
    the pointer line its ``[DASHBOARD]`` block carries, each agentic field's upkeep
    text as ``dashboard_fields`` returns it, and its helper script names -- or
    ``null`` when it has no page.
    ``redacted`` and ``truncated`` each say the wire text shows less than the
    file holds (a secret replaced by its placeholder; a tail past the cap not
    shown), so the panel can say so above the notes instead of leaving
    placeholders and a marker unexplained. A successful read leaves a SEL row
    (``members.briefing.read`` / ``allowed``), as the rules read does.

    ``member`` (query, REQUIRED) is the exact crew name, same posture as the
    activity endpoint: slugification is lossy, and the exact name is echoed
    back so the frontend keys its cache by name rather than by a slug two
    crewmates can share -- and, as on the rules endpoint, the exact name must
    derive this slug, exist, and be the ONLY crew that derives it: the briefing
    is one file per slug, so for a colliding slug the notes belong to neither
    crewmate and the read is refused (409 ``briefing_slug_ambiguous``) rather
    than shown -- with an Edit -- as one of theirs.
    """
    denied = await _deny_app_caller(request, "members.briefing")
    if denied is not None:
        return denied
    # Owner gate, the rules endpoint's boundary: the briefing is the crewmate's
    # private working memory, written for its owner. A Telegram, Teams or Webex
    # allowlist user can mint a dashboard session, so the app-caller guard alone
    # would let a non-owner colleague read notes the owner never shared. Gated
    # before any validation or file IO, so a denial costs no read.
    owner_denied = await require_owner_dashboard_request(request, "members.briefing.read")
    if owner_denied is not None:
        return owner_denied
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    member = request.query.get("member", "")
    # Same eligibility bar as the rules endpoint: the briefing is prompt-injected
    # working memory, so a stored name that cannot reach a model has no notes to
    # show. Display names are free-form text; the identifier grammar is not the test.
    if not members_mod.is_dispatchable_member_name(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )
    # The briefing is a PER-SLUG file and the slug is lossy (`Code_Reviewer` and
    # `code-reviewer` share one), so for a colliding slug the file belongs to
    # neither crewmate cleanly: showing it as one member's notes -- with an Edit
    # that saves over it -- would let the two overwrite each other. Same posture
    # as the rules endpoint: verify the exact member derives this slug, exists,
    # and is the ONLY one that does; otherwise refuse with a coded answer the
    # panel turns into a plain sentence. Config read off-loop (file IO).
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    try:
        if members_mod.member_slug(member, cfg) != slug:
            return web.json_response(
                {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
            )
    except MemberSlugError:
        return web.json_response(
            {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
        )
    if member not in cfg.agents:
        return web.json_response(
            {"error": "no crew member for this slug", "code": "member_not_found"}, status=404
        )
    if _member_names_for_slug(cfg, slug) != [member]:
        return web.json_response(
            {
                "error": "multiple crews share this slug; their notes would be ambiguous",
                "code": "briefing_slug_ambiguous",
            },
            status=409,
        )

    supported = members_mod.member_briefing_supported()

    # The pinned, bounded open (text + mtime from one descriptor) is blocking
    # file IO: one hop off the loop.
    text, updated_ts, read_bounded = await asyncio.to_thread(
        members_mod.read_member_briefing_bounded, slug
    )

    # Same redaction chain as the activity endpoint: the briefing is an
    # AGENT-written file, so a token the crewmate pasted into its own notes
    # would otherwise cross this network boundary into the browser verbatim.
    # Run on the whole BOUNDED buffer, BEFORE the character cap: a redaction
    # over already-capped text cannot match a token the cap split in two, and
    # the plaintext half would cross the boundary unmatched. The cap comes
    # after -- judged on the REDACTED length, so a briefing that only
    # overflowed before its placeholders shrank it is shown whole -- and drops
    # a trailing split word for the same reason (the bounded read has an edge
    # of its own).
    text, url_hits = _h.redact_exfiltration_urls(text)
    text, cred_hits = _h.redact_credentials(text)
    text, truncated = members_mod.cap_member_briefing(text, read_bounded, drop_split_tail=True)
    # Whether the text on the wire shows less than the file holds, so the panel
    # can say so above the notes: ``redacted`` when a placeholder replaced a
    # secret, ``truncated`` when the marker stands in for the tail.
    redacted = bool(url_hits or cred_hits)
    # The system-owned half of what the crewmate is told about its page: each
    # agentic field's upkeep text exactly as `dashboard_fields` returns it, plus the
    # pointer line and the helper names (`dashboard_upkeep.view`). Not the briefing
    # read, so it does not depend on `supported`, and a read that writes nothing.
    from kiro_crew import dashboard_upkeep

    upkeep = await asyncio.to_thread(dashboard_upkeep.view, slug)
    if upkeep is not None:
        # Field names come from the crewmate's own copy of the manifest, which it can
        # edit, so every string crosses the same redaction chain as the notes.
        def _clean(text: str) -> str:
            nonlocal redacted
            value, url_hits_u = _h.redact_exfiltration_urls(text)
            value, cred_hits_u = _h.redact_credentials(value)
            redacted = redacted or bool(url_hits_u or cred_hits_u)
            return value

        upkeep["pointer"] = _clean(upkeep["pointer"])
        upkeep["fields"] = [
            {key: _clean(value) for key, value in row.items()} for row in upkeep["fields"]
        ]
        upkeep["scripts"] = [_clean(name) for name in upkeep["scripts"]]

    # Audit the disclosure, as the rules read does: WHO read a crewmate's
    # private notes matters as much as who was refused, and a denied-only
    # trail cannot answer "was this boundary disclosed". Best effort -- an
    # audit must never change the outcome.
    try:
        _sel().log_api_access(
            caller=request.remote or "",
            operation="members.briefing.read",
            outcome="allowed",
            source="dashboard",
            resources=f"slug={slug}",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for members.briefing.read failed", exc_info=True)

    return web.json_response(
        {
            "slug": slug,
            "member": member,
            "supported": supported,
            "text": text,
            "updated_ts": updated_ts,
            "redacted": redacted,
            "truncated": truncated,
            "upkeep": upkeep,
        }
    )


async def api_member_recap(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/recap?member=<name> — the work a crewmate holds.

    Feeds the cold-start welcome (``mateGreeting.ts``): goals left open in the
    crewmate's thread or a recent session, then its recent sessions
    (:mod:`kiro_crew.member_recap`). A read that writes nothing; the page
    decides when to greet. Same owner gate and exact-name posture as the
    activity read: titles are the owner's own task text.
    """
    from kiro_crew import member_recap

    denied = await _deny_app_caller(request, "members.recap")
    if denied is not None:
        return denied
    owner_denied = await require_owner_dashboard_request(request, "members.recap.read")
    if owner_denied is not None:
        return owner_denied
    _audit_owner_read(request, "members.recap.read")
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    member = request.query.get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    try:
        if member not in cfg.agents or members_mod.member_slug(member, cfg) != slug:
            raise MemberSlugError(member)
    except MemberSlugError:
        return web.json_response(
            {"error": "no crew member for this slug", "code": "member_not_found"}, status=404
        )
    state: DashboardState | None = request.app.get("state")
    log = getattr(state, "conversation_log", None)

    def _gather() -> dict[str, Any]:
        from kiro_crew import session_ledger

        binding = members_mod.read_dm_binding(slug)
        slot_key = binding["slot_key"] if binding and binding.get("member") == member else ""
        thread_ledger = session_ledger.read_state(slot_key) if slot_key else {}
        recent = member_recap.recent_sessions(log.list_sessions() if log else [], member)
        ledgers = [
            session_ledger.read_state(session_ledger.ledger_key(str(r["key"]))) for r in recent
        ]
        return member_recap.recap(thread_ledger, recent, ledgers)

    payload = await asyncio.to_thread(_gather)
    return web.json_response({"slug": slug, "member": member, **payload})
