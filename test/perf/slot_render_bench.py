"""Compare slot-detail render CPU per read: main's row memo against this branch.

Run with the worktree venv, PYTHONPATH=src and an isolated HOME/KIROCREW_HOME,
passing main's ``wire_render.py`` (for example ``git show origin/main:...``):

    PYTHONPATH=src .venv/bin/python test/perf/slot_render_bench.py \\
        --main-wire-render main_wire_render.py --output out.json

Each read is driven the way the slot-detail handler drives it: ``plan_read``
decides between the memo and the plain render, and a first read's fill runs
right after it (synchronously here, in a worker thread in the gateway). The
figure is render-thread CPU, which is the time the GIL is held, split into the
request path and the fill. Three cases, at 100 and 3000 rows:

* ``in_memo``   -- one session re-read with its page filled and whole.
* ``evicted``   -- a filled page after another page's fill pushed part of it
  out of the memo (cap of 1.5 pages).
* ``switching`` -- six sessions read in turn with a cap of about four pages,
  the reader cycling through more history than the memo holds.

The arms alternate within every round so host load drifts across both alike,
and every output is checked byte for byte against the plain render.
"""

import argparse
import importlib.util
import json
import os
import statistics
import time
from pathlib import Path
from unittest.mock import patch

from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard import wire_render as branch


def corpus(count, tag):
    secret = "ghp_" + "k7qmz2x9wv4tb8ncr6yd3fj5hs1gpl0aeuo2"
    prose = "A stable message with Unicode: 中文, café, Δ. " * 18
    return [
        {
            "role": "user" if i % 4 == 0 else "assistant",
            "content": f"{tag} {i}: {prose}" + (secret if i % 9 == 0 else "```py\nx = 1\n```"),
            "ts": f"{tag}-row-{i}",
            "meta": {"mid": f"{tag}-m{i}", "tool": {"name": "read", "status": "ok"}},
        }
        for i in range(count)
    ]


def plain(rows):
    return json.dumps(cu._prepare_messages(rows, False, live_child=""))


class Arm:
    def __init__(self, module, scoped):
        self.wr = module
        self.scoped = scoped

    def render(self, rows, scope):
        extra = {"scope": scope} if self.scoped else {}
        return self.wr.prepare_messages_json(rows, False, live_child="", **extra)

    def read(self, scope, rows):
        """One handler read: ``(output, request_cpu_ms, fill_cpu_ms)``."""
        use_memo, fill = self.wr.plan_read(scope)
        start = time.thread_time_ns()
        out = self.render(rows, scope) if use_memo else plain(rows)
        request = (time.thread_time_ns() - start) / 1e6
        fill_ms = 0.0
        if fill:
            start = time.thread_time_ns()
            self.render(rows, scope)
            self.wr._settle_scope(scope, True)
            fill_ms = (time.thread_time_ns() - start) / 1e6
        return out, request, fill_ms

    def page_bytes(self, rows):
        self.wr.clear_render_memo()
        self.render(rows, None)
        _, charged = self.wr.render_memo_info()
        self.wr.clear_render_memo()
        return charged


def case_in_memo(arm, rows, expected, repeats):
    arm.wr.clear_render_memo()
    arm.read(("s",), rows)  # first read: plain, then the fill
    assert arm.wr.plan_read(("s",)) == (True, False)
    samples = []
    for _ in range(repeats):
        out, request, fill = arm.read(("s",), rows)
        assert out == expected
        samples.append((request, fill))
    return samples


def case_evicted(arm, rows, expected, other, repeats):
    samples = []
    cap = int(arm.page_bytes(rows) * 1.5)
    with patch.object(arm.wr, "RENDER_MEMO_MAX_BYTES", cap):
        for _ in range(repeats):
            arm.wr.clear_render_memo()
            for scope, page in ((("a",), rows), (("b",), other)):
                arm.read(scope, page)  # first read: plain, then the fill
            out, request, fill = arm.read(("a",), rows)
            assert out == expected
            samples.append((request, fill))
    return samples


def case_switching(arm, pages, expected, cycles):
    samples = []
    cap = int(arm.page_bytes(pages[0]) * 4)
    with patch.object(arm.wr, "RENDER_MEMO_MAX_BYTES", cap):
        arm.wr.clear_render_memo()
        for cycle in range(cycles + 2):
            for i, page in enumerate(pages):
                out, request, fill = arm.read((f"s{i}",), page)
                assert out == expected[i]
                if cycle >= 2:  # two warm-up cycles reach the steady state
                    samples.append((request, fill))
    return samples


def summary(samples):
    request = [r for r, _ in samples]
    total = [r + f for r, f in samples]
    return {
        "n": len(samples),
        "request_p50": round(statistics.median(request), 2),
        "request_p95": round(sorted(request)[int((len(request) - 1) * 0.95)], 2),
        "fill_mean": round(statistics.fmean(f for _, f in samples), 2),
        "total_mean": round(statistics.fmean(total), 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-wire-render", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--rows", type=int, nargs="+", default=[100, 3000])
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("wire_render_main", args.main_wire_render)
    main_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main_module)
    arms = {"main": Arm(main_module, scoped=False), "branch": Arm(branch, scoped=True)}
    results = {"unit": "render thread CPU per read, milliseconds", "cases": []}
    for count in args.rows:
        pages = [corpus(count, f"s{i}") for i in range(6)]
        expected = [plain(p) for p in pages]
        data = {(c, a): [] for c in ("in_memo", "evicted", "switching") for a in arms}
        loads = []
        for round_id in range(args.rounds):
            names = list(arms) if round_id % 2 == 0 else list(arms)[::-1]
            for name in names:
                arm = arms[name]
                loads.append(os.getloadavg()[0])
                data[("in_memo", name)] += case_in_memo(arm, pages[0], expected[0], args.repeats)
                data[("evicted", name)] += case_evicted(
                    arm, pages[0], expected[0], pages[1], args.repeats
                )
                data[("switching", name)] += case_switching(arm, pages, expected, 12)
                arm.wr.clear_render_memo()
        results["cases"].append(
            {
                "rows": count,
                "load_min": round(min(loads), 1),
                "load_max": round(max(loads), 1),
                "oracle_differences": 0,
                "summary": {f"{c}/{a}": summary(v) for (c, a), v in data.items()},
            }
        )
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
