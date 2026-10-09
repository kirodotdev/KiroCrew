from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import build_provider_factory
from kiro_crew.history import ConversationLog
from kiro_crew.personal_insights.insights_broker import (
    FACET_BATCH,
    BrokerError,
    OneShotBroker,
    facet_prompt,
    facets_digest,
    synthesis_prompt,
    validate_synthesis,
)
from kiro_crew.personal_insights.insights_facts import aggregate, extract_facts, select_sessions
from kiro_crew.personal_insights.insights_registry import load_registry
from kiro_crew.personal_insights.insights_report import build_actions, render_markdown
from kiro_crew.personal_insights.insights_runs import RunRepository, insights_home
from kiro_crew.platform import boot_platform
from kiro_crew.session import SessionManager

logger = logging.getLogger("personal_insights.run")


def _capabilities_for_prompt() -> list[dict]:
    registry = load_registry()
    return [
        {
            "capability_id": c.capability_id,
            "class": c.cls,
            "behavior_keys": list(c.behavior_keys),
            "description": c.description,
        }
        for c in registry.capabilities
        if c.capability_id != "no-action"
    ]


async def run(days: int, max_sessions: int, progress) -> Path:
    cfg = KiroCrewConfig.load()
    boot_platform(cfg)
    log = ConversationLog()
    log.init()
    repo = RunRepository()
    rows, cataloged = select_sessions(log, days=days, max_sessions=max_sessions)
    progress(f"cataloged {cataloged} dashboard sessions, selected {len(rows)}")
    facts = []
    for row in rows:
        item = extract_facts(log, row["key"], row)
        if item is not None:
            facts.append(item)
    progress(f"extracted facts for {len(facts)} sessions")
    run_id = repo.start_run(window_days=days, cataloged=cataloged, selected=len(facts))
    sessions = SessionManager(cfg, provider_factory=build_provider_factory(cfg))
    broker = OneShotBroker(sessions)
    facets: list[dict] = []
    omitted = 0
    pending = []
    for item in facts:
        digest = facets_digest(item)
        cached = None
        for model in list(broker.receipt.served_models) or ["unknown"]:
            cached = repo.cached_facet(item.key, digest, model)
            if cached:
                break
        if cached:
            facets.append(cached)
        else:
            pending.append((item, digest))
    progress(f"{len(facets)} facets cached, {len(pending)} to compute")
    for start in range(0, len(pending), FACET_BATCH):
        batch = pending[start : start + FACET_BATCH]
        try:
            result = await broker.call_json(
                facet_prompt([b[0] for b in batch]), required_keys=("facets",)
            )
        except BrokerError as exc:
            omitted += len(batch)
            progress(f"facet batch failed: {exc}")
            continue
        by_key = {f.get("key"): f for f in result.get("facets", []) if isinstance(f, dict)}
        for item, digest in batch:
            facet = by_key.get(item.key)
            if not facet:
                omitted += 1
                continue
            facet["measured"] = {
                "owner_turns": item.owner_turns,
                "tool_calls": item.tool_calls,
                "tool_errors": item.tool_errors,
                "correction_markers": item.correction_markers,
                "duration_minutes": item.duration_minutes,
            }
            facets.append(facet)
            repo.store_facet(item.key, digest, broker.receipt.served_model(), facet)
        progress(f"facets {len(facets)}/{len(facts)} after {broker.receipt.calls} calls")
    known = {f.key for f in facts}
    agg = aggregate(facts)
    prior = repo.prior_applied_actions()
    synthesis = await broker.call_json(
        synthesis_prompt(facets, agg, prior, _capabilities_for_prompt()),
        required_keys=("recognition", "working", "friction", "recommendations"),
    )
    problems = validate_synthesis(synthesis, known)
    actions = build_actions(synthesis, prior)
    served = broker.receipt.served_model()
    markdown = render_markdown(
        run_id=run_id,
        window_days=days,
        cataloged=cataloged,
        facts=facts,
        aggregate=agg,
        synthesis=synthesis,
        actions=actions,
        prior_actions=prior,
        served_model=served,
        omitted=omitted,
        problems=problems,
    )
    reports = insights_home() / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    path = reports / f"{run_id}.md"
    path.write_text(markdown, encoding="utf-8")
    os.chmod(path, 0o600)
    raw = reports / f"{run_id}.json"
    raw.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "synthesis": synthesis,
                "actions": actions,
                "aggregate": agg,
                "problems": problems,
                "broker": {
                    "calls": broker.receipt.calls,
                    "retries": broker.receipt.retries,
                    "failures": broker.receipt.failures,
                    "input_bytes": broker.receipt.input_bytes,
                    "served_model": served,
                },
            },
            indent=1,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.chmod(raw, 0o600)
    claims = [
        {
            "claim_id": c["claim_id"],
            "dimension": section,
            "behavior_predicate": c.get("behavior_predicate"),
            "text": c["text"],
            "session_keys": c.get("session_keys", []),
        }
        for section in ("working", "friction")
        for c in synthesis.get(section, [])
    ]
    repo.record_claims(run_id, claims)
    repo.record_actions(run_id, actions, baseline_sessions=len(facts))
    repo.finish_run(
        run_id,
        analyzed=len(facets),
        served_model=served,
        report_path=str(path),
        report_digest=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )
    repo.close()
    progress(
        f"done: {len(facets)} facets, {len(actions)} actions, {broker.receipt.calls} model calls, "
        f"model {served}"
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    report = sub.add_parser("report")
    report.add_argument("--days", type=int, default=30)
    report.add_argument("--max-sessions", type=int, default=40)
    apply = sub.add_parser("do-it")
    apply.add_argument("action_id")
    apply.add_argument("--force", action="store_true")
    undo_cmd = sub.add_parser("undo")
    undo_cmd.add_argument("action_id")
    sub.add_parser("status")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    def progress(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    if args.command in (None, "report"):
        days = getattr(args, "days", 30)
        max_sessions = getattr(args, "max_sessions", 40)
        path = asyncio.run(run(days, max_sessions, progress))
        print(path)
        return 0
    boot_platform(KiroCrewConfig.load())
    from kiro_crew.personal_insights.insights_apply import describe, do_it, undo

    repo = RunRepository()
    try:
        if args.command == "do-it":
            result = do_it(args.action_id, repo=repo, force=args.force)
            print(describe(result))
            return 0 if result.state == "applied_verified" else 1
        if args.command == "undo":
            result = undo(args.action_id, repo=repo)
            print(describe(result))
            return 0 if result.state == "undone" else 1
        for row in repo.recent_runs():
            print(json.dumps(row, sort_keys=True))
        for action in repo.db.execute(
            "SELECT run_id, action_id, action_class, state, title FROM actions ORDER BY run_id, action_id"
        ):
            print(json.dumps(dict(zip(("run_id", "action_id", "class", "state", "title"), action))))
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
