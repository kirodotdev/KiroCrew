"""Deterministic recall harness for session search.

Session search finds a past conversation from a few words: the dashboard history
filter, ``search_chat_history`` and the channel session picker all rank through
``ConversationLog.search_sessions``. This harness answers one question: *given a
labeled set of ``(query -> the conversation it is looking for)`` pairs, does
session search put that conversation near the top?* It writes the golden
sessions as transcripts into a throwaway directory and searches them with that
same call, so a change to the ranking shows up here as an exact delta between two
commits.

The query classes are the ways a person looks for a conversation: an identifier
or phrase copied from it, a description in other words (in English, and in
Chinese), one or two words, an identifier together with a description, and a
topic the corpus never discusses, which must find nothing.

Each session's file is dated *age_days* before the run starts. The ranking boosts
recent sessions against the clock at search time, so dating the files relative to
the run keeps every session the same age, and so the same score, whenever it runs.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

from kiro_crew.eval.bench.errors import BenchRefusal
from kiro_crew.eval.bench.kb_retrieval import mrr_at_k
from kiro_crew.eval.bench.retrieval import recall_any_at_k
from kiro_crew.eval.bench.safepath import UnsafePathError, read_text_nofollow

SESSION_QUERY_CLASSES: tuple[str, ...] = (
    "literal",
    "paraphrase",
    "paraphrase_cjk",
    "short",
    "identifier_plus_description",
    "no_answer",
)

#: The class whose queries have no answer in the corpus.
NO_ANSWER_CLASS = "no_answer"

#: 1 is the row a person reads first; 10 is the rows ``search_chat_history`` shows.
DEFAULT_SESSION_K_VALUES: tuple[int, ...] = (1, 3, 10)

#: The default ranking every surface ran before the meaning leg, and the one this
#: harness has always scored.
KEYWORD_RANKER = "keyword"
#: The meaning leg's own ranking of every session, and the routed result rows.
VECTOR_RANKER, ROUTED_RANKER = "vector", "routed"
#: Shares of sessions embedded that the routed ranking is also scored at (RFC
#: ``rfc-session-semantic-search`` 8.3), the newest first, as a first build reaches them.
#: Each is scored twice, keyword rows first and meaning rows first, which is the
#: choice ``SEMANTIC_MIN_COVERAGE`` makes.
MEANING_COVERAGE_SHARES: tuple[float, ...] = (0.5, 0.75, 0.9)
#: The prefixes of those rankers' names, before ``@<share>``.
KEYWORD_FIRST, MEANING_FIRST = "keyword-first", "meaning-first"

#: A hand-authored golden set has no business approaching this.
_GOLDEN_MAX_BYTES = 4 * 1024 * 1024

#: A session id becomes a file name, so it is held to a plain token that is not a
#: device name Windows refuses to create a file under.
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{n}" for n in range(1, 10)),
        *(f"LPT{n}" for n in range(1, 10)),
    }
)

#: A file can only be dated this far back; ``os.utime`` overflows long before int does.
_MAX_AGE_DAYS = 36500

_ROLES = frozenset({"user", "assistant"})
_SECONDS_PER_DAY = 86400


class SessionGoldenSetError(BenchRefusal):
    """The golden set is malformed, or the search could not see all of it.

    A refusal rather than a number: a session the search never read scores as a
    miss the ranking never made.
    """


@dataclass(frozen=True)
class GoldenSession:
    """One conversation. ``id`` is the gold label queries name and the file name."""

    id: str
    age_days: int
    messages: tuple[tuple[str, str], ...]

    @classmethod
    def from_raw(cls, raw: object) -> "GoldenSession":
        if not isinstance(raw, dict):
            raise SessionGoldenSetError(f"malformed session entry: {raw!r}")
        session_id, age_days, messages = raw.get("id"), raw.get("age_days"), raw.get("messages")
        if (
            not isinstance(session_id, str)
            or not _SESSION_ID.fullmatch(session_id)
            or session_id.upper() in _WINDOWS_DEVICE_NAMES
        ):
            raise SessionGoldenSetError(
                "session id must be 1 to 64 letters, digits, '_' or '-', and not a "
                f"Windows device name: {session_id!r}"
            )
        if (
            isinstance(age_days, bool)
            or not isinstance(age_days, int)
            or not 0 <= age_days <= _MAX_AGE_DAYS
        ):
            raise SessionGoldenSetError(
                f"session {session_id!r} needs an age_days from 0 to {_MAX_AGE_DAYS}"
            )
        if not isinstance(messages, list) or not messages:
            raise SessionGoldenSetError(f"session {session_id!r} needs a non-empty messages list")
        parsed: list[tuple[str, str]] = []
        for message in messages:
            role = message.get("role") if isinstance(message, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if role not in _ROLES or not isinstance(content, str) or not content.strip():
                raise SessionGoldenSetError(
                    f"session {session_id!r} has a message without a user or assistant "
                    f"role and text: {message!r}"
                )
            parsed.append((role, content))
        return cls(id=session_id, age_days=age_days, messages=tuple(parsed))


@dataclass(frozen=True)
class GoldenQuery:
    """One labeled query and the sessions that answer it."""

    id: str
    query_class: str
    query: str
    gold_session_ids: tuple[str, ...]

    @classmethod
    def from_raw(cls, raw: object) -> "GoldenQuery":
        if not isinstance(raw, dict):
            raise SessionGoldenSetError(f"malformed query entry: {raw!r}")
        query_id, query_class = raw.get("id"), raw.get("class")
        query, gold = raw.get("query"), raw.get("gold_session_ids")
        if not isinstance(query_id, str) or not query_id:
            raise SessionGoldenSetError(f"query entry needs a string id: {raw!r}")
        if query_class not in SESSION_QUERY_CLASSES:
            raise SessionGoldenSetError(
                f"query {query_id!r} has unknown class {query_class!r}; "
                f"known: {', '.join(SESSION_QUERY_CLASSES)}"
            )
        if not isinstance(query, str) or not query.strip():
            raise SessionGoldenSetError(f"query {query_id!r} has no query text")
        if not isinstance(gold, list) or not all(isinstance(g, str) for g in gold):
            raise SessionGoldenSetError(f"query {query_id!r} needs a gold_session_ids list")
        if query_class == NO_ANSWER_CLASS and gold:
            raise SessionGoldenSetError(f"query {query_id!r} is {NO_ANSWER_CLASS} but names gold")
        if query_class != NO_ANSWER_CLASS and not gold:
            raise SessionGoldenSetError(f"query {query_id!r} names no gold session")
        return cls(id=query_id, query_class=query_class, query=query, gold_session_ids=tuple(gold))


@dataclass(frozen=True)
class SessionGoldenSet:
    name: str
    sessions: tuple[GoldenSession, ...]
    queries: tuple[GoldenQuery, ...]
    #: Bumped with every edit under an existing name that can move a score.
    label_revision: int | None = None

    def validate(self) -> None:
        """Refuse a set whose labels cannot be scored as written."""
        if not self.sessions or all(q.query_class == NO_ANSWER_CLASS for q in self.queries):
            raise SessionGoldenSetError(
                "golden set needs at least one session and one query with an answer"
            )
        for owner, texts in [
            (s.id, [text for _role, text in s.messages]) for s in self.sessions
        ] + [(q.id, [q.query, *q.gold_session_ids]) for q in self.queries]:
            try:
                for text in texts:
                    text.encode("utf-8")
            except UnicodeEncodeError as exc:
                # A JSON escape such as "\ud800" parses to a lone surrogate,
                # which cannot be written to a transcript file.
                raise SessionGoldenSetError(
                    f"{owner!r} holds text that is not valid UTF-8"
                ) from exc
        session_ids = [s.id for s in self.sessions]
        if len({i.casefold() for i in session_ids}) != len(session_ids):
            # Case-folded: on a case-insensitive filesystem two such ids are one file.
            raise SessionGoldenSetError("golden set has duplicate session ids")
        ages = [s.age_days for s in self.sessions]
        if len(set(ages)) != len(ages):
            # Search lists sessions newest first; two files with one modification
            # time have no defined order, so a tie could rank differently per run.
            raise SessionGoldenSetError("golden set has two sessions with the same age_days")
        query_ids = [q.id for q in self.queries]
        if len(set(query_ids)) != len(query_ids):
            raise SessionGoldenSetError("golden set has duplicate query ids")
        known = set(session_ids)
        for query in self.queries:
            unknown = [g for g in query.gold_session_ids if g not in known]
            if unknown:
                raise SessionGoldenSetError(
                    f"query {query.id!r} names sessions no entry defines: {', '.join(unknown)}"
                )

    @classmethod
    def from_json(cls, path: str | Path) -> "SessionGoldenSet":
        p = Path(path)
        try:
            text = read_text_nofollow(p, what="golden set", max_bytes=_GOLDEN_MAX_BYTES)
        except UnsafePathError as exc:
            raise SessionGoldenSetError(str(exc)) from exc
        except OSError as exc:
            raise SessionGoldenSetError(
                f"golden set could not be read: {str(p)!r} ({exc!r})"
            ) from exc
        except UnicodeError as exc:
            raise SessionGoldenSetError(f"golden set is not valid UTF-8: {str(p)!r}") from exc
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise SessionGoldenSetError(f"golden set is not parseable JSON: {str(p)!r}") from exc
        if not isinstance(raw, dict):
            raise SessionGoldenSetError(f"golden set must be a JSON object: {str(p)!r}")
        name = raw.get("name", p.stem)
        revision = raw.get("label_revision")
        sessions_raw, queries_raw = raw.get("sessions"), raw.get("queries")
        if not isinstance(name, str) or not name or not name.isprintable():
            raise SessionGoldenSetError(f"golden set 'name' must be printable text: {str(p)!r}")
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int)):
            raise SessionGoldenSetError(
                f"golden set 'label_revision' must be an integer: {str(p)!r}"
            )
        if not isinstance(sessions_raw, list) or not isinstance(queries_raw, list):
            raise SessionGoldenSetError(
                f"golden set needs 'sessions' and 'queries' lists: {str(p)!r}"
            )
        golden = cls(
            name=name,
            sessions=tuple(GoldenSession.from_raw(s) for s in sessions_raw),
            queries=tuple(GoldenQuery.from_raw(q) for q in queries_raw),
            label_revision=revision,
        )
        golden.validate()
        return golden


def default_session_golden_set_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "session_golden_v1.json"


@dataclass(frozen=True)
class SessionQueryResult:
    query_id: str
    query_class: str
    ranked_session_ids: tuple[str, ...]
    recall_any: dict[int, float]
    mrr: dict[int, float]


@dataclass
class SessionRecallReport:
    golden_set: str
    label_revision: int | None
    segmenter: str
    session_count: int
    k_values: tuple[int, ...]
    results: list[SessionQueryResult] = field(default_factory=list)
    #: With ``meaning=True``, the results of every other ranker by name: the vector
    #: leg, the routed rows, and the routed rows at each partial coverage share.
    rankers: dict[str, list[SessionQueryResult]] = field(default_factory=dict)
    #: With ``meaning=True``, what the leg cost on this set (see :func:`_measure_leg`).
    resources: dict[str, float] = field(default_factory=dict)

    def _require_k(self, k: int) -> None:
        if k not in self.k_values:
            # A cut-off that was never computed would read as a 0.0 score.
            raise ValueError(f"k={k} was not computed; computed: {list(self.k_values)}")

    def _ranked(self, ranker: str) -> list[SessionQueryResult]:
        if ranker == KEYWORD_RANKER:
            return self.results
        if ranker not in self.rankers:
            raise ValueError(
                f"ranker {ranker!r} was not run; run: {[KEYWORD_RANKER, *self.rankers]}"
            )
        return self.rankers[ranker]

    def _means(self, results: Sequence[SessionQueryResult], k: int) -> dict[str, float]:
        n = len(results)
        return {
            "recall_any": sum(r.recall_any[k] for r in results) / n,
            "mrr": sum(r.mrr[k] for r in results) / n,
            "n": float(n),
        }

    def answerable(self, k: int, ranker: str = KEYWORD_RANKER) -> dict[str, float]:
        """Means over every query that has an answer."""
        self._require_k(k)
        results = self._ranked(ranker)
        return self._means([r for r in results if r.query_class != NO_ANSWER_CLASS], k)

    def by_class(self, k: int, ranker: str = KEYWORD_RANKER) -> dict[str, dict[str, float]]:
        """Means per answerable class; a no-answer class reports how many returned rows."""
        self._require_k(k)
        results = self._ranked(ranker)
        out: dict[str, dict[str, float]] = {}
        for query_class in SESSION_QUERY_CLASSES:
            members = [r for r in results if r.query_class == query_class]
            if not members:
                continue
            if query_class == NO_ANSWER_CLASS:
                with_rows = sum(1 for r in members if r.ranked_session_ids)
                out[query_class] = {"n": float(len(members)), "queries_with_rows": float(with_rows)}
            else:
                out[query_class] = self._means(members, k)
        return out


def _write_sessions(directory: Path, sessions: Sequence[GoldenSession], *, now: float) -> None:
    """Write each session as ``<id>.jsonl``, dated *age_days* before *now*.

    The title is explicit: with none, ``list_sessions`` takes it from the first
    message, and a title hit carries the title boost, which would rank the
    session on its first message instead of its whole content.
    """
    for session in sessions:
        path = directory / f"{session.id}.jsonl"
        lines = [json.dumps({"_type": "metadata", "title": f"session {session.id}"})]
        lines += [
            json.dumps({"role": role, "content": content}, ensure_ascii=False)
            for role, content in session.messages
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stamp = now - session.age_days * _SECONDS_PER_DAY
        os.utime(path, (stamp, stamp))


@contextmanager
def _dictionary_segmenter() -> Iterator[str]:
    """Split Chinese queries with the dictionary the gateway uses, and name the result.

    The gateway registers ``segment_runs``; a process that registers nothing
    matches Chinese character by character, which would score a different search
    than the one users run. Yields ``"dictionary"``, or ``"character path"`` where
    the dictionary cannot load. The slot is process-wide, so the previous
    segmenter is put back on exit.
    """
    from kiro_crew import history_search
    from kiro_crew.history_words import segment_runs

    previous = history_search._cjk_slot.segment
    history_search.register_cjk_segmenter(segment_runs)
    try:
        yield "dictionary" if segment_runs(["中文"]) is not None else "character path"
    finally:
        history_search.register_cjk_segmenter(previous)


def _scored(query: GoldenQuery, ranked: Sequence[str], k_values: tuple[int, ...]):
    gold = list(query.gold_session_ids)
    ranked = tuple(ranked)
    return SessionQueryResult(
        query_id=query.id,
        query_class=query.query_class,
        ranked_session_ids=ranked,
        recall_any={k: recall_any_at_k(ranked, gold, k) for k in k_values},
        mrr={k: mrr_at_k(ranked, gold, k) for k in k_values},
    )


@contextmanager
def _meaning_leg(log, golden_ids: Sequence[str]) -> Iterator[tuple]:
    """Chunk and embed every session with the in-process model; yield the leg and its space.

    Refuses when this host has no in-process model: a leg that cannot answer
    scores as the keyword ranking, which is not what the run was asked to show.
    ``session.semantic_search`` is held on for the run and put back on exit.
    """
    from kiro_crew import embeddings
    from kiro_crew import history_vectors_fill as fill
    from kiro_crew import history_vectors_index as chunking
    from kiro_crew import history_vectors_search as meaning

    tokenizer = chunking.load_tokenizer()
    embedder = meaning.local_embedder()
    if tokenizer is None or embedder is None:
        raise SessionGoldenSetError(
            "the meaning rankers need the in-process embedding model on this host; "
            "run without --meaning, or install the model first"
        )
    signature = embeddings.embedding_space_signature(embedder.model_id, embedder.dim)
    # A bulk embed answers None while the model loads; an interactive one waits for it.
    if embedder.embed("warm", priority=embeddings.PRIORITY_INTERACTIVE) is None:
        raise SessionGoldenSetError("the in-process embedding model did not load")
    store = log._catalog_projection.vector_store
    previous = meaning._SEMANTIC_SEARCH_OVERRIDE
    meaning._SEMANTIC_SEARCH_OVERRIDE = True
    try:
        started = time.process_time()
        # Every fixture session directly: the live chunk pass waits out a quiet period
        # after each write, which a session dated ``age_days: 0`` is still inside.
        for session in golden_ids:
            outcome = chunking.chunk_session(log, store, session, tokenizer)
            if outcome != chunking.INDEXED:
                raise SessionGoldenSetError(f"session {session!r} could not be chunked ({outcome})")
        while fill.fill_batch(log, store)["filled"]:
            pass
        fill_cpu = time.process_time() - started
        embedded = len(store.embedded_keys(signature))
        if embedded != len(golden_ids):
            raise SessionGoldenSetError(
                f"embedded {embedded} of {len(golden_ids)} sessions; the meaning rankers "
                "would score a partial index"
            )
        yield log._catalog_projection.vector_leg, embedder, signature, fill_cpu
    finally:
        meaning._SEMANTIC_SEARCH_OVERRIDE = previous
        log._catalog_projection.vector_leg.release()
        store.close()


def _measure_leg(leg, embedder, signature: str, fill_cpu: float, query_costs) -> dict[str, float]:
    """The leg's cost on this set, for the phase 4 resource ceilings (RFC 10).

    ``matrix_bytes`` is the resident float32 matrix; ``load_peak_bytes`` the most
    the matrix load allocated at once, as ``tracemalloc`` sees it (numpy reports to
    it); ``fill_cpu_ms_per_chunk`` the process CPU the chunk pass and the fill took
    per chunk; and the query figures the median wall and CPU time of one vector
    ranking, the query embedding included.
    """
    import statistics
    import tracemalloc

    leg.release()
    tracemalloc.start()
    try:
        matrix = leg.current_matrix(signature, embedder.dim)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    chunks = len(matrix.rows) if matrix is not None else 0
    matrix_bytes = float(matrix.array.nbytes) if matrix is not None else 0.0
    return {
        "chunks": float(chunks),
        "dim": float(embedder.dim),
        "matrix_bytes": matrix_bytes,
        "bytes_per_chunk": matrix_bytes / chunks if chunks else 0.0,
        "load_peak_bytes": float(peak),
        "fill_cpu_ms_per_chunk": 1000.0 * fill_cpu / chunks if chunks else 0.0,
        "query_wall_ms_median": 1000.0 * statistics.median(wall for wall, _cpu in query_costs),
        "query_cpu_ms_median": 1000.0 * statistics.median(cpu for _wall, cpu in query_costs),
    }


def _routed_at(log, query: str, keyword: list[dict], hits, metas, coverage: float) -> list[str]:
    """The routed order of *hits* from a partial index, read as *coverage* (RFC 8.3).

    The same :func:`~kiro_crew.history_vectors_search.route` the search runs, fed the
    vector leg's hits for the embedded sessions alone: a *coverage* below
    ``SEMANTIC_MIN_COVERAGE`` puts the keyword rows first, and 1.0 the meaning rows.
    """
    from kiro_crew import history_vectors_search as meaning

    top_folded = log._folded_content(keyword[0]["key"])[1] if keyword else None
    literal = meaning.is_literal(query, top_folded)
    routed = meaning.route(keyword, hits, metas, literal=literal, coverage=coverage)
    return [row.meta["key"] for row in routed]


def run_session_recall(
    golden: SessionGoldenSet,
    *,
    k_values: Sequence[int] = DEFAULT_SESSION_K_VALUES,
    meaning: bool = False,
) -> SessionRecallReport:
    """Score *golden* against ``search_sessions`` and return the report.

    The transcripts live in a private temp directory removed on exit, so no live
    session is read. Not for a process that searches sessions on another thread:
    the Chinese segmenter is swapped process-wide while the run holds it, and with
    *meaning* so is ``session.semantic_search``.

    With *meaning*, every session is also chunked and embedded with this host's
    in-process model, and the report adds the vector leg's ranking, the routed rows
    the search returns, both orders of the routed rows at each of
    ``MEANING_COVERAGE_SHARES`` (keyword rows first, and meaning rows first), and
    the leg's cost (``resources``).
    """
    golden.validate()
    k_values = tuple(sorted({int(k) for k in k_values}))
    if not k_values or k_values[0] < 1:
        raise ValueError("k_values must be positive")
    # Imported here so the CLI's boot path stays free of the history module.
    from kiro_crew.history import ConversationLog

    with tempfile.TemporaryDirectory(prefix="session_eval_") as tmp, _dictionary_segmenter() as seg:
        directory = Path(tmp)
        _write_sessions(directory, golden.sessions, now=time.time())
        log = ConversationLog(base_dir=directory)
        report = SessionRecallReport(
            golden_set=golden.name,
            label_revision=golden.label_revision,
            segmenter=seg,
            session_count=len(golden.sessions),
            k_values=k_values,
        )
        try:
            keyword_rows = {}
            for query in golden.queries:
                # Every keyword row, as the search's own fusion sees them; the scores
                # cut the ranking to k themselves.
                rows = log.search_sessions(
                    query.query, limit=max(k_values[-1], len(golden.sessions)), by_meaning=False
                )
                searched = getattr(rows, "searched", None)
                if searched != len(golden.sessions):
                    raise SessionGoldenSetError(
                        f"search read {searched} of {len(golden.sessions)} sessions; raise "
                        "session.search_scan_window to at least the set's size"
                    )
                keyword_rows[query.id] = list(rows)
                report.results.append(_scored(query, [row["key"] for row in rows], k_values))
            if meaning:
                _run_meaning(log, golden, keyword_rows, report)
        finally:
            # Closed before the directory is removed: an open SQLite handle
            # makes the removal fail on Windows.
            log._catalog_projection.search_index.close()
        return report


def _run_meaning(log, golden: SessionGoldenSet, keyword_rows, report) -> None:
    k_values = report.k_values
    metas = {meta["key"]: meta for meta in log.list_sessions()}
    newest = sorted(golden.sessions, key=lambda session: session.age_days)
    shares = {
        share: {session.id for session in newest[: max(1, round(share * len(newest)))]}
        for share in MEANING_COVERAGE_SHARES
    }
    names = [VECTOR_RANKER, ROUTED_RANKER]
    names += [
        _share_name(order, share) for share in shares for order in (KEYWORD_FIRST, MEANING_FIRST)
    ]
    report.rankers = {name: [] for name in names}
    costs = []
    with _meaning_leg(log, [session.id for session in golden.sessions]) as (
        leg,
        embedder,
        signature,
        fill_cpu,
    ):
        modified = {key: meta.get("modified", 0.0) for key, meta in metas.items()}
        for query in golden.queries:
            wall, cpu = time.perf_counter(), time.process_time()
            hits = leg.rank(query.query, modified, embedder, signature) or []
            costs.append((time.perf_counter() - wall, time.process_time() - cpu))
            vector = [hit.key for hit in hits]
            report.rankers[VECTOR_RANKER].append(_scored(query, vector, k_values))
            routed = log.search_sessions(query.query, limit=k_values[-1])
            keys = [row["key"] for row in routed]
            report.rankers[ROUTED_RANKER].append(_scored(query, keys, k_values))
            for share, embedded in shares.items():
                partial = (
                    leg.rank(
                        query.query,
                        {key: value for key, value in modified.items() if key in embedded},
                        embedder,
                        signature,
                    )
                    or []
                )
                for name, coverage in ((KEYWORD_FIRST, 0.0), (MEANING_FIRST, 1.0)):
                    order = _routed_at(
                        log, query.query, keyword_rows[query.id], partial, metas, coverage
                    )
                    report.rankers[_share_name(name, share)].append(
                        _scored(query, order[: k_values[-1]], k_values)
                    )
        report.resources = _measure_leg(leg, embedder, signature, fill_cpu, costs)


def _share_name(order: str, share: float) -> str:
    return f"{order}@{share:.2f}"


def format_session_report(report: SessionRecallReport, *, k: int = 10) -> str:
    labels = (
        f"label revision {report.label_revision}"
        if report.label_revision is not None
        else "unrevised labels"
    )
    lines = [
        f"Session recall eval: {report.golden_set} ({labels})",
        "ranker: search_sessions (keyword)",
        f"chinese segmenter: {report.segmenter}",
        f"sessions: {report.session_count}",
        "",
        f"BY CLASS (R@1, R@{k}, MRR@{k}):",
    ]
    first = report.k_values[0]
    for query_class, m in report.by_class(k).items():
        n = int(m["n"])
        if query_class == NO_ANSWER_CLASS:
            lines.append(
                f"  {query_class:<30} n={n:<3} queries with rows: {int(m['queries_with_rows'])}"
            )
            continue
        at_first = report.by_class(first)[query_class]["recall_any"]
        lines.append(
            f"  {query_class:<30} n={n:<3} R@{first}={at_first:.3f} "
            f"R@{k}={m['recall_any']:.3f} MRR@{k}={m['mrr']:.3f}"
        )
    whole = report.answerable(k)
    whole_first = report.answerable(first)["recall_any"]
    lines.append(
        f"  {'all answerable':<30} n={int(whole['n']):<3} R@{first}={whole_first:.3f} "
        f"R@{k}={whole['recall_any']:.3f} MRR@{k}={whole['mrr']:.3f}"
    )
    if report.rankers:
        lines += _format_meaning(report, k)
    return "\n".join(lines)


def _format_meaning(report: SessionRecallReport, k: int) -> list[str]:
    first = report.k_values[0]
    rankers = (KEYWORD_RANKER, VECTOR_RANKER, ROUTED_RANKER)
    lines = ["", f"MEANING, R@{first} BY CLASS ({' / '.join(rankers)}):"]
    per = {ranker: report.by_class(first, ranker) for ranker in rankers}
    for query_class, m in per[KEYWORD_RANKER].items():
        if query_class == NO_ANSWER_CLASS:
            with_rows = " / ".join(
                str(int(per[r][query_class]["queries_with_rows"])) for r in rankers
            )
            lines.append(f"  {query_class:<30} n={int(m['n']):<3} queries with rows: {with_rows}")
            continue
        scores = " / ".join(f"{per[r][query_class]['recall_any']:.3f}" for r in rankers)
        lines.append(f"  {query_class:<30} n={int(m['n']):<3} {scores}")
    scores = " / ".join(f"{report.answerable(first, r)['recall_any']:.3f}" for r in rankers)
    lines.append(f"  {'all answerable':<30} {'':<5} {scores}")
    lines += [
        "",
        f"PARTIAL COVERAGE, newest first (R@{first}, R@{k}; keyword first | meaning first):",
    ]
    for share in sorted({n.partition("@")[2] for n in report.rankers if "@" in n}):
        cells = []
        for order in (KEYWORD_FIRST, MEANING_FIRST):
            name = f"{order}@{share}"
            cells.append(
                f"R@{first}={report.answerable(first, name)['recall_any']:.3f} "
                f"R@{k}={report.answerable(k, name)['recall_any']:.3f}"
            )
        lines.append(f"  coverage {share}  {cells[0]} | {cells[1]}")
    lines.append(
        f"  coverage 1.00  R@{first}={report.answerable(first, ROUTED_RANKER)['recall_any']:.3f} "
        f"R@{k}={report.answerable(k, ROUTED_RANKER)['recall_any']:.3f} (routed)"
    )
    res = report.resources
    if res:
        lines += [
            "",
            "RESOURCES:",
            f"  chunks: {int(res['chunks'])} at dim {int(res['dim'])}; matrix "
            f"{res['matrix_bytes'] / 1024:.1f} KiB ({res['bytes_per_chunk']:.0f} bytes per "
            f"chunk); load peak {res['load_peak_bytes'] / 1024:.1f} KiB",
            f"  fill: {res['fill_cpu_ms_per_chunk']:.1f} ms CPU per chunk; query: "
            f"{res['query_wall_ms_median']:.1f} ms wall, {res['query_cpu_ms_median']:.1f} ms CPU "
            "(median)",
        ]
    return lines
