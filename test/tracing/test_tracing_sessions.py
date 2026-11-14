"""Phase 6: session lifecycle spans (``tracing/sessions.py``)."""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent, TurnUsage
from kiro_crew.metrics import sessions as metrics_sessions
from kiro_crew.session_parts import turn_context
from kiro_crew.tracing import correlation, ids, provider, runs, sessions, turns

NS = "00112233445566778899aabbccddeeff"
KEY = b"k" * 32
CHAT = "dashboard:chat-1"
CONDUCTOR = "dashboard:conductor-1"
T0 = 1763049600.0
T1 = 1763049700.0


class _Source:
    def __init__(self, acp_session_id, session_key=CHAT):
        self.session_id = acp_session_id
        self._session_key = session_key
        self._model = "auto"
        self._resolved_model_id = ""
        self.active_agent = "kirocrew-default"


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, KEY))
    yield exporter
    turns.reset_for_testing()
    sessions.reset_for_testing()
    runs.reset_for_testing()
    provider.reset_for_testing()


def _hex(value, width):
    return format(value, f"0{width}x")


def _lifecycle(exporter):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(sessions.SPAN_PREFIX)]


async def _events(*events):
    for event in events:
        yield event


def _turn(source):
    async def run():
        complete = AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage())
        async for _ in turns.traced(source, _events(complete)):
            pass

    asyncio.run(run())


def _a_session_lifetime():
    """Open, compact, switch, recycle and reopen one Crew session: the full lifecycle."""
    sessions.opened(CHAT, _Source("acp-1"), T0, harness="kiro")
    _turn(_Source("acp-1"))
    with sessions.compaction(CHAT) as traced:
        traced.outcome = "ok"
    sessions.switched(CHAT, "model")
    sessions.closed(CHAT, "recycled", T0)
    sessions.opened(CHAT, _Source("acp-2"), T1, harness="kiro")
    _turn(_Source("acp-2"))
    sessions.closed(CHAT, "reset", T1)


def test_a_sessions_spans_share_one_session_id_across_an_acp_recycle(spans):
    _a_session_lifetime()
    finished = spans.get_finished_spans()
    assert {s.attributes["session.id"] for s in finished} == {correlation.keyed_hash(KEY, CHAT)}
    assert {s.attributes["kirocrew.acp_session_id"] for s in _lifecycle(spans)} == {
        "acp-1",
        "acp-2",
    }
    # Each generation is its own short trace; the recycle starts a second one.
    gen1 = ids.trace_id(NS, "session", CHAT, sessions.generation_of(T0))
    gen2 = ids.trace_id(NS, "session", CHAT, sessions.generation_of(T1))
    by_trace = {}
    for s in _lifecycle(spans):
        by_trace.setdefault(_hex(s.context.trace_id, 32), []).append(s.name)
    assert by_trace == {
        gen1: [
            "kirocrew.session.open",
            "kirocrew.session.compaction",
            "kirocrew.session.switch",
            "kirocrew.session.close",
        ],
        gen2: ["kirocrew.session.open", "kirocrew.session.reset"],
    }


def test_every_event_hangs_under_its_generations_open_span(spans):
    _a_session_lifetime()
    by_name = {}
    for s in _lifecycle(spans):
        by_name.setdefault(s.name, []).append(s)
    opens = {s.context.trace_id: s for s in by_name.pop("kirocrew.session.open")}
    assert all(s.parent is None for s in opens.values())
    for group in by_name.values():
        for s in group:
            assert s.parent.span_id == opens[s.context.trace_id].context.span_id
    close = by_name["kirocrew.session.close"][0]
    assert close.attributes["kirocrew.session.end_reason"] == "recycled"
    assert (
        by_name["kirocrew.session.compaction"][0].attributes["kirocrew.session.compaction_outcome"]
        == "ok"
    )
    assert by_name["kirocrew.session.switch"][0].attributes["kirocrew.session.switch_kind"] == (
        "model"
    )


