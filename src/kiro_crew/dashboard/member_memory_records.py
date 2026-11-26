"""What a bot's memory holds, for its ``crewmate.memory`` card and its ``memory_list`` tool.

Both read the bot's OWN store only. The store is the one the bot's configuration
binds (:func:`kiro_crew.memory_stores.require_member_memory_store`), checked again
on every read, so a card or a listing never reaches the Global store or another
bot's. Each record is shown through the memory page's own redaction
(``_redact_memory_field``), the same text the user reads on that page.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from kiro_crew import change_card_memory as memory_kind
from kiro_crew.change_card_catalog_kinds import CardCatalogError

LIST_SORTS = memory_kind.LIST_SORTS
LIST_PAGE_MAX = 50


def own_store(member: str) -> str:
    """The store *member*'s configuration binds, or raise ``CardCatalogError``. Blocking.

    A bot that has no store of its own (one on the Global store) is refused: the
    Global store is everyone's, cleaned on the Memory page.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import (
        DEFAULT_MEMORY_STORE,
        UnknownMemoryStore,
        require_member_memory_store,
    )

    try:
        store = require_member_memory_store(KiroCrewConfig.load(), member)
    except UnknownMemoryStore:
        raise CardCatalogError("not_own_store", "this bot's memory store is unavailable") from None
    if store == DEFAULT_MEMORY_STORE:
        raise CardCatalogError(
            "shared_memory",
            "this bot uses the Global memory, which only the Memory page changes",
        )
    return store


def shown_text(entry: dict[str, Any]) -> str:
    """One record's content as the memory page shows it: redacted, a fact decoded."""
    from kiro_crew.change_card_catalog_plans import _plain
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    if entry.get("kind") == "episode":
        text: Any = entry.get("text") or ""
    else:
        try:
            text = _plain(json.loads(entry.get("value_json") or "null"))
        except (TypeError, ValueError):
            text = entry.get("value_json") or ""
    return str(_redact_memory_field(text))


async def _tier(state: Any, store: str) -> Any:
    from kiro_crew.dashboard.handlers._shared import vector_memory_for_store
    from kiro_crew.memory_startup import MemoryStartupUnavailable

    try:
        tier = await vector_memory_for_store(state, store)
    except (MemoryStartupUnavailable, OSError):
        tier = None
    if tier is None:
        raise CardCatalogError("store_unavailable", "this bot's memory store is unavailable")
    return tier


