"""The user's record of shipped skills they uninstalled.

A shipped skill (``builtin_skills/`` or the deploy layer's ``deploy/skills/``)
is re-copied into the skills dir by every gateway start whenever its directory
is missing. Deleting one from the dashboard therefore has to leave a durable
record, or the next start reinstalls it and the delete was a no-op.

The record is ``<skills dir>/.uninstalled-builtins.json``: a sorted JSON list of
skill keys. It is dot-prefixed so skill discovery never treats it as a skill,
and it lives beside the skills it governs, so an isolated home (pod, test,
``KIROCREW_HOME``) carries its own record.

Only keys the package actually ships are ever recorded; a user-authored skill
is simply gone when deleted and needs no record. A recorded name the package
does not ship is harmless: nothing would reinstall it anyway.

The record is read leniently (an unreadable or malformed file reads as empty,
so a damaged record can only fail OPEN to the shipped default, never block
startup) and written atomically.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

UNINSTALLED_RECORD = ".uninstalled-builtins.json"

# Serializes read-modify-write of the record within one process. The dashboard
# runs delete/restore in worker threads, so two concurrent requests would
# otherwise lose one of the two updates.
_lock = threading.Lock()


def _record_path(skills_dir: Path) -> Path:
    return skills_dir / UNINSTALLED_RECORD


def read_uninstalled(skills_dir: Path) -> frozenset[str]:
    """Keys of shipped skills the user uninstalled, or empty on any failure."""
    path = _record_path(skills_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError):
        logger.warning("Could not read %s; treating no shipped skill as uninstalled", path)
        return frozenset()
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Malformed %s; treating no shipped skill as uninstalled", path)
        return frozenset()
    if not isinstance(data, list):
        logger.warning("Unexpected shape in %s; treating no shipped skill as uninstalled", path)
        return frozenset()
    return frozenset(item for item in data if isinstance(item, str) and item)


def _write(skills_dir: Path, names: frozenset[str]) -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(_record_path(skills_dir), json.dumps(sorted(names), indent=2) + "\n")


def mark_uninstalled(skills_dir: Path, name: str) -> None:
    """Record *name* so builtin sync stops reinstalling it."""
    with _lock:
        current = read_uninstalled(skills_dir)
        if name in current:
            return
        _write(skills_dir, current | {name})
    logger.info("Recorded shipped skill %s as uninstalled", name)


def unmark_uninstalled(skills_dir: Path, name: str) -> bool:
    """Drop *name* from the record. Returns whether it was recorded."""
    with _lock:
        current = read_uninstalled(skills_dir)
        if name not in current:
            return False
        _write(skills_dir, current - {name})
    logger.info("Cleared uninstalled record for shipped skill %s", name)
    return True
