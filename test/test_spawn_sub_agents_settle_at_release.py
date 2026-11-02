"""A returned sub-agent result settles at its turn's release on every route.

The mark-and-settle rule of ``kiro_crew.parent_turns``, driven through each route
a completion can take for a parent that is not dashboard-born:

* a cron job whose tab is bound and a channel conversation with an open tab are
  routed to the tab, and still settle at their turn's release;
* a completion's own injected turn confirms only when it landed, and its in-claim
  retry never hands the failed attempt's marks to the retry;
* a mark is held only under the holding that read its child done, so a mark
  processed after its turn ended is never confirmed by the next turn, injection
  or compaction to hold the permit;
* a parked tab completion is routed again from the tab the parent shows at the
  release, owes its held wave siblings to whatever consumes it, and holds a cron
  parent's deferred reset until it is delivered.

Every route call runs on its own task, as a child's report does in production.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_gateway_subagent_wiring import harness, make_info
from test_spawn_sub_agents_duplicate_never_loss import (
    _channel_gateway,
    _channel_route,
    _cron_orchestrator,
    _late_mark,
    _mark_under_the_live_turn,
    _new_sessions,
    _poll_done,
    _prompt_in_flight,
    _returned_child,
    _run_as_far_as_it_goes,
    _spawn_from_the_parents_tool,
    _streams,
    _tombstone_gate_open,
)
from test_subagent_injection_live_turn import _HANG_CEILING_S

from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    AcpEvent,
)
from kiro_crew.llm_helpers_parts.turn_stream import stream_and_collect as _real_stream
from kiro_crew.messaging.turn_bracket import stop_reason_landed
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch):
    yield from harness(monkeypatch)


async def _settled(gateway: Any) -> None:
    """Wait out every parked completion the releases re-routed."""
    for _ in range(5):
        tasks = list(getattr(gateway, "_inline_tasks", ()))
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), _HANG_CEILING_S)
        for _ in range(20):
            await asyncio.sleep(0)


# ── a cron or channel parent with an open dashboard tab ──────────────────────


@contextlib.contextmanager
def _surfaced(*keys: str):
    """Publish *keys* as having an open tab, as ``_sync_dashboard_slots`` does."""
    from kiro_crew.session_surface import dashboard_surfaced_keys, set_dashboard_surfaced

    prior = dashboard_surfaced_keys()
    set_dashboard_surfaced(set(keys))
    try:
        yield
    finally:
        set_dashboard_surfaced(prior)


#: (parent key, the key its tab publishes) for each tab-routed parent.
_CRON_TAB = ("cron:j1", "cron:j1")  # a persistent_session job shown in chat
_SLACK_TAB = ("slack:C1:1700000000.1", "slack:C1:1700000000.1")  # a channel-born tab


async def _drive_tab_parent(
    done: Any,
    parent: tuple[str, str],
    *,
    lands: bool,
    held: tuple[Any, ...] = (),
    close_tab: bool = False,
) -> dict[str, Any]:
    """The real ``_subagent_done`` and tab route over a real ``SessionManager``.

    The parent's turn holds its session; its ``spawn_sub_agents`` reply carrying the
    child was written and marked through the real flagging; the child's completion
    reaches the real consumer on its own task while the turn is live. The turn then
    ends: landed (its engine notes it, then releases) or not (a deadline or a stop:
    released with no note). *held* are the wave siblings the completion's digest
    carries, and *close_tab* closes the tab before the turn ends."""
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    key, surface = parent
    sessions = _new_sessions()
    done.orch.sessions = sessions
    sessions.parent_turns.on_released = done.orch._settle_inline  # as service boot wires it
    resets: list[tuple[str, dict[str, Any]]] = []

    async def _reset(reset_key: str, **kwargs: Any) -> bool:
        resets.append((reset_key, kwargs))
        return True

    sessions.reset = _reset  # recorded: the deferred cron reset runs after delivery
    info = make_info("child-1", parent=key)
    info._digest_settle_deliveries = list(held)
    done.mgr.get = MagicMock(side_effect={info.id: info}.get)
    done.mgr.settle_queued_delivery = AsyncMock()
    seen: dict[str, Any] = {"info": info, "resets": resets}
    with _surfaced(surface):
        slot_name = dashboard_slot_key(key)
        assert slot_name, "the parent's tab is not published"
        done.tabs[key] = slot_name
        seen["slot"] = done.tab(slot_name)
        holding, end = asyncio.Event(), asyncio.Event()

        async def turn() -> None:
            await sessions.get_or_create(key)
            holding.set()
            await end.wait()
            if lands:
                sessions.note_turn_landed(key)  # the engine decided its stop reason succeeded
            sessions.release(key)
            # What the cron turn's own cleanup reads next to decide on its reset.
            seen["counted_at_release"] = done.orch._cron_injecting.get(key, 0)

        parent_turn = asyncio.create_task(turn())
        await asyncio.wait_for(holding.wait(), _HANG_CEILING_S)
        seen["marked"] = _mark_under_the_live_turn(sessions, done.mgr, key, info.id)
        route = asyncio.create_task(done.done(info))
        await _run_as_far_as_it_goes(route)
        seen["acked_while_live"] = info._report_delivered
        seen["gate_open_while_live"] = route.done() and _tombstone_gate_open(info)
        seen["chats_while_live"] = len(done.chats)
        if close_tab:
            done.tabs.pop(key, None)
            done.state.get_slot = MagicMock(return_value=None)
        end.set()
        await asyncio.wait_for(parent_turn, _HANG_CEILING_S)
        await asyncio.wait_for(route, _HANG_CEILING_S)
        await _settled(done.orch)
        await done.drain()
    seen["chats"] = [text for _slot, text, _kw in done.chats]
    return seen


@pytest.mark.asyncio
async def test_a_cron_job_tab_cut_off_at_its_deadline_still_delivers_the_returned_result(
    done: Any,
) -> None:
    """A cron job whose tab is bound is cut by its deadline after the reply carrying
    the result reached kiro-cli. The tab route parks the completion while the cron
    turn holds the session, and the release, with no landing noted, delivers it."""
    seen = await _drive_tab_parent(done, _CRON_TAB, lands=False)

    assert seen["marked"] == {"child-1"}
    assert seen["acked_while_live"] is False, (
        "the tab route acknowledged a cron run's returned result while the turn that "
        "received the reply still held the session"
    )
    assert seen["gate_open_while_live"] is False
    assert len(seen["chats"]) == 1, f"delivered {seen['chats']!r} after the cut turn released"
    assert done.orch._inline_parked == {}


@pytest.mark.asyncio
async def test_a_channel_parent_with_an_open_tab_whose_turn_is_stopped_still_gets_its_result(
    done: Any,
) -> None:
    """A Slack thread whose channel-born tab is open: the user stops the turn after
    the reply reached kiro-cli, and the result is still delivered once."""
    seen = await _drive_tab_parent(done, _SLACK_TAB, lands=False)

    assert (
        seen["acked_while_live"] is False
    ), "the tab route acknowledged a channel parent's returned result under its live turn"
    assert len(seen["chats"]) == 1, f"delivered {seen['chats']!r} after the stopped turn"
    assert done.orch._inline_parked == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [_CRON_TAB, _SLACK_TAB], ids=["cron-tab", "slack-tab"])
async def test_a_tab_parents_landed_turn_plays_no_duplicate_and_settles_at_its_release(
    done: Any, parent: tuple[str, str]
) -> None:
    """A landed turn plays no second turn on a tab parent either. The completion is
    acknowledged and its tombstone settled at the release, never under the live
    turn."""
    seen = await _drive_tab_parent(done, parent, lands=True)

    assert seen["acked_while_live"] is False, "acknowledged under the live turn"
    assert seen["chats"] == [], f"a result the landed turn used was delivered again: {seen!r}"
    assert seen["info"]._report_delivered is True
    done.mgr.settle_queued_delivery.assert_awaited_once()
    assert done.orch._inline_parked == {}


@pytest.mark.asyncio
async def test_a_pending_mark_leaves_a_cron_tabs_queued_completion_queued(tmp_path) -> None:
    """The real ``mark-collected`` endpoint. A completion already QUEUED on the cron
    job's tab stays queued while the cron turn that received the reply is live: the
    endpoint does not remove it and settle its delivery at mark time, before the
    turn has landed."""
    from chat_test_helpers import _make_state
    from test_subagent_queued_visibility import _Req

    from kiro_crew.constants import SUBAGENT_COMPLETION_META_KEY
    from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND, dashboard_slot_key
    from kiro_crew.dashboard.handlers.messaging import api_spawn_mark_collected
    from kiro_crew.subagent import SubagentDelivery
    from kiro_crew.subagent_completion_meta import single_completion_meta

    key = "cron:j1"
    state = _make_state(tmp_path)
    sessions = _new_sessions()
    state.sessions = sessions
    child = _returned_child("a1", key)
    state.subagents = MagicMock(
        get=MagicMock(side_effect={child.id: child}.get), settle_queued_delivery=AsyncMock()
    )
    # The job's tab, linked to the job's key as ``_bind_cron_slot`` links it; created
    # first, since creating a slot re-publishes the surface set from the slot table.
    slot = state.get_or_create_slot("cron-j1")
    slot.linked_session_key = key
    with _surfaced(key):
        assert dashboard_slot_key(key) == "cron-j1"
        announce = "[Subagent completion event] a1 finished"
        slot.queue_append(
            announce,
            kind=SUBAGENT_COMPLETION_KIND,
            meta={
                SUBAGENT_COMPLETION_META_KEY: single_completion_meta(agent_id="a1", outcome="ok")
            },
        )
        slot.note_pending_subagent_delivery(announce, [SubagentDelivery("a1", 1.0, 0.0)])
        await sessions.get_or_create(key)  # the cron turn, still answering
        _poll_done(sessions, state.subagents, key, child.id)
        body = {"ids": ["a1"], "parent_session": key, "written": True}
        resp = await api_spawn_mark_collected(_Req(state, body=body))

    assert resp.status == 200 and "no_slot" not in resp.text, resp.text
    assert [
        q for q in slot._queue if q.get("kind") == SUBAGENT_COMPLETION_KIND
    ], "a mark under a live cron turn removed the member's queued completion"
    state.subagents.settle_queued_delivery.assert_not_awaited()


# ── a completion's own injected turn confirms only when it landed ───────────


class _EndsAs:
    """A provider whose prompt runs *during*, streams some text, then ends as
    *stop_reason* says (``None``: the stream ends with no completion event)."""

    def __init__(self, stop_reason: str | None, during: Any) -> None:
        self._stop_reason = stop_reason
        self._during = during

    async def stream(self, _message: str, **_kw: Any):
        await self._during()
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
        if self._stop_reason is not None:
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=self._stop_reason)


def _nested_gateway(sessions: Any, *infos: Any) -> Any:
    from test_subagent_injection_live_turn import _gateway

    gateway = _gateway(sessions)  # the real _inject_with_retry
    gateway.subagent_mgr.get = MagicMock(side_effect={i.id: i for i in infos}.get)
    gateway.subagent_mgr.settle_queued_delivery = AsyncMock()
    return gateway


async def _drive_nested(stop_reason: str | None) -> dict[str, Any]:
    """X's completion is injected into a Slack-thread parent through the REAL
    ``_inject_with_retry`` and ``stream_and_collect``. That injected turn calls
    ``spawn_sub_agents``: its reply carrying G is written and marked under the
    turn, and G's completion arrives and parks. The turn then ends as
    *stop_reason* says."""
    sessions = _new_sessions()
    key = "slack:C1:1700000000.1"
    x = _returned_child("child-x", key)  # an ordinary completion, never returned inline
    g = _returned_child("child-g", key)  # spawned by X's completion turn
    gateway = _nested_gateway(sessions, x, g)
    seen: dict[str, Any] = {"turns": []}

    async def stream(_client: Any, message: str, **kw: Any) -> str:
        seen["turns"].append(message)
        if len(seen["turns"]) > 1:
            return f"answered {message}"

        async def during() -> None:
            # The nested spawn_sub_agents reply carrying G reached kiro-cli's pipe.
            seen["marked"] = _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, g.id)
            seen["g_route"] = _channel_route(gateway, g, key)
            await _run_as_far_as_it_goes(seen["g_route"])
            seen["g_parked"] = seen["g_route"].done() and not g._report_delivered

        return await _real_stream(_EndsAs(stop_reason, during), message, **kw)

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await asyncio.wait_for(_channel_route(gateway, x, key), _HANG_CEILING_S)
        await _settled(gateway)
    seen.update(gateway=gateway, g=g, x=x)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason",
    ["cancelled", "refusal", "error: tool stall", None],
    ids=["stopped", "refused", "stalled", "no-completion"],
)
async def test_a_completion_turn_that_did_not_land_does_not_confirm_what_it_received_inline(
    stop_reason: str | None,
) -> None:
    """``stream_and_collect`` returns normally for each of these terminals, and none
    of them landed. X's injected turn did not land, so the reply it received (G)
    is in no finished answer: its release revokes G's mark, and G's parked
    completion is delivered as a turn of its own."""
    assert stop_reason_landed(stop_reason) is False  # the repo's own landing contract
    seen = await _drive_nested(stop_reason)

    assert seen["marked"] == {"child-g"}
    assert seen["g_parked"], "G's completion did not park under X's live injected turn"
    assert seen["turns"][:1] == ["child-x done"]
    assert seen["turns"][1:] == ["child-g done"], (
        "a turn that did not land confirmed the inline result it received, and the "
        f"result was acknowledged with no turn (turns={seen['turns']!r})"
    )
    assert seen["g"]._report_delivered is True
    assert seen["gateway"]._inline_parked == {}


@pytest.mark.asyncio
async def test_a_completion_turn_that_lands_confirms_what_it_received_inline() -> None:
    """A nested reply received by an injected turn that landed plays no second turn."""
    seen = await _drive_nested("end_turn")

    assert seen["g_parked"]
    assert seen["turns"] == ["child-x done"], f"turns={seen['turns']!r}"
    assert seen["g"]._report_delivered is True
    seen["gateway"].subagent_mgr.settle_queued_delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_attempt_does_not_hand_its_inline_marks_to_the_retry_that_lands() -> None:
    """``_inject_with_retry`` retries an ``AcpError`` inside the same claim, so both
    attempts are one holding of the permit. Attempt 1 received the nested reply
    carrying G and then failed; attempt 2 re-sends the announce and lands. G's
    mark is settled by attempt 1's own end, so attempt 2's landing never confirms
    a result the attempt that received it did not keep."""
    from kiro_crew.acp.client import AcpError

    sessions = _new_sessions()
    key = "slack:C1:1700000000.1"
    x = _returned_child("child-x", key)
    g = _returned_child("child-g", key)
    gateway = _nested_gateway(sessions, x, g)
    seen: dict[str, Any] = {"turns": []}

    async def stream(_client: Any, message: str, **kw: Any) -> str:
        seen["turns"].append(message)
        if len(seen["turns"]) == 1:
            seen["marked"] = _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, g.id)
            seen["g_route"] = _channel_route(gateway, g, key)
            await _run_as_far_as_it_goes(seen["g_route"])
            raise AcpError("the backend failed mid-turn")  # attempt 1 never lands
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))
        return f"answered {message}"

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await asyncio.wait_for(_channel_route(gateway, x, key), _HANG_CEILING_S)
        await _settled(gateway)

    assert seen["marked"] == {"child-g"}
    assert seen["turns"][:2] == ["child-x done", "child-x done"]
    assert seen["turns"][2:] == ["child-g done"], (
        "the retry's landing confirmed the inline result the failed attempt received "
        f"(turns={seen['turns']!r})"
    )
    assert gateway._inline_parked == {}


# ── a late mark is never held under the next holding of the permit ──────────


@pytest.mark.asyncio
async def test_a_late_mark_is_not_confirmed_by_a_siblings_landed_injection() -> None:
    """The cron turn that received A's result in its reply is cut by its deadline
    before the mark sent after the write is processed. A sibling's completion,
    waiting on the lease, claims the session at the release; the late mark is
    never held under the sibling's injected turn: the holding that read A done was
    the cut turn's. The cut turn's transcript lost A (the backend drops a cancelled
    turn), so A is delivered."""
    sessions = _new_sessions()
    key = "cron:j1:r1"
    a = _returned_child("child-a", key)  # returned inline by the cut cron turn
    c = _returned_child("child-c", key)  # an earlier spawn_run child: an ordinary completion
    orch = _cron_orchestrator(sessions, [a, c])
    orch.ctx_builder.build_message = MagicMock(
        side_effect=lambda announce, *_a, **_k: (announce, None)
    )
    seen: dict[str, Any] = {"turns": []}

    async def stream(_client: Any, message: str, **kw: Any) -> str:
        seen["turns"].append(message)
        if message == "child-c done":
            # The mark for A, sent after the cut turn's reply was written, lands now.
            seen["marked"] = _late_mark(sessions, orch.subagent_mgr, key, a.id)
            seen["a_route"] = asyncio.create_task(
                orch._finish_subagent_cron_or_notify(
                    a, announce="child-a done", body="b", title="t", carried=(a.id,), parent_key=key
                )
            )
            await _run_as_far_as_it_goes(seen["a_route"])
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))  # this injected turn landed
        return f"answered {message}"

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await sessions.get_or_create(key)  # the cron turn, answering with the reply
        _poll_done(sessions, orch.subagent_mgr, key, a.id)  # its tool read A done
        c_route = asyncio.create_task(
            orch._finish_subagent_cron_or_notify(
                c, announce="child-c done", body="b", title="t", carried=(c.id,), parent_key=key
            )
        )
        await async_wait_until(
            lambda: orch._cron_injecting.get(key) == 1, describe=lambda: "the sibling's claim"
        )
        await _run_as_far_as_it_goes(c_route)
        assert not c_route.done(), "the sibling did not wait for the cron turn's lease"
        sessions.release(key)  # the deadline cut the cron turn: it never noted landing
        await asyncio.wait_for(c_route, _HANG_CEILING_S)
        await _settled(orch)
        if "a_route" in seen:
            await asyncio.wait_for(seen["a_route"], _HANG_CEILING_S)

    assert seen["turns"][:1] == ["child-c done"]
    assert "child-a done" in seen["turns"], (
        "a mark that arrived after its turn was cut was confirmed by an unrelated "
        f"turn's landing (turns={seen['turns']!r}, marked={seen.get('marked')!r})"
    )
    assert a._report_delivered is True
    assert orch._inline_parked == {}


@pytest.mark.asyncio
async def test_the_next_messages_landed_turn_does_not_confirm_a_stopped_turns_marks() -> None:
    """A Telegram turn's reply carrying A was written; the mark request is still
    awaiting its identity lookup when the user stops the turn, and the next queued
    message claims the session. The late mark is not held under that successor, so
    its landing confirms nothing, and A's completion is delivered."""
    sessions = _new_sessions()
    key = "telegram:42"
    a = _returned_child("child-a", key)
    gateway = _channel_gateway(sessions, a)
    await sessions.get_or_create(key)  # the stopped turn: its reply carrying A was written
    _poll_done(sessions, gateway.subagent_mgr, key, a.id)
    sessions.release(key)  # stopped: released with no note
    successor_holds, successor_ends = asyncio.Event(), asyncio.Event()

    async def next_message() -> None:
        await sessions.get_or_create(key)
        successor_holds.set()
        await successor_ends.wait()
        sessions.note_turn_landed(key)  # the next message's turn lands
        sessions.release(key)

    successor = asyncio.create_task(next_message())
    await asyncio.wait_for(successor_holds.wait(), _HANG_CEILING_S)
    _late_mark(sessions, gateway.subagent_mgr, key, a.id)  # processed only now
    route = _channel_route(gateway, a, key)
    await _run_as_far_as_it_goes(route)
    successor_ends.set()
    await asyncio.wait_for(successor, _HANG_CEILING_S)
    await asyncio.wait_for(asyncio.shield(route), _HANG_CEILING_S)
    await _settled(gateway)

    gateway._inject_with_retry.assert_awaited_once()
    assert a._report_delivered is True
    assert gateway._inline_parked == {}


