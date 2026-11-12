"""``POST /api/members/{slug}/dashboard/template`` -- the import card's dashboard step.

A ``crewmate.import`` card whose bundle names a built-in dashboard the receiver
ships ends its plan with this step: it adopts that page on the new Bot
(:func:`kiro_crew.dashboard_templates.instance.adopt`). Owner-only, and only from
the card's own apply step (:data:`REQ_CARD_IMPORT_DASHBOARD`, which only the card
hook sets), so no other caller picks a Bot's dashboard through it. Only a template
the product ships is accepted. Undo adds no step: removing the Bot removes its
dashboard with it.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard.handlers.member_goals import _body, _deny, _owner_member

#: Request attribute a ``crewmate.import`` card's dashboard step sets. Set ONLY on the
#: owner-authed card step (``change_cards._arm_card_undo_cron_guard``), never from a request.
REQ_CARD_IMPORT_DASHBOARD = "card_import_dashboard"


async def api_member_import_dashboard(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/template -- ``{member, template_id}``."""
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    who = await _owner_member(
        request, "members.dashboard.import_template", body.get("member"), write=True
    )
    if isinstance(who, web.Response):
        return who
    if request.get(REQ_CARD_IMPORT_DASHBOARD) is not True:
        return _deny(403, "card_only", "this runs only from the Bot's import card")
    template_id = body.get("template_id")
    if not isinstance(template_id, str) or not template_id:
        return _deny(400, "missing_template", "template_id is required")
    from kiro_crew.dashboard_templates import catalog, instance

    try:
        entry = await asyncio.to_thread(catalog.load_one, template_id)
    except catalog.UnknownTemplate:
        return _deny(400, "unknown_template", "no shipped dashboard has this id")
    if entry.origin != catalog.BUILTIN_SOURCE:
        return _deny(400, "unknown_template", "no shipped dashboard has this id")
    slug, _member, _cfg = who
    try:
        await asyncio.to_thread(instance.adopt, slug, entry.id)
    except instance.InstanceError:  # includes InstanceRefused
        return _deny(409, "dashboard_not_set", "this dashboard could not be set")
    return web.json_response({"ok": True, "slug": slug, "template_id": entry.id})
