"""Suites a provider benchmark runs: one adapter per public benchmark, plus Crew's own.

Each suite turns its upstream files into an :class:`~..item_corpus.ItemCorpus` and
names its license, the dimensions it measures, the ingest modes it supports and
whether it needs a reader or judge model. Data is fetched on demand and pinned
(:mod:`..datasets`); nothing is vendored. Suites whose license is not stated
upstream are left out. HaluMem is off by default and converted only in memory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..item_corpus import ItemCorpus


@dataclass(frozen=True)
class SuiteInfo:
    id: str
    name: str
    variants: tuple[str, ...]
    license: str
    license_note: str
    dims: tuple[str, ...]
    ingest_modes: tuple[str, ...]
    #: ``retrieval`` (gold ids), ``mc`` (multiple choice), ``substring``,
    #: ``assertions``, ``forget``, ``isolation``, ``rubric`` or ``extraction``.
    scorer: str
    needs_reader: bool = False
    needs_judge: bool = False
    default_on: bool = True
    #: Shown before a fetch: a non-commercial or no-derivatives license.
    restricted: bool = False
    loader: Callable[..., ItemCorpus] | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "variants": list(self.variants),
            "license": self.license,
            "license_note": self.license_note,
            "dims": list(self.dims),
            "ingest_modes": list(self.ingest_modes),
            "scorer": self.scorer,
            "needs_reader": self.needs_reader,
            "needs_judge": self.needs_judge,
            "default_on": self.default_on,
            "restricted": self.restricted,
        }


def registry() -> dict[str, SuiteInfo]:
    """Every suite, by id. Imported lazily so a missing optional reader costs nothing."""
    from . import beam, forget, halumem, isolation, mab, personamem, precisionmem, public

    suites = [
        public.LOCOMO,
        public.LONGMEMEVAL,
        beam.SUITE,
        mab.SUITE,
        personamem.SUITE,
        precisionmem.SUITE,
        forget.SUITE,
        isolation.SUITE,
        halumem.SUITE,
    ]
    return {s.id: s for s in suites}


def get(suite_id: str) -> SuiteInfo:
    suites = registry()
    if suite_id not in suites:
        raise KeyError(f"unknown suite {suite_id!r}; known: {', '.join(sorted(suites))}")
    return suites[suite_id]
