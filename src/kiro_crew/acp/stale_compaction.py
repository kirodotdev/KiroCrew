"""Discarding the late status of a compaction Crew stopped waiting for.

An idle ``/compact`` that reported nothing is cancelled and drained for a short
bound, and then the session is kept as it is
(``CompactionCoordinator._settle_idle_unconfirmed``). The harness may still send
that compaction's ``_kiro.dev/compaction/status`` afterwards, and the next
turn's dispatch loop would read it as its own: a late ``failed`` arms the
post-failure turn budget, which ends a turn silent for
``_COMPACTION_FAILED_TURN_BUDGET`` with ``STOP_REASON_COMPACTION_FAILED`` and
resets the session, and a late ``completed`` resets the context meter and
announces a compaction mid-turn.

So the idle path marks the client, and every reader of the status (the prompt
dispatch loops and ``wait_for_compaction`` on both client classes) drops what
arrives while the mark lasts: it ends at the first terminal status it drops,
once *within_secs* have passed, or when Crew sends a new ``/compact`` on that
provider, whichever comes first. The provider entry points
(``stream_command("/compact")`` and ``compact()`` on ``AcpProvider`` and
``AcpSessionProvider``) clear it, so a threshold, claude-arm, manual or channel
compaction never has its own status dropped. The residual this accepts: a
status of the abandoned compaction that arrives after a new one starts is read
as the new one's.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

_ATTR = "_stale_compaction_until"
_TERMINAL = ("completed", "failed")
#: Attributes a provider reaches its status reader through: ``AcpProvider``'s
#: ``_client`` (an ``AcpClient``, or the ``AcpSessionProvider`` the kiro path
#: swaps in) and ``AcpSessionProvider``'s ``_handle``.
_LINKS = ("_client", "client", "_handle")
_MAX_DEPTH = 3


def discard_stale_compaction_status(target: Any, *, within_secs: float) -> None:
    """Drop *target*'s compaction statuses for up to *within_secs*, or until a terminal one."""
    setattr(target, _ATTR, time.monotonic() + max(0.0, float(within_secs)))


def _readers(provider: Any) -> Iterator[Any]:
    """The ``AcpClient`` or ``AcpSessionHandle`` objects behind *provider*."""
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.session_handle import AcpSessionHandle

    seen: set[int] = set()
    frontier = [provider]
    for _ in range(_MAX_DEPTH + 1):
        nxt: list[Any] = []
        for obj in frontier:
            if obj is None or id(obj) in seen:
                continue
            seen.add(id(obj))
            if isinstance(obj, (AcpClient, AcpSessionHandle)):
                yield obj
                continue
            nxt.extend(getattr(obj, name, None) for name in _LINKS)
        frontier = nxt


def discard_provider_stale_compaction_status(provider: Any, *, within_secs: float) -> bool:
    """Mark the status reader behind *provider*; False when it fronts none.

    A dedicated process reads on its ``AcpClient``; a shared-runtime session
    reads on its ``AcpSessionHandle``, reached through ``AcpSessionProvider``'s
    ``_handle`` (and through ``AcpProvider._client`` once the kiro path swaps
    that provider in). The mark goes on the object that reads the status.
    """
    marked = False
    for target in _readers(provider):
        discard_stale_compaction_status(target, within_secs=within_secs)
        marked = True
    return marked


def clear_provider_stale_compaction_status(provider: Any) -> None:
    """End any mark behind *provider*: Crew is starting a compaction of its own."""
    for target in _readers(provider):
        if getattr(target, _ATTR, None) is not None:
            setattr(target, _ATTR, None)
            logger.info("Cleared the stale compaction mark: a new /compact starts")


def is_compact_command(command: str) -> bool:
    """Whether *command* is a ``/compact`` slash command, with or without arguments."""
    head = command.split(maxsplit=1)[:1]
    return head == ["/compact"]


def is_stale_compaction_status(target: Any, status_type: str) -> bool:
    """Whether a compaction status of *status_type* on *target* must be dropped."""
    until = getattr(target, _ATTR, None)
    if not isinstance(until, float):
        return False
    if time.monotonic() >= until:
        setattr(target, _ATTR, None)
        return False
    if status_type in _TERMINAL:
        setattr(target, _ATTR, None)
    logger.info(
        "Dropped a late compaction status %r from an abandoned idle compaction", status_type
    )
    return True
