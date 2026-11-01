"""The ``.app-skill-links.json`` sidecar that names the owning app of each app-skill link.

An app skill is linked into the skills root as a symlink (a junction on non-admin
Windows), so unlike a built-in or managed skill it has no directory of its own to carry
a provenance dotfile: the directory the link points at is the app's own tree (the
installed package for a built-in). ``bridges._owning_app`` resolves the owner at runtime,
but that needs a running gateway. This sidecar writes the same fact down where a plain
script can read it offline, next to the other provenance markers
(``.builtin-skill-provenance``, ``.kirocrew-managed``).

The file lives at ``<skills root>/.app-skill-links.json``. Discovery skips dot-entries, so
it never reads as a skill. Its content is a format version and, per registered link (path
relative to the skills root, ``/``-separated), the owning app id::

    {"format": 1,
     "links": {"dev-fleet/pod-e2e": {"app": "dev-fleet"}, "pod-e2e": {"app": "dev-fleet"}}}

It is ADDITIVE provenance: nothing reads it to load, list or show a skill. A missing,
unreadable or unknown-format file never blocks registration — it is logged and rebuilt by
the next ``reconcile_app_skills``.

Part of the ``kiro_crew.apps.bridges`` family: ``bridges_skills`` calls these on every
register / deregister / reconcile so the file tracks the links on disk. This module never
imports the facade.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger("kiro_crew.apps.bridges")

#: Serializes the read-modify-write in ``set_owners`` and the scan-and-write in
#: a rebuild, so two app lifecycle operations on different per-app locks
#: (enabling A while disabling B, each on its own worker thread) cannot each read
#: the same snapshot and have the later write drop the other's change. Atomic
#: replacement makes each WRITE whole; this makes the read-modify-write a
#: transaction. One process-wide lock is enough: the sidecar is a single file and
#: these writes are infrequent (register / deregister / boot reconcile), so there
#: is no contention worth sharding per root.
_write_lock = threading.Lock()

#: The sidecar's name in the skills root. Dot-prefixed so skill discovery
#: (``_skippable_skill_dir``) never descends or reads it.
LINKS_FILENAME = ".app-skill-links.json"

#: Content format version. A file in any other format is treated as absent and
#: rebuilt, rather than parsed on a guess.
LINKS_FORMAT = 1


def _links_path(skills_root: Path) -> Path:
    return skills_root / LINKS_FILENAME


def _parse(raw: str, path: Path) -> dict[str, str]:
    """Owners parsed from the sidecar's *raw* text; ``{}`` if it is not usable.

    Malformed JSON, a non-object, or an unknown ``format`` yields ``{}`` — those
    are "the file does not describe owners in a form I understand", which the
    next rebuild replaces. Entries whose shape is not ``"<path>": {"app": "<str>"}``
    are dropped individually.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("Ignoring malformed %s: %s", path, exc)
        return {}
    if not isinstance(data, dict) or data.get("format") != LINKS_FORMAT:
        logger.warning(
            "Ignoring %s: unexpected format %r",
            path,
            data.get("format") if isinstance(data, dict) else None,
        )
        return {}
    links = data.get("links")
    if not isinstance(links, dict):
        return {}
    owners: dict[str, str] = {}
    for rel, entry in links.items():
        if isinstance(rel, str) and isinstance(entry, dict):
            app = entry.get("app")
            if isinstance(app, str) and app:
                owners[rel] = app
    return owners


