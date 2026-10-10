"""HTTP routes for gateway-owned UI guides (``kirocrew-guide``).

Two halves with deliberately different auth, like the agent-panel surface:

* **Agent half** (``/api/guide/agent/*``) is MCP-only and strict-internal: the
  whole prefix sits in ``server._STRICT_INTERNAL_API_PATHS``, and every handler
  re-asserts ``internal_auth`` itself because a ``local_only=False`` deployment
  reclassifies strict paths as mixed. The caller's slot is derived SOLELY from the
  verified ``X-Session-Key`` by walking the LIVE slot table
  (``session_control.caller_slot_key``) -- a surface-registry hit is not proof a
  tab is open, and no request field can name a slot. App callers, subagents,
  unattended (cron/workflow) tabs and a key with no live slot are refused.

* **Browser half** (``/api/guide/pending|claim|progress|heartbeat|cancel|dismiss|
  replay|refuse|replan|observe``) is
  cookie-authed and OWNER-only (``require_owner_dashboard_request``, which also
  refuses app tokens). It reports what a tab observed; it can never complete a
  mutation step.

Mutation steps complete through :func:`run_guided_crewmate_create`, a narrow post-success
hook the existing owner-only ``POST /api/agents`` route is wrapped in: the tab
names the guide in ``X-Guide-Id`` / ``X-Guide-Tab`` / ``X-Guide-Revision``, the
hook associates the request BEFORE the handler runs, and advances the guide only
from the identity THAT handler returned on success. Nothing the client says about
success is read.

``guide_update`` and ``guide_observe`` WebSocket frames go to owner sockets only.
A live observation (``POST /api/guide/agent/observe`` asking, the tab answering
on ``POST /api/guide/observe``) is the bounded channel of
:mod:`kiro_crew.dashboard.guide_observe`: ids and enum states, one tab, never
stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew import guide_catalog
from kiro_crew.dashboard import chat_cards
from kiro_crew.dashboard.guide_observe import MAX_PREDICATES as MAX_OBSERVE_PREDICATES
from kiro_crew.dashboard.guide_observe import MAX_SCOPES as MAX_OBSERVE_SCOPES
from kiro_crew.dashboard.guide_observe import MAX_TARGETS as MAX_OBSERVE_TARGETS
from kiro_crew.dashboard.guide_observe import (
    NOT_OBSERVED,
    OBSERVE_WAIT_SECONDS,
    REASON_NO_TAB,
    REASON_STALE_TAB,
    observation_hub_for,
)
from kiro_crew.dashboard.guide_runs import GuideError, GuideStore, guide_store_for
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

WS_GUIDE_UPDATE = "guide_update"
#: Owner-only frame asking ONE tab (by ``tab_id``) for a live observation.
WS_GUIDE_OBSERVE = "guide_observe"

HEADER_GUIDE_ID = "X-Guide-Id"
HEADER_GUIDE_TAB = "X-Guide-Tab"
HEADER_GUIDE_REVISION = "X-Guide-Revision"

KIND_CREWMATE_CREATE = guide_catalog.ACTION_CREWMATE_CREATE

_SLOT_QUERY_MAX = 256


def _audit(caller: str, operation: str, outcome: str, error: str = "") -> None:
    try:
        sel().log_api_access(
            caller=caller or "unknown",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources="/api/guide",
            error=error,
        )
    except Exception:  # pragma: no cover - an audit must never change the outcome
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


def _refusal(exc: GuideError) -> web.Response:
    status = exc.status if exc.status in (400, 403, 404, 409, 429) else 400
    return web.json_response({"error": exc.message, "code": exc.code}, status=status)


def _deny(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _store(request: web.Request) -> GuideStore:
    return guide_store_for(request.app["state"])


async def _broadcast(state: Any, guide: dict[str, Any]) -> int:
    # Every public change of a guide passes here, so this is where its row in the
    # conversation learns the new status (and the crew log the outcome).
    chat_cards.record_guide_status(state, guide)
    deliver = getattr(state, "deliver_ws_owners", None)
    if deliver is None:
        return 0
    try:
        return int(await deliver(WS_GUIDE_UPDATE, {"guide": guide}))
    except Exception:
        logger.debug("guide_update delivery failed", exc_info=True)
        return 0


async def _broadcast_swept(request: web.Request) -> None:
    """Deliver any expiry / lease lapse a sweep observed before this request acts."""
    state = request.app["state"]
    store = _store(request)
    retired = store.retire_closed_slots(lambda key: state.get_slot(key) is not None)
    for guide in retired + store.sweep():
        await _broadcast(state, guide)


async def _catalogs_ready() -> None:
    """Load the packaged guide catalogs off the event loop (``warm_catalogs``).

    Validation and observation read them synchronously; warmed here, those
    reads are cache hits, so a cold first call never parses the UI index on
    the gateway loop.
    """
    await asyncio.to_thread(guide_catalog.warm_catalogs)


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise GuideError(400, "invalid_json", "invalid JSON body") from None
    if not isinstance(body, dict):
        raise GuideError(400, "invalid_body", "body must be a JSON object")
    return body


# ── agent half ──


#: Operations that put something in front of the person, or change what they
#: see, on their word: only a turn they sent may. ``member.rename_self`` is a
#: crewmate taking the name the user gave it, so a wake or the hidden welcome
#: kickoff cannot pick one.
_USER_TURN_OPERATIONS = frozenset({"guide.start", "member.rename_self"})


async def _resolve_agent_caller(request: web.Request, operation: str) -> tuple[str, str]:
    """The verified caller's ``(slot_key, session_key)``. Raises :class:`GuideError`.

    The session the request names must be one the transport attests
    (:func:`kiro_crew.member_memory_auth.session_key_is_attested`): the shared
    internal secret alone is any local process's word, so it could name another
    crewmate's live turn.
    Admits only a tool call made by a turn running in the caller's dashboard
    slot that did not come from a messaging channel. A channel-born
    conversation is mirrored into a dashboard slot, so finding a live slot
    proves nothing about who is asking; the turn's own opener provenance
    (``_turn_channel_origin``) and any channel steer admitted into it
    (``_turn_channel_narrowed``) are what the gateway recorded when the turn
    started. An operation that puts something in front of the person (a guide)
    or renames the calling crewmate (:data:`_USER_TURN_OPERATIONS`) also needs
    the turn to be one the person sent (``_turn_user_sent``): a loop wake, a cron or app
    injection, a ``session_send`` and a sub-agent completion are refused with
    403 ``not_user_turn``, and so is a user's turn once any text that is not the
    user's was steered into it (``chat_delivery.steer_into_running_turn``).
    """
    from kiro_crew.dashboard.handlers._shared import _read_session_key
    from kiro_crew.dashboard.session_control import (
        UNATTENDED_SLOT_PREFIXES,
        caller_slot_key,
    )

    sk = _read_session_key(request)
    if request.get("internal_auth") is not True:
        _audit(sk, operation, "denied", "internal secret required")
        raise GuideError(403, "internal_secret_required", "forbidden")
    app_name = request.get("app", "")
    if app_name:
        _audit(str(app_name), operation, "denied", "app callers cannot start guides")
        raise GuideError(403, "app_caller", "apps cannot guide the dashboard user")
    if not sk:
        _audit("anonymous", operation, "denied", "missing session key")
        raise GuideError(400, "missing_session_key", "missing X-Session-Key")
    if sk.startswith("subagent:"):
        _audit(sk, operation, "denied", "subagent caller")
        raise GuideError(
            403,
            "subagent_caller",
            "a subagent has no dashboard tab of its own; guide from the parent session",
        )
    from kiro_crew.member_memory_auth import session_key_is_attested

    if not await asyncio.to_thread(session_key_is_attested, request, sk):
        _audit(sk, operation, "denied", "unattested session")
        raise GuideError(403, "unattested_caller", "this session could not be verified")
    state = request.app["state"]
    slot_key = caller_slot_key(state, sk)
    slot = state.get_slot(slot_key) if slot_key else None
    if not slot_key or slot is None:
        _audit(sk, operation, "denied", "no live slot")
        raise GuideError(
            409,
            "no_live_slot",
            "this session is not open in a dashboard tab, so there is no one to guide",
        )
    if getattr(slot, "_app", ""):
        _audit(sk, operation, "denied", "app-scoped slot")
        raise GuideError(403, "app_scoped_caller", "app-scoped sessions cannot start guides")
    if slot_key.startswith(UNATTENDED_SLOT_PREFIXES):
        _audit(sk, operation, "denied", "unattended slot")
        raise GuideError(403, "unattended_caller", "scheduled runs cannot start guides")
    if not slot.turn_running:
        # The call came from a turn that is not this slot's: a channel's own
        # session answering a message the dashboard merely mirrors.
        _audit(sk, operation, "denied", "no dashboard turn")
        raise GuideError(
            409,
            "no_dashboard_turn",
            "this request did not come from a message sent in the dashboard",
        )
    if getattr(slot, "_turn_channel_origin", False) or getattr(
        slot, "_turn_channel_narrowed", False
    ):
        _audit(sk, operation, "denied", "channel turn")
        raise GuideError(
            403,
            "channel_caller",
            "this message came from a messaging channel, not the dashboard",
        )
    if operation in _USER_TURN_OPERATIONS and getattr(slot, "_turn_user_sent", False) is not True:
        _audit(sk, operation, "denied", "not a user turn")
        raise GuideError(
            403,
            "not_user_turn",
            "only a message the user sent in the dashboard can start this",
        )
    return slot_key, sk


async def api_guide_agent_actions(request: web.Request) -> web.Response:
    """GET /api/guide/agent/actions — the registered action catalog."""
    try:
        await _resolve_agent_caller(request, "guide.actions")
    except GuideError as exc:
        return _refusal(exc)
    await _catalogs_ready()
    return web.json_response({"actions": guide_catalog.list_actions()})


async def api_guide_agent_start(request: web.Request) -> web.Response:
    """POST /api/guide/agent/start — offer a guide in the caller's own tab.

    Body: ``{"actions": [{"id", "params", "note"?}, ...], "intro"?}``. No slot,
    session or tab field is read. The agent's ``intro``/``note`` text is checked
    by ``guide_catalog.clean_guide_text`` and never logged. Returns the Guide
    plus ``delivered_clients`` (0 means queued: the
    offer is held for ``GET /api/guide/pending``, not shown) and ``superseded``
    (the ids of this conversation's unfinished guides the offer replaced).
    """
    try:
        slot_key, sk = await _resolve_agent_caller(request, "guide.start")
        body = await _json_body(request)
        await _catalogs_ready()
        unknown = sorted(set(body) - {"actions", "intro"})
        if unknown:
            raise GuideError(400, "invalid_body", f"unknown field '{unknown[0][:64]}'")
        await _broadcast_swept(request)
        guide, superseded = _store(request).start_superseding(
            slot_key=slot_key,
            session_key=sk,
            actions=body.get("actions"),
            intro=body.get("intro"),
        )
    except GuideError as exc:
        return _refusal(exc)
    state = request.app["state"]
    # The replaced guide's row settles first, then the new offer's row lands at
    # this point of the transcript, before the frame that makes it live.
    for old in superseded:
        await _broadcast(state, old)
    chat_cards.record_guide_offered(state, guide)
    delivered = await _broadcast(state, guide)
    _audit(sk, "guide.start", "ok")
    return web.json_response(
        {
            **guide,
            "delivered_clients": delivered,
            "superseded": [g["guide_id"] for g in superseded],
        }
    )


async def api_guide_agent_status(request: web.Request) -> web.Response:
    """GET /api/guide/agent/status[?guide_id=] — the caller's own guide."""
    try:
        slot_key, _sk = await _resolve_agent_caller(request, "guide.status")
        await _broadcast_swept(request)
        guide = _store(request).status_for_caller(
            slot_key=slot_key, guide_id=request.query.get("guide_id") or None
        )
    except GuideError as exc:
        return _refusal(exc)
    return web.json_response(guide)


async def api_guide_agent_rename_self(request: web.Request) -> web.Response:
    """POST /api/guide/agent/rename — the calling crewmate renames itself.

    The member is the one whose pinned thread the verified caller's slot is,
    never a name from the body: the body carries only the new display name. Like
    every agent-half route it admits only a turn the user sent from the
    dashboard (:func:`_resolve_agent_caller`), so a channel message, a schedule,
    a subagent or an app cannot rename anyone.
    """
    import asyncio

    from kiro_crew import members as members_mod

    try:
        slot_key, sk = await _resolve_agent_caller(request, "member.rename_self")
        body = await _json_body(request)
    except GuideError as exc:
        return _refusal(exc)
    slot = request.app["state"].get_slot(slot_key)
    member = getattr(slot, "agent", "") if slot is not None else ""
    if (
        slot is None
        or getattr(slot, "mode", "") != members_mod.DM_SLOT_MODE
        or not isinstance(member, str)
        or not member
    ):
        _audit(sk, "member.rename_self", "denied", "not a crewmate thread")
        return _deny(403, "not_a_crewmate", "only a crewmate, in its own chat, can rename itself")
    from kiro_crew.memory_stores import (
        MemberAlreadyExists,
        MemberApprovalConflict,
        UnknownMemoryStore,
    )

    try:
        # The store the caller's thread runs on: a crewmate deleted and created
        # again under the same key has another, and its row is not this turn's.
        thread_store = getattr(slot, "memory_store", "") or "default"
        name = await asyncio.to_thread(
            members_mod.rename_member_display,
            member,
            body.get("name"),
            expected_store=str(thread_store),
        )
    except members_mod.SelfRenameError as exc:
        _audit(sk, "member.rename_self", "denied", exc.code)
        status = 403 if exc.code == "not_a_crewmate" else 400
        return _deny(status, exc.code, str(exc))
    except (UnknownMemoryStore, MemberAlreadyExists, MemberApprovalConflict):
        # The crewmate's row cannot be published from here, e.g. one declared only
        # in ``config.local.json``: a refusal the model can say, never a 500.
        _audit(sk, "member.rename_self", "denied", "member_not_writable")
        return _deny(
            409, "member_not_writable", "this crewmate's settings cannot be changed from the chat"
        )
    _audit(sk, "member.rename_self", "ok")
    return web.json_response({"ok": True, "member": member, "display_name": name})


async def api_guide_agent_cancel(request: web.Request) -> web.Response:
    """POST /api/guide/agent/cancel — retire the caller's own guide."""
    try:
        slot_key, sk = await _resolve_agent_caller(request, "guide.cancel")
        body = await _json_body(request)
        await _broadcast_swept(request)
        guide = _store(request).cancel_by_caller(slot_key=slot_key, guide_id=body.get("guide_id"))
    except GuideError as exc:
        return _refusal(exc)
    await _broadcast(request.app["state"], guide)
    _audit(sk, "guide.cancel", "ok")
    return web.json_response(guide)


async def api_guide_agent_observe(request: web.Request) -> web.Response:
    """POST /api/guide/agent/observe — ask the caller's tab what it shows now.

    Body: ``{"targets": [<curated location id>, ...]}`` (at most
    ``guide_observe.MAX_TARGETS``, each in this build's manifest). The reveal
    scopes those targets' plans use, and the runtime predicates their steps
    carry, are asked for too. Asked of the running
    guide's owner tab, else of the tab that sent this slot's latest message,
    and answered within ``guide_observe.OBSERVE_WAIT_SECONDS``: an
    ``observed`` result with one enum status per id, or ``not_observed`` with
    a ``reason`` (``no_tab``, ``stale_tab``, ``build_mismatch``). Never blocks
    longer, never stores the answer.
    """
    try:
        slot_key, sk = await _resolve_agent_caller(request, "guide.observe")
        body = await _json_body(request)
        await _catalogs_ready()
        targets, scopes, predicates = _observe_ids(body)
    except GuideError as exc:
        return _refusal(exc)
    state = request.app["state"]
    manifest = guide_catalog.ui_build_manifest()
    owned = _store(request).owner_tab_for_slot(slot_key)
    hub = observation_hub_for(state)
    tab = owned[1] if owned else hub.sender_for(slot_key)
    if not tab or not manifest.build_digest:
        return web.json_response({"status": NOT_OBSERVED, "reason": REASON_NO_TAB})
    # An auto location is observed through its one stamped render site: the
    # tab is asked for the site id and the answer is named back by location id.
    wire = tuple(manifest.auto_sites.get(t, t) for t in targets)
    back = {w: t for w, t in zip(wire, targets)}
    try:
        pending = hub.begin(
            tab_id=tab,
            targets=wire,
            scopes=scopes,
            build_digest=manifest.build_digest,
            predicates=predicates,
        )
    except GuideError as exc:
        return _refusal(exc)
    try:
        delivered = await _deliver_observe_request(state, pending)
        if delivered == 0:
            return web.json_response({"status": NOT_OBSERVED, "reason": REASON_NO_TAB})
        try:
            result = await asyncio.wait_for(
                asyncio.shield(pending.future), timeout=OBSERVE_WAIT_SECONDS
            )
        except asyncio.TimeoutError:
            if owned:
                noted = _store(request).note_stale_tab(owned[0])
                if noted is not None:
                    await _broadcast(state, noted)
            return web.json_response({"status": NOT_OBSERVED, "reason": REASON_STALE_TAB})
    finally:
        hub.end(pending.request_id)
    _audit(sk, "guide.observe", "ok")
    if isinstance(result, dict) and isinstance(result.get("targets"), list):
        renamed: list[Any] = []
        for t in result["targets"]:
            tid = t.get("id") if isinstance(t, dict) else None
            renamed.append({**t, "id": back.get(tid, tid)} if isinstance(tid, str) else t)
        result = {**result, "targets": renamed}
    return web.json_response(result)


#: ``source`` of :func:`api_guide_agent_language`'s answer.
UI_LOCALE_FROM_TAB = "tab"
UI_LOCALE_FROM_SETTING = "setting"
UI_LOCALE_UNKNOWN = "unknown"


def caller_ui_locale(state: Any, slot_key: str) -> tuple[str, str]:
    """``(tag, source)``: the language the caller's dashboard tab renders.

    The tab that sent this slot's latest message said so in ``X-UI-Lang``
    (``tab``); otherwise the configured ``dashboard.language`` (``setting``);
    otherwise ``("", "unknown")``: the dashboard follows the browser and no tab
    has said which language that is. Either tag names a shipped catalog.
    """
    tag = observation_hub_for(state).ui_lang_for(slot_key)
    if tag:
        return tag, UI_LOCALE_FROM_TAB
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.context import ui_language_tag

        configured = ui_language_tag(KiroCrewConfig.load())
    except Exception:
        configured = ""
    if configured:
        return configured, UI_LOCALE_FROM_SETTING
    return "", UI_LOCALE_UNKNOWN


async def api_guide_agent_language(request: web.Request) -> web.Response:
    """GET /api/guide/agent/language — which language the caller's dashboard shows.

    ``{"ui_lang": <shipped tag or "">, "source": "tab"|"setting"|"unknown"}``
    (:func:`caller_ui_locale`). ``find_ui`` asks it once per call so the labels
    it returns are spelled as the user's screen spells them, whatever language
    the conversation is in. Read-only, and nothing else about the tab crosses.
    """
    try:
        slot_key, _sk = await _resolve_agent_caller(request, "guide.language")
    except GuideError as exc:
        return _refusal(exc)
    tag, source = caller_ui_locale(request.app["state"], slot_key)
    return web.json_response({"ui_lang": tag, "source": source})


def _observe_ids(
    body: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Validate an observe request's ids against this build's manifest.

    Returns the targets, then the reveal scopes and the runtime predicates
    their plans use (bounded, derived from the manifest, never from the body).
    """
    unknown = sorted(set(body) - {"targets"})
    if unknown:
        raise GuideError(400, "invalid_body", f"unknown field '{unknown[0][:64]}'")
    raw = body.get("targets")
    if not isinstance(raw, list) or not raw:
        raise GuideError(400, "invalid_targets", "targets must be a non-empty list")
    if len(raw) > MAX_OBSERVE_TARGETS:
        raise GuideError(400, "too_many_targets", f"at most {MAX_OBSERVE_TARGETS} targets")
    manifest = guide_catalog.ui_build_manifest()
    seen: list[str] = []
    for item in raw:
        # A curated location, or an auto one the auto tier made pointable
        # (only those have a stamped site a tab can find).
        if not isinstance(item, str) or (
            item not in manifest.observable and item not in manifest.auto_sites
        ):
            raise GuideError(400, "unknown_target", "a target is not an observable location id")
        if item not in seen:
            seen.append(item)
    scopes = sorted({s for t in seen for s in manifest.plan_scopes.get(t, ())})
    predicates = sorted({x for t in seen for x in manifest.plan_predicates.get(t, ())})
    return (
        tuple(seen),
        tuple(scopes[:MAX_OBSERVE_SCOPES]),
        tuple(predicates[:MAX_OBSERVE_PREDICATES]),
    )


async def _deliver_observe_request(state: Any, pending: Any) -> int:
    deliver = getattr(state, "deliver_ws_owners", None)
    if deliver is None:
        return 0
    try:
        return int(
            await deliver(
                WS_GUIDE_OBSERVE,
                {
                    "request_id": pending.request_id,
                    "tab_id": pending.tab_id,
                    "targets": list(pending.targets),
                    "scopes": list(pending.scopes),
                    "predicates": list(pending.predicates),
                },
            )
        )
    except Exception:
        logger.debug("guide_observe delivery failed", exc_info=True)
        return 0


# ── browser half ──


async def _require_owner(request: web.Request, operation: str) -> web.Response | None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


async def api_guide_pending(request: web.Request) -> web.Response:
    """GET /api/guide/pending[?slot=] — rehydrate guides after a reload.

    Live guides, plus each slot's newest recently ended one that was not
    dismissed, so the slot's chat keeps its result line across a reload.
    """
    denied = await _require_owner(request, "guide.pending")
    if denied is not None:
        return denied
    slot = request.query.get("slot") or None
    if slot is not None and len(slot) > _SLOT_QUERY_MAX:
        return _deny(400, "invalid_slot", "slot is too long")
    await _broadcast_swept(request)
    return web.json_response({"guides": _store(request).pending(slot)})


def _browser_route(
    operation: str, act: Callable[[GuideStore, dict[str, Any]], dict[str, Any]]
) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def _route(request: web.Request) -> web.Response:
        denied = await _require_owner(request, operation)
        if denied is not None:
            return denied
        try:
            body = await _json_body(request)
            await _catalogs_ready()
            await _broadcast_swept(request)
            guide = act(_store(request), body)
        except GuideError as exc:
            return _refusal(exc)
        await _broadcast(request.app["state"], guide)
        return web.json_response(guide)

    _route.__name__ = f"api_{operation.replace('.', '_')}"
    return _route


api_guide_claim = _browser_route(
    "guide.claim",
    lambda store, b: store.claim(
        guide_id=b.get("guide_id"),
        tab_id=b.get("tab_id"),
        revision=b.get("revision"),
        take_over=b.get("take_over", False),
        placements=b.get("placements"),
    ),
)
api_guide_progress = _browser_route(
    "guide.progress",
    lambda store, b: store.progress(
        guide_id=b.get("guide_id"),
        tab_id=b.get("tab_id"),
        revision=b.get("revision"),
        action_index=b.get("action_index"),
        step_index=b.get("step_index"),
        outcome=b.get("outcome"),
        resume_step_index=b.get("resume_step_index"),
        detail=b.get("detail"),
        step_id=b.get("step_id"),
        resume_step_id=b.get("resume_step_id"),
        find=b.get("find"),
    ),
)
api_guide_heartbeat = _browser_route(
    "guide.heartbeat",
    lambda store, b: store.heartbeat(
        guide_id=b.get("guide_id"), tab_id=b.get("tab_id"), revision=b.get("revision")
    ),
)
api_guide_cancel = _browser_route(
    "guide.cancel_by_user",
    lambda store, b: store.cancel_by_tab(
        guide_id=b.get("guide_id"),
        tab_id=b.get("tab_id"),
        revision=b.get("revision"),
        reason=b.get("reason"),
    ),
)
#: Hides an ended guide's result line in every tab. Changes nothing else.
api_guide_dismiss = _browser_route(
    "guide.dismiss",
    lambda store, b: store.dismiss(guide_id=b.get("guide_id")),
)


#: A tab could not show the guide at all (``build_mismatch``). Only the
#: guide's reason moves; nothing is cancelled or advanced.
api_guide_refuse = _browser_route(
    "guide.refuse",
    lambda store, b: store.refuse(
        guide_id=b.get("guide_id"),
        tab_id=b.get("tab_id"),
        revision=b.get("revision"),
        reason=b.get("reason"),
    ),
)


#: Offers a completed show-me guide again from its first step (Show again).
api_guide_replay = _browser_route(
    "guide.replay",
    lambda store, b: store.replay(guide_id=b.get("guide_id"), revision=b.get("revision")),
)


#: The owning tab's viewport class changed: walk the current ``ui.show``
#: action by its new placement, only at a step boundary both share.
api_guide_replan = _browser_route(
    "guide.replan",
    lambda store, b: store.replan(
        guide_id=b.get("guide_id"),
        tab_id=b.get("tab_id"),
        revision=b.get("revision"),
        action_index=b.get("action_index"),
        placement=b.get("placement"),
    ),
)


async def api_guide_observe(request: web.Request) -> web.Response:
    """POST /api/guide/observe — one tab's reply to a ``guide_observe`` frame.

    Owner-only like every browser route. The body is validated field by field
    (:meth:`ObservationHub.deliver`): exactly the ids asked, enum states and
    booleans only, so no page text can ride along. Nothing is stored.
    """
    denied = await _require_owner(request, "guide.observe_reply")
    if denied is not None:
        return denied
    try:
        body = await _json_body(request)
        observation_hub_for(request.app["state"]).deliver(body)
    except GuideError as exc:
        return _refusal(exc)
    return web.json_response({"ok": True})


# ── mutation-step completion hook ──


def _response_json(resp: web.StreamResponse) -> dict[str, Any] | None:
    if not isinstance(resp, web.Response) or resp.status != 200:
        return None
    body = resp.body
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        data = json.loads(bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def _crewmate_evidence(resp: web.StreamResponse) -> dict[str, Any] | None:
    data = _response_json(resp)
    if not data or data.get("ok") is not True:
        return None
    member_id, name = data.get("member_id"), data.get("name")
    if not isinstance(member_id, str) or not member_id or not isinstance(name, str):
        return None
    return {"member_id": member_id, "name": name}


async def run_guided_crewmate_create(
    request: web.Request,
    impl: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Run the owner's ``POST /api/agents``, and credit a waiting guide on success.

    Without ``X-Guide-Id`` this is exactly ``impl(request)``. With it, the guide is
    associated BEFORE the handler runs (only for the dashboard owner, and only when
    the guide is on its ``crewmate.create`` step, owned by the named tab, at the
    named revision), and advanced afterwards only from the handler's own success
    response naming the created member.
    The handler's response is always returned unchanged: a guide never blocks,
    alters or fakes the save.
    """
    guide_id = request.headers.get(HEADER_GUIDE_ID)
    if not guide_id:
        return await impl(request)
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    state = request.app["state"]
    store = guide_store_for(state)
    token: str | None = None
    if is_owner_dashboard_request(request):
        token = store.begin_commit(
            guide_id=guide_id,
            tab_id=request.headers.get(HEADER_GUIDE_TAB),
            revision=request.headers.get(HEADER_GUIDE_REVISION),
            kind=KIND_CREWMATE_CREATE,
        )
    try:
        resp = await impl(request)
    except BaseException:
        store.abort_commit(token)
        raise
    if token is None:
        return resp
    try:
        evidence = await _crewmate_evidence(resp)
    except Exception:
        logger.debug("guide evidence extraction failed", exc_info=True)
        evidence = None
    if evidence is None:
        store.abort_commit(token)
        return resp
    for retired in store.retire_closed_slots(lambda key: state.get_slot(key) is not None):
        await _broadcast(state, retired)
    guide = store.finish_commit(token, evidence)
    if guide is not None:
        await _broadcast(state, guide)
    return resp


def register_guide_routes(app: web.Application) -> None:
    """Mount both halves. The agent half's prefix must stay strict-internal."""
    app.router.add_get("/api/guide/agent/actions", api_guide_agent_actions)
    app.router.add_post("/api/guide/agent/start", api_guide_agent_start)
    app.router.add_get("/api/guide/agent/status", api_guide_agent_status)
    app.router.add_post("/api/guide/agent/cancel", api_guide_agent_cancel)
    app.router.add_post("/api/guide/agent/rename", api_guide_agent_rename_self)
    app.router.add_post("/api/guide/agent/observe", api_guide_agent_observe)
    app.router.add_get("/api/guide/agent/language", api_guide_agent_language)
    app.router.add_get("/api/guide/pending", api_guide_pending)
    app.router.add_post("/api/guide/claim", api_guide_claim)
    app.router.add_post("/api/guide/progress", api_guide_progress)
    app.router.add_post("/api/guide/heartbeat", api_guide_heartbeat)
    app.router.add_post("/api/guide/cancel", api_guide_cancel)
    app.router.add_post("/api/guide/dismiss", api_guide_dismiss)
    app.router.add_post("/api/guide/replay", api_guide_replay)
    app.router.add_post("/api/guide/refuse", api_guide_refuse)
    app.router.add_post("/api/guide/replan", api_guide_replan)
    app.router.add_post("/api/guide/observe", api_guide_observe)
