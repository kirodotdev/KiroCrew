"""``session/opened.previous`` names the store the slot is really on.

Two regressions live here, and the second is why the first one's fix moved.

The first: the predecessor was read inside the first real turn, straight from
``mapped_sid``. Two sites allocate a session for one slot -- the eager prefetch
and that first turn -- and the prefetch runs FIRST and maps its own session over
the slot's key, so the turn's read answered the successor it had just been
handed, the emitter found the two ids equal and wrote no ``previous`` edge at
all. The predecessor is latched on the slot at whichever allocation observes it
FIRST and handed to exactly one ``session/opened``.

The second: what the latch was fed. A record on the slot naming the store it had
just opened answered correctly inside one process and vanished with it, and the
one window where it was the only source -- a replay-pending allocation, which
keeps the PRIOR resumable id in the mapping on purpose -- is exactly the window a
restart lands in. So the answer now comes from the store: the units under the
slot's own key, ordered by the succession edges they recorded. The test that
matters for that is the one that crosses a real process boundary, because an
in-memory record passes every test that stays inside one interpreter.
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
from kiro_crew.crew_log import crew_log_path, emit
from kiro_crew.crew_log.session_tree import OpenedRecord, fold_slot_head, slot_chain_head
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn, _slot_predecessor_store
from kiro_crew.dashboard.state import _ChatSlot

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

    def _open(sid: str, *, previous: str = "", slot: str = SLOT) -> None:
        emit.on_session_opened(
            sid, slot=slot, agent="default", memory="global", previous_sid=previous
        )
        assert emit.flush(10.0), "the writer did not settle, so the store is not the input"

    yield _open
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


class _Sessions:
    """The mapping boundary, answering one id however often it is asked."""

    def __init__(self, sid: str) -> None:
        self._sid = sid
        self.reads = 0

    def mapped_sid(self, _key: str) -> str:
        self.reads += 1
        return self._sid


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
        assert slot.take_crew_log_previous() == ""

    def test_taking_the_edge_clears_it(self):
        slot = _slot()
        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot.take_crew_log_previous() == PREDECESSOR
        # Left behind, it would make the slot's NEXT store cite this store's
        # predecessor and skip this store -- a gap a chain walker cannot see.
        assert slot.take_crew_log_previous() == ""


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

        assert resolved == NEWEST, "the resolver answered a generation behind the store"

    @pytest.mark.asyncio
    async def test_a_slot_with_no_unit_falls_back_to_the_mapping(self, _store):
        """Nothing on disk is not an error: a slot's FIRST store has no predecessor
        unit, and a re-attach after a restart still needs the id the key was serving."""
        sessions = _Sessions(PREDECESSOR)

        resolved = await _slot_predecessor_store(sessions, _ChatSlot(SLOT), SLOT)

        assert resolved == PREDECESSOR
        assert sessions.reads == 1, "the fallback was not consulted"

    @pytest.mark.asyncio
    async def test_the_units_of_another_slot_are_not_this_slot_s_history(self, _store):
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR, slot="chat-someone-else")
        sessions = _Sessions("")

        resolved = await _slot_predecessor_store(sessions, _ChatSlot(SLOT), SLOT)

        # Succession is a relation inside one slot. Answering another slot's store
        # would join its turns, costs and approvals into this slot's whole-life read.
        assert resolved == PREDECESSOR

    @pytest.mark.asyncio
    async def test_the_store_is_read_off_the_event_loop(self, _store):
        """A listing plus a line pair per unit is blocking work, so it hops a thread.

        Observed rather than asserted structurally: what matters is that the read did
        not happen on the thread the loop runs on, whatever spelling puts it there.
        """
        _store(PREDECESSOR)
        seen: list[tuple[str, str]] = []

        def _record(slot_key: str) -> str:
            seen.append((slot_key, threading.current_thread().name))
            return ""

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
    async def test_a_flag_off_launch_reads_no_store_at_all(self, _store, monkeypatch):
        """The gate is the flag, so a gateway with the crew log off pays nothing."""
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        monkeypatch.delenv(emit.CREW_LOG_ENV)
        sessions = _Sessions(PREDECESSOR)

        assert await _slot_predecessor_store(sessions, _ChatSlot(SLOT), SLOT) == PREDECESSOR


class TestTheHeadFold:
    """Which of a slot's logs is the newest, from the edges alone. Pure."""

    def _record(self, sid: str, previous: str | None = None, *, at: int = 0, slot: str = SLOT):
        return OpenedRecord(sid=sid, slot=slot, created_at=at, previous_sid=previous)

    def test_the_log_no_other_log_cites_is_the_head(self):
        records = [
            self._record(PREDECESSOR),
            self._record(NEWEST, PREDECESSOR),
            self._record(SUCCESSOR, NEWEST),
        ]

        assert fold_slot_head(records, SLOT) == SUCCESSOR

    def test_the_answer_does_not_depend_on_the_order_the_records_arrive_in(self):
        records = [
            self._record(SUCCESSOR, NEWEST),
            self._record(PREDECESSOR),
            self._record(NEWEST, PREDECESSOR),
        ]

        assert fold_slot_head(records, SLOT) == SUCCESSOR

    def test_the_edges_outrank_the_stamp(self):
        """The stamp is wall clock, and a backward step across a restart inverts it.

        The chain does not invert, so the newer log wins here even while carrying the
        earlier stamp -- which is the whole reason the edge was recorded.
        """
        records = [
            self._record(PREDECESSOR, at=2_000),
            self._record(NEWEST, PREDECESSOR, at=1_000),
        ]

        assert fold_slot_head(records, SLOT) == NEWEST

    def test_a_retention_gap_does_not_make_a_cited_log_the_head(self):
        """The oldest log is gone, so the surviving chain's own head still answers."""
        records = [self._record(NEWEST, PREDECESSOR), self._record(SUCCESSOR, NEWEST)]

        assert fold_slot_head(records, SLOT) == SUCCESSOR

    def test_two_chain_starts_are_placed_by_the_stamp(self):
        """The one case the edges cannot decide: a log whose own edge was never
        recorded is a second start, and then the stamp is all that is left."""
        records = [self._record(PREDECESSOR, at=1_000), self._record(NEWEST, at=2_000)]

        assert fold_slot_head(records, SLOT) == NEWEST

    def test_a_foreign_log_is_not_ranked(self):
        records = [self._record(PREDECESSOR), self._record(NEWEST, slot="chat-elsewhere", at=9)]

        assert fold_slot_head(records, SLOT) == PREDECESSOR

    def test_no_record_for_the_slot_answers_nothing(self):
        assert fold_slot_head([], SLOT) == ""
        assert fold_slot_head([self._record(NEWEST, slot="chat-elsewhere")], SLOT) == ""

    def test_a_cycle_does_not_hang_the_fold(self):
        """Only a forged or damaged record can do this, and it must still answer."""
        records = [self._record(PREDECESSOR, NEWEST, at=1), self._record(NEWEST, PREDECESSOR, at=2)]

        assert fold_slot_head(records, SLOT) == NEWEST


