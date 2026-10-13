"""The terminal 'done' row a folder scan writes over its 'scanning' marker.

``FolderWatcher`` binds ``mark_committed`` and ``settle_done`` as its ``_mark_committed``
and ``_settle_done`` methods; :mod:`kiro_crew.knowledge.folder_watcher` stays the import
path. The commit callback writes the row inside the pipeline's finalize hop
(``_mark_committed``), and the scan's own terminal write lands only while the marker is
still there (``_settle_done``), so a dedup verdict that landed in between stands.
"""

from __future__ import annotations

import json
from datetime import datetime

from .ingestion import EXTRACTION_INCOMPLETE_PREFIX


def mark_committed(
    self,
    source_id: str,
    file_path: str,
    item_ids: list[str],
    included: set[str] | None,
    incomplete: str | None = None,
) -> None:
    """Turn the 'scanning' marker into the 'done' row naming *item_ids*.

    A targeted UPDATE: the marker already carries the file's content hash and
    mtime. No transaction control, so the caller decides how it commits.
    *incomplete*, the extraction-incomplete message, is recorded as the row's
    reason and keeps the marker's attempt count; without it the count clears.
    """
    text_hash: str | None = None
    if item_ids:
        row = self.store.db.execute(
            "SELECT content_hash FROM items WHERE id = ?", (item_ids[0],)
        ).fetchone()
        if row:
            text_hash = row["content_hash"]
    # The targets first: a group whose record is missing could keep a removed
    # target's text unnoticed, while a record ahead of its row only costs a
    # re-read.
    if included is not None:
        self._write_include_record(source_id, file_path, included)
    self.store.db.execute(
        "UPDATE folder_file_state SET item_ids = ?, text_hash = ?, "
        "status = 'done', error_message = ?, "
        "attempts = CASE WHEN ? IS NULL THEN 0 ELSE attempts END, "
        "last_seen = ? WHERE source_id = ? AND file_path = ?",
        (
            json.dumps(item_ids),
            text_hash,
            incomplete,
            incomplete,
            datetime.now().isoformat(),
            source_id,
            file_path,
        ),
    )


def settle_done(
    self,
    source_id: str,
    file_path: str,
    content_hash: str,
    mtime: float,
    item_ids: list[str],
    now: str,
    included: set[str] | None,
    *,
    incomplete: bool = False,
) -> bool:
    """The caller's terminal 'done' write, only over this attempt's own marker.

    The commit callback normally wrote the row already. From that moment the
    row is a 'done' row the dedup sweep enumerates, and a collapse can delete
    the group and mark the row 'deduped' before this runs. Writing the
    captured ids over that verdict would name deleted items, so the write
    lands only while the row is still the 'scanning' marker, which no other
    writer settles. Any other status means the row is already settled: by
    the callback, by the sweep, or by a cascade adopting an item into it.

    Runs inside the scan, under the per-source ``self._locks[source_id]``,
    so no other scan of this source can rewrite the marker meanwhile.
    ``BEGIN IMMEDIATE`` holds the writer lock across the read and the write.
    When *incomplete*, the row keeps the reason already recorded and the
    attempt the marker counted, as ``_mark_committed`` does.
    Returns ``True`` when written.
    """
    self.store.db.execute("BEGIN IMMEDIATE")
    try:
        row = self.store.db.execute(
            "SELECT status, error_message, attempts FROM folder_file_state "
            "WHERE source_id = ? AND file_path = ?",
            (source_id, file_path),
        ).fetchone()
        pending = row is not None and row["status"] == "scanning"
        if pending:
            reason, attempts = None, 0
            if incomplete:
                reason = row["error_message"] or f"{EXTRACTION_INCOMPLETE_PREFIX}some chunks"
                attempts = int(row["attempts"] or 0)
            self._update_state(
                source_id,
                file_path,
                content_hash,
                mtime,
                json.dumps(item_ids),
                now,
                "done",
                reason,
                attempts=attempts,
                commit=False,
                included=included,
            )
        self.store.db.execute("COMMIT")
    except BaseException:
        self.store.db.execute("ROLLBACK")
        raise
    return pending
