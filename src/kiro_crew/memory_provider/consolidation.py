"""Consolidation publication when a memory provider replaces Crew's memory.

The pass keeps everything that decides WHEN and WHAT to consolidate -- the triggers,
the persistence and memory-mode refusals, the per-process gate, the lease, the span
snapshot, the retry-eligibility gate and the durable marker. Only publication moves:

- ``ingest: raw``: the provider gets the span's conversation rows (display-only rows
  never reach it) and extracts for itself;
- ``ingest: items``: Crew runs a short extraction turn of its own over the span --
  without showing it Crew's stored memory, which is not the memory in use -- and
  hands each item to ``put`` with a key built from the session and span, after
  Crew's lesson screens (volatile-fact refusal, injection scan);
- ``ingest: none`` or a read-only provider: nothing is handed over and automatic
  learning stops, as the owner acknowledged when choosing the provider.

Nothing is written to Crew's own stores. A provider that does not accept the pass
leaves the span pending, as a failed built-in publication does, so a later pass
retries it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.memory_provider import chokepoints
from kiro_crew.memory_provider.contract import NewItem, ProviderError, truncate_text

if TYPE_CHECKING:
    from kiro_crew.history_consolidation import HistoryConsolidator, _ConsolidationRefusedSentinel
    from kiro_crew.memory_provider.resolver import MemoryRoute

logger = logging.getLogger("kiro_crew.memory_provider")

_MAX_ITEMS = 20

_EXTRACTION_PROMPT = (
    "You are a memory consolidation agent. Process this conversation and return a "
    "JSON object with these keys:\n\n"
    '1. "history_entry": a concise paragraph (2-5 sentences) of what happened, or "" '
    "when nothing is worth remembering.\n"
    '2. "facts": array of {"text": "...", "kind": "preference" | "fact"} for durable '
    "facts the user stated or clearly implied. At most 10.\n"
    '3. "episodes": array of {"text": "..."} for events and decisions worth recalling. '
    "At most 5.\n"
    '4. "lessons": array of {"rule": "...", "applies": "always" | "on_topic"} for '
    "corrections the user taught without asking to remember them. At most 5. "
    "{lesson_rule}\n\n"
    "## Conversation to Process\n{conversation}\n\nRespond with ONLY valid JSON, no "
    "markdown fences."
)


def _strings(result: dict, key: str, field: str) -> list[tuple[str, dict]]:
    rows = result.get(key)
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows[:_MAX_ITEMS]:
        if isinstance(row, dict) and isinstance(row.get(field), str) and row[field].strip():
            out.append((row[field].strip(), row))
    return out


def extracted_items(result: dict) -> list[NewItem]:
    """The model's answer as contract items, with Crew's content screens applied."""
    from kiro_crew.memory_provider.copy_job import screen_refuses

    items: list[NewItem] = []
    entry = result.get("history_entry")
    if isinstance(entry, str) and entry.strip():
        items.append(NewItem(kind="summary", text=truncate_text(entry.strip())))
    for text, row in _strings(result, "facts", "text"):
        kind = "preference" if row.get("kind") == "preference" else "fact"
        items.append(NewItem(kind=kind, text=truncate_text(text)))
    for text, _row in _strings(result, "episodes", "text"):
        items.append(NewItem(kind="episode", text=truncate_text(text)))
    for rule, row in _strings(result, "lessons", "rule"):
        applies = "on_topic" if row.get("applies") == "on_topic" else "always"
        items.append(
            NewItem(
                kind="finding" if applies == "on_topic" else "rule",
                text=truncate_text(rule),
                meta={"applies": applies, "category": "knowledge"},
            )
        )
    # The same screens Crew's own stores run, so a provider never keeps what they refuse.
    return [item for item in items if not screen_refuses(item)]


async def publish(
    consolidator: HistoryConsolidator,
    key: str,
    route: MemoryRoute,
    *,
    store_name: str,
    execution: Any,
    chunk: list[dict],
    attempted: Any,
    lease: str,
    include_history: bool,
    commit_state: Any,
    transcript_moved: Callable[[], Awaitable[_ConsolidationRefusedSentinel | None]],
    refused: _ConsolidationRefusedSentinel,
) -> _ConsolidationRefusedSentinel | None:
    """Publish one span to the provider, under the same fences as Crew's own write path.

    Returns ``None`` when the span is done (published, or a failed turn charged to the
    attempt count), or the pass's refusal value when it stays pending. A turn that
    produced nothing usable is charged through ``_note_failed_attempt``, never published
    as an empty success, and a transcript that moved during extraction discards the
    result, as the built-in path does.
    """
    from kiro_crew.history_consolidation import (
        _ConsolidationNotDispatched,
        _ConsolidationReplyUnusable,
        _fmt_message,
        _prompt_rows,
    )

    if not include_history:
        # The preference-only pass rewrites Crew's preferences file, which a
        # provider that replaces Crew's memory does not use.
        return refused
    provider = route.provider
    if provider is None:
        logger.info("consolidation for %s waits: %s", key, route.unavailable)
        return refused
    try:
        caps = await asyncio.to_thread(provider.describe)
    except ProviderError as exc:
        logger.info("consolidation for %s waits: provider %s", key, exc.code)
        return refused
    rows = _prompt_rows(chunk)
    extracted: list[NewItem] = []
    if caps.ingest == "items" and not caps.read_only:
        from kiro_crew import memory_tier_policy as _tier_policy
        from kiro_crew.memory_origin_gate import configured_mode as _origin_mode
        from kiro_crew.memory_origin_gate import gate_consolidation_lessons

        conversation = "\n".join(_fmt_message(m, prestripped=False) for m in rows)
        tier_mode = _tier_policy.configured_mode()
        prompt = _EXTRACTION_PROMPT.replace(
            "{lesson_rule}", _tier_policy.prompt_rule_for(tier_mode)
        ).replace("{conversation}", conversation)
        failure = ""
        try:
            result = await consolidator._call_llm(prompt, session_key=key)
        except _ConsolidationNotDispatched as exc:
            await consolidator._note_environment_failure(key, str(exc))
            return refused
        except _ConsolidationReplyUnusable as exc:
            result, failure = None, str(exc)
        if not isinstance(result, dict) or not result:
            # A billed turn with nothing usable is charged, as on the built-in path.
            failure = failure or ("empty JSON answer" if isinstance(result, dict) else "no answer")
            if not await consolidator._note_failed_attempt(key, attempted, failure, lease):
                return refused
            return None
        # The lesson origin gate and tier policy Crew's own write path applies, on the
        # rows the model read, before any lesson becomes a provider item.
        result = gate_consolidation_lessons(result, rows, _origin_mode(), session_key=key).result
        result = _tier_policy.apply_tier_policy(result, rows, tier_mode, session_key=key).result
        extracted = extracted_items(result)
        from kiro_crew.history_consolidation import _lesson_extraction_disabled

        if _lesson_extraction_disabled():
            extracted = [i for i in extracted if i.kind not in ("rule", "finding")]
    moved = await transcript_moved()
    if moved is not None:
        return moved
    span_id = _span_id(key, attempted)

    def _publish_held() -> bool:
        # The lease and persistence fences are read first, so a pass that lost its
        # lease or a store whose writes were switched off sends nothing.
        with consolidator._publication_hold_checked(key, commit_state):
            pass
        # The provider exchange runs outside the publication hold, which covers only
        # a short durable commit; its writes are idempotent under the span's keys, so
        # a pass that then loses its lease leaves nothing a retry would double.
        accepted = chokepoints.publish_consolidation(
            route,
            memory_store=store_name,
            session_key=key,
            execution_context=execution,
            messages=rows,
            span=(attempted.offset, attempted.prompted),
            extracted=extracted,
            receipt_id=f"{key}:{span_id}",
            span_id=span_id,
        )
        if not accepted:
            return False
        # Then the hold, only to re-read the lease and persistence fences and commit.
        with consolidator._publication_hold_checked(key, commit_state) as publication:
            publication.mark_committed()
        return True

    if not await asyncio.to_thread(_publish_held):
        if caps.ingest == "items":
            # A billed extraction the provider would not take is charged, so the
            # retry backoff and the attempt cap apply as on Crew's own path.
            if not await consolidator._note_failed_attempt(
                key, attempted, "the memory provider refused the span", lease
            ):
                return refused
            return None
        return refused
    await consolidator._advance_history_marker(key, attempted, lease)
    return None


def _span_id(key: str, attempted: Any) -> str:
    """One span's identity: message offsets, generation and the sliced head's sub-offsets.

    Two slices of one over-budget head share their message offsets and generation, so
    the sub-offsets keep a later slice from reading as a retry of the first.
    """
    return (
        f"{attempted.offset}-{attempted.prompted}:{attempted.generation}"
        f":{getattr(attempted, 'sub_offset', 0)}-{getattr(attempted, 'sub_prompted', 0)}"
    )
