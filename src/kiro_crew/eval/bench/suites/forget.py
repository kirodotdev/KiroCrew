"""Crew's forget suite: delete what was asked, leave nothing findable, delete nothing else.

Built at run time from a public conversational suite (LoCoMo by default) or from an
own-memory set; no data file of its own. Per unit, up to :data:`MAX_TARGETS` queries
with gold evidence are the targets: their evidence items are deleted, the store is
settled, and each target is asked again directly and by its rare terms. A target
passes when no forgotten item comes back; every other query that found its gold
before must still find it (collateral). Modeled on the removed, leaked and
over-deleted rates of recent forgetting benchmarks.
"""

from __future__ import annotations

import random
import re
from collections import Counter

from ..item_corpus import ItemCorpus, ItemQuery, ItemUnit
from . import SuiteInfo

MAX_TARGETS = 20
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'-]{3,}")


def targets(unit: ItemUnit, *, seed: int = 7, limit: int = MAX_TARGETS) -> list[ItemQuery]:
    answerable = [q for q in unit.queries if q.gold_ids and not q.unanswerable]
    rng = random.Random(f"{seed}:{unit.unit_id}")
    rng.shuffle(answerable)
    return answerable[:limit]


def key_terms(text: str, unit: ItemUnit, *, count: int = 4) -> str:
    """The *count* rarest words of *text* across the unit: a deterministic paraphrase."""
    df: Counter[str] = Counter()
    for item in unit.items:
        df.update({w.lower() for w in _WORD.findall(item.text)})
    words = sorted({w.lower() for w in _WORD.findall(text)}, key=lambda w: (df[w], w))
    return " ".join(words[:count])


def load(
    variant: str = "locomo", *, limit: int | None = None, allow_download: bool = True
) -> ItemCorpus:
    from . import get

    base = get(variant)
    if base.loader is None or base.id in ("forget", "isolation"):
        raise ValueError("the forget suite is built on a conversational suite")
    corpus = base.loader(base.variants[0], limit=limit, allow_download=allow_download)
    return ItemCorpus("forget", variant, corpus.units, corpus.notes)


SUITE = SuiteInfo(
    id="forget",
    name="Crew forget suite",
    variants=("locomo", "longmemeval"),
    license="as the suite it is built on",
    license_note="Built at run time; ships no data.",
    dims=("M8",),
    ingest_modes=("items",),
    scorer="forget",
    loader=load,
)
