"""``session/opened.previous`` names the store the slot is really on.

Two regressions live here, and the second is why the first one's fix moved.

The first: the predecessor was read inside the first real turn, straight from
``mapped_sid``. Two sites allocate a session for one slot -- the eager prefetch
and that first turn -- and the prefetch runs FIRST and maps its own session over
the slot's key, so the turn's read answered the successor it had just been
handed, the emitter found the two ids equal and wrote no ``previous`` edge at
all. The predecessor is latched on the slot at whichever allocation observes it
FIRST and handed to exactly one ``session/opened``.

The second: what the latch is fed when the slot has no record of its own. That was
the slot-to-session mapping alone, and a replay-pending allocation keeps the PRIOR
resumable id there on purpose -- so a gateway that restarts inside that window cites
a generation back and the crew log between two citations is cited by nobody. The
store now answers it: the units under the slot's key, ordered by the succession edges
they recorded. The test that matters for that is the one crossing a real process
boundary, because an in-memory record passes every test that stays inside one
interpreter.

Three sources, in order, each covering what the next cannot: the slot's own record of
the store it opened (the only one that can name a store still queued to the writer
thread), the store (the only one that survives the process), the mapping (the only one
that answers for a slot with no unit at all).
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import textwrap
import threading
import unittest.mock
from pathlib import Path

import pytest
from test_chat_runner_coverage import _drive, _runner_state, _slot
from test_chat_send_agent_model_default import _config, _pin_sync_accessors, _turn_state

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import crew_log_path, emit, session_tree, store
from kiro_crew.crew_log.session_tree import (
    EDGE_LEGACY,
    EDGE_NAMED,
    EDGE_NONE,
    EDGE_UNREAD,
    HAND_OVER,
    NO_LOG,
    UNDECIDED,
    OpenedRecord,
    SlotHead,
    fold_slot_head,
    slot_chain_head,
)
from kiro_crew.crew_log.store import crew_log_dir
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn, _slot_predecessor_store
from kiro_crew.dashboard.state import CrewLogPrevious, _ChatSlot

PREDECESSOR = "sid-the-slot-was-writing"
PREWARMED = "sid-the-prefetch-allocated"
#: The store opened AFTER ``PREDECESSOR`` on the same slot, whose id the mapping
#: never received because the allocation that produced it deferred promoting it.
NEWEST = "sid-the-mapping-never-received"
#: The store a third allocation opens, which must cite ``NEWEST``.
SUCCESSOR = "sid-the-third-allocation-opened"

SLOT = "chat-previous-latch"


def _header_of(sid: str) -> dict:
    """*sid*'s header line as the emitter left it."""
    line = crew_log_path("session", sid).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def _drop_announce(sid: str, *, later_than: str) -> None:
    """Leave *sid* with its header alone, stamped after *later_than*'s.

    The state a create passes through: :meth:`CrewLog.create` publishes the header and
    the announce is appended second, so a reader between the two sees a unit that
    proves its slot and cites nothing yet. The stamp is pushed past *later_than*
    because the edges cannot order two logs that both cite nothing, and a fixture
    leaving them inside one millisecond would be exercising the sid tie-break instead.
    """
    header = _header_of(sid)
    header["createdAt"] = _header_of(later_than)["createdAt"] + 1_000
    path = crew_log_path("session", sid)
    path.write_text(json.dumps(header, separators=(",", ":")) + "\n", encoding="utf-8")


