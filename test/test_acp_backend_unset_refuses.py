"""An unset ``agent.acp_backend`` refuses a KAS session instead of starting KAS.

Absent or ``""`` is "no choice". Config load still answers it with the default and
never raises (H3); the provider factory refuses the KAS session before any
provider is built, in every edition.
"""

from __future__ import annotations

import pytest

from kiro_crew import lts_boot
from kiro_crew.agent_sdk import backends as acp_backends
from kiro_crew.config import loader_document

KAS = acp_backends.ACP_BACKEND_KAS

# Captured at import, before the suite fixture swaps it for a no-op.
_REAL_REFUSAL = getattr(lts_boot, "refuse_unset_acp_backend", None)


@pytest.fixture(autouse=True)
def _armed(_floor_monkeypatch):
    if _REAL_REFUSAL is not None:
        _floor_monkeypatch.setattr(lts_boot, "refuse_unset_acp_backend", _REAL_REFUSAL)


def _factory(monkeypatch, selected):
    from kiro_crew import members
    from kiro_crew.agent_sdk.drivers import acp
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(members, "select_provider_backend", lambda *a, **k: selected)
    built: list[dict] = []
    monkeypatch.setattr(acp, "build_acp_provider", lambda **kw: built.append(kw) or kw)
    return KiroCrewConfig().create_provider_factory(), built


def test_unset_acp_backend_refuses_the_kas_session(monkeypatch):
    from kiro_crew.crewmate_harness import HARNESS_UNAVAILABLE_CODE, CrewmateHarnessUnavailable

    monkeypatch.setattr(loader_document, "acp_backend_declared", lambda: False)
    factory, built = _factory(monkeypatch, KAS)
    with pytest.raises(CrewmateHarnessUnavailable) as exc:
        factory(session_key="dashboard:chat-1", agent="kirocrew")
    assert built == [], "a KAS provider was built for an install that never chose a harness"
    assert type(exc.value).__name__ == "AcpBackendUnset"
    assert exc.value.code == HARNESS_UNAVAILABLE_CODE
    assert "agent.acp_backend is not set" in str(exc.value)


def test_a_declared_kas_choice_still_starts(monkeypatch):
    monkeypatch.setattr(loader_document, "acp_backend_declared", lambda: True)
    factory, built = _factory(monkeypatch, KAS)
    factory(session_key="dashboard:chat-1", agent="kirocrew")
    assert [kw["acp_backend"] for kw in built] == [KAS]


def test_an_unset_choice_never_blocks_a_non_kas_session(monkeypatch):
    monkeypatch.setattr(loader_document, "acp_backend_declared", lambda: False)
    claude = acp_backends.ACP_BACKEND_CLAUDE
    factory, built = _factory(monkeypatch, claude)
    factory(session_key="dashboard:chat-1", agent="kirocrew")
    assert [kw["acp_backend"] for kw in built] == [claude]


def test_config_load_still_resolves_an_unset_choice():
    # H3: the selection gate never raises, so an unset install still loads.
    assert acp_backends.resolve_selected_backend(None) in acp_backends.selectable_backends() | {
        acp_backends.governance_floor_backend()
    }
