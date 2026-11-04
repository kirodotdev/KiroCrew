"""A person's answer to an approval tells the work ledger; the host's own decline does not.

``_on_permission_prompt`` (chat_turn/permission_prompt.py) calls
``work_ledger.note_approval_answered`` for the slot exactly where it releases
the approval hold: on an answer no host route made. That is what lets a worker's
"waits on an approval" item stop reading "needs you" once the person approves in the
worker's chat (``test_work_ledger_approval_answered`` pins the ledger half). An
approval the gateway declined itself, because nobody answered in time, is not a
person's answer and records nothing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from dashboard_approval_helpers import (
    _answer_approval,
    _complete_event,
    _context_builder,
    _drain,
    _make_slot,
    _make_state,
    _patch_stats,
    _permission_event,
    _set_stream,
)

from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.handlers import work_ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approved", "rejected_once"])
async def test_a_person_answer_is_noted_for_the_worker_slot(tmp_path, outcome):
    state, client = _make_state(tmp_path, context_builder=_context_builder())
    slot = _make_slot("chat-1-worker")
    _set_stream(client, [_permission_event(), _complete_event()])
    noted = MagicMock()

    answer = asyncio.create_task(_answer_approval(slot, "req-1", outcome))
    try:
        with (
            _patch_stats(),
            patch.object(work_ledger, "note_approval_answered", noted),
        ):
            await _run_chat(state, slot, "hello")
    finally:
        await _drain(answer)

    noted.assert_called_once_with(state, "chat-1-worker")


@pytest.mark.asyncio
async def test_an_approval_nobody_answered_is_not_noted(tmp_path):
    state, client = _make_state(tmp_path, context_builder=_context_builder())
    # The host declines at once: no window left to wait in.
    state.approval_timeout_for = MagicMock(return_value=0)
    slot = _make_slot("chat-1-worker")
    _set_stream(client, [_permission_event(), _complete_event()])
    noted = MagicMock()

    with _patch_stats(), patch.object(work_ledger, "note_approval_answered", noted):
        await _run_chat(state, slot, "hello")

    noted.assert_not_called()
