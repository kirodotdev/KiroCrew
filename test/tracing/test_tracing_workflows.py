"""Phase 3b: a workflow run is one trace: its phases, its agent calls, its outcome.

The runner is driven for real (``WorkflowRunner.run``). Its stub ``agent_fn``
streams a traced turn the way a step's provider does, so the turn's root binding
is exercised exactly as in production.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent, TurnUsage
from kiro_crew.testing.wait import await_until, finished, until_parked
from kiro_crew.tracing import correlation, ids, provider, runs, turns
from kiro_crew.tracing import workflows as trace_workflows
from kiro_crew.workflows.runner import WorkflowRunner

NS = "00112233445566778899aabbccddeeff"
KEY = b"k" * 32
NOW = "2026-06-18T00:00:00Z"
ORIGIN = "dashboard:chat-7"

THREE_PHASES = (
    'META = {"name": "triage"}\n'
    "async def workflow(ctx):\n"
    "    ctx.phase('read')\n"
    "    a = await ctx.agent('read the issue', label='reader')\n"
    "    ctx.phase('plan')\n"
    "    b, c = await ctx.parallel([\n"
    "        lambda: ctx.agent('plan one', label='p1'),\n"
    "        lambda: ctx.agent('plan two', label='p2'),\n"
    "    ])\n"
    "    ctx.phase('write')\n"
    "    d = await ctx.agent('write it up', label='writer')\n"
    "    return [a, b, c, d]\n"
)


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, KEY))
    yield exporter
    turns.reset_for_testing()
    runs.reset_for_testing()
    trace_workflows.reset_for_testing()
    provider.reset_for_testing()


async def _events(*events):
    for event in events:
        yield event


async def _agent(prompt, opts):
    """A step: one traced turn of a provider, then its text."""
    source = SimpleNamespace(
        session_id=f"acp-{prompt}", _session_key="wf:run", _model="auto", _resolved_model_id=""
    )
    usage = TurnUsage(credits=1.0)
    async for _ in turns.traced(
        source, _events(AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=usage))
    ):
        pass
    return f"done:{prompt}"


def _runner():
    return WorkflowRunner(agent_fn=_agent, audit=lambda *a, **k: None)


async def _run(run_id, *, source=THREE_PHASES, **kwargs):
    res = await _runner().run(source, run_id=run_id, now=NOW, session_key=ORIGIN, **kwargs)
    assert res.ok, res.error
    return res


def _hex(value, width):
    return format(value, f"0{width}x")


def _parent(span):
    return _hex(span.parent.span_id, 16) if span.parent is not None else ""


def _named(exporter, prefix):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def _tree(exporter):
    return sorted(
        (s.name, _hex(s.context.trace_id, 32), _hex(s.context.span_id, 16), _parent(s))
        for s in exporter.get_finished_spans()
    )


def _assert_shape(exporter):
    """The tree: workflow anchor -> three phases in order -> their agent turns -> outcome."""
    (root,) = _named(exporter, "workflow triage")
    (outcome,) = _named(exporter, "workflow outcome")
    root_id = _hex(root.context.span_id, 16)
    assert root.parent is None and _parent(outcome) == root_id
    phases = sorted(_named(exporter, "phase "), key=lambda s: s.attributes["kirocrew.phase_index"])
    assert [p.name for p in phases] == ["phase read", "phase plan", "phase write"]
    assert [p.attributes["kirocrew.phase_index"] for p in phases] == [1, 2, 3]
    assert all(_parent(p) == root_id for p in phases)
    expected = {
        "phase read": {"invoke_agent read:reader"},
        "phase plan": {"invoke_agent plan:p1", "invoke_agent plan:p2"},
        "phase write": {"invoke_agent write:writer"},
    }
    agents = _named(exporter, "invoke_agent")
    for phase in phases:
        under = {a.name for a in agents if _parent(a) == _hex(phase.context.span_id, 16)}
        assert under == expected[phase.name], phase.name
    assert outcome.attributes["kirocrew.usage.turns_total"] == 4
    assert outcome.attributes["kirocrew.usage.credits_total"] == 4.0


@pytest.mark.asyncio
async def test_a_workflow_run_is_one_trace_of_its_phases_agents_and_outcome(spans):
    await _run("wf_000101")
    (root,) = _named(spans, "workflow triage")
    (outcome,) = _named(spans, "workflow outcome")
    phases = {s.name: s for s in _named(spans, "phase ")}
    agents = {s.name: s for s in _named(spans, "invoke_agent")}
    root_id = _hex(root.context.span_id, 16)
    assert root.parent is None
    assert _hex(root.context.trace_id, 32) == ids.trace_id(NS, "workflow", "wf_000101", 1)
    assert set(phases) == {"phase read", "phase plan", "phase write"}
    assert {_parent(p) for p in phases.values()} == {root_id}
    assert set(agents) == {
        "invoke_agent read:reader",
        "invoke_agent plan:p1",
        "invoke_agent plan:p2",
        "invoke_agent write:writer",
    }
    for name, agent in agents.items():
        phase = phases["phase " + name.split(" ", 1)[1].split(":")[0]]
        assert _parent(agent) == _hex(phase.context.span_id, 16), name
        assert agent.context.trace_id == root.context.trace_id
        assert agent.attributes["kirocrew.workflow_run_id"] == "wf_000101"
        assert agent.attributes["kirocrew.trigger"] == "workflow"
    assert outcome.name == "workflow outcome finished"
    assert _parent(outcome) == root_id
    # Roll-ups: the run carries all four turns' spend, each phase its own.
    assert outcome.attributes["kirocrew.usage.credits_total"] == 4.0
    assert outcome.attributes["kirocrew.usage.turns_total"] == 4
    assert phases["phase plan"].attributes["kirocrew.usage.credits_total"] == 2.0
    assert phases["phase read"].attributes["kirocrew.usage.turns_total"] == 1
    assert phases["phase plan"].attributes["kirocrew.phase_index"] == 2
    # One session.id across the whole trace: the launching session's (R6).
    session = correlation.keyed_hash(KEY, ORIGIN)
    assert {s.attributes.get("session.id") for s in spans.get_finished_spans()} == {session}


@pytest.mark.asyncio
async def test_a_phase_ends_at_the_next_phase_and_the_last_at_the_run_end(spans):
    await _run("wf_000102")
    phases = sorted(_named(spans, "phase "), key=lambda s: s.attributes["kirocrew.phase_index"])
    (outcome,) = _named(spans, "workflow outcome")
    for earlier, later in zip(phases, phases[1:]):
        assert earlier.end_time <= later.start_time
    assert phases[-1].end_time <= outcome.end_time


@pytest.mark.asyncio
async def test_a_phase_another_branch_ends_keeps_the_spend_of_its_calls_in_flight(spans):
    script = (
        'META = {"name": "overlap"}\n'
        "async def workflow(ctx):\n"
        "    async def one():\n"
        "        ctx.phase('a')\n"
        "        return await ctx.agent('x', label='x')\n"
        "    async def two():\n"
        "        ctx.phase('b')\n"
        "        return await ctx.agent('y', label='y')\n"
        "    return await ctx.parallel([one, two])\n"
    )

    y_called = asyncio.Event()

    async def ordered(prompt, opts):
        # x is held until y is called, which branch two does only after ctx.phase('b'),
        # so x is still in flight when the other branch opens its phase.
        if prompt == "x":
            await await_until(y_called, what="branch two's call")
        else:
            y_called.set()
        return await _agent(prompt, opts)

    res = await finished(
        WorkflowRunner(agent_fn=ordered, audit=lambda *a, **k: None).run(
            script, run_id="wf_000113", now=NOW, session_key=ORIGIN
        ),
        "the overlapping run",
    )
    assert res.ok, res.error
    phases = {s.name: s for s in _named(spans, "phase ")}
    assert phases["phase a"].attributes["kirocrew.usage.credits_total"] == 1.0
    assert phases["phase b"].attributes["kirocrew.usage.credits_total"] == 1.0
    # Still ended where the other branch's phase began, not when its call settled.
    assert phases["phase a"].end_time <= phases["phase b"].start_time


@pytest.mark.asyncio
async def test_an_authored_run_puts_its_authoring_turn_under_the_authoring_phase(spans):
    async def author(intent, *, on_progress):
        await _agent("author", {})  # the author streams its turn in the task it is given
        return {"ok": True, "source": THREE_PHASES}

    res = await _runner().run(
        "", run_id="wf_000116", now=NOW, session_key=ORIGIN, intent="triage", author_fn=author
    )
    assert res.ok, res.error
    (authoring,) = _named(spans, "phase Authoring")
    (turn,) = _named(spans, "invoke_agent Authoring")
    assert turn.context.trace_id == authoring.context.trace_id
    assert _parent(turn) == _hex(authoring.context.span_id, 16)
    assert authoring.attributes["kirocrew.usage.credits_total"] == 1.0
    (outcome,) = _named(spans, "workflow outcome")
    assert outcome.attributes["kirocrew.usage.turns_total"] == 5


@pytest.mark.asyncio
async def test_a_pooled_step_under_a_timeout_keeps_its_turn_under_its_phase(spans):
    # A pooled step runs as ``asyncio.wait_for(stream, timeout)`` (agent_pool._run_step).
    async def pooled(prompt, opts):
        return await asyncio.wait_for(_agent(prompt, opts), 30)

    res = await WorkflowRunner(agent_fn=pooled, audit=lambda *a, **k: None).run(
        THREE_PHASES, run_id="wf_000114", now=NOW, session_key=ORIGIN
    )
    assert res.ok, res.error
    _assert_shape(spans)


@pytest.mark.asyncio
async def test_a_three_phase_replay_reproduces_the_live_tree(spans):
    await _run("wf_000103")
    _assert_shape(spans)
    live = _tree(spans)
    spans.clear()
    turns.reset_for_testing()
    runs.reset_for_testing()
    trace_workflows.reset_for_testing()
    await _run("wf_000103")
    _assert_shape(spans)
    assert _tree(spans) == live
    assert len(live) == 1 + 3 + 5 * 4 + 1  # root, phases, 5 spans per turn, outcome


@pytest.mark.asyncio
async def test_a_resumed_run_re_emits_no_agent_span(spans):
    fingerprints: dict = {}

    def checkpoint(call_index, *, result, ok, error, fingerprint):
        fingerprints[call_index] = fingerprint

    first = await _run("wf_000104", on_agent_result=checkpoint)
    first_trace = {s.context.trace_id for s in spans.get_finished_spans()}
    spans.clear()
    await _run(
        "wf_000105",
        replay_results=dict(first.agent_results),
        replay_before=len(first.agent_results),
        replay_fingerprints=fingerprints,
    )
    assert not _named(spans, "invoke_agent") and not _named(spans, "chat")
    assert not [s for s in spans.get_finished_spans() if s.context.trace_id in first_trace]
    (outcome,) = _named(spans, "workflow outcome")
    assert "kirocrew.usage.turns_total" not in outcome.attributes


@pytest.mark.asyncio
async def test_a_run_that_fails_or_is_cancelled_says_so(spans):
    failing = 'META = {"name": "boom"}\n' "async def workflow(ctx):\n" "    raise ValueError('x')\n"
    res = await _runner().run(failing, run_id="wf_000106", now=NOW, session_key=ORIGIN)
    assert not res.ok
    assert _named(spans, "workflow outcome failed")


@pytest.mark.asyncio
async def test_a_cancelled_run_says_so_and_still_ends_its_phase(spans):
    script = (
        'META = {"name": "stop"}\n'
        "async def workflow(ctx):\n"
        "    ctx.phase('wait')\n"
        "    return await ctx.agent('x', label='x')\n"
    )
    never = asyncio.Event()

    async def hang(prompt, opts):
        await never.wait()

    runner = WorkflowRunner(agent_fn=hang, audit=lambda *a, **k: None)
    task = asyncio.create_task(runner.run(script, run_id="wf_000115", now=NOW, session_key=ORIGIN))
    await until_parked(never)  # the call is in flight
    task.cancel()
    res = await await_until(task, what="the cancelled run")
    assert res.error == "cancelled"
    assert _named(spans, "workflow outcome cancelled")
    assert _named(spans, "phase wait")


@pytest.mark.asyncio
async def test_phase_index_rides_the_run_events(spans):
    res = await _run("wf_000107")
    phase_events = [e for e in res.events if e.type == "phase_started"]
    agent_events = [e for e in res.events if e.type == "agent_started"]
    assert [e.data["phase_index"] for e in phase_events] == [1, 2, 3]
    assert sorted(e.data["phase_index"] for e in agent_events) == [1, 2, 2, 3]


@pytest.mark.asyncio
async def test_off_a_run_traces_nothing_and_its_events_still_carry_phase_index():
    provider.reset_for_testing()
    res = await _run("wf_000108")
    assert [e.data["phase_index"] for e in res.events if e.type == "phase_started"] == [1, 2, 3]
    assert trace_workflows._run("wf_000108") is None  # no run kept, so no call bound under it


# ── the completion turn ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_completion_turn_joins_the_run_and_links_to_its_launcher(spans):
    launcher = SimpleNamespace(
        session_id="acp-launch", _session_key=ORIGIN, _model="auto", _resolved_model_id=""
    )
    launch_turn = turns.start_turn(launcher)
    trace_workflows.pin_launcher("wf_000109", ORIGIN)
    launch_turn.observe(AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage()))
    launch_turn.finish(None)
    await _run("wf_000109")
    (root,) = _named(spans, "workflow triage")

    async def completion():
        # The real dashboard binding a top-level turn takes (dashboard/trace_binding.py).
        from kiro_crew.dashboard import trace_binding

        token = await trace_binding.bind_turn(
            SimpleNamespace(), SimpleNamespace(key="chat-7"), "c" * 32, top=True
        )
        try:
            source = SimpleNamespace(
                session_id="acp-done", _session_key=ORIGIN, _model="auto", _resolved_model_id=""
            )
            async for _ in turns.traced(
                source,
                _events(AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage())),
            ):
                pass
        finally:
            turns.release_turn_root(token)

    # As workflow_startup's _auto_turn starts it: the turn's task copies this context.
    with trace_workflows.delivering("wf_000109"):
        task = asyncio.ensure_future(completion())
    await task
    done = [
        s
        for s in _named(spans, "invoke_agent")
        if s.attributes.get("kirocrew.acp_session_id") == "acp-done"
    ]
    (turn,) = done
    assert turn.context.trace_id == root.context.trace_id
    assert _parent(turn) == _hex(root.context.span_id, 16)
    assert turn.attributes["kirocrew.turn_id"] == "c" * 32
    assert turn.attributes["kirocrew.workflow_run_id"] == "wf_000109"
    (link,) = turn.links
    assert link.context.trace_id == launch_turn.turn_span.get_span_context().trace_id
    assert link.context.span_id == launch_turn.turn_span.get_span_context().span_id


def test_a_cause_is_taken_once_and_only_where_it_was_handed_over():
    trace_workflows.reset_for_testing()
    assert trace_workflows.completion_binding() == (None, (), {})
    with trace_workflows.delivering("wf_unknown"):
        assert trace_workflows.completion_binding() == (None, (), {})


@pytest.mark.asyncio
async def test_a_queued_completion_turn_takes_its_run_from_the_entry(spans):
    await _run("wf_000110")
    entries = [{"id": "q1", "meta": {}}, {"id": "q2", "meta": {"workflow_run_id": "wf_000110"}}]
    assert trace_workflows.run_of(entries) == "wf_000110"
    assert trace_workflows.run_of([{"meta": "x"}, {}]) == ""
    with trace_workflows.delivering(trace_workflows.run_of(entries)):
        join, _links, _attrs = trace_workflows.completion_binding()
        assert trace_workflows.completion_binding() == (None, (), {})  # taken once
    (root,) = _named(spans, "workflow triage")
    assert join is not None and join.span_id == _hex(root.context.span_id, 16)


# ── content safety ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unlabeled_call_never_puts_its_prompt_in_a_span_name(spans):
    script = (
        'META = {"name": "quiet"}\n'
        "async def workflow(ctx):\n"
        "    ctx.phase('read')\n"
        "    return await ctx.agent('summarise my private notes on the merger')\n"
    )
    await _run("wf_000111", source=script)
    names = " ".join(s.name for s in spans.get_finished_spans())
    assert "private" not in names and "summarise" not in names
    (call,) = _named(spans, "invoke_agent")
    assert call.name == "invoke_agent read:a0"


def test_a_name_is_redacted_before_it_is_cut():
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"  # split: not a credential-shaped literal in the repo
    title = "x" * 55 + " " + secret
    assert "AKIA" not in trace_workflows.label(title)
    assert len(trace_workflows.label(title)) <= trace_workflows.NAME_MAX


@pytest.mark.asyncio
async def test_the_outcome_and_the_completion_turn_carry_the_run_id(spans):
    await _run("wf_000112")
    (outcome,) = _named(spans, "workflow outcome")
    assert outcome.attributes["kirocrew.workflow_run_id"] == "wf_000112"
    with trace_workflows.delivering("wf_000112"):
        join, links, attrs = trace_workflows.completion_binding()
    assert join is not None and attrs == {"kirocrew.workflow_run_id": "wf_000112"}
