"""Which sessions are on disk, what they cost, and which of them may be reclaimed.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

The read side: enumerating both stores into one unit per session (:func:`_scan_units`),
the short-lived scan and co-tenant caches and the call that drops both
(:func:`invalidate_scan_cache`), every file one session owns (:func:`_unit_paths`), the
report (:func:`measure`), the row list and the reclaimable selection, the sessions a
co-tenant instance can still resume (:func:`cotenant_sids`), why a reclaim is refused
(:func:`reclaim_block_reason`), and the trash listing (:func:`list_trash`).

``_scan_cache`` and ``_cotenant_cache`` are rebound here (``global``) and nowhere else,
so the facade serves them from this module instead of binding copies that would go stale.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from kiro_crew import hooks, platform_compat
from kiro_crew.chat_attachments import ATTACHMENTS_DIR_SUFFIX, attachments_dir
from kiro_crew.config.paths import (
    CONFIG_DIR_LEAF,
    KIRO_BASE_DIR_NAME,
    data_home,
    kiro_home,
    kiro_sessions_dir,
    legacy_home,
)
from kiro_crew.history import (
    ARCHIVE_DIR_NAME,
    THREADS_DIR_NAME,
    THREADS_SIDECAR_SUFFIX,
    threads_sidecar_for_stem,
)
from kiro_crew.session_map import SESSION_MAP_FILENAME
from kiro_crew.session_storage_parts.batch_manifest import _summarize_manifest
from kiro_crew.session_storage_parts.crew_staging import _crew_staging
from kiro_crew.session_storage_parts.store_layout import (
    _CLI_SUFFIXES,
    _TRANSCRIPT_SUFFIX,
    _UNIT_ID_RE,
    BUCKET_DAYS,
    MIN_RECLAIM_AGE_DAYS,
    STAGE_CLI_LEAF,
    STAGE_CREW_LEAF,
    SessionIndex,
    SessionStorageError,
    SessionUnit,
    StorageBucket,
    StorageReport,
    TrashBatch,
    _archive_index,
    _crew_sessions_dir,
    _RawUnit,
    _tree_bytes,
    _tree_files,
    logger,
    trash_root,
)

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


def _scan_units(
    index: SessionIndex, *, cached: bool = False, strict: bool = False
) -> list[SessionUnit]:
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

    *strict* is for the reclaim: a crew-log store that cannot be listed completely
    raises :class:`SessionStorageError` instead of answering short, because a session
    whose crew logs are missing from the answer would be staged without them.
    """
    raw = _scan_raw(index.stem_to_sid, cached=cached and not strict, strict=strict)
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
            # A crew-log unit is keyed by its ACP session id, so a unit whose id is still
            # mapped belongs to a session the product can resume.
            or any(unit in index.active_sids or unit in cotenant for unit in entry.crew_units)
        )
        live = (
            entry.sid in index.live_sids
            or any(stem in live_stems for stem in entry.stems)
            or any(unit in index.live_sids for unit in entry.crew_units)
        )
        units.append(
            SessionUnit(
                uid=entry.uid,
                sid=entry.sid,
                stems=entry.stems,
                bytes=entry.bytes,
                mtime=entry.mtime,
                active=active,
                live=live,
                crew_units=entry.crew_units,
            )
        )
    return units


def _scan_raw(
    sid_for_stem: Mapping[str, str], *, cached: bool = False, strict: bool = False
) -> list[_RawUnit]:
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
    result = _scan_raw_uncached(sid_for_stem, strict=strict)
    if cached:
        _store_scan(sid_for_stem, result)
    return result


