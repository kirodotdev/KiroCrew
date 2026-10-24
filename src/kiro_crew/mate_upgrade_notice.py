"""The one notice a crewmate moved onto its worker at upgrade gives its user.

The migration marks it owed; ``mate_thread_wakes`` delivers it once, after an
``O_EXCL`` marker claim. A crewmate the migration left where it was owes its
own notice, naming why, through the same claim. Blocking file I/O.
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

#: Written by the migration for each crewmate it left where it was, with why.
KEPT_OWED_FILENAME = "worker_kept_owed.json"
#: Claimed once, right before that notice is delivered.
KEPT_CLAIMED_FILENAME = "worker_kept_claimed.json"


def kept_notice_prompt(reason: str) -> str:
    """The hidden turn that tells a crewmate's user why it was not moved."""
    return WORKER_UPGRADE_PREFIX + crewmate_prompts.fill("upgrade_notice_kept", reason=reason)


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


def mark_kept_owed(name: str, reason: str) -> None:
    """Record that crewmate *name*, left where it was, owes its user *reason*. Never raises."""
    try:
        path = _dir(name) / KEPT_OWED_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        from kiro_crew.atomic_write import atomic_write

        record = {"member": name, "reason": reason}
        atomic_write(path, json.dumps(record) + "\n", restrict_to_owner=True)
    except Exception:  # noqa: BLE001 - no notice is the safe direction
        logger.warning("could not record the kept notice owed by %r", name, exc_info=True)


def clear_kept_owed(name: str) -> None:
    """Drop crewmate *name*'s unclaimed kept notice: it has moved. Never raises."""
    try:
        (_dir(name) / KEPT_OWED_FILENAME).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - the moved-notice guard in claim_kept_notice still holds
        logger.warning("could not clear the kept notice owed by %r", name, exc_info=True)


def _claim(slug: str, member: str, owed: str, claimed: str) -> dict | None:
    """The owed record, claimed once; ``None`` when not owed or already claimed.

    The owed record must name this member. ``O_CREAT | O_EXCL`` on the claim
    marker is the guard: atomic across tasks and processes.
    """
    try:
        directory = members_mod.member_dir(slug)
        record = json.loads((directory / owed).read_text(encoding="utf-8"))
    except (OSError, ValueError, members_mod.MemberSlugError):
        return None
    if not isinstance(record, dict) or record.get("member") != member:
        return None
    try:
        fd = os.open(directory / claimed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    except OSError:
        logger.warning("could not claim the upgrade notice for %r", member, exc_info=True)
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"member": member, "ts": time.time()}, fh)
    return record


def claim_notice(slug: str, member: str) -> bool:
    """Claim *member*'s owed notice once. True exactly once, and only when owed."""
    return _claim(slug, member, NOTICE_OWED_FILENAME, NOTICE_CLAIMED_FILENAME) is not None


def claim_kept_notice(slug: str, member: str) -> str:
    """Claim *member*'s kept notice once: its prompt, or ``""`` when none is owed.

    Never once the crewmate has moved (its moved notice is owed or claimed),
    even if a kept record from an earlier start was left behind.
    """
    try:
        directory = members_mod.member_dir(slug)
    except members_mod.MemberSlugError:
        return ""
    if (directory / NOTICE_OWED_FILENAME).exists() or (
        directory / NOTICE_CLAIMED_FILENAME
    ).exists():
        return ""
    record = _claim(slug, member, KEPT_OWED_FILENAME, KEPT_CLAIMED_FILENAME)
    reason = record.get("reason") if record else None
    return kept_notice_prompt(reason) if isinstance(reason, str) and reason else ""
