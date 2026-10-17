#!/usr/bin/env python3
"""Summarize redact_on_write_replay.py rounds: times per scenario and the sha oracle.

summarize_replay.py <results-dir> <tag>
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

R = Path(sys.argv[1])
TAG = sys.argv[2]


def load(name: str) -> dict | None:
    p = R / f"{TAG}-{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def per(res: dict, i: int) -> dict:
    return res["passes"][i]["per"]


rounds = sorted(
    {
        p.name.removeprefix(f"{TAG}-b").removesuffix("-cold.json")
        for p in R.glob(f"{TAG}-b*-cold.json")
    },
    key=int,
)
rows = []
diffs = {"n_cold": 0, "n_warm": 0, "n_restart": 0, "w_open": 0, "b_warm": 0, "b_restart": 0}
compared = 0
for r in rounds:
    b, br = load(f"b{r}-cold"), load(f"b{r}-restart")
    n, nr = load(f"n{r}-cold"), load(f"n{r}-restart")
    w, wo = load(f"w{r}-write"), load(f"w{r}-open")
    if not all((b, br, n, nr, w, wo)):
        continue
    ref = per(b, 0)
    for key, v in ref.items():
        compared += 1
        for label, other in (
            ("n_cold", per(n, 0)),
            ("n_warm", per(n, 1)),
            ("n_restart", per(nr, 0)),
            ("w_open", per(wo, 0)),
            ("b_warm", per(b, 1)),
            ("b_restart", per(br, 0)),
        ):
            if other[key]["sha"] != v["sha"]:
                diffs[label] += 1

    def wall(res: dict, i: int = 0) -> float:
        return sum(x["s"] for x in per(res, i).values())

    def calls(res: dict, i: int = 0) -> int:
        return sum(x["calls"] for x in per(res, i).values())

    def big(res: dict, i: int = 0) -> float:
        return per(res, i)[next(iter(ref))]["s"]  # keys are largest-first

    rows.append(
        {
            "round": r,
            "base_cold_s": wall(b),
            "base_warm_s": wall(b, 1),
            "base_restart_s": wall(br),
            "new_cold_s": wall(n),
            "new_warm_s": wall(n, 1),
            "new_restart_s": wall(nr),
            "new_open_after_write_s": wall(wo),
            "calls": {
                "base_cold": calls(b),
                "base_warm": calls(b, 1),
                "base_restart": calls(br),
                "new_cold": calls(n),
                "new_warm": calls(n, 1),
                "new_restart": calls(nr),
                "new_open_after_write": calls(wo),
            },
            "largest": {
                "base_cold": big(b),
                "base_restart": big(br),
                "new_cold": big(n),
                "new_restart": big(nr),
                "new_open_after_write": big(wo),
                "base_warm": big(b, 1),
                "new_warm": big(n, 1),
            },
            "write": w,
            "sidecar_bytes_read_built": nr.get("sidecar_bytes"),
        }
    )
out = {"rows": rows, "oracle": {"sessions_x_rounds": compared, "diffs": diffs}}
if rows:
    out["median"] = {k: statistics.median(r[k] for r in rows) for k in rows[0] if k.endswith("_s")}
print(json.dumps(out, indent=1))