def _strip_edge_keys(sid: str) -> None:
    """Rewrite *sid*'s announce with no predecessor key of any kind.

    What a gateway from before these keys existed wrote, byte for byte: the announce
    is there and readable, and it says nothing about a predecessor either way. Every
    store on disk today is made of these units, so a fixture that lets the current
    emitter state the absence is not testing the store the fix will actually meet.
    """
    path = crew_log_path("session", sid)
    lines = path.read_text(encoding="utf-8").splitlines()
    announce = json.loads(lines[1])
    for key in ("previous", "previous_none", "previous_undecided"):
        announce.get("data", {}).pop(key, None)
    lines[1] = json.dumps(announce, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def _runner_config():
    """Serve one real config object to every ``KiroCrewConfig.load()`` in the turn.

    Defined here rather than imported: an imported fixture is never referenced by
    name in this module, so it reads as an unused import that each test's
    parameter then shadows.
    """
    patchers: list[unittest.mock._patch] = []

    def _use(cfg: KiroCrewConfig) -> None:
        patcher = unittest.mock.patch.object(
            chat_runner.KiroCrewConfig, "load", unittest.mock.MagicMock(return_value=cfg)
        )
        patcher.start()
        patchers.append(patcher)

    yield _use
    for patcher in patchers:
        patcher.stop()


@pytest.fixture
def _store(tmp_path, monkeypatch):
    """A crew log store of this test's own, written by the REAL emitter.

    Returns a function that opens one store on a slot, citing the one before it --
    the same call the turn path makes, so the units on disk are the units a gateway
    writes rather than a fixture's idea of them.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()

    def _open(sid: str, *, previous: str = "", slot: str = SLOT, undecided: bool = False) -> None:
        emit.on_session_opened(
            sid,
            slot=slot,
            agent="default",
            memory="global",
            previous_sid=previous,
            previous_undecided=undecided,
        )
        assert emit.flush(10.0), "the writer did not settle, so the store is not the input"

    yield _open
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


class _Sessions:
    """The mapping boundary, answering one id however often it is asked.

    ``replay_pending`` is the one state where the mapping is knowingly NOT ordering:
    allocation leaves the prior resumable id there for a provider that defers
    promotion, so for that window it names a generation behind the store.
    """

    def __init__(self, sid: str, *, replay_pending: bool = False) -> None:
        self._sid = sid
        self._replay_pending = replay_pending
        self.reads = 0

    def mapped_sid(self, _key: str) -> str:
        self.reads += 1
        return self._sid

    def provider_switch_replay_pending(self, _key: str) -> bool:
        return self._replay_pending


def _capture_opened():
    """Intercept the emitter so a turn's ``previous_sid`` kwarg is readable."""
    return unittest.mock.patch.object(
        chat_runner.crew_log_emit, "on_session_opened", unittest.mock.MagicMock()
    )


class TestTheSlotLatch:
    """The latch keeps the FIRST observation and owes it to one entry."""

    def test_the_first_observation_is_the_one_kept(self):
        slot = _slot()

        slot.latch_crew_log_previous(PREDECESSOR)
        slot.latch_crew_log_previous(PREWARMED)

        # The second observation is the successor the first allocation just
        # published, never an earlier store, so overwriting would replace the
        # only id that can be cited with the one being written for.
        assert slot._crew_log_previous_sid == PREDECESSOR

    def test_nothing_observed_latches_nothing(self):
        slot = _slot()

        slot.latch_crew_log_previous("")

        # A slot's first session has no predecessor. "" must stay "" rather than
        # become a store with an empty name, which a chain walker would follow.
        assert slot._crew_log_previous_sid == ""
        assert slot.take_crew_log_previous(now_writing=PREWARMED).sid == ""

    def test_taking_the_edge_clears_it(self):
        slot = _slot()
        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot.take_crew_log_previous(now_writing=NEWEST).sid == PREDECESSOR
        # Left behind, it would make the slot's NEXT store cite this store's
        # predecessor and skip this store -- a gap a chain walker cannot see.
        assert slot.take_crew_log_previous(now_writing=NEWEST).sid == ""

    def test_an_undetermined_predecessor_is_carried_and_cleared_with_the_sid(self):
        """The reason the sid is empty travels WITH it, as one handover.

        Dropped, the announce cannot tell "this slot has no earlier store" from "it
        has one nobody could name", and it records the first -- which invites a later
        fold to pass over this log and elect the store before it.
        """
        slot = _slot()
        slot.latch_crew_log_previous("", undecided=True)

        taken = slot.take_crew_log_previous(now_writing=NEWEST)
        assert taken == CrewLogPrevious(sid="", undecided=True)
        # Spent with the sid, not left for the next store, which has its own answer --
        # and what it leaves behind is "nothing determined", not "determined there is
        # none", because no resolver has answered for the store this slot opens next.
        assert slot.take_crew_log_previous(now_writing=SUCCESSOR) == CrewLogPrevious(
            sid="", undecided=None
        )

    def test_a_named_predecessor_is_not_reported_undetermined(self):
        """The two are exclusive: a named edge already says the predecessor is known,
        so a caller passing both leaves the name winning rather than recording a
        break beside it."""
        slot = _slot()
        slot.latch_crew_log_previous(PREDECESSOR, undecided=True)

        assert slot.take_crew_log_previous(now_writing=NEWEST) == CrewLogPrevious(
            sid=PREDECESSOR, undecided=False
        )

    def test_an_earlier_refusal_does_not_ride_on_a_later_named_latch(self):
        """The flag describes the latch that WON, not one that lost.

        Two latches before a single entry is owed is ordinary: the eager prefetch runs
        its own resolver, and the turn runs one too. If the prefetch's store read
        refused and the turn's then named an id, the pair handed over must be that
        name alone. Carrying the earlier reason forward writes a named edge reported
        undetermined -- the one pairing the sibling above promises a reader never to
        see, and here it arrives from two calls rather than from one caller's mistake.
        """
        slot = _slot()
        slot.latch_crew_log_previous("", undecided=True)
        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot.take_crew_log_previous(now_writing=NEWEST) == CrewLogPrevious(
            sid=PREDECESSOR, undecided=False
        )

    def test_a_later_determination_supersedes_an_earlier_refusal(self):
        """Same two latches, and this time the second one names nothing either.

        A refusal carries no information about the store's CONTENT -- it says the read
        could not be made. A later read that succeeded and found no earlier store is
        strictly better information about the same slot, so it must win. Left standing,
        the earlier refusal has the entry record a chain BREAK -- "a predecessor exists
        and could not be determined" -- for a slot the gateway determined has none, and
        every later fold then refuses on that log instead of passing over it.
        """
        slot = _slot()
        slot.latch_crew_log_previous("", undecided=True)
        slot.latch_crew_log_previous("", undecided=False)

        assert slot.take_crew_log_previous(now_writing=NEWEST) == CrewLogPrevious(
            sid="", undecided=False
        ), "an earlier refusal outlived a later successful read of the same store"

    def test_a_later_handover_does_not_manufacture_a_determination(self):
        """The same rule in the direction that must NOT invent a finding.

        A later latch that determined nothing leaves nothing determined. It may erase an
        earlier refusal -- writing no key is the safe direction, since a fold hands such
        a log to its next source rather than passing over it -- but it must never come
        out as "this slot has no earlier store", which is a claim neither latch made.
        """
        slot = _slot()
        slot.latch_crew_log_previous("", undecided=True)
        slot.latch_crew_log_previous("", undecided=None)

        assert slot.take_crew_log_previous(now_writing=NEWEST) == CrewLogPrevious(
            sid="", undecided=None
        )


class TestTheEagerPrefetchLatchesFirst:
    @pytest.fixture(autouse=True)
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_the_prefetch_names_the_predecessor_before_mapping_its_own(
        self, tmp_path, _runner_config
    ):
        """The prefetch is the earlier allocation, so it is the one that can see A."""
        _runner_config(_config(tmp_path))
        state, _client = _runner_state(tmp_path)
        _pin_sync_accessors(_client)
        slot = _slot()
        state._slots[slot.key] = slot
        state.sessions.release = unittest.mock.MagicMock()
        state.sessions.remove_if_unclaimed = unittest.mock.AsyncMock(return_value=True)
        state.sessions.mapped_sid = unittest.mock.MagicMock(return_value=PREDECESSOR)

        await _eager_spawn(state, slot)

        assert slot._crew_log_previous_sid == PREDECESSOR


class TestTheTurnCitesThePredecessorNotThePrewarm:
    @pytest.mark.asyncio
    async def test_a_prewarmed_turn_cites_the_store_the_prefetch_replaced(
        self, tmp_path, _runner_config
    ):
        """The defect, directly: the mapping already names the pre-warmed session.

        Reading it here answers ``PREWARMED``, which is the session this entry is
        being written FOR -- the emitter drops a self-edge, so the predecessor
        goes uncited and unrepaired. The latch the prefetch wrote is what makes
        the turn answer ``PREDECESSOR`` instead.
        """
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        slot.latch_crew_log_previous(PREDECESSOR)
        state.sessions.mapped_sid = unittest.mock.MagicMock(return_value=PREWARMED)

        with _capture_opened() as opened:
            await _drive(state, slot)

        assert opened.call_args.kwargs["previous_sid"] == PREDECESSOR

    @pytest.mark.asyncio
    async def test_a_turn_with_no_prefetch_reads_the_predecessor_itself(
        self, tmp_path, _runner_config
    ):
        """With nothing latched, this turn IS the first allocation for the slot."""
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        assert slot._crew_log_previous_sid == ""
        state.sessions.mapped_sid = unittest.mock.MagicMock(return_value=PREDECESSOR)

        with _capture_opened() as opened:
            await _drive(state, slot)

        assert opened.call_args.kwargs["previous_sid"] == PREDECESSOR

    @pytest.mark.asyncio
    async def test_the_edge_is_spent_on_the_entry_that_carried_it(self, tmp_path, _runner_config):
        """One latch, one entry: the slot's next store must latch afresh."""
        _runner_config(_config(tmp_path))
        state, _client = _turn_state(tmp_path)
        slot = _slot()
        slot.latch_crew_log_previous(PREDECESSOR)
        state.sessions.mapped_sid = unittest.mock.MagicMock(return_value=PREWARMED)

        with _capture_opened():
            await _drive(state, slot)

        assert slot._crew_log_previous_sid == ""


class TestAReplayPendingAllocationLeavesTheMappingBehind:
    """The mapping can be a generation behind the store the slot is writing.

    An allocation whose history replay is pending keeps the prior resumable id in
    the mapping on purpose, so the id a restart can resume stays durable. The store
    the slot is actually writing is newer than that. What the slot last handed to a
    `session/opened` is the one source that states it, so the latch prefers it and
    the window cannot make an edge point at an older generation.
    """

    def test_the_latch_names_the_store_the_slot_is_on_not_the_mapped_one(self):
        slot = _slot()
        slot.take_crew_log_previous(now_writing=NEWEST)

        # What `mapped_sid` answers inside the deferral window: the generation
        # before the store this slot is on, because the newer id was never
        # published over the mapping.
        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot._crew_log_previous_sid == NEWEST

    def test_three_successive_stores_cite_three_different_predecessors(self):
        """The chain: no store is cited twice and none is left cited by nobody."""
        cited: list[str] = []

        slot = _slot()
        slot.latch_crew_log_previous("")
        cited.append(slot.take_crew_log_previous(now_writing=PREDECESSOR).sid)

        # The mapping is stuck on the first store for both allocations after it.
        slot.latch_crew_log_previous(PREDECESSOR)
        cited.append(slot.take_crew_log_previous(now_writing=NEWEST).sid)

        slot.latch_crew_log_previous(PREDECESSOR)
        cited.append(slot.take_crew_log_previous(now_writing=SUCCESSOR).sid)

        assert cited == ["", PREDECESSOR, NEWEST], "the chain skipped a store"

    def test_a_slot_this_process_has_not_opened_keeps_the_mapped_answer(self):
        """Nothing recorded means the mapping is the only source, and it is used."""
        slot = _slot()

        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot._crew_log_previous_sid == PREDECESSOR

    def test_the_record_is_kept_when_no_entry_carried_the_edge(self):
        """A warm turn writes no opening entry, and the slot is still on that store.

        The record states which store the slot is ON rather than what was appended,
        so a turn that hands over an empty edge must still leave it behind: the next
        allocation has nothing else that names the store it supersedes.
        """
        slot = _slot()

        assert slot.take_crew_log_previous(now_writing=NEWEST).sid == ""

        slot.latch_crew_log_previous(PREDECESSOR)
        assert slot._crew_log_previous_sid == NEWEST

    def test_the_record_does_not_reopen_a_latch_already_filled(self):
        """Write-once holds on the record path too, not just the mapping path.

        The eager prefetch and the first real turn both latch, and the turn's
        observation is the successor the prefetch just produced. Replacing the edge
        there would name the store the turn is writing FOR: the emitter drops a
        self-edge, so the predecessor would go uncited with no second chance.
        """
        slot = _slot()
        slot.take_crew_log_previous(now_writing=NEWEST)
        slot.latch_crew_log_previous(PREDECESSOR)

        slot.latch_crew_log_previous(SUCCESSOR)

        assert slot._crew_log_previous_sid == NEWEST

    def test_a_store_with_no_name_is_not_recorded(self):
        """An emitter call with no session id must not make the slot's record empty."""
        slot = _slot()
        slot.take_crew_log_previous(now_writing=NEWEST)

        slot.take_crew_log_previous(now_writing="")

        slot.latch_crew_log_previous(PREDECESSOR)
        assert slot._crew_log_previous_sid == NEWEST

    @pytest.mark.asyncio
    async def test_two_turns_on_one_slot_cite_two_different_predecessors(
        self, tmp_path, _runner_config
    ):
        """The defect through the real turn path, with the mapping held still.

        Both allocations read the same mapped id, which is what a replay-pending
        allocation leaves behind: it keeps the prior resumable id rather than
        publishing its successor. The second turn must cite the store the first turn
        opened, not repeat the id the first turn cited.
        """
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        slot = _slot()
        state.sessions.mapped_sid = unittest.mock.MagicMock(return_value=PREDECESSOR)

        cited: list[str] = []
        for opened_store in (NEWEST, SUCCESSOR):
            client.session_id = opened_store
            with _capture_opened() as opened:
                await _drive(state, slot)
            cited.append(opened.call_args.kwargs["previous_sid"])

        assert cited == [PREDECESSOR, NEWEST], "the second store repeated the first's predecessor"


class TestTheStoreDecidesNotTheMapping:
    """The units answer which store the slot is on; the mapping is the fallback.

    A replay-pending allocation keeps the PRIOR resumable id in the mapping on
    purpose, so that the id a restart can resume stays durable. The mapping is then
    a generation behind the store the slot is writing, and an edge taken from it
    leaves the store between two citations cited by nobody.
    """

    @pytest.mark.asyncio
    async def test_the_resolver_prefers_the_newest_unit_over_the_mapped_id(self, _store):
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        sessions = _Sessions(PREDECESSOR)

        resolved = await _slot_predecessor_store(sessions, _ChatSlot(SLOT), SLOT)

        assert resolved.sid == NEWEST, "the resolver answered a generation behind the store"

    @pytest.mark.asyncio
    async def test_a_slot_with_no_unit_falls_back_to_the_mapping(self, _store):
        """Nothing on disk is not an error: a slot's FIRST store has no predecessor
        unit, and a re-attach after a restart still needs the id the key was serving."""
        sessions = _Sessions(PREDECESSOR)

        resolved = await _slot_predecessor_store(sessions, _ChatSlot(SLOT), SLOT)

        assert resolved.sid == PREDECESSOR
        assert sessions.reads == 1, "the fallback was not consulted"

    @pytest.mark.asyncio
    async def test_the_units_of_another_slot_are_not_this_slot_s_history(self, _store):
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR, slot="chat-someone-else")
        sessions = _Sessions("")

        resolved = await _slot_predecessor_store(sessions, _ChatSlot(SLOT), SLOT)

        # Succession is a relation inside one slot. Answering another slot's store
        # would join its turns, costs and approvals into this slot's whole-life read.
        assert resolved.sid == PREDECESSOR

    @pytest.mark.asyncio
    async def test_an_undecided_store_read_writes_no_edge_instead_of_asking_the_mapping(
        self, _store
    ):
        """The two empties are different facts, and only one licenses the mapping.

        A unit that will not read means the units could not be judged -- and the
        mapping is the source this read was preferred over, a generation behind inside
        the replay window. Falling back there freezes a wrong citation on an
        append-only entry. Answering nothing costs one edge while the fault lasts.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        newest_dir = crew_log_dir("session", NEWEST)
        real_read_head = session_tree.read_head
        sessions = _Sessions(PREDECESSOR)

        def _faults_on_the_newest(path):
            if path.parent == newest_dir:
                raise OSError("file-descriptor ceiling")
            return real_read_head(path)

        with unittest.mock.patch.object(session_tree, "read_head", _faults_on_the_newest):
            resolved = await _slot_predecessor_store(sessions, _ChatSlot(SLOT), SLOT)

        assert resolved == CrewLogPrevious(sid="", undecided=True), (
            "an undecided read did not SAY so, so the announce it feeds records a log "
            "that claims to start the slot's chain"
        )
        assert sessions.reads == 0, (
            "an indeterminate store read fell back to the mapping, which inside the "
            "replay window is the generation-behind answer this read exists to avoid"
        )

    @pytest.mark.asyncio
    async def test_a_store_with_nothing_to_order_by_does_not_cite_a_pending_mapping(self, _store):
        """The shape every upgraded slot is in, plus the one window the mapping lies in.

        Two stores written before this edge existed: both state they have no
        predecessor, so the store has nothing to order them by and answers "no
        succession". The mapping is normally the right fallback there -- but while
        allocation is holding the prior resumable id back it names a generation
        behind, and citing it orphans the store between the two. Neither source can
        say, so the log records a break instead.

        Driven through the LATCH and the TAKE rather than the resolver alone, because
        that is where the window is asked about: the resolver runs before this turn's
        session exists, so it hands the id back provisional and the take decides.
        """
        _store(PREDECESSOR)
        _store(NEWEST)
        sessions = _Sessions(PREDECESSOR, replay_pending=True)
        slot = _ChatSlot(SLOT)

        resolved = await _slot_predecessor_store(sessions, slot, SLOT)
        slot.latch_crew_log_previous(
            resolved.sid, undecided=resolved.undecided, from_mapping=resolved.from_mapping
        )
        edge = slot.take_crew_log_previous(now_writing=NEWEST, replay_pending=True)

        assert resolved.from_mapping, "the mapped id was not marked provisional"
        assert edge == CrewLogPrevious(sid="", undecided=True), (
            "the mapping was cited while it was knowingly a generation behind, which "
            "orphans the store between the two on an append-only entry"
        )

    @pytest.mark.asyncio
    async def test_a_window_invisible_to_the_resolver_still_stops_the_citation(self, _store):
        """The window the resolver CANNOT see, which is the restart case.

        The marker is an attribute of a live session, and the latch runs before this
        turn's session is allocated -- so on a cold start the resolver is told "no
        replay owed" by the mere absence of anybody to ask, which is exactly when the
        mapping is most likely to be holding the older generation. The resolver here
        sees a clean window and the take sees the real one, and the edge must still be
        a break: a decision made where the answer is unavailable is not a decision.
        """
        _store(PREDECESSOR)
        _store(NEWEST)
        sessions = _Sessions(PREDECESSOR)
        slot = _ChatSlot(SLOT)

        resolved = await _slot_predecessor_store(sessions, slot, SLOT)
        slot.latch_crew_log_previous(
            resolved.sid, undecided=resolved.undecided, from_mapping=resolved.from_mapping
        )
        edge = slot.take_crew_log_previous(now_writing=NEWEST, replay_pending=True)

        assert resolved.sid == PREDECESSOR, "the resolver did not reach the mapping at all"
        assert edge == CrewLogPrevious(sid="", undecided=True), (
            "the citation was decided from a marker read before the session existed, "
            "so a restart inside the replay window freezes the older generation"
        )

    @pytest.mark.asyncio
    async def test_the_same_store_cites_the_mapping_once_the_window_has_passed(self, _store):
        """The other side, so the refusal above is a window and not a ban.

        Outside that window the mapping IS the durable ordering for a store with no
        succession recorded, and refusing there would leave the fallback unreachable
        on every upgraded slot -- no edge would ever be written and each create would
        add one more unrankable log.
        """
        _store(PREDECESSOR)
        _store(NEWEST)
        sessions = _Sessions(PREDECESSOR)
        slot = _ChatSlot(SLOT)

        resolved = await _slot_predecessor_store(sessions, slot, SLOT)
        slot.latch_crew_log_previous(
            resolved.sid, undecided=resolved.undecided, from_mapping=resolved.from_mapping
        )
        edge = slot.take_crew_log_previous(now_writing=NEWEST, replay_pending=False)

        assert edge == CrewLogPrevious(sid=PREDECESSOR, undecided=False)

    @pytest.mark.asyncio
    async def test_a_handover_with_an_empty_mapping_states_nothing(self, _store):
        """The two DECIDED empties are different facts, and only one may be stated.

        Units written before these keys existed sit on disk, so the store hands the
        question on rather than ranking them. The mapping entry is then gone -- the
        state the emitter's own note names, where the map is deleted while the units
        remain -- so neither source names a predecessor.

        That is NOT a finding that the slot has none. Reported as one, the entry records
        `previous_none` for a slot with earlier logs sitting beside it, a later fold
        passes over it because a stated chain start may be passed over, and the log is
        orphaned in an append-only record. The honest answer determines nothing.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        _store(SUCCESSOR)
        for sid in (PREDECESSOR, SUCCESSOR):
            _strip_edge_keys(sid)
        emit.reset_caches()
        slot = _ChatSlot(SLOT)

        resolved = await _slot_predecessor_store(_Sessions(""), slot, SLOT)

        assert resolved.undecided is None, (
            "a hand-over plus an empty mapping was reported as a determination, so the "
            "entry states this slot has no earlier store while three of its logs sit "
            "on disk"
        )
        slot.latch_crew_log_previous(
            resolved.sid, undecided=resolved.undecided, from_mapping=resolved.from_mapping
        )
        edge = slot.take_crew_log_previous(now_writing=PREWARMED, replay_pending=False)
        assert edge == CrewLogPrevious(sid="", undecided=None)

    @pytest.mark.asyncio
    async def test_a_slot_with_no_unit_at_all_still_states_its_absence(self, _store):
        """The other side, so the rule above is a distinction and not a retreat.

        With no unit of this slot in the store the absence IS the whole truth, and an
        empty mapping beside it makes this the slot's first store. That has to stay
        statable: it is the one case `previous_none` exists for, and losing it would
        leave every first log indistinguishable from one written before the keys.
        """
        slot = _ChatSlot(SLOT)

        resolved = await _slot_predecessor_store(_Sessions(""), slot, SLOT)

        assert resolved == CrewLogPrevious(sid="", undecided=False, from_mapping=True)

    @pytest.mark.asyncio
    async def test_a_record_of_this_process_is_not_downgraded_by_the_window(self, _store):
        """The window bears on the MAPPING, not on what this process itself recorded.

        Reachable through the two allocation sites: the prefetch's resolver runs while
        the slot has no record, so it answers from the mapping and flags the id
        provisional -- and by the time that latch lands the turn may already have taken
        an entry, which records the store this process opened. The record wins over the
        mapped id, so what gets latched is the record rather than the mapping's answer,
        and the flag must not survive the substitution.

        The slot's own record is this gateway's statement about which store the slot is
        on, taken as that store became current. A pending replay says nothing about it,
        and downgrading it would lose the one citation no other source can supply: a
        store whose create is still queued to the writer thread.
        """
        slot = _ChatSlot(SLOT)
        slot.take_crew_log_previous(now_writing=PREDECESSOR, replay_pending=False)
        # The mapped id a resolver answered BEFORE that record existed, arriving late.
        slot.latch_crew_log_previous(SUCCESSOR, undecided=False, from_mapping=True)

        edge = slot.take_crew_log_previous(now_writing=NEWEST, replay_pending=True)

        assert edge == CrewLogPrevious(sid=PREDECESSOR, undecided=False), (
            "a pending replay window discarded this process's own record of the store "
            "it opened, which is the only source that can name a queued create"
        )

    @pytest.mark.asyncio
    async def test_a_pending_window_with_no_mapped_id_records_no_break(self, _store):
        """A break claims a predecessor EXISTS, so it must not be written when none does.

        With nothing in the mapping and nothing in the store, the slot has no earlier
        store by either source. Recording a break there would be the same kind of
        false statement this fold exists to avoid, in the other direction.
        """
        sessions = _Sessions("", replay_pending=True)
        slot = _ChatSlot(SLOT)

        resolved = await _slot_predecessor_store(sessions, slot, SLOT)
        slot.latch_crew_log_previous(
            resolved.sid, undecided=resolved.undecided, from_mapping=resolved.from_mapping
        )
        edge = slot.take_crew_log_previous(now_writing=NEWEST, replay_pending=True)

        assert edge == CrewLogPrevious(sid="", undecided=False)

    @pytest.mark.asyncio
    async def test_the_store_is_read_off_the_event_loop(self, _store):
        """A listing plus a line pair per unit is blocking work, so it hops a thread.

        Observed rather than asserted structurally: what matters is that the read did
        not happen on the thread the loop runs on, whatever spelling puts it there.
        """
        _store(PREDECESSOR)
        seen: list[tuple[str, str]] = []

        def _record(slot_key: str) -> tuple[str, bool, bool]:
            seen.append((slot_key, threading.current_thread().name))
            return ("", True, True)

        with unittest.mock.patch.object(chat_runner.crew_log_emit, "slot_previous_store", _record):
            await _slot_predecessor_store(_Sessions(PREDECESSOR), _ChatSlot(SLOT), SLOT)

        assert seen, "the store was never consulted"
        read_slot, thread = seen[0]
        assert read_slot == SLOT
        assert thread != threading.main_thread().name, (
            "the store read ran on the thread the event loop is on, so every turn's "
            "allocation now waits behind a directory listing"
        )

    @pytest.mark.asyncio
    async def test_a_slot_that_already_holds_the_answer_reads_no_store(self, _store):
        """The warm turn, which is nearly every turn, must not pay for the store.

        The latch prefers the slot's own record over anything this resolver returns, so
        a read taken while that record is set is spent on a value the caller discards.
        It is not a cheap discard: the read lists the session root and reads a line pair
        per unit of the slot, so its cost grows with the store rather than with the
        turn. Counted here rather than asserted structurally, because what matters is
        that the work did not happen.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        slot = _ChatSlot(SLOT)
        slot.latch_crew_log_previous(PREDECESSOR)
        seen: list[str] = []

        def _record(slot_key: str) -> tuple[str, bool, bool]:
            seen.append(slot_key)
            return ("", True, True)

        sessions = _Sessions(PREDECESSOR)
        with unittest.mock.patch.object(chat_runner.crew_log_emit, "slot_previous_store", _record):
            resolved = await _slot_predecessor_store(sessions, slot, SLOT)

        assert seen == [], "the store was read for a value the latch then discarded"
        assert sessions.reads == 0, "the mapping was read for a value the latch discards"
        assert resolved == CrewLogPrevious(sid="", undecided=False), (
            "an undecided answer here would make the caller record a break for a slot "
            "whose own record names its predecessor"
        )

    @pytest.mark.asyncio
    async def test_a_flag_off_launch_reads_no_store_at_all(self, _store, monkeypatch):
        """The gate is the flag, so a gateway with the crew log off pays nothing."""
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        monkeypatch.delenv(emit.CREW_LOG_ENV)
        sessions = _Sessions(PREDECESSOR)

        resolved = await _slot_predecessor_store(sessions, _ChatSlot(SLOT), SLOT)
        assert resolved.sid == PREDECESSOR


class TestTheHeadFold:
    """Which of a slot's logs is the newest, from the edges alone. Pure."""

    def _record(
        self,
        sid: str,
        previous: str | None = None,
        *,
        at: int = 0,
        slot: str = SLOT,
        edge: str | None = None,
    ):
        """One log's contribution, with what its announce said about a predecessor.

        ``edge`` defaults to :data:`EDGE_NAMED` when *previous* is given and to
        :data:`EDGE_UNREAD` otherwise -- the answer that refuses -- because a
        hand-built record has read no announce and must not pass for one that did.
        """
        if edge is None:
            edge = EDGE_NAMED if previous else EDGE_UNREAD
        return OpenedRecord(
            sid=sid, slot=slot, created_at=at, previous_sid=previous, previous_edge=edge
        )

    def test_the_log_no_other_log_cites_is_the_head(self):
        records = [
            self._record(PREDECESSOR),
            self._record(NEWEST, PREDECESSOR),
            self._record(SUCCESSOR, NEWEST),
        ]

        assert fold_slot_head(records, SLOT) == SlotHead(sid=SUCCESSOR, decided=True)

    def test_the_answer_does_not_depend_on_the_order_the_records_arrive_in(self):
        records = [
            self._record(SUCCESSOR, NEWEST),
            self._record(PREDECESSOR),
            self._record(NEWEST, PREDECESSOR),
        ]

        assert fold_slot_head(records, SLOT) == SlotHead(sid=SUCCESSOR, decided=True)

    def test_the_edges_outrank_the_stamp(self):
        """The stamp is wall clock, and a backward step across a restart inverts it.

        The chain does not invert, so the newer log wins here even while carrying the
        earlier stamp -- which is the whole reason the edge was recorded.
        """
        records = [
            self._record(PREDECESSOR, at=2_000),
            self._record(NEWEST, PREDECESSOR, at=1_000),
        ]

        assert fold_slot_head(records, SLOT) == SlotHead(sid=NEWEST, decided=True)

    def test_a_retention_gap_does_not_make_a_cited_log_the_head(self):
        """The oldest log is gone, so the surviving chain's own head still answers."""
        records = [self._record(NEWEST, PREDECESSOR), self._record(SUCCESSOR, NEWEST)]

        assert fold_slot_head(records, SLOT) == SlotHead(sid=SUCCESSOR, decided=True)

    def test_two_chain_starts_are_undecided_rather_than_placed_by_the_clock(self):
        """The one case the edges cannot decide, and no clock may decide it either.

        `created_at` is wall clock, so a backward step across a restart hands the
        newer log the earlier stamp and the pick inverts -- permanently, because the
        id goes into an append-only entry. UNDECIDED costs one citation while the
        record is incomplete; a wrong one costs the walk forever.
        """
        records = [self._record(PREDECESSOR, at=1_000), self._record(NEWEST, at=2_000)]

        assert fold_slot_head(records, SLOT) == UNDECIDED

    def test_a_foreign_log_is_not_ranked(self):
        records = [self._record(PREDECESSOR), self._record(NEWEST, slot="chat-elsewhere", at=9)]

        assert fold_slot_head(records, SLOT) == SlotHead(sid=PREDECESSOR, decided=True)

    def test_no_record_for_the_slot_is_no_log_not_undecided(self):
        """A slot with no log is a FACT, and it is the one empty a caller may act on."""
        assert fold_slot_head([], SLOT) == NO_LOG
        assert fold_slot_head([self._record(NEWEST, slot="chat-elsewhere")], SLOT) == NO_LOG

    def test_a_cycle_leaves_no_uncited_log_and_is_undecided(self):
        """Only a forged or damaged record can do this, and it must not be guessed at."""
        records = [self._record(PREDECESSOR, NEWEST, at=1), self._record(NEWEST, PREDECESSOR, at=2)]

        assert fold_slot_head(records, SLOT) == UNDECIDED

    def test_logs_that_all_state_no_predecessor_hand_over_rather_than_refuse(self):
        """The state a store written before the edge existed is in, and the answer
        has to be the one that can still change.

        Every log here READ its announce and none names a predecessor, so the store
        holds no succession for this slot to read -- nothing undecidable, just nothing
        recorded. UNDECIDED would suppress the caller's next source, so no edge would
        ever be written, and every later create would add one more unrankable log:
        permanent, on every store that predates the edge, which is all of them.

        HAND_OVER and not NO_LOG, because units of this slot EXIST. The distinction is
        what the caller may SAY: it may consult its next source either way, and only
        on a complete absence may it record that the slot has no earlier store. Several
        logs each claiming to start the chain is this fold failing to rank them.
        """
        records = [
            self._record(PREDECESSOR, at=1_000, edge=EDGE_NONE),
            self._record(NEWEST, at=2_000, edge=EDGE_NONE),
        ]

        answered = fold_slot_head(records, SLOT)

        assert answered == HAND_OVER
        assert answered.decided is True, "the caller's next source was suppressed"
        assert answered.complete is False, (
            "a store holding units it could not rank reported the absence as complete, "
            "which licenses the caller to record that this slot has no earlier store"
        )

    def test_the_one_log_that_records_an_edge_is_the_head_beside_stated_orphans(self):
        """How a store that predates the edge starts describing itself.

        The first create the fix writes records an edge; the logs before it state they
        have none. Ranking is the edges, and a stated absence is not one, so the log
        that cites is the head -- no backfill, and no clock.
        """
        records = [
            self._record(PREDECESSOR, at=3_000, edge=EDGE_NONE),
            self._record(NEWEST, at=1_000, edge=EDGE_NONE),
            self._record(SUCCESSOR, NEWEST, at=2_000, edge=EDGE_NAMED),
        ]

        assert fold_slot_head(records, SLOT) == SlotHead(sid=SUCCESSOR, decided=True)

    def test_a_log_whose_announce_was_never_read_refuses_the_whole_answer(self):
        """A gap is not a statement, and the difference is the whole point of the flag.

        This log's edge is unknown rather than absent, so it can neither be ranked nor
        passed over: passing over it elects another log and orphans a store this slot
        certainly opened, which is the defect this fold exists to avoid. Its presence
        settles the answer on its own, beside any number of stated ones.
        """
        records = [
            self._record(PREDECESSOR, at=1_000, edge=EDGE_NONE),
            self._record(NEWEST, at=2_000),
        ]

        assert fold_slot_head(records, SLOT) == UNDECIDED

    def test_a_legacy_silence_beside_a_linked_head_goes_to_the_mapping(self):
        """The shape a pre-upgrade store reaches when the old gateway missed one edge.

        The old emitter wrote `previous` only when it had a predecessor to name, so a
        log it failed to resolve carries no key at all -- byte-identical to one that
        starts the slot's chain. Passing it over elects the linked older log and
        orphans it permanently. Refusing would be permanent too, because that silence
        never becomes anything else, so the answer hands the question to the mapping,
        which still knows which store the slot is on.

        It hands over WITHOUT a determination: units of this slot exist, so an empty
        answer from the mapping says only that the mapping had nothing to give, and an
        entry recording that as "this slot has no earlier store" would be the same
        false claim from the other direction.
        """
        records = [
            self._record(PREDECESSOR, at=1_000, edge=EDGE_LEGACY),
            self._record(NEWEST, PREDECESSOR, at=2_000),
            self._record(SUCCESSOR, at=3_000, edge=EDGE_LEGACY),
        ]

        answered = fold_slot_head(records, SLOT)

        assert answered != SlotHead(sid=NEWEST, decided=True), (
            "the linked older log was elected while a legacy silence sat uncited "
            "beside it, which orphans that log on an append-only entry"
        )
        assert answered == HAND_OVER
        assert answered.complete is False, (
            "a legacy silence reported the absence as complete, so the entry may state "
            "the slot has no earlier store while its own units say otherwise"
        )

    def test_an_unread_announce_refuses_rather_than_going_to_the_mapping(self):
        """The other silence, and it takes the other answer.

        No announce means the RECORD is incomplete, and that is recoverable: it answers
        as soon as the announce is readable. A legacy silence is complete and will not
        change, so it hands over instead. Same missing `previous_sid`, opposite calls.
        """
        records = [
            self._record(PREDECESSOR, at=1_000, edge=EDGE_LEGACY),
            self._record(NEWEST, at=2_000, edge=EDGE_UNREAD),
        ]

        assert fold_slot_head(records, SLOT) == UNDECIDED