def test_the_same_inputs_give_the_same_ids(spans):
    _a_session_lifetime()
    first = [(s.name, s.context.trace_id, s.context.span_id) for s in _lifecycle(spans)]
    spans.clear()
    sessions.reset_for_testing()
    _a_session_lifetime()
    again = [(s.name, s.context.trace_id, s.context.span_id) for s in _lifecycle(spans)]
    assert first == again
    gen = sessions.generation_of(T0)
    assert _hex(first[0][2], 16) == ids.span_id(NS, "session.open", CHAT, gen, 0)
    assert _hex(first[1][2], 16) == ids.span_id(NS, "session.compaction", CHAT, gen, 1)


def test_tracing_off_emits_nothing(spans):
    provider.install_for_testing(None)
    sessions.note_lineage("dashboard:worker-1", creator=CONDUCTOR)
    _a_session_lifetime()
    sessions.adopted(CHAT, CONDUCTOR)
    assert spans.get_finished_spans() == ()
    assert sessions._lineage == {} and sessions._generations == {}


def test_a_worker_open_names_its_creator_hashed_and_links_to_its_open_span(spans):
    sessions.opened(CONDUCTOR, _Source("acp-c", CONDUCTOR), T0)
    sessions.note_lineage("dashboard:worker-1", creator=CONDUCTOR, slot_key="dashboard:worker-1")
    sessions.opened("dashboard:worker-1", _Source("acp-w", "dashboard:worker-1"), T1)
    conductor_open, worker_open = _lifecycle(spans)
    assert worker_open.attributes["kirocrew.session.relation"] == "worker"
    assert worker_open.attributes["kirocrew.parent_session_key_hash"] == (
        correlation.keyed_hash(KEY, CONDUCTOR)
    )
    (link,) = worker_open.links
    assert link.context.span_id == conductor_open.context.span_id
    assert link.context.trace_id == conductor_open.context.trace_id


@pytest.mark.parametrize(
    ("lineage", "relation"),
    [
        ({"creator": CONDUCTOR, "forked_from": "dashboard:src"}, "worker"),
        ({"forked_from": "dashboard:src"}, "fork"),
        ({"slot_key": "dashboard:twin"}, "linked"),
    ],
)
def test_the_lineage_precedence_is_creator_then_fork_then_linked(spans, lineage, relation):
    sessions.note_lineage("slack:C1-1700000000.000100", **lineage)
    sessions.opened("slack:C1-1700000000.000100", _Source("acp-x"), T0)
    (span,) = _lifecycle(spans)
    assert span.attributes["kirocrew.session.relation"] == relation
    assert span.links == ()  # the parent is not live in this process


def test_a_session_that_is_its_own_slot_names_no_parent(spans):
    sessions.note_lineage(CHAT, slot_key=CHAT)
    sessions.opened(CHAT, _Source("acp-1"), T0)
    (span,) = _lifecycle(spans)
    assert "kirocrew.parent_session_key_hash" not in span.attributes


def test_an_adoption_names_the_new_parent(spans):
    sessions.opened(CONDUCTOR, _Source("acp-c", CONDUCTOR), T0)
    sessions.opened(CHAT, _Source("acp-1"), T1)
    sessions.adopted(CHAT, CONDUCTOR)
    adopt = [s for s in _lifecycle(spans) if s.name == "kirocrew.session.adopt"][0]
    assert adopt.attributes["kirocrew.parent_session_key_hash"] == (
        correlation.keyed_hash(KEY, CONDUCTOR)
    )
    assert adopt.links[0].context.trace_id == ids_int(
        ids.trace_id(NS, "session", CONDUCTOR, sessions.generation_of(T0))
    )


def ids_int(hex_id):
    return int(hex_id, 16)


