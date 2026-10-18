"""The publishing half of a crew's webview: which crew a caller may write, and the routes.

``_resolve_publishing_crew`` vets an ``/api/agent-panel/*`` caller and resolves it to the
crew whose panel it may write, or, for the dynamic dashboard's routes, to a root session's
own page. ``GET /api/agent-panel/templates`` and ``POST /api/agent-panel/publish`` are the
publish routes; the facade's docstring states why their auth differs from the drawer's.

:mod:`kiro_crew.dashboard.handlers.agent_panel` is this module's facade: callers and tests
import and patch every name through it, and a write there -- ``monkeypatch.setattr``,
``mock.patch`` -- reaches this module too (:mod:`kiro_crew._facade_mirror`). This module
never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.agent_panel_read import (
    _card_slot,
    _panel_record,
    _panel_slot,
    _publish_derived_card,
)
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.handlers.members import _deny_app_caller, _slug_is_claimed_by_any_member
from kiro_crew.dashboard.handlers.session_ledger import _session_unit
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.history import is_incognito_transcript
from kiro_crew.members import MemberSlugError
from kiro_crew.sel import sel
from kiro_crew.session_ledger import _APPEND_FLUSH_SECONDS
from kiro_crew.validation import PANEL_PUBLISH_SCHEMA, ValidationError, validate_tool_args

logger = logging.getLogger("kiro_crew.dashboard.handlers.agent_panel")


def _live_session_key(state: DashboardState, sk: str) -> str:
    """The key the SESSION REGISTRY holds this caller under, or ``""``.

    Two keyspaces one prefix apart, and only this function is allowed to know
    it. Dashboard SLOTS are keyed by the bare name -- what
    :func:`_normalize_slot_key` produces, since it strips the transport prefix --
    while the session REGISTRY is keyed by the full session key the thread runs
    under, ``dashboard:<slot key>`` (:func:`members.member_thread_session_alias`,
    the one derivation every out-of-turn touch of a member session goes
    through). A member DM is the only session the panel tool is ever mounted on,
    so handing the slot key to the registry misses for EVERY member, without
    exception: ``get_agent_selection`` then answers from its own ``session is
    None`` arm with ``("template", "")``, and the publish is refused for a reason
    that is not about crew binding at all.

    ``X-Session-Key`` is used unchanged, because it already IS that full key:
    every identity source ``mcp_core._resolve_session_key_strict`` accepts -- the
    gateway-injected caller context, the signed per-session token,
    ``KIROCREW_SESSION_KEY``, the HMAC host-pid sidecar -- yields one, and that
    gate requires its caller to send back the key it returned. Passing it through
    is also what every other reader of an allocation's selection does
    (``messaging``, ``solo_spawn``, ``subagent`` and the admission gate all hand
    over the session key as they received it).

    A bare slot name therefore resolves to nothing and the publish is refused
    ``session_not_resolved``. That refusal is the point rather than a gap to
    paper over: a bare key here would mean the strict identity gate returned
    something this route does not expect, and rescuing it by re-adding the prefix
    would hide exactly the anomaly the separated refusal exists to surface.
    """
    return sk if state.sessions.has_session(sk) else ""


async def _resolve_publishing_crew(
    request: web.Request, operation: str, *, root_session: bool = False
) -> tuple[tuple[str, str], None] | tuple[None, web.Response]:
    """Vet the caller and resolve it to the crew whose panel it may write.

    Returns ``((slug, crew_name), None)`` or ``(None, refusal)``.

    With *root_session*, a caller bound to no crew that is a ROOT session resolves to
    ``(session store key, "")``: its own dashboard instance, keyed by its own slot. The
    dynamic dashboard's routes pass it; the panel publish routes do not, so a crew
    webview is still published only from a crew's own thread.

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
    # The operator ceiling, read HERE and synchronously, right before the act.
    # The mount sites read it too, but a mount answers only for a session being
    # established: a member whose session was already running when the switch
    # flipped still holds the grant, and nothing short of ending that session
    # would take it back. ``agent.crew_panel``'s own description promises the
    # withdrawal reaches "every member at once", and the two sibling switches in
    # this subsystem keep that promise the same way -- ``session_control_parts/caller_access.py``
    # reads them at the gate rather than at mount time. Read through
    # ``crew_panel_enabled``, so an unreadable or degraded config fails closed
    # here exactly as it does at the mount.
    if not await asyncio.to_thread(members_mod.crew_panel_enabled):
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="agent.crew_panel is off",
        )
        return None, web.json_response(
            {"error": "the crew dashboard is switched off", "code": "crew_panel_disabled"},
            status=403,
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
    # Through the allocation's own selection rather than ``slot.agent``, because
    # those two answer different questions. ``slot.agent`` is a NAME; a session
    # that selected the provider TEMPLATE of the same name carries the identical
    # string, so reading it as a crew binding lets such a session publish into --
    # and overwrite -- the panel of the crew it happens to share a name with.
    # ``get_agent_selection`` reports the namespace the allocation actually chose
    # and is the only caller-side way to tell a member from a template, so a
    # binding is accepted only when it says ``member``.
    #
    # Asked with the key the REGISTRY holds the session under, which is not the
    # slot key -- see ``_live_session_key`` for the two keyspaces.
    #
    # Three distinct refusals, because they have three distinct causes and one
    # message for several of them is the defect this whole change removes. The
    # slot is checked FIRST and answers for itself: its absence is what confines
    # publishing to a dashboard thread, so a live non-slot session (a subagent
    # inheriting its parent's member selection) cannot publish as the crew it
    # descends from. Such a caller's allocation resolves perfectly well, so
    # telling it the allocation could not be resolved would be false.
    if slot is None:
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="caller has no dashboard slot",
        )
        return None, web.json_response(
            {
                "error": (
                    "a crew webview is published from the crew's own dashboard thread, "
                    "and this session is not one"
                ),
                "code": "no_dashboard_slot",
            },
            status=400,
        )
    crew_name = ""
    unresolved = True
    session_key = _live_session_key(state, sk)
    if session_key:
        try:
            namespace, selected = state.sessions.get_agent_selection(session_key)
        except ValueError:
            # The allocation's own refusal when a parent selection is
            # unavailable -- a resolution failure, so it is reported as one.
            # Narrow on purpose: a bare ``except Exception`` here turned a WRONG
            # ATTRIBUTE into a routine "not bound to a crew" and would have
            # refused every publish in production while the tests passed against
            # a stub that happened to define the method.
            namespace, selected = "", ""
        else:
            unresolved = False
            if namespace == "member":
                crew_name = str(selected or "")
    if unresolved:
        # NOT ``no_crew``: the caller may well be a crew, and its allocation is
        # what could not be reached to find out. Reported apart because the two
        # need opposite responses -- a crew binding is the OPERATOR's to add,
        # while an unreachable allocation is a gateway-side fault -- and one
        # message for both is what let a gate closed against every member read
        # as a routine "you have no crew".
        #
        # Audited, like every other refusal here. ``_recognize_session`` has
        # already written an ``outcome="allowed"`` event for this call, so a
        # denial that returns without its own event leaves the SEL trail ending
        # on the ALLOW: the record would say the caller was let through and the
        # HTTP response would be the only trace that it was not.
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="caller's allocation could not be resolved",
        )
        return None, web.json_response(
            {
                "error": (
                    "this session could not be resolved to a live allocation, "
                    "so its crew binding is unknown"
                ),
                "code": "session_not_resolved",
            },
            status=400,
        )
    if not crew_name and root_session:
        # A ROOT session writing ITS OWN page. Only the dynamic dashboard's routes ask
        # for this, and every gate above has already passed: the internal secret, the
        # operator switch, the app-caller denial, the strict session identity, the
        # restricted-mode block and the slot check.
        #
        # The page is named by the CALLER's own slot and never by the body, so a session
        # can reach no instance but its own. A dispatched or adopted session is refused
        # here: its records belong to whoever dispatched it, and the side panel shows it
        # no page to write to. The root test is `is_root_session`, the one the
        # automatic card applies, which fails closed on an unseeded session tree.
        from kiro_crew.dashboard.card_lifecycle import is_root_session
        from kiro_crew.dashboard_templates import instance as instance_store

        if is_root_session(slot):
            return (instance_store.session_instance_key(str(slot.key)), ""), None
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="caller is not a root session and not bound to a crew",
        )
        return None, web.json_response(
            {
                "error": (
                    "only a root session or a crew has a dashboard to write to; this "
                    "session was dispatched or adopted, so its page is its dispatcher's"
                ),
                "code": "not_root_session",
            },
            status=403,
        )
    if not crew_name:
        # No agent binding means no crew, and a panel has nowhere to go. Said
        # plainly rather than silently dropped: a conductor publishing every
        # cycle into a void would look like the feature is broken.
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="caller is not bound to a crew",
        )
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
        # Through ``member_slug`` and NOT ``slug_for_name``, because the two
        # disagree exactly where it matters. ``member_slug`` returns the crew's
        # persisted ``member_id`` when it has one, and memory provisioning
        # deliberately suffixes that id when a deleted crew's stores are still
        # held under the name-derived slug. Publishing by name in that state
        # writes a record keyed differently from the one the drawer and roster
        # read (``members.py`` resolves every read through ``member_slug``), so
        # the crew's dashboard would be written and then never found.
        # Config is read off the loop, as the rest of this surface does.
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        slug = members_mod.member_slug(crew_name, cfg)
        members_mod.validate_slug(slug)
    except MemberSlugError:
        sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources=request.path,
            error="crew name has no addressable slug",
        )
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
    # Read once, up front: the append needs it to resolve the calling session's unit
    # and the broadcast needs it to tell open drawers. ``request.app["state"]``
    # rather than ``.get()``, matching ``_resolve_publishing_crew`` -- a gateway
    # serving this route without a state is a boot bug, not a request to answer.
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
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
    # Without it, strict ownership makes a renamed or deleted crew permanent: its
    # record holds the slug forever and every later crew reaching that slug is told
    # to "rename one of the crews", which cannot be done when the other crew is gone.
    def _owner_is_live(owner_key: str) -> bool:
        # The roster is read HERE, not hoisted above the call: this runs on the
        # worker thread the store already occupies, inside its lock, so the answer
        # cannot be a snapshot taken before the lock was held. Only reached on the
        # collision path, so the extra read costs nothing on a normal publish.
        cfg = KiroCrewConfig.load()
        # A degraded load answers a DIFFERENT question than an empty roster does.
        # ``load()`` returns a defaults-only config when the file is unreadable, so
        # "no crew holds this slug" and "we could not read which crews exist" arrive
        # as the same empty enumeration -- and read as absent, which hands the
        # colliding publish a takeover of a live crew's record. Treated as live so
        # the unreadable case refuses the takeover instead of granting it.
        if cfg.degraded_sections:
            return True
        # Through the liveness enumeration, NOT the addressability one: the create
        # route validates a crew name only against the credential-shape check, so a
        # name the agent-name grammar rejects -- "On call", with a space -- is a real
        # crew that derives this slug. Asking the addressable list would drop it,
        # report its owner as gone, and hand the colliding publish its record.
        return _slug_is_claimed_by_any_member(cfg, slug, owner_key)

    # THE FILE IS THE DURABLE RECORD and is written first, which is what keeps this
    # route working on a gateway with the crew log off -- the default. The crew log
    # entry below is an ADDITIONAL record: it is what gives a panel a history and
    # keeps two crews on one slug from hiding each other's, and it is appended to a
    # session's unit, which retention may collect. A panel outlives any one session,
    # so the log cannot be its only home.
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
    except agent_panel.CrewSlugError as exc:
        # The record path itself is unusable -- the store refuses to write through
        # a symlink or junction standing where the record belongs, because that is
        # how a write reaches an inode outside the fenced directory.
        #
        # Coded rather than left to surface as a 500: the crew cannot fix this and
        # neither can the drawer, so an opaque error tells the one party who CAN
        # (the operator, who has to remove the link) nothing about what happened.
        logger.warning("panel record path unusable for crew %s: %s", slug, exc)
        return web.json_response(
            {"error": str(exc), "code": "panel_record_is_a_symlink"}, status=400
        )
    except MemberSlugError:
        return web.json_response(
            {"error": "this crew has no member space", "code": "bad_crew_slug"}, status=400
        )
    except OSError as exc:
        logger.warning("panel publish failed for crew %s: %s", slug, exc)
        return web.json_response(
            {"error": "could not write the panel", "code": "panel_write_failed"}, status=503
        )

    # The additional record, and BEST-EFFORT by design: the publish has already
    # succeeded and the drawer already has a panel to show, so a crew log that is
    # off, has no unit for this session yet, or refuses the line costs this publish
    # its history row and nothing else. Failing the request here would make the
    # feature's durable half hostage to its observational half.
    #
    # The unit is the CALLING session's own, which is the member's DM session: the
    # panel tool is mounted nowhere else, so the entry lands on slot
    # ``member-<slug>`` and the drawer's slug-keyed read folds it without any
    # binding of its own.
    if crew_log_emit.enabled():
        # The ENTRY's own fields, not the stored document's. The file carries two
        # members the entry type does not declare: ``schema``, which versions the
        # file format, and ``published_at``, which the fold derives from the entry's
        # envelope ``time`` so no line can claim a publish time the log disagrees
        # with. Appending the document whole is refused as ``bad_data_field``, which
        # costs the panel its history while the publish itself reports success.
        entry = {
            "template": str(record.get("template") or ""),
            "data": record.get("data") or {},
            "title": str(record.get("title") or ""),
            "crew": str(record.get("crew") or ""),
            "crew_key": str(record.get("crew_key") or ""),
        }

        def _append() -> bool:
            unit = _session_unit(state, sk)
            if not unit:
                return False
            # Asked BEFORE the append, because an entry over the line ceiling can
            # never land and would otherwise be counted as a dropped write.
            if not crew_log_emit.panel_entry_fits(entry):
                return False
            return crew_log_emit.on_panel_published(unit, entry, timeout=_APPEND_FLUSH_SECONDS)

        if not await asyncio.to_thread(_append):
            # Logged, not returned: the file carries this publish and the read
            # prefers whichever record is newer, so the panel this call wrote IS
            # what a reader gets. What is lost is the history row for this cycle.
            logger.warning("panel history not recorded for crew %s; the panel was written", slug)
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
    state.broadcast_ws("panel_published", {"slug": slug})
    # A PUBLISH is the board-changing event, so it is what produces the card.
    #
    # Building it only on the drawer read left the card with no producer of its own: the one
    # surface that created it is the surface this work exists to replace, so once the drawer's
    # per-crew rendering goes, nothing would ever mint a board card and nobody reading the
    # dashboard could tell an absent board from a crew that published nothing. A publish is
    # also the moment the answer actually changed, which a read is not.
    #
    # Rebuilt through the SAME reader the drawer uses rather than from the response above: the
    # response carries three display fields, while the card needs the derived board, which
    # means the same fold read and the same provider. Failing to build it must not fail the
    # publish -- the panel is already stored, and the next read rebuilds the card -- so the
    # whole step is best-effort like the history append above, and it EVICTS when the record
    # this publish just wrote carries no board.
    #
    # ``owner_key`` comes off that record rather than being recomputed, so the card is built
    # for the crew whose publish this is. The slot resolution and the fold read both belong in
    # a worker thread; the store call belongs on the loop.
    published_owner = str(record.get("crew_key") or "")

    def _rebuild_card() -> tuple[str, dict[str, Any] | None]:
        # ONE config load for both derivations. The fold is read off the PANEL slot, which
        # every crew reaching this slug shares; the card is stored on the slot that names
        # this crew alone, which on a shared key is no slot at all.
        cfg = KiroCrewConfig.load()
        panel_slot = _panel_slot(cfg, crew_name, slug)
        return _card_slot(cfg, crew_name, slug), _panel_record(panel_slot, slug, published_owner)

    try:
        card_slot, card_record = await asyncio.to_thread(_rebuild_card)
        # Both arguments say the same thing about this caller from two directions.
        # ``published_owner`` is this publish's OWN ownership digest, so a record the rebuild read
        # for some other crew stores nothing -- this route has no ownership refusal of its own,
        # being the writer, which is why the check travels with the write. AUTHORITATIVE, because
        # this is the record the request just wrote: at an equal revision it outranks a panel
        # read's snapshot of unknown age.
        _publish_derived_card(state, card_slot, card_record, published_owner, authoritative=True)
    except Exception:
        logger.warning(
            "could not refresh the board card for crew %s after publish", slug, exc_info=True
        )
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
