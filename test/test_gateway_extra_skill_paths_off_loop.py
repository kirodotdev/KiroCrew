"""Gateway start builds the skills loader in a worker thread, not on its loop.

The loader's constructor resolves every ``skills.extra_paths`` root, and one on a
slow or network mount blocks until the mount answers. ``_init_services`` runs on
the gateway's event loop, so the loader is built off it.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from slack_gateway_helpers import _fake_async_proc, _make_orchestrator

_STUBBED = (
    "HookManager",
    "LessonStore",
    "ContextBuilder",
    "SessionManager",
    "HistoryConsolidator",
    "ChannelHistory",
    "MemoryStore",
    "ConversationLog",
)


def test_init_services_builds_the_skills_loader_off_the_loop(tmp_path: Path) -> None:
    orch = _make_orchestrator()
    built_on: list[int] = []

    def _build(**kwargs: object) -> MagicMock:
        built_on.append(threading.get_ident())
        return MagicMock()

    with contextlib.ExitStack() as stack:
        loader_cls = stack.enter_context(patch("kiro_crew.slack.gateway.SkillsLoader"))
        loader_cls.side_effect = _build
        for name in _STUBBED:
            stack.enter_context(patch(f"kiro_crew.slack.gateway.{name}"))
        stack.enter_context(patch("kiro_crew.vector_memory.VectorMemoryStore"))
        stack.enter_context(
            patch("kiro_crew.agent.rebuild_agent_config", return_value=tmp_path / "agent.json")
        )
        proc = _fake_async_proc(stdout=b"kiro-cli 1.30.0")
        stack.enter_context(patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)))
        try:
            asyncio.run(orch._init_services())
        finally:
            orch._stop_memory_startup()

    assert built_on and threading.get_ident() not in built_on
    loader_cls.assert_called_once_with(install_builtins=False)
