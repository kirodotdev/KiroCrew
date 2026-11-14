"""``memory_recall`` through a provider: the evidence rows the recall route bounds.

The route keeps every gate it has (session recognition, the read block, the store
owner gate, the query bound) and the one serializer it has (``bound_recall_payload``
and ``recall_json``, the 3,000-character cap). Only the candidates change: in
``replace`` mode they all come from the provider, in ``augment`` mode the provider's
hits follow Crew's own. A provider that cannot answer gives a notice and no rows;
Crew's own memory is not read in its place.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.memory_provider import render
from kiro_crew.memory_provider.contract import ProviderError
from kiro_crew.memory_provider.resolver import MemoryRoute

logger = logging.getLogger("kiro_crew.memory_provider")

RECALL_LIMIT = 20


def _execution(session_key: str) -> Any:
    if not session_key:
        return None
    try:
        from kiro_crew.execution_context import read_session_execution

        return read_session_execution(session_key)
    except Exception:  # noqa: BLE001 - an unreadable record reads as the global persistent scope
        return None


async def provider_hits(
    route: MemoryRoute,
    store: str,
    query: str,
    session_key: str,
    project: str | None,
    keep: Callable[[list[dict]], list[dict] | None] | None = None,
) -> tuple[list[dict], list[dict], str]:
    """``(facts, episodes, notice)`` from the provider; the notice is ``""`` on success.

    *keep* is the recall decision point's hook, the one Crew's own recall runs: it
    may narrow the episodes, and its consent gate applies to provider hits as it
    does to Crew's.
    """
    execution = await asyncio.to_thread(_execution, session_key)
    scope = render.scope_for(
        store, session_key=session_key, execution_context=execution, project=project
    )
    if scope is None:
        return [], [], ""
    if route.provider is None:
        return [], [], f"{render.provider_name(route)} is unavailable: {route.unavailable}"
    try:
        items = await route.provider.search(scope, query, RECALL_LIMIT, None)
    except ProviderError as exc:
        logger.warning("memory provider %s recall failed: %s", route.provider_id, exc.code)
        return (
            [],
            [],
            (
                f"{render.provider_name(route)} did not answer ({exc.code}); absence of results "
                "is not absence of memory."
            ),
        )
    facts, episodes = render.recall_rows(items, route)
    if keep is not None and episodes:
        try:
            narrowed = await asyncio.to_thread(keep, list(episodes))
        except Exception:  # noqa: BLE001 - as in Crew's recall: keep the similarity result
            narrowed = None
        if isinstance(narrowed, list):
            episodes = narrowed
    return facts, episodes, ""


async def replace_result(
    route: MemoryRoute,
    store: str,
    query: str,
    session_key: str,
    project: str | None,
    keep: Callable[[list[dict]], list[dict] | None] | None = None,
) -> dict[str, Any]:
    facts, episodes, notice = await provider_hits(route, store, query, session_key, project, keep)
    result: dict[str, Any] = {
        "retrieval": {"facts": facts, "episodes": episodes},
        "provider": route.provider_id,
        "provider_status": "unavailable" if notice else "ready",
    }
    if notice:
        result["recall_notice"] = notice
    return result


async def augment_result(
    result: dict[str, Any],
    route: MemoryRoute,
    store: str,
    query: str,
    session_key: str,
    project: str | None,
) -> dict[str, Any]:
    """*result* with the read-only provider's hits after Crew's own."""
    facts, episodes, notice = await provider_hits(route, store, query, session_key, project)
    retrieval = dict(result.get("retrieval") or {})
    retrieval["facts"] = list(retrieval.get("facts") or []) + facts
    retrieval["episodes"] = list(retrieval.get("episodes") or []) + episodes
    merged = {**result, "retrieval": retrieval, "provider": route.provider_id}
    merged["provider_status"] = "unavailable" if notice else "ready"
    if notice and "recall_notice" not in merged:
        merged["recall_notice"] = notice
    return merged
