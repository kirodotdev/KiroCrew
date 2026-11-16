"""The process-global hooks a ``DashboardState`` wires do not keep it alive.

``DashboardState.__init__`` installs a share-tree reader, a parent-project resolver and the
decisions lane's runner factory into module globals that live for the process. A
closure over the state there pinned the last state (and its ``sessions`` with every call
it recorded) until another state replaced it. While the state is alive the hooks answer
as before; once it is gone each one answers what its seam answers when nothing is wired.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from unittest.mock import MagicMock

import pytest
from chat_runner_coverage_helpers import _state

from kiro_crew import subagent
from kiro_crew.acp import chat_runtime_sharing
from kiro_crew.decisions import impl_llm
from kiro_crew.eventlog import service as eventlog_service
from kiro_crew.messaging import turn_ceiling


@pytest.fixture(autouse=True)
def _seams_restored(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Every seam a state wires goes back to what this test inherited."""
    for module, name in (
        (chat_runtime_sharing, "_tree_root_reader"),
        (subagent, "_parent_project_resolver"),
        (impl_llm, "_runner_factory"),
    ):
        _floor_monkeypatch.setattr(module, name, getattr(module, name))


def _wired(tmp_path):
    """A state built in the test body, so no fixture cache holds it.

    A sub-agent manager is what makes the state wire the parent-project resolver.
    """
    return _state(tmp_path, subagents=MagicMock())


def _forget(state_ref: weakref.ref) -> None:
    # The member event-log singleton holds the state's broadcast, and the turn ceiling
    # its pause sink, until they are reset, which the suite's floor does at every test
    # boundary; do both here, then collect.
    eventlog_service.set_service(None)
    turn_ceiling.set_notification_sink(None)
    gc.collect()
    assert state_ref() is None, "something other than the hooks still holds the state"


def test_the_hooks_answer_through_a_live_state(tmp_path) -> None:
    _wired(tmp_path)
    assert chat_runtime_sharing._tree_root_reader is not None
    assert chat_runtime_sharing.share_tree_root_of("dashboard:chat-x") == "dashboard:chat-x"
    assert subagent._parent_project_resolver is not None
    assert impl_llm._runner_factory is not None


def test_a_dropped_state_is_collected_and_the_hooks_answer_unwired(tmp_path) -> None:
    wired = _wired(tmp_path)
    state_ref = weakref.ref(wired)
    reader = chat_runtime_sharing._tree_root_reader
    resolver = subagent._parent_project_resolver
    factory = impl_llm._runner_factory
    del wired

    _forget(state_ref)

    assert reader is not None and reader("dashboard:chat-x") == ""
    assert chat_runtime_sharing.share_tree_root_of("dashboard:chat-x") == "dashboard:chat-x"
    assert resolver is not None and resolver("dashboard:chat-x") == ()
    assert subagent.parent_project_dirs("dashboard:chat-x") == ()
    assert factory is not None
    with pytest.raises(RuntimeError, match="session manager is gone"):
        asyncio.run(factory("some-model")("prompt"))
