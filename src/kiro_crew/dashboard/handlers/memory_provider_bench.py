"""``/api/memory/provider/bench``: compare memory providers on public suites and on a copy of one's own memory.

| Route | Who may call |
|---|---|
| ``GET /bench/suites`` | owner, ``memoryAdmin`` app |
| ``POST /bench/suites/{id}/fetch`` | the owner |
| ``POST /bench/runs`` | the owner; a ``memoryAdmin`` app with ``dry_run: true`` only |
| ``GET /bench/runs``, ``GET /bench/runs/{id}``, ``GET /bench/runs/{id}/report`` | owner, app (its own plans only) |
| ``POST /bench/runs/{id}/resume``, ``DELETE /bench/runs/{id}`` | the owner |
| ``GET /bench/compare?runs=a,b`` | owner, app (its own runs only) |
| ``POST /bench/evalsets``, ``DELETE /bench/evalsets/{id}`` | the owner |
| ``GET /bench/evalsets``, ``GET /bench/evalsets/{id}`` | the owner; an app sees counts only |

No MCP tool and no CLI verb starts a run: an agent cannot cause the egress a run can
cause. Every call passes :func:`memory_provider._caller` (the owner, or an app whose
``memoryAdmin`` grant the owner approved) and leaves one audit record. A run writes
only to ``bench/<run>/<unit>`` stores of each provider and to temporary Crew stores,
never to a store a person uses. Own memory goes only to a provider that says
``egress: local`` and can ``reset``, unless the request acknowledges that provider by
id, app version and the exact item count the plan showed, for that run alone.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers import memory_provider as base

logger = logging.getLogger("kiro_crew.dashboard.handlers.memory_provider_bench")

_tasks: set[asyncio.Task[Any]] = set()
_APPS_PLAN_ONLY = "An app may plan a benchmark (a dry run); only the owner runs one."


def _start(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _notify(app: web.Application) -> Any:
    def push(_run: Any) -> None:
        try:
            app["state"].push_refresh("memory_provider_bench")
        except Exception:  # noqa: BLE001 - a missed refresh costs a stale view only
            pass

    return push


def _own(run: Any, caller: str) -> bool:
    """Whether *caller* may read *run*.

    The owner reads every run. An app (it reached this route holding the approved
    ``memoryAdmin`` grant) reads the runs it planned and every run the owner started,
    since a run an app asked for is started from the owner's own sheet; a report keeps
    ids and scores, never memory text. Another app's plans stay its own.
    """
    return caller == "owner" or run.caller in (caller, "owner")


async def _body(request: web.Request) -> dict[str, Any] | web.Response:
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    body, refusal = await read_bounded_json(request)
    if refusal is not None:
        return refusal
    assert body is not None
    return body


# ── suites ───────────────────────────────────────────────────────────────────


def _suite_rows() -> list[dict[str, Any]]:
    from kiro_crew.eval.bench.datasets import SPECS, SUITE_SPECS, cache_dir
    from kiro_crew.eval.bench.suites import registry

    keys = {
        "locomo": ["locomo10"],
        "longmemeval": ["longmemeval_s"],
        "precisionmem": ["precisionmem_seed", "precisionmem_cases"],
        "personamem": ["personamem_v2_text"],
        "mab": ["mab_conflict_resolution", "mab_accurate_retrieval"],
        "halumem": ["halumem_medium"],
    }
    specs = {**SPECS, **SUITE_SPECS}
    rows = []
    for suite in registry().values():
        files = [specs[k] for k in keys.get(suite.id, []) if k in specs]
        fetched = all((cache_dir() / f.filename).exists() for f in files) if files else None
        rows.append({**suite.to_dict(), "fetched": fetched, "files": [f.key for f in files]})
    return rows


async def api_bench_suites(request: web.Request) -> web.Response:
    """GET /api/memory/provider/bench/suites."""
    await base._caller(request, "bench.suites")
    return base._respond({"suites": await asyncio.to_thread(_suite_rows)})


async def api_bench_suite_fetch(request: web.Request) -> web.Response:
    """POST /api/memory/provider/bench/suites/{id}/fetch: download and pin a suite's data."""
    caller = await base._caller(request, "bench.fetch", owner_only=True)
    from kiro_crew.eval.bench.errors import BenchRefusal
    from kiro_crew.eval.bench.suites import get

    suite_id = request.match_info["id"]
    try:
        suite = get(suite_id)
    except KeyError:
        return base._json_error("no such suite", "not_found", 404)
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    variant = str(body.get("variant") or suite.variants[0])
    if variant not in suite.variants or suite.loader is None:
        return base._json_error("no such variant", "invalid_suite")
    base._audit(caller, "bench.fetch", "allowed", resources=f"{suite_id}:{variant}")
    try:
        corpus = await asyncio.to_thread(suite.loader, variant, limit=None, allow_download=True)
    except (BenchRefusal, ValueError, OSError) as exc:
        return base._json_error(str(exc)[:500], "fetch_failed", 502)
    return base._respond(
        {
            "suite": suite_id,
            "variant": variant,
            "units": len(corpus.units),
            "queries": corpus.query_count,
        }
    )


