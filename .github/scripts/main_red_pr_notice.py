#!/usr/bin/env python3
"""Main-red PR notice: which failing tests of a pull request are already red on main.

`main-red-pr-notice.yml` runs this after a failed `CI` run of a same-repository
pull request, when `main-red-watch.yml` has an open `Main is red:` issue. The
node ids come from `main_red_nodeids.py`, which this file loads from beside it.

    notice     write the one comment text, or exit 11 when no failing test of the
               pull request is also red on main
    last-run   print the id of the red main run whose failing set the issue holds

A failing test counts as "same as main" only when all four hold: its node id is in
the open issue's set, its message prefix equals the one on main, the pull request
does not change its test file, and the pull request's base contains the commit of
the red main run that supplied that set (the issue's last red run). Any other
failure is listed as the author's own. The text never says the pull request is
fine, and a check that names no test is always the author's.

Log text is cut to printable ASCII, has its backticks removed and sits in a code span,
so it cannot mention anyone or break the comment. The issue body is editable, so only
its checked state record is read; the fix line is rebuilt from the issue timeline with
every `@` removed, and the event's SHA and run URL are matched before they are printed.
No pattern here runs over log or issue text: the parsing is D's one-pass code.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

MARKER = "main-red-pr-notice"
NOTHING_TO_SAY = 11
MAX_LISTED = 20


def _sibling() -> Any:
    path = Path(__file__).resolve().with_name("main_red_nodeids.py")
    spec = importlib.util.spec_from_file_location("main_red_nodeids", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    keep = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = keep
    return module


watch = _sibling()

_SHA = re.compile(r"[0-9a-f]{7,40}")
_RUN_URL = re.compile(r"https://github\.com/[A-Za-z0-9_./-]{1,200}")


def test_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def classify(
    pr_failures: dict[str, str],
    main_failures: dict[str, str],
    changed_files: set[str] | None,
    base_has_red_commit: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    """``(same as main, the author's own)``, each ``{node id: message prefix}``.

    ``changed_files`` is ``None`` when the list is unknown or cut short, which
    makes every failure the author's.
    """
    same: dict[str, str] = {}
    own: dict[str, str] = {}
    for nodeid, message in pr_failures.items():
        is_same = (
            base_has_red_commit
            and changed_files is not None
            and nodeid in main_failures
            and main_failures[nodeid] == message
            and test_file(nodeid) not in changed_files
        )
        (same if is_same else own)[nodeid] = message
    return same, own


def fix_line(timeline_rows: list[dict[str, Any]] | None, repo: str, issue_number: int) -> str:
    """The `Fix in progress:` line, rebuilt from the issue's own timeline, no mention.

    The issue body is editable, so its copy of this line is not read. ``None``
    rows mean the timeline could not be read.
    """
    if timeline_rows is None:
        return "Fix in progress: unknown."
    line = watch.fix_line(watch.fix_prs(timeline_rows, repo), issue_number)
    return watch.clean(line.replace("@", ""), 300)


def run_link(sha: str, run_url: str) -> str:
    """``[sha](url)`` from event values, each checked; a value that fails is left out."""
    label = sha[:12] if _SHA.fullmatch(sha) else "this run"
    return f"[{label}]({run_url})" if _RUN_URL.fullmatch(run_url) else label


def _listing(rows: dict[str, str]) -> list[str]:
    lines = [f"- `{nodeid}`" + (f": `{msg}`" if msg else "") for nodeid, msg in rows.items()]
    lines = lines[:MAX_LISTED]
    if len(rows) > MAX_LISTED:
        lines.append(f"- and {len(rows) - MAX_LISTED} more")
    return lines


def render_comment(
    same: dict[str, str],
    own: dict[str, str],
    main_failures: dict[str, str],
    issue_number: int,
    fix: str,
    sha: str,
    run_url: str,
) -> str:
    lines = [
        f"<!-- {MARKER} -->",
        f"Main is red on {len(main_failures)} test(s), tracked in #{issue_number}. {fix}",
        "",
        f"As of CI run {run_link(sha, run_url)} of this pull request:",
        "",
    ]
    if same:
        lines += ["Failing here and on main with the same message start:", ""]
        lines += _listing(same) + [""]
    if own:
        lines += [
            "Failing here and not shown to be on main. Treat these as yours "
            "(a test this pull request changes, a different message, or a base "
            "does not contain the main commit that last reported main red counts here):",
            "",
        ]
        lines += _listing(own) + [""]
    lines += [
        "A failing check that names no test (lint, a gate) is always yours. "
        "This comment changes no check result and is rewritten on the next failed run.",
        "",
    ]
    return "\n".join(lines)


def _read_text(path: str | None) -> str:
    if not path or not Path(path).is_file():
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def cmd_notice(args: argparse.Namespace) -> int:
    state = watch.parse_state(_read_text(args.issue_body))
    if state is None:
        print("The open issue has no readable main-red record; writing nothing.")
        return NOTHING_TO_SAY
    main_failures: dict[str, str] = {str(k): str(v) for k, v in state["failures"].items()}
    pr_failures = json.loads(_read_text(args.pr_failures) or "{}")
    if not set(pr_failures) & set(main_failures):
        print("No failing test of this pull request is red on main; writing nothing.")
        return NOTHING_TO_SAY
    changed: set[str] | None = None
    if not args.changed_unknown:
        changed = {ln.strip() for ln in _read_text(args.changed_files).splitlines() if ln.strip()}
    same, own = classify(pr_failures, main_failures, changed, args.base_ok == "1")
    rows = watch.read_json_values(_read_text(args.timeline)) if args.timeline else None
    fix = fix_line(rows, args.repo, args.issue_number)
    text = render_comment(same, own, main_failures, args.issue_number, fix, args.sha, args.run_url)
    Path(args.out).write_text(text, encoding="utf-8")
    return 0


def cmd_last_run(args: argparse.Namespace) -> int:
    state = watch.parse_state(_read_text(args.issue_body))
    if state is None or not str(state["last_run_id"]).isdigit():
        return 1
    print(int(state["last_run_id"]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    last = sub.add_parser("last-run")
    last.add_argument("issue_body")
    last.set_defaults(run=cmd_last_run)
    notice = sub.add_parser("notice")
    notice.add_argument("--issue-body", required=True)
    notice.add_argument("--issue-number", type=int, required=True)
    notice.add_argument("--pr-failures", required=True)
    notice.add_argument("--repo", required=True)
    notice.add_argument("--timeline")
    notice.add_argument("--changed-files")
    notice.add_argument("--changed-unknown", action="store_true")
    notice.add_argument("--base-ok", choices=("0", "1"), required=True)
    notice.add_argument("--sha", required=True)
    notice.add_argument("--run-url", required=True)
    notice.add_argument("--out", required=True)
    notice.set_defaults(run=cmd_notice)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
