"""Run an agent op with the edition's per-origin sign-in in view.

The interception itself lives in :mod:`kiro_crew.browser_harness.sign_in_guard`,
on the relay's own session of each agent tab, and is on only while the supervisor
says so (:meth:`HarnessSupervisor.active_sign_in_guard`). This wrapper is what an op
adds to it:

- A ``navigate`` names its host to the guard (:meth:`SignInGuard.begin`), and a
  ``tabs new`` the host of the tab it opens (:meth:`SignInGuard.expect_new`), so a
  redirect to a sign-in host for THAT host may be signed. Once a sign for it lands,
  the same URL is opened again here through ``ops.navigate_to``, so the agent gets a
  judged landing (the URL gate, the commit wait) rather than the aborted first one.
- Every op, once it ends, waits for a sign in flight on the caller's tab, deletes
  any cookie a sign-in host managed to set (:meth:`SignInGuard.sweep`), and puts the
  last outcome on the result as ``sign_in: {outcome, reason, provider, host, note}``.
  A renewal the page did by itself between ops shows on the tab's next op.

The URL never leaves: the agent sees the host, a fixed reason code and a fixed note
(or the provider's own short message).
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.browser_harness import sign_in_guard
from kiro_crew.browser_harness.budgets import SIGN_IN_NAV_S
from kiro_crew.browser_harness.op_classes import READ_ONLY_OPS

__all__ = ["run_op"]


async def run_op(
    op: str,
    args: dict[str, Any],
    ctx: harness_ops.OpContext,
    supervisor: Any,
    caller_key: str = "",
) -> dict[str, Any]:
    """``ops.run_op``, with the sign-in guard told about a navigate and read after.

    *caller_key* is the caller's runtime session key, which governance binds on: a
    renewal on the tab later is judged against it."""
    getter = getattr(supervisor, "active_sign_in_guard", None)
    guard = getter() if callable(getter) else None
    if guard is None:
        return await harness_ops.run_op(op, args, ctx)
    target = await _tab(ctx)
    if target:
        guard.note_caller(target, caller_key, ctx.url_allowed, moves=op not in READ_ONLY_OPS)
    raw = args.get("url")
    # Stripped as ``ops._navigate`` strips it, so the host the guard keeps is the one opened.
    raw_url = raw.strip() if isinstance(raw, str) else raw
    url = raw_url if op == "navigate" and isinstance(raw_url, str) else ""
    if target and url:
        guard.begin(target, url)
    if op == "tabs" and args.get("action") == "new" and isinstance(raw_url, str):
        guard.expect_new(raw_url, caller_key, ctx.url_allowed)
        if target:
            # Harness reuses a blank current tab instead of opening one.
            guard.allow(target, raw_url)
    # A renewal the page did since the last op is reported, but only a sign during
    # this op decides whether its navigate opens the page again.
    earlier = guard.take(target) if target else []
    outcomes: list[dict[str, Any]] = []
    new_tabs: list[str] = []
    try:
        result = await harness_ops.run_op(op, args, ctx)
        new_tabs = guard.take_new()
        for tab in ([target] if target else []) + new_tabs:
            await guard.settle(tab)
            outcomes += guard.take(tab)
        # Only a sign whose bounce this navigate caused re-runs it; a renewal already in
        # flight before it is reported, not replayed.
        signed = any(
            o.get("outcome") == sign_in_guard.OUTCOME_SIGNED_IN and o.get("during_navigate")
            for o in outcomes
        )
        if target and url and signed and isinstance(result, dict):
            host = sign_in_guard.host_of(url)
            try:
                result = await asyncio.wait_for(
                    harness_ops.navigate_to(ctx, url, label=f"Open {host} signed in"),
                    SIGN_IN_NAV_S,
                )
            except asyncio.TimeoutError:
                result = harness_ops.refusal("nav_timeout", f"{host} did not open again in time")
            await guard.settle(target)
            outcomes += guard.take(target)
    finally:
        # Also when the op was cut (a takeover): a tab it opened ends with it.
        new_tabs += guard.take_new()
        for tab in ([target] if target else []) + new_tabs:
            guard.end(tab)
    await guard.sweep()
    block = sign_in_guard.told(earlier + outcomes)
    if block is None or not isinstance(result, dict):
        return result
    return {**result, "sign_in": block}


async def _tab(ctx: harness_ops.OpContext) -> str:
    """The caller's tab target id, or ``""``."""
    try:
        tab = await ctx.helper("current_tab")
    except Exception:  # noqa: BLE001 - no tab means nothing to watch
        return ""
    target = (tab.get("targetId") or tab.get("target_id")) if isinstance(tab, dict) else None
    return target if isinstance(target, str) else ""
