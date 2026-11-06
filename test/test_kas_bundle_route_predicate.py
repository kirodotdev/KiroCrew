"""Every reader of "does a KAS spawn take the bundle route" answers from one route half.

``agent_sdk.kas_bundle.kas_spawn_takes_bundle`` is that half (``agent.kas_bundle``
on, or the automatic switch). ``kas_bundle_route`` (and the readiness gate through
it) and the sign-in owner add the backend half; the install probe and a
``?backend=kas`` model list read it bare, because they describe KAS whatever the
default backend is. A reader that spelled the route rule out again would ask
``kas_route`` itself, which fails here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import crewmate_harness
from kiro_crew.agent_sdk import backend_install, kas_bundle
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.auth import bridge, whoami
from kiro_crew.kiro_prerequisite_service import KiroPrerequisiteService


@pytest.mark.parametrize("route_half", [True, False])
@pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO])
def test_every_reader_answers_from_the_shared_route_half(monkeypatch, backend, route_half):
    agent = SimpleNamespace(acp_backend=backend, kas_bundle=False)
    config = SimpleNamespace(agent=agent)
    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", lambda: config)
    monkeypatch.setattr(kas_bundle, "kas_spawn_takes_bundle", lambda a: a is agent and route_half)
    monkeypatch.setattr(kas_bundle, "kas_route", lambda: pytest.fail("route rule re-spelled"))
    monkeypatch.setattr(bridge, "vault_holds_identity", lambda: False)

    on_kas = backend == ACP_BACKEND_KAS
    assert kas_bundle.kas_bundle_route() is (on_kas and route_half)
    assert KiroPrerequisiteService._kas_bundle_route() is (on_kas and route_half)
    assert whoami.agent_owns_sign_in(agent) is (on_kas and route_half)
    assert backend_install._kas_bundle_route() is route_half
    asked = crewmate_harness.models_route(config, {"backend": ACP_BACKEND_KAS})
    assert asked == (ACP_BACKEND_KAS, route_half)
