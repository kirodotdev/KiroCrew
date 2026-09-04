"""Server-sent-event fan-out for the run's live activity.

The page polls for most state, but a run emits terminal cycle events (a keep, a
drafted pull request, an error) that should reach the UI immediately rather than
on the next poll tick. This is the same-origin push path for the browser's own
``EventSource``.

Why a per-client write deadline: a half-closed TCP client whose ``write`` blocks
on a full kernel send buffer must not stall delivery to every other subscriber.
With several PR watchers plus an open dashboard, one stuck client could otherwise
starve the whole stream, so each write is bounded and a client that times out is
dropped rather than retried.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

#: Live subscribers. Module-level because the stream outlives any one request.
sse_clients: set[web.StreamResponse] = set()

#: Per-client write deadline (seconds).
_BROADCAST_TIMEOUT_S = 5.0

#: Hard cap on subscribers. A page that reconnects in a loop (or several open
#: tabs) must not grow this set without bound — each entry pins a response object
#: and a socket.
_MAX_CLIENTS = 32


async def broadcast(msg: dict[str, Any]) -> None:
    """Fan ``msg`` out to every subscriber concurrently, dropping the stuck ones.

    Each client is written under its own timeout and failures are isolated, so one
    dead socket costs one delivery rather than the whole broadcast.
    """
    clients = list(sse_clients)
    if not clients:
        return
    try:
        data = f"data: {json.dumps(msg, default=str)}\n\n".encode()
    except (TypeError, ValueError):
        # An unserializable payload is a producer bug; it must not kill the stream.
        logger.warning("auto-improvement: unserializable SSE payload dropped")
        return

    async def _send(client: web.StreamResponse) -> tuple[web.StreamResponse, bool]:
        try:
            await asyncio.wait_for(client.write(data), timeout=_BROADCAST_TIMEOUT_S)
            return client, True
        except BaseException:  # noqa: BLE001 - any write failure/timeout → drop
            return client, False

    results = await asyncio.gather(*(_send(c) for c in clients), return_exceptions=True)
    for result in results:
        if isinstance(result, tuple) and not result[1]:
            sse_clients.discard(result[0])


async def stream(request: web.Request) -> web.StreamResponse:
    """Subscribe the caller to the event stream until they disconnect."""
    if len(sse_clients) >= _MAX_CLIENTS:
        # `code` is the machine-readable identity the client switches on; `error` is
        # advisory prose that a localized UI is free to replace with its own copy.
        return web.json_response(
            {"code": "sse_subscriber_limit", "error": "too many event subscribers"},
            status=503,
        )

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            # The dashboard may sit behind a reverse proxy; buffering would defeat
            # the whole point of a push channel.
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    sse_clients.add(response)
    try:
        # Hold the connection open. A comment frame doubles as a keepalive so an
        # idle stream is not reaped by an intermediary.
        while not request.transport or not request.transport.is_closing():
            await asyncio.sleep(20)
            try:
                await asyncio.wait_for(response.write(b": keepalive\n\n"), timeout=5.0)
            except BaseException:  # noqa: BLE001 - client gone
                break
    except asyncio.CancelledError:
        # Normal on shutdown or client disconnect; not an error worth logging.
        pass
    finally:
        sse_clients.discard(response)
    return response
