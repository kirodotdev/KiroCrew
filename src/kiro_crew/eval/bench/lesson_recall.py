"""Deterministic recall harness for lesson ranking.

Lessons are the stored corrections and preferences ranked into a prompt, both at
session start and on an explicit ``memory_recall``. This harness answers one
question: *given a labeled set of ``(request -> rules that should apply)`` pairs,
does lesson ranking put the right rules near the top?* It ranks through the same
``rank_lessons`` call both production paths use, over a throwaway
``VectorMemoryStore`` the golden rules are written into with ``write_lesson``, so a
change to the ranker shows up here as an exact delta between two commits.

It is separate from ``bench kb-retrieval``, which measures the Knowledge Library,
and from ``bench retrieval``, which measures episodic memory over public corpora.

Every query is scored on the full ranked list. Lesson ranking orders every
in-scope rule and the character budget then cuts the tail, so recall at a small
cut-off is the number that decides which rules a prompt carries.

The query classes are the cases a lesson ranker gets wrong in different ways: a
distinctive shared term, a paraphrase sharing no word with its rule, a request
whose common words favour unrelated rules, a long first message that shares
ordinary words with most rules, and a request of a few words.

The default embedder is the deterministic toy stand-in, so the harness runs
anywhere and a test can assert on its ranking. It measures term overlap, not
meaning, so its paraphrase scores are expected to be low and its numbers are a
plumbing check. ``--real-embedder`` uses the in-process model for a semantic run.
"""

from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

from kiro_crew import vector_memory
from kiro_crew.eval.bench.errors import BenchRefusal
from kiro_crew.eval.bench.kb_retrieval import mrr_at_k
from kiro_crew.eval.bench.retrieval import ndcg_at_k, recall_all_at_k, recall_any_at_k
from kiro_crew.eval.bench.safepath import UnsafePathError, read_text_nofollow
from kiro_crew.eval.bench.toy_embedder import TOY_EMBEDDER_ID, toy_embed_fn
from kiro_crew.vector_memory import LessonWriteOutcome, VectorMemoryStore
from kiro_crew.vector_memory_runtime import lessons as _lessons
from kiro_crew.vector_memory_runtime.embedding import _RecallQuery

#: Reported as the embedder identity for a run with no vectors, so a keyword-only
#: run can never carry a semantic embedder label.
KEYWORD_ONLY_EMBEDDER_ID = "keyword-only (no embeddings)"

LESSON_QUERY_CLASSES: tuple[str, ...] = (
    "distinctive_term",
    "paraphrase",
    "common_word_trap",
    "long_request",
    "short_request",
)

#: 1 and 3 are where a tight lessons budget cuts; 5 and 10 show the tail.
DEFAULT_LESSON_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)

#: A hand-authored golden set has no business approaching this.
_GOLDEN_MAX_BYTES = 4 * 1024 * 1024

#: The stamp the first golden rule is written at; each later rule is one
#: microsecond newer.
_FIRST_RULE_WRITTEN_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class LessonGoldenSetError(BenchRefusal):
    """The golden set is malformed, or the store did not keep it as written.

    A refusal rather than a number: a rule ``write_lesson`` merged into another
    one is absent from the ranked list under its own id, so every query naming
    it would score as a miss the ranker never made.
    """


@dataclass(frozen=True)
class LessonRule:
    """One stored rule. ``id`` is the gold label queries name."""

    id: str
    rule: str
    negative: str | None = None

    @classmethod
    def from_raw(cls, raw: object) -> "LessonRule":
        if not isinstance(raw, dict):
            raise LessonGoldenSetError(f"malformed rule entry: {raw!r}")
        rule_id, rule, negative = raw.get("id"), raw.get("rule"), raw.get("negative")
        if not isinstance(rule_id, str) or not rule_id or not isinstance(rule, str):
            raise LessonGoldenSetError(f"rule entry needs a string id and rule: {raw!r}")
        if not rule.strip():
            raise LessonGoldenSetError(f"rule {rule_id!r} has empty text")
        if negative is not None and not isinstance(negative, str):
            raise LessonGoldenSetError(f"rule {rule_id!r} has a non-string negative")
        return cls(id=rule_id, rule=rule.strip(), negative=(negative or "").strip() or None)


