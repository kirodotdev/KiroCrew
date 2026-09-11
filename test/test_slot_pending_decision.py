"""Tests for the slot's ``pending_decision`` payload — a buried [OPTIONS:] ask.

The scenario this exists for: a monitor/goal loop's agent ends a turn with
``[OPTIONS: …]``, the user is away, and later loop cycles append option-less
replies on top. The composer chips derive from the NEWEST assistant row, so the
question is now invisible everywhere — the user returns and has to ask "what's
waiting on me?".

``pending_decision`` is the projection-derived answer: the newest assistant
turn carries no options, an earlier one still does, and no human row sits in
between. It is deliberately narrower than its neighbours — ``has_options`` is
the marker on the newest row (chips already visible, nothing owed a badge; that
boundary is pinned by test_slot_needs_input_status.py), and ``needs_input`` is
an unanswered question card. Automation rows (``nudge``, ``inject``) neither
answer nor carry the decision; only a live ``user`` row retires it.

Derived, not stored: every ``to_dict`` re-reads the transcript, so there is no
lifecycle to desynchronise. The only state is the dismiss tombstone, which
names the options row's ``ts``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard.state import DashboardState, _ChatSlot

OPTIONS_TURN = "CTR failed twice on flaky suites.\n\n[OPTIONS: Retry again | Check mainline | Stop]"
CYCLE_REPLY = "Cycle 12: board unchanged, still blocked on your call."


def _state(*slot_keys: str) -> DashboardState:
    """A partially-constructed DashboardState owning real slots.

    Matches the fixture style of test_slot_needs_input_status.py;
    ``push_slots_update`` is a mock so a dismissal can be asserted to have been
    PUSHED, not merely stored.
    """
    st = DashboardState.__new__(DashboardState)
    st._slots = {k: _ChatSlot(k) for k in slot_keys}
    st.push_slots_update = MagicMock()  # type: ignore[method-assign]
    st._log = MagicMock()
    return st


def _turn(slot: _ChatSlot, *rows: tuple[str, str]) -> _ChatSlot:
    """Append LIVE rows (broadcast=True), the shape a real turn produces."""
    for role, content in rows:
        slot.append(role, content, broadcast=True)
    return slot


# ── the boundary: visible chips are not a buried decision ──


def test_fresh_options_turn_is_not_pending() -> None:
    """Options on the NEWEST turn are the composer chips' job, not a badge."""
    slot = _turn(
        _ChatSlot("chat-1"),
        ("user", "which one?"),
        ("assistant", OPTIONS_TURN),
    )
    payload = slot.to_dict()
    assert payload["pending_decision"] is None
    assert payload["has_options"] is True


def test_plain_conversation_is_not_pending() -> None:
    """An ordinary option-less reply after a user row raises nothing."""
    slot = _turn(
        _ChatSlot("chat-1"),
        ("user", "do the thing"),
        ("assistant", "done, 3 files changed"),
    )
    payload = slot.to_dict()
    assert payload["pending_decision"] is None
    assert payload["has_options"] is False


def test_automation_rows_alone_do_not_bury() -> None:
    """A nudge/inject row without a newer assistant reply leaves chips live."""
    slot = _turn(
        _ChatSlot("chat-1"),
        ("assistant", OPTIONS_TURN),
        ("nudge", "[auto-nudge cycle 12]\ncheck the board"),
        ("inject", "[Cron notification] scanner tick"),
    )
    payload = slot.to_dict()
    # The newest CONVERSATIONAL row is still the options turn.
    assert payload["has_options"] is True
    assert payload["pending_decision"] is None


# ── the case the feature exists for ──


def test_option_less_reply_buries_the_decision() -> None:
    slot = _turn(
        _ChatSlot("chat-1"),
        ("assistant", OPTIONS_TURN),
        ("nudge", "[auto-nudge cycle 12]\ncheck the board"),
        ("assistant", CYCLE_REPLY),
    )
    payload = slot.to_dict()
    assert payload["has_options"] is False
    pending = payload["pending_decision"]
    assert pending is not None
    assert pending["options"] == ["Retry again", "Check mainline", "Stop"]
    assert "CTR failed twice" in pending["excerpt"]
    assert "[OPTIONS:" not in pending["excerpt"]
    assert pending["ts"], "the options row's ts names the decision for dismissal"


def test_user_row_after_the_marker_retires_it() -> None:
    """A human message between the marker and now IS the answer channel."""
    slot = _turn(
        _ChatSlot("chat-1"),
        ("assistant", OPTIONS_TURN),
        ("user", "Retry again"),
        ("assistant", CYCLE_REPLY),
    )
    assert slot.to_dict()["pending_decision"] is None


def test_tool_rows_between_do_not_retire() -> None:
    """Loop-cycle plumbing (tool activity, notices) is transparent to the scan."""
    slot = _turn(
        _ChatSlot("chat-1"),
        ("assistant", OPTIONS_TURN),
        ("nudge", "[auto-nudge cycle 12]\ngo"),
        ("tool_call", "gh pr checks 123"),
        ("tool_result", "all green"),
        ("assistant", CYCLE_REPLY),
    )
    assert slot.to_dict()["pending_decision"] is not None


def test_newer_options_turn_supersedes_older_one() -> None:
    """The newest unanswered marker wins; an older one is never resurrected."""
    slot = _turn(
        _ChatSlot("chat-1"),
        ("assistant", "First ask.\n\n[OPTIONS: Old A | Old B]"),
        ("assistant", "Second ask.\n\n[OPTIONS: New A | New B]"),
        ("assistant", CYCLE_REPLY),
    )
    pending = slot.to_dict()["pending_decision"]
    assert pending is not None
    assert pending["options"] == ["New A", "New B"]


