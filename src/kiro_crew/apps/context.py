"""App Context — scoped access to gateway services for app hooks and routes.

The AppContext is created per-app at enable time and injected into route
handlers and lifecycle hooks. It provides a controlled surface area — apps
interact with gateway services only through this interface.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.event_bus import EventBus


@dataclass
class AppHealthStatus:
    """Tracks the health of an app's subsystems after enable."""

    status: str = "healthy"  # "healthy" | "degraded" | "error"
    issues: list[str] = field(default_factory=list)
    last_checked: str = ""  # ISO 8601

    def mark_degraded(self, issue: str) -> None:
        """Mark a non-critical subsystem failure."""
        self.status = "degraded"
        self.issues.append(issue)
        self.last_checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def mark_error(self, issue: str) -> None:
        """Mark a critical failure."""
        self.status = "error"
        self.issues.append(issue)
        self.last_checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status}
        if self.issues:
            d["issues"] = self.issues
        if self.last_checked:
            d["last_checked"] = self.last_checked
        return d


@dataclass
class AppContext:
    """Scoped context passed to app hooks and route handlers.

    Only services declared in the app's permissions are populated;
    others are None.
    """

    name: str
    data_dir: Path
    config: dict[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("kirocrew.app"))
    cron: CronSDK | None = None
    events: EventBus | None = None
    storage: AppStorage | None = None
    health: AppHealthStatus = field(default_factory=AppHealthStatus)


def build_app_context(
    app_name: str,
    data_dir: Path,
    *,
    permissions: dict[str, Any] | None = None,
    cron_service: Any = None,
    broadcast_fn: Any = None,
    app_config: dict[str, Any] | None = None,
) -> AppContext:
    """Factory that builds an AppContext based on app permissions.

    Args:
        app_name: The app's identifier.
        data_dir: The app's data directory path.
        permissions: The app's declared permissions dict from manifest.
        cron_service: The gateway's CronService instance (for CronSDK).
        broadcast_fn: The gateway's broadcast function (for EventBus).
        app_config: App-specific configuration dict.

    Returns:
        AppContext with services populated based on permissions.
    """
    perms = permissions or {}
    ctx_logger = logging.getLogger(f"kirocrew.app.{app_name}")

    # Build CronSDK if permitted
    cron_sdk = None
    if perms.get("cron") and cron_service is not None:
        cron_sdk = CronSDK(app_name, cron_service)

    # Build EventBus if permitted
    event_bus = None
    events_list = perms.get("events", [])
    if events_list and broadcast_fn is not None:
        event_bus = EventBus(app_name, events_list, broadcast_fn)

    # Build AppStorage if permitted
    app_storage = None
    if perms.get("storage"):
        app_storage = AppStorage(app_name, data_dir)

    return AppContext(
        name=app_name,
        data_dir=data_dir,
        config=app_config or {},
        logger=ctx_logger,
        cron=cron_sdk,
        events=event_bus,
        storage=app_storage,
    )
