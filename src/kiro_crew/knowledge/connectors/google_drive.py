"""Google Drive knowledge connector -- per-document rows over the real Drive v3 API.

A structured :class:`~kiro_crew.knowledge.connectors.base.BaseConnector`: it does
NOT collapse a Drive into one text blob. Each Drive file becomes its OWN
:class:`~kiro_crew.knowledge.rows.SourceRow` -- one row, carrying that file's
``fileId`` / ``driveId`` / ``mimeType`` / ``modifiedTime`` / ``version`` and its
own content fingerprint, its own per-user ACL grant, and its own
:class:`~kiro_crew.knowledge.acl.ProviderResourceRef` -- so the shared per-row
ingest pipeline binds each file to its own item group + grant, and two files with
different sharing never share a chunk or a grant.

It drives two real Drive v3 sequences:

* **Snapshot (first sync / after a resync boundary):** ``files.list`` over My
  Drive AND every Shared Drive (both drive flags set), then per file fetch its
  extractable content (native -> ``export``; binary -> ``alt=media``; shortcut ->
  resolve ``targetId`` then fetch the target). Returns ``snapshot=True`` so the
  pipeline may delete rows the listing no longer contains, and a fresh change
  checkpoint (``changes.getStartPageToken``) as the resume token.
* **Incremental (subsequent syncs):** ``changes.list`` from the saved
  ``startPageToken``; each changed file is refetched into a row, each removed file
  yields a deletion. Returns ``snapshot=False`` (absent rows are untouched) and
  the batch's ``newStartPageToken`` as the advanced checkpoint. A STALE saved
  token is not a failure: the client re-fetches the start token and this connector
  falls back to a snapshot resync.

Real network I/O never lives in this connector: every outbound Drive call is a
W01 operation run through
:class:`~kiro_crew.connections.vendors.google_drive.operations.DriveOperations`,
whose :class:`~kiro_crew.connections.vendors.google_drive.operations.OperationRunner`
is INJECTED (the host binds W01's ``execute`` / ``PageWalk`` to the source's
trusted handle + credential custody). The connector holds no token, opens no
session, and sends no HTTP. With no Google account the whole connector --
listing, export/media selection, shortcut resolution, incremental changes,
resync, per-row grant derivation -- is provable against a scripted runner
wrapping the real W01 execute over a fake transport, which is the
``code_complete`` acceptance ceiling here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Mapping, Optional, Tuple

from kiro_crew.connections.vendors.google_drive import drive_api, pagination
from kiro_crew.connections.vendors.google_drive.operations import (
    DriveOperationError,
    DriveOperations,
)
from kiro_crew.knowledge.acl import ProviderResourceRef
from kiro_crew.knowledge.rows import SourceRow

from .base import BaseConnector

logger = logging.getLogger(__name__)

SOURCE_TYPE = "google_drive"

# The max bytes of exported/downloaded content we turn into a row's text. A
# pathological multi-hundred-MB export must not be pulled whole into memory; a
# file over this keeps its metadata row but its text is truncated at the
# boundary (the ingest chunker then works on the head), never silently dropped.
_MAX_CONTENT_BYTES = 10 * 1024 * 1024


class GoogleDriveConnector(BaseConnector):
    """Structured Drive connector: one SourceRow per Drive file.

    ``operations_factory`` builds a
    :class:`~kiro_crew.connections.vendors.google_drive.operations.DriveOperations`
    from a source dict. DriveOperations runs every outbound Drive call through
    W01's executor (an injected OperationRunner the host binds to the source's
    trusted handle + credential custody); this connector NEVER holds a token,
    opens a session, or sends HTTP. The factory is injected so a test drives the
    connector against a scripted runner (wrapping the real W01 execute over a fake
    transport) with no account, and production wires the W01-backed factory. A
    connector with no factory cannot fetch and says so at validate time.
    """

    def __init__(self, operations_factory=None) -> None:
        self._operations_factory = operations_factory

    # ---- BaseConnector surface -------------------------------------------

    def source_type(self) -> str:
        return SOURCE_TYPE

    def supports_rows(self) -> bool:
        return True

    def validate_config(self, config: dict) -> tuple[bool, str]:
        """A Drive source needs an account (driveId or 'my-drive') and a tenant.

        ``account`` scopes the ProviderResourceRef + the query-time binding; a
        blank ``tenant`` would let the row escape tenant scoping (SourceRow
        rejects it), so it is required here with a clear message rather than
        failing deep in fetch_rows. The credential wiring itself is validated by
        the injected client factory at fetch time.
        """
        account = (config.get("account") or config.get("drive_id") or "").strip()
        if not account:
            return False, (
                "Google Drive source requires an 'account' (a Shared Drive driveId, "
                "or 'my-drive' for the user's My Drive)"
            )
        tenant = (config.get("tenant") or "").strip()
        if not tenant:
            return False, (
                "Google Drive source requires a 'tenant' (the KiroCrew workspace/org "
                "the grant belongs to); a managed row cannot be scoped without it"
            )
        if self._operations_factory is None:
            return False, "Google Drive connector has no operations factory wired"
        return True, ""

    async def detect_changes(self, source: dict) -> bool:
        """Always let the sync run: the scheduler's per-row hashing skips
        unchanged rows, and the change feed is cheap when nothing changed. A
        first sync (no checkpoint) and an incremental sync alike proceed."""
        return True

    async def fetch(self, source: dict) -> tuple[str, dict]:
        """Unused: a structured connector uses :meth:`fetch_rows`. Kept explicit
        so the abstract method is satisfied and a mis-wire is loud, not silent."""
        raise NotImplementedError(
            "GoogleDriveConnector is a structured (per-row) connector; the sync "
            "scheduler must call fetch_rows(), not fetch()"
        )

    async def fetch_rows(self, source: dict):
        """Return ``(rows, snapshot, checkpoint)`` for the sync scheduler.

        Chooses snapshot vs incremental by whether a change checkpoint is saved
        (read from ``properties`` -- see :func:`_read_checkpoint` for why the
        top-level key alone is wrong):

        * no checkpoint -> SNAPSHOT: list all files, build a row per file, and
          establish a fresh start page token as the checkpoint. snapshot=True.
        * checkpoint present -> INCREMENTAL: read the change feed from it.
          - A stale token triggers a resync (the client returns a fresh start
            token and no changes) -> reconcile via a full snapshot (snapshot=True)
            so no file is missed.
          - A REMOVED (or trashed) file in the batch cannot be expressed as
            deletion through the incremental row list (the pipeline deletes only
            on ``snapshot=True``, and ``absent != deleted``). Rather than defer to
            a resync that may never come, a batch containing any removal is
            reconciled via a full snapshot NOW (snapshot=True), so the pipeline
            deletes the dropped rows through the SHARED protocol -- no second sync
            path, no vendor delete channel. The fresh batch checkpoint is still
            advanced.
          - A metadata fetch failure for a changed file means that file's change
            was NOT persisted this round. Advancing past it would skip it forever
            (the shared scheduler advances the checkpoint whenever the round
            "fully persisted", and a skipped change never becomes a row). So the
            round returns the ORIGINAL checkpoint (no advance); the next sync
            re-reads the same batch and re-attempts.
          - Otherwise build a row per changed file (snapshot=False) and advance to
            the batch's new checkpoint.
        """
        ops = self._operations_factory(source)
        account = str(source.get("account") or source.get("drive_id") or "").strip()
        tenant = str(source.get("tenant") or "").strip()
        drive_id = None if account in ("", "my-drive") else account
        checkpoint = _read_checkpoint(source)

        if not checkpoint:
            return await self._snapshot(ops, account, tenant, drive_id)

        # Incremental. DriveOperations is synchronous (it runs through W01's
        # synchronous executor); run it off the event loop so a slow change feed
        # does not stall the loop.
        changes, new_checkpoint = await asyncio.to_thread(
            ops.list_changes, str(checkpoint), drive_id=drive_id
        )
        if not changes and new_checkpoint != checkpoint:
            # A RESYNC happened (stale token -> fresh start token). Reconcile the
            # whole source with a full snapshot so no file is missed; keep the
            # fresh token as the checkpoint.
            logger.info(
                "Google Drive source resynced (token boundary); full snapshot "
                "re-list for account %s",
                account or "my-drive",
            )
            snap_rows, _snap, _cp = await self._snapshot(ops, account, tenant, drive_id)
            return snap_rows, True, new_checkpoint

        deduped = _dedup_changes(changes)

        # A removal in the batch must delete through the shared snapshot protocol
        # (the pipeline only deletes on snapshot=True). Reconcile the whole source
        # via a full snapshot NOW rather than waiting for a resync that may never
        # occur; advance to the batch's fresh checkpoint so the removal is not
        # re-processed next round. This reuses the existing _snapshot path -- no
        # second sync mechanism, no vendor-side delete.
        if any(pagination.is_removed_change(c) for c in deduped):
            logger.info(
                "Google Drive incremental batch contains a removal; reconciling "
                "via full snapshot for account %s so deletions land through the "
                "shared pipeline",
                account or "my-drive",
            )
            snap_rows, _snap, _cp = await self._snapshot(ops, account, tenant, drive_id)
            return snap_rows, True, new_checkpoint

        rows: List[SourceRow] = []
        metadata_incomplete = False
        for change in deduped:
            file_id = pagination.change_file_id(change)
            if not file_id:
                continue
            raw_file = change.get("file")
            file_obj: Mapping[str, Any]
            if isinstance(raw_file, Mapping):
                file_obj = raw_file
            else:
                try:
                    file_obj = await asyncio.to_thread(ops.get_metadata, file_id)
                except DriveOperationError:
                    # This changed file's row could not be built this round.
                    # Mark the round incomplete so the checkpoint is NOT advanced
                    # past this change (advancing would skip it permanently).
                    metadata_incomplete = True
                    logger.warning(
                        "Google Drive incremental: metadata fetch failed for %s; "
                        "checkpoint held so the change is retried next round",
                        file_id,
                        exc_info=True,
                    )
                    continue
            row = await self._row_for_file(ops, file_obj, account, tenant)
            if row is not None:
                rows.append(row)
        # Hold the checkpoint at its prior value when any change could not be
        # turned into a row this round, so the next sync re-reads the same batch.
        effective_checkpoint = checkpoint if metadata_incomplete else new_checkpoint
        return rows, False, effective_checkpoint

    # ---- internals -------------------------------------------------------

    async def _snapshot(
        self,
        ops: DriveOperations,
        account: str,
        tenant: str,
        drive_id: Optional[str],
    ) -> Tuple[List[SourceRow], bool, Optional[str]]:
        files = await asyncio.to_thread(ops.list_files, drive_id=drive_id)
        rows: List[SourceRow] = []
        for file_obj in files:
            row = await self._row_for_file(ops, file_obj, account, tenant)
            if row is not None:
                rows.append(row)
        # Establish the resume checkpoint AFTER the listing so a change made
        # during the listing is caught by the next incremental round rather than
        # missed between list and token fetch.
        checkpoint = await asyncio.to_thread(ops.get_start_page_token, drive_id=drive_id)
        return rows, True, checkpoint

    async def _row_for_file(
        self,
        ops: DriveOperations,
        file_obj: Mapping[str, Any],
        account: str,
        tenant: str,
    ) -> Optional[SourceRow]:
        """Build one SourceRow from a Drive file object, fetching its content.

        Returns None for a file with no ingestable identity or (folder) no
        content. Never invents a grant: the subject set is derived from the
        file's REAL permission principals; a file whose principals cannot be read
        is ingested as managed with an EMPTY subject set (explicit deny-all) so
        the query-time probe is the sole authority -- it is never defaulted to
        public.
        """
        file_id = str(file_obj.get("id") or "")
        mime = str(file_obj.get("mimeType") or "")
        if not file_id:
            return None
        if drive_api.is_folder(mime):
            return None
        if file_obj.get("trashed") is True:
            return None

        try:
            content_bytes, content_mime = await asyncio.to_thread(ops.fetch_content, file_obj)
        except DriveOperationError:
            logger.warning(
                "Google Drive: content fetch failed for file %s; skipping row",
                file_id,
                exc_info=True,
            )
            return None

        text = _decode_text(content_bytes)
        # A file with no extractable text (e.g. an image, an unexportable native
        # type) still gets a row so its metadata + ACL are tracked; its text is
        # a short marker rather than empty (an empty text hashes to a constant
        # and would collide unrelated files).
        if not text:
            text = f"[google-drive file {file_id} · {mime} · no extractable text]"

        drive_id_val = file_obj.get("driveId")
        resource_ref = ProviderResourceRef(
            provider="google_drive",
            account=account,
            resource_id=file_id,
            locator={
                "fileId": file_id,
                **(
                    {"driveId": drive_id_val}
                    if isinstance(drive_id_val, str) and drive_id_val
                    else {}
                ),
            },
        )

        subjects = _subjects_from_permissions(file_obj)

        title = str(file_obj.get("name") or file_id)
        return SourceRow(
            key=file_id,
            text=_fingerprinted_text(file_obj, text),
            subjects=subjects,
            tenant=tenant,
            resource_ref=resource_ref,
            title=title,
        )


def _read_checkpoint(source: Mapping[str, Any]) -> Optional[str]:
    """Read the saved resume checkpoint the way the shared scheduler stores it.

    The shared ``SyncScheduler`` persists the connector's resume token in the
    source's ``properties`` blob (``_advance_checkpoint`` writes
    ``props["checkpoint"]``), and ``properties`` on the raw ``sources`` row is a
    JSON STRING. A bare top-level ``source.get("checkpoint")`` is therefore wrong:
    on a raw row there is no ``checkpoint`` column, so it reads ``None`` every
    round and the incremental branch is never entered -- the sync silently
    degrades to a full snapshot every time.

    Read the token from ``properties`` (parsed, fault-tolerant: a malformed blob
    reads as no checkpoint, i.e. a first-sync snapshot, not a crash). A top-level
    ``checkpoint`` is still honoured as a fallback -- the scheduler merges
    ``{**props, **row}`` before calling, and a test may pass a plain dict -- but
    the ``properties`` value is authoritative when both are present, matching
    where the scheduler actually writes it.
    """
    raw_props = source.get("properties")
    if isinstance(raw_props, str) and raw_props:
        try:
            parsed = json.loads(raw_props)
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, Mapping):
            token = parsed.get("checkpoint")
            if isinstance(token, str) and token:
                return token
    elif isinstance(raw_props, Mapping):
        # An already-parsed properties mapping (some callers merge it in).
        token = raw_props.get("checkpoint")
        if isinstance(token, str) and token:
            return token
    top = source.get("checkpoint")
    return top if isinstance(top, str) and top else None


def _subjects_from_permissions(file_obj: Mapping[str, Any]) -> tuple[str, ...]:
    """Derive the row's subject set from the file's REAL Drive principals.

    Reads ``permissions[].emailAddress`` (a user/group principal) and honours a
    genuine ``type == 'anyone'`` permission as PUBLIC (the connector has proof
    the file is link/anyone-shared). A file whose permissions field is absent
    (Drive omits it unless requested, or the caller lacks the
    ``canReadRevisions``-style capability) yields an EMPTY tuple -- an explicit
    deny-all, NOT public: a missing field must never become a public grant. The
    query-time probe is authoritative regardless, so an empty ingest-time set is
    the safe floor.
    """
    from kiro_crew.knowledge.acl import PUBLIC_SUBJECT

    perms = file_obj.get("permissions")
    if not isinstance(perms, list):
        return ()
    subjects: set[str] = set()
    for perm in perms:
        if not isinstance(perm, Mapping):
            continue
        ptype = perm.get("type")
        if ptype == "anyone":
            # Proven link/anyone-shared: genuinely public. Only this explicit
            # signal makes a row public; nothing else defaults to it.
            subjects.add(PUBLIC_SUBJECT)
            continue
        email = perm.get("emailAddress")
        if isinstance(email, str) and email:
            subjects.add(email)
    return tuple(sorted(subjects))


def _fingerprinted_text(file_obj: Mapping[str, Any], text: str) -> str:
    """Bind the row's content hash to the file's version + modifiedTime.

    ``ingest_rows`` hashes ``row.text`` to decide changed-vs-unchanged. Drive's
    ``version`` bumps on any metadata OR content change and ``modifiedTime`` on a
    content change, so prefixing them makes a metadata-only change (a rename, a
    re-share) re-ingest too -- which matters because a re-share changes the ACL
    the row must re-derive. The marker is a single leading line, so the extracted
    body is unaffected.
    """
    version = file_obj.get("version")
    modified = file_obj.get("modifiedTime")
    marker = f"[drive-version {version} · modified {modified}]\n"
    return marker + text


def _decode_text(content: bytes) -> str:
    """Decode content bytes to text, truncating at the byte ceiling.

    UTF-8 with replacement so a stray byte in an exported doc does not crash the
    ingest; a binary that is not text decodes to mostly-replacement noise, which
    the metadata row still tracks. Truncation is at the byte boundary before
    decode so memory is bounded.
    """
    if not content:
        return ""
    clipped = content[:_MAX_CONTENT_BYTES]
    return clipped.decode("utf-8", errors="replace")


def _dedup_changes(changes: List[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Keep only the LAST change per fileId in a batch.

    Drive may report a file several times across a batch (edited then re-shared);
    only the final state matters for the row, and re-ingesting the same file
    twice in one round is wasted work. Order-preserving on the last occurrence.
    """
    last_index: dict[str, int] = {}
    for i, change in enumerate(changes):
        fid = pagination.change_file_id(change)
        if fid:
            last_index[fid] = i
    kept_indices = set(last_index.values())
    return [c for i, c in enumerate(changes) if i in kept_indices]


__all__ = ["GoogleDriveConnector", "SOURCE_TYPE"]
