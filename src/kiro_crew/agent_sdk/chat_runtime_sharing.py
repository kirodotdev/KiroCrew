"""The ``chat_runtime_sharing`` feature preview, through the agent-backend boundary.

The readers are in :mod:`kiro_crew.acp.chat_runtime_sharing`. Application code (the
dashboard's live-config applier, the inventory gauges, the gateway's tree-root
wiring, the scratch resume's tree check) reaches them here, so the boundary gate
sees one surface and there is no second spelling of the preview id or the edition
lock. The ACP module is imported on first call, so importing this one costs
nothing.
"""

from __future__ import annotations

from typing import Any, Callable


def chat_runtime_sharing_enabled(cfg: Any) -> bool:
    """Whether the preview is on in *cfg*; an LTS build reads it as off."""
    from kiro_crew.acp.chat_runtime_sharing import chat_runtime_sharing_enabled as enabled

    return enabled(cfg)


def set_share_tree_placement_reader(reader: Callable[[str], tuple[str, bool]] | None) -> None:
    """Install the gateway's session-key -> (root, verified) reader (``None`` removes it)."""
    from kiro_crew.acp.chat_runtime_sharing import set_share_tree_placement_reader as install

    install(reader)


def share_tree_placement_of(session_key: str) -> tuple[str, bool]:
    """``(root, verified)`` for *session_key*'s session tree."""
    from kiro_crew.acp.chat_runtime_sharing import share_tree_placement_of as placement_of

    return placement_of(session_key)


__all__ = [
    "chat_runtime_sharing_enabled",
    "set_share_tree_placement_reader",
    "share_tree_placement_of",
]
