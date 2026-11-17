"""MemoryAgentBench: FactConsolidation (single and multi hop) and EventQA.

FactConsolidation's context is a numbered list of facts in which a later fact
overrides an earlier one, so it is the cleanest public conflict test: each fact is
an item, written in order, and the question asks for the latest value. EventQA's
context is a long story fed chunk by chunk. Both are scored by substring match of an
accepted answer in the reader's answer; neither carries gold item ids, so retrieval
dimensions are "n/a" for this suite.

Upstream ships Parquet only. Reading it needs ``pyarrow``, which Crew does not
depend on; without it the suite refuses with that reason instead of guessing.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..datasets import SUITE_SPECS, ensure
from ..errors import BenchRefusal
from ..item_corpus import BenchItem, ItemCorpus, ItemQuery, ItemUnit, unit_id_of
from . import SuiteInfo

_FACT = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")
CHUNK_CHARS = 2_000
#: Variant -> (spec key, the ``metadata.source`` prefix of the rows it takes).
VARIANTS = {
    "fc_sh": ("mab_conflict_resolution", "factconsolidation_sh_"),
    "fc_mh": ("mab_conflict_resolution", "factconsolidation_mh_"),
    "eventqa": ("mab_accurate_retrieval", "eventqa_"),
}
#: The smallest context size per variant, so a default run stays quick.
DEFAULT_SIZE = {"fc_sh": "6k", "fc_mh": "6k", "eventqa": "131072"}


def _fact_items(context: str) -> list[BenchItem]:
    items = []
    for line in context.splitlines():
        match = _FACT.match(line)
        if match:
            n = int(match.group(1))
            # The order IS the time: a later fact overrides an earlier one.
            items.append(BenchItem(f"f{n}", match.group(2), "fact", float(n), "facts"))
    return items


def _chunk_items(context: str) -> list[BenchItem]:
    return [
        BenchItem(f"c{n}", context[start : start + CHUNK_CHARS], "episode", float(n), "story")
        for n, start in enumerate(range(0, len(context), CHUNK_CHARS))
    ]


def parse_row(row: dict[str, Any], variant: str, index: int = 0) -> ItemUnit:
    # Rows of one variant share ``metadata.source``, so the row's position in the
    # pinned file is part of its identity: each row gets its own store and query ids.
    source = f"{(row.get('metadata') or {}).get('source') or variant}#{index}"
    context = str(row.get("context") or "")
    items = _chunk_items(context) if variant == "eventqa" else _fact_items(context)
    queries = []
    for n, (question, answers) in enumerate(
        zip(row.get("questions") or [], row.get("answers") or [])
    ):
        accepted = [str(a) for a in (answers or []) if a]
        queries.append(
            ItemQuery(
                query_id=f"{source}:{n}",
                text=str(question),
                category="multi_hop" if "_mh_" in source else "knowledge_update",
                gold_answer=accepted[0] if accepted else None,
                extra=(("accepted", json.dumps(accepted, ensure_ascii=False)),),
            )
        )
    return ItemUnit(unit_id_of(source), tuple(items), tuple(queries))


def _rows(spec_key: str, *, allow_download: bool) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq  # type: ignore[import-not-found,import-untyped]
    except ImportError:
        raise BenchRefusal(
            "MemoryAgentBench ships Parquet only, and reading it needs pyarrow, which "
            "Kiro Crew does not install. Install pyarrow into Crew's environment to run it."
        ) from None
    path = ensure(SUITE_SPECS[spec_key], allow_download=allow_download)
    return list(pq.read_table(path).to_pylist())


def load(
    variant: str = "fc_sh", *, limit: int | None = None, allow_download: bool = True
) -> ItemCorpus:
    base, _, size = variant.partition("@")
    if base not in VARIANTS:
        raise ValueError(f"MemoryAgentBench variant must be one of {', '.join(VARIANTS)}")
    spec_key, prefix = VARIANTS[base]
    wanted = prefix + (size or DEFAULT_SIZE[base])
    rows = [
        r
        for r in _rows(spec_key, allow_download=allow_download)
        if (r.get("metadata") or {}).get("source") == wanted
    ]
    units = tuple(parse_row(r, base, n) for n, r in enumerate(rows[: limit or None]))
    return ItemCorpus("mab", variant, units)


SUITE = SuiteInfo(
    id="mab",
    name="MemoryAgentBench",
    variants=tuple(VARIANTS),
    license="MIT",
    license_note="Repackages other datasets (RULER, InfBench and others) whose own terms apply.",
    dims=("M2", "M4"),
    # Items only: a row is a context of facts or chunks, not a conversation, so
    # there are no sessions to replay.
    ingest_modes=("items",),
    scorer="substring",
    needs_reader=True,
    loader=load,
)
