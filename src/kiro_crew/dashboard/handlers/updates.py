"""Update check/apply, log level, ring buffer, and SSE stream handlers."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew import __version__ as _local_version
from kiro_crew import shutdown_event
from kiro_crew.beacon import distribution
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_dir,
    config_path,
    read_config_for_update,
    write_config_atomically,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.platform.update_governance import (
    min_version,
    resolve_remote_url,
    update_blocked_reason,
    update_required,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_SSE_INTERVAL_SECS = 5

# ── Update ──

# Cached update check result.
#
# ``checked`` is LOAD-BEARING, not decoration: the dashboard renders "you're on
# the latest version" only when a real comparison completed. Before this was
# enforced, a check that never ran (every wheel install — see
# ``_do_update_check``) left this dict at its initial ``available: False`` and the
# UI reported an out-of-date install as up to date.
_update_info: dict[str, object] = {
    "available": False,
    "changes": "",
    "remote_version": "",
    "checked": False,
    "install_kind": "",
    "self_updatable": False,
    "channel": "",
    "update_command": "",
    "error": "",
}
_UPDATE_CHECK_INTERVAL = 43200  # 12 hours
_last_update_check: float = 0.0

#: True while a check is running. ``/api/status`` fires ``_do_update_check`` as a
#: background task on EVERY poll until the interval clock is stamped, and the clock
#: is only stamped when a check FINISHES. While the check was git-only and
#: instantaneous for most installs, overlapping calls were invisible; the feed
#: branch holds a network session for up to ``_FEED_TIMEOUT_SECS``, so a burst of
#: dashboard polls would open a burst of concurrent CDN fetches. First caller wins,
#: the rest no-op.
_check_in_flight = False

#: Release channels the installer publishes. Anything else in the channel file (a
#: hand-edit, junk, a lane this build predates) falls back to ``stable``.
_RELEASE_CHANNELS = ("stable", "insider", "nightly")

#: ``schema`` every CLI artifact manifest carries. A payload without it is not a
#: manifest and must not be read as one.
_CLI_MANIFEST_SCHEMA = "kirocrew-cli-artifact-manifest-v1"

#: Same ceiling ``cli.sh`` applies with ``curl --max-filesize`` when it fetches
#: the very same document. Enforced against RECEIVED bytes, not Content-Length.
_FEED_MAX_BYTES = 65536
_FEED_TIMEOUT_SECS = 15

_VERSION_RE = re.compile(r"^[A-Za-z0-9._+!-]{1,64}$")
_PUB_DATE_RE = re.compile(r"^[0-9TZ:.\-]{1,32}$")

# Error codes for ``_update_info["error"]``. The dashboard maps these to
# localized copy, so they are a contract: add, never silently repurpose.
_ERR_FEED_UNREACHABLE = "feed_unreachable"
_ERR_FEED_MALFORMED = "feed_malformed"
_ERR_GIT_FETCH_FAILED = "git_fetch_failed"
_ERR_GIT_READ_FAILED = "git_read_failed"
_ERR_VERSION_UNPARSEABLE = "version_unparseable"
_ERR_MANAGED_BY_APP = "managed_by_app"
_ERR_MANAGED_BY_IMAGE = "managed_by_image"
_ERR_UNKNOWN = "unknown"

#: Distributions whose code is replaced by something OTHER than this gateway,
#: mapped to the reason they report instead of a verdict.
#:
#: The desktop bundles embed this very backend (``packaging/build-desktop.sh``
#: ships a PBS interpreter tree inside the .app / AppImage), so they DO run this
#: code — and they must not answer with it. Their update surface is the Electron
#: updater, which has its own feed (``latest-mac.yml`` / ``latest-linux.yml``),
#: its own version stream and its own consent UI. Reading the CLI feed here would
#: compare against the WRONG stream and then recommend an installer that does not
#: apply to a DMG. It would also be user-visible: the Settings nav dot is
#: ``status.update_available || desktopUpdateAvailable`` (``App.tsx``), so a
#: false positive lights a badge whose destination says "you're on the latest
#: version" — the desktop About branch, which is the only one rendered there.
#:
#: A container is likewise replaced by pulling a new image, never in place.
#:
#: Windows needs no entry: there is no Windows-native gateway. ``install.ps1``
#: makes the Windows machine a thin client for a gateway running on EC2 Linux, so
#: the backend answering this call is a Linux install that identifies itself
#: correctly on its own.
_EXTERNALLY_MANAGED = {
    "dmg": _ERR_MANAGED_BY_APP,
    "appimage": _ERR_MANAGED_BY_APP,
    "docker": _ERR_MANAGED_BY_IMAGE,
}


def get_update_info() -> dict[str, object]:
    """Return a copy of the cached update-check state."""
    return dict(_update_info)


async def api_update_check(request: web.Request) -> web.Response:
    """GET /api/update/check — is a newer build available for this install?

    Two install layouts, two sources of truth (see ``_do_update_check``): a git
    checkout is compared against its remote, and a wheel install against the
    release channel feed it was installed from.
    """
    await _do_update_check()
    cfg = KiroCrewConfig.load()
    return web.json_response(
        {
            **_update_info,
            "auto_update": cfg.auto_update,
            # Surface the pin so the dashboard can say WHY an update is mandatory
            # rather than showing a bare button.
            "min_version": min_version(),
            "update_required": update_required(_local_version),
        }
    )


#: Pre-release spellings PEP 440 normalizes, mapped to their sort rank.
_PRE_RANKS = {
    "a": 1,
    "alpha": 1,
    "b": 2,
    "beta": 2,
    "c": 3,
    "rc": 3,
    "pre": 3,
    "preview": 3,
}

_PEP440_RE = re.compile(
    r"""^\s*v?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?:[-_.]?(?P<post>post|r|rev)[-_.]?(?P<post_n>[0-9]+)?)?
    (?:[-_.]?(?P<dev>dev)[-_.]?(?P<dev_n>[0-9]+)?)?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_FIRST_INT_RE = re.compile(r"[0-9]+")


