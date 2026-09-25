"""``session/opened.previous`` survives a pre-warmed session.

Regression: the slot's predecessor store was read inside the first real turn,
straight from ``mapped_sid``. Two sites allocate a session for one slot -- the
eager prefetch and that first turn -- and the prefetch runs FIRST and maps its
own session over the slot's key. So on every slot that pre-warms, the turn's
read answered the successor it had just been handed, the emitter compared that
id against the session it was writing for, found them equal and wrote no
``previous`` edge at all. The superseded store was left unlinked, so nothing
joined the slot's history across the restart, and an append-only record has no
later chance to add the edge.

The predecessor is now latched on the slot at whichever allocation observes it
FIRST and handed to exactly one ``session/opened``. These turns drive the real
``_eager_spawn`` and ``_run_chat`` bodies and assert on the ``previous_sid``
kwarg the emitter receives, so they fail if either site goes back to reading the
mapping for itself.
"""

from __future__ import annotations

import itertools
import json
import unittest.mock
from pathlib import Path

import pytest
from test_chat_runner_coverage import _drive, _runner_state, _slot
from test_chat_send_agent_model_default import _config, _pin_sync_accessors, _turn_state

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.crew_log import crew_log_path
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn

PREDECESSOR = "sid-the-slot-was-writing"
PREWARMED = "sid-the-prefetch-allocated"
#: The store opened AFTER ``PREDECESSOR`` on the same slot, whose id the mapping
#: never received because the allocation that produced it deferred promoting it.
NEWEST = "sid-the-mapping-never-received"
#: The store a third allocation opens, which must cite ``NEWEST``.
SUCCESSOR = "sid-the-third-allocation-opened"


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


@pytest.fixture
def _crew_log_home(tmp_path, monkeypatch):
    """An isolated store plus a stepping clock, so real units order deterministically.

    The units of one slot are ordered by their header's ``createdAt``, and two
    creates inside one millisecond tie and fall back to the unit id -- an order
    that has nothing to do with which store was opened first. The clock steps once
    per reading here, so each unit's header carries a distinct time and the order
    under test is the order the stores were opened in.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-log-home"))
    monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    stepping = itertools.count(1_700_000_000_000, 1000)
    monkeypatch.setattr(crew_log_store, "now_ms", lambda: next(stepping))
    crew_log_emit.reset_caches()
    yield
    crew_log_emit.drain_for_shutdown(timeout=2.0)
    crew_log_emit.reset_caches()


def _open_store(sid: str, slot_key: str, *, previous_sid: str = "") -> None:
    """Create a real unit for *sid* whose header names *slot_key*."""
    crew_log_emit.on_session_opened(
        sid,
        agent="kirocrew",
        slot=slot_key,
        cwd="/home/dev/project",
        previous_sid=previous_sid,
    )
    assert crew_log_emit.flush()


def _cited_predecessor(sid: str) -> "dict | None":
    """The ``previous`` object on *sid*'s one ``session/opened`` entry."""
    path: Path = crew_log_path("session", sid)
    with path.open("r", encoding="utf-8") as handle:
        entries = [json.loads(line) for line in handle if line.strip()]
    opened = [entry for entry in entries[1:] if entry.get("type") == "session/opened"]
    assert len(opened) == 1, f"{sid} has {len(opened)} opening entries"
    return opened[0]["data"].get("previous")


class TestTheMappingCanAnswerOlderThanTheNewestStore:
    """A deferred promotion leaves the mapping naming a store that is not the newest.

    An allocation whose history replay is still pending keeps the prior resumable
    id in the mapping on purpose, so the mapping and the store disagree for that
    window: the store the slot is writing is newer than the id the mapping holds.
    The slot's own units are the authority on which store it wrote last, and the
    latch reads them, so the window cannot make an edge point at a generation
    older than the newest store.
    """

    def test_the_latch_names_the_newest_store_not_the_mapped_one(self, _crew_log_home):
        slot = _slot()
        _open_store(PREDECESSOR, slot.key)
        _open_store(NEWEST, slot.key, previous_sid=PREDECESSOR)

        # What `mapped_sid` answers inside the deferral window: the generation
        # before the newest store, because the newest one was never published.
        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot._crew_log_previous_sid == NEWEST

    def test_three_successive_stores_cite_three_different_predecessors(self, _crew_log_home):
        """The chain, end to end: no store is cited twice and none is cited by nobody."""
        slot = _slot()
        _open_store(PREDECESSOR, slot.key)

        slot.latch_crew_log_previous(PREDECESSOR)
        _open_store(NEWEST, slot.key, previous_sid=slot.take_crew_log_previous())

        # The mapping is still stuck on the first store for this allocation too.
        slot.latch_crew_log_previous(PREDECESSOR)
        _open_store(SUCCESSOR, slot.key, previous_sid=slot.take_crew_log_previous())

        assert _cited_predecessor(PREDECESSOR) is None
        assert _cited_predecessor(NEWEST) == {"sid": PREDECESSOR}
        assert _cited_predecessor(SUCCESSOR) == {"sid": NEWEST}, "the chain skipped a store"

    def test_a_slot_with_no_store_yet_keeps_the_mapped_answer(self, _crew_log_home):
        """Nothing to read means the mapping is the only source, and it is used."""
        slot = _slot()

        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot._crew_log_previous_sid == PREDECESSOR

    def test_another_slot_s_store_is_never_taken_as_this_slot_s_predecessor(self, _crew_log_home):
        """The units are read by slot, so a busier neighbour cannot supply the edge."""
        slot = _slot()
        _open_store("sid-belonging-to-another-slot", "chat-someone-else")

        slot.latch_crew_log_previous("")

        assert slot._crew_log_previous_sid == ""

    def test_the_first_answer_survives_the_store_that_allocation_goes_on_to_open(
        self, _crew_log_home
    ):
        """Write-once, tested against the newer store the latching allocation opens.

        The prefetch latches, its session then opens a store of its own, and the
        turn that follows latches again. Replacing the latch there would name the
        store the turn is writing FOR: the emitter drops a self-edge, so the
        predecessor would go uncited with no second chance to add it.
        """
        slot = _slot()
        _open_store(PREDECESSOR, slot.key)
        slot.latch_crew_log_previous(PREDECESSOR)

        _open_store(PREWARMED, slot.key, previous_sid=PREDECESSOR)
        slot.latch_crew_log_previous(PREWARMED)

        assert slot._crew_log_previous_sid == PREDECESSOR
