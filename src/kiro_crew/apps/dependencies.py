"""Dependency resolver — install and clean up app dependencies.

Handles three types of capability dependencies (mcp, skills, agents) and system
command checks. Non-blocking — failures are recorded but don't prevent
app installation.

Capability dependencies resolve through the ``CapabilityManager`` CPP seam
(``platform/interfaces.py``): the edition owns which package manager backs them,
its invocation grammar, and its error translation — the core only calls an
operation and records the outcome.  The public edition ships no capability
manager (``available()`` is ``False``), so such entries are recorded as
unresolved instead of shelling out to any named binary.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.apps.dependency_ledger import (
    CAPABILITY_DEP_TYPES,
    CAPABILITY_UNCLEANABLE_TYPES,
    capability_dep_key,
    capability_dep_type,
    record_install,
    record_uninstall,
)
from kiro_crew.apps.manifest import Dependencies

logger = logging.getLogger(__name__)


@dataclass
class DependencyResult:
    """Result of dependency resolution."""

    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # missing system commands

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.installed:
            d["installed"] = self.installed
        if self.skipped:
            d["skipped"] = self.skipped
        if self.failed:
            d["failed"] = self.failed
        if self.missing:
            d["missing"] = self.missing
        return d


def _get_dep_id(entry: str | dict) -> str:
    """Extract the dependency ID from a string or object entry."""
    if isinstance(entry, dict):
        return str(entry.get("id", ""))
    return str(entry)


def _get_managed_by(entry: str | dict, default: str) -> str:
    """Get the effective managedBy for a dependency entry."""
    if isinstance(entry, dict):
        return str(entry.get("managedBy", default))
    return default


_CAPABILITY_UNAVAILABLE = "no capability manager available in this edition"

#: Dependency types the ``CapabilityManager`` seam can install/uninstall.
#: ``agents`` is declarable (it is in ``CAPABILITY_DEP_TYPES``) but has no seam op
#: — the Protocol exposes ``list_agents`` only, and package/agent install routes
#: were removed — so it is never gateway-installed and is recorded as unresolved
#: rather than silently dropped. Derived from the canonical tuple so adding a type
#: there cannot silently skip this list.
_INSTALLABLE_TYPES = tuple(
    t for t in CAPABILITY_DEP_TYPES if t not in CAPABILITY_UNCLEANABLE_TYPES
)


def _capability_manager() -> Any:
    """The edition's capability manager, or ``None`` when unavailable.

    Read through ``current_context()`` so the ``BoundedCapabilityManager``
    timeout wrapper applied at context composition is inherited (a slow
    companion op must not stall an app install).
    """
    try:
        # circular import: platform.defaults imports kiro_crew.apps.registry, so
        # apps -> platform at module scope closes the cycle. Deferred to call time,
        # the same pattern every other core seam reader uses (agent_discovery.py,
        # mcp_discovery.py, dashboard/handlers/_shared.py).
        from kiro_crew.platform.context import current_context

        mgr = current_context().capability_manager
    except Exception:
        logger.warning("capability_manager lookup failed; treating as unavailable", exc_info=True)
        return None
    try:
        if not mgr.available():
            return None
    except Exception:
        logger.warning("capability_manager.available() raised; treating as unavailable")
        return None
    return mgr


async def _capability_install(mgr: Any, dep_type: str, dep_id: str) -> tuple[bool, str]:
    """Install one dependency through the seam. Returns (ok, message)."""
    if dep_type == "mcp":
        res = await mgr.install_mcp(dep_id)
    elif dep_type == "skills":
        res = await mgr.install_skill(dep_id)
    else:  # pragma: no cover - guarded by _INSTALLABLE_TYPES
        return (False, f"no install operation for dependency type {dep_type!r}")
    return (bool(res.ok), str(res.message or ""))


async def _capability_uninstall(mgr: Any, dep_type: str, dep_id: str) -> tuple[bool, str]:
    """Uninstall one dependency through the seam. Returns (ok, message)."""
    if dep_type == "mcp":
        res = await mgr.uninstall_mcp(dep_id)
    elif dep_type == "skills":
        res = await mgr.uninstall_skill(dep_id)
    else:  # pragma: no cover - guarded by _INSTALLABLE_TYPES
        return (False, f"no uninstall operation for dependency type {dep_type!r}")
    return (bool(res.ok), str(res.message or ""))


async def resolve_dependencies(
    app_name: str,
    deps: Dependencies,
) -> DependencyResult:
    """Resolve and install app dependencies. Non-blocking — failures don't prevent install.

    For ``managedBy="gateway"`` entries: installs via the ``CapabilityManager`` seam.
    For ``managedBy="app"`` entries: only checks existence (no install).
    For ``commands``: checks ``shutil.which()`` and reports missing.

    When the edition provides no capability manager, gateway-managed capability
    entries are recorded as ``failed`` (unresolved) — the app still installs, and
    the caller surfaces the unmet dependency to the user.
    """
    result = DependencyResult()
    default_managed = deps.managedBy

    mgr: Any = None
    mgr_probed = False

    for dep_type in CAPABILITY_DEP_TYPES:
        # CAPABILITY_DEP_TYPES names the CapabilityDependencies fields 1:1; a new
        # type added there without a matching field would silently resolve to no
        # entries, so default defensively rather than raising mid-install.
        entries = getattr(deps.capabilities, dep_type, [])
        for entry in entries:
            dep_id = _get_dep_id(entry)
            if not dep_id:
                continue
            managed_by = _get_managed_by(entry, default_managed)
            dep_key = capability_dep_key(dep_type, dep_id)

            if managed_by == "app":
                # App manages this dep — just note it
                result.skipped.append(dep_key)
                continue

            if dep_type not in _INSTALLABLE_TYPES:
                result.failed.append(dep_key)
                logger.warning(
                    "Dependency %s for app %s has no capability install operation — "
                    "declare it as managedBy=app or install it out of band",
                    dep_key, app_name,
                )
                continue

            # Probe the seam once, lazily — only when there is work for it.
            if not mgr_probed:
                mgr = _capability_manager()
                mgr_probed = True
            if mgr is None:
                result.failed.append(dep_key)
                logger.info(
                    "Cannot resolve dependency %s for app %s: %s",
                    dep_key, app_name, _CAPABILITY_UNAVAILABLE,
                )
                continue

            # Gateway manages — install through the edition's capability manager
            try:
                ok, message = await _capability_install(mgr, dep_type, dep_id)
            except Exception as exc:
                result.failed.append(dep_key)
                logger.warning("Exception installing %s: %s", dep_key, exc)
                continue
            if ok:
                result.installed.append(dep_key)
                record_install(dep_key, app_name, capability_dep_type(dep_type))
                logger.info("Installed dependency %s for app %s", dep_key, app_name)
            else:
                result.failed.append(dep_key)
                logger.warning(
                    "Failed to install dependency %s for app %s: %s",
                    dep_key, app_name, message[:200],
                )

    # Check system commands
    for cmd in deps.commands:
        if shutil.which(cmd):
            result.skipped.append(f"command:{cmd}")
        else:
            result.missing.append(cmd)
            logger.info("Missing command %r for app %s", cmd, app_name)

    return result


async def clean_dependencies(
    app_name: str,
    removable_deps: list[dict[str, Any]],
) -> list[str]:
    """Uninstall removable dependencies and update the ledger.

    Args:
        app_name: The app being uninstalled.
        removable_deps: List of dep dicts from ``classify_for_uninstall()``
                        with ``id`` and ``type`` keys.

    Returns:
        List of successfully uninstalled dependency keys.
    """
    cleaned: list[str] = []
    mgr: Any = None
    mgr_probed = False

    for dep in removable_deps:
        dep_id = dep.get("id", "")
        dep_type = dep.get("type", "")
        if not dep_id:
            continue

        # dep_type is like "capability.mcp" (or a legacy-prefixed equivalent) → "mcp"
        base_type = dep_type.split(".")[-1] if "." in dep_type else ""
        if base_type not in _INSTALLABLE_TYPES:
            logger.warning(
                "Unknown dependency type %r for %s — skipping uninstall", dep_type, dep_id
            )
            continue

        if not mgr_probed:
            mgr = _capability_manager()
            mgr_probed = True
        if mgr is None:
            logger.info("Cannot uninstall %s: %s", dep_id, _CAPABILITY_UNAVAILABLE)
            continue

        # dep_id is the full ledger key ("capability/mcp/name") — take the id.
        pkg_name = dep_id.split("/")[-1] if "/" in dep_id else dep_id
        try:
            ok, message = await _capability_uninstall(mgr, base_type, pkg_name)
        except Exception as exc:
            logger.warning("Exception uninstalling %s: %s", dep_id, exc)
            continue
        if not ok:
            logger.warning("Failed to uninstall %s: %s", dep_id, message[:200])
            continue

        record_uninstall(dep_id, app_name)
        cleaned.append(dep_id)
        logger.info("Cleaned dependency %s for app %s", dep_id, app_name)

    return cleaned
