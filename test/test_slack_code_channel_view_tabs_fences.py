"""Code-channel view tabs: native PR tabs, tab deep links, the interactive Workers tab.

The fences every tab path asks again right before a write or a removal.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channel_view_tabs_helpers import (  # noqa: F401 - fixtures by name
    _NOW,
    ALICE,
    CH,
    PR_URL,
    SESSION,
    _bind,
    _calls,
    _channel_elements,
    _link,
    _project,
    _Refusing,
    _row,
    _scope_flag,
    _set_views,
    _stop_payload,
    _v,
    env_fixture,
    no_host_dns_fixture,
    stop_world_fixture,
)
from slack_code_channels_helpers import OWNER, start_new_turn, stop_running_turn

from kiro_crew.slack.code_channels import (
    actions,
    surface,
    tabs,
    tools,
    views,
    worker_stop,
    wrapup,
)
from kiro_crew.slack.code_channels.board import model, publisher, render, source
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import SurfaceStore
from kiro_crew.slack.code_channels.surface_store import store as surface_store
from kiro_crew.testing.wait import default_timeout


@pytest.mark.asyncio
async def test_a_press_while_a_stop_is_already_in_flight_credits_nobody(
    env: Any, stop_world: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 7
    stop_world.answers.append({"ok": True, "info": "already stopping", "already_stopping": True})
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:already"
    row = _project(env.state).rows[0]
    assert not row.stopped and row.stopped_by == ""
    blocks = render.workers_view_blocks(model.BoardSnapshot(rows=(row,)), _link, now=_NOW)
    assert "Stopped by" not in json.dumps(blocks, ensure_ascii=False)


@pytest.mark.asyncio
async def test_a_refused_pr_view_type_stops_the_rest_of_the_same_pass(env: Any) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setView"] = {
        "ok": False,
        "error": "invalid_view_type",
    }
    wanted = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    assert await tabs.sync_pr_tabs(binding, wanted) == {}
    assert len(_calls(env, "agents.conversations.setView")) == 1


@pytest.mark.asyncio
async def test_a_failed_removal_never_lets_a_third_pr_tab_open(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    await tabs.sync_pr_tabs(
        binding,
        [
            tabs.PrRef(100, PR_URL.format(n=100), "open"),
            tabs.PrRef(90, PR_URL.format(n=90), "open"),
        ],
    )
    env.api.responses["agents.conversations.removeView"] = {"ok": False, "error": "internal_error"}
    held = await tabs.sync_pr_tabs(
        binding,
        [
            tabs.PrRef(110, PR_URL.format(n=110), "open"),
            tabs.PrRef(100, PR_URL.format(n=100), "open"),
        ],
    )
    assert len(_calls(env, "agents.conversations.removeView")) == 1
    assert len(_calls(env, "agents.conversations.setView")) == 2  # #110 waits for room
    assert held == {100: "Vpr100", 90: "Vpr90"}


def test_only_pr_digit_keys_are_reserved_for_pr_tabs() -> None:
    assert views.reserved_key("pr-12")
    assert not views.reserved_key("pr-summary")
    assert not views.reserved_key("pr-")
    assert not views.reserved_key("pr-²")


@pytest.mark.asyncio
async def test_the_agent_may_write_a_view_under_a_pr_word_key(env: Any) -> None:
    _bind(env)
    _set_views(env)
    out = await tools.run_tool(
        env.state,
        SESSION,
        tools.SET_VIEW,
        {"type": "html", "view_key": "pr-summary", "content": "<html/>"},
    )
    assert out.ok, out.text
    assert [c["view_key"] for c in _calls(env, "agents.conversations.setView")] == ["pr-summary"]
    refused = await tools.run_tool(
        env.state, SESSION, tools.SET_VIEW, {"type": "html", "view_key": "pr-7", "content": "x"}
    )
    assert not refused.ok and "'pr-<number>'" in refused.text


@pytest.mark.asyncio
async def test_a_fence_landing_after_the_relist_removes_no_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    both = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    await tabs.sync_pr_tabs(binding, both)

    async def relist(client: Any, channel: str, record: Any) -> Any:
        surface.fence_archived(CH)  # archived while Slack's list was read
        return record

    monkeypatch.setattr(tabs, "_relist", relist)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], settled={12})
    assert _calls(env, "agents.conversations.removeView") == []
    assert views.recorded(surface_store().get(CH), "pr-12") is not None


@pytest.mark.asyncio
async def test_a_fence_landing_while_making_room_removes_nothing(env: Any) -> None:
    _bind(env)
    surface_store().update(CH, views=[_v("report", "html", "9")])
    listed = [{"view_id": "Vreport"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    real_list = env.api.responses["agents.conversations.listViews"]

    def list_views(body: dict[str, Any]) -> dict[str, Any]:
        surface.fence_archived(CH)  # archived between the refusal and the removal
        return real_list(body)

    env.api.responses["agents.conversations.listViews"] = list_views
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(_bind(env), snap, link_for=_link) != "written"
    assert _calls(env, "agents.conversations.removeView") == []
    assert views.recorded(surface_store().get(CH), "report") is not None


@pytest.mark.asyncio
async def test_the_final_gate_resolves_no_config_and_composes_no_context(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    import kiro_crew.platform as platform_pkg
    from kiro_crew.platform import context as platform_context

    real = source.read_board
    reached: list[str] = []

    def loader(name: str) -> Any:
        def _reached(*_a: Any, **_k: Any) -> Any:
            reached.append(name)
            raise AssertionError(f"{name} reached on the way to the stop")

        return _reached

    async def read_board(state: Any, b: Binding) -> Any:
        snapshot = await real(state, b)
        # From here to the stop, nothing may resolve config or compose a context.
        # The channel ceiling's re-reads may read the context the entry gate
        # installed (an attribute read); composing one is what is refused.
        installed = platform_context.installed_context()
        assert installed is not None
        composing = loader("current_context")

        def current_context() -> Any:
            return platform_context._ACTIVE or composing()

        for mod in (worker_stop, actions):
            monkeypatch.setattr(mod, "live_config", loader("live_config"))
        monkeypatch.setattr(platform_context, "current_context", current_context)
        monkeypatch.setattr(platform_pkg, "current_context", current_context)
        return snapshot

    monkeypatch.setattr(source, "read_board", read_board)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert reached == [] and len(stop_world.stopped) == 1


@pytest.mark.parametrize("when", ["during_stop", "during_reply"])
@pytest.mark.asyncio
async def test_a_queued_turn_that_starts_after_the_stop_is_never_marked_stopped(
    env: Any, stop_world: Any, monkeypatch: Any, when: str
) -> None:
    slot = stop_world.slots["chat-w1"]
    slot._turn_generation = 7
    if when == "during_stop":

        async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
            stop_world.stopped.append((s.key, kw))
            s._turn_generation += 1  # the cancelled turn ended; a queued one started
            return {"info": ""}

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    else:
        real_responder = worker_stop._responder

        def responder(click: Any, admitted: Any) -> Any:
            send = real_responder(click, admitted)

            async def _send(text: str) -> None:
                slot._turn_generation += 1  # the queued turn starts mid-reply
                await send(text)

            return _send

        monkeypatch.setattr(worker_stop, "_responder", responder)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert len(stop_world.stopped) == 1
    # The running turn is the queued follow-up: nobody stopped it.
    row = _project(env.state, model.LIVE_RUNNING).rows[0]
    assert not row.stopped and row.stopped_by == ""
    blocks = render.workers_view_blocks(model.BoardSnapshot(rows=(row,)), _link, now=_NOW)
    by_id = {b.get("block_id"): b for b in blocks}
    assert "kc_cc_worker_stop" in [e["action_id"] for e in by_id["worker-it_00000001"]["elements"]]


@pytest.mark.asyncio
async def test_a_stop_aimed_at_the_next_queued_turn_is_a_new_stop(
    env: Any, stop_world: Any
) -> None:
    slot = stop_world.slots["chat-w1"]
    slot._turn_generation = 7
    assert (await worker_stop._handle(env.orch, _stop_payload(ts="3.1"))).startswith("stop:")
    slot._turn_generation = 8  # its queued follow-up started, and shows Stop
    assert (await worker_stop._handle(env.orch, _stop_payload(ts="3.2"))).startswith("stop:")
    assert len(stop_world.stopped) == 2
    # The same turn pressed again inside the window is still only answered.
    assert await worker_stop._handle(env.orch, _stop_payload(ts="3.3")) == "repeat"
    assert len(stop_world.stopped) == 2


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_a_stop_that_raises_leaves_no_repeat_mark(
    env: Any, stop_world: Any, monkeypatch: Any, failure: type[BaseException]
) -> None:
    calls: list[str] = []

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        calls.append(s.key)
        if len(calls) == 1:
            raise failure()
        stop_world.stopped.append((s.key, kw))
        return {"info": ""}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    with pytest.raises(failure):
        await worker_stop._handle(env.orch, _stop_payload(ts="4.1"))
    # Pressed again at once: a fresh stop, not "Already stopping".
    assert (await worker_stop._handle(env.orch, _stop_payload(ts="4.2"))).startswith("stop:")
    assert len(stop_world.stopped) == 1


def test_a_late_note_for_an_earlier_turn_keeps_the_later_turns_stop() -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slot = SimpleNamespace(key="chat-w1", _turn_generation=8)
    stops.note(slot, OWNER, generation=8)  # stop B, on turn 8
    stops.note(slot, OWNER, generation=7)  # stop A resumes late, for turn 7
    state = SimpleNamespace(get_slot=lambda key: slot if key == "chat-w1" else None)
    held = stops.current(state, "chat-w1")
    assert held is not None and held.generation == 8


@pytest.mark.asyncio
async def test_a_snapshot_read_before_the_next_turn_publishes_no_stopped_line(
    env: Any, monkeypatch: Any
) -> None:
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
    monkeypatch.setattr(surface, "dashboard_state", lambda: env.state)
    publisher.forget(CH)
    stops.note(slot, OWNER, generation=3)
    # Read while turn 3 was the stopped one...
    stale = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31"),)
    )
    slot._turn_generation = 4  # ...then the queued turn 4 started before the publish.
    await board.publish(binding, stale)
    sent = _calls(env, "agents.conversations.setView")
    assert sent and "Stopped by" not in json.dumps(sent[-1]["blocks"])
    assert all("Stopped by" not in json.dumps(b) for b in fake.sent + fake.updates)
    assert stops.take_urgent(["chat-w1"]) is False  # no edit-gap skip left over


@pytest.mark.asyncio
async def test_a_newer_stop_landing_before_the_publish_shows_its_own_attribution(
    env: Any, monkeypatch: Any
) -> None:
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
    monkeypatch.setattr(surface, "dashboard_state", lambda: env.state)
    publisher.forget(CH)
    # Read while Alice's stop of turn 3 was showing...
    stale = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=ALICE, stopped_at="13:31"),)
    )
    slot._turn_generation = 4  # ...then the queued turn 4 started, and Bob stopped it.
    stops.note(slot, OWNER, generation=4)
    bob = stops.current(env.state, "chat-w1")
    assert bob is not None
    await board.publish(binding, stale)
    workers = json.dumps(_calls(env, "agents.conversations.setView")[-1]["blocks"])
    boards = [json.dumps(b) for b in fake.sent + fake.updates]
    assert ALICE not in workers and all(ALICE not in b for b in boards)
    assert OWNER in workers and any(OWNER in b for b in boards)


def test_reconcile_sets_each_overlay_to_the_stop_showing_now() -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slots = {
        "chat-w1": SimpleNamespace(key="chat-w1", _turn_generation=4),
        "chat-w2": SimpleNamespace(key="chat-w2", _turn_generation=2),
        "chat-w3": SimpleNamespace(key="chat-w3", _turn_generation=9),
    }
    state = SimpleNamespace(get_slot=slots.get)
    stops.note(slots["chat-w1"], OWNER, generation=4)  # replaces Alice's older stop
    stops.note(slots["chat-w2"], OWNER, generation=2)  # landed after the read
    snap = model.BoardSnapshot(
        rows=(
            _row("it_00000001", session_key="chat-w1", stopped_by=ALICE, stopped_at="13:31"),
            _row("it_00000002", session_key="chat-w2"),
            _row("it_00000003", session_key="chat-w3", stopped_by=ALICE, stopped_at="13:31"),
        )
    )
    rows = stops.reconcile(snap, state).rows
    at1 = stops.current(state, "chat-w1").at  # type: ignore[union-attr]
    at2 = stops.current(state, "chat-w2").at  # type: ignore[union-attr]
    assert [(r.stopped_by, r.stopped_at) for r in rows] == [(OWNER, at1), (OWNER, at2), ("", "")]


@pytest.mark.parametrize("path", ["workers_tab", "board"])
@pytest.mark.asyncio
async def test_a_turn_starting_during_a_publishers_last_read_shows_no_stopped_line(
    env: Any, monkeypatch: Any, path: str
) -> None:
    from kiro_crew.slack.code_channels.board import stops

    binding = _bind(env)
    _set_views(env)
    slot = SimpleNamespace(key="chat-w1", _turn_generation=3)
    env.state.get_slot = lambda key: slot if key == "chat-w1" else None
    monkeypatch.setattr(surface, "dashboard_state", lambda: env.state)
    fake = _Refusing(refuse=0)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: surface.SurfaceClient(env.api.api_call))
    publisher.forget(CH)
    stops.note(slot, OWNER, generation=3)
    stale = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31"),)
    )
    mod = tabs if path == "workers_tab" else publisher
    real_get = mod.get_async

    async def get_async(channel: str) -> Any:
        slot._turn_generation = 4  # the queued turn starts during the publisher's read
        return await real_get(channel)

    monkeypatch.setattr(mod, "get_async", get_async)
    if path == "workers_tab":
        await tabs.publish_workers(binding, stale, link_for=_link, urgent=True)
        sent = [c["blocks"] for c in _calls(env, "agents.conversations.setView")]
    else:
        await publisher.publish(binding, stale, link_for=_link, urgent=True)
        sent = list(fake.sent)
    assert sent and all("Stopped by" not in json.dumps(b) for b in sent)
    assert stops.take_urgent(["chat-w1"]) is False  # the stale mark is gone too


@pytest.mark.parametrize("when", ["code_tab", "tab_read", "board_read"])
@pytest.mark.asyncio
async def test_a_channel_leaving_the_scope_mid_publish_gets_no_tab_and_no_board(
    env: Any, monkeypatch: Any, when: str
) -> None:
    from kiro_crew.slack.code_channels import board

    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: surface.SurfaceClient(env.api.api_call))
    publisher.forget(CH)
    if when == "code_tab":

        async def code_tab(_b: Any) -> bool:
            flag["in"] = False  # the channel turned shared meanwhile
            return False

        monkeypatch.setattr(board, "_code_tab_shown", code_tab)
    elif when == "board_read":
        real_board_get = publisher.get_async

        async def board_get(channel: str) -> Any:
            flag["in"] = False  # ...or while the board message read its record
            return await real_board_get(channel)

        monkeypatch.setattr(publisher, "get_async", board_get)
    else:
        real_get = tabs.get_async

        async def get_async(channel: str) -> Any:
            flag["in"] = False  # ...or while the Workers tab read its record
            return await real_get(channel)

        monkeypatch.setattr(tabs, "get_async", get_async)
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await board.publish(binding, snap) == "out_of_scope"
    if when != "board_read":  # (the Workers tab went out before the board's read)
        assert _calls(env, "agents.conversations.setView") == []
    assert fake.sent == []


@pytest.mark.asyncio
async def test_a_channel_leaving_the_scope_mid_sync_gets_no_pr_tab_write_or_removal(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    both = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    await tabs.sync_pr_tabs(binding, both)
    writes = len(_calls(env, "agents.conversations.setView"))

    async def relist(client: Any, channel: str, record: Any) -> Any:
        flag["in"] = False  # the scope goes while Slack's list is read
        return record

    monkeypatch.setattr(tabs, "_relist", relist)
    newer = [tabs.PrRef(60, PR_URL.format(n=60), "open")]
    await tabs.sync_pr_tabs(binding, newer, settled={12, 42})
    assert len(_calls(env, "agents.conversations.setView")) == writes
    assert _calls(env, "agents.conversations.removeView") == []


@pytest.mark.parametrize("when", ["before_read", "before_post"])
@pytest.mark.asyncio
async def test_a_wrap_up_out_of_scope_links_no_worker_pr_tab(
    env: Any, monkeypatch: Any, when: str
) -> None:
    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    surface_store().update(
        CH, views=[_v("pr-10", "pull_request", "1"), _v("pr-33", "pull_request", "2")]
    )
    # The channel's own PR 10, and worker PR 33 from the cached board.
    monkeypatch.setattr(
        wrapup,
        "_gather_facts_sync",
        lambda b: wrapup.Facts(
            pr_number=10,
            pr_state="open",
            code_tab=True,
            pr_tabs=wrapup._pr_tabs(CH, {10, 33}),
        ),
    )
    if when == "before_read":
        flag["in"] = False
    else:
        real = surface.is_archived

        async def is_archived(b: Any) -> bool:
            flag["in"] = False  # the scope goes between the facts and the post
            return await real(b)

        monkeypatch.setattr(surface, "is_archived", is_archived)
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER)
    posted = json.dumps(fake.sent[-1])
    assert "Vpr33" not in posted
    assert "Vpr10" in posted  # the channel's own PR tab stays


def test_a_wrap_up_resent_out_of_scope_carries_no_tab_link(env: Any, monkeypatch: Any) -> None:
    # Every re-send of a wrap-up (the Archive click's edit, the unsaved one's
    # retirement, the resend without links) goes through ``_shown`` first.
    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    facts = wrapup.Facts(
        pr_number=10, pr_state="open", code_tab=True, pr_tabs=((33, "Vpr33"), (10, "Vpr10"))
    )
    blocks = wrapup.wrap_up_blocks("All done.", facts, CH)
    assert wrapup._shown(binding, blocks) == blocks  # in scope: as posted
    flag["in"] = False  # the channel turned shared since the post
    shown = wrapup._shown(binding, blocks)
    assert "tab_id" not in json.dumps(shown)
    assert [b["type"] for b in shown] == [b["type"] for b in blocks if b["type"] != "rich_text"]


@pytest.mark.asyncio
async def test_a_stop_out_of_scope_after_the_board_read_stops_nothing(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    flag = _scope_flag(monkeypatch)
    real = source.read_board

    async def read_board(state: Any, b: Binding) -> Any:
        snapshot = await real(state, b)
        flag["in"] = False  # the channel turned shared after the read
        return snapshot

    monkeypatch.setattr(source, "read_board", read_board)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []
    assert all("Fix login" not in text for *_ignored, text in env.slack.ephemerals)


@pytest.mark.asyncio
async def test_a_stop_whose_channel_leaves_the_scope_meanwhile_names_no_worker(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.board import stops

    flag = _scope_flag(monkeypatch)

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        stop_world.stopped.append((s.key, kw))
        flag["in"] = False  # shared while the stop ran
        return {"info": ""}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert len(stop_world.stopped) == 1  # the stop itself was admitted, and ran
    # Out of scope by the reply: no reply at all, let alone one naming the worker.
    assert all("Fix login" not in text for *_ignored, text in env.slack.ephemerals)
    assert stops.current(env.state, "chat-w1") is None and stop_world.noted == []


@pytest.mark.parametrize("when", ["before_send", "before_alert", "control"])
@pytest.mark.asyncio
async def test_a_board_publish_out_of_scope_before_a_send_sends_nothing(
    env: Any, monkeypatch: Any, when: str
) -> None:
    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    alerts: list[Any] = []

    async def post_message(*a: Any, **k: Any) -> str:
        alerts.append((a, k))
        return "100.2"

    fake.post_message = post_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    publisher.forget(CH)
    real_get = publisher.get_async
    reads = {"n": 0}

    async def get_async(channel: str) -> Any:
        reads["n"] += 1
        if when == "before_send":
            flag["in"] = False  # during the publish's own record read
        return await real_get(channel)

    monkeypatch.setattr(publisher, "get_async", get_async)
    if when == "before_alert":
        real_summary = publisher._record_summary

        async def record_summary(b: Any, ts: str, admits: Any = None) -> None:
            flag["in"] = False  # after the board went out, before its alert
            await real_summary(b, ts, admits)

        monkeypatch.setattr(publisher, "_record_summary", record_summary)
    waiting = model.BoardSnapshot(
        rows=(
            _row(
                state=model.WAITING_ON_YOU,
                session_key="chat-w1",
                report_stamp="r1",
                waiting_on="report",
            ),
        )
    )
    out = await publisher.publish(
        binding,
        waiting,
        link_for=_link,
        owner_id=OWNER,
        admits=lambda: source.scope_admits(None, binding),
    )
    if when == "before_send":
        assert out == "out_of_scope" and fake.sent == []
    elif when == "before_alert":
        assert fake.sent and alerts == []
    else:  # in scope throughout: the alert does go out
        assert fake.sent and alerts


@pytest.mark.parametrize("when", ["alert_read", "between_alerts"])
@pytest.mark.asyncio
async def test_a_channel_leaving_the_scope_during_the_alerts_gets_no_further_mention(
    env: Any, monkeypatch: Any, when: str
) -> None:
    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    alerts: list[Any] = []

    async def post_message(*a: Any, **k: Any) -> str:
        alerts.append((a, k))
        if when == "between_alerts":
            flag["in"] = False  # shared right after the first mention went out
        return "100.2"

    fake.post_message = post_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    publisher.forget(CH)
    if when == "alert_read":
        real_alert = publisher._alert
        real_get = publisher.get_async

        async def alert(*a: Any, **k: Any) -> None:
            async def get_async(channel: str) -> Any:
                flag["in"] = False  # shared during the alert's own record read
                return await real_get(channel)

            monkeypatch.setattr(publisher, "get_async", get_async)
            await real_alert(*a, **k)

        monkeypatch.setattr(publisher, "_alert", alert)
    waiting = model.BoardSnapshot(
        rows=tuple(
            _row(
                f"it_0000000{n}",
                state=model.WAITING_ON_YOU,
                session_key=f"chat-w{n}",
                report_stamp="r1",
                waiting_on="report",
            )
            for n in (1, 2)
        )
    )
    await publisher.publish(
        binding,
        waiting,
        link_for=_link,
        owner_id=OWNER,
        admits=lambda: source.scope_admits(None, binding),
    )
    assert fake.sent  # the board itself went out while in scope
    assert len(alerts) == (0 if when == "alert_read" else 1)


@pytest.mark.asyncio
async def test_the_channels_own_pr_tab_needs_no_worker_hand_off(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    writes = len(_calls(env, "agents.conversations.setView"))
    # Hand-off off (the default), or the channel turned shared: out of the
    # board's scope. The channel's own new PR 50 still gets its tab; the worker
    # PR 60 is a ledger fact and is not used.
    flag["in"] = False
    newer = [
        tabs.PrRef(60, PR_URL.format(n=60), "open"),
        tabs.PrRef(50, PR_URL.format(n=50), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, newer, own=50)
    sent = _calls(env, "agents.conversations.setView")[writes:]
    assert [c["view_key"] for c in sent] == ["pr-50"]
    assert 50 in held and 60 not in held


@pytest.mark.asyncio
async def test_out_of_scope_the_workers_pr_tabs_are_neither_removed_nor_replaced(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    workers = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    await tabs.sync_pr_tabs(binding, workers)  # two workers' tabs fill both slots
    writes = len(_calls(env, "agents.conversations.setView"))
    flag["in"] = False
    held = await tabs.sync_pr_tabs(
        binding, [tabs.PrRef(50, PR_URL.format(n=50), "open")], settled={12}, own=50
    )
    assert _calls(env, "agents.conversations.removeView") == []
    assert len(_calls(env, "agents.conversations.setView")) == writes
    assert set(held) == {42, 12}


@pytest.mark.asyncio
async def test_with_hand_off_off_a_fresh_channel_gets_its_own_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    flag["in"] = False
    held = await tabs.sync_pr_tabs(binding, [tabs.PrRef(7, PR_URL.format(n=7), "open")], own=7)
    assert held == {7: "Vpr7"}


@pytest.mark.parametrize("when", ["before_sync", "during_relist"])
@pytest.mark.asyncio
async def test_a_channel_set_off_gets_no_own_pr_tab_write_or_removal(
    env: Any, monkeypatch: Any, when: str
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    flag["in"] = False  # worker hand-off off: only the own PR is synced
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    writes = len(_calls(env, "agents.conversations.setView"))
    admits = {"on": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admits["on"])
    if when == "before_sync":
        admits["on"] = False  # the operator set the channel to Off or Review
    else:

        async def relist(client: Any, channel: str, record: Any) -> Any:
            admits["on"] = False  # ...while the refresh was reading Slack's list
            return record

        monkeypatch.setattr(tabs, "_relist", relist)
    await tabs.sync_pr_tabs(
        binding, [tabs.PrRef(50, PR_URL.format(n=50), "open")], settled={42}, own=50
    )
    assert len(_calls(env, "agents.conversations.setView")) == writes
    assert _calls(env, "agents.conversations.removeView") == []


@pytest.mark.asyncio
async def test_a_stop_landing_while_room_is_made_holds_no_backoff(
    env: Any, monkeypatch: Any
) -> None:
    _bind(env)
    surface_store().update(CH, views=[_v("report", "html", "9")])
    listed = [{"view_id": "Vreport"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    real_remove = env.api.responses["agents.conversations.removeView"]

    def remove_view(body: dict[str, Any]) -> dict[str, Any]:
        monkeypatch.setattr(surface, "channel_stopped", lambda channel: True)  # Slack's Stop
        return real_remove(body)

    env.api.responses["agents.conversations.removeView"] = remove_view
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(_bind(env), snap, link_for=_link) == "fenced"
    assert tabs._held(CH, views.WORKERS_KEY) == 0  # a fence, not a failure to back off


@pytest.mark.parametrize("what", ["stop", "set_off"])
@pytest.mark.asyncio
async def test_a_fence_at_the_cap_is_no_full_channel_hold(
    env: Any, monkeypatch: Any, what: str
) -> None:
    _bind(env)
    flag = _scope_flag(monkeypatch)
    surface_store().update(CH, views=[_v("report", "html", "9")])
    listed = [{"view_id": "Vreport"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    real_list = env.api.responses["agents.conversations.listViews"]

    def list_views(body: dict[str, Any]) -> dict[str, Any]:
        if what == "stop":
            monkeypatch.setattr(surface, "channel_stopped", lambda channel: True)  # Slack's Stop
        else:  # Off / Review: the board's scope (which asks activation) refuses
            flag["in"] = False
        return real_list(body)

    env.api.responses["agents.conversations.listViews"] = list_views
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(_bind(env), snap, link_for=_link) == "fenced"
    assert _calls(env, "agents.conversations.removeView") == []
    assert tabs._held(CH, views.WORKERS_KEY) == 0  # not a 10-minute "channel full"


@pytest.mark.asyncio
async def test_a_fenced_pr_tab_removal_holds_no_backoff(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    admits = {"on": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admits["on"])
    real_get = views.get_async

    async def get_async(channel: str) -> Any:
        admits["on"] = False  # set to Off right as the removal reads its record
        return await real_get(channel)

    monkeypatch.setattr(views, "get_async", get_async)
    await tabs.sync_pr_tabs(binding, [], settled={42}, own=42)
    assert _calls(env, "agents.conversations.removeView") == []
    assert tabs._held(CH, "remove:pr-42") == 0


def test_a_channels_view_locks_are_never_replaced_however_many_channels_there_are() -> None:
    first, record = views._lock(CH), views._record_lock(CH)
    for n in range(5000):
        views._lock(f"C{n:08d}")
        views._record_lock(f"C{n:08d}")
    # Unlocked but possibly awaited (a released lock's waiter has not woken yet):
    # a fresh lock for the same channel would let a second writer in beside it.
    assert views._lock(CH) is first and views._record_lock(CH) is record


@pytest.mark.parametrize("what", ["policy", "stop", "set_off", "channel_ceiling"])
@pytest.mark.asyncio
async def test_a_wrap_up_retry_is_fenced_again_after_the_failed_post(
    env: Any, monkeypatch: Any, what: str
) -> None:
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=1)
    real_post = fake.post_blocks
    allowed = {"out": True, "on": True, "ceiling": ""}

    async def outbound() -> bool:
        return allowed["out"]

    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: allowed["on"])
    monkeypatch.setattr(surface, "channel_policy_refusal", lambda *a, **k: allowed["ceiling"])

    async def post_blocks(channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        if not fake.sent:  # while the first (refused) post was out
            if what == "policy":
                allowed["out"] = False  # the channels policy revoked
            elif what == "stop":
                stop_running_turn(CH)  # Slack's Stop
            elif what == "channel_ceiling":
                allowed["ceiling"] = "not in allowed_channel_ids"  # the channel ceiling
            else:
                allowed["on"] = False  # set to Off or Review
        return await real_post(channel, blocks, text, thread_ts)

    fake.post_blocks = post_blocks  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(code_tab=True))
    try:
        assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER) == ""
    finally:
        start_new_turn(CH)
    assert len(fake.sent) == 1  # the refused post only: no retry went out


@pytest.mark.asyncio
async def test_a_stop_during_the_policy_read_fences_the_own_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    flag["in"] = False  # hand-off off: only the own PR, the channel fences alone

    reads = {"n": 0}

    async def outbound() -> bool:
        reads["n"] += 1
        if reads["n"] == 2:  # the read right before the setView itself
            stop_running_turn(CH)  # Slack's Stop lands during the policy read
        return True

    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    try:
        held = await tabs.sync_pr_tabs(binding, [tabs.PrRef(7, PR_URL.format(n=7), "open")], own=7)
    finally:
        start_new_turn(CH)
    assert held == {} and _calls(env, "agents.conversations.setView") == []


@pytest.mark.asyncio
async def test_a_fallback_whose_request_is_replaced_before_its_retry_posts_nothing(
    env: Any, monkeypatch: Any
) -> None:
    """The facts-only fallback answers its /wrap-up only while that request is live:
    a newer /wrap-up during the refused first post means no retry goes out."""
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=1)
    real_post = fake.post_blocks

    async def post_blocks(channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        if not fake.sent:
            wrapup._requests[CH] = "newer"  # while the refused first post was out
        return await real_post(channel, blocks, text, thread_ts)

    fake.post_blocks = post_blocks  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(code_tab=True))
    wrapup._requests[CH] = "tag1"
    try:
        out = await wrapup.post_wrap_up(
            binding, "", caller=OWNER, settles="tag1", only_if_live=True
        )
    finally:
        wrapup._requests.pop(CH, None)
    assert out == ""
    assert len(fake.sent) == 1  # the refused post only: no retry went out


@pytest.mark.asyncio
@pytest.mark.parametrize("settles", ["tag1", None])  # a /wrap-up's post, or the agent's own
async def test_a_stop_fenced_wrap_up_leaves_a_newer_request_live(
    env: Any, monkeypatch: Any, settles: str | None
) -> None:
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=1)
    real_post = fake.post_blocks

    async def post_blocks(channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        if not fake.sent:
            wrapup._requests[CH] = "newer"  # a newer /wrap-up...
            stop_running_turn(CH)  # ...and Slack's Stop while the first post was out
        return await real_post(channel, blocks, text, thread_ts)

    fake.post_blocks = post_blocks  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(code_tab=True))
    surface_store().update(CH, wrapup_pending_since=1.0, wrapup_requested_by=OWNER)
    wrapup._requests[CH] = "tag1"
    try:
        assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER, settles=settles) == ""
    finally:
        start_new_turn(CH)
    try:
        assert wrapup._requests.get(CH) == "newer"  # the newer request is not dropped
        assert surface_store().get(CH).wrapup_pending_since
    finally:
        wrapup._requests.pop(CH, None)


@pytest.mark.asyncio
async def test_a_channel_found_shared_during_the_wrap_up_archive_read_links_no_worker_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    surface_store().update(CH, views=[_v("pr-33", "pull_request", "2")])
    real = surface.is_archived

    async def is_archived(b: Any) -> bool:
        flag["in"] = False  # Slack Connect sharing recorded during the read in the lock
        return await real(b)

    monkeypatch.setattr(surface, "is_archived", is_archived)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(
        wrapup,
        "_gather_facts_sync",
        lambda b: wrapup.Facts(branch="x", pr_tabs=wrapup._pr_tabs(CH, {33})),
    )
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER)
    assert "Vpr33" not in json.dumps(fake.sent[-1])


@pytest.mark.asyncio
async def test_former_own_pr_tabs_give_way_to_the_channels_newer_own_pr(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    flag["in"] = False  # worker hand-off off: only the channel's own PRs
    for n in (10, 20, 30):  # the worktree moves between branches with open PRs
        await tabs.sync_pr_tabs(binding, [tabs.PrRef(n, PR_URL.format(n=n), "open")], own=n)
    keys = [c["view_key"] for c in _calls(env, "agents.conversations.setView")]
    assert keys == ["pr-10", "pr-20", "pr-30"]  # #30 is not blocked by #10 and #20
    removed = [c["view_id"] for c in _calls(env, "agents.conversations.removeView")]
    assert removed == ["Vpr10"]  # the oldest of the channel's own tabs gives way
    recorded = {v["key"]: v.get("origin") for v in surface_store().get(CH).views}
    assert recorded == {"pr-20": "own", "pr-30": "own"}


@pytest.mark.asyncio
async def test_a_worker_pr_that_becomes_the_channels_own_is_relabelled(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    _scope_flag(monkeypatch)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")])
    assert views.recorded(surface_store().get(CH), "pr-42")["origin"] == "worker"
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    assert views.recorded(surface_store().get(CH), "pr-42")["origin"] == "own"
    assert len(_calls(env, "agents.conversations.setView")) == 1  # no second write


@pytest.mark.asyncio
async def test_out_of_scope_a_worker_tab_is_never_relabelled_own(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")])
    flag["in"] = False  # shared (or hand-off off) before the channel's own PR is 42
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    assert views.recorded(surface_store().get(CH), "pr-42")["origin"] == "worker"


def test_a_pr_tabs_origin_survives_the_surface_store_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "surface.json"
    SurfaceStore(path).update(
        CH,
        views=[
            _v("pr-1", "pull_request", "1") | {"origin": "own"},
            _v("pr-2", "pull_request", "2") | {"origin": "worker"},
            _v("pr-3", "pull_request", "3"),
            _v("pr-4", "pull_request", "4") | {"origin": "bogus"},
        ],
    )
    loaded = {v["key"]: v for v in SurfaceStore(path).get(CH).views}
    assert loaded["pr-1"]["origin"] == "own" and loaded["pr-2"]["origin"] == "worker"
    assert [views.from_ledger(loaded[f"pr-{n}"]) for n in (1, 2, 3, 4)] == [
        False,
        True,
        True,  # no recorded origin: a worker's (fails closed)
        True,  # an unknown one too
    ]


@pytest.mark.parametrize("what", ["set_off", "shared"])
@pytest.mark.asyncio
async def test_a_channel_refused_while_the_stop_ran_gets_no_reply_and_no_overlay(
    env: Any, stop_world: Any, monkeypatch: Any, what: str
) -> None:
    from kiro_crew.slack.code_channels.board import stops

    flag = _scope_flag(monkeypatch)
    admits = {"on": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admits["on"])

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        stop_world.stopped.append((s.key, kw))
        if what == "set_off":
            admits["on"] = False  # set to Off or Review while the stop ran
        else:
            flag["in"] = False  # found shared while the stop ran
        return {"info": ""}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    replies = len(env.slack.ephemerals)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert len(stop_world.stopped) == 1
    assert len(env.slack.ephemerals) == replies  # no reply to a refused channel
    assert stops.current(env.state, "chat-w1") is None and stop_world.noted == []
    assert worker_stop._recent == {}  # nothing of the refused press is kept


@pytest.mark.asyncio
async def test_a_stop_that_errors_after_admission_replies_through_the_full_gate(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    admits = {"on": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admits["on"])

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        admits["on"] = False  # set to Off or Review, then the stop blows up
        raise RuntimeError("boom")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    replies = len(env.slack.ephemerals)
    assert await worker_stop.handle_stop_click(env.orch, _stop_payload()) is True
    assert len(env.slack.ephemerals) == replies  # the error reply was gated too


def test_a_huge_failure_count_never_overflows_the_backoff(env: Any) -> None:
    tabs._failures[(CH, views.WORKERS_KEY)] = 10**6  # a tab failing for a very long time
    delay = tabs._backoff(CH, views.WORKERS_KEY, "internal_error")
    assert delay == tabs._RETRY_CAP_SECS
    assert tabs._failures[(CH, views.WORKERS_KEY)] <= tabs._RETRY_MAX_EXPONENT + 1
    assert 0 < tabs._held(CH, views.WORKERS_KEY) <= tabs._RETRY_CAP_SECS
    # A bogus Retry-After never strands the tab either.
    assert tabs._backoff(CH, "pr-1", "ratelimited", retry_after=1e300) == (
        tabs._RETRY_AFTER_CAP_SECS
    )
    for bad in (float("nan"), float("inf"), -5.0):
        tabs._succeeded(CH, "pr-2")
        delay = tabs._backoff(CH, "pr-2", "ratelimited", retry_after=bad)
        assert 0 < delay <= tabs._RETRY_AFTER_CAP_SECS


def test_a_views_stamp_and_its_relist_read_one_injected_clock(env: Any) -> None:
    import asyncio as _asyncio

    env.clock.now = 5000.0
    _asyncio.run(views.record_view(CH, key="report", view_id="Vreport", type="html"))
    assert views.recorded(surface_store().get(CH), "report")["at"] == "5000.000000"

    class Client:
        async def list_views(self, channel_id: str) -> list[dict[str, str]]:
            return []  # someone removed it in Slack

    # Same instant as the record: newer than the listing can know, so kept.
    _asyncio.run(views.reconcile(Client(), CH))
    assert views.recorded(surface_store().get(CH), "report") is not None
    env.clock.now += 1  # a later listing: the view is gone, pruned
    _asyncio.run(views.reconcile(Client(), CH))
    assert views.recorded(surface_store().get(CH), "report") is None


def _stopped_admission() -> Any:
    """Admitted work of CH, then Slack's Stop of it: the commit point refuses it."""
    token = surface.admit()
    stop_running_turn(CH)
    return token


