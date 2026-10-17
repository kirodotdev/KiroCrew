"""Where a session's files live, and how long its archive segments are kept.

Transcript stems and their Slack aliases, the sessions and archive directories, the
durable archive write and its retention sweep, and the file stamps the caches and
housekeeping writes use.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time as _time
from datetime import datetime
from pathlib import Path

from kiro_crew.atomic_write import fsync_dir
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.messaging.link import canonical_key, is_legacy_slack_key, legacy_key
from kiro_crew.owner_only_files import mkdirs_owner_only, owner_only_opener

#: Moved code logs under the facade's own logger name.
logger = logging.getLogger("kiro_crew.history")


SESSIONS_DIR_NAME = "sessions"
ARCHIVE_DIR_NAME = "archive"
ARCHIVE_RETENTION_DAYS = 7

# Separates a transcript's stem from an archive segment's timestamp. NOT a dot,
# because session keys legitimately contain dots (a Slack thread_ts), which would
# make a right-most-dot parse attribute a segment to the wrong session.
ARCHIVE_SEGMENT_DELIMITER = "__"


def _safe_mtime(path: Path) -> float | None:
    """Return a file's mtime, or None if it can't be stat'd."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


#: ``(st_mtime_ns, st_ctime_ns, st_size, st_ino)`` -- see :func:`_cache_identity`.
CacheIdentity = tuple[int, int, int, int]


def _cache_identity(stat: os.stat_result) -> CacheIdentity:
    """Return a cache stamp that survives an mtime-preserving rewrite.

    ``st_size`` rides along because the timestamps alone cannot tell two in-place
    rewrites apart when they land on one clock tick: NTFS stamps writes from a
    system clock that moves in ~1-16 ms steps, and Windows ``st_ctime`` is the
    creation time, which a rewrite never moves. An external writer that appends
    rows to an already-cached transcript within that tick would otherwise be
    served the stale parse, and a window reconcile reading it would see nothing
    to append. Size cannot catch a same-length rewrite; the invalidation
    generation covers those from this process, the flock witness those from
    another.
    """
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)


def _restore_mtime(path: Path, prev_mtime: float | None) -> None:
    """Restore a session file's mtime after a *housekeeping* rewrite.

    ``list_sessions`` orders sessions by file mtime as a proxy for "last
    activity", and only a genuine message :meth:`ConversationLog.append`
    should advance that. Consolidation, rotation, and metadata updates
    (tab_id backfill on restore, title/agent/folder edits, last_consolidated
    bookkeeping) are background housekeeping — they rewrite the file but do
    NOT represent new conversation activity. Left unchecked they bump the
    mtime to "now", so every gateway restart (which consolidates + rehydrates
    open slots) floats long-closed sessions to the top of the session list and
    the "most recent session" a new dashboard/Slack session resolves to becomes
    a stale, unrelated thread. Restoring the pre-write mtime keeps ordering
    faithful to real activity. No-op when ``prev_mtime`` is None (fresh file).
    """
    if prev_mtime is None:
        return
    try:
        os.utime(path, (prev_mtime, prev_mtime))
    except OSError:
        pass


def _sessions_dir() -> Path:
    return config_dir() / SESSIONS_DIR_NAME


def _archive_dir(base: Path | None = None) -> Path:
    return (base or _sessions_dir()) / ARCHIVE_DIR_NAME


def _archive_lines(
    key: str, lines: list[str], reason: str, base: Path | None = None
) -> Path | None:
    """Append dropped message lines to archive/{key}.{YYYYMMDD-HHMMSS}.jsonl. Returns path or None.

    Durable on return: the file, the directory entry naming it and the parent
    entry naming the directory are synced before this returns, and a sync
    failure raises. Every caller archives lines its next transcript rewrite
    removes, so until the archive is on disk the transcript holds their only
    copy, and an unsynced archive could be lost to a crash that the rewrite
    survives. The parent is synced on every call, not only when this call made
    the directory: a directory that exists was not necessarily synced into its
    parent (an earlier call may have created it and then failed that sync).
    """
    if not lines:
        return None
    import itertools

    adir = _archive_dir(base)
    mkdirs_owner_only(adir)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    safekey = _safe_key(key)
    header = (
        json.dumps(
            {
                "_type": "archive",
                "reason": reason,
                "archived_at": now.isoformat(),
                "count": len(lines),
            }
        )
        + "\n"
    )
    payload = header + "".join(lines)
    # Atomic exclusive-create to avoid TOCTOU clobber when two archives land in the same second.
    for n in itertools.count():
        if n > 1000:
            raise RuntimeError(f"Failed to create archive file after {n} attempts")
        suffix = f"-{n}" if n else ""
        candidate = adir / f"{safekey}{ARCHIVE_SEGMENT_DELIMITER}{stamp}{suffix}.jsonl"
        try:
            with open(candidate, "x", encoding="utf-8", opener=owner_only_opener) as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            break
        except FileExistsError:
            continue
    fsync_dir(adir)
    fsync_dir(adir.parent)
    logger.info(
        "Archived %d lines from session %s to %s (reason=%s)",
        len(lines),
        key,
        candidate.name,
        reason,
    )
    _cleanup_old_archives(base=base)
    return candidate


