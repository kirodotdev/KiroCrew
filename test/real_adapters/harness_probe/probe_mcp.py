"""A stdio MCP server that reports how a harness started it.

argv: ``<server-name> <log-dir>``

Appends one JSON line per event to ``<log-dir>/<server-name>.jsonl``: a ``start``
line carrying the ``PROBE_*`` variables it was given, then one ``rpc`` line per
request. It lists its ONE tool only when ``PROBE_TOKEN`` (or ``PROBE_VALUE``)
equals the fixed placeholder below, so a harness that forwards an unexpanded
``${VAR}`` reference shows up as "started, no tools". The value is a fabricated
placeholder by design; nothing here ever sees a credential.
"""

import json
import os
import sys
import time

NAME = sys.argv[1] if len(sys.argv) > 1 else "probe"
LOG_DIR = sys.argv[2] if len(sys.argv) > 2 else "."
EXPECT = "probe-real-7f3a"
LOG = os.path.join(LOG_DIR, f"{NAME}.jsonl")


def log(obj):
    obj["t"] = round(time.time(), 3)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


token = os.environ.get("PROBE_TOKEN", "") or os.environ.get("PROBE_VALUE", "")
log(
    {
        "ev": "start",
        "env": {k: v for k, v in os.environ.items() if k.startswith("PROBE_")},
        "token_seen": token,
        "token_ok": token == EXPECT,
    }
)

# A padded schema, so the probe's own tool is visible in a captured model request.
_PAD = {f"field_{i}": {"type": "string", "description": "padding " * 8} for i in range(20)}
TOOLS = [
    {
        "name": f"{NAME.replace('-', '_')}_ok",
        "description": f"Probe tool of {NAME}. Listed only when PROBE_TOKEN resolved.",
        "inputSchema": {"type": "object", "properties": _PAD},
    }
]


def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    log({"ev": "rpc", "method": method})
    if method == "initialize":
        reply(
            mid,
            {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": NAME, "version": "0.0.1"},
            },
        )
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS if token == EXPECT else []})
    elif method == "tools/call":
        reply(mid, {"content": [{"type": "text", "text": f"{NAME} ok"}]})
    elif mid is not None and method in ("resources/list", "prompts/list"):
        reply(mid, {method.split("/")[0]: []})
    elif mid is not None:
        error = {"code": -32601, "message": "method not found"}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": error}) + "\n")
        sys.stdout.flush()
