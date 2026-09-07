"""``kirocrew bench`` -- external memory benchmarks (LongMemEval, LoCoMo).

A separate command from ``kirocrew eval`` because they answer different questions.
``eval`` runs four hand-written scenarios carrying nineteen substring assertions in
a single pass: a fine smoke test, and a poor instrument for "did this change help",
since one flipped assertion out of nineteen is indistinguishable from sampling
noise and sampling cannot be pinned (no ``temperature`` or ``seed`` is threaded
through the provider stack). ``bench`` measures thousands of questions with gold
evidence against the real ``VectorMemoryStore``, and its primary ruler is
deterministic, so a delta is exact.

Thin CLI layer by design: argument handling, guard messages and output only. The
measurement lives in :mod:`kiro_crew.eval.bench`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kiro_crew import cli_help

DEFAULT_OUT_DIR = "bench_results"


class _BenchError(Exception):
    """Carries an exit code up to the dispatch, so helpers need not print-and-return."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _positive_int(raw: str) -> int:
    """argparse type for every count this CLI accepts. ONE place, by design.

    `type=int` accepts negatives, and every one of these values is a slice size or
    a cut-off where a negative is not merely invalid but silently wrong:
    `--instances -1` reaches `Corpus.subset`, where Python's negative slicing
    quietly measures all-but-the-last instance and reports it as a full run. A
    number that changes what was measured without changing what is claimed is the
    failure this harness exists to avoid.

    Raising `ArgumentTypeError` puts the refusal in argparse's own error path, so
    it is reported before any corpus is read, with the option name attached.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(
            f"expected a positive integer, got {value}. Negative and zero counts "
            "either select from the tail or select nothing, and both would be "
            "reported as a normal run."
        )
    return value


def register_bench_parser(sub: argparse._SubParsersAction) -> None:
    """Wire ``kirocrew bench`` into the top-level parser."""
    parser = cli_help.add_command(
        sub,
        "bench",
        description=(
            "Measures the Kiro Crew memory layer against published benchmarks. The "
            "retrieval ruler is deterministic, so a delta between two commits is "
            "exact and needs a single pass -- unlike an end-to-end answer score, "
            "which is a random variable because sampling cannot be pinned."
        ),
    )
    bench_sub = parser.add_subparsers(dest="bench_action")

    bench_sub.add_parser("list", help="Show the available corpora and what is cached")

    fetch = bench_sub.add_parser(
        "fetch", help="Download a corpus into the local cache and verify its checksum"
    )
    fetch.add_argument(
        "corpus",
        nargs="?",
        help="Corpus key (see 'bench list'). Omit to fetch the small default, locomo10.",
        default="locomo10",
    )

    retr = bench_sub.add_parser(
        "retrieval",
        help="Measure retrieval recall/nDCG against a corpus (deterministic)",
        description=(
            "Ingests each instance's haystack into its own real VectorMemoryStore, "
            "runs every question through search_episodic, and scores whether the "
            "gold evidence was surfaced. Refuses to run against a corpus with no "
            "distractor sessions, because recall there is trivially 1.0."
        ),
    )
    retr.add_argument("corpus", nargs="?", default="locomo10", help="Corpus key")
    retr.add_argument(
        "--instances",
        type=_positive_int,
        default=None,
        help="Head-slice to N instances (smoke runs)",
    )
    retr.add_argument(
        "--queries",
        type=_positive_int,
        default=None,
        help="Head-slice to N queries per instance",
    )
    retr.add_argument(
        "--granularity",
        choices=("turn", "session"),
        default="turn",
        help="One episodic fragment per utterance (default) or per session",
    )
    retr.add_argument(
        "--timeline",
        choices=("now", "anchored", "literal"),
        default="anchored",
        help=(
            "How the corpus's dates map onto created_at. 'anchored' (default) keeps "
            "relative gaps with the newest session at now; 'now' flattens every "
            "fragment to one instant, making the store's recency decay a constant "
            "and isolating pure semantic ranking; 'literal' uses the dataset's "
            "absolute dates, which on a 2023 corpus underflows the decay term."
        ),
    )
    retr.add_argument(
        "--no-mmr",
        action="store_true",
        help="Disable the store's MMR diversity reranking (on by default in production)",
    )
    retr.add_argument(
        "--no-dedup",
        action="store_true",
        help=(
            "Raise the near-duplicate cosine threshold above 1.0 so nothing can be "
            "judged a near-duplicate. Note this does NOT disable all dedup: "
            "write_episodic also rejects any text whose lowercased first 80 "
            "characters already exist, unconditionally. And the cosine path is "
            "gated on a live FAISS index, so without faiss this flag is inert."
        ),
    )
    retr.add_argument(
        "--toy-embedder",
        action="store_true",
        help=(
            "Use a deterministic hashed bag-of-words stand-in instead of the real "
            "model. For verifying the harness on a host that cannot load the "
            "embedder. NOT a source of reportable numbers."
        ),
    )
    retr.add_argument(
        "--out-dir", default=DEFAULT_OUT_DIR, help=f"Where to write the report (default: {DEFAULT_OUT_DIR})"
    )
    retr.add_argument("--stem", default=None, help="Report filename stem")

    cmp_p = bench_sub.add_parser(
        "compare",
        help="Diff two saved JSON reports",
        description=(
            "Refuses to attribute a delta when the two runs disagree on corpus "
            "fingerprint, ingest config, retrieval config or search backend -- any "
            "of those makes the difference unattributable to the code change."
        ),
    )
    cmp_p.add_argument("baseline", help="Path to the baseline .json report")
    cmp_p.add_argument("candidate", help="Path to the candidate .json report")
    cmp_p.add_argument(
        "-k", type=_positive_int, default=5, help="Cut-off to report (default: 5)"
    )

    kb = bench_sub.add_parser(
        "kb-retrieval",
        help="Measure Knowledge Library recall/MRR/nDCG against a golden set (deterministic)",
        description=(
            "Ingests a labeled golden set of documents into a throwaway "
            "KnowledgeStore, runs each query through the real HybridRetriever, and "
            "scores whether the gold doc was surfaced -- reported as recall@k, MRR "
            "and nDCG per query class (clean-fact, multi-hop, time-bound, "
            "correction, contradiction, retraction, reinforcement, "
            "hypothetical-exclusion, abstention, citation-fidelity). Distinct from "
            "'bench retrieval', which measures the conversational memory layer. "
            "Defaults to the deterministic toy embedder so it runs anywhere; pass "
            "--real-embedder for a semantic run when the model is resident."
        ),
    )
    kb.add_argument(
        "golden",
        nargs="?",
        default=None,
        help="Path to a golden-set JSON (default: the packaged kb_golden_v1.json)",
    )
    kb.add_argument(
        "-k",
        type=_positive_int,
        default=3,
        help="Cut-off to headline in the printed summary (default: 3)",
    )
    kb.add_argument(
        "--real-embedder",
        action="store_true",
        help=(
            "Use the in-process Qwen3 embedder instead of the deterministic toy "
            "stand-in. Refuses if the model is not resident on this host. Required "
            "for a reportable semantic number."
        ),
    )
    kb.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Skip the vector leg entirely (keyword+graph only), to isolate the FTS contribution",
    )


def bench_cmd(args: argparse.Namespace) -> int:
    """Dispatch, with the ONE catch site for every deliberate refusal.

    `BenchRefusal` is the base class of every refusal this harness raises on
    purpose, so a new refusal type is reported cleanly the moment it inherits --
    there is no tuple of exception types to forget to extend, which is how
    `bench fetch` ended up catching `CorpusFetchError` while `UnsafePathError`
    escaped with a traceback from the same command.

    The subcommands keep their own narrower handlers where a tailored message is
    worth it; this is the floor, not a replacement.
    """
    from kiro_crew.eval.bench.errors import BenchRefusal

    try:
        return _bench_dispatch(args)
    except BenchRefusal as exc:
        # A refusal is a normal outcome to report: the message explains itself and
        # the exit code says it did not produce a number.
        print(f"error: {exc}")
        return 1
    except OSError as exc:
        # Not a refusal — an ordinary filesystem failure. A read-only output
        # directory, a full disk, a missing parent: `mkdir` and `open` raise these,
        # and this command has no more to say about them than the OS does. Reported
        # rather than tracebacked, because a benchmark that cannot write its report
        # is a failed run, not a crashed program.
        print(f"error: {exc}")
        return 1


def _bench_dispatch(args: argparse.Namespace) -> int:
    """Route to a subcommand. Returns a process exit code."""
    action = getattr(args, "bench_action", None)
    if action is None:
        print("usage: kirocrew bench {list,fetch,retrieval,kb-retrieval,compare}")
        return 2

    # Deferred deliberately, and measured. `cli.py` imports this module at module
    # scope, so every `kirocrew` invocation of every subcommand loads it. Hoisting
    # the bench imports to module scope costs +280 modules and +0.21s of import
    # time on this host (152 -> 432 modules, 0.046s -> 0.254s) and drags
    # `kiro_crew.vector_memory` plus `sqlite3` into the boot path of commands that
    # will never touch a benchmark. `test/test_perf_boot_path.py` exists to pin
    # exactly that class of regression, and `cli.py` already uses this pattern for
    # the same reason (see its `computer_use.cli` import inside the dispatch).
    # AUTOSDE `top-level-imports` is `blocking: false`; its concerns (dependency
    # tracing, IDE navigation, mock targeting) are real but are outweighed here by
    # a cost the repo actively tests for.
    from kiro_crew.eval.bench import datasets

    if action == "list":
        print(datasets.describe())
        return 0

    if action == "fetch":
        try:
            path = datasets.ensure(args.corpus)
        except datasets.CorpusFetchError as exc:
            print(f"error: {exc}")
            return 1
        print(f"ready: {path}")
        return 0

    if action == "compare":
        return _compare(args)

    if action == "retrieval":
        return _retrieval(args)

    if action == "kb-retrieval":
        return _kb_retrieval(args)

    print(f"unknown bench action: {action}")
    return 2


def _load_report(path: str, label: str) -> dict:
    """Read one saved JSON report through the sensitive-path gate.

    Two reasons this is not a bare ``Path(...).read_text()``. The obvious one is
    robustness: a missing file or a truncated report should print one line, not a
    traceback, because every other error path in this command already does.

    The one that actually matters is the principal. These paths come from argv,
    and in this product argv is not always typed by the human who owns the
    machine -- an agent can run any CLI command, so ``kirocrew bench compare
    ~/.aws/credentials x.json`` is a reachable invocation. ``safe_read_file``
    canonicalizes through symlinks, re-checks the RESOLVED target against
    ``is_sensitive_path``, and opens with ``O_NOFOLLOW``; without it this
    subcommand is a file-read primitive that bypasses the gate every other read
    path in the codebase goes through.
    """
    from kiro_crew.hooks import safe_read_file

    # The messages below name only `label` -- never the caller-supplied path and
    # never the raw exception text. Two reasons, and the second is why it is worth
    # the small loss of detail:
    #
    #  * `label` is already unambiguous. `compare` takes exactly two reports, so
    #    "baseline" / "candidate" identifies which one failed without echoing
    #    anything back.
    #  * these paths arrive from argv, and echoing an argv-derived string into
    #    stdout is a taint flow a scanner cannot distinguish from a real leak
    #    (CodeQL `py/clear-text-logging-sensitive-data` flagged all four sites at
    #    high severity). Rather than argue about whether a filename is a secret,
    #    remove the flow: nothing read from the file and nothing derived from argv
    #    reaches the output. A refused sensitive path in particular should not have
    #    its fully-resolved form printed -- resolution follows symlinks, so the
    #    message would disclose more than the user supplied.
    try:
        raw = safe_read_file(path)
    except PermissionError:
        print(
            f"error: refusing to read the {label} report -- it resolves to a "
            "sensitive location (credential store or the governance trust root)."
        )
        raise _BenchError(1) from None
    except FileNotFoundError:
        print(f"error: {label} report not found.")
        raise _BenchError(1) from None
    except OSError:
        print(f"error: cannot read the {label} report.")
        raise _BenchError(1) from None

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        print(
            f"error: the {label} report is not valid JSON. A report truncated by "
            "an interrupted run is the usual cause -- re-run "
            "'kirocrew bench retrieval' to regenerate it."
        )
        raise _BenchError(1) from None

    if not isinstance(loaded, dict):
        print(f"error: the {label} report is not a report object.")
        raise _BenchError(1)
    return loaded


def _compare(args: argparse.Namespace) -> int:
    from kiro_crew.eval.bench import compare_reports

    try:
        base = _load_report(args.baseline, "baseline")
        cand = _load_report(args.candidate, "candidate")
    except _BenchError as exc:
        return exc.code
    print(compare_reports(base, cand, k=args.k))
    return 0


def _load_corpus(key: str):  # noqa: ANN202 - Corpus, but imported lazily
    """Resolve a corpus key to a loaded Corpus via the right adapter."""
    from kiro_crew.eval.bench import datasets
    from kiro_crew.eval.bench.adapters import load_locomo, load_longmemeval

    spec = datasets.SPECS.get(key)
    if spec is None:
        raise SystemExit(
            f"unknown corpus {key!r}; known: {', '.join(sorted(datasets.SPECS))}"
        )
    path = datasets.ensure(spec)
    raw = datasets.load_json(spec)
    if spec.dataset == "locomo":
        return load_locomo(raw, source_path=str(path))
    return load_longmemeval(raw, variant=spec.variant, source_path=str(path))


def _retrieval(args: argparse.Namespace) -> int:
    from kiro_crew.eval.bench import IngestConfig, RetrievalConfig, run_retrieval, write_report
    from kiro_crew.eval.bench.ingest import IngestError
    from kiro_crew.eval.bench.retrieval import RetrievalNotMeasurable
    from kiro_crew.eval.bench.safepath import UnsafePathError

    try:
        corpus = _load_corpus(args.corpus)
    except Exception as exc:  # datasets.CorpusFetchError or a schema error
        print(f"error: {exc}")
        return 1

    if args.instances is not None or args.queries is not None:
        corpus = corpus.subset(instances=args.instances, queries_per_instance=args.queries)

    ingest = IngestConfig(
        granularity=args.granularity,
        timeline=args.timeline,
        # 1.01 is above the maximum possible cosine similarity, so nothing can be
        # judged a duplicate. Cleaner than threading a separate disable flag
        # through the store's own comparison.
        dedup_threshold=1.01 if args.no_dedup else 0.88,
    )
    retrieval = RetrievalConfig(mmr=not args.no_mmr)

    embed_fn = None
    embedder_id = None
    if args.toy_embedder:
        from kiro_crew.eval.bench.toy_embedder import TOY_EMBEDDER_ID, toy_embed_fn

        embed_fn = toy_embed_fn()
        # Recorded into the report so `bench compare` refuses to diff a toy
        # baseline against a real run. Without it the two are indistinguishable in
        # the saved config and the comparison publishes an "exact" delta between a
        # hashed bag-of-words and a language model.
        embedder_id = TOY_EMBEDDER_ID
        print(
            "WARNING: using the toy hashed-bag-of-words embedder. These numbers "
            "measure term overlap, not semantic recall, and must not be reported "
            "as a benchmark result."
        )

    try:
        result = run_retrieval(
            corpus,
            ingest_config=ingest,
            retrieval_config=retrieval,
            embed_fn=embed_fn,
            embedder_id=embedder_id,
        )
    except (RetrievalNotMeasurable, IngestError, UnsafePathError) as exc:
        # Every deliberate refusal in the harness raises rather than returning a
        # meaningless number, so every one of them needs a catch here or the guard
        # that exists to print a good message dumps a traceback instead. IngestError
        # is the one that bites in practice: it fires whenever the embedding model is
        # not resident, which is the normal state on a host whose vendored
        # llama.cpp payload is incomplete.
        print(f"refusing to run: {exc}")
        return 1
    except ValueError as exc:
        # The embedder-identity fail-closed check. Reachable only from a
        # programmatic caller today, but a traceback is never the right output for a
        # deliberate refusal.
        print(f"error: {exc}")
        return 1

    from kiro_crew.eval.bench import format_report

    print(format_report(result))
    try:
        md, js = write_report(result, Path(args.out_dir), stem=args.stem)
    except UnsafePathError as exc:
        # The report is already printed above, so the measurement is not lost --
        # only the file. Say so, rather than letting a refused write look like a
        # failed run.
        print(f"\nnot saved: {exc}")
        return 1
    print(f"\nwrote {md}\n      {js}")
    return 0


def _kb_retrieval(args: argparse.Namespace) -> int:
    """Run the Knowledge Library recall harness against a golden set.

    Lazy imports, like ``_retrieval``: this keeps ``knowledge.store`` /
    ``knowledge.retrieval`` (and their sqlite/embedder pull-ins) out of the boot
    path of every unrelated ``kirocrew`` subcommand -- the perf regression
    ``test/test_perf_boot_path.py`` pins.
    """
    from kiro_crew.eval.bench.kb_retrieval import (
        KBGoldenSet,
        KBGoldenSetError,
        default_golden_set_path,
        format_kb_report,
        run_kb_retrieval,
    )

    path = args.golden or default_golden_set_path()
    try:
        golden = KBGoldenSet.from_json(path)
    except (KBGoldenSetError, UnicodeError) as exc:
        # UnicodeError: a golden file with invalid UTF-8 (or a lone surrogate)
        # raises from the decode inside from_json -- a corrupt input is a
        # deliberate refusal, not a traceback, same as any other malformed file.
        print(f"refusing to run: {exc}")
        return 1

    embed_fn = None
    embedder_id = "toy-hashed-bow"
    if args.real_embedder:
        from kiro_crew.knowledge.embedder import InProcessEmbedder

        embedder = InProcessEmbedder()
        if not embedder.is_available():
            print(
                "refusing to run: --real-embedder requested but the in-process "
                "embedding model is not resident on this host. Omit the flag to "
                "use the deterministic toy embedder (plumbing check only)."
            )
            return 1
        embed_fn = embedder.embed
        # The embedder's own model identity, never a hardcoded literal: a run
        # with a custom model (InProcessEmbedder(model=...) or a swapped backend)
        # must be labeled as that model in the report, or `bench compare` diffs
        # apples against oranges under the same name. Mirrors the fail-closed
        # embedder-identity invariant run_kb_retrieval enforces.
        embedder_id = embedder.model
    elif not args.no_embeddings:
        print(
            "WARNING: using the toy hashed-bag-of-words embedder. These numbers "
            "measure term overlap, not semantic recall, and must not be reported "
            "as a benchmark result. Use --real-embedder for a real number."
        )

    # The SAME driver module the KnowledgeStore uses: on Linux x86_64 that is
    # pysqlite3, whose exception classes are distinct from stdlib sqlite3's --
    # catching the stdlib class there would miss every real store failure.
    from kiro_crew._sqlite_compat import sqlite3

    try:
        report = run_kb_retrieval(
            golden,
            embed_fn=embed_fn,
            embedder_id=embedder_id,
            use_embeddings=not args.no_embeddings,
            # Include the headlined cut-off in the computed set. Otherwise a
            # non-default -k (e.g. -k 2) is absent from every per-query dict and
            # headline()/by_class() default it to 0.0 -- a false-zero benchmark
            # result, the exact failure the sibling retrieval harness forbids.
            k_values=tuple(sorted({1, 3, 5, 10, args.k})),
        )
    except KBGoldenSetError as exc:
        print(f"refusing to run: {exc}")
        return 1
    except (sqlite3.Error, UnicodeError) as exc:
        # The harness builds a throwaway KnowledgeStore in a temp dir; a full
        # temp volume (or any other database-level failure) surfaces here as a
        # sqlite3 error, and doc content that defeats UTF-8 encoding (e.g. a
        # lone surrogate reaching the store/embedding path) as a UnicodeError.
        # A deliberate refusal, not a traceback -- the same boundary rule every
        # other refusal in this dispatch follows.
        print(f"refusing to run: store error while building the eval corpus: {exc}")
        return 1

    print(format_kb_report(report, k=args.k))
    return 0