class TestTheStoreReader:
    """What :func:`slot_chain_head` makes of the units actually on disk."""

    def test_the_newest_unit_of_the_slot_is_answered(self, _store):
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)

        assert slot_chain_head(SLOT) == SlotHead(sid=NEWEST, decided=True)

    def test_one_unit_is_its_own_head(self, _store):
        _store(PREDECESSOR)

        assert slot_chain_head(SLOT) == SlotHead(sid=PREDECESSOR, decided=True)

    def test_a_unit_whose_announce_has_not_landed_leaves_the_read_undecided(self, _store):
        """The create writes the header and the announce is a second write.

        A read between the two finds a store the slot has certainly opened and an edge
        that is merely not written yet, which makes a SECOND uncited unit. Nothing in
        the record orders the two, so this answers UNDECIDED -- and the caller writes
        no edge rather than guessing. The slot's own record covers this window, which
        is why it is consulted ahead of this read: see
        `TestTheResolutionOrder::test_a_second_allocation_cites_a_store_still_queued_to_the_writer`.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        _store(SUCCESSOR, previous=NEWEST)
        _drop_announce(SUCCESSOR, later_than=NEWEST)

        assert slot_chain_head(SLOT) == UNDECIDED

    def test_no_unit_is_no_log_not_undecided(self, _store):
        assert slot_chain_head(SLOT) == NO_LOG
        assert slot_chain_head("") == NO_LOG

    def test_a_store_whose_units_all_state_no_predecessor_hands_over_and_then_describes_itself(
        self, _store
    ):
        """A store whose logs each RECORD that they start a chain, with no backfill.

        Nothing here is undecidable: every log read its announce and each says it has
        no earlier store, so the read hands the question on, the caller's next source
        decides, and the store starts answering for itself from the first create that
        records an edge -- without anything rewriting the units already on disk.

        The hand-over is INCOMPLETE, because units of this slot are on disk. The caller
        may act on it and may state nothing: an entry claiming the slot has no earlier
        store would contradict the very units this read just listed.
        """
        _store(PREDECESSOR)
        _store(NEWEST)

        answered = slot_chain_head(SLOT)
        assert answered == HAND_OVER, (
            "a store of stated chain starts answered UNDECIDED, which writes no edge "
            "and suppresses the fallback, so no later create could ever record one"
        )
        assert answered.complete is False, (
            "a store holding two units called the absence complete, which lets the "
            "entry state this slot has no earlier store"
        )

        _store(SUCCESSOR, previous=NEWEST)

        assert slot_chain_head(SLOT) == SlotHead(sid=SUCCESSOR, decided=True)

    def test_units_from_before_the_keys_hand_over_rather_than_elect_a_cited_log(self, _store):
        """The store the fix actually meets: units that say NOTHING about an edge.

        Every store on disk today is made of these, because writing no edge is
        precisely what the defect did, and their silence is unexplained -- equally "I
        am first" and "I could not tell". Passing them over elects the log the newer
        one cites and orphans the silent one on an append-only entry, so the read hands
        the question to the caller's next source, which is what decided it before this
        fix existed. That is not a regression against anything; it is the pre-fix
        answer, kept until the units themselves can say more.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        _store(SUCCESSOR)
        for sid in (PREDECESSOR, SUCCESSOR):
            _strip_edge_keys(sid)
        emit.reset_caches()

        answered = slot_chain_head(SLOT)

        assert answered != SlotHead(sid=NEWEST, decided=True), (
            "the cited log was elected head while a unit from before the keys sat "
            "uncited beside it, which orphans that unit permanently"
        )
        assert answered == HAND_OVER
        assert answered.complete is False, (
            "units from before the keys reported the absence as complete, so the next "
            "create would state this slot has no earlier store while they sit on disk"
        )

    def test_a_first_store_states_its_absence_rather_than_leaving_the_key_out(self, _store):
        """The write half of the distinction, on a unit the real emitter wrote.

        A log that omits every predecessor key cannot be told from one written before
        the keys existed, whose omission may hide a predecessor nobody named. So the
        gateway that LOOKED and found none has to say so, because that statement is
        the only thing a later fold may pass over.
        """
        _store(PREDECESSOR)

        directory = crew_log_dir("session", PREDECESSOR)
        _header, announce, _ = store.read_head(store.oldest_segment(directory))
        assert announce is not None
        assert announce.data.get("previous_none") is True, (
            "the announce left every predecessor key out, so this log is "
            "indistinguishable from one written before the keys existed"
        )
        assert "previous" not in announce.data
        assert "previous_undecided" not in announce.data

        record = session_tree.opened_record(directory, _header_of(PREDECESSOR), announce)
        assert record is not None
        assert record.previous_edge == session_tree.EDGE_NONE

    def test_a_caller_that_never_looked_states_nothing_about_a_predecessor(self, _store):
        """The statement is the LOOKER's, so a caller that did not look writes no key.

        The channel dispatchers hold no slot record and no store read: they hand over
        one id captured from the mapping, and pass nothing about whether a predecessor
        exists. That id is empty whenever the mapping entry is gone while the slot's
        units remain, and an emitter treating the emptiness as a finding would have
        that log declare itself the slot's first -- a claim no one made, frozen into an
        append-only entry, which a later fold then passes over.

        So the unit must come out in the LEGACY state: the honest reading of a log
        whose announce says nothing either way, and the state that sends the question
        to a source which still knows. Written through the emitter directly rather than
        the fixture's helper, because the fixture passes a determination and the
        dispatchers are the callers that do not.
        """
        emit.on_session_opened(
            NEWEST,
            agent="default",
            slot=SLOT,
            memory="global",
            channel=True,
            previous_sid="",
        )
        assert emit.flush(10.0), "the writer did not settle, so the store is not the input"

        directory = crew_log_dir("session", NEWEST)
        _header, announce, _ = store.read_head(store.oldest_segment(directory))
        assert announce is not None
        assert "previous_none" not in announce.data, (
            "the emitter stated the slot has no earlier store on behalf of a caller "
            "that never determined it, which a later fold is entitled to pass over"
        )
        assert "previous" not in announce.data
        assert "previous_undecided" not in announce.data

        record = session_tree.opened_record(directory, _header_of(NEWEST), announce)
        assert record is not None
        assert record.previous_edge == session_tree.EDGE_LEGACY

    def test_an_undetermined_predecessor_is_recorded_and_does_not_elect_the_log_before_it(
        self, _store
    ):
        """The write half and the read half of one fact, on units the emitter wrote.

        A read fault at CREATE time leaves the resolver unable to name a predecessor.
        If that log simply omits the key it is indistinguishable from a log that
        starts the slot's chain, so a later fold passes over it and elects the log
        before it -- writing, by another route, the citation this read refused to
        guess. The announce records the difference and the fold refuses on it.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        _store(SUCCESSOR, undecided=True)

        # The write half: the break is recorded, and NOT as an empty citation.
        directory = crew_log_dir("session", SUCCESSOR)
        _header, announce, _ = store.read_head(store.oldest_segment(directory))
        assert announce is not None
        assert announce.data.get("previous_undecided") is True, (
            "the announce recorded nothing, so this log is indistinguishable from one "
            "that starts the slot's chain"
        )
        assert "previous" not in announce.data

        # The read half: NEWEST is not elected.
        answered = slot_chain_head(SLOT)
        assert answered != SlotHead(sid=NEWEST, decided=True), (
            "the log before the undetermined one was elected head, which freezes the "
            "citation the store read refused to guess"
        )
        assert answered == UNDECIDED

    def test_the_slot_keeps_citing_forward_after_an_undetermined_predecessor(self, _store):
        """What recovery there is: the break does not stop the next link.

        The lost edge itself cannot come back -- the announce is append-only and the
        predecessor was never recorded. What must keep working is everything after
        it, and that is the slot's own record: the process that opened the store
        names it for the next allocation, so the chain continues forward from the
        break instead of the slot going silent.
        """
        _store(PREDECESSOR)
        _store(NEWEST, undecided=True)
        slot = _ChatSlot(SLOT)
        slot.latch_crew_log_previous("", undecided=True)
        slot.take_crew_log_previous(now_writing=NEWEST)

        slot.latch_crew_log_previous("")

        assert slot.take_crew_log_previous(now_writing=SUCCESSOR).sid == NEWEST

    def test_one_unit_it_cannot_read_makes_the_whole_answer_unknown(self, _store):
        """A partial fold here would be a WRONG citation, not a shorter one.

        The id this returns is latched once and written into an append-only
        `previous`, so a head taken from the units that happened to read is frozen
        with no second chance. ``""`` sends the caller to its next source instead.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        newest_dir = crew_log_dir("session", NEWEST)
        real_read_head = session_tree.read_head

        def _faults_on_the_newest(path):
            if path.parent == newest_dir:
                raise OSError("file-descriptor ceiling")
            return real_read_head(path)

        with unittest.mock.patch.object(session_tree, "read_head", _faults_on_the_newest):
            answered = slot_chain_head(SLOT)

        assert answered == UNDECIDED, (
            "the newest unit could not be read and an older head was returned, which "
            "would freeze a wrong predecessor on an append-only entry"
        )
        # The fault was transient, so the next read answers again.
        assert slot_chain_head(SLOT) == SlotHead(sid=NEWEST, decided=True)

    def test_a_listing_that_cannot_be_made_is_unknown_rather_than_no_log(self, _store):
        """A scan that failed and a slot with no unit are different answers.

        :data:`NO_LOG` is the licence to use the next source, and inside the replay
        window that source holds the generation before -- so letting a failed scan
        answer it spends the licence on a read that saw nothing at all.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)

        with unittest.mock.patch.object(
            store, "_checked_crew_log_root", side_effect=OSError("networked data home")
        ):
            assert slot_chain_head(SLOT) == UNDECIDED

        # The fault was the moment's, not the store's, so the read answers again.
        assert slot_chain_head(SLOT) == SlotHead(sid=NEWEST, decided=True)

    def test_a_newest_unit_the_listing_cannot_prove_does_not_elect_the_one_before_it(self, _store):
        """The listing is taken STRICTLY, so a unit it cannot account for refuses it.

        An ordinary listing is a read and answers the shorter truth: a unit whose
        header will not prove while it already holds entries is left out without a
        word, and the unit likeliest to be in that state is the newest one. Left out,
        it leaves the unit BEFORE it uncited, and that older id is what would be
        frozen into an append-only ``previous``.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        newest_dir = crew_log_dir("session", NEWEST)
        real_proved_header = store._proved_header

        def _will_not_prove_the_newest(directory):
            if directory == newest_dir:
                return None
            return real_proved_header(directory)

        with unittest.mock.patch.object(store, "_proved_header", _will_not_prove_the_newest):
            answered = slot_chain_head(SLOT)

        assert answered != SlotHead(sid=PREDECESSOR, decided=True), (
            "the unit before the unprovable one was elected head, which freezes a "
            "predecessor a generation back onto an append-only entry"
        )
        assert answered == UNDECIDED
        # Nothing latched the refusal: the header proves again and so does the read.
        assert slot_chain_head(SLOT) == SlotHead(sid=NEWEST, decided=True)

    def test_a_unit_whose_header_names_another_slot_makes_the_answer_unknown(self, _store):
        """The listing and the header disagree, and nothing here can say which is
        right, so neither reading is used."""
        _store(PREDECESSOR)

        with unittest.mock.patch.object(
            session_tree,
            "opened_record",
            return_value=OpenedRecord(sid=PREDECESSOR, slot="chat-elsewhere", created_at=1),
        ):
            assert slot_chain_head(SLOT) == UNDECIDED


