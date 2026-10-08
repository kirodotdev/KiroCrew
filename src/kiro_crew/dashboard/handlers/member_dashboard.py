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
import logging
from typing import Any, Callable, Mapping

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members import (
    _deny_app_caller,
    _member_names_for_slug,
    _member_thread_slot,
)
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.members import MemberSlugError
from kiro_crew.platform.context import redact_via_context

logger = logging.getLogger(__name__)

__all__ = ["register_member_dashboard_routes"]


def _bad(code: str, message: str, status: int = 400) -> web.Response:
    """One refusal shape for the whole module: a code a client branches on, and a sentence.

    Both halves, always. A client cannot branch on prose, and a person cannot act on a
    code -- a surface given only one of the two either hard-codes English or shows the
    user ``instance_refused``.
    """
    return web.json_response({"error": message, "code": code}, status=status)


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
    try:
        # Imported here, not at module scope, for the reason every other store call in
        # this handler is: the crew log is an optional subsystem and the boot path must
        # not load it.
        from kiro_crew.crew_log.projection import units_in_succession

        slot = _dashboard_slot(member, slug)
        units = units_in_succession(slot)
        return units[-1] if units else ""
    except Exception:
        logger.debug("dashboard: no DM session for %r", member, exc_info=True)
        return ""


async def _owner_only(request: web.Request, operation: str) -> web.Response | None:
    return await require_owner_dashboard_request(request, operation)


async def _run(fn: Callable[[], Any]) -> Any:
    """Run a store call off the event loop. Every call here is file IO."""
    return await asyncio.to_thread(fn)


def _refusal(exc: Exception) -> web.Response:
    """Turn a store refusal into an answer. Separate codes, because they differ for a client.

    ``*_refused`` is the caller's input and a retry of the same request will be refused
    again; ``*_failed`` is this gateway's state and a retry may work. Collapsing them
    would make a client either retry forever or give up on a transient fault.
    """
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
    slug, _member = resolved
    if request.query.get("preview") == "1":
        # The STAGED page, which no version records. Served from the same route and
        # behind the same owner check: a staged page carries this crewmate's fold
        # values exactly as the live one does, so a route of its own would be a second
        # place to get that gate right. `instance.preview_url` builds this link.
        staged = await _run(lambda: _preview_record(slug))
        if staged is None:
            return web.json_response(
                {"error": "nothing is staged to preview", "code": "no_preview"}, status=404
            )
        body = _safe_body(staged.wire())
        # The CURRENT version, not a new one, because staging wrote none. A reader that
        # saw this number move would believe the page had been installed.
        body["preview"] = True
        rendered = await _run(lambda: _render(slug, _member, staged))
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
            record = fallback
        else:
            # NOTHING TO COMPOSE, so nothing is attempted. An empty record carries
            # `manifest={}`, which `parse_manifest` refuses, so `_render` would log a
            # warning with a traceback for a crewmate that has simply adopted
            # nothing -- on every poll and every projection frame, which is the
            # ordinary state of a build whose registry ships no default. The frame's
            # own empty state IS the answer here, and it needs no html.
            renderable = False
    body = _safe_body(record.wire())
    # STALE belongs here with LIVE and EMPTY: `_state_of` calls a stale copy complete
    # and still renderable -- what it cannot do is be compared against or refreshed
    # from its source, which is what the frame's stale band says. A copy served
    # without composing carries no data island, no bootstrap, no band and no ready
    # beacon, so it shows no values at all. Only ERROR is excluded, because that is
    # the one state in which the copy does not parse.
    if renderable and record.state in (
        instance.STATE_LIVE,
        instance.STATE_EMPTY,
        instance.STATE_STALE,
    ):
        rendered = await _run(lambda: _render(slug, _member, record))
        if rendered is not None:
            body["rendered_html"] = rendered
    return web.json_response(body)


#: Any key whose VALUE is a worker's session key, dropped from a value on its way to a
#: page. Named for the whole repository's rule rather than for one fold: the conductor
#: ledger's is that no reader but the conductor sees a session key, and
#: ``work_ledger_board._MASKED_ITEM_FIELDS`` masks exactly this on the Crew page.
_MASKED_VALUE_KEYS = frozenset({"worker_session_key"})

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


def _safe_body(body: dict[str, Any]) -> dict[str, Any]:
    """One wire body with its page halves scrubbed, in place.

    ``html`` and ``manifest`` are the two keys carrying the stored template, and the
    body carries both RAW beside the composed page. Scrubbing only the composed one
    would leave the same credential one key away in the same response.
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
    """
    if not text:
        return text
    return redact_via_context(text)


