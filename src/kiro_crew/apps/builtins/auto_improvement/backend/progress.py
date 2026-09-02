"""The normalized progress series — the cumulative-best staircase the chart draws.

Reads the per-candidate archive rows, the run metadata, and the active ruler, and
produces ONE target-agnostic series: a point per measured candidate in cycle
order, where ``bestSoFar`` advances only on a ``kept`` row that actually improved
the metric (down for a minimized metric, up for a maximized one).

The normalization is the point. Every profile's ``results.tsv`` has its own
columns, but they all reduce to this shape, so the chart code is identical across
targets and a new profile needs no frontend change.

Every reader here tolerates a malformed row rather than raising: the archive is
written by a long-running background thread, so a partially-flushed or garbled
row is a normal transient state. A crash would take out the whole progress
endpoint and blank the UI; skipping one point degrades a single marker.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import store

logger = logging.getLogger(__name__)

#: Archive status meaning the candidate survived the gate and was kept.
_KEPT = "kept"

#: Ledger statuses that mean a pull request exists (or was queued) for a finding.
_FILED_STATUSES = frozenset({"filed", "committed"})


def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int, for a column that may be blank or non-numeric."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any) -> float | None:
    """Best-effort float, or None when the cell is missing/garbled.

    Load-bearing: a ``primary_delta`` of ``"n/a"``, ``"-"``, ``""`` or any other
    non-numeric string must read as *missing*, not raise. An unguarded ``float()``
    here previously crashed the whole progress read on one bad row.
    """
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _target_slug(target: str) -> str:
    """``src/pkg/search.py::negamax_root`` -> ``search_py_negamax_root``.

    The slug a candidate id embeds, which is the only identifier the archive row
    and the ledger entry have in common (see :func:`read_progress`).

    Mirrors ``spine.proposer._short``: the cand_id is built from the file's BASENAME
    plus the symbol, NOT the full path. Slugging the whole target instead produced
    ``kiro_crew_apps_builtins_..._contracts_py_proposal`` for a nested file, which is
    never a substring of ``c6_wide_contracts_py_Proposal_2cbc5716`` — so the join
    silently failed for every deeply-nested finding and the UI showed a diff with no
    defect/hypothesis, and progress rows carried no PR link. Kept in step with
    ``_short``'s 48-char cap so both sides truncate identically.
    """

    base = target.split("/")[-1]
    slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return slug[:48]


def read_ruler() -> dict[str, Any]:
    """The active ruler, or an uncalibrated placeholder."""
    data = store.read_json(store.ruler_dir() / "ruler.json")
    if isinstance(data, dict):
        return data
    return {"status": "uncalibrated"}


def ruler_calibrated() -> bool:
    """True iff a run is allowed to enter its improvement cycles."""
    return str(read_ruler().get("status") or "") == "calibrated"


def _run_meta() -> dict[str, Any]:
    meta = store.read_json(store.results_dir() / "run.meta.json")
    return meta if isinstance(meta, dict) else {}


def _archive_rows() -> list[dict[str, Any]]:
    """Per-candidate archive rows, in write (cycle) order.

    Read line-by-line so one corrupt tail line — a crash mid-append — cannot hide
    every earlier candidate.
    """
    path = store.results_dir() / "candidates.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_findings() -> list[dict[str, Any]]:
    """Ledger entries, newest first, with the PR reference normalized.

    The on-disk field is historically named ``cr``; both keys are read and the
    result always carries ``pr`` so every consumer speaks one vocabulary.
    """
    path = store.ledger_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        row["pr"] = row.get("pr") or row.get("cr") or ""
        rows.append(row)
    return list(reversed(rows))


def read_progress() -> dict[str, Any]:
    """Build the cumulative-best staircase for the chart.

    Returns ``{primary, anchors, noiseBand, points[]}``. ``points`` is in cycle
    order; ``bestSoFar`` is monotonic in the improving direction, so the series
    renders as a staircase that only ever steps toward the floor.
    """
    ruler = read_ruler()
    primary = ruler.get("primary")
    if not isinstance(primary, dict):
        primary = {"name": "", "unit": "", "direction": "minimize", "label": ""}
    minimize = str(primary.get("direction") or "minimize") != "maximize"

    meta = _run_meta()
    band = ruler.get("noiseBand")
    noise_band = band.get("value") if isinstance(band, dict) else meta.get("noise_band")

    anchors_raw = ruler.get("anchors")
    anchors = anchors_raw if isinstance(anchors_raw, list) else []

    # The staircase starts at the frozen base anchor when the ruler has one;
    # otherwise it starts unknown and picks up from the first measured candidate.
    base_anchor = next(
        (a for a in anchors if isinstance(a, dict) and a.get("value") is not None), None
    )
    best: float | None = None
    if base_anchor is not None:
        best = _coerce_float(base_anchor.get("value"))

    # PR reference per finding, so a chart marker can link to its pull request.
    #
    # Keyed by BOTH fingerprint and a target slug, because the archive row and the
    # ledger have no shared id: a row's ``diff_ref`` is a candidate id
    # (``c1_wide_search_py_negamax_root_<hash>.diff``) while the ledger keys on a
    # content fingerprint (``b8cdaa63…``). Joining on fingerprint alone — which is
    # what the shape suggests — silently never matches, so every chart marker came
    # back with an empty PR link. The target slug embedded in the candidate id is
    # the one thing both sides share.
    pr_by_fp: dict[str, str] = {}
    pr_by_slug: dict[str, str] = {}
    for finding in read_findings():
        if str(finding.get("status") or "") not in _FILED_STATUSES:
            continue
        ref = str(finding.get("pr") or "")
        if not ref:
            continue
        pr_by_fp[str(finding.get("fp") or "")] = ref
        slug = _target_slug(str(finding.get("target") or ""))
        if slug:
            pr_by_slug.setdefault(slug, ref)

    points: list[dict[str, Any]] = []
    for row in _archive_rows():
        delta = _coerce_float(row.get("primary_delta"))
        status = str(row.get("status") or "")
        kept = status == _KEPT

        # Prefer an explicit absolute measurement; else derive it from the running
        # best plus the signed delta (the delta is measured against the cycle base,
        # which is the current best).
        cand_value: float | None = None
        metric = row.get("metric")
        if isinstance(metric, dict) and metric.get("primary_cand") is not None:
            cand_value = _coerce_float(metric.get("primary_cand"))
        if cand_value is None and best is not None and delta is not None:
            cand_value = best + delta

        # Advance the cumulative best ONLY on a keep that genuinely improved the
        # metric. A kept row that did not improve leaves the staircase flat.
        if kept and cand_value is not None:
            if best is None or (cand_value < best if minimize else cand_value > best):
                best = cand_value

        ref_id = str(row.get("diff_ref") or "").removesuffix(".diff")
        cand_id = str(row.get("cand_id") or ref_id)
        # Direct fingerprint hit first (a future writer may key them the same),
        # then fall back to the target slug carried inside the candidate id.
        pr_ref = pr_by_fp.get(ref_id) or ""
        if not pr_ref:
            for slug, candidate_pr in pr_by_slug.items():
                if slug and slug in cand_id.lower():
                    pr_ref = candidate_pr
                    break
        points.append(
            {
                "cycle": _coerce_int(row.get("cycle")),
                "candId": cand_id,
                "status": status,
                "candValue": cand_value,
                "deltaVsBest": delta,
                "bestSoFar": best,
                "kept": kept,
                "fp": ref_id,
                "pr": pr_ref,
                "description": str(row.get("description") or ""),
            }
        )

    return {
        "primary": primary,
        "anchors": [
            {"name": str(a.get("name") or ""), "value": a.get("value")}
            for a in anchors
            if isinstance(a, dict)
        ],
        "noiseBand": noise_band,
        "points": points,
        "run": meta,
    }
