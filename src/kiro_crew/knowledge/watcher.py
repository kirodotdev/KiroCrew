"""Source watcher -- polls registered local_file sources for changes."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

from .embedder import embed_signature
from .folder_watcher import FolderWatcher
from .ingestion import _REBUILD_STALE_AFTER, IngestionPipeline, rebuild_embeddings
from .store import KnowledgeStore

logger = logging.getLogger(__name__)

FOLDER_SOURCE_TYPES = {"local_folder", "obsidian_vault"}


class KnowledgeWatcher:
    """Polls registered local_file sources for file changes and re-ingests."""

    def __init__(self, store: KnowledgeStore, pipeline: IngestionPipeline,
                 interval: int = 300):
        self.store = store
        self.pipeline = pipeline
        self.interval = interval
        self._stop_event = asyncio.Event()
        self._folder_watcher = FolderWatcher(store, pipeline)
        self._reembed_task: asyncio.Task | None = None

    async def start(self):
        logger.info("Source watcher started: interval=%ds", self.interval)
        while not self._stop_event.is_set():
            try:
                await self._scan()
            except Exception:
                logger.exception("Source watcher scan failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        self._stop_event.set()
        logger.info("Source watcher stopped")

    async def _scan(self):
        """Check all watched sources for changes."""
        # Folder sources (local_folder, obsidian_vault)
        folder_rows = self.store.db.execute(
            "SELECT id, uri, source_type, properties FROM sources WHERE source_type IN ({})".format(
                ",".join("?" for _ in FOLDER_SOURCE_TYPES)),
            tuple(FOLDER_SOURCE_TYPES)).fetchall()
        for row in folder_rows:
            try:
                source = dict(row)
                props = self._parse_props(source.get("properties"))
                if props.get("sync_status") in ("paused", "pending_confirmation"):
                    continue
                stats = await self._folder_watcher.scan_source(source)
                if stats.get("error"):
                    logger.warning("Folder scan error for %s: %s", source["uri"], stats["error"])
                elif any(stats.get(k, 0) for k in ("new", "changed", "deleted")):
                    logger.info("Folder scan %s: +%d ~%d -%d", source["uri"],
                                stats.get("new", 0), stats.get("changed", 0), stats.get("deleted", 0))
            except Exception:
                logger.exception("Error scanning folder source %s", row["uri"])

        # Single-file sources (local_file)
        rows = self.store.db.execute(
            "SELECT id, uri, properties FROM sources WHERE source_type = 'local_file'"
        ).fetchall()

        for row in rows:
            try:
                uri = row["uri"]
                if not uri or uri.startswith(("upload://", "code://", "http://", "https://")):
                    continue
                if is_sensitive_path(uri):
                    logger.warning("Skipping sensitive path: %s", uri)
                    continue
                if not Path(uri).exists():
                    # Mark missing
                    props = self._parse_props(row["properties"])
                    if props.get("sync_status") != "missing":
                        props["sync_status"] = "missing"
                        self.store.update_source(row["id"], properties=json.dumps(props))
                    continue

                mtime = os.stat(uri).st_mtime
                props = self._parse_props(row["properties"])
                stored_mtime = props.get("mtime", 0)

                if mtime > stored_mtime:
                    # Check content hash to avoid re-ingesting touched-but-unchanged files
                    content_hash = await asyncio.get_running_loop().run_in_executor(
                        None, self._hash_file, Path(uri))
                    if content_hash != props.get("content_hash"):
                        logger.info("Source changed: %s", uri)
                        await self.pipeline.ingest_file(
                            uri, source_id=row["id"],
                            namespace=props.get("namespace", "default"),
                        )
                        # Re-read props after ingest (ingest may update them)
                        source = self.store.get_source_by_uri(uri)
                        if source:
                            props = self._parse_props(source.get("properties"))
                    props["mtime"] = mtime
                    props["content_hash"] = content_hash
                    self.store.update_source(row["id"], properties=json.dumps(props))
            except Exception:
                logger.exception("Error checking source %s", row.get("uri", row["id"]))

        # After file-level reconciliation, self-heal vectors left stale by an
        # embedding-setup change (model/budget) -- the file gates above never fire
        # for unchanged files, so this is the only path that catches a sig change.
        await self._maybe_reembed_stale()

    async def _maybe_reembed_stale(self) -> None:
        """Trigger a background sig-gated rebuild when items have a stale embedding sig.

        Single-flight: skips if a rebuild job is already processing or our own
        prior re-embed task is still running. The rebuild runs as a detached task
        (not awaited) so file-change detection isn't blocked for its duration; it
        shares the dashboard's ingestion_jobs progress row so the UI sees it too.
        """
        embedder = getattr(self.pipeline, "embedder", None)
        if not embedder:
            return
        if not await embedder.is_available_async():
            return
        if self._reembed_task and not self._reembed_task.done():
            return
        sig = embed_signature(embedder.model)
        stale = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM items "
            "WHERE status = 'active' AND (embedding_sig IS NULL OR embedding_sig != ?)",
            (sig,)).fetchone()["c"]
        if not stale:
            return
        # Don't stack on a dashboard-triggered rebuild already in flight. A row
        # stale past the window is from a crash that bypassed cleanup; ignore it.
        fresh = (datetime.now() - _REBUILD_STALE_AFTER).isoformat()
        active = self.store.db.execute(
            "SELECT id FROM ingestion_jobs WHERE source_id IS NULL AND status = 'processing' "
            "AND updated_at > ? ORDER BY created_at DESC LIMIT 1",
            (fresh,)
        ).fetchone()
        if active:
            return
        job_id = uuid4().hex[:12]
        now = datetime.now().isoformat()
        self.store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES (?, NULL, 'processing', ?, ?)",
            (job_id, now, now))
        self.store.db.commit()
        logger.info("Watcher self-heal: %d items with stale embedding sig, rebuild job %s",
                    stale, job_id)
        self._reembed_task = asyncio.create_task(self._run_reembed_job(embedder, job_id))

    async def _run_reembed_job(self, embedder, job_id: str) -> None:
        try:
            processed = await rebuild_embeddings(self.store, embedder, job_id=job_id)
            self.store.db.execute(
                "UPDATE ingestion_jobs SET status = 'completed', items_processed = ?, "
                "updated_at = ? WHERE id = ?",
                (processed, datetime.now().isoformat(), job_id))
            self.store.db.commit()
            sel().log_tool_invocation(
                session_key="watcher", agent="knowledge-watcher",
                tool_name="knowledge.batch_embed", outcome="completed",
                resources=str({"count": processed, "rebuild": True, "source": "self_heal"}))
        except BaseException as exc:
            # CancelledError is a BaseException in 3.8+; finalize the row so the
            # single-flight guard can't be permanently blocked, then re-raise it.
            is_cancel = isinstance(exc, asyncio.CancelledError)
            status = "cancelled" if is_cancel else "failed"
            if is_cancel:
                logger.debug("Watcher self-heal rebuild %s cancelled", job_id)
            else:
                logger.exception("Watcher self-heal rebuild %s failed", job_id)
            self.store.db.execute(
                "UPDATE ingestion_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, str(exc), datetime.now().isoformat(), job_id))
            self.store.db.commit()
            sel().log_tool_invocation(
                session_key="watcher", agent="knowledge-watcher",
                tool_name="knowledge.batch_embed", outcome=status,
                resources=str({"rebuild": True, "source": "self_heal"}), error=str(exc))
            if is_cancel:
                raise

    @staticmethod
    def _parse_props(raw) -> dict:
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return raw or {}

    @staticmethod
    def _hash_file(path: Path) -> str:
        if is_sensitive_path(str(path)):
            raise PermissionError(f"Refusing to hash sensitive path: {path}")
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
