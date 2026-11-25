#!/usr/bin/env python3
"""Size the red-cause census's GraphQL budget to the bucket the token has.

`ci-red-census.yml` spends GitHub GraphQL points from a bucket every workflow of
the repository shares, and its size is the token's, not a constant: the Actions
token of this repository has 10,000 points an hour. Before the census spends
anything this reads the bucket and says what the census may do with it:

    python3 scripts/ci_red_census_budget.py --needs-points 3000 --workers 4

It reads `GET /rate_limit` through `gh api`, which costs nothing, and from the
`graphql` bucket's size and what is left of it gives the census two numbers:

    reserve   half the bucket, rounded up, and room for the calls in flight.
              The census stops before a call that would start with less
              (`--reserve-points`), so the other workflows keep half an hour's
              bucket whatever the census does.
    most      two fifths of the bucket, rounded down, less the same room. The
              census spends no more than this in one run (`--max-points`).

The room is one call for each worker, at the dearest query's price: the census
checks its budget before a call and learns the cost after it, so the calls
already sent when it stops land past the line.

It refuses, and nothing is read, when the read would not fit:

- two fifths of the bucket hold less than ``--needs-points`` (what the window is
  expected to cost): the bucket is too small for the census at all;
- what is left, above the reserve, is less than ``--needs-points``: other jobs
  have spent this hour's bucket, and the next hour has a new one;
- the bucket cannot be read.

A refusal exits 1. Otherwise the two numbers are printed, written to the file
`GITHUB_OUTPUT` names as `reserve` and `most`, and the exit is 0.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: S404 - fixed argv, no shell
import sys
from typing import Callable, NamedTuple

# The dearest query the census sends: ten check suites asked for 100 red jobs
# each, at the 3 points a suite one day of this repository measured.
MOST_POINTS_A_CALL = 30
MAX_WORKERS = 8
GH_TIMEOUT_SECONDS = 900


class Budget(NamedTuple):
    """What the census may do: stop under ``reserve`` left, spend ``most`` at most."""

    reserve: int
    most: int


class Refusal(NamedTuple):
    reason: str


def room_for(workers: int) -> int:
    """The points the calls in flight can cost when the census stops."""
    return workers * MOST_POINTS_A_CALL


def size(limit: int, remaining: int, needs: int, workers: int) -> Budget | Refusal:
    """The budget for a bucket of ``limit`` points with ``remaining`` left, or why not.

    A budget is given only when a read of ``needs`` points fits both under
    ``most`` and above ``reserve``, so a census that costs what it was expected
    to never reaches either line.
    """
    room = room_for(workers)
    reserve = -(-limit // 2) + room
    most = limit * 2 // 5 - room
    if most < needs:
        return Refusal(
            f"a bucket of {limit} points an hour lets the census spend {max(most, 0)} "
            f"(two fifths, less {room} for calls in flight), and this read needs {needs}"
        )
    if remaining - reserve < needs:
        return Refusal(
            f"{remaining} of {limit} points are left this hour; the census leaves {reserve} "
            f"and this read needs {needs}"
        )
    return Budget(reserve, most)


GhRunner = Callable[[list[str]], tuple[int, str]]


def run_gh(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["gh", *argv],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout


def _points(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def read_bucket(run: GhRunner) -> tuple[int, int] | None:
    """``(limit, remaining)`` of the token's GraphQL bucket, or ``None`` when unreadable."""
    code, out = run(["api", "rate_limit"])
    if code != 0:
        return None
    try:
        body = json.loads(out)
    except ValueError:
        return None
    resources = body.get("resources") if isinstance(body, dict) else None
    bucket = resources.get("graphql") if isinstance(resources, dict) else None
    if not isinstance(bucket, dict):
        return None
    limit, remaining = _points(bucket.get("limit")), _points(bucket.get("remaining"))
    if limit is None or remaining is None or remaining > limit:
        return None
    return limit, remaining


def _count(low: int, high: int | None = None) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            value = low - 1
        if value < low or (high is not None and value > high):
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number in range")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--needs-points",
        required=True,
        type=_count(1),
        help="what the window is expected to cost: about 3,000 for a weekday",
    )
    parser.add_argument(
        "--workers",
        required=True,
        type=_count(1, MAX_WORKERS),
        help="the census's --workers: one call each may be in flight when it stops",
    )
    return parser


def main(
    argv: list[str] | None = None,
    run: GhRunner = run_gh,
    environ: dict[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    environ = dict(os.environ) if environ is None else environ
    bucket = read_bucket(run)
    if bucket is None:
        print("Refused: the GraphQL bucket of this token could not be read. Nothing was read.")
        return 1
    limit, remaining = bucket
    print(f"GraphQL bucket of this token: {limit} points an hour, {remaining} left.")
    budget = size(limit, remaining, args.needs_points, args.workers)
    if isinstance(budget, Refusal):
        print(f"Refused: {budget.reason}. Nothing was read.")
        return 1
    print(
        f"Budget: the census spends {budget.most} points at most and stops "
        f"before a call that would start with under {budget.reserve} left."
    )
    target = environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"reserve={budget.reserve}\nmost={budget.most}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