@pytest.mark.asyncio
async def test_stopped_work_arms_no_workers_tab_retry_in_its_backoff(env: Any) -> None:
    binding = _bind(env)
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    env.api.responses["agents.conversations.setView"] = {"ok": False, "error": "internal_error"}
    assert await tabs.publish_workers(binding, snap, link_for=_link) == "failed"
    assert tabs.take_retry(CH) > 0
    token = _stopped_admission()
    try:
        assert await tabs.publish_workers(binding, snap, link_for=_link) == "fenced"
    finally:
        surface.release(token)
        start_new_turn(CH)
    assert tabs.take_retry(CH) == 0.0  # the board's settle republishes, not this pass


@pytest.mark.asyncio
async def test_a_stop_during_a_failed_workers_write_holds_no_backoff(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    binding = _bind(env)
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))

    async def write_view(*_a: Any, **_k: Any) -> Any:
        stop_running_turn(CH)  # Slack's Stop while the write was out
        raise CodeChannelApiError("agents.conversations.setView", "internal_error")

    monkeypatch.setattr(views, "write_view", write_view)
    token = surface.admit()
    try:
        assert await tabs.publish_workers(binding, snap, link_for=_link) == "fenced"
    finally:
        surface.release(token)
        start_new_turn(CH)
    assert tabs.take_retry(CH) == 0.0
    assert tabs._held(CH, views.WORKERS_KEY) == 0.0


