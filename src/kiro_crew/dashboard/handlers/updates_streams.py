"""The live streams the dashboard holds open, and the gateway's log level.

``GET /api/logs`` (the in-memory ring buffer and its handler), ``GET`` and
``POST /api/logs/level``, and the ``GET /api/stream`` status SSE.

Served through :mod:`kiro_crew.dashboard.handlers.updates`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew import shutdown_event
from kiro_crew.config.live import ConfigChange
from kiro_crew.config.loader import coerce_dict_section, update_config_locked
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.handlers.updates_verdict import _display_local_version
from kiro_crew.dashboard.state import DashboardState, chat_message_frame
from kiro_crew.dashboard.status_counts import cached_status_snapshot

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")


_SSE_INTERVAL_SECS = 5


# ── Logs SSE ──


_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def apply_log_level(level_name: str, *, source: str) -> bool:
    """Set the ``kiro_crew`` logger to *level_name*; False when the name is unknown.

    The one place the runtime level changes, shared by the dashboard endpoint
    and the ``agent.log_level`` config applier so a ``kirocrew config set`` or
    an ``$EDITOR`` edit takes effect exactly like the Logs page toggle. The
    logger is the single level gate for every sink, ``gateway.log`` included:
    the file handler and the queue handler ``cli._setup_cli_logging`` installs
    carry no level of their own, so this one change reaches the file too.
    """
    name = str(level_name or "").upper()
    if name not in _LOG_LEVELS:
        return False
    root = logging.getLogger("kiro_crew")
    if root.level == _LOG_LEVELS[name]:
        return True
    root.setLevel(_LOG_LEVELS[name])
    logger.info("Log level changed to %s via %s", name, source)
    return True


def apply_log_level_from_config(change: ConfigChange) -> None:
    """Config applier for ``agent.log_level``: push the written level to the logger.

    Registered by ``server.py`` at boot next to the other appliers that outlive
    a request, so a write from any writer reaches the logger.
    """
    if not change.touched("agent.log_level"):
        return
    level = change.new.agent.log_level
    if not apply_log_level(level, source="config"):
        logger.warning("agent.log_level %r is not a level name; runtime level unchanged", level)


async def api_log_level(request: web.Request) -> web.Response:
    """POST /api/logs/level — change the kiro_crew logger level at runtime.

    Also persists the new level to config so it survives restarts.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    level_name = body.get("level", "").upper()
    if not apply_log_level(level_name, source="dashboard"):
        return web.json_response({"error": f"invalid level: {level_name}"}, status=400)

    # Persist to config so the level survives restarts: a DELTA read-modify-
    # write of the one key this endpoint owns, inside a single sidecar-flock
    # hold (update_config_locked), dispatched off the loop with both config
    # locks via run_config_write -- the transaction shape run_config_write's
    # docstring prescribes. A whole-document save() here would publish
    # a snapshot that can revert a concurrent writer's unrelated settings.
    def _set_level(doc: dict) -> dict:
        coerce_dict_section(doc, "agent")["log_level"] = level_name
        return doc

    persisted = False
    try:
        await run_config_write(update_config_locked, mutate=_set_level)
        persisted = True
    except Exception:
        logger.warning("Failed to persist log level to config", exc_info=True)

    return web.json_response({"ok": True, "level": level_name, "persisted": persisted})


async def api_log_level_get(request: web.Request) -> web.Response:
    """GET /api/logs/level — current kiro_crew logger level."""
    root = logging.getLogger("kiro_crew")
    return web.json_response({"level": logging.getLevelName(root.level)})


