"""Update check/apply, log level, ring buffer, and SSE stream handlers."""

from __future__ import annotations

import asyncio
import collections
import functools
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
from kiro_crew import dep_sync, shutdown_event
from kiro_crew.changelog import Release, base_version, build_release_list
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.dashboard.handlers._shared import read_capped_response
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.git_divergence import (
    UNREADABLE_TIMEOUT,
    DivergenceUnreadable,
    count_divergence,
)
from kiro_crew.platform.update_capability import (
    CHECK_DEFERRED,
    CHECK_FAILED,
    CHECK_SUCCEEDED,
    CHECK_UNCHECKED,
    ERR_FEED_MALFORMED,
    ERR_FEED_UNREACHABLE,
    ERR_GIT_FETCH_FAILED,
    ERR_GIT_READ_FAILED,
    ERR_UNKNOWN,
    ERR_VERSION_UNPARSEABLE,
    EXTERNALLY_MANAGED_MESSAGES,
    MANAGED_BY_COMMAND,
    MANAGED_BY_GIT,
    MODE_NONE,
    MODE_NOTIFY,
    UpdateCapability,
    derive_capability,
)
from kiro_crew.platform.update_governance import (
    min_version,
    resolve_remote_url,
    update_blocked_reason,
    update_required,
)
from kiro_crew.platform.update_layout import cdn_bases as _cdn_bases
from kiro_crew.platform.update_layout import detect_install_layout
from kiro_crew.platform.update_layout import release_channel as _release_channel
from kiro_crew.platform.update_layout import set_release_channel, wheel_update_command
from kiro_crew.platform.update_provider import CommandProvider, resolve_provider
from kiro_crew.platform_compat import reexec_python_module
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_SSE_INTERVAL_SECS = 5

# ── Update ──

# Cached update check result, in the capability contract's own vocabulary.
#
# ``check_status`` is LOAD-BEARING, not decoration, and ``update_available`` is
# nullable BECAUSE of it: "up to date" means ``check_status == "succeeded" and
# update_available is False``, and a consumer that reads a missing verdict as
# "current" reproduces the defect this contract exists to prevent — a check that
# never ran, rendered as a check that passed.
_update_info: dict[str, object] = {
    "supported": True,
    "managed_by": "",
    "mode": MODE_NONE,
    "can_download": False,
    "can_apply": False,
    "requires_restart": True,
    "channel": "",
    "latest_version": "",
    "changes": "",
    "check_status": CHECK_UNCHECKED,
    "update_available": None,
    #: Did the RELEASE VERSION move? Reported separately from
    #: ``update_available`` because the two answer different questions and one
    #: consumer needs the narrower one.
    #:
    #: ``update_available`` is what the dashboard shows, and for a git checkout it
    #: is true on commit distance alone. The unattended
    #: ``GatewayOrchestrator._auto_apply_update`` applies `git reset --hard`, so
    #: it requires BOTH: acting on commit distance alone would reset a
    #: developer's checkout within 12 hours of any upstream commit, where before
    #: it only did so at a release. Requiring both keeps that path firing no more
    #: often than it did while the verdict was version-only.
    "version_newer": False,
    #: Commit distance from the tracked upstream, BOTH directions. A DIVERGED
    #: checkout (ahead and behind at once) reports ``update_available: False``
    #: exactly like a current one — offering it an update would feed the
    #: destructive ``git reset --hard`` apply path commits to discard — so
    #: without the counts the two states are indistinguishable on the wire and
    #: the panel can only say "up to date" to a user who actually needs to
    #: rebase or merge. 0/0 everywhere except a successful git-checkout check.
    "commits_ahead": 0,
    "commits_behind": 0,
    "error_code": None,
    "unavailable_reason": None,
    "remediation": None,
}
#: Bumped whenever the thing a check is computed AGAINST changes (today: a channel
#: switch). A check already talking to the OLD channel's feed cannot be cancelled,
#: so without this it finishes afterwards and re-pins its stale verdict plus the
#: 12-hourly clock — pinning a stale answer for half a day to a channel the
#: install no longer follows.
_check_generation = 0

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

# Error codes for ``_update_info["error_code"]``. The dashboard maps these to
# localized copy, so they are a contract: add, never silently repurpose. Defined
# in ``platform/update_capability`` so the CLI and the dashboard cannot disagree
# about what a failure is called.


def get_update_info() -> dict[str, object]:
    """Return a copy of the cached update-check state."""
    return dict(_update_info)


def remediation_command(info: dict[str, object]) -> str:
    """The copyable command from a check result's ``remediation``, or ``""``.

    Display/copy only: no caller executes it, and it is composed locally from
    validated inputs rather than from any feed field.
    """
    remediation = info.get("remediation")
    if isinstance(remediation, dict):
        return str(remediation.get("command") or "")
    return ""


def _display_version(version: str, channel: str) -> str:
    """The version string shown to the user for the CURRENT build.

    Promotion never re-stamps: a promoted STABLE build carries the soaked
    candidate's prerelease stamp (``0.3.0rc13`` for the wheel, ``0.3.0-insider.13``
    for the desktop), because that stamp is baked into the bytes at insider-build
    time and is load-bearing there -- it keeps two insider RCs on distinct
    immutable per-version keys, and the auto-updater's compare gate requires the
    app version to equal the feed version. It therefore cannot be made clean in
    the bytes without abandoning promotion. So fold it to its clean base
    (``0.3.0``) for DISPLAY on the stable channel only; insider and nightly keep
    their full stamp, where the prerelease number is meaningful.

    Pure by design: the ``channel`` is passed in (from the off-loop
    ``_update_info["channel"]``) rather than read here, so this never touches the
    event loop. DISPLAY ONLY -- every version COMPARISON (``_is_newer``,
    ``update_required``, the feed check) still uses the raw ``__version__``, so
    folding here can never make a client miscompare or loop on updates.
    """
    return base_version(version) if channel == "stable" else version


