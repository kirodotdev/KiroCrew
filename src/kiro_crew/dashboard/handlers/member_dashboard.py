"""HTTP for one crewmate's dynamic dashboard: the instance, the registry, share, snapshots.

``GET /api/members/{slug}/dashboard`` is the one route the Dashboard tab's frame reads,
and its body is the shape CONTRACT-v3 fixes between the two:
``{instance_version, template: {id, version}, html, manifest, state}``.

**``?member=`` is required**, exactly as the briefing and rules reads require it, and
for the reason those give: slugification is lossy, so two crew names can reach one slug.
A dashboard instance is ONE directory per slug, so for a colliding slug the instance
belongs to neither crewmate. The exact name must derive this
slug, exist in config, and be the only name that derives it.

**The read is owner-gated.** The fields this body carries are work-ledger and crew-log
data, and ``work_ledger_board`` answers a non-owner ``owner_only`` for the same values,
so arriving as rendered html does not make them a wider audience's.

**App tokens are denied outright.** An app token scoped to ``/api/members`` reaches
this by PREFIX, and a crewmate's dashboard is inside exactly what that isolation
withholds.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import Any, Callable, Final, Mapping

from aiohttp import web

from kiro_crew import dashboard_agentic, dashboard_frame
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import conditional_get
from kiro_crew.dashboard.handlers import member_dashboard_fields
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.member_dashboard_render import _render
from kiro_crew.dashboard.handlers.members import (
    _deny_app_caller,
    _member_names_for_slug,
    _member_thread_slot,
)
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.members import MemberSlugError
from kiro_crew.platform.context import redact_code_via_context, redact_via_context

logger = logging.getLogger(__name__)

__all__ = ["register_member_dashboard_routes"]


def _bad(code: str, message: str, status: int = 400) -> web.Response:
    """One refusal shape for the whole module: a code a client branches on, and a sentence.

    Both halves, always. A client cannot branch on prose, and a person cannot act on a
    code -- a surface given only one of the two either hard-codes English or shows the
    user ``instance_refused``.
    """
    return web.json_response({"error": message, "code": code}, status=status)


def _name_unsafe(name: str) -> bool:
    """``True`` when *name* is a string the egress redactor would rewrite.

    Asked of an IDENTIFIER rather than of prose, which is why it answers a boolean and
    not a safe copy. Every other string on this surface is scrubbed by replacing it --
    ``_page_safe`` for a value, ``_template_text_safe`` for markup -- because nothing
    downstream joins on its bytes. A slug is joined on: the body names the package the
    tab mounts, the frame names the package a patch belongs to, and the refetch names
    the package it wants. Rewriting it would make those three disagree, so the only
    answers available are "serve it raw" and "do not serve it".

    ``redact_via_context`` for the reason ``_page_safe`` uses it: it is the canonical
    egress shim, so a host with a loaded companion applies that companion's patterns
    too, and the question here is exactly "would this leave the machine rewritten".
    """
    return bool(name) and redact_via_context(name) != name


async def _resolve(request: web.Request) -> tuple[str, str] | web.Response:
    """``(slug, member)`` for this request, or the refusal to return instead.

    The four questions the sibling member routes ask, in their order: is the slug a
    slug, is the member name one that can reach a model, does that name derive THIS
    slug and exist, and is it the only name that does.
    """
    denied = await _deny_app_caller(request, "members.dashboard")
    if denied is not None:
        return denied
    slug = request.match_info.get("slug", "")
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return _bad("invalid_member_slug", "invalid member slug")
    member = request.query.get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        return _bad("missing_member", "member query parameter required")
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    # The ROSTER's own spelling, before either check. The roster is keyed by the
    # display name somebody typed, so `Atlas` is the key and a tab opened on `atlas`
    # is the same crewmate asking for its own page -- answered, before this, with
    # "no crew member for this slug" while its data sat under the canonical key.
    # Resolved ONCE and carried, because the slug check and the roster check have to
    # agree about which name they are asking about.
    member = members_mod.canonical_member_key(member, cfg)
    try:
        if members_mod.member_slug(member, cfg) != slug:
            return _bad("member_slug_mismatch", "member does not match slug")
    except MemberSlugError:
        return _bad("member_slug_mismatch", "member does not match slug")
    if member not in cfg.agents:
        return _bad("member_not_found", "no crew member for this slug", status=404)
    if _member_names_for_slug(cfg, slug) != [member]:
        return _bad(
            "dashboard_slug_ambiguous",
            "multiple crews share this slug; their dashboards would be ambiguous",
            status=409,
        )
    return slug, member


def _dashboard_slot(member: str, slug: str) -> str:
    """The DM slot this crewmate's dashboard reads its fold values from.

    A V2 member's DM log lives on ``member_slot_key(slug, store)``, so the bare
    ``member_slot_key(slug)`` names a different, empty slot: the dashboard then reads no
    values and renders every field unresolved while the crewmate's thread is right there.
    :func:`_member_thread_slot` is the same derivation ``api_member_thread`` uses to
    CREATE the thread, so it names the slot the session actually runs under.

    Falls back to the V1 key when the store resolution refuses (``UnknownMemoryStore``) --
    an unknown member, or a V2 record that is missing or degraded. A degraded store record
    must read as "no values yet" rather than as a dashboard that cannot be served at all.
    """
    try:
        cfg = KiroCrewConfig.load()
        slot, _store = _member_thread_slot(cfg, member, slug)
    except Exception:
        logger.debug("dashboard: falling back to the V1 slot for %r", slug, exc_info=True)
        return members_mod.member_slot_key(slug)
    return slot


def _write_session(slug: str, member: str) -> str:
    """The session a dashboard change's history entry belongs to: the crewmate's DM log.

    Derived, not looked up, and empty when there is none. The instance record is a file
    and is already committed by the time this is used, so a crewmate whose DM thread has
    never run gets a working dashboard with no history row rather than a refused change.

    Resolved by the slot the member's DM thread runs under, then by the newest session
    unit on it: a slot owns one session id at a time, and the newest is the live one.

    "Newest" is taken from the DURABLE succession chain the store wrote, not from the
    header clock. A unit's ``createdAt`` is stamped once and never rewritten, so a clock
    that steps backward between two units of one slot -- an NTP correction, a VM resume
    -- lists the retired unit last, and this would then attribute every later change to
    a session that is already over.
    """
    return _newest_unit(_dashboard_slot(member, slug))


def _newest_unit(slot: str) -> str:
    """The live session unit on *slot*, or ``""`` when it has none or it cannot be read."""
    try:
        # Imported here, not at module scope, for the reason every other store call in
        # this handler is: the crew log is an optional subsystem and the boot path must
        # not load it.
        from kiro_crew.crew_log.projection import units_in_succession

        units = units_in_succession(slot)
        return units[-1] if units else ""
    except Exception:
        logger.debug("dashboard: no session unit on %r", slot, exc_info=True)
        return ""


def _live_or_newest_unit(state: Any, slot: str | None) -> str:
    """The unit the slot's LIVE session is serving, else :func:`_newest_unit`.

    The page's session folds (``usage``, ``timeline``, ``tools``, ``subagents``) are
    keyed by one unit, and "newest" orders a slot's unrelated chains by their roots'
    header clock: a unit that joined the slot after the live session's chain began --
    an imported or rebuilt log, a chain whose link to the live one was never written --
    would win, and the page would read another session's spend as this one's (a pod
    showed 0.0 credits beside 94.1 recorded). The live provider's own id answers the
    question the page asks, so it leads whenever it is one of the slot's units.
    """
    if not slot:
        return ""
    try:
        from kiro_crew.crew_log.projection import units_in_succession
        from kiro_crew.crew_log.resolve import unit_for_session_key

        live = unit_for_session_key(getattr(state, "sessions", None), f"dashboard:{slot}")
        if live and live in units_in_succession(slot):
            return live
    except Exception:
        logger.debug("dashboard: no live session unit on %r", slot, exc_info=True)
    return _newest_unit(slot)


def _utc_stamp(stamp: str) -> str:
    """*stamp* as one aware UTC second (``...+00:00``), or ``""`` when it is not one.

    A chat row's ``ts`` is stored in two formats -- offset-aware, and the naive local
    wall clock older builds wrote -- and the page compares this against
    ``written_at``, which is UTC. Read through :func:`~kiro_crew.history.
    transcript_sort_key`, the repository's one rule for both formats (a naive value
    is the writer's own local time), so the page is handed one domain and never has
    to guess which clock a bare stamp came from.

    WHOLE SECONDS, cut down rather than rounded, because ``written_at`` is: the fold
    spells a write time with ``timespec="seconds"``. A reply at ``:04.3`` and a list
    the crewmate wrote at ``:04.7`` then compare EQUAL, and only a reply in a LATER
    second folds a card. At sub-second precision the reply would read as the later of
    the two and fold a list written after it.

    A stamp that parses can still be unusable: resolving a naive value at the
    representable boundary raises, which ``_latest_stamp`` documents. That costs the
    stamp, never the page.
    """
    from datetime import datetime, timezone

    from kiro_crew.history import transcript_sort_key

    if not stamp:
        return ""
    try:
        bucket, seconds = transcript_sort_key(stamp)
        if bucket != 0:
            return ""
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ""


def _last_user_at_of(slot: Any, persisted: object = "") -> str:
    """When a PERSON last spoke in *slot*'s chat, as a UTC stamp, or ``""``.

    The newest row a human send path marked -- a typed message, an option clicked in
    the chat, a steer into a running turn -- read by the save path's own
    :func:`~kiro_crew.dashboard.slot_persistence.metadata_line._newest_human_turn_ts`,
    so this and the session's stored ``last_user_at`` agree about what a human turn
    is. ``loaded_messages`` rather than ``messages``: a restored row whose window was
    never loaded must not be loaded just to draw a page, and it has no row that is
    not already on disk.

    *persisted* is that stored stamp, folded in through ``_latest_stamp``, the same
    fold the save makes. It is what answers when the window holds no human row: a
    row still unloaded, a window scrolled past the last turn, or no live slot at all.
    The live window is what answers between saves, which is when a reply matters to
    a page -- a steer is on the slot the moment it is sent and on disk only at the
    next flush.
    """
    from kiro_crew.dashboard.slot_persistence.metadata_line import (
        _latest_stamp,
        _newest_human_turn_ts,
    )

    live = ""
    if slot is not None:
        try:
            live = _newest_human_turn_ts(slot.loaded_messages())
        except Exception:
            logger.debug("dashboard: could not read the person's last turn", exc_info=True)
    stored = persisted if isinstance(persisted, str) else ""
    return _utc_stamp(_latest_stamp(live, stored))


def _person_last_spoke(state: Any, slug: str, member: str, slot: str | None) -> str:
    """:func:`_last_user_at_of` for the chat this page belongs to: its live slot and its file.

    The chat is *slot* for a root session's own page and the crewmate's DM thread
    otherwise -- the slot :func:`read_fields` reads the page's folds from, so a reply
    and the cards it answers are read from one thread.

    *state* is the gateway's ``DashboardState``, or ``None`` where there is none. File
    IO -- the config read behind the DM slot and the transcript's metadata line -- so
    callers run it off the event loop, beside the fold read it is composed with. ``""``
    on any failure, which leaves every card asking, as no stamp at all would.
    """
    if state is None:
        return ""
    try:
        from kiro_crew.dashboard.chat_utils import slot_history_key, slot_transcript_key

        slot_key = slot if slot is not None else _dashboard_slot(member, slug)
        if not slot_key:
            return ""
        live = state.get_slot(slot_key)
        persisted: object = ""
        log = getattr(state, "conversation_log", None)
        if log is not None:
            key = slot_history_key(live) if live is not None else slot_transcript_key(slot_key)
            persisted = log.get_metadata(key).get("last_user_at") or ""
        return _last_user_at_of(live, persisted)
    except Exception:
        logger.debug("dashboard: no last turn for %r's chat", slug, exc_info=True)
        return ""


async def _owner_only(request: web.Request, operation: str) -> web.Response | None:
    return await require_owner_dashboard_request(request, operation)


def _bound_package_slug(member: str) -> str:
    """The ``kind="dashboard"`` artifact bound to *member*, or ``""``.

    :func:`~kiro_crew.artifact_store.dashboard_package.resolve_bound_slug` with this
    route's own answer for the cases it raises on. It raises because its other caller
    must tell "no package" from "cannot prove there is none" -- a write validated
    against the wrong Model replaces a cell with a value of the wrong shape. THIS
    caller is a read that draws a page, and the page it draws when the answer is empty
    is the template instance every crewmate already has. So an unprovable answer is
    answered the same way as an absent one: the crewmate keeps the page it has, and a
    failed lookup costs a package page rather than showing the wrong one.

    ``BindingAmbiguous`` arrives through the same ``except`` by being a subclass of
    ``BindingLookupIncomplete``, and wants the same answer here for the same reason.

    Blocking file IO: it lists the artifact library and reads each candidate. Callers
    run it off the event loop.
    """
    try:
        # ONE lookup, shared with the crewmate's `[DASHBOARD]` turn block, so the
        # page the person sees and the page the crewmate is told about agree.
        return dashboard_agentic.bound_package(members_mod.member_slug(member))
    except Exception:
        logger.debug("dashboard: %r's package binding could not be resolved", member, exc_info=True)
        return ""


def _package_in_force(slug: str) -> tuple[instance.PinnedPackage | None, bool, bool]:
    """``(pinned page, older rule, locked)`` for *slug*, from its instance record.

    The write path asks the first two questions (``dashboard_package.read_package_model``),
    so the page and the write check name one Model. The third is the person's layout lock,
    which a package page reports exactly as a template page does. Blocking file IO.
    """
    record = instance.read(slug)
    return (
        instance.pinned_package(record),
        instance.keeps_older_package_rule(record),
        record.structure_locked,
    )


async def _run(fn: Callable[[], Any]) -> Any:
    """Run a blocking call off the event loop: file IO, or work over a whole page."""
    return await asyncio.to_thread(fn)


def _refusal(exc: Exception) -> web.Response:
    """Turn a store refusal into an answer. Separate codes, because they differ for a client.

    ``*_refused`` is the caller's input and a retry of the same request will be refused
    again; ``*_failed`` is this gateway's state and a retry may work. Collapsing them
    would make a client either retry forever or give up on a transient fault.
    """
    if isinstance(exc, instance.InstanceLocked):
        return _bad("dashboard_locked", str(exc), status=423)
    if isinstance(exc, instance.InstanceRefused):
        return _bad("dashboard_refused", str(exc), status=409)
    if isinstance(exc, catalog.UnknownTemplate):
        return _bad("template_not_found", str(exc), status=404)
    logger.warning("dashboard store call failed", exc_info=exc)
    return _bad("dashboard_failed", "the dashboard store could not serve this", status=500)


# --------------------------------------------------------------------------
# the instance
# --------------------------------------------------------------------------


async def api_member_dashboard(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/dashboard?member=<name> — the crewmate's own dashboard.

    The body is CONTRACT-v3's fixed shape. ``state`` is one of ``empty``, ``live``,
    ``stale`` and ``error``, DERIVED at read time rather than stored: a stored flag
    would be a claim about the registry made when the instance was last written, and a
    template shipping a new version makes every copy of it stale without touching one
    instance file.

    A crewmate that never adopted a template answers 200 with ``state: "empty"``, never
    404: having no dashboard yet is the ordinary first state of every crewmate, and the
    frame's empty state IS that answer. A 404 here would make "nothing adopted" and "no
    such member" one reading for the tab.

    For that case the body also carries the DEFAULT template, rendered. An empty frame
    answers none of the questions a person opened the tab with, so what they see is the
    default page; ``state`` stays ``empty`` and ``instance_version`` stays 0, because
    nothing was adopted and nothing was written -- the snapshot route still refuses on
    that state, and the frame draws whatever ``rendered_html`` carries. ``template``
    names the default rather than staying blank, so a reader of this body can tell which
    page the html belongs to.

    OWNER-ONLY. The fields this body carries are work-ledger and crew-log data, and
    ``work_ledger_board`` answers a non-owner ``owner_only`` for the same values, so
    arriving as rendered html does not make them a wider audience's.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    # OWNER-ONLY, like every other route that serves this crewmate's fold values.
    # The page's fields ARE work-ledger and crew-log data -- task titles, summaries,
    # PR links -- and `work_ledger_board` answers the same caller `owner_only` for
    # exactly those. Serving them here because they arrive as rendered html rather
    # than as JSON would make the gate a property of the response format.
    #
    # Refused rather than rendered with an empty read: an empty read has no resolved
    # fold, which IS the stale condition, so the page would tell a non-owner its
    # numbers are older than the record when the truth is that they were withheld.
    owner_denied = await _owner_only(request, "members.dashboard")
    if owner_denied is not None:
        return owner_denied
    slug, member = resolved
    return await _serve(request, slug, member, None)


async def api_session_dashboard(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/dashboard -- one ROOT session's own dashboard.

    The member route's body and its gates, keyed by the session instead of a crewmate:
    the instance is the session's own (:func:`instance.session_instance_key`), and every
    fold a field reads is read from THIS slot, so a session's page shows the work
    ledger and crew log that session wrote.

    ROOT SESSIONS ONLY, by :func:`card_lifecycle.is_root_session`, the test the
    automatic card already applies. A dispatched or adopted session is answered 404
    ``not_root_session``, which the side panel reads as "this session has no page" --
    a worker's records belong to the board of whoever dispatched it.

    App tokens are denied and the read is owner-only, for the reasons the member route
    gives: the fields are work-ledger and crew-log data.
    """
    denied = await _deny_app_caller(request, "sessions.dashboard")
    if denied is not None:
        return denied
    owner_denied = await _owner_only(request, "sessions.dashboard")
    if owner_denied is not None:
        return owner_denied
    from kiro_crew.dashboard.card_lifecycle import is_root_session

    state = request.app["state"]
    slot = state.get_slot(request.match_info.get("slot", ""))
    if slot is None:
        return _bad("slot_not_found", "no session for this slot", status=404)
    if not is_root_session(slot):
        return _bad(
            "not_root_session",
            "only a root session has a dashboard; this one was dispatched or adopted",
            status=404,
        )
    slot_key = str(slot.key)
    return await _serve(request, instance.session_instance_key(slot_key), "", slot_key)


