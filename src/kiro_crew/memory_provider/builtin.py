"""Crew's own memory, spoken through the provider contract.

A thin adapter over the functions every built-in path already calls: lessons are
``lesson.*`` rows (``write_lesson`` / ``delete_lesson``), facts and preferences are
the other semantic rows (``set_semantic`` / ``delete_semantic``), episodes are
episodic rows (``write_episodic`` / ``delete_episodic``). The prompt builders and
the recall route keep calling those functions directly when Crew's memory answers,
so the default path is the one it always was; this adapter serves what needs one
contract for every provider: the host item routes, migrations, export and import,
and the conformance kit.

Item ids name the row they came from -- ``lesson:<key>``, ``fact:<key>``,
``episode:<id>``, ``doc:preferences`` / ``doc:projects`` (Global V1's Markdown documents,
one ``document`` item each) and ``doc:history.<date>`` (one ``summary`` per history day) --
so ``get`` and ``delete`` reach the same row ``list`` showed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from kiro_crew.memory_provider.contract import (
    KINDS,
    LESSON_KINDS,
    MAX_BATCH,
    MAX_DOCUMENT_TEXT,
    PROVIDER_API_VERSION,
    BlockingProvider,
    Capabilities,
    Health,
    MemoryItem,
    NewItem,
    Page,
    ProviderConflict,
    ProviderNotSupported,
    ProviderRefused,
    ProviderScope,
    ProviderUnavailable,
    Revision,
    WriteResult,
    truncate_text,
)

logger = logging.getLogger("kiro_crew.memory_provider")

_LESSON_PREFIX = "lesson:"
_FACT_PREFIX = "fact:"
_EPISODE_PREFIX = "episode:"
#: ``jsonl`` is Global V1's lessons file and ``document`` its Markdown memory
#: (preferences, projects, history); both are authoritative beside the index.
_SECTIONS = ("lesson", "jsonl", "fact", "episode", "document")
_DOCUMENT_PREFIX = "doc:"
_JSONL_PREFIX = "lesson:jsonl."
_HISTORY_PREFIX = "doc:history."
#: Global V1's two Markdown documents, in the order a page lists them.
_DOCUMENT_NAMES_ORDERED = ("preferences", "projects")
_EPISODE_KINDS = frozenset(("episode", "summary", "note"))
_FACT_KINDS = frozenset(("preference", "fact", "index"))

CAPABILITIES = Capabilities(
    api_version=PROVIDER_API_VERSION,
    provider_name="Crew memory",
    provider_version="",
    egress="local",
    read_only=False,
    # ``context`` is listed because Crew's prompt builders render this memory
    # themselves; nothing calls the adapter's ``context``.
    ops=frozenset(("context", "search", "get", "list", "put", "delete", "put_batch", "revisions")),
    kinds=frozenset(KINDS),
    search_lexical=True,
    search_semantic=True,
    search_filters=frozenset(("kind",)),
    verbatim=True,
    preserves_timestamps=False,
    keeps_origin=False,
    idempotent=True,
    max_batch=MAX_BATCH,
    max_text=MAX_DOCUMENT_TEXT,
    revisions=True,
    ingest="items",
    multi_store=True,
    documents="readwrite",
    max_page=100,
)


def _timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _decoded(value: object) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _provenance(row: dict, *, confidence: bool = True) -> dict[str, str]:
    """The row's provenance in the reserved ``crew.*`` meta keys."""
    meta: dict[str, str] = {}
    source = row.get("source")
    if isinstance(source, str) and source:
        meta["crew.source"] = source
    if confidence and isinstance(row.get("confidence"), (int, float)):
        meta["crew.confidence"] = str(row["confidence"])
    for column, key in (
        ("session_key", "crew.session_key"),
        ("derived_from", "crew.derived_from"),
        ("written_by", "crew.written_by"),
    ):
        value = row.get(column)
        if isinstance(value, str) and value:
            meta[key] = value
    return meta


