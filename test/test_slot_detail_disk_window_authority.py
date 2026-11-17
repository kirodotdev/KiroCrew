"""``GET /api/chat/slots/{slot}``: what each read serves when disk and the window differ.

Builds a disk-vs-window disagreement. A row the window holds under an id disk holds
once is served as the window's copy; a disk row no window row holds (another
writer's append) is served by the bounded branch where disk put it. Both disk
readers (``read_messages_chained_full`` for the bounded branch,
``read_messages_chained`` for the unbounded one) are stubbed with ONE corpus, so a
pass cannot come from either silently reading an empty history; the indexed-page
test reads a real saved transcript instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.state import _ChatSlot

SETTLED = 12
OLDER = 4  # rows above the live window -> _disk_older_count
LIMIT = 4


@pytest.fixture()
def state(tmp_path: Any) -> Any:
    st = _make_state(tmp_path)
    st.push_slots_update = lambda: None  # type: ignore[method-assign]
    return st


def _row(i: int, content: str) -> dict:
    return {
        "role": "user" if i % 2 == 0 else "assistant",
        "content": content,
        "cls": "msg msg-u" if i % 2 == 0 else "msg msg-a",
        "ts": f"2026-09-01T00:00:{i:02d}Z",
        "meta": {"mid": f"mid-{i}"},
    }


def _bind_corpus(state: Any, on_disk: list[dict]) -> None:
    """Stub every disk reader the handler can reach with ONE corpus."""
    state.conversation_log.read_messages_chained = (  # type: ignore[method-assign]
        lambda _key: [dict(r) for r in on_disk]
    )
    state.conversation_log.read_messages_chained_full = (  # type: ignore[method-assign]
        lambda _key: [dict(r) for r in on_disk]
    )
    state.conversation_log.read_rotated_messages_chained = (  # type: ignore[method-assign]
        lambda _key: []
    )
    state.conversation_log.chain_mid_rotation = lambda _key: False  # type: ignore[method-assign]


def _slot(state: Any, window: list[dict], on_disk: list[dict], name: str = "chat-1") -> Any:
    slot = _ChatSlot(key=name)
    slot.messages = window
    slot._disk_older_count = OLDER
    slot._disk_window_len = len(window)
    state._slots[name] = slot
    _bind_corpus(state, on_disk)
    return slot


async def _get(state: Any, query: str, name: str = "chat-1") -> dict:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{name}{query}")
        assert resp.status == 200
        return await resp.json()


def _variant_switch(state: Any) -> None:
    """A variant switch: the newest assistant row rewritten IN PLACE, same mid.

    ``api_chat_slot_switch_variant`` assigns ``target["content"]`` and
    ``target["ts"]`` on the existing window dict and never touches ``meta.mid``,
    so disk holds the previous variant at the SAME id.
    """
    on_disk = [_row(i, f"m{i}") for i in range(SETTLED)]
    window = [dict(r) for r in on_disk[OLDER:]]
    window[-1] = {**window[-1], "content": "SELECTED VARIANT", "ts": "2026-09-01T00:09:99Z"}
    _slot(state, window, on_disk)


def _another_writers_append(state: Any) -> None:
    """Disk holds two rows after the window's last row: another writer appended them."""
    on_disk = [_row(i, f"m{i}") for i in range(SETTLED)]
    window = [dict(r) for r in on_disk[OLDER : SETTLED - 2]]
    _slot(state, window, on_disk)


class TestContentDisagreement:
    @pytest.mark.asyncio
    async def test_bounded_read_returns_the_selected_variant(self, state: Any) -> None:
        _variant_switch(state)
        data = await _get(state, f"?limit={LIMIT}")
        assert data["messages"][-1]["content"] == "SELECTED VARIANT"

    @pytest.mark.asyncio
    async def test_both_branches_agree_on_content(self, state: Any) -> None:
        _variant_switch(state)
        bounded = await _get(state, f"?limit={LIMIT}")
        unbounded = await _get(state, "")
        assert bounded["messages"][-1]["content"] == unbounded["messages"][-1]["content"]

    @pytest.mark.asyncio
    async def test_indexed_page_serves_the_selected_variant(
        self, state: Any, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The same switch on a real transcript, read through the indexed page."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        slot = state.get_or_create_slot("indexed")
        slot.append("user", "q1")
        slot.append("assistant", "first reply")
        slot.drain()
        _save_slot_to_history(state, slot, force=True)
        slot.messages[-1]["content"] = "SELECTED VARIANT"
        slot.messages[-1]["ts"] = "2026-09-01T00:09:59Z"

        def _no_full_reader(_key: str) -> list[dict]:
            raise AssertionError("the indexed page must serve this read")

        state.conversation_log.read_messages_chained_full = _no_full_reader  # type: ignore[method-assign]
        data = await _get(state, f"?limit={LIMIT}", name="indexed")
        assert [m["content"] for m in data["messages"]] == ["q1", "SELECTED VARIANT"]


class TestARepeatedIdKeepsTheDiskCopies:
    @pytest.mark.asyncio
    async def test_a_foreign_row_sharing_the_id_is_still_served(self, state: Any) -> None:
        """Disk holds the window row's id twice: pairing is a guess, so disk wins."""
        older = [_row(i, f"m{i}") for i in range(OLDER)]
        foreign = {**_row(OLDER, "foreign"), "meta": {"mid": "dup"}}
        own = {**_row(OLDER + 1, "own on disk"), "meta": {"mid": "dup"}}
        window = [{**own, "content": "own in window"}]
        _slot(state, window, older + [foreign, own])
        data = await _get(state, f"?limit={SETTLED}")
        assert [m["content"] for m in data["messages"]][OLDER:] == ["foreign", "own on disk"]


class TestAnotherWritersAppend:
    @pytest.mark.asyncio
    async def test_bounded_read_serves_the_rows_disk_holds_after_the_window(
        self, state: Any
    ) -> None:
        _another_writers_append(state)
        data = await _get(state, f"?limit={SETTLED}")
        assert [m["content"] for m in data["messages"]] == [f"m{i}" for i in range(SETTLED)]

    @pytest.mark.asyncio
    async def test_the_no_limit_read_serves_the_window_where_the_bounded_read_adds_them(
        self, state: Any
    ) -> None:
        """Pins today's difference between the two reads, so a change to either is deliberate."""
        _another_writers_append(state)
        bounded = await _get(state, f"?limit={SETTLED}")
        unbounded = await _get(state, "")
        assert [m["content"] for m in unbounded["messages"]] == [
            f"m{i}" for i in range(SETTLED - 2)
        ]
        assert [m["content"] for m in bounded["messages"]] == [f"m{i}" for i in range(SETTLED)]


class TestTheFrozenPrefixSurvives:
    @pytest.mark.asyncio
    async def test_older_session_rows_above_the_window_are_still_served(self, state: Any) -> None:
        """Window authority must not swallow the prefix the bound exists to page into."""
        _variant_switch(state)
        data = await _get(state, f"?limit={SETTLED}")
        contents = [m["content"] for m in data["messages"]]
        assert contents[:OLDER] == [f"m{i}" for i in range(OLDER)]
