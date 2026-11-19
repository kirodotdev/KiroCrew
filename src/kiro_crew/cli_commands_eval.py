"""``kirocrew eval``: run multi-session scenarios and score them.

``_run_eval`` loads the scenarios under ``eval/scenarios`` beside this file, or the
project's own files named by ``--dir`` / ``--file`` (in a worker thread, through
``_load_eval_scenarios``), runs them (``--agent`` and ``--model`` pick what answers,
``--repeat N`` runs the set N times and a scenario passes only when every run does),
scores them with an LLM judge when ``--judge`` is given and prints the results;
``--baseline`` / ``--max-drop`` / ``--junit`` (``eval.baseline``) compare the scores with a
saved run and write a JUnit file; ``write_eval_artifacts`` writes the run's Markdown report
and JSON summary. It has a module of its own because it is its own
command with its own imports, and it sits beside the other ``cli_commands_*`` owners
because it finds the scenarios from this file's own location.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.config.loader import KiroCrewConfig, build_provider_factory
from kiro_crew.eval.judge import LLMJudge
from kiro_crew.eval.runner import EvalRunner, format_results, score_by_dimension
from kiro_crew.eval.scenario import AssertionType, load_scenario, load_scenarios
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.terminal_safe import safe_terminal_line

if TYPE_CHECKING:
    from kiro_crew.eval.runner import ScenarioResult
    from kiro_crew.eval.scenario import Scenario


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


def _read_user_scenario(path: Path) -> Scenario:
    """One scenario file a person named, read through the sensitive-path gate.

    ``hooks.safe_read_file`` resolves every link and refuses a target such as a
    credential store, so a scenario file linked to one is never opened; the text
    it returns is parsed without reopening the path.
    """
    from kiro_crew.eval.scenario import parse_scenario_text
    from kiro_crew.hooks import safe_read_file

    return parse_scenario_text(safe_read_file(str(path)), path.suffix)


def _load_failure(name: str, exc: Exception) -> str:
    """Why the scenario file *name* did not load, as one line for stderr.

    A refusal from the read gate names the target a link resolves to, so its text is
    never printed: only its class is. An OS error is given by its reason, without the
    path it names; any other error is printed as it was raised.
    """
    from kiro_crew.security import is_unverifiable_path_refusal

    refusal = str(exc) if isinstance(exc, PermissionError) else ""
    if is_unverifiable_path_refusal(refusal):
        return f"cannot load scenario {name}: the sensitive-path check did not finish; try again."
    if refusal.startswith("Blocked: access to sensitive path"):
        return (
            f"refusing to read scenario {name}: it resolves to a sensitive location "
            "(a credential store or the governance trust root)."
        )
    if refusal.startswith("Blocked:"):
        return f"refusing to read scenario {name}: the file changed while it was being opened."
    # An OS error's own text names the path it opened, which a link chose: give its reason only.
    detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
    return f"cannot load scenario {name}: {safe_terminal_line(f'{type(exc).__name__}: {detail}')}"


def _load_user_scenarios(args: argparse.Namespace) -> list[Scenario] | None:
    """Load the scenario files ``--dir`` or ``--file`` names, or print why not and return None.

    A directory contributes its own scenario files, not those of its subfolders.
    Any file that does not load stops the run and is named, so a typo never
    shrinks the set silently.
    """
    # Imported here, not at module level, so the names ``cli_commands`` serves stay as they are.
    from kiro_crew.eval.scenario import SCENARIO_SUFFIXES, scenario_files

    if getattr(args, "scenarios", None) or getattr(args, "all_scenarios", False):
        print(
            "Error: --dir/--file cannot be combined with scenario names or --all.", file=sys.stderr
        )
        return None
    if args.eval_dir and args.eval_file:
        print("Error: give --dir or --file, not both.", file=sys.stderr)
        return None
    if args.eval_file:
        source = Path(args.eval_file).expanduser()
        files = [source] if source.is_file() else []
        missing = f"scenario file '{source}' not found."
    else:
        source = Path(args.eval_dir).expanduser()
        files = [f for f in scenario_files(source) if not f.is_dir()] if source.is_dir() else []
        missing = f"no scenario files ({', '.join(SCENARIO_SUFFIXES)}) in '{source}'."
    if not files:
        print(safe_terminal_line(f"Error: {missing}"), file=sys.stderr)
        return None
    scenarios: list[Scenario] = []
    for path in files:
        try:
            scenarios.append(_read_user_scenario(path))
        except Exception as exc:
            print(f"Error: {_load_failure(safe_terminal_line(path.name), exc)}", file=sys.stderr)
            return None
    names = [s.name for s in scenarios]
    for name in sorted(set(names)):
        if names.count(name) > 1:
            print(
                safe_terminal_line(f"Error: scenario name '{name}' is used by more than one file."),
                file=sys.stderr,
            )
            return None
    return scenarios


def _load_eval_scenarios(args: argparse.Namespace) -> list[Scenario] | None:
    """Find and load the scenarios ``args`` names, or print why not and return None.

    Sync on purpose: every step is a disk call, so ``_run_eval`` runs the whole
    lookup in one worker thread rather than on the loop its sessions share.
    """

    if getattr(args, "eval_dir", None) or getattr(args, "eval_file", None):
        return _load_user_scenarios(args)

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
                print(f"Error: scenario '{name}' not found.", file=sys.stderr)
                print(f"Available scenarios: {', '.join(available)}", file=sys.stderr)
                return None
            scenarios.append(load_scenario(resolved))
    else:
        scenarios = [load_scenario(scenarios_dir / "smoke_test.json")]
    return scenarios


async def _eval_provider_kwargs(
    args: argparse.Namespace, config: KiroCrewConfig
) -> dict[str, object]:
    """The provider arguments ``--agent`` and ``--model`` ask for; empty when neither is given.

    ``--agent`` takes a crew member or a kiro agent, resolved as ``kirocrew chat
    --agent`` resolves it. ``--model`` is the session's explicit pick, so it
    outranks the agent's own pin. An unknown crew-member memory store prints
    the error and exits 1, with nothing run.
    """

    agent = (getattr(args, "agent", None) or "").strip()
    model = (getattr(args, "model", None) or "").strip()
    kwargs: dict[str, object] = {}
    member_model = ""
    if agent:
        from kiro_crew.cli_chat import _resolve_cli_agent
        from kiro_crew.memory_stores import UnknownMemoryStore

        try:
            kiro_agent, crew_agent, member_model, _store = await asyncio.to_thread(
                _resolve_cli_agent, config, agent
            )
        except UnknownMemoryStore as exc:
            print(f"Error: {safe_terminal_line(str(exc))}", file=sys.stderr)
            sys.exit(1)
        # The factory reads the agent's model pin from the spec snapshot on the loop,
        # where a cold snapshot answers no pin; warm it first, as the background session does.
        from kiro_crew.agent_discovery import warm_agent_specs

        await warm_agent_specs(operation="eval_agent", source="cli")
        kwargs["agent"] = kiro_agent
        if crew_agent:
            kwargs["crew_agent"] = crew_agent
        else:
            member_model = ""
    if model or member_model:
        kwargs["model_override"] = model or member_model
    return kwargs


async def _judge_results(
    judge: LLMJudge, scenarios: list[Scenario], results: list[ScenarioResult]
) -> None:
    """Score every ``judge`` assertion in one run's *results* with the LLM judge."""

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
                            print(
                                f"  🧑‍⚖️ Judge: {verdict.score}/5 — {safe_terminal_line(reason)}"
                            )
                        except Exception as exc:
                            print(f"  ⚠️ Judge failed for turn: {safe_terminal_line(str(exc))}")
                            tr.assertion_results[idx] = (a, False)


