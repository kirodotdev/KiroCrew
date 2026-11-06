"""Reconnect backoff and the host-leg close-code table.

The client classifies every end of a host connection before it retries, as the
relay contract's Appendix C keepalive-and-backoff rules ask: an authentication
failure renews the host token once and then stops, an expired token renews and
backs off, another instance holding the Crew stops, a drain reconnects at once,
and everything else, a socket that died with no close frame included, backs off.
"""

from __future__ import annotations

import enum
import random
from dataclasses import dataclass
from typing import Optional, Sequence

from kiro_crew.relay_client.protocol import (
    BACKOFF_STEPS_SECONDS,
    CLOSE_DRAIN,
    CLOSE_NO_HELLO,
    CLOSE_RATE_LIMITED,
    CLOSE_REPLACED,
    CLOSE_TOKEN_EXPIRED,
    CLOSE_TOKEN_REFUSED,
    CLOSE_TRY_AGAIN_LATER,
)


class Backoff:
    """Full-jitter backoff over fixed steps: 2, 5, 15, 30 and 60 seconds.

    Each wait is a uniform random time between 0 and the step, then the last step
    from there on. :meth:`reset` starts the steps again; the client calls it once
    a connection has reached ready and stayed up long enough.
    """

    def __init__(
        self,
        steps: Sequence[float] = BACKOFF_STEPS_SECONDS,
        *,
        rng: Optional[random.Random] = None,
    ) -> None:
        if not steps:
            raise ValueError("backoff needs at least one step")
        self._steps = tuple(float(s) for s in steps)
        self._rng = rng or random.Random()
        self._attempt = 0

    @property
    def attempt(self) -> int:
        """How many waits were handed out since the last reset."""
        return self._attempt

    def next_delay(self) -> float:
        step = self._steps[min(self._attempt, len(self._steps) - 1)]
        self._attempt += 1
        return self._rng.uniform(0.0, step)

    def reset(self) -> None:
        self._attempt = 0


class CloseAction(enum.Enum):
    """What the client does when a host connection ends."""

    #: Wait out the next backoff step, then dial again.
    BACKOFF = "backoff"
    #: Dial again at once (a drain or a move).
    RECONNECT_NOW = "reconnect_now"
    #: Ask the token source for a new host token, then back off and dial (4403).
    RENEW_THEN_BACKOFF = "renew_then_backoff"
    #: The host token was refused: renew it once and dial; stop if it is refused again.
    RENEW_ONCE = "renew_once"
    #: The relay refused this client's protocol version: stop until an upgrade.
    STOP_UPGRADE = "stop_upgrade"
    #: The relay refused the ``hello`` itself: retrying sends the same frame, so stop.
    STOP_PROTOCOL = "stop_protocol"
    #: A different instance holds this Crew's endpoint: stop.
    STOP_REPLACED = "stop_replaced"
    #: Something on this side refused to serve the connection: stop with its reason.
    STOP_LOCAL = "stop_local"


@dataclass(frozen=True)
class ConnectionEnd:
    """How one host connection ended.

    ``code`` is the WebSocket close code the relay sent, ``None`` when the socket
    died without one. ``http_status`` is set instead when the upgrade itself was
    refused. ``reached_ready`` says whether ``ready`` ever arrived.
    """

    code: Optional[int] = None
    reason: str = ""
    http_status: Optional[int] = None
    reached_ready: bool = False
    detail: str = ""
    #: Set when this side ended the connection for a reason no redial can fix.
    stop_reason: str = ""


_STOP_REASONS = {
    "version_unsupported": CloseAction.STOP_UPGRADE,
    "mode_unsupported": CloseAction.STOP_PROTOCOL,
    "invalid_message": CloseAction.STOP_PROTOCOL,
}


def classify(end: ConnectionEnd) -> CloseAction:
    """Map how a connection ended to what the client does next."""
    if end.stop_reason:
        return CloseAction.STOP_LOCAL
    if end.http_status is not None:
        # A refused upgrade: 401 is the plain answer to a host token that failed
        # its checks. Any other status (a draining task's 503, a load balancer
        # error) is try-again-later.
        return CloseAction.RENEW_ONCE if end.http_status == 401 else CloseAction.BACKOFF
    code = end.code
    if code == CLOSE_TOKEN_REFUSED:
        return _STOP_REASONS.get(end.reason, CloseAction.RENEW_ONCE)
    if code == CLOSE_TOKEN_EXPIRED:
        return CloseAction.RENEW_THEN_BACKOFF
    if code == CLOSE_REPLACED:
        return CloseAction.STOP_REPLACED
    if code == CLOSE_DRAIN:
        # A drain or a move reconnects at once, but only from a connection that
        # was serving: a 1012 before ready repeated at once would spin.
        return CloseAction.RECONNECT_NOW if end.reached_ready else CloseAction.BACKOFF
    if code in (CLOSE_NO_HELLO, CLOSE_RATE_LIMITED, CLOSE_TRY_AGAIN_LATER):
        return CloseAction.BACKOFF
    return CloseAction.BACKOFF
