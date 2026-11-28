"""A stdio MCP server that serves the era of its first request, as mcp 2.3.0 does.

The first request decides: ``server/discover`` carrying the stateless-era envelope
locks the pipe to that era, where ``initialize`` is refused with -32022;
anything else locks it to the handshake era. One ``tools/call`` tool answers in
either era.
"""

from __future__ import annotations

import json
import sys

_ENVELOPE_KEY = "io.modelcontextprotocol/protocolVersion"


def _reply(req_id: object, **body: object) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, **body}) + "\n")
    sys.stdout.flush()


def main() -> None:
    era = None
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg or "method" not in msg:
            continue
        method = msg["method"]
        meta = (msg.get("params") or {}).get("_meta") or {}
        if era is None:
            era = "modern" if method != "initialize" and _ENVELOPE_KEY in meta else "handshake"
        if method == "initialize":
            if era == "modern":
                _reply(
                    msg["id"],
                    error={
                        "code": -32022,
                        "message": "connection is serving the 2026-07-28 protocol; "
                        "the initialize handshake is not accepted",
                    },
                )
                continue
            _reply(
                msg["id"],
                result={
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-era", "version": "1"},
                },
            )
        elif method == "server/discover":
            _reply(msg["id"], result={"supportedVersions": ["2026-07-28"], "capabilities": {}})
        elif method == "tools/call":
            _reply(msg["id"], result={"content": [{"type": "text", "text": era}]})
        else:
            _reply(msg["id"], error={"code": -32601, "message": "Method not found"})


if __name__ == "__main__":
    main()
