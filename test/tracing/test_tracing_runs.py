"""Phase 1b: sub-agent nesting, the completion join, run roots (R2-R5) and crew attributes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent, TurnUsage
from kiro_crew.dashboard import trace_binding
from kiro_crew.tracing import correlation, crew, provider, runs, subagents, turns

NS = "00112233445566778899aabbccddeeff"
KEY = b"k" * 32
PARENT_KEY = "dashboard:chat-parent"
PARENT_ACP = "acp-parent"
SPAWN_TOOL = "mcp_kirocrew_core_spawn_sub_agents"
RUN_TOOL = "mcp_kirocrew_core_spawn_run"


class _Source:
    def __init__(self, acp_session_id, session_key, agent="kirocrew-default"):
        self.session_id = acp_session_id
        self._session_key = session_key
        self._model = "auto"
        self._resolved_model_id = ""
        self.active_agent = agent


class _Sessions:
    """The one SessionManager call ``unit_for_session_key`` makes."""

    def get_provider(self, key):
        return _Source(PARENT_ACP, PARENT_KEY) if key == PARENT_KEY else None


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, KEY))
    yield exporter
    turns.reset_for_testing()
    subagents.reset_for_testing()
    runs.reset_for_testing()
    provider.reset_for_testing()


def _complete(credits=0.0):
    return AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage(credits=credits))


async def _events(*events):
    for event in events:
        yield event


async def _stream(source, *events):
    async for _ in turns.traced(source, _events(*events)):
        pass


def _run_child(agent_id, acp="acp-child", credits=1.5, conversation_key=""):
    """One sub-agent run, bound the way ``_run_impl`` binds it, in its own task."""
    info = SimpleNamespace(id=agent_id, conversation_key=conversation_key)

    async def run():
        token = subagents.bind_child(info)
        try:
            await _stream(_Source(acp, f"subagent:{agent_id}", "researcher"), _complete(credits))
        finally:
            turns.release_turn_root(token)

    asyncio.run(run())


def _parent_turn():
    return turns.start_turn(_Source(PARENT_ACP, PARENT_KEY))


def _end(turn):
    turn.observe(_complete(2.0))
    turn.finish(None)


def _hex(value, width):
    return format(value, f"0{width}x")


def _named(exporter, prefix):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def _children_of(exporter, agent_id):
    return [
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get("kirocrew.subagent_id") == agent_id
    ]


def _hash(value):
    return correlation.keyed_hash(KEY, value)


# ── sub-agent nesting ─────────────────────────────────────────────────────


def test_a_blocking_child_sits_under_its_spawn_sub_agents_span_with_its_totals(spans):
    parent = _parent_turn()
    turns.tool_started(
        PARENT_ACP, "tc-spawn", kind="other", mcp_server_name="kirocrew-core", tool_name=SPAWN_TOOL
    )
    subagents.pin_origin("sa_one", _Sessions(), PARENT_KEY)
    _run_child("sa_one", credits=1.5)
    turns.tool_finished(PARENT_ACP, "tc-spawn", status="completed")
    _end(parent)

    (spawn,) = _named(spans, "execute_tool")
    assert spawn.name == "execute_tool mcp"  # the parent span is found without naming it
    child = next(s for s in _children_of(spans, "sa_one") if s.name.startswith("invoke_agent"))
    assert child.parent.span_id == spawn.context.span_id
    assert child.context.trace_id == spawn.context.trace_id == parent.turn_span.context.trace_id
    assert spawn.end_time >= child.end_time  # blocking: the spawn ends after its child
    # R3: the totals sit on the span that ended after its children, never as gen_ai.usage.
    assert spawn.attributes["kirocrew.usage.credits_total"] == 1.5
    assert spawn.attributes["kirocrew.usage.turns_total"] == 1
    assert "kirocrew.usage.credits" not in spawn.attributes


def test_an_async_child_sits_under_its_already_ended_spawn_run_span(spans, monkeypatch):
    from opentelemetry.sdk import trace as sdk_trace

    from kiro_crew.testing.clock import ManualClock

    # Every span timestamp is one tick past the last, so the order below is the
    # order the spans really started and ended, on any host's clock resolution.
    ManualClock(tick=0.001).install(monkeypatch, sdk_trace)
    parent = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-run", kind="other", tool_name=RUN_TOOL)
    subagents.pin_origin("sa_async", _Sessions(), PARENT_KEY)
    turns.tool_finished(PARENT_ACP, "tc-run", status="completed")
    _end(parent)
    _run_child("sa_async")  # runs on after the parent turn and its spawn span ended

    (spawn,) = _named(spans, "execute_tool")
    child = next(s for s in _children_of(spans, "sa_async") if s.name.startswith("invoke_agent"))
    assert child.parent.span_id == spawn.context.span_id
    assert child.context.trace_id == parent.turn_span.context.trace_id
    assert child.start_time > spawn.end_time
    assert "kirocrew.usage.credits_total" not in spawn.attributes  # it ended first


def test_every_span_of_a_child_carries_the_parent_traces_session(spans):
    parent = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-run", kind="other", tool_name=RUN_TOOL)
    subagents.pin_origin("sa_sess", _Sessions(), PARENT_KEY)
    turns.tool_finished(PARENT_ACP, "tc-run", status="completed")
    _end(parent)
    info = SimpleNamespace(id="sa_sess", conversation_key="")

    async def run():
        token = subagents.bind_child(info)
        try:
            child = turns.start_turn(_Source("acp-child", "subagent:sa_sess"))
            turns.tool_started("acp-child", "tc-read", kind="read", tool_name="read")
            turns.tool_finished("acp-child", "tc-read", status="completed")
            _end(child)
        finally:
            turns.release_turn_root(token)

    asyncio.run(run())
    trace = parent.turn_span.context.trace_id
    in_trace = [s for s in spans.get_finished_spans() if s.context.trace_id == trace]
    assert {s.attributes["session.id"] for s in in_trace} == {_hash(PARENT_KEY)}
    child_spans = _children_of(spans, "sa_sess")
    assert {s.name.split(" ")[0] for s in child_spans} == {
        "invoke_agent",
        "chat",
        "user.prompt",
        "execute_tool",
        "turn.complete",
    }
    assert {s.attributes["kirocrew.session_key_hash"] for s in child_spans} == {
        _hash("subagent:sa_sess")
    }
    assert {s.attributes["kirocrew.parent_session_key_hash"] for s in child_spans} == {
        _hash(PARENT_KEY)
    }


def test_a_continuation_sits_under_the_origin_of_the_run_it_continues(spans):
    parent = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-run", kind="other", tool_name=RUN_TOOL)
    subagents.pin_origin("sa_first", _Sessions(), PARENT_KEY)
    turns.tool_finished(PARENT_ACP, "tc-run", status="completed")
    _end(parent)
    _run_child("sa_first")
    _run_child("sa_next", acp="acp-child-2", conversation_key="subagent:sa_first")

    (spawn,) = _named(spans, "execute_tool")
    turns_of = [s for s in spans.get_finished_spans() if s.name.startswith("invoke_agent ")]
    nested = [s for s in turns_of if s.attributes.get("kirocrew.subagent_id")]
    assert len(nested) == 2
    assert {s.parent.span_id for s in nested} == {spawn.context.span_id}
    assert len({s.attributes["kirocrew.turn_id"] for s in nested}) == 2  # each run a new turn


def test_a_continuations_completion_joins_the_trace_it_ran_in(spans):
    first = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-run", kind="other", tool_name=RUN_TOOL)
    subagents.pin_origin("sa_orig", _Sessions(), PARENT_KEY)
    turns.tool_finished(PARENT_ACP, "tc-run", status="completed")
    _end(first)
    _run_child("sa_orig")
    # A later parent turn continues it: admission pins the new run to that turn.
    later = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-cont", kind="other", tool_name="spawn_continue")
    subagents.pin_origin("sa_cont", _Sessions(), PARENT_KEY)
    turns.tool_finished(PARENT_ACP, "tc-cont", status="completed")
    _end(later)
    assert later.trace_id != first.trace_id
    _run_child("sa_cont", acp="acp-child-2", conversation_key="subagent:sa_orig")
    ran = next(s for s in _children_of(spans, "sa_cont") if s.name.startswith("invoke_agent"))
    assert _hex(ran.context.trace_id, 32) == first.trace_id

    subagents.note_delivery("q-cont", ["sa_cont"])
    turn = _completion_turn(spans, ["q-cont"])
    assert turn.context.trace_id == ran.context.trace_id
    assert turn.parent.span_id == first.turn_span.context.span_id


def test_a_blocking_spawn_totals_children_moved_onto_a_continuation(spans, monkeypatch):
    # The parent's first 4 spans plus one child's 4 fill a budget of 5, so the
    # second child is bound into a continuation trace while the spawn is open.
    monkeypatch.setattr(runs, "SPAN_BUDGET", 5)
    parent = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-spawn", kind="other", tool_name=SPAWN_TOOL)
    for agent_id in ("sa_x", "sa_y"):
        subagents.pin_origin(agent_id, _Sessions(), PARENT_KEY)
    _run_child("sa_x", acp="acp-x", credits=1.5)
    _run_child("sa_y", acp="acp-y", credits=1.5)
    turns.tool_finished(PARENT_ACP, "tc-spawn", status="completed")
    _end(parent)
    assert _named(spans, "subagent (cont 2)")
    (spawn,) = _named(spans, "execute_tool")
    assert spawn.attributes["kirocrew.usage.credits_total"] == 3.0
    assert spawn.attributes["kirocrew.usage.turns_total"] == 2


def test_spawn_tools_are_found_under_any_harness_prefix_and_nothing_else():
    for name in (
        "spawn_run",
        "spawn_sub_agents",
        "spawn_continue",
        "mcp_kirocrew_core_spawn_run",
        "@kirocrew-core/spawn_sub_agents",
        "kirocrew-core:spawn_continue",
        "kirocrew.spawn_run",
    ):
        assert turns._is_spawn_tool(name), name
    for name in ("", "respawn_run", "spawn_run_x", "spawn_runs", "read", "x" * 300 + "_spawn_run"):
        assert not turns._is_spawn_tool(name), name


def test_two_open_spawn_calls_fall_back_to_the_turn_span(spans):
    parent = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-a", kind="other", tool_name=RUN_TOOL)
    turns.tool_started(PARENT_ACP, "tc-b", kind="other", tool_name=RUN_TOOL)
    subagents.pin_origin("sa_amb", _Sessions(), PARENT_KEY)
    _run_child("sa_amb")
    _end(parent)
    child = next(s for s in _children_of(spans, "sa_amb") if s.name.startswith("invoke_agent"))
    assert child.parent.span_id == parent.turn_span.context.span_id


def test_a_parent_with_no_open_turn_pins_nothing_and_the_child_roots_itself(spans):
    subagents.pin_origin("sa_cron", _Sessions(), PARENT_KEY)
    _run_child("sa_cron")
    child = next(s for s in _children_of(spans, "sa_cron") if s.name.startswith("invoke_agent"))
    assert child.parent is None
    assert _hex(child.context.trace_id, 32) == child.attributes["kirocrew.turn_id"]


def test_off_pins_and_binds_nothing():
    provider.reset_for_testing()
    subagents.pin_origin("sa_off", _Sessions(), PARENT_KEY)
    assert subagents.bind_child(SimpleNamespace(id="sa_off", conversation_key="")) is None
    subagents.note_delivery("q-off", ["sa_off"])
    assert subagents._origins == {} and subagents._deliveries == {}
    provider.reset_for_testing()


# ── the completion join (R4) ──────────────────────────────────────────────


COMPLETION_TURN = "c0ffee00c0ffee00c0ffee00c0ffee00"


def _completion_turn(spans, queue_ids):
    """The parent's next turn, started the way the queue drain starts it."""
    slot = SimpleNamespace(key="chat-parent")

    async def run():
        token = await trace_binding.bind_turn(SimpleNamespace(), slot, COMPLETION_TURN, top=True)
        try:
            await _stream(_Source(PARENT_ACP, PARENT_KEY), _complete())
        finally:
            turns.release_turn_root(token)

    async def drain():
        with subagents.delivered(queue_ids):
            task = asyncio.ensure_future(run())  # the turn's task copies this context
        await task

    asyncio.run(drain())
    return next(
        s
        for s in spans.get_finished_spans()
        if s.name.startswith("invoke_agent")
        and s.attributes.get("kirocrew.turn_id") == COMPLETION_TURN
    )


