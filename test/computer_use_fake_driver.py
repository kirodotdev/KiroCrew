"""A fake ``cua-driver mcp``: line-delimited JSON-RPC on stdin/stdout.

Run as ``python computer_use_fake_driver.py mcp [--log PATH] [--env-out PATH]
[--crash-on TOOL] [--crash-once-on TOOL] [--stall-on TOOL] [--marker PATH]
[--version-out TEXT]``. Behaviour:

* ``initialize`` / ``tools/list`` answer like the real server;
* every ``tools/call`` is appended to ``--log`` (one JSON object per line) BEFORE
  it is acted on, so a test can count deliveries;
* ``--crash-on`` exits without replying when that tool is called;
  ``--crash-once-on`` does so only the first time (a marker file remembers);
* ``--stall-on`` never replies to that tool;
* ``--stop-reading-on`` stops reading stdin (without exiting) once that tool
  is called, so a large next write fills the pipe and blocks;
* ``--env-out`` writes the process environment as JSON at startup;
* ``--version`` prints ``--version-out`` (default ``cua-driver 0.47.8``).
"""

from __future__ import annotations

import json
import os
import sys
import time

TOOLS = [
    "list_apps",
    "list_windows",
    "get_window_state",
    "click",
    "drag",
    "type_text",
    "press_key",
    "hotkey",
    "set_value",
    "scroll",
    "check_permissions",
    "health_report",
    "get_screen_size",
    "launch_app",
    "run_script",
]


def _opt(argv: list[str], name: str) -> str:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return ""


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(_opt(argv, "--version-out") or "cua-driver 0.47.8")
        return 0
    log = _opt(argv, "--log")
    env_out = _opt(argv, "--env-out")
    crash_on = _opt(argv, "--crash-on")
    crash_once = _opt(argv, "--crash-once-on")
    stall_on = _opt(argv, "--stall-on")
    stop_reading_on = _opt(argv, "--stop-reading-on")
    marker = _opt(argv, "--marker")
    if env_out:
        with open(env_out, "w", encoding="utf-8") as fh:
            json.dump(dict(os.environ), fh)
    out = sys.stdout
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            reply = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cua-driver", "version": "0.47.8"},
            }
        elif method == "tools/list":
            reply = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in TOOLS]}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            if log:
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {"pid": os.getpid(), "name": name, "arguments": params.get("arguments")}
                        )
                        + "\n"
                    )
            if name == crash_on:
                os._exit(3)
            if name == crash_once and marker and not os.path.exists(marker):
                open(marker, "w").close()
                os._exit(3)
            if name == stall_on:
                time.sleep(3600)
            if name == stop_reading_on:
                out.write(
                    json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"content": []}}) + "\n"
                )
                out.flush()
                time.sleep(3600)
            reply = {
                "content": [{"type": "text", "text": f"ok {name}"}],
                "structuredContent": {"effect": "confirmed", "tool": name, "pid": os.getpid()},
            }
        elif rid is None:
            continue
        else:
            out.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": "unknown method"},
                    }
                )
                + "\n"
            )
            out.flush()
            continue
        out.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": reply}) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
