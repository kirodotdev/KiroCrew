"""A reminder whose fireAt cannot be read must never reach the store.

The danger is not the one bad row. ``due_reminders`` re-parses every row on every
scheduler tick, so one unparsable instant raises for the whole scan -- and that
scan is what fires reminders AND break nudges. The app would go quiet with no
error surfaced to the user, and a restart would clear the evidence, because the
loader drops unreadable rows on the way in.

So there are two things worth pinning: that ``add`` refuses, and that a row which
somehow got in really does poison the scan (the second is what makes the first
matter, and without it a future refactor could "simplify" the check away).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from kiro_crew.apps.builtins.crew_companion.reminders import (
    Reminder,
    due_reminders,
    parse_iso,
    to_iso,
)
from kiro_crew.apps.builtins.crew_companion.store import CompanionStore


class Clock:
    """A fixed clock, matching the one the sibling store tests use."""

    def __init__(self, start: str = "2026-07-31T14:00:00") -> None:
        self.now = parse_iso(start)

    def __call__(self) -> datetime:
        return self.now


class TestAddRejectsAnUnreadableFireAt:
    @pytest.mark.parametrize(
        "bad",
        [
            "invalid",
            "tomorrow",
            "2026-13-45T99:99:99Z",   # shaped like ISO, not a real instant
            "",
            "   ",
            "1785996935",            # epoch seconds, not ISO
        ],
    )
    def test_it_is_refused(self, tmp_path, bad):
        s = CompanionStore(tmp_path, now=Clock())
        s.load()
        with pytest.raises(ValueError):
            s.add("stretch", bad)
        assert s.snapshot()["reminders"] == [], "nothing may be persisted"

    def test_a_good_one_still_goes_through(self, tmp_path):
        # The guard must not be so eager that it rejects what the renderer sends.
        s = CompanionStore(tmp_path, now=Clock())
        s.load()
        for good in (to_iso(Clock().now), "2026-08-06T07:30:00Z", "2026-08-06T07:30:00+02:00"):
            s.add("stretch", good)
        assert len(s.snapshot()["reminders"]) == 3


class TestWhyItMatters:
    def test_one_unreadable_row_breaks_the_whole_tick(self):
        """This is the blast radius the guard exists to prevent."""
        good = Reminder(id="a", text="ok", fire_at="2026-01-01T00:00:00Z", created_at="x")
        poisoned = replace(good, id="b", fire_at="invalid")
        clock = Clock()

        # a healthy set scans fine
        assert due_reminders([good], clock.now)

        # add one bad row and the scan itself raises -- not just for that row
        with pytest.raises(ValueError):
            due_reminders([good, poisoned], clock.now)
