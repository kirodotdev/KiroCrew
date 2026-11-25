"""Cold and live chats read one answer for "does the harness choose its own steering".

``dashboard.handlers.chat_steering._harness_of`` answers a chat with no live
provider from the configured backend; ``AcpProvider.native_steering`` and
``AcpSessionProvider.native_steering`` answer a live one. All three read
``SessionCapabilities.loads_steering_natively``, so they cannot disagree. Before the
field existed each compared the backend with ``ACP_BACKEND_KAS`` on its own; the
answers are pinned here as they were: KAS only.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKENDS_KNOWN
from kiro_crew.agent_sdk.capabilities import capabilities_for


def _cold(backend: str) -> bool:
    from kiro_crew.dashboard.handlers.chat_steering import _harness_of

    return _harness_of(None, backend)[1]


def _live_acp(backend: str) -> bool:
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = SimpleNamespace(backend=backend)
    return provider.native_steering


def _live_session(backend: str) -> bool:
    from kiro_crew.acp import session_provider_context

    return session_provider_context.native_steering(SimpleNamespace(backend=backend))


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_every_path_gives_the_answer_it_gave_before(backend: str) -> None:
    expected = backend == ACP_BACKEND_KAS
    assert capabilities_for(backend).loads_steering_natively is expected
    assert (_cold(backend), _live_acp(backend), _live_session(backend)) == (expected,) * 3


def test_an_unknown_backend_leaves_the_choice_to_crew() -> None:
    stranger = "some-harness-nobody-registered"
    assert (_cold(stranger), _live_acp(stranger), _live_session(stranger)) == (False,) * 3


def test_all_three_paths_follow_the_capability(monkeypatch) -> None:
    """Give codex the capability and the cold and live answers move together."""
    import kiro_crew.agent_sdk.capabilities as caps_mod

    real = caps_mod.capabilities_for

    def _codex_loads(backend: str):
        record = real(backend)
        if backend == ACP_BACKEND_CODEX:
            return dataclasses.replace(record, loads_steering_natively=True)
        return record

    import kiro_crew.providers.acp as acp_provider_mod

    monkeypatch.setattr(caps_mod, "capabilities_for", _codex_loads)
    # ``providers.acp`` binds ``capabilities_for`` at import, so patch its copy too.
    monkeypatch.setattr(acp_provider_mod, "capabilities_for", _codex_loads)
    assert _cold(ACP_BACKEND_CODEX) is True
    assert _live_acp(ACP_BACKEND_CODEX) is True
    assert _live_session(ACP_BACKEND_CODEX) is True
