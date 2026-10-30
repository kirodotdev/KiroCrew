"""Delivering a sub-agent completion into its parent, pinned through ``on_done``.

A completion goes to the parent's open tab as a turn of its own (or queued behind
the turn the tab is running), into the parent's channel session when it has no
tab, into its cron session, or to the notification bell. Each route decides when
the parent counts as told, and the tab route keeps a count of deliveries in
flight that the synthesis gate reads.

These tests drive those routes through the real ``on_done`` and pin what each one
must keep: the inline-collected skips, the in-flight count across a wait and a
cancellation, the turn's provenance, the per-attempt session wiring and its
timeout bound, the cron session's reset decision, and which route marks the
report delivered.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_gateway_subagent_wiring import (
    _LOST_RUN_SECS,
    _Done,
    _RecordingAsyncio,
    _Slot,
    _until,
    harness,
    make_info,
    recording_clock,
)

from kiro_crew.slack import gateway as gw_mod


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    yield from harness(monkeypatch)


@pytest.fixture
def clock(done: _Done, monkeypatch: pytest.MonkeyPatch) -> _RecordingAsyncio:
    return recording_clock(done, monkeypatch)


def _owner_calls(done: _Done) -> list[Any]:
    return [c for c in done.calls if c[0] in ("acquire", "reset", "cancel", "release", "sleep")]


# ── the tab: blocking-tool collection ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_result_collected_inline_is_not_injected_again(done: _Done) -> None:
    slot = done.tab("main")
    slot.running = True
    current: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    slot.task = current
    slot._pending_synthesis = True
    slot._synthesis_armers = {"a1"}
    slot._subagents_inline_collected = {"a1"}
    info = make_info()

    # Skipped before the tab's running turn is waited on, and never counted in flight.
    delivery = asyncio.ensure_future(done.on_done(info))
    await _until(delivery.done, "the skip, with the tab's turn still running")
    delivery.result()

    assert not current.done()
    assert done.chats == [] and slot.queued == []
    assert slot._subagents_inline_collected == set()
    assert slot._pending_synthesis is False
    assert slot._subagent_deliveries_inflight == 0
    assert info._report_delivered is True


@pytest.mark.asyncio
async def test_a_partial_inline_collection_keeps_synthesis_armed(done: _Done) -> None:
    slot = done.tab("main")
    slot._pending_synthesis = True
    slot._subagents_inline_collected = {"a1", "a2"}

    await done.done(make_info())

    assert slot._subagents_inline_collected == {"a2"}
    assert slot._pending_synthesis is True


@pytest.mark.asyncio
async def test_an_inline_skip_keeps_a_synthesis_another_child_armed(done: _Done) -> None:
    slot = done.tab("main")
    slot._pending_synthesis = True
    slot._synthesis_armers = {"b1"}
    slot._subagents_inline_collected = {"a1"}

    await done.done(make_info())

    assert slot._subagents_inline_collected == set()
    assert slot._pending_synthesis is True
    assert slot._synthesis_armers == {"b1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order", [("a1", "b1"), ("b1", "a1")], ids=["wave-first", "background-first"]
)
async def test_a_background_completion_keeps_its_arm_when_an_inline_sibling_also_armed(
    done: _Done, order: tuple[str, str]
) -> None:
    slot = done.tab("main")
    slot.running = True
    current: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    slot.task = current
    infos = {child: make_info(child) for child in order}

    # Both arm at entry and wait behind the running turn, in either order; the
    # blocking tool's collection of a1 lands during that wait.
    deliveries = []
    for count, child in enumerate(order, start=1):
        deliveries.append(asyncio.ensure_future(done.on_done(infos[child])))
        await _until(
            lambda n=count: slot._subagent_deliveries_inflight == n,
            f"{child} waiting behind the turn",
        )
    assert slot._synthesis_armers == {"a1", "b1"}
    slot._subagents_inline_collected.add("a1")
    current.set_result(None)
    await asyncio.wait_for(asyncio.gather(*deliveries), _LOST_RUN_SECS)

    assert infos["a1"]._report_delivered is True
    assert len(slot.queued) == 1
    assert slot._pending_synthesis is True
    assert slot._synthesis_armers == {"b1"}


@pytest.mark.asyncio
async def test_a_result_collected_inline_while_waiting_is_not_queued(done: _Done) -> None:
    slot = done.tab("main")
    slot.running = True
    current: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    slot.task = current
    info = make_info()

    delivery = asyncio.ensure_future(done.on_done(info))
    await _until(lambda: slot._subagent_deliveries_inflight == 1, "the wait behind the turn")
    slot._subagents_inline_collected.add("a1")
    current.set_result(None)
    await asyncio.wait_for(delivery, _LOST_RUN_SECS)

    assert slot.queued == [] and done.chats == []
    assert slot._subagent_deliveries_inflight == 0
    assert slot._pending_synthesis is False
    assert info._report_delivered is True


@pytest.mark.asyncio
async def test_a_result_collected_inline_during_the_wait_is_skipped_on_an_idle_tab(
    done: _Done,
) -> None:
    slot = done.tab("main")
    slot.running = True
    current: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    slot.task = current
    info = make_info()

    delivery = asyncio.ensure_future(done.on_done(info))
    await _until(lambda: slot._subagent_deliveries_inflight == 1, "the wait behind the turn")
    slot._subagents_inline_collected.add("a1")
    slot.running = False
    current.set_result(None)
    await asyncio.wait_for(delivery, _LOST_RUN_SECS)

    assert done.chats == [] and slot.queued == []
    assert slot._subagent_deliveries_inflight == 0
    assert info._report_delivered is True


# ── the tab: the in-flight count and the wait ────────────────────────────────


@pytest.mark.asyncio
async def test_the_in_flight_count_is_held_while_waiting_and_released_on_cancel(
    done: _Done,
) -> None:
    slot = done.tab("main")
    slot.running = True
    current: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    slot.task = current

    delivery = asyncio.ensure_future(done.on_done(make_info()))
    await _until(lambda: slot._subagent_deliveries_inflight == 1, "the wait behind the turn")
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(delivery, _LOST_RUN_SECS)

    assert slot._subagent_deliveries_inflight == 0
    assert not current.cancelled()
    assert slot.queued == [] and done.chats == []


@pytest.mark.asyncio
async def test_a_turn_that_fails_while_awaited_frees_the_tab_for_the_completion(
    done: _Done,
) -> None:
    slot = done.tab("main")
    slot.running = True
    current: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    slot.task = current

    delivery = asyncio.ensure_future(done.on_done(make_info()))
    await _until(lambda: slot._subagent_deliveries_inflight == 1, "the wait behind the turn")
    slot.running = False
    current.set_exception(RuntimeError("turn failed"))
    await asyncio.wait_for(delivery, _LOST_RUN_SECS)
    await done.drain()

    assert [s for s, _text, _kw in done.chats] == [slot]
    assert slot._queue == []


@pytest.mark.asyncio
async def test_a_tab_still_busy_after_the_wait_queues_and_counts_one_turn(
    done: _Done,
) -> None:
    slot = done.tab("main")
    slot.running = True
    info = make_info()

    await done.done(info)

    assert [(kind, sorted(meta)) for _t, kind, meta in slot.queued] == [
        (gw_mod.SUBAGENT_COMPLETION_KIND, [gw_mod.SUBAGENT_COMPLETION_META_KEY])
    ]
    assert slot._synthesis_completion_turns == 1
    assert [content for content, _ids in slot.owed] == [slot.queued[0][0]]
    assert info._delivery_queued is True
    done.state.push_slots_update.assert_called_once_with()
    assert slot._subagent_deliveries_inflight == 0


# ── the tab: the turn ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_idle_tab_queues_the_completion_and_drains_it_at_once(done: _Done) -> None:
    """The idle tab takes the queue drain's admission path, not a turn of its own."""
    slot = done.tab("main")
    info = make_info()

    await done.done(info)
    await done.drain()

    ((text, kind, meta),) = slot.queued
    assert kind == gw_mod.SUBAGENT_COMPLETION_KIND
    assert sorted(meta) == [gw_mod.SUBAGENT_COMPLETION_META_KEY]
    assert text.startswith(gw_mod.SUBAGENT_COMPLETION_PREFIX)
    # The drain settles the delivery once the announce is consumed.
    assert [content for content, _ids in slot.owed] == [text]
    assert info._delivery_queued is True
    assert done.drains == [slot]
    assert [(s, t) for s, t, _kw in done.chats] == [(slot, text)]
    assert done.turn_is_slot_task == [True]
    assert slot._queue == [] and slot.task is None
    assert slot._turn_admission_reserved is False
    assert slot._synthesis_completion_turns == 1
    assert slot._subagent_deliveries_inflight == 0
    done.state.push_slots_update.assert_called_once_with()


