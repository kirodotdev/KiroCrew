"""A privileged native Crew backend must remain behind its host gateway."""

from __future__ import annotations

import json
import os
from typing import Any

_CLASSES = frozenset({"connection_failed", "launch_approval_required", "identity_unattested"})


class GatewayRequiredError(Exception):
    def __init__(self, error_class: str = "connection_failed") -> None:
        self.error_class = (
            error_class
            if isinstance(error_class, str) and error_class in _CLASSES
            else "connection_failed"
        )
        super().__init__(self.error_class)


class NativeLaunchApprovalRequired(PermissionError):
    pass


def required(args: Any) -> bool:
    if not (
        os.environ.get("KIROCREW_MCP_REQUIRE_GATEWAY") == "1"
        or os.environ.get("KIROCREW_NATIVE_MCP_SCOPE_TOKEN")
    ):
        return False
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

    return args.server in KIROCREW_BIN_MCP_SERVERS


async def serve_refusal(error_class: str) -> int:
    import asyncio
    import sys
    import threading

    from kiro_crew.mcp_gateway.stub_bridge import StubSession

    error_class = GatewayRequiredError(error_class).error_class
    remedies = {
        "connection_failed": "Restore the Crew MCP gateway and start a new session.",
        "launch_approval_required": "Approve this server launch in Crew, then start a new session.",
        "identity_unattested": "Start a new Crew session to obtain a verified session identity.",
    }
    session = StubSession()
    # A failed handoff must end the stub, including a silent/invalid client.
    # Windows then closes even a handle duplicated after our handoff timeout.
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return 1
        try:
            line = await asyncio.wait_for(session.next_line(), remaining)
        except asyncio.TimeoutError:
            return 1
        if not line:
            return 0
        try:
            frame = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(frame, dict) or "id" not in frame or "method" not in frame:
            continue
        response = {
            "jsonrpc": "2.0",
            "id": frame["id"],
            "error": {
                "code": -32002,
                "message": "Crew gateway required. " + remedies[error_class],
                "data": {"class": error_class, "gateway_required": True, "direct_fallback": False},
            },
        }
        data = (json.dumps(response, separators=(",", ":")) + "\n").encode()

        loop = asyncio.get_running_loop()
        settled = loop.create_future()

        def finish() -> None:
            if not settled.done():
                settled.set_result(None)

        def write() -> None:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    loop.call_soon_threadsafe(finish)
                except RuntimeError:
                    pass

        # Like StubSession's reader, this must not hold asyncio.run's default
        # executor shutdown hostage when the MCP client stops reading stdout.
        threading.Thread(target=write, name="native-refusal-stdout", daemon=True).start()
        try:
            await asyncio.wait_for(settled, max(0.001, deadline - loop.time()))
        except asyncio.TimeoutError:
            pass
        return 1
