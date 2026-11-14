"""Whether the ``chat_runtime_sharing`` feature preview is on, through the agent-backend boundary.

The one reader is :func:`kiro_crew.acp.chat_runtime_sharing.chat_runtime_sharing_enabled`.
Application code (the dashboard's live-config applier, the inventory gauges) reaches
it here, so the boundary gate sees one surface and there is no second spelling of
the preview id or the edition lock. The ACP module is imported on first call, so
importing this one costs nothing.
"""

from __future__ import annotations

from typing import Any


def chat_runtime_sharing_enabled(cfg: Any) -> bool:
    """Whether the preview is on in *cfg*; an LTS build reads it as off."""
    from kiro_crew.acp.chat_runtime_sharing import chat_runtime_sharing_enabled as enabled

    return enabled(cfg)


__all__ = ["chat_runtime_sharing_enabled"]