def test_a_failed_or_raising_compaction_is_an_error(spans):
    from opentelemetry.trace import StatusCode

    sessions.opened(CHAT, _Source("acp-1"), T0)
    with sessions.compaction(CHAT) as traced:
        traced.outcome = "failed"
    with pytest.raises(asyncio.CancelledError):
        with sessions.compaction(CHAT):
            raise asyncio.CancelledError
    with sessions.compaction(CHAT) as traced:
        traced.outcome = "a value nobody listed"
    failed, cancelled, other = [
        s for s in _lifecycle(spans) if s.name == "kirocrew.session.compaction"
    ]
    assert failed.attributes["kirocrew.session.compaction_outcome"] == "failed"
    assert cancelled.attributes["kirocrew.session.compaction_outcome"] == "cancelled"
    assert other.attributes["kirocrew.session.compaction_outcome"] == "other"
    assert failed.status.status_code == cancelled.status.status_code == StatusCode.ERROR
    assert other.status.status_code == StatusCode.UNSET


def test_an_unknown_switch_kind_is_dropped(spans):
    sessions.switched(CHAT, "something a caller made up")
    assert _lifecycle(spans) == []


def test_no_span_carries_a_session_key_or_text_outside_the_local_key(spans):
    secret = "dashboard:TOP-SECRET-PROJECT-TITLE"
    sessions.note_lineage(CHAT, forked_from=secret)
    sessions.opened(secret, _Source("acp-s", secret), T0)
    sessions.opened(CHAT, _Source("acp-1"), T1)
    with sessions.compaction(CHAT) as traced:
        traced.outcome = "ok"
    sessions.switched(CHAT, "agent")
    sessions.adopted(CHAT, secret)
    sessions.closed(CHAT, "removed", T1)
    names = {s.name for s in _lifecycle(spans)}
    assert names <= {
        sessions.SPAN_PREFIX + e for e in ("open", "compaction", "switch", "adopt", "close")
    }
    for span in _lifecycle(spans):
        for key, value in span.attributes.items():
            if key != correlation.LOCAL_PREFIX + "session_key":
                assert "SECRET" not in str(value), key


def test_the_metrics_session_records_drive_open_and_close(spans, monkeypatch):
    monkeypatch.setattr(metrics_sessions, "_crumbs_enabled", lambda: False)
    # The module's own clock binding, pinned to distinct readings: two starts in
    # one wall-clock tick would otherwise share a generation.
    clock = itertools.count(T0, 10.0)
    monkeypatch.setattr(metrics_sessions, "time", SimpleNamespace(time=lambda: next(clock)))

    async def lifetime():
        await metrics_sessions.record_session_started(CHAT, _Source("acp-1"))
        await metrics_sessions.record_sessions_ended([CHAT], end_reason="shutdown")
        await metrics_sessions.record_session_started(CHAT, _Source("acp-2"))
        await metrics_sessions.record_session_ended(CHAT, end_reason="reset")

    asyncio.run(lifetime())
    names = [s.name for s in _lifecycle(spans)]
    assert names == [
        "kirocrew.session.open",
        "kirocrew.session.close",
        "kirocrew.session.open",
        "kirocrew.session.reset",
    ]
    assert len({s.context.trace_id for s in _lifecycle(spans)}) == 2


class _State:
    """The one ``DashboardState`` call ``slot_session_key`` makes."""

    def __init__(self, *slots):
        self._slots = {s.key: s for s in slots}

    def get_slot(self, key):
        return self._slots.get(key)


