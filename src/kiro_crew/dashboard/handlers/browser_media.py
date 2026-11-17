"""``/api/browser/media``: the owner's play, pause, seek and volume buttons on an app page.

``POST`` body ``{"session_key", "action", "value"?}``, ``action`` one of ``play``,
``pause``, ``seek``, ``volume``; ``value`` is seconds for ``seek`` and 0-1 for
``volume``. ``GET ?session_key=`` reads the state: a GET, so a player's poll writes no
row through the mutating-call audit middleware. Answers ``{"ok": true, "session_key", "media": {...}}`` with the
element's state (``paused``, ``currentTime``, ``duration``, ``volume``, ``muted``),
or ``{"ok": false, "code", "error"}`` with HTTP 200 when the tab said no
(``no_media``, ``redirect_refused``, ``bad_value``), so the page reads one shape.

Who may call it, in order, like the live card's routes (``handlers/browser_live.py``):

* the internal secret is refused (403 ``internal_secret_required``): it is how the
  agent's tools reach the gateway, and these buttons are the person's, not the
  agent's;
* only the dashboard owner (403 ``owner_only``);
* ``capabilities.browse`` (403 ``policy_denied``);
* ``session_key`` must name a slot this dashboard holds (404 ``unknown_session``),
  owned by an app that holds approved ``browserSites`` (403 ``not_app_browser``),
  so this drives only a tab such an app's own agent opened.

It never starts the browser: with none running, or no tab bound to the session, it
answers 409 ``no_tab``. While the person has taken the tab over it answers
``user_in_control`` and touches nothing. It takes the same gateway-wide op lock and the
same per-session takeover fence the agent's ops do
(``browser_ops._OP_LOCK``), switches the daemon to the session's own tab without
raising it, and runs the walker's ``media`` method in the isolated world
(``browser_harness/media.py``). The page is judged first, so one off the app's
pages is blanked and refused.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps import browser_sites
from kiro_crew.browser_harness import media
from kiro_crew.browser_harness.budgets import LOCK_WAIT_S, OP_TIMEOUT_S
from kiro_crew.browser_harness.governance import DASHBOARD_SURFACE_KEY
from kiro_crew.browser_harness.live_hub import get_live_hub
from kiro_crew.dashboard.handlers import browser_live, browser_ops
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.browser_policy import browse_refusal

logger = logging.getLogger(__name__)

#: Largest body; a real one is under 100 bytes.
_MAX_BODY_BYTES = 1024


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _value(action: str, raw: Any) -> tuple[bool, float | None]:
    """``(ok, value)`` for *action*: a finite number for seek/volume, nothing otherwise."""
    if action not in ("seek", "volume"):
        return True, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False, None
    try:
        value = float(raw)
    except OverflowError:  # an int too large for a float is out of range anyway
        return False, None
    if value != value or value < 0 or (action == "volume" and value > 1):
        return False, None
    if action == "seek" and value > 7 * 24 * 3600:
        return False, None
    return True, value


def _running_supervisor() -> Any:
    """The gateway's supervisor when its browser is up, else ``None``. Starts nothing."""
    if browser_live._current_relay() is None:
        return None
    from kiro_crew.config.paths import config_dir

    sup = importlib.import_module("kiro_crew.browser_harness.supervisor")
    return sup.get_supervisor(config_dir())