# ── runs ─────────────────────────────────────────────────────────────────────


def _load_corpus(options: Any) -> tuple[Any, Any]:
    """``(suite, corpus)`` for a run's options; raises ``ValueError`` with the reason."""
    from kiro_crew.eval.bench import own_memory
    from kiro_crew.eval.bench.suites import get

    if options.suite.startswith("own:"):
        found = own_memory.read(options.suite[4:])
        if found is None:
            raise ValueError("no such eval set")
        return own_memory.OWN_SUITE, found[1]
    try:
        suite = get(options.suite)
    except KeyError as exc:
        raise ValueError(str(exc)) from None
    if suite.loader is None:
        raise ValueError("this suite has no loader")
    variant = options.variant or suite.variants[0]
    if variant.partition("@")[0] not in suite.variants:
        raise ValueError("no such variant")
    corpus = suite.loader(variant, limit=options.limit_instances, allow_download=False)
    return suite, corpus


def _targets(
    run_id: str, options: Any, caller: str, suite_id: str, *, dry_run: bool = False
) -> tuple[dict[str, Any], str]:
    """Every provider's target, or ``({}, refusal)``. Never a live store.

    A plan writes nothing, so its scratch target gets a directory that is never made;
    a resume builds its targets again.
    """
    from kiro_crew.eval.bench.ingest import prepare_embedder
    from kiro_crew.eval.bench.target import BASELINES
    from kiro_crew.eval.bench.target_builtin import ScratchBuiltinTarget
    from kiro_crew.eval.bench.target_provider import ProviderTarget
    from kiro_crew.memory_provider import builtin, resolver
    from kiro_crew.memory_provider.app_client import AppMemoryProvider

    targets: dict[str, Any] = {}
    for pid in options.providers:
        if pid in BASELINES:
            targets[pid] = BASELINES[pid]()
        elif pid in ("builtin", resolver.BUILTIN_PROVIDER_ID):
            if dry_run:
                scratch = Path(tempfile.gettempdir()) / f"kirocrew-bench-{run_id}-plan"
            else:
                scratch = Path(tempfile.mkdtemp(prefix=f"kirocrew-bench-{run_id}-"))
            targets[pid] = ScratchBuiltinTarget(scratch, embed_fn=prepare_embedder(timeout_s=60.0))
        else:
            if not base.app_may_name(caller, "", pid):
                return {}, f"{pid}: an app may name only its own provider"
            provider = base._provider_by_id(pid)
            if provider is None or provider is builtin.BUILTIN:
                return {}, f"{pid}: no such provider"
            refusal = resolver.availability_refusal(provider, None)
            # Live-memory consent is not asked: a public suite sends no Crew memory,
            # and a copy of the owner's own memory needs this run's own acknowledgement
            # (``_egress_refusal``) at plan and at resume.
            if refusal and refusal != resolver.CONSENT_REFUSAL:
                return {}, f"{pid}: {refusal}"
            target = ProviderTarget(provider, run_id, suite=suite_id)
            if not target.isolated:
                return {}, (
                    f"{pid} keeps one memory and no separate benchmark stores, so a run "
                    "would write to and clean up its live memory"
                )
            if isinstance(provider, AppMemoryProvider):
                # Not the live client: live chats' usage never lands in the run's totals.
                target.provider = provider.run_copy()
            targets[pid] = target
    return targets, ""


