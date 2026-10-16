"""Builders shared by the ``test_discord_resumed_busy*.py`` suites.

A DM ``c1`` bound to ``dashboard:chat-1`` whose slot is mid-turn, on the real
``DashboardState`` and slot; a steer-capable ACP client fake; the refusal text every
case either avoids or keeps; and a credential-bearing message for the
display-redaction cases. ``_isolated_home`` is autouse: each suite binds it by name
so pytest registers it for that module.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from test_discord_sessions import _bind_to_chat1, _dispatcher, _log

# The refusal every case in the test_discord_resumed_busy*.py suites either avoids or keeps.
_REFUSAL = "busy with a turn started elsewhere"
# A credential the display redaction rewrites, inside otherwise ordinary text.
_SECRET = "AKIAIOSFODNN7EXAMPLE"
_TYPED = f"use https://docs.example.com/page?key={_SECRET} for this"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse fixture of the test_discord_resumed_busy*.py suites, moved verbatim; none of them calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _steerable(accepted: bool = True) -> MagicMock:
    client = MagicMock()
    client.supports_steer = True
    client.steer_needs_loss_recovery = False
    client.steer = AsyncMock(return_value=accepted)
    return client


def _busy(slot):
    """``running`` is derived from the task, so a busy slot is expressed by one."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


async def _bound_to_busy_dashboard(tmp_path, *, steer_client: MagicMock | None):
    """DM ``c1`` bound to ``dashboard:chat-1``, whose slot is mid-turn.

    Returns the dispatcher, its client, the Discord-side sessions fake, the real
    dashboard state, the slot, and the list of Discord-side ``enqueue`` calls (the
    channel's own queue, which the resumed path must never touch).
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    state = _make_state(tmp_path)
    dispatcher._session_resume.dashboard_state = state
    slot = _busy(state.get_or_create_slot("chat-1"))
    if steer_client is not None:
        slot._acp_client = steer_client
    await _bind_to_chat1(dispatcher, client, log)
    sessions.is_busy = lambda key: True  # type: ignore[method-assign]
    enqueued: list = []
    sessions.enqueue = lambda *a, **k: enqueued.append((a, k)) or True  # type: ignore[attr-defined]
    return dispatcher, client, sessions, state, slot, enqueued


def _sent_texts(client) -> list[str]:
    return [t for t, _ in client.sent]
