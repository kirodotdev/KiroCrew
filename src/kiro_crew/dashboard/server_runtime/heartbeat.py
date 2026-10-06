"""The event-loop heartbeat and its off-loop stall watchdog.

The crash-dump file and exit budget the watchdog is armed with, the 5-second heartbeat
that beats it, the prior session's crash dump reported once, and the watchdog's
shutdown.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        LoopStallWatchdog,
        attribute_dump,
        claim_dump_notification,
        data_home,
        describe,
        dump_age_seconds,
        dump_replay_lines,
        load_loop_stall_exit_after,
        logger,
        newest_dump_with_stacks,
        open_dump_file,
        platform_compat,
        resolve_loop_stall_exit_after,
        rotate_dumps,
        sweep_stale_dumps,
    )


def _register_watchdog_shutdown(app: web.Application, state: DashboardState) -> None:
    """Register the cleanup hook that stops the loop stall watchdog.

    Called before ``runner.setup()`` freezes the app's signal lists (appending
    after setup raises "Cannot modify frozen list"). The watchdog itself is
    created after ``runner.setup()`` and stored on ``state._loop_watchdog``; this
    hook only fires at shutdown — long after that assignment — so the lazy
    ``getattr`` always resolves it.
    """

    async def _watchdog_shutdown(app_: web.Application) -> None:
        wd = getattr(state, "_loop_watchdog", None)
        if wd is not None:
            wd.stop()

    app.on_cleanup.append(_watchdog_shutdown)


async def _open_loop_watchdog(_launch_environment: dict[str, str]) -> LoopStallWatchdog:
    """The loop stall watchdog, armed with a fresh crash-dump file and its exit budget.

    Crash-dump discoverability: route dumps to a dedicated file under
    ~/.kiro/crew/logs/crash-dumps/ so they are findable via `kirocrew doctor`
    and startup warnings, rather than buried in interleaved stderr/journal.
    Crash-dump hygiene: sweep header-only dumps left by prior sessions that
    exited without ever wedging (every startup pre-creates one for
    faulthandler's fd), THEN rotate. Sweeping first keeps empty startup files
    from aging real stall dumps out of the rotation window.
    """
    await asyncio.to_thread(sweep_stale_dumps)
    await asyncio.to_thread(rotate_dumps)
    _dump_file = await asyncio.to_thread(open_dump_file)
    # exit_after is configurable because the right budget is host-dependent: a
    # gateway doing heavy subprocess work (long builds, test suites, bursts of
    # child reaping) can wedge the loop briefly without being genuinely dead,
    # and a hard-coded 25s turned those into hard exits that lost in-flight
    # work. The default is unchanged; the loader clamps the range.
    try:
        _exit_after = float(load_loop_stall_exit_after(_launch_environment))
    except Exception:
        logger.debug("loop-stall exit budget config unavailable; using default", exc_info=True)
        # Config failure must not erase the managed-service grace that protects
        # the process while its config filesystem is itself under pressure.
        _exit_after = float(resolve_loop_stall_exit_after(environ=_launch_environment))
    return LoopStallWatchdog(dump_file=_dump_file, exit_after=_exit_after)


def _start_loop_heartbeat(
    state: DashboardState,
    _loop_watchdog: LoopStallWatchdog,
    _heap_trim_maintainer: platform_compat.HeapTrimMaintainer,
) -> asyncio.Task[None]:
    """Start the event-loop heartbeat that beats *_loop_watchdog*; return its task.

    Event-loop heartbeat: proves the asyncio loop is live (the off-loop /proc
    sampler can't — it runs in a subprocess). Sleeps 5s, then logs actual
    elapsed. If the loop wedges (e.g. a coroutine blocks it), this task can't
    be scheduled, so the log goes SILENT during the stall and the first tick
    after recovery reports a lag >> 5s — that gap IS the wedge, measured.

    The heartbeat also "beats" an off-loop stall watchdog (a daemon thread).
    The recovery-lag log above only fires if the loop EVER recovers; when it
    wedges permanently the log just goes silent. The watchdog runs on its own
    thread — unaffected by a loop thread blocked in a syscall — and dumps all
    thread stacks via faulthandler once the heartbeat stops beating, so the
    stuck frame lands in the log automatically instead of leaving us to sample
    the PID by hand.
    """

    async def _loop_heartbeat() -> None:
        # 5s (not 10s) so the watchdog's dump-then-exit alarm is re-armed at a
        # finer resolution. The alarm fires exit_after seconds after the LAST
        # beat, so the real silence the gateway tolerates before the exit is
        # ``exit_after - (time since last beat)`` — i.e. up to one interval less
        # than exit_after. A 5s interval keeps that worst case at ~20s (vs ~15s
        # at 10s), so genuinely-recoverable 15-20s stalls are less likely to be
        # ended while still landing well under the Electron probe's kill window.
        # The alarm pauses while the host sleeps and this loop's clock does not
        # advance either, so a laptop resume is not silence to the watchdog.
        interval = 5.0
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(interval)
            lag = time.monotonic() - t0 - interval
            # Claimed before beat(): check() holds its own capture flag until a beat.
            capture_lag = _loop_watchdog.claim_lag_enrichment(lag)
            _loop_watchdog.beat()
            # Resource-pressure notifications ride the heartbeat cadence
            # rather than owning a task: the notifier self-gates to its own
            # sample interval, never raises, and off-loads its synchronous
            # probe to a worker thread so a slow config filesystem cannot
            # block the loop this heartbeat exists to watch. After the lag
            # read so the await can't register as loop lag.
            await state.resource_pressure_notifier.maybe_sample()
            released = await _heap_trim_maintainer.maybe_trim()
            if released >= platform_compat.HEAP_TRIM_LOG_THRESHOLD_BYTES:
                logger.info(
                    "Gateway heap trim returned %.0f MiB to the OS",
                    released / (1024 * 1024),
                )
            if lag > 1.0:
                logger.warning("event-loop heartbeat: lag %.1fs (loop was blocked)", lag)
            else:
                # Healthy ticks are DEBUG: at the default WARNING level the loop
                # stays silent unless it actually wedges (the tripwire), and we
                # don't emit ~8.6k INFO lines/day when DEBUG is enabled.
                logger.debug("event-loop heartbeat ok (lag %.2fs)", lag)
            if capture_lag:
                # Bounded like the probes above so a busy executor cannot starve
                # beat(); shielded so the capture still clears its in-flight flag.
                try:
                    capture = asyncio.get_running_loop().run_in_executor(
                        None, _loop_watchdog.log_lag_enrichment, lag
                    )
                    await asyncio.wait_for(asyncio.shield(capture), 2.0)
                except Exception:
                    logger.debug("heartbeat lag capture not awaited", exc_info=True)

    def _heartbeat_done(task: "asyncio.Task") -> None:  # type: ignore[type-arg]
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("event-loop heartbeat task exited unexpectedly", exc_info=exc)

    _hb = asyncio.create_task(_loop_heartbeat())
    _hb.add_done_callback(_heartbeat_done)
    return _hb


async def _report_prior_crash_dump(state: DashboardState) -> None:
    """Report, once, the newest crash dump a previous gateway session left behind.

    The armed dump-then-exit path (exit_after=25s) writes ONLY to the dedicated
    file — not stderr/journal — because faulthandler.dump_traceback_later targets
    a single fd. To ensure journal-only operators (containers) still see the stacks,
    we replay the dump content into the logger on next startup.
    """
    _prior_dump = await asyncio.to_thread(newest_dump_with_stacks)
    if _prior_dump is not None:
        _age_h = await asyncio.to_thread(dump_age_seconds, _prior_dump) / 3600
        # One stall is reported once, on the first start after it, across every
        # surface below. A dump stays on disk for a week and is re-detected on
        # every start, so an unclaimed warning-and-replay prints the same thread
        # stacks at every boot for that week — and a reader cannot tell that log
        # from a gateway wedging right now, which is the only reason to print it
        # at all. The claim is the same idempotency key the notification uses, so
        # the log line, the replay and the notification agree on what has already
        # been reported; the dump stays on disk for `kirocrew doctor` to show on
        # demand.
        if _age_h < 168 and await asyncio.to_thread(claim_dump_notification, _prior_dump):
            logger.warning(
                "⚠️  Prior loop-stall crash dump found: %s (%.1f hours ago). "
                "Run `kirocrew doctor` for details.",
                _prior_dump,
                _age_h,
            )
            # Replay stack content to journal so container/journal-only operators
            # can see it without accessing the file system.
            _replay_lines, _truncated = await asyncio.to_thread(dump_replay_lines, _prior_dump)
            if _replay_lines:
                _replay_body = "\n".join(_replay_lines)
                if _truncated:
                    _replay_body += "\n  [truncated — full dump at above path]"
                logger.warning("Replaying prior crash dump stacks:\n%s", _replay_body)
            # A log line is not enough. This dump means the previous gateway
            # exited by hard-exit: no `finally` ran, nothing was flushed, and any
            # turn in flight lost work that was written but not yet committed.
            # The user needs to know that happened rather than discovering a
            # monitoring loop had silently stopped hours earlier.
            # Say who the loop was working for, from the same evidence the
            # doctor reads, so the person restarting knows which job to look
            # at without opening the dump.
            try:
                _attr_lines = describe(
                    await asyncio.to_thread(attribute_dump, _prior_dump, data_home())
                )
            except Exception:
                logger.debug("stall attribution for notification failed", exc_info=True)
                _attr_lines = []
            try:
                state.notify(
                    "heartbeat",
                    "⚠️ Gateway restarted after an event-loop stall",
                    (
                        f"The previous gateway stopped responding and exited "
                        f"{_age_h:.1f}h ago, then restarted. Work in flight at "
                        f"that moment was interrupted and not saved. "
                        + ("".join(f"{ln}. " for ln in _attr_lines))
                        + f"Thread stacks: {_prior_dump}"
                    ),
                    meta={"url": "/settings", "dump": str(_prior_dump)},
                )
            except Exception:
                logger.debug("stall-exit notification failed", exc_info=True)
