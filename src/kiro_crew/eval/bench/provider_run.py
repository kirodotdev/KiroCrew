"""Provider benchmark runs: plan, run unit by unit, checkpoint, clean up, report.

Phases per provider and unit::

    prepare (empty bench store) -> write or replay (in time order) -> settle
      -> query (largest k, as_of) -> [answer: reader -> judge]
      -> [forget -> settle -> probe again] -> [read back] -> reset -> checkpoint

One unit, one store. After every unit the store is reset and the ledger records
whether it read back empty; a failed reset is a warning on the run, kept in the
ledger for a cancel to retry. A restart resumes at the next unit: a half-done unit
is reset and done again. Per-query results hold ids and scores only, never memory
or query text, so an own-memory run leaves no text in its results or report.

State lives in :func:`~kiro_crew.config.loader_locations.memory_provider_bench_dir`
(a READONLY crew-home leaf). The newest :data:`RUNS_KEPT` plans and, apart, the
newest :data:`RUNS_KEPT` real runs are kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import scorers, stats
from .answer import Budget, BudgetExhausted, Llm, answer_query
from .item_corpus import ItemCorpus, ItemQuery, ItemUnit, ranked_ids
from .suites import SuiteInfo
from .suites import forget as forget_suite
from .target import BenchTarget, NotSettled

logger = logging.getLogger("kiro_crew.eval.bench.provider_run")

RUNS_KEPT = 20
MAX_K = 50
MAX_BUDGET_CHARS = 64_000
MAX_LLM_CALLS = 100_000
STATES = frozenset(
    ("planned", "running", "paused", "done", "failed", "cancelled", "budget_exhausted")
)
ACTIVE_STATES = frozenset(("running",))
_RUN_ID = re.compile(r"^r_[0-9a-f]{12}$")
_io_lock = threading.Lock()
Notify = Callable[["BenchRun"], None]

#: A category of any suite -> the report dimension it feeds.
CATEGORY_DIMS = {
    "single_hop": "M1",
    "information_extraction": "M1",
    "multi_hop": "M2",
    "multi_session_reasoning": "M2",
    "temporal": "M3",
    "temporal_reasoning": "M3",
    "event_ordering": "M3",
    "knowledge_update": "M4",
    "contradiction_resolution": "M4",
    "adversarial": "M5",
    "abstention": "M5",
    "preference": "M6",
    "preference_following": "M6",
    "instruction_following": "M6",
    # Own-memory probe kinds.
    "P1": "M1",
    "P2": "M1",
    "P3": "M4",
    "P4": "M3",
    "P5": "M6",
    "P6": "M5",
}
DIM_NAMES = {
    "M1": "Single-fact recall",
    "M2": "Multi-hop and multi-session",
    "M3": "Temporal reasoning",
    "M4": "Knowledge update",
    "M5": "Abstention",
    "M6": "Preference and instruction following",
    "M7": "Precision",
    "M8": "Forgetting",
    "M9": "Write fidelity",
    "M10": "Isolation",
    "M11": "Latency",
    "M12": "Cost and footprint",
}


class RunCancelled(Exception):
    """The owner cancelled: stop at the next step; the unit in hand is still reset."""


def _stop_if_cancelled(run: BenchRun) -> None:
    if run.cancelled:
        raise RunCancelled()


def _dir() -> Path:
    from kiro_crew.config.loader_locations import memory_provider_bench_dir

    return memory_provider_bench_dir()


def new_run_id() -> str:
    return "r_" + secrets.token_hex(6)


@dataclass
class RunOptions:
    suite: str
    variant: str
    providers: list[str]
    mode: str = "retrieval"
    ingest: str = "auto"
    k: list[int] = field(default_factory=lambda: [1, 5, 10, 20])
    budget_chars: int = 16_000
    limit_instances: int | None = 50
    seed: int = 7
    reader_model: str = "auto"
    judge_model: str = "auto"
    max_llm_calls: int = 2_000
    egress_ack: dict[str, dict[str, Any]] = field(default_factory=dict)
    oracle: bool = False

    @classmethod
    def from_request(cls, body: dict[str, Any]) -> RunOptions:
        """Validated options from a run request; raises ``ValueError`` naming the field."""
        suite = body.get("suite")
        if not isinstance(suite, str) or not suite or len(suite) > 80:
            raise ValueError("suite must name a suite")
        providers = body.get("providers")
        if (
            not isinstance(providers, list)
            or not 1 <= len(providers) <= 8
            or any(not isinstance(p, str) or not p or len(p) > 200 for p in providers)
            or len(set(providers)) != len(providers)
        ):
            raise ValueError("providers must list 1 to 8 distinct provider ids")
        mode = body.get("mode", "retrieval")
        if mode not in ("retrieval", "answer", "both"):
            raise ValueError("mode must be retrieval, answer or both")
        ingest = body.get("ingest", "auto")
        if ingest not in ("auto", "items", "replay"):
            raise ValueError("ingest must be auto, items or replay")
        k = body.get("k", [1, 5, 10, 20])
        if (
            not isinstance(k, list)
            or not k
            or any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_K for v in k)
        ):
            raise ValueError(f"k must list cut-offs from 1 to {MAX_K}")
        budget = body.get("budget_chars", 16_000)
        if (
            isinstance(budget, bool)
            or not isinstance(budget, int)
            or not 500 <= budget <= MAX_BUDGET_CHARS
        ):
            raise ValueError(f"budget_chars must be 500 to {MAX_BUDGET_CHARS}")
        limit = body.get("limit_instances", 50)
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("limit_instances must be a positive integer or null")
        calls = body.get("max_llm_calls", 2_000)
        if isinstance(calls, bool) or not isinstance(calls, int) or not 0 <= calls <= MAX_LLM_CALLS:
            raise ValueError(f"max_llm_calls must be 0 to {MAX_LLM_CALLS}")
        seed = body.get("seed", 7)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        for name in ("reader_model", "judge_model"):
            value = body.get(name, "auto")
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError(f"{name} must be a model id or auto")
        ack = body.get("egress_ack") or {}
        if not isinstance(ack, dict) or any(not isinstance(v, dict) for v in ack.values()):
            raise ValueError("egress_ack must map provider ids to acknowledgements")
        variant = body.get("variant") or ""
        if not isinstance(variant, str) or len(variant) > 80:
            raise ValueError("variant must be a short name")
        return cls(
            suite=suite,
            variant=variant,
            providers=list(providers),
            mode=mode,
            ingest=ingest,
            k=sorted(set(k)),
            budget_chars=budget,
            limit_instances=limit,
            seed=seed,
            reader_model=body.get("reader_model", "auto"),
            judge_model=body.get("judge_model", "auto"),
            max_llm_calls=calls,
            egress_ack={str(p): dict(v) for p, v in ack.items()},
            oracle=body.get("oracle") is True,
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class BenchRun:
    id: str
    options: RunOptions
    caller: str
    state: str = "planned"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    plan: dict[str, Any] = field(default_factory=dict)
    #: ``{"provider": index, "unit": index}`` of the next unit to run.
    checkpoint: dict[str, int] = field(default_factory=lambda: {"provider": 0, "unit": 0})
    progress: dict[str, dict[str, int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    llm_calls: int = 0
    error: str = ""
    cancelled: bool = False
    resolved_models: dict[str, str] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "options": self.options.to_dict(),
            "caller": self.caller,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "plan": self.plan,
            "checkpoint": dict(self.checkpoint),
            "progress": {k: dict(v) for k, v in self.progress.items()},
            "warnings": list(self.warnings),
            "llm_calls": self.llm_calls,
            "error": self.error,
            "resolved_models": dict(self.resolved_models),
        }

    @classmethod
    def from_record(cls, raw: object) -> BenchRun | None:
        if not isinstance(raw, dict) or not _RUN_ID.match(str(raw.get("id") or "")):
            return None
        try:
            options = RunOptions.from_request(dict(raw.get("options") or {}))
            run = cls(
                id=str(raw["id"]),
                options=options,
                caller=str(raw.get("caller") or ""),
                state=str(raw.get("state") or "failed"),
                created_at=float(raw.get("created_at") or 0.0),
                updated_at=float(raw.get("updated_at") or 0.0),
                finished_at=raw.get("finished_at"),
                plan=dict(raw.get("plan") or {}),
                checkpoint={k: int(v) for k, v in (raw.get("checkpoint") or {}).items()},
                progress={k: dict(v) for k, v in (raw.get("progress") or {}).items()},
                warnings=[str(w) for w in raw.get("warnings") or []],
                llm_calls=int(raw.get("llm_calls") or 0),
                error=str(raw.get("error") or ""),
                resolved_models={
                    str(k): str(v) for k, v in (raw.get("resolved_models") or {}).items()
                },
            )
        except (TypeError, ValueError):
            return None
        return run if run.state in STATES else None


# ── persistence ──────────────────────────────────────────────────────────────


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(run: BenchRun) -> None:
    run.updated_at = time.time()
    with _io_lock:
        _atomic_write(_dir() / f"{run.id}.json", json.dumps(run.to_record()))


def _append(run_id: str, suffix: str, lines: Iterable[dict[str, Any]]) -> None:
    body = "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)
    if not body:
        return
    with _io_lock:
        path = _dir() / f"{run_id}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(body)


def _read_lines(run_id: str, suffix: str) -> list[dict[str, Any]]:
    """A run's JSONL file. Only a missing file is empty: a damaged or unreadable one
    raises, so cleanup never reads "nothing created" from it."""
    if not _RUN_ID.match(run_id):
        return []
    try:
        raw = (_dir() / f"{run_id}{suffix}").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError(f"benchmark file {run_id}{suffix} holds a line that is not an object")
        out.append(entry)
    return out


def read_ledger(run_id: str) -> list[dict[str, Any]]:
    return _read_lines(run_id, ".ledger.jsonl")


def read_results(run_id: str) -> list[dict[str, Any]]:
    """Each result row once. A unit a restart ran again (its rows landed, its checkpoint
    did not) appends the same ``(provider, unit, q, cat)`` rows twice; the last counts."""
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in _read_lines(run_id, ".results.jsonl"):
        key = tuple(str(row.get(f, "")) for f in ("provider", "unit", "q", "cat"))
        rows.pop(key, None)
        rows[key] = row
    return list(rows.values())


def load(run_id: str) -> BenchRun | None:
    if not _RUN_ID.match(run_id):
        return None
    try:
        raw = json.loads((_dir() / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    run = BenchRun.from_record(raw)
    if run is not None and run.state in ACTIVE_STATES and run_id not in _live:
        run.state = "paused"
        run.warnings.append("interrupted by a restart; resume to continue")
    return run


_live: dict[str, BenchRun] = {}
#: Run ids a resume admitted and is still preparing (see :func:`reserve`).
_reserved: set[str] = set()
#: Reserved run ids the owner cancelled before their runner started.
_cancel_requested: set[str] = set()


def reserve(run_id: str) -> bool:
    """Admit one runner for *run_id*; ``False`` when one is running or being prepared.

    Called on the gateway loop with no await between the check and the add."""
    if run_id in _live or run_id in _reserved:
        return False
    _reserved.add(run_id)
    return True


def unreserve(run_id: str) -> None:
    _reserved.discard(run_id)
    _cancel_requested.discard(run_id)


def cancel_reserved(run_id: str) -> bool:
    """Cancel a resume still being prepared; its runner stops at its first step."""
    if run_id in _reserved and run_id not in _live:
        _cancel_requested.add(run_id)
        return True
    return False


def get(run_id: str) -> BenchRun | None:
    return _live.get(run_id) or load(run_id)


def list_runs() -> list[BenchRun]:
    try:
        names = [p.stem for p in _dir().glob("r_*.json") if _RUN_ID.match(p.stem)]
    except OSError:
        return []
    runs = [r for r in (get(n) for n in names) if r is not None]
    return sorted(runs, key=lambda r: r.created_at, reverse=True)


def prune() -> None:
    """Keep the newest :data:`RUNS_KEPT` plans and, apart, the newest real runs.

    A plan (``planned``, which an app may make) never evicts a run, so a paused or
    failed run's ledger, which cleanup, cancel and resume read, stays until newer runs
    push it out.
    """
    runs = list_runs()
    plans = [r for r in runs if r.state == "planned"]
    real = [r for r in runs if r.state != "planned"]
    for run in plans[RUNS_KEPT:] + real[RUNS_KEPT:]:
        if run.state in ACTIVE_STATES:
            continue
        for suffix in (".json", ".ledger.jsonl", ".results.jsonl", ".report.json"):
            try:
                (_dir() / f"{run.id}{suffix}").unlink()
            except OSError:
                pass


def save_report(run_id: str, report: dict[str, Any]) -> None:
    with _io_lock:
        _atomic_write(_dir() / f"{run_id}.report.json", json.dumps(report))


def read_report(run_id: str) -> dict[str, Any] | None:
    if not _RUN_ID.match(run_id):
        return None
    try:
        raw = json.loads((_dir() / f"{run_id}.report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


# ── planning ─────────────────────────────────────────────────────────────────


def ingest_mode(options: RunOptions, target: BenchTarget, suite: SuiteInfo) -> tuple[str, str]:
    """``(mode, why)`` for one target: items where it can, replay where the suite needs it."""
    modes = [m for m in target.info.ingest_modes if m in suite.ingest_modes]
    if options.ingest != "auto":
        if options.ingest not in modes:
            return "", f"{target.info.name} cannot ingest by {options.ingest} for this suite"
        return options.ingest, "asked for"
    if not modes:
        return "", f"{target.info.name} supports no ingest mode this suite uses"
    if "items" in modes and target.info.keeps_origin:
        return "items", "keeps origin ids, so retrieval is scored on gold ids"
    if "replay" in modes:
        return "replay", "extracts for itself; scored in answer mode"
    return modes[0], "the only mode both support"


def plan(
    run: BenchRun, corpus: ItemCorpus, suite: SuiteInfo, targets: dict[str, BenchTarget]
) -> dict[str, Any]:
    """What a run would do, per provider, with nothing written."""
    options = run.options
    answer = options.mode in ("answer", "both")
    queries = corpus.query_count
    # The judge runs on every scorer but multiple choice and substring
    # (``answer.answer_query``), so the plan counts it the same way: an upper bound.
    judged = suite.needs_judge or suite.scorer not in ("mc", "substring")
    calls_per_query = (1 + (1 if judged else 0)) if answer else 0
    rows = []
    for pid in options.providers:
        target = targets[pid]
        mode, why = ingest_mode(options, target, suite)
        na: dict[str, str] = {}
        if not target.info.keeps_origin:
            for dim in suite.dims:
                if dim in ("M1", "M2", "M3", "M4", "M7"):
                    na[dim] = (
                        "provider does not keep origin ids; answer mode only (a proxy is shown)"
                    )
        if not target.info.as_of:
            for dim in ("M3", "M4"):
                if dim in suite.dims:
                    na.setdefault(dim, "measured without as_of")
        writes = sum(len(u.items) for u in corpus.units) if mode == "items" else 0
        turns = (
            sum(len(s.messages) for u in corpus.units for s in u.sessions)
            if mode == "replay"
            else 0
        )
        rows.append(
            {
                **target.info.to_dict(),
                "ingest_mode": mode,
                "ingest_why": why,
                "writes": writes,
                "turns": turns,
                "units": len(corpus.units),
                "na": na,
            }
        )
    return {
        "suite": suite.to_dict(),
        "variant": corpus.variant,
        "units": len(corpus.units),
        "queries": queries,
        "items": sum(len(u.items) for u in corpus.units),
        "llm_calls_estimate": queries * calls_per_query * len(options.providers),
        "max_llm_calls": options.max_llm_calls,
        "providers": rows,
        "notes": list(corpus.notes),
    }


# ── running ──────────────────────────────────────────────────────────────────


def _dims_for(suite: SuiteInfo, query: ItemQuery) -> list[str]:
    if suite.scorer == "assertions":
        return ["M7"]
    if suite.scorer == "isolation":
        return ["M10"]
    if suite.scorer == "extraction" and query.category == "extraction":
        return ["M9"]
    if query.unanswerable:
        return ["M5"]
    if query.category == "P1" and query.extra_value("p8") == "1":
        return ["M1", "M7"]
    dim = CATEGORY_DIMS.get(query.category)
    if dim is None and suite.id == "mab":
        dim = "M2" if query.category == "multi_hop" else "M4"
    if dim is None:
        dim = suite.dims[0] if suite.dims else "M1"
    return [dim]


async def _score_unit(
    run: BenchRun,
    suite: SuiteInfo,
    pid: str,
    target: BenchTarget,
    unit: ItemUnit,
    *,
    reader: Llm | None,
    judge: Llm | None,
    budget: Budget,
) -> list[dict[str, Any]]:
    options = run.options
    k_max = max(options.k)
    proxy = not target.info.keeps_origin
    rows: list[dict[str, Any]] = []
    for query in unit.queries:
        _stop_if_cancelled(run)
        if suite.scorer == "extraction" and query.category == "extraction":
            stored = [h.text for h in await target.read_back(unit)]
            points = json.loads(query.extra_value("points", "[]"))
            scores = scorers.extraction_scores(stored, points)
            rows.append({"q": query.query_id, "dims": ["M9"], "cat": "extraction", **scores})
            continue
        started = time.monotonic()
        hits = await target.query(unit, query.text, k_max, query.as_of)
        ms = (time.monotonic() - started) * 1000.0
        ranked, missed = ranked_ids([(h.origin_id, h.text) for h in hits], unit, proxy=proxy)
        row: dict[str, Any] = {
            "q": query.query_id,
            "cat": query.category,
            "dims": _dims_for(suite, query),
            "ms": round(ms, 1),
            "chars": sum(len(h.text) for h in hits[:k_max]),
            "unattributed": missed,
            "proxy": proxy,
        }
        if suite.scorer == "isolation":
            prefix = query.extra_value("leak_prefix")
            # A hit with no origin still leaks when it carries the other unit's text.
            marker = query.extra_value("leak_marker").lower()
            row["leaks"] = sum(
                1
                for h in hits
                if (h.origin_id or "").startswith(prefix) or (marker and marker in h.text.lower())
            )
        elif suite.scorer == "assertions":
            rules = json.loads(query.extra_value("assertions", "{}"))
            row["pass"] = {str(k): scorers.assertions_pass(ranked, rules, k) for k in options.k}
            row["noise"] = scorers.noise_share(ranked, query.gold_ids, k_max)
        elif query.unanswerable:
            # An unanswerable question scores abstention, even when the suite links
            # evidence to it: returning that evidence is not a correct answer.
            row["abstain_retrieval"] = not hits
        elif query.gold_ids:
            row["r"] = {
                str(k): scorers.retrieval_scores(ranked, query.gold_ids, k) for k in options.k
            }
            if query.forbid_ids:
                row["stale"] = scorers.stale_hit(ranked, query.forbid_ids, k_max)
            row["noise"] = scorers.noise_share(ranked, query.gold_ids, k_max)
        if options.mode in ("answer", "both") and reader is not None and suite.needs_reader:
            answered = await answer_query(
                query,
                hits,
                scorer=suite.scorer,
                reader=reader,
                judge=judge,
                budget=budget,
                budget_chars=options.budget_chars,
            )
            row["correct"] = answered.correct
            row["abstained"] = answered.abstained
        rows.append(row)
    return rows


async def _forget_phase(
    target: BenchTarget, unit: ItemUnit, k: int, seed: int
) -> list[dict[str, Any]]:
    """Forget each target's gold, then probe it directly and by its rare terms."""
    chosen = forget_suite.targets(unit, seed=seed)
    if not chosen:
        return []
    proxy = not target.info.keeps_origin

    async def found(query: ItemQuery, text: str) -> list[str]:
        hits = await target.query(unit, text, k, query.as_of)
        return ranked_ids([(h.origin_id, h.text) for h in hits], unit, proxy=proxy)[0]

    others = [q for q in unit.queries if q.gold_ids and q not in chosen]
    before = {q.query_id: bool(set(await found(q, q.text)) & set(q.gold_ids)) for q in others}
    forgotten = sorted({i for q in chosen for i in q.gold_ids})
    removed = await target.forget(unit, forgotten)
    await target.settle(unit)
    rows = []
    for query in chosen:
        direct = await found(query, query.text)
        terms = await found(query, forget_suite.key_terms(query.text, unit))
        leaked = bool((set(direct) | set(terms)) & set(query.gold_ids))
        rows.append({"q": query.query_id, "dims": ["M8"], "cat": "forget", "leaked": leaked})
    collateral = 0
    checked = 0
    for query in others:
        remaining = set(query.gold_ids) - set(forgotten)
        if not before.get(query.query_id) or not remaining:
            continue
        checked += 1
        if not set(await found(query, query.text)) & remaining:
            collateral += 1
    rows.append(
        {
            "q": f"{unit.unit_id}:forget",
            "dims": ["M8"],
            "cat": "forget_summary",
            "asked": len(forgotten),
            "removed": removed,
            "collateral": collateral,
            "checked": checked,
        }
    )
    return rows


