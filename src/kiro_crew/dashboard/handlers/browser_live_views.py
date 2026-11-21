"""``/api/browser/live/views/*``: a second live view of one of an app slot's own tabs.

What an app page's ``BrowserView`` with a ``tabId`` calls (RFC app-sdk-browser,
section 5), for two tabs side by side. All ``POST``, JSON body:

* ``/api/browser/live/views/tabs`` ``{"session_key"}``: the slot's own tabs,
  ``{"tabs": [{"tab_id", "url", "title", "current"}]}``, through the one browser
  door (``browser_ops.run_browser_op``, the ``tabs`` op), so the slot's tab ledger,
  its ``browserSites`` and ``capabilities.browse`` apply as to the Bot;
* ``/api/browser/live/views/open`` ``{"session_key", "tab_id"}``: start a view of
  that tab, ``{"view_key"}``. The page then shows it through the card's own routes
  under ``view_key``, which accept a view on the look-only legs only;
* ``/api/browser/live/views/close`` ``{"session_key": <view_key>}``: end it,
  ``{"closed"}``. Ending always answers, like the card's close.

Who may call, in order: the card routes' own gates (``browser_live._run``: the
internal secret refused, the dashboard owner only, ``capabilities.browse``, a slot
this dashboard holds, and 403 ``app_disabled`` while the slot's app is turned off);
then, for ``tabs`` and ``open``, an app slot (404 ``not_app_slot``), the
``app_browser_sdk`` preview on (404 ``preview_off``) and the owner's approval of
the app's ``view`` verb (403 ``permission_denied``). ``tab_id`` must be one of the
slot's own tabs (``LiveHub.targets_of``) that the browser still lists, else 404
``not_agent_tab``: never another app's, another chat's or the person's own tab.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from aiohttp import web

from kiro_crew.browser_harness import live_hub_views
from kiro_crew.browser_harness.live_hub import LiveHub
from kiro_crew.dashboard.handlers import browser_live

#: A CDP target id: hex in Chromium, bounded either way.
_TAB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _slot_app(request: web.Request, key: str) -> str:
    slots = getattr(request.app["state"], "_slots", None) or {}
    return str(getattr(slots.get(key), "_app", "") or "")


async def _view_refusal(request: web.Request, key: str) -> web.Response | None:
    """An app slot, the preview on, and the app's ``view`` verb approved, or why not."""
    from kiro_crew.apps.browser_sdk import app_browser_verbs, preview_on  # noqa: PLC0415

    app = _slot_app(request, key)
    if not app:
        return _error("not_app_slot", "a second view is for an app's own slot", 404)
    if not await asyncio.to_thread(preview_on):
        return _error("preview_off", "app browser access is turned off", 404)
    if "view" not in await asyncio.to_thread(app_browser_verbs, app):
        return _error(
            "permission_denied", f"the owner has not approved browser view for {app!r}", 403
        )
    return None


async def api_browser_live_views_tabs(request: web.Request) -> web.Response:
    """POST /api/browser/live/views/tabs: the slot's own tabs, to pick one to show."""

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        refused = await _view_refusal(request, key)
        if refused is not None:
            return refused
        if not hub.targets_of(key):
            return {"tabs": []}
        from kiro_crew.apps.browser_page_port import _flat, _redacted  # noqa: PLC0415
        from kiro_crew.dashboard.handlers.browser_ops import (  # noqa: PLC0415
            GatewayCaller,
            run_browser_op,
        )

        answer, _status = await run_browser_op(
            f"dashboard:{key}",
            "tabs",
            {"action": "list"},
            state=request.app["state"],
            caller=GatewayCaller(f"app:{_slot_app(request, key)}", path=request.path),
        )
        out = _flat(answer)
        if out.get("ok") is False:
            # Redact the error text so a browser op failure cannot expose credentials.
            return _error(
                str(out.get("code") or "browser_error"),
                str(_redacted(str(out.get("error") or ""))),
                409,
            )
        sess = hub.get(key)
        current = sess.target_id if sess is not None else ""
        tabs = [
            {
                "tab_id": t.get("target_id"),
                # Redact url and title: tab metadata is passed to app pages and
                # may contain credentials embedded in the page address.
                # _redacted runs both redact_exfiltration_urls and redact_credentials.
                "url": str(_redacted(str(t.get("url") or ""))),
                "title": str(_redacted(str(t.get("title") or ""))),
                "current": t.get("target_id") == current,
            }
            for t in out.get("tabs") or []
            if isinstance(t, dict) and isinstance(t.get("target_id"), str)
        ]
        return {"tabs": tabs}

    return await browser_live._run(request, "browser_live_views_tabs", act)


async def api_browser_live_views_open(request: web.Request) -> web.Response:
    """POST /api/browser/live/views/open: show one of the slot's own tabs in a second view."""

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        tab_id = body.get("tab_id")
        if not isinstance(tab_id, str) or not _TAB_ID_RE.fullmatch(tab_id):
            return _error("invalid_tab", "tab_id must be one of this slot's tabs", 400)
        refused = await _view_refusal(request, key)
        if refused is not None:
            return refused
        view_key = live_hub_views.free_view_key(hub, key)
        if not view_key:
            return _error(
                "view_cap", f"this slot already has {live_hub_views.VIEW_CAP} views open", 409
            )
        browser_live.refresh_live_config(hub)
        try:
            stream = await live_hub_views.start_view(hub, key, view_key, tab_id)
        except LookupError as exc:
            return _error(str(exc) or "not_agent_tab", "that tab cannot be shown in a view", 404)
        if stream is None:
            return _error("live_off", "the live view is turned off", 409)
        view_sess = hub._sessions.get(view_key)
        return {"view_key": view_key, "turn_id": view_sess.turn_id if view_sess else ""}

    return await browser_live._run(request, "browser_live_views_open", act)


async def api_browser_live_views_close(request: web.Request) -> web.Response:
    """POST /api/browser/live/views/close: end a second view. ``closed`` false for any other key.

    The body must carry ``turn_id`` matching the view's current turn so a stale
    component subscribed to a recycled key cannot close a later view.
    """

    async def act(hub: LiveHub, key: str, body: dict[str, Any]) -> dict[str, Any] | web.Response:
        turn_id = body.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return _error("missing_turn_id", "turn_id is required to close a view", 400)
        sess = hub._sessions.get(key)
        if sess is None or not sess.view_of or sess.state == "done":
            return {"closed": False}
        if sess.turn_id != turn_id:
            return {"closed": False, "stale": True}
        return {"closed": await live_hub_views.stop_view(hub, key)}

    return await browser_live._run(
        request, "browser_live_views_close", act, policy=browser_live.UNGATED, views=True
    )


__all__ = [
    "api_browser_live_views_close",
    "api_browser_live_views_open",
    "api_browser_live_views_tabs",
]
