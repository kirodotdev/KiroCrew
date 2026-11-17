"""A gateway's first session-start skill read, made once before any session asks for it.

The first skill read of a process is slower than every later one. It reads each packaged
skill's ``app:`` line (``skill_runtime.catalog._packaged_app_claims``) and walks the
packaged trees (``skills_selection._packaged_skill_names``), two results kept for the
life of the process, and on a scope with no list in memory it waits for the catalog. A
gateway's first session otherwise pays for all of that inside its prompt build, after
the person's message has arrived.

So the gateway makes that read once, past ``KIROCREW_READY`` and off the event loop,
through the loader call a session start makes (``context_assembly.inclusion.skill_parts``).
Nothing is kept here. The loader holds what it read under its own invalidation, and a
session start reads through it exactly as it does without this.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder

logger = logging.getLogger(__name__)


def first_read(builder: "ContextBuilder") -> None:
    """Make the skill read a default-agent session start with no project makes.

    Blocking: run it on a worker thread. The arguments are the ones
    ``build_session_context`` passes for that session, so the read fills what such
    a session's own read would otherwise fill. What it renders is discarded.
    """
    from kiro_crew import context as ctx
    from kiro_crew.context_assembly import inclusion

    cfg = ctx.KiroCrewConfig.load()
    inclusion.skill_parts(
        builder,
        globs=[],
        project=None,
        caps=ctx._resolve_caps(None),
        lazy_skills=bool(getattr(cfg.skills, "lazy_load", False)),
    )


async def first_read_after_ready(
    builder: "ContextBuilder | None", synced: "asyncio.Future[Any] | None"
) -> None:
    """Run :func:`first_read` on a worker thread once the builtin skill sync is done.

    A gateway with no context builder has no session start to read for. *synced*
    is the gateway's builtin sync task, or ``None`` when it runs none. The
    sync drops the catalog when it changes a skill, so a read made before it ended
    would be made again. ``asyncio.wait`` leaves the sync running when this is
    cancelled and does not raise what the sync raised; a sync that was itself
    cancelled means the gateway is stopping, so nothing is read. A failed read is
    logged and dropped: the first session then makes the read itself, as it does
    without this.
    """
    if builder is None:
        return
    if synced is not None:
        await asyncio.wait({synced})
        if synced.cancelled():
            return
    try:
        await asyncio.to_thread(first_read, builder)
    except Exception:  # noqa: BLE001 - a head start must never take the gateway down
        logger.debug("First skill read ahead of a session failed", exc_info=True)
