"""The agent's write path into the Knowledge Library.

Documents the agent comes across during normal work -- a design doc it fetched
from a wiki, a spec someone linked, a page it read to answer a question -- land
in ONE aggregate source named "Auto-added" (``source_type="agent"``, uri
``agent://``), exactly as auto-ingested artifacts land in one "Artifacts"
source.

The aggregate matters for control, not tidiness. A source row gives the user
attribution ("where did this come from?"), one-click bulk removal, and -- via
``agent_item_state`` -- per-document state so one document can be replaced or
removed without touching the rest. Loose items with no owning source row can
never be undone.

This replaces the never-built server-side doc-link scanner. Rather than Kiro Crew
regex-matching links in chat and fetching them unattended -- which needs an SSRF
host allowlist and still fetches under no one's supervision -- the agent reads
the document with its own tools, under its own approval, and hands over text.
Kiro Crew fetches nothing, so ``knowledge.doc_ingest_hosts`` does not apply here;
it stays scoped to the server-fetch path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

from .ingestion import DUPLICATE_JOB_STATUS, IngestionPipeline
from .store import KnowledgeStore

logger = logging.getLogger(__name__)

#: Source type for the aggregate agent-added source. Lets retrieval and the UI
#: distinguish agent-added documents from folders, uploads and artifacts.
AGENT_SOURCE_TYPE = "agent"

#: Stable URI of the single aggregate source row.
AGENT_SOURCE_URI = "agent://"

#: Display name shown in the Sources UI.
AGENT_SOURCE_NAME = "Auto-added"

#: Extension a submitted document is written under before ingestion, chosen so
#: the text goes through the same ``FileReader`` path as a folder file. Markdown
#: because that is what prose documents fetched from a wiki or a doc tool are.
_DEFAULT_EXT = ".md"

#: Title length cap, matching the source-name cap the rename endpoint enforces.
_MAX_TITLE_LEN = 200

#: Identity cap for ``source_uri``. Bounded for the same reason as the title: it
#: is agent-supplied and lands in a state-table key and the audit trail.
_MAX_URI_LEN = 1024

#: Serialises adds into the aggregate source. New items are attributed to a
#: document by diffing the source's item ids before and after the ingest, which
#: is only correct while nothing else writes that source concurrently -- two
#: parallel adds would steal each other's chunks. The artifact path relies on the
#: same invariant, enforced by its own lock.
_ADD_LOCK = asyncio.Lock()


def _redact_for_ingest(text: str) -> str:
    """Scan text for secrets and exfiltration URLs before it is persisted.

    The agent hands over content it fetched from somewhere else, so it may carry
    a credential the agent never inspected. Per the security-controls rule, never
    persist secrets: run the same redaction the artifact-ingest path applies
    before the text crosses into the store.
    """
    cleaned, _ = redact_credentials(text)
    cleaned, _ = redact_exfiltration_urls(cleaned)
    return cleaned


def document_slug(identity: str) -> str:
    """Stable per-document key for the aggregate source's item groups.

    Hashes the document's IDENTITY -- its ``source_uri`` -- and never its content,
    so re-adding an edited document replaces its item group instead of accumulating
    copies, the role an artifact's slug plays for the Artifacts source.

    A title alone is not an identity: two unrelated documents are both routinely
    called "README", and a matching key means "same document, replace it". The
    ``source_uri`` separates them because it names where each document came from,
    which is the part that differs.
    """
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def ensure_agent_source(store: KnowledgeStore) -> tuple[str, bool]:
    """Get-or-create the aggregate source row.

    Returns ``(source_id, created)``.

    Deleting this source deliberately does NOT tombstone it, unlike the
    per-path auto-sources. Those are keyed to one folder, so re-registering a
    folder the user removed would override an explicit choice about that folder.
    This row is not a place -- it is the container for a feature that already has
    its own discoverable off switch, ``knowledge.auto_add_documents``. Making the
    delete a second, hidden, permanent off switch gives one intent two controls,
    and the one with no UI wins: a user who deletes the source has no way back,
    while the toggle they can see still reads on. So deleting it means "clear
    what is in here", and the toggle means "stop adding". This matches the
    sibling Artifacts source.
    """
    existing = store.get_source_by_uri(AGENT_SOURCE_URI)
    if existing:
        return existing["id"], False
    try:
        source_id = store.add_source(
            name=AGENT_SOURCE_NAME,
            source_type=AGENT_SOURCE_TYPE,
            uri=AGENT_SOURCE_URI,
            properties={"sync_status": "active", "auto_added": True},
        )
        return source_id, True
    except Exception:
        # Lost a race on the UNIQUE uri -- re-read and treat as pre-existing.
        existing = store.get_source_by_uri(AGENT_SOURCE_URI)
        if existing:
            return existing["id"], False
        raise


def get_state(store: KnowledgeStore, source_id: str, slug: str) -> tuple[str | None, list[str]]:
    """``(content_hash, item_ids)`` for one document's group, or ``(None, [])``."""
    row = store.db.execute(
        "SELECT content_hash, item_ids FROM agent_item_state "
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


def set_state(store: KnowledgeStore, source_id: str, slug: str, content_hash: str,
              item_ids: list[str], name: str, status: str = "active") -> None:
    """Record one document's hash, item group, display name and status.

    ``status`` is written explicitly on every call: the statement is an
    ``INSERT OR REPLACE``, so omitting it would silently reset a ``deduped``
    marker back to the column default and the document would be re-ingested and
    re-collapsed on every pass.
    """
    store.db.execute(
        "INSERT OR REPLACE INTO agent_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, slug, content_hash, json.dumps(item_ids),
         datetime.now().isoformat(), name, status),
    )
    store.db.commit()


