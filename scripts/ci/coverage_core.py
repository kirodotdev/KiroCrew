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

Two subcommands, both called from the lane's own ``run`` script:

``exports``
    Prints the shell ``export`` line for the two variables when the run's event
    is one that switches, and nothing otherwise. The lane evaluates the output,
    so a variable is either set to its one valid value or not set at all. It is
    never set to an empty string: coverage.py reads an empty ``KC_COV_BRANCH``
    as "not a boolean" and stops.

``check LOG``
    Reads the lane's pytest log and ends the step non-zero unless the switch
    demonstrably held. Every refusal names its reason:

    * the log is missing or empty, so there is nothing to judge;
    * the environment does not carry exactly what ``exports`` gives this event
      (a lane that dropped the ``exports`` line, or set a variable by hand);
    * the switch is on and coverage.py reported ``no-sysmon``, its warning for
      "the sysmon core was asked for and another core measured instead".

All three lanes must switch together. Line data and branch data cannot be
combined (``coverage combine`` stops), so a lane that decided on its own would
turn Coverage Combine red.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

#: What a switching lane exports. The names are also what a test that starts a
#: nested measured run removes from the child's environment.
SWITCH_ENV: Mapping[str, str] = {"KC_COV_BRANCH": "false", "COVERAGE_CORE": "sysmon"}

#: The events whose runs switch. Every other event measures branch arcs.
SWITCH_EVENTS = frozenset({"pull_request", "merge_group"})

#: The slug coverage.py appends, in parentheses, to the warning it prints when the
#: requested ``sysmon`` core cannot be used and it measures with another core.
FALLBACK_MARK = "(no-sysmon)"


def switches(event: str) -> bool:
    """Whether a run of this event measures with ``sysmon`` and without branch arcs."""
    return event in SWITCH_EVENTS


def exports(event: str) -> str:
    """The shell line a lane evaluates: one ``export`` when it switches, else nothing."""
    if not switches(event):
        return ""
    return "export " + " ".join(f"{name}={value}" for name, value in SWITCH_ENV.items())


def refusal(event: str, environ: Mapping[str, str], log: Path) -> str:
    """Why this lane's measurement cannot be trusted, or ``""`` when it can."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"the pytest log {log} cannot be read ({error.__class__.__name__})"
    if not text.strip():
        return f"the pytest log {log} is empty"
    expected = dict(SWITCH_ENV) if switches(event) else {}
    actual = {name: environ[name] for name in SWITCH_ENV if name in environ}
    if actual != expected:
        return (
            f"event {event!r} needs {expected or 'neither variable set'}, "
            f"and this lane runs with {actual or 'neither variable set'}"
        )
    if expected and FALLBACK_MARK in text:
        return (
            f"coverage.py printed {FALLBACK_MARK}: the sysmon core was requested "
            "and another core measured this lane"
        )
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("exports", help="print the export line for this run's event")
    check = commands.add_parser("check", help="fail unless the switch held in this lane")
    check.add_argument("log", type=Path, help="the lane's pytest output")
    args = parser.parse_args(argv)
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if args.command == "exports":
        line = exports(event)
        if line:
            print(line)
        mode = "sysmon core, lines only" if line else "default core, branch arcs"
        print(f"coverage core for event {event!r}: {mode}", file=sys.stderr)
        return 0
    reason = refusal(event, os.environ, args.log)
    if reason:
        print(f"::error::coverage core check: {reason}")
        return 1
    print(f"coverage core check passed for event {event!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
