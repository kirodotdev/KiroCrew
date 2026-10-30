"""Server-side creator filtering, search, paging and counts for the session list.

The query runs over real catalog rows: sessions are written to a real
``ConversationLog`` directory and listed through ``list_sessions``, then filtered
by ``query_sessions``.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from kiro_crew.history import ConversationLog
from kiro_crew.shared_crew import people
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import (
    AttributionAvailability,
    Principal,
    Provenance,
    SessionAttribution,
    SessionQuery,
)
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal
from kiro_crew.shared_crew.session_query import decode_cursor, encode_cursor, query_sessions

ALICE = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_SERVE)
BOB = tailnet_principal("bob@example.com", "b", Provenance.TAILSCALE_SERVE)


def _write_session(base, key: str, *, title: str, creator: Principal | None, mtime: float) -> None:
    meta: dict[str, Any] = {"_type": "metadata", "title": title}
    if creator is not None:
        meta["attribution"] = SessionAttribution(
            creator=creator, availability=AttributionAvailability.RECORDED, created_at=mtime
        ).to_dict()
    path = base / f"{key}.jsonl"
    row = {"role": "user", "content": "x", "ts": "t"}
    path.write_text(json.dumps(meta) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def catalog(tmp_path):
    base = tmp_path / "sessions"
    base.mkdir()
    # 5 by Alice, 3 by Bob, 2 legacy (no attribution), 1 machine transcript.
    t = 1_000_000.0
    for i in range(5):
        _write_session(base, f"dashboard_a{i}", title=f"alpha {i}", creator=ALICE, mtime=t + i)
    for i in range(3):
        _write_session(base, f"dashboard_b{i}", title=f"bravo {i}", creator=BOB, mtime=t + 10 + i)
    for i in range(2):
        _write_session(base, f"dashboard_l{i}", title=f"legacy {i}", creator=None, mtime=t + 20 + i)
    _write_session(base, "subagent_x", title="machine", creator=ALICE, mtime=t + 30)
    log = ConversationLog(base)
    return log


def _rows(log) -> list[dict]:
    return log.list_sessions()


def _machine(key: str) -> bool:
    return key.startswith("subagent_")


def _walk(rows, creator: str, viewer: Principal, limit: int, q: str = "") -> list[list[str]]:
    pages, cursor = [], ""
    while True:
        query = SessionQuery.from_query(
            {"creator": creator, "limit": str(limit), "cursor": cursor, "q": q}
        )
        page = query_sessions(rows, query, viewer, exclude=_machine)
        pages.append([i["key"] for i in page.items])
        cursor = page.next_cursor
        if not cursor:
            return pages


@pytest.mark.parametrize(
    "creator,viewer,expected",
    [
        ("all", ALICE, 10),
        ("mine", ALICE, 5),
        ("mine", BOB, 3),
        ("unknown", ALICE, 2),
        (BOB.id, ALICE, 3),
        ("mine", Principal.unknown(), 0),
    ],
    ids=["all", "mine-alice", "mine-bob", "unknown", "creator-id", "mine-unknown-viewer"],
)
def test_filter_counts_are_over_the_whole_catalog(catalog, creator, viewer, expected):
    query = SessionQuery.from_query({"creator": creator, "limit": "2"})
    page = query_sessions(_rows(catalog), query, viewer, exclude=_machine)
    assert page.total_matching == expected
    assert page.hidden_by_filter == 10 - expected
    assert len(page.items) == min(2, expected)


def test_paging_walk_returns_every_match_once_newest_first(catalog):
    pages = _walk(_rows(catalog), "mine", ALICE, limit=2)
    flat = [k for p in pages for k in p]
    assert flat == [f"dashboard_a{i}" for i in (4, 3, 2, 1, 0)]
    assert [len(p) for p in pages] == [2, 2, 1]


def test_search_combines_with_the_filter_before_paging(catalog):
    query = SessionQuery.from_query({"creator": "all", "q": "BRAVO", "limit": "50"})
    page = query_sessions(_rows(catalog), query, ALICE, exclude=_machine)
    assert {i["key"] for i in page.items} == {"dashboard_b0", "dashboard_b1", "dashboard_b2"}
    assert page.total_matching == 3 and page.hidden_by_filter == 0
    query = SessionQuery.from_query({"creator": "mine", "q": "bravo"})
    page = query_sessions(_rows(catalog), query, ALICE, exclude=_machine)
    assert page.total_matching == 0 and page.hidden_by_filter == 3


def test_a_session_updated_mid_walk_is_not_returned_twice(catalog, tmp_path):
    rows = _rows(catalog)
    q1 = SessionQuery.from_query({"creator": "mine", "limit": "2"})
    first = query_sessions(rows, q1, ALICE, exclude=_machine)
    # a0 (oldest) is touched between pages and moves to the top.
    os.utime(tmp_path / "sessions" / "dashboard_a0.jsonl", (2_000_000.0, 2_000_000.0))
    q2 = SessionQuery.from_query({"creator": "mine", "limit": "50", "cursor": first.next_cursor})
    second = query_sessions(_rows(catalog), q2, ALICE, exclude=_machine)
    seen = [i["key"] for i in first.items] + [i["key"] for i in second.items]
    assert len(seen) == len(set(seen))


def test_items_carry_public_attribution_only(catalog):
    page = query_sessions(_rows(catalog), SessionQuery(), ALICE, exclude=_machine)
    for item in page.items:
        creator = item["attribution"]["creator"]
        assert set(creator) == {"id", "kind", "display"}
    legacy = [i for i in page.items if i["key"].startswith("dashboard_l")]
    assert all(i["attribution"]["availability"] == "legacy_missing" for i in legacy)


@pytest.mark.parametrize("cursor", ["!!", "bm90LWEtY3Vyc29y", encode_cursor((1.0, "xyz"))[:-4]])
def test_a_malformed_cursor_is_refused(cursor):
    with pytest.raises(ContractError):
        if cursor == "!!":
            SessionQuery.from_query({"cursor": cursor})
        else:
            decode_cursor(cursor)


def test_a_cursor_round_trips():
    assert decode_cursor(encode_cursor((-12.5, "0123456789abcdef"))) == (-12.5, "0123456789abcdef")


# -- The routes ------------------------------------------------------------- #


def test_people_registry_is_bounded_and_drops_the_least_recently_seen():
    reg = people.ObservedPeople(max_entries=3, clock=iter(range(100)).__next__)
    ps = [local_operator_principal(f"user-{i}") for i in range(5)]
    for p in ps:
        reg.observe(p, "local")
    reg.observe(ps[2], "local")
    kept = [o.principal.id for o in reg.snapshot()]
    assert kept == [ps[2].id, ps[4].id, ps[3].id]


def test_people_usage_is_reported_only_when_a_source_is_wired():
    people.reset_for_tests()
    people.observe(ALICE, "node:a")
    people.register_usage_source(lambda: {ALICE.id: {"active": 1, "queued": 2}})
    try:
        body = people.people_view(ALICE, [])
    finally:
        people.reset_for_tests()
    assert body["usage_available"] is True
    assert body["people"][0]["usage"] == {"active": 1, "queued": 2}