@pytest.mark.asyncio
async def test_a_failed_solo_run_owes_nothing_to_the_tab_turn(done: _Done) -> None:
    slot = done.tab("main")

    await done.done(make_info(error="boom"))
    await done.drain()

    assert len(done.chats) == 1
    assert [ids for _content, ids in slot.owed] == [[]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("_stop_state", "soft_pending"),
        ("_stopping", True),
        ("_last_turn_auth_required", True),
        ("_turn_admission_reserved", True),
    ],
)
async def test_an_idle_tab_that_cannot_admit_a_turn_keeps_the_completion_queued(
    done: _Done, field: str, value: Any
) -> None:
    """A Stop being resolved, a sign-in hold or a send's reservation: the
    completion waits as a card for the next drain."""
    slot = done.tab("main")
    setattr(slot, field, value)
    info = make_info()

    await done.done(info)
    await done.drain()

    assert done.chats == [] and done.drains == []
    assert [entry["kind"] for entry in slot._queue] == [gw_mod.SUBAGENT_COMPLETION_KIND]
    assert [content for content, _ids in slot.owed] == [slot._queue[0]["content"]]
    assert info._delivery_queued is True
    assert slot._subagent_deliveries_inflight == 0
    done.state.push_slots_update.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["closing", "replaced"])
