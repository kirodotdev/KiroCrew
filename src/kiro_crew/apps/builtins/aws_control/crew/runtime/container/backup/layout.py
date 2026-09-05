"""Mapping between the on-disk backup unit and flat S3 object keys.

The backup unit (``Settings.backup_unit``) is a mix of directories and single
files that live under two independent roots: ``data_home`` (transcripts,
archive, artifacts) and ``config_dir`` (``session_map.json``, ``open_slots.json``).
This module is the one place that decides how a local path becomes an object
key and how a key becomes a local path again. Backup and restore MUST agree on
that decision, so both go through here rather than each inventing a scheme.

Keys carry a two-namespace prefix so the two roots never collide even when
``config_dir`` is nested inside ``data_home`` (the default):

    data/<path relative to data_home>       e.g. data/sessions/<sid>.jsonl
    config/<path relative to config_dir>    e.g. config/session_map.json

The full S3 key additionally carries ``<backup_prefix>/<crew_name>/`` so one
bucket can hold many crews. ``rel`` keys (namespace + relative path) are what
the local change-tracking state records, because they are stable regardless of
where the bucket or crew prefix moves.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator

from ..common import Settings

logger = logging.getLogger(__name__)

_DATA_NS = "data/"
_CONFIG_NS = "config/"


# --- exclusions ------------------------------------------------------------


def is_excluded(rel_path: str) -> bool:
    """True for paths that must never be backed up or restored.

    ``rel_path`` is the namespace-stripped POSIX path (e.g.
    ``sessions/agent-1.jsonl``). Excluded:

    * ``*.pid`` — host-local process ids, meaningless in a new task.
    * ``*.lock`` — the per-session advisory flock sidecars. They carry no
      durable content; a restored stale lock file would just be an empty
      artifact (and Kiro Crew keeps them out of every ``*.jsonl`` glob anyway).
    * ``subagents/*/state.json`` — holds a host-local subagent PID; restoring it
      points the new task at a process that does not exist.
    """
    name = rel_path.rsplit("/", 1)[-1]
    if name.endswith(".pid") or name.endswith(".lock"):
        return True
    parts = rel_path.split("/")
    for i in range(len(parts) - 2):
        if parts[i] == "subagents" and parts[i + 2] == "state.json":
            return True
    return False


# --- classifying a rel key -------------------------------------------------


def is_config_key(rel_key: str) -> bool:
    """True when ``rel_key`` names an object in the ``config/`` namespace.

    Restore writes this namespace and nothing else, so it has to ask the
    question somewhere. It asks here: the namespace constants are private to
    this module on purpose, because backup and restore MUST agree on them, and
    a second hardcoded ``"config/"`` at a call site is exactly how the two
    sides drift apart.
    """
    return rel_key.startswith(_CONFIG_NS)


def sessions_prefix(settings: Settings) -> str:
    """Rel-key prefix under which conversation transcripts live."""
    try:
        tail = settings.sessions_dir.relative_to(settings.data_home).as_posix()
    except ValueError:
        tail = settings.sessions_dir.name
    return _DATA_NS + tail + "/"


def is_transcript(settings: Settings, rel_key: str) -> bool:
    """True when ``rel_key`` names a conversation transcript.

    Both forms count: the live ``sessions/<sid>.jsonl`` and the rotated
    ``sessions/archive/<sid>--<stamp>.jsonl`` segments. An archive segment is
    the same conversation, only older, so a restore that skipped live files and
    pulled the archive would still put another customer's words on this disk.

    This is the classifier the restore's transcript count is computed from. It
    lives here with the rest of the key-shape knowledge rather than being
    re-guessed at the call site, because a deploy gate reads that count: a
    classifier that quietly stopped recognising archive segments would turn the
    gate green while the property was broken.
    """
    return rel_key.startswith(sessions_prefix(settings)) and rel_key.endswith(".jsonl")


# --- rel key <-> local path ------------------------------------------------


def _config_rel(settings: Settings, path: Path) -> str:
    try:
        tail = path.relative_to(settings.config_dir).as_posix()
    except ValueError:
        tail = path.name
    return _CONFIG_NS + tail


def config_keys(settings: Settings) -> dict[str, str]:
    """The two authoritative config-file rel keys, keyed by role name."""
    return {
        "session_map": _config_rel(settings, settings.session_map_path),
        "open_slots": _config_rel(settings, settings.open_slots_path),
    }


def local_path_for_key(settings: Settings, rel_key: str) -> Path | None:
    """Reconstruct the local path for a rel key, or None if it is unroutable.

    Guards against path traversal: an object key is untrusted input, and a key
    like ``data/../../etc/x`` must not escape the data home. A key that would
    resolve outside its namespace root is dropped rather than written.
    """
    if rel_key.startswith(_DATA_NS):
        root = settings.data_home
        tail = rel_key[len(_DATA_NS) :]
    elif rel_key.startswith(_CONFIG_NS):
        root = settings.config_dir
        tail = rel_key[len(_CONFIG_NS) :]
    else:
        return None
    if not tail or tail.endswith("/"):
        return None
    candidate = root / tail
    root_res = os.path.normpath(str(root))
    cand_res = os.path.normpath(str(candidate))
    if cand_res != root_res and not cand_res.startswith(root_res + os.sep):
        return None
    return candidate


# --- walking the backup unit ----------------------------------------------


def _walk_dirs(settings: Settings) -> list[Path]:
    """Directory roots to walk, with nested roots removed.

    ``archive_dir`` lives inside ``sessions_dir`` in the default construction,
    so both appear in ``backup_unit``. Walking both would upload the archive
    twice; drop any directory that is contained in another.
    """
    dirs: list[Path] = []
    file_paths = {settings.session_map_path, settings.open_slots_path}
    for p in settings.backup_unit():
        if p in file_paths:
            continue
        dirs.append(p)
    roots: list[Path] = []
    for d in dirs:
        nested = False
        for other in dirs:
            if d == other:
                continue
            try:
                if d.is_relative_to(other):
                    nested = True
                    break
            except AttributeError:  # pragma: no cover - py<3.9
                if str(d).startswith(str(other) + os.sep):
                    nested = True
                    break
        if not nested and d not in roots:
            roots.append(d)
    return roots


def iter_backup_files(settings: Settings) -> Iterator[tuple[Path, str, Path]]:
    """Yield ``(local_path, rel_key, root)`` for every file in the backup unit.

    ``root`` is the directory this entry was enumerated UNDER, reported rather than
    re-derived by the caller: the reader walks the path one component at a time
    beneath it to refuse a swapped parent, and guessing the wrong root there would
    either raise or check the wrong prefix. There are several roots (see
    ``_walk_dirs``) plus the config files' own parent, so only this function knows.

    Config files first (they are single files, cheap, and their presence is
    what restore checks for completeness), then the data directories.
    """
    for f in (settings.session_map_path, settings.open_slots_path):
        if f.is_file():
            rel = _config_rel(settings, f)
            if not is_excluded(rel[len(_CONFIG_NS) :]):
                yield f, rel, f.parent
    for root in _walk_dirs(settings):
        if not root.exists():
            continue
        real_root = os.path.realpath(root)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # os.walk(followlinks=False) refuses to DESCEND through a directory
            # symlink, but it still lists a symlinked directory in dirnames and a
            # symlinked FILE in filenames, and reading one follows it. The agent
            # writes into this tree (that is what the artifacts directory is for)
            # and its backend auto-approves every tool, so a planted link is a
            # reachable input, and this is the WRITE direction: whatever the link
            # points at gets uploaded to the owner's bucket. A link to
            # /proc/self/environ would persist the task's own credentials.
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            base = Path(dirpath)
            for fn in filenames:
                fp = base / fn
                # Judged on the resolved target, then confirmed to be a real regular
                # file. The islink half is the one that does the work: it refuses an
                # alias whose target is INSIDE the root too, which the containment
                # check below cannot see and which would otherwise upload one
                # conversation twice under two keys.
                target = os.path.realpath(fp)
                if not os.path.isfile(target) or os.path.islink(fp):
                    if os.path.islink(fp):
                        logger.warning(
                            "backup: skipping %s, a symlink to %s -- only real files "
                            "inside the backup root are uploaded",
                            fp,
                            target,
                        )
                    continue
                # Redundant against the check above for every case reachable today,
                # and measured to be so rather than assumed: with islink removed,
                # this catches the out-of-root links, and with THIS removed islink
                # catches them, so neither has an exclusive case. It stays as the
                # statement of the actual rule (nothing outside the root is
                # uploaded), which survives a future edit that loosens the islink
                # test for some legitimate alias.
                #
                # Worth naming what NEITHER catches: a hard link to a file outside
                # the root is not a symlink and its realpath stays inside, so it
                # reads as an ordinary file. Closing that needs a different tool
                # (st_dev/st_ino against the root's tree, or a mount-scoped walk)
                # and is not attempted here.
                if target != real_root and not target.startswith(real_root + os.sep):
                    logger.warning(
                        "backup: skipping %s, which resolves outside the backup root", fp
                    )
                    continue
                try:
                    tail = fp.relative_to(settings.data_home).as_posix()
                except ValueError:
                    tail = (Path(root.name) / fp.relative_to(root)).as_posix()
                if is_excluded(tail):
                    continue
                yield fp, _DATA_NS + tail, root


def artifact_prefix(settings: Settings) -> str:
    """Rel-key prefix under which write-once artifacts live."""
    try:
        tail = settings.artifacts_dir.relative_to(settings.data_home).as_posix()
    except ValueError:
        tail = settings.artifacts_dir.name
    return _DATA_NS + tail + "/"


def needs_lock(settings: Settings, path: Path) -> bool:
    """True for the live transcripts the backend appends to in place.

    Only ``sessions/<sid>.jsonl`` directly under ``sessions_dir`` is appended
    in place under a ``<name>.lock`` flock (``history.py``). Archive segments
    and the JSON config files are written by atomic replace, so a plain read
    already sees a whole file and locking them would guard the wrong inode.
    """
    return path.suffix == ".jsonl" and path.parent == settings.sessions_dir


# --- full S3 key <-> rel key ----------------------------------------------


def object_prefix(settings: Settings) -> str:
    parts = [p for p in (settings.backup_prefix.strip("/"), settings.crew_name.strip("/")) if p]
    return ("/".join(parts) + "/") if parts else ""


def full_key(settings: Settings, rel_key: str) -> str:
    return object_prefix(settings) + rel_key


def rel_from_full(settings: Settings, full: str) -> str | None:
    pre = object_prefix(settings)
    if not full.startswith(pre):
        return None
    return full[len(pre) :]
