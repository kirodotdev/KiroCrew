"""Find and validate every installed Agent Plugin.

``installed.json`` is the list of record, as it is for Kiro IDE and KAS: a folder
under ``installed/`` that the index does not name is not installed. Discovery
only reads. It writes no file, starts no process and adds no skill root, so
calling it can never activate a plugin.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from kiro_crew.agent_plugins.manifest import (
    MANIFEST_FILE,
    MCP_SCHEMA_FOR,
    ManifestError,
    ManifestReadError,
    load_manifest,
)
from kiro_crew.agent_plugins.mcp_config import load_mcp_config
from kiro_crew.agent_plugins.model import Discovery, Issue, PluginRecord
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
#: The ``reason`` of a ``not-a-plugin`` record whose folder holds a ``POWER.md``.
LEGACY_POWER_REASON = "legacy POWER.md power, not an Agent Plugin"
#: An index larger than this is not read; it lists names only.
MAX_INDEX_BYTES = 1024 * 1024


def installed_names(home: Path) -> list[str]:
    """Names ``installed.json`` lists, in order, without duplicates.

    A missing or unreadable index lists nothing, and so does one that is not a
    regular file: a named pipe or a device reports a size of 0 and would block
    or never end, so the size bound would not hold. An entry whose name is not a
    single path segment is dropped, because it would name a folder outside
    ``installed/``. :func:`discover` also reports why an index could not be read.
    """
    return _read_index(home)[0]


def _read_index(home: Path) -> tuple[list[str], str | None]:
    """:func:`installed_names`, plus why the index could not be read, or ``None``.

    Only a failed read is reported. A missing index means nothing is installed,
    and one that is not a regular file, too large, not UTF-8 or not valid JSON
    breaks a rule of its own; each of those lists nothing and reports nothing.
    """
    index = home / INSTALLED_INDEX
    try:
        info = index.stat()
        if not stat.S_ISREG(info.st_mode):
            logger.warning("agent plugins: %s is not a regular file", index)
            return [], None
        if info.st_size > MAX_INDEX_BYTES:
            logger.warning("agent plugins: %s is too large to read", index)
            return [], None
        data = loads_user_json(index.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        logger.warning("agent plugins: %s could not be read: %s", index, exc.__class__.__name__)
        return [], f"{INSTALLED_INDEX} could not be read: {exc.__class__.__name__}"
    except (ValueError, RecursionError) as exc:
        # ValueError covers UnicodeDecodeError. RecursionError: nesting deeper than
        # the parser's stack, well under the size cap.
        logger.warning("agent plugins: %s is not valid JSON: %s", index, exc.__class__.__name__)
        return [], None
    entries = data.get("installedPowers") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return [], None
    names: list[str] = []
    # A set beside the list keeps the de-duplication linear: anything that can
    # write the powers home can fill the 1 MiB index with tens of thousands of names.
    seen: set[str] = set()
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and is_plain_name(name) and name not in seen:
            seen.add(name)
            names.append(name)
    return names, None


def discover(home: Path | None = None) -> Discovery:
    """One record per installed name, sorted by name, and any failure to read the index.

    One plugin can never hide another: an unexpected error while inspecting a
    plugin makes that plugin ``invalid``, and the rest are still listed.
    """
    base = powers_home() if home is None else home
    names, index_error = _read_index(base)
    return Discovery(tuple(_inspect_guarded(base, name) for name in sorted(names)), index_error)


def discover_plugins(home: Path | None = None) -> list[PluginRecord]:
    """The records of :func:`discover`, without the index's own failure."""
    return list(discover(home).plugins)


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
            issues=(Issue("plugin", reason, failed=True),),
            failed=True,
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
        return _unreadable_folder(name, root_str, exc)
    if not is_dir:
        return PluginRecord(name, root_str, "invalid", reason="its folder is missing")
    root_real = os.path.realpath(root)

    try:
        has_manifest = _exists(os.path.join(root_real, MANIFEST_FILE))
        has_power = not has_manifest and _exists(os.path.join(root_real, LEGACY_POWER_FILE))
    except OSError as exc:
        return _unreadable_folder(name, root_str, exc)
    if not has_manifest:
        reason = LEGACY_POWER_REASON if has_power else "no plugin.json"
        return PluginRecord(name, root_str, "not-a-plugin", reason=reason)

    try:
        manifest, issues = load_manifest(root_real)
    except ManifestError as exc:
        failed = isinstance(exc, ManifestReadError)
        return PluginRecord(
            name,
            root_str,
            "invalid",
            reason=str(exc),
            issues=(Issue("plugin", str(exc), failed=failed),),
            failed=failed,
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


def _exists(path: str) -> bool:
    """``os.path.lexists``, except that a failure to look raises instead of reading as absent.

    ``lexists`` answers False when the folder holding *path* cannot be read, which
    would list an unreadable plugin as a folder with no ``plugin.json``.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _unreadable_folder(name: str, root: str, exc: OSError) -> PluginRecord:
    reason = f"its folder could not be read: {exc.__class__.__name__}"
    return PluginRecord(name, root, "invalid", reason=reason, failed=True)
