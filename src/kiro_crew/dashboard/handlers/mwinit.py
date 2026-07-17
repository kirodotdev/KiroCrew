"""WebSocket PTY handler for mwinit — not available in the OSS build (stub).

The original handler spawned ``mwinit -o -s`` over a PTY with an RSA-OAEP
encrypted input channel for Amazon Midway authentication. That flow is
Amazon-internal and is not available in the OSS distribution. This stub keeps
the ``api_mwinit_ws`` symbol (routed in ``dashboard/server.py`` and re-exported
from ``dashboard/handlers/__init__``), accepts the WebSocket, reports that the
feature is unavailable, and closes the connection gracefully. No subprocess is
spawned.
"""

from __future__ import annotations

from aiohttp import web


def _sel():
    import kiro_crew.dashboard.handlers as _pkg
    return _pkg.sel()


async def api_mwinit_ws(request: web.Request) -> web.WebSocketResponse | web.Response:
    """WebSocket endpoint stub — mwinit auth is not available in the OSS build.

    Accepts the WebSocket, sends ``{"error": "not available in OSS"}``, and
    closes gracefully. No PTY and no ``mwinit`` subprocess are involved.
    """
    caller = request.get("user")
    if not caller:
        _sel().log_api_access(
            caller="unknown", operation="mwinit.ws.open",
            outcome="denied", source="dashboard",
            resources=str(request.remote),
        )
        return web.Response(status=401, text="Unauthorized")

    ws = web.WebSocketResponse(heartbeat=30, timeout=300)
    await ws.prepare(request)
    _sel().log_api_access(
        caller=caller, operation="mwinit.ws.open",
        outcome="ok", source="dashboard",
        resources=str(request.remote),
    )
    try:
        await ws.send_json({"error": "not available in OSS"})
    finally:
        await ws.close()
    return ws
