"""``SubagentManager._provider_label_of`` keeps the label the one-module manager persisted.

The run surface reads ``provider_label`` through ``kiro_crew.agent_sdk.drivers.acp``
instead of importing the providers layer. The label is persisted with a run's state and
keys resume compatibility, so the SDK wrapper and the manager must answer exactly what the
providers-layer helper answers, for every provider kind a run can hold: an ``AcpProvider``
per backend, before and after the startup swap to an ``AcpSessionProvider``, a bare
``AcpSessionProvider``, a spec'd mock, a non-ACP object, and the standalone
``ClaudeCodeProvider`` sentinel the manager checks first.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.subagent as subagent
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import PROVIDER_LABEL_BY_BACKEND, PROVIDER_LABEL_CLAUDE
from kiro_crew.agent_sdk.drivers import acp as sdk_acp
from kiro_crew.providers import acp as providers_acp
from kiro_crew.providers.acp import AcpProvider


def _acp_provider(backend: str) -> AcpProvider:
    """An unstarted provider, constructed without spawning anything."""
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


def _session_provider(backend: str) -> AcpSessionProvider:
    runtime = MagicMock(spec=AcpRuntime)
    runtime.acp_backend = backend
    return AcpSessionProvider(MagicMock(), runtime)


def _started(backend: str) -> AcpProvider:
    provider = _acp_provider(backend)
    provider._client = _session_provider(backend)  # type: ignore[assignment]
    return provider


def _spec_mock(backend: str) -> MagicMock:
    mock = MagicMock(spec=AcpProvider)
    mock.client = MagicMock()
    mock.client.backend = backend
    return mock


_BUILDERS = {
    "acp": _acp_provider,
    "started": _started,
    "session": _session_provider,
    "spec-mock": _spec_mock,
}

_CASES = [
    *((kind, backend) for kind in _BUILDERS for backend in sorted(PROVIDER_LABEL_BY_BACKEND)),
    ("spec-mock", "not-a-backend"),
    ("object", ""),
]


def _provider(kind: str, backend: str) -> object:
    return object() if kind == "object" else _BUILDERS[kind](backend)


def _one_module_label(provider: object) -> str:
    """What the one-module ``_provider_label_of`` returned: sentinel first, then the helper."""
    standalone = subagent.ClaudeCodeProvider
    if standalone is not None and isinstance(provider, standalone):
        return PROVIDER_LABEL_CLAUDE
    return providers_acp.provider_label(provider)


@pytest.mark.parametrize(("kind", "backend"), _CASES, ids=[f"{k}:{b}" for k, b in _CASES])
def test_the_sdk_wrapper_and_the_manager_answer_the_providers_label(
    kind: str, backend: str
) -> None:
    provider = _provider(kind, backend)
    expected = providers_acp.provider_label(provider)
    assert sdk_acp.provider_label(provider) == expected
    assert subagent.SubagentManager._provider_label_of(provider) == _one_module_label(provider)
    assert subagent.SubagentManager._provider_label_of(provider) == expected


def test_the_cases_cover_every_label_the_helper_can_return() -> None:
    labels = {providers_acp.provider_label(_provider(k, b)) for k, b in _CASES}
    assert labels == set(PROVIDER_LABEL_BY_BACKEND.values())


def test_the_standalone_sentinel_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The manager reads the facade's ``ClaudeCodeProvider`` before asking the helper."""

    class Standalone:
        pass

    monkeypatch.setattr(subagent, "ClaudeCodeProvider", Standalone)
    assert sdk_acp.provider_label(Standalone()) != PROVIDER_LABEL_CLAUDE
    assert subagent.SubagentManager._provider_label_of(Standalone()) == PROVIDER_LABEL_CLAUDE
