"""Select folder files whose chunks all lack extraction for a one-time re-read.

The source records a cutoff time. Each sweep selects only never-edited active
chunks created before it, so re-read files leave the selection even when their
extraction is empty again. The source is stamped once nothing remains to select.
"""

from __future__ import annotations

import json
from datetime import datetime

#: Set on a folder source's properties once no eligible files remain.
EXTRACTION_BACKFILL_KEY = "extraction_backfill"
EXTRACTION_BACKFILL_SINCE_KEY = "extraction_backfill_since"
_EXTRACTION_BACKFILL_VERSION = 1

# SQLite's default host-parameter limit is 999 on older builds.
_ID_BATCH = 500


def needs_extraction_backfill(props: dict) -> bool:
    """Whether this source's files have not been checked yet."""
    return not props.get(EXTRACTION_BACKFILL_KEY)


def _unextracted_ids(db, ids: list[str], since: str) -> set[str]:
    """Never-edited active items created before the cutoff with no extraction."""
    eligible: set[str] = set()
    for start in range(0, len(ids), _ID_BATCH):
        batch = ids[start : start + _ID_BATCH]
        marks = ",".join("?" * len(batch))
        for row in db.execute(
            f"SELECT i.id FROM items i "  # noqa: S608
            f"WHERE i.status = 'active' AND COALESCE(i.summary, '') = '' "
            f"AND NOT EXISTS (SELECT 1 FROM mentions m WHERE m.item_id = i.id) "
            f"AND i.created_at = i.updated_at AND i.created_at < ? "
            f"AND i.id IN ({marks})",
            [since, *batch],
        ).fetchall():
            eligible.add(row["id"])
    return eligible


def select_backfill(store, source_id: str) -> set[str] | None:
    """Select eligible done files, recording the cutoff and completion on the source.

    ``None`` means the source row is gone or already stamped. Properties are read
    from the row itself, not from the caller's potentially stale copy.

    shortcut: chunks that genuinely name nothing are also re-read once; another
    outage during the re-read is not retried. Per-chunk failure recording and
    retries are needed to distinguish those cases.
    """
    db = store.db
    src = db.execute("SELECT properties FROM sources WHERE id = ?", (source_id,)).fetchone()
    if src is None:
        return None
    try:
        props = json.loads(src["properties"] or "{}")
    except (ValueError, TypeError, RecursionError):
        props = {}
    if not isinstance(props, dict):
        props = {}
    if not needs_extraction_backfill(props):
        return None
    since = props.get(EXTRACTION_BACKFILL_SINCE_KEY)
    if not since:
        since = datetime.now().isoformat()
        store.merge_source_properties(source_id, set_keys={EXTRACTION_BACKFILL_SINCE_KEY: since})
    rows = db.execute(
        "SELECT file_path, item_ids FROM folder_file_state "
        "WHERE source_id = ? AND status = 'done' AND COALESCE(content_hash, '') != ''",
        (source_id,),
    ).fetchall()
    groups: dict[str, list[str]] = {}
    for row in rows:
        try:
            ids = json.loads(row["item_ids"] or "[]")
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids):
            groups[row["file_path"]] = ids
    eligible = _unextracted_ids(db, sorted({i for ids in groups.values() for i in ids}), since)
    selected = {fp for fp, ids in groups.items() if all(i in eligible for i in ids)}
    if not selected:
        store.merge_source_properties(
            source_id,
            set_keys={EXTRACTION_BACKFILL_KEY: _EXTRACTION_BACKFILL_VERSION},
            remove_keys=(EXTRACTION_BACKFILL_SINCE_KEY,),
        )
    return selected