@pytest.mark.asyncio
async def test_a_late_mark_under_a_compaction_is_still_delivered() -> None:
    """The channel parent's turn released before the mark sent after its reply's
    write was processed, and the threshold compaction waiting on the permit took it.
    The compaction's holding ends with a raw ``semaphore.release()``, never through
    ``SessionManager.release``, so a mark held under it would never settle. The
    late mark is not held there, and A's completion is delivered: one duplicate
    turn at worst."""
    sessions = _new_sessions()
    key = "telegram:42"
    a = _returned_child("child-a", key)
    gateway = _channel_gateway(sessions, a)
    await sessions.get_or_create(key)  # the parent's turn: its reply carrying A was written
    _poll_done(sessions, gateway.subagent_mgr, key, a.id)  # its tool read A done
    sessions.release(key)  # the turn ended (a stop) before the mark was processed
    session = sessions._sessions[key]
    finish_compaction = asyncio.Event()

    async def compact_events(_command: str):
        await finish_compaction.wait()
        yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed")

    session.provider.stream_command = compact_events
    compaction = asyncio.create_task(sessions._compact_in_place(key, session, 90.0))
    await async_wait_until(
        lambda: (sessions.turn_token(key) or (None, None))[1] is compaction,
        describe=lambda: "the compaction never held the permit",
    )
    _late_mark(sessions, gateway.subagent_mgr, key, a.id)  # the late mark
    route = _channel_route(gateway, a, key)
    await _run_as_far_as_it_goes(route)
    finish_compaction.set()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(compaction, _HANG_CEILING_S)
    await asyncio.wait_for(asyncio.shield(route), _HANG_CEILING_S)
    await _settled(gateway)

    assert gateway._inline_parked == {}, (
        "a late mark held under a compaction is never settled, so its completion "
        "stays parked for the life of the gateway"
    )
    gateway._inject_with_retry.assert_awaited_once()
    assert a._report_delivered is True


