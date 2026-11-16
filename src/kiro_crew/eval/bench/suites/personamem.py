"""PersonaMem v2: implicit preferences in a 32K chat history, asked as multiple choice.

``benchmark/text/benchmark.csv`` names each question's persona, its 32K chat history
file, the user's query, the correct answer and the wrong ones. One persona's history
is one unit: its messages are the items (the system persona prompt is left out, so
the preference has to be found in the chat), and every question about that persona
is asked against it. Scored without a judge: the reader picks one of the options.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import random
from typing import Any

from ..datasets import SUITE_SPECS, load_json, load_text, personamem_history_spec
from ..item_corpus import BenchItem, ItemCorpus, ItemQuery, ItemUnit, ReplaySession, unit_id_of
from . import SuiteInfo


def _literal(raw: str) -> Any:
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(raw)
        except (ValueError, SyntaxError):
            continue
    return raw


def parse_rows(text: str) -> dict[str, list[dict[str, str]]]:
    """Benchmark rows grouped by their 32K history file, in file order."""
    csv.field_size_limit(1 << 30)
    groups: dict[str, list[dict[str, str]]] = {}
    for row in csv.DictReader(io.StringIO(text, newline="")):
        link = row.get("chat_history_32k_link") or ""
        if link:
            groups.setdefault(link, []).append(row)
    return groups


def parse_unit(history: Any, rows: list[dict[str, str]], unit_id: str, seed: int = 7) -> ItemUnit:
    messages = history.get("chat_history") if isinstance(history, dict) else history
    items: list[BenchItem] = []
    replay = []
    for n, message in enumerate(messages or []):
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        text = str(message.get("content") or "")
        role = "user" if message.get("role") == "user" else "assistant"
        items.append(BenchItem(f"t{n}", text, "episode", float(n), "chat", role))
        replay.append((role, text, float(n)))
    rng = random.Random(seed)
    queries = []
    for n, row in enumerate(rows):
        query = _literal(row.get("user_query") or "")
        text = str(query.get("content") if isinstance(query, dict) else query)
        wrong = _literal(row.get("incorrect_answers") or "[]")
        options = [row.get("correct_answer") or ""] + [str(w) for w in (wrong or [])]
        rng.shuffle(options)
        correct = options.index(row.get("correct_answer") or "")
        queries.append(
            ItemQuery(
                query_id=f"{unit_id}:{n}",
                text=str(text),
                category="preference",
                gold_answer=row.get("correct_answer") or "",
                extra=(
                    ("options", json.dumps(options, ensure_ascii=False)),
                    ("correct", str(correct)),
                ),
            )
        )
    return ItemUnit(unit_id, tuple(items), tuple(queries), (ReplaySession("chat", tuple(replay)),))


def load(
    variant: str = "32k", *, limit: int | None = None, allow_download: bool = True
) -> ItemCorpus:
    text = load_text(SUITE_SPECS["personamem_v2_text"], allow_download=allow_download)
    groups = parse_rows(text)
    units = []
    for link, rows in list(groups.items())[: limit or None]:
        history = load_json(personamem_history_spec(link), allow_download=allow_download)
        units.append(parse_unit(history, rows, unit_id_of(link.rsplit("/", 1)[-1][:-5])))
    return ItemCorpus("personamem", variant, tuple(units))


SUITE = SuiteInfo(
    id="personamem",
    name="PersonaMem v2",
    variants=("32k",),
    license="CC BY 4.0",
    license_note="",
    dims=("M6",),
    ingest_modes=("replay", "items"),
    scorer="mc",
    needs_reader=True,
    loader=load,
)
