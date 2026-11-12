"""The skills loaders alive in this process, so a change made outside them reaches them.

A loader serves its skill list from memory for ``_ITER_CACHE_TTL_SECS`` and from a
stored snapshot after that, and only its own mutators clear either. A writer that
changes a skills root without going through a loader (the app bridge linking or
unlinking an app's skills) calls :func:`invalidate_loaders_of`, and every live loader
of that root drops what it holds, exactly as its own create or delete would.

Loaders are held weakly: tracking one never keeps it alive.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.skills import SkillsLoader

logger = logging.getLogger("kiro_crew.skills")

_LIVE: weakref.WeakSet[SkillsLoader] = weakref.WeakSet()
_LIVE_LOCK = threading.Lock()


def track(loader: SkillsLoader) -> None:
    """Remember *loader* until it is closed or collected."""
    with _LIVE_LOCK:
        _LIVE.add(loader)


def invalidate_loaders_of(skills_root: Path) -> int:
    """Drop the cached catalog of every open loader reading *skills_root*.

    Returns how many were invalidated. Blocking: an invalidation drops the stored
    snapshot, which waits on the index's write lock, so call it off the event loop.
    One loader's failure is logged and does not stop the others.
    """
    target = os.path.realpath(skills_root)
    with _LIVE_LOCK:
        loaders = list(_LIVE)
    invalidated = 0
    for loader in loaders:
        if loader._closed or os.path.realpath(loader._dir) != target:
            continue
        try:
            loader._invalidate_iter_cache()
        except Exception:  # noqa: BLE001 - one loader must not stop the others
            logger.warning("Could not invalidate a skill catalog of %s", target, exc_info=True)
            continue
        invalidated += 1
    return invalidated