# ── what the tab route and the done read owe on every ending ─────────────────


async def _hold_until(sessions: Any, key: str, holds: asyncio.Event, ends: asyncio.Event) -> None:
    """Another turn's holding of *key*: claim, say so, wait, release with no note."""
    await sessions.get_or_create(key)
    holds.set()
    await ends.wait()
    sessions.release(key)


@pytest.mark.asyncio
async def test_only_the_parent_tools_done_read_under_the_spawning_turn_names_a_holding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``POST /api/spawn`` and ``GET /api/spawn/{id}``. A done read names a
    holding (``_seen_done_under``) only when it is the parent's own tool's read under
    the attempt that spawned the run (``_spawned_under``), and only when that attempt
    answered from the read to the reply. A read from another session or without
    internal auth names nothing; a read whose spawning turn ended while it awaited
    names nothing; a read under the next holder of the permit (another turn, the
    same task's next claim) names nothing; and a later read leaves a stamped one."""
    from chat_test_helpers import _make_state
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.dashboard.messaging_api import run_views

    key = "telegram:42"
    state = _make_state(tmp_path)
    sessions = _new_sessions()
    state.sessions = sessions
    child = _returned_child("c1", key)
    later_child = _returned_child("c2", key)
    records = {child.id: child, later_child.id: later_child}
    state.subagents = MagicMock(get=MagicMock(side_effect=records.get), max_concurrent=4)

    async def _read(run: Any = child, caller: str = key, *, internal: bool = True) -> None:
        req = _Req(state, run.id)
        if internal:
            req["internal_auth"] = True
        req.headers["X-Session-Key"] = caller
        assert (await run_views.api_spawn_status(req)).status == 200

    await sessions.get_or_create(key)  # turn T, holding in this task
    _prompt_in_flight(sessions, key)  # T's prompt, and each later holder's, is answering
    await _spawn_from_the_parents_tool(state, key, child)  # T's call spawns c1
    t_holding = sessions.turn_token(key)
    assert t_holding is not None and child._spawned_under == t_holding
    await _read(caller="telegram:7")
    await _read(internal=False)
    assert child._seen_done_under is None, "a read that was not the parent's own tool named it"

    real_view = messaging._apply_result_view
    holds, ends = asyncio.Event(), asyncio.Event()
    successor: list[asyncio.Task[None]] = []

    async def _the_turn_ends_while_the_read_awaits(request: Any, text: str) -> Any:
        sessions.release(key)  # T is stopped; S claims the session
        successor.append(asyncio.create_task(_hold_until(sessions, key, holds, ends)))
        await asyncio.wait_for(holds.wait(), _HANG_CEILING_S)
        return await real_view(request, text)

    monkeypatch.setattr(messaging, "_apply_result_view", _the_turn_ends_while_the_read_awaits)
    await _read()
    assert child._seen_done_under is None, "a read whose turn ended named the successor"
    monkeypatch.setattr(messaging, "_apply_result_view", real_view)

    await _read()  # T's call reads on under S: S did not spawn c1
    s_holding = sessions.turn_token(key)
    assert s_holding is not None and s_holding != t_holding
    assert child._seen_done_under is None, "a read under another turn named that turn"

    # A call in S's turn spawns c2 and reads it done: stamped under S. The routes
    # name the attempt holding the permit, not the task that asks.
    await _spawn_from_the_parents_tool(state, key, later_child)
    await _read(later_child)
    assert later_child._spawned_under == s_holding
    assert later_child._seen_done_under == s_holding
    ends.set()
    await asyncio.wait_for(successor[0], _HANG_CEILING_S)

    await sessions.get_or_create(key)  # turn U, the same task's next claim
    await _read()
    await _read(later_child)
    assert child._seen_done_under is None, "a read under a later turn named that turn"
    assert later_child._seen_done_under == s_holding, "a later read moved the stamped holding"
    sessions.release(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("lands", [True, False], ids=["landed", "revoked"])
async def test_a_queued_tab_completion_is_settled_only_by_its_turns_landed_release(
    tmp_path, lands: bool
) -> None:
    """A completion already QUEUED on a cron job's tab when the mark arrives stays
    queued under the live turn. A landed release removes it and pays its owed
    delivery, as the mark does for a dashboard-born parent, so no second turn
    plays. A revoked release leaves it queued, to play once."""
    from chat_test_helpers import _make_state

    from kiro_crew.constants import SUBAGENT_COMPLETION_META_KEY
    from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND
    from kiro_crew.subagent import SubagentDelivery
    from kiro_crew.subagent_completion_meta import single_completion_meta

    key = "cron:j1"
    state = _make_state(tmp_path)
    sessions = _new_sessions()
    state.sessions = sessions
    child = _returned_child("a1", key)
    orch = _cron_orchestrator(sessions, [child])
    orch.dashboard_state = state
    state.subagents = orch.subagent_mgr
    slot = state.get_or_create_slot("cron-j1")
    slot.linked_session_key = key
    owed = [SubagentDelivery("a1", 1.0, 0.0)]
    with _surfaced(key):
        announce = "[Subagent completion event] a1 finished"
        slot.queue_append(
            announce,
            kind=SUBAGENT_COMPLETION_KIND,
            meta={
                SUBAGENT_COMPLETION_META_KEY: single_completion_meta(agent_id="a1", outcome="ok")
            },
        )
        slot.note_pending_subagent_delivery(announce, owed)
        await sessions.get_or_create(key)  # the cron turn, answering with the reply
        assert _mark_under_the_live_turn(sessions, orch.subagent_mgr, key, child.id) == {"a1"}
        if lands:
            sessions.note_turn_landed(key)
        sessions.release(key)
        await _settled(orch)
    queued = [q for q in slot._queue if q.get("kind") == SUBAGENT_COMPLETION_KIND]
    if lands:
        assert queued == [], "a landed turn's returned result still plays as a second turn"
        orch.subagent_mgr.settle_queued_delivery.assert_awaited_once_with(owed)
    else:
        assert len(queued) == 1, "a revoked mark removed the only delivery of its result"
        orch.subagent_mgr.settle_queued_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cut_cron_turns_tab_completion_holds_the_cron_reset_until_its_turn_ends(
    done: Any,
) -> None:
    """The tab's turn runs on the cron job's own session, so a completion the cut
    cron turn's release re-routes to the tab is counted in ``_cron_injecting``
    inside that release, before the cron turn's cleanup decides to reset the
    session. The count is held until the tab's turn has delivered it; the deferred
    reset runs after, never under a live turn."""
    seen = await _drive_tab_parent(done, _CRON_TAB, lands=False)

    assert seen["counted_at_release"] == 1, "the cron turn's cleanup could reset under it"
    assert len(seen["chats"]) == 1
    assert done.orch._cron_injecting == {}
    assert seen["resets"] == [("cron:j1", {"skip_if_busy": True})]


@pytest.mark.asyncio
@pytest.mark.parametrize("lands", [True, False], ids=["landed", "revoked"])
async def test_a_parked_tab_digest_owes_its_held_siblings_on_every_ending(
    done: Any, lands: bool
) -> None:
    """A parked completion's digest carries held wave siblings. A landed release
    acknowledges it and pays the member's mark and the siblings' together. A
    revoked one hands it to the tab's turn, which owes both to its consumption
    (``_defer_queued_delivery``), never to nobody."""
    from kiro_crew.subagent import SubagentDelivery

    sibling = SubagentDelivery("sib-1", 2.0, 0.1)
    seen = await _drive_tab_parent(done, _SLACK_TAB, lands=lands, held=(sibling,))
    own = SubagentDelivery("child-1", 12.0, 0.25)

    if lands:
        assert seen["chats"] == []
        done.mgr.settle_queued_delivery.assert_awaited_once_with([own, sibling])
    else:
        assert len(seen["chats"]) == 1
        assert [owed for _announce, owed in seen["slot"].owed] == [[own, sibling]]
        done.mgr.settle_queued_delivery.assert_not_awaited()
    assert seen["info"]._digest_settle_deliveries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [_CRON_TAB, _SLACK_TAB], ids=["cron-tab", "slack-tab"])
async def test_a_parked_tab_completion_whose_tab_closed_takes_its_parents_own_route(
    done: Any, parent: tuple[str, str]
) -> None:
    """The tab closes while the turn that received the result holds the session,
    and that turn is then cut. The completion is routed as a fresh one would be
    now: a cron parent's to the cron session (the cron route takes the count over),
    a channel parent's to its own session. It is delivered once."""
    seen = await _drive_tab_parent(done, parent, lands=False, close_tab=True)

    assert seen["chats"] == [], "a closed tab was handed the completion"
    assert len(_streams(done)) == 1, "the parent's own session was not given the completion"
    assert seen["info"]._report_delivered is True
    assert done.orch._cron_injecting == {}
    assert done.orch._inline_parked == {}


# ── a token names one attempt, so no later turn of the same task shares it ───


@pytest.mark.asyncio
async def test_a_failed_attempts_mark_processed_in_the_retry_backoff_is_not_held_under_the_retry() -> (
    None
):
    """Attempt 1's tool read G done and its nested reply carrying G was written; then
    the backend failed with an ``AcpError``. G's mark is processed only in the
    retry's backoff, after ``settle_turn`` ended attempt 1, in the same task and the
    same claim. The retry is a new attempt with a new token, so the mark is not held
    under it, its landing confirms nothing it did not receive, and G's completion is
    delivered as a turn of its own."""
    from kiro_crew.acp.client import AcpError

    sessions = _new_sessions()
    key = "slack:C1:1700000000.1"
    x = _returned_child("child-x", key)
    g = _returned_child("child-g", key)
    gateway = _nested_gateway(sessions, x, g)
    seen: dict[str, Any] = {"turns": []}
    in_backoff = asyncio.Event()
    settle = sessions.settle_turn

    def settle_then_back_off(settled_key: str) -> None:
        settle(settled_key)
        in_backoff.set()  # attempt 1 is settled; the retry's backoff sleep follows

    sessions.settle_turn = settle_then_back_off

    async def mark_in_the_backoff() -> None:
        await in_backoff.wait()
        seen["marked"] = _late_mark(sessions, gateway.subagent_mgr, key, g.id)
        seen["g_route"] = _channel_route(gateway, g, key)
        await _run_as_far_as_it_goes(seen["g_route"])

    async def stream(_client: Any, message: str, **kw: Any) -> str:
        seen["turns"].append(message)
        if len(seen["turns"]) == 1:
            _poll_done(sessions, gateway.subagent_mgr, key, g.id)  # the reply carrying G
            raise AcpError("the backend failed mid-turn")  # attempt 1 never lands
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))
        return f"answered {message}"

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        marker = asyncio.create_task(mark_in_the_backoff())
        await asyncio.wait_for(_channel_route(gateway, x, key), _HANG_CEILING_S)
        await asyncio.wait_for(marker, _HANG_CEILING_S)
        await asyncio.wait_for(asyncio.shield(seen["g_route"]), _HANG_CEILING_S)
        await _settled(gateway)

    assert seen["marked"] == set(), "the failed attempt's mark was held under its retry"
    assert seen["turns"][:2] == ["child-x done", "child-x done"]
    assert seen["turns"][2:] == ["child-g done"], (
        "the retry's landing confirmed a result only the failed attempt received "
        f"(turns={seen['turns']!r})"
    )
    assert g._report_delivered is True
    assert gateway._inline_parked == {}


