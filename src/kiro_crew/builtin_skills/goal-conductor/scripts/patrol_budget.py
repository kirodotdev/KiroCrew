#!/usr/bin/env python3
"""Patrol budget - the one code owner of the conductor's loop bounds.

A conductor picks ``interval_secs``, ``max_cycles`` and ``max_runtime_secs``
when it arms its patrol. Two mistakes recur when the model picks them from
prose: a long interval with few cycles (30 min x 24 cycles ends after half a
day while work is still live), and letting the cap run out instead of renewing
it (a spent loop deactivates, so the conductor never gets the turn in which it
could have called ``monitor_update``). This script makes both calls.

Usage:
    python3 patrol_budget.py check --interval-secs I --max-cycles C \\
        --max-runtime-secs R
    python3 patrol_budget.py renew --line '<the [patrol budget: ...] line>' \\
        --base-cycles B --base-runtime-secs T --open-items K

Each mode prints one JSON object on stdout. Stdlib only.

``check`` - run BEFORE ``monitor_start`` / ``monitor_update``.
    Rule: ``interval x max_cycles >= max_runtime_secs``, so the time budget - not the
    cycle count - is what ends the loop, and ``interval <= 10% of max_runtime_secs``,
    so a cycle always lands in the renewal window. On failure ``suggest`` carries bounds
    that pass.
    Exit: 0 ok, 20 refused (use ``suggest``), 2 bad arguments.

``renew`` - run only on a cycle whose budget line ends ``10% or less left``.
    Reads what is left from the header the gateway already prints, so the
    renewal count comes from the loop record itself, never from model memory:
    renewals so far = how many base budgets were already added on top of the
    first one. When at most 10% of cycles or runtime is left and live items
    remain, it prints the new bounds for ``monitor_update`` - one more full base
    budget each. A renewal is all or nothing: when a full base budget would pass
    1000 cycles or 7 days it refuses (exit 30) rather than add a partial one,
    because a partial one breaks the ``check`` rule and would never count as a
    renewal. The base bounds passed ``check``, so adding a whole base keeps
    ``interval x cycles >= runtime`` true.
    Exit: 0 renew now (pass ``max_cycles`` / ``max_runtime_secs`` to
    ``monitor_update``), 10 not yet, 20 no open items (stop normally),
    30 renewal cap reached (3 renewals, 1000 cycles or 7 days: stop and
    hand the decision to the user),
    2 bad arguments.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from fractions import Fraction

#: Server-side bounds (``validation.py`` FieldSpec for monitor_start/update).
SERVER_MAX_CYCLES = 1000
SERVER_MIN_INTERVAL_SECS = 15
SERVER_MAX_INTERVAL_SECS = 86400

#: Longest total runtime a conductor may give itself, renewals included: 7 days.
#: Tighter than the server's own ceiling on purpose; past it the user decides.
POLICY_MAX_RUNTIME_SECS = 604800

#: Renew when at most this share of either budget is left.
RENEW_THRESHOLD = 0.10
_SHARE = Fraction(RENEW_THRESHOLD).limit_denominator(1000)

#: Renewals allowed on top of the first budget before the user must decide.
MAX_RENEWALS = 3

_CYCLES_RE = re.compile(r"cycle (\d+)/(\d+)")
_RUNTIME_RE = re.compile(r"(\d+)s/(\d+)s runtime left")


def _within_share(part: int, whole: int) -> bool:
    """``part <= RENEW_THRESHOLD * whole`` in integers, so no size of int overflows."""
    return part * _SHARE.denominator <= whole * _SHARE.numerator


def _base_count(extra: int, base: int) -> int:
    """How many whole bases ``extra`` holds, rounded to nearest, in integers."""
    return (2 * extra + base) // (2 * base)


def _emit(obj: dict, code: int) -> int:
    print(json.dumps(obj, sort_keys=True))
    return code


def check(interval: int, cycles: int, runtime: int) -> tuple[dict, int]:
    """Validate one set of loop bounds; suggest a passing set when refused."""
    problems: list[str] = []
    good_interval = min(max(interval, SERVER_MIN_INTERVAL_SECS), SERVER_MAX_INTERVAL_SECS)
    if interval != good_interval:
        problems.append(
            f"interval_secs must be {SERVER_MIN_INTERVAL_SECS}..{SERVER_MAX_INTERVAL_SECS}"
        )
    if not 1 <= runtime <= POLICY_MAX_RUNTIME_SECS:
        problems.append(f"max_runtime_secs must be 1..{POLICY_MAX_RUNTIME_SECS}")
    good_runtime = min(max(runtime, 1), POLICY_MAX_RUNTIME_SECS)
    if not 1 <= cycles <= SERVER_MAX_CYCLES:
        problems.append(f"max_cycles must be 1..{SERVER_MAX_CYCLES}")
    if interval * cycles < runtime:
        problems.append(
            "interval_secs x max_cycles must cover max_runtime_secs, so time ends "
            "the loop before the cycle count does"
        )
    if not _within_share(interval, runtime):
        problems.append(
            "interval_secs must fit in the last 10% of max_runtime_secs, or no "
            "cycle lands in the renewal window"
        )
    clamped_cycles = min(max(cycles, 1), SERVER_MAX_CYCLES)
    if not _within_share(good_interval, good_runtime):
        # Keep the operator's runtime; shorten the interval to fit the window.
        good_interval = max(SERVER_MIN_INTERVAL_SECS, int(RENEW_THRESHOLD * good_runtime))
        good_runtime = max(good_runtime, math.ceil(good_interval / RENEW_THRESHOLD))
    good_cycles = max(clamped_cycles, math.ceil(good_runtime / good_interval))
    if good_cycles > SERVER_MAX_CYCLES:
        # Even the server's cycle ceiling cannot cover this runtime at this
        # interval: shorten the runtime. `renew` cannot add a full base on top
        # of 1000 cycles, so at `10% or less left` it hands the decision to the user.
        good_cycles = SERVER_MAX_CYCLES
        good_runtime = SERVER_MAX_CYCLES * good_interval
    result = {
        "ok": not problems,
        "problems": problems,
        "suggest": {
            "interval_secs": good_interval,
            "max_cycles": good_cycles,
            "max_runtime_secs": good_runtime,
        },
    }
    return result, 0 if not problems else 20


def parse_line(line: str) -> dict:
    """Read the gateway's ``[patrol budget: ...]`` line into numbers."""
    out: dict = {}
    m = _CYCLES_RE.search(line)
    if m:
        out["cycle"], out["max_cycles"] = int(m.group(1)), int(m.group(2))
    m = _RUNTIME_RE.search(line)
    if m:
        out["runtime_left"], out["max_runtime_secs"] = int(m.group(1)), int(m.group(2))
    return out