def _version_key(value: str) -> tuple[tuple[int, ...], int, int, int, int] | None:
    """Comparable ordering key for a version string, or ``None`` if unparseable.

    Returning ``None`` rather than a low-sorting sentinel is the whole point.
    The predecessor of this function coerced anything it could not parse to
    ``(0,)``, which made ``0.1.2rc3`` and ``0.1.3rc2`` compare EQUAL and reported
    "no update available" for every prerelease-to-prerelease step. A caller that
    cannot compare must say so, not answer "up to date".

    The key is ``(release, stage, stage_ordinal, dev_absent, dev_ordinal)`` and
    reproduces PEP 440's ordering::

        X.Y.Z.devN  <  X.Y.ZaN  <  X.Y.ZbN  <  X.Y.ZrcN.devM
                    <  X.Y.ZrcN  <  X.Y.Z   <  X.Y.Z.postN

    ``dev_absent`` is what places ``rc1.dev3`` below ``rc1``: a dev release of a
    prerelease precedes that prerelease, so it must sort first WITHIN the same
    ``(stage, stage_ordinal)`` pair.

    Release tuples are NOT padded here — comparing keys of different arity is the
    caller's job (:func:`_is_newer`), because padding depends on both sides.
    """
    text = str(value or "").strip()
    if not text:
        return None
    match = _PEP440_RE.match(text)
    suffix_ordinal: int | None = None
    if match is None:
        # Semver-ish stamps from the desktop lane: ``0.3.0-insider.2``,
        # ``0.3.0-nightly.20260728t184500``. Treat ANY unrecognised suffix as a
        # prerelease of its release core, so it sorts below the bare release —
        # the same direction PEP 440 orders rc against final. Guessing "newer"
        # here would offer a downgrade as an update.
        core, sep, rest = text.partition("-")
        if not sep:
            core, sep, rest = text.partition("+")
        if not sep:
            return None
        match = _PEP440_RE.match(core)
        if match is None:
            return None
        found = _FIRST_INT_RE.search(rest)
        suffix_ordinal = int(found.group()) if found else 0

    try:
        release = tuple(int(chunk) for chunk in match.group("release").split("."))
    except ValueError:  # pragma: no cover - the regex already bounds this
        return None

    pre_l = match.group("pre_l")
    has_post = bool(match.group("post"))
    has_dev = bool(match.group("dev"))

    if suffix_ordinal is not None:
        stage, ordinal = 3, suffix_ordinal
    elif pre_l:
        stage, ordinal = _PRE_RANKS[pre_l.lower()], int(match.group("pre_n") or 0)
    elif has_post:
        stage, ordinal = 5, int(match.group("post_n") or 0)
    elif has_dev:
        # A dev release of the release itself (X.Y.Z.devN) precedes every
        # prerelease of X.Y.Z, so it gets the lowest stage.
        stage, ordinal = 0, int(match.group("dev_n") or 0)
    else:
        stage, ordinal = 4, 0

    dev_of_pre = has_dev and (pre_l is not None or suffix_ordinal is not None)
    dev_absent = 0 if dev_of_pre else 1
    dev_ordinal = int(match.group("dev_n") or 0) if dev_of_pre else 0
    return (release, stage, ordinal, dev_absent, dev_ordinal)


def _is_newer(remote: str, local: str) -> bool | None:
    """Is *remote* strictly newer than *local*? ``None`` when either is unparseable.

    ``None`` propagates to ``error: version_unparseable`` instead of collapsing
    into ``available: False``: an unreadable version is a failed check, not a
    verdict.
    """
    remote_key = _version_key(remote)
    local_key = _version_key(local)
    if remote_key is None or local_key is None:
        return None
    # Zero-pad the release cores so 0.1 and 0.1.0 compare EQUAL rather than
    # letting the shorter tuple sort first, then fall through to the stage keys.
    r_rel, l_rel = remote_key[0], local_key[0]
    width = max(len(r_rel), len(l_rel))
    r_pad = r_rel + (0,) * (width - len(r_rel))
    l_pad = l_rel + (0,) * (width - len(l_rel))
    if r_pad != l_pad:
        return r_pad > l_pad
    return remote_key[1:] > local_key[1:]


def _cdn_bases() -> tuple[str, str]:
    """``(feed base, artifact base)`` — mirrors ``cli.sh``'s two URL classes.

    The feed host serves mutable pointers (``latest-cli.json``); the artifact host
    serves bytes. ``KIROCREW_CDN_BASE`` overrides BOTH, exactly as the installer's
    ``--cdn`` does, so a test or an alternate CDN moves the check and the install
    it recommends together instead of splitting them across hosts.
    """
    override = (os.environ.get("KIROCREW_CDN_BASE") or "").strip().rstrip("/")
    if override:
        return override, override
    return "https://updates.crew.kiro.dev", "https://download.crew.kiro.dev"


