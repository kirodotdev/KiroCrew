"""Running the update check, and the status fields that report it.

One finite check task is shared by concurrent manual checks and the automatic
coordinator; it reads a git checkout's upstream, a configured command provider or
the release feed, and records the verdict. The check's clock, generation and task
live here with every function that reads or rebinds them.

Served through :mod:`kiro_crew.dashboard.handlers.updates`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import aiohttp
from aiohttp import web

from kiro_crew import __version__ as _local_version
from kiro_crew import platform_compat
from kiro_crew.changelog import release_of_build
from kiro_crew.config.loader import KiroCrewConfig, overlay_pins
from kiro_crew.dashboard.handlers._update_feed import fetch_feed_bytes
from kiro_crew.dashboard.handlers.updates_verdict import (
    _capability_fields,
    _channel_move_pending,
    _display_local_version,
    _display_version,
    _effective_min_version,
    _effective_update_required,
    _is_newer,
    _refresh_auto_update_effect_once,
    _set_update_info,
    _status_auto_update_effect,
    _update_info,
    remediation_command,
)
from kiro_crew.git_divergence import DivergenceUnreadable, count_divergence
from kiro_crew.platform import feed_trust
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
    bundled_by_desktop_app,
    derive_capability,
)
from kiro_crew.platform.update_fetch import fetch_and_pin_upstream
from kiro_crew.platform.update_governance import (
    git_command_env,
    min_version,
    repo_exec_config_reason,
    upstream_remote_and_branch,
)
from kiro_crew.platform.update_layout import cdn_bases as _cdn_bases
from kiro_crew.platform.update_layout import release_channel as _release_channel
from kiro_crew.platform.update_provider import CommandProvider, resolve_provider

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")


#: Bumped whenever the thing a check is computed AGAINST changes (today: a channel
#: switch). A check already talking to the OLD channel's feed cannot be cancelled,
#: so without this it finishes afterwards and re-pins its stale verdict plus the
#: 12-hourly clock — pinning a stale answer for half a day to a channel the
#: install has left.
_check_generation = 0

_UPDATE_CHECK_INTERVAL = 43200  # 12 hours


_last_update_check: float = 0.0

#: The finite operation shared by concurrent manual checks and the automatic
#: coordinator, so every caller consumes one completed verdict.
_check_task: asyncio.Task[None] | None = None
_check_task_generation: int | None = None

#: ``schema`` every CLI artifact manifest carries. A payload without it is not a
#: manifest and must not be read as one.
_CLI_MANIFEST_SCHEMA = "kirocrew-cli-artifact-manifest-v1"

#: Same ceiling ``cli.sh`` applies with ``curl --max-filesize`` when it fetches
#: the very same document. Enforced against RECEIVED bytes, not Content-Length.
_FEED_MAX_BYTES = 65536
_FEED_TIMEOUT_SECS = 15

_VERSION_RE = re.compile(r"^[A-Za-z0-9._+!-]{1,64}$")
_PUB_DATE_RE = re.compile(r"^[0-9TZ:.\-]{1,32}$")
#: The feed's forced-update floor must be a bare release (``0.6.0``) — the
#: publisher enforces the same shape, so anything else here is tampering or
#: hand-editing and is dropped rather than compared.
_MIN_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


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
        # The candidate release's version string, so the proactive update popup
        # can key its per-version snooze/skip without calling the check
        # endpoint (which runs a full check per request). Empty until a check
        # has found a newer build. The changelog text stays OFF this hot-path
        # subset — consumers fetch it on demand.
        #
        # RAW, never folded: this is the same string ``InAppUpdateFlow``,
        # ``api_update_arm``, and the snooze/skip persisted-record key key on —
        # the shadow-venv apply step compares it byte-for-byte against the
        # installed ``kiro_crew.__version__``, which is never folded either
        # (promotion never re-stamps the bytes). For a display-only clean
        # version, use ``update_latest_version_display`` below (or the
        # ``/api/update/check`` endpoint's ``latest_version_display``).
        "update_latest_version": str(_update_info.get("latest_version") or ""),
        # DISPLAY-ONLY fold of the candidate above (clean base on the stable
        # channel), consumed by the proactive update popup's "vX is available"
        # text. The popup's per-version snooze/skip records and every arm
        # path keep keying on the RAW `update_latest_version`.
        "update_latest_version_display": _display_version(
            str(_update_info.get("latest_version") or ""),
            str(_update_info.get("channel") or ""),
        ),
        "update_channel": str(_update_info.get("channel") or ""),
        # Is the running build ahead of everything the FOLLOWED channel
        # publishes? The panel keys its "you must re-run the installer to move
        # onto this lane" copy on this rather than on a comparison of
        # ``update_channel`` against the version-derived ``release_channel``:
        # promotion never re-stamps, so every promoted-stable install looks
        # insider-stamped and read as permanently mid-switch. See
        # ``_channel_move_pending``.
        "update_channel_move_pending": _channel_move_pending(),
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
        "update_last_checked_at": _last_update_check or None,
        "update_check_interval_secs": _UPDATE_CHECK_INTERVAL,
        # Mandatory-update verdict (governance pin OR feed floor) plus the floor
        # that triggered it, on the hot path because the proactive update popup
        # reads the status frame, not the check endpoint — a forced prompt must
        # not depend on the user opening Settings first. Both reads are
        # in-memory (boot-frozen governance + the cached check result).
        "update_required": _effective_update_required(),
        "update_min_version": _effective_min_version(),
        # Whether the in-app arm+approve path applies to this install. The SPA
        # gates its Update button on this, never on managed_by alone — that
        # value also covers bare source installs whose arm would 409.
        "update_can_arm": bool(_update_info.get("can_arm")),
        # What an available update leads to here: ``install`` (with the
        # auto-update switch on), ``notify`` (the switch cannot install on this
        # install), ``mandatory`` (a policy floor installs it regardless), or
        # ``unknown`` before the first derivation. The same derivation the
        # gateway's update loop acts on, so the switch's label cannot promise
        # what the loop will not do.
        "update_auto_effect": _status_auto_update_effect(),
        # Whether the desktop app bundles and launches this gateway, from the
        # baked packaging stamp. Independent of who owns the update: a policy
        # ``updates`` provider makes ``update_managed_by`` read ``command`` on
        # that same bundle. ``update_auto_effect`` still reports what THIS
        # gateway's loop does there — the provider's answer — because the
        # provider, not the app's updater, is what the loop runs.
        "update_bundled_by_app": bundled_by_desktop_app(),
        # The RUNNING build's version folded for display (clean base on the
        # stable channel), so the About page's version chip can show `0.4.0`
        # instead of the promoted candidate's baked-in `0.4.0rc14` stamp.
        # DISPLAY-ONLY sibling of the raw `version` the WS frame and
        # /api/status carry: that one is functional — the SPA compares it
        # across pushes to force a reload over a gateway upgrade, and folding
        # it would collapse e.g. `0.4.1rc1` -> `0.4.1` and `0.4.1rc2` ->
        # `0.4.1`, masking the very upgrade the reload detection exists to
        # catch. Falls back to the raw version until a check has resolved the
        # channel (`_display_local_version` keys on it; only stable folds).
        "version_display": _display_local_version(),
    }


async def api_update_check(request: web.Request) -> web.Response:
    """GET /api/update/check — the update capability contract for this install.

    ``state`` and ``progress`` are absent on purpose: they describe an apply/drain
    lifecycle that does not exist yet, and serving them as constants would
    advertise transitions a consumer could poll for forever.
    """
    # The manual Check is the other way the effect becomes known before the
    # update loop's first cycle: the user asked, so the derivation's git probes
    # are warranted here where a status frame could not afford them. Alongside
    # the check (the two are independent), and through the shared single-flight
    # task, so N tabs opening About at once derive once.
    await asyncio.gather(_do_update_check(), _refresh_auto_update_effect_once())
    # Offloaded for the same reason as every other config read in the update
    # handlers: the data home can be network-backed.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    overlay_override = await asyncio.to_thread(overlay_pins, "auto_update")
    return web.json_response(
        {
            **_update_info,
            "current_version": _display_local_version(),
            "update_auto_effect": _status_auto_update_effect(),
            # DISPLAY-ONLY sibling of the raw `latest_version` above (unpacked
            # via `**_update_info`) — folds a promoted stable candidate's
            # insider/rc stamp to the clean release it means. `latest_version`
            # itself MUST stay raw: it is what `InAppUpdateFlow` and
            # `api_update_arm` arm against, compared byte-for-byte against the
            # installed build's own never-folded `__version__`.
            "latest_version_display": _display_version(
                str(_update_info.get("latest_version") or ""),
                str(_update_info.get("channel") or ""),
            ),
            "auto_update": cfg.auto_update,
            # Whether config.local.json pins it, so the panel can explain a
            # switch that snaps back: this endpoint's writer updates the base
            # file, which the overlay deep-merges over.
            "overlay_override": overlay_override,
            # Surface the pin so the dashboard can say WHY an update is mandatory
            # rather than showing a bare button. ``minimum_version_enforced``
            # stays governance-only (its historical meaning); the combined
            # verdict and the feed floor ride ``update_required`` /
            # ``feed_min_version`` (the latter via ``_update_info`` above).
            "minimum_version_enforced": min_version(),
            "update_required": _effective_update_required(),
            "update_min_version": _effective_min_version(),
        }
    )


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


async def _can_arm_this_install() -> bool:
    """Whether the in-app arm+approve path applies here. Best-effort.

    Probed rather than derived from ``managed_by``: that value also covers bare
    source installs the arm endpoint refuses, so the wider signal would render a
    dead button. Offloaded — it resolves venv paths on disk — and a failure
    answers False, since this runs on the paths that are already reporting one.
    """
    from kiro_crew.platform.wheel_engine import running_from_managed_venv

    try:
        return await asyncio.to_thread(running_from_managed_venv)
    except Exception:
        logger.debug("managed-venv probe failed; reporting can_arm False", exc_info=True)
        return False


def _release_update_check_task(task: asyncio.Task[None]) -> None:
    """Drop ownership even when every caller stopped waiting for the worker."""
    global _check_task, _check_task_generation

    if _check_task is task:
        _check_task = None
        _check_task_generation = None


async def _do_update_check() -> None:
    """Run one shared update check and wait for a current-channel verdict."""
    global _check_task, _check_task_generation

    while True:
        generation = _check_generation
        running = _check_task
        running_generation = _check_task_generation
        if running is None:
            running = asyncio.create_task(_run_update_check())
            running_generation = generation
            _check_task = running
            _check_task_generation = generation
            running.add_done_callback(_release_update_check_task)

        try:
            # No request owns the worker. A disconnected HTTP caller cannot cancel
            # the check under the recurring coordinator or another waiter.
            await asyncio.shield(running)
        finally:
            if _check_task is running and running.done():
                _check_task = None
                _check_task_generation = None
        if running_generation != _check_generation:
            # A channel switch needs a fresh task before any caller returns an
            # unchecked current-channel cache.
            continue
        return


async def _cancel_update_check() -> None:
    """Cancel and reap the shared worker during gateway shutdown only."""
    global _check_task, _check_task_generation

    task = _check_task
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    if _check_task is task:
        _check_task = None
        _check_task_generation = None


async def _run_update_check() -> None:
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
    global _last_update_check

    # Snapshot the generation: everything written below describes the channel as it
    # is RIGHT NOW, and a switch mid-flight makes that verdict describe a lane the
    # install has left.
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
    # Seeded beside the capability above and for the same reason: the except
    # path reads it, and the resolution itself can be what failed.
    provider: object | None = None
    try:
        # A policy-defined provider OWNS the update on this host, the check
        # included. Consulted before the built-in capability derivation for the
        # same reason the apply path consults it first (see api_update_apply):
        # a host whose policy routes updates through an external command must
        # not have its badge computed against the feed/git mechanism that
        # policy excluded — the badge would then advertise updates the Update
        # button (which honors the provider) can never deliver.
        provider = await asyncio.to_thread(resolve_provider)
        if provider is not None:
            await _check_via_provider(provider)
        else:
            # Offloaded inside the worker's try: derivation shells out to git, so
            # it must not run on the event loop, and every failure must still reach
            # the cache/error cleanup below.
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
        # ``can_arm`` rides along for the same reason the feed-failure paths
        # carry it: ``_set_update_info`` re-seeds it to False, so an unexpected
        # raise (a signature probe, a venv-layout read) would otherwise make a
        # managed venv look notify-only until the next successful check.
        # ``can_arm`` only where the arm endpoint could ever say yes. A policy
        # provider owns the update and that endpoint refuses outright, so
        # probing the venv after the PROVIDER branch raised would offer a
        # button whose answer is 409.
        _set_update_info(
            **_capability_fields(capability),
            can_arm=(provider is None and await _can_arm_this_install()),
            check_status=CHECK_FAILED,
            error_code=ERR_UNKNOWN,
        )
    finally:
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


async def _check_git_checkout(proj: str, capability: UpdateCapability) -> None:
    """Compare a git checkout in *proj* against its tracked remote.

    *proj* is a tree an agent can write, and this runs on the host at boot, on
    the 12-hourly poll and on the dashboard's Check button. So every git call
    here goes through the same guards as the unattended apply: ``git`` resolved
    off ``PATH``, :func:`git_command_env` pinning every fixed-key exec vector
    (``core.sshCommand``, ``core.fsmonitor``, ``core.hooksPath``,
    ``diff.external`` ...), and a :func:`repo_exec_config_reason` refusal for
    the repo-NAMED ones (``diff.<driver>.textconv``, ``filter.<name>.*``) that
    no pin can reach. The changelog diff also runs ``--no-textconv
    --no-ext-diff``, so no attribute can route it to a program.
    """
    base: dict[str, object] = _capability_fields(capability)

    git = platform_compat.trusted_git_bin()
    if git is None:
        logger.warning("Update check skipped: no trustworthy git outside PATH")
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return
    exec_config = await asyncio.to_thread(repo_exec_config_reason, proj)
    if exec_config:
        logger.warning(
            "Update check skipped: %s, which git would run during the check", exec_config
        )
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return
    # Carries the shared guards (trusted PATH, built-in transports only, no git
    # proxy, every fixed-key exec vector pinned, location vars stripped).
    env = git_command_env()

    # The tracked upstream to compare against, read from config (not the
    # `origin/<name>` short form). None for a detached HEAD or a branch with no
    # upstream: nothing to compare against, a failed check -- exactly what the
    # old bare `git fetch` + `rev-parse @{u}` reported.
    upstream = await asyncio.to_thread(upstream_remote_and_branch, proj)
    if upstream is None:
        logger.warning("Could not resolve the tracked upstream in %s", proj)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return
    remote_name, remote_branch = upstream

    # The fetch runs through the shared isolated helper: a repo-defined hook
    # cannot fire on the fetch's ref write (it happens in a git dir the gateway
    # owns), while the fetched objects land in this checkout's object store. The
    # tip comes back as a commit id the reads below use directly; no ref in this
    # checkout is written. The same helper fetches for the unattended apply.
    remote_sha = await fetch_and_pin_upstream(
        proj, remote_name, remote_branch, git=git, env=env, timeout=30
    )
    if remote_sha is None:
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_FETCH_FAILED)
        return

    local = await asyncio.create_subprocess_exec(
        git,
        "rev-parse",
        "HEAD",
        cwd=proj,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        local_out, _ = await asyncio.wait_for(local.communicate(), timeout=10)
    except asyncio.CancelledError:
        await platform_compat.kill_and_reap(local)
        raise
    except asyncio.TimeoutError:
        await platform_compat.kill_and_reap(local)
        _set_update_info(**base, check_status=CHECK_FAILED, error_code=ERR_GIT_READ_FAILED)
        return

    local_sha = local_out.decode(errors="replace").strip()
    if not local_sha:
        logger.warning("Could not resolve HEAD in %s", proj)
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
    counts = await count_divergence(proj, remote_sha, git=git, env=env)
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
        git,
        "show",
        "--no-textconv",
        f"{target_sha}:src/kiro_crew/__init__.py",
        cwd=proj,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        show_out, _ = await asyncio.wait_for(show.communicate(), timeout=10)
    except asyncio.CancelledError:
        await platform_compat.kill_and_reap(show)
        raise
    except asyncio.TimeoutError:
        await platform_compat.kill_and_reap(show)
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
            git,
            "diff",
            # A tracked `.gitattributes` can name a diff driver for CHANGELOG.md;
            # these keep git's own text diff whatever the attributes say.
            "--no-textconv",
            "--no-ext-diff",
            f"{diff_base}..{target_sha}",
            "--",
            "CHANGELOG.md",
            cwd=proj,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            diff_out, _ = await asyncio.wait_for(diff.communicate(), timeout=10)
        except asyncio.CancelledError:
            await platform_compat.kill_and_reap(diff)
            raise
        except asyncio.TimeoutError:
            await platform_compat.kill_and_reap(diff)
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
    return await fetch_feed_bytes(url, timeout_secs=_FEED_TIMEOUT_SECS, max_bytes=_FEED_MAX_BYTES)


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

    **Security posture — the manifest is UNTRUSTED display metadata, with one
    verified exception.** This function does not verify the manifest's RSA
    signature for the ordinary update verdict: that check lives in ``cli.sh``,
    which pins the key offline and is the only thing that installs bytes. So
    nothing actionable is taken from the payload — ``wheel_url``, ``sha256``
    and ``signature`` are read by nobody here, and the recommended command is
    composed locally from the already-validated channel name. A tampered feed
    can therefore nag the user or hide an update; it cannot redirect an
    install. The exception is the optional ``min_version`` floor: it coerces
    the UI (a non-dismissible prompt), so it is honored only after
    ``platform/feed_trust.py`` verifies the signature against the same pinned
    key — and every verification failure degrades to the ordinary dismissible
    prompt, never toward coercion.
    """
    channel = _release_channel()
    feed_base, _artifact_base = _cdn_bases()
    # `for_channel` keeps the pair honest: the channel reported here and the command
    # offered alongside it are read at different moments, and a switch landing in
    # between would otherwise publish one lane's name next to a command that moves
    # the install to the other.
    #
    # `can_arm` is probed here, with the verdict, rather than derived by the SPA:
    # `managed_by == "kirocrew"` also covers bare source installs the arm
    # endpoint refuses, so shipping the wider signal would render a dead button.
    # Offloaded — the probe resolves venv paths on disk.
    can_arm = await _can_arm_this_install()
    base: dict[str, object] = {
        **_capability_fields(capability.for_channel(channel)),
        "channel": channel,
        "can_arm": can_arm,
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
    # Optional forced-update floor. Unlike every other field this one COERCES
    # the UI (a non-dismissible prompt), so it is honored only when the
    # manifest's RSA signature verifies against the same offline key cli.sh
    # pins — a tampered feed must not be able to hold the dashboard hostage
    # while the signed installer (correctly) refuses the tampered bytes.
    # Malformed, inconsistent, or UNVERIFIED values are DROPPED, never fatal:
    # every failure degrades to the ordinary dismissible prompt. Offloaded —
    # verification shells out to openssl.
    floor = manifest.get("min_version")
    if isinstance(floor, str) and _MIN_VERSION_RE.match(floor):
        if _is_newer(floor, _display_version(remote_version, channel)) is True:
            # A floor above the very version the feed offers demands an update
            # the feed cannot satisfy — inconsistent, so ignore it. The offered
            # version is folded per channel first: a promoted stable build's
            # manifest says ``0.3.0rc13`` while meaning the ``0.3.0`` release.
            logger.warning("Release feed %s min_version %s exceeds its version", url, floor)
        elif not await asyncio.to_thread(feed_trust.verify_manifest_signature, manifest):
            logger.warning(
                "Release feed %s carries min_version %s but its signature does "
                "not verify — ignoring the forced-update floor",
                url,
                floor,
            )
        else:
            extra["feed_min_version"] = floor
            # The enforcement surface is the dashboard modal, which a headless
            # install never opens — leave evidence in the log so an operator
            # tailing a below-floor gateway learns the update is mandatory.
            if _is_newer(floor, _display_version(_local_version, channel)) is True:
                logger.warning(
                    "This install (%s) is below the release feed's minimum "
                    "supported version %s — the update is mandatory",
                    _local_version,
                    floor,
                )
    elif floor is not None:
        logger.warning("Release feed %s carries an unusable min_version", url)
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
        # The other direction of the same comparison, and the only place it can
        # honestly be computed: the running build is ahead of everything this
        # lane publishes, so the lane never shipped it. Written from the same
        # response as the verdict so a consumer can never pair one channel's
        # move state with another channel's version.
        #
        # Compared by RELEASE: a distribution build stamp (``0.6.0.12``, see
        # ``kiro_crew/__init__``) is a build OF ``0.6.0``, which the lane did
        # ship. Comparing the raw stamp would read every stamped build as
        # permanently ahead of its own lane and pin a standing "re-run the
        # installer" affordance on the About panel, pointing at the bare wheel
        # that would un-stamp it. The ``available`` verdict above needs no fold:
        # ``0.6.0`` is not newer than ``0.6.0.12`` either way.
        channel_move_pending=_is_newer(release_of_build(_local_version), remote_version) is True,
        check_status=CHECK_SUCCEEDED,
        **extra,
    )
