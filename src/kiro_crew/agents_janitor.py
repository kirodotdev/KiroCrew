"""Age-based janitor for the shared kiro agents directory.

The kiro agents directory (:func:`kiro_crew.config.paths.kiro_agents_dir`) is
rewritten continuously by several independent writers — this project's spec
rebuild plus other tools that install agents there. Every writer uses
write-temp-then-rename, and a crash or a race between the write and the rename
strands the temp file forever. Backups written beside the live spec accumulate
the same way. Nothing removes any of it, so the directory only grows and every
consumer that globs it pays for the junk.

This module sweeps only the recognized, unambiguously-dead name shapes, and only
when they are old enough that no in-flight operation could still own them:

* orphaned atomic-write temps ``<base>.json.<digits>.tmp`` (foreign writers) and
  ``tmp<alnum>.tmp`` (this project's own ``mkstemp`` residue) — swept at 24h;
* aged backups ``*.bak-<digits>`` and ``*.json.bak.<digits>`` — OPT-IN
  (``sweep_backups=True``) and swept at a much longer 14-day window. Kiro Crew
  authors no backups in this directory, so every backup a sweep would remove
  belongs to a foreign writer whose retention policy is not ours to decide; and
  a backup exists precisely to outlive its write, so the millisecond "garbage by
  construction" argument that justifies 24h for a temp does not apply. The
  wired-in callers therefore leave backups alone unless
  ``agent.sweep_agents_backups`` is enabled.

Conservative by construction — an in-flight atomic replace completes in
milliseconds, so a recognized temp older than the 24h threshold is garbage. The
sweep is deliberately narrow and fail-open:

* it matches ONLY the exact name shapes above (never a live ``*.json`` spec, and
  never a foreign file whose name does not match);
* it never touches directories;
* it never follows symlinks — every entry is inspected with :func:`os.lstat`,
  and a symlink (even one whose name matches) is skipped, so the sweep can only
  ever unlink a real regular file inside the agents directory itself;
* it removes only regular files whose mtime is at least the threshold old;
* every deletion is logged with the filename and the file's age;
* a failure on any one entry (unreadable, vanished mid-sweep, permission
  denied) is tolerated and the sweep continues.

It is intentionally isolated in its own module with a small, side-effect-free
surface so it can be wired into ``kirocrew doctor`` and gateway boot in one or
two lines each. The same write-temp-then-rename residue exists in other
data-home directories too; the agents directory is swept first because it is the
*shared*, foreign-writer directory that grows fastest, and a general residue
sweep across the other single-writer directories is left as follow-up.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Minimum age before a recognized atomic-write TEMP is considered garbage. An
#: atomic replace finishes in milliseconds, so 24h is orders of magnitude beyond
#: any legitimate lifetime for a ``.tmp`` orphan.
DEFAULT_TEMP_MAX_AGE_SECONDS = 24 * 60 * 60

#: Minimum age before a recognized BACKUP is swept. Backups are deliberately
#: longer-lived than temps: a ``*.bak-<digits>`` / ``*.json.bak.<digits>`` exists
#: precisely to outlive the write that produced it, so it can be used to recover
#: a bad spec. Deleting one after only 24h would remove a recovery artifact — and
#: ``kirocrew doctor``, the command you run *when something is broken*, is one of
#: the sweep triggers — so backups get a much longer window (14 days). The
#: "garbage by construction" argument that justifies 24h for temps does not
#: apply to backups, so they are aged separately.
DEFAULT_BACKUP_MAX_AGE_SECONDS = 14 * 24 * 60 * 60

#: Recognized non-live TEMP shapes, anchored so a match is the WHOLE filename.
#:
#: * ``<base>.json.<digits>.tmp`` — an orphaned atomic-write temp from a foreign
#:   writer. The digits are the writer's disambiguator (a pid, an epoch, a
#:   counter); requiring at least one digit between ``.json.`` and ``.tmp`` is
#:   what distinguishes a stranded temp from a legitimately-named file ending in
#:   ``.tmp``.
#: * ``tmp<alnum>.tmp`` — Kiro Crew's OWN atomic-write residue. ``agent.py``'s
#:   ``_atomic_json_write`` calls ``tempfile.mkstemp(suffix=".tmp")``, which
#:   emits ``tmp`` + random alphanumerics + ``.tmp`` (never the dotted
#:   ``.json.<digits>.tmp`` shape), so without this pattern the project's own
#:   crash residue would never be reclaimed. The ``tmp`` prefix + ``.tmp``
#:   suffix + a non-empty random middle is exactly mkstemp's contract, which is
#:   narrow enough not to sweep an arbitrarily-named foreign file.
#:
#: A live spec is ``<name>.json`` with no trailing temp suffix and never matches,
#: which is the core safety property.
_TEMP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^.+\.json\.\d+\.tmp$"),
    re.compile(r"^tmp[A-Za-z0-9_]+\.tmp$"),
)

#: Recognized BACKUP shapes, anchored to the whole filename.
#:
#: * ``<base>.bak-<digits>`` — an epoch-suffixed backup.
#: * ``<base>.json.bak.<digits>`` — the other backup shape seen in the wild.
_BACKUP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^.+\.bak-\d+$"),
    re.compile(r"^.+\.json\.bak\.\d+$"),
)


def _classify_junk_name(name: str) -> str | None:
    """Return ``"temp"``/``"backup"`` if *name* is a recognized dead shape, else ``None``.

    Name-only classification — it never touches the filesystem, so it cannot be
    a live ``*.json`` spec (none of the anchored patterns match a bare ``.json``)
    and it never depends on TOCTOU-sensitive state. Which class a name falls into
    selects the age threshold applied to it.
    """
    if any(pattern.match(name) for pattern in _TEMP_PATTERNS):
        return "temp"
    if any(pattern.match(name) for pattern in _BACKUP_PATTERNS):
        return "backup"
    return None


@dataclass
class SweepResult:
    """Outcome of one sweep, for logging / doctor reporting.

    ``removed`` and ``freed_bytes`` count files unlinked (or, under ``dry_run``,
    that would be unlinked). ``removed_names`` keeps their basenames so a caller
    can render them without re-listing the directory.
    """

    removed: int = 0
    freed_bytes: int = 0
    removed_names: list[str] = field(default_factory=list)


def _unlink_if_still_stale(
    target: Path,
    seen: os.stat_result,
    threshold: float,
    reference: float,
) -> bool:
    """Unlink *target* only if it is still the same aged regular file we scanned.

    Closes the recreate-same-pathname race: between the directory scan and this
    call a writer can crash-strand a NEW temp at the same pathname (a foreign
    ``<base>.json.<digits>.tmp`` whose pid/counter was reused), and a blind
    ``os.unlink(target)`` would then delete that fresh in-flight file and lose
    the writer's spec write. So re-``lstat`` the path immediately before
    unlinking and refuse unless the entry is byte-for-byte the one we judged:

    * same inode identity (``st_dev`` + ``st_ino``) — the file was not replaced;
    * a regular file, not a symlink — nothing was swapped for a link;
    * unchanged mtime and size — it was not rewritten in place;
    * still at least *threshold* old — belt-and-suspenders on the age.

    Returns ``True`` when the file was unlinked, ``False`` when it was skipped
    (changed, vanished, or unreadable) — never raises, so the sweep stays
    fail-open.
    """
    try:
        now_info = os.lstat(target)
    except OSError:
        # Vanished between scan and re-stat (another writer cleaned it up), or
        # unreadable. Nothing to delete; not an error we must surface.
        logger.debug("agents janitor: %r gone before re-stat; skipping", target.name)
        return False

    if (
        now_info.st_dev != seen.st_dev
        or now_info.st_ino != seen.st_ino
        or now_info.st_mtime != seen.st_mtime
        or now_info.st_size != seen.st_size
        or stat.S_ISLNK(now_info.st_mode)
        or not stat.S_ISREG(now_info.st_mode)
        or (reference - now_info.st_mtime) < threshold
    ):
        # The path no longer holds the aged garbage we classified — a writer
        # recreated or refreshed it. Leave the current occupant alone.
        logger.debug("agents janitor: %r changed before delete; skipping", target.name)
        return False

    try:
        os.unlink(target)
    except OSError:
        # Per-file failure is tolerated: another writer may have removed it, or
        # permissions may forbid it. Never abort the whole sweep.
        logger.debug("agents janitor: could not remove %r; skipping", target.name, exc_info=True)
        return False
    return True


def sweep_agents_dir(
    agents_dir: Path | str,
    *,
    now: float | None = None,
    sweep_backups: bool = False,
    dry_run: bool = False,
) -> SweepResult:
    """Remove aged orphaned atomic-write temps and stale backups in *agents_dir*.

    Deletes only entries whose *name* matches a recognized temp/backup shape
    (see :data:`_TEMP_PATTERNS` / :data:`_BACKUP_PATTERNS`) AND that are regular
    files (not symlinks, not directories) old enough for their class:
    :data:`DEFAULT_TEMP_MAX_AGE_SECONDS` for temps,
    :data:`DEFAULT_BACKUP_MAX_AGE_SECONDS` for backups. Temps and backups are
    aged separately because a backup exists to outlive its write, so the
    millisecond "garbage by construction" argument that justifies a 24h temp
    threshold does not apply to it. Everything else — live ``*.json`` specs,
    foreign files, directories, symlinks, and anything younger than its
    threshold — is left untouched.

    Fail-open throughout: a missing directory, an unreadable entry, or a file
    that vanishes mid-sweep is tolerated and never raises. Returns a
    :class:`SweepResult` so callers can log or report what happened.

    :param now: reference time (epoch seconds) for age computation; defaults to
        :func:`time.time`. Exposed for deterministic tests.
    :param sweep_backups: when false (the default), only temps are swept and
        recognized backups are left entirely alone. Kiro Crew authors no
        ``*.bak-<digits>`` / ``*.json.bak.<digits>`` files in this directory, so
        every backup a sweep would remove belongs to a foreign writer whose
        retention policy is not ours to decide; the default therefore reaps only
        the atomic-write temps, which carry the bulk of the growth at near-zero
        risk. The wired-in callers pass ``agent.sweep_agents_backups`` (also off
        by default), so an operator must opt in twice-over before a foreign
        backup is ever touched.
    :param dry_run: when true, identify eligible files and populate
        ``removed`` / ``freed_bytes`` / ``removed_names`` exactly as a real
        sweep would, but never unlink anything. Lets a read-only caller (e.g.
        ``kirocrew doctor``) REPORT what a sweep would reclaim without mutating
        the directory — deletion is left to the fire-and-forget boot sweep.
    """
    result = SweepResult()
    reference = time.time() if now is None else now
    directory = Path(agents_dir)

    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        # A fresh install / ephemeral instance may not have the directory yet.
        return result
    except OSError:
        # Unreadable directory — fail open, sweep nothing.
        logger.debug("agents janitor: could not list %s; skipping sweep", directory, exc_info=True)
        return result

    for entry in entries:
        name = entry.name
        kind = _classify_junk_name(name)
        if kind is None:
            # Not a recognized shape — includes every live ``*.json`` spec and
            # any foreign file with an unrecognized name.
            continue
        if kind == "backup" and not sweep_backups:
            # Backups belong to foreign writers here — skip unless opted in.
            continue

        try:
            # os.lstat on the entry PATH, deliberately NOT
            # ``entry.stat(follow_symlinks=False)``: the delete-time recheck
            # (:func:`_unlink_if_still_stale`) compares this snapshot against a
            # fresh ``os.lstat`` by inode identity, and on Windows ``scandir``'s
            # cached stat carries ``st_ino == st_dev == 0`` while ``os.lstat``
            # returns the real identifiers — the comparison then NEVER matches
            # and the sweep silently refuses every removal. One syscall family
            # on both sides keeps the identity check meaningful everywhere.
            # (lstat, never stat: it must NOT follow a symlink. A symlink whose
            # name happens to match a recognized shape is skipped entirely, so
            # the sweep can only ever unlink a real regular file inside the
            # agents directory — never a link target elsewhere.)
            info = os.lstat(entry.path)
        except OSError:
            # Raced with a delete, or unreadable — tolerate and move on. The
            # filename is rendered with ``%r`` so an attacker-controlled name
            # (this directory is shared with foreign writers) cannot smuggle a
            # terminal-control sequence through the log.
            logger.debug("agents janitor: could not stat %r; skipping", name, exc_info=True)
            continue

        mode = info.st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            # Symlinks (even matching-named ones) and non-regular entries
            # (directories, sockets, fifos) are never candidates.
            continue

        threshold = (
            DEFAULT_TEMP_MAX_AGE_SECONDS if kind == "temp" else DEFAULT_BACKUP_MAX_AGE_SECONDS
        )
        age = reference - info.st_mtime
        if age < threshold:
            # A genuinely in-flight temp, or a backup still inside its retention
            # window — leave it.
            continue

        if not dry_run:
            if not _unlink_if_still_stale(directory / name, info, threshold, reference):
                # The entry changed between the scan and the delete — recreated,
                # replaced, or refreshed by a writer — so the file at this path
                # is no longer the aged garbage we classified. Skip it: this is
                # the guard against the recreate-same-pathname race where a boot
                # sweep could otherwise unlink a writer's brand-new temp and lose
                # its spec write.
                continue

        result.removed += 1
        result.freed_bytes += info.st_size
        result.removed_names.append(name)
        # ``%r`` on the name: foreign writers control these filenames, so a raw
        # ``%s`` would let a crafted name inject a terminal-control sequence into
        # a log stream an operator later views in a terminal.
        logger.info(
            "agents janitor: %s %r (%s, age %.1fh, %d bytes)",
            "would remove" if dry_run else "removed",
            name,
            kind,
            age / 3600.0,
            info.st_size,
        )

    return result