def _release_channel() -> str:
    """The release channel this install follows, from ``$KIROCREW_HOME/channel``.

    ``cli.sh`` WRITES this file on every install and never reads it back, which is
    also why :func:`_wheel_update_command` always spells ``--channel``: a bare
    re-run of the installer defaults to ``stable`` and would silently move an
    insider install onto the stable lane.
    """
    try:
        raw = (config_dir() / "channel").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "stable"
    channel = raw.strip().lower()
    return channel if channel in _RELEASE_CHANNELS else "stable"


def _wheel_update_command(channel: str, artifact_base: str) -> str:
    """The exact command that upgrades a wheel install on *channel*.

    Composed LOCALLY from an already-validated channel name. Never assembled from
    a feed field — see :func:`_check_release_feed` on why the manifest is treated
    as untrusted display metadata.

    ``--proto '=https'`` is not decoration: this string is copied into a shell and
    pipes an installer into ``sh``, and ``artifact_base`` is overridable via
    ``KIROCREW_CDN_BASE``. Without the restriction, an ``http://`` override would
    hand the user a command that fetches a script in plaintext and executes it, so
    an on-path attacker could swap the installer. curl refuses any other scheme,
    which is also exactly what ``cli.sh`` does for every fetch it makes itself.
    """
    return f"curl -fsSL --proto '=https' {artifact_base}/cli.sh | sh -s -- --channel {channel}"


def _set_update_info(**fields: object) -> None:
    """Replace the cached result wholesale so no key survives from a prior run.

    Mutating selected keys would let a previous success leak into a later failure
    (a stale ``remote_version`` beside a fresh ``error``), which is exactly the
    class of half-truth this module is being fixed for.
    """
    _update_info.clear()
    _update_info.update(
        {
            "available": False,
            "changes": "",
            "remote_version": "",
            "checked": False,
            "install_kind": "",
            "self_updatable": False,
            "channel": "",
            "update_command": "",
            "error": "",
        }
    )
    _update_info.update(fields)


async def _do_update_check() -> None:
    """Refresh ``_update_info``: is a newer build available for THIS install?

    Three install layouts, three answers:

    * **git checkout** — ``KIROCREW_PROJECT_DIR`` with a ``.git``. Fetch the
      remote and compare its ``src/kiro_crew/__init__.py`` ``__version__``. This
      is also the only layout ``POST /api/update`` can act on.
    * **externally managed** — a desktop bundle or a container
      (:data:`_EXTERNALLY_MANAGED`). This gateway is NOT the update surface, so
      it reports which surface is instead of guessing a verdict.
    * **everything else** — the ``cli.sh`` managed venv, a cloud/EC2 source
      install. Compare against the release channel feed the installer pulled
      from. This layout used to return early and leave the cache at its initial
      ``available: False``, so the dashboard told an out-of-date install it was
      up to date.

    The layout is decided by :func:`beacon.distribution`, which reads the value
    stamped into the package tree at packaging time. It is matched by EXCLUSION
    (desktop and container are named; everything else is feed-checkable) on
    purpose: wheels published before the stamp existed carry no ``_build_info``
    and so report the ``source`` default, and an ``== "wheel"`` allowlist would
    silently exclude every already-released CLI install — precisely the
    population this check is being fixed for.

    Every exit path writes the cache, failures included: a check that could not
    run records an ``error`` code and leaves ``checked`` False, so no caller can
    mistake a non-answer for a verdict.
    """
    global _last_update_check, _check_in_flight

    if _check_in_flight:
        return
    _check_in_flight = True

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    # exists() not isdir(): in linked worktrees and submodules ``.git`` is a FILE
    # holding a ``gitdir:`` pointer, and git works fine there.
    is_git = bool(proj) and os.path.exists(os.path.join(proj, ".git"))
    dist = "git" if is_git else distribution()
    try:
        if is_git:
            await _check_git_checkout(proj)
        elif dist in _EXTERNALLY_MANAGED:
            _set_update_info(
                install_kind=dist,
                self_updatable=False,
                error=_EXTERNALLY_MANAGED[dist],
            )
        else:
            await _check_release_feed(dist)
    except Exception:
        logger.debug("Update check failed", exc_info=True)
        _set_update_info(
            install_kind=dist,
            self_updatable=is_git,
            error=_ERR_UNKNOWN,
        )
    finally:
        _check_in_flight = False
        # Stamped even on failure, so an offline host or a broken feed cannot turn
        # the 12-hourly background poll into a hot retry loop. The dashboard's
        # manual button calls this function directly and is never rate-limited.
        _last_update_check = time.time()


