"""Seam-boundary LIVENESS bound for the ``CapabilityManager`` extension point.

The :class:`CapabilityManager` Protocol requires that every operation be
internally time-bounded so a slow companion op cannot stall MCP handlers. As
defense-in-depth the core ALSO wraps every mutation op with ``asyncio.wait_for``
here, and — critically — applies that wrapper at **context composition time**
(``PlatformContext.__post_init__``), NOT at a leaf dashboard accessor. That way
EVERY reader of ``current_context().capability_manager`` inherits the bound: the
dashboard handlers, and any future non-dashboard consumer (subagent, conductor,
MCP-tool code, an apps backend) that follows the documented CPP pattern of
reading the context directly. Enforcing it at the seam — where the contract is
declared — closes the gap where a new call site could obtain an unbounded
manager and reintroduce the exact hang class this exists to prevent.

The constants live here (not in a dashboard module) because the bound is now a
platform-layer contract; ``mcp.py`` imports ``CAPABILITY_UNINSTALL_TIMEOUT`` to
size its pre-lock uninstall phase budget from the same source of truth.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from kiro_crew.platform.interfaces import CapabilityManager, CapabilityResult

#: Max seconds the core waits on a CapabilityManager UNINSTALL op. Tight,
#: because uninstall is quick and (in the apply path) the core wants a bounded
#: worst case.
CAPABILITY_UNINSTALL_TIMEOUT = 60

#: Max seconds the core waits on a CapabilityManager INSTALL op. Generous — a
#: legitimate install shells out to a package manager (npm/pip cold download
#: routinely exceeds 60s), and cancelling it mid-flight could leave partially
#: applied companion registry/filesystem state. Install call sites do NOT hold
#: the process-wide MCP lock, so a longer bound costs no lock availability.
CAPABILITY_INSTALL_TIMEOUT = 600

#: Max seconds the core waits on a CapabilityManager READ op
#: (``list_mcp``/``list_skills``/``list_agents``/``registry``). Reads are quick
#: (list a registry / parse installed entries), so a generous 30s still catches
#: a wedged companion. Bounding reads too keeps the LIVENESS guarantee symmetric:
#: the dashboard POLLS these list endpoints, so an unbounded stalled read would
#: accumulate pending gateway tasks on every poll — the exact wedge class the
#: seam bound exists to prevent — even though reads mutate nothing.
CAPABILITY_READ_TIMEOUT = 30


class BoundedCapabilityManager:
    """Wraps a ``CapabilityManager`` so every async MUTATION op is time-bounded
    with ``asyncio.wait_for``.

    Enforces the Protocol's LIVENESS contract ONCE at the seam boundary — the
    context is composed with this wrapper in ``PlatformContext.__post_init__``,
    so ``mcp.py`` uninstall, the ``agents.py`` capability handlers, and every
    future reader of ``current_context().capability_manager`` inherit the bound
    without having to remember it. Bounds are differentiated: uninstall ops are
    tight (:data:`CAPABILITY_UNINSTALL_TIMEOUT`), install ops are generous
    (:data:`CAPABILITY_INSTALL_TIMEOUT`) so a slow-but-legitimate package-manager
    download is not cancelled mid-mutation; the async READ ops
    (``list_*``/``registry``) are bounded by the tight
    :data:`CAPABILITY_READ_TIMEOUT`. Bounding reads too keeps the guarantee
    symmetric — the dashboard polls the list endpoints, so a stalled unbounded
    read would accumulate pending gateway tasks, the same wedge class the bound
    exists to prevent. Only the sync ``available()`` probe passes straight
    through (it does no I/O).

    An explicit proxy (not a ``__getattr__`` delegator) so a Protocol method
    added later surfaces as a loud ``AttributeError`` here rather than silently
    slipping past the bound — a maintenance trap is preferable to a silent hole
    in the liveness guarantee.
    """

    def __init__(self, inner: "CapabilityManager") -> None:
        self._inner = inner

    def available(self) -> bool:
        return self._inner.available()

    async def list_mcp(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(
            self._inner.list_mcp(), timeout=CAPABILITY_READ_TIMEOUT
        )

    async def list_skills(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(
            self._inner.list_skills(), timeout=CAPABILITY_READ_TIMEOUT
        )

    async def list_agents(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(
            self._inner.list_agents(), timeout=CAPABILITY_READ_TIMEOUT
        )

    async def registry(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(
            self._inner.registry(), timeout=CAPABILITY_READ_TIMEOUT
        )

    async def install_mcp(self, server_id: str) -> "CapabilityResult":
        return await asyncio.wait_for(
            self._inner.install_mcp(server_id), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def uninstall_mcp(self, server_id: str) -> "CapabilityResult":
        return await asyncio.wait_for(
            self._inner.uninstall_mcp(server_id), timeout=CAPABILITY_UNINSTALL_TIMEOUT
        )

    async def install_skill(self, package: str) -> "CapabilityResult":
        return await asyncio.wait_for(
            self._inner.install_skill(package), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def uninstall_skill(self, package: str) -> "CapabilityResult":
        return await asyncio.wait_for(
            self._inner.uninstall_skill(package), timeout=CAPABILITY_UNINSTALL_TIMEOUT
        )


def bind_capability_manager(inner: "CapabilityManager") -> "CapabilityManager":
    """Return *inner* wrapped in :class:`BoundedCapabilityManager` (idempotent).

    Called from ``PlatformContext.__post_init__`` on every composition, including
    the companion's ``dataclasses.replace`` path. Idempotent so a ``replace``
    that copies an already-bounded manager forward is not double-wrapped.
    """
    if isinstance(inner, BoundedCapabilityManager):
        return inner
    return BoundedCapabilityManager(inner)
