"""Owner-rooted private-member dispatch is decided at the create gate alone.

The shared ownership fence ``_caller_is_ownership_fenced`` fences EVERY
agent-created session (a non-empty ``_created_by``), unchanged: it is read for
the per-verb ownership boundary in ``authorize_target``, so a created session
reaches only slots it created and nothing an unfenced creator could reach.

The one allowance a conductor the owner started in their own tab needs -- minting
private-member workers -- lives in ``_delegation_lineage_fenced``, read ONLY at
the ``create_session`` delegation gate. It climbs the ``_created_by`` chain live
at each hop, so:

* an owner-rooted chain (root is an unattributed tab) is NOT fenced and may
  dispatch -- the bug;
* a cron / channel-link / crew-member-rooted chain stays fenced at every depth,
  grandchildren included -- the deputy hole stays closed;
* a creator that BECOMES a member or acquires a channel link after the mint
  fences the whole chain live -- there is no frozen verdict to go stale;
* a gap (a creator slot that is gone, or a chain past the depth bound) fails
  CLOSED to fenced, so a closed middle slot loses dispatch rather than widening.

No verdict is stored on the slot or persisted: the owner-rooted answer is
recomputed live from the chain at each delegation, so a restart changes nothing.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.members import DM_SLOT_KEY_PREFIX

CRON_SLOT = "cron-2c3b2e25"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


class TestSharedFenceContainsEveryAgentCreatedSession:
    """``_caller_is_ownership_fenced`` fences any ``_created_by`` slot (pre-PR).

    This is the answer ``authorize_target`` reads on every verb, so it must not
    depend on who the creator was -- an owner-rooted conductor is still contained
    to its own children at the ownership boundary.
    """

    def test_owner_rooted_agent_created_slot_is_fenced_at_the_shared_predicate(self, tmp_path):
        state = _make_state(tmp_path)
        conductor = state.get_or_create_slot("chat-9")
        conductor._created_by = "chat-1"  # the owner's own tab
        assert sc._caller_is_ownership_fenced(state, "chat-9")

    def test_cron_rooted_agent_created_slot_is_fenced_at_the_shared_predicate(self, tmp_path):
        state = _make_state(tmp_path)
        child = state.get_or_create_slot("chat-9")
        child._created_by = CRON_SLOT
        assert sc._caller_is_ownership_fenced(state, "chat-9")

    def test_unattributed_slot_is_not_fenced(self, tmp_path):
        """A person's own tab reaches get_or_create_slot directly and is unfenced."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1")
        assert not sc._caller_is_ownership_fenced(state, "chat-1")

    def test_member_and_cron_callers_are_fenced(self, tmp_path):
        state = _make_state(tmp_path)
        assert sc._caller_is_ownership_fenced(state, CRON_SLOT)
        assert sc._caller_is_ownership_fenced(state, DM_SLOT_KEY_PREFIX + "radar")


class TestDelegationGateAllowsOwnerRootedDispatch:
    """``_delegation_lineage_fenced`` is the gate-only owner-rooted allowance."""

    def test_owner_rooted_conductor_may_dispatch(self, tmp_path):
        """Root is the owner's own unattributed tab -> not fenced -> dispatch allowed."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1")  # owner tab, unattributed
        conductor = state.get_or_create_slot("chat-9")
        conductor._created_by = "chat-1"
        assert not sc._delegation_lineage_fenced(state, "chat-9")

    def test_owner_rooted_grandchild_may_dispatch(self, tmp_path):
        """The walk climbs to the unattributed root through any depth."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1")
        child = state.get_or_create_slot("chat-9")
        child._created_by = "chat-1"
        grandchild = state.get_or_create_slot("chat-10")
        grandchild._created_by = "chat-9"
        assert not sc._delegation_lineage_fenced(state, "chat-10")

    def test_cron_rooted_chain_stays_fenced(self, tmp_path):
        """A hop that is a cron tab fences the whole chain."""
        state = _make_state(tmp_path)
        child = state.get_or_create_slot("chat-9")
        child._created_by = CRON_SLOT
        assert sc._delegation_lineage_fenced(state, "chat-9")

    def test_member_rooted_chain_stays_fenced(self, tmp_path):
        state = _make_state(tmp_path)
        child = state.get_or_create_slot("chat-9")
        child._created_by = DM_SLOT_KEY_PREFIX + "radar"
        assert sc._delegation_lineage_fenced(state, "chat-9")

    def test_channel_linked_root_stays_fenced(self, tmp_path):
        """A root that carries a (non-cron) channel link fences the chain."""
        state = _make_state(tmp_path)
        root = state.get_or_create_slot("chat-1")
        root.linked_session_key = "slack:C123:456"
        child = state.get_or_create_slot("chat-9")
        child._created_by = "chat-1"
        assert sc._delegation_lineage_fenced(state, "chat-9")

    def test_gap_in_chain_fails_closed(self, tmp_path):
        """A creator slot that is gone cannot prove an owner root -> fenced."""
        state = _make_state(tmp_path)
        child = state.get_or_create_slot("chat-9")
        child._created_by = "chat-404"  # ancestor never created / already closed
        assert sc._delegation_lineage_fenced(state, "chat-9")

    def test_cycle_fails_closed(self, tmp_path):
        """A corrupted ``_created_by`` cycle is treated as a gap, not spun on."""
        state = _make_state(tmp_path)
        a = state.get_or_create_slot("chat-9")
        b = state.get_or_create_slot("chat-10")
        a._created_by = "chat-10"
        b._created_by = "chat-9"
        assert sc._delegation_lineage_fenced(state, "chat-9")