def _async_parent(*agent_ids):
    parent = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-run", kind="other", tool_name=RUN_TOOL)
    for agent_id in agent_ids:
        subagents.pin_origin(agent_id, _Sessions(), PARENT_KEY)
    turns.tool_finished(PARENT_ACP, "tc-run", status="completed")
    _end(parent)
    for n, agent_id in enumerate(agent_ids):
        _run_child(agent_id, acp=f"acp-child-{n}")
    return parent


def test_a_completion_turn_joins_the_parents_trace_under_the_parent_turn(spans):
    parent = _async_parent("sa_done")
    subagents.note_delivery("q-1", ["sa_done"])
    turn = _completion_turn(spans, ["q-1"])
    assert turn.context.trace_id == parent.turn_span.context.trace_id
    assert turn.parent.span_id == parent.turn_span.context.span_id
    assert turn.attributes["kirocrew.turn_id"] == COMPLETION_TURN
    assert turn.attributes["session.id"] == _hash(PARENT_KEY)
    assert not turn.links


def test_two_batched_completions_give_a_root_with_two_links(spans):
    _async_parent("sa_a", "sa_b")
    subagents.note_delivery("q-wave", ["sa_a", "sa_b"])
    turn = _completion_turn(spans, ["q-wave"])
    assert turn.parent is None
    assert _hex(turn.context.trace_id, 32) == COMPLETION_TURN
    child_turns = {
        s.attributes["kirocrew.subagent_id"]: s
        for s in spans.get_finished_spans()
        if s.name.startswith("invoke_agent ") and s.attributes.get("kirocrew.subagent_id")
    }
    assert sorted(
        (link.attributes["kirocrew.subagent_id"], link.context.span_id) for link in turn.links
    ) == sorted((aid, span.context.span_id) for aid, span in child_turns.items())