async def test_a_tab_that_closes_while_the_completion_waits_gets_the_bell(
    done: _Done, change: str
) -> None:
    """A closing tab drops a kinded queue entry with the slot, so the completion
    is not queued there: it rings the bell, as for a tab already gone."""
    slot = done.tab("main")
    info = make_info()

    async with slot._lock:
        delivery = asyncio.ensure_future(done.on_done(info))
        await _until(lambda: slot._subagent_deliveries_inflight == 1, "the wait for the lock")
        if change == "closing":
            slot.is_closing = True
        else:
            done.registry["main"] = _Slot("main")
    await asyncio.wait_for(delivery, _LOST_RUN_SECS)

    assert slot.queued == [] and slot.owed == [] and done.chats == []
    assert slot._synthesis_completion_turns == 0
    assert slot._subagent_deliveries_inflight == 0
    assert info._report_delivered is True
    ((args, kwargs),) = done.notices()
    assert args[0] == "subagent" and kwargs == {"meta": {"slot": "main"}}


@pytest.mark.asyncio
async def test_a_completion_the_drain_refuses_stays_queued(done: _Done) -> None:
    """Shutdown and memory preparation are refused inside the drain."""
    slot = done.tab("main")
    done.drain_refuses = True
    info = make_info()

    await done.done(info)
    await done.drain()

    assert done.drains == [slot] and done.chats == []
    assert [entry["kind"] for entry in slot._queue] == [gw_mod.SUBAGENT_COMPLETION_KIND]
    assert slot._turn_admission_reserved is False
    done.mgr.notify_injection_failed.assert_not_called()


@pytest.mark.asyncio
async def test_a_system_entry_already_queued_runs_before_the_completion(done: _Done) -> None:
    """A cron notice parked in the queue keeps its place; the completion waits
    behind it, and that turn's failure is not reported as this completion's."""
    slot = done.tab("main")
    slot.queue_append("parked earlier", kind=gw_mod.CRON_NOTIFICATION_KIND)
    done.chat_error = RuntimeError("the parked turn failed")
    info = make_info()

    await done.done(info)
    await done.drain()

    assert [t for _s, t, _kw in done.chats] == ["parked earlier"]
    assert [entry["kind"] for entry in slot._queue] == [gw_mod.SUBAGENT_COMPLETION_KIND]
    done.mgr.notify_injection_failed.assert_not_called()


