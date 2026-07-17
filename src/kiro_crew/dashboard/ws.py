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
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_WS_STATUS_INTERVAL = 5  # seconds between dashboard status pushes
_WS_COUNTS_CACHE_TTL = 30  # seconds between refreshing lesson/cron counts

SIDE_RESULT_EVENT = "chat.side_result"
SIDE_KIND = "side"


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
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    state.register_ws(ws)

    # Push current slots immediately so sidebar populates without waiting
    try:
        slots_data = state.serialize_slots()
        await ws.send_json({"type": "slots", "data": slots_data, "yolo": state._yolo})
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
                # Refresh lesson/cron counts every 30s (not every 5s)
                if now - _counts_ts > _WS_COUNTS_CACHE_TTL:
                    _cached_crons = len(state.crons.list_jobs())
                    _cached_lessons = len(state.lessons.load_all())
                    _counts_ts = now
                data = {
                    **state.status_snapshot(cron_jobs=_cached_crons, lessons=_cached_lessons, update_available=bool(_update_info.get("available"))),
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
                        # Send snapshot of active subagents + done events for completed ones
                        if state.subagents:
                            def _r(t: str) -> str:
                                t, _ = redact_exfiltration_urls(t)
                                t, _ = redact_credentials(t)
                                return t
                            for a in state.subagents.running:
                                try:
                                    slot = a.parent_session_key.removeprefix("dashboard:")
                                    await ws.send_json(
                                        {
                                            "type": "subagent_snapshot",
                                            "data": {
                                                "id": a.id,
                                                "slot": slot,
                                                "task": _r(a.task),
                                                "agent": _r(a.agent),
                                                "streaming": _r(a.streaming_text),
                                                "last_tool": _r(a.last_tool),
                                                "started": a.started,
                                            },
                                        }
                                    )
                                except Exception:
                                    pass
                            # Send done events for completed subagents so
                            # reconnecting clients can transition stale cards.
                            for a in state.subagents.all_agents:
                                if not a.done:
                                    continue
                                slot = a.parent_session_key.removeprefix("dashboard:")
                                try:
                                    await ws.send_json(
                                        {
                                            "type": "subagent_done",
                                            "data": {
                                                "id": a.id,
                                                "slot": slot,
                                                "elapsed": a.elapsed,
                                                "error": _r(a.error) if a.error else None,
                                                "task": _r(a.task),
                                                "agent": _r(a.agent),
                                            },
                                        }
                                    )
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
        state.unsubscribe_logs(ws)
        state.unsubscribe_subagents(ws)
        state.unregister_ws(ws)
    return ws
