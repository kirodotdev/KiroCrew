"""The recovery-replay ledger, tested at its interface: ``slot.replays``.

Every family the runner queues for itself is armed with the entry it queued and the
Stop counts and binding it was queued under; the drain claims it for a dispatch
only when it drained alone; either seam revokes it on a Stop, a rebind or newer
user input. One row per family, then the two seams through a real slot.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND, effective_session_key
from kiro_crew.dashboard.recovery_replays import (
    ENTRY_FAMILIES,
    IMAGE_RECOVERY_CANCELLED_TEXT,
    POLICIES,
    SESSION_NOT_FOUND_CANCELLED_TEXT,
    LiveSlot,
    RecoveryReplays,
    ReplayFamily,
    ReplayRevocation,
    cancel_notice,
    consumed_in,
    log_args,
    replays_of,
)
from kiro_crew.dashboard.state import _ChatSlot

_MA = ReplayFamily.MODEL_ACCESS
_SNF = ReplayFamily.SESSION_NOT_FOUND
_IMAGE = ReplayFamily.IMAGE_HISTORY
_CF = ReplayFamily.CONTENT_FILTER
_CONT = ReplayFamily.CONTINUATION

_KEY = "dashboard:s1"

#: Every family whose record names one queue entry, read off the enum rather than
#: the registry, so a member the registry misses is still exercised.
_ENTRY_MEMBERS = [family for family in ReplayFamily if family is not ReplayFamily.CONTINUATION]


class _StopCounts:
    """The session manager's Stop counts, as a dict: the test adapter of the reader port."""

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.reads: list[str] = []

    def __call__(self, key: str) -> int:
        self.reads.append(key)
        return self.counts.get(key, 0)


def _live(
    *,
    key: str = _KEY,
    stop_generation: int = 2,
    stopping: bool = False,
    user_input: bool = False,
    counts: dict[str, int] | None = None,
) -> LiveSlot:
    return LiveSlot(
        session_key=key,
        stop_generation=stop_generation,
        stopping=stopping,
        user_input=user_input,
        session_stop_generation=_StopCounts({_KEY: 3} if counts is None else counts),
    )


def _armed(family: ReplayFamily, *, entry_id: str = "q1", key: str = _KEY) -> RecoveryReplays:
    replays = RecoveryReplays()
    replays.arm(family, entry_id=entry_id, session_key=key, stop_gen=2, session_stop_gen=3)
    return replays


# ---------------------------------------------------------------------- the registry


def test_every_family_is_registered_once_in_the_policy_table():
    assert set(POLICIES) == set(ReplayFamily)
    assert set(ENTRY_FAMILIES) == set(_ENTRY_MEMBERS)
    assert [family for family, policy in POLICIES.items() if not policy.keyed_by_entry] == [_CONT]


def test_the_registry_order_is_the_drain_order():
    """The drain re-checks families in registry order; the order is the contract."""
    assert ENTRY_FAMILIES == (_MA, _SNF, _IMAGE, _CF)


def test_each_entry_family_is_checked_in_exactly_one_consume_phase():
    before = consumed_in("before_allowances")
    after = consumed_in("after_allowances")

    assert before == (_SNF, _IMAGE)
    assert after == (_MA, _CF)
    assert sorted(f.value for f in before + after) == sorted(f.value for f in _ENTRY_MEMBERS)
    assert POLICIES[_CONT].consume_phase is None


def test_every_refund_names_a_real_slot_allowance():
    """A cancellation re-arms its one-shots by attribute name, so a renamed allowance
    would surface only when a replay is cancelled; the names are checked here."""
    named = {
        attr
        for policy in POLICIES.values()
        for refunds in (policy.drain_refunds, policy.consume_refunds, policy.sweep_refunds)
        for attr, _value in refunds
    }

    assert named
    assert named <= set(_ChatSlot.__slots__)


# ---------------------------------------------------------------------- the ledger


