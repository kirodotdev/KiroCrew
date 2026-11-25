"""``tabs {action: list|switch|new|close}`` over the tabs the agent opened.

Only a tab in ``ctx.agent_targets()`` (this chat's own tabs and the popups they
opened) is listed, switched to or closed; a tab the person opened, or another
chat's, is never named. Closing the tab the browser is on moves it to another of
the chat's tabs, or to a new blank one when none is left. ``ops._tabs`` reaches
this module by a call-time import.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.browser_harness import ops as _ops


async def run(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action", "list")
    owned = set(ctx.agent_targets())
    if action == "list":
        tabs = await ctx.helper("list_tabs")
        mine = [
            {
                "target_id": t.get("target_id") or t.get("targetId"),
                "url": t.get("url"),
                "title": t.get("title"),
            }
            for t in (tabs if isinstance(tabs, list) else [])
            if isinstance(t, dict)
            and (t.get("target_id") or t.get("targetId")) in owned
            and (t.get("url") == "about:blank" or ctx.url_allowed(str(t.get("url") or "")))
        ]
        return {"ok": True, "tabs": mine}
    if action == "switch":
        target = args.get("target_id")
        if not isinstance(target, str) or target not in owned:
            return _ops.refusal("not_agent_tab", "only a tab the agent opened can be switched to")
        await _ops._step(ctx, "Switch tab")
        await ctx.helper("switch_tab", target=target)
        return {"ok": True, "target_id": target}
    if action == "new":
        url = args.get("url", "about:blank")
        if not isinstance(url, str) or not url or len(url) > _ops._URL_MAX:
            return _ops.refusal("bad_url", "tabs new needs a `url` string")
        if url != "about:blank" and not url.lower().startswith(("http://", "https://")):
            return _ops.refusal("bad_url", f"refused non-web URL: {url[:80]}")
        await _ops._step(ctx, f"Open a new tab {url}")
        target = await ctx.helper("new_tab", url=url)
        return {"ok": True, "target_id": target}
    if action == "close":
        target = args.get("target_id")
        if not isinstance(target, str) or target not in owned:
            return _ops.refusal("not_agent_tab", "only a tab the agent opened can be closed")
        await _ops._step(ctx, "Close a tab")
        tab = await ctx.helper("current_tab")
        current = (tab.get("targetId") or tab.get("target_id")) if isinstance(tab, dict) else None
        await ctx.helper("close_tab", target=target)
        if target != current:
            return {"ok": True, "closed": target}
        # The daemon was on the closed tab: move it to another of the agent's tabs,
        # or to a new blank one, so the checks after the op have a page to read.
        left = [t for t in ctx.agent_targets() if t != target]
        if left:
            await ctx.helper("switch_tab", target=left[-1])
            return {"ok": True, "closed": target, "current": left[-1]}
        return {
            "ok": True,
            "closed": target,
            "current": await ctx.helper("new_tab", url="about:blank"),
        }
    return _ops.refusal("bad_action", "tabs action must be one of: list, switch, new, close")
