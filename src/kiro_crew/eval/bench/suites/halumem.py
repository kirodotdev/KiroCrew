"""HaluMem (optional, off by default): grades what a provider STORED, not only its answers.

Each line of ``HaluMem-Medium.jsonl`` is one user: sessions with a dialogue and the
memory points a perfect memory would extract, plus questions. The dialogue is
replayed; ``read_back`` lists what the provider kept, and each gold memory point is
looked for among the stored texts (extraction recall), and each stored text among the
points (precision).

License CC BY-NC-ND 4.0 (no derivatives): the file is converted here, in memory, on
the machine that runs the benchmark, and no converted copy is ever written.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..datasets import SUITE_SPECS, load_text
from ..item_corpus import BenchItem, ItemCorpus, ItemQuery, ItemUnit, ReplaySession, unit_id_of
from . import SuiteInfo


def _at(raw: object) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        return (
            datetime.strptime(raw, "%b %d, %Y, %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        )
    except ValueError:
        return None


def parse_user(record: Any) -> ItemUnit:
    unit_id = unit_id_of(str(record.get("uuid") or "user"))
    sessions = []
    points: list[str] = []
    items: list[BenchItem] = []
    queries: list[ItemQuery] = []
    for n, session in enumerate(record.get("sessions") or []):
        messages = []
        for m, message in enumerate(session.get("dialogue") or []):
            at = _at(message.get("timestamp"))
            text = str(message.get("content") or "")
            role = "user" if message.get("role") == "user" else "assistant"
            messages.append((role, text, at))
            items.append(BenchItem(f"s{n}t{m}", text, "episode", at, f"s{n}", role))
        sessions.append(ReplaySession(f"s{n}", tuple(messages), _at(session.get("start_time"))))
        points += [
            str(p.get("memory_content"))
            for p in session.get("memory_points") or []
            if p.get("memory_content")
        ]
        for q, question in enumerate(session.get("questions") or []):
            queries.append(
                ItemQuery(
                    query_id=f"{unit_id}:{n}:{q}",
                    text=str(question.get("question") or ""),
                    category=str(question.get("question_type") or "qa"),
                    gold_answer=str(question.get("answer") or ""),
                    unanswerable=not question.get("evidence"),
                )
            )
    extraction = ItemQuery(
        query_id=f"{unit_id}:extraction",
        text="",
        category="extraction",
        extra=(("points", json.dumps(points, ensure_ascii=False)),),
    )
    return ItemUnit(unit_id, tuple(items), (extraction, *queries), tuple(sessions))


def load(
    variant: str = "medium", *, limit: int | None = None, allow_download: bool = True
) -> ItemCorpus:
    text = load_text(SUITE_SPECS["halumem_medium"], allow_download=allow_download)
    units: list[ItemUnit] = []
    for line in text.splitlines():
        if limit and len(units) >= limit:
            break
        if line.strip():
            units.append(parse_user(json.loads(line)))
    return ItemCorpus("halumem", variant, tuple(units))


SUITE = SuiteInfo(
    id="halumem",
    name="HaluMem",
    variants=("medium",),
    license="CC BY-NC-ND 4.0",
    license_note="Non-commercial, no derivatives: converted in memory at run time only.",
    dims=("M9", "M4"),
    ingest_modes=("replay",),
    scorer="extraction",
    needs_reader=True,
    needs_judge=True,
    default_on=False,
    restricted=True,
    loader=load,
)
