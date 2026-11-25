"""Resume the chats a planned in-app restart cut off.

The dashboard's ``_restart_gateway`` behind ``POST /api/restart`` and Slack's
``/kirocrew restart`` call :func:`record_running_slots` to record chats with a
running turn just before ``close_all()`` cuts them. The next gateway start,
with or without a dashboard, takes that record once, deleting it before it
acts (:func:`take_intent`), and attempts to resume those chats through the
Resume-all run (``chat_resume_all.start_resume_all``), so its bound on
concurrent turns and every refusal it makes still apply. A crash writes no
record: with a dashboard its chats keep the Resume button and wait for the
owner. Without a valid record, this startup recovery path resumes no chats.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read
from kiro_crew.config.paths import config_dir

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

INTENT_NAME = "resume-after-restart.json"
_INTENT_MAX_BYTES = 256 * 1024
#: A record older than this is not this boot's: a planned restart that never
#: happened left it, and a later crash boot must not resume those chats.
_INTENT_MAX_AGE_SECS = 30 * 60


#: The name ``POST /v1/responses`` gives the session of one request
#: (``crew_api_responses``); a test holds the two spellings together.
ONE_REQUEST_API_PREFIX = "resp-"


def _one_request_api_turn(name: str, slot: object) -> bool:
    """Whether *slot* is the Crew API's session for one ``/v1/responses`` request.

    Its caller is gone once the gateway restarts, so resuming the turn would run
    tools nobody waits for. Both marks are needed: a person's own chat can carry
    the prefix, and other one-request sessions carry only the card mark.
    """
    return name.startswith(ONE_REQUEST_API_PREFIX) and (
        getattr(slot, "_dashboard_card_exempt", False) is True
    )


def intent_path() -> Path:
    return config_dir() / INTENT_NAME


def write_intent(names: list[str]) -> None:
    """Write *names*, or remove an earlier record when no chat is running."""
    if names:
        atomic_write(intent_path(), json.dumps(names), restrict_to_owner=True)
    else:
        intent_path().unlink(missing_ok=True)


async def record_running_slots(state: DashboardState | None) -> None:
    """Record the chats a planned restart is about to cut. Call right before ``close_all()``.

    A slot counts when it carries the turn-in-flight marker, which is what the
    next boot's reconcile turns into the interruption row this record resumes.
    Names are read on the loop, the file is written off it with a 5 s bound,
    and a failure is logged: the restart goes on without the record.
    """
    try:
        slots = list(getattr(state, "_slots", {}).items())
        names = sorted(
            n
            for n, s in slots
            if getattr(s, "_turn_in_flight_generation", 0) > 0 and not _one_request_api_turn(n, s)
        )
        await asyncio.wait_for(asyncio.to_thread(write_intent, names), 5.0)
    except Exception:
        logger.warning("Resume-after-restart record failed", exc_info=True)
    # The other record a planned restart must not cut short: an approval answered
    # just now is still being written. Imported here: that module imports handlers.
    from kiro_crew.dashboard.crew_api_approvals import finish_writes_before_restart

    await finish_writes_before_restart(state)


def take_intent() -> frozenset[str]:
    """Read and delete the record. The delete comes first, so a boot tries once."""
    path = intent_path()
    try:
        with open(path, "rb") as fh:
            age = time.time() - os.fstat(fh.fileno()).st_mtime
            raw = bounded_read(fh, _INTENT_MAX_BYTES + 1)
    except FileNotFoundError:
        return frozenset()
    path.unlink(missing_ok=True)
    if age > _INTENT_MAX_AGE_SECS:
        return frozenset()
    try:
        names = json.loads(raw) if len(raw) <= _INTENT_MAX_BYTES else None
    except ValueError:
        names = None
    if not isinstance(names, list):
        return frozenset()
    return frozenset(name for name in names if isinstance(name, str))


async def resume_after_restart(state: DashboardState) -> None:
    """Resume the recorded chats. Logs every failure and never raises."""
    try:
        names = await asyncio.to_thread(take_intent)
        if not names:
            return
        from kiro_crew.dashboard.chat_resume_all import start_resume_all

        # crew_mode: a Crew Mode worker stays with its conductor, as the button leaves it.
        status, body = start_resume_all(state, {"app": ""}, crew_mode=True, only=names)
        logger.info(
            "Resume after restart: %d chat(s) recorded, %s started (status %d)",
            len(names),
            body.get("total", 0),
            status,
        )
    except Exception:
        logger.warning("Resume after restart failed", exc_info=True)


def schedule_resume_after_restart(state: DashboardState) -> None:
    """Start :func:`resume_after_restart` off the boot path, held in the background set."""
    task = asyncio.create_task(resume_after_restart(state), name="resume-after-restart")
    background = getattr(state, "_background_tasks", None)
    if background is not None:
        background.add(task)
        task.add_done_callback(background.discard)