def _provider_changed(saved: dict[str, Any], current: dict[str, Any]) -> str:
    """The first provider whose version or capabilities differ from the run's plan."""
    before = {row["id"]: row for row in saved.get("providers") or []}
    for row in current.get("providers") or []:
        old = before.get(row["id"])
        if old is None:
            continue
        for key in ("version", "app_version", "caps_sha256"):
            if old.get(key, row.get(key)) != row.get(key):
                return str(row["id"])
    return ""


def _egress_refusal(options: Any, suite: Any, planned: dict[str, Any]) -> str:
    """Own memory leaves only to a local provider that can reset, unless acknowledged now."""
    if suite.id != "own":
        return ""
    for row in planned.get("providers") or []:
        pid = row["id"]
        if row.get("egress") == "local" and row.get("cleanup") == "reset":
            continue
        if row.get("cleanup") != "reset":
            return f"{pid} cannot reset a benchmark store, so a copy of your memory could not be cleaned up"
        ack = options.egress_ack.get(pid) or {}
        if ack.get("app_version") != row.get("app_version") or ack.get("items") != planned.get(
            "items"
        ):
            return (
                f"{pid} keeps memory off this machine. Acknowledge it with its app version "
                f"and the exact item count ({planned.get('items')}) to send a copy of your memory."
            )
    return ""


def _models(options: Any) -> dict[str, str] | str:
    from kiro_crew.config.loader import KiroCrewConfig

    agent = KiroCrewConfig.load().agent
    role = agent.resolve_model("background")
    allowed = {"auto", role, *agent.role_models.values()}
    out = {}
    for name in ("reader", "judge"):
        wanted = getattr(options, f"{name}_model")
        if wanted not in allowed:
            return f"{name}_model must be auto or a model a role already uses"
        out[name] = role if wanted == "auto" else wanted
    return out


def _llm(request: web.Request, model: str) -> Any:
    from kiro_crew.decisions.impl_llm import _call_on_own_session

    sessions = request.app["state"].sessions

    async def call(prompt: str) -> str:
        return await _call_on_own_session(sessions, prompt, model)

    return call


async def api_bench_runs(request: web.Request) -> web.Response:
    """GET (list) / POST (plan or start) /api/memory/provider/bench/runs."""
    from kiro_crew.eval.bench import provider_run

    if request.method == "GET":
        caller = await base._caller(request, "bench.runs.list")
        runs = await asyncio.to_thread(provider_run.list_runs)
        state = request.query.get("state")
        rows = [r.to_record() for r in runs if _own(r, caller) and (not state or r.state == state)]
        return base._respond({"runs": rows})
    caller = await base._caller(request, "bench.runs.create")
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    dry_run = body.get("dry_run") is not False
    if caller != "owner" and not dry_run:
        base._audit(caller, "bench.runs.run", "denied", error="owner_only")
        return base._json_error(_APPS_PLAN_ONLY, "owner_only", 403)
    try:
        options = provider_run.RunOptions.from_request(body)
        suite, corpus = await asyncio.to_thread(_load_corpus, options)
    except ValueError as exc:
        return base._json_error(str(exc), "invalid_run")
    except Exception as exc:  # noqa: BLE001 - an unfetched suite is a refusal, not a crash
        return base._json_error(str(exc)[:500], "suite_not_fetched", 409)
    if suite.id == "own" and caller != "owner":
        return base._json_error("Only the owner benchmarks their own memory.", "owner_only", 403)
    run_id = provider_run.new_run_id()
    try:
        targets, refusal = await asyncio.to_thread(
            _targets, run_id, options, caller, suite.id, dry_run=dry_run
        )
    except Exception as exc:  # noqa: BLE001 - an embedder that is not ready is a refusal
        return base._json_error(str(exc)[:500], "target_unavailable", 503)
    if refusal:
        return base._json_error(refusal, "provider_refused", 403)
    run = provider_run.BenchRun(id=run_id, options=options, caller=caller)
    run.plan = provider_run.plan(run, corpus, suite, targets)
    egress = _egress_refusal(options, suite, run.plan)
    if egress and not dry_run:
        base._audit(caller, "bench.runs.run", "denied", error="egress_unacknowledged")
        return base._json_error(egress, "egress_unacknowledged", 403, {"plan": run.plan})
    if egress:
        run.plan["egress_refusal"] = egress
    if dry_run:
        await asyncio.to_thread(provider_run.save, run)
        await asyncio.to_thread(provider_run.prune)
        base._audit(caller, "bench.runs.plan", "allowed", resources=run.id)
        return base._respond({"run_id": run.id, "state": run.state, "plan": run.plan})
    models = await asyncio.to_thread(_models, options)
    if isinstance(models, str):
        return base._json_error(models, "invalid_run")
    run.resolved_models = models
    reader = judge = None
    if options.mode in ("answer", "both"):
        reader, judge = _llm(request, models["reader"]), _llm(request, models["judge"])
    run.state = "running"
    await asyncio.to_thread(provider_run.save, run)
    base._audit(
        caller, "bench.runs.run", "allowed", resources=f"{run.id}:{','.join(options.providers)}"
    )
    _start(_execute(request.app, run, corpus, suite, targets, reader, judge))
    return base._respond({"run_id": run.id, "state": run.state, "plan": run.plan})


