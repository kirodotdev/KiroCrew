"""LoCoMo and LongMemEval, through the adapters the retrieval ruler already uses."""

from __future__ import annotations

from ..adapters.locomo import load_locomo
from ..adapters.longmemeval import load_longmemeval
from ..datasets import load_json
from ..item_corpus import ItemCorpus, from_corpus
from . import SuiteInfo


def _locomo(
    variant: str = "locomo10", *, limit: int | None = None, allow_download: bool = True
) -> ItemCorpus:
    raw = load_json("locomo10", allow_download=allow_download)
    return from_corpus(load_locomo(raw), limit=limit)


def _longmemeval(
    variant: str = "s", *, limit: int | None = None, allow_download: bool = True
) -> ItemCorpus:
    key = "longmemeval_s" if variant == "s" else "longmemeval_oracle"
    raw = load_json(key, allow_download=allow_download)
    return from_corpus(load_longmemeval(raw, variant=variant), limit=limit)


LOCOMO = SuiteInfo(
    id="locomo",
    name="LoCoMo",
    variants=("locomo10",),
    license="CC BY-NC 4.0",
    license_note="Non-commercial use only.",
    dims=("M1", "M2", "M3", "M5"),
    ingest_modes=("items", "replay"),
    scorer="retrieval",
    needs_reader=True,
    # Its answers are graded against a reference answer, as LongMemEval's are.
    needs_judge=True,
    restricted=True,
    loader=_locomo,
)

LONGMEMEVAL = SuiteInfo(
    id="longmemeval",
    name="LongMemEval",
    variants=("s", "oracle"),
    license="MIT",
    license_note="",
    dims=("M1", "M2", "M3", "M4", "M5"),
    ingest_modes=("items", "replay"),
    scorer="retrieval",
    needs_reader=True,
    needs_judge=True,
    loader=_longmemeval,
)
