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
import re
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

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
    # The reader's UI language, as the browser resolved it. Checked against the
    # shipped catalogs in `dashboard_frame.page_locale`; anything else is English.
    locale = request.query.get("locale", "")
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
        rendered = await _run(lambda: _render(slug, _member, staged, locale))
        if rendered is not None:
            body["rendered_html"] = rendered
        return web.json_response(body)
    # THE V3 PACKAGE PAGE, served from this route rather than one of its own. A crewmate
    # has one dashboard and the tab asks one route for it; a second route would be a
    # second place to get the member resolution, the owner gate and the redaction right,
    # which is the same reason the preview is served from here. A crewmate with no
    # package falls straight through and the body below is byte-identical to before.
    packaged = await _run(lambda: _read_package(slug))
    if packaged.model is not None and packaged.package is not None:
        return await _package_body(request, slug, _member, packaged, locale)
    try:
        record = await _run(lambda: instance.read(slug))
    except Exception as exc:
        return _refusal(exc)
    renderable = True
    if record.state == instance.STATE_EMPTY:
        # THE BUILTIN FALLBACK IS SCOPED BY SOURCE, and the source is the condition.
        #
        # v3 has NO DEFAULT PAGE: a dashboard exists once the agent writes a layout,
        # and until then the frame's own empty state is the answer. So a crewmate whose
        # dashboard comes from a PACKAGE never gets a shipped template composed under
        # it -- not when the package serves (that returned above) and not when it is
        # bound but unreadable, which is this branch. Falling back there would hand that
        # crewmate a page they did not compose, carrying their own fold values, and
        # nothing on it would say it is not theirs.
        #
        # A crewmate bound to the TEMPLATE REGISTRY is unchanged: goal-board, standup
        # and office are a live feature and this item does not touch them.
        fallback = None if packaged.bound else await _run(lambda: instance.default_instance(slug))
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
    # THE SAME NAME RULE AS THE PACKAGE BRANCH, and it belongs on both for one reason:
    # a template manifest's field names carry the same grammar a package's do, so a
    # fix on the package alone would leave the hole open on the line this product
    # already serves. The manifest goes out empty and nothing is composed, so no name
    # leaves by either half of the body. See :func:`_unsafe_names`.
    declared = getattr(record, "manifest", None)
    unsafe_declared = _unsafe_names(
        declared.get("fields") if isinstance(declared, Mapping) else None
    )
    if unsafe_declared:
        logger.warning(
            "dashboard: refusing to compose %r's template page: %d declared field "
            "name(s) would be redacted on the way to a page",
            slug,
            unsafe_declared,
        )
        renderable = False
        body["manifest"] = {}
        body.pop("html", None)
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
        rendered = await _run(lambda: _render(slug, _member, record, locale))
        if rendered is not None:
            body["rendered_html"] = rendered
    return web.json_response(body)


