"""Registration-throttle death classification (client / session_handle / session_provider).

A subagent runtime that dies after its dynamic registration is throttled
(HTTP 429 on consecutive attempts) is endpoint throttling, not a crash. The
death paths classify that evidence into the typed transient
``AcpRegistrationRateLimited`` so the retry ladders recover it, instead of the
generic terminal ``AcpProcessDied`` whose message carries a repetitive stderr
wall.

The signature is CONJUNCTIVE per stderr line — registration context AND an
unambiguous too-many-requests marker on the same line — so neither a model-turn
throttle nor an unrelated registration failure can fire it.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp.client import (
    AcpProcessDied,
    AcpRegistrationRateLimited,
    is_registration_throttle_output,
    registration_rate_limited_error,
    registration_throttle_line,
)
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.llm_helpers import acp_error_is_transient

# The sanitized line the field report retained, verbatim shape.
_THROTTLE_LINE = (
    "Dynamic registration failed: Registration failed: HTTP 429 Too Many "
    "Requests: This endpoint has been requested too many times. Try again later."
)


# ── signature ──


def test_matches_observed_registration_throttle_line():
    assert registration_throttle_line(_THROTTLE_LINE) == _THROTTLE_LINE
    assert is_registration_throttle_output(_THROTTLE_LINE)


def test_returns_first_matching_line_from_a_multi_line_tail():
    tail = "\n".join(["boot noise", _THROTTLE_LINE, _THROTTLE_LINE, "exit"])
    assert registration_throttle_line(tail) == _THROTTLE_LINE


def test_429_without_registration_context_does_not_match():
    """A model-turn throttle has its own classifier; this one must not fire."""
    assert registration_throttle_line("HTTP 429 Too Many Requests from Bedrock") is None


def test_registration_failure_without_throttle_marker_does_not_match():
    """An auth-rejected or malformed registration is terminal, never a throttle."""
    line = "Dynamic registration failed: Registration failed: HTTP 401 Unauthorized"
    assert registration_throttle_line(line) is None


def test_conjunction_is_per_line_not_per_tail():
    """A registration failure on one line plus a throttle mention on another
    must not combine into a verdict neither line supports."""
    tail = "Dynamic registration failed: bad response\nupstream said too many requests"
    assert registration_throttle_line(tail) is None


def test_bare_429_digits_are_not_a_marker():
    """A digit landing in an exit code or byte count must not upgrade a crash."""
    assert registration_throttle_line("Dynamic registration failed: code 429") is None


def test_requested_too_many_times_wording_matches():
    line = "registration failed: this endpoint has been requested too many times"
    assert registration_throttle_line(line) == line


# ── typed error ──


def test_error_is_a_process_death_and_transient():
    """Subclass of AcpProcessDied (every death handler keeps catching it) with
    the structured transient verdict the retry ladders read."""
    exc = registration_rate_limited_error("Runtime process died during prompt", _THROTTLE_LINE)
    assert isinstance(exc, AcpRegistrationRateLimited)
    assert isinstance(exc, AcpProcessDied)
    assert exc.transient is True
    assert acp_error_is_transient(exc)


def test_error_message_keeps_one_cause_and_guidance():
    exc = registration_rate_limited_error("base", _THROTTLE_LINE)
    msg = str(exc)
    assert msg.startswith("base — ")
    assert "rate-limited" in msg
    assert "retry later" in msg
    assert msg.count("Try again later") == 1  # one retained cause, never a wall


# ── session_handle._died ──


class _ThrottledRuntime:
    """Runtime double whose retained stderr shows the throttled registration."""

    def __init__(self, tail: str) -> None:
        self._tail = tail
        self.pid = None
        self.is_alive = lambda: False
        self.acp_backend = "kiro"

    def redacted_stderr_tail(self) -> str:
        return self._tail

    def death_summary(self) -> str | None:
        return f"killed [returncode=None] stderr_tail: {self._tail}"

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass


class _LegacyRuntime:
    """Runtime double WITHOUT redacted_stderr_tail — the getattr guard keeps
    the generic path for doubles and out-of-tree protocol implementations."""

    def __init__(self) -> None:
        self.pid = None
        self.is_alive = lambda: False

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass


def _handle(runtime) -> AcpSessionHandle:
    return AcpSessionHandle("sA", asyncio.Queue(), runtime)


def test_died_types_a_registration_throttled_death():
    tail = "\n".join([_THROTTLE_LINE] * 5)
    exc = _handle(_ThrottledRuntime(tail))._died("Runtime process died during prompt")
    assert isinstance(exc, AcpRegistrationRateLimited)
    assert exc.transient is True
    assert str(exc).count("Try again later") == 1


def test_died_stays_generic_without_the_signature():
    exc = _handle(_ThrottledRuntime("segfault at 0x0"))._died("Runtime process died during prompt")
    assert type(exc) is AcpProcessDied
    assert "segfault" in str(exc)


def test_died_degrades_on_runtime_without_stderr_tail():
    exc = _handle(_LegacyRuntime())._died("Runtime process died during prompt")
    assert type(exc) is AcpProcessDied
    assert str(exc) == "Runtime process died during prompt"


# ── session_provider._translate_dead ──


def _provider(runtime, prompt_or_tool_seen: bool = False):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.acp.types import AcpPromptStats

    handle = MagicMock()
    handle.session_id = "s1"
    handle.memory_mode = "persistent"
    handle.last_prompt_stats = AcpPromptStats()
    handle.destroy = AsyncMock()
    handle.prompt_or_tool_seen = prompt_or_tool_seen
    return AcpSessionProvider(handle, runtime)


def test_translate_dead_types_a_registration_throttled_death():
    from unittest.mock import MagicMock

    from kiro_crew.acp.runtime import AcpRuntimeDead

    runtime = MagicMock()
    runtime.saw_not_logged_in.return_value = False
    runtime.redacted_stderr_tail.return_value = "\n".join([_THROTTLE_LINE] * 5)
    exc = _provider(runtime)._translate_dead(AcpRuntimeDead("process exited (rc=1)"))
    assert isinstance(exc, AcpRegistrationRateLimited)
    assert exc.transient is True


def test_translate_dead_auth_wins_over_a_throttle_line():
    """A rejected credential is terminal and actionable; a stray throttle line
    in the same tail must not downgrade it to a retryable throttle."""
    from unittest.mock import MagicMock

    from kiro_crew.acp.client import AcpAuthRequired
    from kiro_crew.acp.runtime import AcpRuntimeDead

    runtime = MagicMock()
    runtime.saw_not_logged_in.return_value = True
    runtime.acp_backend = "kiro"
    runtime.redacted_stderr_tail.return_value = _THROTTLE_LINE
    exc = _provider(runtime)._translate_dead(AcpRuntimeDead("process exited (rc=1)"))
    assert isinstance(exc, AcpAuthRequired)


def test_translate_dead_stays_generic_without_the_signature():
    from unittest.mock import MagicMock

    from kiro_crew.acp.runtime import AcpRuntimeDead

    runtime = MagicMock()
    runtime.saw_not_logged_in.return_value = False
    runtime.redacted_stderr_tail.return_value = "reader crashed"
    exc = _provider(runtime)._translate_dead(AcpRuntimeDead("process exited (rc=1)"))
    assert type(exc) is AcpProcessDied


# ── activity latch: classification is pre-work only ──


def test_died_stays_generic_after_prompt_or_tool_activity():
    """A stale throttle line surviving in the ring past real work must not hand
    the retry ladders a transient verdict for a session whose replay could
    repeat side effects."""
    h = _handle(_ThrottledRuntime(_THROTTLE_LINE))
    h._prompt_or_tool_seen = True
    exc = h._died("Runtime process died during prompt")
    assert type(exc) is AcpProcessDied


def test_translate_dead_stays_generic_after_prompt_or_tool_activity():
    from unittest.mock import MagicMock

    from kiro_crew.acp.runtime import AcpRuntimeDead

    runtime = MagicMock()
    runtime.saw_not_logged_in.return_value = False
    runtime.redacted_stderr_tail.return_value = _THROTTLE_LINE
    provider = _provider(runtime, prompt_or_tool_seen=True)
    exc = provider._translate_dead(AcpRuntimeDead("process exited (rc=1)"))
    assert type(exc) is AcpProcessDied


def test_translate_dead_fails_closed_on_a_handle_without_the_latch():
    """A handle double without the latch must never widen the retry surface."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp.runtime import AcpRuntimeDead
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.acp.types import AcpPromptStats

    handle = MagicMock(spec=["session_id", "memory_mode", "last_prompt_stats", "destroy"])
    handle.session_id = "s1"
    handle.memory_mode = "persistent"
    handle.last_prompt_stats = AcpPromptStats()
    handle.destroy = AsyncMock()
    runtime = MagicMock()
    runtime.saw_not_logged_in.return_value = False
    runtime.redacted_stderr_tail.return_value = _THROTTLE_LINE
    provider = AcpSessionProvider(handle, runtime)
    exc = provider._translate_dead(AcpRuntimeDead("process exited (rc=1)"))
    assert type(exc) is AcpProcessDied


