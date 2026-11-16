"""``kirocrew-bench-report/1``: one run's per-provider dimensions, as JSON and Markdown.

A dimension a provider cannot be measured on is ``{"value": null, "reason": ...}``,
never a zero. Retrieval values come from gold ids; for a provider that keeps no
origin they come from text overlap and carry ``"proxy": true``. Answer-mode values
come from the reader and judge. For an own-memory run the report holds ids, kinds and
scores only.
"""

from __future__ import annotations

import platform
import sys
import time
from collections.abc import Sequence
from typing import Any

from . import stats
from .answer import prompt_hashes
from .provider_run import DIM_NAMES, BenchRun

REPORT_FORMAT = "kirocrew-bench-report/1"


def _cell(values: Sequence[float], metric: str, *, proxy: bool = False) -> dict[str, Any]:
    if not values:
        return {"value": None, "reason": "no scorable queries", "n": 0, "metric": metric}
    cell: dict[str, Any] = {
        "value": round(sum(values) / len(values), 4),
        "ci95": [round(v, 4) for v in stats.bootstrap_ci(values) or ()],
        "n": len(values),
        "metric": metric,
    }
    if proxy:
        cell["proxy"] = True
    return cell


def _retrieval_values(rows: Sequence[dict[str, Any]], dim: str, k: str) -> list[float]:
    field = "recall_all" if dim == "M2" else "recall_any"
    return [float(r["r"][k][field]) for r in rows if "r" in r and k in r["r"]]


def _provider_section(
    run: BenchRun, pid: str, rows: list[dict[str, Any]], info: dict[str, Any]
) -> dict[str, Any]:
    options = run.options
    k = str(min(options.k, key=lambda v: abs(v - 10)))
    by_dim: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for dim in row.get("dims") or []:
            by_dim.setdefault(dim, []).append(row)
    dims: dict[str, Any] = {}
    proxy = not info.get("keeps_origin", True)
    for dim, dim_rows in sorted(by_dim.items()):
        if dim in ("M11", "M12"):
            continue
        answers = [float(bool(r["correct"])) for r in dim_rows if r.get("correct") is not None]
        if dim == "M7":
            passes = [float(r["pass"][k]) for r in dim_rows if "pass" in r]
            if passes:
                dims[dim] = _cell(passes, f"assertion_pass@{k}", proxy=proxy)
            else:
                clean = [1.0 - float(r["noise"]) for r in dim_rows if r.get("noise") is not None]
                dims[dim] = _cell(clean, "gold_share_of_returned", proxy=proxy)
        elif dim == "M8":
            asked = [r for r in dim_rows if r.get("cat") == "forget"]
            dims[dim] = _cell(
                [0.0 if r["leaked"] else 1.0 for r in asked], "not_leaked_rate", proxy=proxy
            )
            summaries = [r for r in dim_rows if r.get("cat") == "forget_summary"]
            checked = sum(int(r.get("checked") or 0) for r in summaries)
            dims[dim]["collateral_rate"] = (
                round(sum(int(r.get("collateral") or 0) for r in summaries) / checked, 4)
                if checked
                else None
            )
            asked_n = sum(int(r.get("asked") or 0) for r in summaries)
            dims[dim]["removed_rate"] = (
                round(sum(int(r.get("removed") or 0) for r in summaries) / asked_n, 4)
                if asked_n
                else None
            )
        elif dim == "M9":
            recalls = [float(r["recall"]) for r in dim_rows if r.get("recall") is not None]
            dims[dim] = _cell(recalls, "extraction_recall")
            precisions = [float(r["precision"]) for r in dim_rows if r.get("precision") is not None]
            dims[dim]["precision"] = (
                round(sum(precisions) / len(precisions), 4) if precisions else None
            )
        elif dim == "M10":
            leaks = sum(int(r.get("leaks") or 0) for r in dim_rows)
            dims[dim] = {
                "value": leaks,
                "n": len(dim_rows),
                "metric": "cross_store_leaks",
                "pass": leaks == 0,
            }
        elif dim == "M5":
            if answers:
                dims[dim] = _cell(answers, "correct_abstain_rate")
            else:
                dims[dim] = _cell(
                    [float(r["abstain_retrieval"]) for r in dim_rows if "abstain_retrieval" in r],
                    "empty_result_rate",
                )
        else:
            retrieval = _retrieval_values(dim_rows, dim, k)
            metric = ("recall_all@" if dim == "M2" else "recall_any@") + k
            cell = (
                _cell(answers, "judge_accuracy")
                if answers
                else _cell(retrieval, metric, proxy=proxy)
            )
            if answers and retrieval:
                cell["retrieval"] = _cell(retrieval, metric, proxy=proxy)
            if dim == "M4":
                stale = [float(r["stale"]) for r in dim_rows if "stale" in r]
                if stale:
                    cell["stale_rate"] = round(sum(stale) / len(stale), 4)
            na = (info.get("na") or {}).get(dim)
            if na:
                cell.setdefault("note", na)
            dims[dim] = cell
    for dim, reason in (info.get("na") or {}).items():
        dims.setdefault(dim, {"value": None, "reason": reason})
    categories: dict[str, Any] = {}
    for row in rows:
        cat = row.get("cat")
        if cat and "r" in row:
            categories.setdefault(cat, []).append(float(row["r"][k]["recall_any"]))
    efficiency = _efficiency(rows)
    return {
        "id": pid,
        "name": info.get("name", pid),
        "version": info.get("version", ""),
        "caps_sha256": info.get("caps_sha256", ""),
        "egress": info.get("egress", "local"),
        "ingest_mode": info.get("ingest_mode", ""),
        "dims": dims,
        "categories": {
            c: {"value": round(sum(v) / len(v), 4), "n": len(v)} for c, v in categories.items()
        },
        "efficiency": efficiency,
        "cleanup": {"method": info.get("cleanup", "reset")},
        "warnings": [w for w in run.warnings if w.startswith(f"{pid}:")],
    }


