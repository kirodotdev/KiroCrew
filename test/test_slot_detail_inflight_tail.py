"""A bounded slot-detail fetch counts messages, not stream progress.

``chunk`` is a wire-only role appended once per streamed delta, so a text
segment still in flight occupies hundreds of rows that render as ONE message.
Those rows reach ``GET /api/chat/slots/{slot}`` only through the un-flushed
in-memory tail the bounded branch appends -- they are never persisted -- and
applying ``limit`` before folding them spends the caller's whole budget inside
one unfinished message, so the response is a mid-sentence fragment instead of
the last N messages.

The caller that meets this on every request is a poller watching an agent work:
mid-response is its normal condition, not an edge case. These tests pin the
fold that makes one row mean one message before the slice is taken.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_utils import _collapse_wire_rows
from kiro_crew.dashboard.state import _ChatSlot

#: Deltas in the in-flight segment. Large enough that it alone overruns the
#: bound below, which is the condition that produces the fragment.
DELTAS = 300
SETTLED = 10
LIMIT = 5


@pytest.fixture()
def state(tmp_path: Any) -> Any:
    st = _make_state(tmp_path)
    st.push_slots_update = lambda: None  # type: ignore[method-assign]
    return st


def _slot_mid_stream(state: Any, name: str = "chat-1") -> Any:
    """A slot whose newest rows are an un-flushed, still-streaming segment.

    Mirrors the live shape: the settled turns are on disk AND in the window,
    and the deltas of the segment being typed exist only in the window.
    """
    settled = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(SETTLED)
    ]
    deltas = [{"role": "chunk", "content": f"d{i} ", "cls": "chunk"} for i in range(DELTAS)]
    slot = _ChatSlot(key=name)
    slot.messages = settled + deltas
    slot._disk_older_count = 0
    state._slots[name] = slot
    state.conversation_log.read_messages_chained = lambda _key: list(settled)  # type: ignore[method-assign]
    return slot


async def _get(state: Any, query: str, name: str = "chat-1") -> dict:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{name}{query}")
        assert resp.status == 200
        return await resp.json()


class TestBoundedFetchDuringAnInFlightSegment:
    @pytest.mark.asyncio
    async def test_returns_n_messages_rather_than_one_fragment(self, state: Any) -> None:
        """`limit` must bound displayed messages, not raw stream rows.

        Slicing before the fold returns a single ``streaming`` row -- the tail
        of the in-flight run -- and none of the conversation behind it.
        """
        _slot_mid_stream(state)
        data = await _get(state, f"?limit={LIMIT}")
        assert len(data["messages"]) == LIMIT
        roles = [m["role"] for m in data["messages"]]
        # Exactly one row stands for the whole in-flight segment, and it is the
        # newest; the rest of the budget goes to real conversation.
        assert roles.count("streaming") == 1
        assert roles[-1] == "streaming"

    @pytest.mark.asyncio
    async def test_the_in_flight_text_arrives_whole(self, state: Any) -> None:
        """The one row carries the entire segment, not the last few deltas.

        A viewer of a running slot needs the text as far as it has been typed,
        which is what the unbounded branch already returns.
        """
        _slot_mid_stream(state)
        data = await _get(state, f"?limit={LIMIT}")
        streaming = [m for m in data["messages"] if m["role"] == "streaming"]
        assert streaming[0]["content"] == "".join(f"d{i} " for i in range(DELTAS))

    @pytest.mark.asyncio
    async def test_total_counts_messages_not_stream_progress(self, state: Any) -> None:
        """`total` feeds the client's cursor clamp, so it must not track deltas."""
        _slot_mid_stream(state)
        data = await _get(state, f"?limit={LIMIT}")
        assert data["total"] == SETTLED + 1

    @pytest.mark.asyncio
    async def test_the_cursor_points_behind_the_returned_page(self, state: Any) -> None:
        """Paging older still works from a page taken during a stream."""
        _slot_mid_stream(state)
        data = await _get(state, f"?limit={LIMIT}")
        assert data["has_more"] is True
        assert data["next_before"] == (SETTLED + 1) - LIMIT


