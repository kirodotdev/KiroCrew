"""A script cron's ``ctx.send_to_session`` is not a turn a person typed.

``POST /api/chat`` from an attested ``cron:`` caller carries no app claim, so the
send handler read it as a person's send: the user row was stamped
``HUMAN_TURN_META_KEY`` (which names the chat, moves the ranking stamp and reads
as ``human`` at the lesson-origin gate) and the turn ran with a user-origin
directive. The same send from a caller that is not a cron stays a person's.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from slot_ownership_checkpoint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _client,
    chat_run_fixture,
    state_fixture,
)

from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.testing.wait import async_wait_until

_CASES = [("cron:job-1", False), ("", True)]


def _as(monkeypatch: pytest.MonkeyPatch, creator: str) -> None:
    async def _creator(_request: Any) -> str:
        return creator

    async def _admit(*_a: Any, **_k: Any) -> tuple[None, bool, dict[str, Any]]:
        return None, False, {}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.cron_slot_creator", _creator)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.cron_creator_admission", _admit)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("creator, person", _CASES)
async def test_an_idle_send(
    state: Any, chat_run: Any, monkeypatch: pytest.MonkeyPatch, creator: str, person: bool
) -> None:
    _as(monkeypatch, creator)
    slot = state.get_or_create_slot("s1")
    async with _client(state, "") as client:
        resp = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": "tick"})
        assert resp.status == 200, await resp.text()
    await async_wait_until(lambda: chat_run.await_count or chat_run.call_count)
    assert chat_run.call_args.kwargs["_directive_user_origin"] is person
    [row] = [m for m in slot.messages if m.get("role") == "user"]
    meta = row.get("meta") or {}
    assert (meta.get(HUMAN_TURN_META_KEY) is True) is person
    # The row names the cron as its actor, as the queued entry and the turn do.
    assert meta.get(TURN_ACTOR_META_KEY) == (None if person else "cron")


@pytest.mark.asyncio
@pytest.mark.parametrize("creator, person", _CASES)
async def test_a_queued_send(
    state: Any, chat_run: Any, monkeypatch: pytest.MonkeyPatch, creator: str, person: bool
) -> None:
    _as(monkeypatch, creator)
    slot = state.get_or_create_slot("s1")
    busy = MagicMock()
    busy.done.return_value = False
    slot.task = busy
    async with _client(state, "") as client:
        resp = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": "tick"})
        assert resp.status == 200, await resp.text()
    [entry] = slot._queue
    # The drain stamps the row human from this mark, so it is the queued arm's witness.
    assert (entry.get("_directive_user_origin") is True) is person
