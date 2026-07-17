"""Dependency resolver — install and clean up app dependencies via AIM CLI.

Handles three types of AIM dependencies (mcp, skills, agents) and system
command checks. Non-blocking — failures are recorded but don't prevent
app installation.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.apps.dependency_ledger import record_install, record_uninstall
from kiro_crew.apps.manifest import Dependencies

logger = logging.getLogger(__name__)

_AIM_TIMEOUT = 120  # seconds per aim install command


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


async def _run_aim(*args: str, timeout: int = _AIM_TIMEOUT) -> tuple[int, str]:
    """Run an ``aim`` CLI command. Returns (returncode, output)."""
    aim = shutil.which("aim")
    if not aim:
        return (1, "aim CLI not found")
    proc = await asyncio.create_subprocess_exec(
        aim, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        return (1, f"aim command timed out after {timeout}s")
    return (proc.returncode or 0, (out or b"").decode(errors="replace"))


# Map dependency type to aim CLI subcommand
_AIM_TYPE_MAP = {
    "mcp": ("mcp", "install"),
    "skills": ("skills", "install"),
    "agents": ("agents", "install"),
}

_AIM_UNINSTALL_MAP = {
    "mcp": ("mcp", "uninstall"),
    "skills": ("skills", "uninstall"),
    "agents": ("agents", "uninstall"),
}


async def resolve_dependencies(
    app_name: str,
    deps: Dependencies,
) -> DependencyResult:
    """Resolve and install app dependencies. Non-blocking — failures don't prevent install.

    For ``managedBy="gateway"`` entries: runs ``aim install``.
    For ``managedBy="app"`` entries: only checks existence (no install).
    For ``commands``: checks ``shutil.which()`` and reports missing.
    """
    result = DependencyResult()
    default_managed = deps.managedBy

    # Process AIM dependencies
    for dep_type, entries in [
        ("mcp", deps.aim.mcp),
        ("skills", deps.aim.skills),
        ("agents", deps.aim.agents),
    ]:
        aim_cmds = _AIM_TYPE_MAP.get(dep_type)
        if not aim_cmds:
            continue
        for entry in entries:
            dep_id = _get_dep_id(entry)
            if not dep_id:
                continue
            managed_by = _get_managed_by(entry, default_managed)
            dep_key = f"aim/{dep_type}/{dep_id}"

            if managed_by == "app":
                # App manages this dep — just note it
                result.skipped.append(dep_key)
                continue

            # Gateway manages — try to install via AIM
            try:
                rc, output = await _run_aim(aim_cmds[0], aim_cmds[1], dep_id)
            except Exception as exc:
                result.failed.append(dep_key)
                logger.warning("Exception installing %s: %s", dep_key, exc)
                continue
            if rc == 0:
                result.installed.append(dep_key)
                record_install(dep_key, app_name, f"aim.{dep_type}")
                logger.info("Installed dependency %s for app %s", dep_key, app_name)
            else:
                result.failed.append(dep_key)
                logger.warning(
                    "Failed to install dependency %s for app %s: %s",
                    dep_key, app_name, output[:200],
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
    for dep in removable_deps:
        dep_id = dep.get("id", "")
        dep_type = dep.get("type", "")
        if not dep_id:
            continue

        # Parse type to get aim subcommand
        # dep_type is like "aim.mcp" → we need "mcp"
        aim_type = dep_type.split(".")[-1] if "." in dep_type else ""
        aim_cmds = _AIM_UNINSTALL_MAP.get(aim_type)

        if not aim_cmds:
            logger.warning("Unknown dependency type %r for %s — skipping uninstall", dep_type, dep_id)
            continue

        # Extract the package name from the dep_id (which is the full key like "aim/mcp/name")
        pkg_name = dep_id.split("/")[-1] if "/" in dep_id else dep_id
        try:
            rc, output = await _run_aim(aim_cmds[0], aim_cmds[1], pkg_name)
        except Exception as exc:
            logger.warning("Exception uninstalling %s: %s", dep_id, exc)
            continue
        if rc != 0:
            logger.warning("Failed to uninstall %s: %s", dep_id, output[:200])
            continue

        record_uninstall(dep_id, app_name)
        cleaned.append(dep_id)
        logger.info("Cleaned dependency %s for app %s", dep_id, app_name)

    return cleaned