#: Run in a FRESH interpreter, against a store an earlier process wrote. Prints the
#: id the next ``session/opened`` of that slot would cite, and nothing else.
_RESTART_PROBE = """
import asyncio, json, os, sys

os.environ["KIROCREW_HOME"] = sys.argv[1]
os.environ["KIROCREW_CREW_LOG"] = "1"
SLOT, MAPPED = sys.argv[2], sys.argv[3]

from kiro_crew.dashboard.chat_runner import _slot_predecessor_store
from kiro_crew.dashboard.state import CrewLogPrevious, _ChatSlot


class Sessions:
    def mapped_sid(self, _key):
        # A replay-pending allocation left the PRIOR resumable id here on purpose,
        # and it is the only thing a restart would otherwise have.
        return MAPPED

    def provider_switch_replay_pending(self, _key):
        # Settled: this probe is about a restart reading a store, not about the
        # window where allocation is still holding the prior id back.
        return False


async def main():
    slot = _ChatSlot(SLOT)
    resolved = await _slot_predecessor_store(Sessions(), slot, SLOT)
    slot.latch_crew_log_previous(resolved.sid, undecided=resolved.undecided)
    print(json.dumps({"cited": slot.take_crew_log_previous(now_writing="sid-being-opened-now").sid}))


asyncio.run(main())
"""


