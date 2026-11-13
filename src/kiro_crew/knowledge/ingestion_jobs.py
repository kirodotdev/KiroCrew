"""Startup upkeep for source-ingest job rows.

Every ingest writes its ``ingestion_jobs`` row as 'processing' before any work and
settles it on the way out, failure included. A gateway that stops mid-ingest (a
restart, a crash, a kill) settles nothing, and without this sweep the row would read
'processing' for good. The corpus rebuild sweeps its own leftovers in
``start_rebuild_job``; this module is the same sweep for the rows that carry a
``source_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import KnowledgeStore

#: The ``error`` an interrupted source-ingest job row is reclaimed with.
INTERRUPTED_JOB_ERROR = "abandoned: the gateway stopped before this ingest finished"


def reclaim_interrupted_ingest_jobs(store: KnowledgeStore, *, created_before: str) -> int:
    """Move source-ingest job rows a previous gateway left 'processing' to 'abandoned'.

    Run once at startup, before this process ingests anything. Only rows created
    before *created_before* (an ISO timestamp taken before this process could start
    an ingest) are moved, so a live job is never reclaimed. Rebuild rows
    (``source_id IS NULL``) keep their own staleness rule. Nothing is re-ingested
    here: a folder file whose ingest was cut short still carries its scan marker,
    and the next scan picks it up.

    Returns how many rows moved. Blocking; callers run it off the event loop.
    """
    cur = store.db.execute(
        "UPDATE ingestion_jobs SET status = 'abandoned', error = ?, updated_at = ? "
        "WHERE source_id IS NOT NULL AND status = 'processing' AND created_at < ?",
        (INTERRUPTED_JOB_ERROR, datetime.now().isoformat(), created_before),
    )
    store.db.commit()
    return cur.rowcount
