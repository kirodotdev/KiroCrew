"""Auto-ingest local artifacts into the Knowledge Library.

Content-bearing local artifacts (markdown/text documents the user saves and
iterates) are mirrored into the Knowledge Library so they become searchable and
stay in sync as the artifact changes, and are removed when the artifact is
deleted. Off by default, opt in with ``knowledge.auto_ingest_artifacts``.

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

* **Reconcile on every start, not a one-shot first-enable backfill.** The
  feature is opt-in, and while it is off the listener is not registered at all,
  so artifacts created / edited / deleted during that window are invisible to
  the Library. Keying the catch-up pass off *creation of the aggregate source
  row* cannot repair that: on an install that ever had the feature on, the row
  already exists, so a later opt-in returns ``created=False`` and the catch-up
  never runs -- the gap becomes permanent and silent. So the pass runs on EVERY
  :meth:`ArtifactKnowledgeSync.start` and is driven by state comparison instead:
  ingest what the store has and ``artifact_item_state`` lacks or disagrees with,
  remove state for artifacts that no longer exist. Converged is the common case
  and costs no extraction calls, because :func:`ingest_artifact` already skips
  unchanged content.

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
    that actually inserts the row. It is reported for logging only -- it must
    NOT gate the catch-up pass: the row survives the feature being switched off,
    so a later opt-in would see ``created=False`` and never repair the drift
    accumulated while the listener was unregistered. :func:`reconcile_artifacts`
    compares state instead, and runs unconditionally.
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


def _record_deduped_state(
    kstore: KnowledgeStore,
    source_id: str,
    slug: str,
    content_hash: str,
    name: str,
    kind: str | None,
) -> None:
    """Terminal write for an artifact the pre-ingest gate refused.

    Invoked BY the gate as its ``on_duplicate`` finalizer, from inside the gate's
    own ``BEGIN IMMEDIATE`` and on its worker thread, so it takes no lock and no
    transaction of its own. The delete of the previous group, the location claim on
    the holder's items, the terminal job row and this record are one atomic unit.

    That ordering is load-bearing for a FIRST-TIME document in particular: after the
    gate's commit this row may not exist yet, and a ``delete_source_cascade``
    landing in that gap reassigns the surviving item here and then has no row to
    adopt it into -- ``_adopt_reassigned_item`` matches on ``(source_id,
    content_hash)``, finds nothing, and returns without logging. Writing inside the
    transaction means the cascade sees either no claim at all or a claim WITH the
    row that names it.

    The group is still DERIVED rather than assumed empty, for the cascade that
    committed before this transaction took the lock. A row that ends up owning items
    is ``active``, not ``deduped``: ``find_document_by_hash`` only matches
    ``active``, and a row owning content while reporting ``deduped`` would let the
    same text in again under a second slug.
    """
    adopted = kstore.surviving_group_in_txn("artifact_item_state", source_id, slug)
    _write_state_row(
        kstore, source_id, slug, content_hash, adopted, name,
        status="active" if adopted else "deduped", kind=kind)


def _set_state(
    kstore: KnowledgeStore,
    source_id: str,
    slug: str,
    content_hash: str,
    item_ids: list[str],
    name: str,
    status: str = "active",
    kind: str | None = None,
) -> None:
    """Record the content hash + item-id group + display name for one artifact,
    keyed by slug in the dedicated ``artifact_item_state`` table. ``name`` is the
    (redacted) artifact name, used as the per-artifact group label in the
    Sources UI.

    ``kind`` is the artifact kind AS INGESTED. Reconcile compares it against the
    artifact's current kind to tell a kind change that happened while sync was
    off (stale chunks) from a kind the user merely excluded from
    ``auto_ingest_artifact_kinds`` (still live) -- two situations that are
    otherwise indistinguishable at start-up.

    ``status`` is written explicitly on every call because the statement is an
    ``INSERT OR REPLACE``: omitting it would reset a ``deduped`` marker back to
    the column default, and the artifact would be re-ingested and re-collapsed on
    every event."""
    _write_state_row(kstore, source_id, slug, content_hash, item_ids, name,
                     status=status, kind=kind)
    kstore.db.commit()


def _write_state_row(
    kstore: KnowledgeStore,
    source_id: str,
    slug: str,
    content_hash: str,
    item_ids: list[str],
    name: str,
    status: str = "active",
    kind: str | None = None,
) -> None:
    """The row write alone, with no transaction control, so a caller already
    holding one can include it."""
    now = datetime.now().isoformat()
    kstore.db.execute(
        "INSERT OR REPLACE INTO artifact_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            slug,
            content_hash,
            json.dumps(item_ids),
            now,
            name,
            status,
            kind,
        ),
    )


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


def _invalidate_content_hash(
    kstore: KnowledgeStore, source_id: str, slug: str
) -> bool:
    """Clear only the recorded content hash, keeping the item group intact.

    Used when an artifact must be re-ingested even though its bytes are
    unchanged -- a kind change, where the group is valid content read by the
    WRONG reader. Deleting the group first would leave the artifact unsearchable
    if the re-ingest then failed; clearing the hash instead defeats
    :func:`ingest_artifact`'s unchanged-content short-circuit and lets
    ``ingest_file`` do its normal atomic replacement, which keeps the old group
    on a failed extraction. Returns ``True`` if a row was updated.

    Only rows that OWN items are touched. A row with an empty group is a deduped
    marker: it holds a dedup claim keyed to the hash recorded here, and
    ``release_stale_claim`` needs that hash to name the document it is detaching
    from. Clearing it would strand the claim, and deleting the winning holder
    would then hand this artifact the superseded text. Such a row also fails the
    short-circuit's ``and old_item_ids`` leg already, so there is nothing to
    defeat.
    """
    cur = kstore.db.execute(
        "UPDATE artifact_item_state SET content_hash = NULL "
        "WHERE source_id = ? AND slug = ? "
        "AND item_ids IS NOT NULL AND TRIM(item_ids) NOT IN ('[]', '')",
        (source_id, slug),
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
    if art.source_missing:
        # File-backed artifact whose live source could not be read: `get` fell
        # back to the stored snapshot and flagged it. That snapshot is not
        # evidence about the live file in EITHER direction -- blank does not mean
        # the artifact was emptied, and non-blank does not mean it is current, so
        # ingesting it can overwrite a newer index with older text. Neither the
        # drop below nor an ingest is safe: keep what is already indexed until
        # the source is readable again. Same rule as
        # `_artifact_is_really_gone` -- only provable state acts.
        logger.warning(
            "artifact KB: %s has no readable source; keeping its indexed group "
            "as-is",
            slug,
        )
        return None
    if not text.strip():
        # An artifact emptied after it was ingested must not keep answering
        # searches from its previous text. There is nothing to index, so drop the
        # group rather than returning early and leaving obsolete chunks live.
        # Offloaded: remove_artifact -> delete_items_batch -> store._load_graph
        # is a graph rebuild inside a SQLite transaction, never loop-safe work.
        prev_hash, old_item_ids = _get_state(kstore, source_id, slug)
        if old_item_ids or prev_hash:
            await asyncio.to_thread(remove_artifact, kstore, source_id, slug)
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
            # Recorded inside the gate's own hop: by the time it reports a refusal
            # it has already committed the delete and the location claim, and a
            # cancellation between here and a post-hoc write would leave both
            # durable with nothing naming them.
            on_duplicate=lambda: _record_deduped_state(
                kstore, source_id, slug, content_hash, title, art.kind),
        )
    finally:
        if tmp_path:
            try:
                await asyncio.to_thread(os.unlink, tmp_path)
            except OSError:
                pass

    status = (pipeline.get_job_status(job_id) or {}).get("status") if job_id else None
    if status == DUPLICATE_JOB_STATUS:
        # The gate refused the write and recorded the terminal state through the
        # ``on_duplicate`` finalizer above, so there is nothing left to write here.
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
    _set_state(kstore, source_id, slug, content_hash, new_ids, title, kind=art.kind)
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


#: Artifacts the reconcile pass may (re-)ingest in ONE run. Each ingest costs
#: LLM extraction calls on a pool of billed sessions, so opting in against a
#: store that drifted while the feature was off must not arrive as one unbounded
#: burst. The pass runs on EVERY start and ``art_store.list`` is newest-first, so
#: a backlog drains over successive boots with the most recent artifacts landing
#: first -- the same newest-first-plus-budget shape the folder watcher applies per
#: sweep. Unchanged artifacts cost nothing and do NOT consume budget, so a
#: converged store never defers.
RECONCILE_INGEST_BUDGET = 50


#: Sentinel for "this slug has no state row at all", distinct from a row whose
#: ``kind`` is NULL (tracked, but ingested before the column existed).
_UNTRACKED = object()


def _known_kinds(kstore: KnowledgeStore, source_id: str) -> dict[str, str | None]:
    """Map slug -> kind AS INGESTED for every item group this source tracks.

    The kind is NULL for rows written before the ``kind`` column existed; see
    :func:`reconcile_artifacts` for why that is treated as "cannot tell".
    """
    rows = kstore.db.execute(
        "SELECT slug, kind FROM artifact_item_state WHERE source_id = ?", (source_id,)
    ).fetchall()
    return {row["slug"]: row["kind"] for row in rows}


async def _artifact_is_really_gone(art_store: ArtifactStore, slug: str) -> bool:
    """True only when the artifact is provably absent from the store.

    ``ArtifactStore.list`` silently omits an artifact whose ``meta.json`` cannot
    be read, so absence from the listing is NOT proof of deletion. Reaping on the
    listing alone would delete a live artifact's indexed content because one file
    was briefly unreadable. Confirm per slug, and treat any error other than
    "not found" as "keep the state" -- a stale group is recoverable on the next
    start, deleted items are not.

    ``ArtifactNotFoundError`` is itself raised for a missing ``meta.json``, which
    a partially-restored artifact directory can hit while its content is still
    there. So require the directory to be gone too: deletion removes the whole
    directory, and demanding both means a mid-restore window costs a stale group
    (recoverable) rather than deleted items (not).
    """
    try:
        await asyncio.to_thread(art_store.get, slug)
    except ArtifactNotFoundError:
        try:
            adir_exists = await asyncio.to_thread((art_store.root / slug).exists)
        except OSError:
            logger.warning(
                "artifact KB reconcile: cannot stat %s's directory; keeping its "
                "item group",
                slug,
            )
            return False
        if adir_exists:
            logger.warning(
                "artifact KB reconcile: %s has no readable metadata but its "
                "directory is still present; keeping its item group",
                slug,
            )
            return False
        return True
    except Exception:
        logger.warning(
            "artifact KB reconcile: cannot read %s; keeping its item group", slug
        )
        return False
    return False


async def reconcile_artifacts(
    pipeline: IngestionPipeline,
    art_store: ArtifactStore,
    source_id: str,
    kinds: set[str],
    *,
    budget: int = RECONCILE_INGEST_BUDGET,
) -> tuple[int, int, int]:
    """Bring the aggregate source back in line with the artifact store.

    Runs on every :meth:`ArtifactKnowledgeSync.start`, so it repairs drift from
    any cause -- the feature having been off, a crash mid-ingest, a restore from
    backup -- not just a first enable. Returns ``(ingested, removed, deferred)``.

    Ordering matters: removals run FIRST and are unbudgeted. They cost no
    extraction calls, and leaving them behind would keep text searchable that has
    no artifact behind it. They also run even when ``kinds`` is empty: an empty
    allowlist means "ingest nothing", not "let deleted content stay searchable".
    Two further off-window drifts are repaired unbudgeted for the same reason:
    a tracked artifact whose kind became structurally unsupported loses its
    stale chunks, and every tracked artifact's group label is refreshed to its
    current (redacted) name.
    """
    # art_store.list walks the artifact directory and reads every metadata file;
    # the state read is SQLite. Offload both so a large store cannot freeze chat
    # turns or the liveness heartbeat during the pass.
    artifacts = await asyncio.to_thread(art_store.list)
    known = await asyncio.to_thread(_known_kinds, pipeline.store, source_id)

    removed = 0

    # Gone-from-the-store is judged against EVERY artifact, not just eligible
    # kinds: a kinds-config change makes an artifact ineligible, not absent, and
    # deleting its items on that basis would silently drop content the user did
    # not delete. Stale-but-present is harmless; over-deleting is not.
    live = {art.slug for art in artifacts}
    for slug in sorted(known.keys() - live):
        if not await _artifact_is_really_gone(art_store, slug):
            continue
        try:
            # remove_artifact -> delete_items_batch -> store._load_graph, a full
            # in-memory graph rebuild inside a SQLite transaction. Never on the
            # loop: once per slug deleted during a long off-window is exactly the
            # wedge `no-blocking-call-on-event-loop` guards, and the sibling live
            # delete in `_handle` offloads this same call for this same reason.
            await asyncio.to_thread(remove_artifact, pipeline.store, source_id, slug)
            removed += 1
        except Exception:
            logger.exception("artifact KB reconcile: failed to remove %s", slug)

    # Kind drift: the artifact's kind CHANGED after it was ingested (markdown
    # ingested, later switched to svg or to an excluded kind while sync was
    # off). Its indexed group was produced by the previous kind's reader, so the
    # old prose would keep answering searches -- the same staleness the live
    # ``_handle`` path removes on a kind-change upsert.
    #
    # This is decided from the kind RECORDED AT INGEST, not from ``kinds``:
    # "the artifact changed" and "the user narrowed the allowlist" are
    # indistinguishable by the current kind alone, and reaping on the latter
    # would delete content the user never touched. A legacy row predating the
    # ``kind`` column carries None. That is unknowable, so it is resolved by
    # which repair is safe: an ELIGIBLE artifact is re-ingested once (below, in
    # budget, no deletion), which both repairs any drift and backfills the
    # column so it never repeats; an INELIGIBLE one is left alone, because the
    # only repair there is deletion and drift was never proven.
    drifted: set[str] = set()
    for art in artifacts:
        prev_kind = known.get(art.slug, _UNTRACKED)
        if prev_kind is _UNTRACKED or prev_kind == art.kind:
            continue
        if art.kind in kinds:
            # Nothing re-reads this artifact until the budgeted loop below, so
            # removing it HERE would be a delete-now / restore-later split: a
            # backlog past the budget would leave the remainder missing from
            # search entirely until a further start. Replace it in-budget
            # instead, where the removal and the re-ingest are one step.
            drifted.add(art.slug)
            continue
        if prev_kind is None:
            # Legacy row, ineligible kind: the only repair available here is
            # deletion, and nothing proves this artifact ever drifted. A stale
            # group is recoverable; deleted items are not.
            continue
        # Ineligible new kind: nothing will re-ingest it under this config, so
        # removal IS the whole repair and is unbudgeted -- it costs no
        # extraction calls, exactly like the deleted-artifact reap above.
        try:
            await asyncio.to_thread(
                remove_artifact, pipeline.store, source_id, art.slug
            )
            removed += 1
            logger.info(
                "artifact KB reconcile: %s changed kind %s -> %s (not ingestible); "
                "removed its stale chunks",
                art.slug,
                prev_kind,
                art.kind,
            )
        except Exception:
            logger.exception("artifact KB reconcile: failed to remove %s", art.slug)

    # Emptied while off: an artifact whose body is now blank must not keep
    # answering searches from its previous text. Dropping it costs no extraction
    # calls, so it belongs here rather than behind the budget -- an emptied
    # artifact sitting past a backlog of newer changes would otherwise stay
    # searchable for as many restarts as it takes the backlog to drain. Only
    # TRACKED artifacts are read: an untracked one has no group to drop. The
    # extra `get` is a disk read (the same one `ingest_artifact` would do later),
    # never an extraction.
    emptied: set[str] = set()
    for art in artifacts:
        if art.slug not in known:
            continue
        try:
            current = await asyncio.to_thread(art_store.get, art.slug)
        except ArtifactNotFoundError:
            continue
        except Exception:
            logger.warning(
                "artifact KB reconcile: cannot read %s; leaving its group alone",
                art.slug,
            )
            continue
        if current.source_missing:
            # Snapshot fallback: blank proves nothing about the live file, the
            # same reason `ingest_artifact` stands down on this flag.
            continue
        if (current.content or "").strip():
            continue
        try:
            await asyncio.to_thread(
                remove_artifact, pipeline.store, source_id, art.slug
            )
            removed += 1
            emptied.add(art.slug)
            logger.info(
                "artifact KB reconcile: %s was emptied; removed its stale chunks",
                art.slug,
            )
        except Exception:
            logger.exception("artifact KB reconcile: failed to remove %s", art.slug)

    # Name drift: a rename during the off-window never reached the Library, so
    # the Sources-UI group label still shows the old name. Metadata-only and
    # unbudgeted, mirroring the live rename path -- content hashes are
    # untouched, so an unchanged artifact still costs nothing below.
    for art in artifacts:
        if art.slug not in known or art.slug in emptied:
            continue
        try:
            await asyncio.to_thread(
                refresh_artifact_name,
                pipeline.store,
                source_id,
                art.slug,
                _redact_for_ingest(art.name),
            )
        except Exception:
            logger.exception(
                "artifact KB reconcile: failed to refresh name for %s", art.slug
            )

    if not kinds:
        return 0, removed, 0

    ingested = 0
    deferred = 0
    for art in artifacts:  # newest-first, per ArtifactStore.list
        if art.kind not in kinds or art.slug in emptied:
            # An artifact dropped above has nothing left to ingest: its body is
            # blank, so re-reading it would only repeat the same decision.
            continue
        if ingested >= budget:
            deferred += 1
            continue
        if art.slug in drifted:
            # Kind changed between two eligible kinds: the recorded content hash
            # still matches, so `ingest_artifact` would short-circuit and keep
            # the previous reader's chunks. Clear ONLY the hash -- the group
            # stays live, and `ingest_file` replaces it atomically (a failed
            # extraction rolls back and keeps the old items), so a valid index is
            # never traded for an extraction that might not land. A deduped row
            # (empty group) is left untouched: its hash is what
            # `release_stale_claim` needs to detach the claim it holds, and it
            # already falls through the short-circuit.
            try:
                await asyncio.to_thread(
                    _invalidate_content_hash, pipeline.store, source_id, art.slug
                )
            except Exception:
                logger.exception(
                    "artifact KB reconcile: failed to invalidate hash for %s",
                    art.slug,
                )
                continue
        try:
            # Returns None for unchanged content (and for ineligible / sensitive
            # / empty), so a converged artifact spends no budget and no tokens.
            job_id = await ingest_artifact(
                pipeline, art_store, art.slug, source_id, kinds
            )
            if not job_id:
                continue
            # A write the pre-ingest hash gate refused as a duplicate returns a
            # job id but performs no extraction. It also re-attempts on every
            # start, because the deduped state row holds an empty group and the
            # per-slug short-circuit requires one -- so counting it would let a
            # budget's worth of duplicates starve every other artifact forever.
            status = (pipeline.get_job_status(job_id) or {}).get("status")
            if status == DUPLICATE_JOB_STATUS:
                continue
            ingested += 1
        except Exception:
            logger.exception("artifact KB reconcile: failed to ingest %s", art.slug)
    return ingested, removed, deferred


class ArtifactKnowledgeSync:
    """Keeps the aggregate Artifacts source in sync with the artifact store.

    Registered as the store's change-listener: ``on_change`` (sync, called from
    the store write path on any thread) schedules the async ingest/remove on the
    gateway loop. A single lock serializes all work so the start-time reconcile
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
        self._reconcile_task: asyncio.Task | None = None

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

    # ── startup / reconcile ───────────────────────────────────────────────

    async def start(self) -> None:
        """Ensure the aggregate source exists, then kick off the reconcile pass
        in the background (so gateway startup isn't blocked).

        The pass runs on EVERY start, not only when the source row is created:
        the row outlives the feature being switched off, so gating on creation
        leaves an opt-in unable to repair the drift from that window. See the
        module docstring.
        """
        source_id, created = ensure_artifact_source(self.kstore)
        logger.info(
            "artifact KB sync started: source=%s created=%s kinds=%s",
            source_id,
            created,
            sorted(self.kinds),
        )
        self._reconcile_task = asyncio.create_task(self._run_reconcile(source_id))

    async def _run_reconcile(self, source_id: str) -> None:
        async with self._lock:
            try:
                ingested, removed, deferred = await reconcile_artifacts(
                    self.pipeline, self.art_store, source_id, self.kinds
                )
            except Exception:
                logger.exception("artifact KB reconcile failed")
                return
        if not (ingested or removed or deferred):
            # The steady state. Logged at debug so an already-converged store
            # does not write a line on every boot.
            logger.debug("artifact KB reconcile: already in sync")
            return
        logger.info(
            "artifact KB reconcile: %d ingested, %d removed, %d deferred to a later start",
            ingested,
            removed,
            deferred,
        )
        if deferred:
            logger.info(
                "artifact KB reconcile: %d artifact(s) exceed the per-run budget of "
                "%d and will be picked up on the next start, newest first",
                deferred,
                RECONCILE_INGEST_BUDGET,
            )
        sel().log_tool_invocation(
            session_key="gateway",
            agent="knowledge-artifacts",
            tool_name="knowledge.artifact_ingest.reconcile",
            outcome="completed",
            resources=str(
                {"ingested": ingested, "removed": removed, "deferred": deferred}
            ),
        )
