"""The Regenerate swap contract, tested as invariants over adversarial inputs.

I1  At the window cap, no row a swap retires is credited to the frozen prefix,
    and every row reaches disk once.
I2  A Regenerate pressed while a swap is in flight is refused as busy, and one
    pressed after it works on the swapped reply.
I3  A Regenerate that ends without a reply (refused, cancelled, failed write,
    gateway gone mid-swap) leaves the previous reply and its versions as they were.

Inputs: the window cap boundary +/-1, a concurrent and a cancelled Regenerate,
reload mid-swap.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401
from test_regenerate_swap import (
    _arm,
    _disk,
    _disk_rows,
    _run_chat_state,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _save_slot_to_history
from kiro_crew.testing.wait import await_until


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_delta", [-1, 0, 1])
@pytest.mark.parametrize("appends_during_write", [0, 1, 2])
async def test_i1_no_retired_row_is_credited_at_the_window_cap(
    state, cap_delta: int, appends_during_write: int
) -> None:
    """The window sits one under, at, or one over the cap while rows land during
    the replace write: the old reply never comes back and no row is written twice."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    _save_slot_to_history(state, slot)
    cap = len(slot.messages) + cap_delta
    real = chat_runner.save_slot_off_loop

    async def _append_during_the_write(*args, **kwargs):
        for i in range(appends_during_write):
            slot.append("assistant", f"late {i}")
        return await real(*args, **kwargs)

    with (
        patch("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", cap),
        patch.object(chat_runner, "save_slot_off_loop", new=_append_during_the_write),
    ):
        await chat_runner.settle_regenerate(state, slot)
        assert _save_slot_to_history(state, slot, force=True)
        for i in range(3):
            slot.append("assistant", f"after {i}")
            assert _save_slot_to_history(state, slot, force=True)
    disk = _disk(state, slot)
    assert "old" not in disk
    assert len(disk) == len(set(disk))
    expected = ["hi", "new", *[f"late {i}" for i in range(appends_during_write)]]
    assert disk == [*expected, *[f"after {i}" for i in range(3)]]


@pytest.mark.asyncio
async def test_i2_a_regenerate_during_the_swap_is_refused_and_after_it_works(
    tmp_path, monkeypatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer
    from chat_regenerate_cov80_helpers import _make_regen_app
    from test_regenerate_swap import _client_streaming

    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    state = _run_chat_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "old")
    slot.drain()
    _save_slot_to_history(state, slot)
    events = [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="fresh"),
        LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state.sessions.get_or_create = AsyncMock(return_value=(_client_streaming(events), True, False))
    in_write = asyncio.Event()
    release = asyncio.Event()
    real = chat_runner.save_slot_off_loop

    async def _held(*args, **kwargs):
        if kwargs.get("retired_rows"):
            in_write.set()
            # Bounded: a test failing before it releases the barrier fails the
            # held write at a named deadline instead of suspending the turn.
            await asyncio.wait_for(release.wait(), 30)
        return await real(*args, **kwargs)

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
            new=AsyncMock(return_value=None),
        ),
        patch.object(chat_runner, "save_slot_off_loop", new=_held),
    ):
        async with TestClient(TestServer(_make_regen_app(state))) as client:
            assert (await client.post("/api/chat/slots/s1/regenerate")).status == 200
            try:
                await await_until(in_write, what="the swap's held write")
                assert slot._swap_in_flight
                during = await client.post("/api/chat/slots/s1/regenerate")
                assert during.status == 409
                # Either busy guard: the turn still owns the slot while it settles.
                assert (await during.json())["code"] in {"slot_running", "slot_busy"}
            finally:
                # Released and drained on every path, so a failed assertion
                # above cannot leave the turn suspended mid-swap.
                release.set()
                await asyncio.wait_for(slot.task, 30)
            # After the swap the reply is "fresh" with versions [old, fresh];
            # the next Regenerate stashes exactly those, built from the swapped row.
            seen: list[str] = []

            async def _capture(*a, **kw):
                seen.extend(v["content"] for v in slot._pending_variants)

            with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
                assert (await client.post("/api/chat/slots/s1/regenerate")).status == 200
                await asyncio.sleep(0)
    assert seen == ["old", "fresh"]


@pytest.mark.asyncio
async def test_i3_a_cancelled_regenerate_leaves_the_reply_and_its_versions(state) -> None:
    slot = _arm(state)
    slot.messages[-1]["variants"] = [{"content": "older"}, {"content": "old"}]
    before = [dict(m) for m in slot.messages]
    target = slot._regenerate_target
    release = chat_runner.release_regenerate_on_done(slot, target)
    cancelled = asyncio.get_running_loop().create_future()
    cancelled.cancel()
    release(cancelled)
    assert slot._regenerate_target is None and slot._pending_variants == []
    assert slot.messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [True, False])
async def test_i3_reload_mid_swap_reads_one_whole_transcript(state, committed: bool) -> None:
    """A gateway gone mid-swap reloads from disk: before the replace write commits
    that is the old reply untouched, after it the new reply with its versions,
    never a mix."""
    slot = _arm(state)
    chat_runner._note_reply_row(slot, slot.append("assistant", "new"))
    _save_slot_to_history(state, slot)
    real = chat_runner.save_slot_off_loop
    on_disk: list = []

    async def _crash_around_the_write(*args, **kwargs):
        if not committed:
            on_disk.extend(_disk_rows(state, slot))
            raise OSError("gateway gone")
        result = await real(*args, **kwargs)
        on_disk.extend(_disk_rows(state, slot))
        return result

    with patch.object(chat_runner, "save_slot_off_loop", new=_crash_around_the_write):
        await chat_runner.settle_regenerate(state, slot)
    contents = [r["content"] for r in on_disk]
    if committed:
        assert contents == ["hi", "new"]
        assert [v["content"] for v in on_disk[-1]["variants"]] == ["old", "new"]
    else:
        assert contents == ["hi", "old", "new"]
        assert "variants" not in on_disk[-1]
