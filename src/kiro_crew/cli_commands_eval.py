"""``kirocrew eval``: run the bundled multi-session scenarios and score them.

``_run_eval`` loads the scenarios under ``eval/scenarios`` beside this file, runs them
(scoring with an LLM judge when ``--judge`` is given) and prints the results;
``write_eval_artifacts`` writes the run's Markdown report and JSON summary. It has a module of its own because it is its own
command with its own imports, and it sits beside the other ``cli_commands_*`` owners
because it finds the scenarios from this file's own location.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig, build_provider_factory
from kiro_crew.eval.judge import LLMJudge
from kiro_crew.eval.runner import EvalRunner, format_results, score_by_dimension
from kiro_crew.eval.scenario import AssertionType, load_scenario, load_scenarios
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def write_eval_artifacts(
    results_dir: Path, ts: str, report: str, json_data: dict[str, object]
) -> tuple[Path, Path]:
    """Write the run's Markdown report and JSON summary, and return their paths.

    Both are written as UTF-8 rather than the host's locale codec.
    ``eval.runner.format_results`` puts a ✅ or ❌ on every scenario, session and
    turn, so the report is never ASCII, and the turn snippets it embeds carry
    whatever the agent said. Under the default codec on a Windows host — cp1252 in
    the US and western Europe, cp950, cp932 and friends elsewhere — encoding that
    raises ``UnicodeEncodeError``, and the raise lands AFTER the whole eval has
    run, taking the JSON summary with it.
    """

    results_dir.mkdir(exist_ok=True)
    report_path = results_dir / f"eval_{ts}.md"
    report_path.write_text(report + "\n", encoding="utf-8")
    json_path = results_dir / f"eval_{ts}.json"
    json_path.write_text(json.dumps(json_data, indent=2) + "\n", encoding="utf-8")
    return report_path, json_path


async def _run_eval(args: argparse.Namespace) -> None:
    """Run multi-session evaluation scenarios."""

    scenarios_dir = Path(__file__).resolve().parent / "eval" / "scenarios"

    if args.all_scenarios:
        scenarios = load_scenarios(scenarios_dir)
    elif args.scenarios:
        scenarios = []
        for name in args.scenarios:
            resolved = None
            for ext in (".json", ".yaml", ".yml"):
                candidate = scenarios_dir / f"{name}{ext}"
                if candidate.exists():
                    resolved = candidate
                    break
            if resolved is None:
                available = sorted(
                    f.stem
                    for f in scenarios_dir.iterdir()
                    if f.suffix in (".json", ".yaml", ".yml")
                )
                print(f"Error: scenario '{name}' not found.")
                print(f"Available scenarios: {', '.join(available)}")
                return
            scenarios.append(load_scenario(resolved))
    else:
        scenarios = [load_scenario(scenarios_dir / "smoke_test.json")]

    total_turns = sum(len(sess.turns) for s in scenarios for sess in s.sessions)
    names = ", ".join(s.name for s in scenarios)
    print(f"Running: {names} ({total_turns} turns)\n")

    config = KiroCrewConfig.load()
    provider_factory = build_provider_factory(config)

    runner = EvalRunner(
        provider_factory=provider_factory, judge_enabled=getattr(args, "judge", False)
    )
    results = await runner.run_scenarios(scenarios)

    # LLM Judge scoring
    if getattr(args, "judge", False):
        judge = LLMJudge(provider_factory=provider_factory)
        await judge.start()
        try:
            for scenario, result in zip(scenarios, results):
                criteria = scenario.judge_criteria or scenario.description
                for sr in result.sessions:
                    for tr in sr.turns:
                        for idx, (a, _) in enumerate(tr.assertion_results):
                            if a.type == AssertionType.JUDGE:
                                try:
                                    verdict = await judge.judge_turn(
                                        scenario.description,
                                        a.value or criteria,
                                        tr.user_message,
                                        tr.agent_response,
                                    )
                                    tr.assertion_results[idx] = (
                                        a,
                                        verdict.score >= judge.pass_threshold,
                                    )
                                    reason, _ = redact_exfiltration_urls(verdict.reason)
                                    reason, _ = redact_credentials(reason)
                                    print(f"  🧑‍⚖️ Judge: {verdict.score}/5 — {reason}")
                                except Exception as exc:
                                    print(f"  ⚠️ Judge failed for turn: {exc}")
                                    tr.assertion_results[idx] = (a, False)
        finally:
            await judge.shutdown()

    report = format_results(results)
    print("\n" + report)

    dims = score_by_dimension(results)
    if dims:
        print("## Dimension Summary")
        for dim, s in sorted(dims.items()):
            status = "✅" if s["rate"] >= 0.75 else "❌"
            print(f"  {status} {dim}: {s['passed']}/{s['total']} ({s['rate']:.0%})")

    overall = sum(1 for r in results if r.passed)
    print(f"\nOverall: {overall}/{len(results)} scenarios passed")

    # Save results
    results_dir = Path.cwd() / "eval_results"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_data = {
        "timestamp": ts,
        "scenarios": [r.summary() for r in results],
        "dimensions": dims,
        "overall_passed": overall,
        "overall_total": len(results),
    }
    report_path, json_path = write_eval_artifacts(results_dir, ts, report, json_data)

    print(f"\nResults saved to:\n  {report_path}\n  {json_path}")
