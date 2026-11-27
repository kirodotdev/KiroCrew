"""Choose the coverage core for a lane that feeds Coverage Combine, and prove it held.

The Linux backend shards, the IPv6 lane and the kernel lock owner lane each write
a coverage data file, and Coverage Combine merges all of them. On a pull-request
or merge-queue run they measure with coverage.py's ``sysmon`` core, which costs
far less than the C tracer. On CPython 3.12 that core records executed lines and
no branch arcs, so the same lanes turn branch measurement off through
``KC_COV_BRANCH`` (read by ``[coverage:run] branch`` in ``setup.cfg``). Coverage
Gate reads the line rate and the per-file line verdict, which line data carries
in full. A push to ``main`` or a release branch sets neither variable and
records branch arcs with the C tracer.

The repository variable ``CI_COVERAGE_SYSMON`` turns the switch off: set to
``off`` (Settings, Secrets and variables, Actions, Variables), every event
exports nothing and a pull-request run measures as a push does. Unset, or any
other value, leaves it on. The steps hand the variable to this script in their
environment, where an unset variable arrives as an empty string.

Three subcommands, called from a lane's own ``run`` script:

``exports [--core sysmon|ctrace]``
    Prints the shell ``export`` line for the run's event when it is one that
    switches, and nothing otherwise. The lane evaluates the output, so a
    variable is either set to its one valid value or not set at all. It is
    never set to an empty string: coverage.py reads an empty ``KC_COV_BRANCH``
    as "not a boolean" and stops. ``--core ctrace`` is for the step that runs
    the ``at_recursion_limit`` tests: it leaves the core alone and turns only
    branch measurement off, so its data combines with the lane's.

``check [--core sysmon|ctrace] LOG``
    Reads the step's pytest log and ends the step non-zero unless the switch
    demonstrably held. Every refusal names its reason:

    * the log is missing or empty, so there is nothing to judge;
    * the environment does not carry exactly what ``exports`` gives this event
      and core (a step that dropped the ``exports`` line, or set a variable by
      hand);
    * the ``sysmon`` switch is on and coverage.py reported ``no-sysmon``, its
      warning for "the sysmon core was asked for and another core measured
      instead".

``marked-files``
    Prints every test file under pytest's ``testpaths`` that names the
    ``at_recursion_limit`` marker, one per line.

A step that starts its measured pytest from another script instead of its own
shell (``scripts/ci_main_red_skip.py measure``) gets the same variables from
:func:`step_env`, for the C tracer: its data file is folded into the shard's.

All three lanes must switch together. Line data and branch data cannot be
combined (``coverage combine`` stops), so a lane that decided on its own would
turn Coverage Combine red.

Why ``at_recursion_limit``. On CPython 3.12 a ``sys.monitoring`` callback is a
Python function and needs a frame of its own. A test that fills the stack to
within a few frames of the recursion limit and then runs product code leaves no
room for it: the callback fails with ``RecursionError`` at whatever instruction
it fires on, and when that is the first instruction of a ``with`` block's
cleanup the block's exit never runs. Production is not monitored, so the lock it
would have released stays held only under measurement, and the test hangs. The
C tracer needs no Python frame. So the lanes deselect the marker from the run
they measure with ``sysmon`` and run exactly those tests in a second step on the
C tracer; they are never left unrun.
"""

from __future__ import annotations

import argparse
import configparser
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: What a switching lane exports. The names are also what a test that starts a
#: nested measured run removes from the child's environment.
SWITCH_ENV: Mapping[str, str] = {"KC_COV_BRANCH": "false", "COVERAGE_CORE": "sysmon"}

#: What a step exports on a switching event, by the core it measures with. The
#: C tracer step records lines too, so its data combines with a sysmon lane's.
CORE_ENV: Mapping[str, Mapping[str, str]] = {
    "sysmon": SWITCH_ENV,
    "ctrace": {"KC_COV_BRANCH": SWITCH_ENV["KC_COV_BRANCH"]},
}

#: The pytest marker for a test that runs product code on a nearly full stack.
MARKER = "at_recursion_limit"

