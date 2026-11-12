"""What counting crew-log reads costs, and whether it counts right, at 4 slot sizes.

Run with the worktree venv, PYTHONPATH=src and an isolated KIROCREW_HOME:

    KIROCREW_HOME=<empty dir> KIROCREW_CREW_LOG=1 PYTHONPATH=src \\
        .venv/bin/python test/perf/crew_log_read_budget_bench.py --output out.json

The largest size has the busy host's biggest slot shape (8 units, 214,421 records, the
largest unit 154,893), with synthetic ``ledger/recorded`` entries; the others are 1/4,
1/16 and 1/64 of it. For each size it times a cold fold with counting off and on (the
arms alternate every round), then checks what the counter saw: the cold fold reads every
record with delta 0, a warm read after 25 appends reads 25 + at most 1, and a restart
resumes from the savepoint within delta + K.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, checkpoint
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import projection, read_budget

#: The busy host's biggest slot, records per unit oldest first: the largest unit as
#: measured, the other 59,528 records split by each unit's byte size.
HOST_UNITS = (9_000, 38_634, 1_444, 7, 109, 10_305, 154_893, 29)
FOLD = "ledger"
DELTA = 25


def _write(slot: str, unit_id: str, records: int, start: int = 0) -> None:
    if CrewLog.exists(lg.KIND_SESSION, unit_id):
        handle = CrewLog.open(lg.KIND_SESSION, unit_id)
    else:
        handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="o", agent="a", slot=slot)
    for at in range(0, records, 2000):
        batch = range(start + at, start + min(records, at + 2000))
        handle.append_many(
            [
                {"type": "ledger/recorded", "data": {"slot": slot, "goal": f"synthetic {i}"}}
                for i in batch
            ],
            src="gateway",
        )
    handle.release_ownership()


def _tallies() -> "list[read_budget.Tally]":
    seen: "list[read_budget.Tally]" = []
    real = read_budget._settle

    def spy(tally: read_budget.Tally) -> None:
        seen.append(tally)
        real(tally)

    read_budget._settle = spy  # type: ignore[assignment]
    return seen


def _cold(units: "list[str]", slot: str, mode: str) -> float:
    os.environ[read_budget.MODE_ENV] = (
        mode  # flake-ok: a standalone bench script that owns its process env, not a test
    )
    projection.forget_slot_folds()
    for unit_id in units:  # no savepoint, so the fold is cold every round
        handle = CrewLog.open(lg.KIND_SESSION, unit_id)
        checkpoint.discard_slot(handle, (FOLD,))
        handle.release_ownership()
    started = time.thread_time()
    projection.fold_slot_warm_revised(FOLD, units, slot=slot)
    return time.thread_time() - started


def run(rounds: int) -> "list[dict[str, object]]":
    crew_log_eager.note_commit = lambda *a, **k: None  # type: ignore[assignment]
    read_budget.mark_gateway_process()
    seen = _tallies()
    out: "list[dict[str, object]]" = []
    for divisor in (64, 16, 4, 1):
        slot = f"chat-bench-{divisor}"
        sizes = [max(1, n // divisor) for n in HOST_UNITS]
        units = [f"acp-bench-{divisor}-{i}" for i in range(len(sizes))]
        for unit_id, size in zip(units, sizes):
            _write(slot, unit_id, size)
        total = sum(sizes)
        off, on = [], []
        for _ in range(rounds):
            off.append(_cold(units, slot, read_budget.MODE_OFF))
            on.append(_cold(units, slot, read_budget.MODE_REPORT))
        cold = seen[-1]
        _write(slot, units[-1], DELTA, start=sizes[-1])
        projection.fold_slot_warm_revised(FOLD, units, slot=slot)
        warm = seen[-1]
        projection.forget_slot_folds()
        _write(slot, units[-1], DELTA, start=sizes[-1] + DELTA)
        projection.fold_slot_warm_revised(FOLD, units, slot=slot)
        restart = seen[-1]
        row = {
            "records": total,
            "largest_unit": max(sizes),
            "cold_cpu_s_off": round(statistics.median(off), 3),
            "cold_cpu_s_on": round(statistics.median(on), 3),
            "counting_overhead_pct": round(
                100.0 * (statistics.median(on) - statistics.median(off)) / statistics.median(off),
                1,
            ),
            "cold_counted": [cold.records, cold.below, cold.cold],
            "warm_counted": [warm.records, warm.below, warm.cold],
            "restart_counted": [restart.records, restart.below, restart.cold],
        }
        assert cold.records == total and cold.cold, row
        assert warm.records - warm.below == DELTA and warm.below <= 1, row
        assert not restart.cold and restart.records - restart.below == 2 * DELTA, row
        assert restart.below <= read_budget.K, row
        print(json.dumps(row))
        out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    rows = run(args.rounds)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows, "snapshot": read_budget.snapshot()}, fh, indent=2)


if __name__ == "__main__":
    main()
