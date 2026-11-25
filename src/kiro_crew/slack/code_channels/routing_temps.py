"""A code channel's attachment temp files: who holds them, and their delete.

A message routed to a bound session carries its attachments as temp-file
paths in the prompt. The files must outlive the routing call (the session's
turn reads them, perhaps after earlier queued turns) and must not outlive the
turns that read them. A path is adopted under the session when the message is
routed, and the session's files are deleted at the end of a turn that leaves
nothing queued behind it -- so a queued message's upload is never deleted by
the turn before it.

Split out of :mod:`.routing` (the file-line cap); :mod:`.routing` keeps every
name as the import path callers use, and the adopt and turn-end steps.
"""

from __future__ import annotations

import contextvars
import logging
import os
from collections.abc import Sequence

logger = logging.getLogger("kiro_crew.slack.code_channels.routing")

_INBOUND_TEMPS: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
    "code_channel_inbound_temps", default=[]
)
_PENDING_TEMPS: dict[str, list[str]] = {}


def _unlink_all(paths: Sequence[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # No path: the file is the user's upload.
            logger.warning(
                "code channel: could not delete an attachment temp file: %s errno=%s winerror=%s",
                type(exc).__name__,
                exc.errno,
                getattr(exc, "winerror", None),
            )
