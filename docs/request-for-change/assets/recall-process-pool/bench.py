"""Turn-context recall benchmark for kirodotdev/KiroCrew#26600 item 4.

Simulates N sessions that each assemble the recall part of a turn context
(semantic + episodic + lessons) at the same moment, while one thread keeps
writing the way history consolidation does. Compares:

  threads   one VectorMemoryStore shared by an 8-thread pool (today's gateway)
  poolN     the same 8 threads, with kiro_crew.recall_pool children answering the reads
  procN     N spawned worker processes, each with its own store and its own
            resident scoring sets; the writer stays in the main process

The query embedding is stubbed (a seeded random unit vector per text) and is
computed in the main process and shipped to the worker, so embedding cost is out
of both arms. The DB is the synthetic one from gen_db.py.

Usage: bench.py <repo_src> <db_dir> --mode threads|proc2|proc4|pool2|pool4 --sessions N
         [--rounds 3] [--write-period 2.0] [--no-writer] [--out result.json]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import multiprocessing as mp
import os
import resource
import shutil
import statistics
import sys
import threading
import time
from pathlib import Path

CAPS = dict(lessons=7458, lessons_startup=37000, lesson_experience=1650, semantic=2541, episodic=2541)
HARD_CAP = 45000
DIM = 1024
VOCAB = [f"w{i:04d}x" for i in range(6000)]


def vec_for(text: str):
    import numpy as np

    seed = int.from_bytes(hashlib.md5(text.encode()).digest()[:4], "little")
    v = np.random.default_rng(seed).standard_normal(DIM).astype("float32")
    v /= np.linalg.norm(v)
    return v.tolist()


def make_query(i: int) -> str:
    import random

    r = random.Random(i)
    # a handful of frequent and rare tokens, like a user message
    return " ".join(VOCAB[min(int(r.paretovariate(0.8)) * r.randint(1, 40) % 6000, 5999)] for _ in range(12))


def rss_mb() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


_STORE = None


def open_store(src: str, db_dir: str):
    sys.path.insert(0, src)
    from kiro_crew.vector_memory import VectorMemoryStore

    s = VectorMemoryStore(Path(db_dir) / "memory.db")
    s.init()
    s.embed_fn = vec_for
    return s


def worker_init(src: str, db_dir: str) -> None:
    global _STORE
    _STORE = open_store(src, db_dir)


def recall_part(store, query: str, vec) -> tuple[int, float]:
    """The recall calls of one turn-context build. Returns (chars, seconds)."""
    from kiro_crew.vector_memory_runtime.embedding import _RecallQuery

    t0 = time.perf_counter()
    sem = store.get_semantic_context(query, cap=CAPS["semantic"], facts_only=True)
    epi = store.get_episodic_context(vec, query, CAPS["episodic"])
    rq = _RecallQuery(vec, store._space_generation, store.recorded_embedding_space())
    les = store.get_lessons_context(
        query_text=query,
        cap=CAPS["lessons"],
        project_dir=None,
        recall_query=rq,
        background=True,
        hard_cap=HARD_CAP,
        directive_budget=CAPS["lessons_startup"],
        experience_budget=CAPS["lesson_experience"],
    )
    return len(sem) + len(epi) + len(les), time.perf_counter() - t0


def worker_task(query: str, vec):
    chars, secs = recall_part(_STORE, query, vec)
    return chars, secs, os.getpid(), peak_rss_mb(), rss_mb()


def writer_loop(store, stop: threading.Event, period: float, stats: dict) -> None:
    """Consolidation-style writes: a few facts, a lesson and an episode per tick."""
    import random

    r = random.Random(99)
    n = 0
    while not stop.is_set():
        t0 = time.perf_counter()
        for _ in range(5):
            n += 1
            words = " ".join(r.choice(VOCAB) for _ in range(r.randint(5, 30)))
            store.set_semantic(f"project.bench.w{n}", words, 0.8, "consolidation")
        store.write_lesson(" ".join(r.choice(VOCAB) for _ in range(15)), "knowledge", source="consolidation")
        store.write_episodic(" ".join(r.choice(VOCAB) for _ in range(40)), conversation_id="bench-conv", tags=[])
        stats["ticks"] += 1
        stats["busy_s"] += time.perf_counter() - t0
        stop.wait(period)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def children_rss_mb() -> dict[int, float]:
    out = {}
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            if ppid != me:
                continue
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmHWM:"):
                        out[int(pid)] = int(line.split()[1]) / 1024
        except OSError:
            pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("db_dir")
    ap.add_argument("--mode", required=True)
    ap.add_argument("--sessions", type=int, required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between rounds")
    ap.add_argument("--write-period", type=float, default=2.0)
    ap.add_argument("--no-writer", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--out")
    a = ap.parse_args()

    # Work on a private copy so the source DB is never written.
    work = Path(os.environ.get("CTXPOOL_WORK", "/tmp")) / f"ctxpool-run-{os.getpid()}"
    work.mkdir(parents=True)
    shutil.copy(Path(a.db_dir) / "memory.db", work / "memory.db")
    try:
        run(a, work)
    finally:
        if str(work).startswith("/tmp/ctxpool-run-"):
            shutil.rmtree(work, ignore_errors=True)


def run(a, work: Path) -> None:
    src = a.src
    store = open_store(src, str(work))
    base_rss = rss_mb()
    queries = [make_query(i) for i in range(a.sessions * a.rounds + 50)]
    vecs = {q: vec_for(q) for q in queries}

    if a.mode == "threads" or a.mode.startswith("pool"):
        if a.mode.startswith("pool"):
            # The real implementation: the same 8 gateway threads, but the three store
            # reads are handed to kiro_crew.recall_pool children by VectorMemoryStore.
            import kiro_crew.recall_pool as rp

            n = int(a.mode.removeprefix("pool"))
            rp.settings = lambda: (n, 3072)
            nproc = n
        else:
            nproc = 0
        pool = cf.ThreadPoolExecutor(max_workers=a.threads, thread_name_prefix="embed")
        submit = lambda q: pool.submit(lambda: recall_part(store, q, vecs[q]))  # noqa: E731
    else:
        nproc = int(a.mode.removeprefix("proc"))
        ctx = mp.get_context("spawn")
        pool = cf.ProcessPoolExecutor(
            max_workers=nproc, mp_context=ctx, initializer=worker_init, initargs=(src, str(work))
        )
        submit = lambda q: pool.submit(worker_task, q, vecs[q])  # noqa: E731

    # Warm-up: one recall per worker (builds the resident scoring sets) and timing of it.
    t0 = time.perf_counter()
    warm = [submit(queries[-1 - i]) for i in range(max(nproc, 1) * 2)]
    wres = [f.result() for f in warm]
    warm_s = time.perf_counter() - t0
    print(f"[{a.mode} N={a.sessions}] warm-up {warm_s:.1f}s", flush=True)

    stop = threading.Event()
    wstats = {"ticks": 0, "busy_s": 0.0}
    wt = None
    if not a.no_writer:
        wt = threading.Thread(target=writer_loop, args=(store, stop, a.write_period, wstats), daemon=True)
        wt.start()

    lat: list[float] = []
    svc: list[float] = []
    chars: list[int] = []
    peaks: dict[int, float] = {}
    t_start = time.perf_counter()
    for rnd in range(a.rounds):
        t_round = time.perf_counter()
        futs = []
        for s in range(a.sessions):
            q = queries[rnd * a.sessions + s]
            t_sub = time.perf_counter()
            f = submit(q)
            done_at: list[float] = []
            f.add_done_callback(lambda _f, d=done_at: d.append(time.perf_counter()))
            futs.append((t_sub, f, done_at))
        for t_sub, f, done_at in futs:
            res = f.result()
            time.sleep(0)
            lat.append(done_at[0] - t_sub if done_at else time.perf_counter() - t_sub)
            svc.append(res[1])
            chars.append(res[0])
            if len(res) > 3:
                peaks[res[2]] = max(peaks.get(res[2], 0), res[3])
        print(f"  round {rnd}: {time.perf_counter() - t_round:.1f}s", flush=True)
        time.sleep(a.gap)
    wall = time.perf_counter() - t_start - a.gap * a.rounds
    stop.set()
    if wt:
        wt.join(timeout=30)

    # latency = submit -> completion callback, so reading order does not inflate it.
    main_peak = peak_rss_mb()
    kids = children_rss_mb() if nproc else {}
    worker_peaks = list(kids.values()) or list(peaks.values())
    result = {
        "mode": a.mode,
        "sessions": a.sessions,
        "rounds": a.rounds,
        "writer": not a.no_writer,
        "writer_ticks": wstats["ticks"],
        "requests": len(lat),
        "warmup_s": round(warm_s, 2),
        "wall_busy_s": round(wall, 2),
        "throughput_rps": round(len(lat) / wall, 2),
        "lat_p50_s": round(pct(lat, 0.5), 2),
        "lat_p95_s": round(pct(lat, 0.95), 2),
        "lat_max_s": round(max(lat), 2),
        "service_p50_s": round(statistics.median(svc), 3),
        "service_p95_s": round(pct(svc, 0.95), 3),
        "main_peak_rss_mb": round(main_peak),
        "base_rss_mb_after_open": round(base_rss),
        "worker_peak_rss_mb": [round(x) for x in worker_peaks],
        "total_peak_rss_mb": round(main_peak + sum(worker_peaks)),
        "chars_mean": round(statistics.mean(chars)),
    }
    print(json.dumps(result), flush=True)
    if a.out:
        with open(a.out, "a") as f:
            f.write(json.dumps(result) + "\n")
    pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
