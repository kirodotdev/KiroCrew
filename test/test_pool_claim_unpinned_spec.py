"""A warm-pool claim switches to the requested model when the pool agent pins none.

The pool agent's spec is read for the model its warm process launched on. A spec
with no ``model`` key reads as ``auto`` and the claim switches to the requested
model; one that spells ``"model": ""`` pins nothing either, so it must switch the
same way instead of leaving the claim on the backend default. Read through the
real ``SessionManager._resolve_agent_model`` over a temporary agents directory.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from session_pool_helpers import _make_manager, _make_provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec",
    [{"name": "kirocrew"}, {"name": "kirocrew", "model": ""}],
    ids=["no-model-key", "empty-model"],
)
async def test_a_claim_on_an_unpinned_pool_agent_switches_to_the_requested_model(
    spec, tmp_path, monkeypatch
) -> None:
    from kiro_crew import session
    from kiro_crew.providers.acp import AcpProvider

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(session, "kiro_agents_dir_path", lambda: agents)
    monkeypatch.setattr(session.SessionManager, "_agent_model_cache", {}, raising=False)
    mgr, _factory = _make_manager(pool_agent="kirocrew")
    pooled = _make_provider()
    pooled.__class__ = AcpProvider
    pooled.client = MagicMock()
    pooled.client.set_model = AsyncMock()
    pooled.client.resumed = False
    pooled.client._session_id = "fake-sid"
    pooled.client.backend = ""
    served = [{"modelId": "claude-opus-5.5"}, {"modelId": "claude-sonnet-5.5"}]
    pooled.available_models = MagicMock(return_value=served)
    mgr._drain_and_claim = AsyncMock(return_value=pooled)
    mgr._schedule_replenish = MagicMock()

    provider, _is_new, _resumed = await mgr.get_or_create(
        "test-key", agent="kirocrew", model="claude-sonnet-5.5"
    )

    assert provider is pooled
    pooled.client.set_model.assert_awaited_once_with("claude-sonnet-5.5")
