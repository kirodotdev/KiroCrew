"""Linking an app's skills into the Kiro Crew skills directory.

``_register_skills`` / ``_deregister_skills`` and the startup reconcile that keeps an
enabled app's manifest-declared skills linked (``reconcile_app_skills``).

Part of the ``kiro_crew.apps.bridges`` family: callers and tests reach every name here
as ``kiro_crew.apps.bridges.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.bridges_sources import (
    _namespace,
    _registration_denied,
    _registration_source,
    _skills_dir,
)
from kiro_crew.apps.manager import get_app
from kiro_crew.apps.manifest import (
    AppManifest,
    Containment,
    contained_in_screened_root,
    screen_app_path,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.bridges")


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------

_RESERVED_SKILL_DIRS = {"auto"}


def _register_skills(
    app_name: str,
    manifest: AppManifest,
    app_root: Path,
    io_failures: list[str] | None = None,
) -> list[str]:
    """Symlink app skill directories into ~/.kiro/crew/skills/.

    Always creates a namespaced link (``skills/{app_name}/{skill_name}``), and
    usually a flat link (``skills/{skill_name}``) too, so the skill scanner finds
    the skill whether it walks subdirectories or only checks the top level. The
    flat link is skipped when the skill has the app's own name (the flat path IS
    the namespaced directory), when the name is in ``_RESERVED_SKILL_DIRS``, and
    when a real directory of that name already exists; an existing flat link is
    repointed, so it belongs to whichever app registered last. Only the
    namespaced path is safe to name in a ``skill://`` mapping.

    ``io_failures``, when supplied, collects the skills skipped because the path
    screen could not check them, which a later registration can still link.

    Returns list of registered skill names (namespaced).
    """
    registered: list[str] = []
    if not manifest.skills:
        # Nothing to link. Creating the namespaced dir anyway leaves an EMPTY
        # ``skills/<app_name>/`` behind, and when a PACKAGED builtin skill shares
        # the app's name that empty dir masks it: ``_ensure_builtin_skills`` copies
        # at gateway start, app registration runs after, and the mkdir then leaves a
        # directory whose ``SKILL.md`` is gone — so every SOP the app's cron prompts
        # point at silently does not exist on disk. Observed with ops-mission-control,
        # whose skill ships under ``builtin_skills/`` precisely because a builtin's
        # app dir is never copied into the data home.
        return registered

    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    app_skills_dir.mkdir(parents=True, exist_ok=True)

    screened_root = screen_app_path(app_root)
    for skill_path_str in manifest.skills:
        skill_path = app_root / skill_path_str
        contained = contained_in_screened_root(skill_path, screened_root)
        if contained is Containment.UNCHECKED:
            # Unverified is not refused: report it as retryable.
            logger.warning("App %s: skill path could not be checked: %s", app_name, skill_path)
            if io_failures is not None:
                io_failures.append(str(skill_path))
            continue
        if contained is Containment.REFUSED:
            logger.warning(
                "App %s: skill path is refused by the path screen: %s", app_name, skill_path
            )
            continue
        if contained is Containment.OUTSIDE:
            logger.warning("App %s: skill path escapes app root: %s", app_name, skill_path)
            continue
        if not skill_path.is_dir():
            logger.warning("App %s: skill directory not found: %s", app_name, skill_path)
            continue

        skill_name = skill_path.name

        # Namespaced link: ~/.kiro/crew/skills/{app_name}/{skill_name}
        link_path = app_skills_dir / skill_name
        if link_path.exists() or platform_compat.is_link_or_junction(link_path):
            if platform_compat.is_link_or_junction(link_path):
                # Symlink OR Windows junction — remove the link, not its target.
                platform_compat.unlink_link_or_junction(link_path)
            else:
                shutil.rmtree(link_path)

        # Flat link: ~/.kiro/crew/skills/{skill_name} (for skill scanner)
        if skill_name in _RESERVED_SKILL_DIRS:
            logger.info("App %s: skipping flat link for reserved name %s", app_name, skill_name)
            flat_link = None
        else:
            flat_link = skills_root / skill_name
            if flat_link.exists() or platform_compat.is_link_or_junction(flat_link):
                if platform_compat.is_link_or_junction(flat_link):
                    platform_compat.unlink_link_or_junction(flat_link)
                else:
                    logger.info(
                        "App %s: skipping flat link for %s — non-symlink dir exists",
                        app_name,
                        skill_name,
                    )
                    flat_link = None  # type: ignore[assignment]

        try:
            # symlink on POSIX; directory junction on non-admin Windows (a plain
            # os.symlink there raises WinError 1314 and would silently drop every
            # app skill for the ordinary user).
            platform_compat.symlink_or_junction(str(skill_path), str(link_path))
            if flat_link is not None:
                platform_compat.symlink_or_junction(str(skill_path), str(flat_link))
            namespaced = _namespace(app_name, skill_name)
            registered.append(namespaced)
            logger.info("Registered skill: %s -> %s", namespaced, skill_path)
        except OSError as exc:
            logger.warning("Failed to link skill %s: %s", skill_name, exc)

    if registered:
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="app_bridge",
            tool_name="register_skills",
            tool_kind="permission_change",
            outcome="completed",
            resources=f"app={app_name} skills={registered}",
        )
    else:
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="app_bridge",
            tool_name="register_skills",
            tool_kind="permission_change",
            outcome="no_op",
            resources=f"app={app_name} skills=[]",
        )
    return registered


def _deregister_skills(app_name: str) -> int:
    """Remove the app's skill symlinks from ~/.kiro/crew/skills/.

    Removes **only what registration created** — the symlinks, and the directory
    itself once it holds nothing else. It must NOT ``rmtree`` unconditionally: when a
    PACKAGED builtin skill shares the app's name, ``skills/<app_name>/`` is that
    skill's real directory, not an app-owned link farm. Blowing it away deletes a
    shipped skill and every SOP under it, leaving the app's cron prompts pointing at
    missing files — silently, since a missing skill file is not an error anywhere.
    Real case: ops-mission-control, whose skill ships under ``builtin_skills/``
    because a builtin app's own directory is never copied into the data home.
    """
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    if not app_skills_dir.exists():
        return 0
    try:
        removed_skills = [
            item.name
            for item in app_skills_dir.iterdir()
            if platform_compat.is_link_or_junction(item)
        ]
        for item in app_skills_dir.iterdir():
            # Symlink on POSIX, directory junction on non-admin Windows.
            if platform_compat.is_link_or_junction(item):
                if item.name in _RESERVED_SKILL_DIRS:
                    continue
                target = item.resolve()
                flat_link = skills_root / item.name
                # is_link_or_junction: a junction (non-admin Windows) is not a
                # symlink, and unlink_link_or_junction removes the link, never
                # the target it points at.
                if platform_compat.is_link_or_junction(flat_link) and flat_link.resolve() == target:
                    platform_compat.unlink_link_or_junction(flat_link)
                platform_compat.unlink_link_or_junction(item)
        # Only prune the directory if registration is all that was ever in it.
        # Any surviving real file means this path belongs to something else.
        if any(app_skills_dir.iterdir()):
            logger.info(
                "Deregistered %d app skill link(s) for %s; keeping %s "
                "(contains files this app does not own)",
                len(removed_skills),
                app_name,
                app_skills_dir,
            )
            return 1 if removed_skills else 0
        app_skills_dir.rmdir()
        logger.info("Deregistered skills for app %s", app_name)
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="app_bridge",
            tool_name="deregister_skills",
            tool_kind="permission_change",
            outcome="completed",
            resources=f"app={app_name} skills={removed_skills}",
        )
        return 1
    except OSError:
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="app_bridge",
            tool_name="deregister_skills",
            tool_kind="permission_change",
            outcome="failed",
            resources=f"app={app_name}",
        )
        return 0


# ---------------------------------------------------------------------------
# Skill reconcile (startup — ensures manifest-declared skills are linked)
# ---------------------------------------------------------------------------


def reconcile_app_skills(app_name: str) -> list[str]:
    """Reconcile skill symlinks for an enabled app at gateway startup.

    Ensures manifest-declared skills are registered (idempotent: existing
    correct symlinks are overwritten by _register_skills, missing ones are
    created).  Also removes stale symlinks for skills that were removed from
    the manifest since the last registration.

    Called from start_enabled_app_backends() so that an app upgraded in-place
    (new manifest declaring new skills) gets its symlinks without needing a
    disable/enable cycle.

    Returns list of currently-registered namespaced skill names.
    """
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        # Self-managed apps own their registration lifecycle -- never touch
        # their symlinks here, even when the manifest declares no skills
        # (dynamically managed skills are not manifest-declared).
        return []

    manifest, app_root = _registration_source(app_name)
    if _registration_denied(
        app_name,
        action="skill_reconcile",
        app_root=app_root,
    ):
        # A policy tightened after a prior registration must revoke stale links,
        # not merely decline to create new ones.
        _deregister_skills(app_name)
        return []

    if not manifest or not manifest.skills:
        # No skills declared — remove any stale symlinks left from a prior version
        _deregister_skills(app_name)
        return []

    # _register_skills is already idempotent (overwrites existing symlinks)
    registered = _register_skills(app_name, manifest, app_root)

    # Clean stale links: skills present as symlinks but absent from the manifest
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    if app_skills_dir.is_dir():
        manifest_skill_names = {Path(s).name for s in manifest.skills}
        for entry in list(app_skills_dir.iterdir()):
            # is_link_or_junction: a junction (non-admin Windows) is not a symlink.
            if platform_compat.is_link_or_junction(entry) and (
                entry.name not in manifest_skill_names
            ):
                # Stale link — skill was removed from manifest
                target = entry.resolve()
                platform_compat.unlink_link_or_junction(entry)
                # Also remove the flat link if it points to the same target
                flat_link = skills_root / entry.name
                if platform_compat.is_link_or_junction(flat_link):
                    try:
                        if flat_link.resolve() == target:
                            platform_compat.unlink_link_or_junction(flat_link)
                    except OSError:
                        pass
                logger.info(
                    "Reconcile: removed stale skill link %s/%s for app %s",
                    app_name,
                    entry.name,
                    app_name,
                )

    return registered