def test_handle_latch_closes_on_a_delivered_text_chunk():
    """The dispatch accounting latches on the first delivered text chunk, and
    a later throttled death then classifies generically."""
    from kiro_crew.acp.types import METHOD_SESSION_UPDATE, JsonRpcMessage

    h = _handle(_ThrottledRuntime(_THROTTLE_LINE))
    assert h.prompt_or_tool_seen is False
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hi"},
            },
        },
    )
    events = h._handle_update(msg)
    assert events, "the text chunk must be delivered"
    assert h.prompt_or_tool_seen is True
    assert type(h._died("Runtime process died during prompt")) is AcpProcessDied


# ── direct client (AcpClient) latch gate ──


@pytest.mark.asyncio
async def test_client_reader_refuses_once_the_process_saw_work():
    """`AcpClient._registration_throttle_line` classifies while the process has
    produced no work, and refuses once `_prompt_or_tool_seen` is latched — the
    same gate the dispatch loop closes on the first text chunk, tool event, or
    a native child's fanned-out tool call."""
    from collections import deque

    from kiro_crew.acp.client import AcpClient

    c = object.__new__(AcpClient)
    c._stderr_task = None
    c._stderr_lines = deque([_THROTTLE_LINE], maxlen=20)
    c._prompt_or_tool_seen = False
    line = await c._registration_throttle_line()
    assert line is not None and "429" in line
    c._prompt_or_tool_seen = True
    assert await c._registration_throttle_line() is None


