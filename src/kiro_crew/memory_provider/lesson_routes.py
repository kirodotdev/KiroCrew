"""The ``/api/lessons`` routes when a provider replaces Crew's memory.

``learn_add``, ``learn_list``, ``learn_remove`` and ``learn_update`` keep their names,
schemas and answers; the handlers in ``dashboard/handlers/cron_lessons.py`` run every
gate they always ran (session recognition, the restricted-mode refusal, the
persistence switches, schema validation, the store owner gate) and hand the resolved
call here. A ``rule`` item is a standing correction and a ``finding`` an on-topic
one, so the lesson vocabulary maps one to one.

Crew's content screens run before the provider sees a rule (the volatile-fact refusal
and the injection scan), and the answer the provider gives is the answer the caller
reads: ``inserted``, ``enriched``, ``unchanged``, ``deduped`` or ``refused`` with its
reason. A provider that cannot answer is ``refused`` with reason
``provider_unavailable``, so the agent can retry; nothing is written to Crew's own
stores in its place. ``lessons.changed`` and the dashboard refresh are emitted by Crew
from each result.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import replace
from typing import Any

from aiohttp import web

from kiro_crew.memory_provider import render
from kiro_crew.memory_provider.chokepoints import lesson_idempotency_key
from kiro_crew.memory_provider.contract import (
    LESSON_KINDS,
    MemoryItem,
    NewItem,
    ProviderConflict,
    ProviderError,
    ProviderScope,
    new_item_of,
)
from kiro_crew.memory_provider.recall import _execution
from kiro_crew.memory_provider.resolver import MemoryRoute
from kiro_crew.project_scope import canonical_scope

logger = logging.getLogger("kiro_crew.memory_provider")

UNAVAILABLE = "provider_unavailable"
#: More lessons than one scan reads, so a change could miss a match.
TOO_MANY = "too_many_lessons"
#: The provider gives lessons no revision, so a tier change could overwrite an edit.
NO_REVISION = "no_revision"
_LIST_SCAN_MAX = 1_000


async def _scope(route: MemoryRoute, store: str, session_key: str) -> ProviderScope | None:
    import asyncio

    execution = await asyncio.to_thread(_execution, session_key)
    return render.scope_for(store, session_key=session_key, execution_context=execution)


def _item(rule: str, *, applies: str | None, category: str, negative: str | None,
          repo_scope: str | None, written_by: Any = None) -> NewItem:  # fmt: skip
    from kiro_crew.memory_provider.builtin import written_by_meta

    meta = {"crew.category": category}
    if negative:
        meta["negative"] = negative
    if applies:
        meta["applies"] = applies
    # The server-derived writer stamp, as Crew's own lesson row keeps it.
    stamp = written_by_meta(written_by)
    if stamp:
        meta["crew.written_by"] = stamp
    return NewItem(
        kind="finding" if applies == "on_topic" else "rule",
        text=rule,
        scopes=(f"repo:{repo_scope}",) if repo_scope else (),
        meta=meta,
    )


def _respond(body: dict[str, Any]) -> web.Response:
    """Every lesson answer: a provider's rules, reasons and ids pass the memory redactor."""
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    return web.json_response(_redact_memory_field(body))


def _refused(reason: str) -> web.Response:
    return _respond({"ok": False, "outcome": "refused", "reason": reason, "superseded": []})


def _emit(state: Any, store: str, action: str | None) -> None:
    state.push_refresh("lessons")
    if action is None or store:
        return
    from kiro_crew.lesson_events import LESSON_SOURCE_USER, emit_lessons_changed

    emit_lessons_changed(action, LESSON_SOURCE_USER)