async def _check_git_checkout(proj: str) -> None:
    """Compare a git checkout in *proj* against its tracked remote."""
    base: dict[str, object] = {"install_kind": "git", "self_updatable": True}

    proc = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        "--quiet",
        cwd=proj,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, fetch_err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        logger.warning("git fetch timed out")
        _set_update_info(**base, error=_ERR_GIT_FETCH_FAILED)
        return
    if proc.returncode != 0:
        logger.warning(
            "git fetch failed (rc=%s): %s",
            proc.returncode,
            (fetch_err or b"").decode(errors="replace").strip(),
        )
        _set_update_info(**base, error=_ERR_GIT_FETCH_FAILED)
        return

    local = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "HEAD",
        cwd=proj,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        local_out, _ = await asyncio.wait_for(local.communicate(), timeout=10)
    except asyncio.TimeoutError:
        try:
            local.kill()
        except ProcessLookupError:
            pass
        await local.communicate()
        _set_update_info(**base, error=_ERR_GIT_READ_FAILED)
        return
    remote = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "@{u}",
        cwd=proj,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        remote_out, _ = await asyncio.wait_for(remote.communicate(), timeout=10)
    except asyncio.TimeoutError:
        try:
            remote.kill()
        except ProcessLookupError:
            pass
        await remote.communicate()
        _set_update_info(**base, error=_ERR_GIT_READ_FAILED)
        return

    local_sha = local_out.decode(errors="replace").strip()
    remote_sha = remote_out.decode(errors="replace").strip()
    if not local_sha or not remote_sha:
        # No upstream (detached HEAD, untracked branch) or an unreadable HEAD.
        # There is nothing to compare against, which is a failed check and not
        # "you are on the latest version".
        logger.warning("Could not resolve HEAD and/or upstream in %s", proj)
        _set_update_info(**base, error=_ERR_GIT_READ_FAILED)
        return

    # Compare the remote's version (or the on-disk one when already pulled).
    target_sha = remote_sha if local_sha != remote_sha else local_sha
    show = await asyncio.create_subprocess_exec(
        "git",
        "show",
        f"{target_sha}:src/kiro_crew/__init__.py",
        cwd=proj,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        show_out, _ = await asyncio.wait_for(show.communicate(), timeout=10)
    except asyncio.TimeoutError:
        try:
            show.kill()
        except ProcessLookupError:
            pass
        await show.communicate()
        _set_update_info(**base, error=_ERR_GIT_READ_FAILED)
        return
    match = re.search(r'__version__\s*=\s*"(.+?)"', show_out.decode(errors="replace"))
    if not match:
        logger.warning("Could not read __version__ at %s", target_sha)
        _set_update_info(**base, error=_ERR_GIT_READ_FAILED)
        return
    remote_version = match.group(1)
    available = _is_newer(remote_version, _local_version)
    if available is None:
        logger.warning(
            "Cannot compare local version %s against remote %s",
            _local_version,
            remote_version,
        )
        _set_update_info(**base, remote_version=remote_version, error=_ERR_VERSION_UNPARSEABLE)
        return

    changes = ""
    if available:
        diff_base = f"v{_local_version}" if local_sha == remote_sha else local_sha
        diff = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            f"{diff_base}..{target_sha}",
            "--",
            "CHANGELOG.md",
            cwd=proj,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            diff_out, _ = await asyncio.wait_for(diff.communicate(), timeout=10)
        except asyncio.TimeoutError:
            try:
                diff.kill()
            except ProcessLookupError:
                pass
            await diff.communicate()
            # The version comparison already succeeded — report the update and
            # simply omit the changelog rather than discarding a good verdict.
            diff_out = b""
        # Extract added lines from the changelog diff.
        lines: list[str] = []
        for line in diff_out.decode(errors="replace").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])
        changes = "\n".join(lines).strip()

    _set_update_info(
        **base,
        available=available,
        changes=changes,
        remote_version=remote_version,
        checked=True,
    )