async def _serve(request: web.Request, slug: str, member: str, slot: str | None) -> web.Response:
    """The dashboard read for one store key, after the caller's gates have passed.

    *slug* is the store key, *member* the crewmate's name (``""`` for a session) and
    *slot* the slot every fold is read from, or ``None`` for the crewmate's DM slot.
    """
    _member = member
    # The reader's UI language, as the browser resolved it. Checked against the
    # shipped catalogs in `dashboard_frame.page_locale`; anything else is English.
    locale = request.query.get("locale", "")
    # The gateway's live chat slots, which say when the person last spoke in this
    # page's chat. Taken here, on the loop; read off it with the fold values.
    state = request.app.get("state")
    package_slug = request.query.get("dashboard", "")
    #: A PREVIEW SHOWS THE STAGED PAGE OR NOTHING. `instance.preview_url` carries no
    #: `dashboard=`, so filling the slug in from the crewmate's binding would serve the
    #: LIVE package page under a panel labelled "not applied yet" -- the person then
    #: approves a page they never saw, which is the one thing this route must not do.
    #: It also armed that package's live page, re-pointing the push machinery mid-look.
    #:
    #: Checked BEFORE the lookup rather than after it, so a preview costs no library
    #: listing and the package branch is not left one condition away from running. A
    #: caller that NAMES a package asked for that package and still gets it; the tab
    #: never sends both.
    staged_only = request.query.get("preview") == "1" and not package_slug
    # WHICH PAGE IS IN FORCE is the instance record's to say, lock included (`read`
    # answers the lock's snapshot while the person has locked the layout). A record
    # that pins a package is served from the gateway's own snapshot of the bytes the
    # owner saved, so no later artifact write, delete-and-save or version pruning
    # moves the page. A record that released packages, or an older record under the
    # lock, serves its template copy. Only an older unlocked record keeps the rule of
    # looking a package up by the crewmate's binding.
    pinned: instance.PinnedPackage | None = None
    older_rule = False
    structure_locked = False
    if not staged_only:
        try:
            pinned, older_rule, structure_locked = await _run(lambda: _package_in_force(slug))
        except Exception as exc:
            return _refusal(exc)
        if pinned is not None:
            package_slug = pinned.slug
        elif not older_rule:
            package_slug = ""
    if older_rule and not package_slug:
        # Named by the crewmate's own binding, off the event loop because it lists the
        # artifact library. THE PACKAGE LINE'S OWN LOOKUP answers this -- one
        # implementation of "which package is bound here" for every caller that asks.
        package_slug = await _run(lambda: _bound_package_slug(_member))
        # ONLY A PAGE A PERSON CUT. A bound package with no such version is the
        # crewmate's saved draft between `dashboard_save` and `dashboard_apply`, and
        # the tab keeps the page it had -- the same answer the write check and the
        # turn block give (`dashboard_agentic.has_person_cut_version`).
        bound = package_slug
        if bound and not await _run(lambda: dashboard_agentic.has_person_cut_version(bound)):
            package_slug = ""
    if _name_unsafe(package_slug):
        # A SLUG IS A NAME TOO, and it travels further than the others: the body carries
        # it twice, every pushed frame carries it, and the refetch answers with it. An
        # artifact slug is `[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?`, which forbids `_` and
        # upper case and so cannot be a `ghp_` or an AWS key -- but it CAN be a Slack
        # bot token (`xoxb-<digits>-<chars>`) or a GitLab PAT (`glpat-<chars>`), and
        # both are shapes the redactor rewrites.
        #
        # HERE, where the slug ENTERS, rather than at each of the three places it
        # leaves: this is the one point both sources pass through, and refusing before
        # `_serve_package` means no package is read and no page is armed -- which also
        # settles the refetch, since it answers from an armed page or 409s.
        #
        # The two sources get different answers because they are different requests.
        if request.query.get("dashboard", ""):
            # THE CALLER NAMED IT, so the caller is told. A code rather than a
            # sentence about secrets, and the slug is not echoed back.
            logger.warning("dashboard: %r named a package slug that would be redacted", slug)
            return _bad(
                "dashboard_slug_refused",
                "that dashboard package cannot be served: its slug would be rewritten "
                "on its way to a page, and the page and its pushes join on that slug",
                status=409,
            )
        # NOBODY ASKED FOR IT: this slug came from the crewmate's own binding. Treated
        # as an unresolved binding, which is the template page every crewmate has on
        # this branch -- so a credential pasted into an artifact's NAME costs its owner
        # the package page and never the tab. Logged, so the rename is findable.
        logger.warning(
            "dashboard: %r's bound package has a slug that would be redacted; serving "
            "the template page instead",
            slug,
        )
        package_slug = ""
    if package_slug:
        # A PAGE ITS OWNER WROTE, served from the same route and behind the same owner
        # check as a template instance. The body keeps the shape the tab already reads
        # -- `rendered_html` is what it mounts -- so a package page needs no second
        # route and no second frame contract, which are the two places this surface
        # cannot afford to get the gate wrong twice.
        return await _serve_package(
            request,
            slug,
            _member,
            package_slug,
            locale,
            # WHO NAMED THE PACKAGE, which decides what an absent one answers. The
            # caller that asked for a slug by name gets a code; a slug that came from
            # the crewmate's own binding does not, because nobody asked -- the same
            # split this route already makes for a slug it refuses above.
            asked_for=bool(request.query.get("dashboard", "")),
            pinned=pinned,
            structure_locked=structure_locked,
        )
    if request.query.get("preview") == "1":
        # The STAGED page, which no version records. Served from the same route and
        # behind the same owner check: a staged page carries this crewmate's fold
        # values exactly as the live one does, so a route of its own would be a second
        # place to get that gate right. `instance.preview_url` builds the PAGE link that
        # reads this: the Members page's Dashboard tab, which asks here with
        # `preview=1` and draws the answer under a preview label.
        #
        # Only a TEMPLATE is ever staged: a composed package goes in force on apply.
        # A store refusal (a linked session directory, an unreadable record) answers
        # in the store's own words, as every other read on this route does.
        try:
            staged = await _run(lambda: _preview_record(slug))
        except Exception as exc:
            return _refusal(exc)
        if staged is None:
            return web.json_response(
                {"error": "nothing is staged to preview", "code": "no_preview"}, status=404
            )
        # Off the loop: the scrub reads the whole stored page. See `_safe_body`.
        body = await _run(lambda: _safe_body(staged.wire()))
        # The CURRENT version, not a new one, because staging wrote none. A reader that
        # saw this number move would believe the page had been installed.
        body["preview"] = True
        # Which staged page this is. The person's Keep sends it back, so the page that
        # lands is the one drawn here and not one staged again after the look.
        body["staged_ms"] = staged.updated_ms
        rendered = await _run(
            lambda: _render(
                slug,
                _member,
                staged,
                locale,
                slot=slot,
                last_user_at=_person_last_spoke(state, slug, _member, slot),
            )
        )
        if rendered is not None:
            body["rendered_html"] = rendered
        return web.json_response(body)
    try:
        record = await _run(lambda: instance.read(slug))
    except Exception as exc:
        return _refusal(exc)
    renderable = True
    if record.state == instance.STATE_EMPTY:
        fallback = await _run(lambda: instance.default_instance(slug))
        if fallback is not None:
            # The page named for an unadopted crewmate, or a root session's default, is
            # served, and the person may have locked it: the lock is the stored
            # record's, not the default's.
            record = dataclasses.replace(fallback, structure_locked=record.structure_locked)
        else:
            # NOTHING TO COMPOSE, so nothing is attempted. An empty record carries
            # `manifest={}`, which `parse_manifest` refuses, so `_render` would log a
            # warning with a traceback for a crewmate that has simply adopted
            # nothing -- on every poll and every projection frame, which is the
            # ordinary state of a crewmate with no page named for it. The frame's
            # own empty state IS the answer here, and it needs no html.
            renderable = False
    body = await _run(lambda: _safe_body(record.wire()))
    # THE SUGGESTIONS DECISION, made once on the server: the tab draws the suggestions
    # page instead of this read's default exactly when this says so, and the crewmate's
    # turn block is told the same answer by the same function. No package is being
    # served on this branch -- that path returned above.
    body["suggestions"] = dashboard_agentic.shows_suggestions(slug, record, package_slug="")
    # STALE belongs here with LIVE and EMPTY: `_state_of` calls a stale copy complete
    # and still renderable -- what it cannot do is be compared against or refreshed
    # from its source, which is what the frame's stale band says. A copy served
    # without composing carries no data island, no bootstrap, no band and no ready
    # beacon, so it shows no values at all. Only ERROR is excluded, because that is
    # the one state in which the copy does not parse.
    composed_a_page = False
    if renderable and record.state in (
        instance.STATE_LIVE,
        instance.STATE_EMPTY,
        instance.STATE_STALE,
    ):
        rendered = await _run(
            lambda: _render(
                slug,
                _member,
                record,
                locale,
                slot=slot,
                last_user_at=_person_last_spoke(state, slug, _member, slot),
            )
        )
        if rendered is not None:
            body["rendered_html"] = rendered
            composed_a_page = True
    # CONDITIONAL GET. The ETag is a STRONG validator over the whole serialized body,
    # not over a hand-listed set of inputs: `strong_content_etag` hashes the bytes, so
    # it covers every field the body carries (the composed page, `structure_locked`,
    # `state_reason`, the panel data) and cannot drift as a field is added -- a 304 then
    # means the client already holds these exact bytes, and the ~180 KB download is
    # skipped. `conditional_get` is the one implementation this repo uses for this (the
    # media routes share it), so there is no second copy of the compare to drift.
    #
    # THE VALIDATOR IS SCOPED TO THE AUTHENTICATED READER AND THE STORE KEY, not to the
    # body alone. This page is a per-reader, owner-only document (every field is this
    # crewmate's work-ledger and crew-log data), so the tag folds in the caller
    # `request["user"]` -- the subject the owner gate above already checked -- and the
    # store key `slug` ahead of the payload. Two different identities, or two different
    # crewmate pages, therefore MINT DIFFERENT TAGS even in the impossible case that
    # their composed bytes coincide, so one reader's `If-None-Match` can never score a
    # 304 against another's page. The owner gate is the real boundary (a non-owner is
    # 403'd before reaching here); this makes a cross-reader short-circuit impossible by
    # construction as well, which is the belt-and-braces a shared validator warrants.
    #
    # `private, no-cache`: the body is owner-only, so a client may revalidate but a
    # shared cache must not store it. Offered only when a page was actually composed --
    # the empty/error/no-html answers are small and carry no ETag, so they are never
    # short-circuited.
    if composed_a_page:
        payload = json.dumps(body).encode("utf-8")
        reader = str(request.get("user") or "")
        scope = f"{reader}\x00{slug}\x00".encode("utf-8")
        return conditional_get.conditional_response(
            request,
            payload,
            "application/json",
            etag=conditional_get.strong_content_etag(scope + payload),
            cache_control="private, no-cache",
        )
    return web.json_response(body)