class TestUnaffectedShapes:
    @pytest.mark.asyncio
    async def test_a_slot_with_no_in_flight_segment_is_unchanged(self, state: Any) -> None:
        """With no deltas in the window the fold is a no-op end to end."""
        settled = [{"role": "assistant", "content": f"m{i}"} for i in range(SETTLED)]
        slot = _ChatSlot(key="chat-2")
        slot.messages = list(settled)
        slot._disk_older_count = 0
        state._slots["chat-2"] = slot
        state.conversation_log.read_messages_chained = lambda _key: list(settled)  # type: ignore[method-assign]
        data = await _get(state, f"?limit={LIMIT}", name="chat-2")
        assert data["total"] == SETTLED
        assert [m["content"] for m in data["messages"]] == [f"m{i}" for i in range(5, SETTLED)]

    @pytest.mark.asyncio
    async def test_folding_a_disk_resident_wire_row_is_output_equivalent(self, state: Any) -> None:
        """The reduction covers the whole corpus, and that changes no output.

        It cannot be scoped to a trailing slice: ``_append_unflushed_tail``
        places an owed row at the disk index it belongs to, so owed rows are not
        a contiguous suffix and a slice would miss the interleaved ones. Both
        the append and the reduction run in a worker thread, so covering the
        whole corpus keeps no extra work on the event loop.

        Folding a disk-resident run is therefore harmless rather than merely
        tolerated: ``_prepare_messages`` folds the same run at render time, so
        the rendered rows below are identical either way. Persisted history does
        not actually carry a ``chunk`` row -- ``_build_message_entry_uncached``
        returns ``None`` for every wire-only role -- so this shape is
        constructed, not reachable; the assertion pins the equivalence, and
        ``total`` records that the run now folds before the bound rather than
        after it.
        """
        disk = [
            {"role": "assistant", "content": "settled"},
            {"role": "chunk", "content": "a"},
            {"role": "chunk", "content": "b"},
        ]
        slot = _ChatSlot(key="chat-3")
        slot.messages = disk + [{"role": "user", "content": "unflushed"}]
        slot._disk_older_count = 0
        state._slots["chat-3"] = slot
        state.conversation_log.read_messages_chained = lambda _key: list(disk)  # type: ignore[method-assign]
        data = await _get(state, "?limit=50", name="chat-3")
        # The two-row disk run folds to one, so the corpus is one shorter than
        # the raw rows -- counted before `limit` is applied, which is the point.
        assert data["total"] == len(disk) + 1 - 1
        # Unchanged from the raw-row placement: one message per turn either way.
        assert [m["role"] for m in data["messages"]] == ["assistant", "streaming", "user"]

    @pytest.mark.asyncio
    async def test_a_done_terminator_does_not_shrink_the_page(self, state: Any) -> None:
        """A settled turn leaves a `done` row in the window forever.

        Nothing removes it -- it is appended at turn end and never persisted --
        so it accumulates one per turn and reaches the slice through the
        un-flushed tail. Counted as a row it consumes one slot and renders as
        nothing, so a completed `limit=N` fetch would come back with N-1
        messages.
        """
        settled = [{"role": "assistant", "content": f"m{i}"} for i in range(SETTLED)]
        slot = _ChatSlot(key="chat-4")
        slot.messages = settled + [{"role": "done", "content": ""}]
        slot._disk_older_count = 0
        state._slots["chat-4"] = slot
        state.conversation_log.read_messages_chained = lambda _key: list(settled)  # type: ignore[method-assign]
        data = await _get(state, f"?limit={LIMIT}", name="chat-4")
        assert len(data["messages"]) == LIMIT
        assert data["total"] == SETTLED


class TestResumeDuringAnInFlightSegment:
    """The resume path bounds the same live window by raw row count.

    ``api_chat_slot_resume`` returns the newest 200 rows of the window. Taken
    before the reduction, an in-flight segment longer than that bound fills it
    entirely, so the response carries one ``streaming`` row holding only the
    bound's slice of the reply and drops the text ahead of it -- a worse outcome
    than the detail handler's, which loses surrounding messages rather than the
    reply itself.
    """

    @pytest.mark.asyncio
    async def test_the_in_flight_text_is_not_truncated_by_the_bound(self, state: Any) -> None:
        slot = _slot_mid_stream(state, name="chat-5")
        slot._disk_older_count = 0
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/chat-5/resume", json={"key": "chat-5"})
            assert resp.status == 200
            data = await resp.json()
        streaming = [m for m in data["messages"] if m["role"] == "streaming"]
        assert len(streaming) == 1
        assert streaming[0]["content"] == "".join(f"d{i} " for i in range(DELTAS))

    @pytest.mark.asyncio
    async def test_total_and_cursor_count_messages(self, state: Any) -> None:
        """Both cursor terms end up in message units, not raw rows.

        `_disk_older_count` is a count of persisted rows, which carry no
        wire-only role, so pairing it with a raw window length mixes units.
        """
        slot = _slot_mid_stream(state, name="chat-6")
        slot._disk_older_count = 0
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/chat-6/resume", json={"key": "chat-6"})
            data = await resp.json()
        assert data["total"] == SETTLED + 1
        assert data["next_before"] == 0
        assert data["has_more"] is False


class TestTheFoldItself:
    def test_a_non_chunk_row_breaks_the_run(self) -> None:
        """Two segments split by a tool row stay two rows, as _prepare_messages folds."""
        rows = [
            {"role": "chunk", "content": "a"},
            {"role": "chunk", "content": "b"},
            {"role": "tool", "content": "t"},
            {"role": "chunk", "content": "c"},
        ]
        out = _collapse_wire_rows(rows)
        assert [(m["role"], m["content"]) for m in out] == [
            ("chunk", "ab"),
            ("tool", "t"),
            ("chunk", "c"),
        ]

    def test_done_rows_drop(self) -> None:
        """A terminator renders as nothing, so it must not occupy a row here."""
        rows = [{"role": "chunk", "content": "a"}, {"role": "done", "content": ""}]
        assert [m["role"] for m in _collapse_wire_rows(rows)] == ["chunk"]

    def test_a_done_row_does_not_split_a_run(self) -> None:
        """_prepare_messages skips `done` without flushing its accumulator.

        A terminator landing between two deltas therefore does not split the
        message there, and this reduction must agree rather than emit two rows.
        """
        rows = [
            {"role": "chunk", "content": "a"},
            {"role": "done", "content": ""},
            {"role": "chunk", "content": "b"},
        ]
        out = _collapse_wire_rows(rows)
        assert [(m["role"], m["content"]) for m in out] == [("chunk", "ab")]

    def test_the_live_window_is_not_mutated(self) -> None:
        """These dicts are shared with the window the event loop appends to."""
        rows = [{"role": "chunk", "content": "a"}, {"role": "chunk", "content": "b"}]
        before = [dict(m) for m in rows]
        _collapse_wire_rows(rows)
        assert rows == before
