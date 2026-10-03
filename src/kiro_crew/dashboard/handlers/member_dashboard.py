"""HTTP for one crewmate's dynamic dashboard: the instance, the registry, share, snapshots.

``GET /api/members/{slug}/dashboard`` is the one route the Dashboard tab's frame reads,
and its body is the shape CONTRACT-v3 fixes between the two:
``{instance_version, template: {id, version}, html, manifest, state}``. The rest of this
module is what puts something there to read -- the registry listing a chooser draws,
adopt, edit, rollback, export, import and snapshot.

**``?member=`` is required on every route here**, exactly as the briefing and rules
reads require it, and for the reason those give: slugification is lossy, so two crew
names can reach one slug. A dashboard instance is ONE directory per slug, so for a
colliding slug the instance belongs to neither crewmate -- serving it to both, with an
editor, would let them overwrite each other's dashboard. The exact name must derive this
slug, exist in config, and be the only name that derives it.

**The read is open to any dashboard caller; every write is owner-gated.** The frame is
reachable by a non-owner dashboard subject, so gating the read would turn their
Dashboard tab into a 403 -- the same boundary ``api_member_panel`` draws. The writes
mutate a stored page the gateway later renders, which is squarely the owner's.

**App tokens are denied outright.** An app token scoped to ``/api/members`` reaches
these by PREFIX, and a crewmate's dashboard is inside exactly what that isolation
withholds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.members import (
    _deny_app_caller,
    _member_names_for_slug,
)
from kiro_crew.dashboard_templates import catalog, instance, share, snapshot
from kiro_crew.members import MemberSlugError

logger = logging.getLogger(__name__)

__all__ = ["register_member_dashboard_routes"]

#: The request body ceiling for a write. An edit carries a page, so this sits above the
#: instance page ceiling with room for the manifest beside it; an import carries a whole
#: share document, which has its own ceiling inside :mod:`share`.
_MAX_BODY_BYTES = 512 * 1024


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


def _write_session(slug: str, member: str) -> str:
    """The session a dashboard change's history entry belongs to: the crewmate's DM log.

    Derived, not looked up, and empty when there is none. The instance record is a file
    and is already committed by the time this is used, so a crewmate whose DM thread has
    never run gets a working dashboard with no history row rather than a refused change.

    Resolved by the slot the member's DM thread runs under, then by the newest session
    unit on it: a slot owns one session id at a time, and the newest is the live one.
    """
    try:
        from kiro_crew.crew_log.store import session_units_for_slot

        slot = members_mod.member_slot_key(slug)
        units = session_units_for_slot(slot)
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
    if isinstance(exc, (instance.InstanceRefused, share.ShareRefused, snapshot.SnapshotRefused)):
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
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    slug, _member = resolved
    try:
        record = await _run(lambda: instance.read(slug))
    except Exception as exc:
        return _refusal(exc)
    if record.state == instance.STATE_EMPTY:
        fallback = await _run(lambda: instance.default_instance(slug))
        if fallback is not None:
            record = fallback
    body = record.wire()
    if record.state in (instance.STATE_LIVE, instance.STATE_EMPTY):
        rendered = await _run(lambda: _render(slug, _member, record))
        if rendered is not None:
            body["rendered_html"] = rendered
    return web.json_response(body)


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
        from kiro_crew.crew_log import projection
        from kiro_crew.dashboard_feed import DashboardFeed
        from kiro_crew.dashboard_templates.manifest import parse_manifest

        manifest = parse_manifest(dict(record.manifest))
        slot = members_mod.member_slot_key(slug)
        feed = DashboardFeed(slot, _write_session(slug, member))
        try:
            feed.subscribe(manifest)
            agentic: Any = {}
            if any(spec.agentic for spec in manifest.fields.values()):
                agentic = projection.read_slot_projection(slot, "agentic").value
            read = feed.read(manifest, agentic if isinstance(agentic, dict) else {})
        finally:
            feed.unsubscribe()
        payload = dashboard_frame.read_payload(
            read.fields,
            agentic=[name for name, spec in manifest.fields.items() if spec.agentic],
            seq=read.seq,
            stale=read.stale,
            missing=read.missing,
        )
        return dashboard_frame.compose_body(record.html, payload)
    except Exception:
        logger.warning("dashboard: could not fill %r's page", slug, exc_info=True)
        return None


async def api_member_dashboard_history(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/dashboard/history?member=<name> — the change history.

    A fold over this instance's ``dashboard/instance_changed`` entries, answered from the
    savepoint every write steps. Rows are newest LAST, which is the order they happened
    in; a reader showing the latest change reads the end rather than reversing a list.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    slug, member = resolved
    session_id = await _run(lambda: _write_session(slug, member))
    try:
        rows = await _run(lambda: instance.history(slug, session_id=session_id))
        kept = await _run(lambda: instance.versions(slug))
    except Exception as exc:
        return _refusal(exc)
    return web.json_response({"history": list(rows), "versions": list(kept)})


async def api_member_dashboard_adopt(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/adopt?member=<name> — copy a template in.

    Body: ``{"template_id": "<id>"}``. A COPY: the page and manifest are stored on the
    instance, so the template's author shipping a new version leaves this dashboard
    showing what was adopted until it adopts again.

    Adopting over an existing dashboard is an ordinary version bump rather than a
    refusal. The previous version stays on disk, so a crewmate that adopted the wrong
    template rolls back to the one it had -- which is a better answer than making the
    caller delete something first.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    slug, member = resolved
    owner_denied = await _owner_only(request, "members.dashboard.adopt")
    if owner_denied is not None:
        return owner_denied
    body, body_error = await read_bounded_json(request, _MAX_BODY_BYTES)
    if body_error is not None:
        return body_error
    template_id = str((body or {}).get("template_id") or "")
    if not template_id:
        return _bad("missing_template_id", "template_id is required")
    session_id = await _run(lambda: _write_session(slug, member))
    try:
        record = await _run(lambda: instance.adopt(slug, template_id, session_id=session_id))
    except Exception as exc:
        return _refusal(exc)
    return web.json_response(record.wire())


async def api_member_dashboard_edit(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/edit?member=<name> — change the crewmate's copy.

    Body: ``{"html": "...", "manifest": {...}}``, at least one. Both halves are checked
    TOGETHER against the parity rule even when only one is sent, because that rule is
    about the pair: a page edited to bind a new field is refused until the manifest
    declares it, which is what keeps an edit from producing an unowned empty cell.

    The template id and version are untouched by an edit, so the instance never claims
    to be a version of a template it does not match.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    slug, member = resolved
    owner_denied = await _owner_only(request, "members.dashboard.edit")
    if owner_denied is not None:
        return owner_denied
    body, body_error = await read_bounded_json(request, _MAX_BODY_BYTES)
    if body_error is not None:
        return body_error
    body = body or {}
    html = body.get("html")
    manifest = body.get("manifest")
    if html is not None and not isinstance(html, str):
        return _bad("bad_html", "html must be a string")
    if manifest is not None and not isinstance(manifest, dict):
        return _bad("bad_manifest", "manifest must be an object")
    if html is None and manifest is None:
        return _bad("empty_edit", "an edit must carry html, manifest, or both")
    session_id = await _run(lambda: _write_session(slug, member))
    try:
        record = await _run(
            lambda: instance.edit(slug, html=html, manifest=manifest, session_id=session_id)
        )
    except Exception as exc:
        return _refusal(exc)
    return web.json_response(record.wire())


async def api_member_dashboard_rollback(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/rollback?member=<name> — restore a past version.

    Body: ``{"to_version": <n>}``. The restored payload becomes a NEW instance version,
    never a rewind: two different pages must not both have been version 2, and a client
    caching by version would keep serving the page it was told was replaced.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    slug, member = resolved
    owner_denied = await _owner_only(request, "members.dashboard.rollback")
    if owner_denied is not None:
        return owner_denied
    body, body_error = await read_bounded_json(request, _MAX_BODY_BYTES)
    if body_error is not None:
        return body_error
    to_version = (body or {}).get("to_version")
    if not isinstance(to_version, int) or isinstance(to_version, bool) or to_version < 1:
        return _bad("bad_to_version", "to_version must be a positive integer")
    session_id = await _run(lambda: _write_session(slug, member))
    try:
        record = await _run(lambda: instance.rollback(slug, to_version, session_id=session_id))
    except Exception as exc:
        return _refusal(exc)
    return web.json_response(record.wire())


# --------------------------------------------------------------------------
# the registry, share, snapshots
# --------------------------------------------------------------------------


async def api_member_dashboard_templates(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/dashboard/templates?member=<name> — what can be adopted.

    Carries ``problems`` beside ``templates``: a directory the registry could not load
    is reported rather than omitted, so a template somebody just wrote and got wrong is
    visible as a broken template instead of as an absent one. The rows do NOT carry each
    page -- no chooser renders them, and a list of every template would otherwise be a
    list of every page.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    try:
        found = await _run(catalog.list_templates)
    except Exception as exc:
        return _refusal(exc)
    return web.json_response(
        {
            "templates": [entry.listing() for entry in found.entries],
            "problems": [{"name": name, "why": why} for name, why in found.problems],
        }
    )


async def api_member_dashboard_export(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/dashboard/export?member=<name>&template_id=<id> — one file.

    Returns the share document as JSON text under ``file``, not as the response body
    itself: the caller writes it somewhere, and shipping it as the body would make this
    route's own error shape and the document's shape the same thing to parse.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    template_id = request.query.get("template_id", "")
    if not template_id:
        return _bad("missing_template_id", "template_id is required")
    try:
        text = await _run(lambda: share.export_template(template_id))
    except Exception as exc:
        return _refusal(exc)
    return web.json_response({"template_id": template_id, "file": text})


async def api_member_dashboard_import(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/import?member=<name> — take one file in.

    Body: ``{"file": "<share document text>", "as_id": "<optional new id>"}``. The
    document is parsed and parity-checked BEFORE anything is written, so a malformed
    share never becomes a directory the registry then reports as broken. A colliding id
    is refused and names the collision; ``as_id`` is how both are kept.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    owner_denied = await _owner_only(request, "members.dashboard.import")
    if owner_denied is not None:
        return owner_denied
    body, body_error = await read_bounded_json(request, _MAX_BODY_BYTES)
    if body_error is not None:
        return body_error
    body = body or {}
    text = body.get("file")
    as_id = body.get("as_id") or ""
    if not isinstance(text, str) or not text.strip():
        return _bad("missing_file", "file is required and must be the share document text")
    if not isinstance(as_id, str):
        return _bad("bad_as_id", "as_id must be a string")
    try:
        entry = await _run(lambda: share.import_template(text, as_id=as_id))
    except Exception as exc:
        return _refusal(exc)
    return web.json_response({"template": entry.listing()})


async def api_member_dashboard_snapshot(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/snapshot?member=<name> — freeze what it shows.

    Body: ``{"fields": {...}, "seq": <n>}``. The template and instance versions come
    from the instance rather than from the caller: a snapshot's whole value is that the
    values and the thing that laid them out were read together, and a caller-supplied
    version could name a page these values never appeared on.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    slug, _member = resolved
    owner_denied = await _owner_only(request, "members.dashboard.snapshot")
    if owner_denied is not None:
        return owner_denied
    body, body_error = await read_bounded_json(request, _MAX_BODY_BYTES)
    if body_error is not None:
        return body_error
    body = body or {}
    fields = body.get("fields")
    seq = body.get("seq")
    if not isinstance(fields, dict) or not fields:
        return _bad("missing_fields", "fields is required and must be a non-empty object")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return _bad("bad_seq", "seq must be a non-negative integer")

    def _take() -> dict[str, Any]:
        record = instance.read(slug)
        if record.state == instance.STATE_EMPTY:
            raise instance.InstanceRefused(
                "this crewmate has no dashboard to snapshot; adopt a template first"
            )
        return snapshot.take_snapshot(
            slug,
            template_id=record.template_id,
            template_version=record.template_version,
            instance_version=record.instance_version,
            fields=fields,
            seq=seq,
        ).wire()

    try:
        taken = await _run(_take)
    except Exception as exc:
        return _refusal(exc)
    return web.json_response({"snapshot": taken})


async def api_member_dashboard_snapshots(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/dashboard/snapshots?member=<name>[&id=<snapshot id>].

    Without ``id``, the snapshot ids this crewmate has, oldest first. With ``id``, that
    one frozen dashboard whole.
    """
    resolved = await _resolve(request)
    if isinstance(resolved, web.Response):
        return resolved
    slug, _member = resolved
    wanted = request.query.get("id", "")
    try:
        if wanted:
            taken = await _run(lambda: snapshot.read_snapshot(slug, wanted))
            return web.json_response({"snapshot": taken.wire()})
        ids = await _run(lambda: snapshot.list_snapshots(slug))
    except Exception as exc:
        return _refusal(exc)
    return web.json_response({"snapshots": list(ids)})


