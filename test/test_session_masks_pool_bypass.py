"""Session-specific masks must be resolved at cold start, never inherited from the pool."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", [(), ("/state/sessions",)])
async def test_a_masked_session_bypasses_the_pool(monkeypatch, hidden):
    from test_aws_project_profiles import _cfg, _recording_factory

    from kiro_crew.platform import context as context_mod
    from kiro_crew.platform.interfaces import SessionMasks, SpawnIdentity
    from kiro_crew.session import SessionManager

    asked = []

    def masks(identity):
        asked.append(identity)
        return SessionMasks(hidden_dirs=hidden)

    monkeypatch.setattr(
        context_mod,
        "current_context",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(session_masks=masks)),
    )
    manager = SessionManager(_cfg({}, pool_size=2), provider_factory=_recording_factory([]))
    claim = AsyncMock(return_value=None)
    manager._drain_and_claim = claim
    manager._schedule_replenish = MagicMock()
    decisions = []
    manager._record_pool_decision = lambda decision, _key: decisions.append(decision)
    try:
        await manager.get_or_create("chat-alpha")
    finally:
        await manager.close_all()

    assert asked == [SpawnIdentity("chat-alpha")]
    assert decisions == ["bypass_session_masks" if hidden else "miss_empty"]
    if hidden:
        claim.assert_not_awaited()
    else:
        claim.assert_awaited_once()
