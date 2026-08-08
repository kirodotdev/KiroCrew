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
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

from kiro_crew import platform_compat
from kiro_crew.config.paths import (
    CONFIG_DIR_LEAF,
    KIRO_BASE_DIR_NAME,
    data_home,
    kiro_home,
    kiro_sessions_dir,
    legacy_home,
)
from kiro_crew.history import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER, SESSIONS_DIR_NAME

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

    @property
    def active_stems(self) -> frozenset[str]:
        """Transcript stems belonging to a session that is still resumable."""
        return frozenset(stem for stem, sid in self.stem_to_sid.items() if sid in self.active_sids)

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

    def age_days(self, now: float) -> float:
        return max(0.0, (now - self.mtime) / _SECONDS_PER_DAY)


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


def _scan_units(index: SessionIndex) -> list[SessionUnit]:
    """Enumerate sessions across both stores, one entry per session.

    A session's age is the NEWEST mtime across every file it owns: a transcript is
    appended to while the session runs, so an older metadata file or a long-since
    rotated archive segment would make a live session look stale.
    """
    sizes: dict[str, int] = {}
    mtimes: dict[str, float] = {}
    sids: dict[str, str] = {}
    stems: dict[str, list[str]] = {}
    sid_for_stem = dict(index.stem_to_sid)

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

    active_stems = index.active_stems
    units = []
    for uid, size in sizes.items():
        sid = sids.get(uid, "")
        owned = tuple(stems.get(uid, []))
        active = sid in index.active_sids or any(stem in active_stems for stem in owned)
        units.append(
            SessionUnit(
                uid=uid,
                sid=sid,
                stems=owned,
                bytes=size,
                mtime=mtimes.get(uid, 0.0),
                active=active,
            )
        )
    return units


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


def measure(index: SessionIndex, *, now: float | None = None) -> StorageReport:
    """Measure session storage and report what is reclaimable."""
    clock = time.time() if now is None else now
    units = _scan_units(index)
    active = [u for u in units if u.active]
    # Sub-floor sessions are neither active nor offered: reporting them as
    # reclaimable would promise bytes no threshold can actually move.
    reclaimable = [u for u in units if not u.active and u.age_days(clock) >= MIN_RECLAIM_AGE_DAYS]
    batches = list_trash()
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
        reclaim_blocked_reason=reclaim_block_reason(),
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


def _replay_store_cotenants() -> list[str]:
    """Names of pod instances that read the same kiro-cli replay store.

    A pod isolates ``KIROCREW_HOME`` but deliberately NOT ``KIRO_HOME``, so every
    pod reads the machine-wide replay store while keeping its own session map. From
    the default instance's side that is the mirror of the isolated-instance hazard:
    the pod's sessions are absent from the map THIS process consults, so they read
    as retired.

    The pod root is host-side state at a known location, which makes this the one
    co-tenant that can actually be discovered. A dev gateway pointed at some other
    ``KIROCREW_HOME`` cannot be, and stays a documented limitation.
    """
    raw = os.environ.get("KIROCREW_POD_ROOT")
    pod_root = Path(raw).expanduser() if raw else Path.home() / ".kirocrew-pods"
    try:
        entries = list(pod_root.iterdir())
    except OSError:
        return []
    return sorted(
        entry.name for entry in entries if entry.is_dir() and not entry.name.startswith(".")
    )


