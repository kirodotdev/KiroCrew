"""What every Slack interaction handler shares: the orchestrator slot, the view registry,
the acknowledge buttons and the tracked-channel scope probe.

``_orch`` is the gateway orchestrator. ``kiro_crew.slack.interactions.init`` writes it
through the facade, so every owner module that reads it holds the same object.
``VIEW_REGISTRY`` maps a modal's ``callback_id`` to its submission handler
(``register_view_handler``). ``ack_button`` replaces an ack or approve button's message
with '✅ Acknowledged', for the cron and subagent acknowledge buttons here and the Stop
confirmation. ``_probe_tracked_channel_scope`` fires the deferred history-readability
probe for newly tracked channels. This is the lowest layer: the other owners import from
it, and it imports none of them.

Split out of ``kiro_crew.slack.interactions``, which stays the import path and the
patch target: every name here is reached as ``kiro_crew.slack.interactions.<name>``,
and a patch through that facade reaches this module (``kiro_crew._facade_mirror``).
Nothing here imports the facade. New shared-handler work goes here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine

import aiohttp

from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.http_proxy import resolve_proxy
from kiro_crew.slack.events_queue import _drain_slack_queue, _spawn_tracked, _track_router_task
from kiro_crew.slack.scope_probe import log_probe_failure, warn_unreadable_tracked_channels

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger("kiro_crew.slack.interactions")


# Module-level orchestrator reference — set by ``init()``.
_orch: GatewayOrchestrator | None = None


def _start_interaction_turn(
    orch: GatewayOrchestrator,
    coro: Coroutine[Any, Any, None],
    session_key: str,
    thread_ts: str | None,
) -> asyncio.Task[None]:
    """Start the turn an interaction opened, as *session_key*'s router task once the key is free.

    A message typed while this turn holds the session is queued with ⏳, because the
    router reads the key as busy (``_key_busy``) or the session's semaphore refuses it.
    Only a router task's end drains that queue, so a turn the router never registered
    leaves the queued message waiting until some later typed turn ends. Registered like a
    routed turn, this one's end runs ``_drain_slack_queue``, which yields while a ``!stop``
    on the key is in flight and leaves the drain to that stop's settle.

    The registration waits for the key: ``_session_tasks[key]`` is the turn every Stop
    cancels, so a click admitted while another turn is tracked must not take its place
    (the Stop would cancel the click's turn, possibly a co-tenant's, and leave the running
    one). The click's task is registered when the current holder ends and the key is free,
    or re-armed on whatever took the key meanwhile. Only ``_session_tasks`` is read here,
    not the stop holds: a click admitted while a stop is in flight is newer intent, left
    running.

    *session_key* is the router's key for the thread -- the bare thread ts, or the ts of
    the message a thread is started from -- the same key a typed message is queued under.
    """
    task = asyncio.create_task(coro)
    orch._handler_tasks.add(task)

    def _after(done: asyncio.Task[None]) -> None:
        orch._handler_tasks.discard(done)
        if orch._session_tasks.get(session_key) is done:
            del orch._session_tasks[session_key]
        _spawn_tracked(_drain_slack_queue(orch, session_key))

    task.add_done_callback(_after)
    _register_when_free(orch, session_key, task, thread_ts)
    return task


def _register_when_free(
    orch: GatewayOrchestrator, session_key: str, task: asyncio.Task[None], thread_ts: str | None
) -> None:
    """Make *task* the key's router task now, or once the task holding the key ends."""
    holder = orch._session_tasks.get(session_key)
    if holder is None or holder is task or holder.done():
        if not task.done():
            _track_router_task(orch, session_key, task, thread_ts)
        return
    holder.add_done_callback(lambda _done: _register_when_free(orch, session_key, task, thread_ts))


def _probe_tracked_channel_scope(channel_ids: set[str]) -> None:
    """Fire a deferred history-readability probe for newly tracked channels.

    A private channel tracked under a Slack install that predates the
    ``groups:history`` scope delivers no message events and nothing logs —
    the probe (see :mod:`kiro_crew.slack.scope_probe`) turns that silent-dead
    state into a warning + dashboard notification. Fire-and-forget so the
    interaction ack is never delayed by a Slack API round-trip.
    """
    if not channel_ids or not _orch or _orch.slack is None:
        return
    t = asyncio.create_task(
        warn_unreadable_tracked_channels(
            _orch.slack,
            channel_ids,
            notify=_orch.dashboard_state.notify if _orch.dashboard_state else None,
        )
    )
    _orch._handler_tasks.add(t)
    t.add_done_callback(_orch._handler_tasks.discard)
    t.add_done_callback(log_probe_failure)


