"""Source watcher -- polls registered local_file sources for changes."""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig, default_project_dir
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

from .autosource import (
    AUTO_ADDED_PROP,
    DEFAULT_DROP_DIRNAME,
    auto_source_still_contained,
    discover_and_register,
)
from .dedup import dedup_sweep
from .embedder import embedder_signature
from .folder_watcher import FolderWatcher, folder_chunk_budget
from .ingestion import (
    FileTooLargeError,
    IngestionPipeline,
    count_stale_items,
    rebuild_embeddings,
    start_rebuild_job,
)
from .project_docs import discover_and_register as discover_project_docs
from .project_docs import (
    is_project_doc_source,
    project_source_still_valid,
)
from .store import KnowledgeStore

logger = logging.getLogger(__name__)

FOLDER_SOURCE_TYPES = {"local_folder", "obsidian_vault"}

# Stale-item count at which the watcher's self-heal rebuild logs a prominent
# warning before starting: a count this large usually means an embedder
# signature change invalidated the whole corpus, and the ensuing full re-embed
# can run for a long time on a big knowledge base.
_LARGE_REBUILD_WARN_THRESHOLD = 1000


class KnowledgeWatcher:
    """Polls registered local_file sources for file changes and re-ingests."""

    def __init__(self, store: KnowledgeStore, pipeline: IngestionPipeline, interval: int = 300,
                 project_dirs: Callable[[], list[str]] | None = None):
        self.store = store
        self.pipeline = pipeline
        self.interval = interval
        self._stop_event = asyncio.Event()
        self._folder_watcher = FolderWatcher(store, pipeline)
        self._reembed_task: asyncio.Task | None = None
        # Last discovery error signature, for log dedup across sweeps.
        self._discover_error_sig: str | None = None
        # Project-docs discovery keeps its OWN error signature: sharing one with
        # the drop folder would let a failure in either suppress the first log of
        # a failure in the other.
        self._project_docs_error_sig: str | None = None
        # Resolver for the directories the user is working in. Injected by the
        # dashboard (which owns chat-slot state) rather than importing dashboard
        # state here. Called ON the event loop -- it reads an in-memory dict the
        # loop mutates -- so it must not do I/O.
        self._project_dirs = project_dirs
        # Sweeps completed, for the dedup cadence.
        self._sweep_count = 0
        # False until a scheduled dedup pass has actually applied deletes; the
        # first scheduled pass previews instead.
        self._dedup_applied_once = False

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

    async def _discover_drop_folder(self) -> None:
        """Register the workspace drop folder if it has appeared.

        Runs every sweep so a folder created after startup is picked up without
        a restart (within one ``interval``). Gated on
        ``knowledge.auto_discover_folder``; re-reads config each sweep so
        toggling the flag takes effect without a restart, matching KiroCrew's
        live-config behaviour. Never raises into the sweep: a discovery failure
        must not stop registered sources from being scanned.
        """
        try:
            cfg = KiroCrewConfig.load()
            if not cfg.knowledge.auto_discover_folder:
                return
            dirname = cfg.knowledge.auto_discover_dirname or DEFAULT_DROP_DIRNAME
            base = await asyncio.to_thread(default_project_dir)
            if not base:
                return
            source_id = await asyncio.to_thread(
                discover_and_register, self.store, base, dirname
            )
            self._discover_error_sig = None
            if source_id:
                # Registering a source that will spend LLM extraction on the
                # user's files is an auditable mutation -- the manual POST path
                # SEL-logs it, so the automatic path must too.
                sel().log_tool_invocation(
                    session_key="gateway", agent="knowledge-watcher",
                    tool_name="knowledge.source.auto_add", outcome="completed",
                    resources=f"source_id={source_id} dirname={dirname}",
                )
        except Exception as exc:
            # Contained so a discovery failure cannot stop the sweep from
            # scanning already-registered sources. Repeats are deduped: this runs
            # every interval, and an unanticipated persistent error would
            # otherwise emit a full traceback forever.
            sig = f"{type(exc).__name__}:{exc}"
            if sig != getattr(self, "_discover_error_sig", None):
                self._discover_error_sig = sig
                logger.warning("Knowledge drop-folder discovery failed", exc_info=True)
            else:
                logger.debug("Knowledge drop-folder discovery still failing: %s", sig)

    async def _discover_project_docs(self) -> None:
        """Register the documents of each project the user is working in.

        Runs every sweep so a project opened after startup is picked up without a
        restart. Gated on ``knowledge.auto_register_project_docs``; re-reads
        config each sweep so toggling the flag takes effect immediately, matching
        Kiro Crew's live-config behaviour. Never raises into the sweep: a
        discovery failure must not stop registered sources from being scanned.
        """
        try:
            cfg = KiroCrewConfig.load()
            if not cfg.knowledge.auto_register_project_docs:
                return
            if self._project_dirs is None:
                return
            # Resolved on the loop: it copies a dict that other coroutines on the
            # loop mutate, so the copy has to happen where those mutations are
            # serialised against it.
            dirs = self._project_dirs()
            if not dirs:
                return
            created = await asyncio.to_thread(discover_project_docs, self.store, dirs)
            self._project_docs_error_sig = None
            for source_id in created:
                # Registering a source that will spend LLM extraction on the
                # user's files is an auditable mutation -- the manual POST path
                # SEL-logs it, so the automatic path must too.
                sel().log_tool_invocation(
                    session_key="gateway", agent="knowledge-watcher",
                    tool_name="knowledge.source.auto_add", outcome="completed",
                    resources=f"source_id={source_id} kind=project_docs",
                )
        except Exception as exc:
            sig = f"{type(exc).__name__}:{exc}"
            if sig != getattr(self, "_project_docs_error_sig", None):
                self._project_docs_error_sig = sig
                logger.warning("Knowledge project-docs discovery failed", exc_info=True)
            else:
                logger.debug("Knowledge project-docs discovery still failing: %s", sig)

    async def _scan(self):
        """Check all watched sources for changes."""
        # Pick up a newly-created workspace drop folder before scanning, so a
        # folder made since the last sweep is ingested in this same pass.
        await self._discover_drop_folder()
        await self._discover_project_docs()
        # Folder sources (local_folder, obsidian_vault)
        folder_rows = self.store.db.execute(
            "SELECT id, uri, source_type, properties FROM sources WHERE source_type IN ({})".format(
                ",".join("?" for _ in FOLDER_SOURCE_TYPES)
            ),
            tuple(FOLDER_SOURCE_TYPES),
        ).fetchall()
        ws_base: str | None = None
        chunk_budget: int | None = None
        for row in folder_rows:
            try:
                source = dict(row)
                props = self._parse_props(source.get("properties"))
                if props.get("sync_status") in ("paused", "pending_confirmation"):
                    continue
                budget: int | None = None
                if props.get(AUTO_ADDED_PROP):
                    # Re-validate containment on EVERY sweep, not just at
                    # registration: the stored URI is a path that can be swapped
                    # for a symlink to an external tree after the fact, and
                    # os.walk would then follow it out of the workspace.
                    if is_project_doc_source(props):
                        # A project repo root lives outside the workspace by
                        # design, so workspace containment is the wrong
                        # invariant; what must still hold is that the recorded
                        # path has not been swapped for a link elsewhere.
                        contained = await asyncio.to_thread(
                            project_source_still_valid, source["uri"])
                        if chunk_budget is None:
                            chunk_budget = self._chunk_budget()
                        budget = chunk_budget or None
                    else:
                        if ws_base is None:
                            ws_base = await asyncio.to_thread(default_project_dir)
                        contained = await asyncio.to_thread(
                            auto_source_still_contained, source["uri"], ws_base or ""
                        )
                    if not contained:
                        logger.warning(
                            "Skipping auto-added source %s: %s no longer resolves to a "
                            "permitted directory", source["id"], source["uri"],
                        )
                        sel().log_tool_invocation(
                            session_key="gateway", agent="knowledge-watcher",
                            tool_name="knowledge.source.auto_scan_denied",
                            outcome="denied",
                            resources=f"source_id={source['id']} reason=not_contained",
                        )
                        continue
                else:
                    # A hand-added folder is paced too. The user asked for the whole
                    # folder and still gets it -- newest files first, the rest on
                    # later sweeps -- but pointing the Library at a source repo no
                    # longer spends the whole bill before anyone can look at it.
                    budget = folder_chunk_budget(props)
                stats = await self._folder_watcher.scan_source(source, chunk_budget=budget)
                if stats.get("error"):
                    logger.warning("Folder scan error for %s: %s", source["uri"], stats["error"])
                elif any(stats.get(k, 0) for k in ("new", "changed", "deleted")):
                    logger.info(
                        "Folder scan %s: +%d ~%d -%d",
                        source["uri"],
                        stats.get("new", 0),
                        stats.get("changed", 0),
                        stats.get("deleted", 0),
                    )
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
                        None, self._hash_file, Path(uri)
                    )
                    if content_hash != props.get("content_hash"):
                        logger.info("Source changed: %s", uri)
                        try:
                            await self.pipeline.ingest_file(
                                uri,
                                source_id=row["id"],
                                namespace=props.get("namespace", "default"),
                            )
                        except FileTooLargeError:
                            # Warning already logged by the pipeline (names the file
                            # and the config key). Mark the source errored and skip
                            # persisting mtime/hash so the file is re-evaluated on
                            # the next scan -- raising knowledge.max_ingest_file_mb
                            # (config is read live) then recovers it automatically.
                            self.store.db.execute(
                                "UPDATE sources SET sync_status = 'error' WHERE id = ?",
                                (row["id"],))
                            self.store.db.commit()
                            continue
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
        self._sweep_count += 1
        await self._maybe_dedup_sweep()

    @staticmethod
    def _chunk_budget() -> int:
        """Chunks an auto-registered source may ingest in one sweep.

        Read per sweep so the value is live. 0 disables the bound.
        """
        try:
            return max(0, int(KiroCrewConfig.load().knowledge.auto_ingest_chunk_budget))
        except Exception:
            logger.debug("Could not read auto_ingest_chunk_budget", exc_info=True)
            return 0

    async def _maybe_dedup_sweep(self) -> None:
        """Collapse duplicate documents on a cadence.

        The per-ingest ``dedup_document`` call is O(n) against the just-written
        document, and the pre-ingest hash gate refuses byte-identical writes --
        but neither catches a NEAR-duplicate (the same document edited slightly
        between two sources) nor a duplicate that predates them. Only a full
        sweep does, which is why one runs here rather than only from the CLI and
        the MCP tool.

        The FIRST sweep in a process is a dry run that only logs what it would
        collapse. A scheduled sweep differs in kind from a human-invoked one, not
        just in frequency: it deletes unattended, and on an existing Library the
        first pass is the one with a backlog to work through. A logged preview
        makes that pass observable before anything is deleted, and costs one
        sweep's delay.

        Gated on ``knowledge.dedup_every_n_sweeps`` (0 disables). Contained: a
        dedup failure must not stop the next sweep from scanning.
        """
        try:
            every = max(0, int(KiroCrewConfig.load().knowledge.dedup_every_n_sweeps))
        except Exception:
            logger.debug("Could not read dedup_every_n_sweeps", exc_info=True)
            return
        if not every or self._sweep_count % every:
            return
        preview = not self._dedup_applied_once
        try:
            # Full O(n^2) corpus pass that can merge entities and rebuild the
            # graph -- never on the event loop.
            # An UNATTENDED pass applies only exact-content matches. A collapse
            # deletes the loser's copy, so a wrong fuzzy match (same filename,
            # cosine over the threshold, different facts -- two weekly reports)
            # would silently cost a document its unique text with nobody watching.
            # Exact-hash duplicates are the case automatic registration actually
            # creates (one file in a repo and its worktree), and those are facts,
            # not judgements. Fuzzy candidates are still found and reported, for
            # the user to apply deliberately via the CLI or the dedup tool.
            results = await asyncio.to_thread(
                dedup_sweep, self.store, apply=not preview, certain_only=True)
        except Exception:
            logger.warning("Scheduled knowledge dedup sweep failed", exc_info=True)
            return
        if preview:
            self._dedup_applied_once = True
            if results:
                logger.warning(
                    "Scheduled knowledge dedup (first pass, PREVIEW ONLY -- nothing "
                    "deleted) found %d duplicate document(s); the next scheduled pass "
                    "will collapse them: %s",
                    len(results),
                    ", ".join(f"{r['loser']} -> {r['winner']} [{r['reason']}]"
                              for r in results[:20]))
            return
        if results:
            logger.info("Scheduled knowledge dedup collapsed %d duplicate document(s)",
                        len(results))

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
        sig = embedder_signature(embedder)
        # Stale count excludes items in retry backoff (recently-failed) so a
        # perpetually-failing item can't drive a fresh rebuild every scan.
        # OFFLOADED: this COUNT(*) scans the items table (no index on
        # embedding_sig); on a large KB under WAL contention from a concurrent
        # embedder it can stall for tens of seconds, and an inline call would
        # block the event loop past the loop-watchdog threshold and crash-loop
        # the gateway (observed on a ~1.3GB KB after an embedder-sig change).
        stale = await asyncio.to_thread(count_stale_items, self.store, sig)
        if not stale:
            return
        if stale >= _LARGE_REBUILD_WARN_THRESHOLD:
            logger.warning(
                "Watcher self-heal: %d items have a stale embedding sig (likely an "
                "embedder signature change) — starting a full background re-embed. "
                "This may take a while on a large knowledge base.", stale,
            )
        # Atomically claim the single-flight slot (sweeps crashed leftovers, guards
        # against racing the dashboard trigger). None -> a rebuild is already running.
        # Offloaded: the BEGIN IMMEDIATE write-lock acquisition (busy_timeout up to
        # 10s) must not block the event loop this coroutine runs on.
        job_id = await asyncio.to_thread(start_rebuild_job, self.store)
        if job_id is None:
            return
        logger.info(
            "Watcher self-heal: %d items with stale embedding sig, rebuild job %s", stale, job_id
        )
        self._reembed_task = asyncio.create_task(self._run_reembed_job(embedder, job_id))

    async def _run_reembed_job(self, embedder, job_id: str) -> None:
        try:
            processed = await rebuild_embeddings(self.store, embedder, job_id=job_id)
            # OFFLOADED: single-row write, but a commit can block up to the
            # busy_timeout behind a concurrent writer — keep it off the loop.
            await asyncio.to_thread(self._finalize_reembed_job, job_id, processed)
            sel().log_tool_invocation(
                session_key="watcher",
                agent="knowledge-watcher",
                tool_name="knowledge.batch_embed",
                outcome="completed",
                resources=str({"count": processed, "rebuild": True, "source": "self_heal"}),
            )
        except BaseException as exc:
            # CancelledError is a BaseException in 3.8+; finalize the row so the
            # single-flight guard can't be permanently blocked, then re-raise it.
            is_cancel = isinstance(exc, asyncio.CancelledError)
            status = "cancelled" if is_cancel else "failed"
            if is_cancel:
                logger.debug("Watcher self-heal rebuild %s cancelled", job_id)
            else:
                logger.exception("Watcher self-heal rebuild %s failed", job_id)
            # Best-effort finalize: if this UPDATE itself raises (e.g. db locked while
            # cancelling), it must not replace the CancelledError -- asyncio shutdown
            # has to see the cancel, so guard the SQL and re-raise unconditionally.
            # Deliberately NOT offloaded: awaiting to_thread inside a CancelledError
            # handler can be re-cancelled before the write lands, breaking the
            # single-flight finalize guarantee; a single-row best-effort write is
            # an acceptable inline cost on this error path.
            try:
                self.store.db.execute(
                    "UPDATE ingestion_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, str(exc), datetime.now().isoformat(), job_id),
                )
                self.store.db.commit()
                sel().log_tool_invocation(
                    session_key="watcher",
                    agent="knowledge-watcher",
                    tool_name="knowledge.batch_embed",
                    outcome=status,
                    resources=str({"rebuild": True, "source": "self_heal"}),
                    error=str(exc),
                )
            except Exception:
                logger.exception(
                    "Watcher self-heal: best-effort finalize of %s also failed", job_id
                )
            if is_cancel:
                raise

    def _finalize_reembed_job(self, job_id: str, processed: int) -> None:
        """Mark a self-heal rebuild job completed (runs on a worker thread).

        ``store.db`` is a per-thread connection, so the write and its commit stay
        on this thread's own connection.
        """
        self.store.db.execute(
            "UPDATE ingestion_jobs SET status = 'completed', items_processed = ?, "
            "updated_at = ? WHERE id = ?",
            (processed, datetime.now().isoformat(), job_id),
        )
        self.store.db.commit()

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
