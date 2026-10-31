"""Owner routes for the People access card (``access_set``).

``GET /api/shared-crew/access`` returns the entries the first start carried over
from config and the first-share warning (real session and Crew memory counts,
whether this host is a laptop, and whether it can be kept awake). Tailscale
decides who gets in; Crew approves nobody itself.

Both routes are owner-only, with the same gate as the invite routes
(``routes_invites._owner_refusal``). The first invite while nobody else has
access is refused ``first_share_ack_required`` until the owner acknowledged the
warning, once per Crew.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.shared_crew import access_set, host_power
from kiro_crew.shared_crew.routes_invites import _actor, _error, _owner_refusal, preview_only

logger = logging.getLogger(__name__)

BASE = "/api/shared-crew/access"
ACK_PATH = f"{BASE}/first-share/ack"


def _access_failure(exc: access_set.AccessError) -> web.Response:
    status = {
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


@preview_only
async def api_access(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    body = await asyncio.to_thread(access_set.view)
    needs_ack = not body["first_share_ack"] and await asyncio.to_thread(
        access_set.nobody_else_has_access
    )
    body["first_share"] = {
        "needs_ack": needs_ack,
        **(await _counts(request)),
        "host": host_power.reported(),
    }
    return web.json_response(body, headers={"Cache-Control": "no-store"})


@preview_only
async def api_ack(request: web.Request) -> web.Response:
    refused = await _owner_refusal(request)
    if refused is not None:
        return refused
    try:
        await asyncio.to_thread(access_set.acknowledge_first_share, _actor(request))
    except access_set.AccessError as exc:
        return _access_failure(exc)
    return web.json_response({"acknowledged": True})


def register(app: web.Application) -> None:
    """Register the access routes on *app*."""
    app.router.add_get(BASE, api_access)
    app.router.add_post(ACK_PATH, api_ack)
