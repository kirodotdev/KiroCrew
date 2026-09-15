"""Small, lineage-independent record history; callers own the SQL transaction.

These extension tables never convert a V1 row to the member schema. Old binaries
can still read/write their original tables. Reconciliation records such writes as
untracked instead of inventing provenance. Vectors and access counters are derived
data and do not change a content revision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kiro_crew._sqlite_compat import sqlite3

V1_ACCEPTED_REVISION_LIMIT = 20


@dataclass(frozen=True)
class CorrectionEvidence:
    """Verified transcript replacement, bound to a pre-extraction fact revision."""

    key: str
    old_value_json: str
    new_value_json: str
    revision: int
    source_ref: str
    observed_at: str


def verified_correction(
    *, key: str, before: dict, value: object, quote: object, messages: list[dict], session_key: str
) -> CorrectionEvidence | None:
    """Recognize explicit literal replacement; ambiguous natural language stays pending.

    The caller supplies the actual pre-model snapshot and user transcript, never
    a model-supplied revision or a model's description of who said something.
    """
    if not isinstance(quote, str) or not 10 <= len(quote) <= 1024:
        return None
    try:
        old = json.loads(before["value_json"])
        revision = before["record_revision"]
    except (KeyError, ValueError, TypeError):
        return None
    if not isinstance(revision, int) or revision < 1 or isinstance(value, (dict, list)):
        return None
    old_text, new_text = str(old), str(value)
    if old_text == new_text or not old_text or not new_text:
        return None
    if old_text not in quote or new_text not in quote:
        return None
    old_literal, new_literal = re.escape(old_text), re.escape(new_text)
    # Validate the complete user statement, not just the model-selected quote:
    # a substring can omit "do not", an enclosing example, or a later refusal.
    # Mixed prose stays a proposal; this recognizer is deliberately not an NLU
    # classifier that attempts to infer a sentence's surrounding authority.
    replacements = (
        rf"(?:please\s+)?replace\s+['\"]?{old_literal}['\"]?\s+with\s+['\"]?{new_literal}['\"]?\s*[.!。]?",
        rf"(?:(?:I|we)\s+)?changed?\s+from\s+{old_literal}\s+to\s+{new_literal}\s*[.!。]?",
        rf"(?:please\s+)?use\s+{new_literal}\s+instead\s+of\s+{old_literal}\s*[.!。]?",
        rf"(?:请把|请将|将|把|我已将)?\s*{old_literal}\s*(?:改为|改成|替换为|更正为)\s*{new_literal}\s*[.!。]?",
    )
    if not any(re.fullmatch(pattern, quote.strip(), re.I) for pattern in replacements):
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        # Never authorize an earlier correction across a newer user turn. The
        # latter may withdraw it; unsupported or injected content stays pending.
        content = message.get("content", "")
        if (
            not isinstance(content, str)
            or content.startswith(("[Cron notification", "[Subagent completion", "[auto-nudge"))
            or quote not in content
        ):
            return None
        statement = content.strip()
        if len(statement) > 1024 or not any(
            re.fullmatch(pattern, statement, re.I) for pattern in replacements
        ):
            return None
        try:
            observed = _timestamp(message.get("ts"), "observed_at")
            current_updated = _timestamp(before.get("updated_at"), "updated_at")
        except ValueError:
            return None
        if not observed or (current_updated and observed < current_updated):
            return None
        return CorrectionEvidence(
            key,
            before["value_json"],
            json.dumps(value),
            revision,
            f"{session_key}#message:{index}: {statement}",
            observed,
        )
    return None


_KINDS = {"fact", "directive", "episode"}
_STATUSES = {"active", "superseded", "expired", "forgotten"}
_FIELDS = (
    "category",
    "subject",
    "predicate",
    "scope",
    "status",
    "valid_from",
    "valid_until",
    "observed_at",
    "source_ref",
)
_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+(?![\w-])"
)
_DERIVED = {"embedding", "last_accessed_at", "access_count", "id", "kind", "metadata"}
# A physical rewrite changes this clock without changing the remembered content.
# Keep it in revision snapshots as provenance, but not in the idempotency digest.
_HASH_DERIVED = _DERIVED | {"updated_at"}


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create additive tables/indexes without executescript's implicit COMMIT."""
    statements = (
        """CREATE TABLE IF NOT EXISTS memory_record_meta (
            record_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
            revision INTEGER NOT NULL, category TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '', predicate TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
            valid_from TEXT NOT NULL DEFAULT '', valid_until TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL, email_addresses TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS memory_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
            revision INTEGER NOT NULL, base_revision INTEGER NOT NULL,
            status TEXT NOT NULL, operation TEXT NOT NULL, source TEXT NOT NULL,
            before_json TEXT, after_json TEXT, metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_memory_revision_record "
        "ON memory_revisions(record_id, id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_fact_identity "
        "ON memory_record_meta(scope, subject, predicate) "
        "WHERE kind IN ('fact', 'directive') AND subject <> '' AND predicate <> '' "
        "AND status = 'active'",
    )
    for statement in statements:
        db.execute(statement)


def record_id_for(kind: str, record_id: str) -> str:
    if kind not in _KINDS or not isinstance(record_id, str) or not record_id:
        raise ValueError("Memory record kind and id are required")
    return record_id if kind == "episode" or record_id.startswith("key:") else f"key:{record_id}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot(row: dict | sqlite3.Row | None) -> dict | None:
    """Keep original content/provenance, excluding only derived vector/access data."""
    if row is None:
        return None
    return {key: value for key, value in dict(row).items() if key not in _DERIVED}


def content_hash(row: dict | sqlite3.Row | None) -> str:
    content = (
        None
        if row is None
        else {key: value for key, value in dict(row).items() if key not in _HASH_DERIVED}
    )
    return hashlib.sha256(_json(content).encode("utf-8")).hexdigest()


def get_record_metadata(db: sqlite3.Connection, record_id: str) -> dict:
    cursor = db.execute("SELECT * FROM memory_record_meta WHERE record_id = ?", (record_id,))
    row = cursor.fetchone()
    if row is None:
        return {}
    result = dict(zip((column[0] for column in cursor.description), row))
    result["email_addresses"] = json.loads(result["email_addresses"])
    return result


def _timestamp(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date or timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date or timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def normalize_metadata(metadata: dict | None) -> dict:
    """Validate a patch; canonical identity is explicit, never inferred from text."""
    if metadata is None:
        return {}
    if not isinstance(metadata, dict) or any(key not in _FIELDS for key in metadata):
        raise ValueError("Unknown memory metadata field")
    result = {}
    for key, value in metadata.items():
        if key in {"valid_from", "valid_until", "observed_at"}:
            result[key] = _timestamp(value, key)
        elif not isinstance(value, str) or len(value) > 1024:
            raise ValueError(f"{key} must be text of at most 1024 characters")
        else:
            result[key] = value.strip() if key == "scope" else " ".join(value.split())
            if key in {"subject", "predicate", "category"}:
                result[key] = result[key].casefold()
    if "status" in result and result["status"] not in _STATUSES:
        raise ValueError("Unknown memory status")
    if (
        result.get("valid_from")
        and result.get("valid_until")
        and result["valid_from"] >= result["valid_until"]
    ):
        raise ValueError("valid_until must be later than valid_from")
    return result


def _emails(row: dict | None) -> list[str]:
    if not row:
        return []
    parts = [str(row.get("text", "")), str(row.get("key", ""))]
    value = row.get("value_json", "")
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        pass
    parts.append(_json(value))
    # Preserve display spelling; casefold only deduplicates the search metadata.
    found = {match.group().casefold(): match.group() for match in _EMAIL.finditer(" ".join(parts))}
    return [found[key] for key in sorted(found)]


def _append_revision(
    db,
    *,
    record_id,
    revision,
    base_revision,
    status,
    operation,
    source,
    before,
    after,
    metadata,
    now,
) -> int:
    cursor = db.execute(
        "INSERT INTO memory_revisions (record_id, revision, base_revision, status, "
        "operation, source, before_json, after_json, metadata_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            revision,
            base_revision,
            status,
            operation,
            source,
            _json(snapshot(before)) if before is not None else None,
            _json(snapshot(after)) if after is not None else None,
            _json(metadata),
            now,
        ),
    )
    return int(cursor.lastrowid or 0)


def limit_v1_accepted_history(db: sqlite3.Connection, record_id: str | None = None) -> None:
    """Keep V1's latest accepted snapshots within the caller's transaction.

    Callers establish V1 ownership before using this helper. A normal write
    examines only its record; opening a V1 store also bounds existing journals.
    Proposal statuses, current metadata and monotonic IDs are never changed.
    """
    record_ids = (
        [record_id]
        if record_id is not None
        else [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT record_id FROM memory_revisions WHERE status = 'accepted'"
            )
        ]
    )
    for accepted_record_id in record_ids:
        db.execute(
            "DELETE FROM memory_revisions WHERE record_id = ? AND status = 'accepted' "
            "AND id NOT IN (SELECT id FROM memory_revisions "
            "WHERE record_id = ? AND status = 'accepted' "
            "ORDER BY id DESC LIMIT ?)",
            (accepted_record_id, accepted_record_id, V1_ACCEPTED_REVISION_LIMIT),
        )


def sync_record(
    db: sqlite3.Connection,
    *,
    kind: str,
    record_id: str,
    before: dict | None,
    after: dict | None,
    source: str = "user_explicit",
    now: str | None = None,
    metadata: dict | None = None,
    operation: str = "update",
    force_revision: bool = False,
    limit_v1_history: bool = False,
) -> dict:
    """Record an accepted mutation atomically with the caller's physical write.

    No commit, savepoint, embedding or external I/O. Raises on invalid metadata
    or a duplicate explicit canonical fact identity, allowing caller rollback.
    An identical retry does not advance revision unless the caller explicitly
    resolves proposals with ``force_revision=True``. Each retained revision has
    prior provenance even when the current source changes. V1 callers bound
    accepted history; V2 callers retain every revision.
    """
    record_id = record_id_for(kind, record_id)
    now = _timestamp(now or datetime.now(timezone.utc).isoformat(), "now")
    old = get_record_metadata(db, record_id)
    patch = normalize_metadata(metadata)
    current = {key: old.get(key, "") for key in _FIELDS}
    current["status"] = old.get("status", "active")
    current["category"] = old.get("category") or (
        "episode" if kind == "episode" else record_id.removeprefix("key:").split(".")[0]
    )
    current["observed_at"] = old.get("observed_at") or now
    current["source_ref"] = old.get("source_ref") or str(
        (after or before or {}).get("source") or source
    )
    current.update(patch)
    if after is None or after.get("is_deleted"):
        current["status"] = patch.get(
            "status",
            (
                old.get("status")
                if old.get("status") in {"superseded", "expired", "forgotten"}
                else "forgotten"
            ),
        )
    elif before and before.get("is_deleted") and "status" not in patch:
        current["status"] = "active"
    if (
        current["valid_from"]
        and current["valid_until"]
        and current["valid_from"] >= current["valid_until"]
    ):
        raise ValueError("valid_until must be later than valid_from")
    digest = content_hash(after)
    emails = _emails(after)
    if (
        not force_revision
        and old
        and old["content_hash"] == digest
        and all(old[key] == current[key] for key in _FIELDS)
    ):
        return old
    revision = int(old.get("revision", 0)) + 1
    result = dict(
        current,
        record_id=record_id,
        kind=kind,
        revision=revision,
        content_hash=digest,
        email_addresses=emails,
        updated_at=now,
    )
    columns = (
        "record_id",
        "kind",
        "revision",
        *_FIELDS,
        "content_hash",
        "email_addresses",
        "updated_at",
    )
    values = tuple(_json(emails) if key == "email_addresses" else result[key] for key in columns)
    updated = db.execute(
        "UPDATE memory_record_meta SET "
        + ", ".join(f"{key}=?" for key in columns[1:])
        + " WHERE record_id=?",
        (*values[1:], record_id),
    )
    if updated.rowcount == 0:
        # A plain INSERT deliberately preserves every constraint failure. In
        # particular, two active records cannot silently share one canonical
        # fact identity under the partial unique index above.
        db.execute(
            f"INSERT INTO memory_record_meta ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
    _append_revision(
        db,
        record_id=record_id,
        revision=revision,
        base_revision=revision - 1,
        status="accepted",
        operation=operation,
        source=source,
        before=before,
        after=after,
        metadata={"before": old, "after": result},
        now=now,
    )
    if limit_v1_history:
        limit_v1_accepted_history(db, record_id)
    return result


def propose_conflict(
    db: sqlite3.Connection,
    *,
    kind: str,
    record_id: str,
    before: dict,
    after: dict,
    source: str,
    metadata: dict | None = None,
    operation: str = "update",
) -> int:
    """Persist a deduplicated proposal without replacing the current fact."""
    record_id = record_id_for(kind, record_id)
    current = get_record_metadata(db, record_id)
    patch = normalize_metadata(metadata)
    revision = int(current.get("revision", 0))
    payload = _json(snapshot(after))
    row = db.execute(
        "SELECT id FROM memory_revisions WHERE record_id=? AND base_revision=? "
        "AND status='conflict' AND after_json=? AND source=? AND operation=? "
        "AND metadata_json=? LIMIT 1",
        (record_id, revision, payload, source, operation, _json(patch)),
    ).fetchone()
    if row:
        return int(row[0])
    return _append_revision(
        db,
        record_id=record_id,
        revision=revision,
        base_revision=revision,
        status="conflict",
        operation=operation,
        source=source,
        before=before,
        after=after,
        metadata=patch,
        now=datetime.now(timezone.utc).isoformat(),
    )


def eligible(metadata: dict, *, now: str | None = None) -> bool:
    """Half-open UTC validity interval; undated legacy records remain eligible."""
    stamp = _timestamp(now or datetime.now(timezone.utc).isoformat(), "now")
    return (
        metadata.get("status", "active") == "active"
        and (not metadata.get("valid_from") or metadata["valid_from"] <= stamp)
        and (not metadata.get("valid_until") or stamp < metadata["valid_until"])
    )


def reconcile(db: sqlite3.Connection) -> int:
    """Backfill metadata, or journal writes made by an older compatible binary."""
    changed = 0
    for relation, kind in (("semantic_memory", "fact"), ("episodic_memories", "episode")):
        cursor = db.execute(f"SELECT * FROM {relation}")
        names = [column[0] for column in cursor.description]
        for values in cursor:
            row = dict(zip(names, values))
            actual_kind = "directive" if str(row.get("key", "")).startswith("lesson.") else kind
            record_id = record_id_for(actual_kind, row["id"] if kind == "episode" else row["key"])
            previous = get_record_metadata(db, record_id)
            if previous.get("content_hash") == content_hash(row):
                continue
            last = (
                db.execute(
                    "SELECT after_json FROM memory_revisions WHERE record_id=? AND status='accepted' ORDER BY id DESC LIMIT 1",
                    (record_id,),
                ).fetchone()
                if previous
                else None
            )
            prior = json.loads(last[0]) if last and last[0] else None
            sync_record(
                db,
                kind=actual_kind,
                record_id=record_id,
                before=prior,
                after=row,
                source="legacy_untracked" if previous else "legacy_import",
                operation="legacy_sync" if previous else "backfill",
            )
            changed += 1
    return changed