async def _run_eval(args: argparse.Namespace) -> None:
    """Run multi-session evaluation scenarios.

    Exits 1 when a scenario name is unknown (nothing runs) or when any scenario
    fails (after the report is written); returns normally when every one passes.
    ``capture KEY`` as the first name saves a recorded chat as a scenario instead
    (``eval.capture``) and exits with that command's code.
    """

    if (getattr(args, "scenarios", None) or [])[:1] == ["capture"] and not getattr(
        args, "all_scenarios", False
    ):
        # `kirocrew eval capture KEY` saves a recorded chat as a scenario file; it runs nothing.
        from kiro_crew.eval.capture import run_capture

        sys.exit(await asyncio.to_thread(run_capture, args))

    scenarios = await asyncio.to_thread(_load_eval_scenarios, args)
    if scenarios is None:
        # Nothing ran, so `kirocrew eval x && next` must stop.
        sys.exit(1)

    repeat = getattr(args, "repeat", 1) or 1
    total_turns = sum(len(sess.turns) for s in scenarios for sess in s.sessions)
    names = ", ".join(safe_terminal_line(s.name) for s in scenarios)
    runs_note = f", {repeat} runs" if repeat > 1 else ""
    print(f"Running: {names} ({total_turns} turns{runs_note})\n")

    # Read before the run so an unusable baseline is named up front; None when not asked for.
    from kiro_crew.eval.baseline import gate_from_args

    gate = await asyncio.to_thread(gate_from_args, args)

    # A stored Kiro CLI pick runs on KAS where kiro-cli offers v3; this process
    # never ran the gateway's KAS floor, so establish that evidence before the
    # load reads the pick. Probes only for a Kiro CLI pick; off the event loop.
    from kiro_crew.agent_sdk.backends import ensure_kiro_cli_kas_verdict

    await asyncio.to_thread(ensure_kiro_cli_kas_verdict)
    config = KiroCrewConfig.load()
    provider_factory = build_provider_factory(config)

    provider_kwargs = await _eval_provider_kwargs(args, config)
    runner = EvalRunner(
        provider_factory=provider_factory,
        judge_enabled=getattr(args, "judge", False),
        provider_kwargs=provider_kwargs,
        # The gate judges each call as the agent the sessions run as, as chat does.
        gate_agent=str(provider_kwargs.get("crew_agent") or provider_kwargs.get("agent") or ""),
    )
    rounds: list[list[ScenarioResult]] = []
    for run_index in range(1, repeat + 1):
        if repeat > 1:
            print(f"Run {run_index}/{repeat}")
        rounds.append(await runner.run_scenarios(scenarios))

    # LLM Judge scoring
    if getattr(args, "judge", False):
        judge = LLMJudge(provider_factory=provider_factory)
        await judge.start()
        try:
            for round_results in rounds:
                await _judge_results(judge, scenarios, round_results)
        finally:
            await judge.shutdown()

    # One run reports as it always did; several fold into one result per scenario
    # that passes only when every run did.
    if repeat == 1:
        results = rounds[0]
    else:
        from kiro_crew.eval.runner import merge_repeats

        results = merge_repeats(rounds)

    report = format_results(results)
    # Scenario text can come from a file someone else wrote: no terminal controls out.
    from kiro_crew.terminal_safe import strip_control_characters

    print("\n" + strip_control_characters(report))

    dims = score_by_dimension(results)
    if dims:
        print("## Dimension Summary")
        for dim, s in sorted(dims.items()):
            status = "✅" if s["rate"] >= 0.75 else "❌"
            print(
                f"  {status} {safe_terminal_line(dim)}: {s['passed']}/{s['total']} ({s['rate']:.0%})"
            )

    overall = sum(1 for r in results if r.passed)
    print(f"\nOverall: {overall}/{len(results)} scenarios passed")

    # Save results
    results_dir = Path.cwd() / "eval_results"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_data: dict[str, object] = {
        "timestamp": ts,
        "scenarios": [r.summary() for r in results],
        "dimensions": dims,
        "overall_passed": overall,
        "overall_total": len(results),
    }
    # Recorded only when asked for, so a plain `kirocrew eval` writes the file it always did.
    if repeat > 1:
        json_data["repeat"] = repeat
    for key in ("agent", "model"):
        if getattr(args, key, None):
            json_data[key] = getattr(args, key)
    report_path, json_path = await asyncio.to_thread(
        write_eval_artifacts, results_dir, ts, report, json_data
    )

    print(f"\nResults saved to:\n  {report_path}\n  {json_path}")

    failed_run = overall < len(results)
    junit_path = getattr(args, "junit", None)
    if gate is not None or junit_path:
        from kiro_crew.eval import baseline

        rows = [r.summary() for r in results]
        reasons: dict[str, str] | None = None
        if gate is not None:
            found = baseline.compare(gate, rows)
            baseline.print_comparison(found, gate.max_drop)
            reasons = found.reasons
            # The gate only adds: a regression fails the run, and so does every failure.
            failed_run = failed_run or bool(found.regressions)
        if junit_path:
            await asyncio.to_thread(baseline.write_junit, junit_path, rows, reasons)

    if failed_run:
        # A failed scenario, or a score below the baseline when one is given, must fail
        # the command, so `kirocrew eval && next` stops and a script can gate on it.
        # The report is already on disk.
        sys.exit(1)


