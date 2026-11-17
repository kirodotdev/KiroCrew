"""The viewer fields of the Shared Crew mode body: ``viewer_via``,
``viewer_is_owner`` and ``owner_label``."""

from __future__ import annotations

import dataclasses

import pytest
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.shared_crew import crew_mode
from kiro_crew.shared_crew.contracts import Principal, Provenance
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal

pytestmark = pytest.mark.usefixtures("host_isolation")

ALICE = tailnet_principal("alice@example.com", "alice-laptop", Provenance.TAILSCALE_SERVE)

_EXPECTED_VIA = {
    Provenance.KIRO_RELAY: "kiro_login",
    Provenance.TAILSCALE_SERVE: "tailscale",
    Provenance.TAILSCALE_DIRECT: "tailscale",
    Provenance.TAILSCALE_LISTENER: "tailscale",
    Provenance.SSM_INVITE: "ssm",
    Provenance.LOCAL_OPERATOR: "local",
    Provenance.INTERNAL: "unknown",
    Provenance.NONE: "unknown",
}


def test_every_provenance_has_an_expected_via():
    assert set(_EXPECTED_VIA) == set(Provenance)


@pytest.mark.parametrize("provenance", list(Provenance), ids=lambda p: p.value)
def test_viewer_via_names_the_path_for_each_provenance(provenance: Provenance):
    viewer = dataclasses.replace(ALICE, provenance=provenance)
    assert crew_mode.viewer_via(viewer) == _EXPECTED_VIA[provenance]


def test_an_unknown_viewer_came_in_on_no_known_path():
    assert crew_mode.viewer_via(Principal.unknown()) == "unknown"


_OFF = {"state": "unavailable"}
_ON = {"state": "available"}


def _view(viewer: Principal, **kw):
    return crew_mode.mode_view(
        viewer,
        [],
        setup_paths={"tailscale": _OFF, "ssm": _ON},
        crew_id="crew-1",
        network_identity_unreadable=False,
        **kw,
    )


def test_the_mode_body_passes_the_owner_fields_through():
    owner = local_operator_principal("local-app")
    body = _view(owner, owner_label="bo@kiro.example", viewer_is_owner=True)
    assert body["viewer_via"] == "local"
    assert body["viewer_is_owner"] is True
    assert body["owner_label"] == "bo@kiro.example"


def test_the_mode_body_for_a_teammate_names_their_path_and_the_owner():
    teammate = dataclasses.replace(ALICE, provenance=Provenance.KIRO_RELAY)
    body = _view(teammate, owner_label="Studio Mac", viewer_is_owner=False)
    assert body["viewer_via"] == "kiro_login"
    assert body["viewer_is_owner"] is False
    assert body["owner_label"] == "Studio Mac"


def test_the_mode_body_defaults_say_not_the_owner():
    body = _view(ALICE)
    assert body["viewer_is_owner"] is False
    assert body["owner_label"] == ""
