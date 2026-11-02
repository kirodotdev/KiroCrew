"""Destroying a deleted crewmate's pinned-thread sessions, one step of the crew delete."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


async def destroy_member_thread_sessions(
    sessions: Any, name: str, cfg: Any, *, claimed: Callable[[str], bool]
) -> list[str] | None:
    """Destroy a deleted crewmate's pinned-thread sessions. Never raises.

    The thread is keyed ``dashboard:member-<slug>`` or, on a private store,
    ``dashboard:member-<slug>.memory-<store>`` in ``session_map.json``. Nothing
    else clears that entry: a Fresh start keeps the key, a restart restores it,
    and member threads are not listed where a session can be deleted.

    Each key goes through ``sessions.destroy``, which ends a live process for the
    thread and deletes its map entry in one step. The slug is resolved from
    *cfg*, the config captured while the record still existed, and nothing is
    destroyed while *claimed* answers that a live crew still derives it. The V1
    key and the current store's key are named outright, so an entry a Fresh
    start left without a session ID is reached; earlier generations are found
    by the exact ``.memory-`` boundary, which keeps a sibling whose slug only
    shares a prefix out.

    Returns the keys destroyed, or ``None`` when a step failed.
    """
    try:
        from kiro_crew.members import (
            MEMORY_STORE_SLOT_SUFFIX,
            member_slug,
            member_thread_session_alias,
        )

        slug = member_slug(name, cfg)
        if await asyncio.to_thread(claimed, slug):
            return []
        smap = sessions._session_map
        exact = member_thread_session_alias(slug)
        keys = [exact]
        store = getattr(cfg.agents.get(name), "memory_store", "")
        if isinstance(store, str) and store and store != "default":
            keys.append(member_thread_session_alias(slug, store))
        prefix = exact + MEMORY_STORE_SLOT_SUFFIX
        keys += sorted(
            key for key in smap.mapped_sids_by_key() if key.startswith(prefix) and key not in keys
        )
        held = [key for key in keys if smap.has_hint(key)]
        for key in held:
            await sessions.destroy(key)
        return held
    except Exception:
        logger.warning(
            "crew delete: could not remove the thread sessions of %r", name, exc_info=True
        )
        return None
