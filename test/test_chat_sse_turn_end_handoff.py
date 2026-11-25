"""A chat stream still reading its turn gets that turn's end when the next send starts.

A turn ends (``done`` pending), and before its stream has read it the next send
on the slot starts a turn directly and clears what earlier turns left pending.
The first stream must still get its ``done``; otherwise it stays open for as
long as the slot is busy, with nothing to read.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import slot_buffers
from kiro_crew.testing.wait import async_wait_until, await_until


def _slot(pending: list[dict[str, str]], consumers: int) -> object:
    class _Slot:
        def __init__(self) -> None:
            self._pending = list(pending)
            self._pending_consumers = consumers
            self._stream_epoch = 0
            # A stream that opened at epoch 0 is the one attached.
            self._stream_handoffs: dict[int, list[dict[str, str]]] = {0: []}
            self.event = asyncio.Event()

        @contextlib.contextmanager
        def pending_consumer(self) -> Iterator[None]:
            self._pending_consumers += 1
            try:
                yield
            finally:
                self._pending_consumers -= 1

        def drain(self) -> list[dict[str, str]]:
            out, self._pending = self._pending, []
            self.event.clear()
            return out

    return _Slot()


def _frame(cls: str, content: str = "") -> dict[str, str]:
    return {"role": cls, "content": content, "cls": cls, "ts": ""}


def _send_with_reader(slot) -> int:
    """A direct send whose stream then attaches, as ``stream_reader`` records it."""
    epoch = slot_buffers.drain_stale_pending(slot)
    slot._stream_handoffs[epoch] = []
    return epoch


def test_an_attached_stream_gets_its_end_and_the_new_stream_does_not() -> None:
    slot = _slot([_frame("chunk", "a"), _frame("done"), _frame("user", "next")], consumers=1)
    new_epoch = _send_with_reader(slot)
    assert new_epoch == 1
    assert slot._pending == []
    assert slot.event.is_set()
    # The old stream reads its turn's tail and end, never the new turn's frames.
    slot._pending = [_frame("chunk", "new")]
    assert [m["content"] for m in slot_buffers.stream_frames(slot, 0)] == ["a", ""]
    assert [m["content"] for m in slot._pending] == ["new"]
    # Its final clear leaves the queue to the new stream, which reads it.
    slot._pending_consumers = 2
    slot_buffers.release_stream(slot, 0)
    assert [m["content"] for m in slot_buffers.stream_frames(slot, new_epoch)] == ["new"]


def test_without_an_attached_stream_the_clear_is_unchanged() -> None:
    slot = _slot([_frame("chunk", "a"), _frame("done")], consumers=0)
    assert slot_buffers.drain_stale_pending(slot) == 1
    assert slot._pending == []
    assert slot._stream_handoffs == {}


def test_a_later_send_does_not_replace_an_unread_end() -> None:
    slot = _slot([_frame("chunk", "a"), _frame("done")], consumers=1)
    assert _send_with_reader(slot) == 1
    # The second stream's turn ended too, unread; a third send starts.
    slot._pending_consumers = 2
    slot._pending = [_frame("chunk", "b"), _frame("done")]
    assert slot_buffers.drain_stale_pending(slot) == 2
    assert [m["content"] for m in slot_buffers.stream_frames(slot, 0)] == ["a", ""]
    assert [m["content"] for m in slot_buffers.stream_frames(slot, 1)] == ["b", ""]


def test_turns_nobody_streams_keep_nothing() -> None:
    # A stream at epoch 0 is stuck; three sends with no stream of their own end.
    slot = _slot([_frame("chunk", "a"), _frame("done")], consumers=1)
    for text in ("b", "c", "d"):
        slot_buffers.drain_stale_pending(slot)
        slot._pending = [_frame("chunk", text), _frame("done")]
    slot_buffers.drain_stale_pending(slot)
    assert list(slot._stream_handoffs) == [0]
    # The last stream to leave takes everything kept with it.
    slot_buffers.release_stream(slot, 0)
    assert slot._stream_handoffs == {}


def test_a_reader_that_ends_early_drops_its_entry() -> None:
    # The stream at epoch 0 stays; a second one leaves on an error before its end.
    slot = _slot([], consumers=1)
    epoch = slot_buffers.drain_stale_pending(slot)
    try:
        with slot_buffers.stream_reader(slot, epoch):
            assert epoch in slot._stream_handoffs
            raise ConnectionResetError
    except ConnectionResetError:
        pass
    assert slot._pending_consumers == 1
    # Its turn ends afterwards; the next send keeps nothing for it.
    slot._pending = [_frame("chunk", "b"), _frame("done")]
    slot_buffers.drain_stale_pending(slot)
    assert list(slot._stream_handoffs) == [0]


def test_a_replaced_stream_with_nothing_kept_still_ends() -> None:
    slot = _slot([], consumers=1)
    slot._stream_epoch = 3
    assert [m["cls"] for m in slot_buffers.stream_frames(slot, 1)] == ["done"]


def test_a_stream_that_already_read_its_end_keeps_nothing() -> None:
    slot = _slot([_frame("chunk", "late")], consumers=1)
    assert slot_buffers.drain_stale_pending(slot) == 1
    assert slot._pending == []
    assert slot._stream_handoffs == {0: []}


def test_an_older_streams_cleanup_keeps_a_newer_streams_end() -> None:
    # A has already read its ``done`` and is still writing it when B starts.
    slot = _slot([], consumers=1)
    b_epoch = _send_with_reader(slot)
    # B's turn ends while B's stream is blocked too; a third send starts.
    slot._pending_consumers = 2
    slot._pending = [_frame("chunk", "b"), _frame("done")]
    slot_buffers.drain_stale_pending(slot)
    # A detaches; B still reads its own tail and end.
    slot_buffers.release_stream(slot, 0)
    assert [m["content"] for m in slot_buffers.stream_frames(slot, b_epoch)] == ["b", ""]


def test_a_stream_sharing_the_slot_does_not_clear_the_others_frames() -> None:
    slot = _slot([_frame("chunk", "b")], consumers=2)
    slot_buffers.release_stream(slot, 0)
    assert [m["content"] for m in slot._pending] == ["b"]
    slot._pending_consumers = 1
    slot_buffers.release_stream(slot, 0)
    assert slot._pending == []


@pytest.mark.asyncio
@pytest.mark.parametrize("second_streams", [False, True], ids=["second-ws", "second-sse"])
async def test_next_send_does_not_take_the_open_streams_turn_end(
    tmp_path, monkeypatch, second_streams
):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")

    first_chunk_written = asyncio.Event()
    release_first_reader = asyncio.Event()
    second_turn_ended = asyncio.Event()

    from kiro_crew.dashboard import chat_handlers

    real_write = chat_handlers.write_sse_or_abort

    async def held_write(request, resp, data):
        # The first stream's chunk write waits until the second turn has
        # ended, so its own turn's end is still unread when that send runs.
        if b"first answer" in data and not first_chunk_written.is_set():
            first_chunk_written.set()
            await await_until(release_first_reader)
        await real_write(request, resp, data)

    async def fake_run_chat(st, sl, msg, **_kwargs):
        sl.append("chunk", f"{msg} answer", "chunk")
        if msg == "first":
            await await_until(first_chunk_written)
        # The turn's end, as the queue cycle writes it, then the slot is idle.
        sl.append("done", "", "done")
        sl.task = None
        if msg == "second":
            second_turn_ended.set()

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.write_sse_or_abort", held_write)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post("/api/chat", json={"message": "first", "slot": "s1"})
        first_body = asyncio.ensure_future(first.read())
        await await_until(first_chunk_written)
        await async_wait_until(
            lambda: slot.task is None or slot.task.done(),
            describe=lambda: f"task={slot.task!r}",
        )

        path = "/api/chat" if second_streams else "/api/chat?ws=1"
        second = await client.post(path, json={"message": "second", "slot": "s1"})
        assert second.status == 200
        second_body = asyncio.ensure_future(second.read())
        await await_until(second_turn_ended)
        if second_streams:
            second_data = await await_until(second_body)
            assert b"second answer" in second_data
            assert second_data.endswith(b"data: [DONE]\n\n")

        release_first_reader.set()
        # The first stream still ends on its own turn's ``done``.
        first_data = await await_until(first_body)
        await await_until(second_body)

    assert b"first answer" in first_data
    assert b"second answer" not in first_data
    assert first_data.endswith(b"data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_a_stream_that_fails_to_open_leaves_no_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")

    first_chunk_written = asyncio.Event()
    release_first_reader = asyncio.Event()
    second_turn_ended = asyncio.Event()

    from aiohttp import web

    from kiro_crew.dashboard import chat_handlers

    real_write = chat_handlers.write_sse_or_abort
    real_prepare = web.StreamResponse.prepare

    async def held_write(request, resp, data):
        if b"first answer" in data and not first_chunk_written.is_set():
            first_chunk_written.set()
            await await_until(release_first_reader)
        await real_write(request, resp, data)

    async def failing_prepare(self, request):
        # The second client is gone before its stream opens.
        if first_chunk_written.is_set() and not release_first_reader.is_set():
            raise ConnectionResetError
        return await real_prepare(self, request)

    async def fake_run_chat(st, sl, msg, **_kwargs):
        sl.append("chunk", f"{msg} answer", "chunk")
        if msg == "first":
            await await_until(first_chunk_written)
        sl.append("done", "", "done")
        sl.task = None
        if msg == "second":
            second_turn_ended.set()

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.write_sse_or_abort", held_write)
    monkeypatch.setattr(web.StreamResponse, "prepare", failing_prepare)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await client.post("/api/chat", json={"message": "first", "slot": "s1"})
        first_body = asyncio.ensure_future(first.read())
        await await_until(first_chunk_written)
        await async_wait_until(
            lambda: slot.task is None or slot.task.done(),
            describe=lambda: f"task={slot.task!r}",
        )
        second_epoch = slot._stream_epoch + 1
        try:
            await client.post("/api/chat", json={"message": "second", "slot": "s1"})
        except Exception:  # noqa: BLE001 - the server drops this request on purpose
            pass
        await await_until(second_turn_ended)
        # The first stream is still attached; the second never opened.
        assert slot._stream_epoch == second_epoch
        assert second_epoch not in slot._stream_handoffs

        release_first_reader.set()
        await await_until(first_body)