def _display_local_version() -> str:
    """``_local_version`` folded for display, keyed on the off-loop channel that
    a prior ``_do_update_check`` resolved into ``_update_info`` -- a plain dict
    read, so this stays off the event loop.
    """
    return _display_version(_local_version, str(_update_info.get("channel") or ""))


def status_update_fields() -> dict[str, object]:
    """The update fields ``/api/status`` and the WebSocket push both carry.

    One reader for both emitters: the hot path carries a deliberate SUBSET of the
    contract (the full thing lives on ``GET /api/update/check``), and two
    hand-rolled copies of that subset would drift the moment a field is added.

    ``update_available`` is passed through unflattened — ``None`` means no
    verdict, and coercing it to ``False`` here would hand the dashboard the
    "never checked reads as current" bug at the last step.
    """
    available = _update_info.get("update_available")
    ahead = _update_info.get("commits_ahead")
    behind = _update_info.get("commits_behind")
    return {
        "update_available": available if isinstance(available, bool) else None,
        "update_can_apply": bool(_update_info.get("can_apply")),
        "update_check_status": str(_update_info.get("check_status") or CHECK_UNCHECKED),
        "update_command": remediation_command(_update_info),
        "update_channel": str(_update_info.get("channel") or ""),
        # The panel needs WHO manages the update to speak honestly: a
        # command-managed host must not render the self-managed installer
        # instructions its policy exists to bypass.
        "update_managed_by": str(_update_info.get("managed_by") or ""),
        # Commit distance for a git checkout, both directions, so the About
        # panel's badge can tell DIVERGED (ahead and behind at once — reported
        # as ``update_available: False`` because the apply path must never be
        # offered local commits) from genuinely current without waiting for a
        # manual check. 0/0 everywhere except a successful git-checkout check.
        "update_commits_ahead": ahead if isinstance(ahead, int) else 0,
        "update_commits_behind": behind if isinstance(behind, int) else 0,
    }