#: Flags that only mean something after ``kirocrew eval run`` (dest names).
_EVAL_RUN_ONLY = ("suite", "tier", "harness", "case_ids", "eval_home", "out", "json", "check")
#: Scenario flags ``kirocrew eval run`` does not take (dest names).
_SCENARIO_ONLY = (
    "all_scenarios",
    "judge",
    "eval_dir",
    "eval_file",
    "agent",
    "baseline",
    "max_drop",
    "junit",
)


def _eval_run_flag_refusal(args: argparse.Namespace) -> str:
    """Why this ``kirocrew eval`` invocation mixes the two forms, or "" when it does not."""
    names = list(getattr(args, "scenarios", None) or [])
    if names[:1] == ["run"]:
        if len(names) > 1:
            return f"kirocrew eval run takes no scenario names (got {' '.join(names[1:])})"
        stray = [n for n in _SCENARIO_ONLY if getattr(args, n, None)]
        if stray:
            return f"kirocrew eval run does not take --{stray[0].replace('_', '-')}"
        return ""
    stray = [
        n
        for n in _EVAL_RUN_ONLY
        # `capture KEY --out FILE` names the scenario file it writes.
        if getattr(args, n, None) and not (n == "out" and names[:1] == ["capture"])
    ]
    if stray:
        return f"--{stray[0].replace('_', '-')} needs kirocrew eval run"
    return ""


