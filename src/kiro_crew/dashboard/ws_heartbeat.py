"""A dashboard socket whose heartbeat does not drop a peer for the loop's own stall.

aiohttp's heartbeat sends a PING and arms ``_pong_not_received`` half a heartbeat
later on the loop's clock. When the event loop stalls past that deadline, the PONG
the peer sent in time is still unread in the kernel when the loop resumes: reading
it only schedules the heartbeat reset for the next loop pass, the expired timer runs
first, and a live socket is closed with 1006. Every tab whose socket was waiting on
a PONG then reconnects at once.

:class:`LagTolerantWebSocketResponse` starts the PONG window again instead of
closing when the timer runs more than :data:`PONG_LATE_TOLERANCE_S` past its
deadline (the loop was not running when the window ended, so the peer never had
it) or when data has arrived and its reset is queued (the peer answered). A timer
that runs on time with nothing received still closes the socket, so a dead peer is
dropped at most one window later per stall.
"""

from __future__ import annotations

from aiohttp import web

#: How late past its deadline the PONG timer may run and still have given the peer
#: its window. A healthy loop runs a timer within milliseconds; this is far above
#: that and far below the window itself (15 s for a 30 s heartbeat).
PONG_LATE_TOLERANCE_S = 1.0


class LagTolerantWebSocketResponse(web.WebSocketResponse):
    """``web.WebSocketResponse`` whose PONG timeout ignores a stall of its own loop."""

    def _pong_not_received(self) -> None:
        loop = self._loop
        deadline = self._pong_response_cb
        if loop is not None and deadline is not None and self._heartbeat is not None:
            now = loop.time()
            # ``_need_heartbeat_reset`` is aiohttp's own flag for data read this
            # pass whose reset runs next pass; versions without it read as False.
            answered = bool(getattr(self, "_need_heartbeat_reset", False))
            if answered or now - deadline.when() > PONG_LATE_TOLERANCE_S:
                self._pong_response_cb = loop.call_at(
                    now + self._pong_heartbeat, self._pong_not_received
                )
                return
        super()._pong_not_received()