async def api_update_check(request: web.Request) -> web.Response:
    """GET /api/update/check — the update capability contract for this install.

    ``state`` and ``progress`` are absent on purpose: they describe an apply/drain
    lifecycle that does not exist yet, and serving them as constants would
    advertise transitions a consumer could poll for forever.
    """
    await _do_update_check()
    cfg = KiroCrewConfig.load()
    return web.json_response(
        {
            **_update_info,
            "current_version": _display_local_version(),
            "auto_update": cfg.auto_update,
            # Surface the pin so the dashboard can say WHY an update is mandatory
            # rather than showing a bare button.
            "minimum_version_enforced": min_version(),
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


def _set_update_info(**fields: object) -> None:
    """Replace the cached result wholesale so no key survives from a prior run.

    Mutating selected keys would let a previous success leak into a later failure
    (a stale ``latest_version`` beside a fresh ``error_code``), which is exactly
    the class of half-truth this contract exists to prevent.
    """
    _update_info.clear()
    _update_info.update(
        {
            "supported": True,
            "managed_by": "",
            "mode": MODE_NONE,
            "can_download": False,
            "can_apply": False,
            "requires_restart": True,
            "channel": "",
            "latest_version": "",
            "changes": "",
            "check_status": CHECK_UNCHECKED,
            "update_available": None,
            "version_newer": False,
            "commits_ahead": 0,
            "commits_behind": 0,
            "error_code": None,
            "unavailable_reason": None,
            "remediation": None,
        }
    )
    _update_info.update(fields)


def _invalidate_update_check(channel: str) -> None:
    """Drop the cached verdict so a stale one cannot be read as current.

    Called when the thing the verdict was computed AGAINST changes — today only a
    channel switch. Resetting to no verdict rather than leaving the old result is
    what keeps the honesty pair intact if the follow-up check no-ops (another one
    already in flight): the panel then says "not checked yet" instead of
    presenting the previous channel's answer as this channel's.

    Bumping the generation is the other half, and the load-bearing one: a check
    ALREADY running against the previous channel's feed cannot be cancelled, so
    without a generation it finishes after this reset and re-pins its stale verdict
    plus the 12-hourly clock. :func:`_do_update_check` compares the generation on
    the way out and discards a superseded result.

    ``channel`` is PASSED IN, never read here. The caller is on the event loop and
    has just written and validated the value, so re-reading
    ``$KIROCREW_HOME/channel`` would be both redundant and a synchronous read on a
    data home the operator may have put on a network mount — the same stall that
    freezes the liveness heartbeat. It is carried through the reset rather than
    left blank so the status pushed between here and the check landing still names
    the channel the user just chose.
    """
    global _last_update_check, _check_generation
    _check_generation += 1
    _last_update_check = 0.0
    _set_update_info(channel=channel)


def _capability_fields(capability: UpdateCapability) -> dict[str, object]:
    """The capability half of a cache entry, shared by every check branch.

    Delegates rather than re-listing the fields: two hand-rolled copies of one
    shape drift the moment a field is added to the contract.
    """
    return capability.to_dict()


async def _do_update_check() -> None:
    """Refresh ``_update_info``: is a newer build available for THIS install?

    The install's capability — who owns its bytes, and whether this process can
    apply an update at all — comes from
    :func:`~kiro_crew.platform.update_capability.derive_capability`, the one
    derivation every update surface shares. Three answers follow from it:

    * **defers** — a desktop bundle or a container. This gateway is NOT the
      update surface, so it reports which surface is, as a DEFERRAL rather than a
      failure.
    * **git checkout** — fetch the remote, then report an update when the
      checkout can be FAST-FORWARDED (behind its upstream and not ahead of it) OR
      when the remote ``src/kiro_crew/__init__.py`` ``__version__`` outranks the
      imported one. This is also the only layout ``POST /api/update`` can act on.
    * **everything else** — the ``cli.sh`` managed venv, a cloud/EC2 source
      install. Compare against the release channel feed the installer pulled
      from.

    Every exit path writes the cache, failures included: a check that could not
    run records an ``error_code`` and leaves ``check_status`` at ``failed``, so no
    caller can mistake a non-answer for a verdict.
    """
    global _last_update_check, _check_in_flight

    if _check_in_flight:
        return
    _check_in_flight = True
    # Snapshot the generation: everything written below describes the channel as it
    # is RIGHT NOW, and a switch mid-flight makes that verdict describe a lane the
    # install no longer follows.
    generation = _check_generation

    # A no-I/O seed, so the except path below always has something to report even
    # when the derivation itself is what failed.
    capability = UpdateCapability(
        supported=True,
        managed_by="",
        mode=MODE_NONE,
        can_download=False,
        can_apply=False,
        requires_restart=True,
    )
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    try:
        # A policy-defined provider OWNS the update on this host, the check
        # included. Consulted before the built-in capability derivation for the
        # same reason the apply path consults it first (see api_update_apply):
        # a host whose policy routes updates through an external command must
        # not have its badge computed against the feed/git mechanism that
        # policy excluded — the badge would then advertise updates the Update
        # button (which honors the provider) can never deliver.
        provider = resolve_provider()
        if provider is not None:
            await _check_via_provider(provider)
        else:
            # Offloaded INSIDE the guard's try: the derivation shells out to git, so
            # it must not run on the event loop, and it must not run where a raise
            # would skip the finally — a leaked single-flight flag stops every future
            # check for the process's lifetime, which is a silently dead updater.
            capability = await asyncio.get_running_loop().run_in_executor(None, derive_capability)
            if capability.defers:
                reason = capability.unavailable_reason or ""
                _set_update_info(
                    **_capability_fields(capability),
                    check_status=CHECK_DEFERRED,
                )
                logger.debug(
                    "Update check deferred: %s", EXTERNALLY_MANAGED_MESSAGES.get(reason, "")
                )
            elif capability.managed_by == MANAGED_BY_GIT:
                await _check_git_checkout(proj, capability)
            else:
                await _check_release_feed(capability)
    except Exception:
        logger.debug("Update check failed", exc_info=True)
        _set_update_info(
            **_capability_fields(capability),
            check_status=CHECK_FAILED,
            error_code=ERR_UNKNOWN,
        )
    finally:
        # The guard stays HELD across the cleanup below, and the inner `finally`
        # is what releases it. Both halves are load-bearing:
        #
        # * Releasing it first (as this did) lets a status poll start and FINISH a
        #   fresh check while the awaited channel read is still in flight; this
        #   coroutine then resumes and overwrites that newer verdict with the reset.
        # * Not releasing it on the error path is worse: an exception here would
        #   leave `_check_in_flight` stuck True and the updater would silently stop
        #   checking for the life of the process — the one failure this whole
        #   contract exists to prevent.
        try:
            if generation != _check_generation:
                # A channel switch landed while this check was talking to the
                # PREVIOUS channel's feed. Discard the verdict and leave the clock
                # UNSTAMPED so the next poll re-checks the new lane immediately,
                # instead of pinning a stale answer for the full 12-hour interval.
                logger.debug("Discarding update check superseded by a channel switch")
                # Offloaded: this reads $KIROCREW_HOME/channel, and the data home can
                # be network-backed (NFS/SMB) where the read can stall long enough to
                # freeze the loop and the liveness heartbeat. Read rather than carried
                # in from the switch on purpose — the file is the authority on which
                # lane the install now follows, so a switch whose write failed cannot
                # leave the panel naming a channel that was never persisted.
                _set_update_info(channel=await asyncio.to_thread(_release_channel))
            else:
                # Stamped even on failure, so an offline host or a broken feed cannot
                # turn the 12-hourly background poll into a hot retry loop. The
                # dashboard's manual button calls this function directly and is never
                # rate-limited.
                _last_update_check = time.time()
        finally:
            _check_in_flight = False


async def _check_git_checkout(proj: str, capability: UpdateCapability) -> None:
    """Compare a git checkout in *proj* against its tracked remote."""
    base: dict[str, object] = _capability_fields(capability)

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
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_FETCH_FAILED)
        return
    if proc.returncode != 0:
        logger.warning(
            "git fetch failed (rc=%s): %s",
            proc.returncode,
            (fetch_err or b"").decode(errors="replace").strip(),
        )
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_FETCH_FAILED)
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
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
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
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return

    local_sha = local_out.decode(errors="replace").strip()
    remote_sha = remote_out.decode(errors="replace").strip()
    if not local_sha or not remote_sha:
        # No upstream (detached HEAD, untracked branch) or an unreadable HEAD.
        # There is nothing to compare against, which is a failed check and not
        # "you are on the latest version".
        logger.warning("Could not resolve HEAD and/or upstream in %s", proj)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return

    # How far the checkout is from its upstream, BOTH directions. This — not the
    # version string — is what "up to date" means for a git checkout: the
    # version is bumped only at a release, so a checkout hundreds of commits
    # behind reads as current for as long as the next bump takes.
    #
    # Both counts are needed because one of the apply paths is DESTRUCTIVE.
    # ``GatewayOrchestrator._auto_apply_update`` runs unattended under
    # ``auto_update`` and applies ``git fetch`` + ``git reset --hard``, so an
    # update offered on a branch carrying local commits discards them with no
    # prompt. ``behind > 0`` alone is not safe: it is true both for a checkout
    # that is purely behind AND for a DIVERGED one (ahead and behind at once),
    # and the second is precisely the case with commits to lose. Only a checkout
    # that is behind and NOT ahead can be fast-forwarded, so only that one is
    # offered an update.
    counts = await count_divergence(proj, "@{u}")
    if isinstance(counts, DivergenceUnreadable):
        # A check that could not count must not answer "you are on the latest
        # version" — the unattended auto-apply reads this verdict.
        logger.warning("Could not count commits against upstream in %s (%s)", proj, counts.reason)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return
    ahead, behind = counts.ahead, counts.behind
    # Fast-forwardable: behind, and carrying nothing of its own to lose.
    can_fast_forward = behind > 0 and ahead == 0

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
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return
    match = re.search(r'__version__\s*=\s*"(.+?)"', show_out.decode(errors="replace"))
    if not match:
        logger.warning("Could not read __version__ at %s", target_sha)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return
    remote_version = match.group(1)
    # Two independent reasons a checkout is out of date, either one sufficient:
    #   * it is behind its upstream — commits to pull;
    #   * the on-disk ``__version__`` outranks the one this process IMPORTED, so
    #     the pull already landed and only a restart is missing.
    version_newer = _is_newer(remote_version, _local_version)
    if version_newer is None and not can_fast_forward:
        # Nothing left to go on: the version comparison failed AND there is no
        # fast-forwardable distance. A check that could not answer must not
        # answer "you are on the latest version". A fast-forwardable checkout is
        # a verdict on its own, so an unparseable version does not discard it.
        logger.warning(
            "Cannot compare local version %s against remote %s",
            _local_version,
            remote_version,
        )
        _set_update_info(
            **base,
            latest_version=remote_version,
            check_status=CHECK_FAILED,
            error_code=ERR_VERSION_UNPARSEABLE,
        )
        return
    # The version signal answers one question only: the pull already landed and
    # this process is still running the code from before it. That state IS
    # ``local_sha == remote_sha``, so requiring it is not a restriction but the
    # signal's actual domain.
    #
    # Without that gate the term reaches a case it was never about. A checkout
    # that pulled a version bump and then committed on top is AHEAD, so its
    # upstream still reads newer than the version this process imported — and
    # the unattended ``_auto_apply_update`` would answer that by resetting hard
    # onto the upstream, dropping those commits. Its own preflight does not
    # catch it either: ``git diff HEAD origin/<branch> --quiet`` compares tree
    # CONTENT, so an ahead checkout whose commits changed anything reads as
    # "has new commits" and proceeds to the reset.
    restart_pending = bool(version_newer) and local_sha == remote_sha
    available = can_fast_forward or restart_pending

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
        update_available=available,
        changes=changes,
        latest_version=remote_version,
        version_newer=bool(version_newer),
        # The raw distance travels with the verdict so a consumer can tell the
        # diverged ``available: False`` from the current one and render "rebase
        # or merge" instead of "up to date".
        commits_ahead=ahead,
        commits_behind=behind,
        check_status=CHECK_SUCCEEDED,
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
            # Streamed to EOF with the cap enforced against RECEIVED bytes
            # rather than a Content-Length claim; the extra byte lets the
            # caller detect an over-cap body (mirroring the installer's
            # --max-filesize on this very document). A single read(n) would
            # resolve on the first buffered chunk of a chunked feed and hand
            # back a truncated document.
            return resp.status, await read_capped_response(resp, _FEED_MAX_BYTES)