def find_document_by_hash(store: KnowledgeStore, source_id: str, content_hash: str,
                          exclude_slug: str) -> tuple[str, str] | None:
    """``(slug, name)`` of another document in the aggregate holding this content.

    The pipeline's duplicate gate excludes the whole incoming ``source_id``, which
    is right for a source holding ONE document but blind inside an aggregate: two
    agent documents with different uris and identical text share the ``agent://``
    source, so a source-level exclusion hides the first from the gate when the
    second arrives. This check covers that gap.

    Only ``active`` groups count. A ``deduped`` row owns no items, so refusing
    against it would reject content the Library does not actually hold.
    """
    row = store.db.execute(
        "SELECT slug, name FROM agent_item_state "
        "WHERE source_id = ? AND content_hash = ? AND slug != ? "
        "AND status = 'active' LIMIT 1",
        (source_id, content_hash, exclude_slug),
    ).fetchone()
    return (row["slug"], row["name"] or "") if row else None


def remove_document(store: KnowledgeStore, source_id: str, slug: str) -> int:
    """Drop one document's items and its state row. Returns items removed."""
    prev_hash, item_ids = get_state(store, source_id, slug)
    if item_ids:
        store.delete_items_batch(item_ids, owner_source_id=source_id)
    else:
        # A document that LOST a dedup owns nothing but still holds the winner's
        # items. Removing it has to release that claim, exactly as the artifact
        # path does, or a later winner deletion resurfaces content this aggregate
        # no longer has.
        store.detach_source_location_by_hash(source_id, prev_hash or "")
    store.db.execute(
        "DELETE FROM agent_item_state WHERE source_id = ? AND slug = ?",
        (source_id, slug))
    store.db.commit()
    return len(item_ids)


async def add_agent_document(
    pipeline: IngestionPipeline,
    *,
    title: str,
    content: str,
    reason: str = "",
    source_uri: str = "",
) -> dict:
    """Add one document to the aggregate "Auto-added" source.

    Takes the document TEXT and opens nothing. The caller reads the document
    through its own file tools, under its own approval and audit, and hands over
    text; documents that arrive fetched (a wiki, a doc tool, a page) are text to
    begin with, and documents living in the user's project are covered by
    project-docs registration, which scans through the guarded folder path.

    ``source_uri`` is where the document came from -- a path, URL or any stable
    handle -- and is a stored identity label: redacted, capped, hashed into the
    document's key (see ``document_slug``), and never opened, resolved, stat-ed or
    fetched. Keep it that way; a caller that needs the bytes at that location reads
    them itself and passes ``content``.

    It is REQUIRED, because the title alone does not identify a document.

    Routes through ``IngestionPipeline.ingest_file`` rather than a parallel write
    path, so the document gets the same chunker, extractor and embedder every
    other source gets -- and the same pre-ingest duplicate gate.

    Returns a result dict with ``status`` in ``added`` / ``duplicate`` /
    ``error``.
    """
    async with _ADD_LOCK:
        return await _add_agent_document(
            pipeline, title=title, content=content, reason=reason,
            source_uri=source_uri)


