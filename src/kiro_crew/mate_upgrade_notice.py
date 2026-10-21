"""The one notice a crewmate moved onto its worker at upgrade gives its user.

The startup migration (:mod:`kiro_crew.mate_config_migration`) records the
notice as owed in the crewmate's directory (``members/<slug>/``) right after it
repoints the binding, so only a crewmate it moved owes one: a crewmate created
after the upgrade never does. The dashboard delivers it as one hidden turn in
the crewmate's own thread (:mod:`kiro_crew.dashboard.mate_thread_wakes`), after
claiming a once-only marker with ``O_EXCL``, so a reload, a second tab or a
later start can never post it twice. Blocking file I/O; callers run it off the
event loop. No module state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from kiro_crew import crewmate_prompts
from kiro_crew import members as members_mod

logger = logging.getLogger(__name__)

#: Written by the migration for each crewmate it moved.
NOTICE_OWED_FILENAME = "worker_notice_owed.json"
#: Claimed once, right before the notice is delivered.
NOTICE_CLAIMED_FILENAME = "worker_notice_claimed.json"

#: The envelope the hidden notice turn opens with.
WORKER_UPGRADE_PREFIX = "[Worker upgrade]"

NOTICE_PROMPT = WORKER_UPGRADE_PREFIX + crewmate_prompts.text("upgrade_notice")


def _dir(name: str) -> Path:
    return members_mod.member_dir(members_mod.member_slug(name))


def mark_notice_owed(name: str) -> None:
    """Record that crewmate *name* owes its upgrade notice. Never raises."""
    try:
        path = _dir(name) / NOTICE_OWED_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        from kiro_crew.atomic_write import atomic_write

        atomic_write(path, json.dumps({"member": name}) + "\n", restrict_to_owner=True)
    except Exception:  # noqa: BLE001 - no notice is the safe direction
        logger.warning("could not record the upgrade notice owed by %r", name, exc_info=True)


def claim_notice(slug: str, member: str) -> bool:
    """Claim *member*'s owed notice once. True exactly once, and only when owed.

    The owed record must name this member. ``O_CREAT | O_EXCL`` on the claim
    marker is the guard: atomic across tasks and processes.
    """
    try:
        directory = members_mod.member_dir(slug)
        record = json.loads((directory / NOTICE_OWED_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, members_mod.MemberSlugError):
        return False
    if not isinstance(record, dict) or record.get("member") != member:
        return False
    try:
        fd = os.open(
            directory / NOTICE_CLAIMED_FILENAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        return False
    except OSError:
        logger.warning("could not claim the upgrade notice for %r", member, exc_info=True)
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"member": member, "ts": time.time()}, fh)
    return True
