"""WebSocket client registry, authorization, serialization, and fan-out."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from aiohttp import web


class WebSocketHubOwner(Protocol):
    """The mutable facade-owned state the hub operates on.

    These collections intentionally remain owned by ``DashboardState``. Existing
    handlers and tests inspect or replace them directly, so copying them into the
    hub would create two registries and make disconnect cleanup depend on which
    reference a caller happened to mutate.
    """

    _ws_clients: list[web.WebSocketResponse]
    _owner_ws_clients: set[web.WebSocketResponse]
    _ws_log_subscribers: set[web.WebSocketResponse]
    _ws_subagent_subscribers: set[web.WebSocketResponse]
    _background_tasks: set[asyncio.Task[Any]]
    _flush_task: asyncio.Task[Any] | None


Redactor = Callable[[str], tuple[str, Any]]


class WebSocketHub:
    """Coordinate WebSocket clients without owning dashboard domain state.

    The owner and providers are injected so this module never imports the state
    facade. Providers are resolved at call time, preserving monkeypatch seams and
    the serving-loop value bound after construction.
    """

    def __init__(
        self,
        owner: WebSocketHubOwner,
        *,
        serving_loop_provider: Callable[[], asyncio.AbstractEventLoop | None],
        logger_provider: Callable[[], logging.Logger],
        redact_credentials_provider: Callable[[], Redactor],
        redact_exfiltration_urls_provider: Callable[[], Redactor],
        scope_state_provider: Callable[[], Any] | None = None,
        running_loop_provider: Callable[[], asyncio.AbstractEventLoop | None] | None = None,
    ) -> None:
        self._owner = owner
        self._serving_loop_provider = serving_loop_provider
        self._logger_provider = logger_provider
        self._redact_credentials_provider = redact_credentials_provider
        self._redact_exfiltration_urls_provider = redact_exfiltration_urls_provider
        self._scope_state_provider: Callable[[], Any] = (
            scope_state_provider if scope_state_provider is not None else lambda: owner
        )
        self._running_loop_provider = running_loop_provider or self._running_loop

    @property
    def _log(self) -> logging.Logger:
        return self._logger_provider()

    def _owner_method(self, name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
        """Resolve a facade seam at call time, falling back for standalone use.

        Dashboard tests and integrations replace several of these methods on an
        individual ``DashboardState`` instance. Looking them up for each fan-out
        keeps those seams live after the implementation moves behind this hub.
        The normal facade wrappers delegate back to the matching hub method, so
        the lookup does not transfer ownership of any collection.
        """
        method = getattr(self._owner, name, None)
        return method if callable(method) else fallback

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        """Return the running loop, or None when called off the event loop."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _spawn_ws_send(self, ws: web.WebSocketResponse, msg: str) -> None:
        """Fire-and-forget a WS send while retaining a strong task reference.

        A fan-out may originate on a worker thread. In that case the send hops to
        the dashboard serving loop and creates its coroutine there. Only a
        synchronous refusal from ``send_str`` escapes; scheduling failures are a
        process condition and must not unregister an otherwise healthy peer.
        """
        loop = self._running_loop_provider()
        if loop is None:
            target = self._serving_loop_provider()
            if target is not None and not target.is_closed():
                try:
                    spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
                    target.call_soon_threadsafe(spawn, ws, msg)
                    return
                except RuntimeError:
                    self._log.debug("WS send: serving loop is shutting down")
            # Still call send_str so a synchronous peer refusal reaches the
            # fan-out. Close a returned coroutine because there is no loop on
            # which it can run.
            coro = ws.send_str(msg)
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            self._log.debug("WS send dropped: no serving loop to run it on")
            return

        # Resolve the provider on-loop as well. DashboardState's provider latches
        # this loop only when startup has not already bound an authoritative one.
        self._serving_loop_provider()
        task = asyncio.ensure_future(ws.send_str(msg))
        self._owner._background_tasks.add(task)
        done = self._owner_method("_on_ws_send_done", self._on_ws_send_done)
        task.add_done_callback(done)

    def _on_ws_send_done(self, task: asyncio.Task[Any]) -> None:
        """Release a completed send task and surface asynchronous failures."""
        self._owner._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log.debug("WS send failed (client likely disconnected): %s", exc)

    def _ws_client_allowed(
        self,
        ws: web.WebSocketResponse,
        msg_type: str,
        data: object,
    ) -> bool:
        """Apply the deny-by-default event-scope gate for one client."""
        if ws.get("_is_dashboard_user", False):
            return True
        ws_app: str = ws.get("_app", "")
        snapshot: frozenset[str] = ws.get("_allowed_events", frozenset())
        data_dict: dict[Any, Any] = data if isinstance(data, dict) else {}
        try:
            from kiro_crew.dashboard.ws_event_scope import (
                _audit_deny,
                effective_allowed_events,
                ws_event_allowed,
            )

            allowed = effective_allowed_events(ws_app, snapshot)
            return ws_event_allowed(
                msg_type,
                data_dict,
                app=ws_app,
                allowed_events=allowed,
                state=self._scope_state_provider(),
            )
        except Exception:
            try:
                _audit_deny(ws_app or "<unknown>", msg_type, "scope_check_exception")
            except Exception as inner_exc:
                self._log.debug(
                    "state: audit for scope_check_exception failed for %s/%s: %s",
                    ws_app,
                    msg_type,
                    inner_exc,
                )
            return False

    def _serialize_for_client(
        self,
        ws: web.WebSocketResponse,
        msg_type: str,
        data: object,
        default_msg: str,
    ) -> str:
        """Return a payload filtered for one dashboard or app client."""
        if ws.get("_is_dashboard_user", False):
            return default_msg
        if msg_type in ("subagent_batch_update", "subagent_batch_chunks"):
            serialize_batch = self._owner_method(
                "_serialize_subagent_batch", self._serialize_subagent_batch
            )
            return serialize_batch(ws, msg_type, data, default_msg)
        if msg_type != "slots":
            return default_msg
        if not isinstance(data, dict) or "slots" not in data:
            return default_msg

        snapshot: frozenset[str] = ws.get("_allowed_events", frozenset())
        ws_app: str = ws.get("_app", "")
        try:
            from kiro_crew.dashboard.ws_event_scope import (
                effective_allowed_events,
                filter_slots_for_app,
                slots_envelope_extras,
            )

            allowed = effective_allowed_events(ws_app, snapshot)
            scope_state = self._scope_state_provider()
            filtered = filter_slots_for_app(data["slots"], ws_app, allowed, scope_state)
            extras = slots_envelope_extras(allowed, yolo=bool(data.get("yolo", False)))
        except Exception:
            # The safe fallback is an empty slot list with no global posture
            # fields; defaulting those fields to false still reveals state.
            filtered = []
            extras = {}
        return json.dumps({"type": "slots", "data": filtered, **extras})

    def _serialize_subagent_batch(
        self,
        ws: web.WebSocketResponse,
        msg_type: str,
        data: object,
        default_msg: str,
    ) -> str:
        """Filter every item in a coalesced subagent frame for one app."""
        from kiro_crew.dashboard.ws_event_scope import (
            _SUBAGENT_BATCH_ITEM_KEY,
            filter_subagent_batch_for_app,
        )

        key = _SUBAGENT_BATCH_ITEM_KEY.get(msg_type, "")
        if not key or not isinstance(data, dict) or not isinstance(data.get(key), list):
            return json.dumps({"type": msg_type, "data": {key or "items": []}})
        snapshot: frozenset[str] = ws.get("_allowed_events", frozenset())
        ws_app: str = ws.get("_app", "")
        try:
            from kiro_crew.dashboard.ws_event_scope import effective_allowed_events

            allowed = effective_allowed_events(ws_app, snapshot)
            items = filter_subagent_batch_for_app(
                data[key],
                ws_app,
                allowed,
                self._scope_state_provider(),
                msg_type=msg_type,
            )
        except Exception:
            # Preserve the facade's historical ``_log`` seam when a harness or
            # embedding supplies it; ordinary DashboardState instances fall
            # back to the module logger provider used by every other hub path.
            batch_log = getattr(self._owner, "_log", self._log)
            batch_log.warning("subagent batch filter failed; dropping items", exc_info=True)
            items = []
        return json.dumps({"type": msg_type, "data": {key: items}})

    def _send_ws_all(self, msg_type: str, data: object, msg: str) -> None:
        """Send one typed frame through the per-client authorization chokepoint."""
        dead: list[web.WebSocketResponse] = []
        skip_owners = msg_type == "slots"
        owners = getattr(self._owner, "_owner_ws_clients", None) or set()
        client_allowed = self._owner_method("_ws_client_allowed", self._ws_client_allowed)
        serialize = self._owner_method("_serialize_for_client", self._serialize_for_client)
        spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws in list(self._owner._ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            if skip_owners and ws in owners:
                continue
            if not client_allowed(ws, msg_type, data):
                continue
            try:
                payload = serialize(ws, msg_type, data, msg)
            except Exception:
                # Payload shaping is our failure, not evidence that the peer is
                # dead. Keep the registration so later frames can recover.
                self._log.warning(
                    "WS payload shaping failed for %s; keeping the client registered",
                    msg_type,
                    exc_info=True,
                )
                continue
            try:
                spawn(ws, payload)
            except Exception:
                # Only a synchronous send_str refusal reaches here.
                dead.append(ws)
        for ws in dead:
            remove(ws)

    def _send_ws_owners(self, msg: str) -> None:
        """Send a pre-serialized message only to owner-authenticated clients."""
        dead: list[web.WebSocketResponse] = []
        spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws in list(self._owner._owner_ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                spawn(ws, msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            remove(ws)

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        """Send a typed message to every authorized WS client."""
        if not self._owner._ws_clients:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        send_all = self._owner_method("_send_ws_all", self._send_ws_all)
        send_all(msg_type, data, msg)

    async def deliver_ws_owners(self, msg_type: str, data: object) -> int:
        """Await owner-only sends and return the number that completed."""
        targets = [ws for ws in list(self._owner._owner_ws_clients) if not ws.closed]
        if not targets:
            return 0
        msg = json.dumps({"type": msg_type, "data": data})
        results = await asyncio.gather(
            *(ws.send_str(msg) for ws in targets),
            return_exceptions=True,
        )
        delivered = 0
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws, result in zip(targets, results):
            if isinstance(result, BaseException):
                self._log.debug("Owner WS send failed (client likely disconnected): %s", result)
                remove(ws)
            else:
                delivered += 1
        for ws in list(self._owner._owner_ws_clients):
            if ws.closed:
                remove(ws)
        return delivered

    def broadcast_ws_owners(self, msg_type: str, data: object) -> None:
        """Send a typed message only to owner-authorized clients."""
        if not getattr(self._owner, "_owner_ws_clients", None):
            return
        msg = json.dumps({"type": msg_type, "data": data})
        send_owners = self._owner_method("_send_ws_owners", self._send_ws_owners)
        send_owners(msg)

    def ws_client_count(self) -> int:
        return len(self._owner._ws_clients)

    def broadcast_browser_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Redact and broadcast a browser activity event."""
        redact_credentials = self._redact_credentials_provider()
        redact_exfiltration_urls = self._redact_exfiltration_urls_provider()
        safe_data: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                value, _ = redact_credentials(value)
                value, _ = redact_exfiltration_urls(value)
            safe_data[key] = value
        payload: dict[str, Any] = {
            "type": "browser_event",
            "event": event_type,
            "ts": time.time(),
        }
        for key, value in safe_data.items():
            if key not in ("type", "event", "ts"):
                payload[key] = value
        broadcast = self._owner_method("broadcast_ws", self.broadcast_ws)
        broadcast("browser_event", payload)

    def register_ws(self, ws: web.WebSocketResponse, *, owner: bool = False) -> None:
        """Register a client and latch the serving loop before its first frame."""
        self._owner._ws_clients.append(ws)
        if owner:
            self._owner._owner_ws_clients.add(ws)
        self._serving_loop_provider()

    def unregister_ws(self, ws: web.WebSocketResponse) -> None:
        remove = self._owner_method("_remove_ws", self._remove_ws)
        remove(ws)

    def _remove_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a client from the registry and every subscriber subset."""
        try:
            self._owner._ws_clients.remove(ws)
        except ValueError:
            pass
        self._owner._owner_ws_clients.discard(ws)
        self._owner._ws_log_subscribers.discard(ws)
        self._owner._ws_subagent_subscribers.discard(ws)

    def subscribe_logs(self, ws: web.WebSocketResponse) -> None:
        self._owner._ws_log_subscribers.add(ws)

    def unsubscribe_logs(self, ws: web.WebSocketResponse) -> None:
        self._owner._ws_log_subscribers.discard(ws)

    def subscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        self._owner._ws_subagent_subscribers.add(ws)

    def unsubscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        self._owner._ws_subagent_subscribers.discard(ws)

    def broadcast_ws_subagent_subscribers(self, msg_type: str, data: object) -> None:
        """Fan out heavy subagent data only to subscribed, authorized clients."""
        if not self._owner._ws_subagent_subscribers:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        dead: list[web.WebSocketResponse] = []
        client_allowed = self._owner_method("_ws_client_allowed", self._ws_client_allowed)
        serialize = self._owner_method("_serialize_for_client", self._serialize_for_client)
        spawn = self._owner_method("_spawn_ws_send", self._spawn_ws_send)
        remove = self._owner_method("_remove_ws", self._remove_ws)
        for ws in list(self._owner._ws_subagent_subscribers):
            if ws.closed:
                dead.append(ws)
                continue
            if not client_allowed(ws, msg_type, data):
                continue
            try:
                payload = serialize(ws, msg_type, data, msg)
            except Exception:
                self._log.warning(
                    "WS subagent payload shaping failed for %s; keeping the client registered",
                    msg_type,
                    exc_info=True,
                )
                continue
            try:
                spawn(ws, payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            remove(ws)

    async def close_all_ws(self) -> None:
        """Cancel the flush loop, close sockets in order, then clear registries."""
        if self._owner._flush_task:
            self._owner._flush_task.cancel()
            self._owner._flush_task = None
        for ws in list(self._owner._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._owner._ws_clients.clear()
        self._owner._owner_ws_clients.clear()
        self._owner._ws_log_subscribers.clear()
        self._owner._ws_subagent_subscribers.clear()
