"""Failed extraction calls: how an ingest counts and reports them, and the in-place
re-extraction of the chunks they left unextracted.

A chunk whose extraction call failed is stored and searchable but marked
(``items.extraction_failed_at``). The ingest paths count such chunks and write
``EXTRACTION_INCOMPLETE_PREFIX`` to the job row; the folder watcher keys its bounded
retry on that prefix and hands the marked items to ``reextract_failed_items``, which
``IngestionPipeline`` binds as a method.

:mod:`kiro_crew.knowledge.ingestion` stays the import path and the patch surface for
every name here. This module never imports the facade: ``_redact`` and
``run_to_completion`` live there and are read from :data:`sys.modules` at call time, so a
patch of either through the facade reaches this code too.
"""

from __future__ import annotations

import asyncio
import logging
import sys as _sys

from kiro_crew.embeddings import PRIORITY_NORMAL

from .extractor import extraction_failed

logger = logging.getLogger("kiro_crew.knowledge.ingestion")


def _facade():  # -> the kiro_crew.knowledge.ingestion module
    return _sys.modules["kiro_crew.knowledge.ingestion"]


# Leads the error recorded when a document committed with chunks whose extraction
# call failed. Those chunks are stored and searchable, so the ingest is not a
# failure, but the folder watcher keys its bounded re-extraction on this prefix.
EXTRACTION_INCOMPLETE_PREFIX = "entity extraction failed for "


def extraction_incomplete_message(failed: int, total: int) -> str | None:
    if not failed:
        return None
    return f"{EXTRACTION_INCOMPLETE_PREFIX}{failed} of {total} chunks"


def _count_failed_extractions(extractions: list[dict], total: int, label: str) -> int:
    failed = sum(1 for extraction in extractions if extraction_failed(extraction))
    if failed:
        logger.warning(
            "Entity extraction failed for %d of %d chunks of %s; they are stored "
            "without entities or summary",
            failed,
            total,
            label,
        )
    return failed


def _redact_extraction(extraction: dict) -> None:
    """Redact, in place, the LLM-derived text an extraction stores."""
    extraction["summary"] = _facade()._redact(extraction.get("summary"))
    for ent in extraction.get("entities", []):
        ent["name"] = _facade()._redact(ent.get("name")) or ""
        ent["description"] = _facade()._redact(ent.get("description"))


async def reextract_failed_items(
    self,
    rows: list[dict],
    *,
    embed_priority: int = PRIORITY_NORMAL,
) -> int:
    """Re-extract stored chunks whose extraction call failed, in place.

    *rows* come from ``KnowledgeStore.items_with_failed_extraction``. A chunk
    that now extracts gets its entities, then its title, type and summary
    with the mark cleared, then a fresh embedding. A chunk that fails again
    keeps everything it had and its mark is re-stamped, which is what the
    caller's backoff reads. Nothing else in the document is touched, so a
    failed retry can never cost a chunk that extracted before. Returns how
    many chunks are still unextracted.
    """
    if not rows:
        return 0
    if not self.extractor.can_extract:
        return len(rows)
    # Held like an ingest's (``ingest_file``): these writes re-link an existing
    # source's items to entities in several autocommit steps, and the deferred
    # orphan sweep must not read an entity between its insert and its mention.
    async with self._ingestion_in_flight():
        extractions = await self.extractor.extract_batch([row["content"] for row in rows])
        still_failed: list[str] = []
        for row, extraction in zip(rows, extractions):
            if extraction_failed(extraction):
                still_failed.append(row["id"])
                continue
            try:
                _redact_extraction(extraction)
                title = _facade()._redact(extraction.get("title")) or row["title"]
                await asyncio.to_thread(self.store.clear_item_entity_links, row["id"])
                await asyncio.to_thread(self._store_entities, extraction, row["id"])

                def _complete(_id=row["id"], _title=title, _extraction=extraction) -> None:
                    self.store.complete_reextraction(
                        _id,
                        title=_title,
                        item_type=_extraction.get("category", "document"),
                        summary=_extraction.get("summary"),
                    )

                await _facade().run_to_completion(_complete)
                await self._embed_item(
                    row["id"],
                    title,
                    extraction.get("summary"),
                    row["content"],
                    embed_priority=embed_priority,
                )
            except Exception:
                logger.exception("Failed to store the re-extraction of item %s", row["id"])
                still_failed.append(row["id"])
        if still_failed:
            await _facade().run_to_completion(
                lambda: self.store.mark_extraction_failed(still_failed)
            )
    return len(still_failed)
