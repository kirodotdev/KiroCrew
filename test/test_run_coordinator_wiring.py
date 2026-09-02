"""The typed coordinator seam is injectable before it becomes authoritative."""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.run_coordinator import MemoryRunCoordinator
from kiro_crew.subagent import SubagentManager


def test_subagent_manager_defaults_to_memory_coordinator() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    assert isinstance(manager._coordinator, MemoryRunCoordinator)


def test_subagent_manager_preserves_injected_coordinator_identity() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    assert manager._coordinator is coordinator
