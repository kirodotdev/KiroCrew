"""EventBus — app-scoped event publishing via gateway WebSocket broadcast.

Thin wrapper over the existing DashboardState.broadcast() mechanism.
Apps publish events scoped to their declared ``permissions.events`` list.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

#: Fixed WS message type for ALL app-published events. App event names are
#: app-chosen and collide with CORE WS types (an app publishing "notification"
#: would land in the dashboard's notification feed), so app events ride under one
#: namespaced type with the real event name carried inside the envelope.
APP_EVENT_WS_TYPE = "app_event"

logger = logging.getLogger(__name__)


class EventBus:
    """App-scoped event publishing.

    Wraps the gateway's broadcast function with permission enforcement.
    Only event types declared in the app's ``permissions.events`` list
    (or ``"*"`` for unrestricted) are allowed.
    """

    def __init__(
        self,
        app_name: str,
        allowed_events: list[str],
        broadcast_fn: Callable[[dict[str, Any]], None],
    ) -> None:
        self._app_name = app_name
        self._allowed = set(allowed_events)
        self._broadcast = broadcast_fn

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def allowed_events(self) -> set[str]:
        return set(self._allowed)

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Broadcast an event to all connected WebSocket clients.

        Raises PermissionError if event_type is not in the app's declared events.
        The broadcast payload is: {"type": event_type, "app": app_name, "data": data}
        """
        self._check_permission(event_type)
        safe_data = self._redact_data(data or {})
        payload = {
            "type": event_type,
            "app": self._app_name,
            "data": safe_data,
        }
        self._broadcast(payload)
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="event_publish",
            outcome="ok",
            resources=event_type,
        )
        logger.debug("App %s published event: %s", self._app_name, event_type)

    def publish_to_app(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Publish event scoped to this app's subscribers.

        v1 behavior: equivalent to publish() (full broadcast). The gateway's
        existing WS handler does not support per-app filtering. The _scope field
        is included in the payload as a forward-compatible marker — when WS
        subscription filtering is added (future), clients already receiving
        _scope="app" payloads will automatically benefit without API changes.
        """
        self._check_permission(event_type)
        safe_data = self._redact_data(data or {})
        payload = {
            "type": event_type,
            "app": self._app_name,
            "data": safe_data,
            "_scope": "app",
        }
        self._broadcast(payload)
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="event_publish_to_app",
            outcome="ok",
            resources=event_type,
        )
        logger.debug("App %s published scoped event: %s", self._app_name, event_type)

    def _check_permission(self, event_type: str) -> None:
        """Raise PermissionError if event_type is not allowed."""
        if event_type not in self._allowed and "*" not in self._allowed:
            sel().log_api_access(
                caller=f"app:{self._app_name}",
                operation="event_publish",
                outcome="denied",
                resources=event_type,
                error="not in declared events",
            )
            raise PermissionError(
                f"App {self._app_name!r} not permitted to publish event {event_type!r}. "
                f"Declared: {sorted(self._allowed)}"
            )

    def _redact_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """Recursively redact credentials and exfiltration URLs from all string values."""
        return self._redact_value(data)  # type: ignore[return-value]

    def _redact_value(self, value: Any) -> Any:
        """Recursively redact string values in nested structures."""
        if isinstance(value, str):
            value, _ = redact_credentials(value)
            value, _ = redact_exfiltration_urls(value)
            return value
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value


def build_broadcast_fn(send: object) -> object:
    """Adapt a ``(msg_type, data)`` sender to the single-payload callback an
    :class:`EventBus` calls.

    The two shapes cannot be wired directly: EventBus hands its callback ONE
    payload dict (``{"type", "app", "data"}``) while the gateway's WS sender takes
    two positional args. Passing the bound method straight through raises
    TypeError inside every publish; passing a non-existent attribute silently
    yields None, which leaves the app with ``events=None`` and every app event a
    no-op. Both failure modes were live.
    """

    # Capture the gateway loop at wiring time (this runs during async gateway
    # setup). `_broadcast` is called from BOTH the loop AND worker threads — the
    # Mochi owner loop offloads its ticks via `asyncio.to_thread`, and those fire
    # app events — while the WS sender schedules each send ON the loop
    # (`ensure_future`). Called straight from a worker thread that raises, and the
    # sender's error path marks every client dead and evicts them. So marshal a
    # worker-thread broadcast back onto the loop with `call_soon_threadsafe`.
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = None

    def _broadcast(payload: dict) -> None:
        event_type = str(payload.get("type", ""))
        # All non-`type` fields ride inside the WS data arg (the wire is
        # `{type, data}`), and the REAL event name goes in `event` — the WS
        # `type` is the fixed APP_EVENT_WS_TYPE so an app's event name can never
        # collide with a core WS message type. `app` and `_scope` are preserved so
        # the client can tell which app emitted it and at what scope.
        envelope = {k: v for k, v in payload.items() if k != "type"}
        envelope["event"] = event_type
        try:
            _running = asyncio.get_running_loop()
        except RuntimeError:
            _running = None
        if _loop is not None and _running is not _loop:
            _loop.call_soon_threadsafe(send, APP_EVENT_WS_TYPE, envelope)  # type: ignore[arg-type]
        else:
            send(APP_EVENT_WS_TYPE, envelope)  # type: ignore[operator]

    return _broadcast
