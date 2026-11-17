#!/usr/bin/env python3
"""Does a composer send leave the browser promptly while a big session loads over a slow link?

Opt-in measurement, not a test: it is not collected by pytest and does nothing unless
``KIROCREW_PERF_LATENCY=1`` is set, so it never runs in the CI matrix.

It boots a disposable gateway from the source tree on ``PYTHONPATH`` (the same harness the
browser E2E suite uses), grows one slot's transcript to ``--mb`` megabytes of synthetic rows,
and puts a delay proxy in front of it (``--delay-ms`` each way, ``--mbit`` shared bandwidth per
direction). Headless Chromium then opens the big tab in one page while a second page of the same
context fires composer sends; the proxy stamps the moment each POST arrives from the browser.
Run it once per source tree (the tree must have its frontend built into
``src/kiro_crew/static/dist``) and compare the printed tables.

    KIROCREW_PERF_LATENCY=1 PYTHONPATH=<tree>/src python scripts/measure_send_under_latency.py \\
        --label main

Needs ``website/node_modules`` (Playwright and its Chromium). Never points at a live home.
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIG, SMALL = "big", "small"


# --------------------------------------------------------------------------- fixture


def grow_transcript(path: Path, target_bytes: int, seed: int = 36194) -> int:
    """Append rows to *path* until it holds *target_bytes*; returns the rows added.

    Row sizes follow a long-tailed mix: mostly 3 KB, some 30-400 KB tool output, a few 690 KB
    file-change rows. Words come from a random vocabulary so gzip sees realistic entropy.
    """
    rng = random.Random(seed)
    vocab = [
        "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=rng.randint(2, 9))) for _ in range(4000)
    ]

    def text(n: int) -> str:
        out: list[str] = []
        size = 0
        while size < n:
            word = rng.choice(vocab)
            out.append(word)
            size += len(word) + 1
        return " ".join(out)[:n]

    rows, size = 0, path.stat().st_size
    with path.open("a", encoding="utf-8") as fh:
        while size < target_bytes:
            pick = rng.random()
            n = 690_000 if pick > 0.9995 else rng.randint(30_000, 400_000) if pick > 0.97 else 3_000
            role = "user" if rows % 2 == 0 else "assistant"
            ts = f"2026-01-01T{rows // 3600 % 24:02d}:{rows // 60 % 60:02d}:{rows % 60:02d}+00:00"
            row = {
                "role": role,
                "content": f"row {rows} " + text(n),
                "ts": ts,
                "meta": {"mid": f"m-{rows:016x}"},
            }
            line = json.dumps(row) + "\n"
            fh.write(line)
            size += len(line)
            rows += 1
    return rows


# --------------------------------------------------------------------------- delay proxy


class DelayProxy:
    """TCP proxy: *delay* seconds each way, one shared bandwidth cap per direction.

    ``rate`` is bytes per second (0 = unlimited). Chunks are released in order at
    ``max(arrival + delay, link free time)``, which is what a single shared tunnel does.
    """

    def __init__(self, upstream_port: int, delay: float) -> None:
        self.upstream_port, self.delay, self.rate = upstream_port, delay, 0
        self.free = {"up": 0.0, "down": 0.0}
        self.post_seen: list[float] = []

    def release_at(self, direction: str, size: int) -> float:
        now = time.monotonic()
        start = max(now, self.free[direction])
        self.free[direction] = start + (size / self.rate if self.rate else 0.0)
        return self.free[direction] + self.delay

    async def _pipe(self, reader, writer, direction: str) -> None:
        queue: asyncio.Queue = asyncio.Queue()

        async def drain() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    break
                when, chunk = item
                await asyncio.sleep(max(0.0, when - time.monotonic()))
                writer.write(chunk)
                await writer.drain()
            writer.close()

        task = asyncio.create_task(drain())
        try:
            while chunk := await reader.read(16384):
                if direction == "up" and chunk.startswith(b"POST /api/chat "):
                    self.post_seen.append(time.time())
                queue.put_nowait((self.release_at(direction, len(chunk)), chunk))
        except ConnectionError:
            pass
        queue.put_nowait(None)
        await task

    async def handle(self, reader, writer) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(
            self._pipe(reader, up_writer, "up"),
            self._pipe(up_reader, writer, "down"),
            return_exceptions=True,
        )

    async def control(self, reader, writer) -> None:
        line = (await reader.readline()).decode()
        await reader.readuntil(b"\r\n\r\n")
        path = line.split()[1] if line.split()[1:] else "/"
        if path.startswith("/throttle"):
            mbit = float(path.split("mbit=")[1])
            self.rate = int(mbit * 1_000_000 / 8)
        body = b"{}"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n")
        writer.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()


# --------------------------------------------------------------------------- report


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def detail_rows(requests: list[dict]) -> str:
    if not requests:
        return "no request"
    wire, raw = sum(r["wire"] for r in requests), sum(r["raw"] for r in requests)
    kinds: dict[str, int] = {}
    for r in requests:
        kind = re.sub(r"=\d+", "=N", r["url"].split("?", 1)[1])
        kinds[kind] = kinds.get(kind, 0) + 1
    mix = ", ".join(f"{n}x {k}" for k, n in sorted(kinds.items()))
    return (
        f"{len(requests)} reads, wire {wire:,} B, decoded {raw:,} B, "
        f"slowest {max(r['ms'] for r in requests)} ms [{mix}]"
    )


def report(label: str, result: dict, proxy: DelayProxy, send_times: list[float], args) -> None:
    # Pair each browser-side send start with the first unused proxy arrival at or after it.
    arrivals = sorted(proxy.post_seen)
    lat: list[float] = []
    for t0 in sorted(send_times):
        nxt = next((a for a in arrivals if a >= t0 - 0.001), None)
        if nxt is not None:
            arrivals.remove(nxt)
            lat.append((nxt - t0) * 1000)
    print(f"\n## {label}: {args.mb} MB transcript, {args.delay_ms} ms each way, {args.mbit} Mbit")
    if lat:
        print(
            f"send leaves browser: n={len(lat)} p50={pct(lat, .5):.0f} ms p95={pct(lat, .95):.0f} ms "
            f"max={max(lat):.0f} ms; under 1 s: {sum(v < 1000 for v in lat)}/{len(lat)}"
        )
    totals = [s["total_ms"] for s in result["sends"] if s["total_ms"] is not None]
    if totals:
        print(
            f"send complete (response read): p50={statistics.median(totals):.0f} ms p95={pct(totals, .95):.0f} ms"
        )
    for name in ("open", "unchanged", "newRows"):
        print(f"{name}:")
        for i, rnd in enumerate(result["rounds"]):
            print(f"  round {i}: {detail_rows(rnd[name])}")


# --------------------------------------------------------------------------- main


def open_session(gw):
    """A cookie-carrying opener and base URL for the gateway's own API."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = f"http://localhost:{gw.port}"
    opener.open(f"{base}/?token={gw.token}").read(1)
    return opener, base


