#!/usr/bin/env python3
"""A minimal stdio MCP server that records every launch.

Used by ``test_mcp_gateway_pool_integ.py`` as the REAL process gatewayd spawns
behind the pool. Appending one line per launch to the file named in ``argv[1]``
turns the question "how many backends did the pool actually create?" into a
line count -- a closed-box observation that needs no access to the pool's
private state, and therefore keeps working when that state is refactored.

Answers ``initialize``: the pool spawns a backend lazily on the first
non-register frame, so one ``initialize`` per stub is enough to force the
spawn-or-reuse decision the launch count is about.

Given an OPTIONAL second path argument, it also advertises
``kirocrew.caller-identity`` and appends the ``sessionKey`` of every
``tools/call``'s caller block to that file -- turning "did gatewayd hand this
shared backend each session's own identity?" into another line read. Advertising
is bundled with recording deliberately: gatewayd injects only into a backend that
advertised, so a recorder that stayed silent would observe nothing and read as a
broken gateway. Without the argument the handshake is byte-for-byte what the
launch-counting tests already assert against.

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
    # Optional second argument: a path to record the caller block each tools/call
    # arrived with, one line per call. Passing it also makes this server ADVERTISE
    # ``kirocrew.caller-identity`` -- gatewayd injects the block only into a
    # backend that advertised, so a recorder that did not advertise would observe
    # nothing and read as "injection is broken". Default off so the pooling tests
    # that only count launches keep the exact handshake they assert against.
    caller_log = sys.argv[2] if len(sys.argv) > 2 else ""

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
        method = msg.get("method")
        if method == "tools/call" and caller_log:
            params = msg.get("params") or {}
            meta = params.get("_meta") or {}
            block = meta.get("kirocrew.caller") or {}
            # The empty string is a meaningful observation -- it is what a
            # backend sees when nothing injected -- so record it rather than
            # skipping the line.
            with open(caller_log, "a", encoding="utf-8") as fh:
                fh.write(f"{block.get('sessionKey', '')}\n")
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}) + "\n"
            )
            sys.stdout.flush()
            continue
        if method != "initialize":
            continue
        params = msg.get("params") or {}
        capabilities: dict = {"tools": {}}
        if caller_log:
            capabilities["experimental"] = {"kirocrew.caller-identity": {"schemaVersion": 1}}
        reply = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": capabilities,
                "serverInfo": {"name": "fake-pool-mcp", "version": "1.0.0"},
            },
        }
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
