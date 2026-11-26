"""An unchanged folder file's failed chunks: when one is due, and its in-place re-extraction.

``FolderWatcher`` binds ``reextract_failed_chunks`` and ``settle_unchanged_done`` as its
``_reextract_failed_chunks`` and ``_settle_unchanged_done`` methods;
:mod:`kiro_crew.knowledge.folder_watcher` stays the import path. The chunks are marked by
the ingest (``items.extraction_failed_at``, see
:mod:`kiro_crew.knowledge.ingestion_extraction_failures`).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from datetime import datetime

from .extractor import extraction_source
from .ingestion import (
    EXTRACTION_INCOMPLETE_PREFIX,
    extraction_incomplete_message,
    run_to_completion,
)

logger = logging.getLogger("kiro_crew.knowledge.folder_watcher")


def _extraction_incomplete(state: dict | None) -> bool:
    """A 'done' row whose last ingest stored chunks its extraction call failed on."""
    return bool(
        state
        and state.get("status") == "done"
        and (state.get("error_message") or "").startswith(EXTRACTION_INCOMPLETE_PREFIX)
    )


def _extraction_retry_due(state: dict | None, max_attempts: int) -> bool:
    """An extraction-incomplete row with re-extraction budget left.

    Its unchanged file is not re-ingested: only the chunks whose call failed are
    re-extracted, in place (``_reextract_failed_chunks``), so a retry can never
    cost a chunk that extracted before. The row stays 'done' throughout, and
    ``attempts`` counts the extractions that came back incomplete under the same
    *max_attempts* cap (``MAX_SCAN_ATTEMPTS``) as an interrupted ingest. A spent budget leaves the row
    'done' with its error message and no further retries until the file changes.
    """
    return _extraction_incomplete(state) and int((state or {}).get("attempts") or 0) < max_attempts


def _budget_spent(chunk_budget: int | None, chunks_ingested: int, stats: dict) -> bool:
    """After a re-extraction charges the scan, whether it spent the chunk budget.

    The same stop an ingest makes, so a retry that uses up the allowance ends the
    sweep before another file is ingested past it.
    """
    if not (chunk_budget and chunks_ingested >= chunk_budget):
        return False
    stats["chunks"] = chunks_ingested
    stats["budget_reached"] = 1
    return True


# A failed chunk is re-extracted no sooner than this after its last failure, doubling
# per attempt (1 h, then 2 h), so the attempts span an outage instead of three
# consecutive sweeps landing inside it.
REEXTRACT_BACKOFF_SECS = 3600


def _reextract_backoff_elapsed(last_failed_at: str | None, attempts: int, now: str) -> bool:
    if not last_failed_at:
        return True
    try:
        waited = (
            datetime.fromisoformat(now) - datetime.fromisoformat(last_failed_at)
        ).total_seconds()
    except ValueError:
        return True
    return waited >= REEXTRACT_BACKOFF_SECS * 2 ** max(0, attempts - 1)


async def reextract_failed_chunks(
    self,
    source_id: str,
    file_path: str,
    state: dict,
    content_hash: str,
    mtime: float,
    now: str,
    embed_priority: int,
) -> int:
    """Re-extract, in place, an unchanged file's chunks whose extraction failed.

    Only those items are touched, and one that fails again keeps what it had.
    Waits out the backoff since the chunks' last failure first. Records the
    outcome on the row, which stays 'done': the message and count clear when
    nothing is left unextracted, otherwise the message names what is left and
    ``attempts`` grows. Returns the extraction calls made, for the scan's
    chunk budget.
    """
    item_ids = json.loads(state.get("item_ids") or "[]")
    attempts = int(state.get("attempts") or 0)
    rows = await asyncio.to_thread(self.store.items_with_failed_extraction, item_ids)
    if rows and not _reextract_backoff_elapsed(
        max(r["extraction_failed_at"] for r in rows), attempts, now
    ):
        return 0
    try:
        with extraction_source(source_id):
            left = await self.pipeline.reextract_failed_items(rows, embed_priority=embed_priority)
    except Exception as exc:
        from .llm_pool import LLMPoolClosed

        if isinstance(exc, LLMPoolClosed):
            # Gateway exit, not an attempt: the marks stay, and the next start retries.
            raise
        # Counted as an attempt like any other that left chunks unextracted:
        # the file's stored chunks are untouched, so it stays 'done'.
        logger.warning("Re-extraction of %s failed", file_path, exc_info=True)
        left = len(rows)
    if left < len(rows):
        # The source's summary is built from its chunk summaries, and some came
        # back: rebuild it now rather than at the next ingest into this source.
        await self.pipeline.generate_source_summary(source_id)
    # Only over the row read before the awaits above: see _settle_unchanged_done.
    await run_to_completion(
        functools.partial(
            self._settle_unchanged_done,
            source_id,
            file_path,
            state,
            content_hash,
            mtime,
            now,
            extraction_incomplete_message(left, len(item_ids)),
            attempts + 1 if left else 0,
        )
    )
    return len(rows)


def settle_unchanged_done(
    self,
    source_id: str,
    file_path: str,
    state: dict,
    content_hash: str,
    mtime: float,
    now: str,
    error_message: str | None,
    attempts: int,
) -> bool:
    """Re-record an unchanged file's 'done' row, only over the row *state* read.

    *state* was read when the sweep began; the callers write after awaits. A
    dedup sweep takes no scan lock and may have collapsed the group meanwhile,
    so, as in ``_settle_done``, the write lands only while the row is still
    that 'done' row (same bytes hash and group), under one ``BEGIN IMMEDIATE``.
    Returns ``True`` when written.
    """
    self.store.db.execute("BEGIN IMMEDIATE")
    try:
        row = self.store.db.execute(
            "SELECT status, content_hash, item_ids FROM folder_file_state "
            "WHERE source_id = ? AND file_path = ?",
            (source_id, file_path),
        ).fetchone()
        current = row is not None and (row["status"], row["content_hash"], row["item_ids"]) == (
            "done",
            state.get("content_hash"),
            state.get("item_ids"),
        )
        if current:
            self._update_state(
                source_id,
                file_path,
                content_hash,
                mtime,
                row["item_ids"],
                now,
                "done",
                error_message,
                attempts=attempts,
                commit=False,
            )
        self.store.db.execute("COMMIT")
    except BaseException:
        self.store.db.execute("ROLLBACK")
        raise
    return current
