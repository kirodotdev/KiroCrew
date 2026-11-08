"""A channel-path turn in flight reads as running on its dashboard tab.

A conversation started in Slack (or any channel) surfaces as a dashboard slot
bound to the channel session (``linked_session_key``). Its turns normally run on
the CHANNEL path: the Slack handler holds the session's turn lease, writes the
user row to the shared transcript, and the reconciler pulls that row into the
tab. The slot's own ``turn_running`` only sees a dashboard-started task, so
before this fix the tab summarised that in-flight turn as ``interrupted`` (a
trailing user row with nothing running) and the sidebar showed
"Turn interrupted · Resume" while the reply was still on its way.

The lease the channel path holds is ``SessionManager.is_busy``. These tests pin
both halves: a held lease reads as running and never as interrupted, and once
the lease is gone (restart, dead process) the same transcript still reads as
interrupted so the Resume affordance comes back.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal, continue_slot_turn

CHANNEL_KEY = "slack:1792530008.294939"
TRAILING_USER = [
    {"role": "user", "content": "earlier ask", "ts": "2026-10-20T20:59:00+00:00"},
    {"role": "assistant", "content": "earlier answer", "ts": "2026-10-20T20:59:10+00:00"},
    {"role": "user", "content": "help me out here", "ts": "2026-10-20T21:00:16+00:00"},
]


@pytest.fixture
def state(tmp_path: Any) -> Any:
    state = _make_state(tmp_path)
    state.sessions.channel_key_for_stem = lambda stem: ""
    busy: set[str] = set()
    state.sessions.is_busy = lambda key: key in busy
    state._test_busy = busy
    return state


def _surface(state: Any, messages: list[dict] | None = None) -> Any:
    slot = channel_slots.surface_channel_session(
        state,
        {"key": CHANNEL_KEY, "title": "Slack thread", "modified": 0},
        {},
        list(messages if messages is not None else TRAILING_USER),
        session_key=CHANNEL_KEY,
    )
    assert slot is not None and slot.linked_session_key == CHANNEL_KEY
    assert not slot.turn_running, "precondition: no dashboard task owns this turn"
    return slot


def test_a_channel_turn_in_flight_reads_running_not_interrupted(state: Any) -> None:
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)

    row = slot.to_dict()

    assert row["running"] is True
    assert row["interrupted"] is False
    assert row["waiting_for_input"] is False


def test_a_real_interruption_still_reads_interrupted(state: Any) -> None:
    """No lease held (gateway restarted, or the turn's process died and its
    handler released the lease): the trailing user row IS an interruption."""
    slot = _surface(state)

    row = slot.to_dict()

    assert row["running"] is False
    assert row["interrupted"] is True


def test_the_row_flips_back_to_interrupted_when_the_lease_drops_without_a_reply(
    state: Any,
) -> None:
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)
    assert slot.to_dict()["interrupted"] is False

    state._test_busy.discard(CHANNEL_KEY)

    row = slot.to_dict()
    assert row["running"] is False
    assert row["interrupted"] is True


def test_another_sessions_lease_does_not_mark_this_tab_running(state: Any) -> None:
    state._test_busy.add("slack:1111111111.000001")
    slot = _surface(state)

    row = slot.to_dict()

    assert row["running"] is False
    assert row["interrupted"] is True


def test_an_unbound_dashboard_slot_ignores_the_lease_probe(state: Any) -> None:
    """A plain dashboard slot has no linked channel session; its running state
    is its own task alone, so the probe is never consulted for it."""
    calls: list[str] = []
    state.sessions.is_busy = lambda key: calls.append(key) or True
    slot = state.get_or_create_slot("chat-plain")
    slot.messages.append({"role": "user", "content": "hi", "ts": "t1"})

    row = slot.to_dict()

    assert row["running"] is False
    assert calls == []


def test_a_probe_failure_falls_back_to_the_dashboard_task(state: Any) -> None:
    def boom(key: str) -> bool:
        raise RuntimeError("session map unavailable")

    state.sessions.is_busy = boom
    slot = _surface(state)

    row = slot.to_dict()

    assert row["running"] is False
    assert row["interrupted"] is True


def test_resume_is_refused_while_the_channel_turn_runs(state: Any) -> None:
    """Resume on the tab would queue a second answer to the very prompt the
    channel path is still answering, so the server refuses it like any other
    running turn."""
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)

    with pytest.raises(SlotContinueRefusal) as refused:
        asyncio.run(continue_slot_turn(state, slot, directive_user_origin=True))

    assert refused.value.code == "slot_running"


def test_the_open_pane_reads_the_channel_turn_as_running(state: Any) -> None:
    """The pane's own detail read writes its run state too; reporting idle there
    put Resume back on the composer the summary had just cleared."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app

    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)

    async def read() -> dict:
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(f"/api/chat/slots/{slot.key}")
            assert resp.status == 200
            return await resp.json()

    body = asyncio.run(read())
    assert body["running"] is True
    # No dashboard turn number exists for a channel-path turn, so the pair that
    # names one is withheld rather than naming the previous turn.
    assert "turn" not in body

    state._test_busy.discard(CHANNEL_KEY)
    assert asyncio.run(read())["running"] is False