@pytest.mark.asyncio
async def test_a_late_mark_is_not_held_under_the_same_tasks_next_turn() -> None:
    """An engine that runs a conversation's turns in one task releases a stopped
    turn and claims the session again for the next message. The stopped turn's
    tool had read A done; A's mark is processed only now, under that same task's
    next claim. The claim is a new attempt with a new token, so the mark is not
    held there and A's completion is delivered."""
    sessions = _new_sessions()
    key = "telegram:42"
    a = _returned_child("child-a", key)
    gateway = _channel_gateway(sessions, a)
    await sessions.get_or_create(key)  # the stopped turn: its reply carrying A was written
    _poll_done(sessions, gateway.subagent_mgr, key, a.id)
    sessions.release(key)  # stopped: released with no note
    await sessions.get_or_create(key)  # the same task's next turn, for the next message

    marked = _late_mark(sessions, gateway.subagent_mgr, key, a.id)  # processed only now
    route = _channel_route(gateway, a, key)
    await _run_as_far_as_it_goes(route)
    sessions.note_turn_landed(key)  # the next message's turn lands
    sessions.release(key)
    await asyncio.wait_for(asyncio.shield(route), _HANG_CEILING_S)
    await _settled(gateway)

    assert marked == set(), "the stopped turn's mark was held under the same task's next turn"
    gateway._inject_with_retry.assert_awaited_once()
    assert a._report_delivered is True
    assert gateway._inline_parked == {}