async def _check_via_provider(provider: CommandProvider) -> None:
    """Populate the cache from a policy-pinned command provider.

    A command-managed install has no release channel: the operator's commands
    decide what an update is and where it comes from, so ``channel`` is left to
    ``_set_update_info``'s empty reset and the panel hides the channel switcher
    instead of offering lanes the provider never reads. ``can_apply`` mirrors
    :meth:`CommandProvider.can_apply` — an ``apply_command`` exists AND can run
    on this platform — so the Update button is only offered when POST
    /api/update (which honors the provider first) can actually act. A True
    here does NOT imply a git checkout; git-reset callers gate on
    :func:`resolve_provider` themselves.

    The provider's error verdict maps to a FAILED check, never a raise: this
    runs inside :func:`_do_update_check`'s single-flight guard, and the honesty
    contract wants "the check failed" on the panel, not a silent dead badge.
    """
    fields: dict[str, object] = {
        "supported": True,
        "managed_by": MANAGED_BY_COMMAND,
        "mode": MODE_NOTIFY,
        "can_download": False,
        "can_apply": provider.can_apply(),
        "requires_restart": True,
    }
    result = await provider.check()
    if result.error:
        logger.debug("Policy-defined update check failed: %s", result.error)
        _set_update_info(**fields, check_status=CHECK_FAILED, error_code=ERR_UNKNOWN)
        return
    _set_update_info(
        **fields,
        update_available=result.available,
        latest_version=result.remote_version,
        version_newer=result.available,
        check_status=CHECK_SUCCEEDED,
    )


