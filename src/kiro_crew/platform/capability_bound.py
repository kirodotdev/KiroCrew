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
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from kiro_crew import extension_governance as _gov
from kiro_crew.mcp_utils import registry_accepts_query

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


async def _off_loop(fn: Any, *args: str) -> "str | None":
    """Run a governance check on the governance executor, off the event loop.

    A check reads the profile store, which stats the profiles directory.
    """
    from kiro_crew.executors import governance_executor

    return await asyncio.get_running_loop().run_in_executor(governance_executor(), fn, *args)


#: A server or skill id as a direct install names it: one path segment.
_INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MCP_TARGET_PREFIX = "mcpServers."


def market_install_id(item: object) -> Optional[str]:
    """The id a marketplace plan item is installed as, or ``None`` if unreadable.

    That is the name a direct ``install_mcp`` / ``install_skill`` uses, so
    governance can ask both install paths under the same list entry. An edition
    may say it outright as ``installId``; otherwise it is read from ``target``:
    ``mcpServers.<id>`` for an MCP item, the last path segment of the folder for
    a skill. Only ``mcp`` and ``skill`` items have one.
    """
    if not isinstance(item, dict) or item.get("kind") not in ("mcp", "skill"):
        return None
    explicit = item.get("installId")
    if explicit is not None:
        ident = explicit if isinstance(explicit, str) else ""
    else:
        target = item.get("target")
        if not isinstance(target, str):
            return None
        if item["kind"] == "mcp":
            if not target.startswith(_MCP_TARGET_PREFIX):
                return None
            ident = target[len(_MCP_TARGET_PREFIX) :]
        else:
            ident = target.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if ".." in ident or not _INSTALL_ID_RE.match(ident):
        return None
    return ident


def _audit_theme_ceiling(outcome: str, decision: object, reason: str = "") -> None:
    """SEL row for the theme-install decision, as the Themes page writes it. Never raises."""
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key="",
            tool_name="capability_market_apply",
            scope="capabilities.theme_install",
            outcome=outcome,
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=reason or getattr(decision, "reason", ""),
        )
    except Exception:  # pragma: no cover - an audit failure never changes the outcome
        pass


def _theme_install_denied() -> Optional[str]:
    """Why ``capabilities.theme_install`` refuses a theme, or ``None``. Fail-closed.

    The same decision ``dashboard/handlers/themes.py`` asks before a pack install;
    an evaluation error denies. Reads the profile store: call it off the loop.
    """
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.theme_install", "", log_warning=False, fail_closed=True
        )
        permitted = bool(getattr(decision, "permitted", False))
        _audit_theme_ceiling("allowed" if permitted else "denied", decision)
        if permitted:
            return None
        return str(getattr(decision, "reason", "") or "theme installation disabled by policy")
    except Exception:
        reason = "theme installation blocked (governance unavailable)"
        _audit_theme_ceiling("denied", None, reason=reason)
        return reason


