"""A Bot's held limits on its Profile tab: ``GET /api/members/{slug}/held-limits`` and the release.

A held limit (``mate_held_rules``) makes the Bot ask a person before the actions it
covers. These routes let that person read them and end them all at once; only a
dashboard owner or Shared Crew teammate reaches them, never an app, tool or agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import mate_held_rules
from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.members import _deny_app_caller, _sel
from kiro_crew.mate_config_migration import LedgerUnreadable
from kiro_crew.members import MemberSlugError

logger = logging.getLogger(__name__)


def _unreadable() -> web.Response:
    return web.json_response(
        {"error": "the held limits could not be read", "code": "held_limits_unreadable"},
        status=500,
    )


async def _gate(request: web.Request, operation: str) -> web.Response | None:
    denied = await _deny_app_caller(request, "members.held_limits")
    if denied is not None:
        return denied
    owner_denied = await require_owner_dashboard_request(request, operation)
    if owner_denied is not None:
        return owner_denied
    try:
        members_mod.validate_slug(request.match_info["slug"])
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )
    return None


def _missing_member() -> web.Response:
    return web.json_response({"error": "member required", "code": "missing_member"}, status=400)


async def _bot(slug: str, member: str) -> KiroCrewConfig | web.Response:
    """The config when *member* is the registered Bot behind *slug*, else the refusal."""
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    try:
        matches = members_mod.member_slug(member, cfg) == slug
    except MemberSlugError:
        matches = False
    if not matches:
        return web.json_response(
            {"error": "member does not match slug", "code": "member_slug_mismatch"}, status=400
        )
    if member not in cfg.agents:
        return web.json_response(
            {"error": "no Bot for this slug", "code": "member_not_found"}, status=404
        )
    return cfg


async def api_member_held_limits_get(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/held-limits?member=<name> — the Bot's held limits."""
    refused = await _gate(request, "members.held_limits.read")
    if refused is not None:
        return refused
    member = request.query.get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        return _missing_member()
    cfg = await _bot(request.match_info["slug"], member)
    if isinstance(cfg, web.Response):
        return cfg
    member_id = str(cfg.agents[member].member_id or "")
    limits = await asyncio.to_thread(mate_held_rules.held_rules_of, member, member_id)
    if limits is None:
        return _unreadable()
    kiro_cli = False
    if limits:
        from kiro_crew.mate_restriction_carry import worker_on_kiro_cli

        kiro_cli = await asyncio.to_thread(worker_on_kiro_cli, cfg, member)
    return web.json_response({"limits": limits, "kiro_cli": kiro_cli})


async def api_member_held_limits_release(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/held-limits/release — end every held limit the page showed.

    Body ``{"member": <name>, "limits": [...]}``: the list the GET answered. Only
    while the record still holds exactly that list is it cleared; otherwise 409.
    """
    refused = await _gate(request, "members.held_limits.release")
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    member = body.get("member", "")
    if not members_mod.is_dispatchable_member_name(member):
        return _missing_member()
    shown: Any = body.get("limits")
    if not isinstance(shown, list) or not shown:
        return web.json_response(
            {"error": "limits must be the non-empty list shown", "code": "missing_limits"},
            status=400,
        )
    cfg = await _bot(request.match_info["slug"], member)
    if isinstance(cfg, web.Response):
        return cfg
    member_id = str(cfg.agents[member].member_id or "")
    try:
        released = await asyncio.to_thread(mate_held_rules.release_held, member, member_id, shown)
    except (LedgerUnreadable, OSError):
        logger.warning("held limits unreadable for %r", member, exc_info=True)
        return _unreadable()
    if not released:
        return web.json_response(
            {"error": "the held limits changed", "code": "held_limits_changed"}, status=409
        )
    texts = [str(r.get("text") or "") if isinstance(r, dict) else str(r) for r in shown]
    try:
        _sel().log_api_access(
            caller=str(request.get("user") or ""),
            operation="members.held_limits.release",
            outcome="allowed",
            source="dashboard",
            resources=f"bot={member} member_id={member_id} limits={'; '.join(texts)}",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for members.held_limits.release failed", exc_info=True)
    from kiro_crew.dashboard.crewmate_worker_reload import schedule_reload_workers

    # A live worker keeps the spec it was built with, so it is reloaded onto one
    # with the approvals the hold stripped.
    schedule_reload_workers(request.app.get("state"), member)
    return web.json_response({"released": shown})
