"""Push the expiry of an open MCP OAuth banner to open tabs when its child ends.

An Authorize link is redeemable only while the ACP child that minted it is
alive: that process holds the loopback callback listener and the PKCE verifier.
The read-time gate (``chat_utils_display._expire_dead_child_oauth_meta``) already
withdraws a dead child's link on every fetch, and the teardowns the dashboard
runs itself (reset, agent switch, discard, watchdog recycle) push the same
verdict with ``_broadcast_expired_oauth_banners``. A child that ends any other
way (killed, crashed, exited on its own) runs none of that code, so a tab that
is already open kept showing a live Authorize link until it refetched.

This module closes that gap from the server. When an authorize banner is
appended, :func:`watch_open_oauth_banners` starts one watch per slot. Every
:data:`POLL_INTERVAL_SECS` it resolves the slot's live child and applies the
read gate's own predicate to each open banner. When a banner is judged dead it
broadcasts the withdrawal frame once; when no open banner is left alive it
stops. No row is rewritten: the read gate stays the one liveness rule, and this
is only how fast an open tab hears about it.

The watch is bounded: one task per slot, it ends as soon as every open banner
is terminal or expired, and it gives up after :data:`MAX_WATCH_SECS` (the read
gate still covers a tab that refetches after that). It never raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: Seconds between liveness checks. ``is_process_alive`` reads a returncode the
#: loop's child watcher already set, so a check costs a pool lookup and a scan of
#: the slot's rows; a few seconds keeps a dead link on screen for at most that long.
POLL_INTERVAL_SECS = 2.0

#: Upper bound on one watch's life. A banner left open longer than this is still
#: withdrawn by the read gate on the tab's next fetch.
MAX_WATCH_SECS = 24 * 60 * 60

#: The running watch per slot key, with the slot object it watches.
_watches: dict[str, tuple["_ChatSlot", asyncio.Task[None]]] = {}


def check_once(state: "DashboardState", slot: "_ChatSlot", announced: set[str]) -> bool:
    """Run one liveness check; return whether any open banner is still live.

    Broadcasts the withdrawal frame for each banner now judged dead that is not
    in *announced*, and records it there, so each row is announced once.
    """
    from kiro_crew.dashboard.chat_utils import (
        _broadcast_expired_oauth_banners,
        _expire_dead_child_oauth_meta,
        _live_child_instance,
    )
    from kiro_crew.dashboard.chat_utils_display import (
        _oauth_banner_row_key,
        _open_oauth_banner_rows,
    )

    if getattr(slot, "window_pending", False) is True:
        return False
    banners = _open_oauth_banner_rows(slot)
    if not banners:
        return False
    live_child = _live_child_instance(state, slot)
    newly_dead: set[str] = set()
    any_live = False
    for message in banners:
        meta = message.get("meta") or {}
        if _expire_dead_child_oauth_meta("mcp_oauth", meta, live_child) is meta:
            any_live = True
            continue
        key = _oauth_banner_row_key(message)
        if key not in announced:
            newly_dead.add(key)
    if newly_dead:
        _broadcast_expired_oauth_banners(state, slot, only=newly_dead)
        announced.update(newly_dead)
    return any_live


async def _watch(state: "DashboardState", slot: "_ChatSlot") -> None:
    announced: set[str] = set()
    deadline = time.monotonic() + MAX_WATCH_SECS
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECS)
            try:
                if not check_once(state, slot, announced):
                    return
            except Exception:
                logger.debug("MCP OAuth expiry check failed for %s", slot.key, exc_info=True)
                return
    finally:
        entry = _watches.get(slot.key)
        if entry is not None and entry[1] is asyncio.current_task():
            _watches.pop(slot.key, None)


def watch_open_oauth_banners(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Start the slot's expiry watch unless one is already running.

    Called right after an authorize banner is appended. Without a running event
    loop (a synchronous caller) it does nothing; the read gate still applies.
    """
    existing = _watches.get(slot.key)
    if existing is not None and not existing[1].done() and existing[0] is slot:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # A slot object rebuilt under the same key replaces the old one, so the
    # watch on the old object is stopped and the new object gets its own.
    if existing is not None and not existing[1].done():
        existing[1].cancel()
    _watches[slot.key] = (slot, loop.create_task(_watch(state, slot)))
