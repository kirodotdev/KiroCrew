"""Tell an app's backend when the media in its own agent's browser tab ends.

An app page learns of an ending by polling ``/api/browser/media``; with the page
closed nobody reads the tab. So once an app's own agent has browsed (a successful op
through ``browser_ops.run_browser_op`` on a slot whose app holds approved
``browserSites``) and the app declares ``backend.hooks.on_browser_media_ended``, the
gateway watches that tab itself: every :data:`WATCH_S` seconds one ``state`` read,
the same read the page's poll makes (``browser_harness/media.py``), and on each new
ending one call of the hook with::

    {"event": "browser_media_ended", "session_key": "<the app's slot key>",
     "origin": "https://www.youtube.com", "duration": 213.4}

``duration`` is seconds, ``None`` when the element did not know it. Nothing of the
page beyond its origin is passed.

No model is asked and no agent is woken here: what the hook does next is the app's.
A tick never queues for the browser: while an agent op, a media request or the
person's take-over holds the tab, it is skipped. The watch ends on the first tick
that finds the slot gone or owned by nobody, the app without approved sites
(disabled, uninstalled, untrusted, sites withdrawn), the hook undeclared, or the
tab closed; the next op of that agent starts it again.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.apps import browser_sites
from kiro_crew.browser_harness import media
from kiro_crew.browser_harness.budgets import OP_TIMEOUT_S
from kiro_crew.browser_harness.governance import DASHBOARD_SURFACE_KEY
from kiro_crew.browser_harness.live_hub import get_live_hub
from kiro_crew.dashboard.handlers import browser_media, browser_ops

logger = logging.getLogger(__name__)

#: The manifest hook (``backend.hooks.<HOOK>``) and the event name it is handed.
HOOK = "on_browser_media_ended"
EVENT = "browser_media_ended"
#: Seconds between two reads of a watched tab. An ending stays readable until the
#: element plays again, so a slow tick only delays the event, it does not lose it.
WATCH_S = 3.0
#: How long a tick waits for the browser lock it found free before it skips.
_ACQUIRE_S = 0.05

#: The running watch of each slot key.
_WATCHES: dict[str, asyncio.Task[None]] = {}
#: Slot key -> the origin whose ending was already handed over, until the element is
#: seen not ended again, so a watch restarted on an ended page does not repeat it.
_ENDED: dict[str, str] = {}


def _hook_declared(app: str) -> bool:
    """Whether *app*'s live manifest declares :data:`HOOK`. Blocking."""
    from kiro_crew.apps.manager import get_app_manifest  # noqa: PLC0415

    try:
        manifest = get_app_manifest(app)
    except Exception:  # noqa: BLE001 - an unreadable manifest declares nothing
        return False
    hooks = getattr(getattr(manifest, "backend", None), "hooks", None)
    return bool(getattr(hooks, HOOK, ""))


def _origin(url: Any) -> str:
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


def ensure_watch(state: Any, key: str) -> None:
    """Start watching slot *key*'s tab unless a watch already runs. Never raises."""
    task = _WATCHES.get(key)
    if task is not None and not task.done():
        return
    try:
        _WATCHES[key] = asyncio.get_running_loop().create_task(_watch(state, key))
    except RuntimeError:
        return


async def _still_watched(state: Any, key: str) -> tuple[str, tuple[str, ...]] | None:
    """``(app, sites)`` while slot *key* should be watched, else ``None``."""
    app = browser_ops.slot_app(state, key)
    if not app:
        return None
    sites = await browser_ops.slot_sites(state, key)
    if not sites or sites == browser_sites.NO_PAGES:
        return None
    if not await asyncio.to_thread(_hook_declared, app):
        return None
    return app, sites


async def _read(hub: Any, key: str, supervisor: Any, sites: tuple[str, ...]) -> dict[str, Any]:
    """One ``state`` read of *key*'s own tab plus its ``origin``, as the media route does."""
    target = hub.target_of(key)
    if not target or target not in set(browser_ops._ledgered_targets(supervisor)):
        return {"ok": False, "code": "no_tab"}
    ctx = browser_ops.build_context(
        key, supervisor, hub, caller_key=DASHBOARD_SURFACE_KEY, sites=sites
    )
    tab = await ctx.helper("current_tab")
    current = (
        str(tab.get("targetId") or tab.get("target_id") or "") if isinstance(tab, dict) else ""
    )
    if current != target:
        await ctx.helper("switch_tab", target=target, activate=False)
        tab = await ctx.helper("current_tab")
    result = await media.control(ctx, "state")
    if result.get("ok"):
        result["origin"] = _origin(tab.get("url") if isinstance(tab, dict) else "")
    return result


async def _tick(hub: Any, key: str, sites: tuple[str, ...]) -> dict[str, Any] | None:
    """One read, or ``None`` when the browser is busy and this tick is skipped."""
    supervisor = browser_media._running_supervisor()
    if supervisor is None or not hub.target_of(key):
        return {"ok": False, "code": "no_tab"}
    if hub.in_control(key) or browser_ops._OP_LOCK.locked() or browser_media._MEDIA_GATE.locked():
        return None
    try:
        # Bounded: a lock just released may still hand itself to a woken waiter.
        await asyncio.wait_for(browser_ops._OP_LOCK.acquire(), _ACQUIRE_S)
    except asyncio.TimeoutError:
        return None
    try:
        async with hub.op_fence(key):
            if hub.in_control(key):
                return None
            return await asyncio.wait_for(_read(hub, key, supervisor, sites), OP_TIMEOUT_S)
    finally:
        browser_ops._OP_LOCK.release()


async def _watch(state: Any, key: str) -> None:
    from kiro_crew.apps.hooks_integration import dispatch_app_event  # noqa: PLC0415

    hub = get_live_hub()
    try:
        while True:
            watched = await _still_watched(state, key)
            if watched is None:
                return
            app, sites = watched
            try:
                result = await _tick(hub, key, sites)
            except Exception as exc:  # noqa: BLE001 - the browser did not answer; try later
                logger.debug("media watch read failed: %s", type(exc).__name__)
                result = None
            if result is not None and result.get("code") == "no_tab":
                _ENDED.pop(key, None)
                return
            if result is not None and result.get("ok"):
                origin = str(result.get("origin") or "")
                if not result.get("ended"):
                    _ENDED.pop(key, None)
                elif _ENDED.get(key) != origin:
                    _ENDED[key] = origin
                    duration = result.get("duration")
                    await dispatch_app_event(
                        app,
                        HOOK,
                        {
                            "event": EVENT,
                            "session_key": key,
                            "origin": origin,
                            "duration": (
                                float(duration)
                                if isinstance(duration, (int, float))
                                and not isinstance(duration, bool)
                                else None
                            ),
                        },
                    )
            await asyncio.sleep(WATCH_S)
    finally:
        if _WATCHES.get(key) is asyncio.current_task():
            _WATCHES.pop(key, None)


__all__ = ["EVENT", "HOOK", "WATCH_S", "ensure_watch"]