#: Any key whose VALUE is a worker's session key, dropped from a value on its way to a
#: page. Named for the whole repository's rule rather than for one fold: the conductor
#: ledger's is that no reader but the conductor sees a session key, and
#: ``work_ledger_board._MASKED_ITEM_FIELDS`` masks exactly this on the Crew page.
#:
#: ``crew_key`` is the panel's ownership digest, which the panel read route withholds
#: from every client; a page that binds the ``panel`` fold must not carry it either.
_MASKED_VALUE_KEYS = frozenset({"worker_session_key", "crew_key"})

#: Event kinds whose ``text`` IS a session key, so masking the item field alone leaves
#: the key on the page inside the event log. The line is kept for its ``kind`` and
#: ``ts``, which a timeline needs and which the key is not required to express.
_KEY_BEARING_EVENT_KINDS = frozenset({"bind"})


def _page_safe(value: Any) -> Any:
    """*value* made safe to put on a page: session keys removed, strings redacted.

    ONE traversal doing both, because both are the same question asked of the same
    bytes -- what must not reach a browser -- and two passes are two places for the
    rule to drift.

    Every string here is AGENT-AUTHORED and nothing between the write and this read
    inspects it. A fold value is whatever a conductor put in the crew log: a
    `session_ledger_record(goal=...)` carrying a pasted private key is rendered by any
    template that binds that fold. Dropping the one field known to be a secret says
    nothing about prose that happens to contain one.

    A mapping's KEYS are agent-authored on the same terms as its values -- an artifact
    name is a free string -- so keys are redacted too, and a key that redacts onto one
    already present is suffixed rather than dropped, so two distinct rows do not
    collapse into one.

    `redact_via_context` rather than a named pair of redactors: it is the canonical
    egress shim, so a host with a loaded companion applies that companion's patterns
    too, and it is fail-closed on a composition error. `work_ledger_board._redact_deep`
    applies the same rule to the same data for the Crew page; this is that rule at the
    dashboard's own chokepoint.

    RECURSIVE and shape-agnostic on purpose. This page is served by
    ``GET /api/members/{slug}/dashboard``, which has no owner check, and a template
    declares its own fold paths -- so which fold reaches a page, and how deep the key
    sits in it, is a decision the TEMPLATE makes. A mask written against one fold's
    item shape covers the template that exists today and not the one adopted tomorrow,
    which is how the same key reached a page twice already: once on a task row and
    once on a board id.

    Applied to the resolved values rather than inside a fold, because the fold is also
    read by the conductor itself, which is the one reader allowed to see the key.
    """
    if isinstance(value, str):
        return redact_via_context(value)
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        kind = value.get("kind")
        for key, item in value.items():
            if key in _MASKED_VALUE_KEYS:
                continue
            if key == "text" and isinstance(kind, str) and kind in _KEY_BEARING_EVENT_KINDS:
                out[key] = ""
                continue
            safe_key = redact_via_context(key) if isinstance(key, str) else key
            if safe_key in out:
                suffix = 2
                while f"{safe_key} ({suffix})" in out:
                    suffix += 1
                safe_key = f"{safe_key} ({suffix})"
            out[safe_key] = _page_safe(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_page_safe(item) for item in value]
    return value


