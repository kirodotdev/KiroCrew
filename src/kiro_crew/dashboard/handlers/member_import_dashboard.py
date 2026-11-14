"""``/api/members/{slug}/dashboard/template`` -- the import card's dashboard step and its Undo.

A ``crewmate.import`` card whose bundle names a built-in dashboard the receiver
ships ends its plan with the POST: it adopts that page on the new Bot
(:func:`~kiro_crew.dashboard_templates.instance_adopt.adopt_if_empty`) and answers the
``instance_version`` it made. A slug that already holds a page (or a record this build
cannot read) is left alone: the answer is ``kept_existing`` with no version. The card's Undo runs the DELETE just before it deletes
the Bot: :func:`~kiro_crew.dashboard_templates.instance.remove_adopted` removes the page
only while it is still that template at that version, so a page the owner changed
since stays. Both are owner-only, and only from the card's own step
(:data:`REQ_CARD_IMPORT_DASHBOARD`, which only the card hook sets), so no other caller
picks or removes a Bot's dashboard through them. Only a template the product ships is
accepted. Deleting a Bot on its own never touches its dashboard. On a platform that
cannot pin the folder (native Windows) the adopt step checks the path with ``lstat``
and then writes by name, as the older local check no longer exists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers.member_goals import _body, _deny, _owner_member

#: Request attribute a ``crewmate.import`` card's dashboard step sets. Set ONLY on the
#: owner-authed card step (``change_cards._arm_card_undo_cron_guard``), never from a request.
REQ_CARD_IMPORT_DASHBOARD = "card_import_dashboard"

logger = logging.getLogger(__name__)


def _tell_open_tabs(request: web.Request, slug: str) -> None:
    """An open Dashboard tab of this Bot re-reads its page."""
    state = request.app.get("state")
    if state is not None:
        state.broadcast_ws("dashboard_instance_changed", {"slug": slug})


async def _card_template(
    request: web.Request, op: str
) -> tuple[str, dict[str, Any], str] | web.Response:
    """``(slug, body, template id)`` of an owner's card step naming a shipped template."""
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    who = await _owner_member(request, op, body.get("member"), write=True)
    if isinstance(who, web.Response):
        return who
    if request.get(REQ_CARD_IMPORT_DASHBOARD) is not True:
        return _deny(403, "card_only", "this runs only from the Bot's import card")
    template_id = body.get("template_id")
    if not isinstance(template_id, str) or not template_id:
        return _deny(400, "missing_template", "template_id is required")
    from kiro_crew.dashboard_templates import catalog

    try:
        entry = await asyncio.to_thread(catalog.load_one, template_id)
    except catalog.UnknownTemplate:
        return _deny(400, "unknown_template", "no shipped dashboard has this id")
    if entry.origin != catalog.BUILTIN_SOURCE:
        return _deny(400, "unknown_template", "no shipped dashboard has this id")
    return who[0], body, entry.id


async def api_member_import_dashboard(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/template -- ``{member, template_id}``."""
    got = await _card_template(request, "members.dashboard.import_template")
    if isinstance(got, web.Response):
        return got
    slug, _body_, template_id = got
    from kiro_crew.dashboard_templates import instance, instance_adopt

    try:
        adopted = await asyncio.to_thread(instance_adopt.adopt_if_empty, slug, template_id)
    except instance.InstanceError:  # includes InstanceRefused
        logger.info("import dashboard step refused: the dashboard could not be set")
        return _deny(409, "dashboard_not_set", "this dashboard could not be set")
    if adopted is None:
        # The slug already holds something (a Bot deleted earlier leaves its dashboard; a record
        # this build cannot read counts too): keep it untouched, adopt nothing and answer no
        # version, so the Undo has nothing of ours to remove.
        logger.info("import dashboard step kept what the slug already holds")
        return web.json_response(
            {"ok": True, "slug": slug, "template_id": template_id, "kept_existing": True}
        )
    _tell_open_tabs(request, slug)
    return web.json_response(
        {
            "ok": True,
            "slug": slug,
            "template_id": template_id,
            "instance_version": adopted.instance_version,
        }
    )


async def api_member_import_dashboard_undo(request: web.Request) -> web.Response:
    """DELETE /api/members/{slug}/dashboard/template -- ``{member, template_id, instance_version}``."""
    got = await _card_template(request, "members.dashboard.import_template_undo")
    if isinstance(got, web.Response):
        return got
    slug, body, template_id = got
    version = body.get("instance_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return _deny(400, "missing_version", "instance_version is required")
    from kiro_crew.dashboard_templates import instance

    try:
        removed = await asyncio.to_thread(instance.remove_adopted, slug, template_id, version)
    except instance.PinUnsupported:
        # No pinned walk here (native Windows): the page stays, as after a plain Bot
        # delete, and the rest of the Undo goes on.
        logger.info("import dashboard undo kept the page: no pinned directory walk here")
        return web.json_response({"ok": True, "slug": slug, "kept": True})
    except instance.InstanceError:  # includes InstanceRefused
        logger.info("import dashboard undo refused: the dashboard could not be removed")
        return _deny(409, "dashboard_not_removed", "this dashboard could not be removed")
    if not removed:
        logger.info("import dashboard undo refused: the page changed after the import")
        return _deny(
            409,
            "dashboard_changed",
            "Undo kept the dashboard because it changed after the import. Remove it from the Bot's Dashboard tab.",
        )
    _tell_open_tabs(request, slug)
    return web.json_response({"ok": True, "slug": slug})
