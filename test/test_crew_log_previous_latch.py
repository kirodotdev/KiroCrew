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

import unittest.mock

import pytest
from test_chat_runner_coverage import _drive, _runner_state, _slot
from test_chat_send_agent_model_default import _config, _pin_sync_accessors, _turn_state

from kiro_crew.config.loader import KiroCrewConfig
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
        assert slot.take_crew_log_previous(now_writing=PREWARMED) == ""

    def test_taking_the_edge_clears_it(self):
        slot = _slot()
        slot.latch_crew_log_previous(PREDECESSOR)

        assert slot.take_crew_log_previous(now_writing=NEWEST) == PREDECESSOR
        # Left behind, it would make the slot's NEXT store cite this store's
        # predecessor and skip this store -- a gap a chain walker cannot see.
        assert slot.take_crew_log_previous(now_writing=NEWEST) == ""


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
        cited.append(slot.take_crew_log_previous(now_writing=PREDECESSOR))

        # The mapping is stuck on the first store for both allocations after it.
        slot.latch_crew_log_previous(PREDECESSOR)
        cited.append(slot.take_crew_log_previous(now_writing=NEWEST))

        slot.latch_crew_log_previous(PREDECESSOR)
        cited.append(slot.take_crew_log_previous(now_writing=SUCCESSOR))

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

        assert slot.take_crew_log_previous(now_writing=NEWEST) == ""

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
