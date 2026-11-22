"""The owner's media buttons on an app page: play, pause, seek, volume on one tab.

An app whose own agent drives the browser on its approved sites
(``apps/browser_sites.py``) shows the person a player on its page. Those buttons
must answer at once, not after an agent turn, so they reach the tab through the
owner-only route ``POST /api/browser/media`` (``dashboard/handlers/browser_media.py``)
and this module: one walker method, ``media``, evaluated in the walker's isolated
world on the session's own tab, exactly like every other read the tool makes
(``ops._page``). It reads the element's playback state and nothing of the page.

A play may be refused by the browser's autoplay rule, since a button pressed in the
dashboard is no gesture in that tab (a new tab, or a tab that moved to another site).
When the element is still paused just after ``play()``, one click at its centre (the
``click`` helper, ``Input.dispatchMouseEvent``) gives the tab its gesture, and
``play()`` runs once more. A play request clicks at most once, and only then.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.browser_harness import ops

#: What the route accepts. ``state`` only reads.
ACTIONS = ("state", "play", "pause", "seek", "volume")
#: How long the first play waits before it asks whether the element really started.
PLAY_SETTLE_S = 0.3


async def _media(ctx: ops.OpContext, action: str, value: Any) -> dict[str, Any]:
    result = await ops._page(ctx, "media", action, value)
    if not isinstance(result, dict):
        return ops.refusal("browser_error", "the tab gave no media state")
    return result


async def control(ctx: ops.OpContext, action: str, value: Any = None) -> dict[str, Any]:
    """Run one media *action* on the tab *ctx* is bound to; its state, or a refusal.

    The page is judged before it is touched (``ops._page``), so a page off the
    session's allowed list is blanked and refused here too.
    """
    if action not in ACTIONS:
        return ops.refusal("bad_action", f"action must be one of: {', '.join(ACTIONS)}")
    try:
        result = await _media(ctx, action, value)
        if action != "play" or not result.get("ok") or not result.get("paused"):
            return result
        await ctx.sleep(PLAY_SETTLE_S)
        result = await _media(ctx, "state", None)
        center = result.get("center")
        if not result.get("paused") or not isinstance(center, list) or len(center) != 2:
            return result
        # The page may have moved since the state read: judge it again right before
        # the click, so no input reaches a page off the session's list.
        refused = await ops._refuse_current_url(ctx)
        if refused is not None:
            return refused
        await ctx.helper("click", x=float(center[0]), y=float(center[1]))
        await ctx.sleep(PLAY_SETTLE_S)
        result = await _media(ctx, "state", None)
        if result.get("ok") and result.get("paused"):
            # The click itself may toggle a site's own player; ask once more.
            result = await _media(ctx, "play", None)
        return result
    except ops._PageRefused as exc:
        return exc.result


__all__ = ["ACTIONS", "PLAY_SETTLE_S", "control"]