async def api_browser_media(request: web.Request) -> web.Response:
    """POST /api/browser/media (an action) and GET (``state``) -- see the module docstring."""
    refusal = await browser_live._owner_gate(request, "browser_media")
    if refusal is not None:
        return refusal
    refusal = await browse_refusal(audit=False)
    if refusal is not None:
        return refusal
    if request.method == "GET":
        # The read a player polls: a GET, so the mutating-call audit middleware writes
        # no row per poll; it changes nothing to audit.
        body: dict[str, Any] = {"session_key": request.query.get("session_key"), "action": "state"}
    else:
        parsed, err = await read_bounded_json(request, max_bytes=_MAX_BODY_BYTES)
        if err is not None or parsed is None:
            return err if err is not None else _error("invalid_json", "invalid JSON", 400)
        body = parsed
    key = browser_live._owned_session_key(request, body)
    if key is None:
        return _error("unknown_session", "session_key does not name an open session", 404)
    action = body.get("action")
    allowed = ("state",) if request.method == "GET" else media.ACTIONS[1:]
    if not isinstance(action, str) or action not in allowed:
        return _error("bad_action", f"action must be one of: {', '.join(allowed)}", 400)
    ok, value = _value(action, body.get("value"))
    if not ok:
        return _error("bad_value", "value must be seconds for seek, or 0 to 1 for volume", 400)
    sites = await browser_ops.session_sites(request, key)
    if not sites or sites == browser_sites.NO_PAGES:
        return _error("not_app_browser", "this session is not an app's own browsing session", 403)
    hub = get_live_hub()
    supervisor = _running_supervisor()
    if supervisor is None or not hub.target_of(key):
        return _error("no_tab", "this session has no open browser tab", 409)
    if hub.in_control(key):
        # The person holds this tab: a read or a click now could blank or move the
        # page under their hands.
        return _error("user_in_control", "you are using this browser tab", 409)
    try:
        await asyncio.wait_for(browser_ops._OP_LOCK.acquire(), timeout=LOCK_WAIT_S)
    except asyncio.TimeoutError:
        return _error("browser_busy", "another chat is using the browser", 409)
    try:
        # The takeover's fence (``LiveHub.op_fence``), as every agent op takes it: a
        # claim waits for this to leave, and one that landed first is seen here.
        async with hub.op_fence(key):
            if hub.in_control(key):
                result = {"ok": False, "code": "user_in_control", "error": "you are using this tab"}
            else:
                result = await asyncio.wait_for(
                    _run(hub, key, supervisor, sites, action, value), timeout=OP_TIMEOUT_S
                )
    except asyncio.TimeoutError:
        result = {"ok": False, "code": "timeout", "error": f"{action} took too long"}
    except Exception as exc:  # noqa: BLE001 - the daemon or the browser failed
        logger.warning("browser media %s failed: %s", action, type(exc).__name__)
        result = {"ok": False, "code": "browser_error", "error": "the browser did not answer"}
    finally:
        browser_ops._OP_LOCK.release()
    # A ``state`` read changes nothing and a player page polls it, so it writes no
    # row: one per poll would crowd real events out of the size-capped log.
    if action != "state":
        browser_live._audit(
            request,
            operation="browser_media",
            outcome="allowed" if result.get("ok") else "error",
            error="" if result.get("ok") else str(result.get("code") or ""),
        )
    if result.get("ok"):
        state = {k: v for k, v in result.items() if k not in ("ok", "center")}
        return web.json_response({"ok": True, "session_key": key, "media": state})
    return web.json_response(result)


async def _run(
    hub: Any,
    key: str,
    supervisor: Any,
    sites: tuple[str, ...],
    action: str,
    value: float | None,
) -> dict[str, Any]:
    """Point the daemon at *key*'s own tab, then run the media action there."""
    target = hub.target_of(key)
    if not target or target not in set(browser_ops._ledgered_targets(supervisor)):
        return {"ok": False, "code": "no_tab", "error": "this session has no open browser tab"}
    ctx = browser_ops.build_context(
        key, supervisor, hub, caller_key=DASHBOARD_SURFACE_KEY, sites=sites
    )
    tab = await ctx.helper("current_tab")
    current = (
        str(tab.get("targetId") or tab.get("target_id") or "") if isinstance(tab, dict) else ""
    )
    if current != target:
        await ctx.helper("switch_tab", target=target, activate=False)
    return await media.control(ctx, action, value)


__all__ = ["api_browser_media"]
