"""SyncScheduler -- orchestrates remote source syncing."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from .connectors.base import BaseConnector

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
            source = self._get_source(source_id)
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
            job = self.pipeline.get_job_status(job_id)
            items_created = job["items_processed"] if job else 0
            now = datetime.now().isoformat()
            props["consecutive_failures"] = 0
            if meta:
                props["metadata"] = meta
            self.store.update_source(source_id, last_synced=now, properties=props)
            result.update(synced=True, items_created=items_created)
        except Exception as e:
            logger.exception("Sync failed for source %s", source_id)
            result["error"] = str(e)
            self._record_failure(source_id)
        return result

    def _record_failure(self, source_id: str):
        source = self._get_source(source_id)
        if not source:
            return
        props = json.loads(source.get("properties") or "{}")
        failures = props.get("consecutive_failures", 0) + 1
        props["consecutive_failures"] = failures
        updates = {"properties": props}
        if failures >= MAX_FAILURES:
            # The column is the single source of truth: the dashboard, the
            # watcher's pre-scan skip and sync_all below all read it.
            updates["sync_status"] = "error"
            logger.warning("Source %s reached %d failures, marking as error", source_id, failures)
        self.store.update_source(source_id, **updates)

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