def package_declared_name_spots(package: Mapping[str, Any]) -> list[str]:
    """Where *package* declares a name a redactor would mask. Positions, never names.

    THE ONE SCAN over the two name sets a dashboard package carries -- its model's
    field names and its theme's token keys -- and the one place
    the rule lives. ``dashboard_save`` asks it and refuses a save that trips it. Two
    copies of this predicate would be two rules, and the first thing the second one
    would do is drift from this one.

    The save keeps a credential out of a page an agent asked for honestly, with a
    refusal it can act on while the name can still be changed.

    REFUSED RATHER THAN MASKED, at both ends, because every one of these names is a
    JOIN KEY: a field name ties the model to the page cells that read it, and a theme
    token is the CSS variable the style references. A redacted key hides nothing -- a declaration is a name, not a value
    -- and leaves the reference pointing at a key the package does not contain.

    The POSITION comes back and the name does not. A name that needs redacting is not
    a name to write to a log, and these sentences travel through the gateway's logs;
    the agent can resolve a position against the package in its own hand.

    The field and token order is the model's sorted key order, which is what the
    package validator canonicalises to, so an index here is the index the stored
    package has.
    """
    raw_model = package.get("model")
    raw_theme = package.get("theme")
    model: Mapping[str, Any] = raw_model if isinstance(raw_model, Mapping) else {}
    theme: Mapping[str, Any] = raw_theme if isinstance(raw_theme, Mapping) else {}
    raw_types = model.get("types")
    raw_tokens = theme.get("tokens")
    types: Mapping[str, Any] = raw_types if isinstance(raw_types, Mapping) else {}
    tokens: Mapping[str, Any] = raw_tokens if isinstance(raw_tokens, Mapping) else {}
    spots: list[str] = []
    for index, name in enumerate(sorted(types), start=1):
        if isinstance(name, str) and redact_via_context(name) != name:
            spots.append(f"declared field #{index} of {len(types)}")
    for index, token in enumerate(sorted(tokens), start=1):
        if isinstance(token, str) and redact_via_context(token) != token:
            spots.append(f"theme token #{index} of {len(tokens)}")
    return spots


def _safe_body(body: dict[str, Any]) -> dict[str, Any]:
    """One wire body with its page halves scrubbed, in place.

    ``html`` and ``manifest`` are the two keys carrying the stored template, and the
    body carries both RAW beside the composed page. Scrubbing only the composed one
    would leave the same credential one key away in the same response.

    Called through :func:`_run`: the scrub reads the whole page, which can be as large
    as :data:`kiro_crew.dashboard_frame.MAX_PAGE_BYTES`.
    """
    if isinstance(body.get("html"), str):
        body["html"] = _template_text_safe(body["html"])
    if "manifest" in body:
        body["manifest"] = _manifest_text_safe(body["manifest"])
    return body


def _template_text_safe(text: str) -> str:
    """One page's own text, scrubbed the way a fold value is.

    The SAME redactors ``_page_safe`` applies to a resolved value, applied to the
    markup and prose around it. Only a template that shipped with the product can
    become a page -- which is a claim about the repository, not about the bytes on
    THIS disk. A template directory somebody hand-edited, or an instance record
    written under an older gateway, reaches this handler as a page the parity check
    passed, and a parity check says nothing about a credential pasted into a heading.

    This is the one step that produces both the raw body and the composed document,
    so it is the only place the page and the values in it can be covered by one pass.
    ``_page_safe`` covers the resolved values and never the page around them.

    The text of a ``<script>`` or ``<style>`` element is the exception, by one pass: it
    is code the page runs, and the bare-secret guess rewrites an alphabet or a data URI
    in it without any error. Its tags, the attributes and the prose get every pass, and
    a page whose script text the rules would leave as prose gets every pass whole.
    """
    if not text:
        return text
    return dashboard_frame.map_page_text(text, redact_via_context, redact_code_via_context)


def _manifest_text_safe(manifest: Any) -> Any:
    """A manifest's or a package's own strings, scrubbed. Keys are left alone.

    Values only, and recursively, because the KEYS on both sides are structural
    names rather than data, and redacting a name breaks the document instead of
    hiding anything: the reference to it stays while the key moves.

    What makes that safe differs by caller, and both are write-side. A manifest's
    field names the loader has already matched against ``^[a-z][a-z0-9_]{0,63}$``,
    a grammar no credential survives. A PACKAGE adds theme tokens, which are CSS
    custom properties -- ``--[a-z][a-z0-9-]{0,63}`` admits a lowercase hyphenated
    look-alike -- so ``dashboard_save`` scans all three of its name sets and
    refuses the save outright. Either way nothing credential-shaped reaches a key
    here, and ``title``, ``label`` and ``description`` are free prose that does.
    """
    if isinstance(manifest, str):
        return redact_via_context(manifest)
    if isinstance(manifest, Mapping):
        return {key: _manifest_text_safe(value) for key, value in manifest.items()}
    if isinstance(manifest, (list, tuple)):
        return [_manifest_text_safe(item) for item in manifest]
    return manifest


def _preview_record(slug: str) -> Any | None:
    """The staged page as a readable record, or ``None`` when nothing is staged.

    Built as an ``Instance`` so that ONE renderer serves both: a preview a person looks
    at must be filled with the same fold values, masked by the same pass and composed
    by the same builder as the page it would replace, or they have been shown something
    other than what applying it would give them.

    ``instance_version`` is the CURRENT record's, because staging wrote no version.
    ``state`` is ``live``, which is what a staged page is in the only sense the frame
    reads that field for: it parses, its bindings match its manifest -- both checked at
    staging -- so it renders. The body carries ``preview: true`` beside it so no reader
    has to infer which of the two it is holding.
    """
    preview = instance.staged_preview(slug)
    if preview is None:
        return None
    try:
        current_version = instance.read(slug).instance_version
    except Exception:
        logger.warning("dashboard: could not read %r's version to preview against", slug)
        current_version = 0
    return instance.Instance(
        slug=slug,
        instance_version=current_version,
        template_id=preview.template_id,
        template_version=preview.template_version,
        html=preview.html,
        manifest=preview.manifest,
        state=instance.STATE_LIVE,
        state_reason="staged for preview; no version written",
        updated_ms=preview.staged_ms,
    )


def _trusted_page(slug: str, record: Any) -> str | None:
    """The markup this gateway will EXECUTE for *record*, or ``None`` to refuse.

    Read from the catalog by the record's ``template_id``, so the bytes come from a
    directory in this repository and never from the crewmate's own writable
    ``instance.json``. Every other half of the render still comes from the record --
    the manifest decides which fields are read and which are the agent's -- and that
    is fine: a manifest can only name fields and fold paths, so the worst a tampered
    one does is draw a cell nothing fills. The HTML is the half that RUNS.

    ``None`` for all four refusals, because the tab draws one unavailable state and
    there is nothing a reader can do differently between them:

    * the record names no template, which is the empty state and not an attack;
    * the manifest's ``source`` is not renderable, kept as the cheap check so a
      record that never went through adopt is turned away before a registry scan;
    * the catalog does not serve that id -- a template removed from the product, or
      an id only ever written by something editing the file;
    * the catalog's own copy will not load, which is a broken checkout rather than
      this crewmate's problem.

    The refusal is LOGGED with the slug and the id, because an id the catalog does
    not serve is the signature of a tampered record and an operator is the only one
    who can go and look.
    """
    from kiro_crew.dashboard_templates import catalog
    from kiro_crew.dashboard_templates import instance as instance_store

    template_id = str(getattr(record, "template_id", "") or "")
    if not template_id:
        logger.warning("dashboard: %r's record names no template, so there is none to run", slug)
        return None
    source = str((record.manifest or {}).get("source") or "")
    if source not in instance_store.RENDERABLE_SOURCES:
        logger.warning("dashboard: refusing to render %r's %r template", slug, source)
        return None
    try:
        entry = catalog.load_one(template_id)
    except catalog.UnknownTemplate:
        logger.warning(
            "dashboard: %r's record names template %r, which this gateway does not "
            "serve; refusing to run the page stored beside it",
            slug,
            template_id,
        )
        return None
    except Exception:
        logger.warning("dashboard: template %r could not be loaded", template_id, exc_info=True)
        return None
    # The CATALOG's own claim about itself, which is the one that counts: the scan
    # already checked this directory's `source` against where the directory actually
    # is, so an entry reaching here is one the repository vouches for.
    if entry.manifest.source not in instance_store.RENDERABLE_SOURCES:
        logger.warning(
            "dashboard: catalog template %r declares %r and will not be run",
            template_id,
            entry.manifest.source,
        )
        return None
    # The id must still name the page the record was made from. An operator page whose
    # id a later release ships resolves to the shipped page, which is different markup
    # under a manifest it was never checked against.
    if entry.origin != source:
        logger.warning(
            "dashboard: %r's record is a %r page but the catalog serves %r as %r; "
            "refusing to run a different page under that id",
            slug,
            source,
            template_id,
            entry.origin,
        )
        return None
    return entry.html


