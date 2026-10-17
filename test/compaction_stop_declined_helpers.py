"""Fakes shared by test_compaction_stop_declined.py and its parts.

A ``/compact`` turn the test controls, the session manager built on it, and the
dashboard and Slack ``!stop`` doubles. ``_clear_channel_decline_markers`` is
autouse, and an autouse fixture applies only in a module that imports it by name,
so every part imports it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager

KEY = "dashboard:chat-14841"


@pytest.fixture(autouse=True)
def _clear_channel_decline_markers():
    from kiro_crew import session_lifecycle as sl

    sl._stop_declined_markers.clear()
    sl._parked_queue.clear()
    yield
    sl._stop_declined_markers.clear()
    sl._parked_queue.clear()


class _Compact:
    """A ``/compact`` turn the test controls: it blocks until released or is failed."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.fail_with: BaseException | None = None
        self.started = asyncio.Event()

    async def stream(self, _command: str):
        self.started.set()
        # flake-ok: pre-existing unbounded wait inside the fake /compact turn until the test releases or cancels it, moved verbatim; tracked for a de-flake pass
        await self.release.wait()
        if self.fail_with is not None:
            raise self.fail_with
        if False:  # pragma: no cover - makes this an async generator
            yield None


def _factory(compact: _Compact, order: list[str]):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.cwd = ""
        m.disown_work_dir = MagicMock()
        m.memory_mode = "persistent"
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 90.0
        m.context_usage_unknown = lambda: False
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: True
        m.runtime_info = lambda: (None, None)
        m.stream_command = MagicMock(side_effect=compact.stream)
        m.wait_for_compaction = AsyncMock(return_value={"type": "failed"})

        # A cooperative cancel is what a soft Stop does to a live turn; here it
        # ends the /compact turn the way the harness would: the stream raises.
        async def _cancel(*, wait_ack_timeout: float = 0.0):
            compact.fail_with = RuntimeError("compaction reported no result")
            compact.release.set()
            return "acked"

        m.cancel = AsyncMock(side_effect=_cancel)
        m.shutdown = AsyncMock(side_effect=lambda: order.append("shutdown"))
        return m

    return factory


async def _setup():
    order: list[str] = []
    compact = _Compact()
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory(compact, order))
    await mgr.get_or_create(KEY)
    key = mgr._fold_key(KEY)
    mgr.release(key)
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps,
        compact_wait_timeout_secs=lambda: 5.0,
        compact_result_wait_secs=lambda _elapsed, _budget: 0.05,
        compact_failure_cooldown_secs=123.0,
    )
    notices: list[tuple[bool, str]] = []

    async def _cb(key, pct, *, success, outcome="compacted"):
        notices.append((success, outcome))

    mgr.set_compact_callback(_cb)
    return mgr, key, compact, order, notices


async def _settle() -> None:
    for _ in range(20):
        # flake-ok: pre-existing 20-turn yield the tests use to let scheduled tasks run, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)


_TEST_DIR = str(Path(__file__).resolve().parent)


def _dashboard_state(tmp_path):
    import sys

    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from chat_test_helpers import _make_state

    return _make_state(tmp_path)


def _slack_orch():
    """The Slack orchestrator double the events suite uses, with a live !stop target."""
    import sys
    from unittest.mock import AsyncMock, MagicMock

    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from test_slack_events_coverage import _make_orch

    orch = _make_orch()
    orch.sessions.has_session = MagicMock(return_value=True)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    orch.sessions.note_stop = MagicMock(return_value=True)
    orch.sessions.clear_queue = MagicMock()
    orch.sessions.detach_queue = MagicMock(return_value=())
    orch.sessions.restore_queue = MagicMock()
    task = MagicMock()
    task.done.return_value = False
    orch._session_tasks = {"100.0": task}
    orch._pending_queue = {"100.0": [("ts", "text", {"paths": []})]}
    orch.slack.post_ephemeral = AsyncMock()
    return orch, task


def _run_slack_stop(orch):
    from unittest.mock import patch

    from kiro_crew.slack import events as ev

    with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
        with patch("kiro_crew.slack.events.is_owner", return_value=True):
            with patch("kiro_crew.slack.events.unlink_queued_temp_paths") as unlink:
                from test_slack_events_coverage import _event

                asyncio.run(ev._route_message(orch, _event(text="!stop"), ev.SeenCache()))
    return unlink
