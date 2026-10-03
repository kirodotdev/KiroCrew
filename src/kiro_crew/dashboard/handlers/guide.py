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

* **Browser half** (``/api/guide/pending|claim|progress|heartbeat|cancel``) is
  cookie-authed and OWNER-only (``require_owner_dashboard_request``, which also
  refuses app tokens). It reports what a tab observed; it can never complete a
  mutation step.

Mutation steps complete through :func:`run_guided_crewmate_create`, a narrow post-success
hook the existing owner-only ``POST /api/agents`` route is wrapped in: the tab
names the guide in ``X-Guide-Id`` / ``X-Guide-Tab`` / ``X-Guide-Revision``, the
hook associates the request BEFORE the handler runs, and advances the guide only
from the identity THAT handler returned on success. Nothing the client says about
success is read.

``guide_update`` WebSocket frames go to owner sockets only.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew import guide_catalog
from kiro_crew.dashboard.guide_runs import GuideError, GuideStore, guide_store_for
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

WS_GUIDE_UPDATE = "guide_update"

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


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise GuideError(400, "invalid_json", "invalid JSON body") from None
    if not isinstance(body, dict):
        raise GuideError(400, "invalid_body", "body must be a JSON object")
    return body


# ── agent half ──


def _resolve_agent_caller(request: web.Request, operation: str) -> tuple[str, str]:
    """The verified caller's ``(slot_key, session_key)``. Raises :class:`GuideError`."""
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
    return slot_key, sk


async def api_guide_agent_actions(request: web.Request) -> web.Response:
    """GET /api/guide/agent/actions — the registered action catalog."""
    try:
        _resolve_agent_caller(request, "guide.actions")
    except GuideError as exc:
        return _refusal(exc)
    return web.json_response({"actions": guide_catalog.list_actions()})


async def api_guide_agent_start(request: web.Request) -> web.Response:
    """POST /api/guide/agent/start — offer a guide in the caller's own tab.

    Body: ``{"actions": [{"id", "params"}, ...]}``. No slot, session or tab field
    is read. Returns the Guide plus ``delivered_clients`` (0 means queued: the
    offer is held for ``GET /api/guide/pending``, not shown).
    """
    try:
        slot_key, sk = _resolve_agent_caller(request, "guide.start")
        body = await _json_body(request)
        unknown = sorted(set(body) - {"actions"})
        if unknown:
            raise GuideError(400, "invalid_body", f"unknown field '{unknown[0][:64]}'")
        await _broadcast_swept(request)
        guide = _store(request).start(
            slot_key=slot_key, session_key=sk, actions=body.get("actions")
        )
    except GuideError as exc:
        return _refusal(exc)
    delivered = await _broadcast(request.app["state"], guide)
    _audit(sk, "guide.start", "ok")
    return web.json_response({**guide, "delivered_clients": delivered})


async def api_guide_agent_status(request: web.Request) -> web.Response:
    """GET /api/guide/agent/status[?guide_id=] — the caller's own guide."""
    try:
        slot_key, _sk = _resolve_agent_caller(request, "guide.status")
        await _broadcast_swept(request)
        guide = _store(request).status_for_caller(
            slot_key=slot_key, guide_id=request.query.get("guide_id") or None
        )
    except GuideError as exc:
        return _refusal(exc)
    return web.json_response(guide)


async def api_guide_agent_cancel(request: web.Request) -> web.Response:
    """POST /api/guide/agent/cancel — retire the caller's own guide."""
    try:
        slot_key, sk = _resolve_agent_caller(request, "guide.cancel")
        body = await _json_body(request)
        await _broadcast_swept(request)
        guide = _store(request).cancel_by_caller(slot_key=slot_key, guide_id=body.get("guide_id"))
    except GuideError as exc:
        return _refusal(exc)
    await _broadcast(request.app["state"], guide)
    _audit(sk, "guide.cancel", "ok")
    return web.json_response(guide)


# ── browser half ──


async def _require_owner(request: web.Request, operation: str) -> web.Response | None:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, operation)


async def api_guide_pending(request: web.Request) -> web.Response:
    """GET /api/guide/pending[?slot=] — rehydrate live guides after a reload."""
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
        guide_id=b.get("guide_id"), tab_id=b.get("tab_id"), revision=b.get("revision")
    ),
)


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
    app.router.add_get("/api/guide/pending", api_guide_pending)
    app.router.add_post("/api/guide/claim", api_guide_claim)
    app.router.add_post("/api/guide/progress", api_guide_progress)
    app.router.add_post("/api/guide/heartbeat", api_guide_heartbeat)
    app.router.add_post("/api/guide/cancel", api_guide_cancel)