async def _isolation_phase(
    run: BenchRun, target: BenchTarget, corpus: ItemCorpus, suite: SuiteInfo, pid: str
) -> None:
    stores = [f"bench/{run.id}/{u.unit_id}" for u in corpus.units]
    await asyncio.to_thread(
        _append,
        run.id,
        ".ledger.jsonl",
        [{"provider": pid, "store": s, "op": "created"} for s in stores],
    )
    rows: list[dict[str, Any]] = []
    try:
        for unit in corpus.units:
            await target.prepare(unit)
            await target.write(unit, unit.items)
        for unit in corpus.units:
            await target.settle(unit)
        for unit in corpus.units:
            rows += await _score_unit(
                run, suite, pid, target, unit, reader=None, judge=None, budget=Budget(0)
            )
    finally:
        for unit, store in zip(corpus.units, stores):
            empty = await target.reset(unit)
            await asyncio.to_thread(
                _append,
                run.id,
                ".ledger.jsonl",
                [{"provider": pid, "store": store, "op": "reset", "confirmed_empty": empty}],
            )
            if not empty:
                run.warnings.append(f"{pid}: {store} did not read back empty after reset")
    for row in rows:
        row["provider"] = pid
    await asyncio.to_thread(_append, run.id, ".results.jsonl", rows)