def test_a_turn_with_no_delivery_and_a_nested_turn_take_no_cause(spans):
    _async_parent("sa_once")
    subagents.note_delivery("q-once", ["sa_once"])
    with subagents.delivered(["q-once"]):
        assert subagents.take_causes() == ("sa_once",)
        assert subagents.take_causes() == ()  # one-shot: a nested turn gets none
    assert subagents.take_causes() == ()


def test_a_drain_that_delivers_nothing_hides_an_earlier_turns_untaken_causes(spans):
    _async_parent("sa_stale")
    subagents.note_delivery("q-stale", ["sa_stale"])
    # The next drain runs inside the earlier turn's task, whose causes went untaken.
    with subagents.delivered(["q-stale"]):
        with subagents.delivered(["q-user-message"]):
            assert subagents.take_causes() == ()
        assert subagents.take_causes() == ("sa_stale",)


# ── run roots: anchors, outcomes, budgets (R2, R3, R5) ────────────────────


def test_an_anchor_and_its_outcome_carry_deterministic_ids_and_the_roll_up(spans):
    state = provider.peek()
    ref = runs.anchor(state, "cron", "cron nightly", ("job1", "run1"), root_session_key=PARENT_KEY)
    rollup = runs.Rollup()
    rollup.add({"kirocrew.usage.credits": 2.0, "gen_ai.usage.input_tokens": 10})
    rollup.add({"kirocrew.usage.credits": 0.5, "gen_ai.usage.input_tokens": True})
    runs.outcome(state, ref, "finished", rollup=rollup)
    anchor_span, outcome_span = spans.get_finished_spans()
    from kiro_crew.tracing import ids

    assert _hex(anchor_span.context.trace_id, 32) == ids.trace_id(NS, "cron", "job1", "run1", 1)
    assert _hex(anchor_span.context.span_id, 16) == ids.span_id(NS, "cron", "job1", "run1", 1)
    assert anchor_span.name == "cron nightly" and anchor_span.parent is None
    assert outcome_span.name == "cron outcome finished"
    assert outcome_span.parent.span_id == anchor_span.context.span_id
    assert outcome_span.attributes["kirocrew.usage.credits_total"] == 2.5
    assert outcome_span.attributes["kirocrew.usage.input_tokens_total"] == 10
    assert outcome_span.attributes["kirocrew.usage.turns_total"] == 2
    assert outcome_span.attributes["session.id"] == _hash(PARENT_KEY)


