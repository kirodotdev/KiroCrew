"""Owner routes for the derived Tailscale access set (``access_set``).

``GET /api/shared-crew/access`` is the People access card: who is waiting for
approval, who the owner approved, the entries the first start carried over from
config, and the first-share warning (real session and Crew memory counts,
whether this host is a laptop, and whether it can be kept awake).

Approve, Dismiss, removing an approved person and acknowledging the first-share
warning are owner-only, with the same gate as the invite routes
(``routes_invites._owner_refusal``): a teammate the set admits cannot approve
anyone. The first approve (or invite) while nobody else has access is refused
``first_share_ack_required`` until the owner acknowledged the warning, once per
Crew.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiohttp import WSCloseCode, web

from kiro_crew.shared_crew import access_set, host_power
from kiro_crew.shared_crew.routes_invites import WS_ACTOR_KEY, _actor, _error, _owner_refusal

logger = logging.getLogger(__name__)

BASE = "/api/shared-crew/access"
ACK_PATH = f"{BASE}/first-share/ack"
APPROVE_PATH = f"{BASE}/waiting/{{user_id}}/approve"
DISMISS_PATH = f"{BASE}/waiting/{{user_id}}/dismiss"
APPROVED_PATH = f"{BASE}/approved/{{user_id}}"
#: Approve, Dismiss and Remove while ``local_authorization`` is off.
CODE_LOCAL_AUTHORIZATION_OFF = "local_authorization_off"
_USER_ID_RE = re.compile(r"\A[1-9][0-9]{0,19}\Z")


def _user_id(request: web.Request) -> str:
    raw = request.match_info.get("user_id", "")
    return raw if _USER_ID_RE.match(raw) else ""


def _access_failure(exc: access_set.AccessError) -> web.Response:
    status = {
        access_set.CODE_NOT_WAITING: 404,
        access_set.CODE_NOT_APPROVED: 404,
        access_set.CODE_ACK_REQUIRED: 409,
        access_set.CODE_UNREADABLE: 503,
    }.get(exc.code, 400)
    return _error(status, exc.code)


async def _counts(request: web.Request) -> dict[str, Any]:
    """Real counts for the first-share warning; a count that cannot be read is ``None``."""
    from kiro_crew.shared_crew.routes_identity import _catalog_rows, _machine_only

    sessions: int | None
    try:
        rows = await _catalog_rows(request)
        sessions = sum(1 for r in rows if not _machine_only(str(r.get("key") or "")))
    except Exception:  # noqa: BLE001 -- an unknown count is shown as unknown
        sessions = None
    memory: int | None = None
    state = request.app.get("state")
    if state is not None:
        try:
            from kiro_crew.dashboard.handlers.memory import _get_vector_store_async

            vector = await _get_vector_store_async(state)
            if vector is not None:
                stats = await asyncio.to_thread(vector.memory_stats)
                memory = int(stats.get("semantic_active") or 0) + int(
                    stats.get("episodic_active") or 0
                )
        except Exception:  # noqa: BLE001
            memory = None
    return {"sessions": sessions, "memory_items": memory}


async def _owner_or_local_refusal(request: web.Request) -> web.Response | None:
    """The owner gate, then 409 ``local_authorization_off`` while Tailscale alone decides."""
    refused = await _owner_refusal(request)
    if refused is None and not access_set.local_authorization():
        return _error(409, CODE_LOCAL_AUTHORIZATION_OFF)
    return refused


async def api_access(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    body = await asyncio.to_thread(access_set.view)
    body["local_authorization"] = access_set.local_authorization()
    needs_ack = not body["first_share_ack"] and await asyncio.to_thread(
        access_set.nobody_else_has_access
    )
    body["first_share"] = {
        "needs_ack": needs_ack,
        **(await _counts(request)),
        "host": host_power.reported(),
    }
    return web.json_response(body, headers={"Cache-Control": "no-store"})


async def api_ack(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    try:
        await asyncio.to_thread(access_set.acknowledge_first_share, _actor(request))
    except access_set.AccessError as exc:
        return _access_failure(exc)
    return web.json_response({"acknowledged": True})


async def api_approve(request: web.Request) -> web.Response:
    refused = await _owner_or_local_refusal(request)
    if refused is not None:
        return refused
    user_id = _user_id(request)
    if not user_id:
        return _error(400, "bad_user_id")
    from kiro_crew.shared_crew.tailscale_invites import _commit_lock

    try:
        # The removal holds this lock through its fence and work stop.
        async with _commit_lock():
            row = await asyncio.to_thread(access_set.approve, user_id, _actor(request))
    except access_set.AccessError as exc:
        return _access_failure(exc)
    return web.json_response({"approved": row})


async def api_dismiss(request: web.Request) -> web.Response:
    refused = await _owner_or_local_refusal(request)
    if refused is not None:
        return refused
    user_id = _user_id(request)
    if not user_id:
        return _error(400, "bad_user_id")
    try:
        await asyncio.to_thread(access_set.dismiss, user_id, _actor(request))
    except access_set.AccessError as exc:
        return _access_failure(exc)
    return web.json_response({"dismissed": user_id})


async def api_remove(request: web.Request) -> web.Response:
    refused = await _owner_or_local_refusal(request)
    if refused is not None:
        return refused
    user_id = _user_id(request)
    if not user_id:
        return _error(400, "bad_user_id")
    try:
        result = await access_set.remove_approval(user_id, _actor(request))
    except access_set.AccessError as exc:
        return _access_failure(exc)
    return web.json_response(result)


def _socket_matches(ws: Any, principal_ids: frozenset[str], login: str) -> bool:
    from kiro_crew.shared_crew.contracts import PrincipalKind
    from kiro_crew.shared_crew.identity import PROVIDER_TAILSCALE

    principal = getattr(ws.get(WS_ACTOR_KEY), "principal", None)
    if getattr(principal, "kind", None) is PrincipalKind.UNKNOWN and ws.get("_is_dashboard_user"):
        # Admitted with no named person (a forwarded request the daemon could
        # not attribute): it may be theirs, so it reconnects and is checked again.
        return True
    if principal is None or getattr(principal, "provider", "") != PROVIDER_TAILSCALE:
        return False
    if getattr(principal, "id", "") in principal_ids:
        return True
    shown = str(getattr(getattr(principal, "display", None), "login", "") or "")
    return bool(login) and shown.strip().lower() == login.strip().lower()


async def close_sockets(app: web.Application, principal_ids: frozenset[str], login: str) -> int:
    """Close every dashboard socket whose verified actor is the removed person."""
    state = app.get("state")
    closed = 0
    for ws in list(getattr(state, "_ws_clients", None) or ()):
        try:
            if not ws.closed and _socket_matches(ws, principal_ids, login):
                await ws.close(code=WSCloseCode.POLICY_VIOLATION, message=b"access removed")
                closed += 1
        except Exception:  # noqa: BLE001 -- one socket never blocks the rest
            logger.debug("closing a removed person's socket failed", exc_info=True)
    return closed


def _drop_queued(state: Any, slot: Any, principal_ids: frozenset[str]) -> int:
    """Drop *slot*'s queued messages a removed person sent that no admitted work covers."""
    from kiro_crew.dashboard import responsive_chat
    from kiro_crew.dashboard.chat_utils_injected_rows import _remove_queued_by_id
    from kiro_crew.shared_crew.admission_entry import QUEUED_WORK_META_KEY
    from kiro_crew.shared_crew.attribution import message_author

    dropped = 0
    for entry in list(getattr(slot, "_queue", None) or ()):
        if not isinstance(entry, dict):
            continue
        meta = entry.get("meta")
        if message_author(meta if isinstance(meta, dict) else None).id not in principal_ids:
            continue
        if isinstance(meta, dict) and meta.get(QUEUED_WORK_META_KEY):
            # Parked admitted work: the admission service cancels it.
            continue
        if responsive_chat.row_already_written(slot, entry):
            continue
        queue_id = str(entry.get("id") or "")
        if not queue_id or slot.queue_remove_by_id(queue_id) is None:
            continue
        _remove_queued_by_id(slot.messages, queue_id)
        # The text never reaches anyone's composer: the sender's sockets are closed.
        state.broadcast_ws("queue_cancel", {"slot": slot.key, "queue_id": queue_id, "content": ""})
        dropped += 1
    return dropped