def test_scan_gives_up_past_the_row_cap() -> None:
    """The backward scan is bounded; a decision buried deeper is dropped."""
    slot = _turn(_ChatSlot("chat-1"), ("assistant", OPTIONS_TURN))
    for i in range(160):
        slot.append("tool_result", f"tick {i}", broadcast=True)
    slot.append("assistant", CYCLE_REPLY, broadcast=True)
    assert slot.to_dict()["pending_decision"] is None


def test_excerpt_is_truncated() -> None:
    slot = _turn(
        _ChatSlot("chat-1"),
        ("assistant", ("x" * 500) + "\n\n[OPTIONS: A | B]"),
        ("assistant", CYCLE_REPLY),
    )
    pending = slot.to_dict()["pending_decision"]
    assert pending is not None
    assert len(pending["excerpt"]) <= 241  # 240 + ellipsis


# ── dismissal: a tombstone naming the message, not a state delete ──


def test_dismiss_silences_exactly_that_decision() -> None:
    st = _state("chat-1")
    slot = _turn(
        st._slots["chat-1"],
        ("assistant", OPTIONS_TURN),
        ("assistant", CYCLE_REPLY),
    )
    pending = slot.to_dict()["pending_decision"]
    assert pending is not None
    assert st.dismiss_pending_decision("chat-1", pending["ts"]) is True
    st.push_slots_update.assert_called()
    assert slot.to_dict()["pending_decision"] is None

    # A LATER options turn is a NEW decision with a new ts: it surfaces.
    slot.append("assistant", "Round two.\n\n[OPTIONS: Ship it | Hold]", broadcast=True)
    slot.append("assistant", CYCLE_REPLY, broadcast=True)
    revived = slot.to_dict()["pending_decision"]
    assert revived is not None
    assert revived["options"] == ["Ship it", "Hold"]


def test_dismiss_refuses_unknown_slot_and_blank_ts() -> None:
    st = _state("chat-1")
    assert st.dismiss_pending_decision("nope", "2026-01-01T00:00:00") is False
    assert st.dismiss_pending_decision("chat-1", "") is False
    st.push_slots_update.assert_not_called()


def test_stale_dismiss_cannot_unsilence_a_newer_dismissal() -> None:
    """The tombstone is monotonic: a delayed dismiss for superseded decision A,
    arriving after newer decision B was dismissed, must not overwrite B's
    tombstone -- otherwise B reappears even though the user silenced it."""
    st = _state("chat-1")
    slot = _turn(
        st._slots["chat-1"],
        ("assistant", OPTIONS_TURN),
        ("assistant", CYCLE_REPLY),
    )
    ts_a = slot.to_dict()["pending_decision"]["ts"]

    # Decision B supersedes A, and the user dismisses B.
    slot.append("assistant", "Round two.\n\n[OPTIONS: Ship it | Hold]", broadcast=True)
    slot.append("assistant", CYCLE_REPLY, broadcast=True)
    ts_b = slot.to_dict()["pending_decision"]["ts"]
    assert ts_a < ts_b, "row timestamps are ordered; supersession means newer"
    assert st.dismiss_pending_decision("chat-1", ts_b) is True
    assert slot.to_dict()["pending_decision"] is None

    # The delayed dismiss for A lands now: refused, and B stays silenced.
    assert st.dismiss_pending_decision("chat-1", ts_a) is False
    assert slot._decision_dismissed_ts == ts_b
    assert slot.to_dict()["pending_decision"] is None

    # Re-dismissing the SAME decision stays acknowledged (idempotent retry).
    assert st.dismiss_pending_decision("chat-1", ts_b) is True


def test_stale_dismiss_ordering_survives_mixed_timestamp_formats(monkeypatch) -> None:
    """The guard orders by transcript_sort_key, not string compare: a legacy
    naive row can sort lexically ABOVE a newer offset-aware row on a
    positive-offset host, and a string compare would then (a) let the stale
    dismiss overwrite the newer tombstone and (b) refuse the user's re-dismiss
    of the reappeared decision -- an un-clearable card. Freeze the local zone
    at +02:00 and pin both legs."""
    import time as _time

    import pytest

    if not hasattr(_time, "tzset"):  # Windows: TZ cannot repoint localtime
        pytest.skip("needs time.tzset to stage a +02:00 local zone")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    _time.tzset()
    try:
        st = _state("chat-1")
        slot = st._slots["chat-1"]
        # Aware row (current writer), then a naive legacy row that is OLDER in
        # real time yet lexically larger than the aware string.
        ts_naive_old = "2026-09-11T11:00:00.000001"  # 09:00 UTC at +02:00
        ts_aware_new = "2026-09-11T10:30:00.000001+00:00"  # 10:30 UTC -- newer
        assert ts_naive_old > ts_aware_new, "string order must invert here"

        # The newer (aware) decision was dismissed; the delayed dismiss for the
        # older naive one must be refused and must not touch the tombstone.
        assert st.dismiss_pending_decision("chat-1", ts_aware_new) is True
        assert st.dismiss_pending_decision("chat-1", ts_naive_old) is False
        assert slot._decision_dismissed_ts == ts_aware_new

        # Recovery leg: with the tombstone on the OLDER naive row (the state
        # a string-ordered overwrite would produce), dismissing the newer
        # aware row is accepted -- the guard cannot brick the current decision.
        slot._decision_dismissed_ts = ts_naive_old
        assert st.dismiss_pending_decision("chat-1", ts_aware_new) is True
        assert slot._decision_dismissed_ts == ts_aware_new

        # An unparseable recorded value never blocks a real dismissal.
        slot._decision_dismissed_ts = "not-a-timestamp"
        assert st.dismiss_pending_decision("chat-1", ts_aware_new) is True
    finally:
        monkeypatch.delenv("TZ", raising=False)
        _time.tzset()
