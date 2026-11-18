"""A Bot's capability read, bounded and logged, for the GET routes that wait on it.

The Abilities pane, its worker-kit read and the ``get_member_capabilities`` guide
tool each wait on a read that can outlast them: it stats, resolves and matches
files, so a hung mount or a large catalog holds it for as long as they take. The
route gives up at :data:`GET_TIMEOUT_SECS` and answers 503
``capabilities_unavailable``; the read is left to its thread, since a GET writes
nothing. Both outcomes are logged with the Bot and the time spent, so the
gateway log says which Bot was slow and by how much.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# The module's own clock binding, so a test can move it without moving the
# event loop's.
_monotonic = time.monotonic

# How long a capability GET waits for its read. Shorter than the dashboard's own
# deadline on the same read (``ABILITIES_READ_TIMEOUT_MS``), so the coded 503 is
# what the pane normally shows.
GET_TIMEOUT_SECS = 20.0

#: The ``reason`` a 503 carries when the read did not finish in time, so the
#: pane can say why instead of only that it failed.
TIMED_OUT = "timed_out"


async def bounded_read(
    read: Callable[[str], _T], member: str, *, what: str, timeout: float | None = None
) -> _T:
    """``read(member)`` off the event loop; ``TimeoutError`` after *timeout* seconds.

    *timeout* defaults to :data:`GET_TIMEOUT_SECS`, read at call time. A read
    past the deadline is logged when the route gives up, and again with its
    real duration when it finishes.
    """
    limit = GET_TIMEOUT_SECS if timeout is None else timeout
    started = _monotonic()

    def _run() -> _T:
        try:
            return read(member)
        finally:
            elapsed = _monotonic() - started
            if elapsed > limit:
                logger.warning(
                    "%s read for Bot %r finished after %.1f s, past the %.0f s limit",
                    what,
                    member,
                    elapsed,
                    limit,
                )

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), limit)
    except TimeoutError:
        logger.warning(
            "%s read for Bot %r did not finish in %.0f s; answered 503 capabilities_unavailable",
            what,
            member,
            limit,
        )
        raise


def timed_out_for_model(member: str) -> str:
    """The ``error`` a model-facing route answers when the read timed out.

    The ``get_member_capabilities`` tool hands ``error`` to the model as is, so
    it says what happened and what to tell the user: a bare code invites a
    retry loop on a read that will time out the same way.
    """
    return (
        f"capabilities_unavailable: reading the abilities of Bot {member!r} did not "
        f"finish in {GET_TIMEOUT_SECS:.0f} s. A Bot with many added abilities, or a "
        "folder on a slow or unreachable drive, can take this long. Do not retry in "
        "a loop. Tell the user the read timed out, that the Bot editor's Abilities "
        "pane has a Try again button, and that the gateway log names the Bot and "
        "how long the read took."
    )
