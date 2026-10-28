"""The Shared Crew People view: observed principals and session creators."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.shared_crew import people
from kiro_crew.shared_crew.contracts import Principal, Provenance
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal

ALICE = tailnet_principal("alice@example.com", "alice-laptop", Provenance.TAILSCALE_SERVE)
BOB = tailnet_principal("bob@example.com", "bob-laptop", Provenance.TAILSCALE_SERVE)
CAROL = tailnet_principal("carol@example.com", "carol", Provenance.TAILSCALE_SERVE)


@pytest.fixture(autouse=True)
def _fresh_registry():
    people.reset_for_tests()
    yield
    people.reset_for_tests()


def _row(creator: Principal | None) -> dict[str, Any]:
    if creator is None:
        return {"attribution": None}
    return {"attribution": {"creator": creator.to_dict()}}


def _by_login(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["principal"]["display"]["login"]: row for row in view["people"]}


def test_unknown_and_system_principals_are_not_observed():
    people.observe(Principal.unknown(), "d1")
    people.observe(Principal.system("internal"), "d1")
    assert people.registry().snapshot() == []


def test_a_repeat_sighting_keeps_first_seen_and_counts_distinct_devices():
    now = [100.0]
    reg = people.reset_for_tests(clock=lambda: now[0])
    people.observe(ALICE, "laptop")
    now[0] = 200.0
    people.observe(ALICE, "phone")
    people.observe(ALICE, "phone")
    people.observe(ALICE, "")
    (obs,) = reg.snapshot()
    assert (obs.first_seen, obs.last_seen, len(obs.devices)) == (100.0, 200.0, 2)


def test_the_registry_drops_the_least_recently_seen_principal_past_its_bound():
    reg = people.ObservedPeople(max_entries=2)
    reg.observe(ALICE)
    reg.observe(BOB)
    reg.observe(ALICE)
    reg.observe(CAROL)
    assert [o.principal.id for o in reg.snapshot()] == [CAROL.id, ALICE.id]


def test_the_view_joins_observed_people_with_session_creators():
    people.observe(ALICE, "laptop")
    rows = [_row(ALICE), _row(BOB), _row(BOB), _row(None), _row(Principal.system("internal"))]
    view = people.people_view(ALICE, rows)
    found = _by_login(view)
    assert set(found) == {"alice@example.com", "bob@example.com"}
    alice, bob = found["alice@example.com"], found["bob@example.com"]
    assert alice["sources"] == [people.SOURCE_OBSERVED, people.SOURCE_SESSION_CREATOR]
    assert alice["is_viewer"] is True and alice["devices"] == 1
    assert bob["sources"] == [people.SOURCE_SESSION_CREATOR]
    assert bob["sessions_created"] == 2 and bob["first_seen_at"] is None
    assert view["directory"] == {"state": "unavailable"}
    assert view["usage_available"] is False and alice["usage"] is None


def test_an_unknown_viewer_is_never_marked_as_a_row():
    people.observe(ALICE)
    view = people.people_view(Principal.unknown(), [])
    assert [row["is_viewer"] for row in view["people"]] == [False]


def test_usage_is_reported_when_a_source_is_wired_and_unavailable_when_it_fails():
    people.observe(ALICE)
    people.register_usage_source(lambda: {ALICE.id: {"active": 1, "queued": 2}})
    view = people.people_view(local_operator_principal("local-app"), [_row(BOB)])
    found = _by_login(view)
    assert view["usage_available"] is True
    assert found["alice@example.com"]["usage"] == {"active": 1, "queued": 2}
    assert found["bob@example.com"]["usage"] == {"active": 0, "queued": 0}

    def _broken() -> dict[str, dict[str, int]]:
        raise RuntimeError("usage reader down")

    people.register_usage_source(_broken)
    view = people.people_view(ALICE, [])
    assert view["usage_available"] is False
    assert view["people"][0]["usage"] is None
