"""SyncScheduler -- orchestrates remote source syncing."""

from __future__ import annotations

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
            props["sync_status"] = "error"
            # Write the column too, so the reader below (and the dashboard, which
            # reads the column) observes the error regardless of which store it checks.
            updates["sync_status"] = "error"
            logger.warning("Source %s reached %d failures, marking as error", source_id, failures)
        self.store.update_source(source_id, **updates)

    async def sync_all(self) -> list[dict]:
        rows = self.store.db.execute(
            "SELECT id, properties, sync_status FROM sources").fetchall()
        results = []
        for row in rows:
            # sync_status lives in two stores: KnowledgeIngestion writes the COLUMN,
            # while _record_failure historically wrote only the properties JSON. Treat
            # an 'error' value in EITHER store as errored so both writers are observed
            # and legacy JSON-only rows are still skipped.
            props = json.loads(row["properties"] or "{}")
            if row["sync_status"] == "error" or props.get("sync_status") == "error":
                continue
            results.append(await self.sync_source(row["id"]))
        return results
