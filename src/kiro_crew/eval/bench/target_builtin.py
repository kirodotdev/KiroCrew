"""Crew's own memory as a benchmark target, on throwaway stores only.

Never benchmarked through ``BuiltinMemoryProvider``: that adapter maps a store id to
the person's real store through ``ContextBuilder.get_memory_for``. This target takes
a directory instead and opens one fresh ``VectorMemoryStore`` per unit inside it,
written through the same :func:`~.ingest.ingest_instance` and ranked by the same
:func:`~.retrieval.retrieve_for_query` the existing retrieval ruler uses. It names no
store, so it cannot reach one a person uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .corpus import BenchInstance, BenchQuery, BenchSession, BenchTurn
from .ingest import IngestConfig, IngestedInstance, ingest_instance
from .item_corpus import BenchItem, ItemUnit, ReplaySession
from .retrieval import RetrievalConfig, retrieve_for_query
from .target import Hit, TargetInfo

EmbedFn = Callable[[str], Any]


def _stamp(at: float | None) -> str:
    """An item time in the format the ingest's timestamp parser reads."""
    if at is None:
        return ""
    when = datetime.fromtimestamp(at, tz=timezone.utc)
    return f"{when:%Y/%m/%d} ({when:%a}) {when:%H:%M}"


def _instance(unit: ItemUnit, items: Sequence[BenchItem]) -> BenchInstance:
    """The unit's items as one instance: a session per item session, a turn per item."""
    order: list[str] = []
    turns: dict[str, list[BenchTurn]] = {}
    times: dict[str, float | None] = {}
    for item in items:
        sid = item.session_id or "s"
        if sid not in turns:
            order.append(sid)
            turns[sid] = []
            times[sid] = item.at
        turns[sid].append(BenchTurn(item.item_id, sid, "", item.text, _stamp(item.at)))
    sessions = tuple(BenchSession(sid, tuple(turns[sid]), _stamp(times[sid])) for sid in order)
    return BenchInstance(unit.unit_id, sessions, ())


class ScratchBuiltinTarget:
    """Crew's store and ranking over a temporary SQLite file per unit."""

    def __init__(
        self,
        root: Path,
        *,
        embed_fn: EmbedFn,
        ingest: IngestConfig | None = None,
        retrieval: RetrievalConfig | None = None,
    ) -> None:
        self.root = root
        self.embed_fn = embed_fn
        # Speaker names are already in the item text.
        self.ingest = ingest or IngestConfig(speaker_prefix=False)
        self.retrieval = retrieval or RetrievalConfig()
        self.info = TargetInfo(
            id="builtin",
            name="Crew memory",
            ingest_modes=("items",),
            notes=["Crew's own store and ranking, on a temporary copy per unit"],
        )
        self._loaded: dict[str, IngestedInstance] = {}
        self._items: dict[str, dict[str, BenchItem]] = {}

    def _db(self, unit: ItemUnit) -> Path:
        return self.root / f"{unit.unit_id}.db"

    async def prepare(self, unit: ItemUnit) -> None:
        await self.reset(unit)
        self._items[unit.unit_id] = {}

    async def write(self, unit: ItemUnit, items: Sequence[BenchItem]) -> dict[str, int]:
        self._items.setdefault(unit.unit_id, {}).update({i.item_id: i for i in items})
        await self._close(unit)
        # Re-ingested whole: one unit, one store, built from every item it holds.
        await asyncio.to_thread(self._remove_files, unit)
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
        inst = _instance(unit, list(self._items[unit.unit_id].values()))
        loaded = await asyncio.to_thread(
            ingest_instance,
            inst,
            db_path=self._db(unit),
            embed_fn=self.embed_fn,
            config=self.ingest,
        )
        self._loaded[unit.unit_id] = loaded
        dropped = loaded.report.dropped_fragments
        return {"inserted": loaded.report.written, "refused": dropped}

    async def replay(self, unit: ItemUnit, sessions: Sequence[ReplaySession]) -> int:
        raise NotImplementedError("Crew's own memory is benchmarked in item mode")

    async def settle(self, unit: ItemUnit) -> float:
        return 0.0

    async def query(self, unit: ItemUnit, text: str, k: int, as_of: float | None) -> list[Hit]:
        loaded = self._loaded.get(unit.unit_id)
        if loaded is None:
            return []
        query = BenchQuery(query_id="q", question=text, category="unknown")
        by_id = self._items.get(unit.unit_id, {})
        # With as_of the cut-off applies before the limit: rank the whole unit, drop what
        # was written later, then keep k, so a later item never takes an earlier one's place.
        fetch = max(k, len(by_id)) if as_of is not None else k
        config = RetrievalConfig(
            k_values=(fetch,),
            mmr=self.retrieval.mmr,
            relevance_filter=self.retrieval.relevance_filter,
        )
        found = await asyncio.to_thread(
            retrieve_for_query, loaded, query, embed_fn=self.embed_fn, config=config
        )
        hits: list[Hit] = []
        for item_id in found.retrieved_turn_ids:
            item = by_id.get(item_id)
            if item is None:
                continue
            # as_of: nothing written after the moment the question is asked at.
            if as_of is not None and item.at is not None and item.at > as_of:
                continue
            hits.append(Hit(item_id, item.text))
        return hits[:k]

    async def read_back(self, unit: ItemUnit) -> list[Hit]:
        loaded = self._loaded.get(unit.unit_id)
        if loaded is None:
            return []
        rows = await asyncio.to_thread(
            loaded.store.get_episodic_list, limit=10_000, offset=0, tag_filter=None
        )
        return [
            Hit(loaded.text_to_turn.get(str(r.get("text") or "")), str(r.get("text") or ""))
            for r in rows
        ]

    async def forget(self, unit: ItemUnit, item_ids: Sequence[str]) -> int:
        loaded = self._loaded.get(unit.unit_id)
        if loaded is None:
            return 0
        drop = set(item_ids)
        rows = await asyncio.to_thread(
            loaded.store.get_episodic_list, limit=10_000, offset=0, tag_filter=None
        )
        gone = 0
        for row in rows:
            if loaded.text_to_turn.get(str(row.get("text") or "")) in drop:
                if await asyncio.to_thread(loaded.store.delete_episodic, str(row.get("id"))):
                    gone += 1
        for item_id in drop:
            self._items.get(unit.unit_id, {}).pop(item_id, None)
        return gone

    async def _close(self, unit: ItemUnit) -> None:
        loaded = self._loaded.pop(unit.unit_id, None)
        if loaded is not None:
            await asyncio.to_thread(loaded.close)

    async def reset(self, unit: ItemUnit) -> bool:
        await self._close(unit)
        self._items.pop(unit.unit_id, None)
        return await asyncio.to_thread(self._remove_files, unit)

    def _remove_files(self, unit: ItemUnit) -> bool:
        """Delete the unit's store files; whether none is left. Run off the loop."""
        for path in self.root.glob(f"{unit.unit_id}.db*"):
            path.unlink(missing_ok=True)
        return not any(self.root.glob(f"{unit.unit_id}.db*"))

    def usage(self) -> dict[str, Any]:
        return {"reported": False}

    async def close(self) -> None:
        for unit_id in list(self._loaded):
            loaded = self._loaded.pop(unit_id)
            await asyncio.to_thread(loaded.close)
