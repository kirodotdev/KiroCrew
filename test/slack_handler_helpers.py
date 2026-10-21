"""Doubles and fixtures shared by the ``test_slack_handler*.py`` modules.

``test_slack_handler`` re-exports ``FakeProvider``, ``FakeSessionManager`` and
``_visible_texts``, which other test modules import from it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import _pending_approvals, _thread_agents, _trusted_sessions
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture(autouse=True)
def _clean_approval_state():
    """Clear module-level approval state between tests to prevent xdist cross-contamination."""
    _pending_approvals.clear()
    _trusted_sessions.clear()
    _thread_agents.clear()
    yield
    _pending_approvals.clear()
    _trusted_sessions.clear()
    _thread_agents.clear()


#: Lost-run ceiling for a wait on something the turn under test does by itself
#: (post and register an approval prompt, park on a gap, finish after a click).
#: Timed wait by wait: at most 0.04 s at ``-n 4`` on a loaded 32-CPU host, and at
#: most 3.6 s when every executor hop is delayed by 1.2 s (a starved-runner model;
#: the turn makes three hops before its prompt). 60 s is over ten times that and
#: half the module's ``--timeout=120``: only a turn that never gets there reaches it.
_LOST_RUN_SECS = 60.0


async def _within_lost_run(awaitable, what: str):
    """Await *awaitable* under ``_LOST_RUN_SECS``; on expiry fail naming *what*.

    A bare ``wait_for`` raises an empty ``TimeoutError``. This one says what never
    finished, the ceiling and the elapsed time. A ``TimeoutError`` the awaited code
    raises on its own, before the ceiling, propagates unchanged.
    """
    started = time.monotonic()
    try:
        return await asyncio.wait_for(awaitable, _LOST_RUN_SECS)
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - started
        # A loop timer can fire up to one clock tick early (15.6 ms on Windows 3.12).
        if elapsed < _LOST_RUN_SECS - 1.0:
            raise
        pytest.fail(
            f"{what} did not finish within the {_LOST_RUN_SECS:.0f}s lost-run ceiling "
            f"({elapsed:.1f}s elapsed)"
        )


async def _approval_prompt_ts(slack, channel: str = "C1") -> str:
    """The ts of the first approval prompt once the turn has posted AND registered it.

    A click that lands between the post and ``_request_approval``'s registration
    answers "no pending approval" and leaves the turn parked on its 120 s approval
    budget. So this waits for the registration, which is the state a click needs,
    and fails by name when it never comes instead of falling through silently.
    """

    def _registered_ts() -> str | None:
        posted = [a for a in slack.actions if a[0] == "blocks"]
        if posted and f"{channel}:{posted[0][1]['ts']}" in _pending_approvals:
            return posted[0][1]["ts"]
        return None

    return await async_wait_until(
        _registered_ts,
        timeout=_LOST_RUN_SECS,
        describe=lambda: f"no approval prompt was registered; actions={slack.actions}",
    )


class FakeProvider:
    """Fake LLMProvider that yields events from stream()."""

    def __init__(self, events: list[LLMEvent] | None = None):
        if events is None:
            events = [LLMEvent(kind="text_chunk", text="The answer is 42")]
        self._events = events
        self.approved: list[str | int] = []
        self.rejected: list[str | int] = []

    async def stream(self, message, timeout=120.0):
        for event in self._events:
            yield event
        yield LLMEvent(kind="complete")

    async def approve_tool(self, request_id, option_id="allow_once"):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def start(self):
        pass

    async def shutdown(self):
        pass

    def context_usage_pct(self):
        return 0.0


class FakeSessionManager:
    """SessionManager that returns a given FakeProvider."""

    def __init__(self, provider: FakeProvider | None = None):
        self._provider = provider or FakeProvider()
        self.keys_seen: list[str] = []
        self.last_agent: str | None = None
        self.last_channel_id: str | None = None
        self._is_new = True
        self.removed: list[str] = []

    async def get_or_create(self, key, agent=None, channel_id=None, start_priority=None):
        self.keys_seen.append(key)
        self.last_agent = agent
        self.last_channel_id = channel_id
        was_new = self._is_new
        self._is_new = False
        return self._provider, was_new, False

    def check_context_usage(self, key, provider):
        return 0.0

    def record_success(self, key):
        pass

    async def record_failure(self, key):
        return False

    async def try_acquire(self, key):
        # Mirror the real SessionManager: succeed only when a session exists
        # (an idle one to compact); the tests never simulate a busy turn.
        return self.has_session(key)

    def release(self, key):
        pass

    def is_busy(self, key):
        # Interface parity with the real SessionManager. False = no other turn is
        # in flight, so the post-footer re-check treats a recorded OPTIONS control
        # as still current — which is what these single-turn tests exercise.
        return False

    def begin_turn(self, key):
        # Pre-dispatch gate parity with the real SessionManager (open state).
        pass

    async def set_channel(self, key, channel_id):
        pass

    def get_channel(self, key):
        return None

    def set_slack_link(self, key, thread_ts, channel_id):
        pass

    def get_slack_link(self, key):
        return None, None

    def get_session_for_thread(self, thread_ts):
        return None

    async def close_all(self):
        pass

    async def remove(self, key):
        self.removed.append(key)

    async def destroy(self, key):
        self.removed.append(f"destroy:{key}")

    async def discard_conversation(self, key):
        self.removed.append(f"discard:{key}")

    def has_session(self, key):
        return key in self.keys_seen

    def get_provider(self, key):
        sess = getattr(self, "_sessions", {}).get(key)
        return sess.provider if sess else None

    async def reset(self, key):
        self.removed.append(f"reset:{key}")

    def get_pid(self, key):
        return None

    def enqueue(self, key, msg_ts, text, **kwargs):
        return False

    def is_cancelled(self, key, msg_ts):
        return False

    def dequeue(self, key):
        return None

    def clear_queue(self, key, owned_by=None):
        pass

    # Interface parity with the real SessionManager's user-Stop record: a
    # replay gap is opened around a transient-compaction reset so a Stop landing
    # while the key has no session still counts. These tests never issue one,
    # so the count stays wherever a test's own override puts it.
    def stop_generation(self, key):
        return getattr(self, "_stop_gen", 0)

    # Idempotent like the real manager: reopening an open gap keeps it and
    # closing a closed one is a no-op, so the recorded sequence is the gap's
    # actual lifetime rather than a count of call sites.
    def open_replay_gap(self, key):
        if not getattr(self, "_gap_open", False):
            self.replay_gaps = getattr(self, "replay_gaps", []) + [("open", key)]
        self._gap_open = True

    def close_replay_gap(self, key):
        if getattr(self, "_gap_open", False):
            self.replay_gaps = getattr(self, "replay_gaps", []) + [("close", key)]
        self._gap_open = False

    async def stop_turn(self, key, *, force=False, owned_by=None, on_soft=None, on_hard=None):
        """Fake stop_turn that defaults to 'soft' outcome."""
        outcome = getattr(self, "_stop_outcome", "soft")
        self.removed.append(f"stop_turn:{key}:force={force}")
        if outcome == "soft" and on_soft:
            await on_soft()
        elif outcome == "hard" and on_hard:
            await on_hard()
        return outcome

    def compact_wait_budget_secs(self) -> float:
        """The real manager's resolved ``session.compact_wait_secs`` (unset: 300 s)."""
        return 300.0


def _visible_texts(slack):
    """Every text the thread saw: fresh posts and edits of the placeholder."""
    return [a[1]["text"] for a in slack.actions if a[0] in ("post", "update")]