async def _execute(
    app: web.Application,
    run: Any,
    corpus: Any,
    suite: Any,
    targets: dict[str, Any],
    reader: Any,
    judge: Any,
) -> None:
    from kiro_crew.eval.bench import provider_run, report

    try:
        done = await provider_run.run_bench(
            run, corpus, suite, targets, reader=reader, judge=judge, notify=_notify(app)
        )
    finally:
        provider_run.unreserve(run.id)
    results = await asyncio.to_thread(provider_run.read_results, run.id)
    # An own-memory run's variant is its eval set, so runs over two different
    # copies of the owner's memory never compare as equal.
    own = run.options.suite.startswith("own:")
    variant = run.options.suite.removeprefix("own:") if own else corpus.variant
    built = await asyncio.to_thread(
        report.build, done, results, suite={**suite.to_dict(), "variant": variant}
    )
    await asyncio.to_thread(provider_run.save_report, run.id, built)
    _notify(app)(done)


def _run_or_404(run_id: str, caller: str) -> Any:
    from kiro_crew.eval.bench import provider_run

    run = provider_run.get(run_id)
    if run is None or not _own(run, caller):
        return base._json_error("no such run", "not_found", 404)
    return run


async def api_bench_run(request: web.Request) -> web.Response:
    """GET (status) / DELETE (cancel and clean up) /api/memory/provider/bench/runs/{id}."""
    from kiro_crew.eval.bench import provider_run

    owner_only = request.method != "GET"
    caller = await base._caller(
        request, "bench.runs.get" if not owner_only else "bench.runs.cancel", owner_only=owner_only
    )
    found = await asyncio.to_thread(_run_or_404, request.match_info["id"], caller)
    if isinstance(found, web.Response):
        return found
    if request.method == "GET":
        ledger = await asyncio.to_thread(provider_run.read_ledger, found.id)
        return base._respond({**found.to_record(), "scratch_stores": ledger})
    found.cancelled = True
    if found.state in provider_run.ACTIVE_STATES or provider_run.cancel_reserved(found.id):
        # The runner stops at its next step and resets the store it is on; the
        # ledger cleanup runs when it has stopped, never under a unit in flight.
        base._audit(caller, "bench.runs.cancel", "allowed", resources=found.id)
        return base._respond({**found.to_record(), "reset_failed": []})
    found.state = "cancelled"
    await asyncio.to_thread(provider_run.save, found)
    failed = await _clean_up(found)
    base._audit(caller, "bench.runs.cancel", "allowed", resources=found.id)
    return base._respond({**found.to_record(), "reset_failed": failed})


def _cleanup_target(pid: str, run: Any) -> Any:
    """A provider target for a cleanup, or ``None`` when the provider is gone. Blocking."""
    from kiro_crew.eval.bench.target_provider import ProviderTarget

    provider = base._provider_by_id(pid)
    if provider is None:
        return None
    return ProviderTarget(provider, run.id, suite=run.options.suite)


