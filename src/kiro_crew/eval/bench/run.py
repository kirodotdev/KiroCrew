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
    IngestError,
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
from .safepath import (
    UnsafePathError,
    guard_output_dir,
    guard_write_path,
    write_text_atomic_nofollow,
)


def _environment_identity() -> dict[str, str]:
    """The measuring environment, as far as it can change a retrieval number.

    Deliberately a short list rather than the whole dependency set. Every entry is
    here because it can alter ranking or tokenization:

    * ``python`` -- full patch version; dict/set iteration and float formatting are
      stable within a patch but the interpreter is the substrate for everything else.
    * ``platform`` -- OS and machine; the vendored llama.cpp payload differs per
      architecture, and so does BLAS.
    * ``sqlite`` -- the FTS5 tokenizer lives here, and the keyword fallback path is
      scored by it.
    * ``numpy`` -- the cosine path's arithmetic, including which BLAS it binds.

    Pinning the full dependency set would be stricter and, with no lockfile in this
    repo, would refuse comparisons for upgrades that cannot touch the numbers. The
    residual risk is a package outside this list changing ranking; that is a real
    gap, and the honest mitigation is that the corpus fingerprint plus these four
    make the common causes visible rather than silent.
    """
    import platform
    import sqlite3
    import sys

    identity = {
        "python": platform.python_version(),
        "platform": f"{sys.platform}-{platform.machine()}",
        "sqlite": sqlite3.sqlite_version,
    }
    try:
        import numpy

        identity["numpy"] = numpy.__version__
    except Exception:  # pragma: no cover - numpy absent is itself worth recording
        identity["numpy"] = "absent"
    return identity


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
    environment: dict[str, str] = field(default_factory=_environment_identity)
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
                # Part of the comparability claim, not decoration: see
                # `_environment_identity`.
                "environment": self.environment,
            },
            "metrics": {
                "scored_queries": self.metrics.scored_queries,
                "skipped_unscorable": self.metrics.skipped_unscorable,
                "unanswerable_queries": self.metrics.unanswerable_queries,
                "skipped_missing_gold": self.metrics.skipped_missing_gold,
                "unattributed_hits": self.metrics.unattributed_hits,
                "session": self.metrics.session,
                "turn": self.metrics.turn,
                "session_measurable": {
                    str(k): v for k, v in self.metrics.session_measurable.items()
                },
                "turn_measurable": {str(k): v for k, v in self.metrics.turn_measurable.items()},
                "session_population": {
                    str(k): v for k, v in self.metrics.session_population.items()
                },
                "turn_population": {str(k): v for k, v in self.metrics.turn_population.items()},
                "by_category": self.metrics.by_category,
            },
            "ingest_totals": {
                "attempted": sum(r.attempted for r in self.ingest_reports),
                "written": sum(r.written for r in self.ingest_reports),
                "dropped_fragments": sum(r.dropped_fragments for r in self.ingest_reports),
                "dropped_gold": sum(len(r.dropped_gold) for r in self.ingest_reports),
                "null_embeddings": sum(r.null_embeddings for r in self.ingest_reports),
                "injection_rejected": sum(
                    r.injection_rejected for r in self.ingest_reports
                ),
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

    Read from the LIVE embedder, not from the module constants. The constants
    describe the bundled model; ``KIROCREW_EMBED_MODEL_PATH`` (and the
    ``memory.embed_model_path`` config knob) make Kiro Crew run a different one,
    and the width can additionally be adopted from the model file itself. Reading
    the constants in that situation stamps a custom run with the bundled
    identity, and ``compare_reports`` -- which refuses only when the two
    identities DIFFER -- would then diff two different vector spaces and call the
    delta exact. That is the failure this identity field exists to prevent.

    Called after ``prepare_embedder`` has confirmed residency, so the singleton
    is loaded and its attributes are truthful rather than provisional.
    """
    from kiro_crew.embeddings import get_shared_embedder

    embedder = get_shared_embedder()
    model_id = getattr(embedder, "model_id", None)
    dim = getattr(embedder, "dim", None)
    if not model_id or not isinstance(dim, int) or dim <= 0:
        # Falling back to the bundled constants here would reintroduce exactly the
        # mislabelling described above, and silently. An identity we cannot read
        # is a report that must not be compared.
        raise IngestError(
            "the embedder does not report its identity "
            f"(model_id={model_id!r}, dim={dim!r}), so this report could not be "
            "compared safely against any other run. Pass an explicit embedder_id "
            "if you are injecting an embedder."
        )
    return f"{model_id}@{dim}"


def _ingest_warnings(reports: list[IngestReport], icfg: IngestConfig) -> list[str]:
    """Caveats a reader needs ABOVE the numbers, derived from the ingest reports.

    A separate function so each warning can be tested without running a retrieval. They
    were inline until one of them was found attributing a refused gold fragment to
    "dedup or the capacity cap" when the actual cause was the store's prompt-injection
    screen, which no dedup setting can change. A warning that sends the reader to a
    setting that cannot help is worse than a vague one, and an inline warning is one
    nobody can test.
    """
    warnings: list[str] = []

    dropped_gold = sum(len(r.dropped_gold) for r in reports)
    injected = sum(r.injection_rejected for r in reports)
    injection_note = (
        f" {injected} of them tripped the store's prompt-injection screen, which no "
        "dedup setting can change."
        if injected
        else ""
    )
    if dropped_gold:
        warnings.append(
            f"{dropped_gold} gold fragment(s) were refused at ingest, so recall for "
            "the affected queries cannot reach 1.0 no matter how good the ranking "
            f"is.{injection_note} The tunable causes are dedup at "
            f"{icfg.dedup_threshold} and the capacity cap; re-run with dedup "
            "disabled to separate 'ranking missed it' from 'it was never stored'."
        )
    # `null_embeddings` is structurally zero now -- ingest refuses on the first NULL
    # embedding instead of completing a degraded run -- so no warning is emitted for it.
    # Kept as a published report key for schema stability.
    span = max((r.decay_span_days for r in reports), default=0)
    if span > 90 and icfg.timeline != "now":
        warnings.append(
            f"the corpus spans {span} days, so the store's recency decay "
            f"(exp(-0.03 * days)) penalises its oldest sessions by a factor of "
            f"~{2.718281828 ** (-0.03 * span):.2e} relative to its newest. At that "
            "magnitude recency dominates semantic similarity outright. Re-run with "
            "timeline='now' to isolate ranking from decay."
        )
    return warnings


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

    metrics = aggregate(
        results,
        instances=corpus.instances,
        k_values=rcfg.k_values,
        # Session granularity has no turn level to score against; the block is
        # omitted rather than filled with a meaningless zero.
        turn_attribution=icfg.granularity != "session",
    )

    warnings.extend(_ingest_warnings(reports, icfg))

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


ABSENT = "—"
"""How a metric that was never measurable is rendered.

Not ``0.000``. A zero is a real and very bad score; an unmeasurable cut-off is the
absence of a score, and the two must not share a glyph. This is the display half of
the rule the metric dicts already follow by omitting the key entirely.
"""


def _metric_cell(block: dict, name: str) -> str:
    """The ONLY way this module renders a metric into a table.

    Exists so ``block.get(name, 0.0)`` cannot be written: a default is a value to be
    wrong about, and the per-category block is exactly where that default fabricated
    a ``0.000`` recall for categories whose ``@5`` was measurable for nobody.
    """
    value = block.get(name)
    if value is None:
        return ABSENT
    return f"{float(value):.3f}"


def _excluded_phrase(m: RetrievalAggregate) -> str:
    """Name WHY queries were excluded, one clause per reason that occurred.

    The single "with no resolvable gold" clause covered both reasons and was wrong for
    the bigger one: on LoCoMo the excluded population is overwhelmingly questions that
    are unanswerable BY DESIGN, where refusal is the correct behaviour and retrieval has
    nothing to be right about. Printing them as broken records invited the reader to
    distrust the corpus instead of understanding the denominator.
    """
    parts: list[str] = []
    if m.unanswerable_queries:
        parts.append(f"{m.unanswerable_queries} unanswerable by design")
    if m.skipped_missing_gold:
        parts.append(f"{m.skipped_missing_gold} with no resolvable gold")
    if not parts:
        return ""
    return f", excluded {m.skipped_unscorable} ({' and '.join(parts)})"


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
        f"scored {m.scored_queries} queries" + _excluded_phrase(m)
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
            (cat, _metric_cell(blk, "recall_all@5"), _metric_cell(blk, "ndcg@5"))
            for cat, blk in m.by_category.items()
        ]
        out += [
            "## By category (session-level, k=5)",
            _table(cat_rows, ("category", "recall_all@5", "ndcg@5")),
            "",
        ]
        if any(ABSENT in row for row in cat_rows):
            out += [
                f"`{ABSENT}` means @5 was measurable for no query in that category — "
                "the retrieval window never exposed five distinct sessions for any of "
                "them. That is an absence of measurement, not a score of zero.",
                "",
            ]

    return "\n".join(out)


def write_report(
    outcome: RunResult, out_dir: Path, *, stem: str | None = None
) -> tuple[Path, Path]:
    """Write markdown + JSON. The JSON is the machine-comparable artifact.

    Both are written because they serve different consumers: a human reads the
    markdown once, while an A/B needs the JSON to diff two runs without re-parsing
    prose. Mirrors what ``kirocrew eval`` already does.
    """
    # Gate BEFORE mkdir, not just before write: `--out-dir` reaches this from argv,
    # and creating a tree under a protected root is already the damage.
    safe_dir = guard_output_dir(out_dir, what="report output directory")
    stem = stem or f"bench_{outcome.corpus_name}_{time.strftime('%Y%m%d_%H%M%S')}"
    # `--stem` also comes from argv, and a path guard is the wrong instrument for it:
    # `guard_write_path` answers "does this name mean somewhere protected", not "is
    # this still inside --out-dir". An absolute or traversing stem is therefore
    # refused outright rather than resolved, because a report has no reason to land
    # anywhere but the directory the caller named.
    if Path(stem).name != stem or stem in (".", ".."):
        raise UnsafePathError(
            f"refusing the report stem {stem!r}: it must be a bare filename, not a "
            "path. A stem containing a separator or '..' composes to a file outside "
            "the --out-dir that was checked."
        )
    md = guard_write_path(safe_dir / f"{stem}.md", what="markdown report")
    js = guard_write_path(safe_dir / f"{stem}.json", what="JSON report")
    safe_dir.mkdir(parents=True, exist_ok=True)
    # Written through the nofollow helper for the same reason the corpus staging file
    # is: `guard_write_path` returns the RESOLVED path, so a symlink planted at
    # `<stem>.md` would already have been followed and `write_text` would land on
    # whatever it points at. The helper opens the path as given with O_NOFOLLOW.
    #
    # Atomic, not in-place: a report is a durable artifact and `--stem` is routinely
    # reused, so truncating the old one before the new bytes exist trades a stored
    # baseline for whatever an interrupted write leaves behind.
    write_text_atomic_nofollow(
        safe_dir / f"{stem}.md", format_report(outcome), what="markdown report"
    )
    write_text_atomic_nofollow(
        safe_dir / f"{stem}.json",
        json.dumps(outcome.to_json(), indent=2, sort_keys=True),
        what="JSON report",
    )
    return md, js


def _measurable_count(metrics: dict, field: str, k: int) -> int | None:
    """Read a per-cut-off count, or ``None`` when it is absent OR unusable.

    The ONE place a count crosses from JSON into arithmetic. `int(raw)` on a value
    that came out of a file raises, and this function is called from the comparison
    path, where a traceback is the wrong output: `bench compare` exists to say
    whether two runs are comparable, and "this report is malformed" is an answer it
    should give rather than crash on. Absent and malformed collapse to the same
    ``None`` deliberately — a caller that cannot get a trustworthy count must take
    the not-comparable branch either way.

    Strict about the type on purpose. `int(12.5)` succeeds and silently truncates to
    12, so a float would be accepted as a slightly different count and compared
    against a real one; this harness always writes these fields as integers, so a
    float means the file came from somewhere else. `bool` is excluded because it is
    an `int` subclass and `True` would read as a count of one.

    The container goes through `_section` for the same reason the leaf does:
    `metrics.get(field, {})` defaults only when the key is MISSING, so a report
    carrying `"session_measurable": null` handed back `None` and the next `.get`
    raised `AttributeError` -- a traceback out of the one command whose job is to
    answer whether two runs are comparable.
    """
    raw = _section(metrics, field).get(str(k))
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw < 0:
        return None
    return raw


def _section(report: object, *names: str) -> dict:
    """Walk into a nested report object, returning ``{}`` for anything unusable.

    The ONE way this module reaches into a parsed report. ``report.get("corpus", {})``
    looks like it defaults safely and does not: the default applies to a MISSING key,
    while ``{"corpus": null}`` yields ``None`` and the next ``.get`` raises
    ``AttributeError``. `bench compare` exists to say whether two runs are comparable,
    so a malformed report has to reach the not-comparable branch rather than a
    traceback.

    Round 8 put this rule on leaf values (``_measurable_count``) and left the
    container access unguarded — the same enforcement point at the wrong altitude.
    Both levels now go through a reader.
    """
    node: object = report
    for name in names:
        if not isinstance(node, dict):
            return {}
        node = node.get(name)
    return node if isinstance(node, dict) else {}


def _digest(metrics: dict, field: str, k: int) -> str | None:
    """A population digest, or ``None`` when it is absent OR unusable.

    Sliced for display (``bp[:12]``), which is a ``TypeError`` on anything that is
    not a string. Same reader shape as `_measurable_count`, for the same reason: a
    value that came out of a file must not reach an operation that assumes its type.
    """
    raw = _section(metrics, field).get(str(k))
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def _metric_value(block: dict, name: str) -> float | None:
    """A metric, or ``None`` when it is absent OR not a usable number.

    ``float(raw)`` accepts a numeric string and raises on a list or a dict, and
    ``bool`` is an ``int`` subclass that would read ``True`` as 1.0. NaN and infinity
    are refused too: both compare falsely (``nan != nan``) and would render as a
    delta no reader could act on.
    """
    import math

    raw = block.get(name)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value):
        return None
    return value


def compare_reports(baseline: dict, candidate: dict, *, k: int = 5) -> str:
    """Diff two saved JSON reports, refusing the comparisons that are invalid.

    Corpus fingerprint, ingest config, retrieval config and search backend must all
    match. If they do not, the delta is not attributable to the code change — it
    could be a different corpus slice, a different ingest granularity, or a host
    where faiss happened to be importable. Saying so is more useful than printing a
    number.
    """
    problems: list[str] = []
    b_corpus = _section(baseline, "corpus")
    c_corpus = _section(candidate, "corpus")
    # Presence first, then equality. Comparing by inequality alone makes two reports
    # that BOTH lack a fingerprint compare as compatible -- `None != None` is False --
    # so an unattributable delta would be published as exact. The old comment below
    # claimed a missing key could not silently pass; that held for one-sided absence
    # only, and `_section` returning {} for a malformed report made the two-sided case
    # easy to reach.
    if not b_corpus.get("fingerprint") or not c_corpus.get("fingerprint"):
        problems.append(
            "one or both reports carry no corpus fingerprint, so it cannot be shown "
            "that the two runs read the same data"
        )
    elif b_corpus.get("fingerprint") != c_corpus.get("fingerprint"):
        problems.append("corpus fingerprints differ — the two runs read different data")
    b_cfg = _section(baseline, "config")
    c_cfg = _section(candidate, "config")
    # `embedder` is in this list for a reason that bit once already: a report saved
    # from a `--toy-embedder` run carried no embedder identity, so it compared as
    # equivalent to a real run and published an "exact" delta between a hashed
    # bag-of-words and a language model. Absence is now refused outright rather than
    # being compared, which covers the case where BOTH sides lack the key.
    # `environment` joins this list for the reason the list exists: a delta is
    # only attributable to the code when everything else held still.
    for key in ("ingest", "retrieval", "search_backend", "embedder", "environment"):
        b_val, c_val = b_cfg.get(key), c_cfg.get(key)
        if b_val is None or c_val is None:
            # Same rule as the fingerprint: absent on both sides is not agreement.
            problems.append(
                f"config.{key} is missing from one or both reports, so it cannot be "
                "shown that the two runs used the same setting"
            )
        elif b_val != c_val:
            problems.append(f"config.{key} differs: {b_val!r} vs {c_val!r}")

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

    b_sess = _section(baseline, "metrics", "session")
    c_sess = _section(candidate, "metrics", "session")

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
    # Routed through one reader so a malformed count cannot reach arithmetic. It
    # returns None for absent AND for unusable, which the branch below already
    # handles as "not comparable" -- the only honest answer when the denominator
    # cannot be trusted.
    bn = _measurable_count(_section(baseline, "metrics"), "session_measurable", k)
    cn = _measurable_count(_section(candidate, "metrics"), "session_measurable", k)

    population_note: str | None = None
    if bn is None or cn is None:
        population_note = (
            f"one or both reports are missing a usable per-cut-off "
            f"`session_measurable` count for @{k} (absent, non-numeric or negative), "
            "so it cannot be shown that the two means were averaged over the same "
            "queries. Re-run both arms with the current harness."
        )
    elif bn == 0 or cn == 0:
        population_note = (
            f"@{k} was measurable for {bn} baseline and {cn} candidate queries; a "
            "cut-off measurable for nobody has no value to compare."
        )
    elif bn != cn:
        population_note = (
            f"@{k} was measurable for {bn} baseline queries but {cn} candidate "
            "queries, so the two means are over different populations and their "
            "difference is not attributable to the code change."
        )
    else:
        # Equal counts are necessary and NOT sufficient. Eligibility depends on how
        # many distinct items the retrieval window happened to expose, so a ranking
        # change can make one query eligible and another ineligible and leave the
        # count untouched. Without this the two means would be over different query
        # sets of the same size and the delta would be published as exact.
        bp = _digest(_section(baseline, "metrics"), "session_population", k)
        cp = _digest(_section(candidate, "metrics"), "session_population", k)
        if not bp or not cp:
            population_note = (
                f"one or both reports predate the per-cut-off `session_population` "
                f"digest, so it cannot be shown that @{k}'s {bn} queries are the SAME "
                f"{bn} queries in both runs. Equal counts do not establish that. "
                "Re-run both arms with the current harness."
            )
        elif bp != cp:
            population_note = (
                f"@{k} was measurable for {bn} queries in each run, but not the same "
                f"{bn} queries — the eligible sets differ (baseline {bp[:12]}…, "
                f"candidate {cp[:12]}…). Equal denominators over different "
                "populations is exactly the case a count check cannot catch, so no "
                "delta is reported."
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
        bv = _metric_value(b_sess, name)
        cv = _metric_value(c_sess, name)
        if bv is not None and cv is not None:
            rows.append((name, f"{bv:.4f}", f"{cv:.4f}", f"{cv - bv:+.4f}"))
        elif name in b_sess or name in c_sess:
            # Present on exactly one side, or present and unusable on one. Named,
            # never zero-filled -- an unreadable value is as uncomparable as a
            # missing one, and treating them the same keeps that from becoming a
            # third case to reason about.
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
