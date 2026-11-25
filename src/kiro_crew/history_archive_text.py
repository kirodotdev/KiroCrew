"""A transcript's size-rotated archive head, as search reads it and retention ends it.

Size rotation moves a transcript's oldest rows into ``sessions/archive/`` as
``reason="rotate"`` segments. The chat's own paging corpus reads them back
(``read_rotated_messages``), so session search reads them too: a word only the
moved rows hold still ranks its chat (:func:`iter_rotated_texts`).

Every search cache is keyed on the LIVE file, which rotation rewrites, so a
rotation already invalidates them. Retention is the one writer that removes a
segment without touching the live file, so it tells search to forget that
text (:func:`remove_expired_segments`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from kiro_crew.jsonl_util import RECORD_CAP, bounded_records

_LOGGER = logging.getLogger("kiro_crew.history")

#: Bytes past which a segment record is skipped rather than read.
_SEGMENT_RECORD_CAP = RECORD_CAP


def rotated_segment_paths(base_dir: Path, key: str) -> list[Path]:
    """*key*'s archive segments under *base_dir*, oldest first, of every reason.

    Through :func:`~kiro_crew.history_files.archive_segments_of`: another chat
    whose stem extends *key*'s with ``__`` shares the prefix.

    Raises ``OSError`` when the archive directory cannot be listed: an empty
    answer would claim the transcript never rotated.
    """
    from kiro_crew import history as facade

    adir = facade._archive_dir(base_dir)
    stem = facade._safe_key(key) + facade.ARCHIVE_SEGMENT_DELIMITER

    def _seg_order(p: Path) -> tuple[str, int]:
        # Same-second segments carry ``-N`` suffixes; lexicographic order
        # puts ``-10`` before ``-2``, so split the numeric part out.
        rest = p.name[len(stem) : -len(".jsonl")]
        stamp, dash, suffix = rest.partition("-")
        # The stamp itself contains one dash (YYYYMMDD-HHMMSS): re-join,
        # then look for a SECOND dash carrying the collision counter.
        if dash:
            stamp2, dash2, suffix2 = suffix.partition("-")
            stamp = f"{stamp}-{stamp2}"
            suffix = suffix2 if dash2 else ""
        try:
            n = int(suffix) if suffix else 0
        except ValueError:
            n = 0
        return (stamp, n)

    try:
        return sorted(facade.archive_segments_of(adir, facade._safe_key(key)), key=_seg_order)
    except OSError as exc:
        raise OSError(f"rotated archive for {key} could not be enumerated") from exc


def iter_rotated_texts(base_dir: Path, key: str) -> Iterator[str]:
    """Yield the string ``content`` of *key*'s ``rotate`` segments, oldest first.

    The same rows ``read_rotated_messages`` serves: other archive reasons hold
    text the product discarded (a rewind, a regenerate), so they stay out.
    Streams each segment through :func:`~kiro_crew.jsonl_util.bounded_records`
    rather than parsing the archive into rows, so a search pass over many chats
    pins no parsed transcript and a record past the cap is skipped. A damaged
    header or row is skipped, as the live file's are; a segment that cannot be opened raises
    ``OSError``, which search reads as "could not read" and does not cache.
    """
    from kiro_crew.dashboard.host_sandbox_refusal import is_refusal_row

    for path in rotated_segment_paths(base_dir, key):
        with open(path, "rb") as handle:
            # Bounded like the live file: a record past the cap is skipped, so
            # one oversized line cannot make search hold it in memory.
            records = bounded_records(
                handle, path, cap=_SEGMENT_RECORD_CAP, label="history_archive_text"
            )
            head = next(records, "")
            try:
                header = json.loads(head) if head.strip() else None
            except (ValueError, RecursionError):
                continue
            if not isinstance(header, dict) or header.get("reason") != "rotate":
                continue
            for line in records:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(row, dict) or "_type" in row or is_refusal_row(row):
                    continue
                content = row.get("content")
                if isinstance(content, str) and content:
                    yield content


def remove_expired_segments(
    sessions_dir: Path, expired: Mapping[str, Sequence[Path]], cutoff: float
) -> int:
    """Delete *expired* segments, one chat at a time under that chat's lock.

    *expired* maps a transcript stem to its segments older than *cutoff*. For
    each stem, under ``_locked(stem)`` (the lock ``index_session`` and
    ``chunk_session`` hold from stat to sync), the chat's search index and
    semantic chunk rows are dropped FIRST, under every spelling the chat's
    caches know it by (as ``delete_session`` drops them), and the segments
    still older than *cutoff* are unlinked only once both drops succeeded. A
    drop that fails keeps the segments, so the next sweep retries the whole
    purge; unlinking first would leave nothing to retry from while the text
    stays indexed. An indexer that already read a segment syncs before the
    purge, never after it, and a row it wrote is dropped.

    The caller must hold no transcript lock: this takes one per chat in turn,
    and a caller holding another could deadlock against a writer that takes
    both. A chat whose lock times out keeps its segments for the next sweep.

    Returns how many segments were removed.

    shortcut: a long-lived second process keeps its own warm fold memo of a
    purged chat until its next write to that chat.
    """
    from kiro_crew.history import ConversationLog, HistoryLockTimeout
    from kiro_crew.history_index import INDEX_FILENAME, SessionSearchIndex
    from kiro_crew.history_vectors_index import vector_store_for

    removed = 0
    log = ConversationLog(sessions_dir)
    db_path = sessions_dir / ".index" / INDEX_FILENAME
    # Opening an index that is not there would create it.
    index = SessionSearchIndex(db_path) if db_path.exists() else None
    vectors = vector_store_for(sessions_dir)
    try:
        for stem, paths in sorted(expired.items()):
            if not stem:
                # Not a segment of any chat: no derived row names it.
                removed += _unlink_all(_still_older(paths, cutoff))
                continue
            try:
                with log._locked(stem):
                    due = _still_older(paths, cutoff)
                    if not due:
                        continue
                    names = sorted(set(log._cache_key_identities(stem)) | {stem})
                    if not vectors.drop(names):
                        _LOGGER.warning("Expired archive of %s kept: chunk rows not dropped", stem)
                        continue
                    if index is not None and not index.drop(names):
                        _LOGGER.warning("Expired archive of %s kept: index row not dropped", stem)
                        continue
                    removed += _unlink_all(due)
                    log._invalidate_cache(stem)
            except HistoryLockTimeout:
                _LOGGER.info("Expired archive of %s kept until the next sweep: chat busy", stem)
    finally:
        vectors.close()
        if index is not None:
            index.close()
    return removed


def _still_older(paths: Sequence[Path], cutoff: float) -> list[Path]:
    """The *paths* that still exist and are still older than *cutoff*."""
    due = []
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                due.append(path)
        except OSError:
            pass
    return due


def _unlink_all(paths: Sequence[Path]) -> int:
    """Unlink each of *paths*; return how many went."""
    gone = 0
    for path in paths:
        try:
            path.unlink()
            gone += 1
        except OSError:
            pass
    return gone
