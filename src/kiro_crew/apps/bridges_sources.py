"""Where an app's resources register from, and the names and directories they land in.

The authoritative manifest and root an app's executable resources come from
(``_registration_source``) and the shared execution decision each bridge applies
(``_registration_denied``), the namespaced resource and link names, and the kiro-cli
agents directory and Kiro Crew skills directory the bridges write into. Every other
``bridges_*`` owner reads this module.

Part of the ``kiro_crew.apps.bridges`` family: callers and tests reach every name here
as ``kiro_crew.apps.bridges.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kiro_crew.apps.execution import app_execution_denied, shipped_builtin_app_root
from kiro_crew.apps.manager import app_dir, get_app_manifest
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.config.loader import config_dir
from kiro_crew.config.paths import kiro_agents_dir

logger = logging.getLogger("kiro_crew.apps.bridges")


# Where kiro-cli looks for agent definitions.
#
# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
KIRO_AGENTS_DIR: Path | None = None


def _kiro_agents_dir() -> Path:
    """The kiro-cli agents directory, resolved against the live data home."""
    return KIRO_AGENTS_DIR if KIRO_AGENTS_DIR is not None else kiro_agents_dir()


# Where Kiro Crew loads skills from
SKILLS_DIR_NAME = "skills"


def _app_resource_root(app_name: str) -> Path:
    """Where an app's declarative resources (agents, skills) actually live.

    A builtin's resources live in the PACKAGE, not the data home. Resolving them
    against the data home always misses — silently, because registration only
    logs a warning — which is how the first builtin to declare agents/skills got
    zero of them registered while its mcpServers (needing no path) registered
    fine.

    Delegates to :func:`shipped_builtin_app_root` rather than deriving the
    directory from the name. That primitive matches on the shipped manifest's own
    ``name`` field after ``resolve(strict=True)`` + containment, so it also
    handles the hyphen/underscore convention (`auto-research` ships as
    `builtins/auto_research`) without a normalising step here, and the path comes
    from a directory listing rather than from the input. Same resolution the
    lifecycle hook loader uses, so the two cannot disagree about where an app is.
    """
    return shipped_builtin_app_root(app_name) or app_dir(app_name)


def _skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


def app_skills_dir(app_name: str) -> Path:
    """Where this app's skills are registered, as an absolute path.

    Public because an app may need to TELL ITS OWN AGENT where its skills
    landed. Session context injects the skill catalogue only for the built-in
    agent (``ContextBuilder`` skips it for custom agents, which are expected to
    resolve their own), and an agent JSON ships inside the app, so it can only
    name paths that exist at packaging time — neither route can express a
    data-home path. An app that renders its agent prompt at runtime can, so
    hand it the directory rather than making it re-derive the layout.

    The namespaced directory is returned, not the flat one: the flat link is
    skipped for reserved names and can be shadowed by another app, while the
    namespaced path is always this app's own.
    """
    return _skills_dir() / app_name


def _namespace(app_name: str, resource_name: str) -> str:
    """Build a namespaced resource name: ``app_name/resource_name``."""
    return f"{app_name}/{resource_name}"


def _safe_link_name(namespaced: str) -> str:
    """Convert ``app/resource`` to a safe filename for symlinks: ``app--resource``.

    Neutralizes BOTH path separators, not just ``/``: on Windows a backslash is
    a separator too, so a resource name carrying ``\\`` would otherwise let the
    constructed link path traverse out of the agents directory.
    """
    return namespaced.replace("/", "--").replace("\\", "--")


def _registration_source(app_name: str) -> tuple[AppManifest | None, Path]:
    """Return the authoritative manifest and root for executable resources.

    A shipped builtin is always read from its immutable package root. This
    prevents mutable installed metadata from borrowing a builtin name and then
    registering attacker-controlled agents, skills, crons, or MCP servers.
    Third-party apps continue to use their installed snapshot.
    """
    shipped_root = shipped_builtin_app_root(app_name)
    if shipped_root is not None:
        try:
            return (
                AppManifest.from_json_file(shipped_root / "app.json"),
                shipped_root,
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "App %s: shipped resource manifest is unreadable: %s",
                app_name,
                exc,
            )
            return None, shipped_root
    return get_app_manifest(app_name), app_dir(app_name)


def _registration_denied(
    app_name: str,
    *,
    action: str,
    app_root: Path,
) -> str | None:
    """Apply the shared execution decision to an executable-resource bridge."""
    denied = app_execution_denied(
        app_name,
        action=action,
        app_root=app_root,
        caller="app_bridge",
    )
    if denied:
        logger.warning(
            "App %s: skipping executable resource registration (%s): %s",
            app_name,
            action,
            denied,
        )
    return denied
