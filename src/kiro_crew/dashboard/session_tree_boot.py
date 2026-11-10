"""Fold the crew log's session tree in the background while the dashboard boots.

A restored tab's ``created_by`` is a claim the restore checks against the crew log's
creation records. Folding those records can take tens of seconds on a large home, and
no restored tab needs to wait for it: a claim the tree cannot confirm yet is restored
as ``UNVERIFIED_CREATOR`` (fenced, matching no caller), and :func:`confirm_creators`
upgrades it once the fold lands. Until then every reader of the tree answers "no
creator known", which refuses rather than allows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def confirm_creator(slot: object) -> bool:
    """Confirm *slot*'s restored claim if the folded tree backs it. No I/O.

    Only a slot the restore marked as waiting on the fold is upgraded; a History reopen
    fences with the same sentinel and stays fenced. The test the restore applies (the line's claim equals the creation records'
    creator), so a claim the records do not back stays fenced. Loop-affine.
    """
    from kiro_crew.dashboard.session_control_parts.session_revive import (
        UNVERIFIED_CREATOR,
        _creation_parent,
    )

    claimed = str(getattr(slot, "_saved_created_by", "") or "")
    if (
        claimed
        and getattr(slot, "_creator_pending_fold", False)
        and getattr(slot, "_created_by", "") == UNVERIFIED_CREATOR
        and _creation_parent(str(getattr(slot, "key", ""))) == claimed
    ):
        slot._created_by = claimed  # type: ignore[attr-defined]
        slot._creator_pending_fold = False  # type: ignore[attr-defined]
        return True
    return False


def confirm_creators(state: "DashboardState") -> int:
    """Confirm every restored claim the folded tree now backs; return how many."""
    return sum(confirm_creator(slot) for slot in list(state._slots.values()))


async def seed_and_confirm(state: "DashboardState") -> None:
    """Fold the tree off the loop, then confirm the restored claims. Never raises.

    shortcut: a fold that fails is retried only by the projection's own cooldown
    on a later read; claims stay fenced until a restart folds cleanly.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.enabled():
            return
        from kiro_crew.crew_log.session_tree_projection import projection

        await asyncio.to_thread(projection().ensure_seeded)
        confirm_creators(state)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("session tree could not be folded in the background", exc_info=True)


def start_background_seed(state: "DashboardState") -> None:
    """Start :func:`seed_and_confirm` as a tracked background task."""
    task = asyncio.create_task(seed_and_confirm(state))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
