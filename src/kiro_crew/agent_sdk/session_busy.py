"""Provider-neutral signal that a running turn kept a session's transport.

A backend driver raises a subclass of :class:`SessionTurnBusy` when a control
request (an effort change, for example) waited its bounded time for the
session's transport and a running turn still owned it. Nothing was sent to the
session, so the caller may record the change for later or retry once the turn
ends. Application code catches this class instead of the driver's own
exception, which keeps it off the backend packages.
"""

from __future__ import annotations


class SessionTurnBusy(Exception):
    """A running turn owned the session's transport past the caller's deadline."""


__all__ = ["SessionTurnBusy"]
