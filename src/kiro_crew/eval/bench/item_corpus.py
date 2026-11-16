"""The neutral shape a provider benchmark runs on: units of items and queries.

A *unit* is one isolated store's worth of work -- one LongMemEval haystack, one
LoCoMo conversation, one BEAM chat, one PrecisionMemBench case group, or a whole
own-memory copy. A unit holds the items to write (each with the id it keeps as its
``origin.id``, its time and kind), the sessions to replay for a provider that
extracts for itself, and the queries with their gold ids.

Retrieval is scored by mapping a returned item back to gold through ``origin.id``.
A provider that does not keep origin cannot be scored that way; its hits are mapped
by text overlap instead (:func:`proxy_match`), and that number is labelled "proxy"
wherever it is shown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .corpus import BenchInstance, BenchQuery, Corpus
from .ingest import parse_timestamp

#: A proxy match needs this token F1 between a returned text and a gold item's text.
PROXY_F1 = 0.5
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class BenchItem:
    """One item a unit writes. ``item_id`` becomes the written item's ``origin.id``."""

    item_id: str
    text: str
    kind: str = "episode"
    at: float | None = None
    session_id: str = ""
    speaker: str = ""
    scopes: tuple[str, ...] = ()
    meta: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReplaySession:
    """One session to replay in date order: ``(role, text, at)`` per message."""

    session_id: str
    messages: tuple[tuple[str, str, float | None], ...]
    at: float | None = None


@dataclass(frozen=True)
class ItemQuery:
    query_id: str
    text: str
    category: str = "unknown"
    #: Item ids that answer the query; empty for an abstention query.
    gold_ids: tuple[str, ...] = ()
    #: Item ids that must NOT come back (a superseded value, a forgotten fact).
    forbid_ids: tuple[str, ...] = ()
    as_of: float | None = None
    gold_answer: str | None = None
    unanswerable: bool = False
    #: Free-form scorer input (choices for multiple choice, a rubric, assertions).
    extra: tuple[tuple[str, str], ...] = ()

    def extra_value(self, key: str, default: str = "") -> str:
        return next((v for k, v in self.extra if k == key), default)


@dataclass(frozen=True)
class ItemUnit:
    unit_id: str
    items: tuple[BenchItem, ...]
    queries: tuple[ItemQuery, ...]
    sessions: tuple[ReplaySession, ...] = ()

    def by_id(self) -> dict[str, BenchItem]:
        return {item.item_id: item for item in self.items}


@dataclass(frozen=True)
class ItemCorpus:
    suite: str
    variant: str
    units: tuple[ItemUnit, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def query_count(self) -> int:
        return sum(len(u.queries) for u in self.units)


_UNIT_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def unit_id_of(raw: str) -> str:
    """A unit id a bench store id can carry (letters, digits, ``._-``, at most 64)."""
    cleaned = _UNIT_ID.sub("-", raw).strip("-.") or "unit"
    return cleaned[:64]


def _epoch(raw: str) -> float | None:
    parsed = parse_timestamp(raw) if raw else None
    return parsed.timestamp() if parsed is not None else None


def _query_of(query: BenchQuery) -> ItemQuery:
    return ItemQuery(
        query_id=query.query_id,
        text=query.question,
        category=query.category,
        gold_ids=query.gold_turn_ids,
        as_of=_epoch(query.ask_date),
        gold_answer=query.gold_answer,
        unanswerable=query.unanswerable,
    )


def unit_from_instance(inst: BenchInstance, *, speaker_prefix: bool = True) -> ItemUnit:
    """A conversational instance as a unit: one item per turn, one session per session.

    The item id is the turn id, so a provider that keeps origin is scored on the same
    gold the existing retrieval ruler uses.
    """
    inst = inst.resolve_gold()
    items: list[BenchItem] = []
    sessions: list[ReplaySession] = []
    for session in inst.sessions:
        session_at = _epoch(session.timestamp)
        messages = []
        for turn in session.turns:
            at = _epoch(turn.timestamp) or session_at
            text = f"{turn.speaker}: {turn.text}" if speaker_prefix and turn.speaker else turn.text
            items.append(
                BenchItem(turn.turn_id, text, "episode", at, session.session_id, turn.speaker)
            )
            # Only an explicit assistant turn is the assistant's: LoCoMo names its two
            # human participants, and both are users to a provider's extractor.
            role = "assistant" if turn.speaker.lower() == "assistant" else "user"
            messages.append((role, text, at))
        sessions.append(ReplaySession(session.session_id, tuple(messages), session_at))
    queries = tuple(_query_of(q) for q in inst.queries)
    return ItemUnit(unit_id_of(inst.instance_id), tuple(items), queries, tuple(sessions))


def from_corpus(corpus: Corpus, *, limit: int | None = None) -> ItemCorpus:
    instances = corpus.instances[:limit] if limit else corpus.instances
    return ItemCorpus(
        suite=corpus.name,
        variant=corpus.variant,
        units=tuple(unit_from_instance(i) for i in instances),
        notes=corpus.notes,
    )


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def token_f1(a: str, b: str) -> float:
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    remaining = list(right)
    common = 0
    for token in left:
        if token in remaining:
            remaining.remove(token)
            common += 1
    if not common:
        return 0.0
    precision, recall = common / len(left), common / len(right)
    return 2 * precision * recall / (precision + recall)


def proxy_match(text: str, candidates: Iterable[BenchItem]) -> str | None:
    """The gold-side item a returned text most likely is, by token F1 >= 0.5.

    shortcut: linear in the unit's items per hit; fine for the few hundred items a
    proxy-scored unit holds, upgrade to an inverted index if units grow past 10K.
    """
    best, best_f1 = None, PROXY_F1
    for item in candidates:
        f1 = token_f1(text, item.text)
        if f1 >= best_f1:
            best, best_f1 = item.item_id, f1
    return best


#: The rank a hit holds when it names no unit item (or repeats one): never gold.
UNMATCHED = ""


def ranked_ids(
    hits: Sequence[tuple[str | None, str]], unit: ItemUnit, *, proxy: bool
) -> tuple[list[str], int]:
    """Hits ``(origin id or None, text)`` as unit item ids in rank order.

    Returns the ids and how many hits could not be attributed. With *proxy* a hit
    with no origin is matched by text; without it, it stays unattributed. Every hit
    keeps its rank: an unattributed or repeated one holds :data:`UNMATCHED`, so a
    gold item below it is not moved up a cutoff and it still counts as noise.
    """
    known = unit.by_id()
    out: list[str] = []
    missed = 0
    for origin_id, text in hits:
        item_id = origin_id if origin_id in known else None
        if item_id is None and proxy:
            item_id = proxy_match(text, known.values())
        if item_id is None:
            missed += 1
        out.append(UNMATCHED if item_id is None or item_id in out else item_id)
    return out, missed
