"""Cron folder definitions: resolving a job's ``folder_id`` against ``cron_folders.json``.

The file is OWNED by the dashboard, whose Schedule page creates, renames and
deletes folders and rewrites the list wholesale. Nothing here writes it; a
non-dashboard caller resolves a folder reference read-only, and an unreadable
store is reported as unknown rather than empty.
"""

from __future__ import annotations

import json
import logging
from typing import Any, NamedTuple

logger = logging.getLogger("kiro_crew.cron")

_CRON_FOLDERS_FILE = "cron_folders.json"


def _read_cron_folders() -> tuple[list[dict[str, Any]], bool]:
    """Read ``cron_folders.json``, reporting whether the STORE was readable.

    Returns ``(folders, readable)``. ``readable=False`` means the file exists
    but could not be read or is not a JSON list — the folder set is UNKNOWN,
    not empty. A caller that can create a folder must tell those apart: acting
    on an unknown set as if it were empty creates a folder the store may
    already hold, and the dashboard's next wholesale save of its own list
    decides which version survives. A file that does not exist is readable and
    genuinely empty; malformed ENTRIES inside a valid list are filtered out and
    leave the store readable, because the list itself was intelligible.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    path = seams.config_dir() / _CRON_FOLDERS_FILE
    try:
        if not path.exists():
            return [], True
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read %s", _CRON_FOLDERS_FILE, exc_info=True)
        return [], False
    if not isinstance(loaded, list):
        logger.warning("%s is not a JSON list", _CRON_FOLDERS_FILE)
        return [], False
    return [
        f
        for f in loaded
        if isinstance(f, dict)
        and isinstance(f.get("id"), str)
        and f.get("id")
        and isinstance(f.get("name"), str)
        and f.get("name")
    ], True


def load_cron_folders() -> list[dict[str, Any]]:
    """Read the cron folder definitions from disk (read-only).

    Returns the usable entries of ``cron_folders.json`` — dicts with a
    non-empty string ``id`` and ``name``. The file is OWNED by the dashboard
    (its Schedule page creates, renames and deletes folders); this helper never
    writes, so a non-dashboard caller can resolve a folder reference without
    racing the dashboard's wholesale rewrites of the file. Malformed entries
    and unreadable files degrade to "no folders" rather than raising: a folder
    lookup is always best-effort decoration on top of the job itself. A caller
    that CREATES folders must use ``_read_cron_folders`` instead, whose second
    element separates an unreadable store from a genuinely empty one.
    """
    return _read_cron_folders()[0]


class CronFolderLookup(NamedTuple):
    """Outcome of resolving a cron-folder reference against existing folders.

    ``missing`` distinguishes the three outcomes a caller must treat
    differently. A reference that matched nothing in a READABLE store
    (``missing=True``) may legitimately be turned into a create by a caller
    that owns a create path. Every other error is a refusal no caller may paper
    over: an ambiguous name, and — the case that is easy to miss — a store that
    could not be read at all, where the folder set is unknown rather than
    empty. Without that flag the only signal is the message text, and matching
    on prose is how a create leg silently starts firing on an ambiguity or on a
    corrupt file.
    """

    folder_id: str
    error: str | None
    missing: bool = False


def lookup_cron_folder_id(ref: str) -> CronFolderLookup:
    """Resolve an EXISTING cron folder reference (id or name) to its id.

    An empty ``ref`` resolves to ``""`` (ungrouped) with no error. Matching
    order: exact id first, then case-insensitive name. A name shared by several
    folders is refused rather than resolved to an arbitrary one, and an unknown
    reference is an error with ``missing=True`` — creating folders is the
    dashboard's job (its state holds the canonical in-memory list and rewrites
    the file wholesale, so an out-of-band append here could be silently
    clobbered by the next UI folder operation). A caller that DOES own a
    server-side create path (the MCP tool, via the dashboard's own endpoint)
    keys off ``missing`` to take it.

    An UNREADABLE store is an error with ``missing=False``: the folder set is
    unknown, so the reference may well exist, and creating it would add a
    duplicate whose survival is decided by the dashboard's next save.
    """
    ref = str(ref or "").strip()
    if not ref:
        return CronFolderLookup("", None)
    folders, readable = _read_cron_folders()
    if not readable:
        return CronFolderLookup(
            "",
            f"cron folder store is unreadable, cannot resolve {ref!r} — "
            f"repair or remove {_CRON_FOLDERS_FILE}",
        )
    if any(f["id"] == ref for f in folders):
        return CronFolderLookup(ref, None)
    matches = [f for f in folders if f["name"].strip().lower() == ref.lower()]
    if len(matches) > 1:
        ids = ", ".join(f["id"] for f in matches)
        return CronFolderLookup(
            "", f"{len(matches)} cron folders are named {ref!r} ({ids}) — pass the folder id"
        )
    if matches:
        return CronFolderLookup(matches[0]["id"], None)
    return CronFolderLookup(
        "",
        f"cron folder not found: {ref!r} — create it first in the dashboard's Schedule page",
        missing=True,
    )
