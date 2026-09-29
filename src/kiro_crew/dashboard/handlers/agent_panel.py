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
from typing import Any, Final, cast

from aiohttp import web

from kiro_crew import agent_panel
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
    _member_thread_slot,
    _slug_is_claimed_by_any_member,
)
from kiro_crew.dashboard.handlers.session_ledger import _session_unit
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.history import is_incognito_transcript
from kiro_crew.members import MemberSlugError, is_readable_member_name
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.sel import sel
from kiro_crew.session_ledger import _APPEND_FLUSH_SECONDS
from kiro_crew.validation import (
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
        return record
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
_PANEL_WITHHELD_KEYS = frozenset({"crew_key", "schema", "board", "contract_replaced"})


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

    def _resolve_and_read() -> tuple[dict[str, Any] | None, str | None, bool]:
        cfg = KiroCrewConfig.load()
        return _read_and_compose(_panel_slot(cfg, member, slug), slug, mine)

    try:
        record, html, render_failed = await asyncio.to_thread(_resolve_and_read)
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


def register_agent_panel_routes(app: web.Application) -> None:
    app.router.add_get("/api/agent-panel/templates", api_agent_panel_templates)
    app.router.add_post("/api/agent-panel/publish", api_agent_panel_publish)
    # The drawer's read. NOT under /api/agent-panel: that prefix is
    # strict-internal (MCP-only), and this one is called by the browser.
    app.router.add_get("/api/members/{slug}/panel", api_member_panel)