async def _fetch_feed_bytes(url: str) -> tuple[int, bytes]:
    """GET *url*, returning ``(status, body)`` with the body read BOUNDED.

    Split out as the single network seam of the update check so tests stub this
    one function instead of the aiohttp machinery — see the autouse guard in
    ``test/conftest.py`` that makes it impossible for the suite to reach the
    real CDN.
    """
    timeout = aiohttp.ClientTimeout(total=_FEED_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            # +1 byte so an oversized body is DETECTED rather than silently
            # truncated into a parse error. Mirrors the installer's
            # --max-filesize on this very document, and is enforced against
            # RECEIVED bytes rather than a Content-Length claim.
            return resp.status, await resp.content.read(_FEED_MAX_BYTES + 1)


async def _check_release_feed(install_kind: str) -> None:
    """Compare a feed-checkable install against the release channel it came from.

    *install_kind* is reported verbatim rather than hardcoded to ``"wheel"``: an
    unstamped pre-``_build_info`` wheel reports ``source``, and flattening the two
    would put a value on the wire that the install cannot back up.

    **Security posture — the manifest is UNTRUSTED display metadata.** This
    function deliberately does not verify its RSA signature: that check lives in
    ``cli.sh``, which pins the key offline and is the only thing that installs
    bytes. So nothing actionable is taken from the payload — ``wheel_url``,
    ``sha256`` and ``signature`` are read by nobody here, and the recommended
    command is composed locally from the already-validated channel name. A
    tampered feed can therefore nag the user or hide an update; it cannot
    redirect an install. Verifying the signature in Python becomes necessary only
    if the gateway itself ever installs, which it does not.
    """
    channel = _release_channel()
    feed_base, artifact_base = _cdn_bases()
    base: dict[str, object] = {
        "install_kind": install_kind,
        "self_updatable": False,
        "channel": channel,
        "update_command": _wheel_update_command(channel, artifact_base),
    }
    url = f"{feed_base}/feed/{channel}/latest-cli.json"

    try:
        status, raw = await _fetch_feed_bytes(url)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        logger.debug("Release feed fetch failed: %s", url, exc_info=True)
        _set_update_info(**base, error=_ERR_FEED_UNREACHABLE)
        return
    if status != 200:
        logger.warning("Release feed %s returned HTTP %s", url, status)
        _set_update_info(**base, error=_ERR_FEED_UNREACHABLE)
        return

    if len(raw) > _FEED_MAX_BYTES:
        logger.warning("Release feed %s exceeded %d bytes", url, _FEED_MAX_BYTES)
        _set_update_info(**base, error=_ERR_FEED_MALFORMED)
        return
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("Release feed %s is not valid JSON", url)
        _set_update_info(**base, error=_ERR_FEED_MALFORMED)
        return
    if not isinstance(manifest, dict):
        _set_update_info(**base, error=_ERR_FEED_MALFORMED)
        return
    # The channel assertion is what stops a mis-wired or swapped feed from
    # advertising another lane's build to this install.
    if manifest.get("schema") != _CLI_MANIFEST_SCHEMA or manifest.get("channel") != channel:
        logger.warning("Release feed %s is not a %s for %s", url, _CLI_MANIFEST_SCHEMA, channel)
        _set_update_info(**base, error=_ERR_FEED_MALFORMED)
        return
    remote_version = manifest.get("version")
    if not isinstance(remote_version, str) or not _VERSION_RE.match(remote_version):
        logger.warning("Release feed %s carries no usable version", url)
        _set_update_info(**base, error=_ERR_FEED_MALFORMED)
        return

    available = _is_newer(remote_version, _local_version)
    if available is None:
        logger.warning(
            "Cannot compare local version %s against feed version %s",
            _local_version,
            remote_version,
        )
        _set_update_info(**base, remote_version=remote_version, error=_ERR_VERSION_UNPARSEABLE)
        return

    extra: dict[str, object] = {}
    pub_date = manifest.get("pub_date")
    if isinstance(pub_date, str) and _PUB_DATE_RE.match(pub_date):
        extra["remote_pub_date"] = pub_date
    # No ``changes``: the manifest carries no changelog, and the CHANGELOG.md
    # bundled into the wheel describes the version already INSTALLED, not the new
    # one. The dashboard's "view full changelog" disclosure covers the gap
    # rather than this function inventing a diff it cannot see.
    _set_update_info(
        **base,
        available=available,
        remote_version=remote_version,
        checked=True,
        **extra,
    )


async def api_update_auto(request: web.Request) -> web.Response:
    """POST /api/update/auto — toggle auto-update on/off."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    enabled = body.get("enabled", True)
    # Read, modify, write config. The read fails CLOSED: treating an unreadable
    # config as {} would write back a single-key file and wipe every other
    # setting the user has (see read_config_for_update).
    path = config_path()
    try:
        data = read_config_for_update(path)
    except ConfigReadError:
        logger.exception("Refusing to toggle auto-update: config is unreadable")
        return web.json_response(
            {"error": "failed to read config file", "code": "config_unreadable"}, status=500
        )
    data["auto_update"] = enabled
    write_config_atomically(path, data)
    return web.json_response({"ok": True, "auto_update": enabled})


def _changelog_path() -> Path | None:
    """Locate CHANGELOG.md across install layouts.

    1. ``KIROCREW_PROJECT_DIR/CHANGELOG.md`` — dev / git installs.
    2. Bundled ``kiro_crew/CHANGELOG.md`` — pip-wheel installs where
       no source tree is present (copied into the package at build time by
       ``setup.py``'s ``BuildWithFrontend._copy_changelog``).

    Returns the first existing path, or ``None`` if neither is found.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if proj:
        p = Path(proj) / "CHANGELOG.md"
        if p.is_file():
            return p
    # updates.py lives at kiro_crew/dashboard/handlers/ — parents[2] == kiro_crew/
    bundled = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    if bundled.is_file():
        return bundled
    return None


#: Cached CHANGELOG.md body, keyed on ``(path, st_mtime_ns, st_size)``.
#: Caching avoids reading and decoding the whole file on the event loop on
#: every ``GET /api/changelog`` request (the About panel re-fetches on each
#: open, and the file grows with every release). The stat signature keeps a
#: dev-install edit visible immediately, so the endpoint stays live.
_changelog_cache: tuple[tuple[str, int, int], str] | None = None


def _read_changelog() -> str:
    """Return CHANGELOG.md's contents, re-reading only when the file changes."""
    global _changelog_cache
    path = _changelog_path()
    if path is None:
        return ""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return ""
    cached = _changelog_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    _changelog_cache = (key, content)
    return content


async def api_changelog(request: web.Request) -> web.Response:
    """GET /api/changelog — read full CHANGELOG.md from project or bundle."""
    return web.json_response({"content": _read_changelog()})


async def _build_frontend(proj: str, state: DashboardState) -> None:
    """Build the in-tree ``website/`` frontend and stage it into ``static/dist``.

    Delegates to the shared :func:`kiro_crew.frontend.build_frontend_async`
    helper so the build/stage logic is not duplicated across the three update
    paths (CLI ``kirocrew update``, this dashboard endpoint, and the gateway
    auto-update). That helper runs ``npm ci`` (fallback ``npm install``) then
    ``npm run build`` in ``<proj>/website`` and copies ``website/dist`` into
    the served ``src/kiro_crew/static/dist`` — without which the dashboard
    would serve a stale bundle after an update.

    Graceful no-op when there is no ``website/`` directory or ``npm`` is not
    installed (a packaged checkout may ship prebuilt assets). Build warnings
    are surfaced via ``state.push_update_progress`` after credential/URL
    redaction.
    """
    from kiro_crew import frontend

    def _push(step: str, msg: str) -> None:
        msg, _ = redact_credentials(msg)
        msg, _ = redact_exfiltration_urls(msg)
        # frontend emits ("warning", detail); show it as a non-fatal build note.
        state.push_update_progress("building", msg)

    state.push_update_progress("building", "Building frontend (npm)…")
    await frontend.build_frontend_async(proj, push_progress=_push)


async def _venv_pip_install(proj: str, state: DashboardState) -> bool:
    """Run `pip install -e .` to (re)install the package from source.

    Returns ``True`` on success. On failure, pushes an error to ``state`` and
    returns ``False`` — caller should ``return``.
    """
    state.push_update_progress("building", "Installing package (pip)…")
    install = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pip",
        "install",
        "-e",
        ".",
        "--quiet",
        cwd=proj,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(install.communicate(), timeout=120)
    except asyncio.TimeoutError:
        try:
            install.kill()
        except ProcessLookupError:
            pass
        await install.communicate()
        state.push_update_progress("error", "pip install timed out")
        return False
    if install.returncode != 0:
        raw_err = stderr.decode()
        raw_err, _ = redact_credentials(raw_err)
        raw_err, _ = redact_exfiltration_urls(raw_err)
        # Build wheel / native extension errors often have the actionable message
        # mid-stderr after a long traceback, so keep up to 1000 chars after redaction.
        if len(raw_err) > 1000:
            raw_err = raw_err[:1000] + "\n…(truncated)"
        state.push_update_progress("error", f"pip install failed: {raw_err}")
        return False
    return True


async def _restart_gateway(state: DashboardState) -> None:
    """Save state, close sessions, and exec the same Python process."""
    state.push_update_progress("restarting", "Restarting server…")
    exe = sys.executable
    if not os.path.isfile(exe) or not os.access(exe, os.X_OK):
        state.push_update_progress("error", "Cannot restart: invalid Python executable path")
        return
    # circular import: kiro_crew.dashboard.chat imports from
    # kiro_crew.dashboard.handlers (which re-exports this module), so this
    # must stay inline to avoid an import cycle at module load.
    from kiro_crew.dashboard.chat import save_all_slots_to_history
    from kiro_crew.executors import subprocess_executor

    try:
        # Offload the synchronous per-slot save (per-session lock + disk I/O)
        # to the bounded subprocess_executor with a deadline: on the event loop
        # a contended session raises HistoryLockTimeout and a wedged disk would
        # block the restart, so a slot's final save must run off-loop and be
        # time-bounded rather than stall (or silently drop) here.
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), save_all_slots_to_history, state
            ),
            timeout=5.0,
        )
    except Exception:
        logger.debug("History save before restart failed", exc_info=True)
    try:
        await state.sessions.close_all()
    except Exception:
        logger.debug("Session cleanup before restart failed", exc_info=True)
    sys.stdout.flush()
    sys.stderr.flush()
    await asyncio.sleep(0.5)
    os.execv(exe, [exe, "-m", "kiro_crew"] + sys.argv[1:])


