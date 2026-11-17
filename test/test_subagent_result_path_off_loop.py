"""The run's display path to ``result.txt`` is built off the event loop.

``agent_dir_for_display`` resolves the run directory with ``Path.resolve``, a realpath
that opens every path part on Windows, so a busy disk turns a call on the loop into a
stall the loop watchdog ends the gateway for. The run start (``_run_inner_impl``)
and the restart orphan notice (``_notify_orphan_impl``) each build that path; both run on
``kiro_crew.subagent``'s globals, which is the binding patched here.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from subagent_turn_resilience_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _complete_event,
    _manager,
    _mock_sessions,
    _text_event,
)

import kiro_crew.subagent as subagent_mod
from kiro_crew.subagent import SubagentManager
from kiro_crew.testing.wait import await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

real_agent_dir_for_display = subagent_mod.agent_dir_for_display


def _record_threads(monkeypatch) -> list[int]:
    """Patch the facade's ``agent_dir_for_display``; return the threads it ran on."""
    threads: list[int] = []

    def _recording(agent_id: str) -> Path:
        threads.append(threading.get_ident())
        return real_agent_dir_for_display(agent_id)

    monkeypatch.setattr(subagent_mod, "agent_dir_for_display", _recording)
    return threads


@pytest.mark.asyncio
async def test_the_run_start_builds_the_result_path_off_the_event_loop(monkeypatch):
    threads = _record_threads(monkeypatch)

    async def stream(*_args, **_kwargs):
        yield _text_event("done")
        yield _complete_event()

    mgr = _manager(_mock_sessions(stream))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do work")
        assert info is not None
        await await_until(mgr._tasks[info.id], what="the run finishing")

    assert info.outcome == "completed", info.error
    expected = real_agent_dir_for_display(info.id) / "result.txt"
    assert info.result_path == str(expected)
    assert threads and threading.get_ident() not in threads, threads


@pytest.mark.asyncio
async def test_the_orphan_notice_builds_the_result_path_off_the_event_loop(monkeypatch):
    threads = _record_threads(monkeypatch)
    state = {
        "id": "orphan-off-loop",
        "task": "old task",
        "parent_session": "",
        "result_complete": True,
    }
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None)

    with patch("kiro_crew.subagent.has_dashboard_surface", return_value=False):
        notice = await mgr._notify_orphan("orphan-off-loop", state, True)

    assert threads and threading.get_ident() not in threads, threads
    expected = real_agent_dir_for_display("orphan-off-loop") / "result.txt"
    assert notice is not None and f"Result saved at: `{expected}`" in notice.splitlines()