async def _clean_up(run: Any) -> list[str]:
    """Reset every scratch store the ledger shows not yet confirmed empty."""
    from kiro_crew.eval.bench import provider_run
    from kiro_crew.eval.bench.item_corpus import ItemUnit
    from kiro_crew.memory_provider.contract import ProviderError

    failed = []
    for pid, store in await asyncio.to_thread(provider_run.stores_to_reset, run.id):
        if not pid.startswith("app:"):
            continue
        unit_id = store.rsplit("/", 1)[-1]
        try:
            # Built off the loop: a target asks the provider's capabilities, which can
            # be a fresh ``connect`` over the wire.
            target = await asyncio.to_thread(_cleanup_target, pid, run)
            empty = target is not None and await target.reset(ItemUnit(unit_id, (), ()))
        except (ProviderError, ValueError):
            empty = False
        await asyncio.to_thread(
            provider_run._append,
            run.id,
            ".ledger.jsonl",
            [{"provider": pid, "store": store, "op": "reset", "confirmed_empty": empty}],
        )
        if not empty:
            failed.append(store)
    return failed


async def api_bench_run_resume(request: web.Request) -> web.Response:
    """POST /api/memory/provider/bench/runs/{id}/resume: continue at the next unit."""
    from kiro_crew.eval.bench import provider_run

    caller = await base._caller(request, "bench.runs.resume", owner_only=True)
    found = await asyncio.to_thread(_run_or_404, request.match_info["id"], caller)
    if isinstance(found, web.Response):
        return found
    # One runner per run: reserved before any await, freed when the runner ends or when
    # this request refuses, so two resumes never share stores or split the call budget.
    if not provider_run.reserve(found.id):
        return base._json_error("This run is already running or starting.", "run_busy", 409)
    started: list[bool] = []
    try:
        return await _resume(request, caller, found, started)
    finally:
        if not started:
            provider_run.unreserve(found.id)


async def _resume(
    request: web.Request, caller: str, found: Any, started: list[bool]
) -> web.Response:
    # A ``budget_exhausted`` run is final: its options carry the budget it spent, so a
    # resume could not make one more reader call.
    if found.state not in ("paused", "failed"):
        return base._json_error("Only a paused or failed run can resume.", "not_resumable", 409)
    try:
        suite, corpus = await asyncio.to_thread(_load_corpus, found.options)
        targets, refusal = await asyncio.to_thread(
            _targets, found.id, found.options, caller, suite.id
        )
    except Exception as exc:  # noqa: BLE001 - a missing suite or embedder is a refusal
        return base._json_error(str(exc)[:500], "not_resumable", 409)
    if refusal:
        return base._json_error(refusal, "provider_refused", 403)
    # Judged on what each provider answers now, never on the saved plan: a provider
    # that was local when planned may have become remote since.
    from kiro_crew.eval.bench import provider_run

    current = provider_run.plan(found, corpus, suite, targets)
    changed = _provider_changed(found.plan, current)
    if changed:
        return base._json_error(
            f"{changed} changed since this run started, so its results would mix two "
            "configurations. Start a new run.",
            "provider_changed",
            409,
        )
    if _egress_refusal(found.options, suite, current):
        return base._json_error(
            "The acknowledgement no longer matches the plan.", "egress_unacknowledged", 403
        )
    reader = judge = None
    if found.options.mode in ("answer", "both"):
        reader = _llm(request, found.resolved_models.get("reader", "auto"))
        judge = _llm(request, found.resolved_models.get("judge", "auto"))
    base._audit(caller, "bench.runs.resume", "allowed", resources=found.id)
    _start(_execute(request.app, found, corpus, suite, targets, reader, judge))
    started.append(True)
    return base._respond(found.to_record())


async def api_bench_run_report(request: web.Request) -> web.StreamResponse:
    """GET /api/memory/provider/bench/runs/{id}/report?format=json|md."""
    from kiro_crew.eval.bench import provider_run, report

    caller = await base._caller(request, "bench.report")
    found = await asyncio.to_thread(_run_or_404, request.match_info["id"], caller)
    if isinstance(found, web.Response):
        return found
    built = await asyncio.to_thread(provider_run.read_report, found.id)
    if built is None:
        return base._json_error("This run has no report yet.", "no_report", 404)
    if request.query.get("format") == "md":
        from kiro_crew.dashboard.handlers._shared import _redact_memory_field

        return web.Response(
            text=_redact_memory_field(report.markdown(built)), content_type="text/markdown"
        )
    return base._respond(built)


