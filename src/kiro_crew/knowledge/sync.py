"""SyncScheduler -- orchestrates remote source syncing."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from .connectors.base import BaseConnector
from .ingestion import ImportChunkBudgetError, run_to_completion

logger = logging.getLogger(__name__)

MAX_FAILURES = 3


class SyncScheduler:
    def __init__(self, store, pipeline, connectors: dict[str, BaseConnector]):
        self.store = store
        self.pipeline = pipeline
        self.connectors = connectors

    def _get_source(self, source_id: str) -> dict | None:
        row = self.store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return dict(row) if row else None

    def get_connector(self, source_type: str) -> BaseConnector | None:
        return self.connectors.get(source_type)

    async def sync_source(self, source_id: str) -> dict:
        result: dict[str, object] = {"synced": False, "items_created": 0, "error": None}
        try:
            # The gate is held from the source lookup through the ingest: the
            # connector awaits sit between the two, and an itemless row with a
            # terminal status is reclaimable by the orphan sweep for that
            # stretch unless the sweep is waiting on this hold.
            async with self.pipeline.ingestion_in_flight():
                # Off the loop: _get_source takes the guarded knowledge
                # connection, and a contended take here busy-waits every task
                # for the whole busy timeout. The lookup deliberately stays
                # INSIDE the gate (see the comment above); only the connection
                # take moves to a worker thread.
                source = await asyncio.to_thread(self._get_source, source_id)
                if not source:
                    result["error"] = f"Source {source_id} not found"
                    return result
                # Merge connector-specific fields from properties into source dict
                props = json.loads(source.get("properties") or "{}")
                source = {**props, **source}
                connector = self.get_connector(source["source_type"])
                if not connector:
                    result["error"] = f"No connector for {source['source_type']}"
                    return result
                if not await connector.detect_changes(source):
                    return result
                text, meta = await connector.fetch(source)
                job_id = await self.pipeline.ingest_text(text, source["name"], source["source_type"],
                                                         source_id=source_id)
            if not job_id:
                return result  # unchanged, nothing to do

            def _settle_success() -> int:
                # One worker unit: the status read decides whether the outcome
                # write may stamp 'synced'. The ingest's own finalize stamps
                # 'error' on a partial or failed ingest and still hands back a
                # job id, so an unconditional 'synced' here would mask that.
                job = self.pipeline.get_job_status(job_id)
                self._record_success(
                    source_id, meta,
                    completed=(job or {}).get("status") == "completed")
                return job["items_processed"] if job else 0

            # run_to_completion, not a bare to_thread: a cancellation landing
            # while the worker item is still queued would drop the outcome
            # write entirely, leaving a stale counter that prematurely
            # quiesces a healthy source. The unit is drained even under
            # cancellation, the way the ingestion finalizers are run.
            items_created = await run_to_completion(_settle_success)
            result.update(synced=True, items_created=items_created)
        except ImportChunkBudgetError as e:
            # A budget deferral is transient, not a sync failure: surface the
            # reasoned message and do NOT call _record_failure (which increments
            # consecutive_failures and can disable the source). The next sync
            # after the window rolls over proceeds normally.
            logger.warning("Sync deferred by import budget for source %s: %s", source_id, e)
            result["deferred"] = str(e)
        except Exception as e:
            logger.exception("Sync failed for source %s", source_id)
            result["error"] = str(e)
            # run_to_completion for the same reason as the success unit: a
            # cancellation that drops this write lets a dead source keep its
            # old count and never reach MAX_FAILURES.
            await run_to_completion(lambda: self._record_failure(source_id))
        return result

    def _record_success(self, source_id: str, meta: dict | None, *,
                        completed: bool):
        # Runs on a worker thread (sync_source offloads it). Recording an
        # OUTCOME -- this reset, or the failure increment below -- is a
        # read-modify-write of the source row, and other writers land on the
        # same row from their own worker threads: the other outcome writer, and
        # the ingest finalize's content-hash stamp. store.revise_source_properties
        # takes the write lock BEFORE reading and applies the revision to the
        # row's CURRENT blob, so the reset can neither resurrect a stale blob
        # nor be lost under one, and two increments cannot both read N. A
        # COMPLETED sync is current information: writing sync_status='synced'
        # clears an 'error' an overlapping failure stamped, so this outcome
        # supersedes it instead of leaving the row quiesced. A partial, failed
        # or duplicate job keeps its hands off the column -- the ingest's own
        # finalize already stamped the truth there.
        def revise(props: dict) -> str | None:
            props["consecutive_failures"] = 0
            if meta:
                props["metadata"] = meta
            return "synced" if completed else None

        self.store.revise_source_properties(
            source_id, revise, last_synced=datetime.now().isoformat())

    def _record_failure(self, source_id: str):
        # Runs on a worker thread (sync_source offloads it); the store's
        # write-locked take keeps the counter read and its write one atomic
        # unit across threads, on this path and the success path alike.
        def revise(props: dict) -> str | None:
            failures = props.get("consecutive_failures", 0) + 1
            props["consecutive_failures"] = failures
            if failures < MAX_FAILURES:
                return None
            # The column is the single source of truth: the dashboard, the
            # watcher's pre-scan skip and sync_all below all read it.
            logger.warning(
                "Source %s reached %d failures, marking as error", source_id, failures)
            return "error"

        self.store.revise_source_properties(source_id, revise)

    async def sync_all(self) -> list[dict]:
        # Off the loop: this is a background coroutine and a contended sqlite
        # read holds it for as long as busy_timeout.
        rows = await asyncio.to_thread(
            lambda: self.store.db.execute("SELECT id, sync_status FROM sources").fetchall())
        results = []
        for row in rows:
            # An errored source stays quiesced instead of being retried every
            # sweep. A row errored before the column existed carries the state in
            # its properties blob only, which cannot be ordered against the
            # column, so it is not promoted: such a row is polled like any other
            # source until an attempt actually FAILS, and that first failure has
            # _record_failure -- whose failure count the row already carries --
            # write the column, after which it quiesces here like the rest. A
            # poll that finds nothing to fetch costs what every healthy source's
            # poll costs; promoting the blob value instead would let a state no
            # writer has touched since overrule the column.
            if row["sync_status"] == "error":
                continue
            results.append(await self.sync_source(row["id"]))
        return results