async def api_update_apply(request: web.Request) -> web.Response:
    """POST /api/update — git pull, rebuild, restart gateway."""
    state: DashboardState = request.app["state"]

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        return web.json_response({"error": "KIROCREW_PROJECT_DIR not set"}, status=400)
    # Mirror the _do_update_check git guard: a tarball install (e.g. cloud/EC2)
    # has no .git, so `git pull` cannot update it — fail with a clear message
    # instead of a generic "git pull failed".
    if not os.path.exists(os.path.join(proj, ".git")):
        return web.json_response(
            {"error": "Not a git checkout — update by redeploying (e.g. `kirocrew cloud launch`)"},
            status=409,
        )

    # Source pin, checked before any state is pushed so a blocked update leaves
    # no "updating" spinner behind. A dashboard token proves the caller is the
    # operator, not that the fleet permits this host to pull from this remote.
    # Offloaded: the seam shells out to git.
    blocked = await asyncio.get_running_loop().run_in_executor(
        None, lambda: update_blocked_reason(resolve_remote_url(proj))
    )
    if blocked:
        logger.warning("Update refused: %s", blocked)
        return web.json_response({"error": blocked, "governance": True}, status=403)

    # Signal updating state via SSE
    state.push_refresh("updating")

    # Check for dirty working tree before updating
    dirty = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain",
        cwd=proj,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        dirty_out, _ = await asyncio.wait_for(dirty.communicate(), timeout=10)
    except asyncio.TimeoutError:
        try:
            dirty.kill()
        except ProcessLookupError:
            pass
        await dirty.communicate()
        return web.json_response(
            {"error": "Timed out checking working tree status"},
            status=500,
        )
    if dirty_out and dirty_out.strip():
        logger.warning("Update skipped: working tree has uncommitted changes")
        return web.json_response(
            {"error": "Working tree has uncommitted changes — commit or stash first"},
            status=409,
        )

    async def _apply() -> None:
        try:
            state.push_update_progress("pulling", "Pulling latest changes…")
            pull = await asyncio.create_subprocess_exec(
                "git",
                "pull",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(pull.communicate(), timeout=60)
            except asyncio.TimeoutError:
                try:
                    pull.kill()
                except ProcessLookupError:
                    pass
                await pull.communicate()
                state.push_update_progress("error", "git pull timed out")
                return
            if pull.returncode != 0:
                state.push_update_progress("error", "git pull failed")
                return

            # Rebuild the in-tree frontend and stage website/dist into the
            # served static/dist (graceful no-op if no website/ or npm).
            # Done before pip install so the served bundle is refreshed even
            # if the package reinstall later hiccups.
            await _build_frontend(proj, state)

            # Reinstall the package so any new Python deps / entry points land.
            if not await _venv_pip_install(proj, state):
                return

            # Restart: save history + clean up sessions then exec the same process.
            logger.info("Update complete — saving history and cleaning up before restart")
            await _restart_gateway(state)
        except Exception:
            logger.exception("Update failed")
            state.push_update_progress("failed", "Update failed — check logs")
            state.push_refresh("update_failed")

    task = asyncio.create_task(_apply())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "status": "updating"})


