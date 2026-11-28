"""``kirocrew mcp`` -- operate the MCP gateway daemon's pooled servers.

``kirocrew mcp reload <server>`` retires one server's pooled backends through
the daemon's ``recycle-server`` control frame
(:func:`kiro_crew.mcp_gateway.daemon_control.recycle_server`), so the next
session to attach starts a fresh process. The daemon and every other server
keep running. Adding, editing or removing a server in ``mcp.json`` already
applies without this; it exists for a boot-time input the pool key cannot see,
such as a token the server reads from a file only when it starts.
"""

from __future__ import annotations

import argparse
import sys

from kiro_crew.mcp_gateway import daemon_control


def _reload(server: str) -> int:
    result = daemon_control.recycle_server(server)
    if result.outcome == "recycled":
        if result.evicted == result.draining == result.private == 0:
            print(
                f"No running backends for MCP server '{server}'. "
                "The next session to use it starts a fresh process."
            )
            return 0
        print(f"Recycled MCP server '{server}':")
        print(f"  stopped now (idle):       {result.evicted}")
        print(
            f"  finishing in-flight work: {result.draining}"
            + (f" (stopped within {int(result.deadline_secs)}s)" if result.draining else "")
        )
        if result.private:
            print(
                f"  session-private:          {result.private} " "(kept until their sessions end)"
            )
        print("The next session to use it starts a fresh process.")
        return 0
    if result.outcome == daemon_control.PROBE_ABSENT:
        print(
            "The MCP gateway daemon is not running, so there is nothing to recycle. "
            "Servers start fresh with the next session.",
            file=sys.stderr,
        )
        return 1
    if result.outcome == "rejected":
        print(f"The MCP gateway daemon refused the reload: {result.reason}", file=sys.stderr)
        return 1
    print(
        f"Could not reach the MCP gateway daemon ({result.outcome}). "
        "Run `kirocrew doctor` to check it.",
        file=sys.stderr,
    )
    return 1


def mcp_cmd(args: argparse.Namespace) -> int:
    """Dispatch ``kirocrew mcp <action>``; returns the process exit code."""
    if getattr(args, "mcp_action", None) == "reload":
        return _reload(args.server)
    print("usage: kirocrew mcp reload <server>", file=sys.stderr)
    return 2
