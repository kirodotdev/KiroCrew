"""WebSocket endpoint — multiplexes all real-time events over a single connection."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from aiohttp import WSMsgType, web

from kiro_crew import __version__ as _local_version
from kiro_crew import shutdown_event
from kiro_crew.dashboard.chat_utils import subagent_event_slot
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_WS_STATUS_INTERVAL = 5  # seconds between dashboard status pushes
_WS_COUNTS_CACHE_TTL = 30  # seconds between refreshing lesson/cron counts
# Reconnect replay: more subagent frames than this collapse into ONE
# subagent_snapshot_batch frame (scale plumbing — a per-agent burst at
# 60-100 agents saturates the socket the moment a client reconnects).
SUBAGENT_REPLAY_BATCH_THRESHOLD = 8

SIDE_RESULT_EVENT = "chat.side_result"
SIDE_KIND = "side"


async def _load_status_counts(state: DashboardState) -> tuple[int, int]:
    """Return ``(cron_count, lesson_count)`` loaded OFF the event loop.

    ``LessonStore.load_all()`` performs blocking file I/O (JSONL ``stat()`` +
    ``read_text()``) and the cron count comes from a direct read-only parse of
    ``crons.json`` (``count_enabled_from_disk``). The WS status pusher runs on
    the event loop, so computing these inline would stall the loop — and with
    it EVERY other WebSocket / coroutine on the gateway — for the duration of
    that disk latency (seconds on a slow/large home dir or a contended NFS
    mount). Offload both to a worker thread so the loop stays responsive; the
    pusher is a periodic background task, so the extra thread hop is free.

    NOTE: this deliberately uses ``count_enabled_from_disk`` rather than
    ``list_jobs``. ``list_jobs`` calls ``_sync()`` → ``_load()`` → ``_arm_timer()``,
    and ``_arm_timer`` calls ``asyncio.create_task`` — with no running loop in a
    worker thread that raises ``RuntimeError``, and since ``_arm_timer`` cancels
    the existing timer first it would silently stop all scheduled cron jobs.
    ``count_enabled_from_disk`` is a pure read that never mutates loop-owned
    state or the timer, so it is safe off-thread.
    """
    crons = await asyncio.to_thread(state.crons.count_enabled_from_disk)
    lessons = await asyncio.to_thread(state.lessons.load_all)
    return crons, len(lessons)


def broadcast_side_result(
    state: DashboardState,
    *,
    slot_key: str,
    run_id: str,
    role: str,
    content: str,
    is_error: bool = False,
    final: bool = False,
    ts: float | None = None,
) -> None:
    """Broadcast a side conversation event on the dedicated side channel.

    Emits ``{type: "chat.side_result", data: payload}`` to all WS clients.
    The event name and payload shape are reused from the upstream OpenClaw
    `/btw` protocol so a future shared client can interop. ``kind`` is
    translated from upstream ``"btw"`` to KiroCrew's ``"side"``.

    The event channel is intentionally separate from ``chat_message`` so
    receivers that don't subscribe to side simply don't see it; this
    keeps side deltas out of the main transcript by construction.
    Receiver-side run-ID isolation is the frontend's responsibility via
    ``local_side_run_ids``.

    Set final=True on the terminal frame of a side turn so the frontend
    can flip the streaming flag off cleanly.

    No payload field is persisted — sidecar-only, ephemeral.
    """
    payload: dict[str, object] = {
        "kind": SIDE_KIND,
        "slot": slot_key,
        "run_id": run_id,
        "role": role,
        "content": redact_credentials(redact_exfiltration_urls(content)[0])[0],
        "ts": ts if ts is not None else time.time(),
    }
    if is_error:
        payload["is_error"] = True
    if final:
        payload["final"] = True
    state.broadcast_ws(SIDE_RESULT_EVENT, payload)


def _check_ws_origin(request: web.Request) -> None:
    """Reject cross-origin WebSocket upgrades.

    Browsers always send an Origin header on WebSocket handshakes.
    We allow only the dashboard's own origins and reject everything else,
    including missing Origin (non-browser clients are not expected).
    """
    if not check_origin(request, require=True):
        raise web.HTTPForbidden(text="WebSocket origin not allowed")


async def api_ws(request: web.Request) -> web.WebSocketResponse:
    """GET /api/ws — single multiplexed WebSocket for all real-time events."""
    _check_ws_origin(request)

    from kiro_crew.dashboard.handlers import _log_ring, _update_info

    state: DashboardState = request.app["state"]
    from kiro_crew.dashboard.handlers.source_providers import (
        CHECK_STATUS_PENDING_MAX,
        CHECK_STATUS_TTL_SECS,
        ensure_gitlab_hosts_loaded,
        gitlab_hosts_generation,
        is_owner_dashboard_request,
        schedule_check_refresh,
    )

    owner_request = is_owner_dashboard_request(request)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # Warm the self-managed GitLab allowlist BEFORE the first serialization.
    # Slot source-link extraction is synchronous and cannot load it, so without
    # this the initial sidebar would omit every self-hosted MR chip until some
    # later provider request happened to populate the snapshot.
    # Done BEFORE register_ws: this awaits, and a cancellation here would
    # otherwise leave the socket registered with no cleanup scope to unregister
    # it (the finally below is only entered after registration succeeds).
    try:
        await ensure_gitlab_hosts_loaded()
    except Exception:
        logger.debug("GitLab allowlist warm-up failed; chips may lag one round", exc_info=True)

    state.register_ws(ws, owner=owner_request)

    # Push current slots immediately so sidebar populates without waiting.
    try:
        slots_data = state.serialize_slots(include_check_status=owner_request)
        await ws.send_json(
            {
                "type": "slots",
                "data": slots_data,
                "yolo": state._yolo,
                # Seed the client's generation baseline so a later change is
                # detectable as a change rather than as a first sighting.
                "gitlabHostsGeneration": gitlab_hosts_generation(),
            }
        )
        if owner_request:
            # Issue links carry no check status — skip them so the scheduler
            # never hands an issue URL to the pull-request-only chip fetch.
            urls = [
                link["url"]
                for payload in slots_data
                for link in payload.get("source_links", [])
                if link.get("kind", "change") == "change"
            ]
            if urls:
                schedule_check_refresh(urls, state.push_slots_update)
    except Exception:
        pass

    # Background task: push dashboard status periodically
    async def _push_status() -> None:
        _cached_lessons = 0
        _cached_crons = 0
        _counts_ts = 0.0
        try:
            while not ws.closed and not shutdown_event.is_set():
                now = time.time()
                # Refresh lesson/cron counts every 30s (not every 5s).
                if now - _counts_ts > _WS_COUNTS_CACHE_TTL:
                    _cached_crons, _cached_lessons = await _load_status_counts(state)
                    _counts_ts = now
                data = {
                    **state.status_snapshot(
                        cron_jobs=_cached_crons,
                        lessons=_cached_lessons,
                        update_available=bool(_update_info.get("available")),
                        update_self_updatable=bool(_update_info.get("self_updatable")),
                        update_checked=bool(_update_info.get("checked")),
                        update_command=str(_update_info.get("update_command") or ""),
                    ),
                    "version": _local_version,
                    "platform": sys.platform,
                }
                try:
                    await ws.send_json({"type": "dashboard", "data": data})
                except Exception:
                    break
                await asyncio.sleep(_WS_STATUS_INTERVAL)
        except (asyncio.CancelledError, Exception):
            pass

    status_task = asyncio.create_task(_push_status())

    # Background task (owner connections only): keep sidebar PR/MR chip
    # status fresh. push_slots_update serves the *cached* check status but
    # never schedules refreshes — without a periodic driver the cache is only
    # populated at connect / slots-GET time, so chips freeze at their initial
    # state (e.g. a PR merged after page load never gains the merge icon).
    # schedule_check_refresh is TTL-gated and inflight-deduped, so multiple
    # owner connections still cost at most one provider fetch per URL per
    # TTL, and on_update broadcasts only when a status actually changed.
    async def _refresh_check_loop() -> None:
        # Rotate the starting offset each round. schedule_check_refresh admits
        # at most CHECK_STATUS_PENDING_MAX URLs per call and backs the rest off
        # for one TTL; because every chip expires in lockstep, feeding URLs in
        # the same slot order every round would let the first-N win forever and
        # starve later chips (deterministic with >N PR-linked slots). Advancing
        # the offset by the admission cap each round cycles every chip through
        # the admitted window within ceil(len/cap) rounds.
        refresh_round = 0
        hosts_generation = gitlab_hosts_generation()
        while not ws.closed and not shutdown_event.is_set():
            # Guard the body (not the whole loop) so a single transient failure
            # from source_link_urls()/schedule_check_refresh logs and continues
            # instead of silently killing the driver and reverting to the
            # frozen-chip bug this loop exists to fix.
            try:
                await asyncio.sleep(CHECK_STATUS_TTL_SECS)
                # Re-read the allowlist off-loop on the same cadence. A host the
                # operator added (or revoked) changes which links are chips at
                # all, and slot extraction is synchronous, so a generation change
                # has to be pushed explicitly -- otherwise the new/removed chip
                # waits for an unrelated message mutation.
                await ensure_gitlab_hosts_loaded()
                if gitlab_hosts_generation() != hosts_generation:
                    hosts_generation = gitlab_hosts_generation()
                    state.push_slots_update()
                urls = state.source_link_urls()
                if urls:
                    offset = (refresh_round * CHECK_STATUS_PENDING_MAX) % len(urls)
                    urls = urls[offset:] + urls[:offset]
                    schedule_check_refresh(urls, state.push_slots_update)
                refresh_round += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "check-status refresh round failed; continuing", exc_info=True
                )

    check_task = asyncio.create_task(_refresh_check_loop()) if owner_request else None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type", "")
                    if msg_type == "subscribe_logs":
                        state.subscribe_logs(ws)
                        # Replay log ring buffer
                        for entry in list(_log_ring):
                            try:
                                parsed = json.loads(entry)
                                await ws.send_json({"type": "log", "data": parsed})
                            except Exception:
                                pass
                    elif msg_type == "unsubscribe_logs":
                        state.unsubscribe_logs(ws)
                    elif msg_type == "subscribe_subagents":
                        state.subscribe_subagents(ws)

                        def _r(t: str) -> str:
                            t, _ = redact_exfiltration_urls(t)
                            t, _ = redact_credentials(t)
                            return t

                        # Collect every replay frame first; below the scale
                        # threshold they are sent individually, above it they
                        # collapse into ONE subagent_snapshot_batch frame — at 60-100 agents
                        # a per-agent replay burst saturates the socket the
                        # moment a client reconnects.
                        _replay: list[dict] = []

                        # Native kiro-cli subagents run inside dashboard chat
                        # slots, not the global SubagentManager. Replay their
                        # slot-owned in-flight state before manager snapshots.
                        # Running cards replay as snapshots; cards that finished
                        # while the socket was down replay as done events so the
                        # terminal card + output survive the reconnect clear.
                        for native in state.native_subagent_snapshots():
                            try:
                                if native.get("done"):
                                    _err = native.get("error")
                                    _replay.append(
                                        {
                                            "type": "subagent_done",
                                            "data": {
                                                "id": native["id"],
                                                "slot": native["slot"],
                                                "elapsed": native["elapsed"],
                                                "error": _r(str(_err)) if _err else None,
                                                "stopped": bool(native.get("stopped")),
                                                "outcome": str(native.get("outcome") or ("stopped" if native.get("stopped") else ("failed" if native.get("error") else "completed"))),
                                                "task": _r(str(native["task"])),
                                                "agent": _r(str(native["agent"])),
                                                "result": _r(str(native["result"])),
                                            },
                                        }
                                    )
                                else:
                                    _replay.append(
                                        {
                                            "type": "subagent_snapshot",
                                            "data": {
                                                "id": native["id"],
                                                "slot": native["slot"],
                                                "task": _r(str(native["task"])),
                                                "agent": _r(str(native["agent"])),
                                                "streaming": _r(str(native["streaming"])),
                                                "last_tool": _r(str(native["last_tool"])),
                                                "started": native["started"],
                                            },
                                        }
                                    )
                            except Exception:
                                pass

                        # Snapshot of managed subagents + done events for completed ones
                        if state.subagents:
                            for a in state.subagents.running:
                                try:
                                    slot = subagent_event_slot(a.parent_session_key)
                                    _replay.append(
                                        {
                                            "type": "subagent_snapshot",
                                            "data": {
                                                "id": a.id,
                                                "slot": slot,
                                                "task": _r(a.task),
                                                "agent": _r(a.agent),
                                                "streaming": _r(a.streaming_text),
                                                "last_tool": _r(a.last_tool),
                                                "tool_count": a.tool_count,
                                                "stalled": a.stalled,
                                                "started": a.started,
                                            },
                                        }
                                    )
                                except Exception:
                                    pass
                            # Done events for completed subagents so
                            # reconnecting clients can transition stale cards.
                            for a in state.subagents.all_agents:
                                if not a.done:
                                    continue
                                # Same slot mapping as the live frames — a raw
                                # prefix-strip tags replayed cards with a slot
                                # no tab reads, so the panel rehydrated empty
                                # after every reconnect for cron/channel tabs.
                                slot = subagent_event_slot(a.parent_session_key)
                                try:
                                    _replay.append(
                                        {
                                            "type": "subagent_done",
                                            "data": {
                                                "id": a.id,
                                                "slot": slot,
                                                "elapsed": a.elapsed,
                                                "error": _r(a.error) if a.error else None,
                                                "stopped": a.user_stopped,
                                                "outcome": a.outcome,
                                                "task": _r(a.task),
                                                "agent": _r(a.agent),
                                            },
                                        }
                                    )
                                except Exception:
                                    pass
                        try:
                            if len(_replay) > SUBAGENT_REPLAY_BATCH_THRESHOLD:
                                await ws.send_json(
                                    {"type": "subagent_snapshot_batch", "data": {"items": _replay}}
                                )
                            else:
                                for _frame in _replay:
                                    await ws.send_json(_frame)
                        except Exception:
                            pass
                    elif msg_type == "unsubscribe_subagents":
                        state.unsubscribe_subagents(ws)
                except (json.JSONDecodeError, Exception):
                    pass
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        status_task.cancel()
        if check_task is not None:
            check_task.cancel()
        state.unsubscribe_logs(ws)
        state.unsubscribe_subagents(ws)
        state.unregister_ws(ws)
    return ws
