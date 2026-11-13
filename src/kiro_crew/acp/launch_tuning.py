"""Moving a running session to another model or effort on a harness that takes both at launch.

A member of :data:`~kiro_crew.agent_sdk.backends.ACP_BACKENDS_MODEL_AT_LAUNCH` (or
:data:`~kiro_crew.agent_sdk.backends.ACP_BACKENDS_EFFORT_AT_LAUNCH`) reads its model
(effort) from its argv when it starts and offers no ACP request that changes it. So a
switch replaces the process, between turns: the client records the new value, which
the harness's launch reads (``_model``, ``_launch_effort``), and
:func:`kiro_crew.acp.relaunch_reload.relaunch_and_reload` ends the process, starts one
that reads it and ``session/load``s the same session id there. It runs now, so the
switch lands or fails here rather than on the person's next message.

The harness owns the session record, so the reloaded session carries the whole
conversation. A relaunch that raised, or that opened another session, is not a
switch: the prior values come back, and the helper leaves the load owed, so the next
``ensure_ready`` reopens the conversation on them.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import TYPE_CHECKING, Any

from kiro_crew import model_registry, model_scope
from kiro_crew.acp.relaunch_reload import SessionNotReloadedError, relaunch_and_reload
from kiro_crew.acp.runtime_models import DEFAULT_MODEL
from kiro_crew.acp.transport_errors import AcpError, AcpModelUnavailable
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_EFFORT_AT_LAUNCH,
    ACP_BACKENDS_MODEL_AT_LAUNCH,
)

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

__all__ = [
    "switch_barrier",
    "switch_launch_model",
    "switch_launch_tuning",
]

logger = logging.getLogger(__name__)

#: What a switch fails with when the relaunched process opened another session.
_NOT_REOPENED_TEXT = (
    "The agent restarted on the new setting but could not reopen this chat, so the "
    "setting was not changed. Send your message again to continue on the previous one."
)

#: What a switch fails with while an earlier relaunch still owes this chat its reopen.
_REOPEN_FIRST_TEXT = (
    "This chat has to be reopened before the setting can change, so it was not changed. "
    "Send a message to reopen the chat, then pick the setting again."
)


def switch_barrier(client: AcpClient) -> AbstractAsyncContextManager[Any]:
    """What a turn's ``ensure_ready`` holds, so it never runs inside a switch's relaunch.

    The switch holds ``_launch_tuning_lock`` across its relaunch. A send or command
    that reaches ``ensure_ready`` meanwhile waits for it, and then runs on the
    relaunched process; a switch that wakes after the send cleared ``_turn_done``
    refuses. Only a member of the launch-time sets takes it: every other harness's
    start is unchanged (harness-parity H13).
    """
    if client.backend in ACP_BACKENDS_MODEL_AT_LAUNCH or (
        client.backend in ACP_BACKENDS_EFFORT_AT_LAUNCH
    ):
        lock = getattr(client, "_launch_tuning_lock", None)
        if lock is not None:
            return lock
    return nullcontext()


def _foreign_to(client: AcpClient, model_id: str) -> bool:
    """Whether *model_id* belongs to another harness's catalog, not *client*'s."""
    if not model_id or model_id == DEFAULT_MODEL:
        return False
    if client.backend == ACP_BACKEND_STRANDS:
        # Lazy: the harness module imports the client, which imports this module.
        from kiro_crew.acp.harness.strands import strands_model_is_foreign

        return strands_model_is_foreign(model_id)
    return bool(model_scope.foreign_namespaces(model_id, client._model_registry_namespace))


def _same_model(a: str, b: str) -> bool:
    """Whether two pins launch the same model: ``auto`` and no pin are one value."""
    return (a or DEFAULT_MODEL) == (b or DEFAULT_MODEL)


async def switch_launch_tuning(
    client: AcpClient, *, model: str | None = None, effort: str | None = None
) -> bool:
    """Relaunch *client*'s process on *model* and/or *effort* and reload its session.

    ``None`` keeps a value. Nothing changes when both already hold (``False``);
    ``True`` once the replacement serves the same session. Raises
    :exc:`AcpError` when there is no session yet, while a turn is in flight (a caller
    applies a pick at the turn boundary), and when the replacement process failed to
    start or opened a different session; the prior values are back in force then.
    """
    lock: asyncio.Lock | None = getattr(client, "_launch_tuning_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        client._launch_tuning_lock = lock
    # Held across the relaunch; the provider's prompt boundary takes it around its own
    # ensure_ready, so a message sent during a switch waits for the switch to land.
    async with lock:
        if not client._session_id:
            raise AcpError("Cannot change the model before the session is initialized")
        target_model = client._model if model is None else model
        target_effort = client._launch_effort if effort is None else effort
        if _same_model(target_model, client._model) and target_effort == client._launch_effort:
            return False
        owed = getattr(client, "_relaunch_owed_session", "")
        if owed and client._session_id != owed:
            # The live session is not the conversation an earlier relaunch still
            # owes: relaunching from it would load the wrong one and settle the
            # debt. The next turn reopens the chat first. When the helper has put
            # the owed id back (a start that failed), the relaunch below loads that
            # very id, so a fallback walk's next candidate may still try.
            raise AcpError(_REOPEN_FIRST_TEXT, transient=False, user_worded=True)
        if not client._turn_done.is_set():
            # The process answers this turn's prompt; replacing it would cut the turn.
            raise AcpError(
                "The model or effort can change only between turns on this harness.",
                transient=False,
                user_worded=True,
            )
        session_id = client._session_id
        prior_model, prior_effort = client._model, client._launch_effort
        client._model, client._launch_effort = target_model, target_effort
        try:
            await relaunch_and_reload(client)
        except SessionNotReloadedError as exc:
            client._model, client._launch_effort = prior_model, prior_effort
            # A process started but serves a fresh session, which is not this chat.
            raise AcpError(_NOT_REOPENED_TEXT, transient=False, user_worded=True) from exc
        except BaseException:
            # A start that failed keeps its own remedy (a refused id, a missing CLI).
            client._model, client._launch_effort = prior_model, prior_effort
            raise
    logger.info(
        "%s relaunched for session %s (model=%s effort=%s)",
        client.backend,
        session_id,
        target_model or DEFAULT_MODEL,
        target_effort or "default",
    )
    return True


async def switch_launch_model(
    client: AcpClient, model_id: str, *, effort: str | None = None
) -> None:
    """``set_model`` for a model-at-launch harness: refuse a foreign id, else relaunch.

    An id another harness's catalog owns (a kiro-cli id picked before switching
    harness, a fallback chain entry) is never launched on: the Strands launch drops it
    and would run its default while the chat named the foreign model. It is refused
    as unavailable, so a picker answers 4xx and a fallback walk skips it. *effort*,
    when given, rides the same relaunch.
    """
    if _foreign_to(client, model_id):
        raise AcpModelUnavailable(model_id, [], backend=client.backend)
    if not await switch_launch_tuning(client, model=model_id, effort=effort):
        return
    client.model_pin_refused = ""
    client._last_substitution_model = None
    served = client._resolved_model_id or model_id
    window = (
        model_registry.model_window(served) if model_registry.has_known_window(served) else None
    )
    client.last_prompt_stats.rebase_to_window(window or 0)
