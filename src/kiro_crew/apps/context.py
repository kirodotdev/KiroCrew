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
from kiro_crew.apps.spawn_sdk import SpawnSDK

# Nav-status render vocabulary (issue #520). The app maps its OWN domain states
# to one of these tones; the core owns the pixels. Unknown tones degrade to
# "neutral" rather than raising, so a typo never breaks the app.
NAV_STATUS_EVENT = "app_nav_status"
NAV_STATUS_TONES = ("neutral", "busy", "positive", "caution", "critical")
NAV_STATUS_LABEL_MAX = 48
_NAV_STATUS_KEY = "_nav_status"  # reserved AppStorage key for durability


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
    spawn: SpawnSDK | None = None
    health: AppHealthStatus = field(default_factory=AppHealthStatus)

    def set_nav_status(self, tone: str, label: str | None = None) -> None:
        """Report a runtime status rendered on the app's sidebar nav icon.

        ``tone`` is mapped to a render treatment by the core; an unrecognized
        tone degrades to ``"neutral"`` (never raises). ``label`` is optional
        free-form text used as the icon tooltip/aria text, length-capped for
        render safety. The app owns its own state vocabulary and maps it to a
        tone; the core validates render safety only, not semantics.

        No-op when the app cannot publish the reserved event — either it
        declared no events (``ctx.events`` is None) or it declared events
        without ``app_nav_status`` (the publish is denied). In both cases
        nothing is broadcast or persisted, and the call never raises into the
        app's hook. When permitted and storage is available, the last status is
        persisted under a reserved AppStorage key so a freshly loaded dashboard
        shows current state before the next live push.
        """
        if self.events is None:
            return
        safe_tone = tone if tone in NAV_STATUS_TONES else "neutral"
        safe_label = (label or "")[:NAV_STATUS_LABEL_MAX]
        status = {"tone": safe_tone, "label": safe_label}
        try:
            # Let EventBus own the permission decision so its SEL audit fires on
            # both the allow and the deny path; it raises PermissionError when
            # the app did not declare the reserved event. Catch it to keep the
            # no-op contract, and persist only after a successful publish so a
            # denied app can never leave a stored status behind.
            self.events.publish(NAV_STATUS_EVENT, status)
        except PermissionError:
            return
        if self.storage is not None:
            self.storage.set(_NAV_STATUS_KEY, status)


def build_app_context(
    app_name: str,
    data_dir: Path,
    *,
    permissions: dict[str, Any] | None = None,
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
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

    # Build SpawnSDK if permitted. Same shape as the others: no permission or
    # no host implementation -> None, and the app's own guard reports it.
    spawn_sdk = None
    if perms.get("spawn") is True and spawn_impl is not None:
        # The completion probe rides on the impl callable (see build_spawn_impl)
        # so the spawn wiring stays a single parameter end to end.
        spawn_sdk = SpawnSDK(
            app_name, spawn_impl, done_probe=getattr(spawn_impl, "done_probe", None)
        )

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
        spawn=spawn_sdk,
    )


def read_persisted_nav_status(data_dir: Path) -> dict[str, str] | None:
    """Return an app's last persisted nav status, render-safe, or None.

    Reads the reserved ``_nav_status`` key from the app's storage so a freshly
    loaded dashboard (``GET /api/apps``) can show current state before the next
    live push. Delegates to :meth:`AppStorage.read_key` — a read-only accessor
    that owns the on-disk layout and does no directory creation / write I/O in
    this read path. Defensive on read: a missing/corrupt/unreadable file yields
    None, an unknown persisted tone is coerced to ``"neutral"``, and the label is
    length-capped, so a hand-edited or corrupt file cannot inject an unrenderable
    tone or oversized text into the nav.
    """
    stored = AppStorage.read_key(data_dir, _NAV_STATUS_KEY)
    if not isinstance(stored, dict):
        return None
    raw_tone = stored.get("tone")
    tone = raw_tone if raw_tone in NAV_STATUS_TONES else "neutral"
    label = str(stored.get("label", ""))[:NAV_STATUS_LABEL_MAX]
    return {"tone": tone, "label": label}
