"""Fakes the TaskRunner V2 scenario modules share.

``test_taskrunner_v2_scenarios.py``, ``test_taskrunner_v2_scenarios_edge_cases.py``
and ``test_taskrunner_v2_scenarios_step_sessions.py`` import them by name: the
autouse ``_passthrough_sandbox`` (real git runs unwrapped), a session-manager
mock, LLM events and a provider that streams one reply.
``test_taskrunner_checkpoint_names_its_spec.py`` reaches ``_make_provider`` and
``_mock_sessions`` through ``test_taskrunner_v2_scenarios``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the TaskRunner V2 scenario modules' autouse unwrapped-git sandbox stub, moved verbatim; tracked for a de-flake pass
def _passthrough_sandbox(monkeypatch):
    """Several scenarios spawn REAL git via ``git_coord._git`` →
    ``sandboxed_spawn_argv`` → ``wrap_argv``, which raises when no OS-level
    sandbox backend is available. These exercise task/git logic, not sandbox
    availability, so run the command unwrapped in-test."""
    import os as _os

    from kiro_crew import git_coord

    monkeypatch.setattr(
        git_coord,
        "sandboxed_spawn_argv",
        lambda argv, *a, **k: (list(argv), dict(_os.environ), None),
    )


# ── Helpers ──


def _mock_sessions() -> MagicMock:
    s = MagicMock()
    s._lock = asyncio.Lock()
    s._sessions = {}
    s.get_or_create = AsyncMock()
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.record_success = MagicMock()
    s.record_failure = AsyncMock()
    s.check_context_usage = MagicMock()

    async def _open_task_session(
        _parent_key, session_key, *, agent=None, cwd=None, approval_policy="", start_priority=None
    ):
        return await s.get_or_create(session_key, agent=agent, cwd=cwd)

    s.open_task_session = _open_task_session
    s.release_subagent_runtime = AsyncMock()
    return s


def _llm_event(kind: str, text: str = ""):
    from kiro_crew.providers.base import LLMEvent

    return LLMEvent(kind=kind, text=text)


def _make_provider(text: str = "done"):
    provider = MagicMock()

    async def _stream(msg: str):
        yield _llm_event("text_chunk", text)
        yield _llm_event("complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider
