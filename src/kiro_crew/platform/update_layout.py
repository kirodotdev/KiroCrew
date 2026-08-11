"""Install-layout detection shared between ``kirocrew update`` and the dashboard.

Provides the same detection logic used by ``dashboard/handlers/updates.py`` in
a reusable form so the CLI update path can dispatch correctly without
duplicating layout heuristics.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from kiro_crew.beacon import distribution
from kiro_crew.config.paths import data_home

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

    ``data_home()`` rather than ``config_dir()``: this is reached from the async
    update check, and ``config_dir()`` is resolve-AND-MAINTAIN -- it refreshes the
    recovery breadcrumb and re-runs the leftover-archive sweep, which can
    ``shutil.rmtree``. Doing that on the event loop as a side effect of asking
    where a directory is, is issue #1057.
    """
    try:
        raw = (data_home() / "channel").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "stable"
    channel = raw.strip().lower()
    return channel if channel in RELEASE_CHANNELS else "stable"


def set_release_channel(channel: str) -> str:
    """Persist the release channel this install follows; return the stored value.

    The channel name becomes a PATH SEGMENT in every feed URL the update check
    builds (``feed/<channel>/latest-cli.json``) and a shell argument in the
    recommended installer command, so it is validated against
    :data:`RELEASE_CHANNELS` here and REJECTED rather than sanitized. Callers get
    ``ValueError``; nothing unvalidated ever reaches the file, and
    :func:`release_channel` re-validates on read as defence in depth.

    Written via a temp file + ``os.replace`` so a crash or a full disk cannot
    leave a half-written channel name behind — a truncated value would silently
    fall back to ``stable`` and move the install off its lane. The byte format is
    ``<channel>\\n``, matching what ``cli.sh`` writes, so the two writers stay
    interchangeable.

    ``data_home()`` for the same reason as :func:`release_channel`: the dashboard
    calls this from an async request handler.
    """
    normalized = str(channel or "").strip().lower()
    if normalized not in RELEASE_CHANNELS:
        raise ValueError(
            f"unknown release channel {channel!r} (expected one of {RELEASE_CHANNELS})"
        )
    target = data_home() / "channel"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(f"{normalized}\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        # A failed replace leaves the temp file behind; an orphan in the data
        # home would be read by nothing but is still litter.
        try:
            tmp.unlink()
        except OSError:
            pass
    return normalized


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
    "set_release_channel",
    "cdn_bases",
    "wheel_update_command",
    "RELEASE_CHANNELS",
    "EXTERNALLY_MANAGED",
]
