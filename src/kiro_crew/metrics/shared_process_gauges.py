"""Register the gauges over processes that several sessions share.

Called once per live recorder build from ``metrics/provider.py::_build_recorder``,
inside the consent gate, beside the process, inventory, crew log and session-tree
sets. Each set has its OWN try, for the reason the sets in the provider do: a
fault in one must not take the other dark, and nothing here may reach the
provider's outer ``except``, which would disable telemetry as a whole.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

#: The provider's logger: these lines were logged under that name before the split.
logger = logging.getLogger("kiro_crew.metrics.provider")


def register_shared_process_gauges(meter: "Meter") -> None:
    """Register the MCP gateway pool and chat runtime sharing gauges on *meter*."""
    # MCP gateway pool gauges observe only in the process that bound a pool
    # (gatewayd); everywhere else the callbacks return before reading anything,
    # so no series is published.
    try:
        from kiro_crew.metrics.mcp_gateway_gauges import register_mcp_gateway_gauges

        register_mcp_gateway_gauges(meter)
    except Exception:
        logger.warning("mcp gateway gauges unavailable", exc_info=True)
    # The chat runtime sharing census observes only once this process has placed
    # or refused a chat with the preview on, so no other process exports zeros.
    try:
        from kiro_crew.metrics.chat_runtime_share import register_chat_runtime_share_gauges

        register_chat_runtime_share_gauges(meter)
    except Exception:
        logger.warning("chat runtime share gauges unavailable", exc_info=True)