async def _package_body(
    request: web.Request,
    slug: str,
    member: str,
    packaged: _Packaged,
    locale: str,
) -> web.Response:
    """THE FIRST LOAD of a v3 package page: the whole page, and the push armed.

    The order is load-bearing. The push is armed BEFORE the values are read, so a fold
    that moves during the read produces a patch the browser will see as a version it
    does not hold and answer with a refetch. Reading first and arming after would lose
    that move silently, which is the one failure a version number cannot report.

    The body is ADDITIVE beside the v2 shape, so a reader can tell the two apart by one
    key: ``package`` is present and carries the artifact slug, its version -- which is
    the ``layout`` a patch is compared against -- and ``push_version``, the counter the
    page starts from. ``read`` is the payload the document is built from and refilled
    with, so the first paint and every patch describe one read; ``blocks`` says which
    blocks hold which of its fields. ``rendered_html`` is the minted document when this
    build carries the renderer.
    """
    package, model = packaged.package, packaged.model
    assert package is not None  # the caller checked both halves
    # THE DM SLOT, resolved here and off the loop because `_dashboard_slot` reads config
    # from disk. It is what the push's slot-scoped folds are keyed by, and the slug is a
    # DIFFERENT slot for a V2 crewmate -- an empty one.
    slot = await _run(lambda: _dashboard_slot(member, slug))
    page = arm_block_push(request, slug, member, model, slot=slot, locale=locale, package=package)
    if page is None:
        # Nothing to push to (no hub). The page is still served in full: a body with no
        # live push is a page that refreshes on its own, not a refusal.
        from kiro_crew import dashboard_package_render as render
        from kiro_crew.dashboard.handlers import member_dashboard_push as push

        page = push.LivePage(
            slug,
            member,
            model,
            slot=slot,
            state=None,
            loop=None,
            redact=_page_safe,
            reread=_reread_model,
            locale=locale,
            display_seam=render.display_values,
            patch_seam=render.block_patch,
            package=package,
        )
    # SUBSCRIBED BEFORE THE VALUES ARE READ, and only when it is not already: a reused
    # page holds values the bus has kept current, and resubscribing would empty that
    # cache and then serve the full load out of it.
    await _run(page.ensure_subscribed)
    blocks, missing = page.blocks()
    read = page.read()
    body: dict[str, Any] = {
        "state": "live",
        "instance_version": int(getattr(model, "version", 0) or 0),
        "package": {
            "slug": str(getattr(model, "slug", "") or ""),
            "version": int(getattr(model, "version", 0) or 0),
            "layout_fingerprint": str(model.layout_fingerprint),
            "bound_to": str(model.bound_to),
        },
        "push_version": page.version,
        "push_frame": _push_frame_type(),
        # The TWO postMessage types the document listens for, read from the server so
        # the frontend holds neither constant: the full-read one for a first paint or a
        # refresh, and the block-patch one a push forwards verbatim.
        "page_message": _page_message_type(),
        "page_patch_message": _page_patch_message_type(),
        "blocks": blocks,
        "missing": missing,
        "read": read,
    }
    rendered = await _run(
        lambda: _minted_package_page(slug, package, read, theme=_page_theme(), title=member)
    )
    if rendered is not None:
        body["rendered_html"] = rendered
    return web.json_response(body)


def _page_theme() -> str:
    """Which base palette the document starts from: the renderer's own first one.

    READ FROM THE RENDERER, not named here, so this handler holds no palette constant
    that could drift from the one the document is actually styled with.

    NOT A READER CHOICE, deliberately. There is no ``?theme=`` on this route: nothing
    in the dashboard asks for one, and a query parameter with no caller is a surface to
    keep working rather than a feature. The tab the page mounts in carries the app's
    own theme, so when a reader's palette does reach the document it will arrive the way
    the rest of the chrome's does, not as a hand-written link.
    """
    from kiro_crew import dashboard_package_render as render

    return str(render.THEMES[0])


def _page_message_type() -> str:
    """The ``postMessage`` type the document's own listener accepts.

    ``dashboard_frame.DATA_MESSAGE_TYPE`` -- the repository already fixed this for the
    v2 frame and the package renderer imports the same constant, so the page half of
    the contract is not something this controller invents. Carried in the body for the
    same reason ``push_frame`` is: the frontend reads the name rather than holding a
    second copy of it.
    """
    from kiro_crew import dashboard_frame

    return dashboard_frame.DATA_MESSAGE_TYPE


def _page_patch_message_type() -> str:
    """The ``postMessage`` type a BLOCK PATCH arrives as, from the renderer.

    Not read from the frame it travels in: the type belongs to the half the DOCUMENT has
    a listener for, so it is the renderer's own ``BLOCK_PATCH_MESSAGE_TYPE`` and the
    frontend reads the name from the server rather than holding a second copy.
    """
    from kiro_crew import dashboard_package_render as render

    return render.BLOCK_PATCH_MESSAGE_TYPE


def _push_frame_type() -> str:
    """The WS message type a patch for this page arrives as.

    Carried in the body so the frontend reads the name from the server rather than
    holding a second copy of it -- the seam with the page is the shape, not a constant
    spelled in two languages.
    """
    from kiro_crew.dashboard.handlers import member_dashboard_push as push

    return push.BLOCK_FRAME


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


