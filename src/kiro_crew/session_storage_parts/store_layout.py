"""What the session store is made of: its names and limits, its data types, its roots.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

The vocabulary every other owner shares: the trash, staging and manifest names, the age
floor and the mutation-lock name, the unit-id shape, :class:`SessionStorageError`, the
index, unit, report and trash-batch types, an empty's callbacks, skip codes and
:class:`BatchIdentity`, where the trash and the transcript directories live
(:func:`trash_root`), the rotated-archive index, and the walk that sizes a directory
tree. It imports no other owner.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.config.paths import data_home
from kiro_crew.history import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER, SESSIONS_DIR_NAME

logger = logging.getLogger("kiro_crew.session_storage")

# Trash lives under the data home (not beside kiro-cli's store) because it holds
# Kiro Crew's own staged deletions: it must survive a kiro-cli upgrade, and
# kiro-cli must not mistake a staged file for a session it can resume.
TRASH_DIR_NAME = "trash"
TRASH_SESSIONS_LEAF = "sessions"

# Staged files keep their origin store in the path. Two halves can share a
# filename, and a flat batch directory would let one silently overwrite the other
# — turning a reversible move into data loss.
STAGE_CLI_LEAF = "cli"
STAGE_CREW_LEAF = "crew"

MANIFEST_NAME = "manifest.jsonl"
MANIFEST_SCHEMA = 1

# Prefix for a cross-filesystem copy still in flight, so an operator who finds one
# after a crash can see what it is. Naming only — :func:`_unlisted_files` deliberately
# does NOT exempt it, because a name says nothing about who wrote it.
INCOMING_PREFIX = ".kc-incoming-"

# Absent on Windows, where creating a symlink needs privilege this threat model does not
# hand out; ``| 0`` then leaves the open unchanged rather than failing at import.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Age buckets (in days) the report splits reclaimable sessions into. The
# boundaries are what make a threshold choice legible: the reclaimable total is
# dominated by the youngest band, so a user picking the most conservative
# threshold needs to see that it frees almost nothing before they pick it.
BUCKET_DAYS: tuple[int, ...] = (7, 30, 90)

# No session touched this recently is ever reclaimable, whatever threshold the
# caller passes. The session map is NOT a complete registry of live sessions —
# a subagent run creates a kiro-cli session that was never mapped, so mapping
# alone would let a threshold of 0 reclaim a conversation that is running right
# now and break its resume. Freshness is the one signal that does not depend on
# which subsystem owns a session: a live session is being appended to. A day is
# far longer than any in-tree conversation retention, and the shortest threshold
# the product offers is a week, so the floor costs nothing real.
MIN_RECLAIM_AGE_DAYS = 1.0

# Serializes the operations that move files. Two concurrent reclaims can select
# the same session and interleave their moves, landing its halves in different
# batches — after which neither batch can restore it. The lock is a FILE lock,
# not a thread lock, because more than one instance can share the same kiro-cli
# store (a pod and the live gateway both read ``~/.kiro/sessions/cli``), so
# in-process mutual exclusion would not actually exclude.
MUTATION_LOCK_NAME = "session-storage.lock"

# A unit id is either a kiro-cli session id (a UUID) or a transcript stem (a
# sanitized session key, so word characters, dot and dash). Both are joined onto
# a directory, so a separator or a parent reference would address a file outside
# the stores; the shape is enforced rather than trusted.
_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,199}$")

# The two files a kiro-cli session is made of: metadata and event log. The
# metadata file is also kiro-cli's index entry, so moving it is what makes the
# session leave kiro-cli's session list — no separate index update is needed.
_CLI_SUFFIXES = (".json", ".jsonl")

_TRANSCRIPT_SUFFIX = ".jsonl"
_SECONDS_PER_DAY = 86400.0


class SessionStorageError(Exception):
    """A storage operation was refused. The message is safe to show a user."""


# Named by the path callers import it from, the facade that binds it: a traceback or a
# log line names ``kiro_crew.session_storage.SessionStorageError``, not this module.
SessionStorageError.__module__ = "kiro_crew.session_storage"


@dataclass(frozen=True)
class SessionIndex:
    """What the caller knows about sessions that are still wired up.

    Supplied by the caller rather than read here, so the exclusion set is explicit
    at the call site and a test can pin it.

    *stem_to_sid* maps a transcript filename stem to the kiro-cli session id it
    shares a session with. The direction is deliberate: one session can own more
    than one stem, because a Slack thread predating the canonical session key still
    logs under its bare ``thread_ts`` name. Build it with
    :func:`kiro_crew.history.transcript_stems`, never by re-deriving the naming
    rules — a missed stem is read as "belongs to no session", which makes a live
    session's history reclaimable.
    """

    stem_to_sid: Mapping[str, str] = field(default_factory=dict)
    active_sids: frozenset[str] = frozenset()
    # Sessions with a turn in flight RIGHT NOW, which is a strictly narrower set
    # than *active_sids*. Kept separate rather than folded in: a reclaim is
    # refused for everything in ``active_sids`` either way, so this exists to let
    # a caller say WHY — "in use at this instant" and "idle but still resumable"
    # are different facts and only the first is a hazard. Nothing here loosens a
    # refusal; an empty set means "no running-state signal available", which the
    # reporting path must read as unknown rather than as idle.
    live_sids: frozenset[str] = frozenset()

    @property
    def active_stems(self) -> frozenset[str]:
        """Transcript stems belonging to a session that is still resumable."""
        return frozenset(stem for stem, sid in self.stem_to_sid.items() if sid in self.active_sids)

    @property
    def live_stems(self) -> frozenset[str]:
        """Transcript stems belonging to a session running right now."""
        return frozenset(stem for stem, sid in self.stem_to_sid.items() if sid in self.live_sids)

    def stems_for(self, sid: str) -> tuple[str, ...]:
        return tuple(stem for stem, owner in self.stem_to_sid.items() if owner == sid)


@dataclass(frozen=True)
class SessionUnit:
    """One session's total on-disk cost, whichever halves it has."""

    uid: str
    sid: str
    stems: tuple[str, ...]
    bytes: int
    mtime: float
    active: bool
    # True only while a turn is in flight. A subset of *active*: everything live is
    # also active, so no refusal depends on this field — it exists so a screen can
    # tell "in use right now" apart from "idle but the product could resume it",
    # which is the difference between a hazard and a preference.
    live: bool = False
    #: The crew-log units (ACP session ids) this session owns; see :func:`_crew_log_units`.
    crew_units: tuple[str, ...] = ()

    def age_days(self, now: float) -> float:
        return max(0.0, (now - self.mtime) / _SECONDS_PER_DAY)


