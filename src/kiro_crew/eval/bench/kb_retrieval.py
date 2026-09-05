"""Deterministic recall harness for the Knowledge Library's ``HybridRetriever``.

This is T0 of the Knowledge Library roadmap: the measurement substrate every
retrieval-quality change (freshness weighting, a reranker, tool-description
rewrites) is judged against. It answers one question exactly -- *given a labeled
set of ``(query -> answering doc)`` pairs, does the KB surface the right doc,
ranked high?* -- and it answers it the same way on every run, so a delta between
two commits is attributable to the code change rather than to sampling.

It is deliberately **separate** from ``bench retrieval`` (``run.py``): that harness
measures the conversational *memory* layer (``VectorMemoryStore`` over LoCoMo /
LongMemEval, turn/session gold). This one measures the *Knowledge Library* --
``HybridRetriever.search`` over a real :class:`KnowledgeStore`, with a chunk-shaped
golden set whose gold labels are document ids.

Why a synthetic golden set. No public retrieval benchmark cleanly models "a later
document supersedes an earlier one", which is exactly what the KB's
correction/contradiction/retraction classes are about. The v1 set is hand-authored,
so lexical overlap can flatter retrieval (especially with the toy embedder) and its
1–2 queries per class are directional rather than statistically stable; a v2 can
graft the answerable classes onto LongMemEval later without changing this scorer.

The default embedder is the deterministic :func:`toy_embed_fn` -- a hashed
bag-of-words stand-in that makes the harness runnable and testable on a host whose
vendored llama.cpp payload cannot load, and makes a test assertion about ranking
stable across processes. It measures term overlap, **not** semantic recall, so a
number produced with it is a plumbing check, not a reportable benchmark result;
the real ``InProcessEmbedder`` is used only when explicitly requested and resident.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from kiro_crew.eval.bench.errors import BenchRefusal
from kiro_crew.eval.bench.retrieval import (
    ndcg_at_k,
    recall_all_at_k,
    recall_any_at_k,
    recall_micro_at_k,
)
from kiro_crew.eval.bench.safepath import UnsafePathError, read_text_nofollow
from kiro_crew.eval.bench.toy_embedder import TOY_EMBEDDER_ID, toy_embed_fn
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

# Module-scope imports are safe here despite the boot-path perf concern: this
# module is itself imported ONLY lazily (cli_bench._kb_retrieval imports it inside
# the dispatch), and eval/bench/__init__ does not import it, so nothing pulls
# knowledge.store / sqlite into an unrelated `kirocrew` subcommand's boot path.
# The lazy import that matters -- the one in cli_bench that gates this whole
# module out of the boot path -- stays where it is.

#: Reported as the embedder identity for a keyword-only run, so a run with the
#: vector leg disabled can never print a semantic embedder label over numbers the
#: embedder did not produce.
KEYWORD_ONLY_EMBEDDER_ID = "keyword-only (no embeddings)"

#: The ten query classes the Knowledge Library is judged across. Ordered as in the
#: merged goal (docs/system-specs/modules/knowledge.md, Success criteria).
KB_QUERY_CLASSES: tuple[str, ...] = (
    "clean_fact",
    "multi_hop",
    "time_bound",
    "correction",
    "contradiction",
    "retraction",
    "reinforcement",
    "hypothetical_exclusion",
    "abstention",
    "citation_fidelity",
)

#: Cut-offs reported per query class. 1 and 3 are the ranks that actually change an
#: answer (the agent reads the top few chunks); 5/10 show the tail.
DEFAULT_KB_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)


# Hard cap on golden-file size. The bundled v1 set is ~40 KiB; a hand-authored
# golden set has no business approaching this, and anything larger is either a
# mistake or a hostile input feeding the JSON parser.
_GOLDEN_MAX_BYTES = 16 * 1024 * 1024


class KBGoldenSetError(BenchRefusal):
    """The golden set is missing, malformed, or internally inconsistent.

    A refusal, not a crash: a golden set that references a gold doc id that no
    document defines cannot produce an interpretable recall number, so the harness
    declines rather than silently scoring against a corpus it cannot trust.
    """


def mrr_at_k(ranked: Sequence[str], gold: Sequence[str], k: int) -> float:
    """Mean Reciprocal Rank at ``k`` for a single query (binary relevance).

    The reciprocal of the 1-based rank of the FIRST gold item inside the window,
    or 0.0 if none appear. This is the metric that captures "is the right doc at
    the top", which is what matters when the agent only reads the first result --
    ``recall@k`` treats rank 1 and rank k as equal, MRR does not.

    Lives here rather than in ``retrieval.py`` because that module's scorers are
    frozen against a differential harness and this is a KB-local addition.
    """
    if not gold:
        return 0.0
    goldset = set(gold)
    for i, item in enumerate(ranked[:k]):
        if item in goldset:
            return 1.0 / (i + 1)
    return 0.0


@dataclass(frozen=True)
class KBDoc:
    """One document in the golden corpus. ``id`` is the retrieval gold label."""

    id: str
    title: str
    content: str
    section_title: str = ""
    tags: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: dict) -> "KBDoc":
        try:
            return cls(
                id=str(raw["id"]),
                title=str(raw["title"]),
                content=str(raw["content"]),
                section_title=str(raw.get("section_title", "")),
                tags=tuple(str(t) for t in raw.get("tags", ())),
            )
        except (KeyError, TypeError) as exc:
            raise KBGoldenSetError(f"malformed doc entry: {raw!r} ({exc})") from exc


@dataclass(frozen=True)
class KBQuery:
    """One labeled query. ``gold_doc_ids`` is empty for an abstention query."""

    id: str
    query_class: str
    question: str
    gold_doc_ids: tuple[str, ...]

    @property
    def is_abstention(self) -> bool:
        return not self.gold_doc_ids

    @classmethod
    def from_raw(cls, raw: dict) -> "KBQuery":
        try:
            qcls = str(raw["class"])
            if qcls not in KB_QUERY_CLASSES:
                raise KBGoldenSetError(
                    f"query {raw.get('id')!r} has unknown class {qcls!r}; "
                    f"known: {', '.join(KB_QUERY_CLASSES)}"
                )
            return cls(
                id=str(raw["id"]),
                query_class=qcls,
                question=str(raw["question"]),
                gold_doc_ids=tuple(str(g) for g in raw.get("gold_doc_ids", ())),
            )
        except (KeyError, TypeError) as exc:
            raise KBGoldenSetError(f"malformed query entry: {raw!r} ({exc})") from exc


@dataclass(frozen=True)
class KBGoldenSet:
    """A frozen golden set: the corpus plus the labeled queries."""

    name: str
    docs: tuple[KBDoc, ...]
    queries: tuple[KBQuery, ...]

    def validate(self) -> None:
        """Refuse a set that cannot yield an interpretable number.

        Failure modes: a duplicate doc id (ambiguous gold label); a gold
        reference no document defines (unhittable, so recall is capped below 1.0
        for reasons unrelated to the retriever); and a class/gold mismatch (an
        abstention query WITH gold docs, or an answerable query WITHOUT any) --
        either makes the query land in the wrong aggregate bucket and report a
        metric that means nothing.
        """
        ids = [d.id for d in self.docs]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise KBGoldenSetError(f"duplicate doc ids: {sorted(dupes)}")
        idset = set(ids)
        if not self.queries:
            raise KBGoldenSetError("golden set has no queries")
        for q in self.queries:
            dangling = [g for g in q.gold_doc_ids if g not in idset]
            if dangling:
                raise KBGoldenSetError(f"query {q.id!r} references undefined gold docs: {dangling}")
            if q.query_class == "abstention" and q.gold_doc_ids:
                raise KBGoldenSetError(
                    f"query {q.id!r} is class 'abstention' but has gold docs {list(q.gold_doc_ids)}"
                )
            if q.query_class != "abstention" and not q.gold_doc_ids:
                raise KBGoldenSetError(
                    f"query {q.id!r} is class {q.query_class!r} but has no gold docs "
                    "(only 'abstention' queries may have an empty gold set)"
                )
        if all(q.query_class == "abstention" for q in self.queries):
            # headline() averages the answerable population; with zero answerable
            # queries _mean([]) would report 0.0 for every answerable metric --
            # a fabricated score for a population that was never measured (the
            # same invariant _require_k enforces for uncomputed cut-offs).
            raise KBGoldenSetError(
                "golden set has no answerable queries (only abstention); "
                "answerable metrics would be fabricated 0.0s -- add at least "
                "one answerable query"
            )

    @classmethod
    def from_json(cls, path: str | Path) -> "KBGoldenSet":
        p = Path(path)
        path_display = repr(str(p))
        # The shared reader opens once, validates that SAME descriptor is a regular
        # non-hardlinked file, and reads at most cap + 1. Keeping file identity and
        # the byte limit in one helper prevents a stat/open swap from turning this
        # parser into an unbounded read of a device or a growing file.
        try:
            text = read_text_nofollow(p, what="golden set", max_bytes=_GOLDEN_MAX_BYTES)
        except UnsafePathError as exc:
            raise KBGoldenSetError(str(exc)) from exc
        except OSError as exc:
            raise KBGoldenSetError(
                f"golden set could not be read: {path_display} ({exc!r})"
            ) from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise KBGoldenSetError(f"golden set is not valid JSON: {path_display} ({exc})") from exc
        except RecursionError as exc:
            # json.loads recurses per nesting level; ~10k nested arrays blow the
            # interpreter's recursion limit BEFORE a JSONDecodeError can be
            # raised. Convert at the source so every caller refuses cleanly
            # instead of each handler chasing parse-time exception classes.
            raise KBGoldenSetError(
                f"golden set is too deeply nested to parse: {path_display}"
            ) from exc
        if not isinstance(raw, dict):
            # A valid-JSON scalar/array (`[]`, `null`, `42`) would crash the .get()
            # calls below with an AttributeError; refuse it as the documented error.
            raise KBGoldenSetError(
                f"golden set must be a JSON object, got {type(raw).__name__}: {path_display}"
            )
        name = raw.get("name", p.stem)
        if not isinstance(name, str):
            raise KBGoldenSetError(
                f"golden set 'name' must be a string, got {type(name).__name__}: {path_display}"
            )
        try:
            name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise KBGoldenSetError(
                f"golden set 'name' must be valid UTF-8 text: {path_display}"
            ) from exc
        if not name or not name.isprintable():
            raise KBGoldenSetError(
                f"golden set 'name' must contain only printable text: {path_display}"
            )

        docs_raw = raw.get("docs", [])
        queries_raw = raw.get("queries", [])
        # A present-but-null (or scalar) docs/queries value would make the tuple
        # comprehension below raise TypeError on iteration; refuse it cleanly.
        if not isinstance(docs_raw, list):
            raise KBGoldenSetError(
                f"golden set 'docs' must be a list, got {type(docs_raw).__name__}: "
                f"{path_display}"
            )
        if not isinstance(queries_raw, list):
            raise KBGoldenSetError(
                f"golden set 'queries' must be a list, got {type(queries_raw).__name__}: "
                f"{path_display}"
            )
        gs = cls(
            name=name,
            docs=tuple(KBDoc.from_raw(d) for d in docs_raw),
            queries=tuple(KBQuery.from_raw(q) for q in queries_raw),
        )
        gs.validate()
        return gs


@dataclass(frozen=True)
class KBQueryResult:
    """Per-query scoring outcome, retained so the report can break down by class."""

    query_id: str
    query_class: str
    is_abstention: bool
    recall_any: dict[int, float]
    recall_all: dict[int, float]
    recall_micro: dict[int, float]
    ndcg: dict[int, float]
    mrr: dict[int, float]
    #: For abstention queries only: 1.0 if the retriever returned nothing (or
    #: nothing above the floor), else 0.0. ``None`` for answerable queries.
    abstained: float | None = None


@dataclass
class KBRetrievalReport:
    """The full run: per-query results plus per-class and overall aggregates."""

    golden_set: str
    embedder_id: str
    k_values: tuple[int, ...]
    results: list[KBQueryResult] = field(default_factory=list)

    def _answerable(self) -> list[KBQueryResult]:
        return [r for r in self.results if not r.is_abstention]

    def by_class(self, k: int) -> dict[str, dict[str, float]]:
        """Mean recall_any / recall_all / recall_micro / ndcg / mrr at ``k``, per query class.

        Abstention is reported separately as an abstention rate, not folded into
        recall (a recall number over zero gold docs is meaningless).
        """
        self._require_k(k)
        out: dict[str, dict[str, float]] = {}
        for qcls in KB_QUERY_CLASSES:
            members = [r for r in self.results if r.query_class == qcls]
            if not members:
                continue
            if qcls == "abstention":
                vals = [r.abstained for r in members if r.abstained is not None]
                out[qcls] = {"abstention_rate": _mean(vals)}
                continue
            out[qcls] = {
                "recall_any": _mean([r.recall_any[k] for r in members]),
                "recall_all": _mean([r.recall_all[k] for r in members]),
                "recall_micro": _mean([r.recall_micro[k] for r in members]),
                "ndcg": _mean([r.ndcg[k] for r in members]),
                "mrr": _mean([r.mrr[k] for r in members]),
            }
        return out

    def _require_k(self, k: int) -> None:
        """A cut-off not in the computed set has no honest number.

        Indexing ``[k]`` below would otherwise KeyError; a ``.get(k, 0.0)`` default
        would be worse -- it silently reports 0.0, which reads as a real low score
        rather than 'not measured'. Fail loud instead (the sibling harness's rule:
        an unmeasurable metric must be absent, never 0.0).
        """
        if k not in self.k_values:
            raise KBGoldenSetError(
                f"cut-off k={k} was not computed (available: {list(self.k_values)}); "
                "pass it in k_values so the metric is real, not a fabricated 0.0"
            )

    def headline(self, k: int = 3) -> dict[str, float]:
        """Overall numbers across answerable queries, plus the abstention rate."""
        self._require_k(k)
        ans = self._answerable()
        abst = [r.abstained for r in self.results if r.abstained is not None]
        return {
            "recall_any@%d" % k: _mean([r.recall_any[k] for r in ans]),
            "recall_all@%d" % k: _mean([r.recall_all[k] for r in ans]),
            "recall_micro@%d" % k: _mean([r.recall_micro[k] for r in ans]),
            "ndcg@%d" % k: _mean([r.ndcg[k] for r in ans]),
            "mrr@%d" % k: _mean([r.mrr[k] for r in ans]),
            "abstention_rate": _mean(abst),
            "n_answerable": float(len(ans)),
            "n_abstention": float(len(abst)),
        }


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def default_golden_set_path() -> Path:
    """Path to the shipped v1 golden set (packaged next to this module)."""
    return Path(__file__).resolve().parent / "data" / "kb_golden_v1.json"


def _fail_closed_embed(
    embed_fn: Callable[[str], list[float] | None],
) -> Callable[[str], list[float]]:
    """Wrap an embedder so an empty/None vector is a refusal, not a silent skip.

    This is the ONE place the "no keyword-only numbers under an embedder label"
    invariant is enforced, and it covers BOTH sides symmetrically: document
    ingest (``_build_store``) and every per-query ``HybridRetriever.search`` call,
    which re-embeds the query through this same callable. Without it, an embedder
    that goes unready AFTER doc ingest would make the vector leg return nothing and
    the run would report degraded keyword rankings under the semantic embedder id.
    """

    def _fn(text: str) -> list[float]:
        vec = embed_fn(text)
        if not vec:
            raise KBGoldenSetError(
                "embeddings enabled but the embedder returned an empty vector; "
                "refusing to report keyword-only metrics under an embedder label. "
                "Use --no-embeddings for a keyword-only run."
            )
        return vec

    return _fn


def _build_store(
    store: "KnowledgeStore",
    docs: Sequence[KBDoc],
    embed_fn: Callable[[str], list[float]] | None,
):
    """Populate an already-created KnowledgeStore with the golden docs.

    The caller creates and OWNS the store (and closes it in a ``finally``), so a
    mid-ingestion raise -- e.g. the fail-closed embedder rejecting an empty vector
    -- still leaves the connection to be closed before the temp dir is removed. On
    Windows an un-closed SQLite handle makes ``TemporaryDirectory.cleanup()`` raise
    ``PermissionError`` (WinError 32), which is the exact failure this ownership
    split fixes on the refusal path. Each doc becomes one source + one item; the
    item's embedding is the ``embed_fn`` of "title + content", packed to bytes
    exactly as production ingestion stores it. ``embed_fn`` is None for a
    keyword-only run; when set it is already fail-closed, so an empty vector raises
    rather than storing NULL.
    """
    for doc in docs:
        source_id = store.add_source(
            name=doc.title,
            source_type="doc",
            uri=f"kbgolden://{doc.id}",
            properties={"section_title": doc.section_title},
        )
        embedding = None
        if embed_fn is not None:
            embedding = floats_to_bytes(embed_fn(f"{doc.title}\n{doc.content}"))
        item_id = store.add_item(
            title=doc.title,
            content=doc.content,
            item_type="chunk",
            source_id=source_id,
            summary=None,
            tags=list(doc.tags),
            embedding=embedding,
        )
        # A source-location row so section_title / chunk_range are present in
        # results, mirroring a real ingested chunk (and exercising the citation
        # enrichment path the citation_fidelity class depends on). A failure here
        # propagates: the harness's own store must accept the row, and a silent
        # skip would let citation-fidelity "succeed" with the enrichment broken.
        store.add_source_location(
            item_id=item_id,
            source_id=source_id,
            chunk_range="1-1",
            section_title=doc.section_title or None,
        )


#: Map an item's ``uri`` back to the golden doc id. The store returns its own item
#: ids in results, so we resolve through the source uri we controlled at ingest.
def _result_doc_id(result: dict, uri_to_docid: dict[str, str]) -> str | None:
    uri = result.get("source_uri")
    if uri and uri in uri_to_docid:
        return uri_to_docid[uri]
    return None


def run_kb_retrieval(
    golden: KBGoldenSet,
    *,
    embed_fn: Callable[[str], list[float] | None] | None = None,
    embedder_id: str = "toy-hashed-bow",
    k_values: Sequence[int] = DEFAULT_KB_K_VALUES,
    use_embeddings: bool = True,
) -> KBRetrievalReport:
    """Score a golden set against a real ``HybridRetriever`` and return the report.

    ``embed_fn`` defaults to the deterministic toy embedder. Pass the real
    embedder's bound ``.embed`` (and its id) for a semantic run. ``use_embeddings``
    False runs keyword-only (the vector leg is skipped), which isolates the FTS
    leg's contribution.

    Idempotent and side-effect-free: it builds the store in a private temp
    directory (cleaned up on exit) and never touches the live KB.
    """
    golden.validate()

    # Resolve the embedder identity from what the vector leg will ACTUALLY do, in
    # one place, so a keyword-only run can never carry a semantic label and a
    # default run is always the toy id. This is the invariant the reviewers kept
    # finding leaks in; deriving it here makes it hold by construction.
    if not use_embeddings:
        resolved_id = KEYWORD_ONLY_EMBEDDER_ID
        wrapped: Callable[[str], list[float]] | None = None
    else:
        if embed_fn is None:
            embed_fn = toy_embed_fn()
            embedder_id = TOY_EMBEDDER_ID
        resolved_id = embedder_id
        # Fail closed on BOTH sides: doc ingest below AND every query search (the
        # retriever re-embeds the query through this same callable).
        wrapped = _fail_closed_embed(embed_fn)

    uri_to_docid = {f"kbgolden://{d.id}": d.id for d in golden.docs}
    k_values = tuple(sorted(set(int(k) for k in k_values)))
    # Clamp the retrieval depth to the corpus size: the store holds exactly
    # len(docs) documents, so a deeper LIMIT cannot change any ranking, and an
    # attacker-sized -k (e.g. 2**62) would otherwise flow into the SQL LIMIT
    # arithmetic and crash with an uncaught OverflowError. The requested
    # k_values are kept as-is for scoring -- recall@k for k > corpus size is
    # well-defined over the (at most corpus-sized) ranked list.
    limit = min(max(k_values), len(golden.docs))

    tmpdir = tempfile.TemporaryDirectory(prefix="kb_eval_")
    db_path = str(Path(tmpdir.name) / "kb.sqlite")

    # Create the store here so the finally below always closes it -- even if
    # _build_store raises mid-ingestion (e.g. the fail-closed embedder rejects an
    # empty vector). A store created inside _build_store and abandoned on raise
    # would leak its SQLite handle and break tmpdir.cleanup() on Windows.
    store = KnowledgeStore(db_path)
    try:
        _build_store(store, golden.docs, wrapped)
        retriever = HybridRetriever(store, embedder=wrapped)

        report = KBRetrievalReport(
            golden_set=golden.name,
            embedder_id=resolved_id,
            k_values=k_values,
        )
        for q in golden.queries:
            hits = retriever.search(q.question, limit=limit)
            ranked = [
                did for did in (_result_doc_id(h, uri_to_docid) for h in hits) if did is not None
            ]
            gold = list(q.gold_doc_ids)
            abstained: float | None = None
            if q.is_abstention:
                # No score floor exists in the retriever, so "abstained" means the
                # retriever surfaced nothing at all for a query with no gold doc.
                abstained = 1.0 if not hits else 0.0
            report.results.append(
                KBQueryResult(
                    query_id=q.id,
                    query_class=q.query_class,
                    is_abstention=q.is_abstention,
                    recall_any={k: recall_any_at_k(ranked, gold, k) for k in k_values},
                    recall_all={k: recall_all_at_k(ranked, gold, k) for k in k_values},
                    recall_micro={k: recall_micro_at_k(ranked, gold, k) for k in k_values},
                    ndcg={k: ndcg_at_k(ranked, gold, k) for k in k_values},
                    mrr={k: mrr_at_k(ranked, gold, k) for k in k_values},
                    abstained=abstained,
                )
            )
        return report
    finally:
        # Close the store's SQLite connection BEFORE removing the temp dir: on
        # Windows an open handle blocks directory deletion and TemporaryDirectory
        # .cleanup() would raise PermissionError, crashing an otherwise-good run.
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        tmpdir.cleanup()


def format_kb_report(report: KBRetrievalReport, *, k: int = 3) -> str:
    """Render a human-readable summary at cut-off ``k``."""
    lines: list[str] = []
    lines.append(f"KB retrieval eval: {report.golden_set}")
    lines.append(f"embedder: {report.embedder_id}")
    head = report.headline(k)
    lines.append("")
    lines.append(f"HEADLINE @{k} (answerable queries):")
    for key in (f"recall_any@{k}", f"recall_all@{k}", f"recall_micro@{k}", f"ndcg@{k}", f"mrr@{k}"):
        lines.append(f"  {key:<16} {head[key]:.3f}")
    lines.append(
        f"  abstention_rate  {head['abstention_rate']:.3f} " f"(n={int(head['n_abstention'])})"
    )
    lines.append("")
    lines.append(f"BY CLASS @{k}:")
    by_class = report.by_class(k)
    for qcls in KB_QUERY_CLASSES:
        if qcls not in by_class:
            continue
        m = by_class[qcls]
        if qcls == "abstention":
            lines.append(f"  {qcls:<24} abstention_rate={m['abstention_rate']:.3f}")
        else:
            lines.append(
                f"  {qcls:<24} recall_any={m['recall_any']:.3f} "
                f"recall_all={m['recall_all']:.3f} "
                f"recall_micro={m['recall_micro']:.3f} "
                f"ndcg={m['ndcg']:.3f} mrr={m['mrr']:.3f}"
            )
    return "\n".join(lines)
