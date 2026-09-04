"""Local notification bus.

Producers push structured :class:`NotificationPayload` objects through
:class:`NotificationBus` behind the ``DashboardState.notify()`` API; the bus
validates, resolves the channel, applies the channel's default priority, and
hands the enriched note dict to a delivery sink (owned by ``DashboardState``).
"""

from kiro_crew.notifications.bus import (
    SYSTEM_CHANNELS,
    NotificationBus,
    NotificationPayload,
    normalize_note,
    payload_from_legacy,
)

__all__ = [
    "NotificationBus",
    "NotificationPayload",
    "SYSTEM_CHANNELS",
    "normalize_note",
    "payload_from_legacy",
]