@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_an_armed_replay_is_claimed_only_when_its_entry_drained_alone(family):
    replays = _armed(family)

    assert replays.armed(family)
    assert replays.entry_id(family) == "q1"
    assert replays.claim([{"id": "q1"}]) == frozenset({family})
    # A merge folding user input into the dispatch is a correction, not the retry.
    assert replays.claim([{"id": "q1"}, {"id": "q2"}]) == frozenset()
    assert replays.claim([{"id": "q2"}]) == frozenset()
    assert replays.claim([]) == frozenset()
    # Claiming reads the record; only a seam consumes it.
    assert replays.armed(family)


def test_one_entry_can_carry_two_families():
    """A content-filter retry re-queued verbatim by another recovery keeps its identity."""
    replays = _armed(_CF)
    replays.arm(_MA, entry_id="q1", session_key=_KEY, stop_gen=2, session_stop_gen=3)

    assert replays.claim([{"id": "q1"}]) == frozenset({_CF, _MA})


@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_arming_with_no_entry_leaves_the_family_unarmed(family):
    replays = _armed(family)
    replays.arm(family, entry_id="", session_key=_KEY, stop_gen=2, session_stop_gen=3)

    assert not replays.armed(family)
    assert replays.entry_id(family) == ""
    assert replays.claim([{"id": ""}]) == frozenset()


@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_disarm_forgets_the_record(family):
    replays = _armed(family)
    replays.disarm(family)

    assert not replays.armed(family)
    assert replays.revalidate(family, _live(stop_generation=99)) == ReplayRevocation()


_TRIGGERS = [
    # (id, live view, expected revocation)
    ("nothing-moved", {}, ReplayRevocation()),
    ("slot-stop", {"stop_generation": 3}, ReplayRevocation(stop_moved=True)),
    ("session-stop", {"counts": {_KEY: 4}}, ReplayRevocation(stop_moved=True)),
    ("stop-in-flight", {"stopping": True}, ReplayRevocation(stopping=True)),
    ("user-input", {"user_input": True}, ReplayRevocation(superseded=True)),
]


