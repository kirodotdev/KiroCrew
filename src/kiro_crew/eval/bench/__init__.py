"""External memory-benchmark harness (LongMemEval, LoCoMo).

Why this exists next to ``kirocrew eval`` rather than inside it: the existing eval
harness runs 4 hand-written scenarios carrying 19 substring assertions in a single
pass, with no repetitions, no seed control and no baseline comparison. That is a
usable smoke test and a poor instrument for "did this change help" — one flipped
assertion out of 19 is indistinguishable from sampling noise, and sampling cannot
be pinned because Kiro Crew threads no ``temperature`` or ``seed`` through its
provider stack.

This package answers that question instead, by splitting the measurement in two:

* :mod:`.retrieval` — a **deterministic** ruler over the real
  ``VectorMemoryStore``. Local embedder, deterministic ranker, thousands of
  questions with gold evidence. A delta here is exact and needs one pass.
* :mod:`.scorers` — the datasets' **official** answer metrics. LoCoMo's is
  token-F1 per category and needs no LLM at all, which makes it the only
  end-to-end score obtainable without an external API key. LongMemEval's is an
  LLM judge and is optional; when no judge is wired, items are reported as
  *unscored* rather than as zeros.

:mod:`.stats` carries the paired-interleaved-median protocol for the noisy half and
deliberately refuses to attach a confidence band to the deterministic half.
"""

from __future__ import annotations

from .corpus import (
    CATEGORIES,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
    Corpus,
)
from .datasets import SPECS, CorpusFetchError, describe, ensure, load_json
from .ingest import IngestConfig, IngestReport, ingest_instance, prepare_embedder, search_backend
from .retrieval import (
    RetrievalAggregate,
    RetrievalConfig,
    RetrievalNotMeasurable,
    corpus_has_distractors,
)
from .run import RunResult, compare_reports, format_report, run_retrieval, write_report

__all__ = [
    "BenchInstance",
    "BenchQuery",
    "BenchSession",
    "BenchTurn",
    "CATEGORIES",
    "Corpus",
    "CorpusFetchError",
    "IngestConfig",
    "IngestReport",
    "RetrievalAggregate",
    "RetrievalConfig",
    "RetrievalNotMeasurable",
    "RunResult",
    "SPECS",
    "compare_reports",
    "corpus_has_distractors",
    "describe",
    "ensure",
    "format_report",
    "ingest_instance",
    "load_json",
    "prepare_embedder",
    "run_retrieval",
    "search_backend",
    "write_report",
]