@dataclass(frozen=True)
class LessonQuery:
    """One labeled request and the rules that should rank first for it."""

    id: str
    query_class: str
    request: str
    gold_rule_ids: tuple[str, ...]

    @classmethod
    def from_raw(cls, raw: object) -> "LessonQuery":
        if not isinstance(raw, dict):
            raise LessonGoldenSetError(f"malformed query entry: {raw!r}")
        query_id, query_class = raw.get("id"), raw.get("class")
        request, gold = raw.get("request"), raw.get("gold_rule_ids")
        if not isinstance(query_id, str) or not query_id:
            raise LessonGoldenSetError(f"query entry needs a string id: {raw!r}")
        if query_class not in LESSON_QUERY_CLASSES:
            raise LessonGoldenSetError(
                f"query {query_id!r} has unknown class {query_class!r}; "
                f"known: {', '.join(LESSON_QUERY_CLASSES)}"
            )
        if not isinstance(request, str) or not request.strip():
            raise LessonGoldenSetError(f"query {query_id!r} has no request text")
        if not isinstance(gold, list) or not gold or not all(isinstance(g, str) for g in gold):
            raise LessonGoldenSetError(f"query {query_id!r} needs a non-empty gold_rule_ids list")
        return cls(
            id=query_id,
            query_class=query_class,
            request=request,
            gold_rule_ids=tuple(gold),
        )


