"""The wait a spawn takes for the kiro-cli floor's verdict on a defaulted KAS.

With no ``agent.acp_backend`` stored, the main backend resolves to KAS; with no
``agent.member_acp_backend`` stored, crew-member DMs do. The gateway checks whether
this kiro-cli can serve KAS (``apply_kas_cli_floor``) in the background, so boot
never waits on its ``acp --help`` probe. A spawn that ran before that verdict landed
would start KAS on a kiro-cli that rejects ``--agent-engine``, and the user's first
turn would fail. The gateway arms this gate from the resolved in-memory backends,
then reads declarations off-loop and releases explicit choices before probing.
Each async spawn path awaits :func:`await_kas_floor_verdict` for its role before it
calls the provider factory. The factory reads whatever the verdict decided.

An explicit KAS choice waits only for the declaration read, never for the probe.
The wait is bounded by the probe's own timeout, counted from when the gate was
armed, so a hung check costs at most one probe's worth of delay in total. After
the deadline, or when the spawn's role is not armed, the spawn goes ahead at once.

A caller that builds its provider on its own thread and event loop calls
:func:`wait_kas_floor_verdict_blocking` instead, off the gateway loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from kiro_crew.kiro_cli import _KAS_HELP_PROBE_TIMEOUT_SECS

#: The longest a spawn waits for the verdict, measured from the arm time.
KAS_FLOOR_WAIT_SECS: float = float(_KAS_HELP_PROBE_TIMEOUT_SECS)

#: The armed verdict, its loop-time deadline and main / member early-release signals.
_armed: (
    tuple[asyncio.Future[Any], float, asyncio.Future[None] | None, asyncio.Future[None] | None]
    | None
) = None


def arm_kas_floor_wait(verdict: asyncio.Future[Any], *, main: bool, member: bool) -> None:
    """Make *main* and/or *member* spawns wait for *verdict*. Call it on the loop."""
    global _armed
    if not (main or member):
        _armed = None
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + KAS_FLOOR_WAIT_SECS
    _armed = (
        verdict,
        deadline,
        loop.create_future() if main else None,
        loop.create_future() if member else None,
    )


def release_kas_floor_wait(verdict: asyncio.Future[Any], *, main: bool, member: bool) -> None:
    """Release explicit choices on the loop without changing the floor's deadline.

    A replaced gate belongs to another floor task and must not be released here.
    """
    armed = _armed
    if armed is None or armed[0] is not verdict:
        return
    for release, signal in ((main, armed[2]), (member, armed[3])):
        if release and signal is not None and not signal.done():
            signal.set_result(None)


async def await_kas_floor_verdict(*, member: bool = False) -> None:
    """Wait, bounded, for an armed floor verdict that has not landed yet.

    *member* says the spawn is a crew-member DM, which runs ``member_acp_backend``.
    Returns at once when that role is not armed or the verdict already landed.
    Never raises for the verdict: a failed or timed-out check leaves the backend as
    configured, which is what the floor itself does on an unknown answer.
    """
    armed = _armed
    if armed is None:
        return
    verdict, deadline, main_release, member_release = armed
    released = member_release if member else main_release
    if released is None or released.done():
        return
    loop = asyncio.get_running_loop()
    # A verdict armed on another loop (an earlier gateway in the same process)
    # cannot be awaited here, and was never this spawn's to wait for.
    if verdict.done() or verdict.get_loop() is not loop:
        return
    remaining = deadline - loop.time()
    if remaining <= 0:
        return
    # ``asyncio.wait`` neither cancels the verdict on timeout nor raises its
    # exception, so one waiter giving up never touches the floor's own task.
    await asyncio.wait({verdict, released}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)


def wait_kas_floor_verdict_blocking() -> None:
    """:func:`await_kas_floor_verdict` for a caller off the gateway loop. Blocks.

    For code that builds a provider on its own thread or event loop, which the
    gateway-bound gate cannot hold. Runs the same bounded wait on the loop that
    armed the gate and blocks this thread until it returns. Returns at once when
    nothing is armed, that loop is not running, or the caller is ON that loop
    (blocking there would stall the verdict it waits for). Waits for the main
    role: no off-loop caller runs a crew-member DM.
    """
    armed = _armed
    if armed is None:
        return
    loop = armed[0].get_loop()
    try:
        running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop or loop.is_closed() or not loop.is_running():
        return
    try:
        waiter = asyncio.run_coroutine_threadsafe(await_kas_floor_verdict(), loop)
    except RuntimeError:
        return  # the loop closed between the check and the submit
    try:
        # The coroutine ends at the gate's own deadline; this bound is a backstop.
        waiter.result(timeout=KAS_FLOOR_WAIT_SECS)
    except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
        waiter.cancel()
