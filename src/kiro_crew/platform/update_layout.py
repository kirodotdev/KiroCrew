"""Install-layout detection shared between ``kirocrew update`` and the dashboard.

Provides the same detection logic used by ``dashboard/handlers/updates.py`` in
a reusable form so the CLI update path can dispatch correctly without
duplicating layout heuristics.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from kiro_crew.beacon import distribution
from kiro_crew.config.loader import config_dir

#: Release channels the installer publishes.
RELEASE_CHANNELS = ("stable", "insider", "nightly")

#: Distributions managed by an external updater (desktop app, container).
EXTERNALLY_MANAGED = {
    "dmg": "Update via the desktop app's built-in updater (About → Check for updates).",
    "appimage": "Update via the desktop app's built-in updater (About → Check for updates).",
    "docker": "Update by pulling a newer image (docker pull).",
}


class InstallLayout(NamedTuple):
    """Describes how this Kiro Crew instance was installed."""

    kind: str  # "git", "wheel", "dmg", "appimage", "docker", or "source"
    proj: str  # KIROCREW_PROJECT_DIR value (may be empty for non-git)
    is_git: bool
    is_externally_managed: bool
    guidance: str  # Human message for externally managed installs


def detect_install_layout() -> InstallLayout:
    """Detect the current install layout using the same logic as the dashboard.

    Returns an InstallLayout describing how to update this instance.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    is_git = bool(proj) and os.path.exists(os.path.join(proj, ".git"))

    if is_git:
        return InstallLayout(
            kind="git",
            proj=proj,
            is_git=True,
            is_externally_managed=False,
            guidance="",
        )

    dist = distribution()
    if dist in EXTERNALLY_MANAGED:
        return InstallLayout(
            kind=dist,
            proj=proj,
            is_git=False,
            is_externally_managed=True,
            guidance=EXTERNALLY_MANAGED[dist],
        )

    # Everything else: cli.sh wheel install, cloud source, etc.
    return InstallLayout(
        kind=dist or "wheel",
        proj=proj,
        is_git=False,
        is_externally_managed=False,
        guidance="",
    )


def release_channel() -> str:
    """The release channel this install follows, from ``$KIROCREW_HOME/channel``.

    Mirrors ``dashboard/handlers/updates.py::_release_channel``.
    """
    try:
        raw = (config_dir() / "channel").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "stable"
    channel = raw.strip().lower()
    return channel if channel in RELEASE_CHANNELS else "stable"


def cdn_bases() -> tuple[str, str]:
    """``(feed base, artifact base)`` — mirrors ``cli.sh``'s two URL classes.

    Respects ``KIROCREW_CDN_BASE`` override for alternate CDNs / testing.
    """
    override = (os.environ.get("KIROCREW_CDN_BASE") or "").strip().rstrip("/")
    if override:
        return override, override
    return "https://updates.crew.kiro.dev", "https://download.crew.kiro.dev"


def wheel_update_command(channel: str | None = None) -> str:
    """The shell command that upgrades a wheel/cli.sh install.

    Composed locally from validated inputs — never from feed data.
    """
    if channel is None:
        channel = release_channel()
    _, artifact_base = cdn_bases()
    return f"curl -fsSL --proto '=https' {artifact_base}/cli.sh " f"| sh -s -- --channel {channel}"


__all__ = [
    "InstallLayout",
    "detect_install_layout",
    "release_channel",
    "cdn_bases",
    "wheel_update_command",
    "RELEASE_CHANNELS",
    "EXTERNALLY_MANAGED",
]
