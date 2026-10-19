"""The cached update verdict, and what the status surfaces derive from it.

``_update_info`` is the last check's result in the capability contract's own
vocabulary. This module also holds how a version is compared and displayed, the
feed's forced-update floor, and the ``auto_update_effect`` the status frame serves
(derived off the loop, memoized, refreshed on a TTL).

Served through :mod:`kiro_crew.dashboard.handlers.updates`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from kiro_crew import __version__ as _local_version
from kiro_crew.changelog import base_version
from kiro_crew.platform.update_capability import (
    AUTO_EFFECT_UNKNOWN,
    CHECK_UNCHECKED,
    MODE_NONE,
    AutoUpdateEffect,
    UpdateCapability,
    auto_update_effect,
)
from kiro_crew.platform.update_governance import min_version, update_required

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")


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
    #: Can THIS install take the in-app arm+approve path? True only for the
    #: cli.sh managed venv — the one shape whose shadow updater the gateway
    #: can run on itself. Carried on the wire so the SPA never renders an Arm
    #: button that the arm endpoint would 409.
    "can_arm": False,
    #: Is the RUNNING build ahead of what the FOLLOWED channel publishes? See
    #: :func:`_channel_move_pending`. Set only by a successful feed check, so a
    #: feedless layout (git checkout, desktop bundle, container) and every
    #: unchecked or failed check leave it False.
    "channel_move_pending": False,
}


#: How long a derived ``auto_update_effect`` is served before the status surface
#: re-derives it (off the loop). Install shape changes by hand (a branch switch,
#: a policy edit), so five minutes is fresh enough while keeping the git probes
#: it costs off every frame.
_AUTO_EFFECT_TTL_SECS = 300.0
#: (when derived, effect). ``None`` until the update loop's first derivation,
#: which is also what arms the status path's re-derivation: a process that runs
#: no update loop never shells out to git from its status frame.
_auto_effect: tuple[float, str] | None = None
_auto_effect_task: "asyncio.Task[None] | None" = None
#: The git-free answer a frame serves before the loop's first derivation,
#: memoized: it depends on install shape alone, so one derivation per process
#: keeps its layout stats off every subsequent frame.
_shape_effect: str | None = None


# Error codes for ``_update_info["error_code"]``. The dashboard maps these to
# localized copy, so they are a contract: add, never silently repurpose. Defined
# in ``platform/update_capability`` so the CLI and the dashboard cannot disagree
# about what a failure is called.


def get_update_info() -> dict[str, object]:
    """Return a copy of the cached update-check state."""
    return dict(_update_info)


def remediation_command(info: dict[str, object]) -> str:
    """The copyable command from a check result's ``remediation``, or ``""``.

    Composed locally from validated inputs, never from a feed field, which is
    what makes it safe for the one caller that RUNS it: the gateway's
    managed-venv auto-apply hands it to a trusted shell
    (``_auto_apply_wheel_update``). Every other reader displays it for the
    operator to copy.
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

    Correct for a version the followed channel actually PUBLISHES, which is what
    every caller passes for a REMOTE version. For the RUNNING build, go through
    :func:`_display_local_version`, which additionally refuses to fold bytes the
    stable lane has never shipped -- see ``channel_move_pending``.
    """
    return base_version(version) if channel == "stable" else version


def _channel_move_pending() -> bool:
    """Is the RUNNING build ahead of what the followed channel publishes?

    True means the followed lane has never shipped these bytes, so this install
    is not ON that lane no matter what its ``channel`` file says: flipping the
    switcher to stable while running an insider build (``0.5.0rc3`` against a
    stable feed at ``0.4.1rc1``) is exactly this state, and it is only left by
    re-running the installer for the chosen channel.

    Derived from the FEED's own answer rather than from the version string's
    prerelease stamp, and that is the whole point. Promotion never re-stamps, so
    the stable lane's current release IS ``0.4.1rc1`` -- a stamp-based rule
    ("insider-looking bytes mean an insider install") therefore misreads the
    entire promoted-stable population as mid-switch, which is the false positive
    this predicate replaces. A stable install merely running BEHIND (``0.4.0rc14``
    against a feed at ``0.4.1rc1``) is not newer, so it is not a move: it is an
    ordinary available update, and ``update_available`` already says so.

    False whenever no feed answer exists -- an unchecked, failed, or feedless
    layout (a git checkout, a desktop bundle, a container) must never be told it
    is mid-switch on the strength of a comparison that never happened.
    """
    return bool(_update_info.get("channel_move_pending"))


def _display_local_version() -> str:
    """``_local_version`` folded for display, keyed on the off-loop channel that
    a prior ``_do_update_check`` resolved into ``_update_info`` -- a plain dict
    read, so this stays off the event loop.

    A pending channel move SUPPRESSES the fold. The fold's premise is that a
    prerelease-looking stamp on a stable install is a promoted candidate, so the
    clean base is the honest label; that premise fails for a build the stable
    lane never published. Folding there renamed a running ``0.5.0rc3`` insider
    build to ``0.5.0`` the moment the switcher was flipped to stable -- claiming
    a stable release that does not exist, next to an "up to date" badge.
    """
    if _channel_move_pending():
        return _local_version
    return _display_version(_local_version, str(_update_info.get("channel") or ""))


def _feed_requires_update() -> bool:
    """Is this install below the release feed's forced-update floor?

    Reads the floor a prior ``_check_release_feed`` stored — a plain dict read,
    so this stays off the event loop. ``_is_newer`` returning ``None`` (an
    unparseable local version) reads as NOT required: a floor must never force
    an update it cannot prove is needed.

    The local version is folded per channel before the comparison (the same
    rule as ``_display_local_version``, delegated to it so the two cannot
    drift): a promoted STABLE build keeps its soaked candidate's prerelease
    stamp (``0.3.0rc13`` IS the ``0.3.0`` release), so comparing the raw stamp
    against a bare floor of ``0.3.0`` would force an update onto the very build
    the floor names. On insider/nightly the stamp is a real prerelease and stays
    significant.
    """
    floor = _update_info.get("feed_min_version")
    if not isinstance(floor, str) or not floor:
        return False
    return _is_newer(floor, _display_local_version()) is True


def _effective_update_required() -> bool:
    """Mandatory-update verdict: the governance pin OR the release feed floor.

    Two independent authorities, one consumer contract. The enterprise pin
    (``security_policy.json``) binds managed fleets; the feed floor binds every
    feed-checkable install when a release declares a breaking floor. Either one
    alone makes the update mandatory — tightest wins, matching the governance
    model everywhere else.
    """
    return update_required(_local_version) or _feed_requires_update()


def _effective_min_version() -> str:
    """The floor to SHOW next to a mandatory update (``""`` when none applies).

    When both authorities pin, the HIGHER floor wins the display slot: it is
    the one the install must actually reach, and naming the lower would tell
    the user a version that still leaves them below the other authority's
    floor. An incomparable pair (unparseable governance pin) falls back to
    whichever authority is enforcing.
    """
    governance_floor = min_version() if update_required(_local_version) else ""
    feed_floor = str(_update_info.get("feed_min_version") or "") if _feed_requires_update() else ""
    if governance_floor and feed_floor:
        return feed_floor if _is_newer(feed_floor, governance_floor) is True else governance_floor
    return governance_floor or feed_floor


def _downgrade_target_below_min_version(version: str, channel: str) -> bool:
    """Whether *version* is a downgrade that crosses the enterprise floor.

    A host already below the floor may still move upward toward compliance when
    its followed lane lags the pin. Target versions may carry prerelease stamps,
    so use this module's update comparator. A Stable feed's promoted candidate
    is the final release despite retaining its RC stamp, so fold only that lane
    for the floor comparison; compare direction against the raw running bytes.
    """
    floor = min_version()
    if not floor:
        return False
    target_for_floor = base_version(version) if channel == "stable" else version
    target_below_floor = _is_newer(floor, target_for_floor) is True
    target_below_running = _is_newer(_local_version, version) is True
    return target_below_floor and target_below_running


def record_auto_update_effect(effect: AutoUpdateEffect) -> None:
    """Serve *effect* on the status surface; the update loop records each one it acts on.

    Also arms the status path's own re-derivation, so a branch switch or policy
    edit shows within one TTL rather than at the loop's next cycle.
    """
    global _auto_effect
    _auto_effect = (time.monotonic(), effect.effect)


def _start_auto_effect_refresh() -> "asyncio.Task[None]":
    """The in-flight re-derivation, started if none is. Needs a running loop."""
    global _auto_effect_task

    if _auto_effect_task is None or _auto_effect_task.done():
        _auto_effect_task = asyncio.get_running_loop().create_task(_refresh_auto_update_effect())
    return _auto_effect_task


async def _refresh_auto_update_effect_once() -> None:
    """Await the shared derivation, starting it only when none is in flight."""
    await asyncio.shield(_start_auto_effect_refresh())


async def _refresh_auto_update_effect() -> None:
    global _auto_effect
    try:
        effect = await asyncio.to_thread(auto_update_effect)
    except Exception:
        logger.debug("auto_update_effect could not be derived", exc_info=True)
        # Stamp the attempt so a persistent failure is retried once per TTL,
        # not on every frame; the last answer keeps being served.
        if _auto_effect is not None:
            _auto_effect = (time.monotonic(), _auto_effect[1])
        return
    if effect is not None:  # git_probes defaults True, so it always answers
        record_auto_update_effect(effect)


def _status_auto_update_effect() -> str:
    """The last derived effect; re-derived off the loop once it is stale.

    Before the update loop's first derivation lands, the shapes whose answer
    needs no git subprocess are derived right here, so a desktop bundle or a
    managed venv is never reported as unknown on a first frame. Only a checkout
    waits: its branch and remote are what git alone can report, and the status
    frame must not shell out.
    """
    cached = _auto_effect
    if cached is not None and time.monotonic() - cached[0] > _AUTO_EFFECT_TTL_SECS:
        # A frame built outside a loop (a test, a CLI) just serves the cache.
        with contextlib.suppress(RuntimeError):
            _start_auto_effect_refresh()
    if cached is not None:
        return cached[1]
    # Read, never computed here: deriving it stats the install tree, which can
    # be network-backed, and this reader runs on the event loop. The status
    # funnel primes it off the loop first (``prime_status_auto_update_effect``).
    return _shape_effect if _shape_effect is not None else AUTO_EFFECT_UNKNOWN


async def prime_status_auto_update_effect() -> None:
    """Derive the git-free effect once, OFF the event loop, for the status frame.

    Called by the status funnel before it reads :func:`status_update_fields`,
    so a frame served before the update loop's first derivation still answers
    every shape but a checkout. Memoized: it depends on install shape alone.
    """
    global _shape_effect
    if _shape_effect is not None or _auto_effect is not None:
        return
    try:
        answer = await asyncio.to_thread(auto_update_effect, git_probes=False)
    except Exception:
        logger.debug("auto_update_effect could not be derived without git", exc_info=True)
        answer = None
    _shape_effect = answer.effect if answer is not None else AUTO_EFFECT_UNKNOWN


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
            "can_arm": False,
            "channel_move_pending": False,
        }
    )
    _update_info.update(fields)


def _capability_fields(capability: UpdateCapability) -> dict[str, object]:
    """The capability half of a cache entry, shared by every check branch.

    Delegates rather than re-listing the fields: two hand-rolled copies of one
    shape drift the moment a field is added to the contract.
    """
    return capability.to_dict()