async def api_update_cancel(request: web.Request) -> web.Response:
    """POST /api/update/cancel — dismiss a stuck/failed update overlay."""
    state: DashboardState = request.app["state"]
    state.clear_update_progress()
    state.push_update_progress("failed", "Update cancelled by user")
    # Give clients a moment to receive the failed event, then clear
    await asyncio.sleep(0.2)
    state.clear_update_progress()
    return web.json_response({"ok": True})


async def api_update_simulate(request: web.Request) -> web.Response:
    """POST /api/update/simulate — walk through update steps with delays.

    For local testing only. Cycles through each progress step with a
    configurable delay (default 2s per step).
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Simulate a pre-flight rejection (e.g. dirty working tree)
    if body.get("reject"):
        msg = body.get(
            "reject_message", "Working tree has uncommitted changes — commit or stash first"
        )
        return web.json_response({"error": msg}, status=409)

    delay = body.get("delay", 2)
    fail_at = body.get("fail_at", "")  # optional: step name to fail at

    async def _sim() -> None:
        steps = [
            ("pulling", "Pulling latest changes…"),
            ("building", "Installing package (pip)…"),
            ("building", "Building frontend (npm)…"),
            ("restarting", "Restarting server…"),
        ]
        for step, detail in steps:
            if fail_at and step == fail_at:
                state.push_update_progress("failed", f"Simulated failure at {step}")
                return
            state.push_update_progress(step, detail)
            await asyncio.sleep(delay)
        # Simulate completion — broadcast "done" so frontend clears the overlay
        state.push_update_progress("done", "Update complete")
        state.clear_update_progress()

    task = asyncio.create_task(_sim())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "status": "simulating"})


# ── Logs SSE ──


_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


async def api_log_level(request: web.Request) -> web.Response:
    """POST /api/logs/level — change the kiro_crew logger level at runtime.

    Also persists the new level to config so it survives restarts.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    level_name = body.get("level", "").upper()
    if level_name not in _LOG_LEVELS:
        return web.json_response({"error": f"invalid level: {level_name}"}, status=400)
    root = logging.getLogger("kiro_crew")
    root.setLevel(_LOG_LEVELS[level_name])
    logger.info("Log level changed to %s via dashboard", level_name)

    # Persist to config so the level survives restarts.
    persisted = False
    try:
        cfg = KiroCrewConfig.load()
        cfg.agent.log_level = level_name
        cfg.save()
        persisted = True
    except Exception:
        logger.warning("Failed to persist log level to config", exc_info=True)

    return web.json_response({"ok": True, "level": level_name, "persisted": persisted})


async def api_log_level_get(request: web.Request) -> web.Response:
    """GET /api/logs/level — current kiro_crew logger level."""
    root = logging.getLogger("kiro_crew")
    return web.json_response({"level": logging.getLevelName(root.level)})


class _QueueLogHandler(logging.Handler):
    """Logging handler that enqueues formatted log entries for SSE delivery."""

    def __init__(self, queue: asyncio.Queue) -> None:  # type: ignore[type-arg]
        super().__init__()
        self._queue: asyncio.Queue[str] = queue  # type: ignore[type-arg]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            data = json.dumps({"level": record.levelname, "msg": msg})
            self._queue.put_nowait(data)
        except Exception:
            pass


# ── Persistent log ring buffer ──

_LOG_RING_SIZE = 1000
_log_ring: collections.deque[str] = collections.deque(maxlen=_LOG_RING_SIZE)
_log_ring_handler_installed = False
_log_ring_handler: _RingLogHandler | None = None


async def _safe_ws_send(ws: web.WebSocketResponse, msg: str, state: DashboardState) -> None:
    """Send a log frame to one subscriber, re-checking its scope first.

    Subscription is granted once, in the ``subscribe_logs`` handler, and the
    handler above then fans out straight to ``_ws_log_subscribers`` without
    passing the broadcast chokepoint. Re-check here so revoking an app's ``log``
    scope (a narrowed manifest, or ``app disable``) stops the stream on a socket
    that is already subscribed, matching what
    ``DashboardState._ws_client_allowed`` does for every other event.

    This runs on the event loop (the caller hands it to ``create_task`` via
    ``call_soon_threadsafe``), which is what makes the check safe: the scope
    cache must never be consulted from the logging thread, where a cold miss
    would fall back to a synchronous manifest read.
    """
    try:
        if not ws.get("_is_dashboard_user", False):
            from kiro_crew.dashboard.ws_event_scope import effective_allowed_events

            app = ws.get("_app", "")
            allowed = effective_allowed_events(
                app, ws.get("_allowed_events", frozenset())
            )
            if "log" not in allowed and "log:all" not in allowed:
                state._ws_log_subscribers.discard(ws)
                return
        await ws.send_str(msg)
    except Exception:
        state._ws_log_subscribers.discard(ws)


