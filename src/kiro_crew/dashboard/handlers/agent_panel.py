"""HTTP routes for a crew's webview.

Two halves with deliberately different auth, because they are different acts:

* **Publishing** (``/api/agent-panel/*``) is MCP-only and strict-internal. The
  crew a call writes to is derived from the CALLING SESSION's identity
  (``X-Session-Key``, vetted by ``_recognize_session``) and then from that
  session's agent -- never from the request body. So a crew can only ever
  publish its own webview, and raw HTTP with no recognized session identity is
  refused. Restricted (incognito/temporary/guest) sessions are refused too: a
  published panel is durable on-disk state, which is exactly what those modes
  promise not to leave behind.

  Both publish routes are listed in ``server._STRICT_INTERNAL_API_PATHS`` --
  without that entry the internal-secret call falls through to cookie auth and
  every publish fails with 403.

* **Reading** (``/api/members/{slug}/panel``) is an ordinary cookie-authed
  dashboard route, because the drawer is what reads it. It is a read: nothing
  under it can publish or edit a panel.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.handlers.members import (
    _deny_app_caller,
    _member_names_for_slug,
)
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.history import is_incognito_transcript
from kiro_crew.members import MemberSlugError
from kiro_crew.sel import sel
from kiro_crew.validation import (
    _AGENT_NAME_RE,
    PANEL_PUBLISH_SCHEMA,
    ValidationError,
    validate_tool_args,
)

logger = logging.getLogger(__name__)


async def _resolve_publishing_crew(
    request: web.Request, operation: str
) -> tuple[tuple[str, str], None] | tuple[None, web.Response]:
    """Vet the caller and resolve it to the crew whose panel it may write.

    Returns ``((slug, crew_name), None)`` or ``(None, refusal)``.

    The crew comes from the session's own agent binding, never from the body: a
    body-supplied name would let one crew publish a webview that presents as
    another's, and the whole point of a per-crew panel is that the operator can
    trust whose state they are reading.

    AUTHORIZATION FIRST, and it cannot be left to the route listing. The crew is
    resolved from a caller-CHOSEN ``X-Session-Key``, so the header is an identity
    claim rather than a lookup key: a caller holding only a dashboard cookie could
    name any live session and have this resolve to THAT crew, then overwrite its
    panel. Requiring ``request["internal_auth"]`` -- set by
    ``token_auth_middleware`` exclusively on a constant-time ``X-Internal-Secret``
    match -- closes the cookie and app-token-over-HTTP variants, and
    ``_deny_app_caller`` closes the one that gate does NOT: an internal caller
    whose identity resolves to an APP.

    Those two are not alternatives, and an earlier version of this docstring
    claimed they were ("never present on a cookie- or app-token-authenticated
    request"). ``token_auth`` sets ``internal_auth`` and then derives
    ``request["app"]`` IN THE SAME BRANCH, precisely so ownership guards
    downstream can see it -- so an app-owned agent granted the panel tools, whose
    slot's ``agent`` happens to name a crew, satisfied the secret gate and
    published as that crew. The read route already denied app callers; the write
    route asserted in prose that it did not need to.
    """
    # `request.app["state"]`, matching every other handler that vets a session
    # (cron.py, memory.py): the vetting helpers take a non-optional
    # `DashboardState`, and a gateway serving this route without one is a boot
    # bug rather than a request to answer. The previous `.get()` typed this
    # `| None` and passed it straight into both helpers, which is the shape mypy
    # rejects -- and it silently claimed a None state was a servable request.
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    if request.get("internal_auth") is not True:
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="internal secret required",
        )
        return None, web.json_response(
            {"error": "forbidden", "code": "internal_secret_required"}, status=403
        )
    # BEFORE `slot.agent` is read, so an app identity can never be resolved into a
    # crew. `await`: the guard offloads its SEL audit, and an un-awaited coroutine
    # is truthy but never runs -- the failure mode that silently disarmed this same
    # helper on the read route once already.
    denied = await _deny_app_caller(request, operation)
    if denied is not None:
        return None, denied
    refusal = await _recognize_session(
        state, sk, operation, blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return None, refusal
    if _is_restricted_session(state, request):
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
            error="Panel publishing is not allowed in this session mode.",
        )
        return None, web.json_response(
            {
                "error": "A crew webview is not available in this session mode.",
                "code": "restricted_session",
            },
            status=403,
        )

    slot = state.get_slot(_normalize_slot_key(sk))
    crew_name = str(getattr(slot, "agent", "") or "") if slot is not None else ""
    if not crew_name:
        # No agent binding means no crew, and a panel has nowhere to go. Said
        # plainly rather than silently dropped: a conductor publishing every
        # cycle into a void would look like the feature is broken.
        return None, web.json_response(
            {
                "error": (
                    "this session is not bound to a crew, so it has no webview " "to publish to"
                ),
                "code": "no_crew",
            },
            status=400,
        )
    try:
        slug = members_mod.slug_for_name(crew_name)
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return None, web.json_response(
            {"error": "this crew's name has no addressable slug", "code": "bad_crew_slug"},
            status=400,
        )
    return (slug, crew_name), None


async def api_agent_panel_templates(request: web.Request) -> web.Response:
    """GET /api/agent-panel/templates — template ids a crew may publish with.

    Also reports the id this crew gets by default, so the caller does not have to
    know that a template named after the crew wins automatically.
    """
    resolved, refusal = await _resolve_publishing_crew(request, "agent_panel_templates")
    if refusal is not None:
        return refusal
    assert resolved is not None
    _slug, crew_name = resolved
    # Discovery reads the override template directory, which REFUSES a linked or
    # junctioned path rather than following it. That refusal has a code, so hand
    # the code back instead of letting it surface as an opaque 500: unlike a bad
    # template id, this one is the OPERATOR's to fix, and a 500 tells nobody
    # which of the two it was.
    try:
        ids = await asyncio.to_thread(agent_panel.available_templates)
        default = await asyncio.to_thread(agent_panel.template_for_crew, crew_name)
    except agent_panel.PanelError as exc:
        return web.json_response({"error": str(exc), "code": exc.code}, status=400)
    return web.json_response({"templates": ids, "default": default})


async def api_agent_panel_publish(request: web.Request) -> web.Response:
    """POST /api/agent-panel/publish — replace the calling crew's webview."""
    resolved, refusal = await _resolve_publishing_crew(request, "agent_panel_publish")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name = resolved

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    try:
        args = validate_tool_args(body, PANEL_PUBLISH_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "validation_error"}, status=400)

    # An omitted template resolves to the one named after the crew when it
    # exists, so a crew with a template of its own gets that bespoke view
    # without being told to ask for it, and every other crew gets the generic
    # one.
    template = str(args.get("template") or "").strip()
    if not template:
        # Same refusal reaches here: default selection also reads the override
        # directory, and it must not become a 500 on the publish path either.
        try:
            template = await asyncio.to_thread(agent_panel.template_for_crew, crew_name)
        except agent_panel.PanelError as exc:
            return web.json_response({"error": str(exc), "code": exc.code}, status=400)

    # Whether the CURRENT owner of this slug is still a crew that exists. Passed as
    # a callback rather than resolved here, because the store must ask it inside its
    # own lock -- deciding out here would decide on a snapshot the lock has not
    # frozen yet. Answered against the config roster, which is the same source the
    # members routes enumerate, and compared on the ownership DIGEST so no crew name
    # has to be carried around to make the comparison.
    #
    # Without it, strict ownership made a renamed or deleted crew permanent: its
    # record held the slug forever and every later crew reaching that slug was told
    # to "rename one of the crews", which cannot be done when the other crew is gone.
    def _owner_is_live(owner_key: str) -> bool:
        # The roster is read HERE, not hoisted above the call: this runs on the
        # worker thread the store already occupies, inside its lock, so the answer
        # cannot be a snapshot taken before the lock was held. Only reached on the
        # collision path, so the extra read costs nothing on a normal publish.
        cfg = KiroCrewConfig.load()
        return any(
            agent_panel.crew_key(name) == owner_key for name in _member_names_for_slug(cfg, slug)
        )

    try:
        record = await asyncio.to_thread(
            agent_panel.publish,
            slug,
            template=template,
            data=args.get("data") or {},
            title=str(args.get("title") or ""),
            crew=crew_name,
            owner_is_live=_owner_is_live,
        )
    except agent_panel.PanelError as exc:
        # The code travels: these refusals are actionable by the crew that made
        # the call (a bad template id, data over the cap), and it can only
        # correct them on its next cycle if it is told which one fired.
        return web.json_response({"error": str(exc), "code": exc.code}, status=400)
    except MemberSlugError:
        return web.json_response(
            {"error": "this crew has no member space", "code": "bad_crew_slug"}, status=400
        )
    except OSError as exc:
        logger.warning("panel publish failed for crew %s: %s", slug, exc)
        return web.json_response(
            {"error": "could not write the panel", "code": "panel_write_failed"}, status=503
        )
    # Tell open drawers a new document exists.
    #
    # Without this the drawer showed its FIRST read for the rest of the session:
    # the query client sets `staleTime: Infinity` because "freshness is driven
    # exclusively by WebSocket push", and nothing pushed for a panel -- so a crew
    # publishing on an unattended loop was invisible after the first render, which
    # is the one thing this feature exists to do. Push rather than a poll because
    # that is both the query client's stated contract and this page's own idiom:
    # every sibling section on the members page reads WebSocket-fed Redux state, so
    # an interval here would be the only poller on a push-driven page.
    #
    # The frame carries the SLUG ONLY. The ownership digest is deliberately absent
    # (a test pins that it never reaches a client), and it is not needed: the frame
    # only says "re-read this slug", and the read route re-applies the ownership
    # check to whoever asks.
    state: DashboardState = request.app["state"]
    state.broadcast_ws("panel_published", {"slug": slug})
    # The data is not echoed: it is the crew's own input, and a response that
    # repeats a 64 KB payload back into the tool result burns the context this
    # feature exists to save.
    return web.json_response(
        {
            "ok": True,
            "panel": {
                "template": record["template"],
                "title": record["title"],
                "published_at": record["published_at"],
            },
        }
    )