@dataclass(frozen=True)
class LessonGoldenSet:
    name: str
    rules: tuple[LessonRule, ...]
    queries: tuple[LessonQuery, ...]

    def validate(self) -> None:
        """Refuse a set whose labels cannot be scored as written."""
        if not self.rules or not self.queries:
            raise LessonGoldenSetError("golden set needs at least one rule and one query")
        texts_by_owner = [(r.id, (r.id, r.rule, r.negative or "")) for r in self.rules] + [
            (q.id, (q.id, q.request, *q.gold_rule_ids)) for q in self.queries
        ]
        for owner, owned_texts in texts_by_owner:
            try:
                for text in owned_texts:
                    text.encode("utf-8")
            except UnicodeEncodeError as exc:
                # A JSON escape such as "\ud800" parses to a lone surrogate,
                # which the store cannot encode when it writes the rule.
                raise LessonGoldenSetError(f"{owner!r} holds text that is not valid UTF-8") from exc
        rule_ids = [r.id for r in self.rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise LessonGoldenSetError("golden set has duplicate rule ids")
        texts = [r.rule.casefold() for r in self.rules]
        if len(set(texts)) != len(texts):
            raise LessonGoldenSetError("golden set has two rules with the same text")
        query_ids = [q.id for q in self.queries]
        if len(set(query_ids)) != len(query_ids):
            raise LessonGoldenSetError("golden set has duplicate query ids")
        known = set(rule_ids)
        for query in self.queries:
            unknown = [g for g in query.gold_rule_ids if g not in known]
            if unknown:
                raise LessonGoldenSetError(
                    f"query {query.id!r} names rules no entry defines: {', '.join(unknown)}"
                )

    @classmethod
    def from_json(cls, path: str | Path) -> "LessonGoldenSet":
        p = Path(path)
        try:
            text = read_text_nofollow(p, what="golden set", max_bytes=_GOLDEN_MAX_BYTES)
        except UnsafePathError as exc:
            raise LessonGoldenSetError(str(exc)) from exc
        except OSError as exc:
            raise LessonGoldenSetError(
                f"golden set could not be read: {str(p)!r} ({exc!r})"
            ) from exc
        except UnicodeError as exc:
            raise LessonGoldenSetError(f"golden set is not valid UTF-8: {str(p)!r}") from exc
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise LessonGoldenSetError(f"golden set is not parseable JSON: {str(p)!r}") from exc
        if not isinstance(raw, dict):
            raise LessonGoldenSetError(f"golden set must be a JSON object: {str(p)!r}")
        name = raw.get("name", p.stem)
        rules_raw, queries_raw = raw.get("rules"), raw.get("queries")
        if not isinstance(name, str) or not name or not name.isprintable():
            raise LessonGoldenSetError(f"golden set 'name' must be printable text: {str(p)!r}")
        if not isinstance(rules_raw, list) or not isinstance(queries_raw, list):
            raise LessonGoldenSetError(f"golden set needs 'rules' and 'queries' lists: {str(p)!r}")
        golden = cls(
            name=name,
            rules=tuple(LessonRule.from_raw(r) for r in rules_raw),
            queries=tuple(LessonQuery.from_raw(q) for q in queries_raw),
        )
        golden.validate()
        return golden


def default_lesson_golden_set_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "lesson_golden_v1.json"


@dataclass(frozen=True)
class LessonQueryResult:
    query_id: str
    query_class: str
    ranked_rule_ids: tuple[str, ...]
    recall_any: dict[int, float]
    recall_all: dict[int, float]
    ndcg: dict[int, float]
    mrr: dict[int, float]


@dataclass
class LessonRecallReport:
    golden_set: str
    embedder_id: str
    k_values: tuple[int, ...]
    results: list[LessonQueryResult] = field(default_factory=list)

    def _require_k(self, k: int) -> None:
        if k not in self.k_values:
            # A cut-off that was never computed would read as a 0.0 score.
            raise ValueError(f"k={k} was not computed; computed: {list(self.k_values)}")

    @staticmethod
    def _means(results: Sequence[LessonQueryResult], k: int) -> dict[str, float]:
        n = len(results)
        return {
            "recall_any": sum(r.recall_any[k] for r in results) / n,
            "recall_all": sum(r.recall_all[k] for r in results) / n,
            "ndcg": sum(r.ndcg[k] for r in results) / n,
            "mrr": sum(r.mrr[k] for r in results) / n,
            "n": float(n),
        }

    def headline(self, k: int = 3) -> dict[str, float]:
        self._require_k(k)
        return self._means(self.results, k)

    def by_class(self, k: int) -> dict[str, dict[str, float]]:
        self._require_k(k)
        out: dict[str, dict[str, float]] = {}
        for query_class in LESSON_QUERY_CLASSES:
            members = [r for r in self.results if r.query_class == query_class]
            if members:
                out[query_class] = self._means(members, k)
        return out


def _write_rules(store: VectorMemoryStore, rules: Sequence[LessonRule]) -> dict[str, str]:
    """Write every rule and return ``{stored rule text: rule id}``.

    ``write_lesson`` deduplicates by substring, keyword overlap and cosine, so a
    rule can be absorbed by an earlier one or replace it. Either way the set no
    longer holds the rule under its own id, which is refused here by name. A
    write that inserted and superseded nothing leaves every earlier rule in
    place, so the store then holds exactly the golden rules.

    Each rule is stamped one microsecond after the one before it, not at the
    wall clock. ``get_lessons`` orders rows newest first and by key within one
    stamp, and ``rank_lessons`` keeps that order for rules tied on every score.
    A coarse clock (about 15.6 ms on Windows under Python 3.12) puts
    back-to-back writes on one stamp in groups that differ from run to run, so
    two runs would rank the same tied rules differently.
    """
    for index, rule in enumerate(rules):
        with _store_clock_at(_FIRST_RULE_WRITTEN_AT + timedelta(microseconds=index)):
            result = store.write_lesson(rule.rule, negative=rule.negative)
        if result.outcome is not LessonWriteOutcome.INSERTED or result.superseded:
            raise LessonGoldenSetError(
                f"write_lesson did not store rule {rule.id!r} as written "
                f"(outcome {result.outcome.value}, reason {result.reason!r}); "
                "reword it so the store's dedup does not merge it with another rule"
            )
    return {rule.rule: rule.id for rule in rules}


@contextmanager
def _store_clock_at(instant: datetime) -> Iterator[None]:
    """Date the rows the store stamps through ``vector_memory._now_iso`` at *instant*.

    A lesson row's ``created_at`` and ``updated_at`` come from that facade seam,
    read at call time, so the write itself runs unchanged. Record-metadata rows
    keep the wall clock; nothing orders on them. The replacement is process-wide
    while it holds, which is why it covers one write and is restored after it.
    The fixed-width form keeps string order, which is what ``ORDER BY
    updated_at`` compares, equal to time order.
    """
    stamp = instant.isoformat(timespec="microseconds")
    original = vector_memory._now_iso
    vector_memory._now_iso = lambda: stamp
    try:
        yield
    finally:
        vector_memory._now_iso = original


def _ranked_rule_ids(
    store: VectorMemoryStore,
    request: str,
    rule_id_by_text: dict[str, str],
    *,
    use_embeddings: bool,
) -> tuple[str, ...]:
    """Rank every stored rule for *request* the way ``get_lessons_context`` does.

    The rows, the renderable entries and the ranking call are the ones
    ``get_lessons_context`` uses, and the query vector is built by
    ``startup_lesson_query``, which embeds the request exactly as explicit recall
    does. Calling ``rank_lessons`` directly rather than parsing the rendered block
    keeps the full order, which the budget would otherwise cut.
    """
    entries = _lessons._renderable_entries(
        store._eligible_rows(store.get_lessons(), "directive"), project_dir=None
    )
    if use_embeddings:
        recall_query = _lessons.startup_lesson_query(store, request)
        if not recall_query.vector:
            raise LessonGoldenSetError(f"the request was not embedded: {request[:60]!r}")
    else:
        recall_query = _RecallQuery(None, None, None)
    ranked = store._rank_lessons(entries, request, recall_query=recall_query)
    return tuple(rule_id_by_text[_stored_rule_text(row)] for row, _text in ranked)


def _stored_rule_text(row: dict) -> str:
    fields = _lessons._lesson_fields(json.loads(row["value_json"]))
    return fields[0] if fields else ""


def run_lesson_recall(
    golden: LessonGoldenSet,
    *,
    embed_fn: Callable[[str], list[float] | None] | None = None,
    embedder_id: str = TOY_EMBEDDER_ID,
    k_values: Sequence[int] = DEFAULT_LESSON_K_VALUES,
    use_embeddings: bool = True,
) -> LessonRecallReport:
    """Score *golden* against ``rank_lessons`` and return the report.

    The store lives in a private temp directory removed on exit, so no live
    memory is read or written. Not for a process that writes vector memory on
    another thread: each rule write swaps ``vector_memory._now_iso``
    process-wide while it runs.
    """
    golden.validate()
    if not use_embeddings:
        resolved_id = KEYWORD_ONLY_EMBEDDER_ID
        wrapped: Callable[[str], list[float] | None] | None = None
    else:
        if embed_fn is None:
            embed_fn = toy_embed_fn()
            embedder_id = TOY_EMBEDDER_ID
        resolved_id = embedder_id
        wrapped = embed_fn
    k_values = tuple(sorted({int(k) for k in k_values}))
    if not k_values or k_values[0] < 1:
        raise ValueError("k_values must be positive")

    with tempfile.TemporaryDirectory(prefix="lesson_eval_") as tmp:
        store = VectorMemoryStore(db_path=Path(tmp) / "memory.db")
        try:
            store.init()
            store.embed_fn = wrapped
            rule_id_by_text = _write_rules(store, golden.rules)
            if use_embeddings:
                # A rule with no vector is ranked on its keyword half alone,
                # which would score a hybrid run partly as a keyword run. The
                # store catches a failed embed and stores the row without one,
                # so this check, not the embedder, is where that is refused.
                unembedded = sorted(
                    rule_id_by_text[_stored_rule_text(row)]
                    for row in store.get_lessons()
                    if not row.get("embedding")
                )
                if unembedded:
                    raise LessonGoldenSetError(
                        f"rules stored without a vector: {', '.join(unembedded)}"
                    )
            report = LessonRecallReport(
                golden_set=golden.name, embedder_id=resolved_id, k_values=k_values
            )
            for query in golden.queries:
                ranked = _ranked_rule_ids(
                    store, query.request, rule_id_by_text, use_embeddings=use_embeddings
                )
                gold = list(query.gold_rule_ids)
                report.results.append(
                    LessonQueryResult(
                        query_id=query.id,
                        query_class=query.query_class,
                        ranked_rule_ids=ranked,
                        recall_any={k: recall_any_at_k(ranked, gold, k) for k in k_values},
                        recall_all={k: recall_all_at_k(ranked, gold, k) for k in k_values},
                        ndcg={k: ndcg_at_k(ranked, gold, k) for k in k_values},
                        mrr={k: mrr_at_k(ranked, gold, k) for k in k_values},
                    )
                )
            return report
        finally:
            # Closed before the directory is removed: an open SQLite handle
            # makes the removal fail on Windows.
            store.close()


def format_lesson_report(report: LessonRecallReport, *, k: int = 3) -> str:
    lines = [f"Lesson recall eval: {report.golden_set}", f"embedder: {report.embedder_id}", ""]
    head = report.headline(k)
    lines.append(f"HEADLINE @{k} ({int(head['n'])} queries):")
    for metric in ("recall_any", "recall_all", "ndcg", "mrr"):
        lines.append(f"  {metric + '@' + str(k):<16} {head[metric]:.3f}")
    lines.append("")
    lines.append(f"BY CLASS @{k}:")
    for query_class, m in report.by_class(k).items():
        lines.append(
            f"  {query_class:<18} n={int(m['n'])} recall_any={m['recall_any']:.3f} "
            f"recall_all={m['recall_all']:.3f} ndcg={m['ndcg']:.3f} mrr={m['mrr']:.3f}"
        )
    return "\n".join(lines)