def reclaim_block_reason() -> str:
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
            cotenants = _replay_store_cotenants()
            if cotenants:
                listed = ", ".join(cotenants[:3])
                return (
                    f"{len(cotenants)} other instance(s) share this kiro-cli session "
                    f"store ({listed}), so their resumable sessions cannot be told "
                    "apart from retired ones. Evict them with `kirocrew pod down "
                    "<name>` to reclaim from here."
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


def _move_file_exclusive(src: Path, dst: Path) -> bool:
    """Move *src* to *dst*, never replacing an existing *dst*. False if occupied.

    Restore's preflight checks that the origin is free, but the session can be
    recreated in the interval before the move — and ``os.rename`` replaces the
    destination silently, so undoing a deletion would destroy the newer data it was
    meant to protect. Creating the destination exclusively makes the check and the
    write one atomic step, so a lost race is reported rather than acted on.
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
            with os.fdopen(fd, "wb") as out, open(src, "rb") as handle:
                shutil.copyfileobj(handle, out)
            shutil.copystat(src, dst)
        except OSError:
            with suppress(OSError):
                dst.unlink()
            raise
        src.unlink()
        return True
    src.unlink()
    return True


def _move_file(src: Path, dst: Path) -> None:
    """Move one file, preferring a rename and falling back to a copy.

    A rename is atomic and instant, which is what makes a trash usable at tens of
    gigabytes. It only works within one filesystem, so a data home mounted apart
    from the kiro-cli store falls back to :func:`shutil.move` — correct, but it
    copies, so it is slow and needs the space twice while it runs.
    """
    try:
        os.rename(src, dst)
    except OSError as exc:
        if getattr(exc, "errno", None) != getattr(os, "EXDEV", 18):
            raise
        shutil.move(str(src), str(dst))


def _file_size(path: Path) -> int:
    """The size recorded for a staged file. Raises ``OSError`` when unreadable.

    A named operation rather than an inline ``stat`` because its failure is
    load-bearing: a file whose size cannot be read cannot be recorded, and an
    unrecorded file is one restore cannot put back.
    """
    return path.stat().st_size


def _rollback(moved: list[tuple[Path, Path]]) -> None:
    """Undo a partial session move, best effort.

    Each pair is (where it landed, where it came from). A rollback that itself
    fails is logged and nothing more: the alternative is raising over a caller
    already handling a failure, which would abandon the rest of the batch too.

    The move is EXCLUSIVE. A rollback runs after something already went wrong, so
    the origin may have been recreated in the meantime — and a plain rename would
    replace that newer session data with the copy being put back, turning a handled
    failure into data loss. An occupied origin leaves the file where it is staged,
    which keeps it recoverable.
    """
    for landed, origin in reversed(moved):
        try:
            if not _move_file_exclusive(landed, origin):
                logger.warning(
                    "not rolling %s back: %s was recreated and is newer",
                    landed,
                    origin,
                )
        except OSError:
            logger.warning("could not roll %s back to %s", landed, origin, exc_info=True)


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

    A scan that cannot be completed RAISES rather than returning a short list. This
    function exists to block deletions, so an empty result must mean "there is
    nothing unaccounted for", never "the walk gave up early" — ``rglob`` skips
    unreadable directories silently, which would turn a transient error into
    permission to delete a file that is the only copy of a session.
    """
    parsed = _read_manifest(batch)
    listed: set[str] = set()
    if parsed is not None:
        for entry in parsed[1]:
            files = entry.get("files")
            if not isinstance(files, list):
                continue
            for record in files:
                if isinstance(record, dict) and isinstance(record.get("rel"), str):
                    listed.add(record["rel"])
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
            f"could not read all of {batch.name}, so it is not known whether it "
            f"holds files nothing lists: {failures[0]}"
        )
    return unlisted


def _discard_restored_batch(batch: Path, header: dict[str, Any]) -> None:
    """Remove a fully-restored batch, unless it holds files nothing listed.

    Deleting the directory wholesale would destroy an interrupted move's staged
    files — on the one path that exists to be safe. Those keep the batch alive
    instead, so a user can still see it and decide.
    """
    try:
        leftovers = _unlisted_files(batch)
    except SessionStorageError as exc:
        # Not knowing is treated exactly like finding leftovers: keep the batch.
        logger.warning("keeping trash batch %s: %s", batch.name, exc)
        _rewrite_manifest(batch, header, [])
        return
    if leftovers:
        logger.warning(
            "keeping trash batch %s: %d staged file(s) are absent from its manifest, "
            "so removing it would delete the only copy",
            batch.name,
            len(leftovers),
        )
        _rewrite_manifest(batch, header, [])
        return
    shutil.rmtree(batch, ignore_errors=True)


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


