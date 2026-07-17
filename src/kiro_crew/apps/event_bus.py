"""EventBus — app-scoped event publishing via gateway WebSocket broadcast.

Thin wrapper over the existing DashboardState.broadcast() mechanism.
Apps publish events scoped to their declared ``permissions.events`` list.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

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