def test_a_span_budget_overflow_starts_a_linked_continuation(spans, monkeypatch):
    # A turn is 4 spans (invoke_agent, user.prompt, chat, turn.complete); the parent
    # adds its spawn tool span. A budget of 5 makes each trace full after one turn.
    monkeypatch.setattr(runs, "SPAN_BUDGET", 5)
    parent = _parent_turn()
    turns.tool_started(PARENT_ACP, "tc-run", kind="other", tool_name=RUN_TOOL)
    for agent_id in ("sa_1", "sa_2"):
        subagents.pin_origin(agent_id, _Sessions(), PARENT_KEY)
    turns.tool_finished(PARENT_ACP, "tc-run", status="completed")
    _end(parent)
    _run_child("sa_1", acp="acp-1")
    _run_child("sa_2", acp="acp-2")

    (spawn,) = _named(spans, "execute_tool")
    (second,) = _named(spans, "subagent (cont 2)")
    (third,) = _named(spans, "subagent (cont 3)")
    for anchor, back_to in ((second, spawn), (third, second)):
        assert anchor.parent is None
        (back,) = anchor.links
        assert (back.context.trace_id, back.context.span_id) == (
            back_to.context.trace_id,
            back_to.context.span_id,
        )
        assert anchor.attributes["session.id"] == _hash(PARENT_KEY)
    assert second.context.trace_id != parent.turn_span.context.trace_id
    assert second.attributes["kirocrew.run.continuation"] == 2
    first_child, second_child = (
        next(s for s in _children_of(spans, aid) if s.name.startswith("invoke_agent"))
        for aid in ("sa_1", "sa_2")
    )
    assert first_child.parent.span_id == second.context.span_id
    assert first_child.context.trace_id == second.context.trace_id
    assert second_child.parent.span_id == third.context.span_id


