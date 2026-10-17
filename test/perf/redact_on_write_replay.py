#!/usr/bin/env python3
"""Replay real transcripts through the slot-detail display projection.

One invocation is one process lifetime (a "gateway"); run it with
``PYTHONPATH=<tree>/src`` to pick the code under test. Nothing here starts a
gateway or touches a live data home: ``--home`` must be a scratch copy.

  render  --home H --keys K --passes N --out OUT.json [--persist]
      For each key, read the chained transcript and render it the way
      ``api_chat_slot_detail`` does (``_prepare_messages`` + ``json.dumps``),
      N passes in this process. Records per-pass wall time, battery calls and
      the sha256 of every rendered body, which is what two trees are compared by.
  write   --home H --keys K --out OUT.json [--window W]
      Simulate saves: feed each transcript's rows one at a time through the
      save-side hooks (``note_written_lines`` + ``submit_committed``) with a
      window of the last W rows, and time each step. Persistence on.

``--persist`` turns stored verdicts on (new tree only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path


def _setup(home: Path) -> None:
    # The data home is fixed at process start, so it is the caller's to set.
    if os.environ.get("KIROCREW_HOME") != str(home):
        raise SystemExit(f"run with KIROCREW_HOME={home}")


def _counted():
    # The battery's home: dashboard.display_battery on this branch, chat_utils
    # on a base tree that predates it.
    try:
        from kiro_crew.dashboard import display_battery as home
    except ImportError:
        from kiro_crew.dashboard import chat_utils as home

    calls = {"n": 0}
    orig_u = home.redact_exfiltration_urls

    def u(text):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return orig_u(text)

    home.redact_exfiltration_urls = u
    return calls


def _pct(v: list[float], q: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))] if v else 0.0


def _keys(path: str) -> list[str]:
    return [k.strip() for k in Path(path).read_text().splitlines() if k.strip()]


def _save(path: str, res: dict) -> None:
    Path(path).write_text(json.dumps(res, indent=1))


def cmd_render(a: argparse.Namespace) -> None:
    home = Path(a.home)
    _setup(home)
    from kiro_crew.dashboard import chat_utils
    from kiro_crew.history import ConversationLog

    try:
        from kiro_crew.dashboard import display_verdicts as dv
    except ImportError:
        dv = None
    if a.persist:
        assert dv is not None and dv.enable(home), "persistence did not enable"
    calls = _counted()
    log = ConversationLog(home / "sessions")
    keys = _keys(a.keys)
    res: dict = {"keys": len(keys), "passes": []}
    for p in range(a.passes):
        per = {}
        t_all = time.perf_counter()
        for key in keys:
            msgs = log.read_messages_chained(key)
            msgs = chat_utils._collapse_wire_rows(msgs)
            c0 = calls["n"]
            t0 = time.perf_counter()
            u0 = time.thread_time()
            if dv is not None:
                with dv.session(key):
                    out = chat_utils._prepare_messages(msgs, False, live_child="", workspace=None)
            else:
                out = chat_utils._prepare_messages(msgs, False, live_child="", workspace=None)
            body = json.dumps({"messages": out})
            dt = time.perf_counter() - t0
            cpu = time.thread_time() - u0
            sha = hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()
            per[key] = {
                "s": round(dt, 4),
                "cpu_s": round(cpu, 4),
                "calls": calls["n"] - c0,
                "sha": sha,
                "rows": len(msgs),
            }
        res["passes"].append({"wall_s": round(time.perf_counter() - t_all, 3), "per": per})
    if dv is not None and dv.enabled():
        root = home / dv.DIR_NAME
        res["sidecar_bytes"] = sum(f.stat().st_size for f in root.iterdir() if f.is_file())
    _save(a.out, res)
    for i, p in enumerate(res["passes"]):
        tot_calls = sum(x["calls"] for x in p["per"].values())
        print(f"pass{i + 1} wall={p['wall_s']}s battery_calls={tot_calls}")


def cmd_write(a: argparse.Namespace) -> None:
    home = Path(a.home)
    _setup(home)
    from kiro_crew.dashboard import display_verdicts as dv

    assert dv.enable(home), "persistence did not enable"
    keys = _keys(a.keys)
    sessions = home / "sessions"
    hook_s: list[float] = []
    proj_s: list[float] = []
    rows_total = 0
    for key in keys:
        lines = []
        with open(sessions / f"{key}.jsonl", encoding="utf-8", errors="surrogatepass") as fh:
            for i, line in enumerate(fh):
                if i == 0:
                    continue  # the metadata line, which the save writes separately
                lines.append(line if line.endswith("\n") else line + "\n")
        for i in range(len(lines)):
            window = lines[max(0, i + 1 - a.window) : i + 1]
            # The save thread's share: the hash pass in compose_payload and the
            # commit hand-off. Timed without the queue, whose worker would add
            # its wake-up latency to a number that is about CPU.
            t0 = time.perf_counter()
            dv.note_written_lines(key, window)
            item = dv.take_pending()
            assert item is not None
            dv.commit_written(item[0], item[2])
            t1 = time.perf_counter()
            # The worker's share: projecting the new rows and appending verdicts.
            if item[1]:
                dv.project(item[0], None, item[1])
            t2 = time.perf_counter()
            hook_s.append(t1 - t0)
            proj_s.append(t2 - t1)
        rows_total += len(lines)
    root = home / dv.DIR_NAME
    res = {
        "rows": rows_total,
        "save_thread_hook_ms": {
            "p50": _pct(hook_s, 0.5) * 1e3,
            "p99": _pct(hook_s, 0.99) * 1e3,
        },
        "projection_ms": {
            "p50": _pct(proj_s, 0.5) * 1e3,
            "p99": _pct(proj_s, 0.99) * 1e3,
            "mean": statistics.fmean(proj_s) * 1e3 if proj_s else 0,
            "total_s": sum(proj_s),
        },
        "sidecar_bytes": sum(f.stat().st_size for f in root.iterdir() if f.is_file()),
    }
    _save(a.out, res)
    print(json.dumps(res))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--home", required=True)
    r.add_argument("--keys", required=True)
    r.add_argument("--passes", type=int, default=1)
    r.add_argument("--out", required=True)
    r.add_argument("--persist", action="store_true")
    w = sub.add_parser("write")
    w.add_argument("--home", required=True)
    w.add_argument("--keys", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--window", type=int, default=200)
    a = ap.parse_args()
    {"render": cmd_render, "write": cmd_write}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
