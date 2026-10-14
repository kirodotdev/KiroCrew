"""A subagent's session belongs to the app its spawn was vetted for.

The child provider's ``owning_app`` is what its own governance reads: a remote
workspace's git proxy judges egress with it, so an app-owned chat's subagent
must not run as if no app owned it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

from kiro_crew.execution_context import execution_for_store
from kiro_crew.subagent import SubagentInfo, SubagentManager


@pytest.mark.asyncio
@pytest.mark.parametrize(("app", "expected"), [("mailer", "mailer"), ("", None)])
async def test_the_subagent_session_carries_its_owning_app(app, expected) -> None:
    sessions = _mock_sessions()
    sessions.get_approval_policy = MagicMock(return_value="")
    manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder_auto_spawn())
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="app01",
        task="clone the repo",
        parent_session_key="dashboard:app-chat",
        app=app,
    )
    manager._log_spawned(info)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        await manager._run_inner(info, "subagent:app01")
    sessions.get_or_create.assert_awaited_once()
    assert sessions.get_or_create.call_args.kwargs.get("app_name") == expected
