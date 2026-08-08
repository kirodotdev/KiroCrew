"""End-to-end benchmark run: corpus in, report out.

Ties the pieces together and — more importantly — refuses to produce a number it
cannot stand behind. Three guards fire before any measurement:

* the corpus must contain distractor sessions, or recall is trivially 1.0
  (LongMemEval's ``oracle`` variant fails this for 500/500 instances);
* the embedder must be resident, or every fragment stores a NULL embedding and
  ``search_episodic`` silently degrades to FTS5 substring matching;
* the ranking backend is recorded, because the store picks one of several based on
  which optional dependencies import, and two hosts can rank the same corpus
  differently.

The output carries its own provenance — corpus fingerprint, ingest config,
retrieval config, backend, and the counts of everything that was skipped or
dropped. That is not ceremony: the reproducibility work in this field identifies
answer model, judge and ingestion granularity as the dominant score drivers, so a
number without its configuration is not comparable to anything, including a later
run of itself.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

from .corpus import Corpus
from .ingest import (
    EmbedFn,
    IngestConfig,
    IngestReport,
    ingest_instance,
    prepare_embedder,
    search_backend,
)
from .retrieval import (
    QueryRetrieval,
    RetrievalAggregate,
    RetrievalConfig,
    RetrievalNotMeasurable,
    aggregate,
    corpus_has_distractors,
    retrieve_for_instance,
)
from .safepath import guard_output_dir, guard_write_path


@dataclass
class RunResult:
    """Everything a report or an A/B comparison needs, and nothing it must guess."""

    corpus_name: str
    corpus_variant: str
    corpus_fingerprint: str
    instances: int
    sessions: int
    turns: int
    queries: int
    ingest: dict[str, object]
    retrieval: dict[str, object]
    backend: str
    #: Which embedder produced the vectors. Recorded because it is the single
    #: largest score driver in a memory benchmark, and because a toy stand-in and
    #: the real model must never compare as equivalent.
    embedder: str
    metrics: RetrievalAggregate
    ingest_reports: list[IngestReport] = field(default_factory=list)
    elapsed_s: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def headline(self, k: int = 5) -> float:
        return self.metrics.headline(k)

    def to_json(self) -> dict[str, object]:
        return {
            "corpus": {
                "name": self.corpus_name,
                "variant": self.corpus_variant,
                "fingerprint": self.corpus_fingerprint,
                "instances": self.instances,
                "sessions": self.sessions,
                "turns": self.turns,
                "queries": self.queries,
            },
            "config": {
                "ingest": self.ingest,
                "retrieval": self.retrieval,
                "search_backend": self.backend,
                "embedder": self.embedder,
            },
            "metrics": {
                "scored_queries": self.metrics.scored_queries,
                "skipped_unscorable": self.metrics.skipped_unscorable,
                "unanswerable_queries": self.metrics.unanswerable_queries,
                "unattributed_hits": self.metrics.unattributed_hits,
                "session": self.metrics.session,
                "turn": self.metrics.turn,
                "session_measurable": {str(k): v for k, v in self.metrics.session_measurable.items()},
                "turn_measurable": {str(k): v for k, v in self.metrics.turn_measurable.items()},
                "by_category": self.metrics.by_category,
            },
            "ingest_totals": {
                "attempted": sum(r.attempted for r in self.ingest_reports),
                "written": sum(r.written for r in self.ingest_reports),
                "dropped_fragments": sum(r.dropped_fragments for r in self.ingest_reports),
                "dropped_gold": sum(len(r.dropped_gold) for r in self.ingest_reports),
                "null_embeddings": sum(r.null_embeddings for r in self.ingest_reports),
                "unparsed_timestamps": sum(r.unparsed_timestamps for r in self.ingest_reports),
                "max_decay_span_days": max(
                    (r.decay_span_days for r in self.ingest_reports), default=0
                ),
            },
            "warnings": self.warnings,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def _production_embedder_id() -> str:
    """The real embedder's identity, as ``model-id@dim``.

    Read from :mod:`kiro_crew.embeddings` rather than hardcoded so a model swap
    changes the recorded identity -- and therefore invalidates comparisons against
    baselines from the previous vector space, which is the correct outcome.
    """
    from kiro_crew import embeddings

    return f"{embeddings._MODEL_ID}@{embeddings._DEFAULT_DIM}"


def run_retrieval(
    corpus: Corpus,
    *,
    ingest_config: IngestConfig | None = None,
    retrieval_config: RetrievalConfig | None = None,
    embed_fn: EmbedFn | None = None,
    embedder_id: str | None = None,
    force_no_distractors: bool = False,
    store_root: Path | None = None,
) -> RunResult:
    """Measure the retrieval ruler over a whole corpus.

    ``force_no_distractors`` exists only so the ingest and attribution paths can be
    smoke-tested against the cheap ``oracle`` variant. It is not a way to get a
    retrieval number out of an evidence-only corpus — the resulting figure is still
    meaningless, and the warning that says so is written into the report.
    """
    icfg = ingest_config or IngestConfig()
    rcfg = retrieval_config or RetrievalConfig()
    warnings: list[str] = []

    ok, why = corpus_has_distractors(corpus)
    if not ok:
        if not force_no_distractors:
            raise RetrievalNotMeasurable(why)
        warnings.append(f"FORCED past a blocking guard: {why}")
    else:
        warnings.append(why)

    if embed_fn is not None and not embedder_id:
        # Fail closed. An injected embedder with no identity would be saved as
        # though it were the production model, and `compare_reports` would then
        # diff it against a real run and call the delta exact.
        raise ValueError(
            "embed_fn was supplied without embedder_id; every report must record "
            "which embedder produced it or it cannot be compared safely"
        )
    fn: EmbedFn = embed_fn or prepare_embedder(timeout_s=icfg.embed_timeout_s)
    embedder = embedder_id or _production_embedder_id()
    backend = search_backend()
    if backend != "faiss":
        warnings.append(
            f"ranking backend is {backend!r}, not FAISS — faiss is not importable "
            "here. Results remain internally consistent, but a comparison is only "
            "valid against another run reporting the same backend."
        )

    started = time.monotonic()
    results: list[QueryRetrieval] = []
    reports: list[IngestReport] = []

    with tempfile.TemporaryDirectory(prefix="kirocrew_bench_") as tmp:
        root = store_root or Path(tmp)
        for idx, inst in enumerate(corpus.instances):
            # One store per instance. Sharing one would overrun episodic_max and
            # start tombstoning by (importance ASC, created_at ASC), quietly
            # deleting the oldest evidence and turning this into a measurement of
            # the eviction policy.
            loaded = ingest_instance(
                inst,
                db_path=root / f"inst_{idx:05d}.db",
                embed_fn=fn,
                config=icfg,
            )
            try:
                reports.append(loaded.report)
                results.extend(retrieve_for_instance(loaded, embed_fn=fn, config=rcfg))
            finally:
                loaded.close()

    metrics = aggregate(results, instances=corpus.instances, k_values=rcfg.k_values)

    dropped_gold = sum(len(r.dropped_gold) for r in reports)
    if dropped_gold:
        warnings.append(
            f"{dropped_gold} gold fragment(s) were refused at ingest (dedup at "
            f"{icfg.dedup_threshold} or the capacity cap), so recall for the "
            "affected queries cannot reach 1.0 no matter how good the ranking is. "
            "Re-run with dedup disabled to separate 'ranking missed it' from 'it "
            "was never stored'."
        )
    null_emb = sum(r.null_embeddings for r in reports)
    if null_emb:
        warnings.append(
            f"{null_emb} fragment(s) were stored without an embedding and are "
            "keyword-searchable only"
        )
    span = max((r.decay_span_days for r in reports), default=0)
    if span > 90 and icfg.timeline != "now":
        warnings.append(
            f"the corpus spans {span} days, so the store's recency decay "
            f"(exp(-0.03 * days)) penalises its oldest sessions by a factor of "
            f"~{2.718281828 ** (-0.03 * span):.2e} relative to its newest. At that "
            "magnitude recency dominates semantic similarity outright. Re-run with "
            "timeline='now' to isolate ranking from decay."
        )

    return RunResult(
        corpus_name=corpus.name,
        corpus_variant=corpus.variant,
        corpus_fingerprint=corpus.fingerprint(),
        instances=len(corpus.instances),
        sessions=corpus.session_count,
        turns=corpus.turn_count,
        queries=corpus.query_count,
        ingest=icfg.describe(),
        retrieval=rcfg.describe(),
        backend=backend,
        embedder=embedder,
        metrics=metrics,
        ingest_reports=reports,
        elapsed_s=time.monotonic() - started,
        warnings=warnings,
    )


# ── Reporting ────────────────────────────────────────────────────────────────


def _table(rows: Sequence[tuple[str, ...]], header: tuple[str, ...]) -> str:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(header)) + " |"]
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in rows:
        lines.append("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)) + " |")
    return "\n".join(lines)


def format_report(outcome: RunResult, *, k_values: Sequence[int] = (1, 5, 8, 10)) -> str:
    """Markdown, with the caveats above the numbers rather than in a footnote."""
    m = outcome.metrics
    out: list[str] = [
        f"# {outcome.corpus_name} / {outcome.corpus_variant} — retrieval",
        "",
        f"corpus fingerprint `{outcome.corpus_fingerprint[:16]}`  ·  "
        f"{outcome.instances} instances, {outcome.sessions} sessions, "
        f"{outcome.turns} turns, {outcome.queries} queries",
        f"embedder `{outcome.embedder}`  ·  backend `{outcome.backend}`  ·  "
        f"granularity `{outcome.ingest['granularity']}`  ·  "
        f"timeline `{outcome.ingest['timeline']}`  ·  mmr `{outcome.retrieval['mmr']}`",
        f"scored {m.scored_queries} queries"
        + (f", skipped {m.skipped_unscorable} with no resolvable gold" if m.skipped_unscorable else "")
        + f"  ·  {outcome.elapsed_s:.1f}s",
        "",
    ]

    if outcome.warnings:
        out.append("## Caveats")
        out += [f"- {w}" for w in outcome.warnings]
        out.append("")

    for level, block, counts in (
        ("session", m.session, m.session_measurable),
        ("turn", m.turn, m.turn_measurable),
    ):
        if not block:
            continue
        rows: list[tuple[str, ...]] = [
            (
                str(k),
                str(counts.get(k, 0)),
                f"{block.get(f'recall_all@{k}', 0.0):.3f}",
                f"{block.get(f'recall_any@{k}', 0.0):.3f}",
                f"{block.get(f'recall_micro@{k}', 0.0):.3f}",
                f"{block.get(f'ndcg@{k}', 0.0):.3f}",
            )
            for k in k_values
            if f"recall_all@{k}" in block
        ]
        # A cut-off the fragment window never exposed is absent from `block`. Name
        # it rather than let it vanish -- a silently missing row reads as "not
        # requested", when in fact it was requested and found unmeasurable.
        omitted = [str(k) for k in k_values if k in counts and f"recall_all@{k}" not in block]
        if rows:
            out += [
                f"## {level}-level",
                _table(
                    rows,
                    ("k", "queries", "recall_all", "recall_any", "recall_micro", "ndcg"),
                ),
                "",
            ]
            if omitted:
                out += [
                    f"k = {', '.join(omitted)} omitted: the retrieval window never "
                    f"exposed that many distinct {level}s for any query, so the "
                    "cut-off is bounded by the window rather than by the ranker. "
                    "Enlarging the window would change what MMR reranks and "
                    "measure a different configuration.",
                    "",
                ]

    if m.by_category:
        cat_rows: list[tuple[str, ...]] = [
            (cat, f"{blk.get('recall_all@5', 0.0):.3f}", f"{blk.get('ndcg@5', 0.0):.3f}")
            for cat, blk in m.by_category.items()
        ]
        out += [
            "## By category (session-level, k=5)",
            _table(cat_rows, ("category", "recall_all@5", "ndcg@5")),
            "",
        ]

    return "\n".join(out)


def write_report(outcome: RunResult, out_dir: Path, *, stem: str | None = None) -> tuple[Path, Path]:
    """Write markdown + JSON. The JSON is the machine-comparable artifact.

    Both are written because they serve different consumers: a human reads the
    markdown once, while an A/B needs the JSON to diff two runs without re-parsing
    prose. Mirrors what ``kirocrew eval`` already does.
    """
    # Gate BEFORE mkdir, not just before write: `--out-dir` reaches this from argv,
    # and creating a tree under a protected root is already the damage. `--stem`
    # reaches the filenames from argv too, so both composed paths are checked
    # individually -- a safe directory plus a traversing stem must not slip through.
    safe_dir = guard_output_dir(out_dir, what="report output directory")
    stem = stem or f"bench_{outcome.corpus_name}_{time.strftime('%Y%m%d_%H%M%S')}"
    md = guard_write_path(safe_dir / f"{stem}.md", what="markdown report")
    js = guard_write_path(safe_dir / f"{stem}.json", what="JSON report")
    safe_dir.mkdir(parents=True, exist_ok=True)
    md.write_text(format_report(outcome), encoding="utf-8")
    js.write_text(json.dumps(outcome.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    return md, js


def compare_reports(baseline: dict, candidate: dict, *, k: int = 5) -> str:
    """Diff two saved JSON reports, refusing the comparisons that are invalid.

    Corpus fingerprint, ingest config, retrieval config and search backend must all
    match. If they do not, the delta is not attributable to the code change — it
    could be a different corpus slice, a different ingest granularity, or a host
    where faiss happened to be importable. Saying so is more useful than printing a
    number.
    """
    problems: list[str] = []
    b_corpus = baseline.get("corpus", {})
    c_corpus = candidate.get("corpus", {})
    if b_corpus.get("fingerprint") != c_corpus.get("fingerprint"):
        problems.append("corpus fingerprints differ — the two runs read different data")
    b_cfg = baseline.get("config", {})
    c_cfg = candidate.get("config", {})
    # `embedder` is in this list for a reason that bit once already: a report saved
    # from a `--toy-embedder` run carried no embedder identity, so it compared as
    # equivalent to a real run and published an "exact" delta between a hashed
    # bag-of-words and a language model. A missing key counts as a mismatch against
    # a present one, so pre-fix reports cannot silently pass either.
    for key in ("ingest", "retrieval", "search_backend", "embedder"):
        if b_cfg.get(key) != c_cfg.get(key):
            problems.append(f"config.{key} differs: {b_cfg.get(key)!r} vs {c_cfg.get(key)!r}")

    lines: list[str] = []
    if problems:
        # Return here rather than falling through to the delta table. Printing an
        # "incompatible" banner and then a table of exact-looking deltas invites
        # exactly the reading the banner exists to prevent -- and the closing note
        # below asserts the deltas ARE exact, which is false once the two runs
        # disagree on corpus or config. Refusing to show a number is the whole
        # point of detecting the mismatch.
        lines += ["## Not comparable", *[f"- {p}" for p in problems], ""]
        lines.append(
            "No delta is reported: with the inputs differing, any difference "
            "between these runs is not attributable to the code change. Re-run "
            "both arms with the same corpus and config."
        )
        return "\n".join(lines)

    b_sess = baseline.get("metrics", {}).get("session", {})
    c_sess = candidate.get("metrics", {}).get("session", {})

    # Two ways a cut-off can be incomparable, and both used to print a number.
    #
    # 1. ABSENT ON ONE SIDE. A cut-off the retrieval window never exposed is omitted
    #    from the metric dict (that is what makes an unmeasurable k visible rather
    #    than falsely low). The old `name in b or name in c` with `.get(name, 0.0)`
    #    turned "the baseline could not measure this" into "the baseline scored zero",
    #    manufacturing an exact-looking improvement of the full candidate value. This
    #    hole was created by the fix that added the measurability filter — a new field
    #    with a default is a new way to be wrong.
    #
    # 2. DIFFERENT POPULATIONS. Even present on both sides, the two means can be over
    #    different query sets: measured on LoCoMo, @5 is measurable for 1977 queries
    #    with the decay neutralised and only 746 with it active. Differencing those is
    #    not a paired comparison, and the closing note below would call it exact.
    b_meas = baseline.get("metrics", {}).get("session_measurable", {})
    c_meas = candidate.get("metrics", {}).get("session_measurable", {})
    bn = b_meas.get(str(k))
    cn = c_meas.get(str(k))

    population_note: str | None = None
    if bn is None or cn is None:
        population_note = (
            f"one or both reports predate the per-cut-off `session_measurable` counts, "
            f"so it cannot be shown that @{k} was averaged over the same queries in "
            "both runs. Re-run both arms with the current harness."
        )
    elif int(bn) == 0 or int(cn) == 0:
        population_note = (
            f"@{k} was measurable for {bn} baseline and {cn} candidate queries; a "
            "cut-off measurable for nobody has no value to compare."
        )
    elif int(bn) != int(cn):
        population_note = (
            f"@{k} was measurable for {bn} baseline queries but {cn} candidate "
            "queries, so the two means are over different populations and their "
            "difference is not attributable to the code change."
        )

    if population_note is not None:
        lines += [
            f"## session-level @{k} — not comparable",
            f"- {population_note}",
            "",
            "No delta is reported.",
        ]
        return "\n".join(lines)

    rows: list[tuple[str, ...]] = []
    missing: list[str] = []
    for name in (f"recall_all@{k}", f"recall_any@{k}", f"recall_micro@{k}", f"ndcg@{k}"):
        if name in b_sess and name in c_sess:
            bv = float(b_sess[name])
            cv = float(c_sess[name])
            rows.append((name, f"{bv:.4f}", f"{cv:.4f}", f"{cv - bv:+.4f}"))
        elif name in b_sess or name in c_sess:
            # Present on exactly one side. Named, never zero-filled.
            missing.append(name)

    if not rows:
        lines += [
            f"## session-level @{k} — not comparable",
            "- no metric at this cut-off is present in both reports",
            "",
            "No delta is reported.",
        ]
        return "\n".join(lines)

    lines += [
        f"## session-level @{k}  ({bn} queries in each arm)",
        _table(rows, ("metric", "baseline", "candidate", "delta")),
        "",
    ]
    if missing:
        lines += [
            "Omitted (present in only one report, and a missing value is not a zero): "
            + ", ".join(missing),
            "",
        ]
    lines.append(
        "Retrieval is deterministic (local embedder, deterministic ranker), so these "
        "deltas are exact — there is no noise band to clear and no repetitions to run."
    )
    return "\n".join(lines)


__all__ = [
    "RunResult",
    "run_retrieval",
    "format_report",
    "write_report",
    "compare_reports",
    "asdict",
]
