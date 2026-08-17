"""Install-layout detection shared between ``kirocrew update`` and the dashboard.

Provides the same detection logic used by ``dashboard/handlers/updates.py`` in
a reusable form so the CLI update path can dispatch correctly without
duplicating layout heuristics.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

from kiro_crew.beacon import distribution
from kiro_crew.config.paths import data_home

#: Release channels the installer publishes.
RELEASE_CHANNELS = ("stable", "insider", "nightly")

#: Distributions managed by an external updater (desktop app, container).
EXTERNALLY_MANAGED = {
    "dmg": "Update via the desktop app's built-in updater (About → Check for updates).",
    "appimage": "Update via the desktop app's built-in updater (About → Check for updates).",
    "deb": "Update via the desktop app's built-in updater (About → Check for updates), "
           "which hands the new package to dpkg, or reinstall the .deb.",
    "rpm": "Update via the desktop app's built-in updater (About → Check for updates), "
           "which hands the new package to rpm, or reinstall the .rpm.",
    "docker": "Update by pulling a newer image (docker pull).",
}


class InstallLayout(NamedTuple):
    """Describes how this Kiro Crew instance was installed."""

    kind: str  # "git", "wheel", "dmg", "appimage", "deb", "rpm", "docker", or "source"
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


#: Characters a CDN base may contain. ``KIROCREW_CDN_BASE`` is operator-set and
#: the resulting base is interpolated into an installer command that is handed to
#: a shell, so anything outside this set (a quote, ``;``, ``$(``, whitespace)
#: could close the URL and append a second command. Also pins the scheme: an
#: ``http://`` override would make the piped installer interceptable on-path.
_SAFE_CDN_BASE_RE = re.compile(r"^https://[A-Za-z0-9._/:%@~+\-]+$")


def cdn_bases_are_safe() -> bool:
    """Are both CDN bases free of shell metacharacters and HTTPS-pinned?

    Every caller that builds a shell command from :func:`cdn_bases` must gate on
    this. It lives here, beside ``cdn_bases``, so the CLI path and the gateway's
    unattended path cannot drift apart on what they consider safe.
    """
    feed_base, artifact_base = cdn_bases()
    return bool(
        _SAFE_CDN_BASE_RE.match(feed_base) and _SAFE_CDN_BASE_RE.match(artifact_base)
    )


def wheel_update_command(channel: str | None = None) -> str:
    """The shell command that upgrades a wheel/cli.sh install.

    Composed locally from validated inputs — never from feed data.

    The installer is held in a shell VARIABLE and never lands on disk, which has
    to satisfy two constraints that pull against each other.

    1. A download failure must fail the command. Plain ``curl … | sh`` reports
       the exit status of ``sh``, and a shell handed empty input exits 0, so a
       CDN failure would look like a successful update: the version would not
       change, the gateway would restart, the check would still see an update
       available, and the unattended path would loop. Assigning the body in a
       command substitution first makes the fetch's own failure abort the
       command, portably — ``pipefail`` is not POSIX and the resolved ``sh`` is
       not guaranteed to be bash.

    2. No writable file may sit between download and execute. Staging to
       ``mktemp`` opened a TOCTOU window: the gateway and an agent share a uid,
       so the file's 0600 mode does not keep the agent out, and it could swap
       the contents after ``curl`` wrote them and before ``sh`` opened them —
       arbitrary code in the gateway's own context. Keeping the body in memory
       removes the window rather than trying to police it.

    ``-s --`` is required here and only here: it tells ``sh`` to read the script
    from stdin and to pass what follows to that script. The file form must NOT
    carry it, since ``cli.sh`` parses argv strictly and answers
    "unknown argument '-s'" with exit 2.
    """
    if channel is None:
        channel = release_channel()
    _, artifact_base = cdn_bases()
    return (
        "set -e; "
        f"_kc_body=\"$(curl -fsSL --proto '=https' {artifact_base}/cli.sh)\"; "
        # An empty body would let sh exit 0 on nothing at all, which is the same
        # false success as the piped form.
        'test -n "$_kc_body"; '
        f'printf \'%s\\n\' "$_kc_body" | sh -s -- --channel {channel}'
    )


__all__ = [
    "InstallLayout",
    "detect_install_layout",
    "release_channel",
    "set_release_channel",
    "cdn_bases",
    "cdn_bases_are_safe",
    "wheel_update_command",
    "RELEASE_CHANNELS",
    "EXTERNALLY_MANAGED",
]