def _lesson_meta(value: dict) -> dict[str, str]:
    meta: dict[str, str] = {}
    if value.get("category") is not None:
        meta["crew.category"] = str(value["category"])
    if value.get("negative"):
        meta["negative"] = str(value["negative"])
    return meta


def _lesson_item(row: dict) -> MemoryItem | None:
    value = _decoded(row.get("value_json", row.get("value")))
    if not isinstance(value, dict) or not isinstance(value.get("rule"), str):
        return None
    scopes = (f"repo:{value['repo_scope']}",) if value.get("repo_scope") else ()
    kind = "finding" if value.get("applies") == "on_topic" else "rule"
    return MemoryItem(
        id=_LESSON_PREFIX + str(row.get("key", "")),
        kind=kind,
        text=truncate_text(value["rule"]),
        scopes=scopes,
        source=row.get("source") if isinstance(row.get("source"), str) else None,
        created_at=_timestamp(row.get("created_at")),
        updated_at=_timestamp(row.get("updated_at")),
        meta={**_provenance(row), **_lesson_meta(value)},
    )


def _fact_item(row: dict) -> MemoryItem:
    key = str(row.get("key", ""))
    value = _decoded(row.get("value_json", row.get("value")))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return MemoryItem(
        id=_FACT_PREFIX + key,
        kind="preference" if key.startswith("pref.") else "fact",
        text=truncate_text(text),
        title=key,
        source=row.get("source") if isinstance(row.get("source"), str) else None,
        created_at=_timestamp(row.get("created_at")),
        updated_at=_timestamp(row.get("updated_at")),
        meta=_provenance(row),
    )


def _episode_item(row: dict) -> MemoryItem:
    tags = row.get("tags")
    meta = {"tags": ",".join(tags)} if isinstance(tags, list) and tags else {}
    meta.update(_provenance(row, confidence=False))
    if isinstance(row.get("importance"), (int, float)):
        meta["crew.importance"] = str(row["importance"])
    conversation = row.get("conversation_id")
    if isinstance(conversation, str) and conversation:
        meta.setdefault("crew.session_key", conversation)
    created = _timestamp(row.get("created_at") or row.get("timestamp"))
    return MemoryItem(
        id=_EPISODE_PREFIX + str(row.get("id", "")),
        kind="episode",
        text=truncate_text(str(row.get("text", ""))),
        source=conversation if isinstance(conversation, str) else None,
        created_at=created,
        updated_at=created,
        meta=meta,
    )


def _jsonl_item(index: int, lesson: Any) -> MemoryItem:
    scopes = (f"repo:{lesson.repo_scope}",) if lesson.repo_scope else ()
    created = _timestamp(lesson.ts)
    return MemoryItem(
        id=f"{_JSONL_PREFIX}{index}",
        kind="finding" if lesson.applies == "on_topic" else "rule",
        text=truncate_text(lesson.rule),
        scopes=scopes,
        created_at=created,
        updated_at=created,
        meta=_lesson_meta({"category": lesson.category, "negative": lesson.negative}),
    )


def _day_timestamp(day: str) -> float | None:
    try:
        return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _document_items(snapshot: dict) -> list[MemoryItem]:
    """Global V1's Markdown memory: one ``document`` per file, one ``summary`` per day.

    A document longer than the contract's document cap is cut and marked, so a move
    can see it was not whole; the plan reports it as truncated.
    """
    items: list[MemoryItem] = []
    for name in _DOCUMENT_NAMES_ORDERED:
        entry = snapshot.get(name) or {}
        text = entry.get("content") or ""
        if not text.strip():
            continue
        updated = _timestamp(entry.get("updated_at"))
        items.append(
            MemoryItem(
                id=f"{_DOCUMENT_PREFIX}{name}",
                kind="document",
                text=truncate_text(text, MAX_DOCUMENT_TEXT),
                title=name,
                created_at=updated,
                updated_at=updated,
                meta={"crew.doc": name},
            )
        )
    for entry in snapshot.get("history") or []:
        day = str(entry.get("date") or "")
        text = entry.get("content") or ""
        if not day or not text.strip():
            continue
        items.append(
            MemoryItem(
                id=f"{_HISTORY_PREFIX}{day}",
                kind="summary",
                text=truncate_text(text),
                title=day,
                created_at=_day_timestamp(day),
                updated_at=_timestamp(entry.get("updated_at")) or _day_timestamp(day),
                meta={"crew.source": "history"},
            )
        )
    return items