async def _add_agent_document(
    pipeline: IngestionPipeline,
    *,
    title: str,
    content: str,
    reason: str,
    source_uri: str = "",
) -> dict:
    store = pipeline.store
    title = (title or "").strip()[:_MAX_TITLE_LEN]
    if not title:
        return {"status": "error", "error": "title is required"}
    text = content or ""
    if not text.strip():
        return {"status": "error", "error": "content is required"}

    source_id, _created = ensure_agent_source(store)

    text = _redact_for_ingest(text)
    title = _redact_for_ingest(title)

    # The identity is hashed from the RAW uri; only the redacted form is stored,
    # returned or audited. Redaction is lossy -- two uris differing only in a
    # same-length credential-shaped segment both reduce to
    # "...?key=[REDACTED: credential]" -- and identity must separate documents that
    # redaction merges. A truncated digest of the raw value discloses nothing: it is
    # one-way and never stored alongside its preimage.
    raw_uri = (source_uri or "").strip()[:_MAX_URI_LEN]
    if not raw_uri:
        return {"status": "error",
                "error": "source_uri is required: it identifies the document, "
                         "so without it two documents sharing a title would "
                         "overwrite each other"}
    source_uri = _redact_for_ingest(raw_uri)
    slug = document_slug(raw_uri)
    prev_hash, old_item_ids = get_state(store, source_id, slug)
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    # The shortcut needs a LIVE item group, not just a matching hash. A row left
    # by a refused write records the hash with an empty group, so hash alone would
    # report "nothing to do" for a document the Library does not actually hold --
    # and would keep doing so after the copy that caused the refusal was deleted,
    # leaving the document permanently unsearchable. An empty group therefore
    # falls through and re-attempts the write.
    if prev_hash == content_hash and old_item_ids:
        return {"status": "duplicate", "reason": "unchanged since last add",
                "slug": slug, "source_id": source_id}

    # Runs BEFORE the ingest and before any state write, so a refusal leaves no
    # row behind and deleting the holder later lets this document be added
    # normally. A ``deduped`` marker here would outlive the holder and suppress
    # every future add, losing the content while its origin still exists.
    twin = find_document_by_hash(store, source_id, content_hash, slug)
    if twin:
        return {"status": "duplicate",
                "reason": f"identical content is already stored as {twin[1]!r}",
                "slug": slug, "source_id": source_id}

    # Same rule as the folder and artifact paths: a row that owned nothing holds a
    # claim for its previous content, and this document's text has changed.
    store.release_stale_claim(source_id, prev_hash, content_hash, old_item_ids)

    before_ids = {r["id"] for r in store.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}

    tmp_path: str | None = None
    try:
        def _write_tmp() -> str:
            fd, p = tempfile.mkstemp(suffix=_DEFAULT_EXT, prefix="kc-agent-doc-")
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
            original_name=f"{title}{_DEFAULT_EXT}",
            source_id=source_id,
            old_item_ids=old_item_ids,
        )
    finally:
        if tmp_path:
            try:
                await asyncio.to_thread(os.unlink, tmp_path)
            except OSError:
                pass

    job = pipeline.get_job_status(job_id) if job_id else None
    status = (job or {}).get("status")
    if status == DUPLICATE_JOB_STATUS:
        # The gate refused the write and deleted this document's previous items,
        # so record that rather than leaving the state row pointing at items that
        # no longer exist -- otherwise every later add would re-attempt from a
        # dead group. The hash IS stored, so re-adding the same text is a cheap
        # no-op instead of a repeated gate round-trip.
        set_state(store, source_id, slug, content_hash, [], title, status="deduped")
        return {"status": "duplicate",
                "reason": "this content is already in the knowledge library",
                "slug": slug, "source_id": source_id}
    if status != "completed":
        return {"status": "error",
                "error": f"ingestion did not complete (status={status})"}

    after_ids = {r["id"] for r in store.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}
    new_ids = list(after_ids - before_ids)
    set_state(store, source_id, slug, content_hash, new_ids, title)
    sel().log_tool_invocation(
        session_key="gateway", agent="knowledge-agent-source",
        tool_name="knowledge.agent_document.add", outcome="completed",
        # The audit trail is a persisted, readable surface, so agent-authored
        # strings are redacted here too -- not only on the copy that reaches the
        # store. ``reason`` never passes through the content redaction above.
        resources=str({"title": _redact_for_ingest(title), "slug": slug,
                       "source_uri": source_uri,
                       "items": len(new_ids),
                       "reason": _redact_for_ingest(reason)[:200]}),
    )
    # A digest cannot carry agent-submitted text into the log; the SEL event above
    # keeps the redacted title, so slug -> title stays recoverable for support.
    logger.info("Agent added knowledge document %s (%d chunk(s))", slug, len(new_ids))
    return {"status": "added", "slug": slug, "source_id": source_id,
            "items": len(new_ids), "title": title, "source_uri": source_uri}
