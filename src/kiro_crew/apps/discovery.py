"""Builtin Auto-Discovery — scan builtins/ directory for app manifests.

Replaces the hardcoded ``_BUILTIN_APPS`` list in ``manager.py`` with
filesystem-based discovery. Each subdirectory of ``builtins/`` that contains
a valid ``app.json`` is registered as a builtin app.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.apps.manifest import AppManifest

logger = logging.getLogger(__name__)


def _get_builtins_dir() -> Path:
    """Return the path to the builtins/ directory within the KiroCrew package."""
    return Path(__file__).parent / "builtins"


def _manifest_to_builtin_dict(manifest: AppManifest) -> dict[str, Any]:
    """Convert an AppManifest to the dict format expected by register_builtin_apps().

    This produces the same structure as the old hardcoded _BUILTIN_APPS entries.
    """
    d: dict[str, Any] = {
        "name": manifest.name,
        "version": manifest.version,
        "displayName": manifest.displayName,
        "description": manifest.description,
    }
    if manifest.author:
        d["author"] = manifest.author
    if manifest.tags:
        d["tags"] = manifest.tags

    # Preserve extra fields (defaultEnabled, highlights, etc.)
    for key, value in manifest.extra.items():
        d[key] = value

    # Permissions
    perms_d = manifest.permissions.to_dict()
    if perms_d:
        d["permissions"] = perms_d

    # UI
    ui_d = manifest.ui.to_dict()
    if ui_d:
        d["ui"] = ui_d

    # Backend (including hooks)
    backend_d = manifest.backend.to_dict()
    if backend_d:
        d["backend"] = backend_d

    # Agents and skills. These are typed fields rather than ``extra``, so they
    # have to be copied explicitly — omitting them silently stripped both from
    # the persisted ``app.json``, and ``bridges.register_app`` re-reads that
    # stripped file, so a builtin's declared agents were never symlinked into
    # ``~/.kiro/agents`` and its skills were never registered.
    if manifest.agents:
        d["agents"] = list(manifest.agents)
    if manifest.skills:
        d["skills"] = list(manifest.skills)

    # MCP servers
    if manifest.mcpServers:
        d["mcpServers"] = manifest.mcpServers

    # Crons
    if manifest.crons:
        d["crons"] = [c.to_dict() for c in manifest.crons]

    # Dependencies
    deps_d = manifest.dependencies.to_dict()
    if deps_d:
        d["dependencies"] = deps_d

    # Setup
    setup_d = manifest.setup.to_dict()
    if setup_d:
        d["setup"] = setup_d

    # Publish provider (Route B registry, §1.3)
    pp_d = manifest.publishProvider.to_dict()
    if pp_d:
        d["publishProvider"] = pp_d

    return d


def discover_builtin_apps(builtins_dir: Path | None = None) -> list[dict[str, Any]]:
    """Scan builtins/ directory for app.json manifests.

    Returns list of app metadata dicts compatible with the existing
    register_builtin_apps() function signature.

    Args:
        builtins_dir: Override path to builtins directory (for testing).
                      Defaults to the package's builtins/ directory.

    Returns:
        List of app metadata dicts, sorted by app name.
    """
    if builtins_dir is None:
        builtins_dir = _get_builtins_dir()

    apps: list[dict[str, Any]] = []
    if not builtins_dir.is_dir():
        logger.debug("Builtins directory not found: %s", builtins_dir)
        return apps

    for entry in sorted(builtins_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Skip __pycache__ and hidden directories
        if entry.name.startswith((".", "_")):
            continue

        manifest_path = entry / "app.json"
        if not manifest_path.is_file():
            continue

        try:
            manifest = AppManifest.from_json_file(manifest_path)
            errors = manifest.validate(app_root=entry)
            if errors:
                logger.warning(
                    "Skipping builtin %s: validation errors: %s",
                    entry.name, "; ".join(errors),
                )
                continue
            apps.append(_manifest_to_builtin_dict(manifest))
            logger.debug("Discovered builtin app: %s v%s", manifest.name, manifest.version)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Failed to parse builtin manifest %s: %s", manifest_path, exc
            )
        except Exception:
            logger.warning(
                "Unexpected error loading builtin manifest: %s",
                manifest_path, exc_info=True,
            )

    logger.info("Discovered %d builtin app(s) from %s", len(apps), builtins_dir)
    return apps
