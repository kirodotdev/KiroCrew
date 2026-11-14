"""Phase 5a: a conductor mode round as one trace, through the real work-ledger routes.

Every write goes through ``api_work_ledger_record`` / ``api_work_report`` on an isolated
data home, and every turn binds through ``trace_binding.bind_turn``, so the tree is the
one the gateway would build.
"""

from __future__ import annotations

import asyncio
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
from kiro_crew.tracing import workers as trace_workers

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
    trace_workers.reset_for_testing()
    provider.reset_for_testing()


class _Source:
    def __init__(self, slot: str, agent: str) -> None:
        self.session_id = f"unit:{slot}"  # the acting unit the ``recorded`` fixture resolves
        self._session_key = f"dashboard:{slot}"
        self._model = "auto"
        self._resolved_model_id = ""
        self.active_agent = agent


async def _turn(slot: str, *writes, credits: float = 1.0, turn_id: str = "") -> str:
    """One dashboard turn on *slot*, bound as ``_run_chat`` binds it; *writes* run inside it."""
    turn_id = turn_id or uuid.uuid4().hex
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
    (round_1,) = _named(finished, "conductor_mode round 1")
    (round_2,) = _named(finished, "conductor_mode round 2")
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
        (closed,) = _named(trace, "conductor_mode outcome closed")
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
    (anchor,) = _named(finished, "conductor_mode round 1")
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
    (round_1,) = _named(finished, "conductor_mode round 1")
    (round_2,) = _named(finished, "conductor_mode round 2")
    (advanced,) = _named(finished, "conductor_mode outcome advanced")
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
    anchors = _named(spans.get_finished_spans(), "conductor_mode round 1")
    assert [a.attributes["kirocrew.work.goal_id"] for a in anchors] == [first, second]


@pytest.mark.asyncio
async def test_turn_mode_roots_every_turn_and_keeps_the_write_in_it(spans, recorded, monkeypatch):
    monkeypatch.setattr(provider, "_conductor_mode_shape", provider.CONDUCTOR_MODE_TURN)
    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    worker_turn = await _work(WORKER_A, credits=1.0)
    finished = spans.get_finished_spans()
    assert not [s for s in finished if s.name.startswith(("conductor_mode", "work_item"))]
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
    (anchor,) = _named(finished, "conductor_mode round 1")
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
    (first,) = _named(finished, "conductor_mode round 1")
    (cont,) = _named(finished, "conductor_mode round 1 (cont 2)")
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
    for name, turn_id in (
        ("conductor_mode round 1", bootstrap),
        ("conductor_mode round 2", opener),
    ):
        (anchor,) = _named(finished, name)
        turn = _turn_span(finished, turn_id)
        assert turn.context.trace_id != anchor.context.trace_id
        (goal,) = _named(_in(finished, anchor.context.trace_id), "kirocrew.work.goal")
        assert _parent(goal) == _hex(anchor.context.span_id)
        assert [link.context.span_id for link in goal.links] == [turn.context.span_id]
    (round_2,) = _named(finished, "conductor_mode round 2")
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
    (closed,) = _named(spans.get_finished_spans(), "conductor_mode outcome closed")
    assert closed.attributes["kirocrew.usage.credits_total"] == pytest.approx(5.0)
    assert closed.attributes["kirocrew.usage.turns_total"] == 3


@pytest.mark.asyncio
async def test_a_rolled_over_round_keeps_its_items_under_its_continuation(
    spans, recorded, monkeypatch
):
    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    (round_1,) = _named(spans.get_finished_spans(), "conductor_mode round 1")
    monkeypatch.setattr(
        runs, "SPAN_BUDGET", provider.span_count(format(round_1.context.trace_id, "032x"))
    )
    before = await _work(WORKER_A, credits=2.0)  # rolls the round over
    monkeypatch.setattr(runs, "SPAN_BUDGET", 10_000)  # and only once
    after = await _work(WORKER_A, credits=1.0)
    await _accept(item_id)
    finished = spans.get_finished_spans()
    (cont,) = _named(finished, "conductor_mode round 1 (cont 2)")
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
    (cont,) = _named(finished, "conductor_mode round 1 (cont 2)")
    trace = _in(finished, cont.context.trace_id)
    (item,) = _named(trace, f"work_item {item_id}")
    assert _parent(_turn_span(trace, worker_turn)) == _hex(item.context.span_id)