@pytest.mark.asyncio
async def test_stopped_work_lists_no_views_and_spends_no_relist(env: Any) -> None:
    _set_views(env)
    surface_store().update(CH, views=[_v("pr-12", "pull_request", "1")])
    record = surface_store().get(CH)
    token = _stopped_admission()
    try:
        assert await tabs._relist(surface.client(), CH, record) is record
    finally:
        surface.release(token)
        start_new_turn(CH)
    assert _calls(env, "agents.conversations.listViews") == []
    # Not stamped: the next pass that is not stopped relists at once.
    assert CH not in tabs._listed_at


@pytest.mark.asyncio
async def test_a_wrap_up_sent_without_its_tab_links_is_retired_without_them(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    fake = _Refusing(refuse=1)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(code_tab=True))

    async def update_async(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")  # the posted wrap-up's ts is not stored

    retired: list[Any] = []

    async def retire(_slack: Any, _channel: str, ts: str, blocks: Any, _b: Any) -> None:
        retired.append(blocks)

    monkeypatch.setattr(wrapup, "update_async", update_async)
    monkeypatch.setattr(wrapup, "_retire_unrecorded", retire)
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER) == ""
    assert len(fake.sent) == 2 and not _channel_elements(fake.sent[1])
    # The retire edit carries what Slack took, not the refused tab links.
    assert retired and not _channel_elements(retired[0])


