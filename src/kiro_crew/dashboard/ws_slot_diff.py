"""``/api/ws`` handshake and inbound handling for the ``slot_diff.1`` capability.

The sender lives in ``state_parts/slots_diff.py``; this module holds the parts
``ws.py`` runs per socket: the operator's capability kill switch, the one-time
note that a dashboard tab connected without the capability, and the
``slots_resync`` handler.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.dashboard import websocket_hub as _ws_hub

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Environment variable naming ``?caps=`` capabilities this gateway ignores,
#: comma-separated (``KIROCREW_WS_DISABLED_CAPS=slot_diff.1``). The rollback lever
#: for a capability that misbehaves: a tab that declares it is served as if it
#: had not, with no new bundle. Read on every connect, so a restart applies it.
DISABLED_CAPS_ENV = "KIROCREW_WS_DISABLED_CAPS"

#: The capabilities whose absence keeps a dashboard tab on the full slot list.
_SCALE_CAPS = (_ws_hub.SLOT_DIFF_CAPABILITY,)

_missing_caps_logged = False


def without_disabled_caps(declared: set[str]) -> set[str]:
    """*declared* minus the capabilities :data:`DISABLED_CAPS_ENV` names."""
    raw = os.environ.get(DISABLED_CAPS_ENV, "")
    disabled = {cap.strip() for cap in raw.split(",") if cap.strip()}
    return declared - disabled if disabled else declared


def note_missing_caps(dashboard_user: bool, declared: Collection[str]) -> None:
    """Log once per process when a dashboard-user socket lacks a scale capability.

    Such a socket (a companion window, an older bundle) is a generic slots
    audience, so every coalesced push still serializes and sends it the full
    list. One line says that case exists on this install; repeating it per
    connect would only add noise.
    """
    global _missing_caps_logged
    if _missing_caps_logged or not dashboard_user:
        return
    missing = [cap for cap in _SCALE_CAPS if cap not in declared]
    if not missing:
        return
    _missing_caps_logged = True
    logger.info(
        "ws: a dashboard socket connected without %s; it gets the full slot list on "
        "every push (logged once per process)",
        ",".join(missing),
    )


def handle_slots_resync(state: "DashboardState", ws: web.WebSocketResponse) -> None:
    """A diff socket missed its base: send it the list its view last received.

    Ignored from any other socket, so a legacy tab or an app token cannot use
    the message to trigger a broadcast.
    """
    if ws.get(_ws_hub.SLOT_DIFF_WS_FLAG) is not True:
        return
    if ws.get("_is_dashboard_user") is not True:
        return
    state.answer_slots_resync(ws)