async def create(
    state: Any,
    route: MemoryRoute,
    *,
    store: str,
    session_key: str,
    rule: str,
    category: str,
    negative: str | None,
    repo_scope: str | None,
    applies: str | None,
    written_by: Any = None,
) -> web.Response:
    from kiro_crew.learn import lesson_contains_injection
    from kiro_crew.lesson_events import lesson_write_action
    from kiro_crew.lesson_validation import contains_volatile_lesson_fact
    from kiro_crew.vector_memory_runtime.write_outcomes import SemanticRejectCode

    if contains_volatile_lesson_fact(rule, negative):
        return _refused("volatile_session_fact")
    if lesson_contains_injection(rule, negative):
        return _refused(SemanticRejectCode.INJECTION.value)
    scope = await _scope(route, store, session_key)
    if scope is None or scope.memory_mode != "persistent":
        return web.json_response(
            {
                "error": "Memory writes are not allowed in this session mode.",
                "code": "restricted_session",
            },
            status=403,
        )
    if route.provider is None:
        return _refused(UNAVAILABLE)
    item = _item(
        rule,
        applies=applies,
        category=category,
        negative=negative,
        repo_scope=repo_scope,
        written_by=written_by,
    )
    from kiro_crew.memory_provider.migration import redact_model_written

    # An agent's lesson is the model's words: a secret it repeats never leaves for a provider.
    item = redact_model_written(item)
    try:
        # One key per call: a provider replays an applied key, so a key reused after a
        # learn_remove would answer the re-add with the old insert and store nothing.
        # The provider's own dedup still answers a lesson sent twice as ``deduped``.
        key = f"{lesson_idempotency_key(rule, repo_scope)}:{secrets.token_hex(8)}"
        result = await route.provider.put(scope, item, key)
    except ProviderError as exc:
        logger.warning("memory provider learn_add not accepted: %s", exc.code)
        return _refused(UNAVAILABLE if exc.code in ("unavailable", "invalid") else exc.message)
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    superseded = _redact_memory_field(list(result.superseded))
    # A replayed key is the same lesson sent again: to the agent nothing changed.
    outcome = "unchanged" if result.replayed and result.stored else result.outcome
    _emit(state, store, lesson_write_action(outcome, bool(superseded)))
    response: dict[str, Any] = {
        "ok": result.stored,
        "outcome": outcome,
        "reason": result.reason,
        "superseded": superseded,
        "provider": route.provider_id,
    }
    if result.winner:
        response["winner"] = _redact_memory_field(result.winner)
        response["winner_repo_scope"] = None
    return _respond(response)


async def _lessons(route: MemoryRoute, scope: ProviderScope) -> tuple[list[MemoryItem], bool]:
    """Every lesson item the provider lists, up to the scan bound, and whether it stopped short."""
    assert route.provider is not None
    items: list[MemoryItem] = []
    cursor: str | None = None
    while True:
        page = await route.provider.list(scope, cursor, LESSON_KINDS, "active")
        items.extend(i for i in page.items if i.kind in LESSON_KINDS)
        cursor = page.cursor
        if not cursor or len(items) >= _LIST_SCAN_MAX:
            return items[:_LIST_SCAN_MAX], bool(cursor)


def _repo_scope(item: MemoryItem) -> str | None:
    for scope in item.scopes:
        if scope.startswith("repo:"):
            return scope[len("repo:") :] or None
    return None


async def list_page(
    route: MemoryRoute, *, store: str, session_key: str, limit: int, offset: int
) -> web.Response:
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    scope = await _scope(route, store, session_key)
    body: dict[str, Any] = {"limit": limit, "offset": offset, "provider": route.provider_id}
    if scope is None:
        return _respond(
            {**body, "lessons": [], "total": 0, "truncated": False,
             "lessons_withheld": "memory_reads_disabled"}  # fmt: skip
        )
    if route.provider is None:
        return _respond(
            {**body, "lessons": [], "total": 0, "truncated": False, "lessons_withheld": UNAVAILABLE}
        )
    try:
        items, more = await _lessons(route, scope)
    except ProviderError as exc:
        logger.warning("memory provider learn_list failed: %s", exc.code)
        return _respond(
            {**body, "lessons": [], "total": 0, "truncated": False, "lessons_withheld": UNAVAILABLE}
        )
    window = items[offset : offset + limit]
    rows = []
    for item in window:
        row: dict[str, Any] = {
            "rule": _redact_memory_field(item.text),
            "category": _redact_memory_field(item.meta.get("crew.category", "knowledge")),
            "ts": item.updated_at or item.created_at or "",
            "repo_scope": _repo_scope(item),
            "id": item.id,
        }
        if item.kind == "finding":
            row["applies"] = "on_topic"
        rows.append(row)
    return _respond(
        {
            **body,
            "lessons": rows,
            "total": len(items),
            "truncated": more or len(window) < len(items),
        }
    )


def _matches(
    item: MemoryItem, rule: str, repo_scope: str | None, exact: bool, scoped: bool
) -> bool:
    text_ok = item.text == rule if exact else rule.casefold() in item.text.casefold()
    return text_ok and (
        not scoped or canonical_scope(_repo_scope(item)) == canonical_scope(repo_scope)
    )


