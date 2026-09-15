"""Handover digest: the paths only reached when several buckets are live at once.

``test_handover.py`` covers what the digest emphasizes. What it never exercises is a
shift change that is actually messy: work stopped with no diagnosis *and* work handed
to another owner *and* known recurring patterns, all in one digest. Those clauses of
the headline and the corresponding blocks of the pasted plain text only appear then —
and the plain text is what an agent drops into a handover thread, so a section that
silently never renders is the section the incoming responder never reads.

Also covered here: the two "nothing to say" arcs — autonomy outside observe mode (no
provider-write caveat) and full coverage (no blind-spot line).
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
    MODE_ACT,
    MODE_OBSERVE,
    STATUS_ESCALATED,
    STATUS_INVESTIGATING,
    STATUS_NEEDS_HUMAN,
    LedgerEntry,
    Signal,
)


def _all_watching() -> list[dict[str, Any]]:
    """Every signal source configured, so the digest has no blind spot to report."""
    return [
        {
            "id": "cloudwatch",
            "display_name": "AWS CloudWatch",
            "roles": ["signal"],
            "configured": True,
        },
        {
            "id": "pagerduty",
            "display_name": "PagerDuty",
            "roles": ["signal", "rotation"],
            "configured": True,
        },
    ]


_ACT_ROTATION: dict[str, Any] = {"mode": MODE_ACT, "rules": 3, "on_shift": True}


class _DataHome(unittest.TestCase):
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
    def _stalled(native_id: str = "alarm/stalled") -> Any:
        """Needs a human, with neither a blocked reason nor a diagnosis to pick up."""
        signal = Signal.create(source="cloudwatch", native_id=native_id, title="Disk full")
        inc = store.claim(signal, operating_mode=MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, STATUS_NEEDS_HUMAN)
        return inc

    @staticmethod
    def _escalated(native_id: str = "alarm/escalated") -> Any:
        signal = Signal.create(source="cloudwatch", native_id=native_id, title="Handed off")
        inc = store.claim(signal, operating_mode=MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, STATUS_INVESTIGATING)
        store.transition(inc.incident_id, STATUS_ESCALATED)
        return inc

    @staticmethod
    def _pattern(
        pattern: str,
        fix: str,
        uses: int,
        *,
        misses: int = 0,
        trust: str = "observed",
        confidence: str = "medium",
    ) -> str:
        entry = LedgerEntry.create(pattern=pattern, fix=fix, confidence=confidence, trust=trust)
        stored = ledger.upsert(entry)
        for _ in range(uses):
            ledger.record_use(stored.entry_id)
        for _ in range(misses):
            ledger.record_miss(stored.entry_id)
        return stored.entry_id


class TestHeadlineOnAMessyShift(_DataHome):
    def test_stalled_and_escalated_work_are_both_named(self) -> None:
        stalled = self._stalled()
        self._escalated()

        digest = handover.build(_all_watching(), _ACT_ROTATION)

        self.assertIn("1 stopped with no diagnosis", digest["headline"])
        self.assertIn("1 escalated", digest["headline"])
        self.assertTrue(digest["headline"].startswith("Start here:"))
        self.assertEqual(
            [row["id"] for row in digest["open_work"]["stalled_without_diagnosis"]],
            [stalled.incident_id],
        )

    def test_autonomy_caveat_is_absent_outside_observe_mode(self) -> None:
        """In act mode the provider-write caveat would be false, so it must not appear."""
        digest = handover.build(_all_watching(), _ACT_ROTATION)
        self.assertNotIn("Autonomy is observe", digest["headline"])
        self.assertEqual(digest["autonomy"]["mode"], MODE_ACT)
        self.assertEqual(digest["autonomy"]["rules"], 3)

    def test_headline_counts_known_recurring_patterns(self) -> None:
        self._pattern("disk fills after log rotation", "extend the volume", 4)

        digest = handover.build(_all_watching(), _ACT_ROTATION)

        self.assertIn("1 recurring pattern(s) known", digest["headline"])


class TestRenderedTextSections(_DataHome):
    def test_every_section_renders_when_every_bucket_is_live(self) -> None:
        stalled = self._stalled()
        escalated = self._escalated()
        self._pattern("refuted thing", "restart the daemon", 5, misses=2)
        self._pattern("sure thing", "extend the volume", 3, trust="verified", confidence="high")

        text = handover.render_text(handover.build(_all_watching(), _ACT_ROTATION))

        self.assertIn("Stopped with no diagnosis (needs a restart, not an answer):", text)
        self.assertIn(stalled.incident_id, text)
        self.assertIn("Escalated:", text)
        self.assertIn(escalated.incident_id, text)
        self.assertIn("What keeps happening (most frequent first):", text)
        self.assertIn("fix: restart the daemon", text)
        self.assertIn("fix: extend the volume", text)

    def test_a_refuted_fix_is_marked_in_the_pasted_text(self) -> None:
        """The thread is where someone reaches for the top fix; misses must show there."""
        self._pattern("refuted thing", "restart the daemon", 5, misses=2)

        text = handover.render_text(handover.build(_all_watching(), _ACT_ROTATION))

        self.assertIn("failed 2\u00d7", text)
        self.assertIn("5\u00d7", text)

    def test_a_proven_fix_is_marked_proven_not_by_trust_labels(self) -> None:
        self._pattern("sure thing", "extend the volume", 3, trust="verified", confidence="high")

        text = handover.render_text(handover.build(_all_watching(), _ACT_ROTATION))

        self.assertIn("[proven]", text)
        self.assertNotIn("verified/high", text)

    def test_no_blind_spot_line_when_everything_is_configured(self) -> None:
        text = handover.render_text(handover.build(_all_watching(), _ACT_ROTATION))

        self.assertIn("Watching: AWS CloudWatch, PagerDuty", text)
        self.assertNotIn("Not configured", text)


if __name__ == "__main__":
    unittest.main()
