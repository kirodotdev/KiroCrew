#!/usr/bin/env python3
"""A minimal stdio MCP server that records every launch.

Used by ``test_mcp_gateway_pool_integ.py`` as the REAL process gatewayd spawns
behind the pool. Appending one line per launch to the file named in ``argv[1]``
turns the question "how many backends did the pool actually create?" into a
line count -- a closed-box observation that needs no access to the pool's
private state, and therefore keeps working when that state is refactored.

Answers only ``initialize``: the pool spawns a backend lazily on the first
non-register frame, so one ``initialize`` per stub is enough to force the
spawn-or-reuse decision this test is about. Anything else is ignored, which
keeps the reply loop small enough to have no platform-specific behaviour.

Stdlib only, and launched as ``sys.executable <this file> <log>`` -- never
through a shell and never via ``-c`` -- so no quoting or backslash assumption
travels onto Windows.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    log = sys.argv[1]
    # One line per process launch. Opened in append mode and closed
    # immediately: a backend that lingers must not hold the handle that the
    # test reads, which on Windows would block the read with a sharing
    # violation.
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}\n")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("method") != "initialize":
            continue
        params = msg.get("params") or {}
        reply = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-pool-mcp", "version": "1.0.0"},
            },
        }
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