def _unsafe_names(names: Any) -> int:
    """How many of *names* :func:`_page_safe` would rewrite. Zero means none.

    A declared field name and a block id are AGENT-AUTHORED, and the grammar both
    sides enforce admits a credential shape: ``[a-z][a-z0-9_]{0,63}`` is satisfied by
    ``ghp_`` followed by 36 lowercase characters. Such a name reaches a page raw in
    the read's ``missing`` and ``agentic`` lists, in the frame's ``blocks`` and in a
    manifest's own keys, beside values the same redactor has already masked.

    A NAME IS AN IDENTIFIER, which is why this counts them rather than redacting
    them. The read's ``fields`` keys, the frame's ``blocks`` and the composed
    document's cell ids are joined by it, so rewriting one here breaks that join and
    leaves a page whose cells resolve to nothing. The caller fails closed instead:
    every name that passes stays byte-identical, and a read carrying one that would
    not is refused whole.

    Returns a COUNT and never the offending name, because the caller logs this and a
    name that needs redacting is not a name to write to a log.
    """
    if isinstance(names, Mapping):
        candidates: Any = names.keys()
    elif isinstance(names, (list, tuple, set, frozenset)):
        candidates = names
    else:
        return 0
    return sum(
        1 for name in candidates if isinstance(name, str) and redact_via_context(name) != name
    )


def _package_names_unsafe(package: Mapping[str, Any]) -> int:
    """The same count over one package's declared names: its field names and block ids.

    Both, because both reach a page as keys: a field name through the read and the
    manifest, a block id through the frame's ``blocks`` mapping.
    """
    model = package.get("model") if isinstance(package, Mapping) else None
    view = package.get("view") if isinstance(package, Mapping) else None
    types = model.get("types") if isinstance(model, Mapping) else None
    blocks = view.get("blocks") if isinstance(view, Mapping) else None
    count = _unsafe_names(types)
    if isinstance(blocks, (list, tuple)):
        count += _unsafe_names([b.get("id") for b in blocks if isinstance(b, Mapping)])
    return count


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

    Values only, and recursively: ``title`` and ``description`` are free prose, while
    a manifest's KEYS are field names the loader has matched against
    ``^[a-z][a-z0-9_]{0,63}$`` -- and that grammar does NOT exclude a credential, since
    ``ghp_`` followed by 36 lowercase characters satisfies it. A key is an identifier
    the composed page joins its cells by, so the caller refuses the whole read rather
    than rewriting one here: see :func:`_unsafe_names`.
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


#: The scope prefix a crewmate's dashboard package is bound with.
#:
#: ``bound_to`` is ``crewmate:<slug>`` or ``session:<slot key>``, and the controller is
#: keyed by SLUG -- the slug it already validated and already proved derives from
#: exactly one crew name. So the binding this route may serve is this one string and
#: nothing else: a session-bound package names a slot, and a crewmate reaching it would
#: be reaching past its own page.
_CREWMATE_BINDING: Final[str] = "crewmate:"

#: Directives a minted package document MUST declare before this gateway serves it.
#:
#: Checked on the renderer's own output rather than assumed of it, and checked as the
#: list of properties the safety argument actually rests on rather than as one string.
#: The shipped policy grants ``script-src 'unsafe-inline'`` and ``style-src
#: 'unsafe-inline'`` -- the script and the style ARE the document, and under no network
#: there is no URL for them to live at -- so a check for ``default-src 'none'`` alone
#: would pass while saying something false. What must hold is that the page cannot
#: compile code it was handed and cannot reach anything: see :func:`_minted_package_page`.
_REQUIRED_CSP_DIRECTIVES: Final[tuple[str, ...]] = (
    "default-src 'none'",
    "connect-src 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
)

#: Anything in the policy that would let the document compile handed-in code or reach
#: the network. ``*`` and the two URL schemes cover a source list opened to a host;
#: ``'unsafe-eval'`` is what turns a string into code.
_FORBIDDEN_CSP_SOURCES: Final[tuple[str, ...]] = ("'unsafe-eval'", "http:", "https:", "*")

