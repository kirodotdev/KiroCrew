"""The readiness gate's bundle-route check is the shared predicate, with the same answers.

``KiroPrerequisiteService._kas_bundle_route`` delegates to
``agent_sdk.kas_bundle.kas_bundle_route`` (backend is kas AND ``agent.kas_bundle``).
Its one caller, ``record_independent_backend_setup``, already requires
``backend == kas``, and that method's callers pass the configured backend. So at
the call site the delegate must answer exactly what the one-fact read of
``agent.kas_bundle`` answered, on every row of the truth table.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.kiro_prerequisite_service import KiroPrerequisiteService

_BAD_CONFIG = object()


def _one_fact_before(config: object) -> bool:
    """The check the readiness gate made before it shared the predicate."""
    if config is _BAD_CONFIG:
        return False
    return bool(config.agent.kas_bundle)  # type: ignore[attr-defined]


def _pin_config(monkeypatch: pytest.MonkeyPatch, config: object) -> None:
    def _load() -> object:
        if config is _BAD_CONFIG:
            raise ValueError("unreadable config")
        return config

    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", _load)


def _config(backend: str, kas_bundle: bool) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, kas_bundle=kas_bundle))


@pytest.mark.parametrize("kas_bundle", [True, False])
@pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO])
def test_the_call_site_decision_is_unchanged(monkeypatch, backend, kas_bundle):
    config = _config(backend, kas_bundle)
    _pin_config(monkeypatch, config)

    # The call site is ``backend == kas and self._kas_bundle_route()``, with
    # ``backend`` the configured one.
    before = backend == ACP_BACKEND_KAS and _one_fact_before(config)
    after = backend == ACP_BACKEND_KAS and KiroPrerequisiteService._kas_bundle_route()

    assert after is before


@pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO])
def test_an_unreadable_config_still_reads_as_the_relay_route(monkeypatch, backend):
    _pin_config(monkeypatch, _BAD_CONFIG)

    assert KiroPrerequisiteService._kas_bundle_route() is False
    assert (backend == ACP_BACKEND_KAS and KiroPrerequisiteService._kas_bundle_route()) is False


@pytest.mark.parametrize(
    ("backend", "kas_bundle", "expected"),
    [
        (ACP_BACKEND_KAS, True, True),
        (ACP_BACKEND_KAS, False, False),
        (ACP_BACKEND_KIRO, True, False),
        (ACP_BACKEND_KIRO, False, False),
    ],
)
def test_the_helper_is_the_shared_two_fact_predicate(monkeypatch, backend, kas_bundle, expected):
    from kiro_crew.agent_sdk import kas_bundle as kas_bundle_mod

    _pin_config(monkeypatch, _config(backend, kas_bundle))

    assert KiroPrerequisiteService._kas_bundle_route() is expected
    assert kas_bundle_mod.kas_bundle_route() is expected