def _after_restart(home: Path, slot: str, mapped: str) -> str:
    """What a NEW gateway process would cite for *slot*, given the store at *home*.

    A real subprocess, because that is the only thing this can be: an in-memory
    record answers correctly for as long as the interpreter that wrote it lives, so
    a test that stays inside one process passes whether or not the answer is
    durable. Modelled on the boot probe in ``test_crew_log_emit``: fixed argv, no
    shell, an absolutised interpreter so a relative PATH entry cannot break the
    child, and the repo's own ``src`` on the path.
    """
    env = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(home.parent),
        "KIROCREW_HOME": str(home),
    }
    if sys.platform == "win32":  # pragma: no cover - parity with the boot probe
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        for name in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
            env[name] = os.environ.get(name, "")
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            os.path.abspath(sys.executable),
            "-c",
            textwrap.dedent(_RESTART_PROBE),
            str(home),
            slot,
            mapped,
        ],
        env=env,
        cwd=home.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert done.returncode == 0, f"the restart probe did not run: {done.stderr[-2000:]}"
    return json.loads(done.stdout.strip().splitlines()[-1])["cited"]


class TestTheAnswerSurvivesTheProcess:
    """The issue, at the only boundary that can show it.

    Everything the pre-restart process knew is gone: a new interpreter, a new slot
    object, a new store cache. What is left is the units it wrote and a mapping that
    is deliberately a generation behind, and the successor must still cite the store
    the slot was actually on.
    """

    def test_a_new_process_cites_the_store_the_old_one_opened(self, _store, tmp_path):
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)

        cited = _after_restart(tmp_path / "home", SLOT, mapped=PREDECESSOR)

        assert cited == NEWEST, (
            "a restart inside the replay window cited a generation back, so the store "
            "between the two citations is cited by nobody and a chain walk steps over it"
        )

    def test_a_new_process_still_has_the_mapping_for_a_slot_with_no_unit(self, _store, tmp_path):
        """The fallback has to survive the boundary too: a slot whose first store is
        being opened has no unit to derive from, and the mapped id is the real answer."""
        cited = _after_restart(tmp_path / "home", SLOT, mapped=PREDECESSOR)

        assert cited == PREDECESSOR


