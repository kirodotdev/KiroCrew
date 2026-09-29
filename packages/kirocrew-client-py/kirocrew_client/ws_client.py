"""WsClient — the Gateway's ``/api/ws`` real-time event stream.

Typed and slot-scoped listeners, raw message access, and auto-reconnect with
exponential backoff. Every Gateway frame is ``{"type": <event>, "data": {...}}``;
slot-bound events normally carry the slot key in ``data["slot"]``. The
``slot_title`` and ``session_summary`` frames use ``data["key"]`` instead.

Listener callbacks run synchronously on the event loop, in the order typed,
then slot-scoped. An exception in one listener is logged and does not stop
the others.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Union

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_RECONNECT_BASE = 1.0
_DEFAULT_RECONNECT_MAX = 30.0


@dataclass
class WsEvent:
    type: str
    data: dict[str, Any]


EventCallback = Callable[[WsEvent], None]
#: ``(connected, is_reconnect)``
ConnectionCallback = Callable[[bool, bool], None]
ReconnectHook = Callable[[], Union[None, Awaitable[None]]]


def compute_reconnect_delay(attempt: int, base: float, maximum: float) -> float:
    if base <= 0 or maximum <= 0:
        return min(base, maximum)
    if base >= maximum:
        return maximum
    if attempt >= math.log2(maximum) - math.log2(base):
        return maximum
    return base * (2**attempt)


class WsClient:
    """Async WebSocket client with auto-reconnect and typed event dispatch.

    Usually built with :meth:`KiroCrewClient.create_ws`, which wires the
    client's auth cookie in::

        ws = mc.create_ws()
        ws.on_slot("slot-1", "chat_chunk", lambda ev: print(ev.data))
        await ws.connect()
        ...
        await ws.disconnect()
    """

    def __init__(
        self,
        ws_url: str,
        *,
        origin: str = "",
        get_headers: Callable[[], dict[str, str]] | None = None,
        reconnect_base_delay: float = _DEFAULT_RECONNECT_BASE,
        reconnect_max_delay: float = _DEFAULT_RECONNECT_MAX,
        on_reconnect: ReconnectHook | None = None,
    ):
        self._ws_url = ws_url
        self._origin = origin
        self._get_headers = get_headers
        self._reconnect_base = reconnect_base_delay
        self._reconnect_max = reconnect_max_delay
        self._on_reconnect = on_reconnect

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._connected = False
        self._destroyed = False
        self._has_connected_once = False
        self._retry_count = 0
        self._task: asyncio.Task[None] | None = None

        self._typed_listeners: dict[str, set[EventCallback]] = {}
        self._slot_listeners: dict[str, set[EventCallback]] = {}
        self._raw_listeners: set[EventCallback] = set()
        self._connection_listeners: set[ConnectionCallback] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Listeners (each returns an unsubscribe function) ──

    def on(self, event_type: str, callback: EventCallback) -> Callable[[], None]:
        listeners = self._typed_listeners.setdefault(event_type, set())
        listeners.add(callback)
        return lambda: listeners.discard(callback)

    def on_slot(self, slot_id: str, event_type: str, callback: EventCallback) -> Callable[[], None]:
        listeners = self._slot_listeners.setdefault(f"{slot_id}:{event_type}", set())
        listeners.add(callback)
        return lambda: listeners.discard(callback)

    def on_raw(self, callback: EventCallback) -> Callable[[], None]:
        self._raw_listeners.add(callback)
        return lambda: self._raw_listeners.discard(callback)

    def on_connection_change(self, callback: ConnectionCallback) -> Callable[[], None]:
        self._connection_listeners.add(callback)
        return lambda: self._connection_listeners.discard(callback)

    # ── Connection ──

    async def connect(self) -> None:
        """Start the connect/reconnect loop in the background. Idempotent."""
        if self._destroyed:
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())

    async def disconnect(self) -> None:
        """Stop reconnecting and close the socket. The client cannot be reused."""
        self._destroyed = True
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._close()

    async def _close(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._set_disconnected()

    async def _run_loop(self) -> None:
        # One session for every retry, so reconnects do not leak sessions.
        self._session = aiohttp.ClientSession()
        try:
            while not self._destroyed:
                try:
                    await self._connect_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("WS connection error, retrying", exc_info=True)
                if self._destroyed:
                    break
                delay = compute_reconnect_delay(
                    self._retry_count, self._reconnect_base, self._reconnect_max
                )
                self._retry_count += 1
                await asyncio.sleep(delay)
                if self._on_reconnect:
                    # e.g. refresh a token that expired while the socket was down
                    try:
                        result = self._on_reconnect()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.debug("on_reconnect hook failed", exc_info=True)
        finally:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _connect_once(self) -> None:
        assert self._session is not None
        headers = self._get_headers() if self._get_headers else {}
        async with self._session.ws_connect(
            self._ws_url,
            headers=headers,
            origin=self._origin or None,
        ) as ws:
            self._ws = ws
            is_reconnect = self._has_connected_once
            self._has_connected_once = True
            self._connected = True
            self._retry_count = 0
            self._emit_connection(True, is_reconnect)
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, dict):
                            self._dispatch(data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                self._ws = None
                self._set_disconnected()

    def _set_disconnected(self) -> None:
        if self._connected:
            self._connected = False
            self._emit_connection(False, False)

    # ── Dispatch ──

    def _dispatch(self, raw: dict[str, Any]) -> None:
        event_type = str(raw.get("type", ""))
        event_data = raw.get("data", raw)
        if not isinstance(event_data, dict):
            event_data = {"value": event_data}
        event = WsEvent(type=event_type, data=event_data)

        for cb in list(self._raw_listeners):
            _safe_call(cb, event)
        for cb in list(self._typed_listeners.get(event_type, ())):
            _safe_call(cb, event)
        slot_id = event_data.get("slot")
        if slot_id is None and event_type in ("slot_title", "session_summary"):
            slot_id = event_data.get("key")
        if isinstance(slot_id, str) and slot_id:
            for cb in list(self._slot_listeners.get(f"{slot_id}:{event_type}", ())):
                _safe_call(cb, event)

    def _emit_connection(self, connected: bool, is_reconnect: bool) -> None:
        for cb in list(self._connection_listeners):
            try:
                cb(connected, is_reconnect)
            except Exception:
                logger.debug("connection listener error", exc_info=True)


def _safe_call(cb: Callable[[Any], None], arg: Any) -> None:
    try:
        cb(arg)
    except Exception:
        logger.debug("WS listener error", exc_info=True)
