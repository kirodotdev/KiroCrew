"""Code-channel view tabs: native PR tabs, tab deep links, the interactive Workers tab.

Stop from the Workers tab: its gates, its final gate and the Stopped by line.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channel_view_tabs_helpers import (  # noqa: F401 - fixtures by name
    _NOW,
    _STOP_KW,
    _TRAILING,
    CH,
    OTHER_CH,
    PR_URL,
    SESSION,
    SLOT,
    STRANGER,
    WORKERS_VIEW,
    _bind,
    _calls,
    _link,
    _project,
    _Refusing,
    _resumed,
    _row,
    _scope_flag,
    _set_views,
    _stop_payload,
    _texts,
    _v,
    env_fixture,
    no_host_dns_fixture,
    stop_world_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.slack.code_channels import actions, surface, tabs, views, worker_stop
from kiro_crew.slack.code_channels.board import model, publisher, render, source
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import SurfaceStore
from kiro_crew.slack.code_channels.surface_store import store as surface_store


@pytest.mark.asyncio
async def test_stop_stops_only_that_workers_turn(env: Any, stop_world: Any) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    assert stop_world.stopped == [("chat-w1", _STOP_KW)]
    assert stop_world.reads == [CH]  # resolved on THIS channel's own board
    assert stop_world.noted == ["chat-w1"]
    row = env.sel.ops(worker_stop.OPERATION)[-1]
    assert row["outcome"] == "allowed" and "slot=chat-w1" in row["resources"]
    assert env.slack.ephemerals[-1] == (
        CH,
        OWNER,
        "Stopping *Fix login*. The conductor keeps going.",
    )


@pytest.mark.parametrize("view_id", ["VSOMEONEELSE", "Vreport", ""])
@pytest.mark.asyncio
async def test_a_click_from_any_other_view_stops_nothing(
    env: Any, stop_world: Any, view_id: str
) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload(view_id=view_id)) == "denied"
    assert stop_world.stopped == []
    assert env.slack.ephemerals[-1] == (CH, OWNER, worker_stop.NOT_THIS_TAB_TEXT)
    assert env.sel.ops(worker_stop.OPERATION)[-1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_a_channel_with_no_workers_tab_of_ours_takes_no_stop(
    env: Any, stop_world: Any
) -> None:
    payload = _stop_payload(channel=OTHER_CH, value="it_00000009", view_id=WORKERS_VIEW)
    assert await worker_stop._handle(env.orch, payload) == "denied"
    assert stop_world.stopped == []


@pytest.mark.parametrize(
    "value", ["it_00000009", "chat-w9", "dashboard:chat-w9", "it_0000000c", "it_deadbeef"]
)
@pytest.mark.asyncio
async def test_the_target_must_be_a_worker_on_this_channels_own_board(
    env: Any, stop_world: Any, value: str
) -> None:
    # Another channel's item, a raw session key, the conductor itself, an unknown item.
    assert await worker_stop._handle(env.orch, _stop_payload(value=value)) == "denied"
    assert stop_world.stopped == []
    assert env.slack.ephemerals[-1] == (CH, OWNER, worker_stop.NOT_ON_BOARD_TEXT)


@pytest.mark.asyncio
async def test_a_payload_session_key_is_never_read(env: Any, stop_world: Any) -> None:
    payload = _stop_payload()
    payload["actions"][0]["session_key"] = "chat-w9"
    payload["session_key"] = "chat-w9"
    payload["container"]["session_key"] = "chat-w9"
    await worker_stop._handle(env.orch, payload)
    assert [key for key, _kw in stop_world.stopped] == ["chat-w1"]


@pytest.mark.asyncio
async def test_a_person_the_context_bar_stop_would_refuse_stops_nothing(
    env: Any, stop_world: Any
) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload(user=STRANGER)) == "denied"
    assert stop_world.stopped == []
    assert env.slack.ephemerals[-1] == (CH, STRANGER, actions.NOT_AUTHORIZED_TEXT)


@pytest.mark.asyncio
async def test_the_stop_uses_the_context_bar_stops_own_rule(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    asked: list[tuple[str, str]] = []

    def may_stop(orch: Any, binding: Binding, user: str) -> bool:
        asked.append((binding.channel_id, user))
        return False

    monkeypatch.setattr(actions, "_may_stop", may_stop)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert asked == [(CH, OWNER)] and stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_stop_in_a_channel_the_policy_refuses_stops_nothing(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from slack_code_channel_policy_helpers import allow_only, policy_refusals

    rows = allow_only(monkeypatch, OTHER_CH)  # allowed_channel_ids leaves this channel out
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == [] and stop_world.reads == []  # refused at the entry gate
    assert env.slack.ephemerals == []  # dropped without a reply
    assert policy_refusals(rows, worker_stop.OPERATION)  # the policy seam's audit row


@pytest.mark.asyncio
async def test_a_channel_refused_during_the_board_read_stops_nothing(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from slack_code_channel_policy_helpers import allow_only, policy_refusals

    real_read = source.read_board
    rows: dict[str, Any] = {}

    async def read_board(state: Any, b: Binding) -> Any:
        rows["sel"] = allow_only(monkeypatch, OTHER_CH)  # the ceiling changes meanwhile
        return await real_read(state, b)

    monkeypatch.setattr(source, "read_board", read_board)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == [] and env.slack.ephemerals == []  # no reply either
    assert policy_refusals(rows["sel"], worker_stop.OPERATION)


@pytest.mark.asyncio
async def test_the_final_gate_asks_the_channel_ceiling_again(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from slack_code_channel_policy_helpers import allow_only

    # The workspace re-check reads the installed context, which a click's entry
    # gate installs; this test calls the gate alone.
    monkeypatch.setattr(worker_stop, "_workspace_still_admitted", lambda team: True)
    from kiro_crew.platform.context import current_context

    current_context()  # installed, as the click's entry gate leaves it
    click, view_id, _value = worker_stop.parse(_stop_payload())
    cfg = worker_stop.live_config(env.orch)
    gate = lambda: worker_stop._final_gate(  # noqa: E731
        env.orch, env.state, stop_world.binding, click, view_id, "chat-w1", cfg=cfg
    )
    from kiro_crew.slack.code_channels.surface_store import get_async

    await get_async(CH)
    assert not isinstance(gate(), str)  # admitted while the ceiling names the channel
    rows = allow_only(monkeypatch, OTHER_CH)
    assert gate() == worker_stop._CHANNEL_REFUSED
    assert rows == []  # re-read unaudited: the entry gate wrote this decision's row


@pytest.mark.asyncio
async def test_a_context_bar_click_in_a_channel_the_policy_refuses_is_not_admitted(
    env: Any, monkeypatch: Any
) -> None:
    from slack_code_channel_policy_helpers import allow_only, policy_refusals

    _bind(env)
    rows = allow_only(monkeypatch, OTHER_CH)
    click, _view, _value = worker_stop.parse(_stop_payload())
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    assert await actions.admit(click, respond, f"channel={CH}") is None
    assert policy_refusals(rows, actions.OPERATION) and replies == []


@pytest.mark.parametrize("variant", ["team", "inbound", "unbound"])
@pytest.mark.asyncio
async def test_a_stop_past_any_channel_gate_does_nothing(
    env: Any, stop_world: Any, monkeypatch: Any, variant: str
) -> None:
    payload = _stop_payload()
    if variant == "team":
        monkeypatch.setattr(surface, "workspace_admitted", lambda team: False)
    elif variant == "inbound":
        env.inbound_ok = False
    else:
        payload["channel"] = {"id": "C0UNBOUND"}
    assert await worker_stop._handle(env.orch, payload) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_double_click_and_a_redelivery_stop_once(env: Any, stop_world: Any) -> None:
    assert (await worker_stop._handle(env.orch, _stop_payload(ts="1.1"))).startswith("stop:")
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.1")) == "duplicate"
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.2")) == "repeat"
    assert len(stop_world.stopped) == 1


def test_the_workers_tab_shares_the_channel_buttons_dedupe_under_its_own_scope() -> None:
    worker_stop.reset_for_tests()
    click = actions.Click(channel=CH, user=OWNER, key="worker-stop", team="T1", dedupe="9.9")
    assert actions._first_delivery(click, worker_stop._DEDUPE_SCOPE)
    assert not actions._first_delivery(click, worker_stop._DEDUPE_SCOPE)  # one cache
    assert actions._first_delivery(click)  # a context-bar click with that id is its own
    assert not hasattr(worker_stop, "_seen") and worker_stop._id is actions._id


@pytest.mark.asyncio
async def test_a_worker_that_is_not_running_is_said_so_and_not_stopped(
    env: Any, stop_world: Any
) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload(value="it_00000002")) == "idle"
    assert stop_world.stopped == []
    assert env.slack.ephemerals[-1] == (CH, OWNER, "*Idle one* isn't running.")


@pytest.mark.asyncio
async def test_with_code_channels_off_the_stop_is_not_handled(env: Any, stop_world: Any) -> None:
    env.on = False
    assert await worker_stop.handle_stop_click(env.orch, _stop_payload()) is False
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_the_interactive_router_answers_the_workers_tab_buttons(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack import interactions

    got: list[Any] = []

    async def stop_click(orch: Any, payload: dict[str, Any]) -> bool:
        got.append(payload["actions"][0]["action_id"])
        return False  # even "not ours" must not fall through

    async def approval(*a: Any, **k: Any) -> None:
        got.append("fell-through")

    monkeypatch.setattr(worker_stop, "handle_stop_click", stop_click)
    monkeypatch.setattr(interactions, "_handle_tool_approval", approval)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: True)
    stop_click_payload = _stop_payload()
    # With a message ts, a fall-through would reach the tool-approval handler.
    stop_click_payload["message"] = {"ts": "1.0"}
    await interactions.dispatch(stop_click_payload)
    open_click = _stop_payload()
    open_click["message"] = {"ts": "1.0"}
    open_click["actions"][0]["action_id"] = "kc_cc_worker_open"
    await interactions.dispatch(open_click)
    assert got == ["kc_cc_worker_stop"]


@pytest.mark.asyncio
async def test_a_worker_slot_whose_running_turn_is_the_conductors_is_never_stopped(
    env: Any, stop_world: Any
) -> None:
    # The worker's slot was rebound mid-turn: the turn it would cancel is the
    # conductor's own session.
    stop_world.slots["chat-w1"]._active_turn_session_key = f"dashboard_{SLOT}"
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []
    row = env.sel.ops(worker_stop.OPERATION)[-1]
    assert row["outcome"] == "denied" and row["error"] == "the cancel target is not that worker"


@pytest.mark.asyncio
async def test_a_running_turn_of_another_slot_is_never_stopped(env: Any, stop_world: Any) -> None:
    stop_world.slots["chat-w1"]._active_turn_session_key = "dashboard_chat-w9"
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []


@pytest.mark.parametrize(
    "change",
    ["revoked", "rebound", "dropped", "archived", "view", "code_off", "team", "inbound"],
)
@pytest.mark.asyncio
async def test_what_changes_while_the_board_is_read_is_judged_again(
    env: Any, stop_world: Any, monkeypatch: Any, change: str
) -> None:
    real = source.read_board
    # The gateway's one config object, which a reload updates in place.
    shared = env.cfg()
    for mod in (worker_stop, actions):
        monkeypatch.setattr(mod, "live_config", lambda _orch=None: shared)

    async def read_board(state: Any, b: Binding) -> Any:
        # Every gate actions.admit passed is revoked mid-read in one variant.
        if change == "code_off":
            shared.slack.code_channels = False
        elif change == "team":
            from kiro_crew.platform.context import installed_context

            gate = installed_context().slack_gate
            monkeypatch.setattr(gate, "check_message_origin", lambda team: False)
        elif change == "inbound":
            env.inbound_ok = False
        elif change == "revoked":
            env.allowed.discard(OWNER)  # the presser loses access meanwhile
        elif change == "rebound":
            env.store.replace_binding(Binding(**{**b.__dict__, "session_key": "chat-new-1"}))
        elif change == "dropped":
            env.store.drop_binding(CH)
        elif change == "archived":
            surface.fence_archived(CH)
        else:
            surface_store().update(CH, views=[_v("workers", "block_kit", "2")])
        return await real(state, b)

    monkeypatch.setattr(source, "read_board", read_board)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_worker_the_store_says_another_conductor_made_is_not_stopped(
    env: Any, stop_world: Any
) -> None:
    env.store.record_worker("chat-w1", "chat-9-1")
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_worker_whose_lineage_cannot_be_read_is_not_stopped(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.store import StoreUnavailable

    def unreadable(_key: str) -> str:
        raise StoreUnavailable("the store failed to load")

    monkeypatch.setattr(env.store, "worker_conductor", unreadable)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_rebound_channel_holds_no_backoff_for_its_own_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)

    calls = {"n": 0}

    def refusal_now(*_a: Any, **_k: Any) -> str:
        calls["n"] += 1
        # The channel is bound to another session after the pass's first check.
        return "" if calls["n"] == 1 else "rebound"

    monkeypatch.setattr(surface, "write_refusal_now", refusal_now)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    assert _calls(env, "agents.conversations.setView") == []
    assert tabs._held(CH, "pr-42") == 0.0  # a fence, not a Slack failure


@pytest.mark.asyncio
async def test_a_container_without_its_own_view_id_is_refused(env: Any, stop_world: Any) -> None:
    payload = _stop_payload()
    payload["container"] = {"type": "view", "tab_id": WORKERS_VIEW}
    assert await worker_stop._handle(env.orch, payload) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_declined_stop_is_said_as_such_and_can_be_pressed_again(
    env: Any, stop_world: Any
) -> None:
    stop_world.answers.append({"ok": True, "info": "compacting", "compacting": True})
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.1")) == "stop:compacting"
    assert "compacting" in env.slack.ephemerals[-1][2]
    # Not "Already stopping": the next press stops it.
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.2")) == "stop:stopping"
    stop_world.answers.append({"ok": True, "info": "no active turn"})
    assert await worker_stop._handle(env.orch, _stop_payload(ts="9.1")) == "repeat"


@pytest.mark.asyncio
async def test_a_stop_that_errors_is_audited_and_told(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    async def boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("provider gone")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", boom)
    assert await worker_stop.handle_stop_click(env.orch, _stop_payload()) is True
    row = env.sel.ops(worker_stop.OPERATION)[-1]
    assert row["outcome"] == "error" and row["caller"] == OWNER
    assert env.slack.ephemerals[-1] == (CH, OWNER, worker_stop.FAILED_TEXT)


@pytest.mark.asyncio
async def test_a_repeat_and_a_missing_dashboard_are_audited(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    await worker_stop._handle(env.orch, _stop_payload(ts="1.1"))
    await worker_stop._handle(env.orch, _stop_payload(ts="1.2"))
    assert "result=repeat" in env.sel.ops(worker_stop.OPERATION)[-1]["resources"]
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    assert await worker_stop._handle(env.orch, _stop_payload(ts="2.1")) == "denied"
    assert env.sel.ops(worker_stop.OPERATION)[-1]["error"] == "no dashboard"


@pytest.mark.asyncio
async def test_a_pr_tab_that_gives_way_to_the_diff_reruns_the_refresh(monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels.models import CodeChannelApiError
    from kiro_crew.slack.code_channels.workspace import service as svc

    calls = {"n": 0}

    class _Client:
        async def set_diff_view(self, channel: str, *a: Any, **k: Any) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise CodeChannelApiError("agents.conversations.setView", "too_many_views")
            return "Vdiff"

    async def make_room(client: Any, channel: str, *, for_key: str, **_kw: Any) -> str:
        assert for_key == "diff"
        return "pr-12"

    monkeypatch.setattr(views, "make_room", make_room)
    service = svc.WorkspaceService(lambda: _Client())
    record = SimpleNamespace(channel_id=CH, key=f"T1:{CH}", session_key=SLOT, slack_off="")
    assert await service._call("set_diff_view", record, "diff --git", room_for="diff") == "Vdiff"
    assert record.key in service._again


@pytest.mark.asyncio
async def test_a_workers_tab_stop_shows_who_stopped_it_on_the_board_and_the_tab(
    env: Any, stop_world: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 7
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    # Still unwinding (the turn reads as running) or already idle: either way it
    # reads as stopped, and offers no Stop.
    for live in (model.LIVE_RUNNING, model.LIVE_IDLE):
        row = _project(env.state, live).rows[0]
        assert (
            row.stopped and row.stopped_by == OWNER and re.fullmatch(r"\d\d:\d\d", row.stopped_at)
        )
        blocks = render.workers_view_blocks(model.BoardSnapshot(rows=(row,)), _link, now=_NOW)
        by_id = {b.get("block_id"): b for b in blocks}
        line = by_id["worker-it_00000001-text"]["elements"][0]["elements"]
        assert {"type": "user", "user_id": OWNER} in line
        assert _texts(by_id["worker-it_00000001-text"]).startswith("Fix login\nStopped by ")
        assert [e["action_id"] for e in by_id["worker-it_00000001"]["elements"]] == [
            "kc_cc_worker_open"
        ]
        board = render.board_blocks(model.BoardSnapshot(rows=(row,)), _link, now=_NOW)
        details = board[-1]["tasks"][0]["details"]["elements"][0]["elements"]
        assert details[:2] == [
            {"type": "text", "text": "Stopped by "},
            {"type": "user", "user_id": OWNER},
        ]
        rows_look = render.board_blocks(
            model.BoardSnapshot(rows=(row,)), _link, look="rows", now=_NOW
        )
        assert f"Stopped by <@{OWNER}> · {row.stopped_at}" in json.dumps(
            rows_look, ensure_ascii=False
        )


@pytest.mark.asyncio
async def test_the_stopped_line_clears_when_the_workers_next_turn_starts(
    env: Any, stop_world: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 7
    await worker_stop._handle(env.orch, _stop_payload())
    assert _project(env.state).rows[0].stopped
    stop_world.slots["chat-w1"]._turn_generation = 8  # its next turn started
    row = _project(env.state, model.LIVE_RUNNING).rows[0]
    assert not row.stopped and row.stopped_by == ""
    stop_world.slots["chat-w1"]._turn_generation = 7  # never comes back
    assert not _project(env.state).rows[0].stopped


@pytest.mark.parametrize("closed_first", [False, True])
@pytest.mark.asyncio
async def test_a_resumed_worker_on_a_new_slot_inherits_no_stop(
    env: Any, stop_world: Any, closed_first: bool
) -> None:
    from kiro_crew.slack.code_channels.board import stops

    stop_world.slots["chat-w1"]._turn_generation = 1
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    assert _project(env.state).rows[0].stopped
    if closed_first:
        # The slot closed and a board read saw it gone before the resume.
        old = stop_world.slots.pop("chat-w1")
        assert stops.current(env.state, "chat-w1") is None
        stop_world.slots["chat-w1"] = old
    # Resumed: a NEW slot, whose first turn is generation 1 again, is running.
    _resumed(stop_world, 1)
    row = _project(env.state, model.LIVE_RUNNING).rows[0]
    assert not row.stopped and row.stopped_by == "" and row.state == model.WORKING
    blocks = render.workers_view_blocks(model.BoardSnapshot(rows=(row,)), _link, now=_NOW)
    by_id = {b.get("block_id"): b for b in blocks}
    assert "kc_cc_worker_stop" in [e["action_id"] for e in by_id["worker-it_00000001"]["elements"]]
    # A snapshot read while the old stop showed is reconciled to the new slot's truth.
    stale = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31"),)
    )
    assert stops.reconcile(stale, env.state).rows[0].stopped_by == ""
    assert stops.take_urgent(["chat-w1"]) is False


@pytest.mark.asyncio
async def test_a_stop_of_the_resumed_slot_is_not_answered_as_a_repeat(
    env: Any, stop_world: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 1
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.1")) == "stop:stopping"
    _resumed(stop_world, 1)  # within the repeat window, on a new slot's first turn
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.2")) == "stop:stopping"
    assert len(stop_world.stopped) == 2


@pytest.mark.asyncio
async def test_an_expired_repeat_mark_does_not_keep_its_slot(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from kiro_crew.testing.clock import ManualClock

    # On worker_stop's own `time` binding only: the event loop keeps its clock.
    clock = ManualClock()
    clock.install(monkeypatch, worker_stop)
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.1")) == "stop:stopping"
    old = stop_world.slots["chat-w1"]
    assert any(v[2] is old for v in worker_stop._recent.values())
    clock.advance(worker_stop._REPEAT_SECS)
    stop_world.slots["chat-w2"].running = True  # a Stop of ANOTHER worker, later
    payload = _stop_payload(ts="2.1", value="it_00000002")
    assert await worker_stop._handle(env.orch, payload) == "stop:stopping"
    assert not any(v[2] is old for v in worker_stop._recent.values())


def test_an_unpublished_stop_on_a_dashboard_slot_is_taken_by_its_ledger_key() -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slot = SimpleNamespace(key="dashboard_chat-w1", _turn_generation=1)
    stops.note(slot, OWNER, generation=1)
    assert stops.take_urgent(["chat-w1"]) is True  # the row's ledger spelling
    assert stops.take_urgent(["chat-w1"]) is False  # spent once


def test_a_stop_is_forgotten_once_its_slot_is_gone() -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slots: dict[str, Any] = {"chat-w1": SimpleNamespace(key="chat-w1", _turn_generation=3)}
    state = SimpleNamespace(get_slot=slots.get)
    stops.note(slots["chat-w1"], OWNER, generation=3)
    assert stops.current(state, "chat-w1") is not None
    old = slots.pop("chat-w1")
    assert stops.current(state, "chat-w1") is None
    slots["chat-w1"] = old  # even the same object coming back: the note is gone
    assert stops.current(state, "chat-w1") is None and stops.take_urgent(["chat-w1"]) is False


@pytest.mark.asyncio
async def test_a_refused_or_declined_stop_marks_nobody(env: Any, stop_world: Any) -> None:
    await worker_stop._handle(env.orch, _stop_payload(user=STRANGER))
    stop_world.answers.append({"ok": True, "info": "compacting", "compacting": True})
    await worker_stop._handle(env.orch, _stop_payload(ts="2.1"))
    assert not _project(env.state).rows[0].stopped


@pytest.mark.asyncio
async def test_a_fresh_stop_skips_both_edit_gaps_once(env: Any, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import board
    from kiro_crew.slack.code_channels.board import stops

    binding = _bind(env)
    _set_views(env)
    slot = SimpleNamespace(key="chat-w1", _turn_generation=3)
    env.state.get_slot = lambda key: slot if key == "chat-w1" else None
    fake = _Refusing(refuse=0)
    fake.updates: list[Any] = []  # type: ignore[misc]

    async def update_message(channel: str, ts: str, text: str = "", blocks: Any = None) -> None:
        fake.updates.append(blocks)

    fake.update_message = update_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    real = surface.SurfaceClient(env.api.api_call)
    monkeypatch.setattr(surface, "client", lambda: real)
    clock = {"now": 5000.0}
    monkeypatch.setattr(publisher, "_clock", lambda: clock["now"])
    publisher.forget(CH)
    working = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await board.publish(binding, working) == "posted"
    clock["now"] += 1
    env.clock.now += 1
    stops.note(slot, OWNER, generation=3)
    stopped = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31"),)
    )
    # One second after the last edit, well inside both gaps: still published now.
    assert await board.publish(binding, stopped) == "updated"
    sent = _calls(env, "agents.conversations.setView")
    assert len(sent) == 2 and "Stopped by" in json.dumps(sent[-1]["blocks"])
    # The mark is spent: the next change inside the gap waits as usual.
    clock["now"] += 1
    env.clock.now += 1
    later = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31", title="x"),)
    )
    assert await board.publish(binding, later) == "deferred"
    publisher.forget(CH)


@pytest.mark.asyncio
async def test_stop_then_refresh_reads_stopped_on_board_and_tab_until_the_next_turn(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    """Stop -> the refresh it schedules -> "Stopped by" on the board and the tab ->
    the worker's next turn clears it, through the production board publisher."""
    from kiro_crew.slack.code_channels import board

    stop_world.slots["chat-w1"]._turn_generation = 4
    fake = _Refusing(refuse=0)
    posted: list[Any] = []

    async def update_message(channel: str, ts: str, text: str = "", blocks: Any = None) -> None:
        posted.append(blocks)

    fake.update_message = update_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    real = surface.SurfaceClient(env.api.api_call)
    monkeypatch.setattr(surface, "client", lambda: real)
    _set_views(env)
    clock = {"now": 9000.0}
    monkeypatch.setattr(publisher, "_clock", lambda: clock["now"])
    publisher.forget(CH)

    def refresh(live: str) -> Any:
        # What the scheduled refresh reads: the board's own projection.
        return board.publish(stop_world.binding, _project(env.state, live))

    assert await refresh(model.LIVE_RUNNING) == "posted"
    clock["now"] += 1
    env.clock.now += 1
    # The click comes from the Workers tab as the gateway recorded it just now.
    payload = _stop_payload(view_id=worker_stop._workers_view(CH))
    assert await worker_stop._handle(env.orch, payload) == "stop:stopping"
    assert stop_world.noted == ["chat-w1"]  # the refresh path was armed
    # The refresh lands inside both edit gaps and is still published at once.
    assert await refresh(model.LIVE_IDLE) == "updated"
    board_text = json.dumps(posted[-1])
    tab = _calls(env, "agents.conversations.setView")[-1]
    for text in (board_text, json.dumps(tab["blocks"])):
        assert '"text": "Stopped by "' in text and f'"user_id": "{OWNER}"' in text
    assert "kc_cc_worker_stop" not in json.dumps(tab["blocks"])
    # The worker's next turn starts: the next refresh omits Stopped.
    stop_world.slots["chat-w1"]._turn_generation = 5
    clock["now"] += publisher.MIN_EDIT_GAP_SECS + 1
    env.clock.now += tabs.WORKERS_MIN_GAP_SECS + 1
    assert await refresh(model.LIVE_RUNNING) == "updated"
    assert "Stopped by" not in json.dumps(posted[-1])
    tab = _calls(env, "agents.conversations.setView")[-1]
    assert "Stopped by" not in json.dumps(tab["blocks"])
    assert "kc_cc_worker_stop" in json.dumps(tab["blocks"])  # Stop is offered again
    publisher.forget(CH)


