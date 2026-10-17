"""Releasing a continuable conversation keeps its disk I/O off the event loop.

``release_conversation_async`` is what the dashboard's release handler and the
reaper's TTL sweep call. Its ``SessionMap`` half must stay on the loop and commit
first; its ``keep=False`` demote and the session-file unlink loop must run on a
worker thread; and the conversation must stay HELD until that worker lands, even
when the awaiting caller is cancelled, or a continuation could re-seed the sid
from a still-``keep=True`` state.json and lose its files to the cleanup.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import subagent as sa
from kiro_crew.subagent import SubagentManager


def _manager(forget_sid: str = "sid-1") -> SubagentManager:
    sessions = MagicMock()
    sessions.forget_conversation = MagicMock(return_value=forget_sid)
    sessions.conversation_provider = MagicMock(return_value="acp")
    sessions.reset = AsyncMock()
    return SubagentManager(sessions=sessions, ctx_builder=None)  # type: ignore[arg-type]


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.asyncio
async def test_disk_half_runs_off_loop_after_the_session_map_forget() -> None:
    mgr = _manager()
    mgr._conversations["subagent:c1"] = 1.0
    order: list[tuple[str, bool]] = []
    mgr._sessions.forget_conversation.side_effect = lambda key: (
        order.append(("forget", _on_loop())) or "sid-1"
    )
    with (
        patch.object(
            sa,
            "update_state",
            side_effect=lambda *a, **k: order.append(("demote", _on_loop())),
        ),
        patch.object(
            sa,
            "_cleanup_session_files_sync",
            side_effect=lambda *a: order.append(("unlink", _on_loop())),
        ),
    ):
        ok, detail = await mgr.release_conversation_async("c1")
    assert (ok, detail) == (True, "released")
    assert order == [("forget", True), ("demote", False), ("unlink", False)]
    assert "subagent:c1" not in mgr._conversations
    assert "c1" not in mgr._abandoned_state_writers


@pytest.mark.asyncio
async def test_conversation_is_held_until_the_worker_lands_even_when_cancelled() -> None:
    mgr = _manager()
    entered = threading.Event()
    gate = threading.Event()
    demoted: list[str] = []

    def _slow_demote(conv_id: str, **fields: object) -> bool:
        entered.set()
        gate.wait(5)
        demoted.append(conv_id)
        return True

    with (
        patch.object(sa, "update_state", side_effect=_slow_demote),
        patch.object(sa, "_cleanup_session_files_sync"),
    ):
        caller = asyncio.ensure_future(mgr.release_conversation_async("c1"))
        assert await asyncio.to_thread(entered.wait, 5)
        # Mid-release: a continuation must be told to retry, not re-seed the sid.
        busy = mgr._conversation_busy("subagent:c1")
        assert busy is not None and busy._state_writer_abandoned
        assert (await mgr.release_conversation_async("c1"))[1].startswith("conversation_busy")
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        # The caller is gone, the worker is not: the hold survives the cancel.
        assert mgr._conversation_busy("subagent:c1") is not None
        gate.set()
        for _ in range(200):
            if "c1" not in mgr._abandoned_state_writers:
                break
            await asyncio.sleep(0.01)
    assert demoted == ["c1"]
    assert mgr._conversation_busy("subagent:c1") is None


@pytest.mark.asyncio
async def test_nothing_to_release_still_demotes() -> None:
    mgr = _manager(forget_sid="")
    with (
        patch.object(sa, "update_state") as update,
        patch.object(sa, "_cleanup_session_files_sync") as cleanup,
    ):
        assert await mgr.release_conversation_async("c1") == (
            False,
            "conversation_gone: nothing to release",
        )
    update.assert_called_once_with("c1", keep=False)
    cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_reaper_sweep_releases_expired_conversations_off_loop() -> None:
    mgr = _manager()
    mgr._conversations["subagent:c1"] = 0.0
    seen: list[bool] = []
    with (
        patch.object(sa, "update_state", side_effect=lambda *a, **k: seen.append(_on_loop())),
        patch.object(sa, "_cleanup_session_files_sync"),
    ):
        await mgr._sweep_conversations_async(now=float(sa._CONVERSATION_TTL_SECS * 3))
    assert "subagent:c1" not in mgr._conversations
    assert seen == [False]


@pytest.mark.asyncio
async def test_sweep_rereads_a_timestamp_refreshed_during_an_earlier_release() -> None:
    mgr = _manager()
    mgr._conversations["subagent:a"] = 0.0
    mgr._conversations["subagent:b"] = 0.0
    now = float(sa._CONVERSATION_TTL_SECS * 3)
    released: list[str] = []

    def _demote(conv_id: str, **fields: object) -> bool:
        released.append(conv_id)
        return True

    real_release = mgr.release_conversation_async

    async def _release(conv_id: str) -> tuple[bool, str]:
        result = await real_release(conv_id)
        # While "a" was releasing, "b" was continued and finished: fresh TTL.
        if conv_id == "a":
            mgr._conversations["subagent:b"] = now
        return result

    with (
        patch.object(sa, "update_state", side_effect=_demote),
        patch.object(sa, "_cleanup_session_files_sync"),
        patch.object(mgr, "release_conversation_async", side_effect=_release),
    ):
        await mgr._sweep_conversations_async(now=now)
    assert released == ["a"]
    assert mgr._conversations["subagent:b"] == now