def _efficiency(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ingest = [r for r in rows if r.get("cat") == "ingest"]
    queries = [r for r in rows if "ms" in r]
    usage = next((r for r in rows if r.get("cat") == "usage"), None)
    timings = next((r for r in rows if r.get("cat") == "timings"), None)
    turns = sum(int(r.get("count") or 0) for r in ingest)
    stored = sum(int(r.get("stored") or 0) for r in ingest)
    chars = [int(r.get("chars") or 0) for r in queries]
    return {
        "ingest_ms_per_item": stats.percentiles([float(r["ingest_ms_per"]) for r in ingest]),
        "settle_s_total": round(sum(float(r.get("settle_s") or 0) for r in ingest), 2),
        "query_ms": stats.percentiles([float(r["ms"]) for r in queries]),
        "context_tokens_per_query": round(sum(chars) / len(chars) / 4) if chars else None,
        "items_stored_per_1k": round(1000 * stored / turns, 1) if turns else None,
        "provider_usage": (
            {k: v for k, v in usage.items() if k not in ("provider", "q", "dims", "cat")}
            if usage
            else {"reported": False}
        ),
        "provider_ops_ms": (timings or {}).get("ops"),
    }


def build(
    run: BenchRun,
    results: Sequence[dict[str, Any]],
    *,
    suite: dict[str, Any],
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    providers_info = {p["id"]: p for p in (run.plan.get("providers") or [])}
    sections = []
    for pid in run.options.providers:
        rows = [r for r in results if r.get("provider") == pid]
        sections.append(_provider_section(run, pid, rows, providers_info.get(pid, {"name": pid})))
    paired = _paired(run, results)
    return {
        "format": REPORT_FORMAT,
        "run": run.id,
        "state": run.state,
        "finished_at": run.finished_at or time.time(),
        "suite": suite,
        "config": {
            "mode": run.options.mode,
            "k": run.options.k,
            "budget_chars": run.options.budget_chars,
            "seed": run.options.seed,
            "limit_instances": run.options.limit_instances,
            "reader_model": run.resolved_models.get("reader", ""),
            "judge_model": run.resolved_models.get("judge", ""),
            "prompts": prompt_hashes(),
        },
        "environment": environment
        or {"python": sys.version.split()[0], "platform": platform.system().lower()},
        "providers": sections,
        "paired": paired,
        "warnings": list(run.warnings),
    }


def _paired(run: BenchRun, results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each provider pair, per dimension, on the queries both answered."""
    k = str(min(run.options.k, key=lambda v: abs(v - 10)))

    def score(row: dict[str, Any], dim: str) -> float | None:
        """The per-query value of the metric :func:`_provider_section` reports for *dim*."""
        if row.get("correct") is not None:
            return float(bool(row["correct"]))
        if dim == "M7":
            if "pass" in row:
                return float(row["pass"][k])
            return 1.0 - float(row["noise"]) if row.get("noise") is not None else None
        if dim == "M5":
            value = row.get("abstain_retrieval")
            return float(value) if value is not None else None
        if dim == "M8":
            return (0.0 if row["leaked"] else 1.0) if row.get("cat") == "forget" else None
        if dim in ("M9", "M10", "M11", "M12"):
            return None
        if "r" in row and k in row["r"]:
            return float(row["r"][k]["recall_all" if dim == "M2" else "recall_any"])
        return None

    table: dict[str, dict[tuple[str, str], float]] = {}
    for row in results:
        for dim in row.get("dims") or []:
            value = score(row, dim)
            if value is not None:
                table.setdefault(row["provider"], {})[(dim, row["q"])] = value
    out = []
    ids = run.options.providers
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            left, right = table.get(a, {}), table.get(b, {})
            for dim in sorted({d for d, _ in left} & {d for d, _ in right}):
                keys = [key for key in left if key[0] == dim and key in right]
                if not keys:
                    continue
                xs, ys = [left[key] for key in keys], [right[key] for key in keys]
                out.append(
                    {
                        "a": a,
                        "b": b,
                        "dim": dim,
                        "n": len(keys),
                        "delta": round((sum(ys) - sum(xs)) / len(keys), 4),
                        "p_sign": stats.sign_test(xs, ys),
                    }
                )
    return out


def markdown(report: dict[str, Any]) -> str:
    suite = report.get("suite") or {}
    lines = [
        f"# Memory provider benchmark {report.get('run')}",
        "",
        f"Suite: {suite.get('name', '')} ({report.get('suite', {}).get('variant', '')}), "
        f"license {suite.get('license', '')}. Mode: {report['config']['mode']}, "
        f"k {report['config']['k']}, budget {report['config']['budget_chars']} chars.",
        "",
    ]
    dims = sorted({d for p in report["providers"] for d in p["dims"]}, key=lambda d: int(d[1:]))
    header = "| Provider | " + " | ".join(f"{d} {DIM_NAMES.get(d, '')}" for d in dims) + " |"
    lines += [header, "|" + "---|" * (len(dims) + 1)]
    for p in report["providers"]:
        cells = []
        for d in dims:
            cell = p["dims"].get(d) or {}
            value = cell.get("value")
            if value is None:
                cells.append(f"n/a ({cell.get('reason', '')})")
            else:
                mark = " proxy" if cell.get("proxy") else ""
                cells.append(f"{value}{mark} (n={cell.get('n', 0)})")
        lines.append(f"| {p['name']} | " + " | ".join(cells) + " |")
    lines.append("")
    for p in report["providers"]:
        eff = p["efficiency"]
        lines.append(
            f"- {p['name']}: query ms {eff.get('query_ms')}, context tokens per query "
            f"{eff.get('context_tokens_per_query')}, provider usage {eff.get('provider_usage')}"
        )
    for w in report.get("warnings") or []:
        lines.append(f"- Warning: {w}")
    return "\n".join(lines) + "\n"


def comparable(reports: Sequence[dict[str, Any]]) -> str | None:
    """Why *reports* cannot be compared, or ``None`` when they can."""
    first = reports[0]
    for other in reports[1:]:
        if other.get("suite", {}).get("id") != first.get("suite", {}).get("id"):
            return "the runs used different suites"
        if other.get("suite", {}).get("variant") != first.get("suite", {}).get("variant"):
            return "the runs used different suite variants"
        for key in (
            "mode",
            "k",
            "budget_chars",
            "seed",
            "limit_instances",
            "reader_model",
            "judge_model",
            "prompts",
        ):
            if other["config"].get(key) != first["config"].get(key):
                return f"the runs differ in {key}"
    return None


def compare(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    reason = comparable(reports)
    if reason:
        return {"comparable": False, "reason": reason}
    rows = []
    for report in reports:
        for p in report["providers"]:
            rows.append(
                {"run": report["run"], "provider": p["id"], "name": p["name"], "dims": p["dims"]}
            )
    return {"comparable": True, "rows": rows}
