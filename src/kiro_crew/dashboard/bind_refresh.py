"""Apply a narrowed ``bind_address`` policy to a gateway that is already listening.

The bind address is asked when the gateway binds (``urls.bind_address_for``), so a live
refresh that refuses it reaches nothing already listening. :func:`restart_if_bind_refused`
is the post-install hook that applies it: one task per gateway waits until the gateway is
idle (the shared quiescence census, and no restart or update step owning the process),
checking again on a capped backoff, then restarts through the in-app restart so the new
process binds what the new ceiling permits. It never forces that restart, so the gateway
keeps serving the refused address while work runs. When, once idle, the restart cannot
run, the gateway stops instead. Narrowing only: a widened policy waits for the next start.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew import quiescence, shutdown_event, update_ownership
from kiro_crew.dashboard.urls import bind_permitted

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Process exit status after a stop for a refused bind address. Non-zero so a supervisor
#: relaunches the gateway, and its own value (``EX_NOPERM`` from ``sysexits.h``) so a
#: post-mortem tells it from the other self-initiated stops (69, 72, 75).
BIND_REFUSED_EXIT_CODE = 77

#: The first wait before a refusal checks again for an idle gateway; each later wait
#: doubles, up to :data:`_RETRY_MAX_SECS`.
_RETRY_FIRST_SECS = 10.0
_RETRY_MAX_SECS = 60.0

# The sleep, named so a test can record the waits instead of sleeping.
_sleep = asyncio.sleep


def restart_if_bind_refused(state: DashboardState, loop: asyncio.AbstractEventLoop) -> None:
    """Restart the gateway when the installed ceiling refuses the address it listens on.

    The address asked is the one the primary listener's bind asked, IPv6 zone included,
    once that listener's address is published. Runs on the refresher thread on every
    confirming poll, so the first ask is a pure read; only a refusal is asked again with
    an audit row. The ``127.0.0.1`` floor is never asked, and an evaluation error
    refuses, as at bind time. The restart itself runs on *loop*.
    """
    if "primary" not in state._listener_sidecars:
        return
    bound = state._bind_asked
    if bind_permitted(bound) or bind_permitted(bound, audit_tool="gateway_bind"):
        return
    logger.warning(
        "bind address %s is not permitted by governance (bind_address); "
        "restarting the gateway once it is idle",
        bound,
    )
    loop.call_soon_threadsafe(_start_restart, state)


def _start_restart(state: DashboardState) -> None:
    # On the loop (handed over by ``call_soon_threadsafe``), so check-and-set cannot race:
    # every later confirming poll finds the pending task and starts no second one.
    pending = state._bind_restart_task
    if pending is not None and not pending.done():
        return
    task = asyncio.ensure_future(_restart_or_stop(state))
    state._bind_restart_task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


async def _restart_or_stop(state: DashboardState) -> None:
    # The facade is the path the restart's callers import and tests patch.
    from kiro_crew.dashboard.handlers import updates

    # Wait here, not for a later poll: polling stops for good once the ceiling names no
    # source. Never forced, so while work runs the gateway keeps serving.
    delay = _RETRY_FIRST_SECS
    while True:
        # A pure read: the ceiling may permit the address again by now.
        if shutdown_event.is_set() or bind_permitted(state._bind_asked):
            return
        if _idle(state):
            refusal = update_ownership.restart_refusal()
            if refusal is None:
                target_refusal = (await updates._resolve_restart_target())[2]
                if target_refusal is None:
                    # A restart that execs ends this task with the process; a declined
                    # one (a race, a coalesced restart) comes back to the backoff.
                    if await updates._restart_gateway(state):
                        return
                # Work admitted during that await is not cut: stop only on reads after it.
                elif (
                    not shutdown_event.is_set()
                    and not bind_permitted(state._bind_asked)
                    and _idle(state)
                ):
                    refusal = target_refusal[1]
            if refusal is not None:
                break
        await _sleep(delay)
        delay = min(2 * delay, _RETRY_MAX_SECS)
    logger.critical(
        "the gateway cannot restart (%s); stopping it rather than keep listening on an "
        "address the bind_address policy refuses",
        refusal,
    )
    state._bind_refused_stop = True
    shutdown_event.set()


def _idle(state: DashboardState) -> bool:
    """No restart or update step owns the process and the census reads quiescent."""
    from kiro_crew.dashboard.handlers import updates

    if state._gateway_restart_in_progress or update_ownership.current_owner() is not None:
        return False
    # Unwired or failed reads ``None``: not idle.
    census = updates._quiescence_of(state, quiet_window_secs=quiescence.QUIET_WINDOW_SECS)
    return census is not None and census.quiescent


def bind_refused_exit_code(state: DashboardState | None) -> int:
    """:data:`BIND_REFUSED_EXIT_CODE` once the gateway stopped itself for a refused bind."""
    return BIND_REFUSED_EXIT_CODE if state is not None and state._bind_refused_stop else 0