@pytest.mark.asyncio
async def test_client_reader_fails_closed_without_the_latch_attribute():
    """A client built without __init__ (a test double's shape) must refuse to
    classify rather than widen the retry surface."""
    from collections import deque

    from kiro_crew.acp.client import AcpClient

    c = object.__new__(AcpClient)
    c._stderr_task = None
    c._stderr_lines = deque([_THROTTLE_LINE], maxlen=20)
    assert await c._registration_throttle_line() is None


# ── KAS child activity builders close the window ──


def test_kas_child_tool_prefix_closes_the_window():
    """A KAS child's nested tool frame closes the registration-throttle window
    in the one builder every call site shares."""
    h = _handle(_ThrottledRuntime(_THROTTLE_LINE))
    assert h.prompt_or_tool_seen is False
    events = h._build_child_tool_activity_prefix(
        {
            "toolCallId": "tc1",
            "title": "write file",
            "_meta": {"kiro": {"agentSubtaskId": "sub1"}},
        }
    )
    assert events and events[0].tool_call_id == "tc1"
    assert h.prompt_or_tool_seen is True
    assert type(h._died("Runtime process died during prompt")) is AcpProcessDied


def test_kas_child_text_activity_closes_the_window():
    """A KAS child's streamed text also closes the window: observed child
    output means the wave did work, whatever happened to its tool frames."""
    h = _handle(_ThrottledRuntime(_THROTTLE_LINE))
    assert h.prompt_or_tool_seen is False
    events = h._handle_kas_subagent_chunk(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "child says hi"},
            "_meta": {"kiro": {"agentSubtaskId": "sub1"}},
        }
    )
    assert events and events[0].text
    assert h.prompt_or_tool_seen is True


def test_kas_parent_lifecycle_frame_closes_the_window():
    """A KAS parent subtask frame means children were spawned; a spawned child
    can mutate state before its first activity frame is observed, so the
    lifecycle frame itself closes the window."""
    h = _handle(_ThrottledRuntime(_THROTTLE_LINE))
    assert h.prompt_or_tool_seen is False
    events = h._handle_kas_subagent(
        {
            "title": "Sub-agent: researcher",
            "status": "in_progress",
            "_meta": {"kiro": {"agentSubtaskId": "sub1", "kind": "agent-subtask"}},
        }
    )
    assert events is not None
    assert h.prompt_or_tool_seen is True
    assert type(h._died("Runtime process died during prompt")) is AcpProcessDied
