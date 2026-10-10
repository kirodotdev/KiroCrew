"""A manual ``/compact`` on KAS goes to the engine's ``_kiro/session/compact`` verb.

Sent as ``session/prompt`` text, ``/compact`` makes the KAS model write a summary as
its reply and the context does not shrink. The engine's own verb is what compacts:
it answers ``{"success": bool}`` and, when it commits a summary, sends
``summarization_completed`` first. These pin the route, the outcome each answer
reports, and that every other backend keeps the prompt it already answers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    METHOD_KAS_SESSION_COMPACT,
    METHOD_PROMPT,
    METHOD_SESSION_UPDATE,
    STOP_REASON_END_TURN,
    JsonRpcMessage,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_HARNESS_MANAGED_COMPACTION,
    ACP_BACKENDS_INLINE_COMPACTION,
)
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session_compaction import _compact_unsupported_backend, _harness_managed_backend

_REQ_ID = 11


class _Runtime:
    """Records the outbound request and answers it with a scripted result.

    The scripted frames and the answer are queued AFTER the send, so the pre-turn
    stale-frame drain cannot eat them.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        *,
        acp_backend: str,
        result: dict[str, Any],
        updates: list[JsonRpcMessage] | None = None,
    ) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = acp_backend
        self.requests: list[tuple[str, dict]] = []
        self._queue = queue
        self._result = result
        self._updates = updates or []
        self._last_activity = time.monotonic()
        self._agent_capabilities: dict = {
            "_meta": {"kiro": {"extensionMethods": [METHOD_KAS_SESSION_COMPACT]}}
        }

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_request(self, method: str, params: dict) -> int:
        self.requests.append((method, params))
        for frame in self._updates:
            self._queue.put_nowait(frame)
        self._queue.put_nowait(JsonRpcMessage(id=_REQ_ID, result=self._result))
        return _REQ_ID


def _summarization_completed(summary: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sK",
            "update": {
                "sessionUpdate": "session_info_update",
                "_meta": {
                    "kiro": {
                        "kind": "summarization_completed",
                        "conversationSummary": summary,
                        "truncated": False,
                    }
                },
            },
        },
    )


def _make(
    *, backend: str = ACP_BACKEND_KAS, result: dict[str, Any], updates=None
) -> tuple[AcpSessionHandle, _Runtime]:
    queue: asyncio.Queue = asyncio.Queue()
    rt = _Runtime(queue, acp_backend=backend, result=result, updates=updates)
    return AcpSessionHandle("sK", queue, rt), rt


async def _prompt(handle: AcpSessionHandle, text: str) -> list:
    return [ev async for ev in handle.prompt(text, timeout=5.0)]


def _statuses(events: list) -> list[tuple[str, str]]:
    return [(e.text, e.title) for e in events if e.kind == EVENT_COMPACTION_STATUS]


# ── The route ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/compact", "  /compact  ", "/compact keep the plan"])
async def test_kas_compact_sends_the_engine_verb(text: str) -> None:
    """The verb takes only the session id, so text after ``/compact`` is not sent."""
    handle, rt = _make(result={"success": True}, updates=[_summarization_completed("s")])
    await _prompt(handle, text)
    assert rt.requests == [(METHOD_KAS_SESSION_COMPACT, {"sessionId": "sK"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["compact", "/compaction", "please /compact", "/help"])
async def test_other_kas_text_stays_a_prompt(text: str) -> None:
    handle, rt = _make(result={"stopReason": "end_turn"})
    await _prompt(handle, text)
    assert [m for m, _ in rt.requests] == [METHOD_PROMPT]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CODEX])
async def test_other_backends_keep_the_compact_prompt(backend: str) -> None:
    handle, rt = _make(backend=backend, result={"stopReason": "end_turn"})
    await _prompt(handle, "/compact")
    assert [m for m, _ in rt.requests] == [METHOD_PROMPT]
    assert rt.requests[0][1]["prompt"][0]["text"] == "/compact"


@pytest.mark.asyncio
async def test_the_next_prompt_after_a_compact_is_ordinary() -> None:
    """The per-turn flag is reset, so a later turn is not read as a compaction."""
    handle, rt = _make(result={"success": True}, updates=[_summarization_completed("s")])
    await _prompt(handle, "/compact")
    rt._result = {"stopReason": "end_turn"}
    rt._updates = []
    events = await _prompt(handle, "hello")
    assert rt.requests[-1][0] == METHOD_PROMPT
    assert _statuses(events) == []


