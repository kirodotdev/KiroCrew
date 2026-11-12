"""One SSE frame write with a deadline, shared by the send path and the
OpenAI-compatible stream.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import _SSE_WRITE_DEADLINE_SECS, logger


async def write_sse_or_abort(request: web.Request, resp: web.StreamResponse, data: bytes) -> None:
    """Write one SSE frame; abort the connection when the client stops reading.

    ``resp.write`` has no deadline of its own. A client that stops reading without
    closing its socket would park the write, and with it the stream loop and the
    slot state that loop owns (the pending queue and the broadcast hold), for as
    long as the socket stays open. Past the deadline the transport is aborted and
    the write raises ``ConnectionResetError``, so the caller's existing disconnect
    handling releases that state.
    """
    try:
        await asyncio.wait_for(resp.write(data), timeout=_SSE_WRITE_DEADLINE_SECS)
    except asyncio.TimeoutError:
        transport = request.transport
        if transport is not None:
            transport.abort()
        logger.info(
            "SSE client stopped reading for %.0fs; connection aborted", _SSE_WRITE_DEADLINE_SECS
        )
        raise ConnectionResetError("SSE client stopped reading") from None