async def _check_release_feed(capability: UpdateCapability) -> None:
    """Compare a feed-checkable install against the release channel it came from.

    Reached for every shape whose bytes this gateway could in principle replace
    but whose ``managed_by`` is not ``git`` — the ``cli.sh`` managed venv, and an
    unstamped pre-``_build_info`` wheel that reports the ``source`` default.
    Matching by exclusion rather than an ``== "wheel"`` allowlist is deliberate:
    an allowlist would skip every already-released CLI install.

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
    feed_base, _artifact_base = _cdn_bases()
    # `for_channel` keeps the pair honest: the channel reported here and the command
    # offered alongside it are read at different moments, and a switch landing in
    # between would otherwise publish one lane's name next to a command that moves
    # the install to the other.
    base: dict[str, object] = {
        **_capability_fields(capability.for_channel(channel)),
        "channel": channel,
    }
    url = f"{feed_base}/feed/{channel}/latest-cli.json"

    try:
        status, raw = await _fetch_feed_bytes(url)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        logger.debug("Release feed fetch failed: %s", url, exc_info=True)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_FEED_UNREACHABLE)
        return
    if status != 200:
        logger.warning("Release feed %s returned HTTP %s", url, status)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_FEED_UNREACHABLE)
        return

    if len(raw) > _FEED_MAX_BYTES:
        logger.warning("Release feed %s exceeded %d bytes", url, _FEED_MAX_BYTES)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_FEED_MALFORMED)
        return
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("Release feed %s is not valid JSON", url)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_FEED_MALFORMED)
        return
    if not isinstance(manifest, dict):
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_FEED_MALFORMED)
        return
    # The channel assertion is what stops a mis-wired or swapped feed from
    # advertising another lane's build to this install.
    if manifest.get("schema") != _CLI_MANIFEST_SCHEMA or manifest.get("channel") != channel:
        logger.warning("Release feed %s is not a %s for %s", url, _CLI_MANIFEST_SCHEMA, channel)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_FEED_MALFORMED)
        return
    remote_version = manifest.get("version")
    if not isinstance(remote_version, str) or not _VERSION_RE.match(remote_version):
        logger.warning("Release feed %s carries no usable version", url)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_FEED_MALFORMED)
        return

    available = _is_newer(remote_version, _local_version)
    if available is None:
        logger.warning(
            "Cannot compare local version %s against feed version %s",
            _local_version,
            remote_version,
        )
        _set_update_info(
            **base,
            latest_version=remote_version,
            check_status=CHECK_FAILED,
            error_code=ERR_VERSION_UNPARSEABLE,
        )
        return

    extra: dict[str, object] = {}
    pub_date = manifest.get("pub_date")
    if isinstance(pub_date, str) and _PUB_DATE_RE.match(pub_date):
        extra["latest_pub_date"] = pub_date
    # No ``changes``: the manifest carries no changelog, and the CHANGELOG.md
    # bundled into the wheel describes the version already INSTALLED, not the new
    # one. The dashboard's "view full changelog" disclosure covers the gap
    # rather than this function inventing a diff it cannot see.
    _set_update_info(
        **base,
        update_available=available,
        latest_version=remote_version,
        # For a feed-checkable install the verdict IS the version comparison, so
        # the two agree by construction. Reported anyway so the field means the
        # same thing on every layout and no consumer has to special-case one.
        version_newer=available,
        check_status=CHECK_SUCCEEDED,
        **extra,
    )


async def api_update_auto(request: web.Request) -> web.Response:
    """POST /api/update/auto — toggle auto-update on/off."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    enabled = body.get("enabled", True)

    def _set_auto_update(data: dict) -> dict:
        data["auto_update"] = enabled
        # RETURN it: `update_config_locked` reads a `None` return as "do not write" and
        # exits with the data untouched, so an in-place-only callback silently drops the
        # write while still reporting success.
        return data

    # `update_config_locked` holds the advisory lock across the READ and the write, so no
    # other process can land between them -- the whole point, since the in-process
    # `_get_config_lock()` this endpoint used to rely on does not serialize against the CLI
    # or a second gateway.
    #
    # Offloaded because that lock is blocking: called inline from this coroutine it would
    # stall every session and the liveness heartbeat while contended, which is what the
    # repo's `no-blocking-call-on-event-loop` rule forbids.
    #
    # The read still fails CLOSED (`on_corrupt` defaults to "fail"): treating an unreadable
    # config as {} would write back a single-key file and wipe every other setting the user
    # has (see read_config_for_update).
    try:
        await asyncio.to_thread(update_config_locked, config_path(), mutate=_set_auto_update)
    except ConfigReadError:
        logger.exception("Refusing to toggle auto-update: config is unreadable")
        return web.json_response(
            {"error": "failed to read config file", "code": "config_unreadable"}, status=500
        )
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


