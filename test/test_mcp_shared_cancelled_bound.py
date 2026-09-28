"""Regression tests for the unbounded _cancelled_ids growth bug (audit E,
mcp_shared.py).

Bug: the stdio loop recorded every ``notifications/cancelled`` request id into
a plain ``set`` that was never pruned. Over the life of a long-lived,
per-session MCP process this leaks one entry per cancel. The fix routes adds
through ``_remember_cancelled_id`` which evicts oldest ids FIFO once the count
exceeds ``CANCELLED_IDS_MAX``, and completion sites discard consumed ids.
"""

from __future__ import annotations

import collections

from kiro_crew.mcp_shared import CANCELLED_IDS_MAX, _remember_cancelled_id


def _fresh():
    return set(), collections.deque()


class TestRememberCancelledIdBound:
    def test_grows_unbounded_without_cap_would_leak(self):
        """Failure scenario: many distinct cancels must NOT grow without bound."""
        ids, order = _fresh()
        cap = 8
        for i in range(1000):
            _remember_cancelled_id(ids, order, f"req-{i}", cap=cap)
        assert len(ids) == cap
        assert len(order) <= cap

    def test_evicts_oldest_first(self):
        ids, order = _fresh()
        cap = 4
        for i in range(6):
            _remember_cancelled_id(ids, order, f"req-{i}", cap=cap)
        # oldest two evicted, newest four retained
        assert "req-0" not in ids
        assert "req-1" not in ids
        assert {"req-2", "req-3", "req-4", "req-5"} == ids

    def test_idempotent_repeat_does_not_duplicate(self):
        ids, order = _fresh()
        for _ in range(10):
            _remember_cancelled_id(ids, order, "same", cap=100)
        assert ids == {"same"}
        assert list(order) == ["same"]

    def test_recent_cancel_still_recognized(self):
        """A just-recorded cancel id must be a member (suppression still works)."""
        ids, order = _fresh()
        _remember_cancelled_id(ids, order, "current", cap=CANCELLED_IDS_MAX)
        assert "current" in ids

    def test_consumed_id_discarded_then_readd_ok(self):
        """After a completion site discards a consumed id, it can be re-added."""
        ids, order = _fresh()
        _remember_cancelled_id(ids, order, "r1", cap=100)
        ids.discard("r1")  # models the completion-site prune
        _remember_cancelled_id(ids, order, "r2", cap=100)
        assert "r2" in ids

    def test_default_cap_is_positive(self):
        assert isinstance(CANCELLED_IDS_MAX, int)
        assert CANCELLED_IDS_MAX > 0

    def test_deque_bounded_when_set_stays_small_via_completion_discards(self):
        """Common path: each cancel targets the in-flight req and is consumed
        (discarded from the SET only) at completion. The deque must not grow
        without bound even though the set stays tiny -- the second (deque-length)
        bound is what prevents the leak the PR set out to close."""
        ids, order = _fresh()
        cap = 8
        for i in range(1000):
            _remember_cancelled_id(ids, order, f"req-{i}", cap=cap)
            # completion site prunes the set only, never the deque
            ids.discard(f"req-{i}")
        assert len(ids) == 0
        assert len(order) <= cap

    def test_eviction_tolerates_short_or_empty_deque(self):
        """Bookkeeping skew (set larger than deque) must never raise
        IndexError on the shared stdio reader -- both eviction loops guard on
        a non-empty deque. (This skew cannot occur once every add routes
        through the helper; the guard is a defensive backstop, so the only
        contract here is 'does not crash'.)"""
        ids = {f"stray-{i}" for i in range(50)}  # set already over any small cap
        order: "collections.deque[str]" = collections.deque()  # empty deque
        # Must not raise even though len(ids) >> cap and order is empty.
        _remember_cancelled_id(ids, order, "new", cap=4)
        # The deque never desyncs below the set in real code, so no membership
        # guarantee is asserted for this artificial skew -- only crash-safety.

    def test_idle_and_busy_paths_share_the_bound(self):
        """Both add sites route through the same helper, so mixing them keeps
        the pair in lockstep and bounded."""
        ids, order = _fresh()
        cap = 4
        for i in range(20):
            _remember_cancelled_id(ids, order, f"idle-{i}", cap=cap)
        assert len(ids) <= cap
        assert len(order) <= cap


class TestProtectedIdsSurviveEviction:
    """GPT 5.6 HIGH: a flood of unrelated cancels must NOT evict the
    cancellation flag of an active or still-queued request before the dispatch
    loop consumes it -- otherwise a cancelled queued call would execute (a
    data-mutation path for destructive tools). ``protected`` ids are never
    evicted."""

    def test_protected_queued_cancel_survives_flood(self):
        ids, order = _fresh()
        cap = 8
        # A queued request "victim" is cancelled while waiting to be dispatched.
        _remember_cancelled_id(ids, order, "victim", cap=cap, protected={"victim"})
        # Now a flood of unrelated cancels arrives, each protecting only the
        # still-live "victim" (models _live_request_ids while it stays queued).
        for i in range(1000):
            _remember_cancelled_id(
                ids, order, f"noise-{i}", cap=cap, protected={"victim"}
            )
        # The victim's cancellation flag is still present -- the queued call
        # will be dropped at dispatch instead of executing.
        assert "victim" in ids

    def test_multiple_protected_ids_all_survive(self):
        ids, order = _fresh()
        cap = 4
        live = {"active", "q1", "q2"}
        for rid in live:
            _remember_cancelled_id(ids, order, rid, cap=cap, protected=live)
        for i in range(500):
            _remember_cancelled_id(ids, order, f"noise-{i}", cap=cap, protected=live)
        assert live <= ids

    def test_unprotected_ids_still_evicted_around_protected(self):
        """Protection must not disable the bound for non-protected ids: the set
        stays close to cap (only the small protected set may push it over)."""
        ids, order = _fresh()
        cap = 8
        for i in range(1000):
            _remember_cancelled_id(
                ids, order, f"noise-{i}", cap=cap, protected={"keepme"}
            )
        # No "keepme" was ever added, so nothing is protected -> strict bound.
        assert len(ids) == cap

    def test_bounded_overflow_when_all_protected(self):
        """If every retained id is protected, the helper stops evicting rather
        than dropping a live cancellation -- overflow is bounded by the
        protected-set size (in production: pending-queue cap + 1)."""
        ids, order = _fresh()
        cap = 2
        protected = {f"live-{i}" for i in range(5)}
        for rid in protected:
            _remember_cancelled_id(ids, order, rid, cap=cap, protected=protected)
        # All protected -> none evicted despite exceeding cap; no crash.
        assert protected <= ids
        assert len(ids) == len(protected)