def _newer_than(item: MemoryItem, updated_after: float | None) -> bool:
    if updated_after is None:
        return True
    stamp = item.updated_at if item.updated_at is not None else item.created_at
    return stamp is not None and stamp > updated_after


def _lesson_scope(item: NewItem) -> str | None:
    for scope in item.scopes:
        if scope.startswith("repo:"):
            return scope[len("repo:") :] or None
    return None


def _fact_key(item: NewItem, vectors: Any) -> str:
    """The item's own key when the store would accept it, else a namespaced digest.

    A provider's title is display text (``Timezone``), so it is reused only when it
    passes the store's own key and prefix checks.
    """
    explicit = item.meta.get("key") or item.title
    if (
        explicit
        and not explicit.startswith("system.")
        and vectors._validate_key(explicit) is None
        and vectors._matches_allowlist(explicit)
    ):
        return explicit
    digest = hashlib.sha256(item.text.encode("utf-8")).hexdigest()[:16]
    return ("pref." if item.kind == "preference" else "user.") + digest


#: ``set_semantic_if_absent``'s answers in the contract's write vocabulary.
_ABSENT_OUTCOMES = {"imported": "inserted", "existing": "unchanged"}


def _merge_lesson(vectors: Any, item: NewItem, store: str) -> WriteResult:
    """Add a lesson without retiring any: a copy or import never changes Crew's own.

    ``write_lesson`` supersedes overlapping rows, which is right for a user's own
    correction and wrong for memory arriving from elsewhere, so an overlap is
    reported as ``deduped`` and only a new rule is written, by the absent-only
    writer, as the onboarding import does.
    """
    from kiro_crew.onboarding_import import _lessons_overlap
    from kiro_crew.vector_memory import _lesson_key

    indexed = vectors.get_lessons()
    for row in indexed:
        stored = _lesson_item(row)
        if (
            stored is not None
            and _same_scope(_lesson_scope(item), _scope_of(stored))
            and _lessons_overlap(item.text, stored.text)
        ):
            return WriteResult("deduped", reason="overlaps_existing", winner=stored.text)
    repo_scope = _lesson_scope(item)
    if not indexed and getattr(vectors, "algorithm_version", "") != "v2":
        file_result = _merge_file_lesson(item, repo_scope, store)
        if file_result is not None:
            return file_result
    value: dict[str, object] = {
        "rule": item.text,
        "category": item.meta.get("crew.category", "knowledge"),
        "negative": item.meta.get("negative") or None,
        "applies": "on_topic" if item.kind == "finding" else "always",
    }
    if repo_scope:
        value["repo_scope"] = repo_scope
    key = _lesson_key(item.text, repo_scope)
    # The onboarding import's source: memory from elsewhere never claims the authority
    # of a rule the person taught Crew directly.
    outcome = vectors.set_semantic_if_absent(key, value, 1.0, "import")
    return WriteResult(_ABSENT_OUTCOMES.get(outcome, "refused"), id=_LESSON_PREFIX + key)


def _scope_of(item: MemoryItem) -> str | None:
    return _lesson_scope(NewItem(kind=item.kind, text="", scopes=item.scopes))


def _same_scope(left: str | None, right: str | None) -> bool:
    """Whether two lessons address the same repository (both unscoped counts as one)."""
    from kiro_crew.project_scope import canonical_scope

    return canonical_scope(left) == canonical_scope(right)


