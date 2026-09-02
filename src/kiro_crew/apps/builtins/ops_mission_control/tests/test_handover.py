"""Tests for the shift handover digest.

The digest's job is to be *read*, so the properties that matter are about what it
emphasizes and what it refuses to imply:

1. **No coverage beats everything.** A board with nothing configured looks calm, and
   telling the incoming responder "all quiet" would be actively misleading.
2. **Waiting-on-a-person is the lede.** That is the one class of work that does not
   progress across a shift change.
3. **Unproven patterns are not presented as answers.** A digest that flattens
   `observed/medium` into "the fix" gets someone to apply the wrong thing confidently.
4. **It stores nothing.** A cached handover goes stale between shifts, which is worse
   than none.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import handover, ledger, store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    MODE_OBSERVE,
    STATUS_ESCALATED,
    STATUS_INVESTIGATING,
    STATUS_NEEDS_HUMAN,
    LedgerEntry,
    Signal,
)


def _providers(*, cloudwatch_ready: bool = True, pagerduty_ready: bool = False) -> list[dict]:
    return [
        {
            "id": "cloudwatch",
            "display_name": "AWS CloudWatch",
            "roles": ["signal"],
            "configured": cloudwatch_ready,
        },
        {
            "id": "pagerduty",
            "display_name": "PagerDuty",
            "roles": ["signal", "rotation"],
            "configured": pagerduty_ready,
        },
        # Non-signal roles must not count as coverage.
        {"id": "noop", "display_name": "Observe only", "roles": ["action"], "configured": True},
    ]


_ROTATION: dict[str, Any] = {"mode": MODE_OBSERVE, "rules": 0, "on_shift": True}


class _Env(unittest.TestCase):
    """Isolated data home (tests under src/ get no conftest fixture)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _claim(native_id: str = "alarm/a", title: str = "Disk full") -> Any:
        signal = Signal.create(source="cloudwatch", native_id=native_id, title=title)
        return store.claim(signal, operating_mode=MODE_OBSERVE)

    @staticmethod
    def _entry(pattern: str, fix: str, uses: int, *, trust: str, confidence: str) -> None:
        entry = LedgerEntry.create(pattern=pattern, fix=fix, confidence=confidence, trust=trust)
        stored = ledger.upsert(entry)
        for _ in range(uses):
            ledger.record_use(stored.entry_id)


class TestCoverage(_Env):
    def test_no_configured_source_is_the_headline(self) -> None:
        """A quiet board with nothing watching must not read as 'all quiet'."""
        digest = handover.build(_providers(cloudwatch_ready=False), _ROTATION)
        self.assertFalse(digest["coverage"]["any_watching"])
        self.assertIn("nothing is being watched", digest["headline"])

    def test_blind_spots_are_named_not_counted(self) -> None:
        digest = handover.build(_providers(), _ROTATION)
        self.assertEqual(digest["coverage"]["watching"], ["AWS CloudWatch"])
        self.assertIn("PagerDuty", digest["coverage"]["not_configured"])

    def test_non_signal_roles_are_not_coverage(self) -> None:
        """An action sink being configured does not mean anything is being watched."""
        digest = handover.build(_providers(cloudwatch_ready=False), _ROTATION)
        self.assertNotIn("Observe only", digest["coverage"]["watching"])
        self.assertFalse(digest["coverage"]["any_watching"])


class TestOpenWork(_Env):
    def test_waiting_on_you_leads_the_headline(self) -> None:
        inc = self._claim()
        assert inc is not None
        store.update_fields(inc.incident_id, blocked_reason="awaiting_approval")
        store.transition(inc.incident_id, STATUS_NEEDS_HUMAN)

        digest = handover.build(_providers(), _ROTATION)
        self.assertEqual(len(digest["open_work"]["waiting_on_you"]), 1)
        self.assertIn("waiting on you", digest["headline"])

    def test_stalled_without_diagnosis_is_its_own_bucket(self) -> None:
        """Needs-human with no reason AND no diagnosis needs a restart, not an answer."""
        inc = self._claim()
        assert inc is not None
        store.transition(inc.incident_id, STATUS_NEEDS_HUMAN)

        work = handover.open_work()
        self.assertEqual(len(work["stalled_without_diagnosis"]), 1)
        self.assertEqual(len(work["waiting_on_you"]), 0)

    def test_a_diagnosed_incident_is_not_stalled(self) -> None:
        inc = self._claim()
        assert inc is not None
        store.transition(inc.incident_id, STATUS_NEEDS_HUMAN, diagnosis="root cause found")
        self.assertEqual(handover.open_work()["stalled_without_diagnosis"], [])

    def test_escalated_is_surfaced_despite_being_terminal(self) -> None:
        """Escalated is terminal, so it is absent from `open_incidents` by design.

        It still belongs in a handover — "we passed this to another owner" is exactly
        what gets lost at shift change — so it is read from the index instead.
        """
        inc = self._claim()
        assert inc is not None
        store.transition(inc.incident_id, STATUS_INVESTIGATING)
        store.transition(inc.incident_id, STATUS_ESCALATED)

        self.assertEqual(store.open_incidents(), [], "escalated must not be open work")
        self.assertEqual(len(handover.open_work()["escalated"]), 1)

    def test_escalated_does_not_make_progressing_negative(self) -> None:
        """`progressing` is a remainder over OPEN work, and escalated is not open.

        Subtracting it (as the first version did) undercounts and goes negative once
        more than one incident has been escalated.
        """
        for n in range(3):
            inc = self._claim(f"alarm/esc{n}")
            assert inc is not None
            store.transition(inc.incident_id, STATUS_INVESTIGATING)
            store.transition(inc.incident_id, STATUS_ESCALATED)

        work = handover.open_work()
        self.assertEqual(len(work["escalated"]), 3)
        self.assertEqual(work["total_open"], 0)
        self.assertGreaterEqual(work["progressing"], 0)

    def test_quiet_shift_says_so(self) -> None:
        digest = handover.build(_providers(), _ROTATION)
        self.assertIn("Nothing is waiting on you", digest["headline"])

    def test_buckets_do_not_double_count(self) -> None:
        """`progressing` is a remainder, so overlapping buckets would make it negative."""
        blocked = self._claim("alarm/a")
        working = self._claim("alarm/b")
        assert blocked is not None and working is not None
        store.update_fields(blocked.incident_id, blocked_reason="awaiting_input")
        store.transition(blocked.incident_id, STATUS_NEEDS_HUMAN)
        store.transition(working.incident_id, STATUS_INVESTIGATING)

        work = handover.open_work()
        self.assertEqual(work["total_open"], 2)
        self.assertGreaterEqual(work["progressing"], 0)


