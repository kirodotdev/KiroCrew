"""Telegram's own turn loop restores the turn a Stop cut off, once.

Telegram's ``/stop`` arms ``prev_turn_cancelled`` through ``stop_running_turn``, and
Telegram's turn engine is not :class:`~kiro_crew.messaging.dispatch.ChannelTurns`, so
it must read and clear the flag itself, as Slack does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from telegram_parity_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _drop_live_config_snapshot,
)
from test_telegram import _dispatcher, _dm


class _AnyKey(dict):
    """A session table that answers every key with one record."""

    def __init__(self, session: SimpleNamespace) -> None:
        super().__init__()
        self._session = session

    def get(self, key: Any, default: Any = None) -> Any:
        return self._session


def _stopped(d: Any, sess: Any) -> SimpleNamespace:
    log = MagicMock()
    log.get_metadata.return_value = {}
    log.get_metadata_status.return_value = ({}, True)
    log.read_messages.return_value = [
        {"role": "user", "content": "write the release notes"},
        {"role": "assistant", "content": "Here is a first draft of"},
    ]
    d.ctx_builder.conversation_log = log
    session = SimpleNamespace(prev_turn_cancelled=True)
    sess._sessions = _AnyKey(session)
    return session


@pytest.mark.asyncio
async def test_the_next_telegram_turn_carries_the_stopped_turn_once() -> None:
    d, _, sess = _dispatcher({7})
    session = _stopped(d, sess)

    await d.handle_message(_dm("continue"))

    call = d.ctx_builder.build_calls[-1]
    sent = call["text"]
    assert "write the release notes" in sent and sent.endswith("continue")
    start, end = call["user_text_range"]
    assert sent[start:end] == "continue", "only the typed text is the person's"
    assert session.prev_turn_cancelled is False

    await d.handle_message(_dm("next"))
    assert d.ctx_builder.build_calls[-1]["text"] == "next", "the restore is one-shot"
    assert "user_text_range" not in d.ctx_builder.build_calls[-1]