def test_each_attempt_of_a_holding_gets_a_token_no_earlier_attempt_had() -> None:
    from kiro_crew.parent_turns import ParentTurnLedger

    ledger = ParentTurnLedger()
    settled: list[tuple[Any, bool, list[str]]] = []
    ledger.on_released = lambda token, landed, ids: settled.append((token, landed, ids))
    first = ledger.open_attempt("telegram:42", "task")
    assert ledger.open_attempt("telegram:42", "task") == first  # one attempt, one token
    assert ledger.open_token("telegram:42", "task") == first
    ledger.hold_marks(first, ["c1"])

    ledger.end_attempt("telegram:42", "other-task")  # another task's end settles none of it
    assert ledger.pending(first)
    ledger.end_attempt("telegram:42", "task")

    assert settled == [(first, False, ["c1"])]
    assert ledger.open_token("telegram:42", "task") is None  # nothing open until the next opens
    second = ledger.open_attempt("telegram:42", "task")
    assert second[:2] == first[:2] and second[2] > first[2]
    ledger.end_attempt("telegram:42", "task")
    assert settled == [(first, False, ["c1"])], "an attempt with no marks settled something"


@pytest.mark.asyncio
async def test_an_attempt_its_finished_task_left_open_is_ended_by_the_next_open() -> None:
    """A holding whose permit is given back with a raw ``semaphore.release()`` (a
    compaction) never ends its attempt itself. Once its task has finished, the next
    attempt opened anywhere ends it: its marks are settled as its release would
    have settled them, and the ledger keeps no attempt of a finished task."""
    from kiro_crew.parent_turns import ParentTurnLedger

    ledger = ParentTurnLedger()
    settled: list[tuple[Any, bool, list[str]]] = []
    ledger.on_released = lambda token, landed, ids: settled.append((token, landed, ids))

    async def compaction() -> Any:
        token = ledger.open_attempt("telegram:42", asyncio.current_task())
        ledger.hold_marks(token, ["c1"])
        return token

    finished = asyncio.create_task(compaction())
    token = await finished
    live = ledger.open_attempt("telegram:42", "next-turn")

    assert settled == [(token, False, ["c1"])]
    assert ledger.open_token("telegram:42", finished) is None
    assert ledger.open_token("telegram:42", "next-turn") == live