def assert_rows_have_identities(gw) -> None:
    """Rows without ``meta.mid`` send the client down its legacy fallback and distort every refresh
    number, so a restored fixture that lacks them is refused rather than measured."""
    opener, base = open_session(gw)
    page = json.loads(opener.open(f"{base}/api/chat/slots/{BIG}?limit=20").read())
    missing = [m for m in page["messages"] if not (m.get("meta") or {}).get("mid")]
    if missing or not page["messages"]:
        raise SystemExit(
            f"fixture rows restored without meta.mid ({len(missing)} of {len(page['messages'])})"
        )


def prepare_gateway(gw, mb: int) -> None:
    """Create both slots, then grow the big slot's transcript on disk and restart so it restores."""
    opener, base = open_session(gw)

    def call(path: str, body: dict) -> bytes:
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
        )
        return opener.open(req).read()

    for name in (BIG, SMALL):
        call("/api/chat/slots", {"name": name})
        call("/api/chat", {"message": "hello", "slot": name})
    time.sleep(5)  # let the two turns finish persisting; a late write would clobber the grown file
    transcript = gw.home / "sessions" / f"dashboard_{BIG}.jsonl"
    rows = grow_transcript(transcript, mb * 1024 * 1024)
    print(f"transcript: {transcript.stat().st_size / 1e6:.1f} MB, {rows} rows added", flush=True)


async def run(gw, args) -> dict:
    proxy = DelayProxy(gw.port, args.delay_ms / 1000)
    front = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    ctl = await asyncio.start_server(proxy.control, "127.0.0.1", 0)
    cfg = {
        "websiteDir": args.website_dir,
        "base": f"http://localhost:{front.sockets[0].getsockname()[1]}",
        "control": f"http://127.0.0.1:{ctl.sockets[0].getsockname()[1]}",
        "token": gw.token,
        "mbit": args.mbit,
        "rounds": args.rounds,
        "sends": args.sends,
        "gapMs": args.gap_ms,
        "settleMs": 4000,
        "refreshWaitMs": 6000,
        "newRows": 3,
    }
    child = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).with_suffix(".mjs")),
        env={**os.environ, "MEASURE_CONFIG": json.dumps(cfg)},
        stdout=subprocess.PIPE,
    )
    out, _ = await child.communicate()
    front.close()
    ctl.close()
    line = next((x for x in out.decode().splitlines() if x.startswith("RESULT:")), None)
    if child.returncode != 0 or line is None:
        raise SystemExit(f"browser half failed (exit {child.returncode})")
    result = json.loads(line[len("RESULT:") :])
    send_times = [s["t0"] / 1000 for s in result["sends"]]
    report(args.label, result, proxy, send_times, args)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--label", default="tree")
    ap.add_argument("--mb", type=int, default=200)
    ap.add_argument("--delay-ms", type=int, default=150)
    ap.add_argument("--mbit", type=float, default=10)
    ap.add_argument(
        "--website-dir",
        default=str(REPO / "website"),
        help="a website/ with node_modules (Playwright)",
    )
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--sends", type=int, default=8, help="sends per round")
    ap.add_argument("--gap-ms", type=int, default=400)
    args = ap.parse_args()
    if os.environ.get("KIROCREW_PERF_LATENCY") != "1":
        sys.exit("opt-in measurement: set KIROCREW_PERF_LATENCY=1 to run it")
    args.website_dir = str(Path(args.website_dir).resolve())
    if not (Path(args.website_dir) / "node_modules" / "@playwright" / "test").is_dir():
        sys.exit(f"--website-dir {args.website_dir} has no node_modules/@playwright/test")

    from kiro_crew.testing import fake_acp_backend
    from kiro_crew.testing.harness import spawn_feature_gateway

    os.environ["KIROCREW_KIRO_BIN"] = str(fake_acp_backend.__file__)
    with spawn_feature_gateway(fixture="minimal", approval="reads") as gw:
        prepare_gateway(gw, args.mb)
        gw = gw.restart()
        assert_rows_have_identities(gw)
        asyncio.run(run(gw, args))


if __name__ == "__main__":
    main()
