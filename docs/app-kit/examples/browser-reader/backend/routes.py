"""Browser Reader's in-gateway routes: read and click the page of its own chat slot.

Two routes under ``/api/apps/browser-reader/``. Both go through ``ctx.browser``, so the
owner's approval of ``read`` and ``act``, the preview and every gate the Bot's own
``browser`` tool passes apply at each call. The app imports nothing from ``kiro_crew``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import web


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: Callable[..., Any]


def _page(request: web.Request, ctx: Any) -> tuple[Any, web.Response | None]:
    """The page of the slot in the path, or the answer that says why there is none."""
    browser = getattr(ctx, "browser", None)
    if browser is None:
        body = {"ok": False, "code": "preview_off", "error": "App browser access is off"}
        return None, web.json_response(body)
    try:
        return browser.page(request.match_info["slot"], state=request.app["state"]), None
    except PermissionError as exc:  # BrowserRefused: not one of this app's slots
        body = {"ok": False, "code": getattr(exc, "code", "not_your_slot"), "error": str(exc)}
        return None, web.json_response(body)


async def outline(request: web.Request, ctx: Any) -> web.Response:
    """``GET /slots/{slot}/outline``: the page outline the Bot reads, cut to 2000 characters."""
    page, refused = _page(request, ctx)
    if refused is not None:
        return refused
    return web.json_response(await page.outline(budget=2000))


async def click(request: web.Request, ctx: Any) -> web.Response:
    """``POST /slots/{slot}/click`` with ``{"ref": "<ref from the outline>"}``."""
    page, refused = _page(request, ctx)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except ValueError:  # an empty or malformed body is answered like a missing ref
        body = None
    ref = body.get("ref") if isinstance(body, dict) else None
    if not isinstance(ref, str) or not ref:
        return web.json_response({"ok": False, "code": "bad_ref", "error": "ref is required"})
    return web.json_response(await page.click(ref))


def register_routes(ctx: Any) -> list[Route]:
    return [
        Route("GET", "/slots/{slot}/outline", outline),
        Route("POST", "/slots/{slot}/click", click),
    ]
