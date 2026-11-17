"""PrecisionMemBench: exactly which memories must and must not come back.

One unit holds the 35 seed beliefs; each of the 77 cases is a query with
``mustInclude`` and ``mustExclude`` assertions (and ``shouldOnlyInclude`` for the
relevant set). Retrieval only, no LLM: a case passes when every must-include id is in
the top k and no must-exclude id is. The rendering assertions (``personaPrelude``)
are about the upstream harness's own prompt and are not checked here.
"""

from __future__ import annotations

import json
from typing import Any

from ..datasets import SUITE_SPECS, load_json
from ..item_corpus import BenchItem, ItemCorpus, ItemQuery, ItemUnit
from . import SuiteInfo

_KIND = {"preference": "preference", "open_question": "note"}


def _epoch(raw: object) -> float | None:
    from datetime import datetime

    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse(seed: Any, cases: Any) -> ItemUnit:
    items = []
    for belief in seed or []:
        if not isinstance(belief, dict) or not isinstance(belief.get("_id"), str):
            continue
        meta = (("status", str(belief.get("epistemic_status") or "")),)
        items.append(
            BenchItem(
                belief["_id"],
                str(belief.get("content") or ""),
                _KIND.get(str(belief.get("type")), "fact"),
                _epoch(belief.get("created_at")),
                "seed",
                scopes=tuple(str(s) for s in belief.get("scope") or []),
                meta=meta,
            )
        )
    queries: list[ItemQuery] = []
    for case in cases or []:
        if not isinstance(case, dict) or not isinstance(case.get("query"), str):
            continue
        include: list[str] = []
        exclude: list[str] = []
        only: list[str] | None = None
        for section, rules in (case.get("expect") or {}).items():
            if section == "personaPrelude" or not isinstance(rules, dict):
                continue
            include += [str(i) for i in rules.get("mustInclude") or []]
            exclude += [str(i) for i in rules.get("mustExclude") or []]
            if isinstance(rules.get("shouldOnlyInclude"), list):
                only = (only or []) + [str(i) for i in rules["shouldOnlyInclude"]]
        assertions = {"include": include, "exclude": exclude, "only": only}
        queries.append(
            ItemQuery(
                query_id=str(case.get("caseId") or len(queries)),
                text=case["query"],
                category=str(case.get("category") or "precision"),
                gold_ids=tuple(include),
                forbid_ids=tuple(exclude),
                unanswerable=not include,
                extra=(("assertions", json.dumps(assertions)),),
            )
        )
    return ItemUnit("precisionmem", tuple(items), tuple(queries))


def load(
    variant: str = "retrieval", *, limit: int | None = None, allow_download: bool = True
) -> ItemCorpus:
    seed = load_json(SUITE_SPECS["precisionmem_seed"], allow_download=allow_download)
    cases = load_json(SUITE_SPECS["precisionmem_cases"], allow_download=allow_download)
    unit = parse(seed, cases)
    if limit:
        unit = ItemUnit(unit.unit_id, unit.items, unit.queries[:limit])
    return ItemCorpus("precisionmem", variant, (unit,))


SUITE = SuiteInfo(
    id="precisionmem",
    name="PrecisionMemBench",
    variants=("retrieval",),
    license="MIT",
    license_note="",
    dims=("M7",),
    ingest_modes=("items",),
    scorer="assertions",
    loader=load,
)
