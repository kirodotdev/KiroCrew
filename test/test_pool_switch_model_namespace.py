"""A warm-pool claim spells its model switch in the harness's model-id namespace.

``session_allocation_parts.get_or_create`` reads the namespace as
``SessionCapabilities.model_id_namespace`` (through the allocation's
``provider_model_namespace`` dependency), not off the harness's name. Only the
``claude_code`` namespace has ids of its own in the registry, so claude takes
``to_provider_id`` and every other backend, an unknown one included, takes kiro's
``to_acp_id`` spelling, exactly as before the read moved.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_pool_helpers import _make_manager, _make_provider

from kiro_crew.agent_sdk import capabilities as caps_mod
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MODEL_AT_LAUNCH,
)
from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_CLAUDE_CODE, capabilities_for

#: A canonical key the two spellings translate differently.
REQUESTED = "opus-4.8-1m"
CLAUDE_SPELLING = "global.anthropic.claude-opus-4-8[1m]"
KIRO_SPELLING = "claude-opus-4.8"

#: What each backend's switch sent before the namespace was read as a capability.
#: Written out per backend so a changed answer names its harness.
EXPECTED = {
    "": KIRO_SPELLING,
    "kas": KIRO_SPELLING,
    "claude": CLAUDE_SPELLING,
    "codex": KIRO_SPELLING,
    "opencode": KIRO_SPELLING,
    "pi": KIRO_SPELLING,
    "goose": KIRO_SPELLING,
    "deepseek": KIRO_SPELLING,
    "copilot": KIRO_SPELLING,
    "mimocode": KIRO_SPELLING,
    "antigravity": KIRO_SPELLING,
    "some-harness-nobody-registered": KIRO_SPELLING,
}


def _pooled(backend: str) -> MagicMock:
    from kiro_crew.providers.acp import AcpProvider

    pooled = _make_provider()
    pooled.__class__ = AcpProvider
    pooled.client = MagicMock()
    pooled.client.backend = backend
    pooled.client.set_model = AsyncMock()
    pooled.client.resumed = False
    pooled.client._session_id = "fake-sid"
    return pooled


async def _switch(backend: str) -> MagicMock:
    mgr, _factory = _make_manager(pool_agent="kirocrew")
    pooled = _pooled(backend)
    mgr._drain_and_claim = AsyncMock(return_value=pooled)
    mgr._schedule_replenish = MagicMock()
    with patch.object(type(mgr), "_resolve_agent_model", return_value="default-model"):
        await mgr.get_or_create("test-key", agent="kirocrew", model=REQUESTED)
    return pooled.client.set_model


def test_the_table_covers_every_switching_backend() -> None:
    """A backend added later must get a row here, so its answer is a decision."""
    switching = set(ACP_BACKENDS_KNOWN) - set(ACP_BACKENDS_MODEL_AT_LAUNCH)
    assert switching <= set(EXPECTED)


def test_only_claude_reports_the_claude_code_namespace() -> None:
    named = {
        b
        for b in ACP_BACKENDS_KNOWN
        if capabilities_for(b).model_id_namespace == MODEL_NAMESPACE_CLAUDE_CODE
    }
    assert named == {ACP_BACKEND_CLAUDE}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(EXPECTED))
async def test_each_backend_switches_with_the_spelling_it_used_before(backend: str) -> None:
    set_model = await _switch(backend)
    set_model.assert_awaited_once_with(EXPECTED[backend])


@pytest.mark.asyncio
async def test_the_spelling_follows_the_capability_not_the_backend_name(monkeypatch) -> None:
    """Give codex claude's namespace and its switch takes claude's spelling."""
    real = caps_mod.capabilities_for

    def _codex_on_claude_ids(backend: str):
        record = real(backend)
        if backend == ACP_BACKEND_CODEX:
            return dataclasses.replace(record, model_id_namespace=MODEL_NAMESPACE_CLAUDE_CODE)
        return record

    monkeypatch.setattr(caps_mod, "capabilities_for", _codex_on_claude_ids)
    set_model = await _switch(ACP_BACKEND_CODEX)
    set_model.assert_awaited_once_with(CLAUDE_SPELLING)
