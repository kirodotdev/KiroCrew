"""Measure how long session-expiry consolidation blocks the event loop.

Builds one large transcript in a throwaway directory, empties the parsed
transcript cache (the state idle expiry finds it in), then calls
``HistoryConsolidator.consolidate_session`` on a running loop with asyncio
debug mode on. Reports the loop's own slow-callback warnings and the longest
gap a 5 ms ticker saw while the call and its follow-up work ran. The model
call is stubbed, so only the eligibility reads are measured.

Usage: python test/perf/expiry_loop_block.py [messages] [rounds]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch


async def _one_round(log, consolidator, key: str) -> tuple[float, float, list[str]]:
    log._msg_cache.clear()
    log._recent_cache.clear()
    gaps: list[float] = []
    stop = False

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.005)  # flake-ok: the tick period is the measurement probe
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    warnings: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = record.getMessage()
            if "took" in msg and "seconds" in msg:
                warnings.append(msg.split(" took ")[-1])

    handler = _Grab()
    logging.getLogger("asyncio").addHandler(handler)
    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    try:
        with patch.object(consolidator, "_consolidate", new_callable=AsyncMock):
            t0 = time.perf_counter()
            consolidator.consolidate_session(key)
            sync_call = time.perf_counter() - t0
            await asyncio.gather(*list(consolidator._tasks))
        await asyncio.sleep(0.05)
    finally:
        stop = True
        await tick
        logging.getLogger("asyncio").removeHandler(handler)
    return sync_call, max(gaps), warnings


async def main(messages: int, rounds: int) -> None:
    loop = asyncio.get_running_loop()
    loop.set_debug(True)
    loop.slow_callback_duration = 0.05
    with tempfile.TemporaryDirectory(prefix="expiry-block-") as tmp:
        root = Path(tmp)
        # Every path Kiro Crew resolves (memory stores, breadcrumbs) lands in
        # the temporary directory, so the run leaves nothing behind.
        os.environ["KIROCREW_HOME"] = str(root / "home")  # flake-ok: standalone process, not a test
        from kiro_crew.history import ConversationLog, HistoryConsolidator
        from kiro_crew.memory import MemoryStore

        log = ConversationLog(base_dir=root / "sessions")
        log.init()
        mem = MemoryStore(workspace=root / "memory")
        mem.init()
        consolidator = HistoryConsolidator(log=log, memory=mem)
        key = "dashboard:chat-big"
        path = log._path(key)
        body = "x" * 600
        with path.open("w", encoding="utf-8") as fh:
            for i in range(messages):
                role = "user" if i % 2 == 0 else "assistant"
                fh.write(
                    json.dumps(
                        {"role": role, "content": f"{i} {body}", "ts": "2026-01-01T00:00:00"}
                    )
                )
                fh.write("\n")
        size_mb = path.stat().st_size / 1e6
        results = [await _one_round(log, consolidator, key) for _ in range(rounds)]
    syncs = sorted(r[0] * 1000 for r in results)
    gaps = sorted(r[1] * 1000 for r in results)
    print(f"transcript: {messages} messages, {size_mb:.1f} MB, rounds={rounds}")
    print(f"consolidate_session call (ms): median={syncs[len(syncs) // 2]:.1f} max={syncs[-1]:.1f}")
    print(f"longest loop gap (ms):        median={gaps[len(gaps) // 2]:.1f} max={gaps[-1]:.1f}")
    print(f"slow-callback warnings:       {sum(len(r[2]) for r in results)} {results[0][2][:2]}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40000
    r = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    asyncio.run(main(n, r))