def _refused(reason: str) -> "CapabilityResult":
    """The result an op returns when governance refuses it (``extension_governance``).

    Checked here, at the seam every reader of ``current_context().capability_manager``
    goes through, so no call site can reach the companion's installer around it. In
    an enterprise build the ``capabilities.extension_install`` baseline is off, so
    the extension signature and version rules that only apply to an install are off
    with it (RFC enterprise edition, Red "Signed extensions only" and "Extension
    versions").
    """
    from kiro_crew.platform.interfaces import CapabilityResult

    return CapabilityResult(ok=False, message=f"Blocked by governance policy: {reason}")


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
        return await asyncio.wait_for(self._inner.list_mcp(), timeout=CAPABILITY_READ_TIMEOUT)

    async def list_skills(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(self._inner.list_skills(), timeout=CAPABILITY_READ_TIMEOUT)

    async def list_agents(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(self._inner.list_agents(), timeout=CAPABILITY_READ_TIMEOUT)

    async def registry(self, query: "str | None" = None) -> List[Dict[str, Any]]:
        """Forward the optional search hint, but only to an inner op that takes it.

        The wrapper must advertise ``query`` in its OWN signature: every caller
        reaches the manager through this bind, so a wrapper that dropped the
        parameter would make the hint undetectable and every edition registry
        unsearchable past the caller's row guard. Forwarding is feature-detected
        so an edition still on the zero-arg signature is called the old way
        instead of raising ``TypeError`` — the hint is dropped, which the caller's
        own filter already tolerates.
        """
        inner = self._inner.registry
        call = inner(query=query) if query and registry_accepts_query(inner) else inner()
        return await asyncio.wait_for(call, timeout=CAPABILITY_READ_TIMEOUT)

    async def install_mcp(self, server_id: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extension_install_denied, "mcp", server_id)
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.install_mcp(server_id), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def uninstall_mcp(self, server_id: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extensions_off, "uninstall_mcp")
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.uninstall_mcp(server_id), timeout=CAPABILITY_UNINSTALL_TIMEOUT
        )

    async def install_skill(self, package: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extension_install_denied, "skill", package)
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.install_skill(package), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def uninstall_skill(self, package: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extensions_off, "uninstall_skill")
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.uninstall_skill(package), timeout=CAPABILITY_UNINSTALL_TIMEOUT
        )

    async def install_agent(self, package: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extension_install_denied, "agent", package)
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.install_agent(package), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    async def uninstall_agent(self, package: str) -> "CapabilityResult":
        denied = await _off_loop(_gov.extensions_off, "uninstall_agent")
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.uninstall_agent(package), timeout=CAPABILITY_UNINSTALL_TIMEOUT
        )

    async def list_plugins(self) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(self._inner.list_plugins(), timeout=CAPABILITY_READ_TIMEOUT)

    async def plugins_out_of_sync(self) -> List[str]:
        return await asyncio.wait_for(
            self._inner.plugins_out_of_sync(), timeout=CAPABILITY_READ_TIMEOUT
        )

    async def sync_plugins(self) -> "CapabilityResult":
        # An INSTALL bound: reconciling drift can shell a package manager once
        # per drifted package, so it inherits the generous install budget rather
        # than the tight uninstall one.
        # Each package the sync would install is asked as ``plugin:<package>``.
        # Off denies before the drift read, so a pinned-off host makes no call.
        denied = await _off_loop(_gov.extensions_off, "sync_plugins")
        if not denied:
            for package in await self.plugins_out_of_sync():
                denied = await _off_loop(_gov.extension_install_denied, "plugin", str(package))
                if denied:
                    break
        if denied:
            return _refused(denied)
        return await asyncio.wait_for(
            self._inner.sync_plugins(), timeout=CAPABILITY_INSTALL_TIMEOUT
        )

    def market_available(self) -> bool:
        # Feature-detected, unlike every other op: an edition written before the
        # marketplace ops has none of them, and must keep working unchanged
        # rather than fail every marketplace route with an AttributeError.
        probe = getattr(self._inner, "market_available", None)
        return bool(self._inner.available() and callable(probe) and probe())

    async def market_plan(self, ref: str) -> Dict[str, Any]:
        # A read: resolving a plan writes nothing, so governance is asked at apply.
        if not self.market_available():
            return {}
        return await asyncio.wait_for(self._inner.market_plan(ref), timeout=CAPABILITY_READ_TIMEOUT)

    async def market_apply(self, ref: str, plan_digest: str) -> "CapabilityResult":
        # A plan can carry several kinds at once (a skill with the MCP server it
        # needs), so each item is asked as its own ``extensions:<kind>:<ref>``,
        # the way plugin sync asks each drifted package. The plan is re-read here
        # and must still carry the confirmed digest, so the items asked are the
        # items the edition is told to install: it refuses any other plan.
        # A theme item is asked of capabilities.theme_install, below.
        denied = await _off_loop(_gov.extensions_off, "market_apply")
        if denied:
            return _refused(denied)
        from kiro_crew.platform.interfaces import CapabilityResult

        plan = await self.market_plan(ref)
        if not isinstance(plan, dict) or plan.get("planDigest") != plan_digest:
            return CapabilityResult(
                ok=False,
                message="the plan changed since it was shown; nothing was installed",
            )
        items = plan.get("items")
        items = items if isinstance(items, list) else []
        # A theme serves sandboxed JS into the dashboard, so a theme in a listing
        # (alone or bundled) answers to the same capabilities.theme_install
        # ceiling as a theme installed from the Themes page.
        if any(isinstance(i, dict) and i.get("kind") == "theme" for i in items):
            denied = await _off_loop(_theme_install_denied)
            if denied:
                return _refused(denied)
        for item in items:
            kind = item.get("kind") if isinstance(item, dict) else None
            if kind not in ("mcp", "skill"):
                continue
            # Asked under the id the item is installed as (the name a direct
            # install_mcp / install_skill asks under) AND under the listing ref,
            # so a list entry written either way binds both install paths. An
            # item whose installed id cannot be read is refused (fail closed).
            install_id = market_install_id(item)
            if install_id is None:
                return CapabilityResult(
                    ok=False,
                    message=f"cannot tell what {kind} the plan installs; nothing was installed",
                )
            for ident in (install_id, str(item.get("ref", ""))):
                denied = await _off_loop(_gov.extension_install_denied, kind, ident)
                if denied:
                    return _refused(denied)
        return await asyncio.wait_for(
            self._inner.market_apply(ref, plan_digest), timeout=CAPABILITY_INSTALL_TIMEOUT
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
