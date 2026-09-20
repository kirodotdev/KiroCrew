"""Coalescing and re-assertion in the pure decision engine.

These pin the window the engine grows over successive changes to ONE subject:
the first actionable change wakes at once, a rapid follow-up folds into one wake,
a head change resets the window, re-assertion re-wakes an unresolved change on
the re-alert interval, and the re-alert map is pruned unconditionally. They are
the engine-terms counterpart of
``test/test_irq_port_baseline.py``: that file pins ``irq.py``, which this port
does not touch, so these tests are what verify the ported behaviour.

Each test states the behaviour it pins in present tense. The window rides on
``MonitorState``; a decision reads and updates it, and the caller persists the
same state, so no file is held here.
"""

from __future__ import annotations

import pytest

from kiro_crew.monitoring import models
from kiro_crew.monitoring.decision import decide_monitor
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_COALESCE_SECS,
    DEFAULT_MONITOR_REALERT_SECS,
    MONITOR_REVISION_KEY_SPACE,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorState,
    monitor_state_from_dict,
)

_FLOOR = DEFAULT_MONITOR_COALESCE_SECS
_REALERT = DEFAULT_MONITOR_REALERT_SECS


def _revision(key: str) -> str:
    """The dedupe key a subject naming no conditions of its own is kept under.

    Its one synthesized condition is revision-scoped and keyed by the subject
    fingerprint, so every window and mask entry in this module carries the
    revision key space.
    """
    return MONITOR_REVISION_KEY_SPACE + key


def _state(**overrides) -> MonitorState:
    base = dict(
        kind="github_pull_request",
        target="owner/repo/pull/1",
        objective="review_ready",
        created_ts=0.0,
        budgets=MonitorBudgets(max_runtime_secs=10_000_000),
    )
    base.update(overrides)
    return MonitorState(**base)


def _actionable(fingerprint: str, *, head_changed: bool = False) -> MonitorObservation:
    return MonitorObservation(
        fingerprint,
        MonitorObservationStatus.ACTIONABLE,
        head_changed=head_changed,
    )


def _decide(state, obs, *, now):
    return decide_monitor(state, obs, now=now).decision


def test_the_first_actionable_change_wakes_at_once():
    """A new actionable fingerprint wakes on the tick it is seen.

    The probe interval is user-set and typically exceeds the floor, so holding
    the first change adds an interval of latency while grouping nothing. The
    first change wakes now and opens the window that a later change folds into.
    """
    state = _state()
    assert _decide(state, _actionable("red:a"), now=100.0) is MonitorDecision.WAKE_ACTIONABLE
    assert state.coalesce_windows == {_revision("red:a"): 100.0}


def test_a_rapid_follow_up_change_folds_into_one_wake():
    """A different change arriving inside the floor is held, then fired once.

    This is the flood case the window exists for: a subject changing again while
    the first wake's window is still open does not buy a second immediate wake.
    """
    state = _state()
    assert _decide(state, _actionable("red:a"), now=100.0) is MonitorDecision.WAKE_ACTIONABLE

    # A second, different change well inside the floor is held.
    assert _decide(state, _actionable("red:b"), now=100.0 + _FLOOR * 0.3) is (
        MonitorDecision.RECORD_ONLY
    )

    # Once the window has aged past the floor, the held change fires.
    assert _decide(state, _actionable("red:b"), now=100.0 + _FLOOR * 1.1) is (
        MonitorDecision.WAKE_ACTIONABLE
    )


def test_a_head_change_opens_a_fresh_window_and_wakes_now():
    """A head change is a new subject state: it wakes at once, not held.

    A follow-up change is folded only within one head. When the head moves the
    window resets, so the first change on the new head wakes immediately the way
    the first change on any subject does.
    """
    state = _state()
    assert _decide(state, _actionable("red:a"), now=0.0) is MonitorDecision.WAKE_ACTIONABLE
    # Same head, rapid follow-up: held.
    assert _decide(state, _actionable("red:b"), now=1.0) is MonitorDecision.RECORD_ONLY
    # Head change: the new head's first change wakes now.
    assert _decide(state, _actionable("red:c", head_changed=True), now=2.0) is (
        MonitorDecision.WAKE_ACTIONABLE
    )
    # The head change dropped both earlier revision-scoped windows, so the new
    # head's condition is the only thing left waiting.
    assert state.coalesce_windows == {_revision("red:c"): 2.0}