class _QueueLogHandler(logging.Handler):
    """Logging handler that enqueues formatted log entries for SSE delivery."""

    def __init__(self, queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
        super().__init__()
        self._queue: asyncio.Queue[str] = queue  # type: ignore[type-arg]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            data = json.dumps({"level": record.levelname, "msg": msg})
            self._queue.put_nowait(data)
        except Exception:
            pass


# ── Persistent log ring buffer ──

_LOG_RING_SIZE = 1000
_log_ring: collections.deque[str] = collections.deque(maxlen=_LOG_RING_SIZE)
_log_ring_handler_installed = False
_log_ring_handler: _RingLogHandler | None = None


async def _safe_ws_send(ws: web.WebSocketResponse, msg: str, state: DashboardState) -> None:
    """Send a log frame to one subscriber, re-checking its scope first.

    Subscription is granted once, in the ``subscribe_logs`` handler, and the
    handler above then fans out straight to ``_ws_log_subscribers`` without
    passing the broadcast chokepoint. Re-check here, through
    ``DashboardState._ws_client_allowed`` itself (rather than a hand-rolled
    scope comparison) so revoking an app's ``log`` scope (a narrowed manifest,
    or ``app disable``) stops the stream on a socket that is already
    subscribed, and so this decision gets the same SEL audit trail as every
    other event instead of a silent, unaudited duplicate of the check.

    This runs on the event loop (the caller hands it to ``create_task`` via
    ``call_soon_threadsafe``), which is what makes the check safe: the scope
    cache must never be consulted from the logging thread, where a cold miss
    would fall back to a synchronous manifest read.
    """
    try:
        # Every socket kind goes through the predicate: for a dashboard user it
        # answers True at once and records the grant under the reserved
        # dashboard-user auditee, so the live stream leaves the same record as
        # the ring replay that ``subscribe_logs`` admitted it to. Skipping the
        # call for that socket kind was the one place the log stream's grant to
        # the owner went unrecorded.
        if not state._ws_client_allowed(ws, "log", {}):
            state._ws_log_subscribers.discard(ws)
            return
        # The hub's stalled-tab limit applies here too, because this stream
        # bypasses its fan-out: a tab over the limit is dropped and aborted.
        if state._drop_if_stalled(ws) is True:
            return
        await ws.send_str(msg)
    except Exception:
        state._ws_log_subscribers.discard(ws)


class _RingLogHandler(logging.Handler):
    """Always-on handler that keeps the last N log entries in a ring buffer.

    Also pushes log events to WebSocket log subscribers.
    """

    def __init__(
        self,
        ring: collections.deque[str],
        max_size: int = _LOG_RING_SIZE,
    ) -> None:
        super().__init__()
        self._ring = ring
        self._max = max_size
        self._state: DashboardState | None = None

    def set_state(self, state: DashboardState) -> None:
        """Attach DashboardState for WS log broadcasting."""
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            data = json.dumps({"level": record.levelname, "msg": msg})
            self._ring.append(data)
            # Push to WS log subscribers. emit() runs on ARBITRARY threads (any
            # logger call anywhere), so the send is handed to the dashboard's one
            # serving loop rather than to a copy latched by this handler.
            loop = self._state.serving_loop if self._state else None
            if self._state and loop and self._state._ws_log_subscribers:
                ws_msg = json.dumps(
                    {"type": "log", "data": {"level": record.levelname, "msg": msg}}
                )
                for ws in list(self._state._ws_log_subscribers):
                    try:
                        loop.call_soon_threadsafe(
                            loop.create_task,
                            _safe_ws_send(ws, ws_msg, self._state),
                        )
                    except RuntimeError:
                        pass
        except Exception:
            pass


def install_log_ring_handler() -> _RingLogHandler | None:
    """Install the persistent ring buffer handler (call once at startup)."""
    global _log_ring_handler_installed, _log_ring_handler  # noqa: PLW0603
    if _log_ring_handler_installed:
        return _log_ring_handler
    _log_ring_handler_installed = True
    handler = _RingLogHandler(_log_ring, _LOG_RING_SIZE)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("kiro_crew").addHandler(handler)
    _log_ring_handler = handler
    return handler


async def api_logs(request: web.Request) -> web.StreamResponse:
    """GET /api/logs — SSE stream of live log entries.

    Query params:
      - ``lines``: max ring-buffer entries to replay on connect (default 200, max 1000).

    On connect, replays the last *lines* log entries from the ring buffer
    so the client sees history even if the Logs page wasn't open.
    """
    try:
        lines_cap = min(max(int(request.query.get("lines", "200")), 1), _LOG_RING_SIZE)
    except (TypeError, ValueError):
        lines_cap = 200
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    try:
        await resp.prepare(request)
    except (ConnectionResetError, ClientConnectionResetError):
        return resp

    # Replay buffered history first (capped by ?lines=N)
    ring_snapshot = list(_log_ring)
    for data in ring_snapshot[-lines_cap:]:
        try:
            await resp.write(f"data: {data}\n\n".encode())
        except (ConnectionResetError, ClientConnectionResetError):
            return resp

    log_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
    handler = _QueueLogHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("kiro_crew")
    root.addHandler(handler)
    try:
        while not shutdown_event.is_set():
            # Drain any queued log entries
            while not log_queue.empty():
                try:
                    data = log_queue.get_nowait()
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.QueueEmpty:
                    break

            # Wait for new entries or keepalive timeout
            try:
                data = await asyncio.wait_for(log_queue.get(), timeout=30)
                await resp.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        root.removeHandler(handler)
    return resp


# ── Dashboard SSE ──


def _without_pin_ranks(payload: str) -> str:
    """The SSE slots frame with every row's pinned-order fields removed.

    A rank is a place in the person's whole pinned order, which is the
    person's own; a scoped stream client gets none of it, as on the app
    WebSocket and the slots GET.
    """
    from kiro_crew.dashboard.pinned_session_order import PIN_ORDER_FIELDS

    try:
        rows = json.loads(payload)
    except (TypeError, ValueError):
        return payload
    if not isinstance(rows, list):
        return payload
    return json.dumps(
        [
            (
                {k: v for k, v in row.items() if k not in PIN_ORDER_FIELDS}
                if isinstance(row, dict)
                else row
            )
            for row in rows
        ]
    )


async def api_stream(request: web.Request) -> web.StreamResponse:
    """SSE endpoint — pushes status + notifications to each connected client.

    Each client gets its own notification queue (broadcast pattern) so
    multiple tabs all receive every notification.  Uses a simple sleep
    loop with short intervals to check for notifications — lightweight
    and avoids future leaks from asyncio.wait().
    """
    state: DashboardState = request.app["state"]
    # POSITIVE signal from the auth middleware, read once per connection (the
    # token cannot change mid-stream). Never inferred from the absence of an app
    # name, and defaulting to False keeps this deny-by-default: a refactor that
    # reaches this handler without the middleware withholds `meta` rather than
    # publishing it to an unscoped client. Mirrors `api_ws`'s
    # `ws["_is_dashboard_user"]`. Consumed by the chat_message arm below.
    is_dashboard_user: bool = bool(request.get("is_dashboard_user", False))
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    try:
        await resp.prepare(request)
    except (ConnectionResetError, ClientConnectionResetError):
        return resp

    # Register a per-client notification queue
    client_q = state.register_sse()
    try:
        while not shutdown_event.is_set():
            # Drain this client's notification/slots queue
            while not client_q.empty():
                try:
                    note = client_q.get_nowait()
                    msg_type = note.get("_type", "")
                    if msg_type == "slots":
                        payload = note["slots"]
                        if not is_dashboard_user:
                            payload = _without_pin_ranks(payload)
                        await resp.write(f"event: slots\ndata: {payload}\n\n".encode())
                    elif msg_type == "slot_title":
                        payload = json.dumps({"key": note["key"], "title": note["title"]})
                        await resp.write(f"event: slot_title\ndata: {payload}\n\n".encode())
                    elif msg_type == "refresh":
                        await resp.write(f"event: refresh\ndata: {note['kinds']}\n\n".encode())
                    elif msg_type == "chat_message":
                        # Built by the SHARED serialiser, not by hand: this door
                        # and the WebSocket arm in state_parts/slots_push.py are fed the same
                        # note by `_broadcast()`, and rebuilding the frame here
                        # is how `meta` (the row's `meta.mid` dedup identity)
                        # goes missing on one transport while the other keeps it.
                        #
                        # `include_metadata` is NOT True unconditionally. This
                        # queue has no per-app filtering — `_broadcast()` fans
                        # the raw note to every registered SSE client — so
                        # `meta` (tool_input, a live oauth_url, approval_id)
                        # would reach any app token granted this route whatever
                        # its `slots:*` scope. Same class as leaking public-repo
                        # status onto this endpoint. The WS
                        # door may pass True because it filters downstream; this
                        # one must decide here.
                        payload = json.dumps(
                            chat_message_frame(note, include_metadata=is_dashboard_user)
                        )
                        await resp.write(f"event: chat_message\ndata: {payload}\n\n".encode())
                    else:
                        payload = json.dumps(note)
                        await resp.write(f"event: notification\ndata: {payload}\n\n".encode())
                except asyncio.QueueEmpty:
                    break

            data = json.dumps(
                {
                    # The SSE stream is the THIRD status emitter. It routes the
                    # lesson/cron counts through the ONE gateway-wide cache all
                    # three emitters share (status_counts.cached_status_snapshot)
                    # so they never compute inline on the event loop, and that
                    # funnel joins in the update fields from
                    # `status_update_fields()` itself — the one reader — so the
                    # tri-state update fields are not flattened.
                    **await cached_status_snapshot(state),
                    "version": _display_local_version(),
                }
            )
            await resp.write(f"event: dashboard\ndata: {data}\n\n".encode())

            # Sleep in short intervals, wake early if notification arrives
            for _ in range(_SSE_INTERVAL_SECS * 4):
                if shutdown_event.is_set() or not client_q.empty():
                    break
                await asyncio.sleep(0.25)
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        state.unregister_sse(client_q)
    return resp
