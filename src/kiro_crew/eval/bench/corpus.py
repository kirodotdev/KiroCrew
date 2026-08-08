"""The neutral corpus contract every memory benchmark normalizes into.

Why a contract instead of two bespoke runners: LongMemEval and LoCoMo disagree
on almost every surface detail — LongMemEval is a JSON array of 500 instances
each owning its own haystack and exactly one question, while LoCoMo is 10
conversations that each carry ~199 questions against one shared haystack whose
sessions are *string-keyed siblings* of a flat dict rather than a list. Scoring
also differs in kind: LongMemEval's official metric is an LLM judge with five
different prompts selected by question type, LoCoMo's is token-level F1 computed
differently per category with one category not scored by F1 at all.

None of that variance belongs in the ruler. The ruler asks one question — did
the memory layer surface the evidence the question needed — and that question is
identical for both datasets once the corpus is expressed as *instances*, each
holding a haystack of sessions and a set of queries with gold evidence ids. The
per-dataset weirdness is confined to the adapters, and the per-dataset scoring is
confined to the scorers.

Shape note that drives everything downstream: one instance == one memory store.
LongMemEval's ``longmemeval_s`` runs ~40 sessions per instance across 500
instances; pouring all of that into a single store would blow through
``episodic_max`` (10 000) and start tombstoning by ``importance ASC, created_at
ASC``, silently deleting the very evidence being scored. Instances are therefore
independent measurement units, never merged.

Everything here is frozen and hashable so a corpus can be fingerprinted and a
run's inputs pinned in the report.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterator

# ── Normalized category vocabulary ───────────────────────────────────────────
# Both datasets bucket their questions, but with disjoint vocabularies and (in
# LoCoMo's case) bare integers whose meaning is only partially confirmed in the
# upstream code. Adapters map onto these names for cross-dataset reporting and
# ALSO keep the dataset-native value in ``raw_category`` so a number in a report
# can always be traced back to the file it came from. Nothing in the ruler
# branches on these; they are report buckets only.
CAT_SINGLE_HOP = "single_hop"
CAT_MULTI_HOP = "multi_hop"
CAT_TEMPORAL = "temporal"
CAT_KNOWLEDGE_UPDATE = "knowledge_update"
CAT_PREFERENCE = "preference"
CAT_COMMONSENSE = "commonsense"
CAT_ADVERSARIAL = "adversarial"
CAT_UNKNOWN = "unknown"

CATEGORIES = (
    CAT_SINGLE_HOP,
    CAT_MULTI_HOP,
    CAT_TEMPORAL,
    CAT_KNOWLEDGE_UPDATE,
    CAT_PREFERENCE,
    CAT_COMMONSENSE,
    CAT_ADVERSARIAL,
    CAT_UNKNOWN,
)


@dataclass(frozen=True)
class BenchTurn:
    """One utterance in a haystack session.

    ``turn_id`` is the join key for turn-level recall and must be unique within
    the instance. LoCoMo supplies one natively (``dia_id``, e.g. ``"D1:9"``);
    LongMemEval has no turn id at all, so its adapter synthesizes one from the
    session id and the turn's index. Treat it as opaque — LongMemEval's session
    ids carry an ``answer_`` prefix on evidence sessions, and parsing structure
    out of that would leak the label into the ranking.

    ``speaker`` is deliberately a free string rather than a role enum: LongMemEval
    uses ``user``/``assistant``, LoCoMo uses the participants' actual names.

    ``timestamp`` is the dataset's raw string, unparsed and unnormalized. The two
    datasets use different formats (``"2023/04/10 (Mon) 23:07"`` vs ``"1:56 pm on
    8 May, 2023"``); parsing is the ingester's job because only it knows whether
    the run wants real dates or not.

    ``is_evidence`` is turn-level ground truth where the dataset states it
    directly (LongMemEval's ``has_answer``). It is NOT the same thing as being in
    some query's gold set — a turn can be marked evidence for a question that
    this instance's query list does not contain. Scoring joins on the query's
    ``gold_turn_ids``; this field is retained for corpus diagnostics only.
    """

    turn_id: str
    session_id: str
    speaker: str
    text: str
    timestamp: str = ""
    is_evidence: bool = False


@dataclass(frozen=True)
class BenchSession:
    """One conversation session — the unit of session-level recall."""

    session_id: str
    turns: tuple[BenchTurn, ...]
    timestamp: str = ""

    def __post_init__(self) -> None:
        for t in self.turns:
            if t.session_id != self.session_id:
                raise ValueError(
                    f"turn {t.turn_id!r} claims session {t.session_id!r} "
                    f"but is filed under {self.session_id!r}"
                )


@dataclass(frozen=True)
class BenchQuery:
    """One question asked against an instance's haystack.

    ``gold_answer`` is intentionally permitted to be ``None`` and must never be
    treated as a string to match against. Three upstream cases force this:

    * LoCoMo's adversarial items (category 5) carry ``adversarial_answer`` and no
      ``answer`` key at all — 444 of 1 986 items.
    * LongMemEval's ``single-session-preference`` items put a *prose rubric* in
      ``answer`` ("The user would prefer responses that suggest resources
      specifically tailored to Adobe Premiere Pro…"), which the official judge
      feeds in under a ``Rubric:`` header rather than as a correct answer.
    * LongMemEval's abstention items put an *explanation of unanswerability* in
      ``answer``, fed to the judge under an ``Explanation:`` header.

    ``unanswerable`` marks that third family plus LoCoMo's adversarial items: the
    correct behavior is refusal, so a scorer that rewards recall would reward
    exactly the wrong thing. Note LongMemEval does not expose abstention as a
    ``question_type`` — it is a ``_abs`` suffix on ``question_id``.

    ``gold_session_ids`` / ``gold_turn_ids`` are the evidence sets the retrieval
    ruler scores against. Either may be empty: 4 LoCoMo items ship an empty
    evidence list, and 7 of its 2 815 evidence refs are dangling (they name a
    ``dia_id`` present in no conversation). Queries with no resolvable gold are
    excluded from retrieval metrics rather than counted as misses — scoring a
    question whose ground truth is missing measures the dataset, not the system.
    """

    query_id: str
    question: str
    category: str
    gold_answer: str | None = None
    adversarial_answer: str | None = None
    gold_session_ids: tuple[str, ...] = ()
    gold_turn_ids: tuple[str, ...] = ()
    unanswerable: bool = False
    ask_date: str = ""
    raw_category: str = ""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r} (see corpus.CATEGORIES)")

    @property
    def scorable_retrieval(self) -> bool:
        """False when the dataset gives no resolvable evidence to score against."""
        return bool(self.gold_session_ids or self.gold_turn_ids)


@dataclass(frozen=True)
class BenchInstance:
    """One haystack plus the queries asked against it. Also one memory store.

    LongMemEval yields 500 of these with one query each; LoCoMo yields 10 with
    ~199 each. That asymmetry is why the ingest cost of the two datasets differs
    by two orders of magnitude for a comparable number of questions, and why
    LoCoMo is the cheaper default.
    """

    instance_id: str
    sessions: tuple[BenchSession, ...]
    queries: tuple[BenchQuery, ...]

    @property
    def turn_count(self) -> int:
        return sum(len(s.turns) for s in self.sessions)

    def iter_turns(self) -> Iterator[BenchTurn]:
        for s in self.sessions:
            yield from s.turns

    def resolve_gold(self) -> "BenchInstance":
        """Drop gold refs that name a turn or session absent from this haystack.

        LoCoMo ships dangling ``dia_id`` refs; leaving them in would depress
        recall for a reason that has nothing to do with the memory layer. This
        rewrites each query's gold sets to the resolvable subset, which may empty
        them — ``scorable_retrieval`` then excludes the query downstream.
        """
        known_turns = {t.turn_id for t in self.iter_turns()}
        known_sessions = {s.session_id for s in self.sessions}
        fixed = tuple(
            BenchQuery(
                query_id=q.query_id,
                question=q.question,
                category=q.category,
                gold_answer=q.gold_answer,
                adversarial_answer=q.adversarial_answer,
                gold_session_ids=tuple(s for s in q.gold_session_ids if s in known_sessions),
                gold_turn_ids=tuple(t for t in q.gold_turn_ids if t in known_turns),
                unanswerable=q.unanswerable,
                ask_date=q.ask_date,
                raw_category=q.raw_category,
            )
            for q in self.queries
        )
        return BenchInstance(self.instance_id, self.sessions, fixed)


@dataclass(frozen=True)
class Corpus:
    """A named, fingerprintable set of instances.

    ``fingerprint`` exists because memory-benchmark numbers are notoriously
    incomparable across harnesses — the field's own reproducibility work
    identifies answer model, judge, ingestion granularity and prompt as the
    dominant score drivers. A run report that cannot state exactly which bytes it
    read is not a measurement anyone can act on, so the corpus hashes its own
    normalized content and the report pins that hash.
    """

    name: str
    variant: str
    instances: tuple[BenchInstance, ...]
    source_path: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def query_count(self) -> int:
        return sum(len(i.queries) for i in self.instances)

    @property
    def session_count(self) -> int:
        return sum(len(i.sessions) for i in self.instances)

    @property
    def turn_count(self) -> int:
        return sum(i.turn_count for i in self.instances)

    def fingerprint(self) -> str:
        """SHA-256 over the normalized corpus, independent of dict ordering.

        Hashes the ids and text that actually reach the store plus each query's
        gold sets — not the raw file — so two different files that normalize to
        the same corpus fingerprint identically, and a change in the adapter's
        normalization shows up as a changed fingerprint (which is the point).
        """
        h = hashlib.sha256()
        h.update(f"{self.name}\x00{self.variant}\x00".encode())
        for inst in self.instances:
            h.update(f"I\x00{inst.instance_id}\x00".encode())
            for s in inst.sessions:
                h.update(f"S\x00{s.session_id}\x00{s.timestamp}\x00".encode())
                for t in s.turns:
                    h.update(f"T\x00{t.turn_id}\x00{t.speaker}\x00{t.text}\x00".encode())
            for q in sorted(inst.queries, key=lambda x: x.query_id):
                h.update(
                    (
                        "Q\x00"
                        + json.dumps(
                            [
                                q.query_id,
                                q.question,
                                q.category,
                                sorted(q.gold_session_ids),
                                sorted(q.gold_turn_ids),
                                q.unanswerable,
                            ],
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\x00"
                    ).encode()
                )
        return h.hexdigest()

    def subset(self, *, instances: int | None = None, queries_per_instance: int | None = None
               ) -> "Corpus":
        """A deterministic head-slice, for smoke runs and tests.

        Head-slicing rather than sampling on purpose: a random subset would make
        two runs incomparable unless the seed were also pinned and reported, and
        a benchmark whose subset changes between arms cannot support a paired
        A/B. The variant string records the slice so the report cannot silently
        claim a full-corpus number.
        """
        insts = self.instances[:instances] if instances is not None else self.instances
        if queries_per_instance is not None:
            insts = tuple(
                BenchInstance(i.instance_id, i.sessions, i.queries[:queries_per_instance])
                for i in insts
            )
        suffix = f"[{instances or 'all'}i/{queries_per_instance or 'all'}q]"
        return Corpus(
            name=self.name,
            variant=f"{self.variant}{suffix}",
            instances=insts,
            source_path=self.source_path,
            notes=self.notes,
        )