@dataclass(frozen=True)
class _RawUnit:
    """One session as the FILESYSTEM sees it: its files, their total, their age.

    Everything here is derived from the two stores plus the transcript-to-replay
    pairing, and nothing from which sessions are in use — which is exactly why a
    pass of these can be cached and reused while the in-use flags are recomputed
    per call. Keeping the two apart is what stops a cache from ever answering
    "may this be reclaimed" off stale state.
    """

    uid: str
    sid: str
    stems: tuple[str, ...]
    bytes: int
    mtime: float
    crew_units: tuple[str, ...] = ()


@dataclass(frozen=True)
class StorageBucket:
    """Reclaimable sessions grouped by how long ago they were last touched."""

    label: str
    sessions: int
    bytes: int


@dataclass(frozen=True)
class TrashBatch:
    """One user-initiated move: the unit a restore undoes."""

    batch_id: str
    created_at: float
    reason: str
    sessions: int
    bytes: int
    #: Sessions that were selected, passed every authority check, and were then
    #: found to have been written to while the batch was being staged — so they
    #: were left in place. Empty for a batch read back off disk
    #: (:func:`list_trash`), which describes what a batch CONTAINS; this field is
    #: about what one move DECLINED, which only the move itself knows.
    revived: tuple[str, ...] = ()


@dataclass(frozen=True)
class StorageReport:
    """What sessions cost, and what is safe to reclaim.

    Deliberately carries no per-store breakdown: a session is one thing with one
    size, and splitting the number would expose an implementation detail the
    reader cannot act on.
    """

    total_bytes: int
    total_sessions: int
    active_sessions: int
    active_bytes: int
    buckets: tuple[StorageBucket, ...]
    reclaimable_sessions: int
    reclaimable_bytes: int
    trash_bytes: int
    trash_batches: int
    trash_same_filesystem: bool
    reclaim_blocked_reason: str = ""


def trash_root() -> Path:
    """Where staged deletions live: ``<data home>/trash/sessions``."""
    return data_home() / TRASH_DIR_NAME / TRASH_SESSIONS_LEAF


def _crew_sessions_dir() -> Path:
    """Kiro Crew's own transcript directory."""
    return data_home() / SESSIONS_DIR_NAME


def _crew_archive_dir() -> Path:
    return _crew_sessions_dir() / ARCHIVE_DIR_NAME