def read_links(skills_root: Path) -> dict[str, str]:
    """Return ``{relative link path: owning app id}`` from the sidecar, or ``{}``.

    Never raises — this is the OFFLINE READER's entry point (a backup or sync
    script, a display). A missing, unreadable, non-JSON or unknown-format file
    yields an empty map so the reader degrades to "no recorded owners". It must
    NOT be used as the base of a read-modify-write: a transient read error here
    reads as empty, and a mutation that merged onto it would publish that
    emptiness over the real entries. Writers use :func:`_read_for_update`.
    """
    path = _links_path(skills_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError (a UTF-16 or otherwise non-UTF-8 file) is a
        # ValueError, not an OSError, so it is caught here on the READ rather
        # than left to escape.
        logger.warning("Could not read %s: %s", path, exc)
        return {}
    return _parse(raw, path)


def _read_for_update(skills_root: Path) -> dict[str, str]:
    """Read the sidecar as the BASE of a read-modify-write — strictly.

    Only a MISSING file reads as empty. Any other read failure (a transient
    ``EACCES`` / ``EIO``, a non-UTF-8 file) is raised, so the caller abandons the
    mutation rather than merging onto an empty base and overwriting the real
    entries with the result. A merge is safe to abandon: the file is additive
    provenance, so the next rebuild restores it from the links on disk.

    A file that is present and readable but malformed or of an unknown format
    still parses to ``{}`` (via :func:`_parse`): there are no trustworthy entries
    to preserve, so merging onto empty loses nothing the file still vouched for.
    """
    path = _links_path(skills_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return _parse(raw, path)


def write_links(skills_root: Path, owners: dict[str, str]) -> None:
    """Write *owners* to the sidecar atomically, sorted for a stable diff.

    Never raises: a write that cannot land is logged and dropped, because this
    file is provenance, not a load-bearing part of registration. The skills root
    is created if the write is the first thing to touch it.
    """
    path = _links_path(skills_root)
    payload = {
        "format": LINKS_FORMAT,
        "links": {rel: {"app": owners[rel]} for rel in sorted(owners)},
    }
    try:
        skills_root.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(payload, indent=2) + "\n")
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


def set_owners(skills_root: Path, new_owners: dict[str, str]) -> None:
    """Merge *new_owners* onto the sidecar and persist.

    Used by registration: a later ``app`` for the same relative path wins, which
    is how a flat link that is repointed to a second app takes that app as owner.
    The read-merge-write runs under :data:`_write_lock` so a concurrent rebuild
    (another app's register / deregister) cannot read the same snapshot and
    overwrite this merge. Deregistration does not drop entries by name; it
    rebuilds the whole file from the links on disk (see
    ``bridges_skills._deregister_skills``), which cannot erase an entry whose
    link is still live.
    """
    if not new_owners:
        return
    with _write_lock:
        try:
            owners = _read_for_update(skills_root)
        except (OSError, UnicodeDecodeError) as exc:
            # The existing file could not be read for a reason other than
            # "missing". Abandon the merge rather than overwrite real entries
            # with just the new ones; the next rebuild restores the file.
            logger.warning("Not updating %s (base unreadable): %s", _links_path(skills_root), exc)
            return
        owners.update(new_owners)
        write_links(skills_root, owners)


def rebuild(skills_root: Path, scan: Callable[[Path], dict[str, str]]) -> None:
    """Replace the whole sidecar with the owners *scan* derives from disk.

    Called at gateway start from the skill reconcile so the file is correct after
    an upgrade, a crash mid-write, or a hand edit. *scan* is called under
    :data:`_write_lock` and its result written in the same critical section, so
    the scan and the write it feeds cannot interleave with a concurrent
    ``set_owners`` and persist a half-updated view. Writing an
    empty map still writes the file (a valid "no app skills linked" state); the
    sidecar is never deleted here, so a reader never has to tell "no app skills"
    from "not written yet".

    A *scan* that RAISES (an enumeration error that would make its result
    partial) abandons the replacement: the previous sidecar is left in place
    rather than overwritten with a map that is missing apps whose links are
    still on disk. The next reconcile, once the error clears, writes the full
    map. A rebuild is always safe to abandon — it is additive provenance.
    """
    with _write_lock:
        try:
            owners = scan(skills_root)
        except OSError as exc:
            logger.warning("Not rebuilding %s (scan incomplete): %s", _links_path(skills_root), exc)
            return
        write_links(skills_root, owners)
