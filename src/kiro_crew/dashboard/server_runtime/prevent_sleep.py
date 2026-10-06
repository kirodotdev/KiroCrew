"""Keeping the host awake while a turn is in flight or the dashboard is on the tailnet.

The decision, the poll that applies it, and the release at shutdown.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _PREVENT_SLEEP_POLL_INTERVAL_SECS,
        DashboardState,
        KiroCrewConfig,
        SleepInhibitor,
        _tailnet_publish_keeps_awake,
        logger,
    )


async def _should_prevent_sleep(state: DashboardState, port: int) -> bool:
    """Whether the host should be kept awake right now.

    Two independent reasons, either sufficient on its own:

    * **A turn is in flight**, and the user opted in via
      ``dashboard.prevent_sleep``. The original reason this poll exists.
    * **The dashboard is published on this machine's tailnet**, and
      ``dashboard.tailscale.keep_awake`` is on. A phone loses the dashboard the
      moment the laptop idles, so publishing is itself the opt-in — an operator
      who put the dashboard on their tailnet asked for it to stay reachable.
      Deliberately NOT also gated on ``dashboard.prevent_sleep``: that switch is
      scoped to in-flight turns, and making someone find it to keep a published
      dashboard alive would be the wrong switch in the wrong place. The escape
      hatch is ``keep_awake``, which turns off the awake half without
      unpublishing.

    Reads config live so either toggle takes effect on the next poll without a
    restart. Fail-closed throughout: any error resolves to "allow sleep", so a
    config or daemon hiccup can never wedge the machine awake.
    """
    try:
        # The live-config watcher is the ONE poller of config.json; this loop
        # reads the config it has already adopted (a plain attribute read) rather
        # than statting the file itself every tick. The load runs only when the
        # watcher has no snapshot yet (the first ticks after boot), and off the
        # loop, because on a slow home filesystem it is a blocking call
        # (no-blocking-call-on-event-loop).
        from kiro_crew.config import live

        cfg = live.snapshot()
        if cfg is None:
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
        # Both reads sit INSIDE the guard, and that placement is the actual
        # defence: a config object predating the tailscale section raises on the
        # attribute, and outside the guard that would propagate — a partially
        # formed config wedging a laptop awake, since the poll swallows the error
        # and retries forever. The getattr defaults are belt-and-braces on top.
        tailscale_cfg = getattr(cfg.dashboard, "tailscale", None)
        tailnet_enabled = bool(getattr(tailscale_cfg, "enabled", False))
        tailnet_keep_awake = bool(getattr(tailscale_cfg, "keep_awake", False))
        tailnet_wants_awake = tailnet_enabled and tailnet_keep_awake
        opted_into_turn_wake = bool(getattr(cfg.dashboard, "prevent_sleep", False))
    except Exception:
        logger.debug("prevent-sleep config read failed", exc_info=True)
        return False
    if tailnet_wants_awake and await _tailnet_publish_keeps_awake(port):
        return True
    if not opted_into_turn_wake:
        return False
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        # In-memory dict scan on the loop thread (no await inside, so no
        # concurrent mutation) — cheap and non-blocking.
        return sessions.any_active_turn()
    except Exception:
        logger.debug("prevent-sleep active-turn check failed", exc_info=True)
        return False


def _register_prevent_sleep_shutdown(app: web.Application, state: DashboardState) -> None:
    """Register the on_cleanup hook that cancels the prevent-sleep poll and
    releases the OS block.

    MUST be called BEFORE ``runner.setup()`` freezes the app's signal lists. The
    inhibitor and task are created after setup (by :func:`_arm_prevent_sleep_poll`)
    and resolved here lazily via ``getattr``. Shared by both ``start_dashboard``
    and the headless ``start_api_server`` (``--slack-only``) so a graceful stop
    never leaves caffeinate / systemd-inhibit / the Windows execution-state
    request dangling, in either mode.
    """

    async def _prevent_sleep_shutdown(app_: web.Application) -> None:
        task = getattr(state, "_prevent_sleep_task", None)
        if task is not None:
            task.cancel()
        inhibitor = getattr(state, "_sleep_inhibitor", None)
        if inhibitor is not None:
            try:
                inhibitor.set_active(False)
            except Exception:
                logger.debug("prevent-sleep release on shutdown failed", exc_info=True)

    app.on_cleanup.append(_prevent_sleep_shutdown)


def _arm_prevent_sleep_poll(state: DashboardState, port: int) -> None:
    """Create the sleep inhibitor and start its poll task on the running loop.

    *port* is the port this server actually bound, needed because one of the two
    awake reasons is "``tailscale serve`` is fronting this dashboard" — a question
    that can only be asked about a specific port. It is the bound port rather than
    the configured one for the same reason ``kirocrew tailnet up`` insists on
    evidence: if the configured port was occupied the gateway moved, and asking
    about the wrong port would report someone else's serve mapping as ours.

    Keeps the host awake while any session has a turn in flight, but only when
    the user opted in via ``dashboard.prevent_sleep``. Decoupled from the turn
    paths on purpose: polling the same active-turn signal the shutdown drain
    filters on covers every surface (dashboard, Slack, CLI, task runner, and
    sub-agents running under a parent turn) without threading acquire/release
    through each path.

    MUST be called AFTER ``runner.setup()`` (it needs a running loop), and paired
    with :func:`_register_prevent_sleep_shutdown` (registered before setup) for
    release. Shared by both server entrypoints so headless ``--slack-only`` mode
    keeps the host awake identically to the full dashboard — a long Slack task
    on a laptop is the case this feature exists for.
    """
    inhibitor = SleepInhibitor()
    state._sleep_inhibitor = inhibitor  # prevent GC; released on cleanup

    async def _prevent_sleep_poll() -> None:
        try:
            while True:
                await asyncio.sleep(_PREVENT_SLEEP_POLL_INTERVAL_SECS)
                try:
                    inhibitor.set_active(await _should_prevent_sleep(state, port))
                except Exception:
                    logger.debug("prevent-sleep poll toggle failed", exc_info=True)
        except asyncio.CancelledError:
            # Release the OS block before propagating so a cancel (shutdown)
            # never leaves the machine unable to sleep.
            inhibitor.set_active(False)
            raise

    def _prevent_sleep_done(task: "asyncio.Task") -> None:  # type: ignore[type-arg]
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("prevent-sleep poll task exited unexpectedly", exc_info=exc)

    task = asyncio.create_task(_prevent_sleep_poll())
    task.add_done_callback(_prevent_sleep_done)
    state._prevent_sleep_task = task  # prevent GC; cancelled on cleanup
