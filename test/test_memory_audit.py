"""The Audit tab's event list: exact counts per filter, search, time range and paging."""

from __future__ import annotations

import pytest
from test_memory_edit import store  # noqa: F401  (the v1 / v2 store fixture)

from kiro_crew import memory_audit


def _event(store, event, memory_type, key, source, when, old=None, new=None):  # noqa: F811
    with store._db_lock:
        store.db.execute(
            "INSERT INTO memory_events (event_type, memory_type, memory_key, old_value, new_value, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event, memory_type, key, old, new, source, when),
        )
        store.db.commit()


@pytest.fixture
def events(store):  # noqa: F811
    with store._db_lock:
        store.db.execute("DELETE FROM memory_events")
        store.db.commit()
    rows = [
        (
            "create",
            "semantic",
            "user.editor",
            "consolidation:dashboard:chat-1",
            "2026-11-01T10:00:00.000000+00:00",
            None,
            "vim",
        ),
        (
            "update",
            "semantic",
            "user.editor",
            "user_explicit",
            "2026-11-02T10:00:00.000000+00:00",
            "vim",
            "helix",
        ),
        (
            "create",
            "semantic",
            "lesson.tabs",
            "consolidation:dashboard:chat-2",
            "2026-11-03T10:00:00.000000+00:00",
            None,
            "Use tabs",
        ),
        (
            "injection_blocked",
            "semantic",
            "user.bad",
            "consolidation:dashboard:chat-3",
            "2026-11-04T10:00:00.000000+00:00",
            None,
            "ignore previous",
        ),
        (
            "create",
            "episodic",
            "ep-1",
            "taskrunner",
            "2026-11-05T10:00:00.000000+00:00",
            None,
            "Shipped the build",
        ),
    ]
    for row in rows:
        _event(store, *row[:5], old=row[5], new=row[6])
    return store


def test_counts_are_exact_and_each_group_ignores_its_own_filter(events):
    body = memory_audit.list_events(events, {"event": ["create"]}, limit=2)
    assert body["total"] == 3
    assert len(body["events"]) == 2 and body["has_more"]
    assert body["facets"]["event"] == {"create": 3, "update": 1, "injection_blocked": 1}
    assert body["facets"]["kind"] == {"fact": 1, "directive": 1, "episode": 1}
    assert body["facets"]["source"] == {"consolidation": 2, "taskrunner": 1}


def test_kind_source_search_and_range_narrow_together(events):
    assert memory_audit.list_events(events, {"kind": ["directive"]})["total"] == 1
    assert memory_audit.list_events(events, {"source": ["consolidation"]})["total"] == 3
    # Search reads the key and both values.
    assert [
        e["memory_key"] for e in memory_audit.list_events(events, {"q": "helix"})["events"]
    ] == ["user.editor"]
    ranged = memory_audit.list_events(
        events, {"since": "2026-11-02T00:00:00Z", "until": "2026-11-04T00:00:00Z"}
    )
    assert ranged["total"] == 2
    assert ranged["facets"]["event"] == {"update": 1, "create": 1}


def test_pages_cover_every_event_once_in_either_order(events):
    newest = [e["id"] for e in memory_audit.list_events(events, {}, limit=2)["events"]]
    rest = [e["id"] for e in memory_audit.list_events(events, {}, limit=2, offset=2)["events"]]
    last = [e["id"] for e in memory_audit.list_events(events, {}, limit=2, offset=4)["events"]]
    assert len(set(newest + rest + last)) == 5
    oldest = memory_audit.list_events(events, {"order": "asc"}, limit=1)["events"][0]
    assert oldest["event_type"] == "create" and oldest["memory_key"] == "user.editor"
    assert oldest["kind"] == "fact"


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": ["lesson"]},
        {"since": "yesterday"},
        {"order": "up"},
        {"since": "2026-11-05T00:00:00Z", "until": "2026-11-01T00:00:00Z"},
    ],
)
def test_bad_filters_are_a_client_error(events, raw):
    with pytest.raises(memory_audit.AuditError):
        memory_audit.list_events(events, raw)
