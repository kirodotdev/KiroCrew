"""Tests for the AIDLC store and enriched dataclasses."""

from __future__ import annotations

import types

import pytest

from kiro_crew.aidlc.store import AidlcStore
from kiro_crew.task_models import Project, Task


@pytest.fixture
def store(tmp_path):
    return AidlcStore(str(tmp_path))


# ── Project CRUD ──


class TestProjectCRUD:
    def test_create_project(self, store):
        p = store.create_project("proj1", "desc1")
        assert p["id"]
        assert p["name"] == "proj1"
        assert p["description"] == "desc1"
        assert p["status"] == "active"
        assert p["created_at"] > 0
        assert p["updated_at"] > 0

    def test_list_projects(self, store, monkeypatch):
        # Hand the store a MONOTONIC counter instead of the wall clock: `time.time()`
        # returns the same value for ~60% of adjacent calls on Windows (the ~15.6ms
        # timer tick) and `sorted` is stable, so two back-to-back creates tie and the
        # newest-first assertion below sees insertion order instead. The ordering is
        # the subject, not the clock. Patched on `store` rather than `models`, because
        # store.py did `from .models import _now` and so reads its OWN binding.
        from kiro_crew.aidlc import store as store_mod

        stamps = iter(range(1, 100))
        monkeypatch.setattr(store_mod, "_now", lambda: float(next(stamps)))

        p1 = store.create_project("first")
        p2 = store.create_project("second")

        listed = store.list_projects()
        assert len(listed) == 2
        # sorted by updated_at desc → second first
        assert listed[0]["id"] == p2["id"]
        assert listed[1]["id"] == p1["id"]

    def test_list_projects_equal_timestamps(self, store, monkeypatch):
        # Coarse clocks (~15.6ms granularity on Windows) hand back-to-back
        # creates identical timestamps; the order must stay deterministic:
        # most recently created first.
        monkeypatch.setattr("kiro_crew.aidlc.store._now", lambda: 1000.0)
        projects = [store.create_project(f"proj-{i}") for i in range(3)]
        assert all(p["created_at"] == 1000.0 for p in projects)
        listed = store.list_projects()
        assert [p["id"] for p in listed] == [p["id"] for p in reversed(projects)]

    def test_list_projects_created_at_breaks_updated_at_tie(self, store, monkeypatch):
        # When updated_at ties but created_at differs (e.g. the clock stepped
        # backwards between creates), created_at decides — not insertion order.
        clock = {"t": 1000.0}
        monkeypatch.setattr("kiro_crew.aidlc.store._now", lambda: clock["t"])
        newer = store.create_project("created-later-clock")
        clock["t"] = 500.0
        older = store.create_project("created-earlier-clock")
        clock["t"] = 2000.0
        store.update_project(newer["id"], description="x")
        store.update_project(older["id"], description="x")
        listed = store.list_projects()
        assert all(p["updated_at"] == 2000.0 for p in listed)
        # created_at desc: 1000.0 before 500.0, overriding reverse insertion.
        assert [p["id"] for p in listed] == [newer["id"], older["id"]]

    def test_list_projects_tolerates_non_numeric_timestamp(self, store, monkeypatch):
        # The store file is hand-editable JSON; a stringified timestamp must
        # not make listing raise on a str/float key comparison.
        monkeypatch.setattr("kiro_crew.aidlc.store._now", lambda: 1000.0)
        good = store.create_project("good")
        bad = store.create_project("bad")
        bad["created_at"] = "2026-01-01T00:00:00"
        listed = store.list_projects()
        # The corrupt record sorts as timestamp 0 on the tiebreak, so the
        # intact record wins the equal-updated_at tie.
        assert [p["id"] for p in listed] == [good["id"], bad["id"]]

    def test_update_project(self, store):
        p = store.create_project("old")
        updated = store.update_project(p["id"], name="new")
        assert updated["name"] == "new"
        assert store.get_project(p["id"])["name"] == "new"

    def test_delete_project(self, store):
        p = store.create_project("doomed")
        store.delete_project(p["id"])
        assert store.get_project(p["id"]) is None

    def test_activity_logging(self, store):
        p = store.create_project("proj")
        # auto-logged 'created' activity
        activities = store.list_activities(project_id=p["id"])
        assert len(activities) == 1
        assert activities[0]["action"] == "created"
        # custom activity
        store.log_activity(p["id"], "task", "t1", action="completed")
        all_acts = store.list_activities(project_id=p["id"])
        assert len(all_acts) == 2
        # filter by target_type
        task_acts = store.list_activities(project_id=p["id"], target_type="task")
        assert len(task_acts) == 1
        assert task_acts[0]["action"] == "completed"

    def test_list_activities_equal_timestamps(self, store, monkeypatch):
        # Coarse clocks (~15.6ms granularity on Windows) hand a burst of
        # activities identical timestamps; the feed must stay deterministic:
        # distinct timestamps strictly newest-first, and a tied group most
        # recently logged first. Activity.timestamp's default_factory
        # captured _now at class definition, so patch the time module that
        # _now reads at call time (scoped to aidlc.models only).
        clock = {"t": 500.0}
        monkeypatch.setattr(
            "kiro_crew.aidlc.models.time", types.SimpleNamespace(time=lambda: clock["t"])
        )
        earlier = store.log_activity("p1", "task", "t-early", action="updated")
        clock["t"] = 1000.0
        acts = [store.log_activity("p1", "task", f"t{i}", action="updated") for i in range(3)]
        assert all(a["timestamp"] == 1000.0 for a in acts)
        listed = store.list_activities()
        assert [a["id"] for a in listed] == [a["id"] for a in reversed(acts)] + [earlier["id"]]

    def test_list_activities_limit_keeps_most_recent_of_tied_group(self, store, monkeypatch):
        # When a tied group is larger than limit, the slice must keep the
        # MOST RECENT activities, not the oldest members of the group.
        monkeypatch.setattr(
            "kiro_crew.aidlc.models.time", types.SimpleNamespace(time=lambda: 1000.0)
        )
        acts = [store.log_activity("p1", "task", f"t{i}", action="updated") for i in range(5)]
        assert all(a["timestamp"] == 1000.0 for a in acts)
        listed = store.list_activities(limit=2)
        assert [a["id"] for a in listed] == [acts[4]["id"], acts[3]["id"]]

    def test_comments(self, store):
        c = store.add_comment("project", "p1", "hello")
        assert store.list_comments("project", "p1") == [c]
        store.delete_comment(c["id"])
        assert store.list_comments("project", "p1") == []


# ── Enriched dataclasses ──


class TestEnrichedDataclasses:
    def test_task_new_fields(self):
        t = Task(
            index=0,
            title="t",
            description="d",
            priority="high",
            story_points=5,
            task_type="fix",
        )
        assert t.priority == "high"
        assert t.story_points == 5
        assert t.task_type == "fix"

    def test_project_new_fields(self):
        p = Project(
            spec_path="",
            spec_content="",
            mode="spec",
            source_spec="do stuff",
            skip_planning=True,
        )
        assert p.mode == "spec"
        assert p.source_spec == "do stuff"
        assert p.skip_planning is True