async def api_releases(request: web.Request) -> web.Response:
    """GET /api/releases — CHANGELOG.md as per-version entries for the archive.

    The list is the changelog's sections plus the release the running build
    belongs to; :mod:`kiro_crew.changelog` owns that rule and the reasoning.

    ``stale`` is the caveat the page has to state out loud: the changelog is
    read from this install (project tree or the copy bundled into the wheel),
    never from the network, so a prerelease build shows the archive as it stood
    when its release branch was cut. A section added to ``main`` afterwards is
    invisible here, which is a real gap for an insider build that can be weeks
    old -- it is reported rather than hidden.

    The read and the parse are offloaded: ``_read_changelog`` stats the file on
    every call and re-reads it whenever it changed, and the parse is linear in
    the file's size. Both are small today (~10 KB), but the changelog only ever
    grows, and this handler is the one place that adds parsing on top of the
    read -- so it runs in a thread rather than betting the gateway's whole loop
    on the file staying small.
    """

    def _read_and_parse() -> list[Release]:
        return build_release_list(_read_changelog(), _display_local_version())

    releases = await asyncio.to_thread(_read_and_parse)
    return web.json_response(
        {
            "current_version": _display_local_version(),
            "releases": [r._asdict() for r in releases],
            "stale": any(r.in_progress for r in releases),
        }
    )


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
    """Install the pulled revision into this gateway's own venv.

    Returns ``True`` on success. On failure, pushes an error to ``state`` and
    returns ``False`` — caller should ``return``.

    Delegates the choice of HOW to :func:`kiro_crew.dep_sync.sync_or_reinstall`:
    an editable reinstall where it can run, and a dependency-only sync where it
    cannot. This endpoint is one of the paths that cannot always run it — the
    gateway it updates is normally started through the very console script pip
    would have to rewrite, which Windows locks, and a reinstall that dies there
    has already deleted the editable ``.pth`` and left the venv unable to import
    the package at all.

    ``dep_sync`` is imported at MODULE level (see its docstring): this runs after
    the pull, so an import deferred to here would parse the file the incoming
    revision shipped.
    """
    state.push_update_progress("building", "Installing package (pip)…")
    loop = asyncio.get_running_loop()

    def _publish(step: str, message: str) -> None:
        # dep_sync hands pip's output over raw; this is the surface that publishes
        # it, so redaction and the length cap belong here.
        message, _ = redact_credentials(message)
        message, _ = redact_exfiltration_urls(message)
        if len(message) > 1000:
            message = message[:1000] + "\n…(truncated)"
        state.push_update_progress(step, message)

    def _emit(message: str, error: bool) -> None:
        # Called from the EXECUTOR THREAD, and push_update_progress touches
        # loop-bound state (the SSE subscriber queues), so it is handed to the
        # serving loop rather than run here. Doing it inline is the kind of
        # cross-thread mutation that works until a client is actually connected
        # and then raises out of a worker thread. Same pattern as the log ring
        # handler below, which emits from arbitrary logger threads.
        loop.call_soon_threadsafe(_publish, "error" if error else "building", message)

    rc = await loop.run_in_executor(
        subprocess_executor(),
        functools.partial(
            dep_sync.sync_or_reinstall,
            Path(proj),
            Path(sys.executable),
            _emit,
            # 600s, not the 120s this endpoint used to put on `pip install -e .`.
            # The bound now covers a dependency install that may be resolving and
            # downloading a set this venv has never seen — and building a wheel for
            # one of them — where 120s is a routine, not an exceptional, overrun.
            # It is a real bound either way: dep_sync kills the pip child on
            # expiry rather than letting the step hang on a wedged index.
            timeout=600,
        ),
    )
    return rc == 0


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
    reexec_python_module("kiro_crew", sys.argv[1:])