async def _settle_and_score(
    run: BenchRun,
    suite: SuiteInfo,
    pid: str,
    target: BenchTarget,
    unit: ItemUnit,
    corpus: ItemCorpus,
    reader: Llm | None,
    judge: Llm | None,
    budget: Budget,
) -> tuple[list[dict[str, Any]], float] | None:
    """The unit's rows and settle seconds, or ``None`` when indexing never finished.

    Scores on a half-built index are not the provider's, so an unsettled unit records
    none and says so in the run's warnings; the caller still resets it.
    """
    try:
        settle_s = await target.settle(unit)
    except NotSettled as exc:
        run.warnings.append(f"{pid}: {unit.unit_id} not scored: {exc}")
        return None
    _stop_if_cancelled(run)
    rows = await _score_unit(
        run, suite, pid, target, unit, reader=reader, judge=judge, budget=budget
    )
    if suite.scorer == "forget" or (suite.scorer == "own" and "P7" in corpus.notes):
        _stop_if_cancelled(run)
        rows += await _forget_phase(target, unit, max(run.options.k), run.options.seed)
    return rows, settle_s


async def run_bench(
    run: BenchRun,
    corpus: ItemCorpus,
    suite: SuiteInfo,
    targets: dict[str, BenchTarget],
    *,
    reader: Llm | None = None,
    judge: Llm | None = None,
    notify: Notify = lambda run: None,
) -> BenchRun:
    """Run (or resume) *run* from its checkpoint, saving after every unit."""
    _live[run.id] = run
    if run.id in _cancel_requested:
        _cancel_requested.discard(run.id)
        run.cancelled = True
    run.state = "running"
    run.error = ""
    budget = Budget(max(0, run.options.max_llm_calls - run.llm_calls))
    try:
        await asyncio.to_thread(save, run)
    except BaseException:
        # Never started: release it, so a resume is not refused as busy.
        _live.pop(run.id, None)
        raise
    notify(run)
    try:
        providers = run.options.providers
        for p_index in range(run.checkpoint.get("provider", 0), len(providers)):
            pid = providers[p_index]
            target = targets[pid]
            mode, why = ingest_mode(run.options, target, suite)
            if not mode:
                run.warnings.append(why)
                run.checkpoint = {"provider": p_index + 1, "unit": 0}
                continue
            start = (
                run.checkpoint.get("unit", 0) if p_index == run.checkpoint.get("provider", 0) else 0
            )
            progress = run.progress.setdefault(pid, {"done": 0, "total": len(corpus.units)})
            if suite.scorer == "isolation":
                # Every store must hold its items at once, or "apart" proves nothing.
                await _isolation_phase(run, target, corpus, suite, pid)
                progress["done"] = len(corpus.units)
                run.checkpoint = {"provider": p_index + 1, "unit": 0}
                await asyncio.to_thread(save, run)
                continue
            for u_index in range(start, len(corpus.units)):
                _stop_if_cancelled(run)
                unit = corpus.units[u_index]
                store = f"bench/{run.id}/{unit.unit_id}"
                await asyncio.to_thread(
                    _append,
                    run.id,
                    ".ledger.jsonl",
                    [{"provider": pid, "store": store, "op": "created"}],
                )
                try:
                    await target.prepare(unit)
                    _stop_if_cancelled(run)
                    started = time.monotonic()
                    if mode == "items":
                        outcome = await target.write(unit, unit.items)
                        count = len(unit.items)
                    else:
                        stored = await target.replay(unit, unit.sessions)
                        outcome = {"inserted": stored}
                        count = sum(len(s.messages) for s in unit.sessions)
                    ingest_ms = (time.monotonic() - started) * 1000.0
                    _stop_if_cancelled(run)
                    scored = await _settle_and_score(
                        run, suite, pid, target, unit, corpus, reader, judge, budget
                    )
                    rows = scored[0] if scored is not None else []
                    if scored is not None:
                        rows.append(
                            {
                                "q": f"{unit.unit_id}:ingest",
                                "dims": ["M11"],
                                "cat": "ingest",
                                "ingest_ms_per": round(ingest_ms / max(1, count), 2),
                                "settle_s": round(scored[1], 2),
                                "stored": sum(
                                    v for k, v in outcome.items() if k in ("inserted", "enriched")
                                ),
                                "count": count,
                            }
                        )
                finally:
                    empty = await target.reset(unit)
                    await asyncio.to_thread(
                        _append,
                        run.id,
                        ".ledger.jsonl",
                        [
                            {
                                "provider": pid,
                                "store": store,
                                "op": "reset",
                                "confirmed_empty": empty,
                            }
                        ],
                    )
                    if not empty:
                        run.warnings.append(f"{pid}: {store} did not read back empty after reset")
                for row in rows:
                    row["provider"] = pid
                    row["unit"] = unit.unit_id
                await asyncio.to_thread(_append, run.id, ".results.jsonl", rows)
                progress["done"] = u_index + 1
                run.llm_calls = run.options.max_llm_calls - (budget.max_calls - budget.used)
                run.checkpoint = {"provider": p_index, "unit": u_index + 1}
                await asyncio.to_thread(save, run)
                notify(run)
            usage = target.usage()
            if usage.get("reported"):
                await asyncio.to_thread(
                    _append,
                    run.id,
                    ".results.jsonl",
                    [{"provider": pid, "q": "usage", "dims": ["M12"], "cat": "usage", **usage}],
                )
            timings = getattr(target, "timings", {})
            if timings:
                await asyncio.to_thread(
                    _append,
                    run.id,
                    ".results.jsonl",
                    [
                        {
                            "provider": pid,
                            "q": "timings",
                            "dims": ["M11"],
                            "cat": "timings",
                            "ops": {op: stats.percentiles(v) for op, v in timings.items()},
                        }
                    ],
                )
            run.checkpoint = {"provider": p_index + 1, "unit": 0}
            await asyncio.to_thread(save, run)
        run.state = "cancelled" if run.cancelled else "done"
    except RunCancelled:
        run.state = "cancelled"
    except BudgetExhausted:
        run.state = "budget_exhausted"
        run.error = f"stopped after {run.options.max_llm_calls} reader and judge calls"
    except asyncio.CancelledError:
        run.state = "paused"
        raise
    except Exception as exc:  # noqa: BLE001 - a run failure is reported, never raised into the loop
        logger.warning("benchmark run %s failed", run.id, exc_info=True)
        run.state = "failed"
        run.error = type(exc).__name__ + (f": {exc}" if str(exc) else "")
    finally:
        run.llm_calls = run.options.max_llm_calls - (budget.max_calls - budget.used)
        if run.state in ("done", "failed", "cancelled", "budget_exhausted"):
            run.finished_at = time.time()
        _live.pop(run.id, None)
        for target in targets.values():
            try:
                await target.close()
            except Exception:  # noqa: BLE001 - closing a scratch target never fails the run
                logger.debug("closing a bench target failed", exc_info=True)
        await asyncio.to_thread(save, run)
        await asyncio.to_thread(prune)
        notify(run)
    return run


def stores_to_reset(run_id: str) -> list[tuple[str, str]]:
    """``(provider, store)`` the ledger shows created and not yet confirmed empty."""
    state: dict[tuple[str, str], bool] = {}
    for entry in read_ledger(run_id):
        key = (str(entry.get("provider")), str(entry.get("store")))
        store = key[1]
        # A ledger line that names anything but this run's own bench stores is ignored.
        if not store.startswith(f"bench/{run_id}/"):
            continue
        if entry.get("op") == "created":
            state[key] = False
        elif entry.get("op") == "reset":
            state[key] = entry.get("confirmed_empty") is True
    return [key for key, empty in state.items() if not empty]


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None
