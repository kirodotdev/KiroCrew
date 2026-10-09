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
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: Note key marking a notification no agent produced.
SYSTEM_ORIGIN_KEY = "producer_system"


def system_origin(**extra: Any) -> dict[str, Any]:
    """Meta for a note no agent produced, merged with *extra* meta keys."""
    return {SYSTEM_ORIGIN_KEY: "1", **extra}


#: Most distinct session keys one note may name and still be attributed. Every key
#: is looked up for its agent before the bridge vets the note, so this bounds the
#: per-note lookup work an app push could otherwise inflate with a long
#: ``producer_session`` list. A note naming more is refused by the bridge rather
#: than vetted on the first keys alone: an unlooked-up session's agent would never
#: be asked. A real producer names one or two keys; a restart digest names up to
#: three per orphan.
MAX_PRODUCER_KEYS = 32


def producer_session_keys(
    identities: Mapping[str, Any], *, limit: int = MAX_PRODUCER_KEYS
) -> list[str]:
    """The distinct session keys *identities* names, collecting at most ``limit + 1``.

    ``session_key``, ``caller``, the slot (qualified as ``dashboard:<slot>`` when
    bare) and each newline-separated ``producer_session``, in that order. Collection
    stops one past *limit*, so a caller can tell an overflowing note from a full one
    without enumerating an inflated list.
    """
    keys: list[str] = []

    def _add(value: object) -> bool:
        if isinstance(value, str):
            value = value.strip()
            if value and value not in keys:
                keys.append(value)
        return len(keys) > limit

    for name in ("session_key", "caller"):
        if _add(identities.get(name, "")):
            return keys
    slot = identities.get("slot", "")
    if isinstance(slot, str) and slot.strip():
        if _add(slot if ":" in slot else f"dashboard:{slot}"):
            return keys
    producer = identities.get("producer_session", "")
    if isinstance(producer, str):
        for part in producer.split("\n"):
            if _add(part):
                return keys
    return keys


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
    return default_agent_names_from(config)


def default_agent_names_from(config: object) -> list[str]:
    """The default-agent names derived from an ALREADY-LOADED *config*.

    The names-only tail of :func:`default_agent_names`, split out so a caller
    holding a config in memory (the gateway's ``self._cfg``) resolves the default
    without a fresh ``KiroCrewConfig.load()``. ``load()`` can read and validate
    ``config.json`` from disk on a cache miss, so calling it from the event loop
    blocks delivery; a caller on the loop passes its own config here instead.
    """
    names: list[str] = []
    default = getattr(config, "default_agent", "")
    entry = getattr(config, "agents", {}).get(default) if isinstance(default, str) else None
    for candidate in (default, getattr(entry, "kiro_agent", "")):
        name = candidate.strip() if isinstance(candidate, str) else ""
        if name and name not in names:
            names.append(name)
    return names


__all__ = [
    "MAX_PRODUCER_KEYS",
    "SYSTEM_ORIGIN_KEY",
    "default_agent_names",
    "default_agent_names_from",
    "producer_session_keys",
    "system_origin",
]