def test_an_old_run_rolls_over_by_age(spans, monkeypatch):
    from kiro_crew.testing.clock import ManualClock

    state = provider.peek()
    clock = ManualClock(start=1_800_000_000.0)
    clock.install(monkeypatch, runs)
    ref = runs.RunRef("subagent", ("t", "s"), "a" * 32, "b" * 16, PARENT_KEY, clock.time())
    assert runs.segment(state, ref, "subagent") is ref
    clock.advance(runs.AGE_BUDGET_SECS)
    nxt = runs.segment(state, ref, "subagent")
    assert nxt.cont == 2 and nxt.trace_id != ref.trace_id
    assert runs.segment(state, ref, "subagent") is nxt  # later children reuse it


# ── Ultra Mode and Bot attributes ─────────────────────────────────────────


def test_a_worker_turn_carries_its_root_and_its_item(spans, monkeypatch):
    from kiro_crew import crew_mode

    row = {crew_mode.WORKER_FIELD: "chat-conductor", crew_mode.DISPATCHER_FIELD: "chat-conductor"}
    monkeypatch.setattr(trace_binding, "_stamped_row", lambda state, key: row)
    monkeypatch.setattr(crew, "bound_item", lambda key: ("chat-conductor", "it_3f9a1c2e", 2))
    turn_id = "abad1dea" * 4

    async def run():
        slot = SimpleNamespace(key="chat-worker")
        token = await trace_binding.bind_turn(SimpleNamespace(), slot, turn_id, top=True)
        try:
            await _stream(_Source("acp-w", "dashboard:chat-worker", "kirocrew-worker"), _complete())
        finally:
            turns.release_turn_root(token)

    asyncio.run(run())
    worker = [
        s for s in spans.get_finished_spans() if s.attributes.get("kirocrew.turn_id") == turn_id
    ]
    assert worker
    for span in worker:
        assert span.attributes["kirocrew.crew.root"] == _hash("chat-conductor")
        assert span.attributes["kirocrew.crew.root_kind"] == "ultra"
        assert span.attributes["kirocrew.crew.dispatcher"] == _hash("chat-conductor")
        assert span.attributes["kirocrew.work.item_id"] == "it_3f9a1c2e"
        assert span.attributes["kirocrew.work.round"] == 2
        assert span.attributes["kirocrew.work.conductor_hash"] == _hash("chat-conductor")


