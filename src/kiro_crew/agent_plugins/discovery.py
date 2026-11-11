"""Find and validate every installed Agent Plugin.

``installed.json`` is the list of record, as it is for Kiro IDE and KAS: a folder
under ``installed/`` that the index does not name is not installed. Discovery
only reads. It writes no file, starts no process and adds no skill root, so
calling it can never activate a plugin.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from kiro_crew.agent_plugins.manifest import (
    MANIFEST_FILE,
    MCP_SCHEMA_FOR,
    ManifestError,
    load_manifest,
)
from kiro_crew.agent_plugins.mcp_config import load_mcp_config
from kiro_crew.agent_plugins.model import Issue, PluginRecord
from kiro_crew.agent_plugins.paths import (
    INSTALLED_DIR,
    INSTALLED_INDEX,
    is_plain_name,
    plugin_data_dir,
    powers_home,
)
from kiro_crew.agent_plugins.skills import discover_skills
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger(__name__)

LEGACY_POWER_FILE = "POWER.md"
#: An index larger than this is not read; it lists names only.
MAX_INDEX_BYTES = 1024 * 1024


def installed_names(home: Path) -> list[str]:
    """Names ``installed.json`` lists, in order, without duplicates.

    A missing or unreadable index lists nothing. An entry whose name is not a
    single path segment is dropped, because it would name a folder outside
    ``installed/``.
    """
    index = home / INSTALLED_INDEX
    try:
        if index.stat().st_size > MAX_INDEX_BYTES:
            logger.warning("agent plugins: %s is too large to read", index)
            return []
        data = loads_user_json(index.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        # RecursionError: nesting deeper than the parser's stack, well under the size cap.
        logger.warning("agent plugins: %s could not be read: %s", index, exc.__class__.__name__)
        return []
    entries = data.get("installedPowers") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and is_plain_name(name) and name not in names:
            names.append(name)
    return names


def discover_plugins(home: Path | None = None) -> list[PluginRecord]:
    """One record per installed name, sorted by name.

    One plugin can never hide another: an unexpected error while inspecting a
    plugin makes that plugin ``invalid``, and the rest are still listed.
    """
    base = powers_home() if home is None else home
    return [_inspect_guarded(base, name) for name in sorted(installed_names(base))]


def _inspect_guarded(home: Path, name: str) -> PluginRecord:
    try:
        return inspect_plugin(home, name)
    except Exception as exc:
        # Only the class is reported: the message and traceback may carry file
        # contents or paths from an untrusted package.
        reason = f"it could not be inspected: {exc.__class__.__name__}"
        logger.warning("agent plugins: %s %s", name, reason)
        return PluginRecord(
            name,
            str(home / INSTALLED_DIR / name),
            "invalid",
            reason=reason,
            issues=(Issue("plugin", reason),),
        )


def inspect_plugin(home: Path, name: str) -> PluginRecord:
    """Validate the plugin installed as *name* under *home*."""
    root = home / INSTALLED_DIR / name
    root_str = str(root)
    try:
        is_dir = root.is_dir()
    except OSError as exc:
        # Path.is_dir reports a missing folder as False but raises on, for
        # example, a permission error on a parent folder.
        reason = f"its folder could not be read: {exc.__class__.__name__}"
        return PluginRecord(name, root_str, "invalid", reason=reason)
    if not is_dir:
        return PluginRecord(name, root_str, "invalid", reason="its folder is missing")
    root_real = os.path.realpath(root)

    if not os.path.lexists(os.path.join(root_real, MANIFEST_FILE)):
        if os.path.lexists(os.path.join(root_real, LEGACY_POWER_FILE)):
            reason = "legacy POWER.md power, not an Agent Plugin"
        else:
            reason = "no plugin.json"
        return PluginRecord(name, root_str, "not-a-plugin", reason=reason)

    try:
        manifest, issues = load_manifest(root_real)
    except ManifestError as exc:
        return PluginRecord(
            name,
            root_str,
            "invalid",
            reason=str(exc),
            issues=(Issue("plugin", str(exc)),),
        )

    skills, skill_issues = discover_skills(root_real)
    data_dir = str(plugin_data_dir(home, name))
    servers, mcp_issues = load_mcp_config(root_real, data_dir, MCP_SCHEMA_FOR[manifest.schema])
    return PluginRecord(
        name,
        root_str,
        "off",
        manifest=manifest,
        skills=skills,
        servers=servers,
        issues=tuple(issues + skill_issues + mcp_issues),
    )