def _manifest_text_safe(manifest: Any) -> Any:
    """A manifest's own strings, scrubbed. Keys are left alone.

    Values only, and recursively, because a manifest's KEYS are field names the
    loader has already matched against ``^[a-z][a-z0-9_]{0,63}$`` -- a grammar no
    credential survives -- while ``title`` and ``description`` are free prose.
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
    return entry.html


def read_fields(slug: str, member: str, manifest: Any) -> Any:
    """One read of *manifest*'s field values for this crewmate, as the page gets them.

    The page's own read, shared with ``dashboard_fields`` so the values an agent is
    shown are the values the reader sees. File IO; raises on a failed read.
    """
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard_feed import DashboardFeed

    slot = _dashboard_slot(member, slug)
    feed = DashboardFeed(slot, _write_session(slug, member))
    try:
        feed.subscribe(manifest)
        agentic: Any = {}
        if any(spec.agentic for spec in manifest.fields.values()):
            agentic = projection.read_slot_projection(slot, "agentic").value
        return feed.read(manifest, agentic if isinstance(agentic, dict) else {})
    finally:
        feed.unsubscribe()


def _render(slug: str, member: str, record: Any) -> str | None:
    """The live page with its values filled in: the frame's half of contract v3 part 5.

    Fold values come through :class:`~kiro_crew.dashboard_feed.DashboardFeed`, which
    subscribes with a baseline -- the bus hands the CURRENT fold value through the
    projection read path, so nothing refolds a log here. Agentic values come from the
    slot's ``agentic`` fold. The result is :func:`dashboard_frame.compose_body`, the one
    builder that also composes a refill, so the page's ``window.kirocrew`` is the same
    shape on first paint and after.

    DEMO SCOPE: the feed is opened and closed per request. The contract wants one
    long-lived feed per open dashboard pushing refills over the WS exporter; until
    that lands, a re-read (focus, the tab's own refetch) is how the page moves.
    ``None`` on any failure, so the raw page still renders under the frame's own
    stale band rather than the tab erroring.
    """
    try:
        from kiro_crew import dashboard_frame
        from kiro_crew.dashboard_templates.manifest import parse_manifest

        manifest = parse_manifest(dict(record.manifest))
        # THE EXECUTABLE PAGE COMES FROM THE CATALOG, NEVER FROM THE RECORD.
        #
        # `record.html` and `record.manifest` both come out of
        # ``members/<slug>/dashboard/instance.json``, which is WRITABLE. Gating the
        # render on `manifest.source` asked that file to vouch for itself: anything
        # that could append a `<script>` to the stored page could leave the
        # `builtin` label in place beside it, and this step is the one that hands
        # the crewmate's task titles and summaries to whatever runs. The label is
        # not evidence, so it cannot be the gate.
        #
        # What IS trusted is the repository. The record's ``template_id`` names a
        # directory in it, so the id is read from the record and the BYTES are read
        # from the catalog. A stored page may stay on disk -- a rollback reads it,
        # and it is what the crewmate copied -- but nothing executes it.
        page = _trusted_page(slug, record)
        if page is None:
            return None
        read = read_fields(slug, member, manifest)
        payload = dashboard_frame.read_payload(
            # MASKED AND REDACTED on the way out, at the one step that hands fold
            # values to a page's own script. A snapshot is taken from what the page
            # holds, so it inherits this rather than needing its own pass.
            {name: _page_safe(value) for name, value in read.fields.items()},
            agentic=[name for name, spec in manifest.fields.items() if spec.agentic],
            seq=read.seq,
            stale=read.stale,
            missing=read.missing,
            # Masked like the values beside them: a stamp is not a secret, but this is
            # the one chokepoint and a field added here later would otherwise skip it.
            written_at={name: str(_page_safe(at)) for name, at in read.written_at.items()},
        )
        # The catalog's page, redacted like the values that go into it. The scrub is
        # kept because this is the one step producing both the raw body and the
        # composed document, and it guards a hand-edited checkout rather than a page
        # an attacker chose: the bytes here came out of the repository.
        return dashboard_frame.compose_body(_template_text_safe(page), payload)
    except Exception:
        logger.warning("dashboard: could not fill %r's page", slug, exc_info=True)
        return None


def register_member_dashboard_routes(app: web.Application) -> None:
    """Register the dynamic dashboard's read route.

    One route, so there is no ordering question here yet. ``server.py`` duplicates the
    path through its deferred binder rather than calling this function, because calling
    it would import this module at boot and the boot-path rule forbids that for an
    optional subsystem; ``test_member_dashboard_routes`` pins the two spellings against
    each other.
    """
    app.router.add_get("/api/members/{slug}/dashboard", api_member_dashboard)
