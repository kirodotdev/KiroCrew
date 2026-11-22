"""The voice-runtime pre-flight follows the spawn guard's backend gate."""

from __future__ import annotations

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.sandbox import voice_runtime_workspace_conflict


@pytest.fixture
def overlapping(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
    runtime = tmp_path / "data" / "run" / "voice-runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setattr(sandbox_mod, "_voice_runtime_sandbox_paths", lambda: (str(runtime),))
    return str(tmp_path)


def test_unknown_backend_keeps_the_strict_answer(overlapping):
    assert voice_runtime_workspace_conflict(overlapping) is not None


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
def test_sandbox_delegating_backends_are_refused(overlapping, backend):
    assert "protected voice runtime" in voice_runtime_workspace_conflict(
        overlapping, backend=backend
    )


def test_claude_backend_is_not_refused(overlapping):
    assert voice_runtime_workspace_conflict(overlapping, backend=ACP_BACKEND_CLAUDE) is None


@pytest.mark.asyncio
async def test_slot_spawn_backend_prefers_the_slots_pick(monkeypatch):
    from unittest.mock import MagicMock

    import kiro_crew.dashboard.slot_spawn_backend as fallback_mod

    slot = MagicMock(backend="claude")
    monkeypatch.setattr("kiro_crew.agent_sdk.backends.resolve_selected_backend", lambda v: v)
    assert await fallback_mod.slot_spawn_backend(slot) == ACP_BACKEND_CLAUDE
    slot = MagicMock(backend="")
    cfg = MagicMock()
    cfg.agent.acp_backend = ACP_BACKEND_KIRO
    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", lambda: cfg)
    assert await fallback_mod.slot_spawn_backend(slot) == ACP_BACKEND_KIRO