def renew(budget: dict, base_cycles: int, base_runtime: int, open_items: int) -> tuple[dict, int]:
    """Decide whether this cycle renews the loop, and with what bounds."""
    if open_items <= 0:
        return {"action": "stop", "why": "no open items; stop normally"}, 20
    max_cycles = budget.get("max_cycles", 0)
    max_runtime = budget.get("max_runtime_secs", 0)
    low = False
    if max_cycles:
        cycles_left = max(0, max_cycles - budget["cycle"])
        low = low or _within_share(cycles_left, max_cycles)
    if max_runtime:
        low = low or _within_share(budget["runtime_left"], max_runtime)
    if not low:
        return {"action": "wait", "why": "more than 10% of the budget is left"}, 10
    renewals = 0
    if max_cycles and base_cycles:
        renewals = max(renewals, _base_count(max_cycles - base_cycles, base_cycles))
    if max_runtime and base_runtime:
        renewals = max(renewals, _base_count(max_runtime - base_runtime, base_runtime))
    new_cycles = max_cycles + base_cycles if max_cycles else 0
    new_runtime = max_runtime + base_runtime if max_runtime else 0
    at_ceiling = new_cycles > SERVER_MAX_CYCLES or new_runtime > POLICY_MAX_RUNTIME_SECS
    if renewals >= MAX_RENEWALS or at_ceiling:
        return {
            "action": "ask_user",
            "renewals": renewals,
            "why": f"renewal cap reached ({MAX_RENEWALS} renewals, 1000 cycles or 7 days)",
        }, 30
    update: dict = {}
    if max_cycles:
        update["max_cycles"] = new_cycles
    if max_runtime:
        update["max_runtime_secs"] = new_runtime
    return {"action": "renew", "renewal": renewals + 1, "monitor_update": update}, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("check")
    c.add_argument("--interval-secs", type=int, required=True)
    c.add_argument("--max-cycles", type=int, required=True)
    c.add_argument("--max-runtime-secs", type=int, required=True)
    r = sub.add_parser("renew")
    r.add_argument("--line", required=True)
    r.add_argument("--base-cycles", type=int, required=True)
    r.add_argument("--base-runtime-secs", type=int, required=True)
    r.add_argument("--open-items", type=int, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.mode == "check":
        return _emit(*check(args.interval_secs, args.max_cycles, args.max_runtime_secs))
    budget = parse_line(args.line)
    if not budget:
        return _emit({"action": "error", "why": "no budget figures found in --line"}, 2)
    return _emit(*renew(budget, args.base_cycles, args.base_runtime_secs, args.open_items))


if __name__ == "__main__":
    sys.exit(main())