# ── the cron run's idle cleanup retires its key unless a turn holds the session ─


def _cron_route_gateway(key: str) -> tuple[Any, Any, Any]:
    from test_subagent_injection_live_turn import _gateway, _provider_factory

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    gateway = _gateway(sessions)
    gateway.cron_svc = MagicMock(name="cron_svc")
    info = _returned_child("child-1", key)
    gateway.subagent_mgr.get = MagicMock(side_effect={info.id: info}.get)
    return sessions, gateway, info


@pytest.mark.asyncio
async def test_a_cron_run_whose_last_completion_timed_out_still_retires_its_key() -> None:
    """The last completion of a cron run times out in the cron route, and the
    timeout arm resets the parent session. The run's idle cleanup then finds no
    session to reset and no turn holding one, so it still retires the run's key: a
    key left registered keeps the finished run live to job ownership for good."""
    key = "cron:j1:r1"
    sessions, gateway, info = _cron_route_gateway(key)
    never = asyncio.Event()

    async def hangs(*_args: Any, **_kwargs: Any) -> str:
        await never.wait()
        return "unreachable"

    gateway._inject_with_retry = AsyncMock(side_effect=hangs)
    with patch("kiro_crew.slack.gateway.INJECTION_TIMEOUT", 0.05):
        await asyncio.wait_for(
            gateway._finish_subagent_cron_or_notify(
                info, announce="done", body="b", title="t", carried=(info.id,), parent_key=key
            ),
            _HANG_CEILING_S,
        )

    assert key not in sessions._sessions, "the timeout arm did not reset the parent"
    gateway.cron_svc.retire_finished_session_key.assert_called_once_with("j1", key)
    assert gateway._cron_injecting == {}


@pytest.mark.asyncio
async def test_the_idle_cleanup_retires_the_key_of_a_cron_parent_already_gone() -> None:
    """The parent session was reaped or reset by another path before the run's last
    completion: nothing is left to reset and nothing holds it, so the key goes."""
    key = "cron:j1:r1"
    sessions, gateway, info = _cron_route_gateway(key)

    await gateway._reset_cron_parent_when_idle(info, key)

    assert key not in sessions._sessions
    gateway.cron_svc.retire_finished_session_key.assert_called_once_with("j1", key)


@pytest.mark.asyncio
async def test_a_turn_that_claims_the_cron_parent_during_the_reset_keeps_the_session() -> None:
    """Nothing holds the session when the cleanup starts, but a turn that is not the
    cron run's own claims it while the reset runs, so the reset declines. The turn
    keeps the session it is using and the finished run's key is retired, exactly as
    when that turn already held the session."""
    key = "cron:j1:r1"
    sessions, gateway, info = _cron_route_gateway(key)
    reset = sessions.reset

    async def reset_while_a_turn_claims(reset_key: str, **kwargs: Any) -> bool:
        await sessions.get_or_create(reset_key)  # a tab turn claims it during the reset
        return await reset(reset_key, **kwargs)

    sessions.reset = reset_while_a_turn_claims

    await gateway._reset_cron_parent_when_idle(info, key)

    assert key in sessions._sessions, "the reset ran under the turn that claimed it"
    gateway.cron_svc.retire_finished_session_key.assert_called_once_with("j1", key)
    sessions.release(key)


def _registered(gateway: Any, key: str) -> Any:
    """A real cron service with the run's key registered, as the cron callback does."""
    from kiro_crew.cron import CronService

    svc = CronService()
    svc.register_active_session_key("j1", key)
    gateway.cron_svc = svc
    return svc