def test_stop_reaches_the_channel_turn_on_its_session(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)

    reply = asyncio.run(stop_slot_turn(state, slot))

    state.sessions.stop_turn.assert_awaited_once_with(CHANNEL_KEY, force=False, preserve_queue=True)
    # Not "not running": that answer idles the pane while the turn still runs.
    assert reply["ok"] is True and reply["already_stopping"] is True
    assert reply.get("info") != "not running"
    assert slot._stop_state == "idle"


def test_stop_on_an_idle_channel_tab_is_still_a_no_op(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    slot = _surface(state)

    reply = asyncio.run(stop_slot_turn(state, slot))

    state.sessions.stop_turn.assert_not_awaited()
    assert reply["info"] == "not running"


def test_the_reconciler_pushes_when_a_channel_turn_ends(state: Any) -> None:
    """The channel path's turn end raises no slots push of its own, so the row
    would keep reading running after the lease is gone; each reconcile pass
    pushes when a tab's channel-path turn started or ended."""
    from unittest.mock import MagicMock

    state._test_busy.add(CHANNEL_KEY)
    _surface(state)
    state.conversation_log = None  # the pass itself has nothing to surface
    state.push_slots_update = MagicMock()

    asyncio.run(channel_slots.reconcile_channel_slots(state, 30))
    assert state.push_slots_update.call_count == 1, "turn start seen"
    # The pass records what it saw, which is what the next pass compares with.
    assert list(channel_slots._LINKED_RUNNING_SEEN[state].values()) == [True]

    asyncio.run(channel_slots.reconcile_channel_slots(state, 30))
    assert state.push_slots_update.call_count == 1, "no change, no push"

    state._test_busy.discard(CHANNEL_KEY)
    asyncio.run(channel_slots.reconcile_channel_slots(state, 30))
    assert state.push_slots_update.call_count == 2, "turn end pushed"
    assert list(channel_slots._LINKED_RUNNING_SEEN[state].values()) == [False]


def test_stop_during_a_compaction_is_declined_then_the_next_press_forces(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session-side stop declines while the session compacts; the
    dashboard half of that decline (arm the marker, escalate the next press)
    must hold for a channel-path turn too, or Stop there is a dead end."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="compacting")
    state.sessions.is_compacting = lambda key: key == CHANNEL_KEY
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)

    first = asyncio.run(stop_slot_turn(state, slot))

    assert first == {"ok": True, "info": "compacting", "compacting": True}
    assert slot._stop_declined_at > 0.0
    assert state.sessions.stop_turn.await_args.kwargs["force"] is False

    state.sessions.stop_turn = AsyncMock(return_value="hard")
    second = asyncio.run(stop_slot_turn(state, slot))

    assert state.sessions.stop_turn.await_args.kwargs["force"] is True
    assert slot._stop_declined_at == 0.0
    assert second["ok"] is True and second.get("info") != "not running"


@pytest.mark.parametrize("compacting", [True, False])
def test_an_explicit_force_is_hard_only_as_the_compaction_escape(
    state: Any, monkeypatch: pytest.MonkeyPatch, compacting: bool
) -> None:
    """Mirrors the dashboard's own turn: without a compaction, a force is a
    retried first press (its reply lost on the wire) and stays cooperative, so
    a re-send cannot hard-reset the channel's session."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="hard" if compacting else "soft")
    state.sessions.is_compacting = lambda key: compacting
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)

    asyncio.run(stop_slot_turn(state, slot, force=True))

    state.sessions.stop_turn.assert_awaited_once_with(
        CHANNEL_KEY, force=compacting, preserve_queue=True
    )


def test_a_stale_decline_marker_is_dropped_once_the_compaction_ended(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.sessions.is_compacting = lambda key: False
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)
    slot._stop_declined_at = time.monotonic()

    asyncio.run(stop_slot_turn(state, slot))

    assert state.sessions.stop_turn.await_args.kwargs["force"] is False
    assert slot._stop_declined_at == 0.0


def test_stop_names_a_held_lease_with_no_provider_turn_yet(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)

    assert asyncio.run(stop_slot_turn(state, slot)) == {"ok": True, "info": "no active turn"}


def test_interrupt_cancels_the_channel_path_turn(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupt with nothing queued: the channel-path turn is the thing to
    stop, so it is cancelled on its session instead of answering 'not running'."""
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.chat import api_chat_slot_interrupt

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)

    async def press() -> dict:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/interrupt")
            assert resp.status == 200
            return await resp.json()

    body = asyncio.run(press())

    state.sessions.stop_turn.assert_awaited_once_with(CHANNEL_KEY, preserve_queue=True)
    assert body == {"ok": True, "info": "stopping linked turn"}