def _shared_during_the_clients_policy_read(
    monkeypatch: Any, flag: dict[str, bool], method: str
) -> None:
    """Slack Connect sharing is recorded while SurfaceClient's own policy read awaits
    for *method* (``set_view``, ``remove_view``), after every check the tab path
    makes before the call."""
    import inspect

    async def outbound() -> bool:
        names = {f.function for f in inspect.stack()}
        if "_call" in names and method in names:
            flag["in"] = False
        return True

    monkeypatch.setattr(surface, "outbound_permitted", outbound)


@pytest.mark.asyncio
async def test_a_channel_shared_during_the_clients_policy_read_gets_no_workers_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    _shared_during_the_clients_policy_read(monkeypatch, flag, "set_view")
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(binding, snap, link_for=_link) == "fenced"
    assert _calls(env, "agents.conversations.setView") == []
    assert tabs.take_retry(CH) == 0.0  # fenced, not failed: no backoff


@pytest.mark.asyncio
async def test_a_channel_shared_during_the_clients_policy_read_keeps_its_worker_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(12, PR_URL.format(n=12), "open")])
    _shared_during_the_clients_policy_read(monkeypatch, flag, "remove_view")
    await tabs.sync_pr_tabs(binding, [], settled={12})
    assert _calls(env, "agents.conversations.removeView") == []
    assert views.recorded(surface_store().get(CH), "pr-12") is not None