def _merge_file_lesson(item: NewItem, repo_scope: str | None, store: str) -> WriteResult | None:
    """Add to Global V1's lessons file while it is still the store's lesson authority.

    The index answers for lessons only once it holds one, so writing the first
    indexed lesson would hide every lesson that lives only in the file. ``None``
    when the file holds none, so the caller writes to the index.
    """
    from datetime import datetime, timezone

    from kiro_crew.context import ContextBuilder
    from kiro_crew.learn import Lesson
    from kiro_crew.onboarding_import import _lessons_overlap

    lessons = ContextBuilder.get_lessons_for(memory_store=store or None)
    existing = lessons.load_all()
    if not existing:
        return None
    for lesson in existing:
        if _same_scope(repo_scope, lesson.repo_scope) and _lessons_overlap(item.text, lesson.rule):
            return WriteResult("deduped", reason="overlaps_existing", winner=lesson.rule)
    from kiro_crew.learn import _MAX_LESSONS_TOTAL

    # ``save`` evicts an old lesson at the cap; memory arriving from elsewhere never does.
    if len(existing) >= _MAX_LESSONS_TOTAL:
        return WriteResult("refused", reason="at_capacity")
    outcome = lessons.save(
        Lesson(
            ts=datetime.now(timezone.utc).isoformat(),
            rule=item.text,
            category=item.meta.get("crew.category", "knowledge"),
            negative=item.meta.get("negative") or None,
            repo_scope=repo_scope,
            applies="on_topic" if item.kind == "finding" else "always",
        )
    )
    return WriteResult(outcome if outcome in ("inserted", "unchanged") else "refused")


