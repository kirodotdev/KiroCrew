"""Advisor output validation and emission guard.

Contract under test (see docs/system-specs/modules/advisor.md):

Output (`advisor/output.py`):
- The reviewer must return a strict machine-readable envelope
  (``version``, ``notes[]`` with ``severity``/``text``). Host validation
  rejects malformed output with a typed error; unvalidated reviewer prose is
  never injected into the primary session.
- Unknown severities, non-list notes, oversized note text, and unknown
  envelope versions are rejected.

Guard (`advisor/guard.py`):
- Normalized dedupe: a note repeating an already-admitted note in the same
  epoch is suppressed.
- Content-free suppression: empty or whitespace/punctuation-only notes never
  emit.
- Per-update non-blocker budget: at most N nits+concerns admitted per update;
  blockers are exempt from the budget.
- Severity escalation: a higher-severity duplicate replaces the admitted
  lower-severity note instead of being suppressed.
- Interruption cooldown: after an admitted blocker interrupts, further
  non-blocker notes are suppressed for the cooldown window; a new blocker is
  exempt.
- Epoch reset clears dedupe and cooldown state.
"""

from __future__ import annotations

import pytest

from kiro_crew.advisor.guard import EmissionGuard
from kiro_crew.advisor.output import (
    ENVELOPE_VERSION,
    NOTE_TEXT_MAX_CHARS,
    AdvisorNote,
    MalformedReviewerOutput,
    parse_reviewer_envelope,
)


def envelope(notes, version=ENVELOPE_VERSION):
    return {"version": version, "notes": notes}


class TestEnvelopeParsing:
    def test_valid_envelope_parses_notes(self):
        notes = parse_reviewer_envelope(
            envelope(
                [
                    {"severity": "nit", "text": "typo in comment"},
                    {"severity": "blocker", "text": "deletes prod table"},
                ]
            )
        )
        assert [n.severity for n in notes] == ["nit", "blocker"]
        assert all(isinstance(n, AdvisorNote) for n in notes)

    def test_empty_notes_list_is_valid(self):
        assert parse_reviewer_envelope(envelope([])) == []

    @pytest.mark.parametrize(
        "bad",
        [
            "free-form prose, not an envelope",
            {"notes": [{"severity": "nit", "text": "x"}]},  # missing version
            envelope("not-a-list"),
            envelope([{"severity": "catastrophic", "text": "x"}]),
            envelope([{"severity": "nit"}]),  # missing text
            envelope([{"text": "x"}]),  # missing severity
            envelope([], version=999),
            None,
            [],
        ],
    )
    def test_malformed_output_raises_typed_error(self, bad):
        with pytest.raises(MalformedReviewerOutput):
            parse_reviewer_envelope(bad)

    def test_oversized_note_text_is_rejected(self):
        with pytest.raises(MalformedReviewerOutput):
            parse_reviewer_envelope(
                envelope([{"severity": "nit", "text": "x" * (NOTE_TEXT_MAX_CHARS + 1)}])
            )

    def test_note_carries_optional_evidence_reference(self):
        notes = parse_reviewer_envelope(
            envelope(
                [
                    {
                        "severity": "concern",
                        "text": "unbounded retry loop",
                        "evidence": "src/retry.py:42",
                    }
                ]
            )
        )
        assert notes[0].evidence == "src/retry.py:42"


class TestGuardDedupe:
    def test_duplicate_note_is_suppressed_within_epoch(self):
        guard = EmissionGuard()
        note = AdvisorNote(severity="concern", text="unbounded retry loop")
        assert guard.admit(note) is not None
        assert guard.admit(note) is None

    def test_normalization_catches_whitespace_and_case_variants(self):
        guard = EmissionGuard()
        guard.admit(AdvisorNote(severity="nit", text="Missing  Null Check"))
        assert guard.admit(AdvisorNote(severity="nit", text="missing null check")) is None

    def test_content_free_note_never_emits(self):
        guard = EmissionGuard()
        for text in ("", "   ", "\n\t", "...", "!!"):
            assert guard.admit(AdvisorNote(severity="nit", text=text)) is None

    def test_epoch_reset_clears_dedupe(self):
        guard = EmissionGuard()
        note = AdvisorNote(severity="concern", text="same finding")
        guard.admit(note)
        guard.begin_epoch()
        assert guard.admit(note) is not None


