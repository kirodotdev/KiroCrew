"""How a session's KAS local-memory policy is planned, for code outside the ACP layer.

The rules live in :mod:`kiro_crew.acp.kas_memory`, read when :func:`memory_plan`
is called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.acp.kas_memory import MemoryPlan as MemoryPlan

__all__ = ["memory_plan"]


def memory_plan(
    *,
    setting_on: bool,
    kas_backend: bool,
    session_key: str,
    session_memory_mode: str,
    owning_app: str,
    recorded_mode: str,
    cloud_session: bool = False,
    personal_session: bool = False,
) -> MemoryPlan:
    """The new session's memory policy, its saved policy, and whether its SID must rotate."""
    from kiro_crew.acp import kas_memory

    return kas_memory.memory_plan(
        setting_on=setting_on,
        kas_backend=kas_backend,
        session_key=session_key,
        session_memory_mode=session_memory_mode,
        owning_app=owning_app,
        recorded_mode=recorded_mode,
        cloud_session=cloud_session,
        personal_session=personal_session,
    )
