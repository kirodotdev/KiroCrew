"""An append to a member's log must not cost the member's whole history.

A member log is never compacted, so a parse of the whole file on every append
makes each append slower for the life of the member. These tests count the
records the store decodes while an append runs.
"""

from __future__ import annotations

import pytest
from eventlog_helpers import _isolated_home  # noqa: F401 - autouse fixture

from kiro_crew.crew_log import lease
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.crew_log.store import CrewLog
from kiro_crew.eventlog import log as log_mod
from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import MemberLog
from kiro_crew.eventlog.service import MemberEventLogService

HISTORY = 40


@pytest.fixture
def decoded(monkeypatch):
    """How many records ``CrewLog.iter_from`` has yielded since the fixture started."""
    seen = {"records": 0}
    real = CrewLog.iter_from

    def counting(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            seen["records"] += 1
            yield entry

    monkeypatch.setattr(CrewLog, "iter_from", counting)
    return seen


def _seeded(slug: str) -> MemberLog:
    log = MemberLog(slug)
    log.create(slug.title())
    for i in range(HISTORY):
        log.append(types.ACTIVITY_RECORD, {"ts": i, "via": "cli", "project": f"p{i}"})
    return log


def _seqs(log: MemberLog) -> list[int]:
    return [event["seq"] for event in log.events]


def test_an_append_decodes_no_history(decoded):
    log = _seeded("alice")
    decoded["records"] = 0

    for i in range(5):
        log.append(types.ACTIVITY_RECORD, {"ts": 100 + i, "via": "cli", "project": "new"})

    assert decoded["records"] == 0, "an append re-read the member's history"
    fresh = MemberLog("alice")
    fresh.load()
    assert log.events == fresh.events
    assert log.retained_from == fresh.retained_from == 0
    assert not log.refresh_if_changed(), "the stamp must describe the events held"


def test_an_append_still_releases_the_write_lease():
    log = _seeded("bob")
    lease_path = log.path.parent / lease.LEASE_FILE
    key = lease.acquire(lease_path, kind=KIND_MEMBER, unit_id="bob", sole=True)
    lease.release(key)


def test_another_writers_events_are_not_skipped(decoded):
    """The hole the reload exists to prevent: a second writer committed first."""
    log = _seeded("carol")
    other = MemberLog("carol")  # stands in for the ``kirocrew-core`` process
    stranger = other.append(types.ACTIVITY_RECORD, {"ts": 500, "via": "cli", "project": "theirs"})

    ours = log.append(types.ACTIVITY_RECORD, {"ts": 501, "via": "cli", "project": "ours"})

    assert ours["seq"] == stranger["seq"] + 1
    assert _seqs(log) == list(range(1, HISTORY + 3))
    assert decoded["records"] >= HISTORY, "a gap in seq must be answered from disk"


def test_the_window_moves_as_a_full_load_would(monkeypatch):
    monkeypatch.setattr(log_mod, "MAX_RETAINED_EVENTS", 5)
    log = _seeded("dave")

    fresh = MemberLog("dave")
    fresh.load()
    assert _seqs(log) == _seqs(fresh) == list(range(HISTORY - 4, HISTORY + 1))
    assert log.retained_from == fresh.retained_from == HISTORY - 4
    # Below the window the answer comes from the store, not from the short tail.
    assert [e["seq"] for e in log.iter_held_events_after(10)] == list(range(11, HISTORY + 1))
    assert [e["seq"] for e in log.iter_held_events_after(HISTORY - 2)] == [HISTORY - 1, HISTORY]


def test_a_service_append_decodes_no_history(tmp_path, monkeypatch, decoded):
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir()
    svc = MemberEventLogService(root)
    svc.ensure("erin", "Erin")
    for i in range(HISTORY):
        svc.append("erin", types.SLOT_OPENED, {"slot_key": f"slot-{i}"})
    decoded["records"] = 0

    svc.ensure("erin", "Erin")
    svc.append("erin", types.SLOT_OPENED, {"slot_key": "one-more"})

    assert decoded["records"] == 0, "the append or its fold re-read the member's history"
    open_slots = svc.snapshot("erin")["values"][types.PROJ_DRIVING]["open"]
    assert "one-more" in open_slots and len(open_slots) == HISTORY + 1