def test_the_slack_transport_refreshes_the_tab_after_releasing_the_session(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release is what ends the turn for a bound tab, and it raises no push
    of its own; the transport refreshes once more AFTER it so the tab drops the
    running state without waiting for the background pass."""
    import importlib
    from unittest.mock import AsyncMock

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
    from kiro_crew.history import ConversationLog
    from kiro_crew.messaging.link import canonical_key
    from kiro_crew.slack import transport_dispatch

    golden = importlib.import_module("test_slack_golden_transcript")
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    events: list[str] = []

    async def refresh(session_key: str) -> None:
        if session_key == turn_key:
            events.append("refresh")

    monkeypatch.setattr(transport_dispatch, "_refresh_dashboard_tab", refresh)
    provider = golden.ScriptedProvider(
        [
            golden.make_event(EVENT_TEXT_CHUNK, text="the reply"),
            golden.make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = golden.FakeSessions(provider)
    # Only this turn's own session: the auto-title background turn claims and
    # releases a session of its own on the side.
    turn_key = canonical_key("1700000000.000100")
    sessions.release = lambda key: events.append("release") if key == turn_key else None

    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=golden.RecordingSlackClient(),
            sessions=sessions,
            channel="C1",
            text="the question",
            thread_ts=None,
            msg_ts="1700000000.000100",
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
    )

    assert "release" in events, "the turn never released its session"
    assert events[-1] == "refresh", f"no tab refresh after the release: {events}"


@pytest.mark.parametrize("valid", [False, True])
def test_a_queued_interrupt_cancels_the_channel_turn_only_once_validated(
    state: Any, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    """A refused interrupt (here: no queue_id) must not kill the live channel
    turn, which may be someone else's message in a shared thread; a valid one
    cancels it and then dispatches the selected card."""
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.chat import api_chat_slot_interrupt

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    start = AsyncMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", start)
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state._test_busy.add(CHANNEL_KEY)
    slot = _surface(state)
    qid = slot.queue_append("queued card")
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)

    async def press() -> int:
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/chat/slots/{slot.key}/interrupt", json={"queue_id": qid} if valid else {}
            )
            return resp.status

    status = asyncio.run(press())

    if valid:
        assert status == 200
        state.sessions.stop_turn.assert_awaited_once_with(CHANNEL_KEY, preserve_queue=True)
        assert start.await_count == 1
    else:
        assert status == 400
        state.sessions.stop_turn.assert_not_awaited()
        start.assert_not_awaited()


def test_a_linked_turns_row_names_no_turn_so_the_open_pane_runs(state: Any) -> None:
    """The row's ``turn`` is the slot's LAST dashboard turn, which the open pane
    already saw end. Beside ``running: true`` that reads to the pane as a stale
    row, so the pane would stay idle and its composer offer Resume while the
    Slack thread is still answering. Running only on the lease, the row names
    none -- the slot detail's rule -- and an idle row still names its turn."""
    slot = _surface(state)
    slot._turn_generation = 7

    idle = slot.to_dict()
    assert idle["running"] is False
    assert idle["turn"] == 7 and idle["turn_gen"]

    state._test_busy.add(CHANNEL_KEY)
    running = slot.to_dict()
    assert running["running"] is True
    assert "turn" not in running and "turn_gen" not in running


def test_a_workflow_binding_does_not_read_the_lease(state: Any) -> None:
    """A workflow or resumed slot's linked session holds the same lease for its
    own runs and for compaction; only a channel or cron binding's turns run
    elsewhere."""
    state._test_busy.add("dashboard:origin-chat")
    slot = state.get_or_create_slot("workflow-run1", linked_session_key="dashboard:origin-chat")
    slot.messages.append({"role": "user", "content": "run", "ts": "t1"})

    assert slot.linked_turn_running is False
    assert slot.to_dict()["running"] is False


def test_a_scheduled_cron_run_reads_running_on_its_tab_and_stops_there(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler runs a persistent job on its tab's ``cron:<id>`` session with
    no task on the tab, so the tab read idle, with no Stop, for the whole run. It
    reads the lease as a channel tab does, Stop cancels the run as the Schedule
    page's Cancel does (it posts nothing and counts nothing), and the reconcile
    pass pushes the row when the run starts and when it ends."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.crons = MagicMock()
    state.crons.cancel = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("cron-nightly", linked_session_key="cron:nightly")
    state.conversation_log = None  # the pass itself has nothing to surface
    state.push_slots_update = MagicMock()

    state._test_busy.add("cron:nightly")
    assert slot.to_dict()["running"] is True
    asyncio.run(channel_slots.reconcile_channel_slots(state, 30))
    assert state.push_slots_update.call_count == 1, "run start pushed"
    assert asyncio.run(stop_slot_turn(state, slot, cron_cancel=True))["ok"] is True
    state.crons.cancel.assert_awaited_once_with("nightly")
    state.sessions.stop_turn.assert_not_awaited()
    # A one-shot whose pause did not reach the store is reported, as the Schedule
    # page reports it, and the run is not cancelled a second way.
    from kiro_crew.cron import CronPauseNotSaved

    state.crons.cancel = AsyncMock(side_effect=CronPauseNotSaved("pause not saved"))
    refused = asyncio.run(stop_slot_turn(state, slot, cron_cancel=True))
    assert refused["ok"] is False and refused["code"] == "cron_pause_not_saved"
    state.sessions.stop_turn.assert_not_awaited()
    # A caller the cron cancel route refuses leaves the run alone and is refused,
    # never told "not running", which would idle the pane while the run goes on.
    state.crons.cancel = AsyncMock(return_value=False)
    refused = asyncio.run(stop_slot_turn(state, slot))
    assert refused["ok"] is False and refused["code"] == "owner_only"
    assert "info" not in refused
    state.crons.cancel.assert_not_awaited()
    state.sessions.stop_turn.assert_not_awaited()
    # An admitted caller with no run of the job going (another path holds the
    # session): the turn on it is cancelled.
    assert asyncio.run(stop_slot_turn(state, slot, cron_cancel=True))["already_stopping"] is True
    state.sessions.stop_turn.assert_awaited_once_with(
        "cron:nightly", force=False, preserve_queue=True
    )

    state._test_busy.discard("cron:nightly")
    assert slot.to_dict()["running"] is False
    asyncio.run(channel_slots.reconcile_channel_slots(state, 30))
    assert state.push_slots_update.call_count == 2, "run end pushed"


@pytest.mark.asyncio
async def test_a_released_linked_lease_pushes_the_tab_without_the_reconciler(state: Any) -> None:
    """With ``surface_channel_sessions`` off no reconcile pass runs, and an
    injected completion or a pick landing that held ``cron:<id>`` pushes no row
    of its own when it ends, so the release observer pushes it."""
    from unittest.mock import MagicMock

    state.sessions._fold_key = lambda key: key
    state.wire_session_release_listener()
    state.get_or_create_slot("cron-nightly", linked_session_key="cron:nightly")
    state.push_slots_update = MagicMock()

    state.sessions.on_session_released("cron:other")
    await asyncio.sleep(0)
    state.push_slots_update.assert_not_called()

    state.sessions.on_session_released("cron:nightly")
    await asyncio.sleep(0)
    state.push_slots_update.assert_called_once_with()


@pytest.mark.parametrize("owner", [True, False], ids=["owner", "non-owner"])
def test_only_a_caller_the_cron_cancel_route_admits_cancels_the_run_from_its_tab(
    state: Any, monkeypatch: pytest.MonkeyPatch, owner: bool
) -> None:
    """A tab Stop ends the cron run through the cron service only for a caller
    ``POST /api/crons/{id}/cancel`` admits; a non-owner dashboard user's Stop
    leaves the run alone (``stop_slot_turn`` refuses it with ``owner_only``)."""
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.chat_handlers import _may_cancel_cron_run
    from kiro_crew.dashboard.handlers import _shared

    async def _gate(request: Any, operation: str) -> Any:
        assert operation == "crons.cancel"
        return None if owner else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", _gate)
    request = MagicMock()
    request.get = lambda key, default=None: "" if key == "app" else default

    assert asyncio.run(_may_cancel_cron_run(request, state, "cron:nightly")) is owner
    # Never for a session that is no cron job's own, whoever asks.
    assert asyncio.run(_may_cancel_cron_run(request, state, "cron:nightly:agentA")) is False


def test_a_non_owner_stop_on_a_running_cron_tab_is_refused_not_idled(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the Stop route itself: a dashboard user who is not the owner
    presses Stop on a cron tab whose run is going. The run is left alone and
    the answer is a refusal the pane shows, not "not running", which would
    settle the pane idle and hide Stop while the cron keeps running."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop
    from kiro_crew.dashboard.handlers import _shared

    async def _gate(request: Any, operation: str) -> Any:
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", _gate)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.crons = MagicMock()
    state.crons.cancel = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("cron-nightly", linked_session_key="cron:nightly")
    state._test_busy.add("cron:nightly")
    assert slot.to_dict()["running"] is True

    app = web.Application()
    app["state"] = state
    request = MagicMock()
    request.app = app
    request.match_info = {"slot": "cron-nightly"}
    request.query = {}
    request.get = lambda key, default="": "" if key == "app" else default

    resp = asyncio.run(api_chat_slot_stop(request))
    body = json.loads(resp.text)
    assert body["ok"] is False and body["code"] == "owner_only"
    assert body.get("info") != "not running"
    state.crons.cancel.assert_not_awaited()
    state.sessions.stop_turn.assert_not_awaited()
    assert slot.to_dict()["running"] is True
