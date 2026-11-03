"""The manager's peer-owned (remote) run records.

A remote run is a shadow record the placement service owns: it joins the local
inventory, the parent's pending work and wave settlement, and never this host's
process count, memory sizing, reaper or task queue. These are the manager's calls
into that record set and into the placement service bound to it.

Each function takes the manager as ``self`` and is bound as the ``SubagentManager``
attribute of the same name in :mod:`kiro_crew.subagent_parts.manager`.

Manager fields read: ``_external_agents``, ``_external_placement``.

Manager fields written: ``_external_agents``, ``_batch_submitted``,
``_batch_progress_ts``, ``_external_placement``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import time

    from kiro_crew.subagent import (
        ExternalPlacement,
        SubagentInfo,
        SubagentManager,
    )


def register_external(self: SubagentManager, info: SubagentInfo) -> None:
    """Register a peer-owned run in the local inventory.

    The remote execution service has already obtained a peer run id before
    calling this method. Registration is synchronous so a completion poll
    cannot race ahead of visibility in ``spawn_list``.
    """
    if info.executor != "remote" or not info.instance_id or not info.remote_id:
        raise ValueError("external subagent record has an incomplete remote binding")
    if info.id in self._agents or info.id in self._external_agents:
        raise ValueError(f"subagent id already registered: {info.id}")
    self._external_agents[info.id] = info
    if info.batch_id:
        submitted = self._batch_submitted.setdefault(
            info.batch_id, [0, max(0, int(info.batch_total))]
        )
        submitted[0] += 1
        # Monotonic, as every other writer: the stuck-wave sweep subtracts
        # this from a monotonic ``now``.
        self._batch_progress_ts[info.batch_id] = time.monotonic()


async def report_external(self: SubagentManager, info: SubagentInfo) -> bool:
    """Deliver one peer-owned terminal result through the normal reporter."""
    if self._external_agents.get(info.id) is not info:
        return False
    if not self._claim_finalize(info):
        return False
    return await self._run_terminal_report(
        info,
        source="Remote subagent",
        injection_timeout_reason="remote completion delivery timed out",
        mark_delivered_on_success=False,
        settle_digest=True,
    )


def forget_external(self: SubagentManager, agent_id: str) -> SubagentInfo | None:
    """Remove one terminal remote shadow record from local inventory."""
    return self._external_agents.pop(agent_id, None)


def bind_external_placement(self: SubagentManager, placement: ExternalPlacement | None) -> None:
    """Bind (or with ``None`` unbind) the service that owns peer-run records."""
    self._external_placement = placement


async def cancel_external(self: SubagentManager, agent_id: str) -> bool:
    """Stop one live peer-owned run through its bound placement service."""
    info = self._external_agents.get(agent_id)
    if info is None or info.done:
        return False
    placement = getattr(self, "_external_placement", None)
    if placement is None:
        raise RuntimeError("remote subagent cancellation is unavailable")
    cancelled = await placement.cancel(info)
    if cancelled:
        info.user_stopped = True
    return cancelled
