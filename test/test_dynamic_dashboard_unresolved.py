"""Why a fold field did not resolve, as ``DashboardFeed.read`` names it in ``unresolved``.

Split from ``test_dynamic_dashboard.py`` for the file-line cap; the feed and event
helpers are that module's own.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from test_dynamic_dashboard import manifest

from kiro_crew import dashboard_feed


class TestWhyAFieldDidNotResolve:
    def event(self, value: Any, revision: int, seq: int = 1) -> Any:
        return SimpleNamespace(revision=revision, seq=seq, value=value)

    def feed(self, *folds: str) -> dashboard_feed.DashboardFeed:
        made = dashboard_feed.DashboardFeed(slot="member-atlas")
        for name in folds:
            made._cells[name] = dashboard_feed._Cell()
        return made

    @pytest.fixture(autouse=True)
    def _no_earlier_reads(self) -> Any:
        """Each case starts on a process that has resolved nothing on this slot."""
        dashboard_feed.forget_resolved()
        yield
        dashboard_feed.forget_resolved()

    def test_an_unwritten_agentic_field_is_absent_but_not_unresolved(self) -> None:
        """ABSENT and UNRESOLVED are different facts, and only one is reported.

        A cell the crewmate has never written is empty; the page dims it, which is
        what `missing` is for, and its upkeep text already asks the crewmate to write
        it. Reporting it as unresolved would tell the crewmate on every turn that a
        binding is broken when nothing has yet produced a value.
        """
        made = self.feed("work")
        made._accept("work", self.event({"conductor": {"entries": 3}}, 5, seq=11))
        read = made.read(manifest())
        assert "open_items" in read.missing, "the unwritten cell is not marked absent"
        assert read.fields["entries"] == 3, "the fold-sourced field did not resolve"
        assert read.unresolved == {}, "an unwritten agentic field was reported unresolved"

    def test_a_fold_never_seen_is_unresolved_as_unseen(self) -> None:
        """A fold that has produced no value is named with that reason."""
        read = self.feed("work").read(manifest())
        assert read.unresolved == {"entries": dashboard_feed.FOLD_UNSEEN}
        assert "entries" in read.missing

    def test_a_field_that_resolved_before_and_not_now_is_an_older_value(self) -> None:
        """A path miss in a fold that HAS a value, after a hit, means the page still
        draws the old number -- also on a fresh feed whose fold has the new value."""
        made = self.feed("work")
        made._accept("work", self.event({"conductor": {"entries": 3}}, 5))
        assert made.read(manifest()).unresolved == {}
        made._accept("work", self.event({"nothing": {}}, 6))
        assert made.read(manifest()).unresolved == {"entries": dashboard_feed.OLDER_VALUE}
        fresh = self.feed("work")
        fresh._accept("work", self.event({"nothing": {}}, 6))
        assert fresh.read(manifest()).unresolved == {"entries": dashboard_feed.OLDER_VALUE}

    def test_a_cold_fold_after_a_hit_is_unseen_never_an_older_value(self) -> None:
        """A cell with no value on this read may only be cold (a rollover with no
        savepoint, an evicted cell, a prime still running). Calling that an older
        value would tell the crewmate to rebind a field that works."""
        made = self.feed("work")
        made._accept("work", self.event({"conductor": {"entries": 3}}, 5))
        assert made.read(manifest()).unresolved == {}
        cold = self.feed("work")
        assert cold.read(manifest()).unresolved == {"entries": dashboard_feed.FOLD_UNSEEN}

    def test_an_unresolved_field_is_named_with_its_path_reason(self) -> None:
        made = self.feed("work")
        made._accept("work", self.event({"nothing": {}}, 5))
        read = made.read(manifest())
        # The two agentic fields are missing too: no agentic fold value was passed,
        # and an agentic cell the crewmate has not written is not a zero.
        assert read.missing == ["open_items", "risk_note", "entries"]
        assert read.unresolved == {"entries": dashboard_feed.PATH_MISSING}
