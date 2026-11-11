"""Apply a narrowed ``bind_address`` policy to a gateway that is already listening.

The bind address is asked when the gateway binds (``urls.bind_address_for``), so a live
refresh that refuses it reaches nothing already listening. :func:`restart_if_bind_refused`
is the post-install hook that applies it: the gateway restarts through the in-app restart,
and the new process binds what the new ceiling permits. When that restart cannot run, the
gateway stops instead of serving on the refused address. Narrowing only: a widened policy
waits for the next start.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew import shutdown_event, update_ownership
from kiro_crew.dashboard.urls import bind_permitted

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Process exit status after a stop for a refused bind address. Non-zero so a supervisor
#: relaunches the gateway, and its own value (``EX_NOPERM`` from ``sysexits.h``) so a
#: post-mortem tells it from the other self-initiated stops (69, 72, 75).
BIND_REFUSED_EXIT_CODE = 77

#: Seconds between checks of whether an update step or a restart still owns the process
#: while a refusal waits for it.
_OWNER_RECHECK_SECS = 1.0


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
        "bind address %s is not permitted by governance (bind_address); restarting the gateway",
        bound,
    )
    loop.call_soon_threadsafe(_start_restart, state)


def _start_restart(state: DashboardState) -> None:
    task = asyncio.ensure_future(_restart_or_stop(state))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


async def _restart_or_stop(state: DashboardState) -> None:
    # The facade is the path the restart's callers import and tests patch.
    from kiro_crew.dashboard.handlers import updates

    # While a restart or an update step owns the process, wait here for it to let go, not
    # for a later poll: polling stops for good once the ceiling names no source. A restart
    # that execs ends this task with the process; one that lets go without it comes back.
    while True:
        while not shutdown_event.is_set() and (
            state._gateway_restart_in_progress or update_ownership.current_owner() is not None
        ):
            await asyncio.sleep(_OWNER_RECHECK_SECS)
        # A pure read: the ceiling may permit the address again by now.
        if shutdown_event.is_set() or bind_permitted(state._bind_asked):
            return
        refusal = update_ownership.restart_refusal()
        if refusal is None:
            target_refusal = (await updates._resolve_restart_target())[2]
            if target_refusal is None:
                if await updates._restart_gateway(state):
                    return
                continue
            refusal = target_refusal[1]
        break
    logger.critical(
        "the gateway cannot restart (%s); stopping it rather than keep listening on an "
        "address the bind_address policy refuses",
        refusal,
    )
    state._bind_refused_stop = True
    shutdown_event.set()


def bind_refused_exit_code(state: DashboardState | None) -> int:
    """:data:`BIND_REFUSED_EXIT_CODE` once the gateway stopped itself for a refused bind."""
    return BIND_REFUSED_EXIT_CODE if state is not None and state._bind_refused_stop else 0