class TestRecurringPatterns(_Env):
    def test_ranked_by_use_count(self) -> None:
        self._entry("rare thing", "fix a", 2, trust="observed", confidence="medium")
        self._entry("common thing", "fix b", 9, trust="verified", confidence="high")

        patterns = handover.recurring_patterns()
        self.assertEqual(patterns[0]["pattern"], "common thing")
        self.assertEqual(patterns[0]["uses"], 9)

    def test_used_once_is_not_a_pattern(self) -> None:
        """One occurrence is an incident; the digest is about what RECURS."""
        self._entry("one-off", "fix", 1, trust="observed", confidence="medium")
        self.assertEqual(handover.recurring_patterns(), [])

    def test_unproven_entries_are_not_marked_proven(self) -> None:
        """Flattening trust would get someone to apply the wrong fix confidently."""
        self._entry("maybe", "fix", 3, trust="observed", confidence="medium")
        self.assertFalse(handover.recurring_patterns()[0]["proven"])

    def test_proven_matches_the_ledger_fast_path_definition(self) -> None:
        """The digest must not disagree with the engine about what counts as proven."""
        self._entry("sure thing", "fix", 3, trust="verified", confidence="high")
        entry = handover.recurring_patterns()[0]
        self.assertTrue(entry["proven"])
        stored = [e for e in ledger.read_entries() if e.pattern == "sure thing"]
        self.assertTrue(ledger.is_fast_path(stored))

    def test_list_is_capped(self) -> None:
        for n in range(handover.MAX_PATTERNS + 5):
            self._entry(f"pattern {n}", "fix", 3, trust="observed", confidence="medium")
        self.assertLessEqual(len(handover.recurring_patterns()), handover.MAX_PATTERNS)

    def test_long_text_is_clipped(self) -> None:
        self._entry("p" * 2000, "f" * 2000, 3, trust="observed", confidence="medium")
        row = handover.recurring_patterns()[0]
        self.assertLessEqual(len(row["pattern"]), handover.MAX_TEXT_CHARS)
        self.assertLessEqual(len(row["fix"]), handover.MAX_TEXT_CHARS)


class TestRenderText(_Env):
    def test_text_leads_with_the_headline(self) -> None:
        digest = handover.build(_providers(), _ROTATION)
        text = handover.render_text(digest)
        self.assertTrue(text.startswith("Shift handover"))
        self.assertIn(digest["headline"], text)

    def test_text_names_blocked_incidents_and_their_reason(self) -> None:
        inc = self._claim()
        assert inc is not None
        store.update_fields(inc.incident_id, blocked_reason="awaiting_approval")
        store.transition(inc.incident_id, STATUS_NEEDS_HUMAN)

        text = handover.render_text(handover.build(_providers(), _ROTATION))
        self.assertIn("Waiting on you:", text)
        self.assertIn(inc.incident_id, text)
        # Underscores are a wire format, not something a responder should read.
        self.assertIn("awaiting approval", text)

    def test_text_reports_blind_spots(self) -> None:
        text = handover.render_text(handover.build(_providers(), _ROTATION))
        self.assertIn("Not configured (blind spots)", text)
        self.assertIn("PagerDuty", text)

    def test_observe_mode_is_stated(self) -> None:
        """Autonomy is the difference between 'it will fix things' and 'it will tell you'."""
        digest = handover.build(_providers(), _ROTATION)
        self.assertIn("observe", digest["headline"])


class TestReadOnly(_Env):
    def test_building_a_digest_writes_nothing(self) -> None:
        """A cached or persisted handover goes stale between shifts."""
        inc = self._claim()
        assert inc is not None
        before = sorted(p.name for p in self.tmp.rglob("*") if p.is_file())

        handover.build(_providers(), _ROTATION)
        handover.render_text(handover.build(_providers(), _ROTATION))

        after = sorted(p.name for p in self.tmp.rglob("*") if p.is_file())
        self.assertEqual(before, after)

    def test_digest_does_not_increment_ledger_use_counts(self) -> None:
        """Reading the digest must not look like the pattern was used again."""
        self._entry("thing", "fix", 4, trust="observed", confidence="medium")
        first = handover.recurring_patterns()[0]["uses"]
        handover.recurring_patterns()
        self.assertEqual(handover.recurring_patterns()[0]["uses"], first)


if __name__ == "__main__":
    unittest.main()