def test_bare_slot_keys_resolve_to_the_registry_keys_sessions_are_traced_under(spans):
    from kiro_crew.dashboard.chat_utils_slots import _history_key_for
    from kiro_crew.dashboard.trace_binding import slot_session_key

    conductor = SimpleNamespace(key="chat-conductor-1", linked_session_key="")
    linked = SimpleNamespace(key="chat-linked-1", linked_session_key="slack:C1-1700000000.000100")
    state = _State(conductor, linked)
    conductor_key = slot_session_key(state, "chat-conductor-1")
    assert conductor_key == "dashboard:chat-conductor-1"
    assert slot_session_key(state, "chat-gone-1") == "dashboard:chat-gone-1"
    assert slot_session_key(state, "") == ""
    # An ordinary tab: its registry key is its own dashboard key, so no parent.
    sessions.note_lineage("dashboard:chat-tab-1", slot_key=_history_key_for("chat-tab-1"))
    sessions.opened("dashboard:chat-tab-1", _Source("acp-t", "dashboard:chat-tab-1"), T0)
    # A worker stamped with its creator's BARE slot key still names that session.
    sessions.opened(conductor_key, _Source("acp-c", conductor_key), T0)
    sessions.note_lineage(
        "dashboard:chat-worker-1",
        creator=slot_session_key(state, "chat-conductor-1"),
        slot_key=_history_key_for("chat-worker-1"),
    )
    sessions.opened("dashboard:chat-worker-1", _Source("acp-w", "dashboard:chat-worker-1"), T1)
    # The channel-linked slot's session names its dashboard twin.
    channel = slot_session_key(state, "chat-linked-1")
    sessions.note_lineage(channel, slot_key=_history_key_for("chat-linked-1"))
    sessions.opened(channel, _Source("acp-l", channel), T1)
    tab, conductor_open, worker, linked_open = _lifecycle(spans)
    assert "kirocrew.session.relation" not in tab.attributes
    assert worker.attributes["kirocrew.parent_session_key_hash"] == (
        conductor_open.attributes["kirocrew.session_key_hash"]
    )
    assert worker.links[0].context.span_id == conductor_open.context.span_id
    assert linked_open.attributes["kirocrew.session.relation"] == "linked"
    assert linked_open.attributes["kirocrew.parent_session_key_hash"] == (
        correlation.keyed_hash(KEY, "dashboard:chat-linked-1")
    )


def test_the_compaction_funnel_records_the_coordinators_answer(spans):
    async def compact(key, pct):
        return "recycled"

    manager = SimpleNamespace(_compaction=SimpleNamespace(_compact_session=compact))
    assert asyncio.run(turn_context._compact_session(manager, CHAT, 91.0)) == "recycled"
    (span,) = _lifecycle(spans)
    assert span.name == "kirocrew.session.compaction"
    assert span.attributes["kirocrew.session.compaction_outcome"] == "recycled"


def test_a_switch_is_recorded_once_its_reset_lands(spans, monkeypatch):
    from kiro_crew.dashboard import chat_handlers as ch

    verdicts = iter([False, True])

    async def reset(state, slot, session_key, *, skip_if_busy):
        return next(verdicts)

    monkeypatch.setattr(ch, "_reset_slot_session", reset)
    state = SimpleNamespace(sessions=SimpleNamespace(get_provider=lambda key: None))
    slot = SimpleNamespace(key=CHAT)

    async def two_attempts():
        declined = await ch._reset_slot_session_or_warn(state, slot, CHAT, switch_kind="conductor")
        landed = await ch._reset_slot_session_or_warn(state, slot, CHAT, switch_kind="conductor")
        return declined, landed

    assert asyncio.run(two_attempts()) == (False, True)
    (span,) = _lifecycle(spans)
    assert span.attributes["kirocrew.session.switch_kind"] == "conductor"