# ---------------------------------------------------------------------------
# View submission registry
# ---------------------------------------------------------------------------

# Handler signature: async def handler(payload: dict) -> None
ViewHandler = Callable[[dict], Awaitable[None]]

VIEW_REGISTRY: dict[str, ViewHandler] = {}


def register_view_handler(callback_id: str, handler: ViewHandler) -> None:  # type: ignore[type-arg]
    """Register a handler for a ``view_submission`` or ``view_closed`` callback_id."""
    VIEW_REGISTRY[callback_id] = handler


# ---------------------------------------------------------------------------
# Shared helper — replace a button message with "✅ Acknowledged"
# ---------------------------------------------------------------------------


async def ack_button(payload: dict, channel: str, msg_ts: str) -> None:
    """Replace an ack/approve button message with '✅ Acknowledged'.

    Tries ``response_url`` first (instant, no API call), then falls
    back to ``chat.update``.
    """
    response_url = payload.get("response_url", "")
    blocks = payload.get("message", {}).get("blocks", [])

    # Strip action blocks, keep content — append ack context
    acked_blocks = []
    for b in blocks:
        if b.get("type") == "actions":
            continue
        if b.get("type") == "section" and b.get("text", {}).get("text", ""):
            b = {**b, "text": {**b["text"], "text": b["text"]["text"][:2990]}}
        acked_blocks.append(b)
    acked_blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "✅ Acknowledged"}]}
    )

    updated = False
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await sess.post(
                    response_url,
                    proxy=resolve_proxy(response_url),
                    json={
                        "replace_original": True,
                        "text": "✅ Acknowledged",
                        "blocks": acked_blocks,
                    },
                )
                updated = resp.status == 200
        except Exception:
            logger.debug("response_url update failed", exc_info=True)

    if not updated and _orch and _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(
                channel, msg_ts, text="✅ Acknowledged", blocks=acked_blocks
            )
        except Exception:
            logger.debug("chat.update fallback failed", exc_info=True)


async def _handle_cron_ack(payload: dict, action: dict, channel: str, msg_ts: str) -> None:
    job_id = action.get("value", "")
    if not (job_id and _orch and _orch.cron_svc):
        return
    await ack_button(payload, channel, msg_ts)
    msg_text = payload.get("message", {}).get("text", "")[:200]
    try:
        await _orch.cron_svc.ack_job_async(job_id, msg_text)
    except (CronStoreBusy, CronStoreUnreadable) as exc:
        # Ack is best-effort context bookkeeping; neither a transiently-contended
        # store nor an unreadable one must fail the Slack interaction. The button
        # already acked visually. Unreadable degrades here rather than surfacing
        # to the user because nothing was requested of the store by the person
        # clicking -- this is the same class as the background writers in
        # CronService, not a user-initiated mutation.
        logger.warning("cron ack skipped: %s (job %s)", type(exc).__name__, job_id)
    if _orch.dashboard_state:
        for n in _orch.dashboard_state._notification_log:
            if n.get("job_id") == job_id and not n.get("acked"):
                await _orch.dashboard_state.ack_notification(n["ts"])
                _orch.dashboard_state.broadcast_ws("notification_ack", {"ts": n["ts"]})


async def _handle_subagent_ack(payload: dict, action: dict, channel: str, msg_ts: str) -> None:
    subagent_id = action.get("value", "")
    await ack_button(payload, channel, msg_ts)
    if not (subagent_id and _orch and _orch.dashboard_state):
        return
    for n in _orch.dashboard_state._notification_log:
        if n.get("kind") == "subagent" and subagent_id in n.get("title", "") and not n.get("acked"):
            await _orch.dashboard_state.ack_notification(n["ts"])
            _orch.dashboard_state.broadcast_ws("notification_ack", {"ts": n["ts"]})
