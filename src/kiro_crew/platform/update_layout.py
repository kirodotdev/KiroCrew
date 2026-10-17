"""Install-layout detection shared between ``kirocrew update`` and the dashboard.

Provides the same detection logic used by ``dashboard/handlers/updates.py`` in
a reusable form so the CLI update path can dispatch correctly without
duplicating layout heuristics.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from kiro_crew.atomic_write import atomic_write
from kiro_crew.beacon import distribution
from kiro_crew.config.paths import data_home
from kiro_crew.platform.update_capability import (
    EXTERNALLY_MANAGED_MESSAGES,
    MANAGED_BY_GIT,
    UNAVAILABLE_MANAGED_BY_APP,
    UNAVAILABLE_MANAGED_BY_IMAGE,
    derive_capability,
)

#: Release channels the installer publishes.
RELEASE_CHANNELS = ("stable", "insider", "nightly")

#: Distributions managed by an external updater (desktop app, container), mapped
#: to the copy shown for them. Built from the capability module's table so the
#: CLI and the dashboard cannot show different words for the same state.
#:
#: The packaged shapes whose updater hands the bytes to a second installer name
#: it: dpkg, rpm, or the NSIS installer. That is the one detail a .deb, .rpm or
#: Windows user needs and the shared sentence cannot carry. The sentence itself
#: still has one source.
_APP_MANAGED = EXTERNALLY_MANAGED_MESSAGES[UNAVAILABLE_MANAGED_BY_APP]


def _app_managed_via(handler: str, package: str) -> str:
    """Extend the app-updater sentence with the package manager it hands off to."""
    return (
        f"{_APP_MANAGED.rstrip('.')}, which hands the new package to {handler}, "
        f"or reinstall the {package}."
    )


EXTERNALLY_MANAGED = {
    "dmg": _APP_MANAGED,
    "appimage": _APP_MANAGED,
    "deb": _app_managed_via("dpkg", ".deb"),
    "rpm": _app_managed_via("rpm", ".rpm"),
    "nsis": _app_managed_via("the NSIS installer", "Setup .exe"),
    "docker": EXTERNALLY_MANAGED_MESSAGES[UNAVAILABLE_MANAGED_BY_IMAGE],
}

#: The upgrade command a user runs for a plain ``pip`` install into an
#: environment they manage themselves (not pipx, not the installer's managed
#: venv). The built-in updater cannot drive this safely: re-running the
#: installer would build a SECOND copy (a pipx venv or a managed venv plus a
#: symlink) while the environment actually serving the user keeps the old
#: version. So the CLI refuses and the gateway's floor warning points here
#: instead — one source of words for both, since they describe the same state.
#:
#: Kiro Crew is not on PyPI, so a bare ``pip install -U kirocrew`` resolves
#: against default PyPI and dead-ends — "no matching distribution", or worse a
#: third-party squat of the name. The command instead installs the channel's
#: signed, hash-pinned wheel by direct URL, so pip consults no index for
#: ``kirocrew`` at all (see :func:`non_managed_pip_upgrade_command`). When that
#: signed wheel cannot be resolved, the surface reports the failure and points
#: at the channel's artifact directory rather than emit any ``kirocrew``
#: name-resolving command — a name-based ``--extra-index-url`` form adds the
#: channel index BESIDE default PyPI, and pip would prefer a higher-versioned
#: public squat of the name, which is the exact dependency-confusion vector the
#: pinned direct URL exists to close.
_NON_MANAGED_PIP_RESTART = "kirocrew restart"


class PipUpgradeHint(NamedTuple):
    """The in-place upgrade step for a plain-``pip`` install.

    Exactly one of two states, because the signed wheel either resolves or it
    does not, and the two must never be confused at an emit site:

    * ``command`` is the runnable ``pip install "<signed wheel>#sha256=<sha>"``
      and ``note`` is empty — the operator runs it.
    * ``command`` is ``None`` and ``note`` is a short failure report with retry
      and manual-install guidance — the signed wheel could not be fetched or
      verified (offline, a CDN failure, a missing ``openssl``), so there is NO
      safe command to hand over. The surface shows ``note`` instead.

    The failure state deliberately carries no fallback command. The only
    index-based form that could stand in resolves ``kirocrew`` across the
    channel index AND default PyPI, where a higher-versioned public squat of the
    name wins — the dependency-confusion vector the signed-wheel path closes.
    Handing that out on every verification failure (which includes a plain-pip
    Windows host with no trusted ``openssl``, and any transient CDN outage)
    would reopen it, so the surface reports the failure rather than emit it.
    """

    command: str | None
    note: str


def _running_interpreter_pip_prefix() -> str:
    """``<this install's python> -m pip`` — the interpreter spelled out.

    A bare ``pip`` resolves through the invoking shell's PATH, which may be a
    DIFFERENT environment than the out-of-date one this install runs from (a
    non-activated venv reached by absolute path, or the gateway logging the hint
    for an environment that is not the shell's active one). The upgrade would
    then land in the wrong environment. Keying the command to ``sys.executable``
    — the interpreter of the install actually being upgraded — makes it
    unambiguous, the same reason and quoting idiom
    :func:`kiro_crew.extras.pip_install_command_for` uses.
    """
    if os.name == "nt":
        exe = sys.executable.replace("'", "''")
        return f"& '{exe}' -m pip"
    return f"{shlex.quote(sys.executable)} -m pip"


def _pinned_wheel_upgrade_command(channel: str, pip: str) -> str | None:
    """``pip install "<signed wheel url>#sha256=<sha>"`` for the channel, or None.

    Resolves the exact wheel the channel's SIGNED manifest names and pins it by
    hash, so the emitted command installs that one artifact by direct URL and
    pip consults NO index for ``kirocrew`` at all. This closes the
    dependency-confusion vector that an ``--extra-index-url`` form leaves open:
    with the channel index merely ADDED beside default PyPI, pip pools
    ``kirocrew`` candidates across both and a higher-versioned public-PyPI squat
    of the name wins; a pinned direct-URL install removes the name resolution
    entirely and verifies the bytes against the signed sha256. Dependencies
    still resolve from PyPI, which a direct-URL requirement does not constrain.

    ``wheel_url`` and ``sha256`` come from
    :func:`kiro_crew.platform.wheel_engine.fetch_verified_manifest`, which
    fetches the per-channel ``latest-cli.json`` and verifies its RSA signature
    against the pinned public key before returning the payload — the same signed
    manifest the wheel-install update path already trusts. The URL is the
    canonical ``<artifact base>/cli/<channel>/<version>/kirocrew-<version>-...``
    the manifest validator rebuilds and refuses if it does not match, so no feed
    value flows into the command unverified.

    Returns ``None`` when the manifest cannot be fetched or verified (offline, a
    CDN failure, a missing ``openssl``) so the caller can report the failure
    with manual-install guidance rather than emit a name-resolving command. The
    fetch does network and ``openssl`` work, so this runs only where blocking is
    allowed — the synchronous CLI path and the gateway's off-event-loop
    ``to_thread`` dispatch.
    """
    # Imported lazily: wheel_engine pulls in the signing/verification stack, and
    # the failure-report path below must not depend on it being importable.
    from kiro_crew.platform.wheel_engine import WheelUpdateError, fetch_verified_manifest

    feed_base, artifact_base = cdn_bases()
    try:
        # fetch_verified_manifest verifies the signature with NO attacker-plantable
        # file: on POSIX the key/signature go to openssl over anonymous pipe FDs
        # and the payload over stdin; only the Windows fallback stages files, and
        # it opens each O_CREAT|O_EXCL|O_NOFOLLOW so a planted symlink is refused.
        # The workdir below is therefore only that fallback's scratch area — a
        # fresh, gateway-private, randomly-named temp dir (0o700), NOT the
        # agent-writable SEL ``trust`` keystone, so no agent can even see it.
        with tempfile.TemporaryDirectory(prefix="kc-upgrade-hint-") as tmp:
            payload = fetch_verified_manifest(
                channel=channel,
                feed_base=feed_base,
                artifact_base=artifact_base,
                workdir=Path(tmp),
            )
    except (WheelUpdateError, OSError):
        return None
    wheel_url = payload["wheel_url"]
    sha256 = payload["sha256"]
    # The manifest validator pins wheel_url to the canonical artifact URL and
    # sha256 to 64 lowercase hex, and cdn_bases_are_safe gates the base against
    # shell metacharacters, so the fragment-pinned URL carries none. Quote it
    # anyway: a URL belongs in quotes on the command line, and the fragment is a
    # shell comment char unquoted.
    return f'{pip} install "{wheel_url}#sha256={sha256}"'


def non_managed_pip_upgrade_command(channel: str | None = None) -> PipUpgradeHint:
    """The in-place upgrade step for a plain-``pip`` install, as a hint.

    Returns a :class:`PipUpgradeHint`. On success its ``command`` is the
    channel's SIGNED, hash-pinned wheel installed by direct URL
    (:func:`_pinned_wheel_upgrade_command`): ``pip install
    "<wheel url>#sha256=<sha>"`` installs that one artifact, so pip consults no
    index for ``kirocrew`` and dependency confusion has no opening. The command
    is keyed to the RUNNING interpreter (``<sys.executable> -m pip``), not a
    bare ``pip`` — a bare ``pip`` trusts the invoking shell's PATH, which may
    name a different environment than the out-of-date one this install runs
    from, so the upgrade would land in the wrong place.

    When the signed wheel cannot be fetched or verified (offline, a CDN failure,
    a missing ``openssl``), the hint's ``command`` is ``None`` and ``note``
    reports the failure with retry and manual-install guidance pointing at the
    channel's artifact directory. It deliberately emits NO command in this
    state. The only index-based stand-in — ``--extra-index-url <channel index>``
    beside default PyPI — pools ``kirocrew`` candidates across both, where a
    higher-versioned public squat of the name wins; that is the exact
    dependency-confusion vector the pinned direct URL closes, and a verification
    failure (common on a plain-pip Windows host with no trusted ``openssl``, and
    on any transient CDN outage) is not rare enough to reopen it on.

    The feed base comes from :func:`cdn_bases`, which honours the
    ``KIROCREW_CDN_BASE`` override, so a test or alternate CDN upgrades from the
    same place it installed from. The channel is validated by
    :func:`release_channel` and the base by :func:`cdn_bases_are_safe`, so the
    rendered strings carry no shell metacharacters.
    """
    if channel is None:
        channel = release_channel()
    pip = _running_interpreter_pip_prefix()
    pinned = _pinned_wheel_upgrade_command(channel, pip)
    if pinned is not None:
        return PipUpgradeHint(command=pinned, note="")
    _feed_base, artifact_base = cdn_bases()
    artifact_dir = f"{artifact_base}/cli/{channel}/"
    note = (
        "could not fetch or verify the signed release manifest "
        "(offline, a CDN failure, or no trusted openssl). The install was not "
        "changed. Retry when connectivity returns, or install the channel's "
        f"signed wheel manually from {artifact_dir} "
        "(verify its sha256 against the published SHA256SUMS)."
    )
    return PipUpgradeHint(command=None, note=note)


def non_managed_pip_update_hint(channel: str | None = None) -> tuple[PipUpgradeHint, str]:
    """The upgrade hint and the restart command, in order.

    ``(upgrade, restart)``: the :class:`PipUpgradeHint` for upgrading in the
    SAME environment Kiro Crew runs from, then the restart command so the
    running process picks the new version up. Returned as a pair rather than a
    joined string so each surface (CLI banner, gateway log line) can frame them
    in its own layout — and so each can branch on whether ``upgrade.command`` is
    a runnable command or ``upgrade.note`` is a failure report.
    """
    return non_managed_pip_upgrade_command(channel), _NON_MANAGED_PIP_RESTART


class InstallLayout(NamedTuple):
    """Describes how this Kiro Crew instance was installed."""

    kind: str  # "git", "wheel", "dmg", "appimage", "deb", "rpm", "nsis", "docker", or "source"
    proj: str  # KIROCREW_PROJECT_DIR value (may be empty for non-git)
    is_git: bool
    is_externally_managed: bool
    guidance: str  # Human message for externally managed installs


def detect_install_layout() -> InstallLayout:
    """Detect the current install layout using the same logic as the dashboard.

    Derived from :func:`derive_capability` rather than from a second reading of
    the same signals. Order matters and it is the reason this delegates: asking
    ``is_git_worktree`` FIRST classified a container whose ``KIROCREW_PROJECT_DIR``
    points at a checkout as a git install, so the channel endpoint refused the
    switch with "a git checkout follows its git remote" instead of the guidance
    naming the surface that actually updates it. An externally managed stamp wins
    over a mounted checkout, and the capability contract is where that precedence
    is decided once.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    capability = derive_capability(install_root=proj)

    if capability.managed_by == MANAGED_BY_GIT:
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
    where a directory is is the blocking hazard this avoids.
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

    Written through :func:`atomic_write` so a crash or a full disk cannot
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
    atomic_write(data_home() / "channel", f"{normalized}\n")
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
    "non_managed_pip_update_hint",
    "non_managed_pip_upgrade_command",
    "RELEASE_CHANNELS",
    "EXTERNALLY_MANAGED",
]
