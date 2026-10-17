"""A fork lane's check-run write keeps gh's error in the job log.

The `fork-*.yml` lanes write their check-runs from bash with
`gh api --method POST|PATCH`. When a write fails for good, the lane prints one
bare `::warning::` that names the check-run, and the only cause an operator can
read is the line `gh` writes to stderr (`gh: <message> (HTTP <status>)`). A write
that sends stderr to /dev/null along with stdout drops that line, so the log
shows that the write failed and never why. Stdout stays discarded: on an HTTP
error `gh` prints the response's JSON body there, which the log does not need.

Only writes are covered. A `--method GET` read decides for itself what a failed
lookup means, so its stderr handling is out of scope here.

Two other `2>&1` in these lanes capture stderr INTO a variable the step reads
next. They are the opposite shape and must keep it, so they are pinned below.
"""

from __future__ import annotations

import pathlib

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: Each spelling that sends a command's stderr to /dev/null.
STDERR_DISCARDS = (">/dev/null 2>&1", "2>/dev/null", "&>/dev/null")

#: How every check-run write in these lanes names its method.
WRITE_METHODS = ("--method POST", "--method PATCH")

#: Lanes whose finalize writes this rule must see. A lane listed here with no
#: check-run write found means the selector stopped matching it.
FINALIZING_LANES = (
    "fork-internal-content-scan.yml",
    "fork-security-scope-review.yml",
    "fork-workflow-guard.yml",
)


def _commands(path: pathlib.Path) -> list[str]:
    """Every logical shell line of every `run:` body, backslash continuations joined."""
    # YAML 1.1 parses a bare `on:` key as the boolean True, so never index "on".
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    commands: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            pending = ""
            for line in str(step.get("run") or "").splitlines():
                if line.endswith("\\"):
                    pending += line[:-1] + " "
                    continue
                commands.append(pending + line)
                pending = ""
            if pending:
                commands.append(pending)
    return commands


def _check_run_writes(path: pathlib.Path) -> list[str]:
    return [
        cmd
        for cmd in _commands(path)
        if "gh api" in cmd and "/check-runs" in cmd and any(m in cmd for m in WRITE_METHODS)
    ]


def test_no_fork_check_run_write_discards_its_stderr() -> None:
    lanes = sorted(WORKFLOWS.glob("fork-*.yml"))
    assert lanes, "no fork-*.yml lanes found -- the glob or the layout moved"
    writes = {path.name: _check_run_writes(path) for path in lanes}

    for name in FINALIZING_LANES:
        assert writes.get(name), f"{name}: no `gh api --method POST|PATCH .../check-runs` found"
    offenders = [
        f"{name}: {' '.join(cmd.split())}"
        for name, cmds in writes.items()
        for cmd in cmds
        if any(discard in cmd for discard in STDERR_DISCARDS)
    ]
    assert not offenders, "a check-run write sends gh's error to /dev/null:\n  " + "\n  ".join(
        offenders
    )


def _only(commands: list[str], *fragments: str) -> str:
    matches = [cmd for cmd in commands if all(f in cmd for f in fragments)]
    assert len(matches) == 1, f"expected one command with {fragments!r}, found {matches!r}"
    return matches[0].strip()


def test_the_two_stderr_captures_keep_capturing() -> None:
    """Each capture folds stderr into the variable its step reads next.

    The workflow guard tells an absent label (HTTP 404, nothing to strip) from a
    failed removal by grepping the captured `err`; without `2>&1` that variable
    is empty, every 404 reads as a failure and the guard blocks the new head. The
    scope lane quotes the captured text as git's error when it cannot measure the
    review surface.
    """
    guard = _commands(WORKFLOWS / "fork-workflow-guard.yml")
    strip = _only(guard, "gh api --method DELETE", "/labels/$OVERRIDE_LABEL")
    assert strip.startswith('err="$(') and strip.endswith('2>&1 >/dev/null)" || rc=$?'), strip

    scope = _commands(WORKFLOWS / "fork-security-scope-review.yml")
    surface = _only(scope, "git diff --name-only", "surface[@]")
    assert surface.startswith('touched="$(') and surface.endswith('2>&1)" || rc=$?'), surface
