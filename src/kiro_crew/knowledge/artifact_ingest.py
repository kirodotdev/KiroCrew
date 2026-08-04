"""Auto-ingest local artifacts into the Knowledge Library.

Content-bearing local artifacts (markdown/text documents the user saves and
iterates) are mirrored into the Knowledge Library so they become searchable and
stay in sync as the artifact changes, and are removed when the artifact is
deleted. On by default (``knowledge.auto_ingest_artifacts``).

Design (plugs into the existing Knowledge source framework rather than adding a
parallel watcher):

* **One aggregate "Artifacts" source** -- a single ``sources`` row of
  ``source_type="artifact"`` (uri ``artifact://``) that appears in the dashboard
  Sources UI like the user's folder/upload sources. Items are grouped
  per-artifact (keyed by slug) in a dedicated ``artifact_item_state`` table --
  the same item-group pattern a folder source uses per file -- so one artifact's
  items can be replaced on edit or removed on delete without touching the rest.

* **Event-driven, no polling.** The gateway is the only process that writes the
  artifact store (the agent's MCP tools, the CLI, the dashboard, and bookmarks
  all HTTP-proxy to the gateway's ``/api/artifacts`` routes; the publishing
  provider pull/clone also funnel through the store). So a single in-process
  change-listener registered on :class:`~kiro_crew.artifacts.ArtifactStore`
  observes every write path. ``upsert`` -> ingest/replace the artifact's item
  group; ``delete`` -> remove it.

* **First-enable backfill tied to source-row creation.** Because the feature is
  on by default, the store may already hold artifacts created before the
  listener existed. The one-time pass that ingests them is tied to the *creation
  of the aggregate source row*: when :func:`ensure_artifact_source` actually
  creates the row (its existence is the idempotency marker), the backfill runs
  once. On every later boot the row already exists, so nothing re-runs.

Content (and the artifact name used as the source title) are redacted for
credentials/exfiltration URLs before they cross into the store, and file-backed
artifacts whose ``source_path`` resolves to a sensitive path are refused.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from kiro_crew.artifacts import ArtifactNotFoundError, ArtifactStore
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel

from .ingestion import DUPLICATE_JOB_STATUS, IngestionPipeline
from .store import KnowledgeStore

logger = logging.getLogger(__name__)

#: Source type for the aggregate artifact source. Lets retrieval/UI distinguish
#: auto-ingested artifacts from folders/uploads.
ARTIFACT_SOURCE_TYPE = "artifact"

#: Stable URI of the single aggregate "Artifacts" source row.
ARTIFACT_SOURCE_URI = "artifact://"

#: Display name of the aggregate source row (shown in the Sources UI).
ARTIFACT_SOURCE_NAME = "Artifacts"

#: Map an artifact ``kind`` to the file extension whose Knowledge reader
#: extractor we want to run. This is the bridge that lets artifacts go through
#: the same ``IngestionPipeline.ingest_file`` -> ``FileReader`` path as folders
#: and uploads (one ingestion path), instead of a parallel raw-text path:
#: html -> ``_read_html`` prose extraction, markdown/text/json -> text. A kind
#: absent here is not ingestible. Every extension here must be in
#: ``FileReader.SUPPORTED``; keep in sync with
#: ``DEFAULT_AUTO_INGEST_ARTIFACT_KINDS``. (``widget`` is intentionally absent
#: -- widgets/dashboards are UI, not documents; ``svg`` is absent -- the reader
#: has no ``.svg`` support.)
_KIND_EXT = {
    "markdown": ".md",
    "text": ".txt",
    "html": ".html",
    "json": ".json",
}


def _redact_for_ingest(text: str) -> str:
    """Scan artifact text for secrets/exfiltration URLs before persisting.

    Artifacts are the user's own local content (low risk), but a scratch
    artifact may contain a pasted credential. Per the security-controls rule
    (never persist secrets), run the same redaction the chat
    path applies before the text lands in the Knowledge store.
    """
    cleaned, _ = redact_credentials(text)
    cleaned, _ = redact_exfiltration_urls(cleaned)
    return cleaned


def ensure_artifact_source(kstore: KnowledgeStore) -> tuple[str, bool]:
    """Get-or-create the single aggregate "Artifacts" source row.

    Returns ``(source_id, created)``. ``created`` is ``True`` only on the call
    that actually inserts the row -- the row's existence is the idempotency
    marker the first-enable backfill keys off, so no separate "backfilled" flag
    is needed.
    """
    existing = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)
    if existing:
        return existing["id"], False
    try:
        source_id = kstore.add_source(
            name=ARTIFACT_SOURCE_NAME,
            source_type=ARTIFACT_SOURCE_TYPE,
            uri=ARTIFACT_SOURCE_URI,
            properties={},
        )
        return source_id, True
    except Exception:
        # Lost a race on the UNIQUE uri -- re-read and treat as pre-existing.
        existing = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)
        if existing:
            return existing["id"], False
        raise


def _get_state(
    kstore: KnowledgeStore, source_id: str, slug: str
) -> tuple[str | None, list[str]]:
    """Return ``(content_hash, item_ids)`` for an artifact's item group, or
    ``(None, [])`` if the artifact has not been ingested yet."""
    row = kstore.db.execute(
        "SELECT content_hash, item_ids FROM artifact_item_state "
        "WHERE source_id = ? AND slug = ?",
        (source_id, slug),
    ).fetchone()
    if not row:
        return None, []
    try:
        ids = json.loads(row["item_ids"] or "[]")
    except (TypeError, ValueError):
        ids = []
    return row["content_hash"], ids


def _set_state(
    kstore: KnowledgeStore,
    source_id: str,
    slug: str,
    content_hash: str,
    item_ids: list[str],
    name: str,
    status: str = "active",
) -> None:
    """Record the content hash + item-id group + display name for one artifact,
    keyed by slug in the dedicated ``artifact_item_state`` table. ``name`` is the
    (redacted) artifact name, used as the per-artifact group label in the
    Sources UI.

    ``status`` is written explicitly on every call because the statement is an
    ``INSERT OR REPLACE``: omitting it would reset a ``deduped`` marker back to
    the column default, and the artifact would be re-ingested and re-collapsed on
    every event."""
    now = datetime.now().isoformat()
    kstore.db.execute(
        "INSERT OR REPLACE INTO artifact_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, slug, content_hash, json.dumps(item_ids), now, name, status),
    )
    kstore.db.commit()


def refresh_artifact_name(
    kstore: KnowledgeStore, source_id: str, slug: str, name: str
) -> bool:
    """Update only the stored display name for an artifact's group, without
    re-ingesting. Used on a metadata-only rename (content unchanged, so no chunk
    churn) to keep the Sources UI group label fresh. Returns ``True`` if a state
    row existed and was updated."""
    cur = kstore.db.execute(
        "UPDATE artifact_item_state SET name = ? WHERE source_id = ? AND slug = ?",
        (name, source_id, slug),
    )
    kstore.db.commit()
    return cur.rowcount > 0


def _del_state(kstore: KnowledgeStore, source_id: str, slug: str) -> None:
    kstore.db.execute(
        "DELETE FROM artifact_item_state WHERE source_id = ? AND slug = ?",
        (source_id, slug),
    )
    kstore.db.commit()


async def ingest_artifact(
    pipeline: IngestionPipeline,
    art_store: ArtifactStore,
    slug: str,
    source_id: str,
    kinds: set[str],
) -> str | None:
    """Ingest (or re-ingest) one artifact into the aggregate Artifacts source.

    Replaces only this artifact's item group within the shared source. Returns
    the ingestion job id, or ``None`` when skipped (ineligible kind, empty
    body, sensitive file-backed path, or unchanged content). Reads live content
    via ``art_store.get`` so file-backed artifacts pick up their on-disk source.
    """
    kstore = pipeline.store
    try:
        # art_store.get reads artifact metadata + content from disk (and the
        # live source file for file-backed artifacts) -- offload the blocking
        # filesystem read so a large/NFS-backed artifact can't stall the loop.
        art = await asyncio.to_thread(art_store.get, slug)
    except ArtifactNotFoundError:
        # Vanished between the event and the read -- nothing to ingest.
        return None
    if art.kind not in kinds:
        return None
    if art.source_path:
        # File-backed artifact: art_store.get() read live content from
        # source_path on disk. Refuse to ingest a file that resolves to a
        # sensitive credential path into the searchable Knowledge store
        # (defense-in-depth; resolves symlinks before checking).
        try:
            resolved = str(Path(art.source_path).expanduser().resolve())
        except (OSError, ValueError, RuntimeError):
            return None
        if is_sensitive_path(resolved):
            logger.warning(
                "Skipping artifact %s: source_path resolves to a sensitive path",
                slug,
            )
            sel().log_tool_invocation(
                session_key="gateway",
                agent="knowledge-artifacts",
                tool_name="knowledge.artifact_ingest",
                outcome="blocked",
                resources=str({"slug": slug, "reason": "sensitive_source_path"}),
            )
            return None
    text = art.content or ""
    if not text.strip():
        return None
    text = _redact_for_ingest(text)
    # art.name is LLM-originated (set by the agent via artifact_save) and is
    # persisted as the source/item title + surfaced in the dashboard, so redact
    # it the same way as the body before it crosses into the store.
    title = _redact_for_ingest(art.name)

    content_hash = hashlib.sha256(text.encode()).hexdigest()
    prev_hash, old_item_ids = _get_state(kstore, source_id, slug)
    if prev_hash == content_hash and old_item_ids:
        # Unchanged since last ingest AND still holding its items -- cheap no-op
        # (per-slug short-circuit). A row with an empty group was left by a refused
        # write, so it falls through and re-attempts rather than reporting a
        # document the Library does not hold.
        return None

    # Same rule as the folder path: a row that owned nothing holds a claim for its
    # previous content, and this artifact has changed.
    kstore.release_stale_claim(source_id, prev_hash, content_hash, old_item_ids)

    ext = _KIND_EXT.get(art.kind)
    if ext is None:
        # Defensive: kind passed the allowlist but has no reader extension
        # (e.g. someone configured an unsupported kind). Skip rather than guess.
        return None

    # Capture the source's items before/after so we can attribute exactly this
    # slug's newly-created items (the only ones the call below adds; the old
    # group is deleted inside ingest_file). The caller serializes events, so
    # nothing else mutates this source concurrently.
    before_ids = {
        r["id"]
        for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)
        ).fetchall()
    }
    # Route through the SAME path as folders/uploads: write the redacted content
    # to a temp file with the kind's real extension and hand it to
    # ingest_file -> FileReader. This gives html artifacts the ``_read_html``
    # prose extraction (not raw markup) and keeps one ingestion path. Redaction
    # is applied to the in-memory content *before* it is written to disk.
    tmp_path: str | None = None
    try:
        def _write_tmp() -> str:
            fd, p = tempfile.mkstemp(suffix=ext, prefix="kc-artifact-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except Exception:
                os.unlink(p)
                raise
            return p

        tmp_path = await asyncio.to_thread(_write_tmp)
        job_id = await pipeline.ingest_file(
            tmp_path,
            original_name=f"{title}{ext}",
            source_id=source_id,
            old_item_ids=old_item_ids,
        )
    finally:
        if tmp_path:
            try:
                await asyncio.to_thread(os.unlink, tmp_path)
            except OSError:
                pass

    status = (pipeline.get_job_status(job_id) or {}).get("status") if job_id else None
    if status == DUPLICATE_JOB_STATUS:
        # The pre-ingest gate refused the write because this text is already in
        # the Library under another source, and deleted this artifact's previous
        # items on the way out. Record that: leaving the prior state would point
        # at deleted items and make every subsequent artifact event re-attempt a
        # write the gate will refuse again.
        _set_state(kstore, source_id, slug, content_hash, [], title, status="deduped")
        return job_id
    if status != "completed":
        # Partial/failed ingest: ingest_file kept the old group and rolled back
        # the new items. Leave the recorded state untouched so the next event
        # retries from the prior good group.
        return job_id
    after_ids = {
        r["id"]
        for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)
        ).fetchall()
    }
    new_ids = list(after_ids - before_ids)
    _set_state(kstore, source_id, slug, content_hash, new_ids, title)
    sel().log_tool_invocation(
        session_key="gateway",
        agent="knowledge-artifacts",
        tool_name="knowledge.artifact_ingest",
        outcome="completed",
        resources=str(
            {"slug": slug, "items": len(new_ids), "content_hash": content_hash[:16]}
        ),
    )
    return job_id


def remove_artifact(kstore: KnowledgeStore, source_id: str, slug: str) -> int:
    """Remove a deleted artifact's item group from the aggregate source.

    Returns the number of Knowledge items removed."""
    prev_hash, item_ids = _get_state(kstore, source_id, slug)
    if item_ids:
        kstore.delete_items_batch(item_ids, owner_source_id=source_id)
    else:
        # An artifact that LOST a dedup owns no items but is still a location of the
        # winner's. Deleting the artifact has to release that claim, or a later winner
        # deletion hands the document to a source whose artifact is gone and the text
        # stays searchable with nothing behind it. Items are the winner's; only the
        # claim is dropped.
        kstore.detach_source_location_by_hash(source_id, prev_hash or "")
    _del_state(kstore, source_id, slug)
    if item_ids:
        logger.info(
            "artifact KB sync: removed %d item(s) for deleted artifact %s",
            len(item_ids),
            slug,
        )
        sel().log_tool_invocation(
            session_key="gateway",
            agent="knowledge-artifacts",
            tool_name="knowledge.artifact_remove",
            outcome="completed",
            resources=str({"slug": slug, "items": len(item_ids)}),
        )
    return len(item_ids)


async def backfill_artifacts(
    pipeline: IngestionPipeline,
    art_store: ArtifactStore,
    source_id: str,
    kinds: set[str],
) -> int:
    """One-time first-enable pass: ingest every eligible pre-existing artifact
    into the freshly-created aggregate source. Returns the number ingested."""
    if not kinds:
        return 0
    ingested = 0
    # art_store.list walks the artifact directory and reads every metadata
    # file -- offload the blocking filesystem walk so a large store doesn't
    # freeze chat turns / liveness heartbeats during the one-time backfill.
    artifacts = await asyncio.to_thread(art_store.list)
    for art in artifacts:
        if art.kind not in kinds:
            continue
        try:
            if await ingest_artifact(pipeline, art_store, art.slug, source_id, kinds):
                ingested += 1
        except Exception:
            logger.exception("artifact KB backfill: failed to ingest %s", art.slug)
    return ingested


class ArtifactKnowledgeSync:
    """Keeps the aggregate Artifacts source in sync with the artifact store.

    Registered as the store's change-listener: ``on_change`` (sync, called from
    the store write path on any thread) schedules the async ingest/remove on the
    gateway loop. A single lock serializes all work so the first-enable backfill
    and live events never interleave mid-ingest.
    """

    def __init__(
        self,
        art_store: ArtifactStore,
        pipeline: IngestionPipeline,
        kinds: set[str],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.art_store = art_store
        self.pipeline = pipeline
        self.kinds = set(kinds)
        self._loop = loop
        self._lock = asyncio.Lock()
        self._backfill_task: asyncio.Task | None = None

    @property
    def kstore(self) -> KnowledgeStore:
        return self.pipeline.store

    # ── store change-listener ─────────────────────────────────────────────

    def on_change(self, action: str, slug: str) -> None:
        """Store change-listener (synchronous). Non-blocking: schedules the
        async handler on the gateway loop and returns immediately, so an
        artifact write is never slowed or broken by ingestion."""
        try:
            self._loop.call_soon_threadsafe(self._schedule, action, slug)
        except RuntimeError:
            # Loop closed (shutdown) -- drop the event.
            logger.debug("artifact KB sync: loop closed, dropped %s %s", action, slug)

    def _schedule(self, action: str, slug: str) -> None:
        task = asyncio.ensure_future(self._handle(action, slug))
        task.add_done_callback(self._on_task_done)

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("artifact KB sync task failed: %s", exc, exc_info=exc)

    async def _handle(self, action: str, slug: str) -> None:
        async with self._lock:
            if action == "delete":
                src = self.kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)
                if src:
                    # Same reasoning as the eligibility path below: a batch delete
                    # plus a graph reload is not loop-safe work.
                    await asyncio.to_thread(remove_artifact, self.kstore, src["id"], slug)
                return
            if action == "rename":
                # Metadata-only rename: refresh the stored display name (the
                # Sources-UI group label) without re-ingesting. No-op if the
                # artifact isn't tracked (ineligible kind / not yet ingested).
                src = self.kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)
                if not src:
                    return
                try:
                    art = await asyncio.to_thread(self.art_store.get, slug)
                except ArtifactNotFoundError:
                    return
                if refresh_artifact_name(
                    self.kstore, src["id"], slug, _redact_for_ingest(art.name)
                ):
                    sel().log_tool_invocation(
                        session_key="gateway",
                        agent="knowledge-artifacts",
                        tool_name="knowledge.artifact_rename",
                        outcome="completed",
                        resources=str({"slug": slug}),
                    )
                return
            source_id, _ = ensure_artifact_source(self.kstore)
            # An upsert can arrive because the artifact's KIND changed, and kind
            # is what decides eligibility. ``ingest_artifact`` early-returns on an
            # ineligible kind, so re-ingesting alone would leave the chunks from
            # the previous kind searchable -- markdown ingested, switched to svg,
            # obsolete prose still answering queries. Reconcile that here: an
            # artifact that is no longer eligible is removed rather than skipped.
            try:
                art = await asyncio.to_thread(self.art_store.get, slug)
            except ArtifactNotFoundError:
                return
            if art.kind not in self.kinds:
                removed = await asyncio.to_thread(
                    remove_artifact, self.kstore, source_id, slug
                )
                if removed:
                    logger.info(
                        "artifact KB sync: %s is no longer an eligible kind (%s); "
                        "removed %d chunk(s)",
                        slug,
                        art.kind,
                        removed,
                    )
                return
            await ingest_artifact(
                self.pipeline, self.art_store, slug, source_id, self.kinds
            )

    # ── startup / backfill ────────────────────────────────────────────────

    async def start(self) -> None:
        """Ensure the aggregate source exists; on first creation, kick off the
        one-time backfill in the background (so gateway startup isn't blocked)."""
        source_id, created = ensure_artifact_source(self.kstore)
        logger.info(
            "artifact KB sync started: source=%s created=%s kinds=%s",
            source_id,
            created,
            sorted(self.kinds),
        )
        if created:
            self._backfill_task = asyncio.create_task(self._run_backfill(source_id))

    async def _run_backfill(self, source_id: str) -> None:
        async with self._lock:
            try:
                ingested = await backfill_artifacts(
                    self.pipeline, self.art_store, source_id, self.kinds
                )
            except Exception:
                logger.exception("artifact KB backfill failed")
                return
        logger.info(
            "artifact KB backfill: %d artifact(s) ingested into new source", ingested
        )
        sel().log_tool_invocation(
            session_key="gateway",
            agent="knowledge-artifacts",
            tool_name="knowledge.artifact_ingest.backfill",
            outcome="completed",
            resources=str({"ingested": ingested}),
        )