async def api_bench_compare(request: web.Request) -> web.Response:
    """GET /api/memory/provider/bench/compare?runs=a,b[,c]: refuses incomparable runs."""
    from kiro_crew.eval.bench import provider_run, report

    caller = await base._caller(request, "bench.compare")
    ids = [i for i in request.query.get("runs", "").split(",") if i][:5]
    if len(ids) < 2:
        return base._json_error("runs must name two to five runs", "invalid_compare")
    reports = []
    for run_id in ids:
        found = await asyncio.to_thread(_run_or_404, run_id, caller)
        if isinstance(found, web.Response):
            return found
        built = await asyncio.to_thread(provider_run.read_report, run_id)
        if built is None:
            return base._json_error(f"{run_id} has no report yet", "no_report", 404)
        reports.append(built)
    return base._respond(report.compare(reports))


# ── own-memory eval sets ─────────────────────────────────────────────────────


def _store_path(store: str) -> Path | None:
    from kiro_crew.memory_stores import owned_store_path

    return owned_store_path(base._store_name(store) or "default")


async def api_bench_evalsets(request: web.Request) -> web.Response:
    """GET (list) / POST (plan or build) /api/memory/provider/bench/evalsets."""
    from kiro_crew.eval.bench import own_memory

    if request.method == "GET":
        caller = await base._caller(request, "bench.evalsets.list")
        rows = await asyncio.to_thread(own_memory.list_evalsets)
        return base._respond(
            {"evalsets": rows if caller == "owner" else [_counts(r) for r in rows]}
        )
    caller = await base._caller(request, "bench.evalsets.create", owner_only=True)
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    try:
        options = own_memory.EvalSetOptions.from_request(body)
        for store in options.stores:
            base._store_name(store)
    except ValueError as exc:
        return base._json_error(str(exc), "invalid_evalset")
    dry_run = body.get("dry_run") is not False
    evalset_id = own_memory.new_evalset_id()
    paraphrase = None
    if options.llm_probes:
        from kiro_crew.config.loader import KiroCrewConfig

        model = KiroCrewConfig.load().agent.resolve_model("background")
        paraphrase = _llm(request, model)
    try:
        plan = await own_memory.build(
            evalset_id, options, resolve_path=_store_path, dry_run=dry_run, paraphrase=paraphrase
        )
    except ValueError as exc:
        return base._json_error(str(exc), "invalid_evalset")
    base._audit(
        caller,
        "bench.evalsets.plan" if dry_run else "bench.evalsets.build",
        "allowed",
        resources=evalset_id,
    )
    return base._respond(
        {
            "evalset_id": None if dry_run else evalset_id,
            "state": "planned" if dry_run else "built",
            "plan": plan.to_dict(),
        }
    )


def _counts(row: dict[str, Any]) -> dict[str, Any]:
    plan = row.get("plan") or {}
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "items_by_kind": plan.get("items_by_kind"),
        "probes_by_kind": plan.get("probes_by_kind"),
        "dropped": plan.get("dropped"),
    }


async def api_bench_evalset(request: web.Request) -> web.Response:
    """GET (counts; the owner also gets probes) / DELETE /api/memory/provider/bench/evalsets/{id}."""
    from kiro_crew.eval.bench import own_memory

    owner_only = request.method != "GET"
    caller = await base._caller(
        request,
        "bench.evalsets.get" if not owner_only else "bench.evalsets.delete",
        owner_only=owner_only,
    )
    evalset_id = request.match_info["id"]
    if request.method == "DELETE":
        gone = await asyncio.to_thread(own_memory.delete, evalset_id)
        base._audit(caller, "bench.evalsets.delete", "allowed", resources=evalset_id)
        return base._respond({"deleted": gone})
    found = await asyncio.to_thread(own_memory.read, evalset_id)
    if found is None:
        return base._json_error("no such eval set", "not_found", 404)
    header, corpus = found
    row = {k: header.get(k) for k in ("id", "created_at", "stores", "probes", "plan")}
    if caller != "owner":
        return base._respond(_counts(row))
    unit = corpus.units[0]
    probes = [
        {"id": q.query_id, "kind": q.category, "text": q.text, "gold": list(q.gold_ids)}
        for q in unit.queries[:500]
    ]
    return base._respond({**row, "probes_sample": probes})


__all__ = [
    "api_bench_compare",
    "api_bench_evalset",
    "api_bench_evalsets",
    "api_bench_run",
    "api_bench_run_report",
    "api_bench_run_resume",
    "api_bench_runs",
    "api_bench_suite_fetch",
    "api_bench_suites",
]
