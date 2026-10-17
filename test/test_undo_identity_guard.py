"""The crewmate-create Undo guard compares the ``member_id`` the card wrote
against the live one UNDER THE SAME LOCK as the delete, and fails closed.

This module is only the vocabulary (the request key the parent arms and the
route reads) and the pure comparison helper. It reads no disk and holds no lock:
the route re-reads the live id inside its own already-held lock and hands both
values to :func:`identity_mismatch`. These exercise that helper directly and pin
the request-key string the parent and the route must agree on.
"""

from __future__ import annotations

from kiro_crew.dashboard.handlers import undo_identity_guard as guard

# ── the armed request key and the shared refusal outcome ──


def test_request_key_and_refusal_outcome_are_stable():
    # The parent (change_cards) arms this exact string and the route
    # (crew_removal) reads it; a drift between them silently disarms the guard.
    assert guard.REQ_CARD_UNDO_CREWMATE_EXPECT == "card_undo_crewmate_expect"
    # The refusal mirrors the schedule-create Undo guard so the dashboard renders
    # one outcome for every "changed since apply" refusal.
    assert guard.UNDO_IDENTITY_CHANGED_CODE == "changed_since_apply"
    assert "undo refused" in guard.UNDO_IDENTITY_CHANGED_MESSAGE


# ── pure comparison, fail-closed ──


def test_identity_mismatch_fails_closed_on_unknowns():
    # No evidence on either side is NOT a match: a member_id the card never
    # recorded, or one a locked read could not find now, cannot prove the crew on
    # disk is still the card's, so the destructive delete must be refused.
    assert guard.identity_mismatch(None, "m1") is True
    assert guard.identity_mismatch("m1", None) is True
    assert guard.identity_mismatch(None, None) is True


def test_identity_mismatch_matches_exact_member_id():
    # The same immutable id the card recorded → proceed; any other live id → the
    # name now wears a replacement crew, so refuse.
    assert guard.identity_mismatch("m1", "m1") is False
    assert guard.identity_mismatch("m1", "m2") is True
    # An empty live id is a replacement with no id, not the card's crew.
    assert guard.identity_mismatch("m1", "") is True


def test_identity_mismatch_matches_across_json_list_tuple():
    # A value carried through JSON arrives as a list; a value read back live may
    # be a tuple. The container type must not make an identical pair look changed.
    assert guard.identity_mismatch([1, 2], (1, 2)) is False
    assert guard.identity_mismatch((1, 2), [1, 2]) is False
    assert guard.identity_mismatch([1, 2], [1, 3]) is True