def _read_manifest(batch: Path) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Parse a batch manifest into its header and session entries.

    A trailing partial line (a crash mid-append) is skipped rather than failing
    the whole batch: every complete line before it describes real moved files that
    must stay restorable.
    """
    try:
        raw = (batch / MANIFEST_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    header: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if header is None:
            header = record
            continue
        entries.append(record)
    if header is None or header.get("schema") != MANIFEST_SCHEMA:
        return None
    return header, entries


def _rewrite_manifest(batch: Path, header: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Replace a manifest atomically after a partial restore."""
    tmp = batch / f"{MANIFEST_NAME}.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header) + "\n")
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    os.replace(tmp, batch / MANIFEST_NAME)


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

    *refresh* re-reads the caller's index INSIDE the lock, immediately before
    anything moves. Scanning a large store takes long enough that a session can be
    resumed and mapped while the selection is being computed, and the stale index
    would then treat it as retired. The two active sets are UNIONED, never
    replaced, so a re-read can only ever add protection.

    Serialized against other mutations, because two interleaved reclaims can put
    one half of a session in each batch and leave neither able to restore it.
    """
    with _mutation_lock():
        return _move_to_trash_locked(uids, reason=reason, index=index, now=now, refresh=refresh)


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

    Each session is recorded as it lands, so an interruption leaves a manifest
    describing exactly what moved — a partial batch stays restorable instead of
    becoming orphaned files nothing points at.
    """
    clock = time.time() if now is None else now
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
    # interval would look retired. Re-reading here makes the last thing before the
    # move loop the freshest view available, and the sets are UNIONED so a re-read
    # can only ever add protection.
    if refresh is not None:
        try:
            latest = refresh()
        except Exception:
            logger.warning("could not re-read the session index", exc_info=True)
            raise SessionStorageError(
                "could not confirm which sessions are live; nothing was moved"
            )
        live_sids = index.active_sids | latest.active_sids
        live_stems = index.active_stems | latest.active_stems
    else:
        live_sids = index.active_sids
        live_stems = index.active_stems

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
    archives = _archive_index()

    moved_bytes = 0
    moved_sessions = 0
    staged_dirs: set[Path] = set()
    cli_files = _cli_index()
    with (target / MANIFEST_NAME).open("w", encoding="utf-8") as manifest:
        _write_header(manifest, batch_id, clock, reason)
        for uid in requested:
            unit = by_uid.get(uid)
            if unit is None:
                continue
            files: list[dict[str, Any]] = []
            done: list[tuple[Path, Path]] = []
            failed = False
            for src, rel in _unit_paths(unit.sid, unit.stems, archives, cli_files):
                try:
                    size = _file_size(src)
                except OSError:
                    # A file that cannot be sized cannot be recorded, and a file
                    # the manifest does not record is one restore cannot put back.
                    # Skipping it here while moving the rest is precisely the split
                    # this loop's rollback exists to prevent, so it is a failure.
                    logger.warning("could not stat %s for staging", src, exc_info=True)
                    failed = True
                    break
                dst = target / rel
                if dst.parent not in staged_dirs:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    staged_dirs.add(dst.parent)
                try:
                    _move_file(src, dst)
                except OSError:
                    logger.warning("could not move %s into the trash", src, exc_info=True)
                    failed = True
                    break
                done.append((dst, src))
                files.append({"rel": rel, "origin": str(src), "bytes": size})
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
                    logger.warning("could not record session %s in the manifest; rolling back", uid)
                    try:
                        manifest.truncate(mark)
                        manifest.seek(mark)
                    except OSError:
                        logger.warning("could not rewind the manifest", exc_info=True)
                    _rollback(done)
                    continue
                moved_sessions += 1
                moved_bytes += sum(int(record["bytes"]) for record in files)

    if not moved_sessions:
        # Leave no empty batch behind — but a rollback that itself failed can have
        # left staged files here, and those are the only copy.
        if _unlisted_files(target):
            raise SessionStorageError(
                "no sessions were staged, and some files could not be put back; " f"see {target}"
            )
        shutil.rmtree(target, ignore_errors=True)
        raise SessionStorageError("none of the selected sessions were found on disk")

    return TrashBatch(
        batch_id=batch_id,
        created_at=clock,
        reason=reason,
        sessions=moved_sessions,
        bytes=moved_bytes,
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
        parsed = _read_manifest(candidate)
        if parsed is None:
            logger.debug("trash batch %s has no readable manifest", candidate.name)
            continue
        header, entries = parsed
        # The DIRECTORY is the batch's identity. A header that names a different
        # batch would make a targeted empty delete the batch it named instead of
        # the one it came from, so a disagreement is treated as corruption rather
        # than resolved in the header's favour.
        claimed = header.get("batch_id")
        if isinstance(claimed, str) and claimed and claimed != candidate.name:
            logger.warning(
                "trash batch %s has a manifest claiming batch id %r; not offering it",
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
                sessions=len(entries),
                bytes=sum(_entry_bytes(e) for e in entries),
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
        return _restore_locked(batch_id, uids)


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
                logger.warning("manifest record for %s is not an object", uid)
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
                        "session %s was recreated while being restored; leaving it " "staged",
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
            logger.warning("could not fully restore session %s", uid, exc_info=True)
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
        _discard_restored_batch(batch, header)
    return restored


def _dir_bytes(root: Path) -> int:
    """Total size of every file under *root*, recursively.

    Uses the stat the directory walk already produced: a batch can hold hundreds
    of thousands of files, where a separate ``Path.stat()`` per file dominates.
    """
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def empty_trash(batch_ids: list[str] | None = None) -> int:
    """Delete staged batches for good; return the bytes freed.

    Serialized against other mutations, so a batch cannot be destroyed while a
    restore is mid-way through putting its files back.
    """
    with _mutation_lock():
        return _empty_trash_locked(batch_ids)


def _empty_trash_locked(batch_ids: list[str] | None = None) -> int:
    """Delete staged batches for real; return the bytes freed.

    This is the only call here that destroys data, and the only one that changes
    free space. Every path is resolved and confirmed to be inside the trash root
    first, so a tampered manifest or a symlinked batch directory cannot direct the
    delete outside it.
    """
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
            logger.warning("refusing to empty %s: outside the trash root", target)
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
            logger.warning("refusing to empty %s: %s", target.name, exc)
            continue
        if leftovers:
            logger.warning(
                "refusing to empty %s: %d staged file(s) are absent from its "
                "manifest, so this would delete the only copy",
                target.name,
                len(leftovers),
            )
            continue
        freed += _dir_bytes(resolved)
        shutil.rmtree(resolved, ignore_errors=True)
    return freed