class TestGuardBudget:
    def test_non_blocker_budget_is_enforced_per_update(self):
        guard = EmissionGuard(non_blocker_budget=2)
        admitted = [guard.admit(AdvisorNote(severity="nit", text=f"note {i}")) for i in range(4)]
        assert [a is not None for a in admitted] == [True, True, False, False]

    def test_blockers_are_exempt_from_budget(self):
        guard = EmissionGuard(non_blocker_budget=1)
        guard.admit(AdvisorNote(severity="nit", text="first"))
        assert guard.admit(AdvisorNote(severity="nit", text="second")) is None
        assert guard.admit(AdvisorNote(severity="blocker", text="real problem")) is not None

    def test_budget_resets_on_new_update(self):
        guard = EmissionGuard(non_blocker_budget=1)
        guard.admit(AdvisorNote(severity="nit", text="first"))
        guard.begin_update()
        assert guard.admit(AdvisorNote(severity="nit", text="fresh note")) is not None


class TestGuardEscalation:
    def test_higher_severity_duplicate_replaces_lower(self):
        guard = EmissionGuard()
        guard.admit(AdvisorNote(severity="nit", text="race in cache write"))
        escalated = guard.admit(AdvisorNote(severity="blocker", text="race in cache write"))
        assert escalated is not None
        assert escalated.severity == "blocker"

    def test_lower_severity_duplicate_stays_suppressed(self):
        guard = EmissionGuard()
        guard.admit(AdvisorNote(severity="blocker", text="race in cache write"))
        assert guard.admit(AdvisorNote(severity="nit", text="race in cache write")) is None


class TestGuardCooldown:
    def test_cooldown_after_blocker_suppresses_non_blockers(self):
        clock = [100.0]
        guard = EmissionGuard(cooldown_secs=60, clock=lambda: clock[0])
        guard.admit(AdvisorNote(severity="blocker", text="stop now"))
        guard.note_interruption()
        clock[0] += 10
        assert guard.admit(AdvisorNote(severity="nit", text="minor thing")) is None

    def test_new_blocker_is_exempt_from_cooldown(self):
        clock = [100.0]
        guard = EmissionGuard(cooldown_secs=60, clock=lambda: clock[0])
        guard.admit(AdvisorNote(severity="blocker", text="first blocker"))
        guard.note_interruption()
        clock[0] += 10
        assert guard.admit(AdvisorNote(severity="blocker", text="second blocker")) is not None

    def test_cooldown_expires(self):
        clock = [100.0]
        guard = EmissionGuard(cooldown_secs=60, clock=lambda: clock[0])
        guard.admit(AdvisorNote(severity="blocker", text="stop"))
        guard.note_interruption()
        clock[0] += 61
        assert guard.admit(AdvisorNote(severity="nit", text="minor")) is not None

    def test_epoch_reset_clears_cooldown(self):
        clock = [100.0]
        guard = EmissionGuard(cooldown_secs=60, clock=lambda: clock[0])
        guard.admit(AdvisorNote(severity="blocker", text="stop"))
        guard.note_interruption()
        guard.begin_epoch()
        assert guard.admit(AdvisorNote(severity="nit", text="minor")) is not None


class TestUnicodeContentAdmitted:
    """Round-26 (Opus): a note written entirely in a non-Latin script is
    real content -- the content gate must be Unicode-aware, not [0-9A-Za-z]."""

    def test_cjk_blocker_is_admitted(self):
        guard = EmissionGuard(non_blocker_budget=4, cooldown_secs=0.0)
        guard.begin_update()
        note = AdvisorNote(severity="blocker", text="この操作はデータを削除します", evidence="")
        assert guard.admit(note) is not None

    def test_punctuation_only_note_still_suppressed(self):
        guard = EmissionGuard(non_blocker_budget=4, cooldown_secs=0.0)
        guard.begin_update()
        note = AdvisorNote(severity="nit", text="!!! --- ...", evidence="")
        assert guard.admit(note) is None


class TestInterruptionCapPerEpoch:
    """A blocker interrupts the running turn; a goal-blind reviewer can be wrong
    about several in a row. The cooldown spaces interruptions out, the cap
    bounds how many one epoch can take at all: past it a blocker still reaches
    the user as a card without steering."""

    def test_cap_closes_after_n_interruptions_and_epoch_reopens_it(self):
        from kiro_crew.advisor.guard import DEFAULT_INTERRUPTION_CAP, EmissionGuard

        guard = EmissionGuard()
        for _ in range(DEFAULT_INTERRUPTION_CAP):
            assert guard.may_interrupt() is True
            guard.note_interruption()
        assert guard.may_interrupt() is False
        guard.begin_update()
        assert guard.may_interrupt() is False, "the cap is per epoch, not per update"
        guard.begin_epoch()
        assert guard.may_interrupt() is True
