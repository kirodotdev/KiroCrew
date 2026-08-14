"""Channel registry: descriptors + the boot/shutdown loops (PR ③ of the RFC).

One registry entry per channel replaces the hand-edited seams that adding a
channel used to require in the host. This module owns the TYPES and the LOOPS
only — it must not import any channel package (``dispatch.py`` pins the
dependency direction: ``<channel> -> messaging``, never the reverse). The one
place that knows every builtin channel is :mod:`kiro_crew.channels`, which sits
ABOVE both this package and the channel packages.

Two views over the same descriptor list, because Slack is deliberately split:

* :func:`governed_members` — ALL channels, including Slack. Governance
  membership (the ``channels`` scope) covers every transport.
* the boot loop (:func:`start_channels`) — only descriptors with a ``start``
  factory. Slack's is ``None`` because it owns its own socket-client lifecycle
  (a governance deny must DROP that client, not merely skip a start call — see
  ``_connect_slack``), so the host starts it separately, after the others,
  preserving today's boot order.

Descriptor honesty rule (learned from the capability truth pass): a field
exists here ONLY if something consumes it in this change. Config schemas,
credential field names for the sandbox denylist, and capability attachment are
REAL parts of the RFC's descriptor design but land with their consumers (the
config-schema PR), not as decorative fields nothing reads.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

#: A channel start factory: takes the gateway orchestrator, returns the live
#: client handle (or ``None`` when the channel is disabled/uncredentialed —
#: every factory is a guarded no-op). The loose ``Any`` for the orchestrator is
#: deliberate for this PR: the factories predate the registry and read
#: pre-hoisted ``orch._<channel>_*`` attributes. The config-schema PR replaces
#: this back-reference with a narrow ``ChannelBootContext``; widening the type
#: now would freeze the wrong contract.
StartFactory = Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True)
class ChannelDescriptor:
    """Everything the host needs to know about one channel, in one place.

    ``channel_type`` is the single identity used EVERYWHERE: the governance
    member id, ``MessagingTransport.channel_type``, the session-key surface
    segment, the config section name, and the dashboard badge prefix. The
    contract tests pin that these never diverge.
    """

    channel_type: str
    """Identity. Must equal the transport class's ``channel_type``."""

    start: Optional[StartFactory] = None
    """Boot factory, or ``None`` for a host-managed lifecycle (Slack)."""


def governed_members(descriptors: tuple[ChannelDescriptor, ...]) -> tuple[str, ...]:
    """Every channel's governance member id — INCLUDING host-managed ones."""
    return tuple(d.channel_type for d in descriptors)


def bootable(descriptors: tuple[ChannelDescriptor, ...]) -> tuple[ChannelDescriptor, ...]:
    """The descriptors the registry boot loop starts (``start`` is not None)."""
    return tuple(d for d in descriptors if d.start is not None)


async def start_channels(
    orch: Any,
    descriptors: tuple[ChannelDescriptor, ...],
    permitted: Mapping[str, bool],
) -> dict[str, Any]:
    """Start every bootable, governance-permitted channel; return live handles.

    ``permitted`` is computed by the CALLER (off the event loop — the
    governance check does blocking profile-file I/O) and a member absent from
    it counts as not-permitted, preserving the enabled-only-eval semantics:
    a disabled channel is never evaluated, never starts, and never emits a
    spurious deny audit.

    Failure isolation is per-channel BY the factories themselves (each
    ``maybe_start_*`` catches, badges the error, and returns ``None``), so a
    raise escaping one factory here is unexpected; it is logged and the
    remaining channels still start rather than aborting the block.
    """
    handles: dict[str, Any] = {}
    for desc in bootable(descriptors):
        if not permitted.get(desc.channel_type, False):
            continue
        assert desc.start is not None  # bootable() guarantees this
        try:
            client = await desc.start(orch)
        except Exception:
            logger.exception("channel registry: %s failed to start", desc.channel_type)
            client = None
        # Legacy attribute kept in sync for existing readers/tests; the dict is
        # the authority and the attributes retire with the config-schema PR.
        setattr(orch, f"_{desc.channel_type}_client", client)
        if client is not None:
            handles[desc.channel_type] = client
    return handles


def shutdown_tasks(handles: Mapping[str, Any], *, timeout: float = 2.0) -> list[Awaitable[Any]]:
    """Bounded ``close()`` awaitables for every live handle, in one place.

    Mirrors the shape the gateway's cleanup gather expects. Handles without a
    ``close`` attribute are skipped with a warning rather than raising — a
    shutdown path must never be the thing that fails shutdown.
    """
    tasks: list[Awaitable[Any]] = []
    for channel_type, client in handles.items():
        close = getattr(client, "close", None)
        if close is None:
            logger.warning("channel registry: %s client has no close()", channel_type)
            continue
        tasks.append(asyncio.wait_for(close(), timeout=timeout))
    return tasks
