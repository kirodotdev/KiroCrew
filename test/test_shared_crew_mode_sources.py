"""The mode body's ``sources``: every connection source at once, each with its
state and how many people came in through it (``crew_mode.sources_view``), and
the mode route reading the Kiro relay's team binding."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
from aiohttp import web
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.dashboard.kiro_relay.caller import CrewKind, HostBinding
from kiro_crew.dashboard.kiro_relay.ingress import RELAY_INGRESS_APP_KEY
from kiro_crew.shared_crew import crew_mode, routes_identity
from kiro_crew.shared_crew.contracts import Principal, Provenance
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal

pytestmark = pytest.mark.usefixtures("host_isolation")

ALICE = tailnet_principal("alice@example.com", "alice-laptop", Provenance.TAILSCALE_SERVE)
BOB = tailnet_principal("bob@example.com", "bob-laptop", Provenance.TAILSCALE_LISTENER)
DEVON = dataclasses.replace(
    tailnet_principal("devon", "d", Provenance.TAILSCALE_SERVE),
    id="p_devon_ssm",
    provenance=Provenance.SSM_INVITE,
)
RELAY_ANA = dataclasses.replace(
    tailnet_principal("ana@example.com", "a", Provenance.TAILSCALE_SERVE),
    id="p_relay_ana",
    provenance=Provenance.KIRO_RELAY,
)
OWNER = local_operator_principal("local-app")
BOTH_ON = {
    "tailscale": {"state": "available", "reason": ""},
    "ssm": {"state": "available", "reason": ""},
    "kiro_login": {"state": "unavailable", "reason": "not_in_this_build"},
}
NONE_ON = {
    "tailscale": {"state": "unavailable", "reason": "not_configured"},
    "ssm": {"state": "unavailable", "reason": "not_configured"},
}


def _by_id(sources: list[dict]) -> dict[str, dict]:
    return {s["id"]: s for s in sources}


def test_every_source_is_listed_in_order_with_its_own_state():
    sources = crew_mode.sources_view(BOTH_ON, [], relay_team=True)
    assert [s["id"] for s in sources] == ["tailscale", "ssm", "organization", "kiro_login"]
    states = {s["id"]: s["state"] for s in sources}
    assert states == {
        "tailscale": "available",
        "ssm": "available",
        "organization": "unavailable",
        "kiro_login": "available",
    }


def test_several_sources_are_on_at_once_and_each_counts_its_own_people():
    observed = [ALICE, BOB, DEVON, RELAY_ANA, OWNER, ALICE]
    sources = _by_id(crew_mode.sources_view(BOTH_ON, observed, relay_team=True))
    assert sources["tailscale"]["people"] == 2
    assert sources["ssm"]["people"] == 1
    assert sources["kiro_login"]["people"] == 1
    assert sources["organization"]["people"] == 0


def test_a_source_that_is_not_set_up_keeps_its_reason_and_counts_nobody_but_who_came_in():
    sources = _by_id(crew_mode.sources_view(NONE_ON, [DEVON]))
    assert sources["tailscale"] == {
        "id": "tailscale",
        "state": "unavailable",
        "reason": "not_configured",
        "people": 0,
    }
    # Someone came in over SSM while it reads off: still counted, never invented.
    assert sources["ssm"]["people"] == 1
    assert sources["kiro_login"]["state"] == "unavailable"
    assert sources["organization"]["reason"] == "not_offered"


def test_unknown_and_system_principals_are_not_people():
    sources = _by_id(
        crew_mode.sources_view(BOTH_ON, [Principal.unknown(), Principal.system("cron")])
    )
    assert all(s["people"] == 0 for s in sources.values())


def test_a_missing_setup_path_reads_as_not_set_up():
    sources = _by_id(crew_mode.sources_view({}, []))
    assert sources["ssm"]["state"] == "unavailable"


def test_the_mode_body_keeps_mode_and_adds_sources():
    body = crew_mode.mode_view(
        OWNER,
        [ALICE, DEVON],
        setup_paths=BOTH_ON,
        crew_id="g1",
        network_identity_unreadable=False,
        relay_team=True,
    )
    assert body["mode"] == "network"
    sources = _by_id(body["sources"])
    assert (sources["tailscale"]["people"], sources["ssm"]["people"]) == (1, 1)
    assert sources["kiro_login"]["state"] == "available"
    assert body["adapters"]["tailscale"]["state"] == "available"


def test_a_team_crew_on_the_relay_is_a_network_crew_before_anyone_joins():
    body = crew_mode.mode_view(
        OWNER,
        [],
        setup_paths=NONE_ON,
        crew_id="g1",
        network_identity_unreadable=False,
        relay_team=True,
    )
    assert body["mode"] == "network"
    alone = crew_mode.mode_view(
        OWNER, [], setup_paths=NONE_ON, crew_id="g1", network_identity_unreadable=False
    )
    assert alone["mode"] == "personal"
    assert _by_id(alone["sources"])["kiro_login"]["state"] == "unavailable"


def _request(binding: object) -> web.Request:
    app = {RELAY_INGRESS_APP_KEY: SimpleNamespace(binding=binding)}
    return SimpleNamespace(app=app)  # type: ignore[return-value]


@pytest.mark.parametrize(
    "binding,expected",
    [
        (HostBinding(crew_kind=CrewKind.TEAM), True),
        (HostBinding(crew_kind=CrewKind.PERSONAL, owner_user_id="u-1"), False),
        (None, False),
    ],
    ids=["team", "personal", "unbound"],
)
def test_the_route_reads_relay_sharing_from_the_host_binding(binding, expected):
    assert routes_identity._relay_team_crew(_request(binding)) is expected


def test_the_route_reads_no_relay_sharing_without_an_ingress():
    assert routes_identity._relay_team_crew(SimpleNamespace(app={})) is False  # type: ignore[arg-type]
