"""Eager spawn never prewarms a chat bound to a Session Environment profile.

A remote start creates or wakes a billed workspace, so it waits for the chat's
first real message whichever signal armed the prewarm (create, re-create,
project or agent switch, focus).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _bindings,
    _cfg,
    _isolate_armed_registry,
    _mock_state,
    _pin_prewarm_allowance,
)

from kiro_crew import session_environment as se
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn
from kiro_crew.dashboard.state import _ChatSlot


def _choices(answer):
    def get(key):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return lambda: SimpleNamespace(get=get)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "choice", "prewarmed"),
    [
        (True, "dev", False),  # bound: the first message starts the workspace
        (True, se.SessionEnvironmentUnavailable("unreadable"), False),  # fail closed
        (True, None, True),  # bound to this machine: prewarmed as before
        (False, "dev", True),  # flag off: the choice is never read
    ],
    ids=["bound", "unreadable", "local", "flag-off"],
)
async def test_eager_spawn_skips_a_chat_bound_to_a_profile(
    tmp_path, _floor_monkeypatch, flag, choice, prewarmed
):
    if flag:
        _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    else:
        _floor_monkeypatch.delenv("KIROCREW_DEV_MODE", raising=False)
    slot = _ChatSlot("c1")
    slot.project = str(tmp_path)
    state = _mock_state(slot)
    with (
        patch.object(se, "_choices", _choices(choice)),
        patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
        patch.object(chat_runner, "resolve_agent_bindings", return_value=_bindings()),
    ):
        await _eager_spawn(state, slot)
    assert state.sessions.get_or_create.await_count == (1 if prewarmed else 0)