# ── phase 5b: worker creation, report -> wake links, manager changes, patrol health ──

#: The one SessionManager call ``unit_for_session_key`` makes: a slot's unit is the
#: ACP session id ``_Source`` streams on.
_SESSIONS = SimpleNamespace(
    get_provider=lambda key: SimpleNamespace(session_id=f"unit:{key.removeprefix('dashboard:')}")
)


async def _patrol(*writes) -> str:
    """One turn of the conductor's work-ledger watch."""
    with work.patrol_turn():
        return await _turn(CONDUCTOR, *writes)


def _links(span) -> list[tuple[int, int]]:
    return [(link.context.trace_id, link.context.span_id) for link in span.links]


def _ids(span) -> tuple[int, int]:
    return (span.context.trace_id, span.context.span_id)


@pytest.mark.asyncio
async def test_a_created_worker_links_its_first_turn_to_the_span_that_created_it(spans, recorded):
    await _goal(1)

    async def create():
        trace_workers.created(_SESSIONS, f"dashboard:{CONDUCTOR}", WORKER_A, "create")

    opener = await _turn(CONDUCTOR, create)
    first = await _turn(WORKER_A)
    second = await _turn(WORKER_A)
    finished = spans.get_finished_spans()
    (made,) = _named(finished, "kirocrew.session.create")
    conductor = _turn_span(finished, opener)
    assert _parent(made) == _hex(conductor.context.span_id)
    assert made.attributes["kirocrew.session.op"] == "create"
    assert made.attributes["kirocrew.session.target_hash"] == _session(f"dashboard:{WORKER_A}")
    assert _links(_turn_span(finished, first)) == [_ids(made)]
    assert _links(_turn_span(finished, second)) == []


