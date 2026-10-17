"""Compare slot-detail render CPU work, not HTTP or browser opening latency.

Run with the worktree venv, PYTHONPATH=src and an isolated HOME/KIROCREW_HOME.
The reference is the unchanged chat_utils renderer followed by json.dumps.
The hoisted arm computes the existing string-key suffix once per render.
All three arms use the same corpus and alternate order within each round.
"""

import argparse
import gc
import hashlib
import hmac
import json
import os
import statistics
import time
import tracemalloc
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


def reference(rows):
    return json.dumps(cu._prepare_messages(rows, False, live_child=""))


def memo(rows):
    return wr.prepare_messages_json(rows, False, live_child="")


def suffix():
    hosts = "\0".join(sorted(cu._exempt_exact_hosts() | cu.current_scoped_exempt_hosts()))
    value = b"\0" + hosts.encode("utf-8", errors="surrogatepass")
    prefixes = cu._token_param_exempt_prefixes()
    if prefixes:
        value += b"\x01" + "\0".join(prefixes).encode("utf-8", errors="surrogatepass")
    return value


def rss_kib():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError("RSS unavailable")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    results = {"unit": "one render plus JSON serialization, milliseconds", "cases": []}
    original_key = cu._display_redaction_cache_key
    current_suffix = b""

    def hoisted_key(text):
        raw = text.encode("utf-8", errors="surrogatepass")
        digest = hmac.new(cu._DISPLAY_REDACTION_SALT, raw + current_suffix, hashlib.sha256).digest()
        return (digest, len(raw)), len(raw)

    def hoisted(rows):
        nonlocal current_suffix
        with cu.scoped_exempt_hosts(cu.allowed_hosts_for(None)):
            current_suffix = suffix()
            return json.dumps(cu._prepare_messages_scoped(rows, False, live_child=""))

    for count in (100, 3000):
        rows = corpus(count)
        expected = reference(rows)
        assert memo(rows) == expected
        with patch.object(cu, "_display_redaction_cache_key", hoisted_key):
            assert hoisted(rows) == expected
        samples = {name: [] for name in ("reference", "hoisted", "memo")}
        rounds = []
        arms = {"reference": reference, "hoisted": hoisted, "memo": memo}
        for round_id in range(args.rounds):
            order = list(arms)
            order = order[round_id % 3 :] + order[: round_id % 3]
            if round_id % 2:
                order.reverse()
            for name in order:
                key = hoisted_key if name == "hoisted" else original_key
                with patch.object(cu, "_display_redaction_cache_key", key):
                    for _ in range(3):
                        assert arms[name](rows) == expected
                    load_start = os.getloadavg()
                    values = []
                    for _ in range(args.repeats):
                        start = time.perf_counter_ns()
                        output = arms[name](rows)
                        values.append((time.perf_counter_ns() - start) / 1e6)
                        assert output == expected
                    samples[name].extend(values)
                    rounds.append(
                        {
                            "round": round_id,
                            "arm": name,
                            "p50": statistics.median(values),
                            "load_start": load_start,
                            "load_end": os.getloadavg(),
                        }
                    )
        results["cases"].append(
            {
                "rows": count,
                "wire_bytes": len(expected.encode()),
                "oracle_differences": 0,
                "summary": {
                    name: {
                        "n": len(v),
                        "p50": statistics.median(v),
                        "p95": sorted(v)[int((len(v) - 1) * 0.95)],
                    }
                    for name, v in samples.items()
                },
                "rounds": rounds,
                "samples": samples,
            }
        )
    rows = corpus(18000)
    reference(rows)
    wr.clear_render_memo()
    gc.collect()
    before_rss = rss_kib()
    tracemalloc.start()
    memo(rows)
    gc.collect()
    traced, peak = tracemalloc.get_traced_memory()
    after_rss = rss_kib()
    entries, charged = wr.render_memo_info()
    wr.clear_render_memo()
    gc.collect()
    cleared, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    results["memory"] = {
        "entries": entries,
        "charged_bytes": charged,
        "traced_before_clear": traced,
        "traced_after_clear": cleared,
        "traced_released": traced - cleared,
        "traced_peak": peak,
        "rss_before_kib": before_rss,
        "rss_after_kib": after_rss,
        "rss_after_clear_kib": rss_kib(),
    }
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            {
                "cases": [
                    {k: v for k, v in case.items() if k not in ("rounds", "samples")}
                    for case in results["cases"]
                ],
                "memory": results["memory"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