# ── The outcome ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_committed_summary_reports_once_and_ends_the_turn() -> None:
    """The engine's own frame is the status; the answer adds no second one."""
    handle, _ = _make(result={"success": True}, updates=[_summarization_completed("kept")])
    handle.last_prompt_stats.context_pct = 18.0
    handle.last_prompt_stats.context_tokens_from_usage = True
    events = await _prompt(handle, "/compact")
    assert _statuses(events) == [("completed", "kept")]
    assert events[-1].kind == EVENT_COMPLETE
    assert events[-1].stop_reason == STOP_REASON_END_TURN
    # The frame reset the meter, as it does for KAS's own summarization.
    assert handle.last_prompt_stats.context_tokens_from_usage is False


@pytest.mark.asyncio
async def test_success_with_nothing_summarized_completes_without_a_reset() -> None:
    """An empty history answers ``success: true`` with no frame. The context did
    not change, so the meter keeps its reading."""
    handle, _ = _make(result={"success": True})
    handle.last_prompt_stats.context_pct = 6.0
    handle.last_prompt_stats.context_tokens_from_usage = True
    events = await _prompt(handle, "/compact")
    assert _statuses(events) == [("completed", "")]
    assert handle.last_prompt_stats.context_tokens_from_usage is True
    assert events[-1].stop_reason == STOP_REASON_END_TURN


@pytest.mark.asyncio
async def test_a_refusal_reports_failed_with_a_reason() -> None:
    """``success: false`` (a turn or another compaction running) sends no frame."""
    handle, _ = _make(result={"success": False})
    events = await _prompt(handle, "/compact")
    statuses = _statuses(events)
    assert len(statuses) == 1
    assert statuses[0][0] == "failed"
    assert "KAS did not compact" in statuses[0][1]
    assert handle.last_compaction_transient is False
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_compact_then_wait_returns_the_turns_outcome() -> None:
    """``compact()`` caches the status from inside the turn, so the wait every
    channel pairs with it returns at once: no status wait, and no 5s grace for a
    ``_kiro.dev/metadata`` frame KAS never sends."""
    handle, _ = _make(result={"success": True}, updates=[_summarization_completed("done")])
    await handle.compact()
    result = await asyncio.wait_for(handle.wait_for_compaction(timeout=30.0), timeout=2.0)
    assert result == {"type": "completed", "summary": "done"}


@pytest.mark.asyncio
async def test_compact_then_wait_returns_a_refusal() -> None:
    handle, _ = _make(result={"success": False})
    await handle.compact()
    result = await asyncio.wait_for(handle.wait_for_compaction(timeout=30.0), timeout=2.0)
    assert result["type"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caps",
    [
        {},
        {"_meta": {"kiro": {"extensionMethods": ["_kiro/session/history"]}}},
        {"_meta": {"kiro": {"extensionMethods": "_kiro/session/compact"}}},
    ],
)
async def test_an_engine_without_the_verb_fails_without_sending(caps) -> None:
    """No verb in ``extensionMethods``: nothing is sent, and the turn reports a
    failed compaction instead of a JSON-RPC error or a fake prompt."""
    handle, rt = _make(result={"success": True})
    rt._agent_capabilities = caps
    events = await _prompt(handle, "/compact")
    assert rt.requests == []
    statuses = _statuses(events)
    assert [s for s, _ in statuses] == ["failed"]
    assert "no compaction command" in statuses[0][1]
    assert events[-1].kind == EVENT_COMPLETE
    await handle.compact()
    result = await asyncio.wait_for(handle.wait_for_compaction(timeout=30.0), timeout=2.0)
    assert result["type"] == "failed"


# ── The gates ────────────────────────────────────────────────────────────────


def test_kas_offers_manual_compact_and_keeps_its_own_threshold() -> None:
    assert ACP_BACKEND_KAS in ACP_BACKENDS_COMPACT
    assert ACP_BACKEND_KAS in ACP_BACKENDS_HARNESS_MANAGED_COMPACTION
    # The outcome rides the turn as a status, not as the turn's terminal alone.
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_INLINE_COMPACTION


def test_a_real_kas_provider_is_not_refused_a_manual_compact() -> None:
    provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
    assert provider.manual_compact_unsupported_backend is None
    assert _compact_unsupported_backend(provider) is None


def test_the_threshold_still_leaves_kas_to_its_engine() -> None:
    """Crew's ``session.autocompact_pct`` still declines KAS: its engine
    summarizes on its own, and this change opens only the manual command."""
    assert _harness_managed_backend(AcpProvider(acp_backend=ACP_BACKEND_KAS)) == ACP_BACKEND_KAS
    for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_CODEX):
        assert _harness_managed_backend(AcpProvider(acp_backend=backend)) is None


def test_a_mock_attribute_is_not_a_harness_managed_claim() -> None:
    assert _harness_managed_backend(MagicMock()) is None
    assert _harness_managed_backend(AsyncMock()) is None
    assert _harness_managed_backend(object()) is None
