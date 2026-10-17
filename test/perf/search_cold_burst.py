"""Cold burst of 6 concurrent first searches, then warm p50, in a fresh process.

  search_cold_burst.py build <dir>        write and index a synthetic corpus
  search_cold_burst.py run <dir> <mode>   print one JSON result line

<mode> is ``same`` (six callers, one query) or ``distinct`` (six queries), with
an optional ``+warm`` suffix that calls ``ConversationLog.warm_search`` first
(this branch only). Run each arm as its own process with PYTHONPATH=<tree>/src
and an isolated KIROCREW_HOME, alternating arms within each round.
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import threading
import time
from pathlib import Path

import kiro_crew
from kiro_crew.history import ConversationLog

WORDS = (
    "deploy pipeline gateway memory session search index cache latency render "
    "kernel python rust thread lock budget window snippet digest token stream "
    "worker cron slack review branch commit merge build test flake retry"
).split()


def build(path: str) -> None:
    rnd = random.Random(7)
    log = ConversationLog(base_dir=Path(path))
    for s in range(240):
        key = f"sess-{s:04d}"
        for m in range(120):
            text = " ".join(rnd.choice(WORDS) for _ in range(60))
            log.append(key, "user" if m % 2 == 0 else "assistant", text)
    proj = log._catalog_projection
    for s in range(240):
        assert proj.index_session(f"sess-{s:04d}")


QUERIES = [
    "deploy gateway",
    "memory latency",
    "rust thread",
    "snippet digest",
    "cron slack",
    "merge flake",
]


def run(path: str, mode: str) -> dict:
    log = ConversationLog(base_dir=Path(path))
    warmed = 0.0
    if mode.endswith("+warm"):
        t = time.perf_counter()
        log.warm_search()
        warmed = time.perf_counter() - t
    qs = [QUERIES[0]] * 6 if mode.startswith("same") else list(QUERIES)
    lat = [0.0] * 6
    barrier = threading.Barrier(6)

    def one(i: int) -> None:
        barrier.wait()
        t = time.perf_counter()
        log.search_sessions(qs[i], 50)
        lat[i] = time.perf_counter() - t

    ts = [threading.Thread(target=one, args=(i,)) for i in range(6)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0
    warm = []
    for _ in range(5):
        for q in QUERIES:
            t = time.perf_counter()
            log.search_sessions(q, 50)
            warm.append(time.perf_counter() - t)
    return {
        "src": kiro_crew.__file__,
        "mode": mode,
        "warm_search_s": round(warmed, 3),
        "burst_wall_s": round(wall, 3),
        "burst_median_s": round(statistics.median(lat), 3),
        "warm_p50_ms": round(statistics.median(warm) * 1000, 1),
    }


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2])
    else:
        print(json.dumps(run(sys.argv[2], sys.argv[3])))