class TestTheStoreReader:
    """What :func:`slot_chain_head` makes of the units actually on disk."""

    def test_the_newest_unit_of_the_slot_is_answered(self, _store):
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)

        assert slot_chain_head(SLOT) == NEWEST

    def test_one_unit_is_its_own_head(self, _store):
        _store(PREDECESSOR)

        assert slot_chain_head(SLOT) == PREDECESSOR

    def test_a_unit_whose_announce_has_not_landed_still_counts(self, _store):
        """The create writes the header and the announce is a second write.

        A read between the two finds a store the slot has certainly opened and an
        edge that is merely not written yet. Leaving it out would answer the store
        before it and orphan it -- the defect this reader exists to remove, arriving
        by a different door. It is also why the several-heads tie cannot be settled by
        succession depth: this unit's depth is 0 while the chain head it must beat is
        three links deep.
        """
        _store(PREDECESSOR)
        _store(NEWEST, previous=PREDECESSOR)
        _store(SUCCESSOR, previous=NEWEST)
        # Two chain starts can only be ordered by the stamp, so the fixture makes the
        # order unambiguous rather than leaving the two creates inside one millisecond
        # and asserting a tie-break by accident.
        _drop_announce(SUCCESSOR, later_than=NEWEST)

        assert slot_chain_head(SLOT) == SUCCESSOR

    def test_no_unit_answers_nothing(self, _store):
        assert slot_chain_head(SLOT) == ""
        assert slot_chain_head("") == ""


#: Run in a FRESH interpreter, against a store an earlier process wrote. Prints the
#: id the next ``session/opened`` of that slot would cite, and nothing else.
_RESTART_PROBE = """
import asyncio, json, os, sys

os.environ["KIROCREW_HOME"] = sys.argv[1]
os.environ["KIROCREW_CREW_LOG"] = "1"
SLOT, MAPPED = sys.argv[2], sys.argv[3]

from kiro_crew.dashboard.chat_runner import _slot_predecessor_store
from kiro_crew.dashboard.state import _ChatSlot


class Sessions:
    def mapped_sid(self, _key):
        # A replay-pending allocation left the PRIOR resumable id here on purpose,
        # and it is the only thing a restart would otherwise have.
        return MAPPED


async def main():
    slot = _ChatSlot(SLOT)
    resolved = await _slot_predecessor_store(Sessions(), slot, SLOT)
    slot.latch_crew_log_previous(resolved)
    print(json.dumps({"cited": slot.take_crew_log_previous()}))


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


class TestNothingProcessLocalDecidesThePredecessor:
    """Enumerated from the source, because this is a claim about ABSENCE.

    A cache reintroduced on the slot would answer correctly in every in-process
    test, and the only behavioural test that can catch it is the subprocess one
    above, which cannot say WHERE the state came back. These reds name the place.

    Mutation guard: adding a second ``_crew_log_*`` slot to ``_ChatSlot``, taking a
    "which store are we writing" argument back onto ``take_crew_log_previous``, or
    feeding the latch from anything but the resolver, each reds one assertion here.
    """

    def test_the_slot_holds_one_crew_log_field_and_it_is_the_per_turn_latch(self):
        held = [name for name in _ChatSlot.__slots__ if name.startswith("_crew_log")]

        # The latch is per-handover state, spent inside one turn. A SECOND field
        # here would be a record of which store the slot is on -- the exact state
        # that dies with the process and leaves the mapping's stale answer behind.
        assert held == ["_crew_log_previous_sid"], (
            "a new crew-log field on the slot: if it records which store the slot is "
            f"on, a restart loses it and the chain gap comes back -- {held}"
        )

    def test_spending_the_edge_records_nothing_in_exchange(self):
        taken = inspect.signature(_ChatSlot.take_crew_log_previous)

        assert list(taken.parameters) == ["self"], (
            "take_crew_log_previous accepts a value again, which is how the store the "
            "slot is on came to be remembered in memory rather than read back"
        )

    def test_the_resolver_reads_the_store_first_and_the_mapping_only_after(self):
        source = inspect.getsource(_slot_predecessor_store)

        # Enumerated rather than paraphrased: the store call, the thread hop that
        # keeps it off the loop, and the non-pruning mapping accessor as the
        # fallback. Nothing else may decide this.
        assert "asyncio.to_thread(crew_log_emit.slot_previous_store, slot.key)" in source
        assert "return derived or sessions.mapped_sid(session_key)" in source
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