_last_cleanup: float = 0.0


def _resolve_retention_days() -> int:
    """Read session.archive_retention_days from config.

    Returns the configured retention window in days, or ``-1`` when cleanup is
    disabled.  Falls back to the hardcoded default if config can't be loaded
    (e.g. during early init or in a stripped test environment).
    """
    try:
        return int(KiroCrewConfig.load().session.archive_retention_days)
    except Exception:
        return ARCHIVE_RETENTION_DAYS


def _cleanup_old_archives(retention_days: int | None = None, base: Path | None = None) -> int:
    """Delete archive files older than retention_days. Rate-limited to once per hour.

    When *retention_days* is None, the value is resolved from config
    (``session.archive_retention_days``).  A negative value disables cleanup
    entirely — the user manages archive deletion manually.

    The same pass expires closed SESSION CREW LOGS, on the same setting and inside
    the same throttle (:func:`kiro_crew.crew_log.store.sweep_expired`). One switch
    governs both because a session's message bodies live in its crew log now: a
    build that expired the transcript archive while the crew log it points into grew
    forever would keep the larger half of the same history indefinitely, and a
    second setting for it would be a second thing to find and turn off.
    """
    global _last_cleanup

    # Explicit negative disables cleanup immediately (no config read needed).
    if retention_days is not None and retention_days < 0:
        return 0  # cleanup disabled
    # Rate-limit guard runs BEFORE resolving retention from config so a
    # throttled call (the common case on hot archive paths) returns without
    # the expensive KiroCrewConfig.load() disk read + parse.
    now = _time.time()
    if now - _last_cleanup < 3600:
        return 0
    # Past the throttle window: stamp _last_cleanup NOW, before resolving
    # retention. Otherwise a config-resolved "disabled" (negative) would return
    # without updating the window, so every subsequent archive write would
    # re-run the expensive KiroCrewConfig.load().
    _last_cleanup = now
    # Resolve retention from config if not given, honoring a config-resolved
    # negative as the disable signal too.
    if retention_days is None:
        retention_days = _resolve_retention_days()
    if retention_days < 0:
        return 0  # cleanup disabled
    adir = _archive_dir(base)
    cutoff = now - retention_days * 86400
    removed = 0
    # An absent archive directory is not a reason to skip the crew log half: a
    # session can hold a crew log long before anything of its transcript is
    # archived, so returning here would leave that half uncollected until the
    # first archive ever written.
    if adir.exists():
        for p in adir.glob("*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Cleaned %d expired archive files (>%dd)", removed, retention_days)
    _cleanup_expired_crew_logs(retention_days, now)
    return removed


def _cleanup_expired_crew_logs(retention_days: int, now: float) -> None:
    """Expire closed the sessions' logs, best-effort, never at the transcript's cost.

    Off the event loop, which is what makes the added filesystem work safe rather
    than merely cheap: the only caller is ``_cleanup_old_archives``, reached from
    ``_archive_lines`` on the rotation path, and that path runs in the dashboard's
    flush executor thread (see ``chat_persistence``, which documents
    ``_save_slot_to_history`` running there) or on the shutdown save. The sweep
    reads a header and a bounded tail per closed unit, inside the hourly throttle
    the archive cleanup already has, so the cost is once an hour in a worker rather
    than per delete on the loop.

    Imported lazily and swallowed on failure for one reason each. Lazily because
    this module is imported on every startup while the crew log store is only
    reachable behind ``KIROCREW_CREW_LOG``, and a launch with the flag off
    should not pay for the import. Swallowed because the caller is on the
    transcript ARCHIVE path: a crew log tree that cannot be swept is a disk-space
    problem, and letting it raise here would turn that into a failure to archive
    the transcript, which loses history rather than retaining too much of it. The
    sweep logs its own counts.
    """
    try:
        from kiro_crew.crew_log.store import sweep_expired

        sweep_expired(retention_days, now=now)
    except Exception:
        logger.debug("The session's log retention sweep failed", exc_info=True)


