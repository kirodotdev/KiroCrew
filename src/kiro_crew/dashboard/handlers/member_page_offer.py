"""``POST /api/members/{slug}/dashboard/page-offer`` -- the yes on a page-offer card.

The ``crewmate.dashboard_page`` card's one apply step. It writes no page (asked-once
was written before the card was shown; the yes only marks that record pending until
its wake starts): it wakes the crewmate once to write,
``dashboard_save`` and ``dashboard_apply`` its page (:mod:`kiro_crew.dashboard.page_offer`).
Owner-only, and only from the card's own apply step (:data:`REQ_CARD_PAGE_OFFER_APPLY`,
which only the card hook sets), so no other caller can start that wake.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard import page_offer
from kiro_crew.dashboard.handlers.member_goals import _body, _deny, _owner_member

#: Request attribute a ``crewmate.dashboard_page`` card's Apply step sets. Set ONLY
#: on the owner-authed card step (``change_cards._arm_card_undo_cron_guard``).
REQ_CARD_PAGE_OFFER_APPLY = "card_page_offer_apply"
#: The page the card's yes was given over (its last previewed snapshot), set beside
#: :data:`REQ_CARD_PAGE_OFFER_APPLY` by the same hook.
REQ_CARD_PAGE_OFFER_PAGE = "card_page_offer_page"


async def api_member_page_offer(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/page-offer -- ``{member}``: the person said yes."""
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    who = await _owner_member(
        request, "members.dashboard.page_offer", body.get("member"), write=True
    )
    if isinstance(who, web.Response):
        return who
    if request.get(REQ_CARD_PAGE_OFFER_APPLY) is not True:
        return _deny(403, "card_only", "this runs only from the crewmate's page-offer card")
    slug, member, _cfg = who
    # The asked-once record was written before the card was shown; the yes only marks
    # it pending until the wake starts, best effort, so this step has no write that
    # can fail. Off the loop: the mark is an fsynced write.
    await asyncio.to_thread(page_offer.record_yes, slug)
    page_offer.schedule_write(
        request.app.get("state"), slug, member, page=request.get(REQ_CARD_PAGE_OFFER_PAGE)
    )
    return web.json_response({"ok": True, "slug": slug, "member": member, "scheduled": True})
