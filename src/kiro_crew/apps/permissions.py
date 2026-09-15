"""App permissions — validate and enforce declared permissions.

Apps declare permissions in their manifest. KiroCrew validates them at
install time and enforces them at runtime.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.apps.manifest import AppManifest

logger = logging.getLogger(__name__)


@dataclass
class PermissionCheck:
    """Result of a permission validation check."""

    allowed: bool = True
    warnings: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"allowed": self.allowed}
        if self.warnings:
            d["warnings"] = self.warnings
        if self.denied:
            d["denied"] = self.denied
        return d


def validate_permissions(manifest: AppManifest) -> PermissionCheck:
    """Validate an app's declared permissions.

    Returns a PermissionCheck with warnings for broad permissions
    and denied items for unsafe requests.
    """
    result = PermissionCheck()
    perms = manifest.permissions

    # Check for broad MCP tool access
    if len(perms.mcpTools) > 10:
        result.warnings.append(
            f"App requests access to {len(perms.mcpTools)} MCP tools — review carefully"
        )

    # Network access warning
    if perms.network:
        result.warnings.append(
            "App requests network access — it can make outbound HTTP calls"
        )

    # Shared memory warning
    if perms.memory == "shared":
        result.warnings.append(
            "App requests shared memory access — it can read/write KiroCrew's memory stores"
        )

    # Path traversal in any resource paths
    for path_list_name in ("agents", "skills", "sops"):
        for p in getattr(manifest, path_list_name, []):
            if ".." in str(p):
                result.denied.append(f"path traversal in {path_list_name}: {p}")
                result.allowed = False

    return result


def check_tool_permission(app_name: str, tool_name: str, manifest: AppManifest) -> bool:
    """Check if an app is allowed to use a specific MCP tool.

    Returns True if the tool is in the app's declared mcpTools list,
    or if the app declares no tool restrictions (empty list = unrestricted).
    """
    if not manifest.permissions.mcpTools:
        return True  # no restrictions declared
    return tool_name in manifest.permissions.mcpTools


def format_permissions_summary(manifest: AppManifest) -> str:
    """Format a human-readable permissions summary for display during install."""
    perms = manifest.permissions
    lines: list[str] = []

    if perms.mcpTools:
        lines.append(f"  MCP Tools: {', '.join(perms.mcpTools[:5])}")
        if len(perms.mcpTools) > 5:
            lines.append(f"             ... and {len(perms.mcpTools) - 5} more")
    if perms.storage:
        lines.append("  Storage:   app-scoped data directory")
    if perms.network:
        lines.append("  Network:   outbound HTTP access")
    if perms.memory:
        lines.append(f"  Memory:    {perms.memory}")
    if perms.cron:
        lines.append("  Cron:      scheduled agent jobs")

    if not lines:
        return "  No special permissions required"
    return "\n".join(lines)