def _read_and_compose(slug: str) -> tuple[dict[str, Any] | None, str | None]:
    """One record read, and the document composed from that same record.

    Both halves of the response come from a single snapshot, so a publish landing
    mid-request cannot pair one version's HTML with another version's summary.
    Runs in a worker thread: two blocking file reads (the record, then the
    template) plus the compose.
    """
    record = agent_panel.read(slug)
    return record, agent_panel.render_record(record)


async def api_member_panel(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/panel — the crew's composed webview document.

    Returned as a JSON string rather than a ``text/html`` body: the drawer feeds
    it to the same srcdoc builder the artifact frames use, which adds the strict
    CSP and the theme variables. Serving it as HTML here would invite loading it
    directly, outside the sandbox that makes it safe to render at all.

    The raw ``data`` object travels alongside the document, and the drawer's
    DOCKED summary is rendered from it natively rather than from the document.
    That is the whole reason it is here: a dashboard needs a full page to be
    legible (four tiles across, multi-column grids), so a ~250px drawer column
    cannot host one -- it clipped the crew's most important line below the fold.
    Reading the summary from the data lets the drawer show the few fields that
    matter, in the order the crew published them, as ordinary escaped text.

    Duplicating the data (it is also inside the document's island) is deliberate
    and bounded: ``publish`` caps it, and the alternative -- parsing it back out
    of the composed HTML -- would make the drawer a consumer of the template's
    markup. Insertion order survives because ``json.dumps`` does not sort keys
    and ``JSON.parse`` preserves the order of non-numeric keys.

    An app token scoped to ``/api/members`` reaches this route by PREFIX -- it is a
    child of that parent -- so app callers are denied explicitly. Apps are isolated
    from member surfaces generally (``handlers/members.py`` denies its three routes
    the same way); a panel is a crew's own published state and a rendered document,
    which is squarely inside what that isolation exists to withhold.
    """
    # ``await``: this guard is a coroutine (it offloads its SEL audit off the
    # event loop). Calling it without awaiting returns a truthy coroutine that
    # never runs, so the deny path silently stops denying -- the rebase that
    # made it async produced no conflict here, only a dead guard.
    denied = await _deny_app_caller(request, "members.panel")
    if denied is not None:
        return denied
    slug = request.match_info.get("slug", "")
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    # ``member`` (query, REQUIRED) is the exact crew name, exactly as
    # ``api_member_activity`` requires it and for the same reason: slugification is
    # lossy, so ``Oncall`` and ``oncall`` reach one slug and therefore one record.
    # ``publish`` refuses the colliding WRITE, which stops one crew overwriting the
    # other -- but with the read keyed on the slug alone the loser of that race
    # still saw the winner's dashboard in its own drawer. Verifying the stored
    # ownership claim here is the other half of the same guard, and making the
    # parameter required makes the mixed read impossible by construction rather
    # than a caller obligation.
    member = request.query.get("member", "")
    if not member or not _AGENT_NAME_RE.match(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )
    # ONE read, both halves. `render` + `read` as separate calls let a publish land
    # between them and returned the old document beside the new summary, so the
    # docked chip and the expanded view could disagree. Composed inside the same
    # worker hop, which also keeps the template resolution off the event loop.
    record, html = await asyncio.to_thread(_read_and_compose, slug)
    # Compared on the DIGEST of the exact name. The stored ``crew`` is the REDACTED
    # display text, so a credential-shaped crew name would never equal the exact
    # name it was redacted from -- that crew could not read its own panel, and two
    # different such crews would look like the same owner.
    #
    # An UNOWNED record is refused, not served. This guard used to require
    # ``owner_key`` to be truthy before comparing, so a record with an empty
    # ``crew_key`` skipped the check and was handed to whoever asked -- a
    # fail-OPEN default on the one guard that keeps a crew's drawer its own. The
    # justification for that was wrong in every branch: this schema has never
    # shipped, so no record predates the ownership field; the publish route
    # refuses a session with no crew binding (``no_crew``), so no caller reaches
    # the store without a name; and :func:`publish` now rejects an empty crew
    # outright. What is left that can produce an unowned record is a forgery,
    # which is exactly what must not render.
    owner_key = str((record or {}).get("crew_key") or "")
    if record is not None and (not owner_key or owner_key != agent_panel.crew_key(member)):
        # Another crew owns this slug's record. Reported as "nothing published"
        # rather than as a refusal: from this crew's point of view it HAS no panel,
        # and naming the other crew would disclose a colliding name the viewer of
        # this drawer has no other way to learn.
        return web.json_response({"panel": None, "html": None})
    if html is None:
        # One code for "this crew never published" and "its template no longer
        # renders": both mean there is nothing to show, and the drawer's empty
        # state is the same either way.
        return web.json_response({"panel": None, "html": None})
    data = (record or {}).get("data")
    return web.json_response(
        {
            "panel": {
                "template": str((record or {}).get("template") or ""),
                "title": str((record or {}).get("title") or ""),
                "crew": str((record or {}).get("crew") or ""),
                "published_at": str((record or {}).get("published_at") or ""),
                # ``read`` already refuses a record whose data is not an object,
                # so this is a dict or the record was rejected; the guard is for
                # the rejected case rather than for a shape the store allows.
                "data": data if isinstance(data, dict) else {},
            },
            "html": html,
        }
    )


def register_agent_panel_routes(app: web.Application) -> None:
    app.router.add_get("/api/agent-panel/templates", api_agent_panel_templates)
    app.router.add_post("/api/agent-panel/publish", api_agent_panel_publish)
    # The drawer's read. NOT under /api/agent-panel: that prefix is
    # strict-internal (MCP-only), and this one is called by the browser.
    app.router.add_get("/api/members/{slug}/panel", api_member_panel)
