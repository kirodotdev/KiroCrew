"""The bounded session close every in-process restart runs before it ends the process.

The dashboard's ``_restart_gateway``, the update restart
(``GatewayOrchestrator._restart_after_update_claimed``) and the Slack
``/kirocrew restart`` (``_handle_restart``) each close every session with
``SessionManager.close_all()`` and then exec or exit, which is the only thing that
ends the old image. ``close_all`` bounds its turn drain and its provider shutdown,
but it cancels the cleanup task and every owned background task and then awaits
them with no deadline of its own: the hard cap is its caller's. A task that keeps
running after its cancel (a cancel handler blocked on a stalled mount) holds that
await with no end, with admission already shut and the old image still serving.
``asyncio.wait_for`` is no cap against it: at its deadline it cancels the close and
then waits for the close to unwind, which is the same wait.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable

from kiro_crew import gateway_shutdown_budget

logger = logging.getLogger(__name__)


async def close_sessions_for_restart(
    closing: Awaitable[object], timeout: float | None = None
) -> None:
    """Await *closing* (a ``close_all()`` call) for at most *timeout* s; never raise an error.

    *timeout* defaults to ``gateway_shutdown_budget.RESTART_SESSION_CLOSE_SECS``,
    read at call time. The close runs in its own task and only that task is
    waited on, so a step inside it that swallows cancellation cannot extend the
    wait. At the deadline the task is cancelled and left, a warning names the
    step, and the caller goes on to the exec or exit that ends what is left; the
    next start's orphan sweep reaps the children a cut close did not stop. A
    failed close is logged the same way. A cancellation of the caller cancels the
    close too and propagates.
    """
    if timeout is None:
        timeout = gateway_shutdown_budget.RESTART_SESSION_CLOSE_SECS
    loop = asyncio.get_running_loop()
    started = loop.time()
    task = asyncio.ensure_future(closing)
    try:
        await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not task.done():
        task.cancel()
        logger.warning(
            "Restart: closing sessions did not finish in %.1fs; restarting anyway", timeout
        )
    elif task.cancelled():
        logger.warning("Restart: closing sessions was cancelled; restarting anyway")
    elif task.exception() is not None:
        logger.warning(
            "Restart: closing sessions failed; restarting anyway", exc_info=task.exception()
        )
    else:
        logger.info("Restart: sessions closed in %.1fs", loop.time() - started)
