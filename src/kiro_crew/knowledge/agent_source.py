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

from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

from .ingestion import DUPLICATE_JOB_STATUS, ImportChunkBudgetError, IngestionPipeline
from .store import AUTO_ADDED_PROP, KnowledgeStore

logger = logging.getLogger(__name__)


def _normalize_newlines(text: str) -> str:
    """Convert ``\\r\\n`` and bare ``\\r`` to ``\\n``, as the file reader does.

    ``IngestionPipeline.ingest_file`` reads the temp file through
    ``readers._decode_text_bytes``, which performs exactly this translation, and
    stamps every chunk with ``sha256`` of the result. The crash-residue marker
    must carry that same hash to name the chunks a kill leaves behind, so the
    submitted text is normalized here BEFORE both the hash is taken and the temp
    file is written. Without it a document containing a CR hashes one way on the
    marker and another on its chunks, and the sweep can never match them.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
_ADD_LOCK = LoopBoundLock()


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
            properties={"sync_status": "active", AUTO_ADDED_PROP: True},
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


def _record_deduped_state(store: KnowledgeStore, source_id: str, slug: str,
                          content_hash: str, name: str,
                          *, source_uri: str) -> None:
    """Terminal write for a document the pre-ingest gate refused.

    Invoked BY the gate as its ``on_duplicate`` finalizer, from inside the gate's
    own ``BEGIN IMMEDIATE`` and on its worker thread, so it takes no lock and no
    transaction of its own. The delete of the previous group, the location claim on
    the holder's items, the terminal job row and this record are one atomic unit.

    That matters most for a FIRST-TIME document: after the gate's commit this row may
    not exist yet, and a ``delete_source_cascade`` landing in that gap reassigns the
    surviving item here with no row to adopt it into, so the record that follows
    reports an empty group while the source owns the item.

    The group is still DERIVED, for the cascade that committed before this
    transaction took the lock. A row that ends up owning items must be ``active``,
    not ``deduped``: ``find_document_by_hash`` only matches ``active``, and a row
    that owns the content while reporting ``deduped`` would let identical text in
    again under a second slug.
    """
    adopted = store.surviving_group_in_txn("agent_item_state", source_id, slug)
    _write_state_row(store, source_id, slug, content_hash, adopted, name,
                     status="active" if adopted else "deduped",
                     source_uri=source_uri)


def mark_ingesting(store: KnowledgeStore, source_id: str, slug: str,
                   content_hash: str, name: str, *, source_uri: str) -> None:
    """Record an intent marker BEFORE the ingest commits items.

    This is the positive evidence :meth:`KnowledgeStore.reclaim_agent_source_residue`
    needs. A hard kill between the item commit and the ownership-row write leaves
    committed items that no ``active`` row names -- indistinguishable, by absence
    alone, from items a bundle import legitimately leaves unowned. The marker
    carries this ingest's ``content_hash`` (the same value every chunk it writes
    is stamped with), so a marker that survives into a drained maintenance window
    tells the sweep exactly which unowned items are crash residue. The finalize
    hop clears the marker via :func:`clear_ingesting`; so does every refusal or
    error exit, so a completed or refused ingest leaves none.

    The marker lives in its own ``agent_ingest_intent`` table, keyed on
    ``(source_id, slug, content_hash)`` -- NOT as a status on the ownership row.
    A re-add that REPLACES a live group holds an ``active`` ownership row for its
    slug while this in-flight intent exists, so the two must be able to coexist;
    and two interrupted attempts at one slug (a crash, then an edited retry that
    also crashes) each keep their own evidence under their own hash rather than
    the second overwriting the first. The write runs in one IMMEDIATE
    transaction so no concurrent ownership write can interleave with it.
    """
    _write_ingest_intent(store, source_id, slug, content_hash)


def _write_ingest_intent(store: KnowledgeStore, source_id: str, slug: str,
                         content_hash: str) -> None:
    """Insert the intent row inside one IMMEDIATE transaction.

    ``BEGIN IMMEDIATE`` takes the write lock up front so the row cannot be
    written against a snapshot a concurrent commit has already moved past; the
    marker is independent of the ownership row, so it needs no read-modify-write
    of that row and simply records that this content began ingesting. On a
    conflict with an existing marker for the same ``(source_id, slug,
    content_hash)`` the EARLIEST ``started_at`` is kept, so a retry's start never
    overwrites an earlier crashed attempt's start.
    """
    store.db.execute("BEGIN IMMEDIATE")
    try:
        # Keep the EARLIEST start on conflict, never the retry's timestamp. A
        # hard-killed ingest leaves an orphan plus this marker; a same-content
        # retry at the same (source_id, slug) hits this row before the sweep
        # reaps the orphan. Overwriting ``started_at`` with the retry's later
        # time would push the older orphan below the finalize's
        # ``created_at >= started_at`` gate, so ``clear_ingesting`` would drop
        # the marker and strand that orphan as a permanent duplicate. ``MIN``
        # retains the first attempt's start so the orphan still counts as its
        # residue and the marker survives until the sweep reaps it.
        store.db.execute(
            "INSERT INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source_id, slug, content_hash) DO UPDATE SET "
            "started_at = MIN(agent_ingest_intent.started_at, excluded.started_at)",
            (source_id, slug, content_hash, datetime.now().isoformat()))
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
    store.db.execute("COMMIT")


def clear_ingesting(store: KnowledgeStore, source_id: str, slug: str,
                    content_hash: str | None = None,
                    *, preserve_if_orphans: bool = False) -> None:
    """Drop this ingest's intent marker on finalize or a non-success exit.

    A completed, refused or failed add names its own ``content_hash``, so the
    marker for exactly that attempt is removed and any other attempt's evidence
    at the same slug is left intact. ``content_hash=None`` drops every marker for
    the slug, which the sweep uses once it has reaped a crashed attempt's items.

    ``preserve_if_orphans`` is set by the FINALIZE callers only. A crashed
    attempt and a later successful retry of the SAME content at the SAME slug
    share one marker row ``(source_id, slug, content_hash)``, so a naive clear on
    the retry's finalize would erase the crashed attempt's evidence while its
    orphaned items are still unowned -- stranding them as permanent duplicates.
    With this flag the marker is kept whenever an UNOWNED active item of that
    hash still exists under the source: the retry's own items are owned (named by
    the row it just wrote), so only genuine leftover orphans hold the marker, and
    the maintenance sweep clears it once it reaps the last one. A non-success
    exit committed no items, so it clears unconditionally (default).
    """
    if content_hash is not None and preserve_if_orphans:
        # Preserve only for a genuine orphan of a crashed attempt -- an unowned
        # same-hash item created at or after that attempt's marker began. A
        # pre-existing or bundle item that merely shares the hash was created
        # before any marker and is NOT residue, so it must not pin the marker
        # open (the sweep's own attempt-time gate would never delete it anyway).
        started_rows = store.db.execute(
            "SELECT started_at FROM agent_ingest_intent "
            "WHERE source_id = ? AND slug = ? AND content_hash = ?",
            (source_id, slug, content_hash)).fetchall()
        earliest_started = min(
            (r["started_at"] or "" for r in started_rows), default=None)
        if earliest_started is not None:
            # The owned-id derivation lives in one place -- the store method the
            # sweep uses too. ``None`` means a row's group is unreadable: the
            # orphan set cannot be proven, so keep the evidence (a stale marker
            # is recoverable, a dropped one is not), exactly as the sweep stands
            # down on the same signal.
            owned_ids = store.agent_owned_item_ids(source_id)
            if owned_ids is None:
                return
            orphan = store.db.execute(
                "SELECT id, created_at FROM items "
                "WHERE source_id = ? AND status = 'active' AND content_hash = ?",
                (source_id, content_hash)).fetchall()
            for row in orphan:
                if (row["id"] not in owned_ids
                        and (row["created_at"] or "") >= earliest_started):
                    # An unowned same-hash item created within this attempt is a
                    # crashed attempt's orphan; keep the marker so the sweep can
                    # still reap it.
                    return
    if content_hash is None:
        store.db.execute(
            "DELETE FROM agent_ingest_intent WHERE source_id = ? AND slug = ?",
            (source_id, slug))
    else:
        store.db.execute(
            "DELETE FROM agent_ingest_intent "
            "WHERE source_id = ? AND slug = ? AND content_hash = ?",
            (source_id, slug, content_hash))
    # No commit here. The connection runs in autocommit (isolation_level=None),
    # so a standalone DELETE persists on its own; and a finalize hook calls this
    # inside the gate's own BEGIN IMMEDIATE, where committing would flush that
    # transaction out from under the gate. The DELETE rides whichever applies.


def set_state(store: KnowledgeStore, source_id: str, slug: str, content_hash: str,
              item_ids: list[str], name: str, status: str = "active",
              *, source_uri: str) -> None:
    """Record one document's hash, item group, display name and status.

    ``status`` is written explicitly on every call: the statement is an
    ``INSERT OR REPLACE``, so omitting it would silently reset a ``deduped``
    marker back to the column default and the document would be re-ingested and
    re-collapsed on every pass. ``source_uri`` (the redacted locator) rides the
    same rule, and is required so no caller can erase it by accident.
    """
    _write_state_row(store, source_id, slug, content_hash, item_ids, name,
                     status=status, source_uri=source_uri)
    store.db.commit()


def _write_state_row(store: KnowledgeStore, source_id: str, slug: str,
                     content_hash: str, item_ids: list[str], name: str,
                     status: str = "active", *, source_uri: str) -> None:
    """The row write alone, with no transaction control, so a caller already
    holding one can include it.

    ``source_uri`` is the document's REDACTED locator (see
    ``add_agent_document``); it is stored so a search hit can cite the document
    itself rather than the aggregate's ``agent://``. It is required and
    keyword-only for the same reason ``status`` is written explicitly on every
    call: the statement is an ``INSERT OR REPLACE``, so a caller allowed to
    omit it would silently erase the locator a previous add recorded. A caller
    with genuinely no locator states ``source_uri=""``, stored as NULL.
    """
    store.db.execute(
        "INSERT OR REPLACE INTO agent_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status, "
        "source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, slug, content_hash, json.dumps(item_ids),
         datetime.now().isoformat(), name, status, source_uri or None),
    )


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
        # does not have.
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
    fetched. The redacted form is persisted on the document's state row so search
    hits cite the document's own locator rather than the aggregate ``agent://``.
    Keep it that way; a caller that needs the bytes at that location reads
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

    # Off the loop: the get-or-create takes the guarded knowledge connection,
    # and a contended take here busy-waits every task for the whole busy
    # timeout (the watchdog heartbeat included).
    source_id, _created = await asyncio.to_thread(ensure_agent_source, store)

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
    # Off the loop: the state read takes the guarded connection.
    prev_hash, old_item_ids = await asyncio.to_thread(get_state, store, source_id, slug)
    # Normalize newlines to match what the file reader does before chunking, so
    # the hash recorded on the intent marker is the SAME value the pipeline
    # stamps on every chunk (see _normalize_newlines). The temp file below is
    # written from this same normalized text, so a CR in the submitted document
    # cannot make the marker hash and the chunk hash diverge.
    text = _normalize_newlines(text)
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    # The shortcut needs a LIVE item group, not just a matching hash. A row left
    # by a refused write records the hash with an empty group, so hash alone would
    # report "nothing to do" for a document the Library does not actually hold --
    # and would keep doing so after the copy that caused the refusal was deleted,
    # leaving the document permanently unsearchable. An empty group therefore
    # falls through and re-attempts the write.
    if prev_hash == content_hash and old_item_ids:
        # The shortcut writes no state row, and an unchanged document takes it
        # on EVERY re-add -- so it must repair the one thing a legacy row can
        # lack: a NULL locator from before the column existed. Without this,
        # "re-add to backfill" never works for a document whose content did not
        # change, and its citations say ``agent://`` forever.
        def _backfill_uri() -> None:
            cur = store.db.execute(
                "UPDATE agent_item_state SET source_uri = ? "
                "WHERE source_id = ? AND slug = ? "
                "AND (source_uri IS NULL OR source_uri = '')",
                (source_uri, source_id, slug))
            if cur.rowcount:
                store.db.commit()
        await asyncio.to_thread(_backfill_uri)
        return {"status": "duplicate", "reason": "unchanged since last add",
                "slug": slug, "source_id": source_id}

    # Runs BEFORE the ingest and before any state write, so a refusal leaves no
    # row behind and deleting the holder later lets this document be added
    # normally. A ``deduped`` marker here would outlive the holder and suppress
    # every future add, losing the content while its origin still exists.
    twin = await asyncio.to_thread(find_document_by_hash, store, source_id, content_hash, slug)
    if twin:
        return {"status": "duplicate",
                "reason": f"identical content is already stored as {twin[1]!r}",
                "slug": slug, "source_id": source_id}

    # Same rule as the folder and artifact paths: a row that owned nothing holds a
    # claim for its previous content, and this document's text has changed.
    # Off the loop: it takes the write lock through the guarded connection.
    await asyncio.to_thread(
        store.release_stale_claim, source_id, prev_hash, content_hash, old_item_ids)

    # Ownership is recorded from inside the ingest's finalize hop rather than
    # after it returns. The items become durable during the ingest, and every
    # await between that and a later write here -- the temp-file cleanup, the
    # job-status read -- is a cancellation point that would leave them owned by
    # nobody. Nothing then names the group: `get_state` reports no previous
    # items, so the next add of this document neither replaces them nor is
    # refused by `find_document_by_hash`, and the content is stored twice.
    recorded_ids: list[str] = []

    def _record_ownership(new_ids: list[str]) -> None:
        recorded_ids[:] = new_ids
        set_state(store, source_id, slug, content_hash, new_ids, title,
                  source_uri=source_uri)
        # The ownership row and the intent marker live in separate tables, so
        # writing the row does not touch the marker; drop THIS attempt's marker
        # explicitly so a finished ingest leaves no crash evidence behind. But a
        # same-content retry at this slug shares the marker row with an earlier
        # crashed attempt, so preserve it while any unowned same-hash orphan the
        # earlier attempt stranded is still waiting for the sweep.
        clear_ingesting(store, source_id, slug, content_hash,
                        preserve_if_orphans=True)

    tmp_path: str | None = None

    def _write_tmp() -> str:
        fd, p = tempfile.mkstemp(suffix=_DEFAULT_EXT, prefix="kc-agent-doc-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            os.unlink(p)
            raise
        return p

    def _finalize_deduped(_text_hash: str) -> None:
        # The duplicate-branch sibling of _record_ownership, inside the gate's
        # hop for the same reason: the gate has committed the delete and the
        # location claim by the time it reports back. This content is accounted
        # for by the winner's row, so clear this attempt's crash marker too.
        _record_deduped_state(
            store, source_id, slug, content_hash, title, source_uri=source_uri)
        clear_ingesting(store, source_id, slug, content_hash,
                        preserve_if_orphans=True)

    # Hold the ingestion gate across the whole span -- the intent-marker write,
    # the ingest (which commits the items), and the marker cleanup -- not only
    # the ``ingest_file`` call. ``mark_ingesting`` writes the marker BEFORE
    # ``ingest_file`` takes the gate on its own, so without this outer hold the
    # maintenance sweep could begin in that gap: it retires every marker for a
    # hash across all slugs once the earlier orphan is reaped, which would drop
    # THIS live add's fresh marker, and a hard kill after the item commit then
    # leaves unmarked residue no later sweep can reclaim. The hold is re-entrant
    # per task, so ``ingest_file``'s own entry inside it neither double-counts
    # nor waits behind a window waiting for this very holder.
    async with pipeline.ingestion_in_flight():
        # Positive crash-residue evidence: record that an ingest of this content
        # has STARTED before any item commit. A hard kill before the finalize
        # hop leaves this marker beside the orphaned items, which is how the
        # maintenance sweep tells crash residue from a legitimately-unowned
        # import. The finalize hop clears the marker; every non-success exit
        # below clears it too.
        await asyncio.to_thread(
            mark_ingesting, store, source_id, slug, content_hash, title,
            source_uri=source_uri)
        try:
            tmp_path = await asyncio.to_thread(_write_tmp)
            job_id = await pipeline.ingest_file(
                tmp_path,
                original_name=f"{title}{_DEFAULT_EXT}",
                source_id=source_id,
                old_item_ids=old_item_ids,
                on_committed=_record_ownership,
                on_duplicate=_finalize_deduped,
            )
        except ImportChunkBudgetError as exc:
            # The cross-file import budget refused this add. Surface WHY to the
            # agent -- the exception's message is the reasoned, ASCII,
            # budget/window/spent text built for exactly this, and the whole
            # point of refusing rather than silently truncating is that the
            # caller can report it. Nothing was written for THIS attempt, so
            # clear the intent marker and report; the agent can retry after the
            # window rolls over or the operator can raise the budget. But an
            # EARLIER crashed attempt at this same (slug, hash) shares this one
            # marker row and may still have unowned orphans waiting for the
            # sweep, so preserve the marker while any such orphan exists -- an
            # unconditional clear here would strand them as permanent duplicates.
            await asyncio.to_thread(clear_ingesting, store, source_id, slug,
                                    content_hash, preserve_if_orphans=True)
            return {"status": "deferred", "reason": str(exc),
                    "slug": slug, "source_id": source_id}
        except BaseException:
            # Any other failure (including cancellation) before the finalize hop
            # ran leaves no ``active`` row for THIS attempt; drop its intent
            # marker so a later unrelated item sharing this hash is never
            # mistaken for this add's residue. Preserve it, though, while an
            # earlier crashed attempt's unowned same-hash orphan still waits for
            # the sweep -- the two attempts share this marker row and dropping it
            # would strand those orphans.
            if not recorded_ids:
                await asyncio.to_thread(clear_ingesting, store, source_id, slug,
                                        content_hash, preserve_if_orphans=True)
            raise
        finally:
            if tmp_path:
                try:
                    await asyncio.to_thread(os.unlink, tmp_path)
                except OSError:
                    pass

        # Off the loop: the status read takes the guarded connection. A bare
        # to_thread suffices -- unlike the artifact path there is no fallback
        # ownership write paired with this read (ownership is persisted inside
        # the ingest's own finalize hop via _record_ownership, and a hop failure
        # propagates rather than being swallowed), so a cancellation that drops
        # the read strands nothing.
        job = (await asyncio.to_thread(pipeline.get_job_status, job_id)) if job_id else None
        status = (job or {}).get("status")
        if status == DUPLICATE_JOB_STATUS:
            # The gate refused the write and recorded the terminal state through
            # the ``on_duplicate`` finalizer above, so there is nothing left to
            # write here.
            return {"status": "duplicate",
                    "reason": "this content is already in the knowledge library",
                    "slug": slug, "source_id": source_id}
        if status != "completed":
            # Nothing was committed for THIS attempt, but preserve the shared
            # marker while an earlier crashed attempt's unowned same-hash orphan
            # is still awaiting the sweep (see the except branches above).
            if not recorded_ids:
                await asyncio.to_thread(clear_ingesting, store, source_id, slug,
                                        content_hash, preserve_if_orphans=True)
            return {"status": "error",
                    "error": f"ingestion did not complete (status={status})"}

    new_ids = list(recorded_ids)
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
