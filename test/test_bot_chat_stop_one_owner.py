"""Two stops of one Bot chat (two apps it mounts) share one discard and one answer.

The first stop owns the chat's ending fence and its discard; a second stop that
arrives while the first is stuck in the shared runtime's close waits for that
answer instead of discarding on its own, where it would find no session, report
a confirmed close and lower the first stop's fences early. ``discard_conversation``
is held on an event, so the second stop lands inside the first without a sleep.
"""

from __future__ import annotations

import asyncio

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)
from test_bot_chat_swap_fence import _bounded, _end

from kiro_crew.bot_chat_ending_fence import discard_with_ending_fence
from kiro_crew.session import SessionManager

KEY = "dashboard:bot-chat-two-apps"


@pytest.mark.asyncio
async def test_a_second_stop_waits_for_the_first_and_shares_its_unconfirmed_answer(cfg):
    """Let the second stop discard on its own and it reads "no session" as
    confirmed while the first is still closing, and its exit lowers the ending
    fence the first one raised; take the answer from anything but the first stop
    and the second reports a close nobody confirmed."""
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await _bounded(mgr.get_or_create(KEY))
    mgr.release(mgr._fold_key(KEY))
    folded = mgr._fold_key(KEY)
    entered = asyncio.Event()
    release = asyncio.Event()
    handle = (object(), "native-sid")
    calls: list[str] = []

    async def _discard(key, **kw):
        calls.append(key)
        if len(calls) > 1:
            return True  # what a second discard sees: the session is already gone
        entered.set()
        await _bounded(release.wait())  # the shared runtime's close, stuck
        kw["unconfirmed_handle_out"].append(handle)
        return False

    mgr.discard_conversation = _discard  # type: ignore[method-assign]
    first = asyncio.create_task(discard_with_ending_fence(mgr, KEY, []))
    second_out: list = []
    second = asyncio.create_task(discard_with_ending_fence(mgr, KEY, second_out))
    try:
        await _bounded(entered.wait())
        assert not second.done()
        assert folded in mgr._allocation_boundary().state.ending_keys
        release.set()
        assert await _bounded(first) == (False, None)
        assert await _bounded(second) == (False, None)
        assert second_out == [handle]
        assert calls == [KEY]
    finally:
        release.set()
        await _end(first)
        await _end(second)
