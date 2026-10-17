"""The services ``DashboardState`` hosts, and the coordinators it reaches them through.

The coordinator factories and the lazy accessors (``_notifications_for`` ..
``_registry_for``) a ``__new__``-built state still resolves its coordinators by.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard.dashboard_persistence import DashboardPersistenceCoordinator
from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator, QuestionCoordinator
from kiro_crew.dashboard.notification_coordinator import NotificationCoordinator
from kiro_crew.dashboard.slot_registry import SlotRegistry
from kiro_crew.dashboard.state_parts.notification_log import (
    _MAX_PERSISTED_NOTIFICATIONS,
    _notification_io_executor,
    _persist_notification,
    _redact_note_value,
    _rewrite_notifications,
    sweep_expired_notifications,
)
from kiro_crew.dashboard.state_parts.transcript_rows import _mark_permission_resolved
from kiro_crew.dashboard.websocket_hub import WebSocketHub
from kiro_crew.notifications.bus import NotificationValidationError, payload_from_legacy
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.dashboard.state")


def _new_notification_coordinator() -> NotificationCoordinator:
    """Build a coordinator whose providers resolve facade seams at call time."""
    return NotificationCoordinator(
        logger_provider=lambda: logger,
        payload_from_legacy=lambda *args, **kwargs: payload_from_legacy(*args, **kwargs),
        validation_error=NotificationValidationError,
        redact_value=lambda value: _redact_note_value(value),
        sweep_expired=lambda notes: sweep_expired_notifications(notes),
        persist_one=lambda note: _persist_notification(note),
        rewrite_all=lambda notes: _rewrite_notifications(notes),
        executor_provider=lambda: _notification_io_executor(),
        max_persisted=_MAX_PERSISTED_NOTIFICATIONS,
    )


def _notifications_for(state: Any) -> NotificationCoordinator:
    """Return the per-state coordinator, including for ``__new__`` test states."""
    coordinator = getattr(state, "_notification_coordinator", None)
    if not isinstance(coordinator, NotificationCoordinator):
        coordinator = _new_notification_coordinator()
        state._notification_coordinator = coordinator
    return coordinator


def _approvals_for(state: Any) -> ApprovalCoordinator:
    coordinator = getattr(state, "_approval_coordinator", None)
    if not isinstance(coordinator, ApprovalCoordinator):
        coordinator = ApprovalCoordinator()
        state._approval_coordinator = coordinator
    return coordinator


def _questions_for(state: Any) -> QuestionCoordinator:
    coordinator = getattr(state, "_question_coordinator", None)
    if not isinstance(coordinator, QuestionCoordinator):
        coordinator = QuestionCoordinator()
        state._question_coordinator = coordinator
    return coordinator


def _permission_marker() -> Callable[[list[dict], str, str], bool]:
    """Resolve the module-level marker lazily for monkeypatched tests/callers."""
    return _mark_permission_resolved


def _new_websocket_hub(state: Any) -> WebSocketHub:
    """Build a hub whose providers resolve facade seams at call time."""
    return WebSocketHub(
        state,
        serving_loop_provider=lambda: state.serving_loop,
        running_loop_provider=lambda: state._running_loop(),
        logger_provider=lambda: logger,
        redact_credentials_provider=lambda: redact_credentials,
        redact_exfiltration_urls_provider=lambda: redact_exfiltration_urls,
        scope_state_provider=lambda: state,
    )


def _websocket_for(state: Any) -> WebSocketHub:
    """Return the per-state hub, including for ``__new__`` test states."""
    hub = getattr(state, "_websocket_hub", None)
    if not isinstance(hub, WebSocketHub):
        hub = _new_websocket_hub(state)
        state._websocket_hub = hub
    return hub


def _new_dashboard_persistence() -> DashboardPersistenceCoordinator:
    """Build persistence providers that preserve module monkeypatch seams."""
    return DashboardPersistenceCoordinator(
        config_dir_provider=lambda: config_dir(),
        atomic_write_provider=lambda: atomic_write,
        logger_provider=lambda: logger,
        json_codec_provider=lambda: json,
        wall_time_provider=lambda: time.time(),
    )


def _persistence_for(state: Any) -> DashboardPersistenceCoordinator:
    """Return the per-state coordinator, including for ``__new__`` test states."""
    coordinator = getattr(state, "_persistence_coordinator", None)
    if not isinstance(coordinator, DashboardPersistenceCoordinator):
        coordinator = _new_dashboard_persistence()
        state._persistence_coordinator = coordinator
    return coordinator


def _registry_for(state: Any) -> SlotRegistry:
    """Return the per-state registry boundary for facade-compatible test states."""
    registry = getattr(state, "_slot_registry", None)
    if not isinstance(registry, SlotRegistry):
        registry = SlotRegistry()
        state._slot_registry = registry
    return registry
