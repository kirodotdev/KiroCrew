"""Which workflow engine serves a backend, and the single KAS-workflows gate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKENDS_NATIVE_WORKFLOWS
from kiro_crew.kas_workflows.engine import (
    ENGINE_CREW,
    ENGINE_KAS,
    backend_brings_kas_workflows,
    engine_for,
    kas_workflows_enabled,
)


def _config(backend: str, on: object) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, kas_workflows=on))


def test_engine_follows_capability_membership() -> None:
    assert ACP_BACKENDS_NATIVE_WORKFLOWS == frozenset({ACP_BACKEND_KAS})
    assert engine_for(ACP_BACKEND_KAS) == ENGINE_KAS
    assert backend_brings_kas_workflows(ACP_BACKEND_KAS)
    for other in ("", "claude", "no-such-backend"):
        assert engine_for(other) == ENGINE_CREW
        assert not backend_brings_kas_workflows(other)


def test_the_gate_needs_kas_and_the_opt_in() -> None:
    assert kas_workflows_enabled(_config(ACP_BACKEND_KAS, True))
    assert not kas_workflows_enabled(_config(ACP_BACKEND_KAS, False))
    assert not kas_workflows_enabled(_config("", True))
    assert not kas_workflows_enabled(_config("claude", True))


@pytest.mark.parametrize(
    "config",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(agent=None),
        SimpleNamespace(agent=SimpleNamespace()),
    ],
)
def test_the_gate_is_off_on_any_doubt(config: object) -> None:
    assert not kas_workflows_enabled(config)
