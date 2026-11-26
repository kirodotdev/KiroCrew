"""Send a KAS start's configured model, and its effort, while the start's MCP wait runs.

KAS connects a new session's MCP servers right after ``session/set_mode``, and while
it does, its event loop answers every request late: a ``session/set_config_option``
sent once that wait is over can take longer than its 10 s bound, so the start pays
the bound in full and the session is recorded on the wrong model. Sent from
``create_session``'s ``on_mode_set`` instead, the write is answered during time the
start already spends waiting, and ``AcpSessionHandle.set_config_option`` keeps
waiting for that answer until the wait ends.

The pin is judged against the session's own ``model`` select, as ``set_model``
judges every concrete pin, so the task first waits for that select
(:func:`~kiro_crew.acp.session_handle_model_selection.kas_model_select_known`). From
engine 0.66.117 ``session/new`` answers without it and a ``config_option_update``
carries it a moment later, which the MCP wait's drain stores
(``AcpSessionHandle.kas_select_seen``). A select that has not come by the time the MCP
wait ends sends nothing early, and the start sends the pin after the wait as before.
The served-default switch comes after the pin's write: until the task's own switch
ends, the MCP wait leaves that check to the task (``served_default_after_pin``),
which makes it once the write ends, so the switch never lands on top of the pin and
is never sent twice. The MCP wait never waits for the pin, since the pin's answer may
wait for the MCP wait's end. The effort follows the model's answer at once, since KAS
offers its ``effortLevel`` select only once a concrete model is set.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from kiro_crew.acp.client import DEFAULT_MODEL
from kiro_crew.acp.session_handle_model_selection import (
    START_WAIT_END,
    apply_configured_model,
    kas_model_select_known,
    kas_switch_off_unserved_auto,
)
from kiro_crew.acp.types import ACP_BACKEND_KAS

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.providers.acp import AcpProvider

#: What an early pin's task returns: whether the pin was handled early (sent, or
#: refused before the wire), and the ``time.monotonic()`` its write ended, ``None``
#: when nothing was written.
EarlyPin = tuple[bool, float | None]


def start_pin_early(
    provider: AcpProvider,
    configured_model: str,
    tasks: list[asyncio.Task[EarlyPin]],
    handle: AcpSessionHandle,
) -> None:
    """``create_session``'s ``on_mode_set`` for a KAS start: start the pin's task.

    Appends the task to *tasks*, which the start awaits with
    :func:`finish_early_pin` once ``create_session`` returns, or drops with
    :func:`drop_early_pin` when it fails. Starts nothing for an unpinned start.
    """
    if not configured_model or configured_model == DEFAULT_MODEL:
        return
    handle.served_default_after_pin = True
    tasks.append(asyncio.ensure_future(_pin_then_effort(provider, handle, configured_model)))


async def _select_known(handle: AcpSessionHandle) -> bool:
    """Wait for *handle*'s ``model`` select, until the start's MCP wait ends at most."""
    if kas_model_select_known(handle):
        return True
    start_wait_end = START_WAIT_END.get()
    if start_wait_end is None:
        return False
    waiters = {
        asyncio.ensure_future(handle.kas_select_seen.wait()),
        asyncio.ensure_future(start_wait_end.wait()),
    }
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
    return kas_model_select_known(handle)


async def _pin_then_effort(
    provider: AcpProvider, handle: AcpSessionHandle, configured_model: str
) -> EarlyPin:
    """Apply the pin once the select is known, then the start's effort on the session.

    Between the two it makes the served-default check the MCP wait left to it, when
    the pin did not land: the session then sits on a default the account may not be
    served. A pin that landed leaves nothing to judge.
    """
    try:
        handled = await _select_known(handle)
        write_ms = (
            await apply_configured_model(handle, configured_model, backend=ACP_BACKEND_KAS)
            if handled
            else None
        )
        # Still deferred, so the MCP wait's own check cannot send this switch too.
        if handle.model in ("", DEFAULT_MODEL):
            refused = handle.model_pin_refused
            await kas_switch_off_unserved_auto(handle)
            # The switch's own ``set_model`` clears the refusal this pin recorded.
            handle.model_pin_refused = handle.model_pin_refused or refused
    finally:
        handle.served_default_after_pin = False
    if not handled:
        return False, None
    if write_ms is None:
        return True, None
    pin_done = time.monotonic()
    # ``_apply_initial_effort`` logs and swallows its own failures.
    await provider._apply_initial_effort(client=handle)
    provider._start_effort_applied = True
    return True, pin_done


async def finish_early_pin(tasks: list[asyncio.Task[EarlyPin]]) -> tuple[bool, float | None]:
    """Wait for the early pin, if one started: ``(handled early, ms the start waited)``.

    ``False`` when no pin started, or its select never came during the MCP wait:
    the start then sends the pin itself. The milliseconds count only the part of
    the pin's write that ran after ``create_session`` returned, the time the pin
    added to the start, and not the effort write that follows it; ``None`` when no
    pin was written.
    """
    if not tasks:
        return False, None
    waited = time.monotonic()
    handled, pin_done = await tasks[0]
    if not handled:
        return False, None
    return True, None if pin_done is None else max(0.0, pin_done - waited) * 1000.0


async def drop_early_pin(tasks: list[asyncio.Task[EarlyPin]]) -> None:
    """Cancel an early pin whose start failed, and wait for it to stop.

    ``asyncio.wait`` re-raises neither the task's cancellation nor its error, so a
    cancel of the start itself still reaches the caller.
    """
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.wait(tasks)
    for task in tasks:
        if not task.cancelled():
            task.exception()
