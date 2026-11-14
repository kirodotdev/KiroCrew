"""Phase 5a: a conductor mode round as one trace, through the real work-ledger routes.

Every write goes through ``api_work_ledger_record`` / ``api_work_report`` on an isolated
data home, and every turn binds through ``trace_binding.bind_turn``, so the tree is the
one the gateway would build.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import work_ledger_projection_helpers as helpers
from work_ledger_projection_helpers import ACCEPTANCE, CONDUCTOR, _record, _report, _Slot

from kiro_crew import crew_mode
from kiro_crew import work_ledger as wl
from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent, TurnUsage
from kiro_crew.dashboard import trace_binding
from kiro_crew.tracing import correlation, provider, runs, turns, work

_isolated = helpers._isolated
recorded = helpers.recorded

NS = "00112233445566778899aabbccddeeff"
KEY = b"k" * 32
WORKER_A = "chat-9-worker-a"
WORKER_B = "chat-9-worker-b"
#: Free text fed to every field that takes it: none of it may reach a span.
SENTINEL = "SENTINEL-do-not-trace"


@pytest.fixture
def spans(monkeypatch):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, KEY))
    rows = {CONDUCTOR: {crew_mode.ROOT_FIELD: True}}
    monkeypatch.setattr(trace_binding, "_stamped_row", lambda state, key: rows.get(key, {}))
    exporter.rows = rows
    yield exporter
    turns.reset_for_testing()
    runs.reset_for_testing()
    work.reset_for_testing()
    provider.reset_for_testing()


class _Source:
    def __init__(self, slot: str, agent: str) -> None:
        self.session_id = f"unit:{slot}"  # the acting unit the ``recorded`` fixture resolves
        self._session_key = f"dashboard:{slot}"
        self._model = "auto"
        self._resolved_model_id = ""
        self.active_agent = agent


async def _turn(slot: str, *writes, credits: float = 1.0) -> str:
    """One dashboard turn on *slot*, bound as ``_run_chat`` binds it; *writes* run inside it."""
    turn_id = uuid.uuid4().hex
    agent = "kirocrew-conductor" if slot == CONDUCTOR else "kirocrew-worker"
    token = await trace_binding.bind_turn(
        SimpleNamespace(), SimpleNamespace(key=slot), turn_id, top=True
    )

    async def events():
        for write in writes:
            await write()
        yield AcpEvent(
            kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage(credits=credits)
        )

    try:
        async for _ in turns.traced(_Source(slot, agent), events()):
            pass
    finally:
        turns.release_turn_root(token)
    return turn_id


def _ok(result):
    status, body = result
    assert status == 200, body
    return body


async def _goal(round_number: int, **extra) -> None:
    _ok(
        await _record(
            CONDUCTOR, {"action": "goal", "goal": SENTINEL, "round": round_number, **extra}
        )
    )


async def _item(worker: str, spans) -> str:
    """create + bind *worker*, both written inside one conductor turn."""
    made: dict[str, str] = {}

    async def create():
        body = _ok(
            await _record(
                CONDUCTOR, {"action": "create", "title": SENTINEL, "acceptance": ACCEPTANCE}
            )
        )
        made["id"] = body["item"]["item_id"]

    async def bind():
        helpers._SLOTS[worker] = _Slot(created_by=CONDUCTOR)
        _ok(
            await _record(
                CONDUCTOR, {"action": "bind", "item_id": made["id"], "worker_session_key": worker}
            )
        )

    await _turn(CONDUCTOR, create, bind)
    spans.rows[worker] = {crew_mode.WORKER_FIELD: CONDUCTOR, crew_mode.DISPATCHER_FIELD: CONDUCTOR}
    return made["id"]


async def _work(worker: str, credits: float) -> str:
    async def report():
        _ok(
            await _report(
                worker,
                {"status": "done", "summary": SENTINEL, "artifacts": {"note": SENTINEL}},
            )
        )

    return await _turn(worker, report, credits=credits)


async def _accept(item_id: str) -> None:
    _ok(await _record(CONDUCTOR, {"action": "verdict", "item_id": item_id, "verdict": "pass"}))
    _ok(await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}))


def _hex(span_id: int) -> str:
    return format(span_id, "016x")


def _parent(span) -> str:
    return _hex(span.parent.span_id) if span.parent is not None else ""


def _named(finished, name):
    return [s for s in finished if s.name == name]


def _turn_span(finished, turn_id):
    (span,) = [
        s
        for s in finished
        if s.name.startswith("invoke_agent") and s.attributes.get("kirocrew.turn_id") == turn_id
    ]
    return span


def _session(key: str) -> str:
    return correlation.keyed_hash(KEY, key)


async def _two_rounds(spans) -> dict:
    await _goal(1)
    item_a = await _item(WORKER_A, spans)
    worker_a = await _work(WORKER_A, credits=2.5)
    await _accept(item_a)
    await _goal(2)
    conductor_2 = await _turn(CONDUCTOR)
    item_b = await _item(WORKER_B, spans)
    worker_b = await _work(WORKER_B, credits=4.0)
    await _accept(item_b)
    await _goal(3)  # a round ends when the board moves on
    return {"a": item_a, "b": item_b, "wa": worker_a, "wb": worker_b, "c2": conductor_2}


@pytest.mark.asyncio
async def test_two_rounds_are_two_linked_traces_with_one_goal_id(spans, recorded):
    ran = await _two_rounds(spans)
    finished = spans.get_finished_spans()
    (round_1,) = _named(finished, "ultra round 1")
    (round_2,) = _named(finished, "ultra round 2")
    goal_id = wl.read_conductor(CONDUCTOR).goal_id
    assert goal_id and round_1.attributes["kirocrew.work.goal_id"] == goal_id
    assert round_2.attributes["kirocrew.work.goal_id"] == goal_id
    assert round_1.context.trace_id != round_2.context.trace_id
    assert [(link.context.trace_id, link.context.span_id) for link in round_2.links] == [
        (round_1.context.trace_id, round_1.context.span_id)
    ]
    conductor_session = _session(f"dashboard:{CONDUCTOR}")
    for rnd, item_id, worker_turn, worker in (
        (round_1, ran["a"], ran["wa"], WORKER_A),
        (round_2, ran["b"], ran["wb"], WORKER_B),
    ):
        trace = [s for s in finished if s.context.trace_id == rnd.context.trace_id]
        anchor = _hex(rnd.context.span_id)
        (item,) = _named(trace, f"work_item {item_id}")
        assert _parent(item) == anchor
        worker_span = _turn_span(trace, worker_turn)
        assert _parent(worker_span) == _hex(item.context.span_id)
        assert worker_span.attributes["kirocrew.session_key_hash"] == _session(
            f"dashboard:{worker}"
        )
        (report,) = _named(trace, "kirocrew.work.report")
        assert _parent(report) == _hex(worker_span.context.span_id)
        assert report.attributes["kirocrew.work.status"] == "done"
        conductor_turns = [s for s in trace if s.name == "invoke_agent kirocrew-conductor"]
        assert conductor_turns and all(_parent(s) == anchor for s in conductor_turns)
        for action in ("create", "bind"):
            (write,) = _named(trace, f"kirocrew.work.{action}")
            assert _parent(write) in {_hex(s.context.span_id) for s in conductor_turns}
            assert write.attributes["kirocrew.work.item_id"] == item_id
        (done,) = _named(trace, "work_item outcome accepted")
        assert _parent(done) == _hex(item.context.span_id)
        assert done.attributes["kirocrew.work.verdict"] == "pass"
        (closed,) = _named(trace, "ultra outcome closed")
        assert _parent(closed) == anchor
        assert all(s.attributes["session.id"] == conductor_session for s in trace)
    assert _named(finished, "work_item outcome accepted")[0].attributes[
        "kirocrew.usage.credits_total"
    ] == pytest.approx(2.5)
    assert _turn_span(finished, ran["c2"]).context.trace_id == round_2.context.trace_id


@pytest.mark.asyncio
async def test_no_ledger_text_reaches_a_span(spans, recorded):
    await _two_rounds(spans)
    made = _ok(
        await _record(CONDUCTOR, {"action": "create", "title": SENTINEL, "acceptance": ACCEPTANCE})
    )
    decide = {"action": "decide", "item_id": made["item"]["item_id"], "decision": SENTINEL}
    _ok(await _record(CONDUCTOR, decide))
    assert _named(spans.get_finished_spans(), "kirocrew.work.decide")
    for span in spans.get_finished_spans():
        assert SENTINEL not in span.name
        assert not any(SENTINEL in str(value) for value in span.attributes.values())


@pytest.mark.asyncio
async def test_a_restart_rederives_the_anchor_and_emits_it_once(spans, recorded):
    await _goal(1)
    first = await _turn(CONDUCTOR)
    # A restart: every in-process registry is gone; the ledger on disk remains.
    turns.reset_for_testing()
    runs.reset_for_testing()
    work.reset_for_testing()
    second = await _turn(CONDUCTOR)
    finished = spans.get_finished_spans()
    (anchor,) = _named(finished, "ultra round 1")
    for turn_id in (first, second):
        span = _turn_span(finished, turn_id)
        assert span.context.trace_id == anchor.context.trace_id
        assert _parent(span) == _hex(anchor.context.span_id)
    rnd = work.round_of(CONDUCTOR, wl.read_conductor(CONDUCTOR))
    again = work._round_ref(provider.peek(), rnd)
    assert (int(again.trace_id, 16), int(again.span_id, 16)) == (
        anchor.context.trace_id,
        anchor.context.span_id,
    )


@pytest.mark.asyncio
async def test_an_item_open_across_a_round_change_moves_to_the_new_round(spans, recorded):
    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    await _goal(2)
    worker_turn = await _work(WORKER_A, credits=1.0)
    finished = spans.get_finished_spans()
    (round_1,) = _named(finished, "ultra round 1")
    (round_2,) = _named(finished, "ultra round 2")
    (advanced,) = _named(finished, "ultra outcome advanced")
    assert _parent(advanced) == _hex(round_1.context.span_id)
    moved = [
        s
        for s in _named(finished, f"work_item {item_id}")
        if s.context.trace_id == round_2.context.trace_id
    ]
    assert len(moved) == 1
    span = _turn_span(finished, worker_turn)
    assert _parent(span) == _hex(moved[0].context.span_id)


@pytest.mark.asyncio
async def test_a_new_goal_mints_a_new_goal_id(spans, recorded):
    await _goal(1)
    first = wl.read_conductor(CONDUCTOR).goal_id
    await _goal(1)  # a reword keeps the goal
    assert wl.read_conductor(CONDUCTOR).goal_id == first
    await _goal(1, new_goal=True)
    second = wl.read_conductor(CONDUCTOR).goal_id
    assert second and second != first
    anchors = _named(spans.get_finished_spans(), "ultra round 1")
    assert [a.attributes["kirocrew.work.goal_id"] for a in anchors] == [first, second]


@pytest.mark.asyncio
async def test_turn_mode_roots_every_turn_and_keeps_the_write_in_it(spans, recorded, monkeypatch):
    monkeypatch.setattr(provider, "_conductor_mode_shape", provider.CONDUCTOR_MODE_TURN)
    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    worker_turn = await _work(WORKER_A, credits=1.0)
    finished = spans.get_finished_spans()
    assert not [s for s in finished if s.name.startswith(("ultra", "work_item"))]
    span = _turn_span(finished, worker_turn)
    assert span.parent is None and span.attributes["kirocrew.work.item_id"] == item_id
    (report,) = _named(finished, "kirocrew.work.report")
    assert _parent(report) == _hex(span.context.span_id)
    # A write no turn made is a trace of its own, in the conductor's session.
    (goal,) = _named(finished, "kirocrew.work.goal")
    assert goal.parent is None
    assert goal.attributes["session.id"] == _session(f"dashboard:{CONDUCTOR}")


@pytest.mark.asyncio
async def test_a_write_no_turn_made_sits_under_its_item(spans, recorded):
    """A person's steer and a peer's report arrive with no local turn: ``unit=""``."""
    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    item = wl.read_work_item(CONDUCTOR, item_id)
    await work.recorded(
        "",
        CONDUCTOR,
        "report",
        actor="worker",
        item=item,
        header=wl.read_conductor(CONDUCTOR),
        event_id="e1",
    )
    finished = spans.get_finished_spans()
    (anchor,) = _named(finished, f"work_item {item_id}")
    (report,) = _named(finished, "kirocrew.work.report")
    assert _parent(report) == _hex(anchor.context.span_id)
    assert report.attributes["kirocrew.work.item_id"] == item_id  # raw: a peer joins on it


@pytest.mark.asyncio
async def test_a_bot_board_is_traced_as_a_mate_root(spans, recorded):
    spans.rows[CONDUCTOR] = {"mode": crew_mode.MATE_SLOT_MODE}
    await _goal(1)
    turn_id = await _turn(CONDUCTOR)
    finished = spans.get_finished_spans()
    (anchor,) = _named(finished, "ultra round 1")
    assert anchor.attributes["kirocrew.crew.root_kind"] == "mate"
    span = _turn_span(finished, turn_id)
    assert span.attributes["kirocrew.crew.root_kind"] == "mate"
    assert _parent(span) == _hex(anchor.context.span_id)


@pytest.mark.asyncio
async def test_a_round_past_its_span_budget_rolls_over(spans, recorded, monkeypatch):
    await _goal(1)
    monkeypatch.setattr(runs, "SPAN_BUDGET", 2)  # the anchor and the goal write
    later = await _turn(CONDUCTOR)
    finished = spans.get_finished_spans()
    (first,) = _named(finished, "ultra round 1")
    (cont,) = _named(finished, "ultra round 1 (cont 2)")
    assert cont.links[0].context.span_id == first.context.span_id
    assert _turn_span(finished, later).context.trace_id == cont.context.trace_id


@pytest.mark.asyncio
async def test_off_reads_nothing_and_emits_nothing(recorded, monkeypatch):
    provider.reset_for_testing()
    monkeypatch.setattr(
        trace_binding, "_stamped_row", lambda state, key: {crew_mode.ROOT_FIELD: True}
    )
    assert await work.header_before(CONDUCTOR, "goal") is work.NOT_READ
    await _goal(1)
    assert work.binding(CONDUCTOR, None) == (None, "")
    await work.recorded(
        "", CONDUCTOR, "goal", actor="conductor", header=wl.read_conductor(CONDUCTOR)
    )
    assert provider.peek() is None


def test_the_board_mints_a_goal_id_that_a_round_bump_keeps():
    wl.ensure_conductor(CONDUCTOR, goal="g")
    minted = wl.read_conductor(CONDUCTOR).goal_id
    assert len(minted) == 16
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g2", round_number=3)
    assert wl.read_conductor(CONDUCTOR).goal_id == minted


@pytest.mark.asyncio
async def test_a_rebuild_keeps_the_goal_id(monkeypatch):
    helpers._real_units(monkeypatch)
    await _goal(1)
    minted = wl.read_conductor(CONDUCTOR).goal_id
    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_conductor(CONDUCTOR).goal_id == minted


async def _create(made: dict) -> None:
    body = _ok(
        await _record(CONDUCTOR, {"action": "create", "title": SENTINEL, "acceptance": ACCEPTANCE})
    )
    made["id"] = body["item"]["item_id"]


def _in(finished, trace_id):
    return [s for s in finished if s.context.trace_id == trace_id]


@pytest.mark.asyncio
async def test_the_turn_that_opens_a_round_puts_its_writes_in_that_round(spans, recorded):
    """A turn is bound before it writes, so the turn that bootstraps a board, or bumps
    its round number, sits outside the round it opens: its writes go to that round,
    linked back to the turn."""
    made: dict[str, str] = {}

    async def bump():
        await _goal(2)

    async def create():
        await _create(made)

    bootstrap = await _turn(CONDUCTOR, lambda: _goal(1))
    opener = await _turn(CONDUCTOR, bump, create)
    finished = spans.get_finished_spans()
    for name, turn_id in (("ultra round 1", bootstrap), ("ultra round 2", opener)):
        (anchor,) = _named(finished, name)
        turn = _turn_span(finished, turn_id)
        assert turn.context.trace_id != anchor.context.trace_id
        (goal,) = _named(_in(finished, anchor.context.trace_id), "kirocrew.work.goal")
        assert _parent(goal) == _hex(anchor.context.span_id)
        assert [link.context.span_id for link in goal.links] == [turn.context.span_id]
    (round_2,) = _named(finished, "ultra round 2")
    trace = _in(finished, round_2.context.trace_id)
    (item,) = _named(trace, f"work_item {made['id']}")
    (create_span,) = _named(trace, "kirocrew.work.create")
    assert _parent(create_span) == _hex(item.context.span_id)


@pytest.mark.asyncio
async def test_a_round_ends_once_with_every_conductor_turns_spend(spans, recorded):
    """Closing the last open item does not end the round: a new item can follow. The
    round ends at the goal write that moves the board on, after the turn making that
    write has added its own spend."""
    await _goal(1)
    first = await _item(WORKER_A, spans)  # conductor turn: 1.0
    await _accept(first)  # no item open now
    second = await _item(WORKER_B, spans)  # conductor turn: 1.0

    async def close_second():
        await _accept(second)

    async def bump():
        await _goal(2)

    await _turn(CONDUCTOR, close_second, bump, credits=3.0)
    (closed,) = _named(spans.get_finished_spans(), "ultra outcome closed")
    assert closed.attributes["kirocrew.usage.credits_total"] == pytest.approx(5.0)
    assert closed.attributes["kirocrew.usage.turns_total"] == 3


@pytest.mark.asyncio
async def test_a_rolled_over_round_keeps_its_items_under_its_continuation(
    spans, recorded, monkeypatch
):
    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    (round_1,) = _named(spans.get_finished_spans(), "ultra round 1")
    monkeypatch.setattr(
        runs, "SPAN_BUDGET", provider.span_count(format(round_1.context.trace_id, "032x"))
    )
    before = await _work(WORKER_A, credits=2.0)  # rolls the round over
    monkeypatch.setattr(runs, "SPAN_BUDGET", 10_000)  # and only once
    after = await _work(WORKER_A, credits=1.0)
    await _accept(item_id)
    finished = spans.get_finished_spans()
    (cont,) = _named(finished, "ultra round 1 (cont 2)")
    trace = _in(finished, cont.context.trace_id)
    (item,) = _named(trace, f"work_item {item_id}")
    assert _parent(item) == _hex(cont.context.span_id)
    for turn_id in (before, after):
        assert _parent(_turn_span(trace, turn_id)) == _hex(item.context.span_id)
    (done,) = _named(trace, "work_item outcome accepted")
    assert _parent(done) == _hex(item.context.span_id)
    assert done.attributes["kirocrew.usage.credits_total"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_a_rebuild_of_a_dirty_cache_keeps_the_goal_id(monkeypatch):
    helpers._real_units(monkeypatch)
    await _goal(1)
    minted = wl.read_conductor(CONDUCTOR).goal_id
    wl.mark_cache_dirty(CONDUCTOR, "an undo failed")
    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_conductor(CONDUCTOR).goal_id == minted


@pytest.mark.asyncio
async def test_an_item_open_across_a_round_change_keeps_its_whole_spend(spans, recorded):
    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    await _work(WORKER_A, credits=2.0)
    await _goal(2)  # the item is still open
    await _work(WORKER_A, credits=1.0)
    await _accept(item_id)
    (done,) = _named(spans.get_finished_spans(), "work_item outcome accepted")
    assert done.attributes["kirocrew.usage.credits_total"] == pytest.approx(3.0)
    assert done.attributes["kirocrew.usage.turns_total"] == 2


@pytest.mark.asyncio
async def test_a_round_older_than_the_age_budget_rolls_over_after_a_restart(
    spans, recorded, monkeypatch
):
    import time

    from kiro_crew.testing.clock import ManualClock

    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    # A restart: the registries are gone, and the round began more than six days ago.
    turns.reset_for_testing()
    runs.reset_for_testing()
    work.reset_for_testing()
    ManualClock(start=time.time() + runs.AGE_BUDGET_SECS + 60).install(monkeypatch, runs)
    worker_turn = await _work(WORKER_A, credits=1.0)
    finished = spans.get_finished_spans()
    (cont,) = _named(finished, "ultra round 1 (cont 2)")
    trace = _in(finished, cont.context.trace_id)
    (item,) = _named(trace, f"work_item {item_id}")
    assert _parent(_turn_span(trace, worker_turn)) == _hex(item.context.span_id)