def _safe_key(key: str) -> str:
    """Convert a session key (e.g. Slack thread_ts) to a safe filename."""
    return re.sub(r"[^\w\-.]", "_", key)


def transcript_stem(key: str) -> str:
    """The canonical filename stem *key*'s transcript and archive segments share.

    Exported so callers that account for or reclaim a session's disk usage can
    pair a session key with its files without re-deriving the sanitization. A
    second copy of that rule would drift the moment this one changed, and the
    failure is silent and destructive: the pairing misses, and a caller deleting
    "the session" removes one half and leaves the other behind.

    Prefer :func:`transcript_stems` when the answer feeds a decision about which
    files belong to a session — a Slack thread predating the canonical
    ``slack:<ts>`` key still logs under its bare thread_ts stem, and this function
    alone would not find it.
    """
    return _safe_key(key)


_TAB_ID_INDEX_STEM_PREFIX = "dashboard_chat-"
_TAB_ID_INDEX_GLOB = f"{_TAB_ID_INDEX_STEM_PREFIX}*.jsonl"


def _index_key_for_stem(stem: str) -> str:
    """The key form :attr:`ConversationLog._tab_id_index` stores for *stem*.

    One derivation shared by the index builder (which starts from a filename)
    and the in-place updater (which starts from a session key), because a second
    copy would drift the moment either side changed and the failure is silent:
    the two spellings stop matching, so an updater's lookup misses an entry that
    is really there.
    """
    return stem.replace("_", ":", 1)


def can_hold_tab_id_index_entry(key: str) -> bool:
    """True when *key*'s transcript is one :meth:`_rebuild_tab_id_index` scans.

    The index is built by globbing :data:`_TAB_ID_INDEX_GLOB`, so a transcript
    whose stem does not match can never appear in it -- a channel-keyed session
    (``slack:<ts>`` and friends) writes ``slack_<ts>.jsonl``, which the glob
    never returns. Saving such a transcript therefore cannot add, remove or
    change any index entry, which is what makes a no-op the correct response to
    one rather than an invalidation.
    """
    return transcript_stem(key).startswith(_TAB_ID_INDEX_STEM_PREFIX)


def transcript_stems(key: str) -> tuple[str, ...]:
    """Every filename stem *key*'s transcript could occupy, canonical first.

    :meth:`ConversationLog._path` falls back to the pre-migration bare
    ``thread_ts`` filename for Slack threads that predate the canonical session
    key, so one session key can legitimately resolve to either name. A caller that
    only knew the canonical stem would treat the legacy transcript as belonging to
    no session — and therefore as reclaimable while the session is still
    resumable. Returning both keeps that decision correct without duplicating the
    fallback rule.
    """
    stems = [_safe_key(key)]
    bare = legacy_key(key)
    if bare is not None:
        legacy = _safe_key(bare)
        if legacy not in stems:
            stems.append(legacy)
    return tuple(stems)


def transcript_lock_stems(key: str) -> tuple[str, ...]:
    """Canonical and bare physical lock stems for either Slack spelling.

    Unlike :func:`transcript_stems`, which preserves the caller's exact path
    identity for ownership decisions, this helper is deliberately symmetric:
    ``slack:<ts>``, ``slack_<ts>``, and bare ``<ts>`` all lock the same two
    sidecars. Non-Slack keys have one lock stem.
    """
    bare = legacy_key(canonical_key(key))
    if bare is None and key.startswith("slack_"):
        candidate = key[len("slack_") :]
        if is_legacy_slack_key(candidate):
            bare = candidate
    if bare is None:
        return (_safe_key(key),)
    return (_safe_key(f"slack:{bare}"), _safe_key(bare))
