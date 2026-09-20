"""WebSocket client registry, authorization, serialization, and fan-out."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from aiohttp import web

#: Socket flag: this client is between its ``members_subscribed`` read and the
#: frame's arrival. ``_send_ws_all`` withholds ``member_projection`` while it is
#: set, because the baseline is a ceiling the client truncates against and a
#: projection applied before it would be dropped by that truncate.
MEMBERS_BASELINE_PENDING = "_members_baseline_pending"

#: Socket store: the ``member_projection`` payloads withheld while the baseline
#: was pending, keyed by ``(slug, key)`` and flushed in insertion order once the
#: baseline is on the wire. Withholding alone LOSES the value: ``lastSeqs`` is
#: read BEFORE the withheld append, so the baseline does not carry it and the
#: client stays stale until that key next changes. Keying by identity is what
#: bounds the store without losing anything -- a projection is last-write-wins
#: per ``(slug, key)``, so a superseded payload is not worth keeping.
MEMBERS_BASELINE_HELD = "_members_baseline_held"

#: Hard cap on distinct held identities per socket. Reaching it means something
#: is wrong with the assumption above (identities are bounded by members x keys),
#: so the socket is closed instead of being handed a partial replay: it
#: reconnects and gets a whole fresh baseline.
MEMBERS_BASELINE_HELD_MAX = 512

#: Marker placed in the held store when the cap is reached. A distinct sentinel
#: rather than a second socket key: the store is cleared and re-created in
#: several places, and a flag living elsewhere would survive one of them.
_HELD_OVERFLOW = "_held_overflow"


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
        # A socket still awaiting its members_subscribed baseline must not be
        # handed a projection first: it would apply the value and then truncate it
        # away against a ceiling read before the value existed.
        gate_baseline = msg_type == "member_projection"
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
            if gate_baseline and ws.get(MEMBERS_BASELINE_PENDING, False):
                # HELD, not dropped: the baseline's lastSeqs were read before
                # this append, so dropping the frame leaves the client stale
                # until the key next changes. Authorize and serialize here, the
                # same as delivery would, then queue the payload for the flush
                # that follows the baseline.
                if not client_allowed(ws, msg_type, data):
                    continue
                try:
                    payload = serialize(ws, msg_type, data, msg)
                except Exception:
                    self._log.warning(
                        "WS payload shaping failed for %s; keeping the client registered",
                        msg_type,
                        exc_info=True,
                    )
                    continue
                self._hold_projection(ws, data, payload)
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

    def _hold_projection(self, ws: web.WebSocketResponse, data: object, payload: str) -> None:
        """Queue one withheld ``member_projection`` payload on *ws*.

        Coalesced by ``(slug, key)``: the frame carries a whole value, so a later
        payload for the same identity supersedes an earlier one and only the last
        needs replaying. Over the cap the queue is abandoned and the socket is
        marked for closure -- a partial replay would be a silent half-truth.
        """
        held = ws.get(MEMBERS_BASELINE_HELD)
        if not isinstance(held, dict):
            held = {}
            ws[MEMBERS_BASELINE_HELD] = held
        if isinstance(data, dict):
            ident: object = (data.get("slug"), data.get("key"))
        else:  # pragma: no cover - the frame's shape is fixed by types.py
            ident = object()
        if ident not in held and len(held) >= MEMBERS_BASELINE_HELD_MAX:
            held.clear()
            held[_HELD_OVERFLOW] = ""
            return
        if _HELD_OVERFLOW in held:
            return
        held[ident] = payload

    async def _flush_held_projections(self, ws: web.WebSocketResponse) -> None:
        """Send the payloads withheld during the baseline, oldest identity first.

        Drains rather than iterating once: ``send_str`` yields, so a broadcast can
        add a payload while this runs. The pending flag is cleared by the caller
        only after this returns empty, so nothing races past the replay.
        """
        while True:
            held = ws.get(MEMBERS_BASELINE_HELD)
            if not isinstance(held, dict) or not held:
                return
            if _HELD_OVERFLOW in held:
                ws[MEMBERS_BASELINE_HELD] = {}
                self._log.warning(
                    "members_subscribed: more than %d projections withheld; "
                    "closing the socket so it re-baselines",
                    MEMBERS_BASELINE_HELD_MAX,
                )
                try:
                    await ws.close()
                except Exception:
                    self._log.debug("held-projection overflow close failed", exc_info=True)
                return
            ident = next(iter(held))
            payload = held.pop(ident)
            try:
                await ws.send_str(payload)
            except Exception:
                self._log.debug("held projection replay failed", exc_info=True)
                ws[MEMBERS_BASELINE_HELD] = {}
                return

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

    def dashboard_user_ws_count(self) -> int:
        """Count open sockets belonging to a dashboard USER, not an app token.

        ``ws_client_count`` counts every ``/api/ws`` registration, and an app
        token is one of them (``_is_dashboard_user`` False, set from the auth
        middleware in ``dashboard/ws.py``). Such a socket does not receive an
        owner-surface frame unless its manifest declared that event -- the same
        first line ``_ws_client_allowed`` gates on -- so a caller asking "is a
        human watching?" must not count it.

        Closed-but-not-yet-pruned sockets are skipped: the registry prunes
        lazily, on the next broadcast.
        """
        return sum(
            1
            for ws in list(self._owner._ws_clients)
            if not ws.closed and ws.get("_is_dashboard_user", False)
        )

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

    async def send_members_subscribed(self, ws: web.WebSocketResponse) -> None:
        """Send the one-shot ``members_subscribed`` frame to a NEW owner socket.

        Carries ``{"lastSeqs": {slug: last_seq}}`` from the per-member event-log
        service, so the client can drop any held member_projection frame whose
        seq is newer than this baseline (a replay/stale-frame guard).

        The socket is marked as AWAITING this baseline for the duration, and
        ``_send_ws_all`` withholds ``member_projection`` from a socket in that
        state. Without it a projection reaching the socket between the
        ``last_seqs`` read and this send is applied by the client and then thrown
        away by the truncate that follows -- live state it already had, lost. A
        withheld frame is HELD, not discarded, and replayed once the baseline is
        on the wire: ``last_seqs`` was read before that append, so the baseline
        does NOT carry its seq and a discarded frame would leave the client stale
        until the same key next changed.

        Owner-only: the caller must gate on a dashboard-user connection and skip
        app-token connections (``member_projection`` / ``members_subscribed`` are
        classified owner-only in ``ws_event_scope``). Best-effort — a serialize
        or send fault is logged and swallowed so it never fails the connection.
        """
        # Set BEFORE the read, cleared only once the frame is on the wire (or has
        # failed): the window this closes starts at the read, not at the send.
        ws[MEMBERS_BASELINE_PENDING] = True
        try:
            try:
                from kiro_crew.eventlog.service import get_service

                # last_seqs() iterates and parses every uncached member log on
                # first dashboard connect -- synchronous file I/O that would stall
                # the serving loop. Offload it, matching the sibling
                # _handle_eventlog_frame read.
                last_seqs = await asyncio.to_thread(get_service().last_seqs)
            except Exception:
                self._log.debug("members_subscribed: last_seqs read failed", exc_info=True)
                return
            try:
                msg = json.dumps({"type": "members_subscribed", "data": {"lastSeqs": last_seqs}})
                await ws.send_str(msg)
            except Exception:
                self._log.debug("members_subscribed send failed", exc_info=True)
                return
            # Only after the baseline is on the wire: a replayed payload carries a
            # seq above the ceiling the client just installed, so it is applied as
            # live rather than truncated away.
            await self._flush_held_projections(ws)
        finally:
            # Cleared even when the baseline never arrived: withholding for the
            # rest of the connection would be a worse failure than one truncate.
            # Anything still held then is dropped with it -- there is no ceiling
            # for the client to apply it against.
            ws[MEMBERS_BASELINE_PENDING] = False
            ws[MEMBERS_BASELINE_HELD] = {}

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