@pytest.mark.asyncio
async def test_a_tab_or_user_turn_keeps_a_finished_runs_session_and_the_key_is_retired() -> None:
    """The run's last completion is cleaned up while a turn that is not the cron
    run's own (a tab that shows the job, or a person's own) holds the session. That
    turn keeps the session, left to the idle sweep, and the finished run's key is
    retired, so job ownership does not count the run as live."""
    key = "cron:j1:r1"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = _registered(gateway, key)
    provider, _is_new, _resumed = await sessions.get_or_create(key)  # the tab turn
    session = sessions._sessions[key]

    await gateway._reset_cron_parent_when_idle(info, key)

    assert sessions._sessions.get(key) is session, "the session was reset under the tab turn"
    provider.shutdown.assert_not_awaited()
    assert key not in svc.active_session_keys(), "the finished run still looks live"
    sessions.release(key)


@pytest.mark.asyncio
async def test_the_cron_runs_own_turn_keeps_its_key_for_its_finalizer() -> None:
    """The same cleanup while the cron run's own turn holds the session: the session
    and the key are both left to that turn's finalizer."""
    key = "cron:j1:r1"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = _registered(gateway, key)
    run_task = asyncio.current_task()
    gateway._cron_runs_holding.add(run_task)  # the cron run's own task
    provider, _is_new, _resumed = await sessions.get_or_create(key)  # the cron turn

    await gateway._reset_cron_parent_when_idle(info, key)

    provider.shutdown.assert_not_awaited()
    assert key in svc.active_session_keys(), "the key was retired under the run's own turn"
    gateway._cron_runs_holding.discard(run_task)
    sessions.release(key)


@pytest.mark.asyncio
async def test_an_idle_cron_parent_is_reset_and_its_key_retired() -> None:
    """Nothing holds the run's session: it is reset and the key is retired."""
    key = "cron:j1:r1"
    sessions, gateway, info = _cron_route_gateway(key)
    svc = _registered(gateway, key)
    provider, _is_new, _resumed = await sessions.get_or_create(key)
    sessions.release(key)  # the cron turn ended earlier; nothing holds the session

    await gateway._reset_cron_parent_when_idle(info, key)

    assert key not in sessions._sessions, "the idle session was not reset"
    provider.shutdown.assert_awaited()
    assert key not in svc.active_session_keys()


@pytest.mark.asyncio
async def test_a_cron_turn_resumed_after_a_post_token_error_confirms_nothing_its_first_prompt_got() -> (
    None
):
    """The cron turn's first prompt read A done and its reply carrying A was written
    and marked; then a transient backend error cut it after text streamed, and the
    dispatcher sent its one-shot CONTINUE re-prompt on the live session. The
    continuation is a new attempt, so its landing confirms nothing the first prompt
    received: A's completion is delivered as a turn of its own, a duplicate at worst,
    never a loss."""
    from test_spawn_sub_agents_duplicate_never_loss import _cron_callback, _cron_job
    from test_subagent_injection_live_turn import _provider_factory

    from kiro_crew.acp.client import AcpError
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    key = "cron:j1:r1"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    a = _returned_child("child-a", key)
    orch = _cron_orchestrator(sessions, [a])
    seen: dict[str, Any] = {"prompts": [], "injected": []}

    async def inject(info: Any, _client: Any, _msg: Any, _parent: str, _label: str) -> str:
        seen["injected"].append(info.id)
        info._report_delivered = True  # _inject_with_retry acknowledges on landing
        return "answered from the completion"

    orch._inject_with_retry = inject
    callback = await _cron_callback(orch)

    async def stream(_client: Any, message: Any, **kw: Any) -> str:
        seen["prompts"].append(message)
        if len(seen["prompts"]) == 1:
            _mark_under_the_live_turn(sessions, orch.subagent_mgr, key, a.id)
            seen["route"] = asyncio.create_task(
                orch._finish_subagent_cron_or_notify(
                    a, announce="child-a done", body="b", title="t", carried=(a.id,), parent_key=key
                )
            )
            await _run_as_far_as_it_goes(seen["route"])
            kw["on_chunk"]("partial answer ")  # text streamed before the error
            err = AcpError("Bedrock ThrottlingException: rate exceeded")
            err.transient = True
            raise err
        kw["on_complete"](SimpleNamespace(stop_reason="end_turn"))  # the continuation lands
        return "the rest of the answer"

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=(key, "task")),
        patch("kiro_crew.slack.gateway.transient_retry_delay", return_value=0.0),
    ):
        await asyncio.wait_for(callback(_cron_job()), _HANG_CEILING_S)
        await asyncio.wait_for(asyncio.shield(seen["route"]), _HANG_CEILING_S)
        await _settled(orch)

    assert len(seen["prompts"]) == 2, "the one-shot CONTINUE re-prompt did not run"
    assert seen["injected"] == ["child-a"], (
        "the continuation's landing confirmed a result only the interrupted prompt "
        f"received (prompts={len(seen['prompts'])}, injected={seen['injected']!r})"
    )
    assert a._report_delivered is True


@pytest.mark.asyncio
async def test_the_real_cron_turn_keeps_its_key_while_a_confirmed_completion_is_cleaned_up() -> (
    None
):
    """The real cron callback: while its turn answers, a child whose result an earlier
    turn already confirmed completes. Its route acknowledges it with no claim and runs
    the run's cleanup under the live cron turn, which the cron engine marked as the
    run's own. The key stays for that turn's finalizer, which retires it once the turn
    ends."""
    from test_spawn_sub_agents_duplicate_never_loss import _confirmed, _cron_callback, _cron_job
    from test_subagent_injection_live_turn import _provider_factory

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    key = "cron:j1:r1"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    c = _returned_child("child-c", key)
    _confirmed(c)
    orch = _cron_orchestrator(sessions, [c])
    callback = await _cron_callback(orch)
    seen: dict[str, Any] = {}

    async def stream(_client: Any, _message: Any, **kw: Any) -> str:
        route = orch._finish_subagent_cron_or_notify(
            c, announce="child-c done", body="b", title="t", carried=(c.id,), parent_key=key
        )
        await asyncio.wait_for(route, _HANG_CEILING_S)  # acknowledged, no claim
        seen["alive_while_live"] = key in sessions._sessions
        seen["retired_while_live"] = (
            orch.cron_svc.clear_active_session_key.called
            or orch.cron_svc.retire_finished_session_key.called
        )
        kw["on_complete"](SimpleNamespace(stop_reason="end_turn"))
        return "the cron's answer"

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=(key, "task")),
    ):
        await asyncio.wait_for(callback(_cron_job()), _HANG_CEILING_S)

    assert seen["alive_while_live"], "the cleanup reset the session under the cron turn"
    assert seen["retired_while_live"] is False, "the key was retired under the run's own turn"
    orch.cron_svc.clear_active_session_key.assert_called_with("j1", key)  # by the finalizer
    assert key not in sessions._sessions
    assert orch._cron_runs_holding == set(), "the run's own turn was left marked"


