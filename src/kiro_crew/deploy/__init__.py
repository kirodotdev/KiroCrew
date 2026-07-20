"""KiroCrew core deploy module — AWS deploy engine, profiles, and route handlers.

Folded from the former ``deploy_web`` builtin app (team decision 2026-07-15).
Core now owns the AWS deploy layer directly; the "Artifact Deploy" page lives
at ``/artifacts/deploy`` in the main dashboard.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


_MANAGED_MARKER = ".kirocrew-managed"


def _register_core_skills() -> None:
    """Idempotently install deploy skills into <home>/skills/.

    Always copies (never symlinks) so that _find_skills' realpath containment
    check sees the skill files as living inside the skill root. A symlink whose
    target is in site-packages resolves outside the root and gets pruned.

    Called at gateway startup. Uses config_dir() so pods/tests isolate correctly.

    Safety: only removes/replaces directories that contain a `.kirocrew-managed`
    marker file (written by us on creation). User-placed directories with the
    same name are left untouched with a warning.
    """
    target = config_dir() / "skills"
    target.mkdir(parents=True, exist_ok=True)

    for skill_dir in _SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        link = target / skill_dir.name

        # Migration: if an existing entry is a symlink (from older versions),
        # unlink it and replace with a fresh copy regardless of target match.
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            # Real directory exists at that name — only remove if we created it
            if not (link / _MANAGED_MARKER).exists():
                logger.warning(
                    "Skipping deploy skill %s: user-placed directory at %s "
                    "(remove it manually to allow KiroCrew to manage this skill)",
                    skill_dir.name,
                    link,
                )
                continue
            shutil.rmtree(link)

        # Always copy (not symlink) so realpath stays within skill root.
        try:
            shutil.copytree(skill_dir, link)
            # Write the managed marker so future refreshes know it's ours
            (link / _MANAGED_MARKER).write_text("")
            logger.debug("Copied deploy skill %s", skill_dir.name)
        except OSError:
            logger.error("Failed to install deploy skill %s", skill_dir.name)
            raise