def test_the_same_change_re_wakes_only_on_the_re_alert_interval():
    """An unresolved change re-wakes once its re-alert entry ages out.

    Level-triggered re-assertion: the same fingerprint stays masked inside the
    interval and re-wakes past it, so a persisting condition is re-reported
    rather than told once. ``decide_monitor`` reads the alert map; the caller
    stamps it on a wake, so this simulates that stamp between decisions.
    """
    state = _state()
    assert _decide(state, _actionable("red:a"), now=0.0) is MonitorDecision.WAKE_ACTIONABLE
    # The caller records the wake it was handed.
    state.last_wake_fingerprint = "red:a"
    state.coalesce_alerted[_revision("red:a")] = 0.0

    # Inside the interval: masked.
    assert _decide(state, _actionable("red:a"), now=_REALERT * 0.5) is MonitorDecision.NO_CHANGE
    # Past the interval: re-asserted.
    assert _decide(state, _actionable("red:a"), now=_REALERT * 1.1) is (
        MonitorDecision.WAKE_ACTIONABLE
    )


def test_a_future_re_alert_timestamp_reads_as_stale():
    """A re-alert time in the future does not suppress a wake forever.

    A clock rollback (or corrupt state) can leave a future timestamp; reading it
    as stale rather than fresh keeps it from silencing the subject permanently.
    """
    state = _state(coalesce_alerted={_revision("red:a"): 10_000.0})
    assert _decide(state, _actionable("red:a"), now=100.0) is MonitorDecision.WAKE_ACTIONABLE


def test_the_re_alert_map_is_pruned_unconditionally():
    """Entries past the re-alert interval are dropped on the next decision.

    The map lives on a durable per-loop record, so an entry past its interval
    suppresses nothing and is dropped rather than kept, bounding growth across
    restarts.
    """
    state = _state(coalesce_alerted={"old:1": 0.0, "old:2": 1.0})
    # A fresh unrelated change past the interval prunes the stale entries.
    _decide(state, _actionable("red:new"), now=_REALERT + 100.0)
    assert "old:1" not in state.coalesce_alerted
    assert "old:2" not in state.coalesce_alerted


def test_a_record_without_the_window_fields_loads_as_an_unopened_window():
    """A persisted record written before the window fields loads as no window.

    The loader takes the dataclass default for any absent field, so an old
    record shows an empty window map -- not a window opened at time zero -- and
    its first actionable change wakes at once like any first change.
    """
    raw = {
        "kind": "github_pull_request",
        "target": "owner/repo/pull/1",
        "objective": "review_ready",
        "created_ts": 0.0,
    }
    state = monitor_state_from_dict(raw)
    assert state.coalesce_windows == {}
    assert state.coalesce_alerted == {}
    assert _decide(state, _actionable("red:a"), now=100.0) is MonitorDecision.WAKE_ACTIONABLE


def test_the_retired_window_scalars_are_dropped_rather_than_carried():
    """A record holding the two scalars this version retired loses them.

    ``extra_fields`` is reserved for fields a NEWER version owns, and it is
    written back on every persist. Carrying a retired field there would keep two
    spellings of the coalescing window in the record forever, so the loader drops
    them and the window loads unopened.
    """
    raw = {
        "kind": "github_pull_request",
        "target": "owner/repo/pull/1",
        "objective": "review_ready",
        "created_ts": 0.0,
        "coalesce_fingerprint": "red:a",
        "coalesce_opened_at": 100.0,
    }
    state = monitor_state_from_dict(raw)
    assert state.extra_fields == {}
    assert state.coalesce_windows == {}


def test_a_non_dict_coalesce_windows_is_refused():
    """A wrong-typed window map is rejected at construction.

    Absent is legal and loads as an unopened window; present-but-wrong is not,
    because a decision reads and mutates the field and a malformed value would
    raise deep in a tick rather than at the boundary.
    """
    with pytest.raises(ValueError, match="coalesce_windows"):
        _state(coalesce_windows=["not", "a", "map"])


def test_a_non_numeric_coalesce_window_time_is_refused():
    """A wrong-typed window open time is rejected at construction."""
    with pytest.raises(ValueError, match="coalesce_windows"):
        _state(coalesce_windows={_revision("red:a"): "not-a-number"})