# ── a mark is held only under the attempt that spawned the child ─────────────


async def _drive_a_cut_turns_call(
    tmp_path: Any, *, first_read_under_cut_turn: bool
) -> dict[str, Any]:
    """The real cron route, the real ``_inject_with_retry``, the real
    ``POST /api/spawn``, the real ``GET /api/spawn/{id}`` and the real mark
    flagging, over a real SessionManager.

    The cron run's ``spawn_sub_agents`` call spawns A and C and waits on both. C
    finishes while the run's turn holds the session. The wake budget then cancels
    the run: its turn releases with no landing note, and kiro-cli is not told
    (``session_handle_turn.py``), so the call reads on. C's completion claims the
    session for its injection. A finishes while that injection holds the permit;
    the call reads it done, writes its reply into the abandoned turn and marks A
    and C. Every route call runs on its own task, as a child's report does.
    """
    from chat_test_helpers import _make_state
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.messaging_api import run_views

    sessions = _new_sessions()
    key = "cron:j1:r1"
    a = _returned_child("child-a", key)
    a.done = False  # still running while the cron turn holds the session
    c = _returned_child("child-c", key)
    orch = _cron_orchestrator(sessions, [a, c])
    orch.ctx_builder.build_message = MagicMock(
        side_effect=lambda announce, *_a, **_k: (announce, None)
    )
    state = _make_state(tmp_path)
    state.sessions = sessions
    state.subagents = orch.subagent_mgr

    async def call_reads(aid: str) -> None:
        """The parent's own call polling one child: internal auth, its own session."""
        req = _Req(state, aid)
        req["internal_auth"] = True
        req.headers["X-Session-Key"] = key
        assert (await run_views.api_spawn_status(req)).status == 200

    seen: dict[str, Any] = {"turns": []}

    async def stream(_client: Any, message: str, **kw: Any) -> str:
        seen["turns"].append(message)
        _prompt_in_flight(sessions, key)  # this injection's prompt is answering
        if message == "child-c done":
            # C's injection holds the permit. A finishes; the abandoned turn's call
            # reads it done (its last child), writes the reply and marks.
            a.done = True
            await call_reads(a.id)
            seen["stamp_a"] = a._seen_done_under
            seen["marked"] = _late_mark(sessions, orch.subagent_mgr, key, a.id, c.id)
            seen["a_route"] = asyncio.create_task(
                orch._finish_subagent_cron_or_notify(
                    a, announce="child-a done", body="b", title="t", carried=(a.id,), parent_key=key
                )
            )
            await _run_as_far_as_it_goes(seen["a_route"])
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))  # this injected turn landed
        _prompt_in_flight(sessions, key, False)
        return f"answered {message}"

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await sessions.get_or_create(key)  # the cron run's turn
        _prompt_in_flight(sessions, key)  # its prompt is answering
        await _spawn_from_the_parents_tool(state, key, a, c)  # its call spawns A and C
        seen["spawned_under"] = sessions.turn_token(key)
        await call_reads(c.id)  # C done under the cron turn: stamped there
        if first_read_under_cut_turn:
            a.done = True  # A also finished, and was read, under the cron turn
        await call_reads(a.id)
        seen["stamp_a_before_cut"] = a._seen_done_under
        sessions.release(key)  # the wake budget cancelled the run: no landing note
        _prompt_in_flight(sessions, key, False)  # its consumer left the prompt
        c_route = asyncio.create_task(
            orch._finish_subagent_cron_or_notify(
                c, announce="child-c done", body="b", title="t", carried=(c.id,), parent_key=key
            )
        )
        await asyncio.wait_for(c_route, _HANG_CEILING_S)
        await _settled(orch)
        if "a_route" in seen:
            await asyncio.wait_for(seen["a_route"], _HANG_CEILING_S)
        await _settled(orch)
    seen["a_spawned_under"] = a._spawned_under
    seen["parked_after"] = dict(orch._inline_parked)
    return seen


@pytest.mark.asyncio
async def test_a_cut_turns_call_never_confirms_a_child_under_a_siblings_injection(
    tmp_path: Any,
) -> None:
    """A cron run cut by its wake budget leaves its ``spawn_sub_agents`` call reading
    on. A child it first reads done while a sibling's injection holds the permit is
    stamped under nothing, since that injection did not spawn it: the call's mark
    holds nothing, the sibling's landing confirms nothing for it, and the child's
    completion is injected in a turn of its own."""
    seen = await _drive_a_cut_turns_call(tmp_path, first_read_under_cut_turn=False)

    assert seen["a_spawned_under"] == seen["spawned_under"] is not None
    assert seen["stamp_a_before_cut"] is None
    assert seen["stamp_a"] is None, "a read under the sibling's injection named it"
    assert "child-a" not in seen["marked"], "the cut turn's mark was held under the sibling"
    assert seen["turns"][:1] == ["child-c done"]
    assert "child-a done" in seen["turns"], (
        "a child first read done by a cut turn's call under a sibling's injection was "
        f"acknowledged by that sibling's landing and never delivered (turns={seen['turns']!r})"
    )
    assert seen["parked_after"] == {}


@pytest.mark.asyncio
async def test_a_cut_turns_late_mark_of_a_child_it_read_done_before_the_cut_is_delivered(
    tmp_path: Any,
) -> None:
    """A finished and was read done under the cron turn that spawned it, before the
    cut, so its stamp names the cut turn; the late mark under the sibling's
    injection holds nothing and A is delivered."""
    seen = await _drive_a_cut_turns_call(tmp_path, first_read_under_cut_turn=True)

    assert seen["stamp_a_before_cut"] == seen["spawned_under"] is not None
    assert "child-a done" in seen["turns"], f"turns={seen['turns']!r}"
    assert seen["parked_after"] == {}


@pytest.mark.parametrize("leaked", [True, False])
def test_a_parked_completion_keeps_its_leaked_process_flag(leaked: bool) -> None:
    """A completion parked on a pending mark owes the tombstone its run loop did not
    write, so a teardown that left the process running rides on that owed mark;
    without it the settled tombstone hides the process from leaked-process recovery."""
    from kiro_crew.slack.gateway_runtime.subagent_injection import _park_inline

    orch = SimpleNamespace(_inline_parked={})
    info = make_info()
    assert info.outcome == "completed"
    info._leaked_process = leaked
    _park_inline(
        orch,  # type: ignore[arg-type]
        "token-1",
        info,
        route="cron",
        announce="done",
        body="b",
        title="t",
        carried=(info.id,),
        parent_key="cron:job-1",
    )

    (completion,) = orch._inline_parked["token-1"]
    assert [d.leaked_process for d in completion.own] == [leaked]
    assert info._delivery_queued is True
