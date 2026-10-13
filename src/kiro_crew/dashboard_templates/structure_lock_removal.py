"""The crew delete route's half of a dashboard structure lock's lifecycle.

The person sets and clears the lock from the owner-gated route, through
:mod:`kiro_crew.dashboard_templates.instance`. This module holds
the only other path that removes the lock file: the delete teardown, for a crewmate
the roster does not hold. It sits beside ``instance`` rather than inside it only
to keep that module under the line cap; it reuses the same lock path and guard file.
"""

from __future__ import annotations

import os
from typing import Callable

from kiro_crew.atomic_write import fsync_dir
from kiro_crew.dashboard_templates import instance


def remove_structure_lock(slug: str, *, still_unclaimed: Callable[[], bool]) -> bool:
    """Remove the structure lock of a crewmate the roster does not hold.

    The crew delete route's half of the lock's lifecycle, and the only other path that
    removes :func:`~kiro_crew.dashboard_templates.instance.lock_path`. A lock left
    behind would greet a later crewmate that derives the same slug with a page locked
    to the deleted one's snapshot.

    *still_unclaimed* is asked under the same guard the owner's setter holds, and a
    ``False`` answer removes nothing: a lock still keyed by a live crewmate is that
    person's decision, and clearing it would let its agent change the page. Only the
    lock file is touched -- not the guard file, which another writer may be holding,
    and nothing under ``members/<slug>/``.

    Returns whether a lock file was removed. A crewmate that never locked returns
    without taking the guard, so the delete creates nothing in ``crew-panels``.
    """
    path = instance.lock_path(slug)
    if not os.path.lexists(path):
        return False
    with instance._locked(path.parent, lock_file=f"{slug}{instance._LOCK_GUARD_SUFFIX}"):
        if not still_unclaimed():
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        fsync_dir(path.parent)
    return True
