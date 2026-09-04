"""Measure the disk a session costs, and reclaim it under user control.

A session is presented as ONE thing with ONE size, because that is what it is to
the person looking at it. Underneath, its bytes are split across two stores that
Kiro Crew and kiro-cli each own:

* ``<data home>/sessions/<stem>.jsonl`` plus its rotated
  ``sessions/archive/<stem>__<stamp>.jsonl`` segments — the transcript, read by
  dashboard history, search and memory consolidation.
* ``<kiro home>/sessions/cli/<sid>.json`` + ``<sid>.jsonl`` — kiro-cli's replay
  log, read to resume the session.

That split is an implementation detail and never surfaces in a report: callers get
one total and one session count.

The split does, however, dictate the deletion rule. **A session's halves are
always reclaimed together.** Removing only one leaves a broken session rather than
a freed one: drop the replay log and the transcript still lists a session that can
no longer resume; drop the transcript and a resumable session has no history or
search. Either way the user is left with something worse than both extremes.
:func:`_unit_paths` is therefore the single place that answers "what files is this
session made of", and every move, restore and delete goes through it.

Not every session has both halves, and that is normal rather than an error — a
subagent run leaves only a replay log, and a session whose mapping was pruned
leaves only a transcript. The rule is that whatever halves exist move together.

Reclaiming moves files into ``<data home>/trash/sessions/<batch>/`` rather than
unlinking them. On a default install both stores sit under ``~/.kiro``, so the
move is a same-filesystem :func:`os.rename`: instant regardless of size, and
instantly reversible. Space is **not** returned to the filesystem until the trash
is emptied; :attr:`StorageReport.trash_bytes` exists so callers can say so plainly
instead of reporting a reclaim that leaves ``df`` unchanged.

Every batch carries an append-only manifest recording each file's original
absolute path, so a restore puts files back where they came from instead of
inferring it from the layout. A batch can span six figures of sessions, so a
whole-document manifest rewritten per move would cost quadratic bytes; appending
also leaves a partial batch fully restorable after an interruption.

Reading is cached; reclaiming never is
--------------------------------------
Enumerating the stores is the entire cost of a read here — half a million replay
files on the measured machine — so a pass is kept briefly and shared between the
row list, the totals and each row's detail. Only the FILESYSTEM halves are
cached — the store scan and, on read paths, the co-tenant pass beside it: the
flags derived from the caller's index are recomputed every call, every
refusal-deriving path runs uncached, and every mutation re-enumerates and
re-reads the index inside the lock. So no refusal is ever answered from a
snapshot, and a stale cache can only make the report slightly old, never make a
reclaim take something it should not.

Sessions this instance does not own
-----------------------------------
The replay store can be shared. A pod now gets its own (``pod/runtime.py`` exports
``KIRO_HOME`` inside the pod home), so a current pod is not a co-tenant — but a
directory left by a pod from before that export may still own sessions in the
machine-wide store. Those maps are files at a known host-side path, so
:func:`cotenant_sids` reads them and protects the sessions they name individually,
exactly like this instance's own. A blanket refusal is kept only where ownership
genuinely cannot be established: a co-tenant whose map is unreadable, or a data
home isolated from the store it reads (see :func:`reclaim_block_reason`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO, Any

from kiro_crew import hooks, pinned_fs, platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir, replace_with_retry
from kiro_crew.config.paths import (
    CONFIG_DIR_LEAF,
    KIRO_BASE_DIR_NAME,
    data_home,
    kiro_home,
    kiro_sessions_dir,
    legacy_home,
)
from kiro_crew.history import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER, SESSIONS_DIR_NAME
from kiro_crew.session_map import SESSION_MAP_FILENAME

logger = logging.getLogger(__name__)

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


# ------------------------------------------------------------------ scan cache
#
# Enumerating both stores is the whole cost of every read on this surface. The
# measured motivating machine holds ~470,000 replay files, which is ~2s of
# stat() per pass — and one open of the inventory screen needs the same
# enumeration for the row list, again for the totals printed beside it, and again
# for every row the user expands. That is seconds of disk per interaction for an
# answer that cannot meaningfully change between them.
#
# So a pass is kept for a few seconds and shared. Two properties make that safe
# rather than merely faster:
#
# * Only the filesystem halves are cached — the store scan and, on read paths,
#   the co-tenant pass beside it (its own cache, further down). The flags derived
#   from the caller's index are applied over them on every call, and every path
#   that DERIVES A REFUSAL — the reclaim gates, the pre-move re-read, the
#   dashboard's trash pre-classification — runs uncached, so no refusal is ever
#   decided from a snapshot. The one cached contribution to ``active`` (the
#   co-tenant set) therefore only ever staleness-affects what a read displays.
# * Only READ paths opt in. A reclaim re-enumerates, and additionally re-reads
#   the index inside the mutation lock, so the selection it acts on is current.
#
# A mutation invalidates the entry outright, so a screen refetching after a move
# does not show what it just deleted.
_SCAN_CACHE_TTL = 30.0

_scan_cache_lock = threading.Lock()
# (expires_at, cache key, units)
_scan_cache: tuple[float, tuple[object, ...], list[_RawUnit]] | None = None


def _scan_key(sid_for_stem: Mapping[str, str]) -> tuple[object, ...]:
    """Everything a cached pass depends on besides the contents of the stores.

    The store LOCATIONS are part of it. Both are resolved per call by design — a
    pod overrides the data home, and an unmigrated install resolves the legacy one
    — so a process can legitimately enumerate different stores over its lifetime,
    and a key without them would answer a question about one store with a pass over
    another. This was not hypothetical: it showed up immediately as one test's
    totals being served to the next.

    Pairing is the rest of it: it decides which unit a transcript belongs to, so a
    session that gained or lost its mapping must not be answered from an older
    pass. The mapping is compared directly by dict equality — still by value,
    never hashed — because a hash collision here would serve a pass built under
    different assumptions, and the whole point of the key is that it cannot.
    Embedding the dict makes the key tuple unhashable, so "never hashed" is
    enforced by the interpreter rather than by convention, and the hit check
    is linear — one dict copy plus one dict compare — with no sort. The
    ``dict()`` copy is load-bearing:
    it snapshots the pairing, so a caller's later in-place edit cannot mutate
    the stored key in lockstep and masquerade as a hit.
    """
    return (
        str(kiro_sessions_dir()),
        str(_crew_sessions_dir()),
        dict(sid_for_stem),
    )


def _cached_scan(sid_for_stem: Mapping[str, str]) -> list[_RawUnit] | None:
    with _scan_cache_lock:
        if _scan_cache is None:
            return None
        expires, key, units = _scan_cache
        if time.monotonic() >= expires or key != _scan_key(sid_for_stem):
            return None
        return units


def _store_scan(sid_for_stem: Mapping[str, str], units: list[_RawUnit]) -> None:
    global _scan_cache
    with _scan_cache_lock:
        _scan_cache = (time.monotonic() + _SCAN_CACHE_TTL, _scan_key(sid_for_stem), units)


def invalidate_scan_cache() -> None:
    """Drop any cached filesystem pass. Called after anything moves or is deleted.

    Public rather than private because a test needs to start from a known state:
    the cache is process-wide and its key covers the store paths and the pairing,
    not the CONTENTS of the stores, so a test that writes more files and re-reads
    inside the TTL would otherwise be answered from its own earlier pass.

    Drops the co-tenant pass too (see :func:`cotenant_sids`). A mutation that
    invalidates one cache has changed the world the other described, and clearing
    both here keeps every existing mutation hook correct without each having to
    know that two caches exist. Pod-lifecycle changes — a pod appearing, being
    torn down, or rewriting its map — have no hook here and are covered by the
    TTL alone; that is acceptable because every destructive path re-reads the
    co-tenant view uncached regardless.
    """
    global _scan_cache, _cotenant_cache
    with _scan_cache_lock:
        _scan_cache = None
    with _cotenant_cache_lock:
        _cotenant_cache = None


def _scan_units(index: SessionIndex, *, cached: bool = False) -> list[SessionUnit]:
    """Enumerate sessions across both stores, one entry per session.

    Two halves of the answer, deliberately separated. :func:`_scan_raw` does the
    filesystem work and knows nothing about which sessions are in use; this
    applies the caller's index over that result. So the expensive half can be
    reused (see *cached*) while the flags derived from the index are always
    computed from the index the caller passed in this call. ``active`` has one
    further input — the co-tenant set — which follows *cached* below.

    *cached* permits a recent filesystem pass to be reused — both halves of it:
    the store scan (:func:`_scan_raw`) and the co-tenant lookup below thread the
    same flag. It is for READ paths only: every path that derives a refusal from
    the result (the mutation gates, and the dashboard pre-classification that
    feeds one) leaves it False, and a reclaim must not select against a snapshot
    at all, so every mutation path leaves it False too.
    """
    raw = _scan_raw(index.stem_to_sid, cached=cached)
    active_stems = index.active_stems
    live_stems = index.live_stems
    # Sessions another instance sharing this replay store can still resume. Marked
    # active HERE rather than folded into the caller's index, because this is the
    # one place every read and every reclaim passes through — measure, the row
    # list, the pre-classification and move_to_trash all derive `active` from it,
    # so one assignment protects all of them. A caller cannot forget to ask.
    cotenant, _unreadable = cotenant_sids(cached=cached)
    units = []
    for entry in raw:
        active = (
            entry.sid in index.active_sids
            or entry.sid in cotenant
            or any(stem in active_stems for stem in entry.stems)
        )
        live = entry.sid in index.live_sids or any(stem in live_stems for stem in entry.stems)
        units.append(
            SessionUnit(
                uid=entry.uid,
                sid=entry.sid,
                stems=entry.stems,
                bytes=entry.bytes,
                mtime=entry.mtime,
                active=active,
                live=live,
            )
        )
    return units


def _scan_raw(sid_for_stem: Mapping[str, str], *, cached: bool = False) -> list[_RawUnit]:
    """Enumerate both stores: what each session is made of and what it costs.

    A session's age is the NEWEST mtime across every file it owns: a transcript is
    appended to while the session runs, so an older metadata file or a long-since
    rotated archive segment would make a live session look stale.

    Carries no in-use flags. That is what makes the result cacheable: it depends
    on the filesystem and on the transcript-to-replay-log pairing, neither of
    which can turn a retired session back into a resumable one.
    """
    if cached:
        hit = _cached_scan(sid_for_stem)
        if hit is not None:
            return hit
    result = _scan_raw_uncached(sid_for_stem)
    if cached:
        _store_scan(sid_for_stem, result)
    return result


def _scan_raw_uncached(sid_for_stem: Mapping[str, str]) -> list[_RawUnit]:
    sizes: dict[str, int] = {}
    mtimes: dict[str, float] = {}
    sids: dict[str, str] = {}
    stems: dict[str, list[str]] = {}
    sid_for_stem = dict(sid_for_stem)

    def record(uid: str, size: int, mtime: float) -> None:
        sizes[uid] = sizes.get(uid, 0) + size
        mtimes[uid] = max(mtimes.get(uid, 0.0), mtime)

    def add_stem(uid: str, stem: str) -> None:
        owned = stems.setdefault(uid, [])
        if stem and stem not in owned:
            owned.append(stem)

    # kiro-cli half. A paired session is keyed on its sid so both halves land on
    # the same unit.
    try:
        with os.scandir(kiro_sessions_dir()) as it:
            for entry in it:
                if not entry.name.endswith(_CLI_SUFFIXES):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                sid = entry.name.rsplit(".", 1)[0]
                if not _UNIT_ID_RE.match(sid):
                    continue
                sids[sid] = sid
                stems.setdefault(sid, [])
                record(sid, st.st_size, st.st_mtime)
    except OSError:
        logger.debug("kiro-cli session store unreadable", exc_info=True)

    archives = _archive_index()

    def attribute(stem: str) -> str:
        """The unit a transcript stem belongs to: its paired sid, else itself."""
        paired = sid_for_stem.get(stem, "")
        return paired if paired in sids else stem

    # Transcript half, plus any rotated segments.
    try:
        with os.scandir(_crew_sessions_dir()) as it:
            for entry in it:
                if not entry.name.endswith(_TRANSCRIPT_SUFFIX):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                stem = entry.name[: -len(_TRANSCRIPT_SUFFIX)]
                if not _UNIT_ID_RE.match(stem):
                    continue
                uid = attribute(stem)
                add_stem(uid, stem)
                sids.setdefault(uid, sid_for_stem.get(stem, "") if uid != stem else "")
                record(uid, st.st_size, st.st_mtime)
    except OSError:
        logger.debug("transcript store unreadable", exc_info=True)

    for stem, segments in archives.items():
        uid = attribute(stem)
        if uid not in sizes and uid not in stems:
            # Segments outliving their transcript still cost space and still
            # belong to a session, so they form a unit of their own.
            sids.setdefault(uid, "")
        add_stem(uid, stem)
        for _path, size, mtime in segments:
            record(uid, size, mtime)

    return [
        _RawUnit(
            uid=uid,
            sid=sids.get(uid, ""),
            stems=tuple(stems.get(uid, [])),
            bytes=size,
            mtime=mtimes.get(uid, 0.0),
        )
        for uid, size in sizes.items()
    ]


def _cli_index() -> dict[str, list[Path]]:
    """Group every file in the kiro-cli store by the session id it belongs to.

    Enumerated rather than assumed. A session is *identified* by its ``.json`` /
    ``.jsonl`` pair, but reclaiming it must take whatever else kiro-cli has
    written alongside — a lock file today, a sidecar a future version adds. If
    reclaiming moved only the two suffixes it knows, an unrecognised sidecar would
    be left behind, which is exactly the partial-session removal this module
    exists to prevent.

    Built once per operation: globbing per session would be quadratic across a
    store that reaches six figures of files.
    """
    index: dict[str, list[Path]] = {}
    try:
        with os.scandir(kiro_sessions_dir()) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                sid = entry.name.rsplit(".", 1)[0]
                if not _UNIT_ID_RE.match(sid):
                    continue
                index.setdefault(sid, []).append(Path(entry.path))
    except OSError:
        pass
    return index


def _unit_paths(
    sid: str,
    stems: tuple[str, ...],
    archives: dict[str, list[tuple[Path, int, float]]] | None = None,
    cli_files: dict[str, list[Path]] | None = None,
) -> list[tuple[Path, str]]:
    """Every file a session owns, as (absolute path, staged relative path).

    The single answer to "what is this session made of". Move, restore and delete
    all resolve through here so none of them can operate on half a session.
    """
    found: list[tuple[Path, str]] = []
    if sid:
        owned = cli_files.get(sid, []) if cli_files is not None else _cli_index().get(sid, [])
        for path in owned:
            found.append((path, f"{STAGE_CLI_LEAF}/{path.name}"))
    for stem in stems:
        transcript = _crew_sessions_dir() / f"{stem}{_TRANSCRIPT_SUFFIX}"
        if transcript.is_file():
            found.append((transcript, f"{STAGE_CREW_LEAF}/{transcript.name}"))
        segments = (
            archives.get(stem, []) if archives is not None else _archive_index().get(stem, [])
        )
        for path, _size, _mtime in segments:
            found.append((path, f"{STAGE_CREW_LEAF}/{ARCHIVE_DIR_NAME}/{path.name}"))
    return found


def _bucketize(reclaimable: list[SessionUnit], now: float) -> tuple[StorageBucket, ...]:
    """Split reclaimable sessions into the age bands the UI offers as thresholds."""
    edges = list(BUCKET_DAYS)
    labels = [f"under_{edges[0]}d"]
    labels += [f"{edges[i]}_{edges[i + 1]}d" for i in range(len(edges) - 1)]
    labels.append(f"over_{edges[-1]}d")
    counts = [0] * len(labels)
    byte_totals = [0] * len(labels)
    for unit in reclaimable:
        age = unit.age_days(now)
        position = len(edges)
        for i, edge in enumerate(edges):
            if age < edge:
                position = i
                break
        counts[position] += 1
        byte_totals[position] += unit.bytes
    return tuple(
        StorageBucket(label=label, sessions=counts[i], bytes=byte_totals[i])
        for i, label in enumerate(labels)
    )


def _same_filesystem(a: Path, b: Path) -> bool:
    """Whether a rename between *a* and *b* can avoid a copy.

    Walks up to each path's nearest existing ancestor, because the trash root does
    not exist before the first reclaim.
    """

    def device(path: Path) -> int | None:
        for candidate in (path, *path.parents):
            try:
                return candidate.stat().st_dev
            except OSError:
                continue
        return None

    dev_a = device(a)
    dev_b = device(b)
    return dev_a is not None and dev_a == dev_b


def measure(
    index: SessionIndex,
    *,
    now: float | None = None,
    units: list[SessionUnit] | None = None,
    batches: list[TrashBatch] | None = None,
) -> StorageReport:
    """Measure session storage and report what is reclaimable.

    *units* lets a caller that already enumerated the stores hand that result over
    rather than paying for a second pass. The inventory screen needs both the rows
    and these totals, and they must describe the same instant anyway — computing
    them from one pass makes agreement structural instead of coincidental.

    *batches* is the same bargain for the trash, with one sharpening: unlike the
    units fallback, which is answered from a 30s scan cache, ``list_trash`` reads
    every batch manifest uncached on each call — so a caller that already has the
    list should always hand it over, and a payload that lists the batches beside
    these totals must not let the two describe different instants.
    """
    clock = time.time() if now is None else now
    units = _scan_units(index, cached=True) if units is None else units
    active = [u for u in units if u.active]
    # Sub-floor sessions are neither active nor offered: reporting them as
    # reclaimable would promise bytes no threshold can actually move.
    reclaimable = [u for u in units if not u.active and u.age_days(clock) >= MIN_RECLAIM_AGE_DAYS]
    batches = list_trash() if batches is None else batches
    report = StorageReport(
        total_bytes=sum(u.bytes for u in units),
        total_sessions=len(units),
        active_sessions=len(active),
        active_bytes=sum(u.bytes for u in active),
        buckets=_bucketize(reclaimable, clock),
        reclaimable_sessions=len(reclaimable),
        reclaimable_bytes=sum(u.bytes for u in reclaimable),
        trash_bytes=sum(b.bytes for b in batches),
        trash_batches=len(batches),
        trash_same_filesystem=_same_filesystem(kiro_sessions_dir(), trash_root()),
        reclaim_blocked_reason=reclaim_block_reason(cached=True),
    )
    # The per-store split stays out of the report but is the first thing worth
    # knowing when a total looks wrong.
    logger.debug(
        "session storage: %d sessions, %d paired, %d replay-only, %d transcript-only",
        len(units),
        sum(1 for u in units if u.sid and u.stems),
        sum(1 for u in units if u.sid and not u.stems),
        sum(1 for u in units if u.stems and not u.sid),
    )
    return report


def list_units(index: SessionIndex, *, cached: bool = True) -> list[SessionUnit]:
    """Every session on disk as one unit each, with its total size and age.

    The inventory screen's row source. Exposed separately from :func:`measure`
    because that answers "how much in total" and deliberately collapses to
    aggregates, while a list needs the individual sessions back.

    A filesystem scan only — no file CONTENT is read, so this stays usable on a
    six-figure store. Anything requiring a read (a title's metadata line, a first
    message, a turn or image count) is fetched per row instead.

    A recent scan is reused by default: this is a read, and the flags derived
    from *index* are recomputed on every call regardless. The co-tenant
    contribution to ``active`` rides the cached half though, so a caller that
    derives a REFUSAL from the result — the dashboard's trash pre-classification
    — must pass ``cached=False`` and pay for a fresh pass instead.
    """
    return _scan_units(index, cached=cached)


def select_reclaimable(
    index: SessionIndex,
    older_than_days: float,
    *,
    now: float | None = None,
) -> list[SessionUnit]:
    """The sessions a reclaim at *older_than_days* would move.

    Separate from :func:`move_to_trash` so a caller can show the exact count and
    size before anything moves, and so the selection is re-derived at the moment
    of the move rather than trusted from a stale UI.
    """
    if older_than_days < 0:
        raise SessionStorageError("older_than_days must not be negative")
    clock = time.time() if now is None else now
    # The caller's threshold can only be MORE conservative than the floor.
    cutoff = max(float(older_than_days), MIN_RECLAIM_AGE_DAYS)
    return [u for u in _scan_units(index) if not u.active and u.age_days(clock) >= cutoff]


def _pod_root() -> Path:
    raw = os.environ.get("KIROCREW_POD_ROOT")
    return Path(raw).expanduser() if raw else Path.home() / ".kirocrew-pods"


def _replay_store_cotenants() -> list[str]:
    """Names of pod instances that may read the same kiro-cli replay store.

    A pod now gets its OWN store (``pod/runtime.py`` exports ``KIRO_HOME`` inside
    the pod home), so a current pod is not a co-tenant at all — its sessions never
    enter the machine-wide store. What remains is a pod directory left by a build
    from before that export existed: those pods did share the store, and a
    surviving map still names sessions in it.

    So this returns CANDIDATES, and :func:`cotenant_sids` decides by reading each
    one's map. The pod root is host-side state at a known location, which is what
    makes even that much discoverable. A dev gateway pointed at some other
    ``KIROCREW_HOME`` cannot be, and stays a documented limitation.
    """
    try:
        entries = list(_pod_root().iterdir())
    except OSError:
        return []
    return sorted(
        entry.name for entry in entries if entry.is_dir() and not entry.name.startswith(".")
    )


_cotenant_cache_lock = threading.Lock()
# (expires_at, cache key, (protected sids, refusals))
_cotenant_cache: (
    tuple[float, tuple[object, ...], tuple[frozenset[str], tuple[tuple[str, str], ...]]] | None
) = None


def _cotenant_key() -> tuple[object, ...]:
    """Everything a cached co-tenant pass depends on besides the maps themselves.

    The pod root is part of it, for the same reason the store locations are part
    of :func:`_scan_key`: it is resolved per call (``KIROCREW_POD_ROOT`` overrides
    it), so a process can legitimately answer for different roots over its
    lifetime, and a key without the location would answer a question about one
    root with a pass taken over another.

    The home overrides are part of it because the answer is not a function of the
    pod root alone: each map is read through ``hooks.safe_read_file``, whose
    sensitive-path gate re-anchors on ``KIROCREW_HOME`` / ``KIRO_HOME`` — the
    same symlinked map can be refused under one anchoring and readable under
    another, and *refusals* is part of what this cache stores. The RAW env values
    are keyed, not the sanitized ``data_home()``/``kiro_home()`` forms, because
    the gate reads them raw with no validity check: an unsafe override and an
    unset one anchor differently even though both sanitize to the default. Of the
    two, the read tier re-anchors on ``KIROCREW_HOME``; ``KIRO_HOME`` reaches
    only the write tier today and is kept as defensive over-keying — a spurious
    miss costs a re-scan, a spurious hit would cost a wrong answer.
    """
    return (
        str(_pod_root()),
        os.environ.get("KIROCREW_HOME"),
        os.environ.get("KIRO_HOME"),
    )


def cotenant_sids(*, cached: bool = False) -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
    """Replay-store sessions a co-tenant instance can still resume.

    Returns ``(sids, refusals)``. The sids are protected exactly like this
    instance's own mapped sessions. *refusals* names the instances that make a
    reclaim unsafe at all, as ``(instance, why)`` — the cases per-session
    protection cannot cover.

    A co-tenant's mapping is not a mystery: it is a file at a known host-side
    path, and reading it turns "some other instance may still want these" into the
    specific list of sessions it wants. The blanket refusal this replaces keyed on
    a pod DIRECTORY existing, which was wrong twice over on a machine that has run
    pods — a current pod keeps its own replay store and so cannot be harmed by a
    reclaim here at all, and a torn-down pod's leftover directory owns nothing.

    Three outcomes, because they need different treatment:

    * **Its own replay store** (``<pod home>/kiro`` exists). It cannot own anything
      in this store, so its sids are recorded but it constrains nothing. This is
      what a pod provisioned by current code looks like.
    * **No own store but a map naming sids** — a genuine shared-store instance.
      Per-session protection is NOT enough for these: they can seed and resume a
      session at any moment, including part-way through a move loop that runs for
      six figures of sessions, so the snapshot this function returns can go stale
      mid-move. Those force a refusal.
    * **Unreadable or malformed map.** Ownership cannot be established, so also a
      refusal.

    A co-tenant with no session map has mapped nothing and protects nothing; that
    is an ordinary state (a pod torn down mid-provision leaves its audit log and
    little else), not a failure to read.

    Liveness is deliberately not the test anywhere here. A stopped instance's map
    still names sessions it would resume if restarted, so ownership — not whether a
    process is running this second — is what protects them.

    *cached* permits a recent pass over the pod maps to be reused, mirroring
    :func:`_scan_raw`'s flag: opt-in per call site, never global. It exists for
    the read paths behind :func:`_scan_units` and display callers of
    :func:`reclaim_block_reason`. The pre-move re-read in
    :func:`move_to_trash` (and :func:`reclaim_block_reason`'s default) must
    never opt in — a stale answer there could let a reclaim proceed against a
    store another instance still holds, which is the exact staleness the
    pre-move re-read exists to close.
    """
    global _cotenant_cache
    if cached:
        with _cotenant_cache_lock:
            if _cotenant_cache is not None:
                expires, key, result = _cotenant_cache
                if time.monotonic() < expires and key == _cotenant_key():
                    return result
    protected: set[str] = set()
    refusals: list[tuple[str, str]] = []
    root = _pod_root()
    for name in _replay_store_cotenants():
        home = root / name
        path = home / SESSION_MAP_FILENAME
        try:
            # Through the centralized gate, never a bare read: the pod root is
            # writable, so a session_map.json replaced with a symlink would
            # otherwise make the gateway read whatever it points at.
            # ``safe_read_file`` resolves the link, re-checks the RESOLVED target
            # against ``is_sensitive_path``, and opens with ``O_NOFOLLOW``.
            raw = hooks.safe_read_file(str(path))
        except FileNotFoundError:
            continue
        except (PermissionError, OSError, UnicodeDecodeError):
            # Refused as sensitive, lost a symlink race, genuinely unreadable, or
            # not valid UTF-8 — ``safe_read_file`` decodes, and
            # ``UnicodeDecodeError`` is a ``ValueError``, so it would otherwise
            # escape past the parse guard below and reach the caller. All four
            # mean the same thing here — which sessions this instance claims
            # cannot be established — so fail closed on it.
            # %r, not %s: the directory name is agent-influenced and passes no
            # identifier gate, so a newline in it would forge a second record.
            logger.warning("co-tenant %r has an unreadable session map", name, exc_info=True)
            refusals.append((name, "its session map could not be read"))
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("co-tenant %r has a malformed session map", name)
            refusals.append((name, "its session map could not be parsed"))
            continue
        if not isinstance(data, dict):
            refusals.append((name, "its session map is not an object"))
            continue

        claimed: set[str] = set()
        for entry in data.values():
            # A plain string is the LEGACY entry format, and
            # ``SessionMap._load`` still migrates it to ``{"sid": ...}`` on read.
            # Skipping it would fail OPEN on exactly the population this function
            # exists for — a co-tenant old enough to predate the pod store split
            # is also old enough to have been written in that format.
            if isinstance(entry, str):
                if entry and _UNIT_ID_RE.match(entry):
                    claimed.add(entry)
                continue
            if not isinstance(entry, dict):
                continue
            # Both the live sid and a discarded one. A discarded sid is a session
            # the co-tenant has stopped resuming but still remembers, and taking
            # its files is not this module's decision to make on another
            # instance's behalf.
            for field_name in ("sid", "discarded_sid"):
                value = entry.get(field_name)
                if isinstance(value, str) and value and _UNIT_ID_RE.match(value):
                    claimed.add(value)

        protected |= claimed
        if claimed and not _has_own_replay_store(home):
            refusals.append(
                (name, "it shares this replay store and can resume sessions at any time")
            )
    result = (frozenset(protected), tuple(refusals))
    if cached:
        with _cotenant_cache_lock:
            _cotenant_cache = (time.monotonic() + _SCAN_CACHE_TTL, _cotenant_key(), result)
    return result


def _has_own_replay_store(pod_home: Path) -> bool:
    """Whether a co-tenant reads its own replay store rather than this one.

    Current pods export ``KIRO_HOME`` into the pod home, so their replay logs never
    enter the machine-wide store. The directory is the only observable proxy for
    that from outside the process, and it is read in the fail-CLOSED direction: an
    instance we cannot show to be self-contained is treated as sharing this store,
    which refuses rather than reclaims.
    """
    return (pod_home / KIRO_BASE_DIR_NAME.lstrip(".")).is_dir() or (pod_home / "kiro").is_dir()


def reclaim_block_reason(*, cached: bool = False) -> str:
    """Why this instance must not reclaim, or ``""`` when it may.

    The exclusion set is built from THIS instance's session map, but the kiro-cli
    replay store can be shared by several instances. An instance with its own data
    home reading the machine-wide store cannot see the default instance's
    mappings, so a resumable conversation reads as retired and could be staged and
    then emptied out from under a gateway this process cannot see.

    Decided on RESOLVED paths, never on whether the environment variables are set,
    and by requiring the store to be CONTAINED in this instance's data home rather
    than by testing it against the default location. Both overrides are validated
    and silently fall back to the default when they name an unsafe target, so
    ``KIRO_HOME=/etc`` alongside an isolated ``KIROCREW_HOME`` leaves this process
    reading the shared store while an env-presence test would report it isolated —
    the exact hazard, reached through a rejected override. Containment also covers
    a store that is shared without being the default one: two isolated instances
    pointed at the same custom ``KIRO_HOME`` see neither the default store nor each
    other's maps.

    The freshness floor narrows the window but does not close it — a session idle
    for a day is still resumable — so the operation is refused rather than
    attempted. Isolating both homes together, or neither, is safe.

    Pods are handled per session rather than per instance, because their mappings
    ARE discoverable: see :func:`cotenant_sids`. Only a pod whose map cannot be
    read still costs the whole instance its ability to reclaim.

    *cached* permits reusing a recent pass over co-tenant pod mappings,
    mirroring :func:`cotenant_sids`'s flag: opt-in per call site, never global.
    It is passed by display aggregators like :func:`measure` to avoid paying an
    extra uncached scan on top of :func:`list_units`. Mutation paths
    (like :func:`_move_to_trash_locked`) keep the default ``cached=False`` so the
    destructive operation always re-evaluates the authoritative state in real
    time.
    """

    def _norm(path: Path) -> Path:
        # A symlinked HOME would otherwise make the default home compare unequal to
        # itself and report every instance as isolated.
        try:
            return path.resolve()
        except OSError:  # pragma: no cover - defensive
            return path

    home = _norm(Path.home())
    # BOTH of these are defaults, not isolation: an install that has not yet
    # migrated legitimately reports the legacy home, and treating that as an
    # isolated instance would refuse every pre-migration install.
    defaults = {home / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF, _norm(legacy_home())}
    data = _norm(data_home())
    if data in defaults:
        # The mirror of the isolated-instance case, and just as destructive: a pod
        # shares this store while keeping its own map, so ITS sessions read as
        # retired from here. Only checked when the store is the default one, since
        # that is the only store a pod reads.
        if _norm(kiro_home()) == home / KIRO_BASE_DIR_NAME:
            _protected, refusals = cotenant_sids(cached=cached)
            if refusals:
                # !r, not plain interpolation: the directory name is
                # agent-influenced and passes no identifier gate, so a newline
                # or ANSI payload in it would forge a second record the moment
                # a caller logs this text (the #6281/#6371 forgery class).
                listed = "; ".join(f"{name!r} — {why}" for name, why in refusals[:3])
                return (
                    f"{len(refusals)} other instance(s) sharing this kiro-cli session "
                    f"store make reclaiming unsafe ({listed}). Evict them with "
                    "`kirocrew pod down <name>` to reclaim from here."
                )
        return ""
    # An isolated instance may reclaim only when its replay store is provably its
    # own. Testing against the DEFAULT store location would miss the sharing that
    # matters most: two isolated instances pointed at one custom KIRO_HOME see
    # neither the default store nor each other's maps. Requiring the store to live
    # inside this instance's data home fails closed on every such arrangement.
    store = _norm(kiro_home())
    if store == data or data in store.parents:
        return ""
    return (
        "This instance has its own data home but its kiro-cli session store sits "
        "outside it, so the store may be shared with instances whose sessions this "
        "one cannot see. Point KIRO_HOME inside KIROCREW_HOME to reclaim from here."
    )


@contextmanager
def _mutation_lock() -> Iterator[None]:
    """Serialize every operation that moves or deletes staged files.

    Two concurrent reclaims can select the same session and interleave their
    moves, landing one half in each batch — after which neither batch can restore
    it, and emptying either destroys half a session. A file lock rather than a
    thread lock because instances share the kiro-cli store: a pod and the live
    gateway both read ``~/.kiro/sessions/cli``, so in-process exclusion excludes
    nothing.

    :func:`platform_compat.file_lock` fails closed, so a lock that cannot be taken
    raises instead of entering the section unserialized.
    """
    lock_path = trash_root().parent / MUTATION_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with platform_compat.file_lock(fd, exclusive=True, required=True):
            yield
    finally:
        os.close(fd)


def _batch_dir(batch_id: str) -> Path:
    """Resolve a batch id to its directory, refusing anything that is not one.

    The id itself cannot traverse — it is pattern-checked — but a **link** planted
    under the trash root can, and its name would pass that check. Following one
    would let a crafted manifest drive moves into or out of paths this module does
    not own, in either direction. A batch is a real directory inside the trash root
    or it is not a batch, so both the link check and the containment check happen
    here, at the one place every caller resolves an id through.

    The link test goes through :func:`platform_compat.is_link_or_junction` because
    ``is_symlink()`` reports False for an NTFS **junction**: on Windows a junction
    named as a valid batch id would read as a real directory, and the delete would
    resolve through it to whatever batch it points at.

    Containment is anchored to the DATA HOME, not to the trash root alone. The trash
    root lives under a directory the agent can write, so it could itself be replaced
    with a link: resolving relative to it would then accept batches under whatever it
    points at, and "empty everything" would recurse into directories this module
    does not own.

    The root must also not BE a link. Anchoring alone is not enough, because a link
    can point at another directory *inside* the data home — the live sessions or
    archive tree — which satisfies both the anchor and containment while making
    `empty` delete real session data.
    """
    if not _UNIT_ID_RE.match(batch_id):
        raise SessionStorageError(f"not a valid batch id: {batch_id!r}")
    root = trash_root()
    if platform_compat.is_link_or_junction(root):
        raise SessionStorageError(f"the trash root is a link: {root}")
    path = root / batch_id
    if platform_compat.is_link_or_junction(path):
        raise SessionStorageError(f"not a valid batch id: {batch_id!r}")
    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
        home = data_home().resolve()
    except OSError as exc:  # pragma: no cover - defensive
        raise SessionStorageError(f"not a valid batch id: {batch_id!r}") from exc
    if home not in resolved_root.parents:
        raise SessionStorageError(f"the trash root does not live under the data home: {root}")
    if resolved_root not in resolved.parents:
        raise SessionStorageError(f"not a valid batch id: {batch_id!r}")
    return path


def _open_source_no_follow(src: Path) -> IO[bytes]:
    """Open *src* for reading, refusing to traverse a symlink in its final component.

    Both movers' COPY fallbacks read through this; their fast paths neither need it nor
    have the hole. ``os.rename`` moves a symlink rather than what it points at, and
    ``os.link`` links to the link itself — only a copy dereferences. Without this the
    same mover would mean two different things depending on whether source and
    destination happen to share a filesystem, and the cross-device meaning is the
    dangerous one in both directions: an indexed session file swapped for a link to a
    credential store would have the SECRET's bytes copied into a trash the agent can
    read, and a link planted in the trash would have arbitrary bytes copied back out
    over a restored origin. This module's threat model already assumes planted files,
    so it has to assume planted links too.

    Refused rather than followed, and not recreated as a link either: a symlink in the
    session store is anomalous, the staging loop already knows how to leave a session
    alone when one of its files will not move, and failing closed is the direction that
    cannot leak.

    The guard covers the FINAL component, which is what the swap it defends against
    replaces. A symlinked parent directory is still traversed; closing that needs an
    ``openat`` walk down the chain and is its own change.
    """
    # O_NOFOLLOW is POSIX and absent on Windows, where creating a symlink needs
    # privilege this model does not hand out. Degrade rather than fail to import.
    return open(src, "rb", opener=lambda path, flags: os.open(path, flags | _O_NOFOLLOW))


def _publish_then_drop(src: Path, dst: Path) -> None:
    """Make a just-created *dst* durable, then remove *src*. Both movers end here.

    One code path for both of :func:`_move_file_exclusive`'s branches, because they have
    the same shape and the same hazard: a hard link and a copy both leave TWO directory
    entries for one file, and removing the source is a SECOND metadata operation. Unlike
    a same-filesystem rename — one atomic operation, where forcing the destination side
    forces the removal with it — nothing orders these two for us. If the unlink reaches
    disk and the new name does not, the inode has no name left at all and the session is
    gone, which is why the destination is synced BEFORE the source is dropped rather than
    alongside it.

    A failing destination sync ATTEMPTS to withdraw *dst* — the unlink is suppressed so
    that it cannot displace the sync error, which is the one that says what went wrong.
    That withdrawal is safe because the SOURCE has not been touched at that point, so
    removing *dst* provably restores the state the call started in. The unlink below is
    different: once it has been attempted, no observation available here proves whether
    the original survived, so nothing is withdrawn there. This function's callers read an
    exception as "the origin was not taken", which the sync case honours exactly. The
    unlink case can leave *dst* behind, and that leftover is deliberate: it is a file no
    manifest entry names, so :func:`_unlisted_files` refuses to delete it and the batch
    cannot be emptied until an operator clears it. That is a worse report than the truth
    but a recoverable one, and it is chosen over withdrawing a copy that may be the last
    one. Only the calling move created *dst*, so keeping it costs nothing that predates
    the call either.

    The source-side sync is ``best_effort`` and runs after the unlink, where nothing may
    raise: the move is complete by then, so an exception could only mislabel finished work
    as failed, and a source entry that is not yet durable can at worst resurrect a
    duplicate — never lose the copy now at *dst*.
    """
    try:
        fsync_dir(dst.parent)
    except OSError:
        with suppress(OSError):
            dst.unlink()
        raise
    # Propagates untouched on failure, leaving *dst* where it is. Withdrawing it would
    # be guessing: a filesystem can report a failure for an unlink that DID commit (a
    # network filesystem losing the reply to a completed operation), and the source name
    # existing afterwards does not prove the original still does — a concurrent append
    # recreates that name with a NEW, empty file, at which point *dst* holds the only
    # copy of the history and deleting it destroys exactly what this module exists to
    # keep. Nothing available here separates those cases, so the leftover is accepted:
    # it stays in the batch as a file no manifest entry names, which _unlisted_files()
    # refuses to delete, and it blocks emptying that batch until an operator clears it.
    # A wedge an operator can undo beats bytes nobody can get back.
    src.unlink()
    fsync_dir(src.parent, best_effort=True)


def _move_file_exclusive(src: Path, dst: Path) -> bool:
    """Move *src* to *dst*, never replacing an existing *dst*. False if occupied.

    Restore's preflight checks that the origin is free, but the session can be
    recreated in the interval before the move — and ``os.rename`` replaces the
    destination silently, so undoing a deletion would destroy the newer data it was
    meant to protect. Creating the destination exclusively makes the check and the
    write one atomic step, so a lost race is reported rather than acted on.

    Both branches finish through :func:`_publish_then_drop`, which is where the
    durability ordering lives.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        return False
    except OSError:
        # A different filesystem, or one without hard links: copy into a file that
        # must not already exist, which keeps the no-clobber guarantee.
        try:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as out, _open_source_no_follow(src) as handle:
                shutil.copyfileobj(handle, out)
                # Before the unlink, not after: this is the moment the copy stops
                # being a second copy. Buffered bytes live in this process and then
                # in the page cache, so without forcing them out a power-off between
                # the last write and the unlink reaching disk comes back to an
                # empty-or-partial destination and no source — the one outcome a MOVE
                # of the only copy must never produce.
                #
                # copystat first so the fsync forces the metadata with the bytes.
                out.flush()
                shutil.copystat(src, dst)
                os.fsync(out.fileno())
        except OSError:
            with suppress(OSError):
                dst.unlink()
            raise
        _publish_then_drop(src, dst)
        return True
    _publish_then_drop(src, dst)
    return True


def _move_file(src: Path, dst: Path) -> None:
    """Move one file, preferring a rename and falling back to a copy.

    A rename is atomic and instant, which is what makes a trash usable at tens of
    gigabytes. It only works within one filesystem, so a data home mounted apart
    from the kiro-cli store falls back to a copy — slow, and it needs the space
    twice while it runs.

    That fallback is NOT ``shutil.move``. ``shutil.move`` copies straight to the
    final name and then unlinks the source, so it spends the whole copy with a
    partial file sitting under the name the manifest will record: an exit there
    leaves a staged file that looks complete and silently is not. Copy into a
    private incoming name instead, force the bytes out, and publish with a rename —
    then the destination name only ever exists whole, and the source is not
    unlinked until it is durable.
    """
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if getattr(exc, "errno", None) != getattr(os, "EXDEV", 18):
            raise
    _copy_across_filesystems(src, dst)


def _copy_across_filesystems(src: Path, dst: Path) -> None:
    """Copy *src* to *dst* durably and atomically, then remove *src*.

    The incoming file is created in ``dst.parent`` because the publishing step is a
    rename, and a rename is only atomic within one filesystem — the destination's
    own directory is the only place that is guaranteed to be. It carries
    :data:`INCOMING_PREFIX` so an operator who finds one after a crash can see what it
    is. That is ALL the prefix does: :func:`_unlisted_files` deliberately exempts
    nothing by name, so an interrupted copy stays protected as unlisted data rather
    than being deleted as scratch. Erring that way is the safe direction — a name says
    nothing about who wrote it, and this module already assumes planted files — at the
    documented cost that such a leftover blocks its batch from being emptied until an
    operator removes it.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(dst.parent), prefix=INCOMING_PREFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, _open_source_no_follow(src) as handle:
            shutil.copyfileobj(handle, out)
            out.flush()
            # On the temp, not on dst: dst does not exist yet, and the point is that
            # the name appears already carrying the source's mode and timestamps. The
            # mtime in particular is what the rest of this module ages sessions by.
            #
            # BEFORE the fsync, because copystat writes the inode and the fsync is
            # what forces the inode out. After it, a power loss could publish the
            # bytes under the temp's own 0600 and its creation time instead.
            shutil.copystat(src, tmp)
            os.fsync(out.fileno())
        replace_with_retry(tmp, dst)
    except BaseException:
        # BaseException, matching atomic_write: a Ctrl-C mid-copy must not leave the
        # incoming file behind either. The exception itself propagates untouched.
        with suppress(OSError):
            tmp.unlink()
        raise
    # Publish, then make the publication durable, and only then drop the source.
    # Reversing the last two would be the same lost-only-copy window the exclusive
    # mover guards against.
    #
    # A failing sync takes the published name back out. The caller reads an exception
    # as "this file did not move", and it is only entitled to read it that way if
    # nothing of the move survives: leaving dst behind would put a file in the batch
    # that no manifest entry names, which blocks emptying that batch for good. The
    # source has not been touched yet, so unlinking dst restores the state this call
    # started in exactly.
    try:
        fsync_dir(dst.parent)
    except OSError:
        with suppress(OSError):
            dst.unlink()
        raise
    # Propagates untouched on failure, leaving *dst* in the batch: see
    # :func:`_publish_then_drop` for why withdrawing it would be a guess that can
    # destroy the only remaining copy.
    src.unlink()
    # Nothing raising may follow the unlink. The move is COMPLETE here, and this
    # function's caller reads an exception as "this file did not move" — it rolls the
    # session back and omits the file from the manifest, which for a file that HAS
    # moved is a half-staged session that nothing records: the exact split the loop's
    # rollback exists to prevent. The source directory still wants syncing (see
    # :func:`_sync_batch`, which does it once per batch), so the caller collects it
    # rather than paying a per-file sync here.


def _file_stamp(path: Path) -> tuple[int, float]:
    """The size and last-modified time of a file about to be staged.

    Raises ``OSError`` when unreadable. A named operation rather than an inline
    ``stat`` because both readings are load-bearing and both come from ONE call:
    a file whose size cannot be read cannot be recorded, and an unrecorded file
    is one restore cannot put back; the mtime is what tells the move loop the
    file has been touched since this batch was validated (see
    :func:`_move_to_trash_locked`). Reading them separately would be two stats
    describing two instants, which is the class of bug the caller is guarding
    against.
    """
    info = path.stat()
    return info.st_size, info.st_mtime


def _rollback(moved: list[tuple[Path, Path]]) -> bool:
    """Undo a partial session move, best effort. True if everything went back.

    Each pair is (where it landed, where it came from). A rollback that itself
    fails is logged and nothing more: the alternative is raising over a caller
    already handling a failure, which would abandon the rest of the batch too.
    The return value exists for the one caller that makes a POSITIVE claim about
    the outcome — "this session was left in place" is only true if every file
    actually went back — and callers that merely omit a session can ignore it.

    The move is EXCLUSIVE. A rollback runs after something already went wrong, so
    the origin may have been recreated in the meantime — and a plain rename would
    replace that newer session data with the copy being put back, turning a handled
    failure into data loss. An occupied origin leaves the file where it is staged,
    which keeps it recoverable, and is reported here as an incomplete rollback
    because the staged copy is then in the batch without being in its manifest.
    """
    complete = True
    for landed, origin in reversed(moved):
        try:
            if not _move_file_exclusive(landed, origin):
                logger.warning(
                    "not rolling %s back: %s was recreated and is newer",
                    landed,
                    origin,
                )
                complete = False
        except OSError:
            logger.warning("could not roll %s back to %s", landed, origin, exc_info=True)
            complete = False
    return complete


def _staged_path(batch: Path, rel: str) -> Path | None:
    """Resolve a manifest ``rel`` inside *batch*, or ``None`` if it escapes.

    ``Path("/a/b") / "/etc/passwd"`` is ``/etc/passwd`` — joining an absolute
    string discards the base entirely. The manifest lives under the data home,
    which is agent-writable, so a tampered or corrupted ``rel`` would otherwise
    let restore pick up any file on the host and relocate it. Both the absolute
    form and ``..`` traversal are refused.
    """
    if not rel or Path(rel).is_absolute():
        return None
    candidate = batch / rel
    try:
        resolved = candidate.resolve()
        root = batch.resolve()
    except OSError:
        return None
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return candidate


def _canonical_origin(rel: str) -> Path | None:
    """Where a staged file must have come from, DERIVED from its staged path.

    The staged path already encodes the store and the filename, and the filename
    is the session's identity — a replay log is ``<sid>.jsonl`` and a transcript is
    ``<stem>.jsonl``. So the origin is not information the manifest needs to
    supply, and accepting it would let a tampered in-store origin relocate one
    session's file under another session's name: both paths pass a
    "inside a session store" test, and the result is a corrupted session with no
    error a user could connect to a restore.

    Deriving removes the choice. The recorded origin is then only checked for
    agreement, so a disagreement is refused instead of followed.
    """
    parts = PurePosixPath(rel).parts
    if not parts:
        return None
    name = parts[-1]
    if not name or name != Path(name).name or name in (os.curdir, os.pardir):
        return None
    if parts[0] == STAGE_CLI_LEAF and len(parts) == 2:
        return kiro_sessions_dir() / name
    if parts[0] == STAGE_CREW_LEAF:
        if len(parts) == 2:
            return _crew_sessions_dir() / name
        if len(parts) == 3 and parts[1] == ARCHIVE_DIR_NAME:
            return _crew_archive_dir() / name
    return None


def _origin_path(origin: str) -> Path | None:
    """Accept an *origin* only if it names a file inside a session store.

    Restore writes to this path, so an unconstrained origin from a tampered
    manifest is an arbitrary-write primitive. Only the two directories this module
    ever reclaims from are permitted; anything else — a credential file, a config,
    a path outside the homes entirely — is refused.
    """
    if not origin:
        return None
    candidate = Path(origin)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve()
        allowed = [
            kiro_sessions_dir().resolve(),
            _crew_sessions_dir().resolve(),
            _crew_archive_dir().resolve(),
        ]
    except OSError:
        return None
    if not any(resolved.is_relative_to(root) for root in allowed):
        return None
    return candidate


def _unlisted_files(batch: Path) -> list[Path]:
    """Staged files no manifest entry names.

    A process exit between moving a file into a batch and appending its manifest
    line leaves a file nothing points at. It is still the ONLY copy of that
    session's data, and the user was never shown it — the batch may even list zero
    sessions — so no cleanup path may remove it.

    An unreadable manifest yields every file, which is the safe direction: without
    the manifest nothing in the batch is accounted for.

    There is deliberately NO exemption for the incoming name
    :func:`_copy_across_filesystems` writes, even though a crash can leave one here and
    a caller then cannot empty the batch until an operator removes it. A name is not
    proof of who wrote it: everything else in this module's threat model assumes a
    batch can be planted in (a linked manifest, a file substituted at the manifest's
    name), so anything that may rename a file inside a batch could pick that prefix and
    have this scan pass over a staged file the manifest never approved for deletion.
    Blocking a deletion that should proceed is recoverable; letting one through is not.

    A scan that cannot be completed RAISES rather than returning a short list. This
    function exists to block deletions, so an empty result must mean "there is
    nothing unaccounted for", never "the walk gave up early" — ``rglob`` skips
    unreadable directories silently, which would turn a transient error into
    permission to delete a file that is the only copy of a session.
    """
    # An unreadable manifest yields no listed names, so every file below counts as
    # unlisted — the safe direction this docstring describes.
    listed: set[str] = set(_manifest_rels(batch))
    failures: list[OSError] = []
    unlisted = []
    for root, _dirs, names in os.walk(batch, onerror=failures.append):
        for name in names:
            path = Path(root) / name
            if name == MANIFEST_NAME:
                continue
            try:
                if not path.is_file():
                    continue
            except OSError as exc:
                failures.append(exc)
                continue
            if path.relative_to(batch).as_posix() not in listed:
                unlisted.append(path)
    if failures:
        raise SessionStorageError(
            f"could not read all of {batch.name!r}, so it is not known whether it "
            f"holds files nothing lists: {failures[0]}"
        )
    return unlisted


def _identity_of(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` for *path*, or None if it cannot be read.

    Links are not followed: the question is what this NAME holds, never what it points at.
    Taken so a later removal can be bound to the directory the caller was actually looking
    at - see :func:`_remove_emptied_batch` for why a pinned walk alone does not establish
    that.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _discard_restored_batch(batch: Path, expect: tuple[int, int] | None) -> None:
    """Remove a fully-restored batch, unless it holds files nothing listed.

    Deleting the directory wholesale would destroy an interrupted move's staged
    files — on the one path that exists to be safe. Those keep the batch alive
    instead, so a user can still see it and decide.

    *expect* is the batch's identity as the CALLER recorded it, before the restore read
    anything inside it. It is not re-taken here on purpose: this function is reached only
    after the restore has read the manifest and moved every listed file out, so an identity
    sampled at this point would already be a substitute's if an ancestor had been swapped
    during any of that. Taken at the top of the operation instead, the same comparison
    refuses a swap that landed at ANY point in it. ``None`` means the caller could not read
    it, which refuses.

    NOTHING here writes, and a batch this declines to remove stays exactly as it was found -
    including its manifest, whose entries all describe files the restore has already moved
    back OUT. That stale listing is a decided residual, not an oversight: clearing it would
    mean writing to a path that a refusal has just given evidence about - a swapped ancestor,
    an identity that could not be read - and ``atomic_write`` REPLACES its destination, so the
    tidying would land wherever the path now leads. See the caller for why the write is not
    hoisted above this call either. `empty_trash` clears the batch on the user's own say-so.
    """
    try:
        leftovers = _unlisted_files(batch)
    except SessionStorageError as exc:
        # Not knowing is treated exactly like finding leftovers: keep the batch.
        logger.warning("keeping trash batch %r: %s", batch.name, exc)
        return
    if leftovers:
        logger.warning(
            "keeping trash batch %r: %d staged file(s) are absent from its manifest, "
            "so removing it would delete the only copy",
            batch.name,
            len(leftovers),
        )
        return
    _remove_emptied_batch(batch, "the restored batch", expect=expect)


def _approve_emptied_batch(
    batch: Path,
    root_fd: int,
    tree: pinned_fs.PinnedTree,
) -> str | None:
    """Re-ask "is this the batch, and does it hold anything unlisted" of a DESCRIPTOR.

    The trash-specific half of :func:`_remove_emptied_batch`, passed to ``pinned_fs`` as its
    approval hook. Both questions were already answered by path before the removal was
    reached - which is exactly the answer that cannot be relied on, because resolving the
    batch's parent FOLLOWS an ancestor that is already a symbolic link. Pinning the walk
    then reaches the link's target faithfully, and the removal would empty that.

    So the two questions are asked again here, of the descriptor the removal will actually
    address, from ONE read of ONE file:

    * the pinned root must BE the directory the caller was looking at, compared by
      ``(st_dev, st_ino)`` against an identity the caller captured before its own by-path
      read - at the batch's creation on one path, before the leftover scan on the other.
      This is the check that makes the rest sound. Without it the only thing standing
      between a swapped ancestor and a deletion is whether the directory the link points at
      can produce a convincing manifest, and a tree an actor can write to can: they plant a
      header naming this batch and a listing covering its contents, and every other question
      here answers yes about the wrong directory. An inode cannot be forged by writing files.
    * the manifest is opened through ``root_fd``, its inode is checked on the OPEN handle
      against the one the scan recorded, and its header must claim THIS batch's
      directory name. The header is the link back to the caller's own selection: a directory
      substituted for the batch brings its own contents, and a directory that is not a
      trash batch at all brings none - neither can produce a header naming this name.
      :func:`_header_names_this_batch` is the same check the approval on the delete path
      makes, for the same reason.
    * NOTHING but the manifest may remain. Not "nothing unlisted" - nothing at all. Both
      callers reach here only when the batch should already be empty of files: the restore
      path has MOVED every listed file back out, and the staging path has a manifest with no
      entries at all. So a file at a listed path is not the listed file; it is something that
      arrived at that name afterwards, and it may be the only copy of whatever it is.
      Consulting the listing here would authorise deleting exactly that, on the strength of a
      name the manifest happens to mention. The stricter rule also needs no listing, which is
      why the entries this read produced are discarded.

    ONE read, not two, and that is the load-bearing part rather than an efficiency note.
    Asking :func:`_summarize_manifest` for the header and :func:`_manifest_rels` for a listing
    opens the file TWICE, and a manifest replaced in between passes the header check on the
    first file while the second answers for another. The header now comes from one handle
    whose inode was verified on that same handle, so there is no interval for a replacement to
    land in.

    Returns None to allow the removal, or a reason code to withhold it. The identity
    comparison against the caller's recorded inode happens BEFORE this, in
    :func:`_remove_emptied_batch`'s own hook -- it owns that answer because the caller also
    needs to know whether the path was verified, and a reason code cannot carry that.

    Links are refused too, not exempted. A symbolic link holds no data of its own, which is
    why the delete path removes scanned ones - but it is also the only entry a removal can
    take by NAME, with a scanned-inode check and an unlink that are two separate syscalls.
    Admitting one is admitting that window, so a batch holding a link is kept and left for
    `empty_trash`, which removes links through its own approval when a user asks for the
    empty.
    """
    scanned_ino = tree.files.get((MANIFEST_NAME,))
    if scanned_ino is None:
        # Nothing here vouches for this being the selected batch, and `list_trash` would not
        # offer a batch with no manifest either.
        logger.warning("keeping trash batch %r: it has no manifest to identify it", batch.name)
        return SKIP_UNREADABLE
    try:
        parsed = _read_manifest(batch, dir_fd=root_fd, expect_ino=scanned_ino)
    except UnicodeDecodeError:
        # `_read_manifest` reports an unreadable manifest as None, but it DECODES strictly,
        # so bytes that are not UTF-8 raise instead - and a ValueError, which neither its own
        # `except OSError` nor anything between here and the caller catches. Reached when the
        # file is corrupted after the by-path pre-screen read it, which is the very race this
        # pinned re-read exists for. A manifest that cannot be decoded cannot identify the
        # batch, so it withholds the removal like any other unreadable one instead of
        # aborting a restore that has already moved the user's files back.
        logger.warning("keeping trash batch %r: its manifest is not valid UTF-8", batch.name)
        return SKIP_UNREADABLE
    if parsed is None:
        logger.warning(
            "keeping trash batch %r: its manifest is not readable as the file that was scanned",
            batch.name,
        )
        return SKIP_UNREADABLE
    header, _entries = parsed
    # `_header_names_this_batch` takes a `_summarize_manifest` triple; it consults only the
    # header, and handing it the one this read already produced is what keeps this to one
    # open.
    if not _header_names_this_batch((header, 0, 0), batch.name):
        return SKIP_IDENTITY_CHANGED
    for key in tree.files:
        if key == (MANIFEST_NAME,):
            continue
        logger.warning(
            "keeping trash batch %r: it still holds %r, which nothing here may remove",
            batch.name,
            "/".join(key),
        )
        return SKIP_UNLISTED_FILES
    if tree.links:
        # A link is the ONE entry the removal takes by name: `unlink_verified` compares the
        # scanned inode and then unlinks, and POSIX offers no "unlink if this inode", so those
        # are two syscalls. Admitting a link therefore hands an actor a lever - plant one, let
        # this approval pass it, then put a file at that name in the window and the removal
        # deletes the file instead of the link. A file is refused above for the same reason and
        # a link is not different enough to treat differently here.
        #
        # It costs nothing that matters: neither of these two removals was asked for, and a
        # batch kept for this reason is still cleared by `empty_trash` on the user's own
        # say-so, which removes scanned links through its own approval.
        logger.warning(
            "keeping trash batch %r: it holds %d link(s), which the removal could only take "
            "by name",
            batch.name,
            len(tree.links),
        )
        return SKIP_UNLISTED_FILES
    return None


def _remove_emptied_batch(batch: Path, what: str, *, expect: tuple[int, int] | None) -> bool:
    """Remove a batch that holds nothing worth keeping, by descriptor rather than by name.

    Both callers reach this having already established that the batch holds no file the
    manifest does not list - a fully restored batch, and one no session was staged into.
    What is left is the removal, and it used to be ``shutil.rmtree(batch)``: a PATH, which
    the kernel re-resolves component by component. The trash root and the directories above
    it are writable by the same user, which in this product includes an agent, so one of
    them swapped to a symbolic link after the caller's own read is followed and the removal
    lands wherever the link points. Neither caller holds the mutation lock across that
    window, and the checks above them are all by path, so nothing else closed it.

    :func:`kiro_crew.pinned_fs.remove_tree_pinned` is the same mechanism the delete path
    uses, reached through the same module rather than respelled here: the parent chain is
    pinned one ``openat`` per component, the batch is opened through it with
    ``O_NOFOLLOW``, and every removal inside addresses a descriptor whose inode a scan
    already recorded.

    Pinning is only half of it, and the weaker half. It closes the window after the path is
    resolved; the swap that lands BEFORE the resolve is followed by ``Path.resolve()``
    itself, and the pinned walk then reaches the wrong tree correctly - faithfully pinned to
    a directory that is not this batch. What closes that is
    :func:`_approve_emptied_batch`, which compares the pinned root against *expect* - an
    identity the CALLER captured before its own by-path read - and then re-asks through the
    same descriptor the two questions the callers asked by path.

    *expect* being None means the caller could not read the batch's identity, and the removal
    is refused: with nothing to bind to, every remaining question would be answered about an
    unknown directory.

    A refusal LEAVES THE BATCH, and that is the safe direction on both paths: the batch is
    still listed, so a user can still see it and act. Reporting a success that did not
    happen is what a bare ``ignore_errors=True`` did. Nothing here writes to the batch on
    the way out either: every refusal below is evidence about the path, and answering it
    with a by-path write would be writing through the very redirection it just caught.

    Returns whether the batch is gone.
    """
    if not _FD_SAFE_DELETE:
        # No ``openat``/``O_NOFOLLOW``, so there is no descriptor to bind the removal to.
        # It goes through the same rename-verify-remove the explicit empty uses on this
        # platform, from the same owner: the batch is renamed aside inside the trash root,
        # its identity is checked THERE, and only the staged name is removed.
        #
        # Refusing outright was the earlier answer here, on the reasoning that nobody asked
        # for these two removals so the safer half of the trade came free. It does not: this
        # branch is the whole of Windows, so refusing leaves a batch behind after EVERY
        # restore and every rolled-back move, still listing the sessions it no longer holds.
        # Five pre-existing tests read that as a failure and so would a user. The residual
        # accepted instead is the one `empty_trash` already accepts here and documents - an
        # actor who can observe the staging name inside the window can redirect the removal
        # through an ancestor swapped afterwards - and it is bounded by an identity check
        # that a same-named impostor cannot pass.
        if expect is None:
            # Nothing to bind to, so the verify half would be answered about an unknown
            # directory and the rename would only move the batch out of sight.
            logger.warning("keeping trash batch %r: its identity was never established", batch.name)
            return False
        staged, skip = _stage_batch_by_name(batch, expect=expect)
        if staged is None:
            logger.warning(
                "keeping trash batch %r: %s was not removed (%s)", batch.name, what, skip
            )
            return False
        try:
            shutil.rmtree(staged)
        except OSError as exc:
            # Back under the name the user saw: a batch left under a staging name is one
            # `list_trash` does not offer, so it would be neither visible nor restorable.
            logger.warning(
                "keeping trash batch %r: %s could not be removed: %s", batch.name, what, exc
            )
            _rename_back(staged, batch)
            return False
        return True
    try:
        # Only the PARENT is resolved, and the name is re-joined onto it. `Path.resolve()`
        # follows the FINAL component too, so a batch directory replaced by a symlink would
        # resolve to its target and every removal below would run there - deleting from
        # outside the trash on the strength of a name inside it. Re-joining the name leaves
        # the last component for the pinned ``O_NOFOLLOW`` open to refuse. This is the same
        # rule :func:`_approve_batch` follows, for the same reason.
        resolved = batch.parent.resolve() / batch.name
    except OSError as exc:
        logger.warning(
            "keeping trash batch %r: its location could not be read: %s", batch.name, exc
        )
        return False

    def _approve(root_fd: int, tree: pinned_fs.PinnedTree) -> str | None:
        if expect is None:
            # Nothing to bind to, so every remaining question would be answered about an
            # unknown directory.
            logger.warning("keeping trash batch %r: its identity was never established", batch.name)
            return SKIP_UNREADABLE
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != expect:
            logger.warning(
                "keeping trash batch %r: it is not the directory that was selected", batch.name
            )
            return SKIP_IDENTITY_CHANGED
        return _approve_emptied_batch(batch, root_fd, tree)

    try:
        outcome = pinned_fs.remove_tree_pinned(
            str(resolved),
            what=what,
            refusal=OSError,
            approve=_approve,
            # The manifest goes LAST, and comes back if the batch itself will not go.
            # `list_trash` omits a batch with no readable manifest, so removing it first and
            # then failing on the directory would leave a batch holding data that is neither
            # visible nor restorable - the same reason the delete path moves it aside rather
            # than unlinking it.
            keep_until_empty=MANIFEST_NAME,
        )
    except OSError as exc:
        # Includes the ancestor swap the pinned walk exists to catch. Warned rather than
        # raised: on both paths the caller has already finished the work the user asked for,
        # and a leftover batch is visible and restorable.
        logger.warning(
            "keeping trash batch %r: it could not be removed safely: %s", batch.name, exc
        )
        return False
    if not outcome.removed:
        logger.warning(
            "keeping trash batch %r: %s left %d entr(y/ies) behind%s",
            batch.name,
            outcome.reason or "the removal",
            outcome.survivors,
            f"; part of it is now {outcome.staged_name!r}" if outcome.staged_name else "",
        )
    return outcome.removed


def _write_header(handle: IO[str], batch_id: str, created_at: float, reason: str) -> None:
    header = {
        "schema": MANIFEST_SCHEMA,
        "batch_id": batch_id,
        "created_at": created_at,
        "reason": reason,
    }
    handle.write(json.dumps(header) + "\n")
    handle.flush()


def _append_entry(handle: IO[str], entry: dict[str, Any]) -> None:
    """Record one moved session and flush, so an interruption loses at most the
    session currently in flight."""
    handle.write(json.dumps(entry) + "\n")
    handle.flush()


# Longest manifest record accepted, in characters (terminator excluded: the
# threshold applies to the stripped record). A real record is a header or one
# session's file list — kilobytes at most. The manifest lives in an
# agent-writable tree, so a reader that trusts line length would hand a single
# multi-gigabyte no-newline line one allocation; a record past this cap ABORTS the
# read instead (the whole batch is then treated as having no readable manifest,
# exactly like an unreadable file). Skipping just the record would be worse than
# failing: a partial restore REWRITES the manifest from the records it parsed, so
# a skipped record's staged files would be silently orphaned. The transient peak
# on a hostile manifest is a small multiple of this value (up to two cap-sized
# reads concatenated, times Python's up-to-4-bytes-per-char widening), flat
# regardless of file size — this constant is the dial if that ceiling moves.
_MANIFEST_RECORD_CAP = 8 * 1024 * 1024


class _OversizedManifestRecord(Exception):
    """A manifest record exceeded ``_MANIFEST_RECORD_CAP``; the batch is unreadable."""


def _manifest_records(handle: IO[str], batch: Path) -> Iterator[dict[str, Any]]:
    """Yield each JSON object record of an open manifest, header first.

    Record boundaries match ``str.splitlines`` — the previous whole-file reader —
    so a manifest split on any unicode line boundary parses exactly as it always
    did: reads are accumulated in a carry-over buffer and re-split per chunk, so a
    cap-sized read ending mid-record never invents or destroys a boundary. Blank
    and non-dict lines are skipped silently. A record that fails to parse is
    skipped and counted: a trailing partial line (a crash mid-append) is expected
    and logged at debug, while any other unparseable record — mid-file corruption —
    gets one aggregated warning per read (#6292 item 3). Either way every complete
    record before it describes real moved files that must stay restorable, so a
    parse failure never fails the batch wholesale.

    A record longer than ``_MANIFEST_RECORD_CAP`` is different: it raises
    :class:`_OversizedManifestRecord` (readers map it to ``None``, the
    no-readable-manifest posture) rather than being skipped, and its bytes are
    never materialised past the cap. Skipping would lose data: ``restore()``
    rewrites the manifest from the records it parsed, so a skipped record's
    staged files would be orphaned. Aborting keeps every file protected by the
    unlisted-file guards until a human looks.

    The skip counting and its log lines run only when the generator is driven to
    exhaustion; both callers do, and a future caller that stops early forfeits
    them knowingly.
    """
    corrupt = 0
    trailing_partial = False
    buf = ""  # partial record carried between reads; capped below

    def _parse(piece: str) -> dict[str, Any] | None:
        nonlocal corrupt
        piece = piece.strip()
        if not piece:
            return None
        if len(piece) > _MANIFEST_RECORD_CAP:
            raise _OversizedManifestRecord(batch.name)
        try:
            record = json.loads(piece)
        except ValueError:
            corrupt += 1
            return None
        return record if isinstance(record, dict) else None

    while True:
        chunk = handle.readline(_MANIFEST_RECORD_CAP)
        if not chunk:
            break
        data = buf + chunk
        pieces = data.splitlines()
        # The final piece is complete iff data ends on a line boundary. \n is the
        # common case; the appended probe catches every other splitlines boundary
        # (\u2028, \x1c, ...) without enumerating them.
        ends_on_boundary = data.endswith("\n") or len((data + "x").splitlines()) > len(pieces)
        if ends_on_boundary:
            complete, buf = pieces, ""
        else:
            complete, buf = pieces[:-1], pieces[-1]
        for piece in complete:
            record = _parse(piece)
            if record is not None:
                yield record
        if len(buf) > _MANIFEST_RECORD_CAP:
            raise _OversizedManifestRecord(batch.name)
    # EOF: whatever is left in buf is a genuinely unterminated final line. It can
    # never exceed the cap here — an over-cap carry raised above — and the assert
    # keeps that invariant enforced if the loop is ever reshaped.
    assert len(buf) <= _MANIFEST_RECORD_CAP
    if buf:
        stripped = buf.strip()
        if stripped:
            try:
                record = json.loads(stripped)
            except ValueError:
                trailing_partial = True
            else:
                if isinstance(record, dict):
                    yield record
    if corrupt:
        # %r: the batch name is a directory name from an agent-writable tree, so it
        # can embed newlines; the repr keeps one log record from forging others.
        logger.warning("trash manifest in %r: skipped %d unparseable line(s)", batch.name, corrupt)
    if trailing_partial:
        logger.debug("trash manifest in %r ends in a partial line (crash mid-append)", batch.name)


def _read_manifest(
    batch: Path, *, dir_fd: int | None = None, expect_ino: int | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Parse a batch manifest into its header and session entries.

    Streams the file line by line rather than materialising it as one string; the
    entries list itself is still O(sessions), which the two callers that rewrite or
    enumerate the manifest genuinely need. Callers that need only aggregates should
    use :func:`_summarize_manifest` instead. A trailing partial line (a crash
    mid-append) is skipped rather than failing the whole batch — every complete
    line before it describes real moved files that must stay restorable (see
    :func:`_manifest_records`).

    *expect_ino* refuses a manifest that is not the file the caller already identified,
    checked by ``fstat`` on the OPEN handle rather than by a stat of the name - so there is
    no interval between the check and the read for a replacement to land in. A caller that
    needs both the header and the listing to describe ONE file passes it and makes a single
    call: two calls are two opens, and a manifest rewritten between them lets the header
    check pass on one file while the listing that authorises a deletion comes from another.
    """
    header: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    try:
        with _open_manifest(batch, dir_fd) as handle:
            if expect_ino is not None and os.fstat(handle.fileno()).st_ino != expect_ino:
                return None
            for record in _manifest_records(handle, batch):
                if header is None:
                    header = record
                    continue
                entries.append(record)
    except OSError:
        return None
    except _OversizedManifestRecord:
        logger.warning(
            "trash manifest in %r has a record over %d chars; treating it as unreadable",
            batch.name,
            _MANIFEST_RECORD_CAP,
        )
        return None
    if header is None or header.get("schema") != MANIFEST_SCHEMA:
        return None
    return header, entries


def _open_manifest(batch: Path, dir_fd: int | None) -> IO[str]:
    """Open *batch*'s manifest, by path or relative to an already-pinned descriptor.

    With *dir_fd* the read is bound to the directory the caller pinned rather than to
    whatever answers to the batch's name by the time the read happens. That matters where
    the manifest's numbers and the directory's identity are recorded together: derived from
    one pinned object they describe the same batch, while a path-based read and a separate
    stat can straddle a swap and approve a replacement's identity with the original's
    numbers. ``O_NOFOLLOW`` because a link at that name is never this batch's manifest.
    """
    if dir_fd is None:
        return (batch / MANIFEST_NAME).open(encoding="utf-8")
    fd = os.open(MANIFEST_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        return os.fdopen(fd, encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _summarize_manifest(
    batch: Path, *, dir_fd: int | None = None
) -> tuple[dict[str, Any], int, int] | None:
    """The manifest's header plus (session count, staged byte total).

    A batch can hold six figures of sessions, and the trash listing needs only
    these two aggregates — so they are accumulated during the same single streamed
    pass :func:`_read_manifest` makes, without ever holding the parsed entries.
    Guards match :func:`_read_manifest` exactly: same skipped-line tolerance, same
    schema rejection, and ``None`` on an unreadable manifest.
    """
    header: dict[str, Any] | None = None
    sessions = 0
    staged_bytes = 0
    try:
        with _open_manifest(batch, dir_fd) as handle:
            for record in _manifest_records(handle, batch):
                if header is None:
                    header = record
                    continue
                sessions += 1
                staged_bytes += _entry_bytes(record)
    except OSError:
        return None
    except _OversizedManifestRecord:
        logger.warning(
            "trash manifest in %r has a record over %d chars; treating it as unreadable",
            batch.name,
            _MANIFEST_RECORD_CAP,
        )
        return None
    if header is None or header.get("schema") != MANIFEST_SCHEMA:
        return None
    return header, sessions, staged_bytes


def _rewrite_manifest(batch: Path, header: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Replace and sync a manifest after a partial restore."""
    header_line = json.dumps(header) + "\n"
    entry_lines = "".join(json.dumps(entry) + "\n" for entry in entries)
    atomic_write(batch / MANIFEST_NAME, header_line + entry_lines, fsync=True)
    # ``fsync=True`` made the new CONTENT durable; this makes the rename that gave
    # it the manifest's name durable. Without it a power-off just after this returns
    # can come back to the pre-restore manifest, which lists files restore has
    # already moved back out — an entry that now names live session data.
    fsync_dir(batch)


def _levels_between(root: Path, leaf: Path) -> list[Path]:
    """Every directory from *root*'s first child down to *leaf*, shallowest first.

    ``mkdir(parents=True)`` creates intermediates, and a directory's own NAME lives in
    its PARENT's entries — so syncing only the leaf leaves the entries that reach it
    unrecorded. ``relative_to`` bounds the walk: a leaf outside *root* raises here
    rather than climbing past it.
    """
    levels: list[Path] = []
    level = root
    for part in leaf.relative_to(root).parts:
        level = level / part
        levels.append(level)
    return levels


def _fsync_tree(root: Path, leaf: Path) -> None:
    """Sync *root* and every level down to *leaf*, deepest first."""
    for level in reversed(_levels_between(root, leaf)):
        fsync_dir(level)
    fsync_dir(root)


def _sync_batch(
    target: Path,
    staged_dirs: set[Path],
    manifest: IO[str],
    source_dirs: set[Path] | None = None,
) -> None:
    """Force a staged batch out to disk: the manifest's bytes, then every name.

    Called at each point :func:`_move_to_trash_locked` can leave a batch on disk,
    because until this runs the batch is only durable by luck. The moves themselves
    are renames, whose metadata a journalling filesystem commits on its own
    schedule; the manifest is buffered text. So a power-off just after the move
    reported success can come back to a batch holding the sessions' only copies with
    an empty manifest — and a batch with no readable manifest is one
    :func:`list_trash` omits, which puts those files out of reach of restore AND of
    empty. That is the loss this whole layer exists to prevent, arrived at by
    reporting success too early.

    Order matters. The manifest's CONTENT first, because it is what makes the moves
    reversible; then the directories that hold the staged names, deepest first, so a
    child's entries are durable before the entry naming that child is.

    A failing sync RAISES rather than being logged away. The files have already moved,
    so raising costs the caller a reported failure for work that partly happened —
    but the alternative is reporting a durable batch that is not one, and the batch is
    the only copy. Each caller decides what to do with it; nothing is swallowed here.

    *source_dirs* — the live-store directories the batch drained — are the exception,
    on both counts. They are synced ONCE here rather than per file, because the
    same-filesystem path is a bare ``os.rename`` and a sync per staged file is exactly
    the cost that would stop a trash working at tens of gigabytes; and they are synced
    ``best_effort`` because a source entry that is not yet durable can only resurrect
    a name whose staged copy is intact — a duplicate the user thought they reclaimed,
    never a lost session. Raising for that after the batch is fully recorded would
    report a completed move as failed.

    This does not make a crash MID-move consistent — a batch interrupted halfway
    still holds files its manifest never named, which is what :func:`_unlisted_files`
    exists to protect. It makes a move that RETURNED durable.
    """
    manifest.flush()
    os.fsync(manifest.fileno())
    for staged in sorted(staged_dirs, key=lambda path: len(path.parts), reverse=True):
        fsync_dir(staged)
    fsync_dir(target)
    for source in sorted(source_dirs or (), key=lambda path: len(path.parts), reverse=True):
        fsync_dir(source, best_effort=True)


def _entry_bytes(entry: dict[str, Any]) -> int:
    files = entry.get("files")
    if not isinstance(files, list):
        return 0
    total = 0
    for record in files:
        if isinstance(record, dict):
            size = record.get("bytes")
            if isinstance(size, int):
                total += size
    return total


def move_to_trash(
    uids: list[str],
    *,
    reason: str,
    index: SessionIndex,
    now: float | None = None,
    refresh: Callable[[], SessionIndex] | None = None,
) -> TrashBatch:
    """Stage whole sessions for deletion and return the batch that can undo it.

    Every half of each session moves together — replay log, transcript and rotated
    archive segments. Leaving one behind would produce a session that lists but
    cannot resume, or resumes with no history.

    Refuses a session that is still mapped, and one touched within
    :data:`MIN_RECLAIM_AGE_DAYS`: a mapped session is resumable, and a fresh one
    may be running under a subsystem that never registered it.

    *refresh* re-reads the caller's index INSIDE the lock: once immediately before
    anything moves, and again before every session the loop reaches. Scanning a
    large store takes long enough that a session can be resumed and mapped while
    the selection is being computed, and the move loop after that is not instant
    either. The active sets are UNIONED, never replaced, so a re-read can only ever
    add protection.

    Called up to once per selected session, so it must be CHEAP when nothing has
    changed. Return the same :class:`SessionIndex` object as last time to say
    "nothing has moved" — an identical object is recognised by identity and costs
    one comparison. Returning a freshly built index every call is correct but pays
    for a full re-read per session, which at a six-figure selection is minutes.

    Two shapes of mid-staging resume are caught, and both are needed. A resume
    that WRITES is caught by mtime: each file is already stat'd for the manifest,
    so a source modified since the reclaim began fails the check before it moves.
    A resume that only READS an old transcript writes nothing, so it is invisible
    to mtime and is caught by the re-read instead — it is mapped. Either way the
    session is left in place and named in the returned batch's ``revived``: it was
    asked for and not taken.

    Serialized against other mutations, because two interleaved reclaims can put
    one half of a session in each batch and leave neither able to restore it.
    """
    with _mutation_lock():
        try:
            return _move_to_trash_locked(uids, reason=reason, index=index, now=now, refresh=refresh)
        finally:
            # In a finally, not after a success: a partially-completed batch has
            # already moved files, so a raised refusal still leaves any cached
            # pass describing sessions that are no longer where it says.
            invalidate_scan_cache()


def _move_to_trash_locked(
    uids: list[str],
    *,
    reason: str,
    index: SessionIndex,
    now: float | None = None,
    refresh: Callable[[], SessionIndex] | None = None,
) -> TrashBatch:
    """Stage whole sessions for deletion and return the batch that can undo it.

    Every half of each session moves together — replay log, transcript and rotated
    archive segments. Leaving one behind would produce a session that lists but
    cannot resume, or resumes with no history.

    Refuses any session that is still mapped: it is one the product can resume,
    and moving its files out from under a live slot breaks it with no error a user
    would connect to this action.

    Every such check runs before the move loop, so each describes the instant it
    ran. The loop closes the remaining window itself, with two per-session signals.
    A source file whose mtime is newer than an instant taken before any of those
    checks has been written to since the reads that certified it, which an idle
    session's file cannot be. And a session the re-read now reports as mapped is
    one the product can resume, however old its files are — the case mtime cannot
    see, because a resume that only reads a transcript writes nothing. Either way
    the whole session is left in place and named in ``TrashBatch.revived``.

    Each session is recorded as it lands, so an interruption leaves a manifest
    describing exactly what moved — a partial batch stays restorable instead of
    becoming orphaned files nothing points at.
    """
    clock = time.time() if now is None else now
    # Anchor for the move loop's revival check, taken BEFORE the scan and before
    # every authority check below — not after them. Everything this function is
    # about to reason over is read after this instant, so a source file written
    # later than it may have been written after the read that certified it, and
    # the loop must not trust that file. Stamping after the checks would leave
    # exactly that gap: a session resumed during the scan, or between the index
    # re-read and the loop, would carry an mtime older than the stamp and be
    # staged.
    #
    # Taking it this early costs nothing in false positives. A candidate has to be
    # untouched for MIN_RECLAIM_AGE_DAYS to qualify at all, so a legitimate one's
    # mtime is days older than this stamp however early it is taken.
    #
    # Deliberately real wall-clock rather than *clock*: a caller may inject
    # ``now`` (tests do), and this is compared against an mtime the kernel writes,
    # so it has to measure elapsed reality.
    validated_at = time.time()
    blocked_reason = reclaim_block_reason()
    if blocked_reason:
        raise SessionStorageError(blocked_reason)
    requested = [_validate_unit_id(uid) for uid in uids]
    if not requested:
        raise SessionStorageError("no sessions selected")

    by_uid = {u.uid: u for u in _scan_units(index)}

    # The authority check runs AFTER the scan, not before it. Enumerating a
    # six-figure store is the slow part of this function, so an index read before
    # it is already stale by the time anything moves — a session continued in that
    # interval would look retired. Re-reading here is what the checks below get to
    # judge against, and the sets are UNIONED so a re-read can only ever add
    # protection. It is not the last word: the move loop re-reads per session, for
    # the stretch this one cannot describe.
    #
    # *seen* keeps the index this returned, so the loop can tell an unchanged view
    # apart from a new one by identity and skip re-deriving the sets for it.
    seen: SessionIndex | None = None
    if refresh is not None:
        try:
            latest = refresh()
        except Exception:
            logger.warning("could not re-read the session index", exc_info=True)
            raise SessionStorageError(
                "could not confirm which sessions are live; nothing was moved"
            )
        seen = latest
        live_sids = index.active_sids | latest.active_sids
        live_stems = index.active_stems | latest.active_stems
    else:
        live_sids = index.active_sids
        live_stems = index.active_stems

    # Co-tenant ownership is re-read here for exactly the reason *refresh* exists,
    # and must not be left out of it: the scan above is the slow part, so the
    # co-tenant view taken during it is seconds stale by the time anything moves.
    # Refreshing only the local map would leave a co-tenant that adopted a
    # PRE-EXISTING replay log in that window unprotected — and the freshness floor
    # does not cover that case, because the session it adopted is old.
    #
    # An instance that genuinely shares this store refuses the move outright rather
    # than being handled per session: it can seed and resume a session at any
    # moment, including part-way through the loop below, which a snapshot taken
    # here cannot cover however fresh it is.
    cotenant_now, refusals = cotenant_sids()
    if refusals:
        name, why = refusals[0]
        # name!r, not plain interpolation: the directory name is agent-influenced
        # and passes no identifier gate, so a newline or ANSI payload in it would
        # forge a second record the moment a caller logs str(exc) (the
        # #6281/#6371 forgery class).
        raise SessionStorageError(
            f"{len(refusals)} instance(s) sharing this session store make reclaiming "
            f"unsafe ({name!r} — {why}); nothing was moved"
        )
    live_sids = live_sids | cotenant_now

    def is_live(uid: str) -> bool:
        unit = by_uid.get(uid)
        if unit is None:
            return False
        if unit.active or (unit.sid and unit.sid in live_sids):
            return True
        return any(stem in live_stems for stem in unit.stems)

    blocked = [uid for uid in requested if is_live(uid)]
    if blocked:
        raise SessionStorageError(
            f"refusing to move {len(blocked)} session(s) still in use: {blocked[0]}"
        )
    # Freshness is checked HERE, not only in the selection helper, because this is
    # the chokepoint: a caller passing ids directly would otherwise bypass the
    # floor and take a session that is running right now but was never mapped.
    fresh = [
        uid
        for uid in requested
        if uid in by_uid and by_uid[uid].age_days(clock) < MIN_RECLAIM_AGE_DAYS
    ]
    if fresh:
        raise SessionStorageError(
            f"refusing to move {len(fresh)} session(s) touched in the last "
            f"{MIN_RECLAIM_AGE_DAYS:g} day(s): {fresh[0]}"
        )

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(clock))
    batch_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
    target = _batch_dir(batch_id)
    target.mkdir(parents=True, exist_ok=False)
    # Recorded HERE, at creation and under the mutation lock, because it is the strongest
    # anchor this path will ever have: the directory was made by this call, so nothing has
    # had a window to substitute anything for it yet. The cleanup below removes this batch
    # by descriptor and compares that descriptor against these numbers, so a swapped
    # ancestor reaching a different directory - however convincingly it is dressed up - is
    # refused rather than emptied.
    created_identity = _identity_of(target)
    # And the entry itself has to exist ON DISK before anything is moved in: the batch
    # directory is about to become the only home of these files, so a crash with the
    # mkdir still in the page cache would come back to no batch directory at all — and
    # the files were renamed INTO it, so they would be gone with it.
    #
    # Every level down from the data home, not just target.parent: on the FIRST batch
    # this machine ever stages, parents=True creates `trash/` and `trash/sessions/`
    # too, and an unsynced `trash/sessions` entry takes the batch inside it. Bounded
    # by relative_to, which raises rather than climbing past the data home if the
    # trash root is ever moved out from under it (_batch_dir already refuses that).
    _fsync_tree(data_home(), target.parent)
    archives = _archive_index()

    moved_bytes = 0
    moved_sessions = 0
    revived: list[str] = []
    refresh_failed = False
    staged_dirs: set[Path] = set()
    source_dirs: set[Path] = set()
    cli_files = _cli_index()
    with (target / MANIFEST_NAME).open("w", encoding="utf-8") as manifest:
        _write_header(manifest, batch_id, clock, reason)
        for uid in requested:
            unit = by_uid.get(uid)
            if unit is None:
                continue
            if refresh is not None:
                # Re-read before EVERY unit, not once before the loop. The mtime
                # check below is the only other per-unit signal and it sees writes
                # only, so a resume that merely READS an old transcript to rebuild
                # history — recording the turn that follows under a newly mapped
                # sid — leaves every mtime days old and slips past it (#7118). The
                # index is where that resume IS visible, so it has to be consulted
                # at the same cadence.
                #
                # Affordable because an unchanged view costs one identity
                # comparison: *refresh* may return the same object it returned last
                # time to say "nothing has moved", and the dashboard's does exactly
                # that behind a stat of the session map. A caller that returns a
                # fresh index every time is not wrong, only slower — the sets are
                # re-derived and the answer is identical.
                try:
                    latest = refresh()
                except Exception:
                    # Degraded, not fatal. Every re-read only ever WIDENS the live
                    # sets, so losing one leaves the guard exactly as strong as the
                    # pre-loop read already made it — it cannot make a session
                    # reclaimable that the checks before this loop protected. A
                    # batch part-way through has already moved files, and
                    # abandoning it over the loss of an additive signal would be
                    # the worse trade. A refresh broken from the start still fails
                    # closed: the pre-loop call above raises.
                    if not refresh_failed:
                        refresh_failed = True
                        logger.warning(
                            "could not re-read the session index while staging; "
                            "continuing with the last view read",
                            exc_info=True,
                        )
                else:
                    if latest is not seen:
                        seen = latest
                        live_sids = live_sids | latest.active_sids
                        live_stems = live_stems | latest.active_stems
            if is_live(uid):
                # Mapped since the checks that certified it. Nothing of this
                # session has moved yet, so there is nothing to roll back — it is
                # simply not taken, and reported the same way a write-shaped
                # revival is.
                revived.append(uid)
                continue
            files: list[dict[str, Any]] = []
            done: list[tuple[Path, Path]] = []
            failed = False
            woke = False
            for src, rel in _unit_paths(unit.sid, unit.stems, archives, cli_files):
                try:
                    size, mtime = _file_stamp(src)
                except OSError:
                    # A file that cannot be sized cannot be recorded, and a file
                    # the manifest does not record is one restore cannot put back.
                    # Skipping it here while moving the rest is precisely the split
                    # this loop's rollback exists to prevent, so it is a failure.
                    logger.warning("could not stat %s for staging", src, exc_info=True)
                    failed = True
                    break
                if mtime > validated_at:
                    # Every authority check ran before the loop, and moving a
                    # six-figure store is not instant, so a session can be resumed
                    # between being certified retired and being reached here. Its
                    # replay log is then written to, and this is the one signal of
                    # that which costs nothing: the stat is already being taken for
                    # the manifest.
                    #
                    # A candidate qualified by being untouched for
                    # MIN_RECLAIM_AGE_DAYS, so an mtime newer than the instant we
                    # certified it cannot be the same idle file — something has it
                    # open. Leave the whole session alone rather than any part of
                    # it: staging half is the split the rollback below exists to
                    # prevent, and the half left behind would be the live half.
                    woke = True
                    break
                dst = target / rel
                if dst.parent not in staged_dirs:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    # Before anything moves INTO it, not with the batch at the end.
                    # The same-filesystem move is a rename, which removes the file's
                    # only other name in the same atomic step that creates this one:
                    # if the rename reaches disk while the mkdir that created its
                    # parent does not, the file has no reachable name left. Syncing
                    # the chain here orders the two — the directory that will hold
                    # the file is durable before the source entry can be given up.
                    #
                    # Cheap in the way the per-file case would not be: once per new
                    # directory, not once per file, so the hot path stays a bare
                    # rename (see :func:`_sync_batch` for the end-of-batch sync that
                    # forces the entries those renames then add).
                    try:
                        _fsync_tree(target, dst.parent)
                    except OSError:
                        # Same treatment as a file that would not move: nothing of
                        # this session has entered this directory yet, so rolling it
                        # back and leaving the rest of the batch to be recorded and
                        # synced properly is strictly better than abandoning a batch
                        # whose manifest has not been forced out.
                        logger.warning(
                            "could not sync the staging directory %s", dst.parent, exc_info=True
                        )
                        failed = True
                        break
                    # Recorded only once the chain is durable. On the failure above
                    # nothing was staged under it, so there is nothing for the
                    # end-of-batch sync to force; a later session needing the same
                    # directory re-runs the mkdir and retries the sync.
                    staged_dirs.update(_levels_between(target, dst.parent))
                try:
                    _move_file(src, dst)
                except OSError:
                    logger.warning("could not move %s into the trash", src, exc_info=True)
                    failed = True
                    break
                # The live-store directory this file just left. Synced once at the end
                # rather than per file, because the same-filesystem path is a bare
                # rename and this is the hot path (see :func:`_sync_batch`).
                source_dirs.add(src.parent)
                done.append((dst, src))
                files.append({"rel": rel, "origin": str(src), "bytes": size})
            if woke:
                # Put back whatever already moved for this session: the point of
                # leaving it alone is that it stays resumable, and half a session
                # is not.
                if not _rollback(done):
                    # The rollback could not finish — most plausibly because the
                    # resume that triggered this recreated an origin, and putting a
                    # file back is deliberately non-overwriting. Two things then
                    # have to be true before this returns.
                    #
                    # First, whatever is still staged must be IN the manifest.
                    # Unlisted files in a batch are reachable by nothing: restore
                    # enumerates the manifest, and so does empty. Recording them is
                    # safe because restore moves exclusively too, so a later restore
                    # declines the origin the resume recreated rather than
                    # overwriting the live session with this stale copy.
                    stranded = [
                        record for record, (landed, _origin) in zip(files, done) if landed.exists()
                    ]
                    if stranded:
                        try:
                            _append_entry(manifest, {"uid": uid, "files": stranded})
                        except OSError:
                            logger.warning(
                                "could not record the stranded half of %r; it is in %s",
                                uid,
                                target,
                                exc_info=True,
                            )
                    # Second, this must not be reported as a revival. "Left in
                    # place" would be false, so raise instead — matching how this
                    # module already treats a file it could not put back — and name
                    # the batch so the fragment can be found.
                    #
                    # The message promises the fragment "can be restored", and that
                    # is only true of a manifest that reached disk: sync before
                    # raising, because nothing after this point will.
                    #
                    # A sync failure here is logged rather than raised — the one place
                    # in this module where that is right. This path is ALREADY
                    # failing, with an error that names which session was resumed and
                    # where its fragment is; replacing that with an OSError would cost
                    # the operator the only pointer to the fragment in exchange for
                    # information the log line already carries.
                    try:
                        _sync_batch(target, staged_dirs, manifest, source_dirs)
                    except OSError:
                        logger.warning(
                            "could not sync %s while recording the stranded half of %r; "
                            "the fragment is on disk but may not survive a power loss",
                            target,
                            uid,
                            exc_info=True,
                        )
                    raise SessionStorageError(
                        f"session {uid!r} was resumed while being staged and could "
                        f"not be fully put back; what is still staged is recorded in "
                        f"{target} and can be restored"
                    )
                revived.append(uid)
                continue
            if failed:
                # A session that moved only partly is the broken state this module
                # exists to prevent, and it would be invisible: the manifest would
                # list the staged half while the rest stayed in place, so emptying
                # the trash would destroy one half of a session nobody knew was
                # split. Put back whatever moved and leave the session alone.
                _rollback(done)
                continue
            if files:
                # The manifest is what makes a move reversible, so a session that
                # moved but could not be recorded is worse than one that never
                # moved: its files are gone from live storage and no entry names
                # them, so they can neither be restored nor resumed. Rewind the
                # partial line and put the files back.
                mark = manifest.tell()
                try:
                    _append_entry(manifest, {"uid": uid, "files": files})
                except OSError:
                    logger.warning("could not record session %r in the manifest; rolling back", uid)
                    try:
                        manifest.truncate(mark)
                        manifest.seek(mark)
                    except OSError:
                        logger.warning("could not rewind the manifest", exc_info=True)
                    _rollback(done)
                    continue
                moved_sessions += 1
                moved_bytes += sum(int(record["bytes"]) for record in files)
        # Inside the handle's scope, so the manifest can still be synced through the
        # descriptor that wrote it. Closing only flushes to the OS.
        _sync_batch(target, staged_dirs, manifest, source_dirs)

    if revived:
        # Reported, not just skipped: the endpoint's own rule is that doing less
        # than the user asked without saying so is a defect. uid!r because a unit
        # id reaching here has passed _validate_unit_id, but the log line is read
        # by people who also read agent-influenced names, so keep it quoted.
        logger.warning(
            "left %d session(s) in place: resumed while staging (first: %r)",
            len(revived),
            revived[0],
        )

    if not moved_sessions:
        # Leave no empty batch behind — but a rollback that itself failed can have
        # left staged files here, and those are the only copy.
        if _unlisted_files(target):
            raise SessionStorageError(
                "no sessions were staged, and some files could not be put back; " f"see {target}"
            )
        _remove_emptied_batch(target, "the empty batch", expect=created_identity)
        if revived:
            # A distinct message from "not found": every selected session is still
            # exactly where the caller left it, and it is still resumable. Saying
            # "none were found" here would send someone looking for lost files.
            raise SessionStorageError(
                f"all {len(revived)} selected session(s) were resumed while being "
                "staged; nothing was moved"
            )
        raise SessionStorageError("none of the selected sessions were found on disk")

    return TrashBatch(
        batch_id=batch_id,
        created_at=clock,
        reason=reason,
        sessions=moved_sessions,
        bytes=moved_bytes,
        revived=tuple(revived),
    )


def list_trash() -> list[TrashBatch]:
    """Every staged batch, newest first.

    A batch with no readable manifest is omitted: without one its files could not
    be put back, so presenting it as restorable would be a false promise.
    """
    batches: list[TrashBatch] = []
    root = trash_root()
    try:
        # Same two rules as _batch_dir: a trash root that has been replaced with a
        # link would otherwise have foreign directories enumerated AS batches, which
        # is what makes them reachable by "empty everything". The anchor alone is not
        # enough — a link can point INSIDE the data home, at the live sessions tree.
        if platform_compat.is_link_or_junction(root):
            logger.warning("trash root %s is a link; not offering any batches", root)
            return []
        if data_home().resolve() not in root.resolve().parents:
            logger.warning("trash root %s does not live under the data home", root)
            return []
        candidates = list(root.iterdir())
    except OSError:
        return []
    for candidate in candidates:
        # A link is not a batch. Skipping it here (rather than raising) keeps a
        # planted link from wedging "empty everything" forever, while an explicit
        # request naming that id is still refused loudly by _batch_dir. Junctions
        # count: is_symlink() reports False for one, so a Windows junction would be
        # listed as a real batch and the sweep would delete through it.
        if platform_compat.is_link_or_junction(candidate) or not candidate.is_dir():
            continue
        parsed = _summarize_manifest(candidate)
        if parsed is None:
            # %r: the directory name is agent-controlled; repr keeps an embedded
            # newline from forging additional log records.
            logger.debug("trash batch %r has no readable manifest", candidate.name)
            continue
        header, sessions, staged_bytes = parsed
        # The DIRECTORY is the batch's identity. A header that names a different
        # batch would make a targeted empty delete the batch it named instead of
        # the one it came from, so a disagreement is treated as corruption rather
        # than resolved in the header's favour.
        claimed = header.get("batch_id")
        if isinstance(claimed, str) and claimed and claimed != candidate.name:
            # %r on the name: agent-controlled, same forgery risk as above.
            logger.warning(
                "trash batch %r has a manifest claiming batch id %r; not offering it",
                candidate.name,
                claimed,
            )
            continue
        created = header.get("created_at")
        batches.append(
            TrashBatch(
                batch_id=candidate.name,
                created_at=float(created) if isinstance(created, (int, float)) else 0.0,
                reason=str(header.get("reason") or ""),
                sessions=sessions,
                bytes=staged_bytes,
            )
        )
    batches.sort(key=lambda b: b.created_at, reverse=True)
    return batches


def restore(batch_id: str, uids: list[str] | None = None) -> int:
    """Put staged sessions back where they came from; return sessions restored.

    Serialized against other mutations so a concurrent reclaim cannot move a
    session's other half while this one is putting it back.
    """
    with _mutation_lock():
        try:
            return _restore_locked(batch_id, uids)
        finally:
            invalidate_scan_cache()


def _restore_locked(batch_id: str, uids: list[str] | None = None) -> int:
    """Put staged sessions back where they came from; return sessions restored.

    *uids* restores only those sessions, which is what makes a large
    policy-driven batch usable: a user remembers the one conversation they want,
    not the batch it happened to land in.

    A session is restored only when EVERY one of its files can go back. Restoring
    part of one would recreate the half-session this module exists to avoid, so a
    blocked file leaves the whole session staged.
    """
    batch = _batch_dir(batch_id)
    # BEFORE the first read of anything inside the batch, which is the point. This identity is
    # what the cleanup at the bottom binds its removal to, and every check between here and
    # there is by path: an ancestor swapped at ANY point during the restore is caught, because
    # the pinned root the cleanup opens will not be the inode recorded here. Captured later -
    # after the manifest read, or after the files have moved - the swap that landed in between
    # would be recorded as though it were the batch, and the cleanup would then verify
    # consistently against a directory the caller was never looking at.
    identity = _identity_of(batch)
    parsed = _read_manifest(batch)
    if parsed is None:
        raise SessionStorageError(f"no restorable batch {batch_id!r}")
    header, entries = parsed
    wanted = None if uids is None else {_validate_unit_id(uid) for uid in uids}

    remaining: list[dict[str, Any]] = []
    restored = 0
    for entry in entries:
        uid = str(entry.get("uid") or "")
        if wanted is not None and uid not in wanted:
            remaining.append(entry)
            continue
        files = entry.get("files")
        files = files if isinstance(files, list) else []
        planned: list[tuple[Path, Path]] = []
        blocked = False
        for record in files:
            if not isinstance(record, dict):
                # A record we cannot read is a file we cannot place. Restoring the
                # rest would leave the session split with that file staged and
                # unreferenced, so the whole session stays put.
                logger.warning("manifest record for %r is not an object", uid)
                blocked = True
                break
            rel = str(record.get("rel") or "")
            src = _staged_path(batch, rel)
            recorded = _origin_path(str(record.get("origin") or ""))
            origin = _canonical_origin(rel)
            if src is None or origin is None or recorded is None:
                logger.warning("refusing a manifest record outside the session stores")
                blocked = True
                break
            if recorded.resolve() != origin.resolve():
                # Both are inside a session store, so neither check alone catches
                # this: the recorded origin names a DIFFERENT session's file.
                logger.warning(
                    "refusing a manifest record whose origin does not match its " "staged path"
                )
                blocked = True
                break
            # An occupied origin means the session came back on its own; the
            # occupant is newer, and undoing a deletion must not cause one.
            # is_file() FOLLOWS links, so a staged symlink would pass as a file and
            # restore would put the link back where the session's data belongs.
            if platform_compat.is_link_or_junction(src) or not src.is_file() or origin.exists():
                blocked = True
                break
            planned.append((src, origin))
        if blocked or not planned:
            remaining.append(entry)
            continue
        done: list[tuple[Path, Path]] = []
        try:
            lost_race = False
            for src, origin in planned:
                origin.parent.mkdir(parents=True, exist_ok=True)
                if not _move_file_exclusive(src, origin):
                    # The session came back between the preflight and now. The
                    # occupant is newer, so the whole session is put back and the
                    # entry retained — restoring the rest would splice two
                    # generations of one session together.
                    logger.warning(
                        "session %r was recreated while being restored; leaving it " "staged",
                        uid,
                    )
                    lost_race = True
                    break
                done.append((origin, src))
            if lost_race:
                _rollback(done)
                remaining.append(entry)
                continue
        except OSError:
            logger.warning("could not fully restore session %r", uid, exc_info=True)
            # Without this the session is split *and* wedged: the files that did
            # move are gone from the batch while the manifest still lists them, so
            # every later retry fails its own "staged file present" check and the
            # session can never be restored or cleanly emptied again.
            _rollback(done)
            remaining.append(entry)
            continue
        restored += 1

    if remaining:
        _rewrite_manifest(batch, header, remaining)
    else:
        # NO write on this branch, and that is a decided residual rather than an oversight.
        # Every entry below the header describes a file this restore moved back OUT, so a
        # batch the cleanup then declines to remove still lists sessions that are not in it.
        # Clearing them needs a write to `batch`, and there is nowhere safe to put one:
        # AFTER the cleanup the descriptor is closed and every refusal is itself evidence
        # the path may be redirected, and BEFORE it this branch has no write at all today,
        # so adding one hands `atomic_write` - which REPLACES its destination - a path that
        # a batch swapped to a link points wherever an actor chose, including another
        # batch's manifest, whose sessions would become unlistable. A stale listing is
        # visible and reversible; that is not. `empty_trash` clears the batch on the user's
        # own say-so.
        _discard_restored_batch(batch, identity)
    return restored


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
    file's identity, but it used to check against a map built by its OWN scan - which is
    self-consistent and authorises nothing. A listed file replaced between the approval and
    the delete had its replacement's inode recorded, matched, and was unlinked: an
    unapproved file, whose only copy it may be, destroyed on consent given for a different
    one. Both are None on the coarse platform for the reason ``dirs`` is.
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


# How many deleted files pass between two progress callbacks. Per file would be six
# figures of calls for one batch of the size this reporting exists for.
_PROGRESS_EVERY_FILES = 64


def _rels_digest(rels: list[str]) -> str:
    """A stable digest of the staged paths a manifest names.

    Order-sensitive on purpose: the delete walks the rels in order, and two listings that
    differ only in order are still two different listings. NUL-separated so no path can
    forge a boundary by containing the separator - the rels are plain batch-relative paths,
    which cannot contain NUL on any supported platform.

    Not a security hash of a secret, just a comparison that does not need to hold six
    figures of strings in the approval: sha256 because it is what the stdlib makes cheap and
    nobody has to reason about collisions.
    """
    digest = hashlib.sha256()
    for rel in rels:
        digest.update(rel.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest_rels(batch: Path, *, dir_fd: int | None = None) -> list[str]:
    """Every staged file the batch's manifest names, as batch-relative paths.

    With *dir_fd* the manifest is read through that already-pinned directory rather than
    by path, which is what the approval needs: the listing it records has to be the one
    inside the directory whose identity it is recording, not whatever answers to the name
    a moment later.
    """
    parsed = _read_manifest(batch, dir_fd=dir_fd)
    if parsed is None:
        return []
    rels: list[str] = []
    for entry in parsed[1]:
        files = entry.get("files")
        if not isinstance(files, list):
            continue
        for record in files:
            if isinstance(record, dict) and isinstance(record.get("rel"), str):
                rels.append(record["rel"])
    return rels


#: True when this platform can delete a file by (directory fd, name) and can open a
#: directory refusing to follow a link. Both are what make a per-file delete safe;
#: Windows has neither, so it takes the coarse path below.
#:
#: The pinned-walk half is asked of :mod:`kiro_crew.pinned_fs` rather than restated here.
#: That module exists because two closed PRs (#2446, #2447) tried to spell this mechanism
#: per call site and neither converged, so a second spelling is the failure it was created
#: to end. What is added on top is only what THIS path needs beyond walking a tree: the
#: three mutating calls it makes relative to a descriptor.
_FD_SAFE_DELETE = (
    pinned_fs.supports_pinned_tree_walk()
    # `os.link` is here for the manifest RECOVERY: putting the manifest back must fail
    # rather than replace, and `rename` cannot express that. A platform without it takes
    # the coarse path instead of reaching a recovery it cannot perform safely.
    and {os.unlink, os.rmdir, os.rename, os.link} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)

#: Flags for every directory this module opens on the delete path, from the same source as
#: the walk. Called rather than captured at import: the Windows-simulation tests delete
#: ``os.O_NOFOLLOW`` at runtime, and a frozen constant would keep offering a flag the
#: platform no longer has.
_dir_open_flags = pinned_fs.dir_flags


#: Descriptor bookkeeping for the pinned walks below, from the same source as the walk
#: itself. Aliases rather than second copies: a leaked descriptor pins its inode for the
#: life of the process, and the drain's "pop as you go" rule exists so a failure part-way
#: cannot leave an already-closed number for a later cleanup to close again.
_close_all = pinned_fs.close_all
_drain = pinned_fs.drain_verified_chain

#: The pinned traversal and the verified chain-open used to be spelled here. They are
#: consumer-agnostic - "walk a tree without ever re-resolving a name" and "open a
#: component only as the inode a scan recorded" say nothing about batches, manifests or
#: approval - so they now live in :mod:`kiro_crew.pinned_fs` with the rest of the
#: mechanism, and what stays in this module is the trash-specific part: which map
#: authorises the delete, and what a refusal means to the user.
_scan_tree = pinned_fs.scan_tree_pinned
_open_chain = pinned_fs.open_verified_chain


def _plain_parts(rel: str) -> tuple[str, ...] | None:
    """The components of *rel*, or None if it is not a plain path under a batch.

    A manifest is a file on disk: a tampered or corrupted one can name an absolute
    path or walk out with ``..``. Every consumer of a manifest entry goes through
    here first, so "is this name usable" is answered in ONE place rather than at each
    call site - the size read below skipped this check when it was written inline in
    the delete, and stat'd whatever the manifest said.

    Absoluteness is asked of the PATH rather than of any spelling of a separator.
    Enumerating those was the earlier bug: ``PurePosixPath("//tmp/victim").parts`` is
    ``("//", "tmp", "victim")`` - a POSIX root of two slashes, which a check against
    ``"/"`` misses - and an absolute path passed to ``os.open`` ignores ``dir_fd``
    entirely, so the open leaves the batch. Both flavours are consulted because the
    coarse path joins these names with the local ``Path``: on Windows ``..\\x`` is a
    parent reference and ``C:\\x`` is absolute, neither of which POSIX parsing sees.

    A DRIVE is rejected whether or not the path is absolute, which is not the same
    question. ``C:.ssh/id_rsa`` is drive-RELATIVE - ``is_absolute()`` is False - yet
    joining it onto the batch on Windows replaces the anchor, because pathlib lets a
    right-hand side that carries a drive take over, and it then resolves against that
    drive's working directory. So the size read would stat a path outside the batch and
    report its existence and size. Asking for ``.drive`` covers every spelling of that,
    including ``c:y``, which a check for a component ending in a colon does not see.

    That also refuses a POSIX file legitimately named ``a:b``, since Windows parsing
    reads the same two characters as a drive. Accepted deliberately: this store names
    its own files, so nothing it writes looks like that, and the cost of being wrong is
    one batch reported incomplete and kept rather than a path escaping the batch.

    An embedded NUL is refused with no such trade, because no file name can contain one:
    a JSON manifest CAN carry it (``"\\u0000"``), and every ``os`` call then raises
    ``ValueError``, which is not ``OSError`` and so was not caught where a bad name is
    meant to be skipped - it escaped mid-loop and left the batch half-deleted.
    """
    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        logger.warning("refusing a staged path that is absolute")
        return None
    if PureWindowsPath(rel).drive:
        logger.warning("refusing a staged path that names a drive")
        return None
    parts = PurePosixPath(rel).parts
    if not parts or any(
        part in ("", ".", "..") or "\\" in part or "\x00" in part for part in parts
    ):
        logger.warning("refusing a staged path that is not a plain name")
        return None
    return parts


def _listed_bytes(batch: Path, rels: list[str]) -> int:
    """Best-effort size of the manifest's files plus the manifest itself.

    Statting the named files rather than walking the tree, for the same reason the
    delete does not walk it - and only names that pass :func:`_plain_parts`, so a
    tampered entry cannot make this measure a file outside the batch.
    """
    total = 0
    for rel in rels:
        parts = _plain_parts(rel)
        if parts is None:
            continue
        try:
            total += batch.joinpath(*parts).lstat().st_size
        # ValueError as well as OSError: a name the validator somehow still admits must
        # cost this ONE file, never the whole read. `os` raises ValueError, not OSError,
        # for a name it cannot even pass to the kernel.
        except (OSError, ValueError):
            continue
    try:
        total += (batch / MANIFEST_NAME).lstat().st_size
    except OSError:
        pass
    return total


def _open_absolute_nofollow(path: Path) -> tuple[int, int]:
    """Open *path* and its parent, walking from the filesystem root.

    Returns ``(parent fd, path fd)``. The parent is returned because removing the
    directory itself must also happen by descriptor: ``rmtree`` and ``rmdir`` take a
    PATH, which re-resolves the whole prefix, so the last step of the delete would
    reopen the batch through exactly the ancestors this walk exists to pin.

    The walk itself is :func:`kiro_crew.pinned_fs.pin_parent` - one ``openat`` per
    component, each ``O_NOFOLLOW`` - rather than a second copy of it here. ``O_NOFOLLOW``
    constrains only the LAST component, so opening the batch by its path left every
    ancestor to be re-resolved by the kernel: the trash root and the directories above it
    are writable by the same user, and one swapped to a link after validation is followed,
    after which every unlink through the returned descriptors points outside the trash.

    *path* must already be RESOLVED, which is both what ``pin_parent`` requires and what
    makes demanding it safe rather than brittle: a resolved path contains no links, so
    ``O_NOFOLLOW`` can only fail because something changed underneath - the case worth
    failing on - and not on an install whose data home legitimately sits behind a symlinked
    home directory, which a naive "no links anywhere" rule would refuse outright.
    """
    if path.parent == path:
        raise OSError(f"refusing to open {path}: it has no parent")
    parent = pinned_fs.pin_parent(str(path.parent), what="trash batch", refusal=OSError)
    try:
        fd = os.open(path.name, _dir_open_flags(), dir_fd=parent)
    except OSError:
        _close_all((parent,))
        raise
    return parent, fd


def _remove_scanned_dirs(
    batch_fd: int,
    dirs: dict[tuple[str, ...], int],
    device: int,
    cache: dict[tuple[str, ...], int],
) -> None:
    """Remove the batch's directories, deepest first, each by ``(parent fd, name)``.

    Returns nothing: the fresh post-condition scan is what decides whether the batch is
    empty, so a second answer from here was bookkeeping no caller read. Replaces a recursive
    sweep that re-discovered
    children by name and descended into whatever it found: a directory swapped in after
    verification was recursed into and its links and empty directories removed, because
    discovery cannot tell an approved directory from a substituted one.

    Driven by the APPROVED map instead, so there is nothing to discover. Each parent is
    reached through :func:`_open_chain`, which admits a component only as the inode the
    approval recorded, and the removal is ``rmdir`` relative to that descriptor - so a
    directory the approval never named is not opened, not descended into, and not removed.

    Deepest first, by component count, because ``rmdir`` needs a directory to be empty and
    a parent cannot go before its children. Iterative for the same reason the scan is: a
    deep tree must not raise ``RecursionError``.

    The removal itself goes through a rename to an unguessable name, because ``rmdir``
    addresses a NAME and so does the check above it: an actor with write access to the
    parent could swap the name between the two and have an unapproved directory removed on
    another one's approval. That mechanism is
    :func:`kiro_crew.pinned_fs.remove_dir_verified`, which is where the batch directory and
    the whole-tree removal get it too - one owner rather than a copy per site. What stays
    here is the policy: this path logs and CONTINUES, because the post-condition scan below
    is what decides whether the batch is incomplete.
    """
    for key in sorted(dirs, key=len, reverse=True):
        try:
            # The FULL key, so the directory about to be removed is itself checked against
            # the approved inode - not merely the chain leading to it. A top-level staged
            # directory's parent IS the batch, so verifying only the parent verified
            # nothing about it, and `rmdir` addresses a NAME: an empty directory swapped
            # into that name would have been removed on the approval of another one.
            _open_chain(batch_fd, key, cache=cache, dirs=dirs, device=device)
            parent = _open_chain(batch_fd, key[:-1], cache=cache, dirs=dirs, device=device)
        except OSError as exc:
            logger.warning("could not reach staged directory %r: %s", key[-1], exc)
            continue
        outcome = pinned_fs.remove_dir_verified(
            parent,
            key[-1],
            expect=(device, dirs[key]),
        )
        if outcome.removed:
            continue
        if outcome.reason == pinned_fs.REMOVAL_IDENTITY_CHANGED:
            # The object under the staging name is NOT the approved directory, so the listed
            # name is not ours to write to either. An actor who swapped the directory and
            # then placed something at the listed name would have had a rename-back destroy
            # it. It stays under the unguessable name, logged, and the post-condition
            # reports the batch incomplete.
            logger.error(
                "refusing a staged directory that is not the one that was approved; it is "
                "now %r and is left there rather than renamed back",
                outcome.staged_name,
            )
        elif outcome.reason == pinned_fs.REMOVAL_UNVERIFIABLE:
            # Also not put back: what is under the staging name is unknown, so renaming it
            # onto the listed name would replace whatever is there now.
            logger.warning(
                "could not re-check staged directory %r; leaving it as %r: %s",
                key[-1],
                outcome.staged_name,
                outcome.error,
            )
        elif outcome.reason == pinned_fs.REMOVAL_STAGE_FAILED:
            logger.warning(
                "could not stage staged directory %r for removal: %s", key[-1], outcome.error
            )
        else:
            logger.warning("could not remove staged directory %r: %s", key[-1], outcome.error)
            if outcome.staged_name is not None:
                # The identity matched, so this IS the approved directory - but it could not
                # be put back, and a directory under a staging name is one nothing
                # recognises.
                logger.error(
                    "staged directory %r is now %r and could not be put back",
                    key[-1],
                    outcome.staged_name,
                )


def _unlink_debris(parent_fd: int, debris: str, expect_ino: int | None) -> None:
    """Unlink the moved-aside manifest, only while that name still holds it.

    The debris name lives in the trash root, and by the time this runs the batch removal has
    either succeeded or failed - so time has passed in a directory an actor may be able to
    write to. Unlinking by name alone would destroy whatever answers to it by then, which is
    the same trusted-a-name mistake every other removal on this path was rewritten to avoid.
    Best effort: a name that no longer holds the manifest is left alone, not chased.
    """
    if expect_ino is None:
        return
    with suppress(OSError):
        if os.stat(debris, dir_fd=parent_fd, follow_symlinks=False).st_ino == expect_ino:
            os.unlink(debris, dir_fd=parent_fd)


def _inode_of(name: str, dir_fd: int) -> int | None:
    """Return *name*'s inode relative to *dir_fd*, or None if it cannot be read.

    Links are not followed: the question is always what this NAME holds, never what it
    points at.
    """
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_ino
    except OSError:
        return None


def _remove_pinned_batch(parent_fd: int, batch_fd: int, name: str) -> None:
    """Remove the batch directory, or raise ``OSError`` having removed nothing.

    The last `rmdir` had the same flaw the interior ones did, one level up: the final scan
    proves the batch is empty by DESCRIPTOR, and then the removal addressed a NAME. A late
    file plus a swap of the batch's name in that interval removed an empty replacement
    instead - and by then the manifest has already been moved aside, so the real batch was
    left holding data with nothing to list it, while the caller reported success.

    So the name is moved to one nothing can predict, its identity is checked against the
    descriptor the whole operation was pinned to, and only that name is removed - by
    :func:`kiro_crew.pinned_fs.remove_dir_verified`, the same primitive the interior
    directories use. Raising rather than reporting lets the caller's existing recovery run
    unchanged: it renames the manifest back through ``batch_fd``, so it lands in the REAL
    batch and the batch stays listed and restorable.
    """
    expected = os.fstat(batch_fd)
    outcome = pinned_fs.remove_dir_verified(
        parent_fd,
        name,
        expect=(expected.st_dev, expected.st_ino),
    )
    if outcome.removed:
        return
    if outcome.reason == pinned_fs.REMOVAL_STAGE_FAILED:
        # Nothing was moved, so there is nothing to report about a staging name. Raised as
        # the OSError the rename itself produced, which is what this function's callers
        # already handle.
        raise outcome.error or OSError(f"could not stage the batch for removal: {name!r}")
    if outcome.reason in (pinned_fs.REMOVAL_IDENTITY_CHANGED, pinned_fs.REMOVAL_UNVERIFIABLE):
        # NOT renamed back. Whatever is under the staging name is not the pinned batch, so
        # the batch's name is not ours to write to either - and POSIX rename REPLACES its
        # destination, so putting this back would destroy whatever now answers to that name.
        # It stays under the unguessable name, named in the log for a human to deal with.
        logger.error(
            "the batch name no longer denotes the pinned batch; what was there is now %r "
            "in the trash root and is left there rather than renamed back",
            outcome.staged_name,
        )
        raise OSError(f"the batch name no longer denotes the pinned batch: {name!r}")
    if outcome.staged_name is not None:
        # The identity matched, so this IS the pinned batch - but the rmdir failed AND it
        # could not be renamed back, so it is now under a name `list_trash` does not offer.
        logger.error(
            "staged directory %r is now %r and could not be put back", name, outcome.staged_name
        )
    raise outcome.error or OSError(f"could not remove the batch directory: {name!r}")


def _stage_batch_by_name(batch: Path, *, expect: tuple[int, int]) -> tuple[Path | None, str | None]:
    """Rename *batch* aside inside the trash root and verify its identity THERE.

    The half of rename-verify-remove that works on a platform with no ``openat``: there is
    no descriptor to bind a removal to, and checking the path and then handing that same
    path to a remover re-resolves it, so a swap in between is followed and an unapproved
    replacement destroyed. The rename is what makes the check meaningful rather than
    decorative. After it the approved name no longer exists, so nothing can be substituted
    at it; the identity is verified on the RENAMED directory, so a swap that landed before
    the rename is caught and the impostor is put back rather than removed; and the name a
    caller finally removes existed for microseconds and carries random characters.

    NOT airtight, and better said than implied: the caller's removal still resolves the
    staging path, so an actor who can OBSERVE that name inside the window can redirect it
    through an ancestor swapped afterwards. That residual is accepted on this platform
    because the alternative is worse in both directions - refusing the empty a user
    explicitly asked for, or leaving a batch behind after every restore, which no test and
    no user would read as success.

    Returns ``(staged path, None)`` when the caller may remove it, or ``(None, skip code)``
    with the batch already back under the name the user saw. One owner for three callers:
    the explicit empty, and the two cleanups that follow work which already succeeded.
    """
    staging = batch.parent / f".{batch.name}.removing-{uuid.uuid4().hex[:8]}"
    try:
        os.rename(batch, staging)
    except OSError as exc:
        logger.warning("refusing to remove trash batch %r: %s", batch.name, exc)
        return None, SKIP_UNREADABLE
    try:
        current = os.stat(staging, follow_symlinks=False)
    except OSError as exc:
        logger.warning("refusing to remove trash batch %r: %s", batch.name, exc)
        _rename_back(staging, batch)
        return None, SKIP_UNREADABLE
    if (current.st_dev, current.st_ino) != expect:
        logger.warning(
            "refusing to remove trash batch %r: it is not the directory that was selected",
            batch.name,
        )
        _rename_back(staging, batch)
        return None, SKIP_IDENTITY_CHANGED
    return staging, None


def _rename_back(staging: Path, batch: Path) -> None:
    """Put a staged-for-removal directory back under the name the user saw.

    Best effort by necessity - if this fails there is nothing further to try - but logged
    loudly, because a batch left under a staging name is one `list_trash` does not offer:
    visible nowhere, restorable never. The staging name carries the batch id so a human
    can finish the job by hand.
    """
    try:
        os.rename(staging, batch)
    except OSError:
        logger.error(
            "could not restore trash batch %r from its staging name %r; it is on disk but "
            "will not be listed until the directory is renamed back",
            batch.name,
            staging.name,
        )


def _coarse_remove(
    target: Path,
    rels: list[str],
    on_progress: EmptyProgress | None,
    base_bytes: int,
) -> tuple[int, str | None]:
    """``shutil.rmtree`` with an honest byte figure and a post-condition.

    The removal for platforms with neither ``openat`` nor ``O_NOFOLLOW``. The bytes are
    measured AFTER the attempt and the survivors subtracted, because ``ignore_errors``
    means a locked file - the normal Windows failure - leaves the tree standing while
    ``rmtree`` returns quietly, and the up-front figure would then be reported as freed
    with the bytes still on disk. Whether the directory actually went is the separate
    question :func:`_incomplete_if_present` answers.
    """
    before = _listed_bytes(target, rels)
    shutil.rmtree(target, ignore_errors=True)
    freed = before - _listed_bytes(target, rels)
    if on_progress is not None:
        on_progress(base_bytes + freed)
    return freed, _incomplete_if_present(target)


def _delete_listed_files(
    batch: Path,
    on_progress: EmptyProgress | None,
    base_bytes: int,
    expect_identity: BatchIdentity | None = None,
) -> tuple[int, str | None]:
    """Delete the files the manifest names, reporting bytes freed as it goes.

    Returns ``(bytes freed, skip code or None)``. A skip code means the batch is still
    there and the caller must say so: reporting bytes alone left a batch that survived
    looking exactly like an empty one - "0 bytes freed, success" - which is the silent
    refusal this module's skip codes exist to remove. The two ways that happens are the
    batch not opening at all, and the tree not being gone afterwards.

    Driven by the MANIFEST, not by a directory walk, and that is a safety property
    rather than a convenience. A walk has to decide per directory entry whether to
    descend, and on Windows a junction is not a symlink - ``os.path.islink`` reports
    False for one - so ``os.walk`` descends into it and would unlink the files it
    points at, outside the trash entirely. Naming the files means nothing is ever
    discovered by traversal.

    Each file is then removed by ``(directory fd, name)``, with every directory
    component opened ``O_NOFOLLOW`` from the batch down. That is what makes the
    per-file delete safe rather than merely validated: checking a path and then
    unlinking it re-resolves the prefix, so a component swapped to a link in between is
    followed and the same-named file outside the trash deleted. Nothing here resolves a
    path at unlink time, so there is no window to win.

    Removing the emptied directories is by descriptor too, bottom-up, so no step of
    this path ever resolves a path - not even the last one. Finishing with
    ``rmtree(batch)`` re-resolved the whole prefix, which reopened the batch through
    exactly the ancestors the walk above exists to pin. Callers reach here only after
    :func:`_unlisted_files` has confirmed the batch holds nothing the manifest omits,
    so "every listed file" is every file.

    Progress is per file rather than per batch because that is the point of naming the
    files: the store this exists for stages a single batch holding tens of thousands of
    sessions, where "one batch done" is one step from nothing to finished, minutes
    later. Where the platform can neither open a directory refusing to follow a link
    nor delete relative to one - Windows has neither - the coarse ``rmtree`` is taken
    instead, with the bytes measured after the attempt and the survivors subtracted,
    and progress degrades to one report per batch. A smoother bar is not worth a weaker
    delete.

    ``base_bytes`` is what earlier batches in the same call already freed, so the
    number handed to ``on_progress`` is always the total for the whole operation and
    never a figure that restarts per batch.
    """
    # BEFORE the manifest is read, and before the platform branch, because both paths
    # destroy data on the strength of what it says. A symlink or an NTFS junction at this
    # name was not written here - the product writes it with `atomic_write` - so its
    # entries describe something else, and on the coarse path `rmtree` would remove the
    # link while a locked staged file survived, leaving a batch that `list_trash()` omits
    # with data still inside it.
    #
    # `is_link_or_junction` rather than `is_symlink()`: on Windows a junction reports False
    # for the latter, and the coarse path is the Windows path.
    #
    # The descriptor path checks this again from its pinned scan. That is not duplication:
    # this one is computed from a path and so cannot see a link planted after it, while the
    # scan's view can - and only this one runs on the coarse path at all.
    if platform_compat.is_link_or_junction(batch / MANIFEST_NAME):
        logger.warning(
            "refusing to empty %r: its manifest is a link, so its listing is not this "
            "batch's own",
            batch.name,
        )
        return 0, SKIP_UNREADABLE
    rels = _manifest_rels(batch)
    # The listing is WHAT the delete is allowed to remove, so it has to be the listing the
    # approval was computed from. Its inode is not enough: rewritten in place the manifest
    # keeps that inode, and every other identity check still passes because none of the
    # FILES changed - only which of them the delete now believes it may unlink. A file
    # already sitting in the batch, unlisted, is refused today; add it to the listing after
    # the approval and it would be deleted as if the user had approved it.
    if expect_identity is not None and expect_identity.rels_digest is not None:
        if _rels_digest(rels) != expect_identity.rels_digest:
            logger.warning(
                "refusing to empty %r: its manifest lists different files than the ones "
                "that were selected",
                batch.name,
            )
            return 0, SKIP_IDENTITY_CHANGED
    if not _FD_SAFE_DELETE:
        if expect_identity is None:
            return _coarse_remove(batch, rels, on_progress, base_bytes)
        # Rename aside inside the trash root, verify the identity THERE, remove only the
        # staged name - see :func:`_stage_batch_by_name` for why the rename is what makes
        # the check meaningful, and for the residual this platform accepts. Shared with the
        # two cleanup paths rather than respelled here.
        staging, skip = _stage_batch_by_name(
            batch, expect=(expect_identity.dev, expect_identity.ino)
        )
        if staging is None:
            return 0, skip
        freed, skip = _coarse_remove(staging, rels, on_progress, base_bytes)
        if skip is not None:
            # A tree that would not go must not be left under a staging name: `list_trash`
            # would not offer it, so the batch would be neither visible nor restorable. Put
            # the name back, which is where the user last saw it.
            _rename_back(staging, batch)
        return freed, skip

    try:
        parent_fd, batch_fd = _open_absolute_nofollow(batch)
    except OSError as exc:
        # A refusal, not a silent success: the batch is still on disk and still on the
        # user's screen, and an open that fails here is exactly the ancestor swap this
        # walk exists to catch.
        logger.warning("refusing to empty %r: %s", batch.name, exc)
        return 0, SKIP_UNREADABLE

    cache: dict[tuple[str, ...], int] = {}
    freed = 0
    files = 0
    cleared = False
    try:
        try:
            # The approved directory, or nothing. `batch` is a NAME, and the mutation lock
            # was released between the snapshot that showed the user what would go and this
            # open - so a directory moved into that name would be deleted on the strength of
            # consent given for a different one. Checked on the DESCRIPTOR rather than by
            # another stat of the path: the fd is the object every removal below actually
            # addresses, so a further swap after this point does not reach the data. Nothing
            # is deleted yet, so refusing here costs only the batch.
            #
            # Unconditional even when there is nothing to compare against, because the same
            # call supplies the device every directory INSIDE the batch is pinned to.
            try:
                opened = os.fstat(batch_fd)
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning("refusing to empty %r: %s", batch.name, exc)
                return 0, SKIP_UNREADABLE
            if expect_identity is not None and (opened.st_dev, opened.st_ino) != (
                expect_identity.dev,
                expect_identity.ino,
            ):
                logger.warning(
                    "refusing to empty %r: it is not the directory that was selected",
                    batch.name,
                )
                return 0, SKIP_IDENTITY_CHANGED
            device = opened.st_dev
            # One traversal of the batch through its own descriptor, before anything is
            # removed: it records the inode of every directory inside it and names every
            # file actually present. The caller's `_unlisted_files` asks the same "is
            # anything here unaccounted for" question by PATH; this re-establishes it from
            # the pinned descriptor, so the answer the delete acts on is the one it can
            # bind to.
            dirs: dict[tuple[str, ...], int] = {}
            present: dict[tuple[str, ...], int] = {}
            links: dict[tuple[str, ...], int] = {}
            try:
                scanned = _scan_tree(batch_fd, device=device)
            except OSError as exc:
                logger.warning("refusing to empty %r: %s", batch.name, exc)
                return 0, SKIP_UNREADABLE
            dirs, present, links = scanned.dirs, scanned.files, scanned.links
            # The interior has to match the APPROVAL, not merely be self-consistent. A map
            # built here records whatever is present by now - a directory swapped in during
            # the handoff included - so it cannot be the thing that authorises the delete.
            # `staged_targets` recorded the same map under the mutation lock; equality is
            # demanded in both directions, so a directory added, removed or replaced since
            # then is a refusal rather than something to reconcile. A concurrent restore
            # that removed a staged directory lands here too, and refusing is right: the
            # approval no longer describes the batch.
            approved = None if expect_identity is None else expect_identity.dirs
            if approved is not None and dirs != approved:
                logger.warning(
                    "refusing to empty %r: its directories are not the ones that were " "selected",
                    batch.name,
                )
                return 0, SKIP_IDENTITY_CHANGED
            verify = dirs if approved is None else approved
            # The files and links get the same treatment, and for a sharper reason than
            # symmetry: the per-file identity check further down compares each name against
            # the scan taken HERE, which is self-consistent and authorises nothing. A listed
            # file replaced during the handoff had its replacement's inode recorded, matched,
            # and was unlinked - an unapproved file destroyed on consent given for a
            # different one. Comparing the whole map against the approval refuses the batch
            # instead. A concurrent restore that removed staged files lands here too, and
            # refusing is right for the same reason it is right for directories: the
            # approval no longer describes the batch.
            if expect_identity is not None:
                for label, seen_now, was in (
                    ("files", present, expect_identity.files),
                    ("links", links, expect_identity.links),
                ):
                    if was is not None and seen_now != was:
                        logger.warning(
                            "refusing to empty %r: its %s are not the ones that were selected",
                            batch.name,
                            label,
                        )
                        return 0, SKIP_IDENTITY_CHANGED
            # A manifest that is a SYMLINK is not this batch's manifest. It matters here
            # rather than at read time because the link loop below unlinks every recorded
            # link, and the manifest is only excluded from the FILE loop - so a symlink at
            # that name is removed early, defeating the whole reason the real manifest goes
            # last. What that costs is not the link but the listing: `list_trash()` omits a
            # batch with no readable manifest, so a batch that then fails to empty (one
            # unwritable directory, one file held open) leaves the user data on disk they
            # can neither see nor restore.
            #
            # Refusing beats deferring it to the end. The product writes this file with
            # `atomic_write`, so a symlink here was not written by us, and the entries the
            # approval was computed from were read THROUGH it - they may not describe this
            # batch at all. Deleting on the strength of that is the one outcome worth
            # avoiding, and a batch kept costs the user a second attempt.
            if (MANIFEST_NAME,) in links:
                logger.warning(
                    "refusing to empty %r: its manifest is a symlink, so its listing is "
                    "not this batch's own",
                    batch.name,
                )
                return 0, SKIP_UNREADABLE
            listed = {p for p in (_plain_parts(rel) for rel in rels) if p is not None}
            # The batch's own manifest is accounted for by definition. A file named
            # `manifest.jsonl` deeper in the tree is not: it is data nothing lists, and
            # keeping it is the safe direction.
            unaccounted = [p for p in present if p != (MANIFEST_NAME,) and p not in listed]
            if unaccounted:
                logger.warning(
                    "refusing to empty %r: %d staged file(s) are absent from its manifest, "
                    "so this would delete the only copy",
                    batch.name,
                    len(unaccounted),
                )
                return 0, SKIP_UNLISTED_FILES
            for rel in rels:
                # No absolute path, no traversal, no empty component: a tampered manifest
                # does not get to name anything but a plain path under this batch.
                parts = _plain_parts(rel)
                if parts is None:
                    continue
                # Nor does it get to name the MANIFEST, which is the batch's own recovery
                # metadata: `list_trash()` omits a batch without a readable one, so
                # removing it here - before the sweep has proved every other file gone -
                # turns any surviving session data into files the user can neither see nor
                # restore. The manifest is removed once, at the end, below.
                if parts == (MANIFEST_NAME,):
                    logger.warning("refusing a staged entry that names the manifest")
                    continue
                try:
                    holder = _open_chain(
                        batch_fd, parts[:-1], cache=cache, dirs=verify, device=device
                    )
                    info = os.stat(parts[-1], dir_fd=holder, follow_symlinks=False)
                # ValueError as well as OSError, and it matters more here than anywhere: an
                # exception escaping this loop stops an IRREVERSIBLE operation partway, with
                # earlier files already unlinked. `os` raises ValueError, not OSError, for a
                # name it cannot hand to the kernel at all, so a single bad entry would abort
                # the batch instead of costing itself. One unusable name must skip its own
                # file and leave the batch reported incomplete and restorable.
                except (OSError, ValueError):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    continue
                # The leaf is the one thing a descriptor cannot hold: POSIX has no
                # unlink-by-inode, so this addresses a NAME. What it can do is refuse a
                # name that no longer denotes the object the pinned scan saw there - which
                # closes the interval between that scan and this unlink, and leaves only
                # the two syscalls between the stat above and the unlink below, plus a file
                # substituted before the scan under a name the manifest already lists.
                # Stated as a residual in the PR rather than implied to be closed.
                seen = present.get(parts)
                if seen is None or (info.st_dev, info.st_ino) != (device, seen):
                    logger.warning("refusing a staged file that is not the one that was scanned")
                    continue
                try:
                    os.unlink(parts[-1], dir_fd=holder)
                except (OSError, ValueError):
                    continue
                freed += info.st_size
                files += 1
                # Reporting per file would call back six figures of times for one batch;
                # this is often enough that a reader sees the number move.
                if on_progress is not None and files % _PROGRESS_EVERY_FILES == 0:
                    on_progress(base_bytes + freed)
            # Links recorded by the scan go too. Removing a link destroys nothing - the
            # thing it points at is untouched, and nothing here ever follows one - while
            # leaving them would make a batch holding one impossible to empty for good.
            #
            # But "it is only a link" is a statement about what the SCAN saw, not about
            # what this name holds now, and this is an unlink by name like any other. A
            # regular file moved onto a recorded link's name in between is data, and
            # deleting it would be the very loss the file loop's identity check exists to
            # prevent - so the same check applies here: still a link, and still the one
            # that was recorded.
            for key, seen_ino in links.items():
                try:
                    holder = _open_chain(
                        batch_fd, key[:-1], cache=cache, dirs=verify, device=device
                    )
                    info = os.stat(key[-1], dir_fd=holder, follow_symlinks=False)
                except (OSError, ValueError):
                    continue
                if not stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (
                    device,
                    seen_ino,
                ):
                    logger.warning("refusing a staged link that is not the one that was scanned")
                    continue
                with suppress(OSError, ValueError):
                    os.unlink(key[-1], dir_fd=holder)
            # The manifest is not one of its own entries, and on a batch of this size it is
            # not a rounding error, so its size is counted here - but it is NOT removed yet.
            # It is the only thing that makes the batch restorable: `list_trash()` omits a
            # batch with no readable manifest, so deleting it while a listed file still
            # survived (an unwritable staged directory, a file held open) left the user with
            # data on disk they could neither see nor restore. It goes last, only once
            # nothing else is left.
            manifest_bytes = 0
            with suppress(OSError):
                manifest_bytes = os.stat(
                    MANIFEST_NAME, dir_fd=batch_fd, follow_symlinks=False
                ).st_size
            # The descriptor cache is dropped FIRST, so the removal phase re-opens every
            # directory and re-checks its inode. Reusing a descriptor cached during the
            # file phase would satisfy the check with the identity of the directory that
            # was there THEN, and the `rmdir` below addresses a name - so an empty
            # directory swapped in afterwards was removed on the strength of it.
            _drain(cache)
            # The directories the APPROVAL named, deepest first, each through a descriptor
            # chain that admits only the approved inode. Nothing is discovered by walking,
            # so a directory substituted after verification is not opened and not removed.
            _remove_scanned_dirs(batch_fd, dirs, device, cache)
            _drain(cache)
            # The post-condition, read the same way everything else here is: a fresh pinned
            # scan, and nothing may be left but the manifest. This replaces a recursive
            # sweep that answered "is it empty" while also deciding what to delete - two
            # jobs, and the deciding half could be pointed at a directory nobody approved.
            left_dirs: dict[tuple[str, ...], int] = {}
            left_files: dict[tuple[str, ...], int] = {}
            left_links: dict[tuple[str, ...], int] = {}
            try:
                left = _scan_tree(batch_fd, device=device)
            except OSError as exc:
                logger.warning("emptying %r: could not confirm it is empty: %s", batch.name, exc)
                cleared = False
            else:
                left_dirs, left_files, left_links = left.dirs, left.files, left.links
                # By INODE, not by name. Everything after this point treats the survivor as
                # the batch's own manifest: it is renamed aside and, once the batch is gone,
                # the debris is unlinked. So a file substituted at that name after the first
                # scan would be accepted here and then destroyed - an unapproved file, whose
                # only copy this is, deleted on the strength of matching a name. The first
                # scan recorded what the manifest WAS, and that is what has to still be here.
                cleared = (
                    not left_dirs
                    and not left_links
                    and list(left_files) == [(MANIFEST_NAME,)]
                    and left_files[(MANIFEST_NAME,)] == present.get((MANIFEST_NAME,))
                )
                if not cleared and list(left_files) == [(MANIFEST_NAME,)]:
                    logger.warning(
                        "emptying %r: the file at its manifest's name is not the one that "
                        "was scanned, so it is left alone",
                        batch.name,
                    )
            if cleared:
                # The manifest and the batch directory have to go TOGETHER, and `rmdir`
                # cannot run while the manifest is still in there. Unlinking it first left a
                # window that a file created after the sweep turned into silent loss: the
                # rmdir then failed on a non-empty directory, and the batch - now without a
                # manifest - vanished from `list_trash()` with that data inside it,
                # unreachable and unrestorable.
                #
                # So it is MOVED to the trash root under a debris name instead of deleted.
                # From there the batch can be removed, and if that fails the manifest is
                # renamed straight back, leaving the batch listed and restorable exactly as
                # it was. `list_trash()` enumerates directories only, so the debris name is
                # never mistaken for a batch, and a crash between the two renames leaves one
                # small file rather than an unreadable batch.
                # The suffix is RANDOM, like the coarse path's staging name and for the same
                # reason: `os.rename` replaces an existing destination silently on POSIX, so
                # a deterministic name is one an actor with write access to the trash root
                # can plant a file at and have this destroy its only copy. A name nothing
                # can predict cannot be squatted.
                debris = f".{batch.name}.{MANIFEST_NAME}.removing-{uuid.uuid4().hex[:8]}"
                moved_aside = True
                try:
                    os.rename(MANIFEST_NAME, debris, src_dir_fd=batch_fd, dst_dir_fd=parent_fd)
                except OSError as exc:
                    logger.warning("emptying %r left its manifest in place: %s", batch.name, exc)
                    cleared = False
                    moved_aside = False
                # The post-condition verified the manifest's inode, and this rename addresses
                # its NAME - two syscalls apart, the same irreducible interval the leaf unlink
                # has, because POSIX has no rename-by-inode either. What CAN be checked is
                # what landed: if the debris is not the file that was verified, the rename
                # moved something else, and the unlink at the end of this block would destroy
                # it. So it is left as debris rather than removed, and both names are logged.
                # The real manifest was already replaced by then - that loss is not this
                # code's to undo - but it does not have to add a second one.
                if moved_aside:
                    landed = _inode_of(debris, parent_fd)
                    if landed != present.get((MANIFEST_NAME,)):
                        logger.error(
                            "emptying %r moved a file that is not its manifest aside as %r; "
                            "leaving it in the trash root rather than deleting it (inode %r, "
                            "expected %r)",
                            batch.name,
                            debris,
                            landed,
                            present.get((MANIFEST_NAME,)),
                        )
                        cleared = False
                        moved_aside = False
                if moved_aside:
                    try:
                        _remove_pinned_batch(parent_fd, batch_fd, batch.name)
                    except OSError as exc:
                        logger.warning("emptying %r did not remove it: %s", batch.name, exc)
                        cleared = False
                        restored = False
                        # `landed` equalled this inode above, which is what let this branch be
                        # reached at all. Bound to a local, and the recovery skipped outright
                        # if it is unknown: without an identity to check, the debris name is
                        # just a name in a directory an actor may be able to write to.
                        debris_ino = present.get((MANIFEST_NAME,))
                        # Never `rename`: POSIX rename REPLACES its destination silently,
                        # which is the property the debris name above is chosen to be safe
                        # against - and this direction needs the opposite guarantee. If
                        # anything has created a `manifest.jsonl` in the batch since ours
                        # was moved aside, renaming over it would destroy the only copy of a
                        # file this code has never read.
                        #
                        # `pinned_fs.put_back_no_clobber` owns that: the debris name is opened
                        # `O_NOFOLLOW` and checked against `debris_ino` before anything is read
                        # from it, `os.link` first because it cannot clobber, and an
                        # `O_CREAT | O_EXCL` create-and-copy where the MOUNT has no hard links
                        # - which the `os.supports_dir_fd` probe cannot see, since it tests
                        # what the OS accepts and not what the filesystem does. The debris is
                        # unlinked only once the batch has its manifest back, so no window has
                        # neither.
                        back: str | None = pinned_fs.PUT_BACK_FAILED
                        if debris_ino is not None:
                            back = pinned_fs.put_back_no_clobber(
                                parent_fd,
                                batch_fd,
                                debris,
                                MANIFEST_NAME,
                                expect_ino=debris_ino,
                            )
                        restored = back is None
                        if back == pinned_fs.PUT_BACK_NAME_TAKEN:
                            # Something arrived at the name. NOT overwritten - that is the
                            # whole reason this is not a rename - and the batch is still
                            # listable, because whatever arrived is a manifest by name and
                            # `list_trash()` can read it. The debris stays for a human.
                            logger.error(
                                "emptying %r could not restore its manifest because another "
                                "file now holds that name; the manifest is %r in the trash "
                                "root",
                                batch.name,
                                debris,
                            )
                        elif not restored:
                            logger.error(
                                "emptying %r removed neither the batch nor its manifest; "
                                "the manifest is now %r in the trash root",
                                batch.name,
                                debris,
                            )
                        if restored:
                            _unlink_debris(parent_fd, debris, debris_ino)
                    else:
                        _unlink_debris(parent_fd, debris, present.get((MANIFEST_NAME,)))
                        freed += manifest_bytes
        finally:
            _drain(cache)
            _close_all((batch_fd,))
    finally:
        _close_all((parent_fd,))
    if on_progress is not None:
        on_progress(base_bytes + freed)
    return freed, None if cleared else SKIP_INCOMPLETE


def _incomplete_if_present(batch: Path) -> str | None:
    """``SKIP_INCOMPLETE`` if the batch survived its own delete, else None.

    For the COARSE path only. It calls ``rmtree(ignore_errors=True)``, which reports
    nothing, so a tree it could not remove - a file held open by another process, a
    permission it does not have - left the batch on screen while the job said it had
    succeeded. Whether the directory is actually gone is the one question that
    distinguishes those, and it costs a stat. The descriptor path needs no second look:
    its own sweep already knows whether anything was left.
    """
    try:
        if batch.exists():
            logger.warning("emptying %r did not remove it", batch.name)
            return SKIP_INCOMPLETE
    except OSError:
        return SKIP_INCOMPLETE
    return None


def _header_names_this_batch(summary: tuple[dict[str, Any], int, int] | None, name: str) -> bool:
    """Whether a manifest summary's header claims the batch it was read from.

    `list_trash()` already refuses a batch whose header names a different id -- a header
    claiming another batch would make a targeted empty delete the batch it named rather than
    the one it came from. That check happens at LISTING time, and the approval reads the
    manifest again afterwards, so the same rule has to hold on the second read: a directory
    swapped into the selected name between the two brings its OWN manifest, whose header
    names ITS id, and everything else about that approval is self-consistent -- identity,
    files and digest all describe one directory. What they do not describe is the batch the
    user selected. The name is the only link back to that selection, and this is what checks
    it.

    The header is never RESOLVED in favour of, only compared: a disagreement withholds the
    batch, which is the same posture the listing takes.

    It demands EQUALITY rather than tolerating a missing id. `_write_header` always writes
    `batch_id` beside `schema`, and a summary is only returned at all when the header's schema
    is the current one -- so within the schema that reaches this check the id is always
    present, and its absence means the header was tampered with or truncated. Accepting that
    as "nothing to disagree with" is the fail-OPEN reading, and it is the one an actor gets to
    choose: strip the field and the check waves the swap through.
    """
    if summary is None:
        return False
    claimed = summary[0].get("batch_id")
    if claimed != name:
        # %r on both: manifest content is agent-controlled, same forgery risk as the listing.
        logger.warning(
            "refusing to approve %r: its manifest claims batch id %r",
            name,
            claimed,
        )
        return False
    return True


def _approve_batch(path: Path) -> tuple[BatchIdentity, int] | None:
    """What *path* IS and what it holds, both read through ONE pinned descriptor.

    `list_trash()` reads each manifest by path and takes no lock, so its byte totals
    describe whatever answered to that name then. Recording an identity separately, by the
    same name, can straddle a swap: the approval would then carry the REPLACEMENT's
    identity with the original's numbers, and the delete - which checks the identity it was
    given - would destroy session data the user was never shown. Pinning once and taking
    both from that descriptor makes the pair describe one object or nothing.

    None means no identity, and a batch with no identity is dropped from the approved set
    rather than carried unchecked.
    """
    try:
        # Only the PARENT is resolved. `Path.resolve()` follows the final component too, so
        # a batch directory replaced by a symlink resolved to its TARGET and everything below
        # pinned that target - the approval would record another directory's identity under
        # this batch's id, and the delete, checking the identity it was handed, would destroy
        # session data from outside the trash. Re-joining the name onto the resolved parent
        # keeps the walk's `O_NOFOLLOW` on the component that matters, which refuses the link
        # instead of resolving through it.
        resolved = path.parent.resolve() / path.name
    except OSError:
        return None
    if not _FD_SAFE_DELETE:
        # No openat: the interior cannot be walked by descriptor and the coarse removal
        # cannot use such a map anyway. `lstat`, not `stat`, so a batch directory that is
        # itself a link records the LINK - which the delete then refuses - rather than
        # silently resolving through it.
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        summary = _summarize_manifest(path)
        if not _header_names_this_batch(summary, path.name):
            return None
        if summary is None:
            return None
        return (
            BatchIdentity(
                info.st_dev, info.st_ino, None, rels_digest=_rels_digest(_manifest_rels(path))
            ),
            summary[2],
        )
    try:
        parent_fd, batch_fd = _open_absolute_nofollow(resolved)
    except OSError:
        return None
    try:
        info = os.fstat(batch_fd)
        # The listing FIRST, and through the pinned descriptor. Taken after the scan it
        # could be a manifest rewritten in between - recording the NEW listing against the
        # OLD inode map, which authorises exactly the file the digest exists to refuse.
        # Taken by path it could be a different directory's manifest altogether. Ordering
        # it first also fails closed: a rewrite after this point leaves the digest
        # describing the old listing, so the delete refuses.
        digest = _rels_digest(_manifest_rels(path, dir_fd=batch_fd))
        scanned = _scan_tree(batch_fd, device=info.st_dev)
        dirs, files, links = scanned.dirs, scanned.files, scanned.links
        summary = _summarize_manifest(path, dir_fd=batch_fd)
    except OSError:
        return None
    finally:
        _close_all((batch_fd, parent_fd))
    if not _header_names_this_batch(summary, path.name):
        return None
    if summary is None:
        return None
    return (
        BatchIdentity(
            info.st_dev,
            info.st_ino,
            dirs,
            files,
            links,
            rels_digest=digest,
        ),
        summary[2],
    )


def staged_targets(
    batch_ids: list[str] | None = None,
) -> tuple[list[str], int, dict[str, BatchIdentity]]:
    """Which staged batches an empty would destroy, and what they hold.

    Taken under the mutation lock, which is the whole point of it being here rather
    than a list comprehension at the caller. ``move_to_trash`` holds that lock for
    the length of a staging run, and :func:`list_trash` does not take it - so an
    unlocked read can see a batch whose directory and manifest header exist while its
    sessions are still being moved in. Selecting that id then makes the delete wait
    for staging to finish and destroy the FINISHED batch, including sessions appended
    after the user clicked. Under the lock a batch is either fully staged or not
    visible, so the set returned is one a user could actually have seen.

    Returns explicit ids even for "everything", because the caller must be able to
    hand the worker a fixed set: ``empty_trash(None)`` re-enumerates when it runs,
    which is later, and a staged batch is the only copy of its sessions.

    Each id is returned WITH the ``(st_dev, st_ino)`` of the directory it named at
    snapshot time. An id is only a name: the lock is released for the handoff, and a
    directory swapped in its place would be opened by that same name and deleted -
    session data the user was never shown and never approved. The delete re-checks the
    pair against the descriptor it actually opened, which is the object every later
    operation uses, so a replacement is refused rather than destroyed. A batch whose
    directory cannot be stat'd here is dropped from the set: no identity means no
    check, and deleting it unchecked is the one outcome worth avoiding.
    """
    with _mutation_lock():
        # Explicit ids go through the SAME resolver every other caller uses, before
        # anything is filtered. Filtering alone silently dropped an id that is not a
        # batch - a typo, or one already emptied - so the worker received an empty
        # list and reported success for a delete the user asked for and did not get.
        # This keeps the guard `empty_trash` has always applied through `_batch_dir`,
        # which the filter had quietly taken over from.
        for batch_id in batch_ids or []:
            _batch_dir(batch_id)
        wanted = None if batch_ids is None else set(batch_ids)
        chosen = [batch for batch in list_trash() if wanted is None or batch.batch_id in wanted]
        if wanted is not None:
            # A well-formed id whose batch is not staged - already emptied, or never
            # existed - passes `_batch_dir`, which does not require the directory to
            # exist. Filtering it out silently turned "destroy this" into a zero-byte
            # success. It is a refusal: the caller named something that is not there,
            # and on the pre-snapshot path the batch would at least have reported an
            # unreadable-batch skip once it reached the delete.
            missing = wanted - {batch.batch_id for batch in chosen}
            if missing:
                raise SessionStorageError(
                    f"{len(missing)} of the batches named are no longer staged"
                )
        identities: dict[str, BatchIdentity] = {}
        ids: list[str] = []
        total = 0
        unapprovable: list[str] = []
        for batch in chosen:
            # Identity AND size from one pinned descriptor. `batch.bytes` came from
            # `list_trash()`, which reads by path under no lock, so pairing it with a
            # separately-stat'd identity could describe two different directories.
            approved = _approve_batch(_batch_dir(batch.batch_id))
            if approved is None:
                # No identity means no check, so it must NOT be deleted. It is still returned
                # in the id list: `empty_trash` refuses an id that a supplied approval map
                # does not name, so the batch comes back as a skip with a reason. Dropping it
                # here instead left the user a success message above a batch still on screen,
                # which is the bug the missing-id refusal above exists to prevent.
                logger.warning(
                    "refusing to approve %r: its identity or its manifest could not be read",
                    batch.batch_id,
                )
                unapprovable.append(batch.batch_id)
                ids.append(batch.batch_id)
                continue
            recorded, staged_bytes = approved
            identities[batch.batch_id] = recorded
            ids.append(batch.batch_id)
            total += staged_bytes
        if unapprovable and wanted is not None:
            # Only for a NAMED selection, which is exactly the asymmetry the missing-id rule
            # above draws. Raising on the sweep would let one batch damaged by a crash
            # mid-append make the whole trash un-emptyable, and the delete path deliberately
            # skips rather than aborts for that reason. A named batch is different: the
            # caller asked for that batch, and it is not going to be deleted.
            raise SessionStorageError(
                f"{len(unapprovable)} of the batches named cannot be verified for deletion"
            )
        return ids, total, identities


def empty_trash(
    batch_ids: list[str] | None = None,
    on_progress: EmptyProgress | None = None,
    on_skip: EmptySkip | None = None,
    expect: dict[str, BatchIdentity] | None = None,
) -> int:
    """Delete staged batches for good; return the bytes freed.

    Serialized against other mutations, so a batch cannot be destroyed while a
    restore is mid-way through putting its files back.

    ``on_progress`` is called with the running total of bytes freed and ``on_skip``
    with a ``SKIP_*`` code for each batch kept rather than deleted, both from the
    calling thread. They are advisory: a caller that passes neither gets exactly the
    behaviour it always had, and nothing here waits on or trusts a callback.

    ``on_skip`` exists because keeping a batch is a REFUSAL a person needs told.
    Returning only the bytes freed made a batch that was deliberately kept
    indistinguishable from an empty one — "0 bytes freed, success" — with the reason
    reaching a log the user cannot read.

    ``expect`` maps a batch id to the ``(st_dev, st_ino)`` :func:`staged_targets` saw
    under the lock. A batch whose opened descriptor does not match is kept, because the
    id is only a name and a directory swapped in during the handoff would otherwise be
    deleted on the strength of the user having approved a different one. Omitted means
    unchecked, which is what a direct caller that never took a snapshot gets.
    """
    with _mutation_lock():
        try:
            return _empty_trash_locked(batch_ids, on_progress, on_skip, expect)
        finally:
            invalidate_scan_cache()


def _empty_trash_locked(
    batch_ids: list[str] | None = None,
    on_progress: EmptyProgress | None = None,
    on_skip: EmptySkip | None = None,
    expect: dict[str, BatchIdentity] | None = None,
) -> int:
    """Delete staged batches for real; return the bytes freed.

    This is the only call here that destroys data, and the only one that changes
    free space. Every path is resolved and confirmed to be inside the trash root
    first, so a tampered manifest or a symlinked batch directory cannot direct the
    delete outside it - and the removal itself then works through descriptors opened
    from the filesystem root, so nothing above the batch can be substituted between
    that check and the bytes going away.
    """

    def _skipped(code: str) -> None:
        if on_skip is not None:
            on_skip(code)

    try:
        root = trash_root().resolve()
    except OSError:
        return 0
    if batch_ids is None:
        targets = [_batch_dir(b.batch_id) for b in list_trash()]
    else:
        targets = [_batch_dir(batch_id) for batch_id in batch_ids]

    freed = 0
    for target in targets:
        try:
            resolved = target.resolve()
        except OSError:
            continue
        if resolved == root or not resolved.is_relative_to(root):
            logger.warning("refusing to empty %r: outside the trash root", target)
            _skipped(SKIP_OUTSIDE_ROOT)
            continue
        # A batch can hold files no manifest line mentions, left by an interruption
        # between the move and the append. The user was never shown them — the
        # batch may even list zero sessions — so "empty this batch" is not informed
        # consent for destroying them, and they are the only copy.
        try:
            leftovers = _unlisted_files(resolved)
        except SessionStorageError as exc:
            # Skip, not abort: one unreadable batch must not make the whole trash
            # un-emptyable. Skipping deletes nothing, which is the safe direction.
            logger.warning("refusing to empty %r: %s", target.name, exc)
            _skipped(SKIP_UNREADABLE)
            continue
        if leftovers:
            logger.warning(
                "refusing to empty %r: %d staged file(s) are absent from its "
                "manifest, so this would delete the only copy",
                target.name,
                len(leftovers),
            )
            _skipped(SKIP_UNLISTED_FILES)
            continue
        # An approval map that was SUPPLIED and does not name this batch is not the same as
        # no approval at all. `staged_targets` leaves out a batch whose identity it could not
        # read, and treating that as "nothing to check" would delete it unverified - the one
        # outcome the map exists to prevent. Refusing here is also what makes the omission
        # VISIBLE: the batch comes back as a skip with a reason instead of vanishing from the
        # job and leaving the user a success message above a batch still on screen.
        if expect is not None and target.name not in expect:
            logger.warning(
                "refusing to empty %r: it is not in the approved set, so it cannot be " "verified",
                target.name,
            )
            _skipped(SKIP_UNREADABLE)
            continue
        deleted, skip = _delete_listed_files(
            resolved, on_progress, freed, None if expect is None else expect.get(target.name)
        )
        freed += deleted
        if skip is not None:
            _skipped(skip)
    return freed