@pytest.mark.asyncio
async def test_a_cancelled_first_bind_keeps_the_worker_creation_link(spans, recorded, monkeypatch):
    await _goal(1)

    async def create():
        trace_workers.created(_SESSIONS, f"dashboard:{CONDUCTOR}", WORKER_A, "create")

    await _turn(CONDUCTOR, create)

    async def cancelled(_state, slot_key, _hash_key):
        assert slot_key == WORKER_A
        raise asyncio.CancelledError

    with monkeypatch.context() as patched:
        patched.setattr(trace_binding, "_crew_attrs", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await _turn(WORKER_A)

    first = await _turn(WORKER_A)
    second = await _turn(WORKER_A)
    finished = spans.get_finished_spans()
    (made,) = _named(finished, "kirocrew.session.create")
    assert _links(_turn_span(finished, first)) == [_ids(made)]
    assert _links(_turn_span(finished, second)) == []


@pytest.mark.asyncio
async def test_a_fork_outside_a_traced_turn_records_nothing(spans, recorded):
    trace_workers.created(_SESSIONS, "dashboard:chat-nobody", WORKER_A, "fork")
    trace_workers.created(_SESSIONS, f"dashboard:{CONDUCTOR}", WORKER_A, "rename")
    assert not _named(spans.get_finished_spans(), "kirocrew.session.fork")
    assert trace_workers.first_turn_link(WORKER_A) == ()


@pytest.mark.asyncio
async def test_a_patrol_turn_links_once_to_every_report_it_answers(spans, recorded):
    await _goal(1)
    await _item(WORKER_A, spans)
    await _item(WORKER_B, spans)
    await _work(WORKER_A, credits=1.0)
    await _work(WORKER_B, credits=1.0)
    person = await _turn(CONDUCTOR)  # not a patrol turn: the reports stay owed
    wake = await _patrol()
    later = await _patrol()
    finished = spans.get_finished_spans()
    reports = {_ids(span) for span in _named(finished, "kirocrew.work.report")}
    assert len(reports) == 2
    assert _links(_turn_span(finished, person)) == []
    assert set(_links(_turn_span(finished, wake))) == reports
    assert _links(_turn_span(finished, later)) == []


@pytest.mark.asyncio
async def test_a_cancelled_patrol_bind_keeps_report_links_for_the_next_patrol(
    spans, recorded, monkeypatch, tmp_path
):
    from chat_runner_coverage_helpers import _slot, _state

    from kiro_crew.dashboard import chat_runner

    await _goal(1)
    await _item(WORKER_A, spans)
    await _work(WORKER_A, credits=1.0)
    state, slot = _state(tmp_path), _slot(CONDUCTOR)

    async def cancelled(_state, slot_key, _hash_key):
        assert slot_key == CONDUCTOR
        raise asyncio.CancelledError

    slot.task = ending = asyncio.get_running_loop().create_future()
    try:
        with work.patrol_turn():
            with monkeypatch.context() as patched:
                patched.setattr(trace_binding, "_crew_attrs", cancelled)
                with pytest.raises(asyncio.CancelledError):
                    await trace_binding.bind_turn(state, slot, uuid.uuid4().hex, top=True)
            unclaimed = work.in_patrol_turn()
            # Run the real hand-off: it spends only the mark, not the pending reports.
            await chat_runner._hand_off_queue(
                state, slot, drain=False, allow_automatic_successor=False
            )
            assert not work.in_patrol_turn()
            successor = await asyncio.create_task(_turn(CONDUCTOR))
    finally:
        ending.cancel()

    wake = await _patrol()
    later = await _patrol()
    finished = spans.get_finished_spans()
    (report,) = _named(finished, "kirocrew.work.report")
    assert _links(_turn_span(finished, successor)) == []
    assert _links(_turn_span(finished, wake)) == [_ids(report)]
    assert _links(_turn_span(finished, later)) == []
    assert unclaimed


@pytest.mark.asyncio
@pytest.mark.parametrize("slot_key", [CONDUCTOR, WORKER_A])
async def test_a_failed_crew_read_still_consumes_session_links(
    spans, recorded, monkeypatch, slot_key
):
    await _goal(1)
    if slot_key == CONDUCTOR:
        await _item(WORKER_A, spans)
        await _work(WORKER_A, credits=1.0)
        span_name = "kirocrew.work.report"
    else:

        async def create():
            trace_workers.created(_SESSIONS, f"dashboard:{CONDUCTOR}", WORKER_A, "create")

        await _turn(CONDUCTOR, create)
        span_name = "kirocrew.session.create"

    async def failed(_state, key, _hash_key):
        assert key == slot_key
        raise RuntimeError("crew read failed")

    with monkeypatch.context() as patched:
        patched.setattr(trace_binding, "_crew_attrs", failed)
        with work.patrol_turn(slot_key == CONDUCTOR):
            first = await _turn(slot_key)
    with work.patrol_turn(slot_key == CONDUCTOR):
        second = await _turn(slot_key)
    finished = spans.get_finished_spans()
    (cause,) = _named(finished, span_name)
    assert _links(_turn_span(finished, first)) == [_ids(cause)]
    assert _links(_turn_span(finished, second)) == []


@pytest.mark.asyncio
async def test_the_report_link_is_ready_when_the_workers_mirror_lands(spans, recorded, monkeypatch):
    await _goal(1)
    await _item(WORKER_A, spans)
    emit = helpers.routes.crew_log_emit
    append = emit.on_work_recorded
    seen: list[list[tuple[int, int]]] = []

    def mirror_wake(unit, data):
        landed = append(unit, data)
        if data.get("mirror"):
            with work.patrol_turn():
                seen.append(
                    [
                        (link.context.trace_id, link.context.span_id)
                        for link in work.answered_links(CONDUCTOR)
                    ]
                )
        return landed

    monkeypatch.setattr(emit, "on_work_recorded", mirror_wake)
    await _work(WORKER_A, credits=1.0)
    assert [
        (unit, bool(data.get("mirror")))
        for unit, data in recorded
        if data.get("action") == "report"
    ] == [(f"unit:{CONDUCTOR}", False), (f"unit:{WORKER_A}", True)]
    (report,) = _named(spans.get_finished_spans(), "kirocrew.work.report")
    assert seen == [[_ids(report)]]


@pytest.mark.asyncio
async def test_an_undelivered_report_is_an_event_under_its_item_the_wake_links_to(spans, recorded):
    await _goal(1)
    item_id = await _item(WORKER_A, spans)
    noted = {"conductor_slot_key": CONDUCTOR, "item_id": item_id, "tool": "work_report"}
    await work.report_undelivered({**noted, "at": "2026-11-14T00:00:00+00:00"})
    await work.report_undelivered({**noted, "conductor_slot_key": "", "at": "x"})  # no board
    wake = await _patrol()
    finished = spans.get_finished_spans()
    (anchor,) = _named(finished, f"work_item {item_id}")
    (event,) = _named(finished, "kirocrew.work.report_undelivered")
    assert _parent(event) == _hex(anchor.context.span_id)
    assert event.attributes["kirocrew.work.item_id"] == item_id
    assert _links(_turn_span(finished, wake)) == [_ids(event)]


@pytest.mark.asyncio
async def test_the_conductors_board_read_puts_its_health_on_the_reading_turn(spans, recorded):
    rows = [
        {"state": "open", "stale": True, "orphaned": False, "pending_overdue": False},
        {"state": "open", "stale": False, "report_undelivered": {"tool": "work_report"}},
        {"state": "accepted", "orphaned": True, "pending_overdue": True},
    ]

    async def read():
        work.note_board_health(_SESSIONS, CONDUCTOR, rows)

    turn_id = await _turn(CONDUCTOR, read)
    span = _turn_span(spans.get_finished_spans(), turn_id)
    assert {k: v for k, v in span.attributes.items() if k.endswith("_items")} == {
        "kirocrew.work.open_items": 2,
        "kirocrew.work.stale_items": 1,
        "kirocrew.work.orphaned_items": 1,
        "kirocrew.work.pending_overdue_items": 1,
        "kirocrew.work.undelivered_items": 1,
    }


@pytest.mark.asyncio
async def test_a_manager_change_sits_in_the_calling_turn_or_alone(spans, recorded):
    async def handoff():
        trace_workers.manager_changed(
            _SESSIONS, f"dashboard:{CONDUCTOR}", "handoff", WORKER_A, CONDUCTOR, WORKER_B
        )

    turn_id = await _turn(CONDUCTOR, handoff)
    # A close hands its sessions up with no turn to sit in.
    trace_workers.manager_changed(None, "", "hand_back", WORKER_B, WORKER_A, "")
    trace_workers.manager_changed(None, "", "promote", WORKER_B, WORKER_A, "")  # not an op
    finished = spans.get_finished_spans()
    moved, handed = _named(finished, "kirocrew.session.manager_change")
    assert _parent(moved) == _hex(_turn_span(finished, turn_id).context.span_id)
    assert moved.attributes["kirocrew.session.op"] == "handoff"
    assert moved.attributes["kirocrew.session.previous_manager_hash"] == _session(
        f"dashboard:{CONDUCTOR}"
    )
    assert moved.attributes["kirocrew.session.manager_hash"] == _session(f"dashboard:{WORKER_B}")
    assert handed.parent is None and handed.attributes["kirocrew.session.op"] == "hand_back"
    assert "kirocrew.session.manager_hash" not in handed.attributes
    assert handed.attributes["session.id"] == _session(f"dashboard:{WORKER_B}")
    for span in (moved, handed):
        assert "kirocrew.session.parent_hash" not in span.attributes
        assert "kirocrew.session.previous_parent_hash" not in span.attributes


@pytest.mark.asyncio
@pytest.mark.parametrize("in_turn", [True, False])
@pytest.mark.parametrize(
    ("op", "previous", "parent"),
    [
        ("adopt", "", CONDUCTOR),
        ("adopt", WORKER_B, CONDUCTOR),
        ("release", CONDUCTOR, ""),
    ],
)
async def test_tree_moves_export_parent_hashes_not_manager_hashes(
    spans, recorded, monkeypatch, in_turn, op, previous, parent
):
    from kiro_crew.dashboard.handlers import session_control as routes
    from kiro_crew.tracing import ids

    request = SimpleNamespace(headers={"X-Session-Key": f"dashboard:{CONDUCTOR}"})
    monkeypatch.setattr(routes, "_read_session_key", lambda req: req.headers["X-Session-Key"])
    state = SimpleNamespace(sessions=_SESSIONS)

    async def move():
        routes._trace_manager_change(
            request,
            state,
            op,
            {"target": WORKER_A, "previous_parent": previous, "parent": parent},
            "parent",
            "parent" if op == "adopt" else "",
        )

    async def handoff():
        trace_workers.manager_changed(
            _SESSIONS, f"dashboard:{CONDUCTOR}", "handoff", WORKER_A, CONDUCTOR, WORKER_B
        )

    if in_turn:
        turn_id = await _turn(CONDUCTOR, handoff, move)
    else:
        await move()
    finished = spans.get_finished_spans()
    moves = [s for s in finished if s.attributes.get("kirocrew.session.op") == op]
    assert len(moves) == 1
    (moved,) = moves
    assert moved.name == "kirocrew.session.parent_change"
    expected = {
        "kirocrew.session.op": op,
        "kirocrew.session.target_hash": _session(f"dashboard:{WORKER_A}"),
    }
    if previous:
        expected["kirocrew.session.previous_parent_hash"] = _session(f"dashboard:{previous}")
    if parent:
        expected["kirocrew.session.parent_hash"] = _session(f"dashboard:{parent}")
    assert {k: v for k, v in moved.attributes.items() if k.startswith("kirocrew.session.")} == (
        expected
    )
    if in_turn:
        turn = _turn_span(finished, turn_id)
        assert moved.context.trace_id == turn.context.trace_id
        assert _parent(moved) == _hex(turn.context.span_id)
        # Tree and manager moves share the turn's ordinal: the handoff was first.
        parts = (turn_id, turn.attributes["kirocrew.attempt"], 2, op, WORKER_A, previous, parent)
    else:
        assert moved.parent is None
        parts = (op, WORKER_A, previous, parent)
        assert moved.context.trace_id == int(ids.trace_id(NS, "session.parent_change", *parts), 16)
        assert moved.attributes["session.id"] == _session(f"dashboard:{WORKER_A}")
    assert _hex(moved.context.span_id) == ids.span_id(NS, "session.parent_change", *parts)


def test_the_routes_name_the_managers_their_results_carry(monkeypatch):
    from kiro_crew.dashboard.handlers import session_control as routes

    seen = []
    monkeypatch.setattr(trace_workers, "manager_changed", lambda *a: seen.append(a[1:]))
    request = SimpleNamespace(headers={"X-Session-Key": "dashboard:chat-c"})
    monkeypatch.setattr(routes, "_read_session_key", lambda req: req.headers["X-Session-Key"])
    state = SimpleNamespace(sessions=None)
    routes._trace_manager_change(
        request,
        state,
        "handoff",
        {"target": "w", "manager": "m2", "previous_manager": "m1"},
        "manager",
        "manager",
    )
    routes._trace_manager_change(
        request,
        state,
        "adopt",
        {"target": "w", "parent": "p2", "previous_parent": ""},
        "parent",
        "parent",
    )
    routes._trace_manager_change(
        request, state, "release", {"target": "w", "previous_parent": "p1"}, "parent", ""
    )
    assert seen == [
        ("dashboard:chat-c", "handoff", "w", "m1", "m2"),
        ("dashboard:chat-c", "adopt", "w", "", "p2"),
        ("dashboard:chat-c", "release", "w", "p1", ""),
    ]


@pytest.mark.asyncio
async def test_off_the_5b_hooks_record_and_keep_nothing(recorded):
    provider.reset_for_testing()
    trace_workers.created(_SESSIONS, f"dashboard:{CONDUCTOR}", WORKER_A, "create")
    trace_workers.manager_changed(None, "", "hand_back", WORKER_A, CONDUCTOR, "")
    await work.report_undelivered({"conductor_slot_key": CONDUCTOR, "item_id": "it_00000001"})
    work.note_board_health(_SESSIONS, CONDUCTOR, [{"state": "open"}])
    with work.patrol_turn():
        assert work.answered_links(CONDUCTOR) == ()
    assert trace_workers.first_turn_link(WORKER_A) == ()
    assert provider.peek() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("watch", [True, False])
async def test_a_work_ledger_watch_turn_runs_as_a_patrol_turn(watch):
    """The dashboard fire marks the turn it starts for a work-ledger watch, and only it."""
    from unittest.mock import AsyncMock, patch

    import test_autonudge_banner as banner

    orch = banner._orchestrator()
    slot = banner._slot()
    orch.dashboard_state.get_slot = lambda _key: slot
    seen: list[bool] = []

    async def run_chat(*_args, **_kwargs):
        seen.append(work.in_patrol_turn())

    spawned: list[asyncio.Task] = []

    def _spawn(_state, _slot, coro, **_kwargs):
        spawned.append(asyncio.create_task(coro))
        return spawned[-1]

    with (
        patch.object(banner.gw, "spawn_guarded_turn", _spawn),
        patch.object(banner.gw, "compose_nudge_body", new=AsyncMock(return_value="patrol")),
        patch.object(orch, "_work_ledger_wake_loop", lambda loop, wake: watch),
        patch.object(orch, "_work_ledger_wake_text", AsyncMock(return_value="")),
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
    ):
        assert await orch._fire_dashboard_nudge(banner._loop()) is True
        await asyncio.gather(*spawned)
    assert seen == [watch]
    assert work.in_patrol_turn() is False


@pytest.mark.asyncio
async def test_a_turn_a_patrol_turn_hands_off_to_takes_no_report_links(spans, recorded):
    """A queued turn starts, after the patrol turn bound, in a task that copies its context."""
    await _goal(1)
    await _item(WORKER_A, spans)
    await _work(WORKER_A, credits=1.0)
    await _work(WORKER_A, credits=1.0)
    with work.patrol_turn():
        wake = await _turn(CONDUCTOR)
        successor = await asyncio.create_task(_turn(CONDUCTOR))
    finished = spans.get_finished_spans()
    reports = {_ids(span) for span in _named(finished, "kirocrew.work.report")}
    assert set(_links(_turn_span(finished, wake))) == reports
    assert _links(_turn_span(finished, successor)) == []


@pytest.mark.asyncio
async def test_a_patrol_turn_admission_runs_in_a_new_task_still_takes_its_links(spans, recorded):
    """Fair admission runs the turn's body in a task of its own: the mark goes with it."""
    await _goal(1)
    await _item(WORKER_A, spans)
    await _work(WORKER_A, credits=1.0)
    with work.patrol_turn():
        wake = await asyncio.create_task(_turn(CONDUCTOR))
        assert not work.in_patrol_turn()  # taken
    finished = spans.get_finished_spans()
    (report,) = _named(finished, "kirocrew.work.report")
    assert _links(_turn_span(finished, wake)) == [_ids(report)]


@pytest.mark.asyncio
async def test_a_patrol_turn_that_never_bound_leaves_no_mark_for_its_successor(
    spans, recorded, tmp_path
):
    """A watch's turn refused at admission (or bound with tracing off) never claims the
    mark; its queue hand-off spends it, so the queued turn after it takes no links."""
    from chat_runner_coverage_helpers import _slot, _state

    from kiro_crew.dashboard import chat_runner

    await _goal(1)
    await _item(WORKER_A, spans)
    await _work(WORKER_A, credits=1.0)
    state, slot = _state(tmp_path), _slot()
    slot.task = ending = asyncio.get_running_loop().create_future()
    try:
        with work.patrol_turn():
            await chat_runner._hand_off_queue(
                state, slot, drain=False, allow_automatic_successor=False
            )
            successor = await asyncio.create_task(_turn(CONDUCTOR))
    finally:
        ending.cancel()
    wake = await _patrol()
    finished = spans.get_finished_spans()
    (report,) = _named(finished, "kirocrew.work.report")
    assert _links(_turn_span(finished, successor)) == []
    assert _links(_turn_span(finished, wake)) == [_ids(report)]


@pytest.mark.asyncio
async def test_a_long_turn_keeps_its_move_count_however_many_other_turns_move(
    spans, recorded, monkeypatch
):
    """The count lives on the turn: no table evicts it while the turn runs."""
    other_moves = 5
    # A small bound stands in for a long gateway life reaching the real one.
    monkeypatch.setattr(trace_workers, "_MAX", 2)

    async def move():
        trace_workers.manager_changed(
            _SESSIONS, f"dashboard:{CONDUCTOR}", "handoff", WORKER_A, "", WORKER_B
        )

    async def long_turn():
        await move()
        for _ in range(other_moves):  # other sessions' turns, each with a move
            await asyncio.create_task(_turn(WORKER_B, _other))
        await move()

    async def _other():
        trace_workers.manager_changed(
            _SESSIONS, f"dashboard:{WORKER_B}", "release", WORKER_A, WORKER_B, ""
        )

    await _turn(CONDUCTOR, long_turn)
    finished = spans.get_finished_spans()
    manager_moves = _named(finished, "kirocrew.session.manager_change")
    parent_moves = _named(finished, "kirocrew.session.parent_change")
    assert len(manager_moves) == 2
    assert len(parent_moves) == other_moves
    assert len({_ids(span) for span in manager_moves + parent_moves}) == other_moves + 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "span_name"),
    [
        ("handoff", "kirocrew.session.manager_change"),
        ("adopt", "kirocrew.session.parent_change"),
        ("release", "kirocrew.session.parent_change"),
    ],
)
async def test_two_moves_of_one_session_in_one_turn_are_two_spans_with_replayable_ids(
    spans, recorded, op, span_name
):
    previous, manager = (WORKER_B, "") if op == "release" else ("", WORKER_B)

    async def twice():
        for _ in range(2):  # the same move twice: only its ordinal tells them apart
            trace_workers.manager_changed(
                _SESSIONS, f"dashboard:{CONDUCTOR}", op, WORKER_A, previous, manager
            )

    turn_id = "fade" * 8
    await _turn(CONDUCTOR, twice, turn_id=turn_id)
    first = [_ids(s) for s in _named(spans.get_finished_spans(), span_name)]
    assert len(set(first)) == 2
    # The same turn replayed in a fresh process gives the same two ids.
    spans.clear()
    turns.reset_for_testing()
    trace_workers.reset_for_testing()
    await _turn(CONDUCTOR, twice, turn_id=turn_id)
    again = [_ids(s) for s in _named(spans.get_finished_spans(), span_name)]
    assert again == first