async def stop_slot_work(app: web.Application, principal_ids: frozenset[str]) -> dict[str, int]:
    """Drop a removed person's queued messages, then stop the turns they started.

    Covers the work no admitted record holds (fair admission off). Queued
    messages go first, so a stopped turn's queue drain cannot start one.
    """
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn
    from kiro_crew.shared_crew.turn_gate import cancel_unadmitted, unadmitted_slots

    state = app.get("state")
    slots = getattr(state, "_slots", None)
    if state is None or not isinstance(slots, dict):
        return {"queued": 0, "running": 0}
    queued = 0
    for slot in list(slots.values()):
        try:
            queued += _drop_queued(state, slot, principal_ids)
        except Exception:  # noqa: BLE001 -- one slot never blocks the rest
            logger.debug("dropping a removed person's queued message failed", exc_info=True)
    running = 0
    for key in unadmitted_slots(principal_ids):
        slot = slots.get(key)
        if slot is None:
            running += int(cancel_unadmitted(key))
            continue
        if not slot.running:
            continue
        try:
            result = await stop_slot_turn(
                state, slot, source="shared_crew_access_removed", escalate=False
            )
            if isinstance(result, dict) and result.get("compacting"):
                # A stop declined while the session compacts stopped nothing:
                # cancel the turn task charged to them instead, and count only that.
                stopped = cancel_unadmitted(key)
                if not stopped:
                    logger.warning("a removed person's turn on %s was not stopped", key)
                running += int(stopped)
            else:
                running += 1
        except Exception:  # noqa: BLE001 -- one slot never blocks the rest
            logger.warning("stopping a removed person's turn failed", exc_info=True)
    if queued or running:
        state.push_slots_update()
    return {"queued": queued, "running": running}


async def _install_closer(app: web.Application) -> None:
    async def close(principal_ids: frozenset[str], login: str) -> int:
        return await close_sockets(app, principal_ids, login)

    async def stop(principal_ids: frozenset[str]) -> dict[str, int]:
        return await stop_slot_work(app, principal_ids)

    access_set.socket_closer = close
    access_set.slot_work_stopper = stop


def register(app: web.Application) -> None:
    """Register the access routes on *app*."""
    app.router.add_get(BASE, api_access)
    app.router.add_post(ACK_PATH, api_ack)
    app.router.add_post(APPROVE_PATH, api_approve)
    app.router.add_post(DISMISS_PATH, api_dismiss)
    app.router.add_delete(APPROVED_PATH, api_remove)
    install_closer(app)


def install_closer(app: web.Application) -> None:
    app.on_startup.append(_install_closer)
