import json
import re
from pathlib import Path
from typing import Any

from kiro_crew.personal_insights.insights_report import FOLLOW_THROUGH_MIN_SESSIONS
from kiro_crew.personal_insights.insights_runs import RunRepository

ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
_ACTION_FENCE_RE = re.compile(r"```insights-action\n.*?\n```\n?", re.DOTALL)
_ACTIONS_HEADING = "## Recommended next changes"
_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_ACTION_FIELDS = (
    "action_id",
    "action_key",
    "action_class",
    "behavior_predicate",
    "title",
    "cta",
    "executable",
    "why",
    "display_artifact",
    "expected_observation",
    "verification",
    "undo",
    "rank",
    "claim_ids",
)


def split_report(markdown: str) -> dict[str, str]:
    cleaned = _ACTION_FENCE_RE.sub("", markdown)
    start = cleaned.find(_ACTIONS_HEADING)
    if start < 0:
        return {"before": cleaned, "after": ""}
    rest = cleaned[start + len(_ACTIONS_HEADING) :]
    nxt = _HEADING_RE.search(rest)
    after = rest[nxt.start() :] if nxt else ""
    return {"before": cleaned[:start].rstrip() + "\n", "after": after}


def _action_view(repo: RunRepository, record: dict[str, Any], total: int) -> dict[str, Any]:
    payload = record["payload"]
    view = {k: payload.get(k) for k in _ACTION_FIELDS}
    view["action_id"] = record["action_id"]
    view["state"] = record["state"]
    for stamp in ("applied_at", "verified_at", "undone_at"):
        view[stamp] = record.get(stamp)
    claims = repo.claims_for_action(record["run_id"], list(payload.get("claim_ids") or []))
    keys: list[str] = []
    for claim in claims:
        for key in claim["session_keys"]:
            if key not in keys:
                keys.append(key)
    view["evidence_sessions"] = len(keys)
    view["evidence_total"] = total
    view["evidence_keys"] = keys[:8]
    return view


def _actions_for_run(repo: RunRepository, run_id: str, total: int) -> list[dict[str, Any]]:
    rows = repo.db.execute(
        "SELECT action_id FROM actions WHERE run_id=? ORDER BY action_id", (run_id,)
    ).fetchall()
    out: list[dict[str, Any]] = []
    for (action_id,) in rows:
        record = repo.get_action(action_id)
        if record is not None and record["run_id"] == run_id:
            out.append(_action_view(repo, record, total))
    out.sort(key=lambda a: (a.get("rank") or 99, a["action_id"]))
    return out


def _run_row(repo: RunRepository, run_id: str | None) -> dict[str, Any] | None:
    runs = repo.recent_runs(limit=50)
    if run_id is None:
        complete = [r for r in runs if r["status"] == "complete"]
        return complete[0] if complete else None
    for row in runs:
        if row["run_id"] == run_id:
            return row
    return None


def run_view(repo: RunRepository, run_id: str | None = None) -> dict[str, Any] | None:
    row = _run_row(repo, run_id)
    if row is None:
        return None
    markdown = ""
    path = row.get("report_path")
    if path and Path(path).is_file():
        markdown = Path(path).read_text(encoding="utf-8")
    report = split_report(markdown)
    total = int(row.get("analyzed") or 0)
    return {
        "run": {
            k: row[k]
            for k in (
                "run_id",
                "created_at",
                "window_days",
                "cataloged",
                "analyzed",
                "served_model",
                "artifact_slug",
                "status",
            )
        },
        "actions": _actions_for_run(repo, row["run_id"], total),
        "prior_actions": [
            {
                k: p.get(k)
                for k in (
                    "run_id",
                    "action_id",
                    "action_class",
                    "title",
                    "state",
                    "applied_at",
                    "verified_at",
                    "baseline_sessions",
                )
            }
            for p in repo.prior_applied_actions()
            if p["run_id"] != row["run_id"]
        ],
        "report_before": report["before"],
        "report_after": report["after"],
        "runs": [
            {k: r[k] for k in ("run_id", "created_at", "analyzed", "status", "artifact_slug")}
            for r in repo.recent_runs(limit=20)
        ],
        "follow_through_min_sessions": FOLLOW_THROUGH_MIN_SESSIONS,
    }


def action_summary(result: Any) -> dict[str, Any]:
    data = result.as_dict() if hasattr(result, "as_dict") else dict(result)
    return json.loads(json.dumps(data, default=str))