def _check_edits(tier: Any, params: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Refuse the card when the memory editor would refuse one of its ticked edits. Blocking.

    Runs the editor's own check (``memory_edit._after``) on each record as it is now,
    so a card never starts its writes when one edit would be refused part way.
    A record that is gone is left to ``build_preview`` (``not_found``).
    """
    from kiro_crew import memory_edit

    by_key = {memory_kind.record_key(e["kind"], e["id"]): e for e in entries}
    skip = set(params.get(memory_kind.FIELD_SKIP) or [])
    for at, change in enumerate(params["changes"]):
        if change["action"] != memory_kind.ACTION_EDIT or at in skip:
            continue
        entry = by_key.get(memory_kind.record_key(change["kind"], change["id"]))
        if entry is None:
            continue
        field = "text" if change["kind"] == "episode" else "value"
        operation = {"type": "set", field: change[field]}
        try:
            with tier._db_lock:
                memory_edit._after(tier, entry, operation)
        except memory_edit.MemoryEditError as exc:
            raise CardCatalogError(
                "memory_not_editable",
                f"memory {change['id']} cannot be edited this way: {exc}; "
                "leave it out or delete it instead",
            ) from None


async def memory_state(params: dict[str, Any], state: Any) -> dict[str, Any]:
    """The snapshot a ``crewmate.memory`` card is built and checked against.

    ``{"store", "records": {kind:id -> {revision, shown, updated_at}}}`` for every
    record the card names that still exists. The card's store must be the one the
    bot's configuration binds now, or the read refuses. A ticked edit the memory
    editor would refuse refuses the read too (``memory_not_editable``); the read
    runs at build, at every re-preview and again before the first apply step.
    """
    store = await asyncio.to_thread(own_store, params["member"])
    if store != params["store"]:
        raise CardCatalogError("not_own_store", "a bot can change only its own memory")
    from kiro_crew import memory_edit

    tier = await _tier(state, store)
    items = [{"kind": c["kind"], "id": c["id"]} for c in params["changes"]]
    result = await asyncio.to_thread(memory_edit.refresh_records, tier, items)
    await asyncio.to_thread(_check_edits, tier, params, result.get("entries") or [])
    records = {
        memory_kind.record_key(e["kind"], e["id"]): {
            "revision": e["revision"],
            "shown": shown_text(e),
            "updated_at": e.get("updated_at") or "",
        }
        for e in result.get("entries") or []
        if e.get("kind") in memory_kind.MEMORY_KINDS
    }
    return {"store": store, "records": records}


def _last_used(tier: Any, episode_ids: list[str]) -> dict[str, str]:
    """When recall last returned each experience (facts keep no such time). Blocking.

    A V2 store never records it (``episodic_search.touch_last_accessed`` returns
    early), so it yields nothing there and the rows carry no ``last_used``.
    """
    if not episode_ids or tier.algorithm_version == "v2":
        return {}
    marks = ",".join("?" for _ in episode_ids)
    with tier._db_lock:
        rows = tier.db.execute(
            f"SELECT id, last_accessed_at FROM episodic_memories WHERE id IN ({marks})",
            episode_ids,
        ).fetchall()
    return {str(r["id"]): str(r["last_accessed_at"] or "") for r in rows}


async def list_own(
    state: Any,
    member: str,
    *,
    kind: str,
    query: str = "",
    sort: str = "updated",
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """One page of *member*'s own facts and experiences, for ``memory_list``.

    Each row: ``kind``, ``id``, ``text`` (as the memory page shows it), ``created_at``,
    ``updated_at``, ``last_used`` (experiences, only on a store that records it:
    not V2), ``confidence`` (facts) or
    ``importance`` (experiences). Raises ``CardCatalogError`` or
    ``memory_edit.MemoryEditError``.
    """
    from kiro_crew import memory_edit

    if kind not in memory_kind.MEMORY_KINDS:
        raise CardCatalogError("invalid_param", "'kind' must be fact or episode")
    if sort not in LIST_SORTS:
        raise CardCatalogError("invalid_param", "'sort' must be one of " + ", ".join(LIST_SORTS))
    store = await asyncio.to_thread(own_store, member)
    tier = await _tier(state, store)
    page = await asyncio.to_thread(
        memory_edit.list_records,
        tier,
        {"q": query, "kind": kind},
        limit=limit,
        offset=offset,
        sort=sort,
        descending=True,
    )
    entries = [e for e in page.get("entries") or [] if e.get("kind") in memory_kind.MEMORY_KINDS]
    used = await asyncio.to_thread(
        _last_used, tier, [str(e["id"]) for e in entries if e.get("kind") == "episode"]
    )
    rows = []
    for e in entries:
        row: dict[str, Any] = {
            "kind": e["kind"],
            "id": e["id"],
            "text": shown_text(e),
            "created_at": e.get("created_at") or "",
            "updated_at": e.get("updated_at") or "",
        }
        if e["kind"] == "episode":
            row["importance"] = e.get("importance")
            if tier.algorithm_version != "v2":
                row["last_used"] = used.get(str(e["id"]), "")
        else:
            row["confidence"] = e.get("importance")
        rows.append(row)
    return {
        "entries": rows,
        "total": page.get("total", len(rows)),
        "has_more": bool(page.get("has_more")),
        "next_offset": offset + limit if page.get("has_more") else None,
    }
