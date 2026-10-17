"""Builders shared by the ``test_subagent_coverage*.py`` modules.

``_sessions`` is a ``SessionManager`` double carrying the seams those modules touch,
``_manager`` builds a ``SubagentManager`` over it with every callback off unless a
test wires one, and ``_info`` builds a ``SubagentInfo``. Nothing here launches a
process.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kiro_crew.subagent import SubagentInfo, SubagentManager

# ── Fixtures / builders ───────────────────────────────────────────────────


def _sessions() -> MagicMock:
    """A SessionManager double with the seams this module touches."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_provider = MagicMock(return_value=None)
    sessions.mark_continuable = MagicMock()
    sessions.forget_conversation = MagicMock(return_value="")
    sessions.conversation_provider = MagicMock(return_value="acp")
    sessions.is_session_sharing_eligible = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _manager(**kwargs: object) -> SubagentManager:
    """Build a manager with every callback off unless a test wires one."""
    kwargs.setdefault("sessions", _sessions())
    kwargs.setdefault("ctx_builder", None)
    return SubagentManager(**kwargs)  # type: ignore[arg-type]


def _info(agent_id: str = "a1", **kwargs: object) -> SubagentInfo:
    kwargs.setdefault("task", "t")
    return SubagentInfo(id=agent_id, **kwargs)  # type: ignore[arg-type]
