#!/usr/bin/env python3
"""Per-item credit spend — the deterministic half of the conductor's budget check.

Sums the credits a work item's sessions have burned, from the gateway's usage
shards, and answers "is this item inside its budget?" as data. The conductor
never estimates spend from a transcript; it reads this script's verdict.

Usage:
    python3 credit_spend.py --slots KEY[,KEY...] [--budget 100]
                            [--usage-dir PATH] [--max-shards N]

    --slots      the item's session slot keys (the current session plus any
                 retries), comma-separated
    --budget     the item's allowance in credits; omit for a plain rollup
    --usage-dir  default: <data home>/usage/tokens  (data home =
                 $KIROCREW_HOME, else ~/.kiro/crew)
    --max-shards newest daily shards scanned. Default: ALL retained shards —
                 a cost bound is opt-in, and a bounded scan that skipped older
                 shards never claims ``within`` (see verdicts)

stdout (JSON):
    {"slots": {"<key>": {"credits": 12.4, "turns": 9}},
     "total_credits": 12.4, "shards_scanned": 3, "truncated": false,
     "budget": 100.0, "remaining": 87.6, "verdict": "within"}

``verdict``: ``exhausted`` (total >= budget — monotone: more shards or missing
meters can only ADD spend, so it stands even on a partial view) | ``unmetered``
(ANY watched slot had no shard row — the caller must treat spend as unknown,
not as zero: today's shards are written by the dashboard chat runner, so a
session outside it burns invisibly) | ``truncated`` (the under-budget scan is
incomplete: older shards were skipped, a shard was unreadable, or a matched
row was corrupt — spend outside the readable view could flip the answer) |
``within`` (complete scan, every slot metered, under budget).

Exit code: 0 when the rollup ran (verdict carries the outcome); 2 on malformed
arguments. Reads only; writes nothing; no subprocess.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path


def data_home() -> Path:
    env = os.environ.get("KIROCREW_HOME")
    return Path(env) if env else Path.home() / ".kiro" / "crew"


def rollup(
    slots: list[str], usage_dir: Path, max_shards: int | None
) -> tuple[dict[str, dict[str, float]], int, bool]:
    """Per-slot sums, shards scanned, and whether older shards were SKIPPED.

    ``max_shards`` is a cost bound the caller opts into (None = scan every
    retained shard, the default). Truncation is reported, never silent: spend
    older than the window would otherwise vanish from the total and turn a
    genuinely exhausted item back into ``within``.
    """
    per_slot: dict[str, dict[str, float]] = {key: {"credits": 0.0, "turns": 0.0} for key in slots}
    wanted = set(slots)
    seen_any = {key: False for key in slots}
    all_shards = sorted(usage_dir.glob("*.jsonl"), reverse=True)
    shards = all_shards if max_shards is None else all_shards[:max_shards]
    truncated = len(shards) < len(all_shards)
    for shard in shards:
        try:
            lines = shard.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            # An unreadable shard means the total is INCOMPLETE. Do not crash
            # (the readable shards still inform the monotone `exhausted` case),
            # but the scan may no longer claim completeness: flag it like a
            # truncation so an under-budget answer is never reported `within`.
            truncated = True
            continue
        for line in lines:
            if not line.strip():
                continue  # a blank line is not a torn row
            try:
                row = json.loads(line)
            except ValueError:
                # A torn row (append interrupted mid-write) means the total is
                # INCOMPLETE: readable rows still inform the monotone
                # `exhausted` case, but flag like a truncation so an
                # under-budget answer is never reported `within`.
                truncated = True
                continue
            if not isinstance(row, dict) or row.get("_type") != "tokens":
                continue
            slot = row.get("slot")
            if slot not in wanted:
                continue
            seen_any[slot] = True
            bucket = per_slot[slot]
            # One accepted token row IS one turn: production rows are written
            # per turn with a literal ``turns: 0`` field (measured on real
            # shards), so summing the field would report zero turns for every
            # active session. Count rows; sum only the spend.
            bucket["turns"] += 1
            value = row.get("credits")
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
                bucket["credits"] += float(value)
            else:
                # A matched tokens row whose credits are missing, non-finite,
                # or negative is a CORRUPT row, not a free one: silently
                # dropping it understates spend on a scan still claiming to be
                # complete. Degrade like a truncation.
                truncated = True
    for key, seen in seen_any.items():
        if not seen:
            per_slot[key]["unmetered"] = 1.0
    return per_slot, len(shards), truncated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slots", required=True)
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--usage-dir", default=None)
    parser.add_argument(
        "--max-shards",
        type=int,
        default=None,
        help="newest daily shards to scan (cost bound; default: ALL retained shards)",
    )
    args = parser.parse_args(argv)
    slots = [item.strip() for item in args.slots.split(",") if item.strip()]
    if not slots:
        print("no slot keys given", file=sys.stderr)
        return 2
    if args.budget is not None and (not math.isfinite(args.budget) or args.budget <= 0):
        # NaN compares false against everything, so a NaN budget would fall
        # through every branch and print `within`. Malformed input is exit 2.
        print(f"budget must be a finite positive number, got {args.budget}", file=sys.stderr)
        return 2
    usage_dir = Path(args.usage_dir) if args.usage_dir else data_home() / "usage" / "tokens"
    max_shards = None if args.max_shards is None else max(args.max_shards, 1)
    per_slot, scanned, truncated = rollup(slots, usage_dir, max_shards)
    total = sum(bucket["credits"] for bucket in per_slot.values())
    out: dict[str, object] = {
        "slots": per_slot,
        "total_credits": round(total, 4),
        "shards_scanned": scanned,
        "truncated": truncated,
    }
    if args.budget is not None:
        out["budget"] = args.budget
        out["remaining"] = round(args.budget - total, 4)
        if total >= args.budget:
            # Monotone: more shards (or the missing meters) can only ADD
            # spend, so exhaustion stands even on a partial view.
            out["verdict"] = "exhausted"
        elif any("unmetered" in bucket for bucket in per_slot.values()):
            # ANY unmetered slot makes an under-budget answer unknowable:
            # the metered slots' spend is real, but the unmetered one could
            # hold anything. `within` on mixed metering would skip the burn
            # review exactly when spend is least visible.
            out["verdict"] = "unmetered"
        elif truncated:
            # Under-budget on a partial view is NOT a verdict: spend older
            # than the window could flip it. Say so instead of guessing.
            out["verdict"] = "truncated"
        else:
            out["verdict"] = "within"
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