@pytest.mark.parametrize("family", [*ENTRY_FAMILIES, _CONT])
@pytest.mark.parametrize(("case", "view", "expected"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_revalidate_names_what_happened_since_the_replay_was_queued(family, case, view, expected):
    replays = _armed(family, entry_id="" if family is _CONT else "q1")

    revocation = replays.revalidate(family, _live(**view))

    assert revocation == expected
    assert revocation.revoked is (case != "nothing-moved")


@pytest.mark.parametrize("family", [*ENTRY_FAMILIES, _CONT])
def test_a_rebind_moves_the_replay_and_an_empty_binding_never_does(family):
    entry = "" if family is _CONT else "q1"
    rebound = _armed(family, entry_id=entry).revalidate(
        family, _live(key="slack:elsewhere", counts={_KEY: 3, "slack:elsewhere": 3})
    )
    unbound = _armed(family, entry_id=entry, key="").revalidate(
        family, _live(key="slack:elsewhere", counts={"slack:elsewhere": 3})
    )

    assert rebound == ReplayRevocation(rebound=True)
    assert unbound == ReplayRevocation()


def test_an_entry_family_reads_the_recorded_sessions_stop_count():
    """A Stop on the session the replay was queued for revokes it after a rebind too."""
    counts = {_KEY: 4, "slack:elsewhere": 3}
    for family in ENTRY_FAMILIES:
        view = _live(key="slack:elsewhere", counts=counts)
        assert _armed(family).revalidate(family, view).stop_moved
        assert view.session_stop_generation.reads == [_KEY]


def test_the_continuation_reads_the_live_sessions_stop_count():
    counts = {_KEY: 4, "slack:elsewhere": 3}
    view = _live(key="slack:elsewhere", counts=counts)

    revocation = _armed(_CONT, entry_id="").revalidate(_CONT, view)

    assert revocation == ReplayRevocation(rebound=True)
    assert view.session_stop_generation.reads == ["slack:elsewhere"]


def test_a_view_reads_each_sessions_count_once():
    view = _live(counts={_KEY: 3})
    replays = _armed(_MA)
    replays.arm(_SNF, entry_id="q2", session_key=_KEY, stop_gen=2, session_stop_gen=3)

    replays.revalidate(_MA, view)
    replays.revalidate(_SNF, view)

    assert view.stop_count_for(_KEY) == 3
    assert view.session_stop_generation.reads == [_KEY]


@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_an_unarmed_family_reads_as_not_revoked(family):
    view = _live(stop_generation=9, stopping=True, user_input=True, key="slack:elsewhere")

    assert RecoveryReplays().revalidate(family, view) == ReplayRevocation()
    assert view.session_stop_generation.reads == []


def test_the_continuation_record_starts_at_zero_counts_and_disarm_resets_it():
    replays = RecoveryReplays()
    fresh = _live(stop_generation=0, counts={})

    assert replays.armed(_CONT)
    assert not replays.revalidate(_CONT, fresh).revoked
    assert replays.revalidate(_CONT, _live(stop_generation=1, counts={})).stop_moved

    replays.arm(_CONT, entry_id="", session_key=_KEY, stop_gen=5, session_stop_gen=1)
    assert replays.revalidate(_CONT, fresh).revoked
    replays.disarm(_CONT)
    assert not replays.revalidate(_CONT, fresh).revoked


def test_sweep_forgets_only_records_whose_entry_left_the_queue():
    replays = _armed(_MA, entry_id="gone")
    replays.arm(_SNF, entry_id="here", session_key=_KEY, stop_gen=2, session_stop_gen=3)
    replays.arm(_IMAGE, entry_id="gone-too", session_key=_KEY, stop_gen=2, session_stop_gen=3)

    swept = replays.sweep(iter(["here", "other"]), (_MA, _SNF))

    assert swept == [(_MA, "gone")]
    assert not replays.armed(_MA)
    assert replays.entry_id(_SNF) == "here"
    # Outside the families asked about, nothing is touched.
    assert replays.entry_id(_IMAGE) == "gone-too"
    assert replays.sweep([], (_IMAGE,)) == [(_IMAGE, "gone-too")]


_NOTICES = [
    (
        _MA,
        ReplayRevocation(rebound=True),
        "ℹ️ Model-fallback retry cancelled — this chat moved to another session.",
    ),
    (
        _MA,
        ReplayRevocation(rebound=True, stop_moved=True),
        "ℹ️ Model-fallback retry cancelled — the turn was stopped.",
    ),
    (
        _CF,
        ReplayRevocation(superseded=True, stopping=True),
        "ℹ️ Content-filter retry cancelled — your newer message runs instead.",
    ),
    (
        _CF,
        ReplayRevocation(stop_moved=True),
        "ℹ️ Content-filter retry cancelled — the turn was stopped.",
    ),
    (_SNF, ReplayRevocation(rebound=True), SESSION_NOT_FOUND_CANCELLED_TEXT),
    (_IMAGE, ReplayRevocation(superseded=True), IMAGE_RECOVERY_CANCELLED_TEXT),
    (
        _CONT,
        ReplayRevocation(superseded=True, rebound=True),
        "ℹ️ Auto-continue cancelled — your message takes over.",
    ),
    (
        _CONT,
        ReplayRevocation(rebound=True, stop_moved=True),
        "ℹ️ Auto-continue cancelled — this chat moved to another session, nothing was run.",
    ),
    (
        _CONT,
        ReplayRevocation(stopping=True),
        "ℹ️ Auto-continue cancelled — the turn was stopped, nothing was run.",
    ),
]


@pytest.mark.parametrize(("family", "revocation", "notice"), _NOTICES)
def test_each_family_explains_a_cancellation_in_its_own_words(family, revocation, notice):
    assert cancel_notice(family, revocation) == notice


def test_the_lost_session_log_names_each_reason():
    revocation = ReplayRevocation(stop_moved=True, superseded=False, rebound=True)

    assert log_args(_SNF, revocation) == (True, False, True)
    assert log_args(_MA, revocation) == (revocation,)
    assert repr(revocation) == "ReplayRevocation(rebound=True, stopped=True, superseded=False)"


def test_replays_of_attaches_a_ledger_to_a_slot_without_one():
    double = SimpleNamespace()
    partial = _ChatSlot.__new__(_ChatSlot)

    assert replays_of(double) is replays_of(double)
    assert isinstance(double.replays, RecoveryReplays)
    assert replays_of(partial) is partial.replays


def test_replays_of_refuses_a_slot_that_cannot_keep_its_ledger():
    """A ledger the slot would not keep would drop every record armed on it, and
    with them the Stop and rebind checks: fail closed instead."""

    class _Sealed:
        __slots__ = ()

    with pytest.raises(AttributeError):
        replays_of(_Sealed())


# ---------------------------------------------------------------------- the two seams


def _state(tmp_path, monkeypatch, counts: dict[str, int] | None = None):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    counts = {} if counts is None else counts
    state.sessions.stop_generation = lambda key: counts.get(key, 0)
    return state


#: The one-shot each family's drain-side cancellation re-arms (None: it stays spent).
_DRAIN_REFUNDS = {
    _MA: "_model_access_fallback_used",
    _SNF: "_session_not_found_retry_used",
    _IMAGE: "_poisoned_reset_used",
    _CF: None,
}
#: What a turn vetoed at its consume seam leaves re-armed. The lost-session and
#: image checks run first and refund their own one-shot; the model-access swap's
#: stays spent. The content-filter check runs after the turn-start refresh, which
#: re-arms the model-access one-shot for any turn that is not that swap's replay.
_REFUNDED_AFTER_A_CONSUME_VETO = {
    _MA: set(),
    _SNF: {"_session_not_found_retry_used"},
    _IMAGE: {"_poisoned_reset_used"},
    _CF: {"_model_access_fallback_used"},
}
_ONE_SHOTS = (
    "_model_access_fallback_used",
    "_session_not_found_retry_used",
    "_poisoned_reset_used",
)


def _spend_one_shots(slot) -> None:
    for attr in _ONE_SHOTS:
        setattr(slot, attr, True)
    slot._refusal_fallback_attempted = True


def _refunded(slot) -> set[str]:
    return {attr for attr in _ONE_SHOTS if getattr(slot, attr) is False}


@pytest.mark.asyncio
@pytest.mark.parametrize("family", _ENTRY_MEMBERS)
async def test_the_drain_drops_a_stopped_replay_and_refunds_its_family(
    tmp_path, monkeypatch, family
):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    _spend_one_shots(slot)
    qid = slot.queue_insert(0, "the user's words", kind=SYNTHETIC_RECOVERY_KIND)
    slot.replays.arm(
        family,
        entry_id=qid,
        session_key=effective_session_key(slot),
        stop_gen=slot._stop_generation,
        session_stop_gen=0,
    )
    slot._stop_generation += 1
    dispatched = MagicMock()
    monkeypatch.setattr(cr, "_run_chat", dispatched)

    assert await cr._start_next_queued_turn(state, slot) is False

    dispatched.assert_not_called()
    assert slot._queue == []
    assert not slot.replays.armed(family)
    assert [m["content"] for m in slot.messages if m.get("role") == "notice"] == [
        cancel_notice(family, ReplayRevocation(stop_moved=True))
    ]
    expected = {_DRAIN_REFUNDS[family]} - {None}
    assert _refunded(slot) == expected
    assert slot._refusal_fallback_attempted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("family", _ENTRY_MEMBERS)
async def test_the_drain_claims_a_replay_that_may_run(tmp_path, monkeypatch, family):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    qid = slot.queue_insert(0, "the user's words", kind=SYNTHETIC_RECOVERY_KIND)
    slot.replays.arm(
        family,
        entry_id=qid,
        session_key=effective_session_key(slot),
        stop_gen=slot._stop_generation,
        session_stop_gen=0,
    )
    seen: list[dict] = []

    async def _record(_state, _slot, _message, **kwargs) -> None:
        seen.append(kwargs)

    monkeypatch.setattr(cr, "_run_chat", _record)

    assert await cr._start_next_queued_turn(state, slot) is True
    await slot.task

    assert seen[0]["_replay"] == frozenset({family})


@pytest.mark.asyncio
async def test_the_drain_head_forgets_a_swept_model_access_replay(tmp_path, monkeypatch):
    """The model-access record is forgotten, and its one-shot refunded, before the
    drain reads the queue: an empty queue returns before any family's own step,
    where the other records are forgotten."""
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    _spend_one_shots(slot)
    for family in ENTRY_FAMILIES:
        slot.replays.arm(
            family, entry_id=f"gone-{family.value}", session_key="", stop_gen=0, session_stop_gen=0
        )

    assert await cr._start_next_queued_turn(state, slot) is False

    assert [f for f in ENTRY_FAMILIES if slot.replays.armed(f)] == [_SNF, _IMAGE, _CF]
    assert _refunded(slot) == {"_model_access_fallback_used"}
    assert not any(m.get("role") == "notice" for m in slot.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", (_SNF, _IMAGE, _CF))
async def test_a_family_step_forgets_a_swept_replay_without_a_refund(tmp_path, monkeypatch, family):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    _spend_one_shots(slot)
    slot.queue_insert(0, "[cron]", kind="cron_notification")
    slot.replays.arm(family, entry_id="gone", session_key="", stop_gen=0, session_stop_gen=0)
    slot._stop_generation += 1

    async def _turn(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(cr, "_run_chat", _turn)

    await cr._start_next_queued_turn(state, slot)
    if slot.task is not None:
        await slot.task

    assert not slot.replays.armed(family)
    assert _refunded(slot) == set()
    assert not any(m.get("role") == "notice" for m in slot.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", _ENTRY_MEMBERS)
async def test_the_consume_seam_vetoes_a_stopped_replay_before_the_provider(
    tmp_path, monkeypatch, family
):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    _spend_one_shots(slot)
    slot.replays.arm(
        family,
        entry_id="q-dispatched",
        session_key=effective_session_key(slot),
        stop_gen=slot._stop_generation,
        session_stop_gen=0,
    )
    slot._stop_generation += 1
    state.sessions.get_or_create = AsyncMock()
    monkeypatch.setattr(cr, "_start_next_queued_turn", AsyncMock(return_value=False))

    await cr._run_chat(state, slot, "the user's words", _replay=frozenset({family}))

    state.sessions.get_or_create.assert_not_awaited()
    assert not slot.replays.armed(family)
    assert [m["content"] for m in slot.messages if m.get("role") == "notice"] == [
        cancel_notice(family, ReplayRevocation(stop_moved=True))
    ]
    assert _refunded(slot) == _REFUNDED_AFTER_A_CONSUME_VETO[family]
    assert slot._refusal_fallback_attempted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("family", _ENTRY_MEMBERS)
async def test_an_accepted_replay_is_consumed_unless_its_turn_settles_it(
    tmp_path, monkeypatch, family
):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    slot.replays.arm(
        family,
        entry_id="q-dispatched",
        session_key=effective_session_key(slot),
        stop_gen=slot._stop_generation,
        session_stop_gen=0,
    )

    phase = POLICIES[family].consume_phase
    assert phase is not None
    vetoed = await cr._replay_vetoed_at_consume(state, slot, frozenset({family}), phase)

    assert vetoed is False
    assert slot.replays.armed(family) is (family is _CF)
    assert not any(m.get("role") == "notice" for m in slot.messages)


@pytest.mark.asyncio
async def test_each_consume_phase_checks_only_its_own_claimed_families(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    for family in (_SNF, _IMAGE, _MA):
        slot.replays.arm(family, entry_id="q", session_key="", stop_gen=0, session_stop_gen=0)
    slot._stop_generation += 1

    # A model-access replay is not checked before the allowance refresh.
    assert not await cr._replay_vetoed_at_consume(
        state, slot, frozenset({_MA}), "before_allowances"
    )
    assert slot.replays.armed(_MA)
    # In its phase, the first claimed family in registry order decides.
    assert await cr._replay_vetoed_at_consume(
        state, slot, frozenset({_IMAGE, _SNF}), "before_allowances"
    )
    assert not slot.replays.armed(_SNF)
    assert slot.replays.armed(_IMAGE)
