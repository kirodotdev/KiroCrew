"""The activity projection's ``today`` / ``week`` counts.

Both reads that serve them -- the live fold's ``ActivityProjection.view`` and the
member-scoped ``scope_activity_view`` -- count through ``_recency_counts``, so the two
windows are pinned once on the helper and once on each caller's wiring. The clock is
the module's own ``datetime`` binding, frozen through ``ManualClock``.
"""

from __future__ import annotations

import pytest

from kiro_crew.eventlog import members_projections as mp
from kiro_crew.testing.clock import ManualClock

NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    clock = ManualClock(start=NOW)
    clock.install(monkeypatch, mp, time=False, datetime=True)
    return clock


def test_each_window_counts_only_what_is_younger_than_it() -> None:
    stamps = [NOW, NOW - DAY + 1, NOW - DAY, NOW - 7 * DAY + 1, NOW - 7 * DAY, None]
    assert mp._recency_counts(stamps, NOW) == (2, 4)


def test_the_live_view_counts_the_records_it_serves(frozen: ManualClock) -> None:
    recent = [
        {"ts": NOW - 60},
        {"ts": NOW - 3 * DAY},
        {"ts": NOW - 8 * DAY},
        {"ts": "not a time"},
    ]
    view = mp.ActivityProjection().view({"recent": recent})
    assert [r["ts"] for r in view["recent"]] == [NOW - 60, NOW - 3 * DAY, NOW - 8 * DAY]
    assert (view["today"], view["week"]) == (1, 2)


def test_the_member_scoped_view_counts_only_the_owners_records(frozen: ManualClock) -> None:
    view = {
        "recent": [
            {"member": "Ada", "ts": NOW - 60},
            {"member": "Ada", "ts": NOW - 3 * DAY},
            {"member": "ada", "ts": NOW - 60},
            {"member": "Ada", "ts": None},
        ],
        "today": 9,
        "week": 9,
    }
    scoped = mp.scope_activity_view(view, "Ada")
    assert [r["member"] for r in scoped["recent"]] == ["Ada", "Ada", "Ada"]
    assert (scoped["today"], scoped["week"]) == (1, 2)