async def api_update_apply(request: web.Request) -> web.Response:
    """POST /api/update — git pull, rebuild, restart gateway."""
    state: DashboardState = request.app["state"]

    # A policy-defined provider OWNS the update on this host. Checked before the
    # git precondition below so an authenticated operator clicking Update cannot
    # run the built-in mechanism their own policy excluded. A dashboard token
    # proves who the caller is, not that this host may update by git.
    from kiro_crew.platform.update_provider import apply_policy_update

    applied = await apply_policy_update()
    if applied is not None:
        if not applied:
            # A configured provider's failure is a failure. Falling through to
            # the git path would be the bypass the policy exists to prevent.
            state.push_update_progress("failed", "Policy-defined update command failed — see logs")
            return web.json_response(
                {
                    "error": "policy-defined update command failed",
                    "code": "policy_update_failed",
                    "governance": True,
                },
                status=500,
            )
        await _restart_gateway(state)
        return web.json_response({"ok": True, "status": "updating"})

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        return web.json_response({"error": "KIROCREW_PROJECT_DIR not set"}, status=400)
    # Same derivation the check path uses, so this endpoint and the button the
    # dashboard renders from ``can_apply`` cannot disagree. Offloaded: it shells
    # out to git.
    capability = await asyncio.get_running_loop().run_in_executor(
        None, lambda: derive_capability(install_root=proj)
    )
    if capability.managed_by != MANAGED_BY_GIT:
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

    # Diverged precondition, enforced at the layer that owns the destructive
    # action. The dashboard's own render-site guards can only cover the clients
    # that ran a fresh check; a stale client (an Update button armed before the
    # checkout gained local commits, a cached-changelog modal whose pre-apply
    # check never re-ran) still POSTs here. Fetch FIRST, fail closed: the
    # counts below read the remote-tracking refs, and refs from an old fetch
    # can report behind=0 for a checkout whose remote has since moved — which
    # would wave through the very state this guard exists to refuse.
    fetch = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        "--quiet",
        cwd=proj,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(fetch.communicate(), timeout=30)
    except asyncio.TimeoutError:
        try:
            fetch.kill()
        except ProcessLookupError:
            pass
        await fetch.communicate()
        return web.json_response(
            {
                "error": "Timed out refreshing the remote before updating",
                "code": "git_fetch_failed",
            },
            status=500,
        )
    if fetch.returncode != 0:
        logger.warning("Update refused: pre-apply git fetch failed (rc=%s)", fetch.returncode)
        return web.json_response(
            {
                "error": "Could not reach the remote to verify the update",
                "code": "git_fetch_failed",
            },
            status=409,
        )
    counts = await count_divergence(proj, "@{u}")
    if isinstance(counts, DivergenceUnreadable):
        if counts.reason == UNREADABLE_TIMEOUT:
            return web.json_response(
                {"error": "Timed out checking upstream distance", "code": "git_read_failed"},
                status=500,
            )
        # A failed or unparseable count alike: the guard cannot read the
        # distance, so it refuses rather than waving the pull through.
        logger.warning("Update refused: could not count commits against upstream in %s", proj)
        return web.json_response(
            {
                "error": "Could not compare against upstream — check the tracked remote",
                "code": "git_read_failed",
            },
            status=409,
        )
    ahead, behind = counts.ahead, counts.behind
    if ahead > 0 and behind > 0:
        logger.warning(
            "Update refused: checkout diverged from upstream (%d ahead, %d behind)", ahead, behind
        )
        return web.json_response(
            {
                "error": "Checkout has diverged from its upstream — rebase or merge in a terminal",
                "code": "checkout_diverged",
                "commits_ahead": ahead,
                "commits_behind": behind,
            },
            status=409,
        )

    async def _apply() -> None:
        try:
            state.push_update_progress("pulling", "Pulling latest changes…")
            # --ff-only makes the non-fast-forward classes unreachable at the
            # action primitive itself, not just at the precondition above: a
            # remote that moves in the window between the guard's fetch and
            # this pull fails the pull instead of minting an unrequested merge
            # commit into the user's branch.
            pull = await asyncio.create_subprocess_exec(
                "git",
                "pull",
                "--ff-only",
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
    passing the broadcast chokepoint. Re-check here, through
    ``DashboardState._ws_client_allowed`` itself (rather than a hand-rolled
    scope comparison) so revoking an app's ``log`` scope (a narrowed manifest,
    or ``app disable``) stops the stream on a socket that is already
    subscribed, and so this decision gets the same SEL audit trail as every
    other event instead of a silent, unaudited duplicate of the check.

    This runs on the event loop (the caller hands it to ``create_task`` via
    ``call_soon_threadsafe``), which is what makes the check safe: the scope
    cache must never be consulted from the logging thread, where a cold miss
    would fall back to a synchronous manifest read.
    """
    try:
        if not ws.get("_is_dashboard_user", False):
            if not state._ws_client_allowed(ws, "log", {}):
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

    def set_state(self, state: DashboardState) -> None:
        """Attach DashboardState for WS log broadcasting."""
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            data = json.dumps({"level": record.levelname, "msg": msg})
            self._ring.append(data)
            # Push to WS log subscribers. emit() runs on ARBITRARY threads (any
            # logger call anywhere), so the send is handed to the dashboard's one
            # serving loop rather than to a copy latched by this handler.
            loop = self._state.serving_loop if self._state else None
            if self._state and loop and self._state._ws_log_subscribers:
                ws_msg = json.dumps(
                    {"type": "log", "data": {"level": record.levelname, "msg": msg}}
                )
                for ws in list(self._state._ws_log_subscribers):
                    try:
                        loop.call_soon_threadsafe(
                            loop.create_task,
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
                    # The SSE stream is the THIRD status emitter, and it was reading
                    # the cache directly on a key this contract renamed — so it
                    # published `False` unconditionally, and flattened the tri-state
                    # while doing it (a check that never ran is not "no update").
                    # `status_update_fields()` is the one reader; /api/status and the
                    # WebSocket push already go through it.
                    **state.status_snapshot(**status_update_fields()),  # type: ignore[arg-type]
                    "version": _display_local_version(),
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


async def api_update_channel(request: web.Request) -> web.Response:
    """POST /api/update/channel — move this install onto another release channel.

    Only meaningful for a feed-checkable install (a ``cli.sh`` wheel, a cloud
    source install). A git checkout tracks a git remote and a desktop bundle or
    container is updated by something else entirely, so those layouts are
    REFUSED rather than silently writing a file nothing reads — a switcher that
    appears to work and changes nothing is worse than no switcher.

    Switching does not install anything. It changes which feed the next check
    compares against, and which ``--channel`` the recommended installer command
    spells; the user still runs that command (or clicks Update on a
    self-updatable layout). That keeps a channel change from ever being an
    unattended, unconsented version jump.

    **Not a governance bypass.** ``UpdatePins`` constrains the git ``source`` and
    a ``min_version`` floor; it has no release-channel key, so there is no pin
    for this to escape. When the pins define a command provider the endpoint
    refuses outright — the provider's commands never read the channel file, so
    a "successful" switch would be the lying switcher described above. Nor does
    the endpoint grant a new capability: the channel
    file is an ordinary file in the data home that the operator can already
    write. What it adds is an authenticated, allowlist-validated path to the same
    write. Introducing an enterprise channel pin is a governance change in its
    own right — the profile parser fails closed on unknown keys, so a new key has
    to be rolled out before it can be set.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    # A well-formed JSON ARRAY or scalar parses fine and then has no ``.get``, so
    # the type check is separate from the parse guard above: without it, a body of
    # ``[]`` from an authenticated caller raises AttributeError and answers 500
    # where the honest answer is 400.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )

    requested = body.get("channel")
    if not isinstance(requested, str):
        return web.json_response(
            {"error": "channel must be a string", "code": "invalid_channel"}, status=400
        )

    # A policy-defined provider OWNS updates on this host, and its commands do
    # not read the channel file — the switch would "succeed" and change nothing,
    # which is exactly the lying switcher the layout refusals below exist to
    # prevent. Checked before the layout probe because it needs no git shell-out.
    if resolve_provider() is not None:
        return web.json_response(
            {
                "error": "Updates on this install are managed by policy, not a release channel.",
                "code": "channel_not_applicable_command_managed",
            },
            status=409,
        )

    # Offloaded: the layout derivation shells out to git, and this handler runs on
    # the event loop — a synchronous probe here freezes every gateway task and the
    # liveness heartbeat for as long as git takes to answer.
    layout = await asyncio.to_thread(detect_install_layout)
    if layout.is_git:
        return web.json_response(
            {
                "error": "A git checkout follows its git remote, not a release channel.",
                "code": "channel_not_applicable_git",
            },
            status=409,
        )
    if layout.is_externally_managed:
        return web.json_response(
            {"error": layout.guidance, "code": "channel_not_applicable_managed"},
            status=409,
        )

    try:
        # Offloaded: mkdir + write + os.replace are synchronous syscalls, and the
        # data home can be network-backed (NFS/SMB), where even a ten-byte write
        # can stall long enough to freeze the loop and the liveness heartbeat with
        # it. Small does not mean non-blocking.
        stored = await asyncio.to_thread(set_release_channel, requested)
    except ValueError:
        # The allowlist is the whole guard: `channel` becomes a path segment in
        # every feed URL and an argument in a shell command, so an unknown value
        # is rejected, never coerced.
        return web.json_response(
            {"error": "unknown release channel", "code": "invalid_channel"}, status=400
        )
    except OSError:
        logger.exception("Failed to persist release channel")
        return web.json_response(
            {"error": "failed to write channel file", "code": "channel_write_failed"}, status=500
        )

    # Re-check immediately against the NEW feed. Without this the panel would
    # keep showing the previous channel's verdict until the next 12-hourly poll,
    # which reads as the switch having done nothing. `stored` is what the write
    # above just validated and persisted, so the invalidation needs no read of
    # its own.
    _invalidate_update_check(stored)
    await _do_update_check()
    # Same reason as the write above: a config read is disk I/O on a path the
    # operator may have put on a network mount.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    _, artifact_base = _cdn_bases()
    return web.json_response(
        {
            "ok": True,
            **_update_info,
            "auto_update": cfg.auto_update,
            # AFTER the spread, deliberately. When a check was already in flight
            # `_do_update_check` returns early and the cache still holds the
            # invalidated ``channel: ""`` / ``update_command: ""``; letting those
            # win would blank the switcher right after a successful switch, and --
            # worse -- leave the client falling back to the PREVIOUS channel's
            # command, so copy-pasting it would move the install straight back.
            #
            # Neither value needs the check: both are pure functions of the channel
            # now on disk, so they are composed locally from the already-validated
            # name (same helper, same https pin as the check's own path).
            "channel": stored,
            "update_command": wheel_update_command(stored),
        }
    )


async def api_gateway_restart(request: web.Request) -> web.Response:
    """POST /api/restart — restart the gateway process without updating anything.

    The missing half of the non-desktop update flow. A wheel install cannot
    replace its own code, so the panel hands the user an installer command to
    run in a terminal; once they have, the gateway is still executing the OLD
    code with no in-app way to pick up the new one. Short of this endpoint the
    only route was killing the process by hand.

    Deliberately NOT part of ``POST /api/update``: that endpoint pulls, rebuilds
    and reinstalls before restarting, and it refuses every layout that is not a
    git checkout. Restart has no such precondition — it is valid on every
    layout, including a desktop bundle's embedded gateway.
    """
    state: DashboardState = request.app["state"]

    # Reply BEFORE restarting. os.execv replaces the process image, so a restart
    # kicked off inline would tear down the connection mid-response and the
    # client could not distinguish "restarting" from "the request failed".
    async def _restart() -> None:
        # Let the response flush before the process image is replaced.
        await asyncio.sleep(0.25)
        try:
            await _restart_gateway(state)
        except Exception:
            logger.exception("Gateway restart failed")
            state.push_update_progress("failed", "Restart failed — check logs")

    task = asyncio.create_task(_restart())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True, "status": "restarting"})
