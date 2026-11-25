"""The fixtures-free builders the pending-steer tests share.

A dedicated ``*_helpers.py`` imported by bare name, per the repo convention: no
``test_*`` module imports another. ``test_pending_steer`` and its sibling
``test_pending_steer_*`` modules all send, run and hand off through these.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from session_control_helpers import (
    _key,
    _peer_target,
    _slot,
    _steerable,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_control as sc

TEXT = "stop, the base moved; rebase before you push"
#: How long a test awaits a task it expects to finish; only a hang ever waits it out.
_AWAIT_SECS = 30


def _running(slot):
    """A slot whose turn is in flight, ended by cancelling ``slot.task``."""
    slot.task = asyncio.get_running_loop().create_future()
    return slot


async def _send(state, caller, target="chat-2", message=TEXT):
    return await sc.send_to_target(
        state, caller_session_key=_key(caller), target=target, message=message, steer=True
    )


def _loss_recovery_target(state):
    caller = _slot(state, "chat-1")
    target = _running(_peer_target(state, "chat-2", caller))
    client = _steerable(accepted=True)
    client.steer_needs_loss_recovery = True
    target._acp_client = client
    return caller, target, client


async def _hand_off(state, target):
    """End the target's turn the way ``_hand_off_queue`` does; return the spawn mock."""
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
        patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
    ):
        try:
            await chat_runner._hand_off_queue(
                state, target, drain=True, allow_automatic_successor=False
            )
        finally:
            if target.task is not None:
                target.task.cancel()
    return spawn


async def _send_plain(state, caller, message):
    """A plain ``session_send`` (no steer): queued at the tail of a busy target."""
    return await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message=message, steer=False
    )
