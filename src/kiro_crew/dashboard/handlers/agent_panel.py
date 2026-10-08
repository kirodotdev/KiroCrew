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
import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final, cast

from aiohttp import web

from kiro_crew import agent_panel, dashboard_agentic
from kiro_crew import members as members_mod
from kiro_crew import pipeline_board_contract
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection
from kiro_crew.crew_log.entry_types import PANEL_FOLD_NAME
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.handlers.members import (
    _deny_app_caller,
    _member_names_for_slug,
    _member_thread_slot,
    _slug_is_claimed_by_any_member,
)
from kiro_crew.dashboard.handlers.session_ledger import _session_unit
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.history import is_incognito_transcript
from kiro_crew.members import MemberSlugError, is_readable_member_name
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.sel import sel
from kiro_crew.session_ledger import _APPEND_FLUSH_SECONDS
from kiro_crew.validation import (
    DASHBOARD_WRITE_SCHEMA,
    PANEL_PUBLISH_SCHEMA,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.work_vocab import WORK_FOLD_NAME, WorkBoardView

logger = logging.getLogger(__name__)


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
    # The operator ceiling, read HERE and synchronously, right before the act.
    # The mount sites read it too, but a mount answers only for a session being
    # established: a member whose session was already running when the switch
    # flipped still holds the grant, and nothing short of ending that session
    # would take it back. ``agent.crew_panel``'s own description promises the
    # withdrawal reaches "every member at once", and the two sibling switches in
    # this subsystem keep that promise the same way -- ``session_control.py``
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


def _panel_slot(cfg: KiroCrewConfig, member: str, slug: str) -> str:
    """The DM slot a crew's panel fold lives on.

    The same derivation ``api_member_thread`` uses to CREATE that thread, so it names
    the slot the publishing session actually runs under -- a member's DM session is
    the only place the panel tool is mounted. Derived rather than looked up so a crew
    whose thread is not running still reads its last panel.

    The fallback is the V1 key, taken when the memory-store resolution refuses: an
    unknown member, or a member identity whose V2 store record is missing or
    degraded. Blanking a published panel because a store record is unreadable would
    make an unrelated degradation look like the crew never published, and the
    fallback cannot serve another crew's record -- the slug is part of both keys, and
    the ownership digest is re-checked on whatever is read.
    """
    try:
        slot, _store = _member_thread_slot(cfg, member, slug)
    except UnknownMemoryStore:
        return members_mod.member_slot_key(slug)
    return slot


#: Slugs already warned about, so the shared-key refusal is reported once rather than on every
#: panel read.
#:
#: BOUNDED TWICE, because one bound was an inference and inferences are what this file keeps
#: getting wrong. ``slug`` arrives on the REQUEST PATH, so "bounded by the roster" was only true of
#: the slugs a configured crew actually claims -- a caller asking about slugs nobody owns grew this
#: set for free. So: nothing is recorded unless two configured crews really do collide (below), and
#: the set is CLEARED at a named ceiling in case a long-lived process sees many rosters through hot
#: config edits.
_SHARED_KEY_WARN_CAP: Final[int] = 256

_SHARED_KEY_LOGGED: set[str] = set()


def _card_slot(cfg: KiroCrewConfig, member: str, slug: str) -> str:
    """The slot a crew's board card may be stored on, or ``""`` when no slot names it.

    The panel slot is not always enough. A V2 member's key carries its private store
    generation, which is unique to that member, so the key names exactly one crew. The V1
    key is the slug alone, and slugification is many-to-one -- ``Oncall`` and ``oncall``
    reach one slug, and two agents may even carry one ``member_id`` -- so on V1 a single
    key is every colliding crew's key at once.

    The FOLD survives that, keeping a record per ownership digest, and the panel routes
    survive it by checking the digest on whatever they read. The card store cannot: it is
    keyed on the slot, and the slot carries no crew of its own, only a per-session
    identity. So a card built from one crew's record, stored on a shared key, is served by
    the owner-only card route as the OTHER crew's board -- one crew's state read under
    another's name, which is the defect :func:`kiro_crew.agent_panel.publish` exists to
    prevent, reappearing on the surface that has no digest to check.

    Answered by asking which configured crews reach this key, rather than by reading a
    binding off the slot, because there is no such binding to read. Exactly one is the
    only safe answer: two means the key is shared, and none means no crew in this config
    claims it, so nothing here can say whose board it would be.

    Refusing costs the CARD and nothing else -- the drawer keeps its own slot and both
    crews keep their panels -- and the ambiguity is a configuration a person can end by
    renaming, which is the remedy ``member`` collisions already carry elsewhere.
    """
    key = _panel_slot(cfg, member, slug)
    if not key:
        return ""
    seen = 0
    for name in getattr(cfg, "agents", {}) or {}:
        try:
            other = _panel_slot(cfg, name, members_mod.member_slug(name, cfg))
        except Exception:
            # A crew whose identity cannot be resolved claims no key. Skipped rather than
            # counted: counting it would refuse every card in a config holding one
            # degraded member, and treating a failure as a claim on THIS key is a guess
            # about the very thing that could not be read.
            continue
        if other == key:
            seen += 1
    # ONE decision, in one place. An early exit on the second claimant was here and no test
    # could tell it from this line, because it answered the same question a loop iteration
    # sooner: a crew roster is small, so it bought nothing a reader had to reason about.
    if seen == 1:
        return key
    # LOGGED ONLY FOR A REAL COLLISION, and that is the memory bound as much as it is the right
    # message. Two or more configured crews reaching one key is the case the remedy fits: rename
    # one. ZERO claimants is a different thing -- a slug this config does not hold, which every
    # caller can ask about freely -- so advising a rename there would be wrong AND would let a
    # request path decide what this set retains.
    #
    # Once per slug rather than per call, because a panel read happens on every drawer open and a
    # warning per read is a log nobody reads. The remedy travels in the message, since whoever
    # meets the line has no reason to come looking for this docstring.
    if seen > 1 and slug not in _SHARED_KEY_LOGGED:
        if len(_SHARED_KEY_LOGGED) >= _SHARED_KEY_WARN_CAP:
            # CLEARED, not evicted by age. There is no order worth maintaining here and the cost of
            # clearing is one repeated warning, which is the harmless direction; the alternative is
            # either dropping warnings forever or keeping a list that grows.
            _SHARED_KEY_LOGGED.clear()
        _SHARED_KEY_LOGGED.add(slug)
        logger.warning(
            "no board card for member slug %s: %d configured crews resolve to one slot key, so "
            "the key cannot say whose board a card on it would be. Rename one crew, or give it a "
            "private memory store, to separate them.",
            slug,
            seen,
        )
    return ""


#: How long a pipeline board's newest work entry may be before the header calls it
#: stale. A HOST value: the fold cannot know it and the publisher must not decide when
#: its own board stops counting as current.
#:
#: About TEN patrol intervals -- the conductor skill arms its patrol near 90 seconds --
#: rather than one plus slack. One interval would call a board stale the moment a single
#: cycle did no work, which is the normal quiet cycle and not news; ten means the log has
#: been silent across many cycles, and a fleet nobody has heard from in a quarter of an
#: hour is the thing a reader needs told.
BOARD_STALE_AFTER_SECONDS: Final[int] = 900


def _panel_record(slot: str, slug: str, owner_key: str) -> dict[str, Any] | None:
    """The crew's panel record, with a contract template's NUMBERS taken from the log.

    Two steps: pick the record (:func:`_published_record`, whose selection rules are
    their own story), then, for the one template that has a declared contract, replace
    its data with :func:`~kiro_crew.pipeline_board_contract.build_pipeline_board`'s
    output. Everything else is served exactly as published.

    Done HERE rather than in the drawer because both surfaces read this one record: the
    composed document carries it in its data island, and the docked native summary is
    rendered from the same ``data`` object travelling beside it. A frontend fix would
    have to be made twice and could not be made at all in the document -- its srcdoc
    runs on a null origin under ``connect-src 'none'``, so it can never fetch anything.
    """
    record = _published_record(slot, slug, owner_key)
    if record is None:
        return record
    if str(record.get("template") or "") != pipeline_board_contract.BOARD_TEMPLATE_ID:
        return record
    return _with_board_numbers(slot, record)


def _with_board_numbers(slot: str, record: dict[str, Any]) -> dict[str, Any]:
    """*record* with its data rebuilt from the ``work`` fold, or *record* unchanged.

    UNCHANGED is the answer for a board that is not there. A crew whose work fold is
    absent, empty or unreadable must not be handed a complete board of zeros: zero
    items is a fact about a board that exists, and "no board" is a different one. Left
    as published, the template's three-state renderer reads the missing sections as
    ABSENT and says "not said", which is the true statement.

    The fold is read through the ordinary slot-keyed projection -- the same warm kernel
    the panel fold above uses, so a second drawer open folds no entry again and this
    route keeps no cache of its own. It is the member's OWN DM slot both times: a
    conductor's board binds to the slot its conductor entries name, which for a crew
    publishing its own panel is that same DM slot.

    A publisher that wrote the free shape is not an error to the reader -- the author
    is gone and the payload is already on disk -- so its unusable keys are dropped, the
    derived numbers are rendered, and ``contract_replaced`` names what was dropped. A
    publisher quietly overriding the log is the lie this whole contract exists to stop,
    so being overridden has to leave a mark.
    """
    try:
        view = projection.read_slot_projection(slot, WORK_FOLD_NAME).value
    except Exception:
        # Same totality contract as the panel fold above: a damaged log reads as
        # "nothing folded" rather than as a 500, and WARNING because a panel whose
        # numbers silently stopped updating is a crew-visible symptom with no other
        # trace, reproduced on every read until an operator repairs the log.
        logger.warning("work fold unreadable for slot %s", slot, exc_info=True)
        # MARKED, because "I could not derive the board" and "there is no board" are different
        # facts that the record alone cannot separate: both leave the published data in place.
        # For the drawer they are the same answer -- the three-state renderer says "not said",
        # which is true of both -- but for the CARD they are opposite instructions. An absence is
        # authoritative and evicts; a failure must leave the last good card alone, or one
        # unreadable fold deletes a valid card, broadcasts its removal, and does it again on
        # every read until an operator repairs the log.
        #
        # Withheld from the response by ``_PANEL_WITHHELD_KEYS``, like ``board`` and ``card``.
        unreadable = dict(record)
        unreadable["board_unreadable"] = True
        return unreadable
    if not _is_work_board(view):
        return record
    try:
        judgment = pipeline_board_contract.validate_judgment(record.get("data"))
        replaced: list[str] = []
    except pipeline_board_contract.JudgmentError as exc:
        judgment = pipeline_board_contract.EMPTY_JUDGMENT
        replaced = sorted(record.get("data") or {}) if isinstance(record.get("data"), dict) else []
        logger.warning(
            "panel data for slot %s is not a board judgment (%s); rendering the log's "
            "own numbers and dropping the published keys %s",
            slot,
            exc,
            replaced,
        )
    panel = pipeline_board_contract.build_pipeline_board(
        # THE CAST'S LIMIT, stated rather than left to be assumed. ``Projection.value``
        # is ``Any``, so this asserts the shape instead of checking it -- mypy proves
        # the PROVIDER reads only fields ``WorkBoardView`` declares, and that
        # ``_work_render`` writes exactly them, but nothing type-checks that this
        # value came from that renderer. ``_is_work_board`` above is the runtime half
        # that makes the assertion safe in the direction that bites: a value that is
        # not a board at all is refused before it reaches here.
        cast("WorkBoardView", view),
        judgment,
        name=str(record.get("crew") or ""),
        captured_at=str(record.get("published_at") or ""),
        stale_after_seconds=BOARD_STALE_AFTER_SECONDS,
        now_epoch=time.time(),
    )
    out = dict(record)
    # THE CARD, built here because this is the one place the board exists: the fold has
    # been read, the publisher's judgment validated and the provider run, so the only
    # thing left is the flattening -- and doing it anywhere else would mean reading the
    # fold a second time.
    #
    # WITHHELD from the drawer response like ``board`` is, and handed to the dynamic-card
    # store by the async caller rather than from here: this function runs in a worker
    # thread and the store is loop-owned, so the hop belongs at the boundary that already
    # exists. No model is called on this path and none can be: ``panel_card_data`` is
    # arithmetic over a TypedDict.
    try:
        out["card"] = {
            "html": _card_page(),
            "data": pipeline_board_contract.panel_card_data(panel),
        }
    except OSError:
        # A page the build did not ship, or one that cannot be read. The CARD is optional and
        # the PANEL is what this route owes its caller, so this degrades to no card rather than
        # to a 500 on somebody's drawer -- the record is returned without a ``card`` key, which
        # the publisher reads as "evict", the same as any other board that is not there.
        #
        # Caught HERE rather than left to the caller: ``_panel_record`` runs outside
        # ``_read_and_compose``'s own ``try``, and the route catches only ``MemberSlugError``
        # and ``ValueError``, so an OSError from the page read would have escaped as a 500 --
        # which is what ``_card_page``'s docstring already claimed does not happen.
        logger.warning("the board card page could not be read for slot %s", slot, exc_info=True)
    # A SIBLING key, and ``data`` is left exactly as published.
    #
    # The two surfaces want different things from this record. The document renders the
    # contract, so the composer reads ``board``. The drawer's DOCKED card is native
    # React that walks ``data``'s own key order and prints the first entries as headline
    # tiles -- so putting the derived board in ``data`` made a conductor's compact card
    # lead with "contract version 1" and "omitted 0", which is the least interesting
    # pair of numbers on it. The publisher's judgment is what belongs in a one-line
    # card: it is the sentence a person wrote.
    out["board"] = pipeline_board_contract.panel_payload(panel)
    if replaced:
        # On the RECORD, and deliberately not on the read route's JSON response: the
        # override has to leave a mark a reader of this record can find, but a response
        # field nothing renders is a field with no reader. The warning above is what
        # reaches the operator, who is the party that can act on it.
        out["contract_replaced"] = replaced
    return out


def _publish_derived_card(
    state: Any,
    slot_key: str,
    record: Mapping[str, Any] | None,
    expected_owner: str,
    authoritative: bool = False,
) -> None:
    """Hand the board's card to the dynamic-card store, if this record carries one.

    *expected_owner* is the ownership digest of the crew this slot's card may describe, and it
    is REQUIRED rather than defaulted so a future caller cannot omit it and reopen the hole
    below. ``_published_record`` answers with the slug-keyed file, unfiltered, when the fold
    holds nothing under the reading digest -- and one slug can carry two crews. The panel READ
    route is safe by ordering, refusing a foreign record before it reaches here; the publish
    route had no such refusal, so a colliding crew's board could be stored on the live slot and
    then served by the owner-only card route. Checked here rather than at one call site because
    the refusal belongs with the write it protects.

    A mismatch stores nothing AND evicts nothing: evicting on a foreign record would be the
    same bug facing the other way, letting a colliding crew delete the owner's card.

    *authoritative* says this caller holds the record it just wrote, rather than a snapshot of
    unknown age. Only the publish route does, and the store uses it to break a tie between two
    records that share one second-granularity ``published_at``.

    An EMPTY *slot_key* means no slot names this crew alone -- see :func:`_card_slot` --
    and nothing is written or removed. Taken before the absent-record branch, because a
    retirement is a write too: on a shared key, one crew's missing panel would drop the
    other crew's card.

    ON THE EVENT LOOP, after the worker hop: the store is loop-owned and
    :func:`_with_board_numbers` runs in a thread, so this is the boundary that already
    exists rather than a new threadsafe call added inside it.

    TOTAL. A card that cannot be stored -- no store bound yet, a slot that is not running,
    a payload the host refuses -- must not turn somebody's drawer read into a 500: the
    panel response is what this route owes the caller, and the card is a side effect of
    having built the board anyway.
    """
    if not slot_key:
        # NO SLOT NAMES THIS CREW ALONE. Neither store nor evict: the store is keyed on the
        # slot and the slot carries no crew, so a write here would land on whatever crew the
        # shared key currently presents, and a removal would drop that crew's card. Above
        # the ownership check below rather than beside it, because that check reads the
        # RECORD's owner -- it answers whether the record is ours, never whether the slot is.
        return
    if getattr(state, "_slots", {}).get(slot_key) is None:
        # THE SLOT IS GONE, so neither a store write nor a RETIREMENT belongs here. This runs a
        # worker hop after the read that produced *record*, and ``push_slot_removed`` can clear
        # the slot in that gap: it already evicts the slot's derived card AND drops its
        # retirement stamp as the one place that knows the removal is definitive. A retirement
        # written here after that close has no later publish to clear it -- ``forget_retired``
        # only fires on a subsequent publish, and a gone slot has none -- so each closed slot
        # that lost this race would leave one orphan ``(key, stamp)`` pair in ``_retired`` for
        # the life of the process. Refusing before BOTH retirement branches (absent-record and
        # no-card) and the store write keeps every retained field bounded: a slot that is gone
        # has no card to serve and nothing to order a late write against.
        return
    if record is None:
        # NO RECORD AT ALL is the board being gone, not a record about somebody else -- the crew
        # never published, or its panel has been removed. Taken BEFORE the ownership check
        # below, because that check reads a crew key off the record and a record that is not
        # there has none: routed through it, an absent panel matched nothing, returned early,
        # and left the previous card served as `status: published` -- a board that is gone,
        # presented as current.
        #
        # Retired with NO revision, which the store reads as "do not order this": there is no
        # stamp to order by, and inventing one would let an absent panel outrank a real board.
        #
        # ORDERED BY WHO IS ASKING, because with no stamp nothing else can order it. The publish
        # route is AUTHORITATIVE here: it just wrote this record, so an absent one means the crew
        # really retracted its board, and the retirement stands. The panel-read REFRESHER is not:
        # its record is a fresh read, but the retirement runs a worker hop later on the event
        # loop, so a publish can store a newer board in between -- and a stampless removal would
        # then drop that live board, presenting a gone card the reader cannot tell from current.
        # So a non-authoritative absent read leaves a held card ALONE; the next authoritative
        # publish (or an owner change) retires it, and nothing live is dropped on a race. A board
        # genuinely gone with no publish behind it is retired by the record-present path below,
        # which carries the record's own stamp and orders correctly.
        cards = getattr(state, "_dynamic_cards", None)
        if cards is not None and (authoritative or cards.derived.get(slot_key) is None):
            cards.retire_derived(slot_key, "", authoritative=authoritative)
        return
    if not expected_owner or str(record.get("crew_key") or "") != expected_owner:
        # BEFORE anything reads the card or touches the store: see the docstring. Neither store
        # nor evict, because this record is not about this slot's crew at all.
        return
    if record.get("board_unreadable"):
        # The fold could not be READ, which is not the board being gone. Leave the stored card
        # exactly as it is: a board that cannot be derived for a moment is not a board that
        # changed, and the same reasoning already governs a payload the host refuses.
        return
    card = record.get("card")
    cards = getattr(state, "_dynamic_cards", None)
    make_store = getattr(state, "ensure_dynamic_card_store", None)
    if cards is None and callable(make_store):
        # THE STORE IS CREATED FOR A DERIVED CARD TOO, and this is what makes the "a derived
        # card is free, so the cost opt-in does not gate it" claim actually true. The store
        # itself was only ever constructed on the first `set_dynamic_cards_enabled(True)`, so
        # availability depended on TOGGLE HISTORY: an owner who never enabled model-written
        # cards got no board either, and one who enabled them once and turned them off kept
        # getting one. Same feature, opposite answers, decided by a switch neither answer is
        # about.
        #
        # ``enabled=False`` is the whole point: this constructs the container and starts no
        # worker, spends no attempt and calls no model. The model path stays exactly as
        # opt-in as it was.
        #
        # BUILT FOR A RETIREMENT AS WELL AS A CARD, and not as a convenience: a retirement
        # records the stamp that orders everything arriving after it, so with no store there is
        # nowhere to put it and the retirement is simply lost. A delayed read still holding the
        # pre-retirement board then creates the store itself and publishes that board as
        # current. Deciding whether a store is needed from the record's CONTENT is what left
        # that gap -- ordering is a property of the record stream, not of the payload -- so the
        # store is resolved once, for both outcomes, before either is taken.
        cards = make_store()
    if not isinstance(card, dict):
        # NO CARD MEANS EVICT, never "leave things as they are". This record is the
        # authoritative answer for the slot, so a record without a board says the board is
        # gone -- the crew republished to another template, or its work fold holds no board
        # one. Returning early kept the PREVIOUS card in the store and the card route went on
        # serving it as `status: published`, which is a stale board presented as current: the
        # one state a status surface must never reach, because a reader cannot tell it from a
        # live one.
        if cards is not None:
            # ORDERED, like a publication: this removal is a statement about ONE record, so a
            # delayed read that snapshotted a no-board record must not drop a card some later
            # publish stored. Unordered, the removal path took any arrival and inverted the
            # order the write path had just been taught to keep.
            cards.retire_derived(
                slot_key,
                str((record or {}).get("published_at") or ""),
                authoritative=authoritative,
            )
        return
    if cards is None:
        # No store exists and none can be built: ``ensure_dynamic_card_store`` is the
        # constructor, and the branch above takes it whenever this state has one. Reaching here
        # means it does not -- an older state object, or a test double -- which is a state with
        # no card surface at all, so nothing can be served from it either. Compatibility
        # fallback, and the card waits until a store exists.
        return
    # LIVE by the top guard: ``_slots`` was checked the moment this function began and nothing
    # between there and here awaits, so the slot cannot have been closed in the gap -- the
    # lookup only re-fetches the object the store write needs, it is not a second existence test.
    slot = state._slots[slot_key]
    try:
        # The record's own publish stamp travels with the card so the store can ORDER two writes
        # built from different records. This runs after a worker hop, so a panel read that
        # snapshotted an older record can arrive AFTER a publish stored a newer card, and with
        # no stamp the store would simply take the late arrival and serve the older board.
        cards.publish_derived(
            slot,
            card,
            str((record or {}).get("published_at") or ""),
            authoritative=authoritative,
        )
    except Exception:
        logger.warning(
            "could not store the derived board card for slot %s", slot_key, exc_info=True
        )


@lru_cache(maxsize=1)
def _card_page() -> str:
    """The card page's markup, read once per process.

    CACHED because it is shipped package data that cannot change under a running gateway,
    and this runs on every drawer read. Unlike the crew webview templates there is no
    operator override directory for a card page, so there is nothing a cache could hide:
    the one thing an override would need -- a re-read -- is the thing that does not exist
    here.

    A page missing from the build raises ``OSError``, which :func:`_with_board_numbers`
    catches so the record comes back with no card. Deliberately NOT left to the route: that
    catches only ``MemberSlugError`` and ``ValueError``, so an escaping ``OSError`` would be
    a 500 on a drawer read -- a packaging fault taking out the panel along with the card.
    """
    return pipeline_board_contract.card_template_path().read_text(encoding="utf-8")


def _is_work_board(view: Any) -> bool:
    """Whether *view* is a board that EXISTS, as opposed to an unbound empty fold.

    ``entries``, not the item count: a conductor that recorded its goal and nothing
    else has a real board with no items yet, and that board's zeros are true. A fold
    over a slot that never carried a work entry has none, and its zeros are not.
    """
    if not isinstance(view, dict):
        return False
    conductor = view.get("conductor")
    if not isinstance(conductor, dict):
        return False
    try:
        return int(conductor.get("entries") or 0) > 0
    except (TypeError, ValueError):
        return False


def _published_record(slot: str, slug: str, owner_key: str) -> dict[str, Any] | None:
    """The crew's panel record: the folded one, else the stored file.

    THE FILE DECIDES THE PANEL when this owner has one, and the fold supplies the
    history. The file cannot be staler: the publish route writes it BEFORE it
    appends, and returns without appending if that write fails, so every publish is
    in the file while only the ones whose append landed are in the fold. That write
    order is the invariant this selection rests on -- a future writer that appends
    without writing the file would break it, and this comment is the contract it
    would be breaking.

    The fold is read for *slot* -- the member's own DM slot, which is the only slot a
    publish can append under -- through the ordinary slot-keyed projection, so it is
    served by the same warm kernel every other slot fold uses and this route keeps no
    cache of its own. It answers alone when the file has nothing to say: a crew whose
    file was never written, or was removed, or cannot be parsed.

    *owner_key* selects WHICH record, because one slot can carry two crews: the slot
    is the member slug's, and a crew whose persisted ``member_id`` is another crew's
    name-derived slug lands on the same one. The fold keeps a record per ownership
    digest, so each crew is answered with its own rather than with whichever of them
    published last -- which the single file cannot do. The file is checked against the
    same digest before it is used at all, since it is keyed by slug alone, so a
    collision cannot let one crew's publish displace the other's reading.

    The file is also what makes a panel outlive its session. It answers for a crew
    that published with the crew log off, for one that published before this entry
    type existed, and for one whose session unit has since been collected by
    retention -- a fold-only read would blank a webview that is still on disk.
    Nothing here writes it: this is a GET, and the store owns that write.

    An empty ``template`` is how the fold says "nothing published": the store refuses
    a publish naming no template, so no real record has one.
    """
    try:
        folded = projection.read_slot_projection(slot, PANEL_FOLD_NAME).value
    except Exception:
        # A damaged or unreadable log reads as "nothing folded" rather than as a 500,
        # matching the store's own totality contract: this route renders somebody's
        # drawer, and the fallback below may still have a panel to show.
        #
        # WARNING, not debug: the crew-visible symptom is a panel that went blank
        # with nothing saying why, and a log that stays damaged produces it on every
        # read. The operator is the only party who can repair it, so the trace has to
        # be at a level they will actually see.
        logger.warning("panel fold unreadable for slot %s", slot, exc_info=True)
        folded = {}
    owners = folded.get("owners") if isinstance(folded, dict) else None
    mine = owners.get(owner_key) if isinstance(owners, dict) else None
    if not (isinstance(mine, dict) and str(mine.get("template") or "")):
        return agent_panel.read(slug)

    stored = agent_panel.read(slug)
    # The file is keyed by SLUG alone, so on a slug two crews resolve to it may hold
    # the other crew's panel. Only this owner's own file may be used, or a collision
    # would let one crew's publish displace the other's reading.
    if stored is None or str(stored.get("crew_key") or "") != owner_key:
        return mine

    # THE FILE DECIDES THE PANEL, because it cannot be staler than the fold: the
    # publish route writes it BEFORE it appends, and returns without appending if
    # that write fails. So every publish is in the file, while only the ones whose
    # append landed are in the fold -- the crew log may be off for a cycle, or the
    # entry may exceed the log's whole-LINE ceiling while its data is under the
    # store's own cap.
    #
    # Preferring the fold instead pinned the drawer to the last LOGGED cycle and kept
    # serving it while the route answered the crew ok, which is the one failure a
    # published panel must not have: a viewer cannot tell a stale dashboard from a
    # current one. Comparing the two ``published_at`` stamps does not fix it either,
    # because both are second-granularity and two publishes in one second tie.
    #
    # The history is the fold's alone, and those rows stay true of the cycles that
    # were logged, so they ride along rather than being lost with it. They count
    # LOGGED publishes, which is what the fold can see.
    newest = dict(stored)
    for carried in ("history", "publishes", "history_omitted"):
        if carried in mine:
            newest[carried] = mine[carried]
    return newest


def _read_and_compose(
    slot: str,
    slug: str,
    owner_key: str,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """One record read, and the document composed from that same record.

    Both halves of the response come from a single snapshot, so a publish landing
    mid-request cannot pair one version's HTML with another version's summary.
    Runs in a worker thread: the slot fold (or the fallback file read) plus the
    template read plus the compose. The final flag distinguishes composition failure
    from an absent record without exposing an unowned record before ownership is
    checked.
    """
    record = _panel_record(slot, slug, owner_key)
    try:
        return record, agent_panel.render_record(record), False
    except (agent_panel.PanelError, TypeError, ValueError):
        return record, None, True


# The keys a panel record carries by the time the drawer serializes it, split into
# the two disjoint sets the read makes of each: SERVED reaches the client, WITHHELD
# is for the server's own use and stays server-side. Most come from
# ``projection._panel_owner_record``; the last two are added by this route's own
# provider step. Every key the record carries is in exactly one of these, and
# ``_panel_meta`` reddens on a key in neither, so a field added at either layer and
# classified in neither set fails loud -- ``test_the_drawer_serializer_classifies_
# every_record_key`` for the fold's keys, ``test_the_drawer_serializer_accepts_every_
# key_the_provider_adds`` for this route's -- rather than being silently served or
# silently dropped. An allow-list keyed on the record's OWN keys is what keeps the
# record's shape and the drawer's shape from diverging silently.
_PANEL_SERVED_KEYS = frozenset(
    {
        "template",
        "title",
        "crew",
        "data",
        "published_at",
        # The fold computes and bounds these on every publish, so serving them costs
        # nothing and gives the drawer the crew's publish history the server keeps.
        "history",
        "publishes",
        "history_omitted",
    }
)
# WITHHELD, each for its own reason:
#  * ``crew_key`` is a digest of the exact crew name, which may itself be
#    credential-shaped; a sibling route test pins that it never reaches a client.
#  * ``schema`` is the record's internal version tag, meaningful only to the fold.
#  * ``board`` is the log-derived pipeline board ``_with_board_numbers`` puts on a
#    contract template's record; the composer renders it into the document's data
#    island, and the docked card walks ``data``, so no client reads it off the meta.
#  * ``contract_replaced`` is the mark left on the record when a free-shape payload
#    was overridden by the log's numbers; it has no renderer, so the operator's
#    warning is what reaches a person, not a response field nothing reads.
#  * ``card`` is the dynamic dashboard card ``_with_board_numbers`` builds from the same
#    board. It goes to the CARD STORE, which the dashboard reads on its own route, so
#    serving it here too would publish one board through two contracts and let a viewer
#    see the two disagree.
_PANEL_WITHHELD_KEYS = frozenset(
    {"crew_key", "schema", "board", "card", "contract_replaced", "board_unreadable"}
)


def _panel_meta(record: Mapping[str, Any]) -> dict[str, Any]:
    """The drawer's metadata, as an allow-list over the record's own keys.

    Iterates the keys the record carries and serves exactly those in
    ``_PANEL_SERVED_KEYS``, so a key the fold does not produce is simply not served
    and a served key tracks the field rather than a hand-listed name. A key in
    NEITHER set is a programming error -- a field on ``_panel_owner_record`` whose
    drawer stance no one has decided -- and raises rather than defaulting either way,
    which keeps a computed-but-unserved field from slipping through.

    ``data`` is coerced to a dict because the store refuses a record whose data is
    not an object, so this handles only the rejected case, not a shape the store
    allows.
    """
    served: dict[str, Any] = {}
    for key in record:
        if key in _PANEL_WITHHELD_KEYS:
            continue
        if key not in _PANEL_SERVED_KEYS:
            raise KeyError(
                f"panel record key {key!r} is classified neither served nor withheld; "
                "add it to _PANEL_SERVED_KEYS or _PANEL_WITHHELD_KEYS in agent_panel.py"
            )
        served[key] = record[key]
    # Coerced to the exact shapes the drawer's client type promises, so the served
    # payload does not depend on which record form (fold, file, or the merge of the
    # two) reached us. The four text fields carry the ``str(... or "")``
    # normalisation; ``publishes``/``history_omitted`` are counts and ``history`` is
    # a list of ``{at,title,template}`` rows.
    out: dict[str, Any] = {
        "template": str(served.get("template") or ""),
        "title": str(served.get("title") or ""),
        "crew": str(served.get("crew") or ""),
        "published_at": str(served.get("published_at") or ""),
        # ``read`` already refuses a record whose data is not an object, so this is a
        # dict or the record was rejected; the guard is for the rejected case rather
        # than for a shape the store allows.
        "data": served["data"] if isinstance(served.get("data"), dict) else {},
    }
    # A raw legacy FILE record carries none of these three, so they are served only
    # when the record has them: a file-only panel reads without empty history keys.
    if "history" in served:
        rows = served["history"]
        out["history"] = [dict(r) for r in rows] if isinstance(rows, list) else []
    if "publishes" in served:
        out["publishes"] = projection._as_int(served["publishes"])
    if "history_omitted" in served:
        out["history_omitted"] = projection._as_int(served["history_omitted"])
    return out


async def api_member_panel(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/panel — the crew's composed webview document.

    Returned as a JSON string rather than a ``text/html`` body: the drawer feeds
    it to the same srcdoc builder the artifact frames use, which adds the strict
    CSP and the theme variables. Serving it as HTML here would invite loading it
    directly, outside the sandbox that makes it safe to render at all.

    The raw ``data`` object travels alongside the document, and the drawer's
    DOCKED summary is rendered from it natively rather than from the document.
    That is the whole reason it is here: a dashboard needs a full page to be
    legible (four tiles across, multi-column grids), and the panel hosting the
    docked view starts at 320px wide, which cannot host one without pushing the
    crew's most important line below the fold. Reading the summary from the data
    lets the panel show the few fields that matter, in the order the crew
    published them, as ordinary escaped text.

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
    # The READ is served to any dashboard caller, exactly as it was before this card
    # existed: the crew page's webview (`website/.../CrewWebview.tsx:memberPanel`) is
    # reachable by a non-owner dashboard subject, and gating the whole route would turn
    # their webview into a 403. Only the derived-card WRITE below is owner-gated, because
    # that write mutates the owner-facing dynamic-card store; see its guard. This keeps the
    # read's backwards compatibility while closing the non-owner-write hole.
    slug = request.match_info.get("slug", "")
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    # ``member`` (query, REQUIRED) is the exact crew name, exactly as
    # ``api_member_activity`` requires it and for the same reason: slugification is
    # lossy, so ``Oncall`` and ``oncall`` reach one slug and therefore one slot. The
    # fold keeps a record per ownership digest, so both crews' panels survive there
    # -- but a read keyed on the slug alone still hands whichever published last to
    # both of them. Verifying the stored ownership claim here is what picks the
    # asking crew's own record, and making the parameter required makes the mixed
    # read impossible by construction rather than a caller obligation.
    member = request.query.get("member", "")
    if not is_readable_member_name(member):
        return web.json_response(
            {"error": "member query parameter required", "code": "missing_member"}, status=400
        )
    # ONE read, both halves. `render` + `read` as separate calls let a publish land
    # between them and returned the old document beside the new summary, so the
    # docked chip and the expanded view could disagree. Composed inside the same
    # worker hop, which also keeps the template resolution off the event loop.
    #
    # The SLOT comes from the crew name plus the slug, through the same derivation
    # the members page uses for the DM thread: the fold lives on that slot, because
    # that is the only session a publish can append from. Derived rather than looked
    # up so a member whose thread is not running still reads its last panel.

    # Derived from the crew name alone, so it is known BEFORE the read and can select
    # which of a shared slot's records to fold out. The post-read comparison below is
    # the same value re-checked against what was actually read: it is what guards the
    # legacy file, which is keyed on the slug only and therefore cannot be selected.
    mine = agent_panel.crew_key(member)

    def _resolve_and_read() -> tuple[str, dict[str, Any] | None, str | None, bool]:
        cfg = KiroCrewConfig.load()
        # The slot key comes BACK from the worker hop, because the card store is keyed on
        # it and resolving it a second time out here would load the config twice and could
        # answer differently if it changed between the two reads.
        #
        # TWO keys, deliberately. The fold is read off the panel slot, which is shared by
        # every crew whose name reaches this slug; the card is stored only on a slot that
        # names one crew, so the key returned for the store is empty when this one is
        # shared. Both come from the same load, so they cannot disagree about the config.
        slot = _panel_slot(cfg, member, slug)
        return (_card_slot(cfg, member, slug), *_read_and_compose(slot, slug, mine))

    try:
        slot_key, record, html, render_failed = await asyncio.to_thread(_resolve_and_read)
    except (MemberSlugError, ValueError):
        # The crew name has no addressable member space, so it has no DM slot and
        # therefore no panel. Reported as the empty state rather than a refusal: from
        # this caller's point of view there is nothing published, and the slug it
        # asked about is not evidence of anything else.
        return web.json_response({"panel": None, "html": None})
    # Compared on the DIGEST of the exact name. The stored ``crew`` is the REDACTED
    # display text, so a credential-shaped crew name would never equal the exact
    # name it was redacted from -- that crew could not read its own panel, and two
    # different such crews would look like the same owner.
    #
    # An UNOWNED record is refused, not served. Treating an empty ``crew_key`` as
    # "nothing to compare" would be a fail-OPEN default on the one guard that keeps
    # a crew's drawer its own, and nothing legitimate produces such a record: this
    # schema ships with the ownership field, the publish route refuses a session
    # with no crew binding (``no_crew``), and :func:`publish` rejects an empty crew
    # outright. A forgery is the only thing left that can write one, which is
    # exactly what must not render.
    owner_key = str((record or {}).get("crew_key") or "")
    if record is not None and (not owner_key or owner_key != mine):
        # Another crew owns this slug's record. Reported as "nothing published"
        # rather than as a refusal: from this crew's point of view it HAS no panel,
        # and naming the other crew would disclose a colliding name the viewer of
        # this drawer has no other way to learn.
        return web.json_response({"panel": None, "html": None})
    # AFTER the ownership refusal above, never beside the read.
    #
    # ``_published_record`` answers with ``agent_panel.read(slug)`` unfiltered when the
    # fold holds nothing for this owner, and that file is keyed on the SLUG alone -- so a
    # colliding crew's record, or an unowned one, reaches this far. ``_panel_record``
    # gates only on the template id, so such a record still builds a card out of its
    # ``lede``, its per-item action sentences and its crew name. Stored before the check
    # above, that card is then served by the owner-only dashboard-card route, and the text
    # THIS route just refused to disclose is read off the other surface instead. The
    # refusal is what decides whether this record may be seen at all, so nothing derived
    # from it may leave the request before it.
    #
    # Deliberately BEFORE the render/compose branches below: a card is built from the fold
    # and the record, not from the drawer template, so a crew whose template is broken
    # still has a board worth showing.
    #
    # OWNER-GATED, and only here. The read above is open to any dashboard caller (the crew
    # webview depends on it), but this write mutates the owner-facing dynamic-card store and
    # broadcasts to owners -- the member-subject class the sibling routes guard. So a
    # non-owner `!dashboard` subject still READS its panel but triggers no store write: the
    # narrow gate the design review asked for, closing the non-owner-write hole without the
    # 403 a route-level gate put on the webview.
    if is_owner_dashboard_request(request):
        _publish_derived_card(request.app["state"], slot_key, record, mine)
    if render_failed:
        # A published panel that cannot be composed is temporarily unavailable,
        # matching the write path's 503 and giving the drawer a retryable error.
        return web.json_response(
            {"error": "could not render the panel", "code": "panel_render_failed"},
            status=503,
        )
    if html is None:
        # Only a record the store treats as absent reaches this empty state. A
        # readable published record with a broken template returns the error above.
        return web.json_response({"panel": None, "html": None})
    try:
        panel = _panel_meta(record) if record is not None else None
    except KeyError:
        # A record carrying a key the serializer classifies as neither served nor
        # withheld is a programming error the test suite is meant to catch, but a
        # live drawer must not 500 on somebody's panel: log it for the operator and
        # show the empty state, the same failure mode every other read defect here
        # degrades to.
        logger.warning("panel record has an unclassified key for slug %s", slug, exc_info=True)
        return web.json_response({"panel": None, "html": None})
    if panel is not None:
        # The template's own opt-in to render in the docked card, read from the
        # document served beside it. It is derived from ``html`` at response time,
        # not a record field, so it rides on the served panel rather than through
        # the record-keyed allow-list. ``None`` keeps the native summary, which
        # costs the drawer no mint.
        panel["docked_height"] = agent_panel.docked_height(html)
    return web.json_response(
        {
            "panel": panel,
            "html": html,
        }
    )


# -------------------------------------------------------------------------- #
# the dynamic dashboard's agent surface (contract v3, part 6)
# -------------------------------------------------------------------------- #


def read_instance(slug: str, member: str) -> dashboard_agentic.Instance | None:
    """The crewmate's dashboard instance, or ``None`` when it has none to write to.

    THE SEAM TO THE REGISTRY, and the one place this file depends on a store it does
    not own. ``dashboard_templates.instance.read`` is the reader and
    ``GET /api/members/{slug}/dashboard`` serves the same record to the frame, so
    this surface and the page agree about which manifest is in force without a
    second source of truth.

    Resolved through the MODULE rather than imported at the top, which keeps the
    boot-path rule (this handler is reached deferred) and lets a test replace this
    one function with a stub.

    It narrows the record deliberately. The store's own ``Instance`` carries the
    html and the state; this returns only the parsed manifest and the version,
    because an agent tool that could read its own page's markup is one that could
    be talked into reporting it, and the state is the frame's branch, not a
    writer's.

    ``live`` and ``stale`` resolve to the stored copy and ``empty`` to the DEFAULT, the
    page that crewmate is actually shown; only ``error`` and a missing default resolve
    to ``None``, which the write path reports as ``no_instance``. The test is whether a
    field name can be checked against a manifest a reader can trust. A stale copy HAS
    its own manifest, complete and still renderable, and the write is checked against
    the same manifest the page is drawn from; what a stale copy cannot do is be
    compared against its source, which says nothing about a field the crewmate
    declares. An ``empty`` record has no copy of its own, but the page it renders has a
    manifest all the same -- the default's -- and validating against that is what makes
    the written value appear on the page the reader is looking at. ``error`` is the one
    state where no manifest parses, so there is nothing to validate against and
    refusing is the only honest answer.

    An absent registry is NOT one of those states and is not caught here. The
    registry ships in the same package as this surface, so a build without it is a
    packaging fault, and swallowing the ``ImportError`` would report that fault as
    ``no_instance`` -- sending every crewmate to adopt a template through a registry
    the build does not have.
    """
    from kiro_crew.dashboard_templates import instance as instance_store
    from kiro_crew.dashboard_templates.manifest import ManifestError, parse_manifest

    try:
        record = instance_store.read(slug)
    except Exception:
        logger.warning("the dashboard instance for %s could not be read", slug, exc_info=True)
        return None
    # A STALE copy is writable and a LIVE one is: `_state_of` calls a stale copy
    # complete and still renderable, and the write is validated against the COPY's own
    # manifest, which a stale copy still has. What a stale copy cannot do is be
    # compared against its source, and that says nothing about a field the crewmate
    # declares and writes.
    #
    # EMPTY resolves to the DEFAULT, because the default is the page that crewmate is
    # actually being shown: `api_member_dashboard` falls back to `default_instance` for
    # exactly this state, so refusing here made the two paths disagree about which
    # template is in force. The disagreement was not academic -- the default page's one
    # agentic field is the "needs you" answer, nothing in P1 calls the adopt route, and
    # the conductor skill writes that field every cycle, so every write was refused with
    # a remedy (adopt a template) that has no control to carry it out.
    #
    # ERROR stays refused on its own terms: it is the state in which the manifest does
    # not parse, so there is nothing to validate a write against.
    live = getattr(instance_store, "STATE_LIVE", "live")
    stale = getattr(instance_store, "STATE_STALE", "stale")
    empty = getattr(instance_store, "STATE_EMPTY", "empty")
    state = getattr(record, "state", "")
    if state == empty:
        try:
            fallback = instance_store.default_instance(slug)
        except Exception:
            # No default to fall back to: the registry is unreadable or ships none. A
            # refusal here is the honest answer, and it is the one case where
            # `no_instance` still describes the world.
            logger.warning("no default dashboard for %s to write against", slug, exc_info=True)
            return None
        if fallback is None:
            # `default_instance` ANSWERS None when the registry ships no default; it
            # does not raise, so the guard above does not cover this.
            return None
        record = fallback
    elif state not in (live, stale):
        return None
    try:
        manifest = parse_manifest(dict(record.manifest))
    except (ManifestError, TypeError, ValueError):
        # A record the store called live whose manifest will not parse here is a
        # disagreement between two readers, not a crewmate's mistake. Logged for the
        # operator and reported as no instance, so the write is refused rather than
        # checked against a manifest this process could not read.
        logger.warning("the dashboard manifest for %s does not parse", slug, exc_info=True)
        return None
    return dashboard_agentic.Instance(
        manifest=manifest, instance_version=int(record.instance_version)
    )


def _mistake_book(slot: str) -> dict[str, Any]:
    """The crewmate's folded mistake book, or an empty one.

    Read through the ordinary slot-keyed projection -- the same warm kernel the
    panel fold uses -- and NEVER by refolding a log, which the contract forbids
    twice. TOTAL: a damaged or absent fold reads as an empty book, because a
    crewmate with no readable mistakes is in the same position as one that has made
    none, and failing a write because its mistake book would not load would be the
    book costing the feature it exists to improve.
    """
    try:
        value = projection.read_slot_projection(slot, dashboard_agentic.MISTAKES_FOLD).value
    except Exception:
        logger.warning("the mistakes fold is unreadable for slot %s", slot, exc_info=True)
        return {}
    return value if isinstance(value, dict) else {}


async def _resolve_dashboard_caller(
    request: web.Request, operation: str
) -> tuple[tuple[str, str, str], None] | tuple[None, web.Response]:
    """Vet the caller and resolve it to ``(slug, crew_name, slot)``.

    Through the SAME gate the publish route uses, so the dashboard surface inherits
    every refusal that one earns: the internal-secret requirement, the app-caller
    denial, the restricted-session block, the operator switch, and the rule that
    the crew comes from the session's own binding and never from the body.

    The SLOT comes back too, because both handlers need it -- one to read the
    mistake book, the other to append -- and resolving it twice would load the
    config twice and could answer differently if it changed in between.

    AND the slug must be this crew's alone. An instance is one directory per slug,
    and slugification is lossy, so two configured names that differ only in case --
    ``Oncall`` and ``oncall`` -- resolve to one directory: either crew could then
    replace the other's staged or installed page, and a rollback by one would
    restore a version the other wrote. The browser's own resolver refuses that slug
    outright (``member_dashboard._resolve``, ``dashboard_slug_ambiguous``); this
    surface reaches the same files through MCP and therefore owes the same refusal.
    It is asked HERE rather than per route so a route added later inherits it.
    """
    resolved, refusal = await _resolve_publishing_crew(request, operation)
    if refusal is not None:
        return None, refusal
    assert resolved is not None
    slug, crew_name = resolved

    def _owners_then_slot() -> tuple[list[str], str]:
        # ONE config load for both answers, and the OWNERS question first: an
        # ambiguous slug is one `_member_thread_slot` refuses outright, so resolving
        # the slot before asking would answer a 409 case with a 500.
        cfg = KiroCrewConfig.load()
        owners = _member_names_for_slug(cfg, slug)
        if owners and owners != [crew_name]:
            return owners, ""
        return owners, _panel_slot(cfg, crew_name, slug)

    owners, slot = await asyncio.to_thread(_owners_then_slot)
    # EMPTY is allowed and is not the ambiguous case. A publishing crew need not be
    # a configured MEMBER -- `_member_names_for_slug` lists addressable members, and
    # `_resolve_publishing_crew` has already vetted a caller that is not one -- so an
    # empty list means no configured member claims this slug, not that somebody else
    # does. What is refused is a slug a configured name OTHER than this crew resolves
    # to, which is both the case-collision pair and the plain wrong-owner case.
    if owners and owners != [crew_name]:
        return None, web.json_response(
            {
                "error": (
                    "more than one crew resolves to this dashboard, so a write here "
                    "would land on a page that is not only yours; ask the human to "
                    "rename one of them"
                ),
                "code": "dashboard_slug_ambiguous",
            },
            status=409,
        )
    return (slug, crew_name, slot), None


async def api_dashboard_fields(request: web.Request) -> web.Response:
    """GET /api/agent-panel/dashboard/fields -- the fields, and the mistake book.

    The read that makes an agent's first write usually correct. It is on the
    strict-internal prefix with the publish routes because it reports a crewmate's
    own refused writes, which is state about that crewmate and nobody else's to
    read.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_fields")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
    instance = await asyncio.to_thread(read_instance, slug, crew_name)
    mistakes = await asyncio.to_thread(_mistake_book, slot) if slot else {}
    # The version history rides along on THIS read rather than having a tool of its
    # own. An agent that is about to answer "go back" already has to make this call to
    # know its fields, the rows are capped at ten, and a second round trip for ten rows
    # is a cycle spent on a payload that fits in this one.
    rows, retained = await asyncio.to_thread(_instance_history, slug, slot)
    values, written_at = await asyncio.to_thread(_current_values, slug, crew_name, instance)
    return web.json_response(
        dashboard_agentic.fields_for_agent(
            instance,
            mistakes,
            history=rows,
            rollback_versions=retained,
            values=values,
            written_at=written_at,
        )
    )


def _current_values(
    slug: str, crew_name: str, instance: dashboard_agentic.Instance | None
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """The page's current field values, read the way the page reads them.

    ``(None, {})`` when there is no instance or the read fails, so the listing still
    answers with the fields and their shapes rather than failing the whole read.
    """
    if instance is None:
        return None, {}
    try:
        from kiro_crew.dashboard.handlers.member_dashboard import read_fields

        read = read_fields(slug, crew_name, instance.manifest)
    except Exception:
        logger.warning("dashboard values for %s are unreadable", slug, exc_info=True)
        return None, {}
    return dict(read.fields), dict(read.written_at)


async def api_dashboard_write(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/write -- one type-checked agentic value.

    The order of the steps is the feature:

    1. read the instance, so the check is against the manifest this crewmate is
       actually running rather than one the caller names;
    2. read the mistake book, so a refusal can say "you have done this before" and
       quote what worked;
    3. check the write -- and on a refusal, RECORD it and hand the sentence back;
    4. on success, append the value, then append the correction when this write
       answers a mistake the book was still holding.

    Step 3's recording is what makes the next cycle cheaper, and it happens before
    the response: an agent that is refused and then crashes has still taught its
    successor. Step 4's correction comes after the value lands, never before -- a
    correction for a write that failed to append would tell the crewmate a wrong
    field name was fixed by one that was never stored.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_write")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, slot = resolved
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
        args = validate_tool_args(body, DASHBOARD_WRITE_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "validation_error"}, status=400)

    field = str(args.get("field") or "")
    value = args.get("value")
    instance = await asyncio.to_thread(read_instance, slug, crew_name)
    mistakes = await asyncio.to_thread(_mistake_book, slot) if slot else {}
    owner_key = agent_panel.crew_key(crew_name)
    try:
        entry = dashboard_agentic.check_write(instance, field, value, mistakes)
    except dashboard_agentic.WriteRefused as refused:
        # RECORDED BEFORE THE RESPONSE. The refusal is already decided, so this
        # append costs the caller nothing it was going to get, and it is the only
        # reason the next cycle is cheaper than this one.
        recorded = dashboard_agentic.refusal_entry(refused, owner_key)
        await asyncio.to_thread(_record_refusal, state, sk, recorded)
        # The SAME scrubbed strings the entry carries. Reading them off `refused`
        # again would hand the caller the unscrubbed sentence and field name, so the
        # response and the record would disagree about what the refusal said.
        return web.json_response(
            {
                # The WHOLE scrubbed sentence, where the entry stores a 240-character
                # prefix of it. Same scrub and same wording, so the record and the
                # body cannot disagree about what the refusal said -- but the body is
                # not cut, because the tail is the remedy: the valid field names come
                # first and the "you have done this before; 'x' worked" note last, so
                # a manifest with about five fields pushes exactly the useful half
                # past the cap.
                "error": dashboard_agentic.refusal_sentence(refused),
                "code": recorded["code"],
                # Absent from the entry exactly when the refusal named no field, and
                # then `refused.field` is itself empty, so this carries no agent text.
                "field": recorded.get("field", refused.field),
            },
            status=400,
        )
    entry["crew_key"] = owner_key

    def _append() -> tuple[bool, bool]:
        unit = _session_unit(state, sk)
        if not unit:
            return False, False
        # ASKED BEFORE THE APPEND, like the panel route asks its own: an entry over
        # the log's whole-LINE ceiling can never land, so writing it would report a
        # value as stored that no fold will ever see.
        if not crew_log_emit.dashboard_entry_fits(dashboard_agentic.VALUE_ENTRY_TYPE, entry):
            return False, False
        if not crew_log_emit.on_dashboard_agentic(unit, entry, timeout=_APPEND_FLUSH_SECONDS):
            return False, False
        correction = dashboard_agentic.correction_entry(field, mistakes, owner_key)
        if correction is None:
            return True, False
        return True, crew_log_emit.on_dashboard_refused(
            unit, correction, timeout=_APPEND_FLUSH_SECONDS
        )

    if not crew_log_emit.enabled():
        # THE LOG IS THE DASHBOARD'S ONLY RECORD for an agentic value -- there is no
        # file beside it, because no host Python computes a dashboard value. So this
        # is a REFUSAL rather than the best-effort shrug the panel publish gives:
        # reporting success would promise a cell that will never fill.
        return web.json_response(
            {
                "error": (
                    "this gateway is not recording a crew log, and an agentic dashboard "
                    "value has nowhere else to live -- ask the human to switch the crew "
                    "log on"
                ),
                "code": "crew_log_off",
            },
            status=503,
        )
    stored, corrected = await asyncio.to_thread(_append)
    if not stored:
        return web.json_response(
            {
                "error": (
                    "the value could not be written to this crewmate's crew log; try "
                    "again on your next cycle"
                ),
                "code": "append_failed",
            },
            status=503,
        )
    # Tell an open dashboard a value changed. The fold's own bus event is what
    # drives the frame's refill; this frame is for a client watching the panel
    # surface, and it carries the SLUG only, like the publish broadcast.
    state.broadcast_ws("dashboard_value_written", {"slug": slug})
    return web.json_response(
        {"ok": True, "written": {"field": field, "type": entry["type"]}, "corrected": corrected}
    )


def _record_refusal(state: DashboardState, sk: str, entry: dict[str, Any]) -> bool:
    """Append one refusal to the caller's own log. Best-effort, never raises.

    BEST-EFFORT unlike the value append, and the asymmetry is the point: by the
    time this runs the caller is already being refused, so a log that is off costs
    the mistake book this row and nothing else. Failing the request here would turn
    "we could not remember your mistake" into "your write was not refused", which
    is false.
    """
    if not crew_log_emit.enabled():
        return False
    try:
        unit = _session_unit(state, sk)
        if not unit:
            return False
        return crew_log_emit.on_dashboard_refused(unit, entry, timeout=_APPEND_FLUSH_SECONDS)
    except Exception:
        logger.warning("a refused dashboard write was not recorded", exc_info=True)
        return False


def _instance_history(slug: str, slot: str) -> tuple[tuple[dict[str, Any], ...], tuple[int, ...]]:
    """This instance's history rows and the versions a rollback can still reach.

    TOTAL, like the mistake book beside it: a history that will not load costs the
    rows and never the read. An agent that cannot see its history is in the position of
    one whose dashboard has none, and failing the fields read because of it would make
    the history cost the call it rides on.

    The two halves come from different places on purpose. The ROWS are the fold, capped
    at :data:`instance.MAX_HISTORY_ROWS`; the VERSIONS are the payloads still on disk,
    capped lower at :data:`instance.MAX_RETAINED_VERSIONS`. So the fold can name a
    version a rollback would refuse, and listing only what is retained is what keeps an
    agent from spending a cycle being told the page it asked for is gone.
    """
    from kiro_crew.dashboard_templates import instance as instance_store

    rows: tuple[dict[str, Any], ...] = ()
    retained: tuple[int, ...] = ()
    try:
        rows = instance_store.history(slug, session_id=_history_session(slot))
    except Exception:
        logger.warning("the dashboard history for %s is unreadable", slug, exc_info=True)
    try:
        retained = instance_store.versions(slug)
    except Exception:
        logger.warning("the retained dashboard versions for %s are unreadable", slug)
    return rows, retained


def _history_session(slot: str) -> str:
    """The session a dashboard change's history entry belongs to: the crewmate's DM log.

    The same derivation ``member_dashboard._write_session`` makes, from the slot this
    surface already resolved rather than from the member name -- resolving it twice
    would load the config twice and could answer differently if it changed between.

    "Newest" comes from the DURABLE succession chain and not from a header clock, for
    the reason that handler gives: a unit's ``createdAt`` is stamped once, so a clock
    that steps backward between two units of one slot would attribute every later
    change to a session that is already over.

    Empty when there is none, which is not a failure. The instance record is a file and
    is committed either way, so a crewmate whose DM thread has never run gets a working
    dashboard with no history row rather than a refused change.
    """
    if not slot:
        return ""
    try:
        from kiro_crew.crew_log.projection import units_in_succession

        units = units_in_succession(slot)
        return units[-1] if units else ""
    except Exception:
        logger.warning("could not resolve the history session for slot %s", slot, exc_info=True)
        return ""


def _instance_refusal(exc: Exception) -> web.Response:
    """One refused instance write, as the tool's caller reads it.

    A refusal carries the store's own sentence, which is written for an agent: it says
    what was wrong and what to do instead. An unexpected failure does NOT -- it carries
    a path or an errno -- so it answers 503 with a fixed sentence and the traceback goes
    to the log, where an operator reads it and a crewmate cannot.
    """
    from kiro_crew.dashboard_templates import instance as instance_store

    if isinstance(exc, instance_store.InstanceRefused):
        return web.json_response({"error": str(exc), "code": "refused"}, status=400)
    logger.warning("a dashboard instance call failed", exc_info=True)
    return web.json_response(
        {
            "error": (
                "this crewmate's dashboard store could not be written; try again on "
                "your next cycle"
            ),
            "code": "store_failed",
        },
        status=503,
    )


def _template_matches(listing: Mapping[str, Any], query: str) -> bool:
    """Whether one catalog listing answers *query*.

    The haystack includes the FOLD NAMES AND FIELD PATHS, not only the title and the
    description. A person asking for "the one that shows cost" is naming what they want
    to see, and the template that shows it is the one whose fields READ a usage fold --
    a word its author may never have put in the title. Matching only the prose would
    answer "there is none" for a template that does exactly the thing.

    Substring, case-folded, every whitespace-separated word required. Not a ranking:
    the catalog is a handful of templates, a chooser reads all of them, and a relevance
    score here would be a number nobody can explain to the person who asked.
    """
    haystack = " ".join(
        str(part).casefold()
        for part in (
            listing.get("id", ""),
            listing.get("title", ""),
            listing.get("description", ""),
            " ".join(str(f) for f in listing.get("fields", []) or ()),
            " ".join(str(f) for f in listing.get("folds", []) or ()),
            " ".join(str(p) for p in listing.get("paths", []) or ()),
        )
    )
    return all(word in haystack for word in query.casefold().split())


def _templates_for_agent(slug: str, query: str) -> dict[str, Any]:
    """The catalog as a chooser reads it, with this crewmate's current template marked.

    MARKED rather than filtered out. "Show me another" is answered by a list, and a
    list that silently omits the page already on screen makes the one thing the reader
    can verify -- that their current page is in there -- look like a missing template.

    ``problems`` is carried through from the scan. A directory that will not load is
    not this caller's fault and cannot be fixed by it, but a listing that silently drops
    it is how a template somebody saved stays invisible with nothing anywhere to read.
    """
    from kiro_crew.dashboard_templates import catalog
    from kiro_crew.dashboard_templates import instance as instance_store

    try:
        current = instance_store.read(slug).template_id
    except Exception:
        logger.warning("could not read %s's current template", slug, exc_info=True)
        current = ""
    found = catalog.list_templates()
    rows: list[dict[str, Any]] = []
    for entry in found.entries:
        listing = dict(entry.listing())
        # The paths its fields READ, which `listing()` does not carry: it reports the
        # fold names, and a path is the half that names the quantity ("usage.cost_usd")
        # rather than the fold it sits in. Added here, where the search needs it, rather
        # than widening every reader of a catalog listing.
        listing["paths"] = sorted(spec.path for spec in entry.manifest.fields.values() if spec.path)
        if query and not _template_matches(listing, query):
            continue
        listing["current"] = entry.id == current and bool(current)
        rows.append(listing)
    return {
        "templates": rows,
        "current_template_id": current,
        "query": query,
        "problems": [{"template": name, "problem": why} for name, why in found.problems],
    }


async def api_dashboard_templates(request: web.Request) -> web.Response:
    """GET /api/agent-panel/dashboard/templates?query= -- the catalog, searchable.

    On the strict-internal prefix with the rest of this surface. The listing itself is
    not a crewmate's data, but the body also says which template THIS crewmate is
    running, and that is state about this crewmate like its refused writes are.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_templates")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, _crew_name, _slot = resolved
    query = str(request.query.get("query") or "").strip()[:200]
    try:
        body = await asyncio.to_thread(_templates_for_agent, slug, query)
    except Exception as exc:
        return _instance_refusal(exc)
    return web.json_response(body)


async def _instance_body(request: web.Request) -> dict[str, Any] | web.Response:
    """The request's JSON object, or the response that refuses it."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    return body


async def api_dashboard_preview(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/preview -- stage a template for a look.

    NOTHING IS RECORDED. The staged page is checked and written beside the record, the
    instance version does not move, and the body hands back the link a person opens to
    see it. That split is the feature: an agent can offer a page without having changed
    the one somebody is currently reading, so "show me another" costs nothing if the
    answer is no.

    Only a ``template_id`` is accepted. A body carrying ``html`` or ``manifest`` is
    refused by the store, which owns that refusal so every route into it answers the
    same way; this handler passes them through rather than dropping them, because a
    caller that sent a page it wrote has to be TOLD, not silently given a template it
    did not ask for.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_preview")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, _crew_name, _slot = resolved
    body = await _instance_body(request)
    if isinstance(body, web.Response):
        return body
    template_id = body.get("template_id")
    html = body.get("html")
    manifest = body.get("manifest")
    if template_id is not None and not isinstance(template_id, str):
        return web.json_response(
            {"error": "template_id must be a string", "code": "validation_error"}, status=400
        )

    from kiro_crew.dashboard_templates import instance as instance_store

    def _stage() -> dict[str, Any]:
        preview = instance_store.stage_preview(
            slug, template_id=template_id, html=html, manifest=manifest
        )
        return preview.wire()

    try:
        staged = await asyncio.to_thread(_stage)
    except Exception as exc:
        return _instance_refusal(exc)
    return web.json_response({"ok": True, "preview": staged})


async def api_dashboard_apply(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/apply -- keep the staged page.

    Takes NO arguments, which is what makes it safe to call on a person's "yes": the
    page it installs is the one that was staged and therefore the one they were shown.
    A body naming a template here would let the thing applied differ from the thing
    looked at, and the agent in between is the one place that difference would not be
    visible to anybody.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_apply")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, _crew_name, slot = resolved

    from kiro_crew.dashboard_templates import instance as instance_store

    def _apply() -> Any:
        return instance_store.apply_preview(slug, session_id=_history_session(slot))

    try:
        record = await asyncio.to_thread(_apply)
    except Exception as exc:
        return _instance_refusal(exc)
    # Tell an open dashboard its page changed, the same one-key frame the agentic
    # write broadcasts. The tab refetches the page; this carries the slug only.
    state: DashboardState = request.app["state"]
    state.broadcast_ws("dashboard_instance_changed", {"slug": slug})
    return web.json_response(
        {
            "ok": True,
            "instance_version": record.instance_version,
            "template_id": record.template_id,
        }
    )


async def api_dashboard_rollback(request: web.Request) -> web.Response:
    """POST /api/agent-panel/dashboard/rollback -- go back to a retained version.

    FORWARD, like the store's own rollback: version 1 restored over version 2 becomes
    version 3. The response says which version is now current rather than echoing the
    one that was asked for, because those are two different numbers and a caller that
    reported the second would tell a person they are on a version nobody is on.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_rollback")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, _crew_name, slot = resolved
    body = await _instance_body(request)
    if isinstance(body, web.Response):
        return body
    to_version = body.get("to_version")
    if not isinstance(to_version, int) or isinstance(to_version, bool) or to_version < 1:
        return web.json_response(
            {"error": "to_version must be a positive integer", "code": "validation_error"},
            status=400,
        )

    from kiro_crew.dashboard_templates import instance as instance_store

    def _rollback() -> Any:
        return instance_store.rollback(slug, to_version, session_id=_history_session(slot))

    try:
        record = await asyncio.to_thread(_rollback)
    except Exception as exc:
        return _instance_refusal(exc)
    state: DashboardState = request.app["state"]
    state.broadcast_ws("dashboard_instance_changed", {"slug": slug})
    return web.json_response(
        {
            "ok": True,
            "instance_version": record.instance_version,
            "restored_from": to_version,
            "template_id": record.template_id,
        }
    )


def register_agent_panel_routes(app: web.Application) -> None:
    app.router.add_get("/api/agent-panel/templates", api_agent_panel_templates)
    app.router.add_post("/api/agent-panel/publish", api_agent_panel_publish)
    # The dynamic dashboard's agent surface. Under the SAME prefix, which is what
    # gives it the same auth: ``server._STRICT_INTERNAL_API_PATHS`` lists
    # ``/api/agent-panel`` and the middleware matches by prefix, so these are
    # MCP-only and strict-internal without a second entry. That matters here as
    # much as for publish -- a write lands in the crewmate's own crew log, so a
    # caller holding only a dashboard cookie must not reach it.
    app.router.add_get("/api/agent-panel/dashboard/fields", api_dashboard_fields)
    app.router.add_post("/api/agent-panel/dashboard/write", api_dashboard_write)
    # The page's own four, under the same prefix and so with the same auth. A preview
    # and an apply reach a crewmate's instance store and its crew log, so a caller
    # holding only a dashboard cookie must not reach them either.
    app.router.add_get("/api/agent-panel/dashboard/templates", api_dashboard_templates)
    app.router.add_post("/api/agent-panel/dashboard/preview", api_dashboard_preview)
    app.router.add_post("/api/agent-panel/dashboard/apply", api_dashboard_apply)
    app.router.add_post("/api/agent-panel/dashboard/rollback", api_dashboard_rollback)
    # The drawer's read. NOT under /api/agent-panel: that prefix is
    # strict-internal (MCP-only), and this one is called by the browser.
    app.router.add_get("/api/members/{slug}/panel", api_member_panel)
