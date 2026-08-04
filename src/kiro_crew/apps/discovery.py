"""Builtin Auto-Discovery — scan builtins/ directory for app manifests.

Discovers builtins by scanning the filesystem: each subdirectory of
``builtins/`` that contains a valid ``app.json`` is registered as a builtin app.
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
    """Convert an AppManifest to the dict format expected by register_builtin_apps()."""
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

    perms_d = manifest.permissions.to_dict()
    if perms_d:
        d["permissions"] = perms_d

    ui_d = manifest.ui.to_dict()
    if ui_d:
        d["ui"] = ui_d

    # Backend (including hooks)
    backend_d = manifest.backend.to_dict()
    if backend_d:
        d["backend"] = backend_d

    # Agents and skills are typed fields rather than ``extra``, so they must be
    # copied explicitly — otherwise they are stripped from the persisted
    # ``app.json`` that ``bridges.register_app`` re-reads, so a builtin's
    # declared agents are never symlinked into ``~/.kiro/agents`` and its skills
    # never registered.
    if manifest.agents:
        d["agents"] = list(manifest.agents)
    if manifest.skills:
        d["skills"] = list(manifest.skills)

    if manifest.mcpServers:
        d["mcpServers"] = manifest.mcpServers

    if manifest.crons:
        d["crons"] = [c.to_dict() for c in manifest.crons]

    deps_d = manifest.dependencies.to_dict()
    if deps_d:
        d["dependencies"] = deps_d

    setup_d = manifest.setup.to_dict()
    if setup_d:
        d["setup"] = setup_d

    # Publish provider (Route B registry, §1.3)
    pp_d = manifest.publishProvider.to_dict()
    if pp_d:
        d["publishProvider"] = pp_d

    # The REMAINING declarative fields, same reasoning as the agents/skills block
    # above (#1076): this dict is what register_builtin_apps() persists as the
    # app.json snapshot, and register_app() reads that snapshot rather than the
    # packaged manifest — so any typed field not copied here is silently dropped
    # for every builtin. agents/skills was the instance that shipped; these are
    # the rest of the same class, and the round-trip guard covers all of them.
    if manifest.sops:
        d["sops"] = list(manifest.sops)
    if manifest.jobFamilies:
        d["jobFamilies"] = list(manifest.jobFamilies)
    notif_d = manifest.notifications.to_dict()
    if notif_d:
        d["notifications"] = notif_d
    platform_d = manifest.platform.to_dict() if manifest.platform else {}
    if platform_d:
        d["platform"] = platform_d
    if manifest.license:
        d["license"] = manifest.license
    if manifest.minKiroCrewVersion:
        d["minKiroCrewVersion"] = manifest.minKiroCrewVersion
    if manifest.signer:
        d["signer"] = manifest.signer
    if manifest.signature:
        d["signature"] = manifest.signature

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
                    entry.name,
                    "; ".join(errors),
                )
                continue
            apps.append(_manifest_to_builtin_dict(manifest))
            logger.debug("Discovered builtin app: %s v%s", manifest.name, manifest.version)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse builtin manifest %s: %s", manifest_path, exc)
        except Exception:
            logger.warning(
                "Unexpected error loading builtin manifest: %s",
                manifest_path,
                exc_info=True,
            )

    logger.info("Discovered %d builtin app(s) from %s", len(apps), builtins_dir)
    return apps