@pytest.mark.asyncio
async def test_a_stopped_worker_whose_next_turn_starts_during_the_write_is_written_again(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 7
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    snap = _project(env.state)
    assert snap.rows[0].stopped
    _set_views(env)
    real = views.write_view

    async def write_view(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        stop_world.slots["chat-w1"]._turn_generation = 8  # its next turn started meanwhile
        return out

    monkeypatch.setattr(views, "write_view", write_view)
    tabs.take_retry(CH)
    out = await tabs.publish_workers(stop_world.binding, snap, link_for=_link, urgent=True)
    assert out == "written"
    assert "Stopped by" in json.dumps(_calls(env, "agents.conversations.setView")[-1])
    assert tabs._sent[CH] == ""  # not taken for what the tab shows
    assert tabs.take_retry(CH) == tabs.WORKERS_MIN_GAP_SECS


@pytest.mark.asyncio
async def test_a_stopped_worker_whose_next_turn_starts_during_the_board_send_is_edited_again(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 7
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    snap = _project(env.state)
    fake = _Refusing(refuse=0)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    publisher.forget(CH)
    real = publisher._send

    async def send(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        stop_world.slots["chat-w1"]._turn_generation = 8  # its next turn started meanwhile
        return out

    monkeypatch.setattr(publisher, "_send", send)
    assert await publisher.publish(stop_world.binding, snap, link_for=_link) == "posted"
    assert "Stopped by" in json.dumps(fake.sent[-1])
    assert surface_store().get(CH).board_digest == ""  # the next pass edits it
    assert publisher.take_retry(CH) >= publisher.MIN_EDIT_GAP_SECS
    publisher.forget(CH)


async def _cancelled_during_the_record_write(monkeypatch: Any, run: Any) -> None:
    """Run *run* as a task and cancel it while its record write (``update_async``) is
    in flight, after Slack already answered; the cancel must still reach the caller."""
    entered = asyncio.Event()
    release = asyncio.Event()
    real = views.update_async

    async def update_async(*a: Any, **k: Any) -> Any:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=default_timeout())
        return await real(*a, **k)

    monkeypatch.setattr(views, "update_async", update_async)
    task = asyncio.ensure_future(run())
    try:
        # The run's record write is reached (a missed signal fails here, not as a hang).
        await asyncio.wait_for(entered.wait(), timeout=default_timeout())
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=default_timeout())
    finally:
        release.set()
        task.cancel()


@pytest.mark.asyncio
async def test_a_cancel_after_slack_opened_a_tab_still_records_it(
    env: Any, monkeypatch: Any
) -> None:
    _bind(env)
    _set_views(env)

    async def write() -> str:
        return "Vpr42"

    async def run() -> Any:
        return await views.write_view(
            surface.client(), CH, key="pr-42", type="pull_request", write=write
        )

    await _cancelled_during_the_record_write(monkeypatch, run)
    # Up in Slack, so held as ours: its PR merging later removes it by id.
    assert views.recorded(surface_store().get(CH), "pr-42")["view_id"] == "Vpr42"


@pytest.mark.asyncio
async def test_a_cancel_after_slack_removed_a_tab_still_forgets_it(
    env: Any, monkeypatch: Any
) -> None:
    _bind(env)
    _set_views(env)
    surface_store().update(CH, views=[_v("pr-12", "pull_request", "1")])

    async def run() -> Any:
        return await views.remove_ours(surface.client(), CH, "pr-12")

    await _cancelled_during_the_record_write(monkeypatch, run)
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_id": "Vpr12"}
    ]
    # Gone from Slack, so the record neither links it nor counts it in a slot.
    assert views.recorded(surface_store().get(CH), "pr-12") is None


@pytest.mark.asyncio
async def test_a_settled_prs_tab_the_record_never_got_is_removed_by_key_once(
    env: Any,
) -> None:
    binding = _bind(env)
    _set_views(env)
    # PR 12's setView landed but its answer was lost (or a restart cut off the
    # record): the record holds nothing for it. PR 12 is then seen merged.
    await tabs.sync_pr_tabs(binding, [], settled={12}, own=12)
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_key": "pr-12"}
    ]
    await tabs.sync_pr_tabs(binding, [], settled={12}, own=12)
    assert len(_calls(env, "agents.conversations.removeView")) == 1  # once per process


