"""Ingestion pipeline -- orchestrates read -> chunk -> extract -> store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel

from .autosource import AUTO_ADDED_PROP
from .chunker import CHUNK_OVERLAP, CHUNK_TOKEN_SIZE, HeadingAwareChunker
from .dedup import PERSISTENT_SOURCE_TYPES, dedup_document
from .embedder import embedder_signature, floats_to_bytes
from .extractor import EntityExtractor
from .readers import FileReader
from .store import KnowledgeStore

logger = logging.getLogger(__name__)

CODE_EXTS = {
    '.py', '.java', '.ts', '.js', '.rs', '.go', '.rb', '.c', '.cpp', '.h',
    '.sh', '.cs', '.kt', '.swift', '.scala',
}

MARKDOWN_EXTS = {'.md', '.docx'}

#: ``ingestion_jobs.status`` for a write the pre-ingest gate refused because the
#: exact content is already in the Library. Terminal, like 'completed'.
DUPLICATE_JOB_STATUS = 'skipped_duplicate'

DEFAULT_MAX_INGEST_FILE_MB = 100.0
_MB = 1024 * 1024


class FileTooLargeError(RuntimeError):
    """Raised when a source file exceeds ``knowledge.max_ingest_file_mb``."""


def _max_ingest_file_mb() -> float:
    # config.loader imports knowledge.doc_links, so a top-level import here
    # would create an import cycle.
    from kiro_crew.config.loader import KiroCrewConfig  # circular import
    try:
        return KiroCrewConfig.load().knowledge.max_ingest_file_mb
    except Exception:
        return DEFAULT_MAX_INGEST_FILE_MB


def _run_chunker(chunker: HeadingAwareChunker, ext: str, text: str, uri: str) -> list[dict]:
    """Dispatch to the right chunker. CPU-bound -- run via asyncio.to_thread."""
    if ext == '.pptx':
        return chunker.chunk_slides(text)
    if ext in CODE_EXTS:
        return chunker.chunk_code(text, language=ext.lstrip('.'))
    if ext in MARKDOWN_EXTS:
        return chunker.chunk_markdown(text)
    return chunker.chunk(text, source_uri=uri)


def _redact(text: str | None) -> str | None:
    """Redact LLM-derived text before storing."""
    if not text:
        return text
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _redact_for_ingest(text: str) -> str:
    """Scrub a document's OWN text of secrets before anything downstream reads it.

    Distinct from :func:`_redact`, which cleans text the model produced. This one
    runs on the raw document, ahead of chunking, extraction and storage, so a
    credential pasted into a file never reaches the extraction worker or the index.
    Same helper the artifact path uses on its body.
    """
    cleaned, _ = redact_credentials(text)
    cleaned, _ = redact_exfiltration_urls(cleaned)
    return cleaned


def _coerce_chunk_param(value: object, default: int, minimum: int) -> int:
    """Coerce a per-source chunk_size/chunk_overlap property to a usable int.

    Source ``properties`` are user-editable JSON (the dashboard add_source API
    accepts an arbitrary ``properties`` object), so these may hold a
    non-numeric string ("abc", "1.5"), NaN, or a nonsensical number. int()
    raises ValueError/TypeError on those — and by the time the chunker is
    built the ingestion job row already exists, so the crash aborts the ingest
    and strands the job in 'processing'. Fall back to the default instead, and
    treat values below ``minimum`` (e.g. a zero/negative target_size, which
    breaks the recursive splitter) as absent.
    """
    try:
        coerced = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return coerced if coerced >= minimum else default


def _job_status(processed: int, total: int) -> str:
    if processed == 0 and total > 0:
        return 'failed'
    if processed < total:
        return 'partial'
    return 'completed'


def _first_line_title(content: str) -> str:
    """Extract title from first non-empty line, stripped of markdown markers."""
    for line in content.split("\n"):
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return ""


class IngestionPipeline:
    """Orchestrates: read file -> chunk -> extract entities -> store."""

    def __init__(self, store: KnowledgeStore, extractor: EntityExtractor,
                 chunker: HeadingAwareChunker, reader: FileReader, embedder=None,
                 dedup_enabled: bool = True):
        self.store = store
        self.extractor = extractor
        self.chunker = chunker
        self.reader = reader
        self.embedder = embedder
        self._dedup_enabled = dedup_enabled

    def _skip_as_duplicate(self, content_hash: str, source_id: str | None,
                           old_item_ids: list[str] | None = None) -> str | None:
        """Terminal job id when this exact document is already in the Library.

        Returns ``None`` when the write should proceed.

        Every chunk of a document carries the document's whole-text
        ``content_hash``, so an exact hit on that indexed column means the text is
        already stored. Refusing the write here -- rather than writing it and
        letting the de-duplication sweep collapse it afterwards -- is what makes
        "no duplicate is written" true, and it costs one indexed lookup instead of
        a chunking pass plus an LLM extraction call per chunk. It covers every
        ingest path because it sits inside the pipeline, not at a call site.

        A hit inside *source_id* itself is not a duplicate: that is the same
        document being re-ingested, which must proceed so its item group is
        replaced.

        ``old_item_ids`` -- the items this call was going to REPLACE -- are
        deleted before returning. Refusing the write is not the same as doing
        nothing: the document's content changed to something already stored
        elsewhere, so its previous items are now stale. Leaving them would keep
        superseded text searchable, and (for a folder file, whose state row is
        then recorded with an empty group) leave them unreachable by the deleted-
        file path forever.

        This gate is exact-hash only, and complements rather than replaces the
        sweep: only the sweep catches a NEAR-duplicate (the same document edited
        slightly between two sources) or a duplicate that already exists, and it
        needs embeddings, so it can never run inline.

        The skip is recorded as a terminal ``ingestion_jobs`` row rather than a
        bare ``None`` return, so a caller can tell "already present" from
        "nothing to do" through the job status it already reads.
        """
        if not content_hash:
            return None
        holder = self.store.find_doc_by_content_hash(
            content_hash, exclude_source_id=source_id)
        if not holder:
            return None
        if self._outranks_holder(source_id, str(holder.get("source_type") or "")):
            return None
        if old_item_ids:
            self.store.delete_items_batch(list(old_item_ids), owner_source_id=source_id)
        # This source HAS a copy of the document -- it just does not need a second
        # physical one. Under "one document, many locations" that has to be recorded,
        # or the copy is invisible to the reference count: deleting the holder would
        # destroy the only items while this source's file still sits on disk, and the
        # content would vanish from the Library with nothing to bring it back.
        # Attaching costs nothing and makes the refusal safe.
        if source_id:
            for row in self.store.db.execute(
                    "SELECT id FROM items WHERE content_hash = ? AND source_id = ?",
                    (content_hash, holder.get("source_id"))).fetchall():
                self.store.add_source_location(row["id"], source_id)
        job_id = uuid4().hex[:12]
        now = datetime.now().isoformat()
        self.store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, items_total, "
            "items_processed, created_at, updated_at) "
            f"VALUES (?, ?, '{DUPLICATE_JOB_STATUS}', 0, 0, ?, ?)",
            (job_id, source_id, now, now))
        self.store.db.commit()
        logger.info(
            "Skipping ingest: identical content already in source %r (%s)",
            holder.get("source_name"), holder.get("source_type"))
        return job_id

    def _source_is_auto_added(self, source_id: str) -> bool:
        """True when this source was registered automatically, not chosen by the user.

        Read from the source row's own properties rather than passed in, because every
        auto path (project docs, the agent aggregate, the auto-registered drop folder)
        already marks itself and a caller-supplied flag would be one more thing each
        new path could forget. Failure is treated as NOT auto-added: a missing or
        malformed row must not silently start scrubbing a hand-registered folder.
        """
        try:
            row = self.store.db.execute(
                "SELECT properties FROM sources WHERE id = ?", (source_id,)).fetchone()
        except Exception:
            return False
        if not row or not row["properties"]:
            return False
        try:
            props = json.loads(row["properties"])
        except (TypeError, ValueError):
            return False
        return bool(isinstance(props, dict) and props.get(AUTO_ADDED_PROP))

    def _outranks_holder(self, source_id: str | None, holder_type: str) -> bool:
        """True when the incoming source must win over the current holder.

        "Already exists elsewhere" does not mean the existing copy is the one
        worth keeping. De-duplication ranks copies by ``PERSISTENT_SOURCE_TYPES``
        -- a folder, vault or wiki, something that re-syncs, outranks a transient
        one-shot upload or chat capture -- and this gate honours the same ranking
        so arrival order cannot invert it.

        So when a watched project folder holds content that currently lives only
        in a transient upload, the folder copy is allowed to land and the
        post-ingest sweep collapses the pair through ``pick_winner``, keeping the
        persistent copy. Otherwise the only searchable copy would sit in an upload
        whose deletion takes the content with it.

        Equal rank refuses, which is the cheap path: it skips the chunking and
        extraction the sweep would immediately undo.
        """
        if not source_id or holder_type in PERSISTENT_SOURCE_TYPES:
            return False
        row = self.store.db.execute(
            "SELECT source_type FROM sources WHERE id = ?", (source_id,)).fetchone()
        return bool(row) and str(row["source_type"] or "") in PERSISTENT_SOURCE_TYPES

    def _maybe_dedup(self, source_id: str, content_hash: str = "") -> None:
        """Collapse cross-source duplicates of the just-ingested document.

        Targeted (O(n)) -- compares only the new document against the corpus, not the
        whole corpus against itself. *content_hash* names the document just written:
        a source id alone is ambiguous once a source holds more than one document.
        Best-effort: a dedup failure must never fail an ingestion that already
        succeeded, so errors are swallowed (logged at debug). No-op when disabled.
        """
        if not self._dedup_enabled:
            return
        try:
            dedup_document(self.store, source_id, content_hash=content_hash or None,
                           apply=True)
        except Exception:
            logger.debug("Post-ingest dedup skipped", exc_info=True)

    async def ingest_file(self, path: str, on_progress=None, original_name: str = "", namespace: str = "default", source_id: str = "", old_item_ids: list[str] | None = None) -> str | None:
        """Full pipeline. Returns job_id, or None if content hash unchanged.

        If source_id is provided, ingests into that existing source instead of
        creating a new one (used for remote source sync).
        If old_item_ids is provided, only those items are replaced (folder sources).
        Otherwise all items for the source are replaced (single-file sources).
        """
        p = Path(path)
        display_name = original_name or p.name
        # Separate copy for the two sinks a name is allowed to reach: the error
        # raised back to the caller who supplied it, and the audit record. It never
        # reaches the application log -- see the oversized branch below. display_name
        # is caller-supplied (an upload's filename, a folder file's name, a document
        # title) and a name is free-form enough to carry a credential, so both of
        # those sinks take the redacted form. The stored display_name is untouched --
        # it is the document's title. No ``or`` fallback: _redact returns its input
        # unchanged when falsy, so a fallback could only ever re-yield the same empty
        # string while handing static analysis a genuine unredacted edge.
        log_name = _redact(display_name)
        ext = (Path(original_name).suffix if original_name else p.suffix).lower()

        # Defense-in-depth: refuse sensitive paths before any filesystem access
        # (size stat below, reader.read after), even though callers pre-filter.
        resolved = await asyncio.to_thread(lambda: str(p.resolve()))
        if is_sensitive_path(path) or is_sensitive_path(resolved):
            sel().log_tool_invocation(
                session_key="ingestion", agent="knowledge-ingest",
                tool_name="knowledge.ingest_denied", outcome="denied",
                resources=f"source_id={source_id} file={log_name} reason=sensitive_path",
            )
            raise PermissionError(f"Refusing to ingest sensitive path: {log_name}")

        # Size guard BEFORE reading: chunking a very large file is CPU-bound and
        # previously hung gateway startup for 25s+ with only a raw faulthandler
        # dump (no actionable error). Skip with a clear WARNING naming the file.
        limit_mb = _max_ingest_file_mb()
        try:
            file_size = await asyncio.to_thread(os.path.getsize, path)
        except OSError:
            file_size = 0
        if limit_mb > 0 and file_size > limit_mb * _MB:
            msg = (
                f"Skipping oversized file '{log_name}' "
                f"({file_size / _MB:.1f} MB > knowledge.max_ingest_file_mb={limit_mb:g} MB); "
                f"raise knowledge.max_ingest_file_mb in config to ingest it"
            )
            # The name goes to the caller and the audit record, NOT to the log. A
            # document name is caller-supplied and free-form enough to carry a
            # credential, and the application log is the one sink of the three with
            # no redaction contract and the widest reach (files, aggregators, and
            # anyone with host access). The size, the limit and the source id are
            # enough to act on: they say what to raise and which source to look at,
            # and the SEL event below carries the redacted name for the audit trail.
            logger.warning(
                "Skipping oversized file for source_id=%s (%.1f MB > "
                "knowledge.max_ingest_file_mb=%g MB); raise "
                "knowledge.max_ingest_file_mb in config to ingest it",
                source_id or "(new)", file_size / _MB, limit_mb,
            )
            sel().log_tool_invocation(
                session_key="ingestion", agent="knowledge-ingest",
                tool_name="knowledge.ingest_denied", outcome="denied",
                resources=(f"source_id={source_id} file={log_name} "
                           f"reason=oversized size_mb={file_size / _MB:.1f} limit_mb={limit_mb:g}"),
            )
            raise FileTooLargeError(msg)

        # 1. Read (offloaded: readers do synchronous whole-file parsing -- pdfplumber,
        # python-docx, etc. -- which must not block the event loop on a large file)
        if on_progress:
            on_progress('reading', 0, 1)
        text, meta = await asyncio.to_thread(self.reader.read, path)
        if meta.get('format') == 'error':
            raise RuntimeError(f"Failed to read {path}: {meta.get('error')}")

        # Content the user never explicitly chose to index gets its secrets scrubbed
        # BEFORE anything else sees it. A hand-registered folder is a deliberate act;
        # an auto-registered one is not, so a credential sitting in a project runbook
        # would otherwise reach the extraction worker and the index without anyone
        # having agreed to it. This is the same scrub the artifact path already applies
        # to its body, in the same position -- ahead of the hash, so the stored text
        # and its identity agree and a re-scan is stable.
        if source_id and self._source_is_auto_added(source_id):
            text = _redact_for_ingest(text)

        # 2. Hash check + source resolution
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        _old_item_ids: list[str] = old_item_ids if old_item_ids is not None else []
        if source_id:
            # Ingest into existing source (remote sync path)
            if old_item_ids is None:
                # Single-file/remote source: replace all items for this source
                _old_item_ids = [row['id'] for row in self.store.db.execute(
                    "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
            props: dict[str, object] = {}
            existing: dict | None = {"id": source_id}  # sentinel — source already exists
            src_row = self.store.db.execute(
                "SELECT uri, properties FROM sources WHERE id = ?", (source_id,)).fetchone()
            uri = src_row["uri"] if src_row else display_name
            props = json.loads(src_row["properties"] or "{}") if src_row else {}
        else:
            # Local file path: find or create source by URI
            uri = str(p.resolve())
            existing = self.store.get_source_by_uri(uri)
            if existing:
                props = json.loads(existing.get('properties', '{}')) if isinstance(existing.get('properties'), str) else existing.get('properties', {})
                if props.get('content_hash') == content_hash:
                    return None
                source_id = existing['id']
                _old_item_ids = [row['id'] for row in self.store.db.execute(
                    "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
            else:
                props = {}
                source_id = self.store.add_source(
                    name=display_name, source_type='local_file', uri=uri,
                    properties={'content_hash': content_hash, **meta},
                )

        # 3. Job record
        dupe_job = self._skip_as_duplicate(content_hash, source_id, _old_item_ids)
        if dupe_job:
            return dupe_job
        job_id = uuid4().hex[:12]
        now = datetime.now().isoformat()
        self.store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) VALUES (?, ?, 'processing', ?, ?)",
            (job_id, source_id, now, now))
        self.store.db.commit()

        # The job row above is persisted as 'processing' BEFORE any fallible
        # work runs, and nothing below ever wrote 'failed' on an uncaught
        # exception — so any crash between here and finalize (chunker,
        # extractor, DB error) stranded the job in 'processing' forever and
        # the folder-watcher retried the file every scan. Mark the job failed
        # on the way out and re-raise; callers keep seeing the original error.
        try:
            return await self._ingest_file_body(
                job_id=job_id, source_id=source_id, props=props, meta=meta,
                ext=ext, text=text, uri=uri, content_hash=content_hash,
                display_name=display_name, namespace=namespace,
                existing=existing, old_item_ids=old_item_ids,
                _old_item_ids=_old_item_ids, path=path, on_progress=on_progress,
            )
        except Exception:
            try:
                self.store.db.execute(
                    "UPDATE ingestion_jobs SET status = 'failed', updated_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), job_id))
                self.store.db.execute(
                    "UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,))
                self.store.db.commit()
            except Exception:  # noqa: BLE001 - never mask the original error
                logger.warning("failed to mark ingestion job %s failed", job_id, exc_info=True)
            raise

    async def _ingest_file_body(self, *, job_id, source_id, props, meta, ext, text,
                                uri, content_hash, display_name, namespace,
                                existing, old_item_ids, _old_item_ids, path,
                                on_progress) -> str | None:
        """Chunk/extract/store/finalize — split out so ingest_file can mark the
        pre-inserted job row 'failed' on ANY exception in one place."""
        # 4. Chunk (use per-source chunk size if configured)
        chunker = self.chunker
        chunk_size = props.get("chunk_size")
        chunk_overlap = props.get("chunk_overlap")
        if chunk_size or chunk_overlap:
            chunker = HeadingAwareChunker(
                target_size=_coerce_chunk_param(chunk_size, CHUNK_TOKEN_SIZE, minimum=1),
                overlap=_coerce_chunk_param(chunk_overlap, CHUNK_OVERLAP, minimum=0),
            )
        is_markdown = ext in MARKDOWN_EXTS
        # Chunking is CPU-bound (recursive separator splitting); offloaded so a
        # large document can't block the event loop past the loop watchdog.
        chunks = await asyncio.to_thread(_run_chunker, chunker, ext, text, uri)

        total = len(chunks)
        self.store.db.execute("UPDATE ingestion_jobs SET items_total = ? WHERE id = ?", (total, job_id))
        self.store.db.commit()

        # 5. Extract all chunks in batch via pool
        # Capture existing items before ingestion (for safe partial-failure rollback)
        _before_ids = {r["id"] for r in self.store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}
        chunk_contents = [chunk['content'] for chunk in chunks]
        extractions = await self.extractor.extract_batch(chunk_contents)

        processed = 0
        for i, (chunk, extraction) in enumerate(zip(chunks, extractions)):
            try:
                extraction['summary'] = _redact(extraction.get('summary'))
                for ent in extraction.get('entities', []):
                    ent['name'] = _redact(ent.get('name')) or ''
                    ent['description'] = _redact(ent.get('description'))
                item_title = (
                    _redact(extraction.get('title'))
                    or _first_line_title(chunk['content'])
                    or f"{Path(display_name).stem} chunk {i}"
                )
                item_tags = ['content_type:markdown'] if is_markdown else None
                item_id = self.store.add_item(
                    title=item_title,
                    content=chunk['content'],
                    item_type=extraction.get('category', 'document'),
                    source_id=source_id,
                    chunk_index=chunk.get('chunk_index', i),
                    summary=extraction.get('summary'),
                    namespace=namespace,
                    tags=item_tags,
                    content_hash=content_hash,
                )
                self.store.add_source_location(
                    item_id=item_id, source_id=source_id,
                    chunk_range=f"{chunk.get('line_start', 0)}-{chunk.get('line_end', 0)}",
                    section_title=chunk.get('section_title'),
                )
                # Entity storage is O(entities): find_entity does a full-table
                # alias scan and each add_entity/add_mention/add_entity_relation
                # commits + mutates the graph. On a many-entity chunk this blocked
                # the asyncio loop past the 25s watchdog (loop-stall crash dumps,
                # dashboard "connection lost"). Offloaded: the graph is RLock-guarded
                # and sqlite connections are thread-local, so this is thread-safe.
                await asyncio.to_thread(self._store_entities, extraction, item_id)
                await self._embed_item(
                    item_id, item_title, extraction.get('summary'), chunk['content']
                )
                processed += 1
            except Exception:
                logger.exception("Failed to process chunk %d of %s", i, path)
            if on_progress:
                on_progress('extracting', i + 1, total)

        # 6. Finalize
        now = datetime.now().isoformat()
        if processed == total:
            self.store.delete_items_batch(_old_item_ids, owner_source_id=source_id)
            if existing:
                self.store.update_source(source_id, properties=json.dumps({**props, 'content_hash': content_hash, **meta}))
            self.store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (source_id,))
            self.store.update_source(source_id, last_synced=now)
            # Generate file-level summary from chunk summaries
            try:
                await self.generate_source_summary(source_id)
            except Exception:
                logger.debug("Source summary generation skipped for %s", source_id, exc_info=True)
        elif processed < total:
            # Partial failure: remove only items created during THIS ingestion call
            after_ids = {r["id"] for r in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,),
            ).fetchall()}
            new_ids = list(after_ids - _before_ids)
            self.store.delete_items_batch(new_ids)
            self.store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,))
        self.store.db.execute(
            "UPDATE ingestion_jobs SET status = ?, items_processed = ?, updated_at = ? WHERE id = ?",
            (_job_status(processed, total), processed, now, job_id))
        self.store.db.commit()
        # Cross-source dedup for whole-source ingests (upload / remote / chat). Folder-file
        # ingests (old_item_ids is a list) are swept by FolderWatcher at end of scan.
        if processed == total and old_item_ids is None:
            # dedup_document can merge entities -> _load_graph (full graph
            # rebuild). Offloaded so a large source's dedup cannot stall the loop
            # (RLock-guarded graph + thread-local sqlite make this thread-safe).
            await asyncio.to_thread(self._maybe_dedup, source_id, content_hash)
        return job_id

    async def ingest_text(self, text: str, title: str, source_type: str = 'manual',
                          source_id: str | None = None,
                          old_item_ids: list[str] | None = None) -> str | None:
        """Ingest raw text (dashboard drop, chat, or a shared aggregate source).

        Without ``source_id`` the source is found-or-created by a
        content-hash-derived URI (legacy dashboard-drop behaviour: each
        distinct body is its own source). With ``source_id`` the text is
        ingested into that existing source:

        * ``old_item_ids is None`` -> replace *all* items for the source
          (single-text / remote-sync source).
        * ``old_item_ids`` provided -> replace only that item group, leaving
          the source's other item groups untouched. This is what lets one
          source hold many independently-replaceable documents -- the
          aggregate "Artifacts" source keys a group per artifact slug, exactly
          as a folder source keys a group per file.
        """
        content_hash = hashlib.sha256(text.encode()).hexdigest()

        # Resolve the source and the prior item ids this call should replace.
        _old_item_ids: list[str] = old_item_ids if old_item_ids is not None else []
        if source_id is None:
            uri = f"{source_type}://{content_hash[:16]}"
            existing = self.store.get_source_by_uri(uri)
            if existing:
                props = json.loads(existing.get('properties', '{}')) if isinstance(existing.get('properties'), str) else existing.get('properties', {})
                if props.get('content_hash') == content_hash:
                    return None  # unchanged
                source_id = existing['id']
                _old_item_ids = [row['id'] for row in self.store.db.execute(
                    "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
            else:
                source_id = self.store.add_source(
                    name=title, source_type=source_type, uri=uri,
                    properties={'content_hash': content_hash},
                )
        elif old_item_ids is None:
            # Existing source, replace-all (single-text / remote-sync source).
            _old_item_ids = [row['id'] for row in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]

        dupe_job = self._skip_as_duplicate(content_hash, source_id, _old_item_ids)
        if dupe_job:
            return dupe_job

        job_id = uuid4().hex[:12]
        now = datetime.now().isoformat()
        self.store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) VALUES (?, ?, 'processing', ?, ?)",
            (job_id, source_id, now, now))
        self.store.db.commit()

        chunks = await asyncio.to_thread(self.chunker.chunk, text)
        total = len(chunks)

        # Snapshot items present before this call so a partial failure removes
        # only what THIS call created -- never another item group that shares
        # the same source_id (critical for the aggregate Artifacts source).
        _before_ids = {r["id"] for r in self.store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}

        chunk_contents = [chunk['content'] for chunk in chunks]
        extractions = await self.extractor.extract_batch(chunk_contents)

        processed = 0
        for i, (chunk, extraction) in enumerate(zip(chunks, extractions)):
            try:
                extraction['summary'] = _redact(extraction.get('summary'))
                for ent in extraction.get('entities', []):
                    ent['name'] = _redact(ent.get('name')) or ''
                    ent['description'] = _redact(ent.get('description'))
                item_id = self.store.add_item(
                    title=chunk.get('section_title') or f"{title} chunk {i}",
                    content=chunk['content'],
                    item_type=extraction.get('category', 'document'),
                    source_id=source_id,
                    chunk_index=chunk.get('chunk_index', i),
                    summary=extraction.get('summary'),
                    content_hash=content_hash,
                )
                self.store.add_source_location(
                    item_id=item_id, source_id=source_id,
                    chunk_range=f"{chunk.get('line_start', 0)}-{chunk.get('line_end', 0)}",
                    section_title=chunk.get('section_title'),
                )
                # Entity storage is O(entities): find_entity does a full-table
                # alias scan and each add_entity/add_mention/add_entity_relation
                # commits + mutates the graph. On a many-entity chunk this blocked
                # the asyncio loop past the 25s watchdog (loop-stall crash dumps,
                # dashboard "connection lost"). Offloaded: the graph is RLock-guarded
                # and sqlite connections are thread-local, so this is thread-safe.
                await asyncio.to_thread(self._store_entities, extraction, item_id)
                await self._embed_item(
                    item_id,
                    chunk.get('section_title') or f"{title} chunk {i}",
                    extraction.get('summary'),
                    chunk['content'],
                )
                processed += 1
            except Exception:
                logger.exception("Failed to process chunk %d of text '%s'", i, title)

        now = datetime.now().isoformat()
        if processed == total:
            self.store.delete_items_batch(_old_item_ids, owner_source_id=source_id)
            self.store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (source_id,))
            self.store.update_source(source_id, last_synced=now)
            try:
                await self.generate_source_summary(source_id)
            except Exception:
                logger.debug("Source summary generation skipped for %s", source_id, exc_info=True)
        elif processed < total:
            # Partial failure: remove only items created during THIS call so we
            # never delete another item group sharing this source_id.
            after_ids = {r["id"] for r in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,),
            ).fetchall()}
            self.store.delete_items_batch(list(after_ids - _before_ids))
            self.store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (source_id,))
        self.store.db.execute(
            "UPDATE ingestion_jobs SET status = ?, items_total = ?, items_processed = ?, updated_at = ? WHERE id = ?",
            (_job_status(processed, total), total, processed, now, job_id))
        self.store.db.commit()
        # Cross-source dedup for whole-source ingests (upload / remote / chat).
        # Group-level replaces (old_item_ids provided -- e.g. a single artifact's
        # group within the aggregate Artifacts source) defer to the folder-scan
        # dedup sweep, mirroring ingest_file, so one artifact edit doesn't rescan
        # the entire aggregate source.
        if processed == total and old_item_ids is None:
            # dedup_document can merge entities -> _load_graph (full graph
            # rebuild). Offloaded so a large source's dedup cannot stall the loop
            # (RLock-guarded graph + thread-local sqlite make this thread-safe).
            await asyncio.to_thread(self._maybe_dedup, source_id, content_hash)
        return job_id

    def get_job_status(self, job_id: str) -> dict | None:
        row = self.store.db.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def _store_entities(self, extraction: dict, item_id: str):
        """Deduplicate and store entities, mentions, and relations."""
        entity_map: dict[str, str] = {}  # name -> entity_id
        for ent in extraction.get('entities', []):
            name = ent.get('name', '').strip()
            if not name:
                continue
            existing = self.store.find_entity(name)
            if existing:
                eid = existing['id']
            else:
                eid = self.store.add_entity(
                    name=name,
                    entity_type=ent.get('type', 'concept'),
                    description=ent.get('description'),
                )
            entity_map[name] = eid
            self.store.add_mention(item_id, eid, context=ent.get('description'))

        for rel in extraction.get('relations', []):
            src_name = rel.get('source', '').strip()
            tgt_name = rel.get('target', '').strip()
            src_id = entity_map.get(src_name)
            tgt_id = entity_map.get(tgt_name)
            if src_id and tgt_id:
                self.store.add_entity_relation(
                    source_id=src_id, target_id=tgt_id,
                    relation_type=_redact(rel.get('type', 'uses')) or 'uses',
                    description=_redact(rel.get('description')),
                    source_item_id=item_id,
                )

    async def _embed_item(
        self, item_id: str, title: str, summary: str | None, content: str | None = None
    ) -> None:
        """Generate and store embedding for an item. No-op if embedder is None.

        Includes chunk ``content`` so vector search matches body text, not just
        the title/summary (which previously left body-only queries unmatchable).
        """
        if not self.embedder:
            return
        # Capture the signature BEFORE the embed, and stamp the row with THAT
        # value — the same discipline _write_item_embedding already follows by
        # taking `sig` as a parameter.
        #
        # `self.embedder` is a thin wrapper whose `.model` property resolves the
        # LIVE shared singleton (InProcessEmbedder.model -> get_shared_embedder()),
        # so evaluating embedder_signature() down at the UPDATE would read
        # whichever model is current THEN, not the one that produced `vec`. A
        # model change landing in that gap would stamp an old-model vector with
        # the new signature — and because the re-embed sweep is sig-gated
        # (`embedding_sig != ?`), that row would be skipped forever rather than
        # self-healing on the next pass.
        #
        # Capturing first fails safe in the one direction that matters: if the
        # swap lands mid-embed, the vector is new but the sig is old, so the
        # sweep re-embeds it — wasteful, never wrong.
        sig = embedder_signature(self.embedder)
        loop = asyncio.get_running_loop()
        vec = await loop.run_in_executor(
            None, self.embedder.embed_for_item, title, summary, content
        )
        if vec:
            self.store.db.execute(
                "UPDATE items SET embedding = ?, embedding_sig = ?, embedded_at = ? WHERE id = ?",
                (floats_to_bytes(vec), sig,
                 datetime.now().isoformat(), item_id))
            self.store.db.commit()

    async def generate_source_summary(self, source_id: str) -> None:
        """Generate a file-level summary from chunk summaries via LLM pool. No-op if pool unavailable."""
        if not self.extractor._pool:
            return
        rows = self.store.db.execute(
            "SELECT summary FROM items WHERE source_id = ? AND summary IS NOT NULL AND summary != '' ORDER BY chunk_index",
            (source_id,)).fetchall()
        if not rows:
            return
        chunk_summaries = "\n".join(r["summary"] for r in rows)
        # Cap input to avoid token overflow (~2000 tokens max)
        if len(chunk_summaries) > 4000:
            chunk_summaries = chunk_summaries[:4000]
        prompt = (
            "Given these section summaries from a document, produce a JSON object with:\n"
            '- "topic": a single sentence (max 30 words) describing the document\n'
            '- "themes": an array of 3-5 short theme tags\n\n'
            f"Sections:\n{chunk_summaries}\n\n"
            "Respond with ONLY the JSON object, no markdown."
        )
        try:
            response = await self.extractor._pool.send(prompt, timeout=30.0)
            # Parse JSON from response
            m = re.search(r'\{[\s\S]*\}', response)
            if m:
                data = json.loads(m.group())
                topic = _redact(data.get("topic", ""))
                themes = json.dumps([r for t in data.get("themes", [])[:5] if (r := _redact(t))])
                self.store.db.execute(
                    "UPDATE sources SET summary_topic = ?, summary_themes = ? WHERE id = ?",
                    (topic, themes, source_id))
                self.store.db.commit()
        except Exception:
            logger.debug("Source summary generation failed for %s", source_id, exc_info=True)


_REBUILD_BATCH_SIZE = 50

# A rebuild commits progress (refreshing updated_at) at least every batch. A job
# row stuck in 'processing' past this window is from a crash that bypassed cleanup,
# so the single-flight guard treats it as dead and lets a new rebuild start.
_REBUILD_STALE_AFTER = timedelta(minutes=10)

# Items that just failed a re-embed (vec is None) keep a stale sig but get an
# `embedded_at` stamp; the watcher backs off from re-triggering on them until this
# window elapses, so a perpetually-failing item (Ollama down) can't drive a fresh
# rebuild every scan interval. Longer than _REBUILD_STALE_AFTER so a legit retry
# isn't suppressed but a tight retrigger loop is.
_REEMBED_RETRY_BACKOFF = timedelta(minutes=15)


def _heartbeat_rebuild_job(store, job_id: str, now_iso: str) -> None:
    """Advance a rebuild job's ``updated_at`` (single write + commit).

    Called via ``asyncio.to_thread`` from the async rebuild loop so the blocking
    SQLite write never runs on the event loop. ``store.db`` resolves to the
    WORKER thread's own connection (per-thread ``threading.local``), so this is
    safe to run off-loop; the loop awaits it serially.
    """
    store.db.execute(
        "UPDATE ingestion_jobs SET updated_at = ? WHERE id = ?", (now_iso, job_id)
    )
    store.db.commit()


def _write_item_embedding(store, item_id: str, blob: bytes, sig: str, now_iso: str, snap) -> bool:
    """Persist a freshly-computed vector for one item; return True if it landed.

    Runs via ``asyncio.to_thread`` (blocking SQLite off the event loop; worker's
    own thread-local connection). The ``updated_at <= snap`` guard skips the
    write when a concurrent re-ingest rewrote the item mid-embed (lost-update
    protection) — ``cur.rowcount == 0`` then, so the caller counts it as failed.
    """
    cur = store.db.execute(
        "UPDATE items SET embedding = ?, embedding_sig = ?, embedded_at = ? "
        "WHERE id = ? AND (updated_at IS NULL OR updated_at <= ?)",
        (blob, sig, now_iso, item_id, snap))
    store.db.commit()
    return bool(cur.rowcount)


def _stamp_embed_attempt(store, item_id: str, now_iso: str) -> None:
    """Stamp ``embedded_at`` after a transient embed failure (leaves sig stale so
    the item is retried, but backs the watcher off it). Offloaded like the other
    per-item writes so it never blocks the event loop.
    """
    store.db.execute(
        "UPDATE items SET embedded_at = ? WHERE id = ?", (now_iso, item_id))
    store.db.commit()


def _init_rebuild_total(store, count_where: str, params_tail: tuple, job_id: str) -> None:
    """Count the rebuild's total items and stamp it on the job row (single commit).

    Runs on a worker thread (``asyncio.to_thread``): the COUNT(*) scans the items
    table and can stall for tens of seconds on a large KB under WAL contention, so
    it must never run inline on the gateway event loop. ``store.db`` is a
    per-thread connection; the UPDATE and its commit stay on this thread's own
    connection.
    """
    total = store.db.execute(
        f"SELECT COUNT(*) AS c FROM items WHERE {count_where}",  # noqa: S608
        params_tail).fetchone()["c"]
    store.db.execute(
        "UPDATE ingestion_jobs SET items_total = ?, updated_at = ? WHERE id = ?",
        (total, datetime.now().isoformat(), job_id))
    store.db.commit()


def _fetch_rebuild_page(store, page_where: str, params_tail: tuple, last_id: str):
    """Fetch one keyset page of items to re-embed (worker thread; see above)."""
    return store.db.execute(
        f"SELECT id, title, summary, content, updated_at FROM items WHERE {page_where} "  # noqa: S608
        "ORDER BY id LIMIT ?",
        (*params_tail, last_id, _REBUILD_BATCH_SIZE)).fetchall()


def _commit_rebuild_progress(store, job_id: str | None, processed: int, failed: int) -> None:
    """Write end-of-batch progress counters and commit (worker thread; see above)."""
    if job_id is not None:
        store.db.execute(
            "UPDATE ingestion_jobs SET items_processed = ?, items_failed = ?, "
            "updated_at = ? WHERE id = ?",
            (processed, failed, datetime.now().isoformat(), job_id))
    store.db.commit()


def _select_active_rebuild_job(store, *, now: datetime | None = None):
    """Return a FRESH (within the staleness window) active rebuild job row, or None.

    A corpus-wide rebuild is identified by ``source_id IS NULL`` + ``status='processing'``.
    Rows older than the staleness window are crashed leftovers and are NOT returned
    (callers sweep them). Must be called inside an open transaction by the claimer.
    """
    fresh = ((now or datetime.now()) - _REBUILD_STALE_AFTER).isoformat()
    return store.db.execute(
        "SELECT id FROM ingestion_jobs WHERE source_id IS NULL AND status = 'processing' "
        "AND updated_at > ? ORDER BY created_at DESC LIMIT 1",
        (fresh,)).fetchone()


def start_rebuild_job(store, *, now: datetime | None = None) -> str | None:
    """Atomically claim the single-flight slot for a corpus rebuild.

    Wraps check-then-insert in a single ``BEGIN IMMEDIATE`` transaction so the
    watcher's 300s tick and a user-clicked rebuild can't both observe "no active
    job" and each insert one (the per-path single-flight guard was not safe across
    paths). Also sweeps any stale ``processing`` rows from prior crashes to
    ``abandoned`` so they don't accumulate as phantom jobs forever.

    Returns the new ``job_id`` if this caller claimed the slot, or ``None`` if a
    fresh rebuild is already in flight (caller should not start one).
    """
    now = now or datetime.now()
    ts = now.isoformat()
    fresh = (now - _REBUILD_STALE_AFTER).isoformat()
    store.db.execute("BEGIN IMMEDIATE")
    try:
        if _select_active_rebuild_job(store, now=now) is not None:
            store.db.execute("COMMIT")
            return None
        # Sweep crashed leftovers (stale 'processing' rows) so they don't linger.
        store.db.execute(
            "UPDATE ingestion_jobs SET status = 'abandoned', "
            "error = 'abandoned: updated_at past staleness window', updated_at = ? "
            "WHERE source_id IS NULL AND status = 'processing' AND updated_at <= ?",
            (ts, fresh))
        job_id = uuid4().hex[:12]
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES (?, NULL, 'processing', ?, ?)",
            (job_id, ts, ts))
        store.db.execute("COMMIT")
        return job_id
    except Exception:
        store.db.execute("ROLLBACK")
        raise


def count_stale_items(store, sig: str, *, now: datetime | None = None) -> int:
    """Count active items whose embedding sig is stale and not in retry backoff.

    Excludes items re-attempted within ``_REEMBED_RETRY_BACKOFF`` (stale sig but a
    recent ``embedded_at``) so a perpetually-failing item can't make the watcher
    re-trigger every scan interval.
    """
    cutoff = ((now or datetime.now()) - _REEMBED_RETRY_BACKOFF).isoformat()
    return store.db.execute(
        "SELECT COUNT(*) AS c FROM items WHERE status = 'active' "
        "AND (embedding_sig IS NULL OR embedding_sig != ?) "
        "AND (embedded_at IS NULL OR embedded_at < ?)",
        (sig, cutoff)).fetchone()["c"]


async def rebuild_embeddings(store, embedder, *, job_id: str | None = None,
                             force: bool = False) -> int:
    """Re-embed active items in place, stamping the current embedding signature.

    Sig-gated by default: only items whose stored ``embedding_sig`` differs from the
    current setup (or is NULL) are re-embedded, which makes the operation idempotent
    — a partial-failure retry skips already-done items, and a re-run on an unchanged
    setup is a no-op. ``force=True`` re-embeds every active item regardless of sig
    (escape hatch for suspected vector corruption).

    Vectors are overwritten one item at a time so search stays queryable throughout.
    When ``job_id`` is given, progress is written to that ``ingestion_jobs`` row; the
    same function powers the dashboard trigger and the watcher self-heal. Returns the
    number of items successfully re-embedded.

    Serial single-item embed (the in-process embedder is the CPU floor and fans out
    internally); batch size is only the commit/progress cadence, not a throttle.
    """
    loop = asyncio.get_running_loop()
    sig = embedder_signature(embedder)
    processed = 0
    failed = 0
    # Keep the COUNT predicate and the page predicate as separate strings so neither
    # is derived by stripping a clause out of the other (a string-replace that would
    # silently no-op if the WHERE were ever reworded).
    if force:
        count_where = "status = 'active'"
        page_where = "status = 'active' AND id > ?"
        params_tail: tuple = ()
    else:
        count_where = "status = 'active' AND (embedding_sig IS NULL OR embedding_sig != ?)"
        page_where = count_where + " AND id > ?"
        params_tail = (sig,)

    if job_id is not None:
        # OFFLOADED: the total COUNT scans the items table and can stall on a
        # large KB; an inline call blocks the event loop (loop-watchdog risk).
        await asyncio.to_thread(_init_rebuild_total, store, count_where, params_tail, job_id)

    last_id = ""
    while True:
        # OFFLOADED: page reads can block behind a concurrent writer's
        # busy_timeout; keep every DB touch in this loop off the event loop.
        rows = await asyncio.to_thread(
            _fetch_rebuild_page, store, page_where, params_tail, last_id)
        if not rows:
            break
        for row in rows:
            vec = await loop.run_in_executor(
                None, embedder.embed_for_item, row["title"], row["summary"], row["content"]
            )
            now_iso = datetime.now().isoformat()
            # Per-item SQLite writes are OFFLOADED (asyncio.to_thread): a sync
            # write can block up to the busy_timeout under a concurrent writer,
            # and rebuild_embeddings is awaited on the gateway event loop — an
            # inline write would freeze chat/liveness (the
            # no-blocking-call-on-event-loop rule). store.db is a per-thread connection
            # (threading.local, WAL); the loop awaits each serially, so there is
            # no concurrent-connection use.
            if vec:
                # Guard against a lost update: if ingestion (file-change re-ingest)
                # rewrote title/content while we were embedding, its updated_at moved
                # past our snapshot's -- skip our stale-vector UPDATE and let that
                # item re-embed via its own _embed_item. ``snap`` is the row's
                # updated_at at read time.
                snap = row["updated_at"]
                landed = await asyncio.to_thread(
                    _write_item_embedding, store, row["id"], floats_to_bytes(vec), sig, now_iso, snap
                )
                if landed:
                    processed += 1
                else:
                    failed += 1  # raced with a concurrent writer; counts as not-done
            else:
                # Transient embed failure: leave sig stale (so it's retried) but stamp
                # embedded_at as the attempt time so the watcher backs off this item.
                await asyncio.to_thread(_stamp_embed_attempt, store, row["id"], now_iso)
                failed += 1
            last_id = row["id"]
            if job_id is not None:
                # Heartbeat the job row PER ITEM, not just per batch: a single embed
                # is the CPU floor (Ollama), so 50 serial embeds can exceed
                # _REBUILD_STALE_AFTER on a slow/cold host. If updated_at only
                # advanced at end-of-batch, the single-flight claimer would judge a
                # live rebuild abandoned mid-batch and start a second one (duplicated
                # embedding work). Committing the timestamp each item keeps the job
                # demonstrably alive within the staleness window. The heavier
                # progress counters still land once per batch below.
                #
                # OFFLOAD the write: rebuild_embeddings is awaited on the gateway
                # event loop, and a synchronous SQLite write can block up to the
                # busy_timeout when another writer holds the lock — freezing chat /
                # liveness (the no-blocking-call-on-event-loop rule). ``store.db`` is
                # a per-thread connection (threading.local, WAL), so the worker
                # thread safely uses its OWN connection to the same db; the loop
                # awaits it serially, so there is no concurrent-connection use.
                await asyncio.to_thread(_heartbeat_rebuild_job, store, job_id, now_iso)
        # OFFLOADED end-of-batch progress write + commit (same no-blocking rule).
        await asyncio.to_thread(_commit_rebuild_progress, store, job_id, processed, failed)

    return processed