#: The events whose runs switch. Every other event measures branch arcs.
SWITCH_EVENTS = frozenset({"pull_request", "merge_group"})

#: The repository variable that turns the switch off, and the one value that does.
SWITCH_VARIABLE = "CI_COVERAGE_SYSMON"
SWITCH_OFF = "off"

#: The slug coverage.py appends, in parentheses, to the warning it prints when the
#: requested ``sysmon`` core cannot be used and it measures with another core.
FALLBACK_MARK = "(no-sysmon)"


def switches(event: str, switch: str = "") -> bool:
    """Whether a run of this event measures with ``sysmon`` and without branch arcs.

    ``switch`` is the repository variable's value; only ``off`` turns the switch off.
    """
    return event in SWITCH_EVENTS and switch.strip().lower() != SWITCH_OFF


def step_env(event: str, core: str = "sysmon", switch: str = "") -> dict[str, str]:
    """The variables a step measuring with ``core`` runs with: none unless the run switches."""
    return dict(CORE_ENV[core]) if switches(event, switch) else {}


def exports(event: str, core: str = "sysmon", switch: str = "") -> str:
    """The shell line a step evaluates: one ``export`` when it switches, else nothing."""
    env = step_env(event, core, switch)
    return "export " + " ".join(f"{name}={value}" for name, value in env.items()) if env else ""


def test_sources(root: Path = ROOT) -> Iterator[tuple[str, str]]:
    """Every readable ``*.py`` under pytest's ``testpaths``: (repo-relative path, text)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(root / "setup.cfg", encoding="utf-8")
    seen: set[str] = set()
    for testpath in parser.get("tool:pytest", "testpaths").split():
        for path in sorted((root / testpath).rglob("*.py")):
            name = path.relative_to(root).as_posix()
            if name in seen:
                continue
            seen.add(name)
            try:
                yield name, path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue


def marked_files(root: Path = ROOT) -> list[str]:
    """Every test file under pytest's ``testpaths`` that names the marker, repo-relative."""
    needle = f"mark.{MARKER}"
    return sorted(name for name, text in test_sources(root) if needle in text)


def refusal(
    event: str, environ: Mapping[str, str], log: Path, core: str = "sysmon", switch: str = ""
) -> str:
    """Why this step's measurement cannot be trusted, or ``""`` when it can."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"the pytest log {log} cannot be read ({error.__class__.__name__})"
    if not text.strip():
        return f"the pytest log {log} is empty"
    expected = step_env(event, core, switch)
    actual = {name: environ[name] for name in SWITCH_ENV if name in environ}
    if actual != expected:
        return (
            f"event {event!r} on the {core} core needs {expected or 'neither variable set'}, "
            f"and this step runs with {actual or 'neither variable set'}"
        )
    if expected and core == "sysmon" and FALLBACK_MARK in text:
        return (
            f"coverage.py printed {FALLBACK_MARK}: the sysmon core was requested "
            "and another core measured this lane"
        )
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("exports", help="print the export line for this run's event")
    check = commands.add_parser("check", help="fail unless the switch held in this step")
    check.add_argument("log", type=Path, help="the step's pytest output")
    for command in (export, check):
        command.add_argument("--core", choices=sorted(CORE_ENV), default="sysmon")
    commands.add_parser("marked-files", help=f"print the test files that name {MARKER}")
    args = parser.parse_args(argv)
    if args.command == "marked-files":
        print("\n".join(marked_files()))
        return 0
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    switch = os.environ.get(SWITCH_VARIABLE, "")
    if args.command == "exports":
        line = exports(event, args.core, switch)
        if line:
            print(line)
        mode = f"{args.core} core, lines only" if line else "default core, branch arcs"
        print(
            f"coverage core for event {event!r} with {SWITCH_VARIABLE}={switch!r}: {mode}",
            file=sys.stderr,
        )
        return 0
    reason = refusal(event, os.environ, args.log, args.core, switch)
    if reason:
        print(f"::error::coverage core check: {reason}")
        return 1
    print(f"coverage core check passed for event {event!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
