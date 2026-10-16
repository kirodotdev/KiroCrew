"""Compare slot-detail render CPU per request: the plain render against the row memo.

Run with the worktree venv, PYTHONPATH=src and an isolated HOME/KIROCREW_HOME:

    PYTHONPATH=src .venv/bin/python test/perf/slot_render_bench.py --output out.json

Arms, all on the same corpus, interleaved in a rotating order inside every round
so host load drifts across arms alike, each timed on a warm per-string cache:

* ``plain_old``  -- ``json.dumps(_prepare_messages(...))`` with the display key
  built the way it was before the key's suffix bytes were reused (copied below).
* ``memo_hit``   -- the row memo with every row already stored (a filled page).
* ``memo_evict`` -- the row memo with a cap smaller than the page, so every row
  misses (a page whose rows were evicted).
* ``plain``      -- ``json.dumps(_prepare_messages(...))`` as the handler runs it.

Each arm is measured with and without persistent display verdicts (the MAC base
the gateway uses once ``display_verdicts`` is enabled). The figure is CPU time of
the rendering thread, which is the time it holds the GIL; wall time is recorded
too. Every output is checked byte for byte against ``plain_old``.
"""

import argparse
import hashlib
import hmac
import json
import os
import statistics
import time
from pathlib import Path
from unittest.mock import patch

from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard import wire_render as wr


def corpus(count):
    secret = "ghp_" + "k7qmz2x9wv4tb8ncr6yd3fj5hs1gpl0aeuo2"
    prose = "A stable message with Unicode: 中文, café, Δ. " * 18
    return [
        {
            "role": "user" if i % 4 == 0 else "assistant",
            "content": f"{i}: {prose}" + (secret if i % 9 == 0 else "```py\nx = 1\n```"),
            "ts": f"row-{i}",
            "meta": {"mid": f"m{i}", "tool": {"name": "read", "status": "ok"}},
        }
        for i in range(count)
    ]


def legacy_key(text, base=cu._NO_BASE):
    """The display key as built before its suffix bytes were reused."""
    raw = text.encode("utf-8", errors="surrogatepass")
    hosts = "\0".join(sorted(cu._exempt_exact_hosts() | cu.current_scoped_exempt_hosts())).encode(
        "utf-8", errors="surrogatepass"
    )
    prefixes = cu._token_param_exempt_prefixes()
    tail = hosts
    if prefixes:
        tail += b"\x01" + "\0".join(prefixes).encode("utf-8", errors="surrogatepass")
    if base is cu._NO_BASE:
        base = cu._display_verdicts.mac_base()
    if base is None:
        digest = hmac.new(cu._DISPLAY_REDACTION_SALT, raw + b"\0" + tail, hashlib.sha256).digest()
    else:
        mac = base.copy()
        mac.update(len(raw).to_bytes(8, "big"))
        mac.update(raw)
        mac.update(b"\0")
        mac.update(tail)
        digest = mac.digest()
    return (digest, len(raw)), len(raw)


def plain(rows):
    return json.dumps(cu._prepare_messages(rows, False, live_child=""))


def memo(rows):
    return wr.prepare_messages_json(rows, False, live_child="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--rows", type=int, nargs="+", default=[100, 3000])
    args = parser.parse_args()
    results = {"unit": "render thread CPU per request, milliseconds", "cases": []}
    current_key = cu._display_redaction_cache_key
    verdict_base = hmac.new(b"bench-verdict-key", b"rules-v1", hashlib.sha256)
    arms = {
        "plain_old": (plain, legacy_key, None),
        "memo_hit": (memo, legacy_key, None),
        "memo_evict": (memo, legacy_key, 4096),
        "plain": (plain, current_key, None),
    }
    for persist in (False, True):
        base = verdict_base if persist else None
        with (
            patch.object(cu._display_verdicts, "active", lambda: (None, base)),
            patch.object(cu._display_verdicts, "mac_base", lambda: base),
        ):
            for count in args.rows:
                rows = corpus(count)
                with patch.object(cu, "_display_redaction_cache_key", legacy_key):
                    expected = plain(rows)
                cpu = {name: [] for name in arms}
                wall = {name: [] for name in arms}
                rounds = []
                names = list(arms)
                for round_id in range(args.rounds):
                    shift = round_id % len(names)
                    order = names[shift:] + names[:shift]
                    for name in order:
                        fn, key, cap = arms[name]
                        wr.clear_render_memo()
                        with (
                            patch.object(cu, "_display_redaction_cache_key", key),
                            patch.object(
                                wr, "RENDER_MEMO_MAX_BYTES", cap or wr.RENDER_MEMO_MAX_BYTES
                            ),
                        ):
                            for _ in range(2):
                                assert fn(rows) == expected
                            load = os.getloadavg()[0]
                            values = []
                            for _ in range(args.repeats):
                                c0 = time.thread_time_ns()
                                w0 = time.perf_counter_ns()
                                out = fn(rows)
                                wall[name].append((time.perf_counter_ns() - w0) / 1e6)
                                values.append((time.thread_time_ns() - c0) / 1e6)
                                assert out == expected
                            cpu[name].extend(values)
                            rounds.append(
                                {
                                    "round": round_id,
                                    "arm": name,
                                    "p50": statistics.median(values),
                                    "load": load,
                                }
                            )
                results["cases"].append(
                    {
                        "rows": count,
                        "persistent_verdicts": persist,
                        "oracle_differences": 0,
                        "cpu": {
                            n: {
                                "n": len(v),
                                "p50": round(statistics.median(v), 3),
                                "p95": round(sorted(v)[int((len(v) - 1) * 0.95)], 3),
                            }
                            for n, v in cpu.items()
                        },
                        "wall_p50": {n: round(statistics.median(v), 3) for n, v in wall.items()},
                        "rounds": rounds,
                    }
                )
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            [{k: v for k, v in c.items() if k != "rounds"} for c in results["cases"]],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