def _scan_raw_uncached(sid_for_stem: Mapping[str, str], *, strict: bool = False) -> list[_RawUnit]:
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
                sids.setdefault(uid, "")
                record(uid, st.st_size, st.st_mtime)
    except OSError:
        logger.debug("transcript store unreadable", exc_info=True)

    # Attachments half: ``<stem>.attachments/`` directories beside the transcripts.
    # Their bytes are the session's, and a write into one is activity on the
    # session, so both land on the same unit the transcript does.
    try:
        with os.scandir(_crew_sessions_dir()) as it:
            adirs = [
                entry.name
                for entry in it
                if entry.name.endswith(ATTACHMENTS_DIR_SUFFIX)
                and entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        adirs = []
    for name in adirs:
        stem = name[: -len(ATTACHMENTS_DIR_SUFFIX)]
        if not _UNIT_ID_RE.match(stem):
            continue
        try:
            files = _attachment_files(_crew_sessions_dir() / name)
        except OSError:
            logger.debug("attachments directory %r unreadable", name, exc_info=True)
            continue
        if not files:
            continue
        uid = attribute(stem)
        if uid not in sizes and uid not in stems:
            # Images outliving their transcript still cost space and still belong
            # to a session, so they form a unit of their own, like segments do.
            sids.setdefault(uid, "")
        add_stem(uid, stem)
        for path in files:
            try:
                st = os.lstat(path)
            except OSError:
                continue
            record(uid, st.st_size, st.st_mtime)

    for stem, segments in archives.items():
        uid = attribute(stem)
        if uid not in sizes and uid not in stems:
            # Segments outliving their transcript still cost space and still
            # belong to a session, so they form a unit of their own.
            sids.setdefault(uid, "")
        add_stem(uid, stem)
        for _path, size, mtime in segments:
            record(uid, size, mtime)

    # Reply-thread half: ``.threads/<stem>.json`` beside the transcripts. One
    # file per session; its bytes and its last write belong to the same unit. A
    # reply writes ONLY this file, so a sidecar's mtime is what keeps a recently
    # threaded session young: each entry is judged on its own, as the transcripts
    # are above -- one sidecar renamed away by a concurrent delete must not cost
    # every other session its freshness. An entry that is there but cannot be
    # stat'ed, or a directory that cannot be listed, is read as "fresh now": the
    # scan cannot say how old those sessions are, and the answer that cannot
    # stage a live thread is the young one.
    thread_entries: list[tuple[str, os.stat_result | None]] = []
    threads_unreadable = False
    try:
        with os.scandir(_crew_sessions_dir() / THREADS_DIR_NAME) as it:
            for entry in it:
                if not entry.name.endswith(THREADS_SIDECAR_SUFFIX):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    thread_entries.append((entry.name, entry.stat(follow_symlinks=False)))
                except FileNotFoundError:
                    continue
                except OSError:
                    logger.debug("thread sidecar %r unreadable", entry.name, exc_info=True)
                    thread_entries.append((entry.name, None))
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        logger.warning("thread sidecar directory unreadable; ages read as now", exc_info=True)
        threads_unreadable = True
    scan_now = time.time()
    for name, sidecar_st in thread_entries:
        stem = name[: -len(THREADS_SIDECAR_SUFFIX)]
        if not _UNIT_ID_RE.match(stem):
            continue
        uid = attribute(stem)
        if uid not in sizes and uid not in stems:
            # A thread file outliving its transcript still costs space and still
            # belongs to a session, so it forms a unit of its own.
            sids.setdefault(uid, "")
        add_stem(uid, stem)
        if sidecar_st is None:
            record(uid, 0, scan_now)
        else:
            record(uid, sidecar_st.st_size, sidecar_st.st_mtime)
    if threads_unreadable:
        for uid in list(sizes):
            record(uid, 0, scan_now)

    # Crew-log half: attributed LAST, because the header's slot names a transcript
    # and the transcripts above are what it is matched against.
    stem_owner = {stem: uid for uid, owned in stems.items() for stem in owned}
    crew_units: dict[str, list[str]] = {}
    for unit_id, slot, crew_files in _crew_log_units(strict=strict):
        uid = _crew_log_owner(unit_id, slot, stem_owner, sizes)
        if not uid:
            continue
        sids.setdefault(uid, "")
        stems.setdefault(uid, [])
        crew_units.setdefault(uid, []).append(unit_id)
        sizes.setdefault(uid, 0)
        for size, mtime in crew_files:
            record(uid, size, mtime)

    return [
        _RawUnit(
            uid=uid,
            sid=sids.get(uid, ""),
            stems=tuple(stems.get(uid, [])),
            bytes=size,
            mtime=mtimes.get(uid, 0.0),
            crew_units=tuple(crew_units.get(uid, ())),
        )
        for uid, size in sizes.items()
    ]


def _crew_log_units(*, strict: bool = False) -> list[tuple[str, str, list[tuple[int, float]]]]:
    """Every session crew-log unit as ``(unit id, header slot, [(size, mtime), ...])``.

    Units come from the crew-log store's own header-proved listing, so a directory
    whose header does not name it is not attributed to anyone. On a read path a unit
    that cannot be walked is skipped: answering short costs a slightly low total.

    With *strict* the answer must be complete, because the reclaim stages what it
    lists: a store that cannot be listed, a unit directory that holds entries its
    header cannot prove, or a unit that cannot be walked raises
    :class:`SessionStorageError`. A store never created is an empty answer.
    """
    from kiro_crew.crew_log import store as crew_store
    from kiro_crew.crew_log.schema import KIND_SESSION

    try:
        unit_ids = crew_store.unit_ids(KIND_SESSION)
        slot_of = {
            unit: slot
            for slot, owned in crew_store.session_units_by_slot(strict=strict).items()
            for unit in owned
        }
    except FileNotFoundError:
        return []
    except Exception as exc:
        if strict:
            raise SessionStorageError(
                "the crew logs could not all be listed, so no session was moved: a "
                "session staged without them would leave its history behind"
            ) from exc
        logger.debug("crew log store unreadable", exc_info=True)
        return []
    found: list[tuple[str, str, list[tuple[int, float]]]] = []
    for unit_id in unit_ids:
        directory = crew_store.unit_dir_for(KIND_SESSION, unit_id)
        if directory is None:
            continue
        try:
            files = _tree_files(directory)
        except OSError as exc:
            if strict:
                raise SessionStorageError(
                    f"crew log {unit_id!r} could not be read, so no session was moved"
                ) from exc
            logger.debug("crew log %r unreadable", unit_id, exc_info=True)
            continue
        found.append((unit_id, slot_of.get(unit_id, ""), files))
    return found


def _crew_log_owner(
    unit_id: str, slot: str, stem_owner: Mapping[str, str], units: Mapping[str, int]
) -> str:
    """The session a crew-log unit belongs to, or ``""`` when it cannot be named.

    The header's slot is the conversation: a dashboard slot's transcript is
    ``dashboard_<slot>`` and a channel slot's IS its stem, so every unit a reset left
    behind joins the transcript it wrote beside. Without a matching transcript the
    unit joins the replay log that shares its id, and otherwise it is a row of its own.
    """
    if slot:
        for stem in (slot, f"dashboard_{slot}"):
            owner = stem_owner.get(stem)
            if owner:
                return owner
    if unit_id in units:
        return unit_id
    return unit_id if _UNIT_ID_RE.match(unit_id) else ""


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
        # os.lstat + S_ISREG, never Path.is_file(): the latter follows a symlink,
        # so a link planted at the transcript's name between a scan and the move it
        # feeds would be owned and the move would relocate the link (or read through
        # it) instead of the file. This matches the scans and the sidecar handling
        # below, which answer only to regular files. lstat rather than is_file(
        # follow_symlinks=False) because that argument only exists on Python 3.13+.
        try:
            if stat.S_ISREG(os.lstat(transcript).st_mode):
                found.append((transcript, f"{STAGE_CREW_LEAF}/{transcript.name}"))
        except FileNotFoundError:
            pass
        segments = (
            archives.get(stem, []) if archives is not None else _archive_index().get(stem, [])
        )
        for path, _size, _mtime in segments:
            found.append((path, f"{STAGE_CREW_LEAF}/{ARCHIVE_DIR_NAME}/{path.name}"))
        # The images the transcript's rows reference. Regular files only, never
        # followed: the directory is code-written and flat, so anything else in
        # it is not this session's and is left where it is -- the owned files
        # and the transcript still move, and the directory stays behind holding
        # only the foreign entry (rmdir refuses a non-empty directory).
        adir = attachments_dir(_crew_sessions_dir(), stem)
        for path in _attachment_files(adir):
            found.append((path, f"{STAGE_CREW_LEAF}/{adir.name}/{path.name}"))
        # The reply threads on the transcript's messages: primary content, one
        # file per session, so it travels with the transcript or the reclaim
        # would leave the session's replies orphaned in the live store.
        # A sidecar that is absent is a session with no threads; one that cannot
        # be looked at (permission drift, a file where the `.threads` directory
        # should be) RAISES, as `_attachment_files` does: answering "none" would
        # let the transcript move and leave the replies behind.
        sidecar = threads_sidecar_for_stem(_crew_sessions_dir(), stem)
        if sidecar.parent.exists() and platform_compat.is_link_or_junction(sidecar.parent):
            # A link where `.threads` should be points this scan -- and the move
            # it feeds -- outside the session store. Refuse, as the write, delete
            # and restore paths do; the reclaim then answers with the reason.
            raise NotADirectoryError(
                errno.ENOTDIR, "thread sidecar directory is a link", str(sidecar.parent)
            )
        try:
            if stat.S_ISREG(os.lstat(sidecar).st_mode):
                found.append((sidecar, f"{STAGE_CREW_LEAF}/{THREADS_DIR_NAME}/{sidecar.name}"))
        except FileNotFoundError:
            pass
    return found


def _attachment_files(adir: Path) -> list[Path]:
    """The regular files directly inside an attachments directory, sorted.

    Empty for a MISSING directory only. Any other failure to read it RAISES: a
    directory that exists but cannot be listed holds files this scan cannot see,
    and answering "none" would let the transcript move without them -- the
    orphaned, still-served images this half exists to take along. A link where
    the directory should be is not entered, and a link or subdirectory inside it
    is not listed.
    """
    try:
        info = os.lstat(adir)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        return []
    with os.scandir(adir) as it:
        entries = [e for e in it if e.is_file(follow_symlinks=False)]
    return sorted((Path(e.path) for e in entries), key=lambda q: q.name)


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
    """The host-side pod root, anchored the same way ``pod.config`` anchors it.

    A SECOND reader of ``KIROCREW_POD_ROOT``, independent of :class:`PodConfig`
    (this module must not import the pod package). It gets the same
    ``expanduser`` + ``abspath`` treatment for the same reason: a relative
    override resolved against this process's working directory, so a gateway
    started from ``/`` scanned a different root than the CLI that wrote it and
    the co-tenant probe below silently found nothing. See
    ``pod.config._canonical_override`` for the full rationale; the two must not
    drift, which is why the rule is restated rather than left implicit.
    """
    raw = os.environ.get("KIROCREW_POD_ROOT")
    if not raw:
        return Path.home() / ".kirocrew-pods"
    return Path(os.path.abspath(os.path.expanduser(raw)))


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
                # a caller logs this text (the log-forgery class).
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
                bytes=staged_bytes + _tree_bytes(_crew_staging(candidate.name)),
            )
        )
    batches.sort(key=lambda b: b.created_at, reverse=True)
    return batches
