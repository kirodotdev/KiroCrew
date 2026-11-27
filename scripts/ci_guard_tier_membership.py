#!/usr/bin/env python3
"""Show what a change does to the guard tier, and refuse a silent exit from it.

The guard tier (`scripts/ci_test_map.py guard-tier`) is the set of tests that
read the test or source tree as data. It runs on every change, so it is the
floor under diff-based test selection, and it is computed from each test
file's text. An edit can therefore take a file out of the tier without anyone
deciding that it should leave.

This compares the tier of the working tree with the tier of the commit the
change is measured against (`scripts/ratchet_scope.py` `change_merge_base`):

* a file that JOINS the tier is reported, and never fails;
* a file that LEAVES the tier because it was deleted or renamed is reported;
* a file that leaves the tier while it still exists FAILS, unless that file
  says so itself, on a line of its own:

      # guard-tier: not a member -- <why this file no longer reads a tree>

  The reason is at least 20 characters. The line lives in the file that left,
  so it is part of the diff a reviewer reads;
* a file that carries that line and IS in the tier fails too, so the line
  cannot be planted ahead of the edit it would excuse.

Both sides come from one change and its own base, so nothing another change
merges can alter the verdict. The base is read by the base's own copy of the
rule, so a change to the rule is held to the same test as a change to a file.

Exit codes: 0 nothing unexplained, 1 a file left unexplained or a stale line,
2 the comparison could not be made.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ci_test_map  # noqa: E402  (path set immediately above)
import ratchet_scope  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The line a test file carries to say it left the tier on purpose.
NOT_A_MEMBER = re.compile(r"^# guard-tier: not a member -- (\S.{19,})$", re.M)

#: GitHub Actions keeps at most ten error annotations from one step.
MAX_ANNOTATIONS = 10


class Verdict(NamedTuple):
    """What one change does to the tier."""

    joined: list[str]
    gone: list[str]
    excused: dict[str, str]
    unexplained: list[str]
    stale: list[str]

    @property
    def failed(self) -> bool:
        return bool(self.unexplained or self.stale)


def _reason(path: Path) -> str | None:
    """The reason on *path*'s not-a-member line, or None when it has none."""
    try:
        found = NOT_A_MEMBER.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return found.group(1).strip() if found else None


def judge(base: Iterable[str], head: Iterable[str], root: Path) -> Verdict:
    """Compare the tier at the base with the tier at *root*, the tree under review."""
    before, after = set(base), set(head)
    gone: list[str] = []
    excused: dict[str, str] = {}
    unexplained: list[str] = []
    for path in sorted(before - after):
        if not (root / path).is_file():
            gone.append(path)
            continue
        reason = _reason(root / path)
        if reason is None:
            unexplained.append(path)
        else:
            excused[path] = reason
    stale = [path for path in sorted(after) if _reason(root / path) is not None]
    return Verdict(sorted(after - before), gone, excused, unexplained, stale)


def _command_text(text: str) -> str:
    """*text* as workflow-command data: a file name must not start a command of its own."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _command_property(text: str) -> str:
    return _command_text(text).replace(":", "%3A").replace(",", "%2C")


def _cell(text: str) -> str:
    """*text* as one Markdown table cell."""
    flat = " ".join(text.split())
    return flat.replace("|", "\\|").replace("`", "'").replace("<", "&lt;")


def workflow_commands(verdict: Verdict) -> list[str]:
    """Workflow commands for the run log: one notice, and an error for each failure."""
    lines: list[str] = []
    if verdict.joined:
        lines.append(
            f"::notice title=Guard tier::{len(verdict.joined)} test file(s) joined the "
            "guard tier; the job summary lists them."
        )
    failures = [
        (
            path,
            "left the guard tier but still exists. Restore what made it a tree-reading "
            "test, or add the line `# guard-tier: not a member -- <reason>` to it.",
        )
        for path in verdict.unexplained
    ] + [
        (
            path,
            "is in the guard tier and carries a `# guard-tier: not a member` line. "
            "Remove the line.",
        )
        for path in verdict.stale
    ]
    for path, message in failures[:MAX_ANNOTATIONS]:
        lines.append(
            f"::error file={_command_property(path)},title=Guard tier::"
            f"{_command_text(path)} {message}"
        )
    if len(failures) > MAX_ANNOTATIONS:
        lines.append(
            f"::error title=Guard tier::{len(failures) - MAX_ANNOTATIONS} more file(s) "
            "failed; the job summary lists every one."
        )
    return lines


def summary(verdict: Verdict, base_count: int, head_count: int, base_label: str) -> str:
    """The Markdown job summary: the counts, then every file that moved."""
    rows = (
        [(path, "left, unexplained (fails)") for path in verdict.unexplained]
        + [(path, "carries a not-a-member line but is a member (fails)") for path in verdict.stale]
        + [(path, f"left on purpose: {reason}") for path, reason in verdict.excused.items()]
        + [(path, "left: deleted or renamed") for path in verdict.gone]
        + [(path, "joined") for path in verdict.joined]
    )
    out = [
        "### Guard tier membership",
        "",
        f"{base_count} file(s) at {_cell(base_label)}, {head_count} with this change.",
        "",
    ]
    if not rows:
        out.append("No file joined or left the tier.")
    else:
        out += ["| Test file | Change |", "|---|---|"]
        out += [f"| `{_cell(path)}` | {_cell(change)} |" for path, change in rows]
    return "\n".join(out) + "\n"


def base_tier(base: str, root: Path) -> tuple[list[str], str]:
    """The tier of commit *base* and whose rule decided it.

    The base's own `guard-tier` command reads an export of the base's `test/`
    tree, so a change that alters the rule itself is measured against the rule
    it replaces: the files it drops show up as having left. A base from before
    that command existed is read with this checkout's rule instead.
    """
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", base, "scripts", "test"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="guard-tier-base-") as scratch:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(scratch, filter="data")
        command = [sys.executable, str(Path(scratch, "scripts", "ci_test_map.py"))]
        own = subprocess.run(
            [*command, "--root", scratch, "guard-tier"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if own.returncode == 0 and own.stdout.strip():
            return own.stdout.split(), "the base's own rule"
        tier = ci_test_map.tier_lines(ci_test_map.guard_tier(Path(scratch)), None)
        return tier, "this checkout's rule, as the base has no guard-tier command"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root")
    parser.add_argument(
        "--base-tier", help="file holding the base tier, one path per line, instead of asking git"
    )
    parser.add_argument("--summary", help="file to append the Markdown summary to")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.base_tier:
            base = Path(args.base_tier).read_text(encoding="utf-8").split()
            label = "the given base tier"
        else:
            commit, how = ratchet_scope.change_merge_base("GUARD_TIER_BASE", cwd=root)
            if commit is None:
                print(f"error: no base to compare the guard tier with: {how}", file=sys.stderr)
                return 2
            base, rule = base_tier(commit, root)
            label = f"{how}, by {rule}"
        head = ci_test_map.tier_lines(ci_test_map.guard_tier(root), ci_test_map.tracked_tests(root))
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    verdict = judge(base, head, root)
    for line in workflow_commands(verdict):
        print(line)
    text = summary(verdict, len(base), len(head), label)
    # Printed as well as appended: the run log is what an API reader can fetch.
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return 1 if verdict.failed else 0


if __name__ == "__main__":
    sys.exit(main())