def test_a_non_dict_coalesce_alerted_is_refused():
    """A wrong-typed re-alert map is rejected at construction.

    A persisted ``coalesce_alerted: []`` is the shape that raised inside a tick
    when the map was iterated; refusing it at the boundary turns that into a
    clean rejection the loader quarantines.
    """
    with pytest.raises(ValueError, match="coalesce_alerted"):
        _state(coalesce_alerted=[])


def test_re_assertion_changes_only_what_the_dedup_guard_compares(monkeypatch):
    """The same actionable change re-asserts past its period, stays masked before.

    Constructed as a real wake leaves it: after waking "red:a" the caller sets
    last_wake_fingerprint and the window records the alert time. The dedup guard
    keeps its rule and position; only the value it compares is derived from the
    re-alert period, so within the period the raw fingerprint matches and it
    suppresses, and past the period a different value is presented and it does
    not. The composed proof through the real writers is in
    test_monitor_controller.py; this pins the derived-comparison in isolation.
    """
    realert = 50.0
    monkeypatch.setattr(models, "DEFAULT_MONITOR_REALERT_SECS", realert)
    # Post-wake state the caller leaves behind.
    state = _state(
        last_wake_fingerprint="red:a",
        coalesce_windows={_revision("red:a"): 1_000.0},
        coalesce_alerted={_revision("red:a"): 1_000.0},
    )

    # Inside the period: masked on the guard's own terms.
    assert (
        decide_monitor(state, _actionable("red:a"), now=1_000.0 + realert * 0.5).decision
        is MonitorDecision.NO_CHANGE
    )

    # Past the period: re-asserts. The caller stamps the alert time; decide only
    # reads the map to derive its comparison, so this asserts the decision alone.
    assert (
        decide_monitor(state, _actionable("red:a"), now=1_000.0 + realert * 1.1).decision
        is MonitorDecision.WAKE_ACTIONABLE
    )


def test_a_pre_upgrade_record_seeds_the_alert_time_from_its_last_wake():
    """A record with no re-alert map but a recorded wake is seeded from it.

    A monitor that already woke before this change persisted no coalesce_alerted.
    Reconstructing the alert time from the completion time of that wake means the
    monitor is not read as never-alerted and does not re-wake once on its first
    post-upgrade probe. The map is seeded only when it is absent; a present,
    empty map is a live monitor that has alerted nothing yet and is left alone.
    """
    woke = {
        "kind": "github_pull_request",
        "target": "owner/repo/pull/1",
        "objective": "review_ready",
        "created_ts": 0.0,
        "last_wake_fingerprint": "red:a",
        "last_completed_at": 5_000.0,
    }
    seeded = monitor_state_from_dict(woke)
    assert seeded.coalesce_alerted == {_revision("red:a"): 5_000.0}

    # Absent wake record: nothing to seed from, loads as an unopened window.
    fresh = {
        "kind": "github_pull_request",
        "target": "owner/repo/pull/1",
        "objective": "review_ready",
        "created_ts": 0.0,
    }
    assert monitor_state_from_dict(fresh).coalesce_alerted == {}

    # Present, empty map: a live monitor, left exactly as stored.
    empty = dict(woke, coalesce_alerted={})
    assert monitor_state_from_dict(empty).coalesce_alerted == {}


def test_a_persisted_pre_space_alert_map_is_adopted_into_the_revision_space():
    """A stored key carrying no key space still masks the condition it names.

    A bare key names a whole-subject fingerprint, which is exactly what the
    revision space holds, so the loader reads it as a revision-space key. Read
    literally instead, such a key matches nothing this version computes, and an
    armed monitor wakes a second time for a condition it has already reported.
    """
    raw = {
        "kind": "github_pull_request",
        "target": "owner/repo/pull/1",
        "objective": "review_ready",
        "created_ts": 0.0,
        "coalesce_alerted": {"red:a": 1_000.0},
    }
    state = monitor_state_from_dict(raw)
    assert state.coalesce_alerted == {_revision("red:a"): 1_000.0}
    # And the mask is live: the same subject stays suppressed inside the interval.
    assert _decide(state, _actionable("red:a"), now=1_000.0 + _REALERT * 0.5) is (
        MonitorDecision.NO_CHANGE
    )