def test_the_eager_prefetch_notes_the_workers_parent_before_it_registers(spans, monkeypatch):
    """The prefetch is the allocation that can register a worker first, so its open
    span must already see the creator the first turn would have noted."""
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_runner import SpeculativeResumeRefused
    from kiro_crew.dashboard.state import CrewLogPrevious

    conductor = SimpleNamespace(key="chat-conductor-1", linked_session_key="")
    worker = SimpleNamespace(
        key="chat-worker-1",
        linked_session_key="",
        _lineage_minted=True,
        _created_by="chat-conductor-1",
        forked_from="",
        model="",
        agent="",
        project="",
        reasoning_effort="",
        latch_crew_log_previous=lambda *a, **k: None,
    )
    state = _State(conductor, worker)
    worker_key = "dashboard:chat-worker-1"
    seen = []

    async def get_or_create(key, **kwargs):
        # What the registration's own open span would read.
        seen.append(sessions._lineage.get(key))
        raise SpeculativeResumeRefused("left to the first turn")

    async def no_predecessor(*args):
        return CrewLogPrevious(sid="", undecided=None)

    monkeypatch.setattr(chat_runner, "_slot_predecessor_store", no_predecessor)
    monkeypatch.setattr(chat_runner, "_app_allocation_kwargs", lambda slot: {})
    monkeypatch.setattr(chat_runner, "_slot_backend_allocation_kwargs", lambda slot: {})
    asyncio.run(
        chat_runner._spawn_admitted_prefetch(
            state,
            worker,
            SimpleNamespace(get_or_create=get_or_create),
            worker_key,
            0.0,
            kiro_agent=None,
            crew_alias="",
            agent_model="",
            allow_resume=False,
            _bound=(),
            start_priority=None,
        )
    )
    assert seen == [("dashboard:chat-conductor-1", sessions.RELATION_WORKER)]


def test_a_switch_names_the_session_its_reset_tore_down(spans, monkeypatch):
    """A successor that registers during the reset's teardown awaits keeps the switch
    span out of its trace: the span lands in the generation the reset popped."""
    from kiro_crew.dashboard import chat_handlers as ch

    sessions.opened(CHAT, _Source("acp-old"), T0)

    async def reset(state, slot, session_key, *, skip_if_busy):
        sessions.closed(CHAT, "reset", T0)
        sessions.opened(CHAT, _Source("acp-new"), T1)  # a prefetch, mid-teardown
        return True

    monkeypatch.setattr(ch, "_reset_slot_session", reset)
    state = SimpleNamespace(sessions=SimpleNamespace(get_provider=lambda key: None))
    assert asyncio.run(
        ch._reset_slot_session_or_warn(state, SimpleNamespace(key=CHAT), CHAT, switch_kind="model")
    )
    (switch,) = [s for s in _lifecycle(spans) if s.name == "kirocrew.session.switch"]
    assert _hex(switch.context.trace_id, 32) == (
        ids.trace_id(NS, "session", CHAT, sessions.generation_of(T0))
    )
    assert switch.attributes["kirocrew.acp_session_id"] == "acp-old"


def test_an_oversized_acp_session_id_is_not_retained(spans):
    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

    sessions.opened(CHAT, _Source("x" * (MAX_ACP_SESSION_ID_LEN + 1)), T0)
    assert sessions._generations[CHAT].acp_session_id == ""
    (span,) = _lifecycle(spans)
    assert "kirocrew.acp_session_id" not in span.attributes


class _DeferredSlot:
    """The fields ``apply_deferred_model_switch`` reads on a dashboard slot."""

    def __init__(self, session_key):
        self.key = session_key.split(":", 1)[1]
        self.linked_session_key = ""
        self.model = "auto"
        self._deferred_model_switch_key = session_key
        self._model_pick_lock = asyncio.Lock()


class _DeferredState:
    def __init__(self):
        self.sessions = SimpleNamespace(get_provider=lambda key: object())

    def push_slots_update(self):
        pass


def _wire_deferred(monkeypatch, model_switch, live):
    from contextlib import asynccontextmanager

    from kiro_crew.dashboard import chat_handlers as ch

    @asynccontextmanager
    async def no_lock(key):
        yield

    monkeypatch.setattr(model_switch, "_another_alias_is_mid_turn", lambda state, slot: False)
    monkeypatch.setattr(model_switch, "slot_switch_session_lock", no_lock)
    monkeypatch.setattr(ch, "_try_live_model_switch", live)
    monkeypatch.setattr(ch, "_sync_served_model", lambda slot, provider: None)
    monkeypatch.setattr(ch, "_broadcast_context_reset", lambda state, key, provider: None)


