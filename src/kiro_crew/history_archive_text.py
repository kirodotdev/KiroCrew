"""A transcript's size-rotated archive head, as search reads it and retention ends it.

Size rotation moves a transcript's oldest rows into ``sessions/archive/`` as
``reason="rotate"`` segments. The chat's own paging corpus reads them back
(``read_rotated_messages``), so session search reads them too: a word only the
moved rows hold still ranks its chat (:func:`iter_rotated_texts`).

Every search cache is keyed on the LIVE file, which rotation rewrites, so a
rotation already invalidates them. Retention is the one writer that removes a
segment without touching the live file, so it tells search to forget that
text (:func:`forget_expired_archive_text`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

from kiro_crew.jsonl_util import RECORD_CAP, bounded_records

_LOGGER = logging.getLogger("kiro_crew.history")

#: Bytes past which a segment record is skipped rather than read.
_SEGMENT_RECORD_CAP = RECORD_CAP


def rotated_segment_paths(base_dir: Path, key: str) -> list[Path]:
    """*key*'s archive segments under *base_dir*, oldest first, of every reason.

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
        return sorted((p for p in adir.glob(f"{stem}*.jsonl")), key=_seg_order)
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


def forget_expired_archive_text(sessions_dir: Path, stems: Iterable[str]) -> None:
    """Make search stop matching text retention just deleted from *stems*' archives.

    Drops each stem's rows from the search index and the semantic chunk store,
    which are keyed on the live file and so would keep vouching for the old
    text, and moves the stem's cache generation, which ends every in-process
    fold and snippet memo of it. The next query reads the files; the next
    backfill pass re-indexes them.

    shortcut: covers retention in this process and the shared stores only; a
    long-lived second process keeps its own warm fold memo until its next write.
    """
    from kiro_crew.history import ConversationLog
    from kiro_crew.history_index import INDEX_FILENAME, SessionSearchIndex
    from kiro_crew.history_vectors_index import vector_store_for

    stems = sorted({s for s in stems if s})
    if not stems:
        return
    log = ConversationLog(sessions_dir)
    for stem in stems:
        log._invalidate_cache(stem)
    vectors = vector_store_for(sessions_dir)
    try:
        if not vectors.drop(stems):
            _LOGGER.warning("Semantic chunks kept for expired archives of %d chat(s)", len(stems))
    finally:
        vectors.close()
    db_path = sessions_dir / ".index" / INDEX_FILENAME
    if not db_path.exists():
        # No index to clear, and opening one would create it.
        return
    index = SessionSearchIndex(db_path)
    try:
        if not index.drop(stems):
            _LOGGER.warning("Search index kept rows for expired archives of %d chat(s)", len(stems))
    finally:
        index.close()