#: Where the policy is read from in the composed document.
#: The attribute value is delimited by a BACKREFERENCE to its own opening quote, not by
#: "anything but a quote": a policy is full of ``'none'`` and ``'unsafe-inline'``, so a
#: character class excluding the apostrophe captures ``default-src`` and stops -- which
#: made every directive read as missing and the gate refuse the document it was built
#: for. Fail-closed on anything it cannot parse, including a different attribute order.
_CSP_META_RE: Final[Any] = re.compile(
    r"""<meta\s+http-equiv=["']Content-Security-Policy["']\s+content=(["'])(.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class _Packaged:
    """What asking "does this crewmate's dashboard come from a package?" answered.

    THREE states, not two, and the third is the one that matters: no package bound here
    (``bound`` False), a package this route can serve (``model`` set), and a package
    bound here that cannot be read (``bound`` True, ``model`` None).

    The third must not collapse into the first. A crewmate whose agent composed a
    layout HAS a dashboard, and the answer to a package this gateway cannot read is the
    frame's empty state -- never a shipped builtin page, which would show that crewmate
    somebody else's dashboard and let them act on its numbers.
    """

    bound: bool
    package: dict[str, Any] | None = None
    model: Any = None


def _read_package(slug: str) -> _Packaged:
    """Whether a dashboard package is bound to *slug*, and it if it can be served.

    ONE read serving both halves of a servable package: the Model drives the field
    values and the push's subscriptions, and the raw package carries the view and the
    theme the renderer composes from. A Model-only read answers just the first, so a
    second read would be a second chance for the two to disagree about which version
    they describe.

    Resolved BY this crewmate's own binding, so a package that comes back is this
    crewmate's by construction -- :func:`_owns_package` re-asks on the push path, where
    the question is live because a rebind writes no version.
    """
    from kiro_crew.artifact_store import dashboard_package as pkg
    from kiro_crew.artifacts import ArtifactError, get_default_store

    try:
        store = get_default_store()
    except Exception:
        # No artifact store on this build at all. NOT "bound": nothing was read, so
        # there is no package to be loyal to, and a crewmate with a v2 template adopted
        # must still be served it.
        logger.debug("dashboard: this build has no artifact store to ask about %r", slug)
        return _Packaged(bound=False)
    try:
        bound = pkg.resolve_bound_slug(f"{_CREWMATE_BINDING}{slug}", store=store)
    except Exception:
        # The store is there and the SCAN failed, so whether a package is bound here is
        # unknown. Answered as bound, which suppresses the builtin fallback: the cost of
        # that is the frame's empty state for a crewmate who may have a v2 template, and
        # the cost of the other answer is handing a crewmate who composed a layout a
        # shipped page full of their own fold values with nothing saying it is not
        # theirs.
        logger.warning("dashboard: the package scan for %r failed", slug, exc_info=True)
        return _Packaged(bound=True)
    if bound is None:
        return _Packaged(bound=False)
    try:
        loaded = store.get(bound)
        package = pkg.parse_package(loaded.content or "")
    except (ArtifactError, OSError, ValueError):
        # BOUND AND UNREADABLE. The binding scan already parsed this record, so getting
        # here means the content changed under the read or the store failed -- and
        # either way this crewmate's dashboard is the package, not a builtin.
        logger.warning("dashboard: %r's package %r could not be read", slug, bound, exc_info=True)
        return _Packaged(bound=True)
    unsafe = _package_names_unsafe(package)
    if unsafe:
        # BOUND AND UNSERVEABLE, answered as the unreadable case above is: a name this
        # page cannot carry makes the whole package unservable, because the alternative
        # is a response whose keys are the secret. See :func:`_unsafe_names` for why a
        # name is refused rather than redacted. The count is logged and the name is not.
        logger.warning(
            "dashboard: refusing to serve %r's package %r: %d declared name(s) would be "
            "redacted on the way to a page",
            slug,
            bound,
            unsafe,
        )
        return _Packaged(bound=True)
    return _Packaged(
        bound=True,
        package=package,
        model=pkg.model_of(package, slug=loaded.slug, version=loaded.version),
    )


def _owns_package(slug: str, model: Any) -> bool:
    """Whether the package *model* is THIS crewmate's to be served and pushed.

    THE OWNER CHECK, kept here with the redaction and never moved into the push module
    or the page: the controller is the code that knows which slug this request resolved
    to, and it is the code the owner gate already runs in front of.

    One string comparison, because ``bound_to`` is one string. A package bound to
    another crewmate, or to a session slot, is not this crewmate's -- and the second is
    the case a looser check would miss, since a slot key and a member slug look alike.
    """
    return str(getattr(model, "bound_to", "") or "") == f"{_CREWMATE_BINDING}{slug}"


def _minted_package_page(
    slug: str,
    package: Mapping[str, Any],
    read: Mapping[str, Any],
    *,
    theme: str = "light",
    title: str = "",
) -> str | None:
    """The document this gateway will EXECUTE for a package page, or ``None`` to refuse.

    THE GATE FOR A V3 PAGE, and it is deliberately not
    ``instance.RENDERABLE_SOURCES``. That set decides whether a stored TEMPLATE
    INSTANCE's provenance may become a page, and :func:`_trusted_page` answers it by
    loading markup from the template catalog by ``template_id``. A package has no
    catalog directory and carries no markup at all, so it can never reach that gate --
    widening the set would loosen the instance adopt path and buy this page nothing.

    What makes a package page safe to execute here. Stated as it actually is, because
    the shipped policy is NOT ``default-src 'none'`` alone -- it grants inline script
    and inline style, since the script and the style ARE the document and under no
    network there is no URL for them to live at:

    * **The inline script is THE REPOSITORY'S, not the agent's.** It is the renderer's
      own code plus the libraries it vendors, composed by a function in this repo. A
      validated package holds ``kind``, ``bound_to``, ``model``, ``view`` and ``theme``
      and has no key that can carry markup or script, so the agent supplies DATA and a
      layout -- never a statement. ``'unsafe-eval'`` is withheld, so the script cannot
      turn the data it was handed into code either.
    * **The block catalogue is CLOSED.** Every block's ``type`` is re-checked against
      ``view_block_catalog()`` here, at the render site, rather than trusted from the
      write path -- the same reason ``_trusted_page`` does not trust the record's own
      ``source`` label. An agent chooses which blocks to place and never what a block
      is made of.
    * **The page has nowhere to send anything.** ``connect-src 'none'``, no origin on
      any fetching directive, ``form-action 'none'`` and ``base-uri 'none'``, all
      verified on the composed output by :func:`_csp_problems`. That is what makes a
      document trusted with an operator's own numbers: not that it cannot be wrong, but
      that being wrong cannot carry them anywhere.

    THE RESIDUAL RISK, named rather than hidden: ``script-src 'unsafe-inline'`` means an
    escaping defect in the renderer would turn an agent-supplied VALUE into running
    script. What that script could then do is bounded by the directives above -- no
    fetch, no post, no navigation, no eval -- and the agent already chooses those
    values, so what it gains is the page's appearance and not its reach. This is the
    posture the repository already serves for its theme overlay in
    ``dashboard/theme_validate.py``, not one introduced here.

    None of the three holds for an authored template, which is why that gate stays at
    one value.
    """
    from kiro_crew.artifact_store.dashboard_package import view_block_catalog

    catalogue = view_block_catalog()
    blocks = package.get("view", {}).get("blocks", [])
    unknown = [str(b.get("type")) for b in blocks if str(b.get("type")) not in catalogue]
    if unknown:
        logger.warning(
            "dashboard: refusing to mint %r's page: block types %s are not in the catalogue",
            slug,
            sorted(set(unknown)),
        )
        return None
    from kiro_crew import dashboard_package_render as render

    # THE SPEC IS REDACTED BEFORE THE RENDERER READS IT, not only after it writes.
    #
    # A field's own spec is agent-authored, and the renderer CONCATENATES parts of it
    # into strings: `format_value` appends a `unit` to the value. A credential planted
    # in a unit therefore leaves the formatter already joined to a number -- and the
    # redactor's pattern for an assignment does not match what that join produces, so a
    # scrub applied only to the result masks the `KEY=` prefix and leaves the key body
    # in the page. Redacting the spec FIRST means the only bytes the renderer can
    # concatenate are already masked.
    #
    # The egress scrub below stays: it covers the theme's CSS and every other string
    # the document is built from, which this pass does not reach into.
    try:
        document = render.render_dashboard(
            _manifest_text_safe(package), read, theme=theme, title=title
        )
    except Exception:
        logger.warning("dashboard: could not mint %r's package page", slug, exc_info=True)
        return None
    if not isinstance(document, str):
        return None
    problems = _csp_problems(document)
    if problems:
        logger.warning(
            "dashboard: refusing to serve %r's package page, its policy %s",
            slug,
            "; ".join(problems),
        )
        return None
    # Redacted like a template's own text, and for the same reason: this is the one step
    # that produces the document, and the theme's tokens and CSS are agent-authored.
    return _template_text_safe(document)


def _csp_problems(document: str) -> list[str]:
    """What is wrong with *document*'s content-security policy. Empty means nothing is.

    Read out of the document's own ``<meta http-equiv>`` rather than taken from the
    renderer's constant: the constant is a claim about what the renderer means to emit,
    and this gate is about what it DID emit.

    A check for ``default-src 'none'`` as a substring would pass on the shipped policy
    while the justification beside it read as false, because that policy starts with
    exactly those bytes and then grants inline script. So the check is the list of
    properties the argument rests on, each named in the refusal.
    """
    found = _CSP_META_RE.search(document or "")
    if found is None:
        return ["declares no Content-Security-Policy"]
    policy = found.group(2)
    problems = [f"is missing {d!r}" for d in _REQUIRED_CSP_DIRECTIVES if d not in policy]
    problems += [f"grants {s!r}" for s in _FORBIDDEN_CSP_SOURCES if s in policy]
    return problems


def arm_block_push(
    request: web.Request,
    slug: str,
    member: str,
    model: Any,
    *,
    slot: str,
    locale: str = "",
    package: Mapping[str, Any] | None = None,
) -> Any:
    """Arm -- or refresh -- the live push for this crewmate's package page.

    Called from the read, so the whole page and the subscriptions behind its patches are
    established by one request: a page armed anywhere else could be pushing to a browser
    that never got a first load to apply patches to.

    *slot* IS RESOLVED BY THE CALLER, off the loop, through :func:`_dashboard_slot` --
    the same derivation the v2 read uses and the same one that CREATES the thread. It is
    passed rather than derived here because this function runs on the event loop and
    that derivation reads config from disk. The push subscribes by it, and the slug is
    not a substitute: see :meth:`member_dashboard_push.LivePage.bus_key`.

    THE OWNER CHECK AND THE REDACTION ARE PASSED FROM HERE AND IMPLEMENTED HERE. The
    push module gets ``_page_safe`` as the one function values cross on their way to a
    socket, and ``_reread_model`` as the only way it can learn the package moved -- so a
    binding this crewmate does not own closes the page rather than pushing from it.
    """
    from kiro_crew.dashboard.handlers import member_dashboard_push as push

    if not _owns_package(slug, model):
        push.close_page(slug)
        return None
    state = request.app.get("state")
    if state is None:
        # No hub in this application, so there is nobody to push to. Not an error: the
        # route is registered standalone in tests and by the route-table check.
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - the route always runs on a loop
        loop = None
    from kiro_crew import dashboard_package_render as render

    page = push.open_page(
        slug,
        member,
        model,
        slot=slot,
        state=state,
        loop=loop,
        redact=_page_safe,
        reread=_reread_model,
        locale=locale,
        display_seam=render.display_values,
        patch_seam=render.block_patch,
        package=package,
    )
    return page


def _reread_model(slug: str) -> tuple[Mapping[str, Any], Any] | None:
    """``(package, model)`` bound to *slug* now, or ``None``. File IO; off the loop.

    Handed to the push so the ONE question it may ask about ownership is asked through
    the controller's own binding rule. It resolves BY the binding, so a package rebound
    to another crewmate answers ``None`` here rather than coming back attached to a page
    that is not its.

    BOTH halves, because the push holds both: ``block_patch`` narrows per block off
    ``package["view"]`` while the Model says which fields exist, and handing back only
    the Model would let a page replace one and keep the other.
    """
    read = _read_package(slug)
    if read.model is None or read.package is None:
        return None
    if not _owns_package(slug, read.model):
        return None
    return read.package, read.model


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


def _render(slug: str, member: str, record: Any, locale: str = "") -> str | None:
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
            locale=locale,
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