@pytest.mark.asyncio
async def test_a_hand_back_has_the_same_id_however_often_it_is_replayed(spans, recorded):
    for _ in range(2):
        trace_workers.manager_changed(None, "", "hand_back", WORKER_B, WORKER_A, CONDUCTOR)
    trace_workers.manager_changed(None, "", "hand_back", WORKER_B, WORKER_A, "")
    moves = _named(spans.get_finished_spans(), "kirocrew.session.manager_change")
    assert len(moves) == 3 and len({_ids(span) for span in moves}) == 2


@pytest.mark.asyncio
async def test_a_create_registers_its_link_before_the_first_message_starts_the_worker(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.handlers import session_control as routes

    order: list[str] = []
    request = helpers._req("POST", "/api/session-control/create", sk="dashboard:chat-c")
    monkeypatch.setattr(routes, "_require_internal", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_body", AsyncMock(return_value={"message": "start"}))
    monkeypatch.setattr(routes, "_read_session_key", lambda req: "dashboard:chat-c")
    monkeypatch.setattr(routes, "_refuse_unprompted_seed", AsyncMock())
    monkeypatch.setattr(routes.sc, "prewarm_enabled_check", AsyncMock())
    monkeypatch.setattr(
        routes.sc, "create_session", AsyncMock(return_value={"ok": True, "target": "chat-w"})
    )

    async def deliver(*_a):
        order.append("first message")
        return {}

    monkeypatch.setattr(routes, "_deliver_first_message", deliver)
    monkeypatch.setattr(trace_workers, "created", lambda *a: order.append(f"created {a[2]} {a[3]}"))
    from kiro_crew.dashboard.handlers import work_ledger as ledger_routes

    monkeypatch.setattr(ledger_routes, "holds_ledger", AsyncMock(return_value=False))
    monkeypatch.setattr(routes.conductor_patrol, "ensure_patrol", AsyncMock(return_value=("", "")))
    monkeypatch.setattr(routes.conductor_patrol, "patrol_note", lambda *a, **k: "")
    monkeypatch.setattr(routes, "_add_worker_capabilities", AsyncMock())
    monkeypatch.setattr(routes, "_write_crew_dispatch_notice", lambda *a: None)
    response = await routes.api_session_control_create(request)
    assert response.status == 200
    assert order == ["created chat-w create", "first message"]


@pytest.mark.asyncio
async def test_a_cancelled_undelivered_trace_still_wakes_the_conductor(monkeypatch):
    from kiro_crew import conductor_wake, undelivered_call_watch
    from kiro_crew import work_ledger as ledger

    noted = {"conductor_slot_key": CONDUCTOR, "item_id": "it_00000001", "tool": "work_report"}
    woken: list[tuple[str, str]] = []
    monkeypatch.setattr(ledger, "undelivered_seen", lambda key: 1)
    monkeypatch.setattr(ledger, "note_undelivered", lambda *a, **k: noted)

    async def cancelled(_noted):
        raise asyncio.CancelledError

    monkeypatch.setattr(work, "report_undelivered", cancelled)
    monkeypatch.setattr(conductor_wake, "wake_for_undelivered_report", lambda *a: woken.append(a))
    with pytest.raises(asyncio.CancelledError):
        await undelivered_call_watch.record_undelivered_call(WORKER_A, "work_report")
    assert woken == [(CONDUCTOR, "it_00000001")]
