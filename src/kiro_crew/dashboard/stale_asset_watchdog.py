"""Periodic watchdog that detects stale/missing dashboard assets.

When an update prunes kirocrew, the running gateway's install directory is
pruned — the process keeps running but its static assets are gone. The gateway
then serves the fallback page (see ``DASHBOARD_HTML_NOT_FOUND_MARKER`` in
``handlers/core.py``) and rejects freshly minted tokens (signing key mismatch).
External clients can kill and restart the process, but detection can take
minutes and a forced restart may fail on slow cold starts.

This watchdog runs inside the gateway itself and catches the problem at the
source: if the dashboard static bundle is missing, log a CRITICAL warning
and initiate graceful shutdown so a supervisor (systemd, launchd) can
restart a fresh process immediately. Two cases stand it down instead: a gap
an update step this gateway is running owns (``update_ownership``), since
shutting down would cancel that step mid-write; and a supervisor that could not
relaunch the gateway (``gateway_restart.supervisor_reentry``), since the exit
would then leave nothing running.

The check is cheap (one Path.is_file() + one Path.is_file() — no I/O beyond
stat()) and runs every 60 seconds by default. It only arms itself if assets
are present at startup — a dev/source install that never built its frontend
won't be killed (the watchdog detects "assets vanished", not "assets never
existed").

The presence check mirrors ``handlers/core.py:index()``'s serve criterion
exactly (``dist/index.html`` is a file — there is no ``dashboard.html``
fallback), so a partial-prune state where an empty
``dist/`` directory node remains cannot mask a genuine vanish.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from kiro_crew import update_ownership
from kiro_crew.dashboard.handlers.core import _DIST_INDEX
from kiro_crew.update_ownership import Reentry, ReentryVerdict

logger = logging.getLogger(__name__)

# Default check interval (seconds). Long enough to be negligible overhead,
# short enough that an update mid-session is caught within a minute.
_CHECK_INTERVAL_SECS = 60

# Delay before re-checking a failed sample (seconds). A frontend rebuild in a
# source install deletes and recreates static/dist/ in well under this window;
# a genuine update prune is permanent, so the confirmation only adds this
# much detection latency.
_CONFIRM_DELAY_SECS = 2.0

# Max time to let in-flight backend turns finish before forcing shutdown after
# a confirmed asset vanish. An update prune only breaks static-asset serving —
# live ACP turns keep working — so draining lets active turns complete (result
# captured, history saved) instead of being killed mid-prompt when the
# supervisor restarts. Bounded so a wedged turn can't defer the restart forever;
# the drain after the re-entry check gets only what the first one left of it.
_DRAIN_TIMEOUT_SECS = 120.0
# Poll cadence while draining (seconds). Also the max latency to react to an
# external SIGTERM arriving mid-drain.
_DRAIN_POLL_SECS = 2.0

# Bound on one re-entry check (it reads the service definition and probes an
# interpreter). Past it the check is inconclusive; the probe is not restarted
# while it is still running.
_REENTRY_CHECK_TIMEOUT_SECS = 30.0
# After the re-entry check refuses, it is asked again on a backoff instead of
# every tick: from one check interval, doubling, up to this ceiling (seconds).
# A few intervals, so a repaired install is relaunched within minutes.
_REENTRY_RECHECK_MAX_SECS = 300.0

# Process exit status the gateway uses when THIS watchdog initiated the
# shutdown. Non-zero on purpose: the whole point of the shutdown is to be
# restarted by a supervisor, and ``Restart=on-failure`` (systemd) /
# ``KeepAlive.SuccessfulExit=false`` (launchd) style policies only relaunch a
# process that did NOT exit 0. A unit generated before ``Restart=always``
# landed, or one an operator hand-edited back to ``on-failure``, would
# otherwise treat the clean exit as "done" and leave the gateway down until
# a human notices. 75 is ``EX_TEMPFAIL`` from ``sysexits.h`` ("temporary
# failure; retry later"), the closest standard meaning to "restart me".
STALE_ASSET_EXIT_CODE = 75


def shutdown_exit_code(watchdog: "asyncio.Future[bool] | None") -> int:
    """Map the watchdog task's outcome onto the gateway's process exit status.

    ``STALE_ASSET_EXIT_CODE`` iff the watchdog has finished and reported
    ``True`` (it confirmed a vanish and set the shutdown event itself). Every
    other state is 0: no watchdog, still running (the event was set by
    SIGTERM/``systemctl stop`` while it slept), cancelled, or crashed — a
    crashed watchdog must not turn an operator's stop into a restart.
    """
    if watchdog is None or not watchdog.done() or watchdog.cancelled():
        return 0
    try:
        fired = watchdog.result()
    except Exception:
        logger.debug("Stale-asset watchdog task raised", exc_info=True)
        return 0
    return STALE_ASSET_EXIT_CODE if fired else 0


@dataclass
class _Standing:
    """A refusal keeping the gateway up: when to ask again, and what was said."""

    until: float
    backoff: float
    said: str


class _ShutdownSignal(Protocol):
    """Minimal contract we need from a shutdown-signalling event."""

    def is_set(self) -> bool:
        ...

    def set(self) -> None:
        ...

    async def wait(self) -> bool:
        ...


def assets_present() -> bool:
    """Return True if the dashboard can serve a real page (not the fallback).

    Mirrors the criterion used by ``handlers/core.py:index()``: the React
    bundle's ``dist/index.html`` must be present (there is no ``dashboard.html``
    fallback). Checking ``_DIST_INDEX.is_file()``
    (not ``_DIST_DIR.is_dir()``) is critical: an empty ``dist/`` directory node
    is a valid partial-prune state where the handler serves the guidance page,
    and the watchdog must recognise that as "assets vanished."
    """
    return _DIST_INDEX.is_file()


async def run_stale_asset_watchdog(
    shutdown_event: _ShutdownSignal,
    *,
    interval: float = _CHECK_INTERVAL_SECS,
    confirm_delay: float = _CONFIRM_DELAY_SECS,
    count_in_flight: Callable[[], int] | None = None,
    drain_timeout: float = _DRAIN_TIMEOUT_SECS,
    drain_poll: float = _DRAIN_POLL_SECS,
    reentry_check: Callable[[], ReentryVerdict] | None = None,
) -> bool:
    """Background loop: check asset presence, trigger shutdown if stale.

    Returns ``True`` iff this watchdog is the one that set ``shutdown_event``
    (a confirmed asset vanish). Every other exit — never armed, or
    ``shutdown_event`` set externally by SIGTERM/``systemctl stop`` — returns
    ``False``. The gateway maps ``True`` onto :data:`STALE_ASSET_EXIT_CODE` so
    the process exits non-zero and a restart-on-failure supervisor relaunches
    it, while an operator-initiated stop still exits 0 and stays stopped.

    Only arms if assets are present at startup. A fresh source/dev install
    that never built its frontend will NOT be killed — the watchdog
    specifically detects "assets were here and then vanished" (the update
    scenario), not "assets never existed."

    A failed check is re-confirmed after ``confirm_delay`` seconds before
    shutdown is triggered, so a transient asset gap (e.g. a frontend rebuild
    that deletes and recreates ``static/dist/``) that coincides with a tick
    cannot kill an otherwise-healthy gateway. Presence is re-checked once more
    once in-flight work has drained, covering a rebuild that outlives the
    confirmation but finishes while turns are still draining; on an idle
    gateway there is nothing to drain and that re-check adds no grace. A
    genuine update prune is permanent and fails every check, so it still shuts
    down.

    Once a vanish is confirmed, in-flight backend work is *drained* before the
    shutdown event is set (see ``_drain_in_flight``): the prune only breaks
    static-asset serving, so active ACP turns can finish rather than being
    killed mid-prompt when the supervisor restarts a fresh process.

    A missing bundle that an update step this gateway is running owns
    (``update_ownership.current_owner``) does not shut down. The owner is read on
    every missing sample, and once more as the last thing before signalling,
    with no await in between, because a step can start inside the confirm or
    drain window. A bundle missing at startup while a step owns it is waited out
    rather than read as a dev install; if it is still missing once that step
    ends, the update made the gap, so the watchdog arms and handles it like any
    other vanish.

    Right before signalling it also asks whether the supervisor could relaunch
    the gateway (``reentry_check``), and whether an update chose to stay up
    instead of restarting (``update_ownership.restart_refusal``, read with no
    await before the signal). A refusal keeps the gateway up on its loaded code:
    it is said once, then asked again on a backoff without re-running the confirm
    and the drain each tick; a positive answer then runs them again, and the check
    after the drain decides. An inconclusive answer is logged and is not a
    refusal, but it does not end one that is standing either. Work admitted while
    the check ran is drained again before the signal.

    Parameters
    ----------
    shutdown_event:
        The gateway's global shutdown event. Setting it initiates graceful
        shutdown (same as SIGTERM).
    interval:
        Seconds between checks. Default 60s.
    confirm_delay:
        Seconds to wait before re-checking a failed sample. Default 2s.
    count_in_flight:
        Optional callable returning the number of in-flight backend tasks
        (active provider turns, Slack session turns). When provided, the
        watchdog waits for it to reach zero — bounded by ``drain_timeout`` —
        before triggering shutdown. ``None`` disables draining (shut down
        immediately on vanish).
    drain_timeout:
        Max seconds to wait for in-flight work to finish, shared by the drains
        before and after the re-entry check. Default 120s.
    drain_poll:
        Seconds between in-flight re-counts while draining. Default 2s.
    reentry_check:
        Optional: whether the supervisor could relaunch the gateway. Blocking;
        run on its own worker under a bound, and a check that raises or does not
        answer is inconclusive. A check still running is waited on again rather
        than started twice, and one that answered after its ask gave up is read
        once before a new one starts.
    """
    announced: str | None = None

    def _owner(*, after_drain: bool = False) -> str | None:
        nonlocal announced
        owner = update_ownership.current_owner()
        if owner is not None and (after_drain or owner != announced):
            # After a drain that logged "proceeding with shutdown", always say
            # why the shutdown is not happening.
            announced = owner
            logger.warning(
                "Stale-asset watchdog: assets missing while %s owns the gap; "
                "standing down%s.",
                owner,
                " after drain" if after_drain else "",
            )
        return owner

    loop = asyncio.get_running_loop()
    check_worker: concurrent.futures.ThreadPoolExecutor | None = None
    check_pending: asyncio.Future[ReentryVerdict] | None = None
    # The refusal generation when the pending check started, and when the
    # check whose verdict was last read started.
    check_generation = 0
    answered_generation = 0
    standing: _Standing | None = None
    inconclusive_said: str | None = None

    async def _reentry() -> ReentryVerdict:
        nonlocal check_worker, check_pending, check_generation, answered_generation
        if reentry_check is None:
            return ReentryVerdict(Reentry.REENTERABLE)
        try:
            # A check an earlier ask stopped waiting for is waited on again, and
            # read if it has answered since; only one that was read is replaced.
            if check_pending is None:
                if check_worker is None:
                    check_worker = concurrent.futures.ThreadPoolExecutor(
                        1, thread_name_prefix="stale-asset-reentry"
                    )
                check_generation = update_ownership.refusal_generation()
                check_pending = loop.run_in_executor(check_worker, reentry_check)
            verdict = await asyncio.wait_for(
                asyncio.shield(check_pending), _REENTRY_CHECK_TIMEOUT_SECS
            )
            check_pending = None
            answered_generation = check_generation
            return verdict
        except asyncio.TimeoutError:
            return ReentryVerdict(Reentry.INCONCLUSIVE, "the check did not answer")
        except Exception as exc:
            check_pending = None
            logger.debug("Stale-asset watchdog: re-entry check failed", exc_info=True)
            return ReentryVerdict(Reentry.INCONCLUSIVE, str(exc) or type(exc).__name__)

    def _with_stay_up(verdict: ReentryVerdict) -> ReentryVerdict:
        """Under a service manager, an update that chose to stay up refuses too.

        Read with no await.

        A probe that proved the relaunch would start is fresher evidence than a
        refusal recorded before it began (an install repaired out of band), so
        it clears that refusal; a refusal recorded since still wins.
        """
        from kiro_crew.config.loader import launched_as_managed_service

        if verdict.status is Reentry.REENTERABLE and verdict.probed:
            update_ownership.clear_restart_refusal(recorded_by=answered_generation)
        if not launched_as_managed_service():
            # Nothing relaunches through a service manager here, so nothing
            # is refused: the exit behaves as it always has.
            return verdict
        stayed_up = update_ownership.restart_refusal()
        if stayed_up is not None:
            return ReentryVerdict(Reentry.REFUSED, f"an update stopped before restart: {stayed_up}")
        return verdict

    def _say_inconclusive(verdict: ReentryVerdict) -> None:
        nonlocal inconclusive_said
        if verdict.reason != inconclusive_said:
            inconclusive_said = verdict.reason
            logger.warning(
                "Stale-asset watchdog: could not tell whether the gateway's supervisor "
                "can relaunch it (%s).",
                verdict.reason,
            )

    def _refused(verdict: ReentryVerdict) -> None:
        """Hold off for the next backoff step; say a refusal once per reason."""
        nonlocal standing
        said = standing.said if standing is not None else ""
        if verdict.status is Reentry.REFUSED and verdict.reason != said:
            said = verdict.reason
            logger.critical(
                "Dashboard static assets vanished, but the gateway's supervisor could "
                "not relaunch it (%s), so it stays up on its loaded code.",
                verdict.reason,
            )
        elif verdict.status is Reentry.INCONCLUSIVE:
            _say_inconclusive(verdict)
        backoff = interval
        if standing is not None:
            backoff = max(interval, min(standing.backoff * 2, _REENTRY_RECHECK_MAX_SECS))
        standing = _Standing(until=loop.time() + backoff, backoff=backoff, said=said)

    present = assets_present()
    owner_seen = False
    while not present and _owner() is not None:
        # An update started at boot can have the bundle missing before the
        # watchdog arms: wait for it rather than disarm as a dev install.
        owner_seen = True
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return False
        except asyncio.TimeoutError:
            pass
        present = assets_present()
    if not present and not owner_seen:
        # Assets were never here — this is likely a dev/source install that
        # hasn't built its frontend yet. Don't arm the watchdog; let the
        # gateway serve the fallback page as it always has.
        logger.info(
            "Stale-asset watchdog: assets not present at startup — "
            "not arming (dev/source install without a built frontend)."
        )
        return False

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return False
        except asyncio.TimeoutError:
            pass

        if not assets_present():
            if _owner() is not None:
                continue
            if standing is not None:
                # A refusal is standing: skip the confirm and the drain (both
                # would only lead back to it) until the backoff is up, and go on
                # to them only on a positive answer.
                if loop.time() < standing.until:
                    continue
                asked = _with_stay_up(await _reentry())
                if asked.status is not Reentry.REENTERABLE:
                    _refused(asked)
                    continue
            # Re-confirm after a short delay: a frontend rebuild in a source
            # install deletes and recreates static/dist/, and an unlucky tick
            # inside that window must not kill a healthy gateway. An update
            # prune is permanent, so it still fails the second check. Wait
            # on shutdown_event so an external SIGTERM interrupts promptly.
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=confirm_delay
                )
                return False
            except asyncio.TimeoutError:
                pass
            if assets_present():
                logger.warning(
                    "Stale-asset watchdog: assets briefly missing but "
                    "reappeared — likely a frontend rebuild; not shutting "
                    "down."
                )
                continue
            if shutdown_event.is_set():
                # Someone else shut us down during the confirm window; don't
                # log a misleading "watchdog fired" CRITICAL.
                return False
            drain_started = loop.time()
            await _drain_in_flight(
                shutdown_event,
                count_in_flight,
                drain_timeout=drain_timeout,
                drain_poll=drain_poll,
            )
            drained = loop.time() - drain_started
            if shutdown_event.is_set():
                # An external SIGTERM arrived during the drain window and has
                # already begun graceful shutdown — don't double-signal.
                return False
            verdict = await _reentry()
            if shutdown_event.is_set():
                # An operator stop landed during that check: it is theirs.
                return False
            # A turn can be admitted while the check runs: drain again. On an
            # idle gateway this counts once and returns without yielding, and it
            # is the last await before signalling: everything below reads state
            # and signals without yielding. One budget covers both drains, so a
            # wedged turn holds the signal for at most one drain's worth.
            await _drain_in_flight(
                shutdown_event,
                count_in_flight,
                drain_timeout=max(0.0, drain_timeout - drained),
                drain_poll=drain_poll,
            )
            if shutdown_event.is_set():
                return False
            # Re-check once more now that in-flight work has drained. This
            # covers a rebuild that outlives the confirmation but finishes
            # while turns are still draining; it adds no grace on an idle
            # gateway, where _drain_in_flight returns immediately. Without it a
            # drain long enough for the assets to reappear still ends in a
            # shutdown — the healthy-gateway kill the confirmation exists to
            # prevent.
            if assets_present():
                logger.warning(
                    "Stale-asset watchdog: assets reappeared while draining "
                    "in-flight work — likely a slow frontend rebuild; not "
                    "shutting down."
                )
                continue
            # With no await in between: an update step can have started, or an
            # update can have chosen to stay up, inside the confirm, the drains
            # or the re-entry check.
            if _owner(after_drain=True) is not None:
                continue
            verdict = _with_stay_up(verdict)
            if verdict.status is Reentry.REFUSED or (
                standing is not None and verdict.status is not Reentry.REENTERABLE
            ):
                _refused(verdict)
                continue
            if verdict.status is Reentry.INCONCLUSIVE:
                _say_inconclusive(verdict)
            logger.critical(
                "Dashboard static assets vanished — an update likely "
                "pruned the running install. Initiating graceful shutdown "
                "so a supervisor can restart a fresh gateway."
            )
            shutdown_event.set()
            return True
        else:
            # A healthy sample ends the gap: a later one is announced, asked and
            # said afresh, and a check still running from it is not its answer.
            announced = None
            standing = None
            inconclusive_said = None
            check_pending = None
    # Loop never entered: the event was already set when the watchdog armed.
    return False


async def _drain_in_flight(
    shutdown_event: _ShutdownSignal,
    count_in_flight: Callable[[], int] | None,
    *,
    drain_timeout: float,
    drain_poll: float,
) -> None:
    """Wait (bounded) for in-flight backend turns to finish before shutdown.

    An update prune breaks only static-asset serving; live ACP turns keep
    working. Draining lets active turns complete (result captured, history
    saved) instead of being killed mid-prompt when the supervisor restarts —
    directly preventing the "❌ lost to gateway restart / no result captured"
    orphaning seen on an abrupt prune.

    Returns as soon as any of the following is true:
      * there is no in-flight work,
      * the ``drain_timeout`` elapses (remaining tasks are snapshotted for
        resume by the normal shutdown path), or
      * an external shutdown is signalled mid-drain (SIGTERM wins).

    Any failure to count in-flight work is treated as "idle" — a broken
    predicate must never wedge shutdown.
    """
    if count_in_flight is None or drain_timeout <= 0:
        return
    try:
        pending = count_in_flight()
    except Exception:
        logger.debug(
            "Stale-asset watchdog: initial in-flight count failed — "
            "skipping drain.",
            exc_info=True,
        )
        return
    if pending <= 0:
        return

    logger.warning(
        "Stale-asset watchdog: draining %d in-flight task(s) before shutdown "
        "(up to %.0fs)…",
        pending,
        drain_timeout,
    )
    loop = asyncio.get_event_loop()
    deadline = loop.time() + drain_timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        # Sleep interruptibly: an external SIGTERM sets shutdown_event and
        # wakes us immediately so we don't keep draining past a real shutdown.
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=min(drain_poll, remaining)
            )
            logger.warning(
                "Stale-asset watchdog: external shutdown during drain — "
                "stopping drain."
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            pending = count_in_flight()
        except Exception:
            logger.debug(
                "Stale-asset watchdog: in-flight count failed mid-drain — "
                "proceeding with shutdown.",
                exc_info=True,
            )
            return
        if pending <= 0:
            logger.warning(
                "Stale-asset watchdog: all in-flight tasks drained — "
                "proceeding with shutdown."
            )
            return

    logger.warning(
        "Stale-asset watchdog: drain timeout (%.0fs) elapsed with %d task(s) "
        "still in flight — proceeding with shutdown; open sessions resume "
        "from snapshot on restart.",
        drain_timeout,
        pending,
    )