def _validate_unit_id(uid: str) -> str:
    """Return *uid* unchanged, or raise if it could address a file outside the stores."""
    if not _UNIT_ID_RE.match(uid):
        raise SessionStorageError(f"not a valid session id: {uid!r}")
    return uid


def _archive_index() -> dict[str, list[tuple[Path, int, float]]]:
    """Group rotated archive segments by the transcript stem they belong to.

    Built once per operation: resolving a stem's segments by scanning the archive
    directory per session would be quadratic across a bulk reclaim.
    """
    index: dict[str, list[tuple[Path, int, float]]] = {}
    try:
        with os.scandir(_crew_archive_dir()) as it:
            for entry in it:
                if not entry.name.endswith(_TRANSCRIPT_SUFFIX):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                stem = entry.name.rsplit(ARCHIVE_SEGMENT_DELIMITER, 1)[0]
                if stem == entry.name:  # not a segment, so not attributable
                    continue
                index.setdefault(stem, []).append((Path(entry.path), st.st_size, st.st_mtime))
    except OSError:
        pass
    return index


def _tree_files(directory: Path) -> list[tuple[int, float]]:
    """``(size, mtime)`` of every regular file under *directory*, links not followed."""
    found: list[tuple[int, float]] = []
    pending = [directory]
    while pending:
        with os.scandir(pending.pop()) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    found.append((info.st_size, info.st_mtime))
    return found


def _tree_bytes(directory: Path) -> int:
    """Total size of the regular files under *directory*; 0 when it cannot be read."""
    try:
        return sum(size for size, _mtime in _tree_files(directory))
    except OSError:
        return 0


# Called with the running total of bytes freed by an empty. See :func:`empty_trash`.
EmptyProgress = Callable[[int], None]

#: Called with a reason code when a batch is kept instead of deleted. See
#: :func:`empty_trash`.
EmptySkip = Callable[[str], None]

#: Why a batch was kept. Codes, not prose: the caller that shows them to a person
#: has to translate them, and a log line cannot be translated.
SKIP_OUTSIDE_ROOT = "outside_trash_root"
SKIP_UNREADABLE = "unreadable_batch"
SKIP_UNLISTED_FILES = "unlisted_files"
#: The delete ran but the batch is still on disk, so it is still on the user's screen.
SKIP_INCOMPLETE = "incomplete"
#: The directory answering to an approved batch id is not the one that was selected.
SKIP_IDENTITY_CHANGED = "identity_changed"


@dataclass(frozen=True)
class BatchIdentity:
    """What a batch WAS when the user approved it, as objects rather than names.

    ``dev``/``ino`` identify the batch directory. ``dirs`` maps each directory INSIDE it
    to its inode, keyed by batch-relative components - because pinning the batch does not
    pin its interior: the batch's own inode is unchanged by a rename that happens inside
    it, so a live directory moved onto a staged directory's name would be opened and its
    files unlinked. Establishing that map at approval time, under the mutation lock, is
    what makes it an approval rather than an observation: a map built at delete time
    records whatever is there by then, impostor included.

    ``dirs`` is None where the platform cannot walk a tree by descriptor (Windows), which
    is also the platform that takes the coarse removal; there the batch is renamed to an
    unguessable staging name before it is removed, which moves the whole subtree at once.

    ``files`` and ``links`` are the same argument one level down, and they are recorded for
    the same reason rather than as symmetry for its own sake. The delete checks each staged
    file's identity against the APPROVAL's map, never against a map built by its OWN scan -
    which is self-consistent and authorises nothing. A listed file replaced between the
    approval and the delete would have its replacement's inode recorded, matched, and
    unlinked: an unapproved file, whose only copy it may be, destroyed on consent given for
    a different one. Both are None on the coarse platform for the reason ``dirs`` is.
    """

    dev: int
    ino: int
    dirs: dict[tuple[str, ...], int] | None
    files: dict[tuple[str, ...], int] | None = None
    links: dict[tuple[str, ...], int] | None = None
    #: Digest of the listing the approval was computed from. The manifest's INODE is not
    #: enough: rewritten in place it keeps that inode, and the rewrite decides WHICH files
    #: the delete is allowed to remove. A file already sitting in the batch, unlisted, is
    #: refused today - add it to the listing after the approval and every identity check
    #: still passes, because the file itself never changed. A digest rather than the rels
    #: themselves: constant memory on a batch with six figures of entries, and a refusal
    #: does not need to say which line moved.
    rels_digest: str | None = None