@pytest.mark.asyncio
async def test_a_delivery_that_starts_no_turn_drains_what_it_held(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a completion waits behind a turn, that turn's end keeps a held user
    message queued. When the completion then turns out to be collected inline, the
    last delivery out drains the idle tab, or the message would wait for the next
    send."""
    kick = AsyncMock()
    monkeypatch.setattr(gw_mod, "drain_idle_parent_queue_for_stall", kick)
    slot = done.tab("main")
    slot.queue_append("sent while the children ran")
    slot.running = True
    current: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    slot.task = current

    delivery = asyncio.ensure_future(done.on_done(make_info()))
    await _until(lambda: slot._subagent_deliveries_inflight == 1, "the wait behind the turn")
    slot._subagents_inline_collected.add("a1")
    slot.running = False
    current.set_result(None)
    await asyncio.wait_for(delivery, _LOST_RUN_SECS)
    await done.drain()

    kick.assert_awaited_once_with(done.state, "dashboard:main")
    assert [entry["content"] for entry in slot._queue] == ["sent while the children ran"]


@pytest.mark.asyncio
async def test_a_delivery_that_started_its_turn_does_not_drain_again(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    kick = AsyncMock()
    monkeypatch.setattr(gw_mod, "drain_idle_parent_queue_for_stall", kick)
    slot = done.tab("main")
    slot.queue_append("sent while the children ran")
    gate = asyncio.Event()

    async def _held_turn(_state: Any, _slot: Any, text: str, **_kw: Any) -> None:
        done.chats.append((_slot, text, _kw))
        await gate.wait()

    done._run_chat = _held_turn  # type: ignore[method-assign]
    await done.done(make_info())
    try:
        kick.assert_not_awaited()
    finally:
        gate.set()
        await done.drain()


@pytest.mark.asyncio
async def test_a_failed_tab_turn_reports_its_reason_redacted(done: _Done) -> None:
    slot = done.tab("main")
    done.chat_error = RuntimeError("bad SECRET at EXFIL")
    info = make_info()

    await done.done(info)
    await done.drain()

    done.mgr.notify_injection_failed.assert_called_once_with(info, reason="bad [R] at [U]")
    assert slot.task is None


@pytest.mark.asyncio
async def test_the_announce_is_redacted_again_before_it_reaches_the_tab(done: _Done) -> None:
    done.tab("main")
    info = make_info()
    info.agent = "agentSECRET"

    await done.done(info)
    await done.drain()

    ((_slot, text, _kw),) = done.chats
    assert "Agent `a1` (agent[R]) completed ✅" in text


@pytest.mark.asyncio
async def test_a_closed_tab_gets_the_bell_with_its_slot_and_counts_as_delivered(
    done: _Done,
) -> None:
    info = make_info()

    await done.done(info)

    ((args, kwargs),) = done.notices()
    assert args[:2] == ("subagent", "Subagent `a1` ✅")
    assert kwargs == {"meta": {"slot": "main"}}
    assert info._report_delivered is True
    assert done.chats == []


# ── a channel parent with no tab ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_channel_attempt_builds_the_parent_turn_from_its_session(
    done: _Done,
) -> None:
    info = make_info(parent="slack:C1:1700000000.1")

    await done.done(info)

    builds = [c for c in done.calls if c[0] == "build"]
    assert len(builds) == 1
    _tag, args, kwargs = builds[0]
    assert args[1:] == (True, "slack:C1:1700000000.1")
    assert kwargs == {
        "memory_store": "STORE",
        "provider_type": done.orch._cfg.agent.provider,
        "context_provider": done.client,
        "resumed": False,
        "skill_trigger_text": "",
    }
    assert ("stream", f"built:{args[0]}") in done.calls
    assert info._report_delivered is True
    assert _owner_calls(done) == [
        ("acquire", "slack:C1:1700000000.1"),
        ("cancel", "slack:C1:1700000000.1"),
        ("release", "slack:C1:1700000000.1"),
    ]
    ((notice_args, _kw),) = done.notices()
    assert notice_args[:2] == ("subagent", "Subagent `a1` ✅")


@pytest.mark.asyncio
async def test_a_timed_out_channel_attempt_resets_waits_and_tries_once_more(
    done: _Done, clock: _RecordingAsyncio
) -> None:
    clock.timeouts = 2
    info = make_info(parent="slack:C1:1700000000.1")

    await done.done(info)

    key = "slack:C1:1700000000.1"
    timeout = gw_mod.INJECTION_TIMEOUT
    attempts = [c for c in done.calls if c[0] in ("wait_for", "reset", "sleep", "release")]
    assert attempts == [
        ("wait_for", timeout),
        ("reset", key),
        ("release", key),
        ("sleep", 2),
        ("wait_for", timeout),
        ("reset", key),
        ("release", key),
    ]
    secs = int(timeout)
    done.mgr.notify_injection_failed.assert_called_once_with(
        info,
        reason=f"attempt 1 timed out after {secs}s; attempt 2 timed out after {secs}s",
    )
    assert len(done.notices()) == 1


@pytest.mark.asyncio
async def test_a_failed_channel_attempt_is_not_retried(done: _Done) -> None:
    done.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no runtime"))
    info = make_info(parent="slack:C1:1700000000.1")

    await done.done(info)

    done.mgr.notify_injection_failed.assert_called_once_with(
        info, reason="attempt 1 failed: no runtime"
    )
    assert not any(c[0] in ("cancel", "release") for c in done.calls)
    assert len(done.notices()) == 1


# ── a cron parent ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cron_parent_gets_the_redacted_reply_and_its_session_reset(
    done: _Done,
) -> None:
    done.stream_results = ["cron reply SECRET"]
    info = make_info(parent="cron:j1:r1")

    await done.done(info)

    key = "cron:j1:r1"
    builds = [c for c in done.calls if c[0] == "build"]
    assert [kw["memory_store"] for _t, _a, kw in builds] == ["STORE"]
    done.orch._deliver_cron_response.assert_awaited_once_with(key, "cron reply [R]", silent=False)
    ((args, kwargs),) = done.notices()
    assert args[2].endswith("\n\ncron reply [R]")
    assert kwargs == {"meta": None}
    assert _owner_calls(done) == [
        ("acquire", key),
        ("cancel", key),
        ("release", key),
        ("reset", key),
    ]
    done.orch.cron_svc.clear_active_session_key.assert_called_once_with("j1", key)
    assert done.orch._cron_injecting == {}
    assert info._report_delivered is True


@pytest.mark.asyncio
async def test_a_silent_cron_job_delivers_its_reply_silently(done: _Done) -> None:
    done.orch._cron_job_is_silent = MagicMock(return_value=True)

    await done.done(make_info(parent="cron:j1"))

    done.orch._deliver_cron_response.assert_awaited_once_with(
        "cron:j1", "parent reply", silent=True
    )
    done.orch._cron_job_is_silent.assert_called_once_with("cron:j1")


@pytest.mark.asyncio
async def test_a_queued_sibling_keeps_the_cron_session(
    done: _Done, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gw_mod, "_subagent_queued_count", AsyncMock(return_value=1))

    await done.done(make_info(parent="cron:j1:r1"))

    assert not any(c[0] == "reset" for c in done.calls)
    done.orch.cron_svc.clear_active_session_key.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_reset_keeps_the_cron_key_registered(done: _Done) -> None:
    done.sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))

    await done.done(make_info(parent="cron:j1:r1"))

    done.orch.cron_svc.clear_active_session_key.assert_not_called()


@pytest.mark.asyncio
async def test_a_swallowed_cron_failure_marks_the_report_undelivered(done: _Done) -> None:
    done.stream_results = [RuntimeError("cron turn broke")]
    info = make_info(parent="cron:j1")

    await done.done(info)

    assert info._report_undelivered is True
    done.mgr.notify_injection_failed.assert_not_called()
    done.orch._deliver_cron_response.assert_not_awaited()
    assert len(done.notices()) == 1
    assert done.orch._cron_injecting == {}


@pytest.mark.asyncio
async def test_a_timed_out_cron_injection_resets_and_reports_the_bound(
    done: _Done, clock: _RecordingAsyncio
) -> None:
    clock.timeouts = 1
    info = make_info(parent="cron:j1")

    await done.done(info)

    timeout = gw_mod.INJECTION_TIMEOUT
    assert [c for c in done.calls if c[0] in ("wait_for", "reset")][:2] == [
        ("wait_for", timeout),
        ("reset", "cron:j1"),
    ]
    done.mgr.notify_injection_failed.assert_called_once_with(
        info, reason=f"injection timed out after {int(timeout)}s"
    )


# ── no conversation to inject into ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_parent_with_no_conversation_is_told_by_the_bell(done: _Done) -> None:
    info = make_info(parent="subagent:p1")

    await done.done(info)

    ((args, kwargs),) = done.notices()
    assert args[:2] == ("subagent", "Subagent `a1` ✅")
    assert kwargs == {"meta": None}
    assert info._report_delivered is True
    assert not any(c[0] == "acquire" for c in done.calls)


@pytest.mark.asyncio
async def test_a_silent_run_rings_no_bell_and_still_counts_as_delivered(done: _Done) -> None:
    info = make_info(parent="subagent:p1")
    info.silent = True

    await done.done(info)

    assert done.notices() == []
    assert info._report_delivered is True
