"""What each memory chokepoint does when a provider answers instead of Crew's memory.

Every reader and writer of Crew's memory asks :func:`resolver.route_for` first. When
Crew's own memory answers (``route.builtin_answers``) the caller runs exactly the code
it ran before this seam existed; these helpers carry the other branch, so the
callers stay one ``if`` long:

- session start, per-message lessons and the post-compaction copy
  (``context_assembly/session_start.py``, ``store_admission.py``);
- the recall route behind ``memory_recall`` (``dashboard/handlers/memory_member.py``);
- the lesson routes behind ``learn_add`` / ``learn_list`` / ``learn_remove`` /
  ``learn_update`` (``dashboard/handlers/cron_lessons.py``, via
  ``memory_provider/lesson_routes.py``);
- consolidation publication (``history_consolidation_pass.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from typing import Any

from kiro_crew.memory_provider import render
from kiro_crew.memory_provider.contract import (
    NewItem,
    ProviderError,
    TranscriptSpan,
)
from kiro_crew.memory_provider.resolver import BUILTIN_ROUTE, MemoryRoute, route_for

logger = logging.getLogger("kiro_crew.memory_provider")


def session_route(memory_store: str | None, session_key: str | None) -> MemoryRoute:
    """The route a prompt build reads through. Never raises.

    The default config answers before any read, so a fault here can only come from a
    configured provider's session record or app lookup. That fault reads as the
    provider being unavailable, never as Crew's own memory standing in for it.
    """
    try:
        return route_for(memory_store, session_key=session_key)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("memory provider route unreadable", exc_info=True)
        try:
            from kiro_crew.config.loader import KiroCrewConfig
            from kiro_crew.memory_provider.resolver import _chosen

            mode, provider_id = _chosen(KiroCrewConfig.load().memory.provider)
        except Exception:  # noqa: BLE001 - an unreadable config loads as the defaults
            return BUILTIN_ROUTE
        if mode != "replace":
            # Crew's own memory answers in builtin and augment, so a fault drops only
            # the extra source; it never takes Crew's own memory out of the prompt.
            if mode == "augment":
                return MemoryRoute(
                    "augment",
                    provider_id,
                    unavailable="the memory provider could not be resolved for this chat",
                )
            return BUILTIN_ROUTE
        return MemoryRoute(
            "replace",
            provider_id,
            unavailable="the memory provider could not be resolved for this chat",
        )


def replace_session_parts(
    blocks: Any,
    route: MemoryRoute,
    *,
    memory_store: str | None,
    session_key: str | None,
    execution_context: Any,
    project: str | None,
    query_text: str,
    caps: Any,
    include_memory: bool,
    include_lessons: bool,
) -> int | None:
    scope = render.scope_for(
        memory_store,
        session_key=session_key,
        execution_context=execution_context,
        project=project,
    )
    if scope is None:
        return None
    index = render.session_parts(
        blocks,
        route,
        scope=scope,
        query_text=query_text,
        caps=caps,
        include_memory=include_memory,
        include_lessons=include_lessons,
        session_key=session_key,
    )
    if include_memory:
        _crew_documents_parts(blocks, route, memory_store, caps)
    return index


def _crew_documents_parts(
    blocks: Any, route: MemoryRoute, memory_store: str | None, caps: Any
) -> None:
    """Global V1's ``preferences.md`` and ``projects.md`` when the provider does not hold them.

    A move leaves those documents in Crew for a provider without ``documents:
    readwrite`` and says Crew keeps reading them; this is that read. Only the two
    documents: Crew's facts and lessons never stand in for the provider's.
    """
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

    if memory_store not in (None, "", DEFAULT_MEMORY_STORE) or route.provider is None:
        return
    try:
        if route.provider.capabilities().holds_documents:
            return
        from kiro_crew.context import ContextBuilder

        memory = ContextBuilder.get_memory_for(memory_store=DEFAULT_MEMORY_STORE)
        if getattr(getattr(memory, "vector_store", None), "algorithm_version", "") == "v2":
            return
        documents = [
            ("preferences", memory.read_preferences() or ""),
            ("projects", memory.read_projects() or ""),
        ]
    except Exception:  # noqa: BLE001 - a missing document read costs those two blocks only
        logger.debug("Crew documents unreadable beside a memory provider", exc_info=True)
        return
    render.crew_document_parts(blocks, documents, caps)


def augment_session_part(
    blocks: Any,
    route: MemoryRoute,
    *,
    memory_store: str | None,
    session_key: str | None,
    execution_context: Any,
    project: str | None,
    query_text: str,
    caps: Any,
) -> None:
    scope = render.scope_for(
        memory_store,
        session_key=session_key,
        execution_context=execution_context,
        project=project,
    )
    if scope is not None:
        render.augment_part(blocks, route, scope=scope, query_text=query_text, caps=caps)


def compaction_rules(
    route: MemoryRoute,
    session_key: str,
    *,
    memory_store: str | None = None,
    project: str | None = None,
    caps: Any = None,
    query_text: str = "",
) -> str:
    """The rules block for the first turn after a compaction: the one shown at start.

    A session this process never started (resumed slim after a restart) has none
    cached, so the block is read from the provider again.
    """
    if not route.replaces:
        return ""
    cached = render.cached_rules_block(session_key)
    if cached or caps is None:
        return cached
    scope = render.scope_for(memory_store, session_key=session_key, project=project)
    if scope is None:
        return ""
    return render.rebuilt_rules_block(route, scope, query_text, caps, session_key)


def turn_rules(
    route: MemoryRoute,
    *,
    memory_store: str | None,
    session_key: str,
    execution_context: Any,
    project: str | None,
    text: str,
    shown: Any,
) -> list[tuple[str, str]]:
    scope = render.scope_for(
        memory_store,
        session_key=session_key,
        execution_context=execution_context,
        project=project,
    )
    if scope is None:
        return []
    return render.turn_lessons_block(route, scope=scope, text=text, shown=shown)


# ── Consolidation ────────────────────────────────────────────────────────────


def _ingest_key(session_key: str, span_id: str, item: NewItem) -> str:
    """An item's key, from what the item says rather than its position.

    A retried span is extracted again and may list its items in another order, so a
    positional key would replay one item's write for a different item and lose it.
    """
    digest = hashlib.sha256(f"{item.kind}\0{item.text}".encode()).hexdigest()[:32]
    return f"consolidate:{session_key}:{span_id}:{digest}"


def publish_consolidation(
    route: MemoryRoute,
    *,
    memory_store: str | None,
    session_key: str,
    execution_context: Any,
    messages: list[dict],
    span: tuple[int, int],
    extracted: list[NewItem],
    receipt_id: str,
    span_id: str = "",
) -> bool:
    """Hand one consolidation pass to a provider that replaces Crew's memory.

    ``ingest: raw`` gets the transcript span (display-only rows already removed by
    the pass); ``ingest: items`` gets one ``put`` per item Crew extracted, each with
    an idempotency key built from the session, span and the item's own text, so a
    retried pass writes nothing twice whatever order it lists the items in. ``ingest: none`` takes nothing: automatic learning stops, which
    the owner acknowledged when choosing the provider.

    Returns whether the provider accepted the pass; ``False`` leaves the pass's
    retry receipt in place, exactly as a failed built-in publication does.
    """
    provider = route.provider
    scope = render.scope_for(
        memory_store, session_key=session_key, execution_context=execution_context
    )
    if provider is None:
        return False
    if scope is None or scope.memory_mode != "persistent":
        return True
    try:
        caps = provider.capabilities()
        if caps.read_only or caps.ingest == "none":
            return True
        if caps.ingest == "raw":
            from kiro_crew.dashboard.handlers._shared import _redact_memory_field

            # The transcript is the chat as written: a secret pasted into it, or printed
            # by a tool, is redacted before the span leaves for a provider.
            receipt = provider.blocking_ingest(  # type: ignore[attr-defined]
                scope,
                TranscriptSpan(
                    tuple(
                        {
                            "role": str(m.get("role", "")),
                            "text": _redact_memory_field(str(m.get("content", ""))),
                        }
                        for m in messages
                    ),
                    span[0],
                    span[1],
                ),
                receipt_id,
            )
            return receipt.accepted
        accepted = True
        for item in extracted:
            if item.kind not in caps.kinds:
                continue
            result = provider.blocking_put(  # type: ignore[attr-defined]
                scope, item, _ingest_key(session_key, span_id or f"{span[0]}-{span[1]}", item)
            )
            # A refused item is not published: the span stays pending for a retry,
            # and the items already stored answer ``unchanged`` under their keys.
            accepted = accepted and result.outcome != "refused"
        return accepted
    except ProviderError as exc:
        logger.warning("memory provider consolidation not accepted: %s", exc.code)
        return False


def lesson_idempotency_key(rule: str, scope_name: str | None) -> str:
    digest = hashlib.sha256(f"{scope_name or ''}\0{rule}".encode()).hexdigest()[:32]
    return f"lesson:{digest}"


async def put_lesson(
    route: MemoryRoute,
    *,
    store: str | None,
    session_key: str,
    execution_context: Any,
    rule: str,
    category: str,
    negative: str | None,
) -> bool:
    """A secondary writer's lesson (the task runner's), handed to the provider.

    Crew's lesson screens run first; returns whether the provider stored it. A
    provider that cannot take lessons, or cannot answer, skips it with a log line.
    """
    from kiro_crew.learn import lesson_contains_injection
    from kiro_crew.lesson_validation import contains_volatile_lesson_fact
    from kiro_crew.memory_provider.migration import redact_model_written

    if not isinstance(rule, str) or not rule.strip():
        return False
    if contains_volatile_lesson_fact(rule, negative) or lesson_contains_injection(rule, negative):
        return False
    scope = render.scope_for(store, session_key=session_key, execution_context=execution_context)
    if route.provider is None or scope is None or scope.memory_mode != "persistent":
        return False
    meta = {"crew.category": str(category or "tool")}
    if negative:
        meta["negative"] = str(negative)
    try:
        if not (await asyncio.to_thread(route.provider.capabilities)).supports_lessons:
            logger.info("memory provider takes no lessons; task lesson skipped")
            return False
        result = await route.provider.put(
            scope,
            redact_model_written(NewItem(kind="rule", text=rule.strip(), meta=meta)),
            # One key per task run: a lesson deleted and extracted again must be
            # stored again, not answered with the earlier insert.
            f"{lesson_idempotency_key(rule.strip(), None)}:{secrets.token_hex(8)}",
        )
    except ProviderError as exc:
        logger.info("memory provider did not take the task lesson: %s", exc.code)
        return False
    return result.stored
