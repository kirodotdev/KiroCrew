"""Crew compaction entry points while an abandoned idle compaction's status is owed.

``stream_command("/compact")`` raises ``AcpError``. A refused ``compact()`` sends
nothing, and its ``wait_for_compaction`` reports one ``failed`` result. Owed
statuses already queued on an idle reader are consumed before either refuses.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import stale_compaction
from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, JsonRpcMessage
from kiro_crew.providers.acp import AcpProvider


def _build_provider(backend: str) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


async def _drain(it):
    return [x async for x in it]


def _async_iter(items):
    async def _gen():
        for it in items:
            yield it

    return _gen()


class TestUnresolvedCompactionRefusal:
    """Crew compaction entry points refuse an unresolved status owner."""

    @staticmethod
    def _status(status_type: str) -> JsonRpcMessage:
        return JsonRpcMessage(
            method="_kiro.dev/compaction/status", params={"status": {"type": status_type}}
        )

    @staticmethod
    def _entry(tmp_path, shared: bool, entry: str) -> tuple[Any, Any, Any]:
        """A provider, its status reader, and the mock that would send the compaction."""
        if shared:
            reader = AcpSessionHandle("s1", asyncio.Queue(), MagicMock())
            runtime = MagicMock()
            runtime.acp_backend = ""
            provider = AcpSessionProvider(reader, runtime=runtime)
            provider._claim_shared_turn = MagicMock(return_value=None)
            provider._end_shared_turn = AsyncMock()
            reader.command_is_native = lambda _command: False
        else:
            reader = AcpClient(work_dir=tmp_path)
            reader.ensure_ready = AsyncMock()
            reader.confirm_member_projection = AsyncMock()
            provider = _build_provider(backend="")
            provider._client = reader
        wire = MagicMock(return_value=_async_iter([]))
        if entry == "compact" and shared:
            wire = AsyncMock()
            reader.compact = wire
        elif entry == "compact":
            reader.stream_events = wire
        else:
            reader.stream_command = wire
        return provider, reader, wire

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shared", [False, True], ids=["dedicated", "shared"])
    @pytest.mark.parametrize("entry", ["stream_command", "compact"])
    async def test_entry_refuses_then_runs_at_count_zero(self, tmp_path, shared, entry):
        provider, reader, wire = self._entry(tmp_path, shared, entry)
        assert stale_compaction.discard_provider_stale_compaction_status(
            provider, within_secs=300.0
        )

        if entry == "compact":
            # A refused compact() raises nothing, so a caller's broad except
            # cannot answer it with a conversation reset. Its wait reports it.
            await provider.compact()
            assert await provider.wait_for_compaction(timeout=1.0) == {
                "type": "failed",
                "summary": stale_compaction.UNRESOLVED_COMPACTION_ERROR,
            }
            assert stale_compaction.take_provider_compaction_refusal(provider) is None
        else:
            with pytest.raises(AcpError) as raised:
                await _drain(provider.stream_command("/compact"))
            assert str(raised.value) == stale_compaction.UNRESOLVED_COMPACTION_ERROR

        wire.assert_not_called()
        assert stale_compaction.is_stale_compaction_status(reader, "failed") is True
        if entry == "compact":
            await provider.compact()
            wire.assert_called_once_with("" if shared else "/compact")
            assert stale_compaction.take_provider_compaction_refusal(provider) is None
        else:
            await _drain(provider.stream_command("/compact"))
            wire.assert_called_once_with("/compact")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("where", ["handle_queue", "client_buffer", "client_notifications"])
    @pytest.mark.parametrize("entry", ["stream_command", "compact"])
    async def test_an_owed_terminal_already_queued_is_consumed_before_the_check(
        self, tmp_path, where, entry
    ):
        provider, reader, wire = self._entry(tmp_path, where == "handle_queue", entry)
        before = JsonRpcMessage(
            method="session/update", params={"update": {"sessionUpdate": "agent_message_chunk"}}
        )
        after = JsonRpcMessage(method="_kiro.dev/metadata", params={})
        assert stale_compaction.discard_provider_stale_compaction_status(
            provider, within_secs=300.0
        )
        frames = (before, self._status("completed"), after)
        if where == "handle_queue":
            for frame in frames:
                reader._queue.put_nowait(frame)
        elif where == "client_buffer":
            reader._buffer.extend(frames)
        else:
            reader._mcp_notifications.extend(frames)
            reader._mcp_report_frame_floor = 2

        if entry == "compact":
            await provider.compact()
        else:
            await _drain(provider.stream_command("/compact"))

        wire.assert_called_once()
        assert reader._unresolved_compaction_statuses is None
        if where == "handle_queue":
            kept = [reader._queue.get_nowait() for _ in range(reader._queue.qsize())]
        elif where == "client_buffer":
            kept = list(reader._buffer)
        else:
            kept = list(reader._mcp_notifications)
            # The floor still marks the frame it marked before the removal.
            assert reader._mcp_report_frame_floor == 1
        assert len(kept) == 2 and kept[0] is before and kept[1] is after

    @pytest.mark.asyncio
    async def test_a_reader_inside_a_turn_keeps_its_frames_for_that_turn(self):
        provider, reader, wire = self._entry(None, True, "compact")
        assert stale_compaction.discard_provider_stale_compaction_status(
            provider, within_secs=300.0
        )
        late = self._status("completed")
        reader._queue.put_nowait(late)
        reader._turn_done.clear()

        await provider.compact()

        wire.assert_not_called()
        assert reader._queue.get_nowait() is late
        assert reader._unresolved_compaction_statuses == 1

    @pytest.mark.asyncio
    async def test_shared_normal_readers_drop_unresolved_terminals(self):
        handle = AcpSessionHandle("s1", asyncio.Queue(), MagicMock())
        stale_compaction.discard_stale_compaction_status(handle, within_secs=0.0)
        await handle._queue.put(self._status("failed"))
        await handle._queue.put(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))

        events = [event async for event in handle._dispatch_events(1, 1.0)]

        assert EVENT_COMPACTION_STATUS not in [event.kind for event in events]
        assert handle._compaction_failed_at is None
        assert handle._unresolved_compaction_statuses is None

        stale_compaction.discard_stale_compaction_status(handle, within_secs=0.0)
        await handle._queue.put(self._status("completed"))
        await handle._queue.put(self._status("failed"))
        result = await handle.wait_for_compaction(timeout=1.0)
        assert result["type"] == "failed"
        assert handle._unresolved_compaction_statuses is None