@pytest.mark.asyncio
async def test_newer_unseen_pr_tabs_are_not_removed_for_older_confirmed_prs(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    newer = [
        tabs.PrRef(100, PR_URL.format(n=100), "open"),
        tabs.PrRef(90, PR_URL.format(n=90), "open"),
    ]
    await tabs.sync_pr_tabs(binding, newer)
    # This pass only confirmed two OLDER open PRs; #100 and #90 were not read.
    older = [
        tabs.PrRef(20, PR_URL.format(n=20), "open"),
        tabs.PrRef(10, PR_URL.format(n=10), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, older)
    assert _calls(env, "agents.conversations.removeView") == []
    assert held == {100: "Vpr100", 90: "Vpr90"}
    assert len(_calls(env, "agents.conversations.setView")) == 2  # no third PR tab


@pytest.mark.parametrize("tail", _TRAILING)
def test_slack_ids_are_refused_with_anything_trailing(tail: str) -> None:
    assert render.tab_element(CH, "VPR12") is not None
    assert render.tab_element(CH + tail, "VPR12") is None
    assert render.tab_element(CH, "VPR12" + tail) is None


@pytest.mark.parametrize("tail", _TRAILING)
def test_a_stoppers_user_id_is_refused_with_anything_trailing(tail: str) -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slot = SimpleNamespace(key="chat-w1", _turn_generation=1)
    state = SimpleNamespace(get_slot=lambda key: slot if key == "chat-w1" else None)
    stops.note(slot, "U123ABC" + tail, generation=1)
    assert stops.current(state, "chat-w1") is None
    stops.note(slot, "U123ABC", generation=1)
    assert stops.current(state, "chat-w1") is not None


@pytest.mark.parametrize("tail", _TRAILING)
@pytest.mark.asyncio
async def test_a_stop_item_id_is_refused_with_anything_trailing(
    env: Any, stop_world: Any, tail: str
) -> None:
    payload = _stop_payload(value="it_00000001" + tail)
    assert await worker_stop._handle(env.orch, payload) == "denied"
    assert stop_world.stopped == [] and stop_world.reads == []  # refused before the board read
    assert "not a work item" in env.sel.ops(worker_stop.OPERATION)[-1].values()


@pytest.mark.parametrize(
    "value, ok",
    [
        ("U123ABC", True),
        ("W12", True),
        ("U1", False),  # too short
        ("u123ABC", False),
        ("U123abc", False),
        ("X123ABC", False),
        ("U" + "A" * 31, False),  # too long
        ("U123ABC\n", False),
        ("", False),
    ],
)
def test_user_ids_have_the_exact_shape(value: str, ok: bool) -> None:
    from kiro_crew.slack.code_channels.board import stops

    assert stops._is_user_id(value) is ok


@pytest.mark.parametrize(
    "value, ok",
    [
        ("it_00000001", True),
        ("it_" + "a" * 32, True),
        ("it_0000001", False),  # too short
        ("it_" + "a" * 33, False),
        ("it_0000000G", False),
        ("it_0000000A", False),  # upper-case hex is not minted
        ("IT_00000001", False),
        ("it_00000001\n", False),
        ("", False),
    ],
)
def test_item_ids_have_the_exact_shape(value: str, ok: bool) -> None:
    assert worker_stop._is_item_id(value) is ok


@pytest.mark.asyncio
async def test_a_pass_whose_board_read_failed_removes_no_workers_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    _scope_flag(monkeypatch)
    workers = [
        tabs.PrRef(11, PR_URL.format(n=11), "open"),
        tabs.PrRef(10, PR_URL.format(n=10), "open"),
    ]
    await tabs.sync_pr_tabs(binding, workers)
    # The board read fails: no rows, so only the channel's own (newer) PR is seen.
    own = tabs.PrRef(12, PR_URL.format(n=12), "open")
    held = await tabs.sync_pr_tabs(
        binding, tabs.wanted_prs(own, ()), settled=tabs.settled_prs(own, ()), own=12
    )
    assert _calls(env, "agents.conversations.removeView") == []
    assert held == {11: "Vpr11", 10: "Vpr10"}  # #12 waits for a free slot
    # Two NEWER PRs seen open do take the slots: the unseen tabs give way then.
    seen = [
        tabs.PrRef(13, PR_URL.format(n=13), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, seen, own=12)
    removed = sorted(c["view_id"] for c in _calls(env, "agents.conversations.removeView"))
    assert removed == ["Vpr10", "Vpr11"] and set(held) == {13, 12}


@pytest.mark.asyncio
async def test_the_stop_reads_the_surface_store_only_off_the_loop_or_from_memory(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    def on_the_loop(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a blocking SurfaceStore.get on the event loop")

    real_get = ss.SurfaceStore.get

    async def get_async(channel_id: str) -> Any:  # what the coroutine path uses
        import asyncio

        return await asyncio.to_thread(real_get, ss.store(), channel_id)

    monkeypatch.setattr(ss, "get_async", get_async)
    monkeypatch.setattr(ss.SurfaceStore, "get", on_the_loop)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"


def test_an_unloaded_surface_store_answers_nothing_from_memory(tmp_path: Path) -> None:
    fresh = SurfaceStore(tmp_path / "cold.json")
    assert fresh.loaded_get(CH) is None  # never reads the disk to answer


class _Subagents:
    """The sub-agent manager's ``cancel_for_parent``: what it was asked, and when."""

    def __init__(self, stop_world: Any, answers: list[Any]) -> None:
        self.asked: list[tuple[str, int]] = []
        self._stop_world = stop_world
        self._answers = answers

    async def cancel_for_parent(self, parent: str) -> tuple[int, int]:
        # How many turn stops had been sent when this sweep ran.
        self.asked.append((parent, len(self._stop_world.stopped)))
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.asyncio
async def test_a_workers_tab_stop_also_stops_the_agents_that_worker_started(
    env: Any, stop_world: Any
) -> None:
    subagents = _Subagents(stop_world, [(2, 1)])
    env.state.subagents = subagents
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    # Together with the turn's stop, which goes out first with no await before it
    # (so what the final gate admitted is what it stops): the state the channel's
    # Stop leaves a helper in.
    assert subagents.asked == [("dashboard:chat-w1", 1)]
    assert stop_world.stopped == [("chat-w1", _STOP_KW)]
    replies = [text for *_ignored, text in env.slack.ephemerals]
    assert replies[-1] == (
        "Stopping *Fix login* and the 3 agents it started. The conductor keeps going."
    )
    assert "subagents=3" in env.sel.ops(worker_stop.OPERATION)[-1]["resources"]


@pytest.mark.asyncio
async def test_a_failed_sub_agent_sweep_still_stops_the_worker_and_says_so(
    env: Any, stop_world: Any
) -> None:
    env.state.subagents = _Subagents(stop_world, [RuntimeError("manager down")])
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    assert stop_world.stopped == [("chat-w1", _STOP_KW)]
    text = [t for *_ignored, t in env.slack.ephemerals][-1]
    assert text.endswith("Some of the agents it started could not be stopped.")
    assert "subagents=error" in env.sel.ops(worker_stop.OPERATION)[-1]["resources"]


@pytest.mark.asyncio
async def test_an_idle_workers_stop_still_stops_the_agents_it_started(
    env: Any, stop_world: Any
) -> None:
    # Its turn has ended but a sub-agent it started runs on: the bar's Stop
    # counts that as running, so the Workers tab sweeps it the same way.
    subagents = _Subagents(stop_world, [(1, 0)])
    env.state.subagents = subagents
    assert await worker_stop._handle(env.orch, _stop_payload(value="it_00000002")) == "idle"
    assert stop_world.stopped == []
    assert [parent for parent, _sent in subagents.asked] == ["dashboard:chat-w2"]
    assert env.slack.ephemerals[-1] == (
        CH,
        OWNER,
        "*Idle one* isn't running. Stopped 1 agent it started.",
    )
    assert "result=idle subagents=1" in env.sel.ops(worker_stop.OPERATION)[-1]["resources"]


@pytest.mark.parametrize("worker", ["it_00000001", "it_00000002"])  # running, idle
@pytest.mark.asyncio
async def test_a_worker_linked_onto_another_session_sweeps_nothing(
    env: Any, stop_world: Any, worker: str
) -> None:
    # A restored slot whose linked session is the conductor's: the sweep would
    # cancel the conductor's agents, so the press is refused before any stop.
    subagents = _Subagents(stop_world, [(1, 0)])
    env.state.subagents = subagents
    key = "chat-w1" if worker == "it_00000001" else "chat-w2"
    stop_world.slots[key].linked_session_key = SESSION
    assert await worker_stop._handle(env.orch, _stop_payload(value=worker)) == "denied"
    assert subagents.asked == [] and stop_world.stopped == []


def test_the_stop_dialog_says_it_stops_the_agents_the_worker_started() -> None:
    row = _row(session_key="chat-w1")
    blocks = render.workers_view_blocks(model.BoardSnapshot(rows=(row,)), _link, now=_NOW)
    stop = next(
        e
        for b in blocks
        if b.get("type") == "actions"
        for e in b["elements"]
        if e["action_id"] == render.WORKER_STOP_ACTION
    )
    assert "stops this helper agent and the agents it started" in stop["confirm"]["text"]["text"]


@pytest.mark.asyncio
async def test_a_queued_turn_runnable_when_the_gate_passes_is_not_the_one_stopped(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    slot = stop_world.slots["chat-w1"]
    slot._turn_generation = 7
    real_gate = worker_stop._final_gate

    def final_gate(*a: Any, **k: Any) -> Any:
        verdict = real_gate(*a, **k)
        # The pressed turn ends and a queued one starts at the next loop pass.
        asyncio.get_running_loop().call_soon(setattr, slot, "_turn_generation", 8)
        return verdict

    stopped_turns: list[int] = []

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        stopped_turns.append(s._turn_generation)
        return {"info": ""}

    monkeypatch.setattr(worker_stop, "_final_gate", final_gate)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    env.state.subagents = _Subagents(stop_world, [(0, 0)])
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    assert stopped_turns == [7]  # the turn the gate admitted, not the queued one


@pytest.mark.asyncio
async def test_a_workers_tab_stop_goes_through_the_channel_stops_one_helper_path(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    """One path for stopping one helper: the brake's (``stop_helpers``), never a copy."""
    from kiro_crew.slack.code_channels import stop_helpers

    asked: list[tuple[str, dict[str, Any]]] = []

    async def stop_one_helper(state: Any, helper: Any, **kw: Any) -> Any:
        asked.append((helper.key, kw))
        return stop_helpers.Tally(subagents=2), {"info": ""}

    monkeypatch.setattr(stop_helpers, "stop_one_helper", stop_one_helper)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    assert asked == [("chat-w1", {"escalate": False, "cancel_key": "dashboard_chat-w1"})]
    assert stop_world.stopped == []  # nothing stopped around that path
    text = [t for *_ignored, t in env.slack.ephemerals][-1]
    assert text == "Stopping *Fix login* and the 2 agents it started. The conductor keeps going."