#: Meta keys that record where a row came from, not what the lesson says.
_PROVENANCE_META = frozenset(
    {"crew.source", "crew.confidence", "crew.session_key", "crew.derived_from", "crew.written_by"}
)


def _copy_identity(item: MemoryItem) -> tuple[Any, ...]:
    """What makes two items copies of one lesson for ``keep_one``.

    Everything the lesson says (kind, text, title, scopes, status, clause and other
    meta), never its id, timestamps or provenance: an ``always`` rule and an
    ``on_topic`` finding with the same text are two lessons and both stay.
    """
    meta = tuple(sorted((k, v) for k, v in item.meta.items() if k not in _PROVENANCE_META))
    return (item.kind, item.text, item.title, tuple(sorted(item.scopes)), item.status, meta)


async def delete(
    state: Any,
    route: MemoryRoute,
    *,
    store: str,
    session_key: str,
    rule: str,
    repo_scope: str | None,
    scoped: bool,
    exact: bool,
    keep_one: bool = False,
) -> web.Response:
    """Remove matching lessons; *keep_one* removes only the extra copies of each text."""
    from kiro_crew.lesson_events import LESSON_REMOVED

    scope = await _scope(route, store, session_key)
    if scope is None or route.provider is None:
        return _respond({"ok": False, "reason": UNAVAILABLE})
    try:
        items, more = await _lessons(route, scope)
        if more:
            # A match past the scan bound would stay while the call reports success.
            return _respond({"ok": False, "reason": TOO_MANY})
        removed = 0
        kept: set[tuple[Any, ...]] = set()
        for item in items:
            if _matches(item, rule, repo_scope, exact, scoped):
                copy = _copy_identity(item)
                if keep_one and copy not in kept:
                    kept.add(copy)
                    continue
                result = await route.provider.delete(scope, item.id)
                removed += result.outcome == "deleted"
    except ProviderError as exc:
        logger.warning("memory provider learn_remove failed: %s", exc.code)
        return _respond({"ok": False, "reason": UNAVAILABLE})
    if removed:
        _emit(state, store, LESSON_REMOVED)
    return _respond({"ok": bool(removed)})


async def update(
    state: Any,
    route: MemoryRoute,
    *,
    store: str,
    session_key: str,
    rule: str,
    repo_scope: str | None,
    current: str,
    applies: str,
) -> web.Response:
    scope = await _scope(route, store, session_key)
    if scope is None or scope.memory_mode != "persistent" or route.provider is None:
        return _respond({"ok": False, "outcome": "unsupported", "reason": UNAVAILABLE})
    try:
        items, more = await _lessons(route, scope)
        if more:
            return _respond({"ok": False, "outcome": "unsupported", "reason": TOO_MANY})
        found = [i for i in items if _matches(i, rule, repo_scope, True, repo_scope is not None)]
        if not found:
            return _respond({"ok": False, "outcome": "missing"})
        # As the builtin store answers: two rows with this text are not guessed
        # between, and a tier the caller did not read is not overwritten.
        if len(found) > 1:
            return _respond({"ok": False, "outcome": "ambiguous"})
        target = found[0]
        if (target.kind == "finding") != (current == "on_topic"):
            return _respond({"ok": False, "outcome": "conflict"})
        if ("finding" if applies == "on_topic" else "rule") == target.kind:
            return _respond({"ok": True, "outcome": "unchanged"})
        if target.revision is None:
            # Without a revision to write against, an edit landing after the read
            # would be overwritten with the text read here.
            return _respond({"ok": False, "outcome": "unsupported", "reason": NO_REVISION})
        # Only the tier changes: origin, source, title, timestamps and every other
        # meta key the item carries are kept, so a later copy can still trace it.
        item = replace(
            new_item_of(target),
            kind="finding" if applies == "on_topic" else "rule",
            meta={**target.meta, "applies": applies},
        )
        key = (
            "crew-tier:"
            + hashlib.sha256(
                f"{target.id}\0{applies}\0{secrets.token_hex(16)}".encode()
            ).hexdigest()
        )
        result = await route.provider.put(
            scope, item, key, target.id, expected_revision=target.revision
        )
    except ProviderConflict:
        return _respond({"ok": False, "outcome": "conflict"})
    except ProviderError as exc:
        logger.warning("memory provider learn_update failed: %s", exc.code)
        return _respond({"ok": False, "outcome": "unsupported", "reason": UNAVAILABLE})
    state.push_refresh("lessons")
    return _respond({"ok": result.stored, "outcome": "updated" if result.stored else "refused"})
