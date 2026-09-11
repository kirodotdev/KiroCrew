"""Paged memory editing and atomic, signed previews for either store lineage.

The preview carries a selector and its digest, never a server-side copy of a
large collection. Applying it re-reads that exact collection inside one SQLite
write transaction. Embeddings are invalidated and rebuilt by normal backfill.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

from kiro_crew import memory_schema

MAX_BATCH_ROWS = 10000
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_EXPLICIT_ROWS = 500
PREVIEW_ROWS = 25
PREVIEW_TTL = 900
_KINDS = {"all", "fact", "directive", "episode"}
_PROTECTED_FIELDS = {"repo_scope", "scope", "source", "source_ref", "derived_from", "category"}


class MemoryEditError(ValueError):
    def __init__(self, message: str, code: str = "invalid_memory_edit", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _query(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"q", "kind"}:
        raise MemoryEditError("Invalid memory filter.")
    query, kind = raw.get("q", ""), raw.get("kind", "all")
    if (
        not isinstance(query, str)
        or len(query) > 2000
        or not isinstance(kind, str)
        or kind not in _KINDS
    ):
        raise MemoryEditError("Use a valid kind and a query up to 2000 characters.")
    return {"q": query.strip(), "kind": kind}


def _identity(item: Any, *, revision: bool = False) -> tuple[str, str]:
    if not isinstance(item, dict):
        raise MemoryEditError("A memory selection must contain record identities.")
    kind, record_id = item.get("kind"), item.get("id")
    if (
        not isinstance(kind, str)
        or kind not in _KINDS - {"all"}
        or not isinstance(record_id, str)
        or not 1 <= len(record_id) <= 512
    ):
        raise MemoryEditError("Invalid memory identity.")
    if revision and (not isinstance(item.get("revision"), str) or len(item["revision"]) != 64):
        raise MemoryEditError("Refresh memories before selecting them.")
    return kind, record_id


def _selection(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise MemoryEditError("Select memories to edit.")
    if set(raw) == {"items"}:
        items = raw["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_EXPLICIT_ROWS:
            raise MemoryEditError("Select 1–500 records, or use all matching memories.")
        identities = [_identity(item, revision=True) for item in items]
        if len(set(identities)) != len(identities):
            raise MemoryEditError("A memory cannot be selected twice.")
        return {
            "items": [
                {"kind": item["kind"], "id": item["id"], "revision": item["revision"]}
                for item in items
            ]
        }
    if "query" in raw and not set(raw) - {"query", "exclude"}:
        excluded = raw.get("exclude", [])
        if not isinstance(excluded, list) or len(excluded) > MAX_EXPLICIT_ROWS:
            raise MemoryEditError("Exclude at most 500 records, or narrow the filter.")
        return {
            "query": _query(raw["query"]),
            "exclude": [dict(zip(("kind", "id"), _identity(item))) for item in excluded],
        }
    raise MemoryEditError("Select explicit records or all matching memories.")


def _operation(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise MemoryEditError("Choose an editing operation.")
    mode = raw.get("type")
    if mode == "forget" and set(raw) == {"type"}:
        return raw
    if mode == "set" and set(raw) in ({"type", "value"}, {"type", "text"}):
        try:
            _json(raw)
        except (TypeError, ValueError):
            raise MemoryEditError("Memory content must be valid finite JSON.") from None
        return raw
    if mode == "replace_text" and not set(raw) - {"type", "find", "replacement", "match_case"}:
        find, replacement = raw.get("find"), raw.get("replacement")
        if (
            not isinstance(find, str)
            or not 1 <= len(find) <= 2000
            or not isinstance(replacement, str)
            or len(replacement) > 2000
            or not isinstance(raw.get("match_case", False), bool)
        ):
            raise MemoryEditError(
                "Provide literal find and replacement text of at most 2000 characters."
            )
        return {
            "type": mode,
            "find": find,
            "replacement": replacement,
            "match_case": raw.get("match_case", False),
        }
    raise MemoryEditError("Unsupported editing operation.")


def _record(row: Any) -> dict:
    value = dict(row)
    value["revision"] = hashlib.sha256(_json(value).encode()).hexdigest()
    return value


def _rows(store: Any, query: dict, identities: list[tuple[str, str]] | None = None):
    from kiro_crew.memory_record_metadata import get_record_metadata, record_id_for

    # A UNION over the compatibility relations preserves both physical schemas.
    # No vector column is read; row width and Python memory remain bounded.
    sem_ids = [record_id for kind, record_id in identities or [] if kind != "episode"]
    epi_ids = [record_id for kind, record_id in identities or [] if kind == "episode"]

    def clause(column: str, values: list[str]) -> str:
        if identities is None:
            return ""
        return f" AND {column} IN ({','.join('?' for _ in values)})" if values else " AND 0"

    sql = (
        "SELECT CASE WHEN key LIKE 'lesson.%' THEN 'directive' ELSE 'fact' END AS kind, "
        "key AS id, key, value_json, value_json AS text, source, updated_at, '' AS tags, "
        f"confidence AS importance FROM semantic_memory WHERE is_deleted = 0{clause('key', sem_ids)} UNION ALL "
        "SELECT 'episode', id, NULL, NULL, text, '', created_at, tags, importance "
        f"FROM episodic_memories WHERE is_deleted = 0{clause('id', epi_ids)}"
    )
    if store._lineage == memory_schema.LINEAGE_CREW:
        sql = (
            f"SELECT b.*, p.derived_from, p.source AS _source, p.updated_at AS _updated "
            f"FROM ({sql}) b JOIN memory_items p ON p.id = "
            "CASE WHEN b.kind='episode' THEN b.id ELSE 'key:' || b.id END ORDER BY b.kind,b.id"
        )
    else:
        sql = f"SELECT b.*, '' AS derived_from FROM ({sql}) b ORDER BY b.kind,b.id"
    tokens = _normalized(query["q"]).split()
    cursor = store.db.execute(sql, sem_ids + epi_ids)
    try:
        for row in cursor:
            if identities is not None and (row["kind"], row["id"]) not in identities:
                continue
            if query["kind"] != "all" and row["kind"] != query["kind"]:
                continue
            meta = get_record_metadata(store.db, record_id_for(row["kind"], row["id"]))
            text = row["text"]
            if row["value_json"] is not None:
                try:
                    text = _json(json.loads(row["value_json"]))
                except (ValueError, TypeError):
                    pass
            searchable = f"{row['key'] or ''} {text} {row['tags']} {meta.get('category', '')} {meta.get('subject', '')} {meta.get('predicate', '')}"
            folded = _normalized(searchable)
            if tokens and not all(token in folded for token in tokens):
                continue
            record = dict(row)
            record["source"] = record.pop("_source", record["source"] or meta.get("source_ref", ""))
            record["updated_at"] = record.pop("_updated", record["updated_at"])
            if (
                store._lineage != memory_schema.LINEAGE_CREW
                and row["kind"] == "episode"
                and meta.get("revision", 0) > 1
            ):
                record["updated_at"] = meta.get("updated_at", record["updated_at"])
            record["metadata"] = meta
            record["email_addresses"] = meta.get("email_addresses", [])
            yield _record(record)
    finally:
        cursor.close()


def list_records(store: Any, query: dict, *, limit: int = 50, offset: int = 0) -> dict:
    query = _query(query)
    if isinstance(limit, bool) or isinstance(offset, bool) or not 1 <= limit <= 100 or offset < 0:
        raise MemoryEditError("Use a page size of 1–100 and a non-negative offset.")
    entries, total = [], 0
    with store._db_lock:
        for row in _rows(store, query):
            if offset <= total < offset + limit:
                _conflict_count(store, row)
                entries.append(row)
            total += 1
    return {"entries": entries, "total": total, "has_more": total > offset + limit}


def _conflict_count(store: Any, row: dict) -> None:
    metadata = row["metadata"]
    count = store.db.execute(
        "SELECT COUNT(*) FROM memory_revisions WHERE record_id = ? AND status = 'conflict' AND base_revision = ?",
        (metadata.get("record_id", ""), metadata.get("revision", 0)),
    ).fetchone()[0]
    metadata["pending_conflicts"] = count


def _check_selection_size(matched: int, size: int) -> None:
    if matched > MAX_BATCH_ROWS or size > MAX_BATCH_BYTES:
        raise MemoryEditError(
            "Narrow this selection to at most 10,000 memories and 32 MiB of content.",
            "memory_selection_too_large",
            413,
        )


def refresh_records(store: Any, items: Any = None, *, selection: Any = None) -> dict:
    if selection is not None:
        selected = _selection(selection)
        if items is not None or "query" not in selected:
            raise MemoryEditError("Refresh explicit identities or one matching query.")
        excluded = {(item["kind"], item["id"]) for item in selected["exclude"]}
        matched, size = 0, 0
        with store._db_lock:
            for row in _rows(store, selected["query"]):
                if (row["kind"], row["id"]) in excluded:
                    continue
                matched += 1
                size += len(_json(row).encode())
                _check_selection_size(matched, size)
        return {"matched_count": matched}
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_EXPLICIT_ROWS:
        raise MemoryEditError("Select 1–500 record identities to refresh.")
    identities = list(dict.fromkeys(_identity(item) for item in items))
    with store._db_lock:
        entries = list(_rows(store, _query({}), identities))
        for row in entries:
            _conflict_count(store, row)
    found = {(row["kind"], row["id"]) for row in entries}
    return {
        "entries": entries,
        "missing": [
            {"kind": kind, "id": record_id}
            for kind, record_id in identities
            if (kind, record_id) not in found
        ],
    }


def record_history(store: Any, item: dict, *, limit: int = 25, offset: int = 0) -> dict:
    from kiro_crew.memory_record_metadata import get_record_metadata, record_id_for

    kind, identity = _identity(item)
    if not 1 <= limit <= 100 or offset < 0:
        raise MemoryEditError("Use a page size of 1–100 and a non-negative offset.")
    record_id = record_id_for(kind, identity)
    with store._db_lock:
        metadata = get_record_metadata(store.db, record_id)
        if not metadata or metadata["kind"] != kind:
            raise MemoryEditError(
                "This memory has no available history.", "memory_record_missing", 404
            )
        rows = store.db.execute(
            "SELECT * FROM memory_revisions WHERE record_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (record_id, limit + 1, offset),
        ).fetchall()
    return {
        "entries": [dict(row) for row in rows[:limit]],
        "has_more": len(rows) > limit,
        "current_revision": metadata["revision"],
    }


def _replace(value: Any, pattern: re.Pattern, replacement: str) -> Any:
    if isinstance(value, str):
        return pattern.sub(lambda _: replacement, value)
    if isinstance(value, list):
        return [_replace(item, pattern, replacement) for item in value]
    if isinstance(value, dict):
        return {
            key: item if key in _PROTECTED_FIELDS else _replace(item, pattern, replacement)
            for key, item in value.items()
        }
    return value


def _after(store: Any, before: dict, operation: dict) -> dict | None:
    from kiro_crew.vector_memory import _contains_injection

    mode = operation["type"]
    if mode == "forget":
        return None
    after = dict(before)
    episode = before["kind"] == "episode"
    value = before["text"] if episode else json.loads(before["value_json"])
    if mode == "set":
        field = "text" if episode else "value"
        if field not in operation:
            raise MemoryEditError(f"This record requires {field}.")
        new_value = operation[field]
        old_protected = (
            {key: value[key] for key in _PROTECTED_FIELDS if key in value}
            if isinstance(value, dict)
            else {}
        )
        new_protected = (
            {key: new_value[key] for key in _PROTECTED_FIELDS if key in new_value}
            if isinstance(new_value, dict)
            else {}
        )
        if old_protected != new_protected:
            raise MemoryEditError(
                "Identity, scope and provenance cannot be changed in the content editor."
            )
    else:
        pattern = re.compile(re.escape(operation["find"]), 0 if operation["match_case"] else re.I)
        new_value = _replace(value, pattern, operation["replacement"])
    # JSON booleans and numbers are distinct, even though Python considers
    # True == 1 (also inside nested dictionaries/lists).
    if _json(new_value) == _json(value):
        return after
    if episode:
        if (
            not isinstance(new_value, str)
            or not 10 <= len(new_value.strip()) <= 2000
            or _contains_injection(new_value)
        ):
            raise MemoryEditError(
                "Episode text must contain 10–2000 characters and no blocked instruction markers.",
                status=422,
            )
        after["text"] = new_value.strip()
    else:
        error = store.validate_semantic(before["key"], new_value, 1.0, "user_explicit")
        if error:
            raise MemoryEditError(error[1], status=422)
        after["value_json"] = _json(new_value)
        after["text"] = after["value_json"]
    return after


def _collect(store: Any, selection: dict, operation: dict) -> tuple[str, int, list]:
    explicit = {(item["kind"], item["id"]): item["revision"] for item in selection.get("items", [])}
    excluded = {(item["kind"], item["id"]) for item in selection.get("exclude", [])}
    query = selection.get("query", _query({}))
    digest = hashlib.sha256()
    matched, size, changes = 0, 0, []
    for before in _rows(store, query, list(explicit) if explicit else None):
        identity = before["kind"], before["id"]
        if (explicit and identity not in explicit) or identity in excluded:
            continue
        if explicit and explicit[identity] != before["revision"]:
            raise MemoryEditError(
                "A selected memory changed. Refresh and preview again.", "stale_memory_preview", 409
            )
        matched += 1
        size += len(_json(before).encode())
        _check_selection_size(matched, size)
        digest.update(_json((identity, before["revision"])).encode())
        after = _after(store, before, operation)
        if operation["type"] == "set":
            pending = store.db.execute(
                "SELECT id FROM memory_revisions WHERE record_id = ? AND status = 'conflict' "
                "AND base_revision = ? ORDER BY id",
                (before["metadata"]["record_id"], before["metadata"]["revision"]),
            ).fetchall()
            # A proposal arriving after review must not be silently dismissed.
            digest.update(_json([row["id"] for row in pending]).encode())
            if pending and after == before:
                after = dict(after, _resolution=True)
        if after != before:
            changes.append((before, after))
    if explicit and matched != len(explicit):
        raise MemoryEditError(
            "A selected memory is no longer active. Refresh and preview again.",
            "stale_memory_preview",
            409,
        )
    if operation["type"] == "set" and matched != 1:
        raise MemoryEditError("Single-record correction requires exactly one memory.")
    return digest.hexdigest(), matched, changes


def _encode(payload: dict, secret: bytes) -> str:
    data = base64.urlsafe_b64encode(_json(payload).encode()).rstrip(b"=")
    signature = hmac.new(secret, data, hashlib.sha256).hexdigest().encode()
    return (data + b"." + signature).decode()


def _preview_offset(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value >= MAX_BATCH_ROWS
        or value % PREVIEW_ROWS
    ):
        raise MemoryEditError(
            "Preview offset must identify a 25-record page.",
            "invalid_memory_preview_offset",
            400,
        )
    return value


def _preview_response(
    token: str,
    expires: int,
    matched: int,
    changes: list,
    offset: int,
) -> dict:
    return {
        "preview_id": token,
        "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
        "matched_count": matched,
        "changed_count": len(changes),
        "unchanged_count": matched - len(changes),
        "entries": [
            {
                "before": before,
                "after": (
                    {key: value for key, value in after.items() if key != "_resolution"}
                    if after is not None
                    else None
                ),
                "operation": (
                    "forget"
                    if after is None
                    else "resolve" if after.get("_resolution") else "correct"
                ),
            }
            for before, after in changes[offset : offset + PREVIEW_ROWS]
        ],
        "preview_offset": offset,
        "preview_limit": PREVIEW_ROWS,
        "preview_has_more": offset + PREVIEW_ROWS < len(changes),
        "warnings": [],
    }


def preview_edit(store: Any, store_name: str, secret: bytes, body: dict) -> dict:
    paging = "preview_id" in body
    if paging:
        if set(body) - {"store", "preview_id", "offset"}:
            raise MemoryEditError(
                "Preview pages use only the signed preview and page offset.",
                "invalid_memory_preview_page",
                400,
            )
        payload = _decode(body.get("preview_id"), secret, store_name)
        selection, operation = payload["selection"], payload["operation"]
        offset = _preview_offset(body.get("offset"))
    else:
        selection, operation = _selection(body.get("selection")), _operation(body.get("operation"))
        offset = 0
    with store._db_lock:
        digest, matched, changes = _collect(store, selection, operation)
    if paging:
        if digest != payload["digest"]:
            raise MemoryEditError(
                "The matching memories changed. Review a fresh preview before applying.",
                "stale_memory_preview",
                409,
            )
        if offset and offset >= len(changes):
            raise MemoryEditError(
                "That preview page does not exist.",
                "invalid_memory_preview_offset",
                400,
            )
        token, expires = body["preview_id"], payload["expires"]
    else:
        expires = int(time.time()) + PREVIEW_TTL
        token = _encode(
            {
                "store": store_name,
                "selection": selection,
                "operation": operation,
                "digest": digest,
                "expires": expires,
            },
            secret,
        )
    return _preview_response(token, expires, matched, changes, offset)


def _decode(token: Any, secret: bytes, store_name: str) -> dict:
    try:
        if not isinstance(token, str) or len(token) > 256000:
            raise ValueError()
        data, signature = token.encode().rsplit(b".", 1)
        if not hmac.compare_digest(
            hmac.new(secret, data, hashlib.sha256).hexdigest().encode(), signature
        ):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4)))
        if payload["store"] != store_name:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise MemoryEditError(
            "This preview is invalid. Generate a new preview.", "invalid_memory_preview", 400
        ) from None
    if payload["expires"] < time.time():
        raise MemoryEditError(
            "This preview expired. Preview your edits again.", "expired_memory_preview", 409
        )
    return payload


def _write(store: Any, before: dict, after: dict | None, now: str) -> None:
    from kiro_crew.memory_record_metadata import sync_record

    episode = before["kind"] == "episode"
    lineage = store._lineage
    relation = (
        memory_schema.episodic_relation(lineage)
        if episode
        else memory_schema.semantic_relation(lineage)
    )
    guard = (
        memory_schema.episodic_guard(lineage) if episode else memory_schema.semantic_guard(lineage)
    )
    identity = "id" if episode else "key"
    record_id = before["id"]
    crew = lineage == memory_schema.LINEAGE_CREW
    read_relation = "episodic_memories" if episode else "semantic_memory"
    physical_before = dict(
        store.db.execute(
            f"SELECT * FROM {read_relation} WHERE {identity} = ?", (record_id,)
        ).fetchone()
    )
    resolving = bool(after and after.get("_resolution"))
    if resolving:
        pass  # Explicit review preserves the current content, source and embedding.
    elif after is None:
        store.db.execute(
            f"UPDATE {relation} SET is_deleted = 1 WHERE {identity} = ?{guard}", (record_id,)
        )
    elif episode:
        extra = ", source = 'user_explicit', updated_at = ?" if crew else ""
        params = (after["text"], now, record_id) if crew else (after["text"], record_id)
        store.db.execute(
            f"UPDATE {relation} SET text = ?, embedding = NULL{extra} WHERE id = ?{guard}", params
        )
    else:
        extra = ", text = ?" if crew else ""
        params = (after["value_json"], now)
        if crew:
            params += (after["value_json"],)
        store.db.execute(
            f"UPDATE {relation} SET value_json = ?, confidence = 1.0, source = 'user_explicit', "
            f"updated_at = ?, embedding = NULL{extra} WHERE key = ?{guard}",
            params + (record_id,),
        )
    store.db.execute(
        "INSERT INTO memory_events (event_type, memory_type, memory_key, old_value, new_value, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "delete" if after is None else "resolve" if resolving else "correct",
            "episodic" if episode else "semantic",
            record_id,
            before["text"],
            after["text"] if after else None,
            "user_explicit",
            now,
        ),
    )
    physical_after = dict(
        store.db.execute(
            f"SELECT * FROM {read_relation} WHERE {identity} = ?", (record_id,)
        ).fetchone()
    )
    sync_record(
        store.db,
        kind=before["kind"],
        record_id=record_id,
        before=physical_before,
        after=physical_after,
        source="user_explicit",
        now=now,
        operation="forget" if after is None else "resolve" if resolving else "correct",
        force_revision=resolving,
        limit_v1_history=store.algorithm_version == "v1",
    )


def apply_edit(store: Any, store_name: str, secret: bytes, token: Any) -> dict:
    payload = _decode(token, secret, store_name)
    receipt_key = "bulk_edit_receipt:" + hashlib.sha256(token.encode()).hexdigest()
    with store._db_lock:
        store.db.execute("BEGIN IMMEDIATE")
        try:
            receipt = store.db.execute(
                "SELECT value FROM memory_meta WHERE key = ?", (receipt_key,)
            ).fetchone()
            if receipt is not None:
                store.db.rollback()
                return json.loads(receipt["value"])
            digest, _, changes = _collect(store, payload["selection"], payload["operation"])
            if digest != payload["digest"]:
                raise MemoryEditError(
                    "The matching memories changed. Review a fresh preview before applying.",
                    "stale_memory_preview",
                    409,
                )
            now = datetime.now(timezone.utc).isoformat()
            for before, after in changes:
                _write(store, before, after, now)
            result = {"ok": True, "changed_count": len(changes)}
            store.db.execute(
                "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, ?)",
                (receipt_key, _json(result), now),
            )
            cutoff = datetime.fromtimestamp(time.time() - PREVIEW_TTL, timezone.utc).isoformat()
            store.db.execute(
                "DELETE FROM memory_meta WHERE key GLOB 'bulk_edit_receipt:*' AND updated_at < ?",
                (cutoff,),
            )
            store.db.commit()
        except Exception:
            store.db.rollback()
            raise
        if any(
            before["kind"] == "episode" and not (after and after.get("_resolution"))
            for before, after in changes
        ):
            store.invalidate_episode_content()
    return result