def read_fields(
    slug: str,
    member: str,
    manifest: Any,
    *,
    slot: str | None = None,
    prime_wait: float | None = None,
) -> Any:
    """One read of *manifest*'s field values for this crewmate, as the page gets them.

    The page's own read, shared with ``dashboard_fields`` so the values an agent is
    shown are the values the reader sees. File IO; raises on a failed read.

    *prime_wait* caps the wait for cold slot folds (``DashboardFeed.wait_for_primes``);
    ``None`` is its default. The turn block passes ``0``, because a prompt build must
    not wait on a prime: a cold fold then reads as unseen, which the block skips.
    """
    from kiro_crew import agent_panel
    from kiro_crew.crew_log import projection
    from kiro_crew.crew_log.entry_types import PANEL_FOLD_NAME
    from kiro_crew.dashboard.handlers.agent_panel import owned_panel_record
    from kiro_crew.dashboard_feed import DashboardFeed

    # A root session's page is read by its own slot and has no publish of its own,
    # whatever crew name the caller carries.
    own_slot = slot is None
    if slot is None:
        slot = _dashboard_slot(member, slug)
        session = _write_session(slug, member)
    else:
        session = _newest_unit(slot)
    # The reader's OWN panel record, picked by the panel read route's own rule, so the
    # page shows what that route shows and a crew sharing this slot never fills it. A
    # root session publishes no panel, so it has no owner and reads an empty one.
    record = None
    if own_slot and member and PANEL_FOLD_NAME in manifest.folds:
        record = owned_panel_record(slot, slug, agent_panel.crew_key(member))
    feed = DashboardFeed(slot, session, panel_record=record)
    try:
        feed.subscribe(manifest)
        # One-shot: a cold slot fold arrives from its background prime, not as a
        # baseline, so wait for it before reading instead of reporting it missing.
        if prime_wait is None:
            feed.wait_for_primes()
        else:
            feed.wait_for_primes(prime_wait)
        agentic: Any = {}
        if any(spec.agentic for spec in manifest.fields.values()):
            agentic = projection.read_slot_projection(slot, "agentic").value
        return feed.read(manifest, agentic if isinstance(agentic, dict) else {})
    finally:
        feed.unsubscribe()


def _refuse_agent_caller(request: web.Request, operation: str, message: str) -> web.Response | None:
    """``human_only`` for an internal-secret caller -- an MCP server acting for an agent.

    The person-only routes ask this first. Audited like every other permission
    decision: an agent reaching for the person's own controls is exactly the event
    the security log exists to record.
    """
    if not (request.get("internal_auth") or request.headers.get("X-Internal-Secret") is not None):
        return None
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller=str(request.get("user") or "internal"),
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="internal_secret_caller",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for the %s refusal failed", operation, exc_info=True)
    return _bad("human_only", message, status=403)