def _run_eval_tasks(args: argparse.Namespace) -> int:
    """``kirocrew eval run --suite tasks``: run or ``--check`` the task cases; the exit code.

    Exit: 0 every scored run passed, 1 a run FAILED (or none was scored), 2 usage,
    3 preflight refused (rfc-builtin-eval.md §3.4).
    """
    import os

    from kiro_crew.eval import tasks

    suite = getattr(args, "suite", None) or ""
    if suite != "tasks" and not (suite and Path(suite).expanduser().is_dir()):
        print("Error: --suite must be 'tasks' or a suite directory.", file=sys.stderr)
        return 2
    suite_dir = tasks.resolve_suite(args.suite)
    try:
        cases = tasks.find_cases(suite_dir, args.tier or "all", list(args.case_ids or []))
    except tasks.CaseError as exc:
        print(safe_terminal_line(f"Error: {exc}"), file=sys.stderr)
        return 2
    if not cases:
        print(safe_terminal_line(f"Error: no cases under {suite_dir}/cases."), file=sys.stderr)
        return 2
    if args.check:
        problems = tasks.check_cases(cases)
        for line in problems:
            print(safe_terminal_line(f"FAIL {line}"), file=sys.stderr)
        print(
            f"{len(cases) - len({p.split(':')[0] for p in problems})}/{len(cases)} cases bite and are solvable"
        )
        return 1 if problems else 0
    try:
        arms = tasks.resolve_harnesses(args.harness or "all")
    except ValueError as exc:
        print(safe_terminal_line(f"Error: {exc}"), file=sys.stderr)
        return 2
    if not args.eval_home:
        print(
            "Error: --suite tasks needs --eval-home DIR; task arms never run in your own home.",
            file=sys.stderr,
        )
        return 2
    eval_root = Path(args.eval_home).expanduser().resolve()
    if eval_root == suite_dir or suite_dir in eval_root.parents or eval_root in suite_dir.parents:
        print("Error: --eval-home must not contain or sit inside the case suite.", file=sys.stderr)
        return 2
    out_dir = (Path(args.out).expanduser() if args.out else Path.cwd() / "eval_results").resolve()
    if out_dir == suite_dir or suite_dir in out_dir.parents:
        # The suite is fingerprinted after every run; results written into it would
        # read as tampering and stop the batch.
        print("Error: --out must not be the case suite or sit inside it.", file=sys.stderr)
        return 2
    env = dict(os.environ)
    refusals = tasks.preflight(arms, env)
    if refusals:
        for line in refusals:
            print(safe_terminal_line(f"Refused: {line}"), file=sys.stderr)
        return 3
    from kiro_crew.owner_only_files import mkdirs_owner_only

    mkdirs_owner_only(eval_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    records = tasks.run_batch(
        cases, arms, args.repeat, eval_root, out_dir, suite_dir, args.model or "", env
    )
    rows = tasks.summarize(records, tasks.requested_runs(cases, args.repeat))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    summary: dict[str, object] = {
        "timestamp": ts,
        "suite": "tasks",
        "summary": rows,
        "runs": records,
    }
    report_path, json_path = write_eval_artifacts(out_dir, ts, tasks.format_summary(rows), summary)
    if args.json:
        print(json.dumps({"timestamp": ts, "summary": rows, "report": str(json_path)}))
    else:
        print(tasks.format_summary(rows))
        print(f"\nResults saved to:\n  {report_path}\n  {json_path}")
    scored = [r for r in records if r["verdict"] != "VOID"]
    return 0 if scored and all(r["verdict"] == "PASSED" for r in scored) else 1