@pytest.mark.asyncio
async def test_an_empty_board_removes_an_unrecorded_workers_tab_by_key_once(
    env: Any,
) -> None:
    binding = _bind(env)
    _set_views(env)
    empty = model.BoardSnapshot(rows=())
    assert await tabs.publish_workers(binding, empty, link_for=_link) == ""
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_key": "workers"}
    ]
    assert await tabs.publish_workers(binding, empty, link_for=_link) == ""
    assert len(_calls(env, "agents.conversations.removeView")) == 1
    assert _calls(env, "agents.conversations.setView") == []


@pytest.mark.asyncio
async def test_a_tab_written_again_after_its_sweep_is_swept_again(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    await tabs.sync_pr_tabs(binding, [], settled={12}, own=12)
    # PR 12 reopens; its tab's setView lands with its answer lost (nothing recorded).
    env.api.responses["agents.conversations.setView"] = {"ok": False, "error": "internal_error"}
    reopened = [tabs.PrRef(12, PR_URL.format(n=12), "open")]
    await tabs.sync_pr_tabs(binding, reopened, own=12)
    assert views.recorded(surface_store().get(CH), "pr-12") is None
    _set_views(env)
    tabs._held_until.clear()
    await tabs.sync_pr_tabs(binding, [], settled={12}, own=12)  # and it closes again
    by_key = [c for c in _calls(env, "agents.conversations.removeView") if "view_key" in c]
    assert by_key == [{"channel_id": CH, "view_key": "pr-12"}] * 2


@pytest.mark.parametrize("which", ["own_pr_tab", "workers_tab"])
@pytest.mark.asyncio
async def test_a_sweep_slack_refuses_for_now_arms_a_refresh_after_its_backoff(
    env: Any, monkeypatch: Any, which: str
) -> None:
    from slack_code_channel_view_tabs_helpers import _rate_limited

    from kiro_crew.slack.code_channels import board

    armed: list[tuple[str, Any]] = []
    monkeypatch.setattr(board, "_schedule", lambda ch, delay=None: armed.append((ch, delay)))
    binding = _bind(env)
    _set_views(env)
    env.api.responses["agents.conversations.removeView"] = _rate_limited("30")
    flag = _scope_flag(monkeypatch)
    flag["in"] = which == "workers_tab"  # the own PR's tab: no board, a shared channel

    async def sweep() -> None:
        if which == "own_pr_tab":
            await tabs.sync_pr_tabs(binding, [], settled={12}, own=12)
        else:
            await tabs.publish_workers(binding, model.BoardSnapshot(rows=()), link_for=_link)

    await sweep()
    # Armed on the board's own timer, which runs the refresh with or without a board.
    assert armed == [(CH, 30.0)]
    await sweep()
    assert len(_calls(env, "agents.conversations.removeView")) == 1  # held meanwhile
    assert armed[-1][0] == CH and 0 < armed[-1][1] <= 30.0  # and armed again
    _set_views(env)
    env.clock.now += 31
    await sweep()
    key = "pr-12" if which == "own_pr_tab" else "workers"
    assert _calls(env, "agents.conversations.removeView")[-1] == {"channel_id": CH, "view_key": key}


@pytest.mark.parametrize("what", ["policy", "off_or_review"])
@pytest.mark.asyncio
async def test_a_wrap_up_withdrawn_while_its_facts_are_read_posts_nothing(
    env: Any, monkeypatch: Any, what: str
) -> None:
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    allowed = {"out": True, "on": True}

    async def outbound() -> bool:
        return allowed["out"]

    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: allowed["on"])
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)

    def facts(_b: Any) -> Any:
        # Withdrawn while the facts were read: the host's Slack outbound profile,
        # or the channel set to Off or Review.
        allowed["out" if what == "policy" else "on"] = False
        return wrapup.Facts(branch="x")

    monkeypatch.setattr(wrapup, "_gather_facts_sync", facts)
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER) == ""
    assert fake.sent == []


@pytest.mark.asyncio
async def test_a_workers_tab_refused_for_its_tab_links_goes_out_without_them(
    env: Any,
) -> None:
    binding = _bind(env)
    surface_store().update(CH, views=[_v("pr-12", "pull_request", "1")])
    _set_views(env, refuse="invalid_blocks", times=1)
    row = _row(session_key="chat-w1", pr_number=12, pr_url=PR_URL.format(n=12), pr_state="open")
    assert await tabs.publish_workers(
        binding, model.BoardSnapshot(rows=(row,)), link_for=_link
    ) == ("written")
    sent = _calls(env, "agents.conversations.setView")
    assert len(sent) == 2
    assert "Vpr12" in json.dumps(sent[0]) and "Vpr12" not in json.dumps(sent[1])
    # And kept off for the channel: the next write does not try the links again.
    later = model.BoardSnapshot(rows=(replace(row, title="Fix login, again"),))
    env.clock.now += tabs.WORKERS_MIN_GAP_SECS
    assert await tabs.publish_workers(binding, later, link_for=_link) == "written"
    assert "Vpr12" not in json.dumps(_calls(env, "agents.conversations.setView")[-1])
