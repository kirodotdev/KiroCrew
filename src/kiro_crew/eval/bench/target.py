"""What a provider benchmark drives: one :class:`BenchTarget` per provider in a run.

A target owns one store per unit. ``prepare`` makes it empty, ``write`` or
``replay`` fills it, ``settle`` waits for background work, ``query`` asks, and
``reset`` empties it again. Every implementation works on throwaway stores only:

- :class:`~.target_builtin.ScratchBuiltinTarget` -- Crew's own store, one temporary
  SQLite file per unit; never a store a person uses.
- :class:`~.target_provider.ProviderTarget` -- an app's provider, through
  :func:`kiro_crew.memory_provider.contract.bench_scope` stores only.
- the baselines here: ``none`` (answers nothing, the abstention floor) and
  ``recency`` (the latest items up to the budget, the long-context baseline).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .item_corpus import BenchItem, ItemUnit, ReplaySession


class NotSettled(Exception):
    """A provider still had background work pending when the settle wait ran out."""


@dataclass(frozen=True)
class Hit:
    """One returned item: the ``origin.id`` it kept (or ``None``) and its text."""

    origin_id: str | None
    text: str
    score: float | None = None


@dataclass
class TargetInfo:
    """What the plan and the report say about a target."""

    id: str
    name: str
    egress: str = "local"
    version: str = ""
    #: The installed app's version, which an egress acknowledgement names.
    app_version: str = ""
    keeps_origin: bool = True
    preserves_timestamps: bool = True
    as_of: bool = True
    deterministic: bool = True
    reset: bool = True
    settle: bool = False
    ingest_modes: tuple[str, ...] = ("items",)
    max_concurrency: int = 1
    caps_sha256: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "egress": self.egress,
            "version": self.version,
            "app_version": self.app_version,
            "keeps_origin": self.keeps_origin,
            "preserves_timestamps": self.preserves_timestamps,
            "as_of": self.as_of,
            "deterministic": self.deterministic,
            "cleanup": "reset" if self.reset else "delete",
            "settle": self.settle,
            "ingest_modes": list(self.ingest_modes),
            "max_concurrency": self.max_concurrency,
            "caps_sha256": self.caps_sha256,
            "notes": list(self.notes),
        }


class BenchTarget(Protocol):
    info: TargetInfo

    async def prepare(self, unit: ItemUnit) -> None: ...

    async def write(self, unit: ItemUnit, items: Sequence[BenchItem]) -> dict[str, int]: ...

    async def replay(self, unit: ItemUnit, sessions: Sequence[ReplaySession]) -> int: ...

    async def settle(self, unit: ItemUnit) -> float: ...

    async def query(self, unit: ItemUnit, text: str, k: int, as_of: float | None) -> list[Hit]: ...

    async def read_back(self, unit: ItemUnit) -> list[Hit]: ...

    async def forget(self, unit: ItemUnit, item_ids: Sequence[str]) -> int: ...

    async def reset(self, unit: ItemUnit) -> bool: ...

    def usage(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class _BaselineBase:
    """Holds what a unit wrote in memory; never touches a store."""

    def __init__(self, info: TargetInfo) -> None:
        self.info = info
        self._items: dict[str, list[BenchItem]] = {}

    async def prepare(self, unit: ItemUnit) -> None:
        self._items[unit.unit_id] = []

    async def write(self, unit: ItemUnit, items: Sequence[BenchItem]) -> dict[str, int]:
        self._items.setdefault(unit.unit_id, []).extend(items)
        return {"inserted": len(items)}

    async def replay(self, unit: ItemUnit, sessions: Sequence[ReplaySession]) -> int:
        return 0

    async def settle(self, unit: ItemUnit) -> float:
        return 0.0

    async def query(self, unit: ItemUnit, text: str, k: int, as_of: float | None) -> list[Hit]:
        return []

    async def read_back(self, unit: ItemUnit) -> list[Hit]:
        return [Hit(i.item_id, i.text) for i in self._items.get(unit.unit_id, [])]

    async def forget(self, unit: ItemUnit, item_ids: Sequence[str]) -> int:
        drop = set(item_ids)
        kept = [i for i in self._items.get(unit.unit_id, []) if i.item_id not in drop]
        gone = len(self._items.get(unit.unit_id, [])) - len(kept)
        self._items[unit.unit_id] = kept
        return gone

    async def reset(self, unit: ItemUnit) -> bool:
        self._items.pop(unit.unit_id, None)
        return True

    def usage(self) -> dict[str, Any]:
        return {"reported": False}

    async def close(self) -> None:
        self._items.clear()


class NoneBaseline(_BaselineBase):
    """Returns nothing: every answerable query misses, every abstention passes."""

    def __init__(self) -> None:
        super().__init__(TargetInfo(id="baseline:none", name="No memory"))


class RecencyBaseline(_BaselineBase):
    """The latest items, newest first, up to *budget_chars*: the long-context baseline."""

    def __init__(self, budget_chars: int = 16_000) -> None:
        super().__init__(TargetInfo(id="baseline:recency", name="Most recent turns"))
        self.budget_chars = budget_chars

    async def query(self, unit: ItemUnit, text: str, k: int, as_of: float | None) -> list[Hit]:
        items = [
            i
            for i in self._items.get(unit.unit_id, [])
            if as_of is None or i.at is None or i.at <= as_of
        ]
        items.sort(key=lambda i: (i.at or 0.0), reverse=True)
        out: list[Hit] = []
        used = 0
        for item in items:
            if used + len(item.text) > self.budget_chars:
                break
            out.append(Hit(item.item_id, item.text))
            used += len(item.text)
        return out


BASELINES = {"baseline:none": NoneBaseline, "baseline:recency": RecencyBaseline}