class BuiltinMemoryProvider(BlockingProvider):
    """Crew's own memory for whichever store a call's scope names."""

    provider_id = "builtin"
    display_name = "Crew memory"

    def capabilities(self) -> Capabilities:
        return CAPABILITIES

    def blocking_health(self) -> Health:
        return Health(ok=True)

    def _vectors(self, scope: ProviderScope) -> Any:
        from kiro_crew.context import ContextBuilder

        store = "" if scope.store_id in ("", "default") else scope.store_id
        memory = ContextBuilder.get_memory_for(memory_store=store or None)
        vectors = memory.vector_store
        if vectors is None:
            raise ProviderUnavailable("Crew's memory index is not ready")
        return vectors

    def blocking_context(
        self, scope: ProviderScope, query: str, budget_chars: int
    ) -> list[MemoryItem]:
        # The prompt builders render Crew's own memory through its own renderers.
        raise ProviderNotSupported("context")

    def blocking_search(
        self, scope: ProviderScope, query: str, limit: int, kinds: frozenset[str] | None
    ) -> list[MemoryItem]:
        vectors = self._vectors(scope)
        wanted = kinds or frozenset(KINDS)
        needle = query.casefold().strip()
        found: list[MemoryItem] = []
        if wanted & LESSON_KINDS:
            for row in vectors.get_lessons():
                item = _lesson_item(row)
                if item and item.kind in wanted and needle in item.text.casefold():
                    found.append(item)
        if wanted & _FACT_KINDS and len(found) < limit:
            rows = vectors.get_all_semantic(limit=limit, q=query, exclude_lessons=True)
            found.extend(i for i in map(_fact_item, rows) if i.kind in wanted)
        if wanted & _EPISODE_KINDS and len(found) < limit:
            rows = vectors.get_episodic_list(limit=limit, offset=0, tag_filter=None, q=query)
            found.extend(map(_episode_item, rows))
        return found[:limit]

    def _file_item(self, scope: ProviderScope, vectors: Any, item_id: str) -> MemoryItem | None:
        """A listed file-backed item (Global V1's lessons file or a Markdown document) by id."""
        if item_id.startswith(_JSONL_PREFIX):
            rows = self._jsonl_lessons(scope, vectors)
        elif item_id.startswith(_DOCUMENT_PREFIX):
            rows = self._documents(scope, vectors)
        else:
            return None
        return next((row for row in rows if row.id == item_id), None)

    def blocking_get(self, scope: ProviderScope, item_id: str) -> MemoryItem | None:
        vectors = self._vectors(scope)
        if item_id.startswith((_JSONL_PREFIX, _DOCUMENT_PREFIX)):
            return self._file_item(scope, vectors, item_id)
        if item_id.startswith(_LESSON_PREFIX) or item_id.startswith(_FACT_PREFIX):
            key = item_id.split(":", 1)[1]
            row = vectors.get_semantic(key)
            if row is None:
                return None
            return _lesson_item(row) if key.startswith("lesson.") else _fact_item(row)
        if item_id.startswith(_EPISODE_PREFIX):
            wanted = item_id[len(_EPISODE_PREFIX) :]
            for offset in range(0, 10_000, 100):
                rows = vectors.get_episodic_list(limit=100, offset=offset, tag_filter=None)
                for row in rows:
                    if str(row.get("id")) == wanted:
                        return _episode_item(row)
                if len(rows) < 100:
                    break
        return None

    def blocking_list(
        self,
        scope: ProviderScope,
        cursor: str | None,
        kinds: frozenset[str] | None,
        status: str | None,
        updated_after: float | None = None,
    ) -> Page:
        """One page: lessons, then facts, then episodes, then documents.

        The cursor is ``<section>:<offset>``. Crew's own memory lists live rows only, so
        ``status=retired`` lists nothing and ``all`` lists what ``active`` does.
        """
        if status not in (None, "", "active", "all"):
            return Page(())
        vectors = self._vectors(scope)
        wanted = kinds or frozenset(KINDS)
        section, offset = "lesson", 0
        if cursor:
            name, _, raw = cursor.partition(":")
            if name in _SECTIONS and raw.isdigit():
                section, offset = name, int(raw)
        page = CAPABILITIES.max_page
        while True:
            if section == "lesson" and wanted & LESSON_KINDS:
                rows = vectors.get_lessons(limit=page, offset=offset)
                items = [i for i in map(_lesson_item, rows) if i and i.kind in wanted]
            elif section == "fact" and wanted & _FACT_KINDS:
                rows = vectors.get_all_semantic(limit=page, offset=offset, exclude_lessons=True)
                items = [i for i in map(_fact_item, rows) if i.kind in wanted]
            elif section == "episode" and wanted & _EPISODE_KINDS:
                rows = vectors.get_episodic_list(limit=page, offset=offset, tag_filter=None)
                items = [i for i in map(_episode_item, rows) if i.kind in wanted]
            elif section == "jsonl" and wanted & LESSON_KINDS:
                rows = self._jsonl_lessons(scope, vectors)[offset : offset + page]
                items = [i for i in rows if i.kind in wanted]
            elif section == "document":
                rows = self._documents(scope, vectors)[offset : offset + page]
                items = [i for i in rows if i.kind in wanted]
            else:
                rows, items = [], []
            items = [i for i in items if _newer_than(i, updated_after)]
            if len(rows) == page:
                return Page(tuple(items), f"{section}:{offset + page}")
            following = _SECTIONS.index(section) + 1
            if following >= len(_SECTIONS):
                return Page(tuple(items), None)
            if items:
                return Page(tuple(items), f"{_SECTIONS[following]}:0")
            section, offset = _SECTIONS[following], 0

    def _jsonl_lessons(self, scope: ProviderScope, vectors: Any) -> list[MemoryItem]:
        """Global V1's file lessons the index does not already hold, in file order."""
        if getattr(vectors, "algorithm_version", "") == "v2":
            return []
        from kiro_crew.context import ContextBuilder

        store = "" if scope.store_id in ("", "default") else scope.store_id
        indexed = {i.text for i in map(_lesson_item, vectors.get_lessons()) if i}
        lessons = ContextBuilder.get_lessons_for(memory_store=store or None).load_all()
        return [
            _jsonl_item(n, lesson)
            for n, lesson in enumerate(lessons)
            if truncate_text(lesson.rule) not in indexed
        ]

    def _memory(self, scope: ProviderScope) -> Any:
        from kiro_crew.context import ContextBuilder

        store = "" if scope.store_id in ("", "default") else scope.store_id
        return ContextBuilder.get_memory_for(memory_store=store or None)

    def _documents(self, scope: ProviderScope, vectors: Any) -> list[MemoryItem]:
        """Global V1's Markdown memory as items. A V2 store's anchors are member documents."""
        if getattr(vectors, "algorithm_version", "") == "v2":
            return []
        return _document_items(self._memory(scope).markdown_snapshot())

    def _put_document(self, scope: ProviderScope, item: NewItem, explicit: bool) -> WriteResult:
        """Write one Global V1 Markdown document.

        An explicit edit (``put`` with the item's id) replaces the file. A move or an
        import never overwrites one that differs: it writes an empty file and refuses
        a different one with ``document_differs``, so the plan can show it.
        """
        name = item.meta.get("crew.doc")
        if name not in _DOCUMENT_NAMES_ORDERED:
            raise ProviderRefused('a document item needs meta["crew.doc"]')
        vectors = self._vectors(scope)
        if getattr(vectors, "algorithm_version", "") == "v2":
            raise ProviderNotSupported("a Memory V2 store keeps its documents as member files")
        memory = self._memory(scope)
        current = getattr(memory, f"read_{name}")() or ""
        if current == item.text:
            return WriteResult("unchanged", id=f"{_DOCUMENT_PREFIX}{name}")
        if current.strip() and not explicit:
            return WriteResult("refused", id=f"{_DOCUMENT_PREFIX}{name}", reason="document_differs")
        if not getattr(memory, f"write_{name}")(item.text, expected_baseline=current):
            raise ProviderConflict("the document changed while it was being written")
        outcome = "enriched" if current.strip() else "inserted"
        return WriteResult(outcome, id=f"{_DOCUMENT_PREFIX}{name}")

    def blocking_put(
        self,
        scope: ProviderScope,
        item: NewItem,
        idempotency_key: str,
        item_id: str | None = None,
        *,
        expected_revision: int | None = None,
        dry_run: bool = False,
    ) -> WriteResult:
        if dry_run:
            raise ProviderNotSupported("put with dry_run")
        if item.kind == "document":
            if item_id and item_id != f"{_DOCUMENT_PREFIX}{item.meta.get('crew.doc')}":
                raise ProviderRefused(f"{item_id!r} is not this document's id")
            return self._put_document(scope, item, explicit=item_id is not None)
        vectors = self._vectors(scope)
        if item.kind in LESSON_KINDS:
            if item_id is not None:
                raise ProviderNotSupported("put with item_id on a lesson")
            store = "" if scope.store_id in ("", "default") else scope.store_id
            return _merge_lesson(vectors, item, store)
        if item.kind in _FACT_KINDS:
            if item_id and not item_id.startswith(_FACT_PREFIX):
                raise ProviderRefused(f"{item_id!r} is not a fact id")
            key = item_id[len(_FACT_PREFIX) :] if item_id else _fact_key(item, vectors)
            if not item_id:
                # A move or import is merge-only: a fact already under this key stays.
                outcome = vectors.set_semantic_if_absent(key, item.text, 0.9, "owner")
                return WriteResult(_ABSENT_OUTCOMES.get(outcome, "refused"), id=_FACT_PREFIX + key)
            existed = vectors.get_semantic(key) is not None
            refusal = vectors.set_semantic(key, item.text, 0.9, "owner")
            if refusal is not None:
                code, message = refusal
                return WriteResult("refused", reason=str(getattr(code, "value", code)) or message)
            return WriteResult("enriched" if existed else "inserted", id=_FACT_PREFIX + key)
        if item_id is not None:
            raise ProviderNotSupported("put with item_id on an episode")
        from kiro_crew import vector_memory as vm
        from kiro_crew.vector_memory_runtime.write_outcomes import EpisodicWriteOutcome

        # The store answers REFUSED for bad text and for a duplicate alike, so the
        # text bounds are checked here first and only a duplicate reads as deduped.
        text = item.text.strip()
        if not vm._EPISODIC_TEXT_MIN <= len(text) <= vm._EPISODIC_TEXT_MAX:
            return WriteResult("refused", reason="episode_length")
        # Merge-only, as an import is: a full store refuses the episode instead of
        # retiring one of Crew's own.
        outcome = vectors.write_episodic_outcome(item.text, source="owner", preserve_existing=True)
        if outcome is EpisodicWriteOutcome.WRITTEN:
            return WriteResult("inserted")
        if outcome is EpisodicWriteOutcome.AT_CAPACITY:
            return WriteResult("refused", reason="at_capacity")
        return WriteResult("deduped")

    def blocking_revisions(self, scope: ProviderScope, item_id: str) -> list[Revision]:
        """The row's history from ``memory_revisions``, newest first (at most 200)."""
        if item_id.startswith((_LESSON_PREFIX, _FACT_PREFIX)) and not item_id.startswith(
            _JSONL_PREFIX
        ):
            record_id = "key:" + item_id.split(":", 1)[1]
        elif item_id.startswith(_EPISODE_PREFIX):
            record_id = item_id[len(_EPISODE_PREFIX) :]
        else:
            return []
        vectors = self._vectors(scope)
        try:
            with vectors._db_lock:
                rows = vectors.db.execute(
                    "SELECT revision, operation, source, created_at FROM memory_revisions "
                    "WHERE record_id = ? ORDER BY id DESC LIMIT 200",
                    (record_id,),
                ).fetchall()
        except sqlite3.Error:
            # A store opened before the revisions table existed has no history to show.
            return []
        out: list[Revision] = []
        for row in rows:
            at = _timestamp(row[3])
            if at is None or not isinstance(row[0], int):
                continue
            out.append(Revision(row[0], at, str(row[1] or ""), str(row[2] or "")))
        return out

    def blocking_delete(self, scope: ProviderScope, item_id: str) -> WriteResult:
        vectors = self._vectors(scope)
        if item_id.startswith(_DOCUMENT_PREFIX):
            raise ProviderNotSupported("a Markdown document is edited in Crew's memory editor")
        if item_id.startswith(_JSONL_PREFIX):
            from kiro_crew.context import ContextBuilder

            item = self._file_item(scope, vectors, item_id)
            if item is None:
                return WriteResult("unchanged", id=item_id, reason="not_found")
            store = "" if scope.store_id in ("", "default") else scope.store_id
            lessons = ContextBuilder.get_lessons_for(memory_store=store or None)
            scope_name = _lesson_scope(NewItem(kind="rule", text="", scopes=item.scopes))
            ok = bool(lessons.remove(item.text, scope_name, exact=True))
            return WriteResult("deleted" if ok else "unchanged", id=item_id)
        if item_id.startswith(_LESSON_PREFIX):
            row = vectors.get_semantic(item_id[len(_LESSON_PREFIX) :])
            item = _lesson_item(row) if row else None
            if item is None:
                return WriteResult("unchanged", id=item_id, reason="not_found")
            scope_name = _lesson_scope(NewItem(kind="rule", text="", scopes=item.scopes))
            ok = vectors.delete_lesson(item.text, scope_name, exact=True)
        elif item_id.startswith(_FACT_PREFIX):
            ok = vectors.delete_semantic(item_id[len(_FACT_PREFIX) :], "owner")
        elif item_id.startswith(_EPISODE_PREFIX):
            ok = vectors.delete_episodic(item_id[len(_EPISODE_PREFIX) :])
        else:
            ok = False
        return WriteResult("deleted" if ok else "unchanged", id=item_id)


BUILTIN = BuiltinMemoryProvider()
