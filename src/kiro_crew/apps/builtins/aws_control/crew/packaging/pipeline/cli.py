"""The two verbs, ``plan`` and ``build``, and the argument parser in front of them.

Thin by design: each verb composes the owners in the order the refusals need -- the
``--out`` screen, crew resolution, the spec read, enumeration, the plan, the report
ownership check -- and prints what they decided. ``main`` turns an ``ExportRefused`` into
the refusal line and exit status 2; nothing else is caught here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import candidates as _candidates
from . import crew as _crew
from . import destination as _destination
from . import plan as _plan
from . import report as _report
from . import transaction as _transaction
from .contract import PLAN_FILENAME, ExportRefused
from .plan import _KINDS, Drift


def _print_decision(decision: dict) -> None:
    for kind in _KINDS:
        ids = decision["included"][kind]
        print(f"  include {kind:<7} {len(ids)}: {', '.join(ids) or '(none)'}")
    print(f"  denied {len(decision['denied'])}:")
    for d in decision["denied"]:
        print(f"    - {d['kind']}/{d['id']}: {d['reason']}")


def _cmd_plan(crew_name: str, out: Path, allow: list[Path], source: Path | None) -> int:
    _destination._refuse_unc_out(out)
    crew = _crew.resolve_crew(crew_name, source)
    agent_spec = _crew.read_agent_spec(crew)
    candidates = _candidates.enumerate_all(crew, agent_spec)

    plan_path = out / PLAN_FILENAME
    # No ``is_file()`` check before the write: that check and the write were not atomic, so a
    # plan created by a racer in between was truncated. ``write_plan`` now claims the name with
    # ``O_EXCL`` and reports whether THIS call created it, which is the same no-replace-on-
    # creation rule the promote transaction uses -- a name this command did not claim is not
    # its own to overwrite.
    if _plan.write_plan(plan_path, crew.name, candidates):
        print(f"wrote deny-by-default review template: {plan_path}")
        print("Everything is excluded. Nothing ships until you sign it and pass it with --allow.")
    else:
        # Left exactly as it is. To proceed: edit this template to set include/reviewed_by,
        # then re-run with --allow pointing at it. To start over, remove it first.
        print(f"review template already present: {plan_path} (left as-is)")
        print("Edit it and re-run with --allow <path>, or remove it to regenerate.")

    plan = _plan.merge_plans(allow, crew.name)
    if plan is not None:
        _plan.verify(plan, crew.name, candidates)  # refuse an unsigned/laundered --allow early
    print("decision set (no bundle written):")
    _print_decision(_plan._decision_set(candidates, plan))
    return 0


def _cmd_build(crew_name: str, out: Path, allow: list[Path], source: Path | None) -> int:
    _destination._refuse_unc_out(out)
    crew = _crew.resolve_crew(crew_name, source)
    agent_spec = _crew.read_agent_spec(crew)
    candidates = _candidates.enumerate_all(crew, agent_spec)

    plan = _plan.merge_plans(allow, crew.name)
    if plan is not None:
        drift = _plan.verify(plan, crew.name, candidates)
    else:
        drift = Drift()

    # The report path is validated BEFORE build_bundle, not after it.
    #
    # The check itself landed last round, at the write -- which is after build_bundle has
    # staged, moved the previous bundle aside, renamed staging into place and deleted the
    # aside copy. So it refused a foreign report only once every destructive step had already
    # run: the operator's file was intact and their bundle directory had been replaced anyway.
    # A preflight that runs after the thing it guards is a message, not a guard.
    #
    # Derived here rather than passed down, because it is derived from --out the same way the
    # writer derives it, and two spellings of one derivation is how the staging marker and
    # this path came to have different rules in the first place.
    json_path = out.parent / f"{out.name}.smc-bundle.json"
    _report._refuse_unless_our_report(json_path, out)

    report = _transaction.build_bundle(crew, agent_spec, candidates, plan, out)

    # The report itself is written by ``build_bundle``, before the swap, so a failure there
    # cannot land after the previous bundle is gone. What stays here is the ownership check
    # above (which has to run before anything is built) and the human output below.

    # Human-readable progress first; the machine marker is the LAST line.
    print(f"bundle:  {report.bundle_dir}")
    print(f"digest:  {report.digest}")
    print(f"skills:  {report.skill_count}")
    print(f"mcp:     {', '.join(report.mcp_servers) or '(none)'}")
    if report.denied:
        print(f"denied:  {len(report.denied)} (see SMC_BUNDLE_JSON)")
    if drift.describe():
        print(f"note:    since the plan was written, {drift.describe()}")
    for note in report.notes:
        print(f"  - {note}")
    if not report.skill_count and not report.mcp_servers:
        print("Nothing private was selected: a valid bundle with the crew's persona only.")
    print(f"SMC_BUNDLE_JSON={json_path}")
    return 0


def _source_from(args_source: str | None) -> Path | None:
    raw = args_source or os.environ.get("SMC_CREW_SOURCE")
    return Path(raw).expanduser() if raw else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m packaging.build",
        description="Curate a local crew into a deployable bundle (deny-by-default).",
    )

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--crew", required=True, help="crew name")
        p.add_argument("--out", type=Path, required=True, help="bundle output directory")
        p.add_argument(
            "--allow",
            type=Path,
            action="append",
            default=[],
            metavar="PATH",
            help="a signed curation plan whose selected skills/MCP servers may ship "
            "(repeatable). Omit for an empty-but-valid bundle.",
        )
        p.add_argument(
            "--source",
            default=None,
            help="crew home holding agents/<name>.json and skills/ (defaults to the "
            "real Kiro Crew locations; $SMC_CREW_SOURCE also honoured).",
        )

    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="print the decision set and write a review template")
    _add_common(p_plan)
    p_build = sub.add_parser("build", help="write the bundle (the default verb)")
    _add_common(p_build)

    # `build` is the default verb: if the first token is neither a subcommand nor
    # a top-level help flag, inject it. Done here rather than by putting the shared
    # required args on the top parser, which would make argparse demand them before
    # the subcommand token and reject `plan --crew ...`.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("plan", "build", "-h", "--help"):
        pass
    else:
        raw = ["build"] + raw

    args = parser.parse_args(raw)
    source = _source_from(args.source)
    try:
        if args.cmd == "plan":
            return _cmd_plan(args.crew, args.out, args.allow, source)
        return _cmd_build(args.crew, args.out, args.allow, source)
    except ExportRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