def test_the_live_model_switch_records_no_span_itself():
    """Its callers record it after their rebind guards: a rebind during its RPCs
    must not put a switch on the session the slot was rebound to."""
    import inspect

    from kiro_crew.dashboard.chat_api import slot_model

    assert "trace_sessions" not in inspect.getsource(slot_model._try_live_model_switch)


def test_a_deferred_live_switch_rebound_mid_rpc_records_no_switch(spans, monkeypatch):
    """The deferred switch's rebind guard: the slot's binding moves while set_model
    awaits, so no switch span lands on either session."""
    from kiro_crew.dashboard.session_control_parts import model_switch

    sessions.opened(CHAT, _Source("acp-old"), T0)
    slot = _DeferredSlot(CHAT)

    async def live(name, slot_, provider, model):
        slot_.linked_session_key = "cron:job-1"  # a cron result binds the tab mid-RPC
        return True

    _wire_deferred(monkeypatch, model_switch, live)
    asyncio.run(model_switch.apply_deferred_model_switch(_DeferredState(), slot))
    assert [s for s in _lifecycle(spans) if s.name == "kirocrew.session.switch"] == []


def test_a_deferred_live_switch_records_the_switch_on_its_session(spans, monkeypatch):
    from kiro_crew.dashboard.session_control_parts import model_switch

    sessions.opened(CHAT, _Source("acp-old"), T0)
    slot = _DeferredSlot(CHAT)

    async def live(name, slot_, provider, model):
        return True

    _wire_deferred(monkeypatch, model_switch, live)
    asyncio.run(model_switch.apply_deferred_model_switch(_DeferredState(), slot))
    (switch,) = [s for s in _lifecycle(spans) if s.name == "kirocrew.session.switch"]
    assert switch.attributes["kirocrew.session.switch_kind"] == "model"
    assert switch.attributes["kirocrew.acp_session_id"] == "acp-old"


def test_a_reset_switch_uses_the_key_the_registry_folds_onto(spans, monkeypatch):
    from kiro_crew.dashboard import chat_handlers as ch

    sessions.opened("slack:1700000000.000100", _Source("acp-s"), T0)

    async def reset(state, slot, session_key, *, skip_if_busy):
        return True

    monkeypatch.setattr(ch, "_reset_slot_session", reset)
    folded = {"1700000000.000100": "slack:1700000000.000100"}
    state = SimpleNamespace(
        sessions=SimpleNamespace(
            get_provider=lambda key: None, _fold_key=lambda k: folded.get(k, k)
        )
    )
    slot = SimpleNamespace(key="chat-1")
    assert asyncio.run(
        ch._reset_slot_session_or_warn(state, slot, "1700000000.000100", switch_kind="model")
    )
    (switch,) = [s for s in _lifecycle(spans) if s.name == "kirocrew.session.switch"]
    assert switch.attributes["kirocrew.acp_session_id"] == "acp-s"


def test_a_live_switch_under_an_alias_key_lands_in_the_registered_session(spans, monkeypatch):
    """The slot names its session by an alias the registry folds (a bare channel ts);
    the switch span still lands in that session's own lifecycle trace."""
    from kiro_crew.dashboard.session_control_parts import model_switch

    sessions.opened("slack:1700000000.000100", _Source("acp-s"), T0)
    slot = _DeferredSlot("dashboard:chat-1")
    slot.linked_session_key = "1700000000.000100"
    slot._deferred_model_switch_key = "1700000000.000100"
    monkeypatch.setattr(model_switch, "effective_session_key", lambda s: s.linked_session_key)
    state = _DeferredState()
    state.sessions._fold_key = lambda k: "slack:" + k if not k.startswith("slack:") else k

    async def live(name, slot_, provider, model):
        return True

    _wire_deferred(monkeypatch, model_switch, live)
    asyncio.run(model_switch.apply_deferred_model_switch(state, slot))
    (switch,) = [s for s in _lifecycle(spans) if s.name == "kirocrew.session.switch"]
    assert switch.attributes["kirocrew.acp_session_id"] == "acp-s"
