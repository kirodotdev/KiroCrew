"""Whether a transport's write-buffer level shows that its reader is reading.

A neutral leaf module, for the reason :mod:`kiro_crew.cold_start_sizing` and
:mod:`kiro_crew.image_refs` are leaves: two callers need this answer and only one
of them may import the ACP package. :mod:`kiro_crew.acp.transport_framing` bounds a
write to an agent's stdin by how fast the level falls, and
:mod:`kiro_crew.dashboard.websocket_hub` drops a dashboard tab whose level stops
falling. The hub is application code, which the agent-SDK boundary gate
(``scripts/check_agent_sdk_boundary.py``) forbids from importing ``kiro_crew.acp``.

The event loop decides the answer, not the transport. Under the selector loops a
transport trims its buffer as the socket or pipe accepts bytes, so the level falls
while the reader consumes. Under the proactor loop Windows runs, the level is the
one overlapped send in flight plus the bytes queued behind it
(``asyncio.proactor_events``): it stays flat while a reader that is reading takes
one large send, and it reads 0 for a reader that stopped once the kernel has taken
every byte.

Stdlib-only, so neither caller pays an import cost for it.
"""

from __future__ import annotations

import asyncio


def is_proactor_loop(loop: asyncio.AbstractEventLoop) -> bool:
    """Whether ``loop`` is Windows's proactor loop (the loop that drives sockets and
    subprocess pipes through overlapped I/O). Keyed to the public
    ``asyncio.ProactorEventLoop`` class the platform owns, which exists only on
    Windows, rather than to a private transport class name."""
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    return proactor is not None and isinstance(loop, proactor)


def level_is_progress_signal() -> bool:
    """Whether a transport's write-buffer level moves as its reader consumes, for the
    transports of the running loop.

    True under the selector loops, False under the proactor loop. Decided from the
    running loop, not from a transport's class name, so call it on the loop that
    runs the transport.
    """
    return not is_proactor_loop(asyncio.get_running_loop())