async def api_member_dashboard_lock(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/lock?member=<name> -- the person's structure lock.

    Body ``{"locked": true|false}``. While set, the store refuses every new page version,
    so an agent's apply or rollback answers ``dashboard_locked`` until the person clears
    it here. Values and previews keep working.

    PERSON-ONLY, in two gates. An internal-secret caller is an MCP server acting for an
    agent, and the lock exists to stop exactly that caller, so it is refused before
    anything else is read. Then the owner gate, as for the read.
    """
    agent = _refuse_agent_caller(
        request,
        "members.dashboard_lock",
        "only the person can lock or unlock a dashboard, from the crewmate's Profile card",
    )
    if agent is not None:
        return agent
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner_denied = await _owner_only(request, "members.dashboard_lock")
    if owner_denied is not None:
        return owner_denied
    slug, member = resolved
    try:
        body = await request.json()
    except Exception:
        return _bad("invalid_json", "invalid JSON body")
    locked = body.get("locked") if isinstance(body, dict) else None
    if not isinstance(locked, bool):
        return _bad("validation_error", "locked must be true or false")
    try:
        record = await _run(lambda: instance.set_structure_lock(slug, locked))
    except Exception as exc:
        return _refusal(exc)
    state = request.app.get("state")
    if state is not None:
        state.broadcast_ws("dashboard_instance_changed", {"slug": slug})
    return web.json_response({"ok": True, "structure_locked": record.structure_locked})


async def _staged_ms_from_body(request: web.Request) -> int | web.Response:
    """The ``staged_ms`` a Keep or Discard names, or the 400 to answer instead."""
    try:
        body = await request.json()
    except Exception:
        return _bad("invalid_json", "invalid JSON body")
    staged_ms = body.get("staged_ms") if isinstance(body, dict) else None
    if not isinstance(staged_ms, int) or isinstance(staged_ms, bool) or staged_ms <= 0:
        return _bad("validation_error", "staged_ms must name the staged page that was shown")
    return staged_ms


async def api_member_dashboard_preview_apply(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/preview/apply?member=<name> -- the person's Keep.

    Body ``{"staged_ms": <int>}``: the staged page the preview read handed out, so what
    lands is what was drawn. ``dashboard_apply`` gets that guarantee by taking no
    arguments while the person is asked in chat; a button has no such pause, so it names
    the page and the store refuses ``preview_changed`` if another was staged since.

    The SAME writer as the agent's apply (:func:`instance.apply_preview`), so the lock,
    the parity re-check and the one-shot discard are the store's and not this route's.
    PERSON-ONLY, with the lock route's gates: an internal-secret caller is refused, then
    the owner gate.
    """
    agent = _refuse_agent_caller(
        request,
        "members.dashboard_preview_apply",
        "only the person can keep a previewed page from the Dashboard tab; an agent "
        "puts one live with dashboard_apply",
    )
    if agent is not None:
        return agent
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner_denied = await _owner_only(request, "members.dashboard_preview_apply")
    if owner_denied is not None:
        return owner_denied
    slug, member = resolved
    staged_ms = await _staged_ms_from_body(request)
    if isinstance(staged_ms, web.Response):
        return staged_ms
    session_id = await _run(lambda: _write_session(slug, member))
    try:
        record = await _run(
            lambda: instance.apply_preview(
                slug, session_id=session_id, expected_staged_ms=staged_ms
            )
        )
    except instance.NothingStaged as exc:
        return _bad("no_preview", str(exc), status=404)
    except instance.PreviewChanged as exc:
        return _bad("preview_changed", str(exc), status=409)
    except Exception as exc:
        return _refusal(exc)
    state = request.app.get("state")
    if state is not None:
        state.broadcast_ws("dashboard_instance_changed", {"slug": slug})
    return web.json_response(
        {"ok": True, "instance_version": record.instance_version, "template_id": record.template_id}
    )


async def api_member_dashboard_preview_discard(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/preview/discard?member=<name> -- the person's Discard.

    Body ``{"staged_ms": <int>}``, like Keep: only the page that was shown is dropped,
    and one staged since answers ``preview_changed`` rather than vanishing unseen.
    Discarding a page that already went answers ok, because the person's ask -- "not
    that one" -- is met either way. The live page is untouched. Same gates as Keep.
    """
    agent = _refuse_agent_caller(
        request,
        "members.dashboard_preview_discard",
        "only the person can discard a previewed page from the Dashboard tab",
    )
    if agent is not None:
        return agent
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner_denied = await _owner_only(request, "members.dashboard_preview_discard")
    if owner_denied is not None:
        return owner_denied
    slug, _member = resolved
    staged_ms = await _staged_ms_from_body(request)
    if isinstance(staged_ms, web.Response):
        return staged_ms

    def _discard() -> bool:
        instance.discard_shown_preview(slug, expected_staged_ms=staged_ms)
        # The store call swallows a failed delete, so ok is earned by reading back:
        # nothing an apply would install may remain.
        return instance.staged_preview(slug) is None

    try:
        gone = await _run(_discard)
    except instance.PreviewChanged as exc:
        return _bad("preview_changed", str(exc), status=409)
    except Exception as exc:
        return _refusal(exc)
    if not gone:
        logger.warning("dashboard: %r's staged page could not be removed", slug)
        return _bad(
            "dashboard_failed", "the staged page could not be removed; it is still staged", 500
        )
    state = request.app.get("state")
    if state is not None:
        state.broadcast_ws("dashboard_instance_changed", {"slug": slug})
    return web.json_response({"ok": True})


def register_member_dashboard_routes(app: web.Application) -> None:
    """Register the dynamic dashboard's read routes, its layout lock, the gap refetch
    and the person's Keep / Discard of a staged page.

    Six routes: a crewmate's read, a root session's read, the person's layout lock,
    the member read's gap refetch, and the preview's apply and discard. The lock,
    refetch and preview paths are strict suffixes of the member read path, so
    aiohttp's own resolver separates them by METHOD; the
    session read's path is not a prefix of another route's pattern, so there is no
    ordering question here either.
    ``server.py`` duplicates the paths through its deferred binder rather than calling this function, because calling
    it would import this module at boot and the boot-path rule forbids that for an
    optional subsystem; ``test_member_dashboard_routes`` pins the two spellings against
    each other.
    """
    app.router.add_get("/api/members/{slug}/dashboard", api_member_dashboard)
    app.router.add_get("/api/chat/slots/{slot}/dashboard", api_session_dashboard)
    app.router.add_post("/api/members/{slug}/dashboard/lock", api_member_dashboard_lock)
    app.router.add_post("/api/members/{slug}/dashboard/refetch", api_member_dashboard_refetch)
    app.router.add_post(
        "/api/members/{slug}/dashboard/preview/apply", api_member_dashboard_preview_apply
    )
    app.router.add_post(
        "/api/members/{slug}/dashboard/preview/discard", api_member_dashboard_preview_discard
    )


#: One armed page per (crewmate, package), on the app so a reload replaces rather than
#: stacks. Keyed by BOTH because one crewmate can be shown through two packages at once
#: and each needs its own subscriptions and its own version counter.
_LIVE_PAGES = "member_dashboard_live_pages"

#: How many pages may be armed at once. Each one holds a bus subscription per fold its
#: model reads, and nothing disarms a page when its tab closes -- the browser cannot
#: say so -- so an operator who opens every crewmate's dashboard over a long-running
#: gateway leaves every one of those subscriptions behind for its life.
#:
#: The least recently armed page is disarmed when the cap is passed. That costs its tab
#: its pushes until its next read arms it again, and its next read is a refetch of the
#: whole value set -- so the failure mode is a page that stops moving, never one that
#: presents a stale value as the current one.
_MAX_LIVE_PAGES: Final[int] = 32


def _live_pages(request: web.Request) -> dict[tuple[str, str], Any]:
    pages: dict[tuple[str, str], Any] | None = request.app.get(_LIVE_PAGES)
    if pages is None:
        pages = {}
        request.app[_LIVE_PAGES] = pages
    return pages


def _push_frame(state: Any, slug: str, package_slug: str, frame: dict[str, Any]) -> None:
    """Send one field frame to the owner's sockets. Called on the FOLD WORKER thread.

    ``broadcast_ws_owners`` -- the same hub and the same owner-socket set every other
    fold-driven frame goes out on, so the owner gate, the backpressure and the
    reconnect are not re-implemented for a second channel.
    """
    try:
        # THE PACKAGE IS NAMED on every frame. The socket is per connection, so a
        # frame reaches every tab the owner has open -- and one crewmate can be shown
        # through two different packages at once (two tabs, or a template page beside a
        # package page). Without the id each tab applies the other's patches, and its
        # refetch answers with the other package's read.
        state.broadcast_ws_owners(
            member_dashboard_fields.FIELD_FRAME,
            {"slug": slug, "dashboard": package_slug, "read": frame},
        )
    except Exception:  # pragma: no cover - a failed send must not cost the folder
        logger.debug("dashboard: a field frame for %s could not be broadcast", slug)


def governance_session_key(slot: str) -> str:
    """The session key governance resolves a member dashboard's profile under.

    A profile is BOUND to a surface and the surface is read off this key's prefix by
    ``sel._infer_source``, the one classifier governance delegates to. A crewmate's
    slot is a bare ``member-*`` string, which that classifier sends to its Slack
    fallback -- so a key built from the slot alone skips every dashboard-bound profile.

    Named here because the product has no shared builder for it: the ``dashboard:``
    prefix is spelled inline at about a dozen call sites, and a surface whose key is
    assembled by each caller is a surface one of them will eventually spell wrong.
    This is the dashboard handler's single spelling, and
    ``test_dashboard_page_scripts_ceiling`` pins it by asking the CLASSIFIER what it
    resolves to rather than by comparing it to a literal.
    """
    return f"dashboard:{slot}"


@dataclasses.dataclass(frozen=True)
class _EgressDecision:
    """One script-grant decision, with the layer and reason that produced it.

    The reason exists because this decision is AUDITED, and an audit row saying only
    "withheld" cannot be acted on: an operator reconstructing why a crewmate's page went
    static needs to know whether the ceiling restricts egress, a profile narrows it, or
    neither could be read at all. The three have different remedies and only one of them
    is a misconfiguration.
    """

    granted: bool
    #: Which layer decided: ``policy``, ``profile``, or ``resolution`` when a read failed.
    layer: str
    #: Why, as a token stable enough to query a log by.
    reason: str


def owner_agent_egress_unrestricted(owner_agent: str = "", session_key: str = "") -> bool:
    """Whether POLICY AND PROFILE both put NO network restriction on the owner agent.

    The BOOLEAN half of :func:`_egress_decision`, which is the whole answer for every
    caller that only mounts a page. The audited call site reads the decision itself,
    because an audit row needs the layer and the reason as well as the verdict.
    """
    return _egress_decision(owner_agent, session_key).granted


def _egress_decision(owner_agent: str = "", session_key: str = "") -> _EgressDecision:
    """Whether POLICY AND PROFILE both put NO network restriction on the owner agent.

    This decides whether an agent-written page may run script, and it enforces
    the RFC amendment's fourth reason. That reason licenses a running page by
    saying the owner agent already reaches the network, so a page navigating its
    own frame opens no path the agent lacked. The reason therefore holds only
    while the agent's egress is UNRESTRICTED.

    A host allowlist is a restriction, and it is the case worth spelling out: the
    agent may fetch ``api.github.com`` and nothing else, but a frame that runs
    script can navigate ITSELF anywhere, so the page would reach hosts the
    ceiling never permitted. "Some egress" is not the premise; "no restriction"
    is. Deny-all and allowlist both withhold ``allow-scripts``.

    Answers True only for an agent nothing restricts:

    * no ceiling, or ``network.egress`` absent from its controls -- True, the
      standalone default, which is every install that configures no policy;
    * ``deny`` mode with an EMPTY deny list -- True, a ruleset that denies
      nothing;
    * ``allow`` mode, with any contents -- False. An allow-mode ruleset is a
      closed set, so it restricts even when it names hosts;
    * ``deny`` mode naming anything -- False;
    * any other archetype governing the scope -- False. The scope is governed by
      something this handler cannot read, and guessing "unrestricted" would hand
      out script on a ceiling nobody here understood.

    FAILS CLOSED. An exception reading the ceiling answers False, so a page runs
    only when the ceiling was read and found to restrict nothing. The cost is
    that a broken governance read makes dashboards static rather than scripted,
    which is the direction that cannot open an egress path.
    """
    from kiro_crew.platform.governance import MODE_DENY, ScopedRuleset

    try:
        from kiro_crew.platform.governance import last_composed_ceiling, policy_level_control

        ceiling = last_composed_ceiling()
        # The policy level as ``resolve`` enforces it: in an enterprise build an
        # unnamed ``network.egress`` is the built-in baseline (off), not absent.
        control = policy_level_control(ceiling, "network.egress")
        # NO CEILING, OR NONE GOVERNING EGRESS, IS NOT AN ANSWER -- it only means the
        # outer bound restricts nothing, and the profile below still decides. Returning
        # True here was the hole: the standalone default is every install that
        # configures no policy, and a profile can restrict egress in all of them.
        if control is not None:
            if not isinstance(control, ScopedRuleset):
                return _EgressDecision(False, "policy", "ceiling_egress_control_unreadable")
            # Only one shape leaves the agent unrestricted: deny-mode denying nothing.
            if not (control.mode == MODE_DENY and not control.deny):
                return _EgressDecision(False, "policy", "ceiling_restricts_egress")
    except Exception:
        logger.debug("could not read the egress ceiling; withholding the page's scripts")
        return _EgressDecision(False, "resolution", "ceiling_unreadable")
    # AND THE ACTIVE PROFILE, because the ceiling is only the outer bound.
    #
    # A profile narrows what the ceiling permits, and the two come apart in the
    # ordinary case: an open policy with a profile that restricts `network.egress` is
    # an agent whose real egress is restricted, and reading the ceiling alone calls
    # that agent unrestricted and hands its page script. The page can then navigate its
    # own frame, carrying fold values, to a host the profile forbids.
    #
    # THE SAME ARCHETYPE TEST AS THE CEILING, not a permitted-host probe. Asking
    # whether one host is permitted answers only about that host: a profile denying
    # `github.com` permits every other name, so any probe it happens to allow reads as
    # "unrestricted" while the restriction is still there. The question is whether the
    # scope is RESTRICTED AT ALL, and only one ruleset shape is not.
    #
    # The profile is resolved for the SURFACE this page belongs to -- a profile is
    # bound to one, and the resolver infers it from the session key. Without the key
    # the scope resolves to "unknown", which matches no surface profile, so the
    # intersection falls back to policy alone.
    try:
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        profile = resolve_active_scope(session_key, agent=owner_agent)
    except Exception:
        logger.debug("could not resolve the active profile; withholding the page's scripts")
        return _EgressDecision(False, "resolution", "profile_unresolvable")
    if profile is None:
        # POLICY-ONLY, which the ceiling above has already answered. No profile is
        # bound to this surface, so there is no second narrowing to apply.
        return _EgressDecision(True, "policy", "no_restriction")
    control = (getattr(profile, "controls", None) or {}).get("network.egress")
    if control is None:
        # This profile governs other scopes and not this one, so it narrows nothing here.
        return _EgressDecision(True, "policy", "no_restriction")
    if not isinstance(control, ScopedRuleset):
        return _EgressDecision(False, "profile", "profile_egress_control_unreadable")
    if control.mode == MODE_DENY and not control.deny:
        return _EgressDecision(True, "profile", "no_restriction")
    return _EgressDecision(False, "profile", "profile_restricts_egress")


#: How many (crewmate, package) pairs the grant audit remembers its last decision for.
#: Bounded for the reason the live-page registry is: the key is a pair an operator can
#: create, so an unbounded map is a slow leak on a long-lived gateway. Evicting the
#: oldest costs at most one repeated audit row, never a missed change.
_GRANT_AUDIT_REMEMBERED: Final[int] = 256

#: ``(crewmate, package) -> (version, granted, layer, reason)`` for the last decision
#: AUDITED, which is what makes the audit a record of decisions rather than of reads.
_grant_audited: dict[tuple[str, str], tuple[int, bool, str, str]] = {}


def _audit_page_script_grant(
    *,
    member: str,
    package_slug: str,
    version: int,
    session_key: str,
    decision: _EgressDecision,
) -> None:
    """Record ONE script-grant decision on the security event log; never raise.

    THIS IS AN AUTHORIZATION DECISION, which is why it is audited at all: it decides
    whether a page a crewmate wrote may run script against that crewmate's own fold
    values, and the repository's backend security controls require a decision of that
    kind to leave a record. Both directions, because "which of my crewmates' pages
    ever ran script" is the question an operator reconstructs afterwards and a
    withheld-only log cannot answer it.

    DEDUPED ON ``(crewmate, package, version, decision)``. The tab polls on a timer and
    refetches on two WS frames, so one open dashboard re-decides this every few minutes
    with the same answer; emitting per read would turn an authorization log into a
    page-view log and push the rows that matter out of any practical retention window.
    A row is written when any part of that tuple changes -- a new applied version, or a
    profile edit that flips the verdict -- which is exactly when there is something new
    to record.

    BEST EFFORT. ``log_governance_decision`` without ``critical`` enqueues to the
    background writer, which swallows persistence failures; everything here is inside
    one ``except`` besides. A page's grant is decided by policy and profile and must
    not change because an audit write failed, and the caller has already computed it:
    this function cannot reach the value it records. Redaction is the writer's own --
    the shared helper applies ``redact_via_context`` to ``operation``, ``item`` and
    ``reason`` before it logs, the same pass every other governance row gets.
    """
    try:
        key = (member, package_slug)
        current = (version, decision.granted, decision.layer, decision.reason)
        if _grant_audited.get(key) == current:
            return
        if key not in _grant_audited and len(_grant_audited) >= _GRANT_AUDIT_REMEMBERED:
            _grant_audited.pop(next(iter(_grant_audited)), None)
        _grant_audited[key] = current

        from kiro_crew import sel as _sel_mod

        _sel_mod.sel().log_governance_decision(
            session_key=session_key,
            tool_name=f"dashboard_package:{package_slug}",
            scope="network.egress",
            item=f"{member}/{package_slug}@{version}",
            # The vocabulary `log_governance_decision` pins in its own docstring, and
            # the one the canonical governance helper emits. A local spelling would
            # make these rows the ones a query for allowed decisions misses.
            outcome="allowed" if decision.granted else "denied",
            rule="dashboard_page_scripts",
            layer=decision.layer,
            reason=decision.reason,
        )
    except Exception:
        logger.debug(
            "dashboard page script-grant audit failed for %r/%r",
            member,
            package_slug,
            exc_info=True,
        )


#: The lifecycle event types whose actor is decided by the AUTH PATH, out of
#: ``artifact_store.records.ALLOWED_EVENT_TYPES``. Both come from
#: :meth:`~kiro_crew.artifacts.ArtifactStore.update`, where the actor is
#: ``"agent" if is_mcp else "user"`` -- read off the request's headers, reachable from
#: no field a caller sends.
#:
#: THE OTHER THREE ARE EXCLUDED, each for its own reason, and the reasons are not
#: interchangeable:
#:
#: * ``referenced`` and ``comment`` name a version they did not create. The chat panel
#:   posts the first every time a person opens an artifact, the second follows a comment
#:   action, and both are stamped with the record's CURRENT version -- so counting them
#:   reads a glance as an approval of whatever the agent wrote last.
#: * ``created`` takes its actor from ``create``'s ``source``, which the request body
#:   may supply -- and that turns out to be no way in at all. ``source`` is validated
#:   against ``artifact_store.rules.ALLOWED_SOURCES``, which has no ``user`` member, so
#:   a ``created`` event's actor is one of those labels or ``agent`` and never the
#:   person. Listing it would add a rule that matches nothing while telling the next
#:   reader that creating a page approves it.
#:
#: AN ALLOWLIST, never a denylist of annotations: a lifecycle type the store gains
#: later is excluded until somebody decides otherwise, which is the safe default and the
#: opposite of how ``referenced`` came to count. ``TestTheApprovingTypeSetIsAnAllowlist``
#: pins all three premises -- that every name here is a type the store emits, that every
#: type it emits is either here or excluded for a stated reason, and that
#: ``ALLOWED_SOURCES`` still has no ``user`` -- so each one fails where its reason is
#: written rather than silently widening this set.
_VERSION_CUTTING_EVENTS: Final[frozenset[str]] = frozenset({"edited", "reverted"})


def _record_identity(art: Any) -> tuple[str, str]:
    """What makes a record THIS record, for comparing two reads of one slug.

    A slug is reusable and a version number restarts at 1 for a recreated record, so
    neither tells two records apart. ``created_at`` is minted once when the record is
    created and never rewritten, and the slug is carried with it so a comparison cannot
    accidentally pass between two records that share a timestamp.

    An absent ``created_at`` answers the empty string, which compares EQUAL to another
    absent one and UNEQUAL to any real timestamp -- so a store that does not stamp
    records is not turned into a permanent refusal, while a record that has one is
    never matched against a record that does not.
    """
    return (
        str(getattr(art, "slug", "") or ""),
        str(getattr(art, "created_at", "") or ""),
    )


def applied_package_version(loaded: Any) -> int:
    """The newest version of this package a PERSON cut, or ``0`` when there is none.

    THE APPLIED MARKER, and it is not a new field. A version exists only because
    :meth:`~kiro_crew.artifacts.ArtifactStore.update` was called with ``snapshot=True``,
    and every such cut appends a lifecycle event carrying ``by=<actor>``. That actor is
    derived from HOW the request authenticated -- ``actor = "agent" if is_mcp else
    "user"``, where ``is_mcp`` is the presence of the ``X-Internal-Secret`` header the
    MCP loopback helpers attach -- so it is not a value a caller spells for itself.

    BOTH HALVES ARE REQUIRED, and each answers a different way in.

    ``by == "user"`` is the half an agent cannot forge: the actor comes from the auth
    path, so neither a body field nor an MCP tool call produces it. On its own it is not
    enough, because ``by`` is stamped on EVERY lifecycle event and two of them are things
    a person does while merely looking -- ``referenced``, which the chat panel posts each
    time a person opens an artifact in a slot, and ``comment``. Both are stamped with the
    record's CURRENT version, so counting them reads ordinary reading as approval and
    serves the agent's latest draft.

    So the type must be one whose actor the auth path decided -- see
    :data:`_VERSION_CUTTING_EVENTS`, which is ``edited`` and ``reverted``. An annotation
    names a version it did not create, and a ``created`` event's actor is one of the
    store's ``source`` labels, none of which is a person.

    The type alone would be the wrong half to keep: ``event_type`` is settable from the
    request body, so an agent can spell its own save ``edited``. It still arrives with
    ``by="agent"``.

    WHY THIS IS THE QUESTION. Serving the record's CURRENT content serves whatever was
    written last, and an agent writing over MCP cuts a version on every save with no
    person involved -- so "this package has versions" says nothing about whether anyone
    approved one. The newest user-actor version is the newest page a person's own action
    put there.

    WHAT THIS DOES NOT CLAIM. The actor is unforgeable from the REQUEST -- neither a
    body field nor an MCP tool call can produce ``by="user"``. It is not a proof of
    approval against a writer that already runs as this OS user: such a writer can read
    the dashboard's own credential off disk and call the API as the browser, exactly as
    it could rewrite the stored bytes or this module. That residual is the one named at
    the stored-bytes read below, bounded the same way -- the page runs with no network on
    an opaque origin and is sent only the fields its model declares. What this closes is
    the write path every agent and app actually has.

    A version whose event has been evicted (the log is FIFO-capped) is not counted, and
    a version whose bytes were pruned fails the read that follows. Both answer "nothing
    applied", which is the fail-closed direction: an unprovable approval is not one.
    """
    best = 0
    for event in getattr(loaded, "events", None) or []:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("by") or "") != "user":
            continue
        if str(event.get("type") or "") not in _VERSION_CUTTING_EVENTS:
            continue
        version = event.get("version")
        # `bool` is an `int`, and `True` would compare as version 1.
        if isinstance(version, bool) or not isinstance(version, int):
            continue
        if version > best:
            best = version
    return best


def _nothing_to_serve(slug: str, package_slug: str) -> web.Response:
    """A BOUND PACKAGE WITH NOTHING TO SERVE RENDERS NOTHING AT ALL.

    ``rfc-crewmate-dynamic-dashboard.md``'s decision 3 puts it in those words: naming a
    package and publishing one are different acts. The binding
    answers WHICH package a reader would see; whether there is yet anything to see is a
    separate question, and when the answer is no, the frame draws its empty state.

    Three other answers were reachable and all three are wrong. A CODE is wrong because
    nobody asked a question -- this slug came from the crewmate's own binding, and the
    tab would draw the refusal as a failure the reader could clear. The DEFAULT
    TEMPLATE is worse: the no-package path serves the shipped default so that every
    crewmate has a page, and reaching it here draws a page that is not this crewmate's
    under this crewmate's name, labelled ``instance_version`` 0 like an ordinary fresh
    crewmate. And the LAST PAGE THAT LOADED would say the record still has one.

    ``instance_version`` 0 with ``state: empty`` is the shape the tab already reads for
    a crewmate that adopted nothing, so this needs no new client branch. The reason is
    carried in ``state_reason``, which is where this body says things a reader of one
    response could not otherwise work out.
    """
    logger.info(
        "dashboard: %r is bound to package %r, which has no version to serve", slug, package_slug
    )
    return web.json_response(
        {
            "instance_version": 0,
            # NOT the package's id. A template id names a page the body carries, and
            # this body carries none; filling it in would make a reader look for one.
            "template": {"id": "", "version": 0},
            "dashboard": package_slug,
            "html": "",
            "manifest": {},
            "state": instance.STATE_EMPTY,
            "state_reason": "the bound dashboard package has no applied version",
            "fields": [],
            "locale": "",
        }
    )


async def _serve_package(
    request: web.Request,
    slug: str,
    member: str,
    package_slug: str,
    locale: str,
    *,
    asked_for: bool = True,
    pinned: instance.PinnedPackage | None = None,
    structure_locked: bool = False,
) -> web.Response:
    """Serve the ``kind="dashboard"`` artifact *package_slug* as this crewmate's page.

    *pinned* is the page the crewmate's instance record put in force, read from the
    gateway's snapshot (``instance.pinned_package``); ``None`` keeps the older rule for
    a record written before packages could be pinned -- the newest version a person cut.

    *structure_locked* is the person's layout lock from the same record, carried into
    the body as a template page carries it, so the Profile switch and the Dashboard's
    lock badge read the lock that holds.

    *asked_for* is whether a CALLER named this slug. False means it came from the
    crewmate's own binding, and it changes one answer: a package with nothing to serve
    renders the frame's empty state rather than a code, because nobody asked a question
    for the code to answer. See :func:`_nothing_to_serve`.

    THE BINDING IS CHECKED, not looked up. The tab names the package it is opening and
    this asks whether that package is THIS crewmate's -- which is the question that
    matters, and the one a scan of the artifact library would answer less directly. A
    package bound elsewhere is refused rather than rendered, because a page carries the
    binding's own fold values and rendering it here would show one crewmate's work
    under another's name.

    Arming the live page is part of serving it: the first paint and every later patch
    come from one :class:`~kiro_crew.dashboard.handlers.member_dashboard_fields.LivePage`,
    so the version the body carries is the one the first patch is checked against.
    """
    from kiro_crew.artifact_store.dashboard_package import (
        DASHBOARD_KIND,
        binding_refusal,
        parse_package,
        subscribed_fields,
    )
    from kiro_crew.artifacts import ArtifactError, get_default_store

    if pinned is not None:
        # THE GATEWAY'S OWN SNAPSHOT of the bytes the owner saved, pinned by the
        # instance record. No artifact read: the artifact is where the owner
        # authors, and nothing done to it later moves the page in force.
        version, content = pinned.version, pinned.content
    else:
        try:
            loaded = await _run(lambda: get_default_store().get(package_slug))
        except ArtifactError:
            if not asked_for:
                return _nothing_to_serve(slug, package_slug)
            return _bad("dashboard_not_found", "no dashboard package at that slug", status=404)
        except OSError:
            logger.warning("dashboard: package %r could not be read", package_slug)
            return _bad("dashboard_failed", "the dashboard package could not be read", status=500)
        # THE RECORD'S OWN KIND, checked before anything is parsed. Content alone is not
        # enough: a LINKED artifact's bytes come from a file on disk, so its content can be
        # changed without passing the artifact write gate at all -- and parsing whatever is
        # there would arm a page nobody authorized. Only a stored `kind="dashboard"`
        # artifact that owns its own bytes reaches the parse below.
        if str(getattr(loaded, "kind", "")) != DASHBOARD_KIND:
            return _bad(
                "dashboard_not_a_package",
                "that artifact is not a dashboard package",
                status=409,
            )
        if str(getattr(loaded, "source_path", "") or "") and not bool(
            getattr(loaded, "source_copy_only", False)
        ):
            # A live pointer: the artifact serves a file's current bytes, which nothing in
            # the artifact API gates. Refused rather than rendered, because the page RUNS.
            return _bad(
                "dashboard_live_source",
                "a dashboard package cannot be served from a linked file: its bytes would "
                "bypass the write gate that decides whose page this is",
                status=409,
            )
        # THE RESIDUAL, named where it bites. These are the record's OWN bytes, and the
        # store rotates a random `content_token` per write rather than hashing them, so a
        # write made straight into the stored file -- around the artifact API and so around
        # the owner gate -- is not detectable at this line. Nor can a check here close it: a
        # writer reaching that file is the same OS user as this process, so any digest or key
        # this handler compared against sits in a file that writer can rewrite too, and it
        # could edit this module instead. What bounds the damage is the frame rather than the
        # gate -- the page runs with no network on an opaque origin and is sent only the
        # fields the model declares, so tampered markup can mislead the owner reading it and
        # reach nothing beyond that tab. The gate's own job is the write that arrives over
        # the API, which is the one every agent and app actually has.
        # THE VERSION IN FORCE, and the bytes come from IT rather than from the record's
        # current content: `get(slug)` with no version answers whatever was written last.
        # The owner's `dashboard_save` pins the version it wrote in the instance record;
        # an older record falls back to the newest version a person cut (see
        # `applied_package_version` for why that marker is the event's actor).
        version = applied_package_version(loaded)
        if version <= 0:
            # NOTHING APPLIED, so nothing renders -- for a caller that named the slug too.
            # This is not "no such package": the package is here and its owner has written
            # pages into it. There is just no page a person has said yes to, and the one
            # honest thing to draw for that is nothing.
            return _nothing_to_serve(slug, package_slug)
        try:
            applied = await _run(lambda: get_default_store().get(package_slug, version=version))
            # THE SAME RECORD, not merely the same slug and number.
            #
            # The approval was read off `loaded` and the bytes are fetched by slug and
            # version in a second call, so a delete-and-recreate in between answers with a
            # DIFFERENT artifact's version N -- and that one's v2 is whatever its agent
            # wrote, served under an approval a person gave to the record that is gone.
            # A slug is reusable and a version number restarts at 1, so neither identifies
            # the record; `created_at` is minted once per record and does.
            if _record_identity(applied) != _record_identity(loaded):
                logger.warning(
                    "dashboard: package %r was replaced between its approval read and its "
                    "versioned read; serving nothing",
                    package_slug,
                )
                return _nothing_to_serve(slug, package_slug)
        except (ArtifactError, OSError):
            # The approval is recorded and its bytes are gone -- a pruned version, or a
            # history this process cannot read. Fail closed: an approval whose page cannot
            # be produced is not a page to serve.
            logger.warning(
                "dashboard: package %r has an applied version %d whose bytes could not be read",
                package_slug,
                version,
            )
            return _nothing_to_serve(slug, package_slug)
        content = applied.content or ""
    try:
        package = await _run(lambda: parse_package(content))
    except Exception as exc:
        # THE VALIDATOR QUOTES THE VALUE IT REFUSED, and this sentence is an HTTP body.
        #
        # A refusal names the offending key and its value so the author can fix the
        # page -- and a value in a stored package can be credential-shaped, so the
        # quoted text goes through the redactor before it leaves the process. The same
        # rule the declared-name check, the `view.html` validator and `binding_refusal`
        # follow: every name this surface says out loud is redacted first.
        from kiro_crew.platform.context import redact_via_context

        return _bad(
            "dashboard_refused",
            f"the dashboard package does not load: {redact_via_context(str(exc))}",
            status=409,
        )
    # OFF THE LOOP: `binding_refusal` resolves the caller's slug through the roster,
    # which loads the config from disk.
    refused = await _run(lambda: binding_refusal(package.get("bound_to"), crew=member))
    if refused:
        return _bad("dashboard_not_bound", refused, status=403)
    state = request.app["state"]
    pages = _live_pages(request)
    # KEYED BY (crewmate, package), because one crewmate can have two packages open and
    # each needs its own subscriptions and its own version counter.
    key = (slug, package_slug)
    previous = pages.pop(key, None)
    if previous is not None:
        # DISARMED FIRST. A reload or a layout change would otherwise leave the old
        # page's subscriptions on the bus, pushing frames computed under a layout
        # nothing is rendering.
        await _run(previous.disarm)
    # BOTH RESOLVERS ARE FILE IO and neither may run here: `_dashboard_slot` loads the
    # config and `_write_session` walks the slot's unit succession, reading each unit's
    # opening entry. On the event loop that blocks every other session's chat and the
    # heartbeat for as long as the walk takes.
    slot, session_unit = await _run(
        lambda: (_dashboard_slot(member, slug), _write_session(slug, member))
    )
    live_unit = await _run(lambda: _live_or_newest_unit(state, slot))
    session_unit = live_unit or session_unit
    page = member_dashboard_fields.LivePage(
        slug,
        slot,
        package,
        lambda frame: _push_frame(state, slug, package_slug, frame),
        session_unit=session_unit,
        layout_version=version,
        # The reader's own UI language, so a patch describes the same read the first
        # paint did. Without it every frame carries the default and a non-English
        # reader is handed an English page on every push.
        locale=locale,
        # THE SAME masking and redaction pass the template path applies, handed in
        # rather than re-implemented: this is the one place a package's values are
        # produced, so a value that did not cross it cannot reach a browser.
        redact=_page_safe,
    )
    read = await _run(page.arm)
    # RE-CHECKED AFTER ARMING, because arming awaits: two GETs for one crewmate (two
    # tabs, or both tabs refetching after a reconnect) each pop before either assigns,
    # so without this the first armed page is replaced without ever being disarmed --
    # and it stays subscribed to the bus for the life of the gateway, pushing frames
    # from its own version counter that the open page can only read as gaps.
    raced = pages.get(key)
    if raced is not None and raced is not page:
        await _run(raced.disarm)
    pages[key] = page
    # THE CAP, applied after the assignment so the page this request serves is never
    # the one evicted. A dict iterates in insertion order and every serve pops its key
    # before re-inserting it, so the first key is the least recently ARMED one -- the
    # order the eviction wants, with no second structure to keep in step with it.
    while len(pages) > _MAX_LIVE_PAGES:
        oldest = next(iter(pages))
        if oldest == key:
            break
        stale = pages.pop(oldest)
        await _run(stale.disarm)
    # SCRUBBED BEFORE COMPOSING, with the same pass the `html` field below gets. The
    # composed document is the half that RUNS, so scrubbing only the raw field left a
    # credential pasted into the page's own markup in the response one key away.
    safe_page = dict(package)
    safe_view = dict(package.get("view") or {})
    # OFF THE EVENT LOOP: the scrub runs every redactor over the whole page, which takes
    # a few hundred milliseconds for a page near the frame's ceiling.
    safe_view["html"] = await _run(lambda: _template_text_safe(safe_view.get("html") or ""))
    safe_page["view"] = safe_view
    try:
        # DECIDED BEFORE THE PAGE IS COMPOSED, because it changes what is composed.
        # OFF THE EVENT LOOP. Resolving the profile makes the store check its
        # directory's freshness -- an enumerate plus a stat per file, and a reload of
        # any that moved -- so running it here pauses every other session's chat and
        # the heartbeat for as long as that filesystem work takes. The two resolvers
        # above are offloaded for the same reason.
        # THE CANONICAL DASHBOARD KEY, not the bare slot. The surface classifier reads
        # the key's PREFIX: `dashboard:<slot>` is this surface, and a bare `member-*`
        # falls through to the Slack fallback -- so a dashboard-bound egress profile is
        # skipped and the page runs script the operator restricted.
        grant_key = governance_session_key(slot)
        decision = await _run(lambda: _egress_decision(member, grant_key))
        scripts = decision.granted
        # AUDITED HERE RATHER THAN INSIDE THE DECISION, because the row names the page
        # the decision was made for and only this frame knows the package and version.
        # After `scripts` is bound, so the grant is already settled when the audit runs.
        _audit_page_script_grant(
            member=member,
            package_slug=package_slug,
            version=version,
            session_key=grant_key,
            decision=decision,
        )
        # Off the loop for the scrub's reason: framing checks and copies the whole page.
        html = await _run(
            lambda: member_dashboard_fields.render_package_page(safe_page, read, scripts=scripts)
        )
    except Exception as exc:
        await _run(page.disarm)
        pages.pop(key, None)
        # REDACTED for the reason the parse refusal above gives: the frame's own
        # validator quotes the markup or the binding name it refused, and a stored
        # package's values are not re-validated by whoever wrote them.
        from kiro_crew.platform.context import redact_via_context

        return _bad(
            "dashboard_refused",
            f"the page cannot be framed: {redact_via_context(str(exc))}",
            status=409,
        )
    return web.json_response(
        {
            "instance_version": version,
            "template": {"id": package_slug, "version": version},
            # NAMED for the tab: it carries this back on a refetch and drops any frame
            # that belongs to a different package.
            "dashboard": package_slug,
            # The package's own page, kept beside the composed one exactly as the
            # template path keeps its stored copy, and scrubbed by the same pass.
            "html": safe_view["html"],
            "manifest": _manifest_text_safe(package.get("model") or {}),
            "state": instance.STATE_LIVE,
            # The person's layout lock, as a template page's body carries it.
            "structure_locked": structure_locked,
            "rendered_html": html,
            # THE ALLOWLIST, named in the body so a reader of one response can see
            # exactly which fields this page can ever be sent.
            "fields": sorted(subscribed_fields(package)),
            # Whether this page may run script, decided by POLICY AND the owner's
            # active PROFILE rather than by the page. False withholds
            # `allow-scripts` in the host's frame; see
            # owner_agent_egress_unrestricted for why that follows from the RFC
            # amendment's fourth reason, and why the profile has to be read too.
            "scripts": scripts,
            "locale": read.get("locale", locale) if isinstance(read, dict) else locale,
        }
    )


async def api_member_dashboard_refetch(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/refetch -- the whole read, after a version gap.

    The host's half of the frame's own gap rule: a page that cannot place a patch in
    order asks for a full read rather than applying one out of order, and this answers
    it. The tree-keyed folds are re-read here too, which is the other half of why a
    refetch exists -- nothing can push a tree, so this is the only moment its value
    moves.

    Owner-gated like the read it belongs to. A page nothing has armed answers 409: the
    remedy is to re-read the dashboard, which arms one.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner_denied = await _owner_only(request, "members.dashboard")
    if owner_denied is not None:
        return owner_denied
    slug, _member = resolved
    # The PACKAGE is named, for the same reason a frame names it: a crewmate can have
    # two open, and answering the wrong one's read would replace a page's values with
    # another page's wholesale.
    package_slug = request.query.get("dashboard", "")
    if not package_slug:
        return _bad("missing_dashboard", "name the dashboard package to refetch")
    page = _live_pages(request).get((slug, package_slug))
    if page is None:
        return _bad("no_live_page", "no dashboard page is armed for this package", status=409)
    return web.json_response({"dashboard": package_slug, "read": await _run(page.refetch)})