def test_a_nested_worker_turn_carries_its_root_and_item_without_taking_causes(spans, monkeypatch):
    from kiro_crew import crew_mode

    row = {crew_mode.WORKER_FIELD: "chat-conductor", crew_mode.DISPATCHER_FIELD: "chat-conductor"}
    monkeypatch.setattr(trace_binding, "_stamped_row", lambda state, key: row)
    monkeypatch.setattr(crew, "bound_item", lambda key: ("chat-conductor", "it_3f9a1c2e", 2))
    turn_id = "abad1dea" * 4
    subagents.note_delivery("q-nested", ["sa_pending"])

    async def run():
        slot = SimpleNamespace(key="chat-worker")
        with subagents.delivered(["q-nested"]):
            token = await trace_binding.bind_turn(SimpleNamespace(), slot, turn_id, top=False)
            try:
                await _stream(
                    _Source("acp-w", "dashboard:chat-worker", "kirocrew-worker"), _complete()
                )
                assert subagents.take_causes() == ("sa_pending",)
            finally:
                turns.release_turn_root(token)

    asyncio.run(run())
    worker = [
        s for s in spans.get_finished_spans() if s.attributes.get("kirocrew.turn_id") == turn_id
    ]
    assert worker
    for span in worker:
        assert span.attributes["kirocrew.crew.root"] == _hash("chat-conductor")
        assert span.attributes["kirocrew.work.item_id"] == "it_3f9a1c2e"


def test_crew_roots_by_kind():
    from kiro_crew import crew_mode

    assert crew.root_of("c", {crew_mode.ROOT_FIELD: True}) == ("c", "", "ultra")
    assert crew.root_of(
        "w", {crew_mode.MATE_ROOT_FIELD: "m", crew_mode.MATE_DISPATCHER_FIELD: "m"}
    ) == (
        "m",
        "m",
        "mate",
    )
    assert crew.root_of("p", {}) == ("", "", "")
    assert crew.attrs(KEY, "p", {}, None) == {}
    # No key: the hashed ids are omitted, the raw kind and item id stay.
    assert crew.attrs(None, "w", {crew_mode.WORKER_FIELD: "c"}, ("c", "it_00000001", 0)) == {
        "kirocrew.crew.root_kind": "ultra",
        "kirocrew.work.item_id": "it_00000001",
    }


def test_bound_item_reads_the_binding_and_the_items_round(monkeypatch):
    from kiro_crew import work_ledger

    monkeypatch.setattr(work_ledger, "read_binding", lambda key: ("chat-c", "it_11111111"))
    monkeypatch.setattr(work_ledger, "read_work_item", lambda c, i: SimpleNamespace(round=3))
    assert crew.bound_item("chat-w") == ("chat-c", "it_11111111", 3)
    monkeypatch.setattr(work_ledger, "read_binding", lambda key: None)
    assert crew.bound_item("chat-w") is None