class _RingLogHandler(logging.Handler):
    """Always-on handler that keeps the last N log entries in a ring buffer.

    Also pushes log events to WebSocket log subscribers.
    """

    def __init__(
        self,
        ring: collections.deque[str],
        max_size: int = _LOG_RING_SIZE,
    ) -> None:
        super().__init__()
        self._ring = ring
        self._max = max_size
        self._state: DashboardState | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_state(self, state: DashboardState) -> None:
        """Attach DashboardState for WS log broadcasting."""
        self._state = state
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            data = json.dumps({"level": record.levelname, "msg": msg})
            self._ring.append(data)
            # Push to WS log subscribers (thread-safe via call_soon_threadsafe)
            if self._state and self._loop and self._state._ws_log_subscribers:
                ws_msg = json.dumps(
                    {"type": "log", "data": {"level": record.levelname, "msg": msg}}
                )
                for ws in list(self._state._ws_log_subscribers):
                    try:
                        self._loop.call_soon_threadsafe(
                            self._loop.create_task,
                            _safe_ws_send(ws, ws_msg, self._state),
                        )
                    except RuntimeError:
                        pass
        except Exception:
            pass


def install_log_ring_handler() -> _RingLogHandler | None:
    """Install the persistent ring buffer handler (call once at startup)."""
    global _log_ring_handler_installed, _log_ring_handler  # noqa: PLW0603
    if _log_ring_handler_installed:
        return _log_ring_handler
    _log_ring_handler_installed = True
    handler = _RingLogHandler(_log_ring, _LOG_RING_SIZE)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("kiro_crew").addHandler(handler)
    _log_ring_handler = handler
    return handler


async def api_logs(request: web.Request) -> web.StreamResponse:
    """GET /api/logs — SSE stream of live log entries.

    Query params:
      - ``lines``: max ring-buffer entries to replay on connect (default 200, max 1000).

    On connect, replays the last *lines* log entries from the ring buffer
    so the client sees history even if the Logs page wasn't open.
    """
    try:
        lines_cap = min(max(int(request.query.get("lines", "200")), 1), _LOG_RING_SIZE)
    except (TypeError, ValueError):
        lines_cap = 200
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    try:
        await resp.prepare(request)
    except (ConnectionResetError, ClientConnectionResetError):
        return resp

    # Replay buffered history first (capped by ?lines=N)
    ring_snapshot = list(_log_ring)
    for data in ring_snapshot[-lines_cap:]:
        try:
            await resp.write(f"data: {data}\n\n".encode())
        except (ConnectionResetError, ClientConnectionResetError):
            return resp

    log_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
    handler = _QueueLogHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("kiro_crew")
    root.addHandler(handler)
    try:
        while not shutdown_event.is_set():
            # Drain any queued log entries
            while not log_queue.empty():
                try:
                    data = log_queue.get_nowait()
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.QueueEmpty:
                    break

            # Wait for new entries or keepalive timeout
            try:
                data = await asyncio.wait_for(log_queue.get(), timeout=30)
                await resp.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        root.removeHandler(handler)
    return resp


# ── Dashboard SSE ──


async def api_stream(request: web.Request) -> web.StreamResponse:
    """SSE endpoint — pushes status + notifications to each connected client.

    Each client gets its own notification queue (broadcast pattern) so
    multiple tabs all receive every notification.  Uses a simple sleep
    loop with short intervals to check for notifications — lightweight
    and avoids future leaks from asyncio.wait().
    """
    state: DashboardState = request.app["state"]
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    try:
        await resp.prepare(request)
    except (ConnectionResetError, ClientConnectionResetError):
        return resp

    # Register a per-client notification queue
    client_q = state.register_sse()
    try:
        while not shutdown_event.is_set():
            # Drain this client's notification/slots queue
            while not client_q.empty():
                try:
                    note = client_q.get_nowait()
                    msg_type = note.get("_type", "")
                    if msg_type == "slots":
                        payload = note["slots"]
                        await resp.write(f"event: slots\ndata: {payload}\n\n".encode())
                    elif msg_type == "slot_title":
                        payload = json.dumps({"key": note["key"], "title": note["title"]})
                        await resp.write(f"event: slot_title\ndata: {payload}\n\n".encode())
                    elif msg_type == "refresh":
                        await resp.write(f"event: refresh\ndata: {note['kinds']}\n\n".encode())
                    elif msg_type == "chat_message":
                        payload = json.dumps(
                            {
                                "slot": note["slot"],
                                "role": note["role"],
                                "content": note["content"],
                                "ts": note.get("ts", ""),
                            }
                        )
                        await resp.write(f"event: chat_message\ndata: {payload}\n\n".encode())
                    else:
                        payload = json.dumps(note)
                        await resp.write(f"event: notification\ndata: {payload}\n\n".encode())
                except asyncio.QueueEmpty:
                    break

            data = json.dumps(
                {
                    **state.status_snapshot(update_available=bool(_update_info.get("available"))),
                    "version": _local_version,
                }
            )
            await resp.write(f"event: dashboard\ndata: {data}\n\n".encode())

            # Sleep in short intervals, wake early if notification arrives
            for _ in range(_SSE_INTERVAL_SECS * 4):
                if shutdown_event.is_set() or not client_q.empty():
                    break
                await asyncio.sleep(0.25)
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        state.unregister_sse(client_q)
    return resp