def register_member_dashboard_routes(app: web.Application) -> None:
    """Register the dynamic dashboard routes.

    The literal children are registered BEFORE the bare ``/dashboard`` path. aiohttp
    resolves in registration order and these are all literals, so the order is not
    load-bearing here -- it is kept specific-first anyway to match the rest of the route
    table, where it is.
    """
    app.router.add_get("/api/members/{slug}/dashboard/templates", api_member_dashboard_templates)
    app.router.add_get("/api/members/{slug}/dashboard/history", api_member_dashboard_history)
    app.router.add_get("/api/members/{slug}/dashboard/export", api_member_dashboard_export)
    app.router.add_get("/api/members/{slug}/dashboard/snapshots", api_member_dashboard_snapshots)
    app.router.add_post("/api/members/{slug}/dashboard/adopt", api_member_dashboard_adopt)
    app.router.add_post("/api/members/{slug}/dashboard/edit", api_member_dashboard_edit)
    app.router.add_post("/api/members/{slug}/dashboard/rollback", api_member_dashboard_rollback)
    app.router.add_post("/api/members/{slug}/dashboard/import", api_member_dashboard_import)
    app.router.add_post("/api/members/{slug}/dashboard/snapshot", api_member_dashboard_snapshot)
    # LAST: the frame's read, and the only shape CONTRACT-v3 fixes.
    app.router.add_get("/api/members/{slug}/dashboard", api_member_dashboard)
