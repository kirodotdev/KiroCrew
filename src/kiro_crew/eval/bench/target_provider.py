"""An app's memory provider as a benchmark target, on ``bench/`` stores only.

Every scope here comes from :func:`~kiro_crew.memory_provider.contract.bench_scope`,
and the target calls the provider object directly: a bench scope never reaches
``resolver.route_for``, so live routing can neither serve one nor be pointed at one.
``reset`` is refused client side for any other scope, and the fixture provider and the
conformance kit check that a provider refuses it too.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from kiro_crew.memory_provider.contract import (
    MemoryProvider,
    NewItem,
    Origin,
    ProviderError,
    ProviderScope,
    TranscriptSpan,
    bench_scope,
)

from .item_corpus import BenchItem, ItemUnit, ReplaySession
from .target import Hit, NotSettled, TargetInfo

#: The longest a unit waits for a provider's background work to settle.
SETTLE_CAP_SECS = 600.0
_SETTLE_FLOOR_SECS = 0.5
Sleep = Callable[[float], Awaitable[None]]


class ProviderTarget:
    """Drives one provider for one run. ``run_id`` names every store it creates."""

    def __init__(
        self,
        provider: MemoryProvider,
        run_id: str,
        *,
        suite: str,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.run_id = run_id
        self.suite = suite
        self.sleep = sleep
        self.clock = clock
        caps = provider.capabilities()
        self.caps = caps
        modes = []
        if caps.has("put"):
            modes.append("items")
        if caps.ingest != "none" and caps.has("ingest"):
            modes.append("replay")
        self.info = TargetInfo(
            id=provider.provider_id,
            name=provider.display_name,
            egress=caps.egress,
            version=caps.provider_version,
            # What an egress acknowledgement names: the installed app's version.
            app_version=str(getattr(provider, "app_version", "") or ""),
            keeps_origin=caps.keeps_origin,
            preserves_timestamps=caps.preserves_timestamps,
            as_of=caps.bench.as_of,
            deterministic=caps.bench.deterministic,
            reset=caps.bench.reset,
            settle=caps.bench.settle,
            ingest_modes=tuple(modes),
            max_concurrency=caps.bench.max_concurrency,
            caps_sha256=hashlib.sha256(
                json.dumps(caps.to_wire(), sort_keys=True).encode()
            ).hexdigest(),
        )
        if not caps.bench.scratch:
            self.info.notes.append("the provider does not say it keeps benchmark stores apart")
        if not caps.bench.reset:
            self.info.notes.append(
                "cleanup is one delete per item, which may leave derived data behind"
            )
        self._written: dict[str, list[str]] = {}
        self._ids: dict[str, dict[str, str]] = {}
        self.timings: dict[str, list[float]] = {}

    @property
    def isolated(self) -> bool:
        """Whether a bench store is apart from the provider's live memory."""
        return self.caps.bench.scratch or self.caps.multi_store

    def scope(self, unit: ItemUnit) -> ProviderScope:
        return bench_scope(self.run_id, unit.unit_id)

    async def _timed(self, op: str, call: Awaitable[Any]) -> Any:
        started = self.clock()
        try:
            return await call
        finally:
            self.timings.setdefault(op, []).append((self.clock() - started) * 1000.0)

    async def prepare(self, unit: ItemUnit) -> None:
        await self.reset(unit)

    def _new_item(self, item: BenchItem) -> NewItem:
        return NewItem(
            kind=self.caps.kind_for(item.kind),
            text=item.text,
            scopes=item.scopes,
            meta=dict(item.meta),
            created_at=item.at,
            updated_at=item.at,
            origin=Origin("bench", self.suite, item.item_id),
        )

    async def write(self, unit: ItemUnit, items: Sequence[BenchItem]) -> dict[str, int]:
        scope = self.scope(unit)
        ordered = sorted(items, key=lambda i: (i.at is None, i.at or 0.0))
        entries = [
            (self._new_item(i), f"bench:{self.run_id}:{unit.unit_id}:{i.item_id}") for i in ordered
        ]
        results = await self._timed("put_batch", self.provider.put_batch(scope, entries))
        outcomes: dict[str, int] = {}
        ids = self._ids.setdefault(unit.unit_id, {})
        for item, result in zip(ordered, results):
            outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
            if result.id:
                ids[result.id] = item.item_id
                self._written.setdefault(unit.unit_id, []).append(result.id)
        return outcomes

    async def replay(self, unit: ItemUnit, sessions: Sequence[ReplaySession]) -> int:
        scope = self.scope(unit)
        stored = 0
        ordered = sorted(sessions, key=lambda s: (s.at is None, s.at or 0.0))
        for n, session in enumerate(ordered):
            span = TranscriptSpan(
                tuple({"role": role, "text": text} for role, text, _ in session.messages),
                0,
                len(session.messages),
                times=tuple(at for _, _, at in session.messages),
                session_at=session.at,
            )
            receipt = await self._timed(
                "ingest",
                self.provider.ingest(scope, span, f"bench:{self.run_id}:{unit.unit_id}:{n}"),
            )
            stored += receipt.items
        return stored

    async def settle(self, unit: ItemUnit) -> float:
        """Poll ``settle`` until nothing is pending; answers the seconds waited."""
        if not self.caps.bench.settle:
            return 0.0
        settle = getattr(self.provider, "settle")
        started = self.clock()
        while True:
            answer = await self._timed("settle", settle(self.scope(unit)))
            waited = self.clock() - started
            if answer.pending <= 0:
                return waited
            if waited >= SETTLE_CAP_SECS:
                raise NotSettled(
                    f"{answer.pending} items still indexing after {SETTLE_CAP_SECS:.0f}s"
                )
            await self.sleep(max(_SETTLE_FLOOR_SECS, answer.retry_after_secs or 0.0))

    def _hit(self, unit: ItemUnit, item: Any) -> Hit:
        origin = (
            item.origin.id if item.origin is not None and item.origin.provider == "bench" else None
        )
        if origin is None:
            origin = self._ids.get(unit.unit_id, {}).get(item.id)
        return Hit(origin, item.text, item.score)

    async def query(self, unit: ItemUnit, text: str, k: int, as_of: float | None) -> list[Hit]:
        scope = self.scope(unit)
        timed = as_of is not None and self.caps.bench.as_of
        if self.caps.supports_search and timed:
            search_as_of = getattr(self.provider, "search_as_of")
            items = await self._timed("search", search_as_of(scope, text, k, None, as_of))
        elif self.caps.supports_search:
            items = await self._timed("search", self.provider.search(scope, text, k, None))
        elif timed:
            context_as_of = getattr(self.provider, "context_as_of")
            items = await self._timed("context", context_as_of(scope, text, 16_000, as_of))
        else:
            items = await self._timed("context", self.provider.context(scope, text, 16_000))
        return [self._hit(unit, i) for i in items[:k]]

    async def read_back(self, unit: ItemUnit) -> list[Hit]:
        scope = self.scope(unit)
        hits: list[Hit] = []
        cursor: str | None = None
        for _page in range(200):
            page = await self._timed("list", self.provider.list(scope, cursor, None, "all"))
            hits.extend(self._hit(unit, i) for i in page.items)
            cursor = page.cursor
            if not cursor:
                break
        return hits

    async def forget(self, unit: ItemUnit, item_ids: Sequence[str]) -> int:
        """Delete the provider items that stand for *item_ids* (by origin or by write id)."""
        scope = self.scope(unit)
        drop = set(item_ids)
        targets = [pid for pid, origin in self._ids.get(unit.unit_id, {}).items() if origin in drop]
        if not targets:
            targets = [h_id for h_id, h in await self._listed(unit) if h.origin_id in drop]
        gone = 0
        for pid in targets:
            result = await self._timed("delete", self.provider.delete(scope, pid))
            gone += result.outcome == "deleted"
        return gone

    async def _listed(self, unit: ItemUnit) -> list[tuple[str, Hit]]:
        scope = self.scope(unit)
        out: list[tuple[str, Hit]] = []
        cursor: str | None = None
        for _page in range(200):
            page = await self.provider.list(scope, cursor, None, "all")
            out.extend((i.id, self._hit(unit, i)) for i in page.items)
            cursor = page.cursor
            if not cursor:
                break
        return out

    async def reset(self, unit: ItemUnit) -> bool:
        """Empty the unit's bench store; answers whether it reads back empty."""
        scope = self.scope(unit)
        self._ids.pop(unit.unit_id, None)
        self._written.pop(unit.unit_id, None)
        if not self.isolated:
            # A single-store provider ignores the bench scope: any cleanup would empty
            # its live memory.
            return False
        reset = getattr(self.provider, "reset", None)
        if self.caps.bench.reset and callable(reset):
            answer = await self._timed("reset", reset(scope))
            if not self.caps.supports_list:
                return bool(answer.empty)
        elif self.caps.supports_delete and self.caps.supports_list:
            for item_id, _hit in await self._listed(unit):
                await self.provider.delete(scope, item_id)
        if not self.caps.supports_list:
            return False
        try:
            left = await self.provider.list(scope, None, None, "all")
        except ProviderError:
            return False
        # Empty means the end of the listing: an empty page with a cursor is not proof.
        return not left.items and not left.cursor

    def usage(self) -> dict[str, Any]:
        drain = getattr(self.provider, "drain_usage", None)
        blocks = drain() if callable(drain) else []
        if not blocks:
            return {"reported": False}
        totals: dict[str, Any] = {"reported": True}
        for name in ("llm_calls", "llm_input_tokens", "llm_output_tokens", "embed_tokens"):
            values = [getattr(b, name) for b in blocks if getattr(b, name) is not None]
            totals[name] = sum(values) if values else None
        return totals

    async def close(self) -> None:
        return None