class TestTheResolutionOrder:
    """Three sources, and each one covers a window the next cannot.

    The slot's own record of the store it opened is FIRST, because it is the only
    source that can name a store whose unit is not on disk yet: the create is queued
    to the writer thread, so a second allocation inside that window finds nothing to
    read. The store is next, and it is the only source that survives the process. The
    mapping is last, and it is a proxy rather than an answer -- a replay-pending
    allocation holds the prior resumable id there on purpose.

    Dropping any tier reopens a window: without the record, a buffered create is
    orphaned in-process; without the store, a restart cites a generation back;
    without the mapping, a slot's first store has no id to re-attach to.
    """

    def test_the_record_outranks_the_store_and_the_mapping(self, _store):
        """The tier order where all three can answer, and they disagree."""
        _store(PREDECESSOR)
        slot = _slot()
        slot.take_crew_log_previous(now_writing=NEWEST)

        # What the fallback pair would answer: the only unit on disk.
        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot._crew_log_previous_sid == NEWEST

    @pytest.mark.asyncio
    async def test_a_second_allocation_cites_a_store_still_queued_to_the_writer(
        self, tmp_path, _runner_config, _store
    ):
        """The window no store read can cover, through the real turn path.

        `on_session_opened` queues the create, so while that job is buffered the
        slot's newest store has NO unit for any reader to find. The store on disk
        answers the generation before it, and the mapping is held still the way a
        replay-pending allocation leaves it. Only the slot's own record names the
        store the second turn must cite.

        Mutation guard: resolving from the store or the mapping alone answers
        `PREDECESSOR` twice here, which orphans the store the first turn opened.
        """
        _store(PREDECESSOR)
        _runner_config(_config(tmp_path))
        state, client = _turn_state(tmp_path)
        slot = _slot()
        state.sessions.mapped_sid = unittest.mock.MagicMock(return_value=PREDECESSOR)

        cited: list[str] = []
        for opened_store in (NEWEST, SUCCESSOR):
            client.session_id = opened_store
            # The emitter is intercepted, so nothing is appended for these two
            # stores -- which is exactly what a buffered writer looks like to a
            # reader: the unit is not there yet.
            with _capture_opened() as opened:
                await _drive(state, slot)
            cited.append(opened.call_args.kwargs["previous_sid"])

        assert cited == [PREDECESSOR, NEWEST], "the store the first turn opened is orphaned"

    def test_the_resolver_reads_the_store_first_and_the_mapping_only_after(self):
        source = inspect.getsource(_slot_predecessor_store)

        # Enumerated rather than paraphrased: the store call, the thread hop that
        # keeps it off the loop, and the non-pruning mapping accessor behind it.
        # Nothing else may decide the fallback pair. Flattened, because whether the
        # call fits on one line is the formatter's business.
        assert "asyncio.to_thread( crew_log_emit.slot_previous_store, slot.key )" in " ".join(
            source.split()
        )
        # The mapping is reached only on a DECIDED empty, and it comes back flagged
        # rather than cited: whether allocation is holding the prior id back cannot be
        # read here, so the decision belongs where a session exists to be asked. An
        # undecided read names no store and SAYS so, which is what stops the announce
        # reading as a chain start. And a DECIDED empty states the absence only when the
        # store's answer is COMPLETE -- units it could not rank make an empty mapping
        # no finding about this slot.
        assert "undecided=False if complete else None," in source
        assert 'return CrewLogPrevious(sid="", undecided=True)' in source
        assert "provider_switch_replay_pending" not in source, (
            "the window is decided here again, before this turn's session exists, so a "
            "cold start reads 'no replay owed' from there being nobody to ask"
        )
        assert "resumable_sid(" not in source, (
            "resumable_sid stats the transcript on the calling thread and PRUNES the "
            "mapping, which erases the id exactly when the two stores disagree"
        )

    def test_the_store_side_is_keyed_by_the_slot_not_the_session_key(self):
        """A channel-born slot runs its turns on the channel's session key, while a
        unit's header records the SLOT. Reading the units under the session key would
        find none for exactly those slots, and every one of them would lose its edge."""
        source = inspect.getsource(_slot_predecessor_store)

        assert "slot_previous_store, slot.key" in source
