"""BEAM: ten memory abilities over very long chats (the 100K and 500K splits).

Upstream shape: ``chat.json`` is a list of batches, each with ``turns`` (a list of
sessions, each a list of messages ``{role, id, time_anchor, content}``), and
``probing_questions.json`` maps each ability to questions whose
``source_chat_ids`` name the message ids that answer them. A message id becomes the
item id, so a provider that keeps origin is scored by recall@k on those ids; every
ability is also graded by a rubric judge in answer mode.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..datasets import beam_spec, load_json
from ..item_corpus import BenchItem, ItemCorpus, ItemQuery, ItemUnit, ReplaySession
from . import SuiteInfo

#: Ability -> the report category and dimension it feeds.
ABILITIES = {
    "information_extraction": "M1",
    "multi_session_reasoning": "M2",
    "temporal_reasoning": "M3",
    "event_ordering": "M3",
    "knowledge_update": "M4",
    "contradiction_resolution": "M4",
    "abstention": "M5",
    "preference_following": "M6",
    "instruction_following": "M6",
    "summarization": "",
}
CONVERSATIONS = {"100K": 20, "500K": 35}


def _anchor(raw: object) -> float | None:
    if not isinstance(raw, str):
        return None
    for fmt in ("%B-%d-%Y", "%b-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _ids(raw: object) -> tuple[list[str], list[str]]:
    """``(gold, forbidden)`` message ids; an update's original value is forbidden."""
    if isinstance(raw, list):
        return [f"m{i}" for i in raw if isinstance(i, int)], []
    if isinstance(raw, dict):
        old_keys = ("original_info", "first_statement")
        gold: list[str] = []
        old: list[str] = []
        for key, value in raw.items():
            ids = [f"m{i}" for i in value if isinstance(i, int)] if isinstance(value, list) else []
            (old if key in old_keys else gold).extend(ids)
        return gold, old
    return [], []


def parse(chat: Any, questions: Any, unit_id: str) -> ItemUnit:
    items: list[BenchItem] = []
    sessions: list[ReplaySession] = []
    if not isinstance(chat, list):
        raise ValueError("BEAM chat.json must be a list of batches")
    for batch in chat:
        for n, session in enumerate((batch or {}).get("turns") or []):
            sid = f"b{(batch or {}).get('batch_number', 0)}s{n}"
            messages = []
            session_at = None
            for message in session or []:
                if not isinstance(message, dict) or not isinstance(message.get("id"), int):
                    continue
                at = _anchor(message.get("time_anchor"))
                session_at = session_at or at
                text = str(message.get("content") or "")
                role = "user" if message.get("role") == "user" else "assistant"
                items.append(BenchItem(f"m{message['id']}", text, "episode", at, sid, role))
                messages.append((role, text, at))
            if messages:
                sessions.append(ReplaySession(sid, tuple(messages), session_at))
    queries: list[ItemQuery] = []
    for ability, entries in (questions or {}).items():
        if ability not in ABILITIES or not isinstance(entries, list):
            continue
        for n, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("question"), str):
                continue
            gold, old = _ids(entry.get("source_chat_ids"))
            answer = next(
                (entry[k] for k in ("answer", "ideal_answer", "ideal_response") if entry.get(k)),
                None,
            )
            rubric = entry.get("rubric") or entry.get("compliance_indicators") or []
            queries.append(
                ItemQuery(
                    query_id=f"{unit_id}:{ability}:{n}",
                    text=entry["question"],
                    category=ability,
                    gold_ids=tuple(gold),
                    forbid_ids=tuple(old),
                    gold_answer=str(answer) if answer is not None else None,
                    unanswerable=ability == "abstention",
                    extra=(("rubric", json.dumps(rubric, ensure_ascii=False)),),
                )
            )
    return ItemUnit(unit_id, tuple(items), tuple(queries), tuple(sessions))


def load(
    variant: str = "100K", *, limit: int | None = None, allow_download: bool = True
) -> ItemCorpus:
    if variant not in CONVERSATIONS:
        raise ValueError(f"BEAM variant must be one of {', '.join(CONVERSATIONS)}")
    count = min(limit or CONVERSATIONS[variant], CONVERSATIONS[variant])
    units = []
    for n in range(1, count + 1):
        chat = load_json(beam_spec(variant, n, "chat"), allow_download=allow_download)
        questions = load_json(beam_spec(variant, n, "questions"), allow_download=allow_download)
        units.append(parse(chat, questions, f"beam-{variant}-{n}"))
    return ItemCorpus("beam", variant, tuple(units))


SUITE = SuiteInfo(
    id="beam",
    name="BEAM",
    variants=tuple(CONVERSATIONS),
    license="CC BY-SA 4.0 (data), MIT (code)",
    license_note="Share-alike: a redistributed derivative keeps the license.",
    dims=("M1", "M2", "M3", "M4", "M5", "M6"),
    ingest_modes=("replay", "items"),
    scorer="rubric",
    needs_reader=True,
    needs_judge=True,
    loader=load,
)
