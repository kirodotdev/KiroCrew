"""The ``chat_runtime_sharing`` feature preview, through the agent-backend boundary.

The readers are in :mod:`kiro_crew.acp.chat_runtime_sharing`. Application code (the
dashboard's live-config applier, the inventory gauges, the gateway's tree-root
wiring) reaches them here, so the boundary gate sees one surface and there is no
second spelling of the preview id or the edition lock. The ACP module is imported
on first call, so importing this one costs nothing.
"""

from __future__ import annotations

from typing import Any, Callable


def chat_runtime_sharing_enabled(cfg: Any) -> bool:
    """Whether the preview is on in *cfg*; an LTS build reads it as off."""
    from kiro_crew.acp.chat_runtime_sharing import chat_runtime_sharing_enabled as enabled

    return enabled(cfg)


def set_share_tree_reader(reader: Callable[[str], str] | None) -> None:
    """Install the gateway's session-key -> tree-root reader (``None`` removes it)."""
    from kiro_crew.acp.chat_runtime_sharing import set_share_tree_reader as install

    install(reader)


__all__ = ["chat_runtime_sharing_enabled", "set_share_tree_reader"]
