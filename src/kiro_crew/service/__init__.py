"""Service management for the KiroCrew gateway.

Provides ``install``, ``uninstall``, ``status`` for systemd (Linux, preferring
the per-user manager) and launchd (macOS, LaunchAgent under
``~/Library/LaunchAgents/``).

The gateway always runs as the invoking user — never root. On Linux,
sudo is only needed by the system-scope fallback and optional host hardening.

Public entry points used by the CLI:
    install_service()
    uninstall_service()
    service_status()
    is_service_active()
    stop_service()
    restart_service()
"""

from __future__ import annotations

from kiro_crew.service.common import (
    SERVICE_NAME,
    Platform,
    current_platform,
)
from kiro_crew.service.controller import (
    install_service,
    is_service_active,
    restart_service,
    service_status,
    stop_service,
    uninstall_service,
)

__all__ = [
    "Platform",
    "SERVICE_NAME",
    "current_platform",
    "install_service",
    "is_service_active",
    "restart_service",
    "service_status",
    "stop_service",
    "uninstall_service",
]
