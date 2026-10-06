"""Who produced a notification, for the bridge's fail-closed attribution rule.

The bridge egresses a note only when it can name every governance profile the
producer answers to. A note produced by an agent (a conversation, a task run, a
subagent, a cron job's agent turn) must name that agent; one that cannot is refused
rather than vetted under the host and surface profiles alone. A note no agent
produced -- an update notice, a config reload, a resource-pressure warning -- says so
explicitly with :func:`system_origin`, and only such a note may pass on the host
profile alone.

The tag is honoured only on ``source == "system"`` notes. Every route that builds a
note from a request body forces its own source (``app:<name>`` for an app push) or
builds the meta server-side (the agent push), so the tag cannot be supplied by a
caller that is not the gateway itself.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Note key marking a notification no agent produced.
SYSTEM_ORIGIN_KEY = "producer_system"


def system_origin(**extra: Any) -> dict[str, Any]:
    """Meta for a note no agent produced, merged with *extra* meta keys."""
    return {SYSTEM_ORIGIN_KEY: "1", **extra}


def default_agent_names() -> list[str]:
    """The agent a session runs as when nothing more specific selected one.

    What :func:`kiro_crew.session_agent_selection.resolve_session_agent_bindings`
    falls back to (``config.default_agent``), plus the provider template that entry
    names, since a task-bound profile may be bound to either. Empty when the config
    cannot be read, which leaves the note unattributed and therefore refused.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 - an unreadable config attributes nothing
        logger.debug("default agent lookup failed", exc_info=True)
        return []
    names: list[str] = []
    default = getattr(config, "default_agent", "")
    entry = getattr(config, "agents", {}).get(default) if isinstance(default, str) else None
    for candidate in (default, getattr(entry, "kiro_agent", "")):
        name = candidate.strip() if isinstance(candidate, str) else ""
        if name and name not in names:
            names.append(name)
    return names


__all__ = ["SYSTEM_ORIGIN_KEY", "default_agent_names", "system_origin"]
