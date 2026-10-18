"""KAS drops a prompt that got no answer before the same prompt is sent again.

KAS stores the user row before the model request, so a turn that fails (a
throttle) leaves that row in the model's history. Re-sending the prompt then adds a
second copy. ``AcpSessionHandle.discard_unanswered_prompt`` rewinds the session to
before that row with the engine's ``_kiro/checkpoint/revertMultiple`` verb, keyed by
the row id KAS reports as a ``user_message_id_assigned`` session_info_update.

These pin the verb and its params, and every case where the handle must send
nothing: not KAS, no checkpoints capability, a later turn already started, a steer
landed after the prompt, a second call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import METHOD_PROMPT, METHOD_SESSION_UPDATE, JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.providers.acp import AcpProvider

# The engine's wire name, spelled out: a rename on Crew's side must not move it.
_REVERT = "_kiro/checkpoint/revertMultiple"
_SID = "sK"
_THROTTLE = {
    "code": -32603,
    "message": "Prompt failed",
    "data": {
        "message": "Too many requests, please wait before trying again.",
        "errorType": "ClientThrottleError",
        "retryErrorType": "THROTTLING",
    },
}


def _info(kind: str, **fields: Any) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": _SID,
            "update": {
                "sessionUpdate": "session_info_update",
                "_meta": {"kiro": {"kind": kind, **fields}},
            },
        },
    )


def _row_assigned(row_id: str) -> JsonRpcMessage:
    return _info("user_message_id_assigned", userMessageId=row_id)


class _Runtime:
    """Answers each request with the next scripted (frames, result, error) step."""

    def __init__(
        self,
        queue: asyncio.Queue,
        steps: list[tuple[list[JsonRpcMessage], Any, Any]],
        *,
        acp_backend: str = ACP_BACKEND_KAS,
        checkpoints: bool = True,
    ) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = acp_backend
        self.requests: list[tuple[str, dict]] = []
        self._queue = queue
        self._steps = list(steps)
        self._next_id = 20
        self._last_activity = time.monotonic()
        self._agent_capabilities: dict = {"_meta": {"kiro": {"checkpoints": checkpoints}}}

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_request(
        self, method: str, params: dict, *, on_reserved: Callable[[int], None] | None = None
    ) -> int:
        self._next_id += 1
        req_id = self._next_id
        if on_reserved is not None:
            on_reserved(req_id)
        self.requests.append((method, params))
        frames, result, error = self._steps.pop(0)
        for frame in frames:
            self._queue.put_nowait(frame)
        self._queue.put_nowait(JsonRpcMessage(id=req_id, result=result, error=error))
        return req_id


def _make(steps, **runtime_kw) -> tuple[AcpSessionHandle, _Runtime]:
    queue: asyncio.Queue = asyncio.Queue()
    runtime = _Runtime(queue, steps, **runtime_kw)
    return AcpSessionHandle(_SID, queue, runtime), runtime


async def _prompt(handle: AcpSessionHandle, text: str) -> None:
    async for _event in handle.prompt(text, timeout=5.0):
        pass


async def _throttled_prompt(handle: AcpSessionHandle, text: str = "hello") -> None:
    with pytest.raises(AcpError) as raised:
        await _prompt(handle, text)
    assert raised.value.throttle is True


_REVERTED = ([], {"success": True, "affectedFiles": [], "totalFiles": 0}, None)


@pytest.mark.asyncio
async def test_rewind_sends_the_revert_verb_for_the_failed_prompt_row() -> None:
    handle, runtime = _make([([_row_assigned("row-1")], None, _THROTTLE), _REVERTED])
    await _throttled_prompt(handle)

    assert await handle.discard_unanswered_prompt() is True
    assert runtime.requests[-1] == (_REVERT, {"sessionId": _SID, "messageId": "row-1"})


@pytest.mark.asyncio
async def test_rewind_is_one_shot() -> None:
    handle, runtime = _make([([_row_assigned("row-1")], None, _THROTTLE), _REVERTED])
    await _throttled_prompt(handle)
    assert await handle.discard_unanswered_prompt() is True
    sent = len(runtime.requests)

    assert await handle.discard_unanswered_prompt() is False
    assert len(runtime.requests) == sent


@pytest.mark.asyncio
async def test_a_refused_revert_reads_false() -> None:
    not_found = ([], {"success": False, "error": 'Message "row-1" not found'}, None)
    handle, _runtime = _make([([_row_assigned("row-1")], None, _THROTTLE), not_found])
    await _throttled_prompt(handle)

    assert await handle.discard_unanswered_prompt() is False


@pytest.mark.asyncio
async def test_no_rewind_once_a_later_turn_started() -> None:
    """The row belongs to the earlier turn; rewinding to it would drop the later one."""
    handle, runtime = _make(
        [
            ([_row_assigned("row-1")], None, _THROTTLE),
            ([], {"stopReason": "end_turn"}, None),
        ]
    )
    await _throttled_prompt(handle)
    await _prompt(handle, "a later turn whose row id never arrived")

    assert await handle.discard_unanswered_prompt() is False
    assert [method for method, _ in runtime.requests] == [METHOD_PROMPT, METHOD_PROMPT]


@pytest.mark.asyncio
async def test_no_rewind_after_a_steer_landed_in_the_turn() -> None:
    """A steer sits in history after the prompt row, so a rewind would drop it too."""
    steered = [_row_assigned("row-1"), _info("steering_injected", content="also do X")]
    handle, runtime = _make([(steered, None, _THROTTLE)])
    await _throttled_prompt(handle)

    assert await handle.discard_unanswered_prompt() is False
    assert [method for method, _ in runtime.requests] == [METHOD_PROMPT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_kw", [{"checkpoints": False}, {"acp_backend": ACP_BACKEND_KIRO}], ids=str
)
async def test_no_rewind_without_checkpoints_or_off_kas(runtime_kw: dict) -> None:
    """Off KAS no row is recorded at all, so the capability flag alone never sends."""
    handle, runtime = _make([([_row_assigned("row-1")], None, _THROTTLE)], **runtime_kw)
    with pytest.raises(AcpError):
        await _prompt(handle, "hello")

    assert await handle.discard_unanswered_prompt() is False
    assert [method for method, _ in runtime.requests] == [METHOD_PROMPT]


@pytest.mark.asyncio
async def test_no_rewind_when_kas_reported_no_row() -> None:
    handle, runtime = _make([([], None, _THROTTLE)])
    await _throttled_prompt(handle)

    assert await handle.discard_unanswered_prompt() is False
    assert [method for method, _ in runtime.requests] == [METHOD_PROMPT]


@pytest.mark.asyncio
async def test_providers_forward_the_rewind_and_default_to_false() -> None:
    """The session provider forwards to its handle; a client without one answers False."""
    session_provider = AcpSessionProvider.__new__(AcpSessionProvider)
    session_provider._handle = MagicMock()
    session_provider._handle.discard_unanswered_prompt = AsyncMock(return_value=True)
    assert await session_provider.discard_unanswered_prompt() is True

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = session_provider
    assert await provider.discard_unanswered_prompt() is True
    provider._client = object()
    assert await provider.discard_unanswered_prompt() is False
