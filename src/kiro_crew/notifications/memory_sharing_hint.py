"""The "Reduce memory use" button on the host-memory notes, when a sharing switch is off.

Two switches cut what each chat costs in memory: MCP sharing (``mcp_gateway.enabled``:
chats with the same server configuration share one MCP server process) and ACP runtime
sharing (the ``chat_runtime_sharing`` Feature Preview: several chats share one agent
process). The tight and critical notes in :mod:`resource_pressure` ask the user to free
memory; while one of these is off, they also carry one button that opens its control.
The button's target names the switch: the notes add no body line for it, because the
banner card shows only the first two lines of a body and would cut it off.

A button only ever opens a switch that can be turned on there: a pointer at a
disabled control is a dead end. MCP sharing is offered first, because a chat starts one
process per MCP server it has, so on a host with many servers it is the larger saving;
it is offered only while the page's switch can be turned on
(``mcp_gateway_routes.mcp_sharing_can_turn_on``: the broker runs on this host and at
least one server is marked or eligible to share). Otherwise, or once MCP sharing is on,
the note offers ACP runtime sharing. A build that locks Feature Previews (LTS) has no
ACP switch to open, so it is never offered there. A config that cannot be read offers
nothing. Reading the MCP side scans the agent specs, so the notifier calls this off the
event loop (``ResourcePressureNotifier.refresh_sharing_hint``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ACTION_ID = "reduce_memory"
ACTION_LABEL = "Reduce memory use"
#: The MCP Servers page on its Sharing view, where the page-wide sharing switch is.
MCP_SHARING_URL = "/mcp?view=sharing"
#: Settings > Developer > Feature Previews, where ACP runtime sharing is switched on.
ACP_SHARING_URL = "/settings/developer/feature-previews"


@dataclass(frozen=True)
class SharingHint:
    """The one action a note carries: the control it opens."""

    url: str

    def actions(self) -> list[dict[str, str]]:
        return [{"id": ACTION_ID, "label": ACTION_LABEL, "url": self.url}]


def _load_config() -> Any:
    """The live config the gateway adopted, else a fresh load."""
    from kiro_crew.config import live

    cfg = live.snapshot()
    if cfg is None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
    return cfg


def _mcp_sharing_can_turn_on() -> bool:
    from kiro_crew.dashboard.handlers.mcp import mcp_sharing_can_turn_on

    return mcp_sharing_can_turn_on()


def sharing_hint(cfg: Any = None) -> SharingHint | None:
    """The hint for the first sharing switch that is off and can be turned on, or ``None``.

    BLOCKING (scans the agent specs for the MCP side). Never raises.
    """
    try:
        if cfg is None:
            cfg = _load_config()
        mcp_on = getattr(getattr(cfg, "mcp_gateway", None), "enabled", False) is True
        if not mcp_on and _mcp_sharing_can_turn_on():
            return SharingHint(MCP_SHARING_URL)
        from kiro_crew.agent_sdk.chat_runtime_sharing import chat_runtime_sharing_enabled
        from kiro_crew.edition_locks import local_enable_locked

        if not local_enable_locked("feature_previews") and not chat_runtime_sharing_enabled(cfg):
            return SharingHint(ACP_SHARING_URL)
    except Exception:  # noqa: BLE001 - a note must still go out without its button
        logger.debug("memory sharing hint: config unreadable", exc_info=True)
    return None
