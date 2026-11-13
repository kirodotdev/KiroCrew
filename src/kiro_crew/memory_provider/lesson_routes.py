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

import logging
from typing import Any

from aiohttp import web

from kiro_crew.memory_provider import render
from kiro_crew.memory_provider.chokepoints import lesson_idempotency_key
from kiro_crew.memory_provider.contract import (
    LESSON_KINDS,
    MemoryItem,
    NewItem,
    ProviderError,
    ProviderScope,
)
from kiro_crew.memory_provider.recall import _execution
from kiro_crew.memory_provider.resolver import MemoryRoute

logger = logging.getLogger("kiro_crew.memory_provider")

UNAVAILABLE = "provider_unavailable"
_LIST_SCAN_MAX = 1_000


async def _scope(route: MemoryRoute, store: str, session_key: str) -> ProviderScope | None:
    import asyncio

    execution = await asyncio.to_thread(_execution, session_key)
    return render.scope_for(store, session_key=session_key, execution_context=execution)


def _item(rule: str, *, applies: str | None, category: str, negative: str | None,
          repo_scope: str | None) -> NewItem:  # fmt: skip
    meta = {"category": category}
    if negative:
        meta["negative"] = negative
    if applies:
        meta["applies"] = applies
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
    item = _item(rule, applies=applies, category=category, negative=negative, repo_scope=repo_scope)
    try:
        result = await route.provider.put(scope, item, lesson_idempotency_key(rule, repo_scope))
    except ProviderError as exc:
        logger.warning("memory provider learn_add not accepted: %s", exc.code)
        return _refused(UNAVAILABLE if exc.code in ("unavailable", "invalid") else exc.message)
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    superseded = _redact_memory_field(list(result.superseded))
    _emit(state, store, lesson_write_action(result.outcome, bool(superseded)))
    response: dict[str, Any] = {
        "ok": result.stored,
        "outcome": result.outcome,
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
            "category": _redact_memory_field(item.meta.get("category", "knowledge")),
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
    return text_ok and (not scoped or (_repo_scope(item) or "") == (repo_scope or "").strip())


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
        items, _ = await _lessons(route, scope)
        removed = 0
        kept: set[tuple[str, str]] = set()
        for item in items:
            if _matches(item, rule, repo_scope, exact, scoped):
                copy = (item.text, _repo_scope(item) or "")
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
    applies: str,
) -> web.Response:
    scope = await _scope(route, store, session_key)
    if scope is None or scope.memory_mode != "persistent" or route.provider is None:
        return _respond({"ok": False, "outcome": "unsupported", "reason": UNAVAILABLE})
    try:
        items, _ = await _lessons(route, scope)
        target = next(
            (i for i in items if _matches(i, rule, repo_scope, True, repo_scope is not None)), None
        )
        if target is None:
            return _respond({"ok": False, "outcome": "missing"})
        if ("finding" if applies == "on_topic" else "rule") == target.kind:
            return _respond({"ok": True, "outcome": "unchanged"})
        item = _item(
            target.text,
            applies=applies,
            category=target.meta.get("category", "knowledge"),
            negative=target.meta.get("negative"),
            repo_scope=_repo_scope(target),
        )
        result = await route.provider.put(
            scope, item, lesson_idempotency_key(target.text + "\0" + applies, None), target.id
        )
    except ProviderError as exc:
        logger.warning("memory provider learn_update failed: %s", exc.code)
        return _respond({"ok": False, "outcome": "unsupported", "reason": UNAVAILABLE})
    state.push_refresh("lessons")
    return _respond({"ok": result.stored, "outcome": "updated" if result.stored else "refused"})
