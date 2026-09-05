"""POSITIVE CONTROL for issue #7526 — must be RED against unpatched main.

Builds the disk-vs-window disagreement the issue describes and asserts the
bounded branch returns the WINDOW's answer. Both branches read the SAME corpus
here, stubbed on BOTH readers (the bounded branch calls
``read_messages_chained_full``, the unbounded one ``read_messages_chained``), so
a pass cannot come from one of them silently reading an empty tmp history.
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

#: These four are RED on current main and stay red until the authority contract for
#: the bounded branch is ruled on (issue #7526). ``strict`` on purpose: whoever
#: lands the fix gets a failure here telling them to delete the marker, so the
#: reproduction cannot rot into a silently-passing test the way the round-1..4
#: tests preserved in ``c9979c43`` did -- those stub ``read_messages_chained``,
#: which the bounded branch no longer calls, so they now pass vacuously.
DISAGREEMENT = pytest.mark.xfail(
    strict=True,
    reason="#7526: the bounded branch is disk-authoritative for the window region",
)


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


def _regenerate_truncation(state: Any) -> None:
    """A regenerate whose inline save failed: disk still holds the deleted tail."""
    on_disk = [_row(i, f"m{i}") for i in range(SETTLED)]
    # `del slot.messages[u_idx + 1:]` dropped the last two turns.
    window = [dict(r) for r in on_disk[OLDER : SETTLED - 2]]
    _slot(state, window, on_disk)


class TestContentDisagreement:
    @DISAGREEMENT
    @pytest.mark.asyncio
    async def test_bounded_read_returns_the_selected_variant(self, state: Any) -> None:
        _variant_switch(state)
        data = await _get(state, f"?limit={LIMIT}")
        assert data["messages"][-1]["content"] == "SELECTED VARIANT"

    @DISAGREEMENT
    @pytest.mark.asyncio
    async def test_both_branches_agree_on_content(self, state: Any) -> None:
        _variant_switch(state)
        bounded = await _get(state, f"?limit={LIMIT}")
        unbounded = await _get(state, "")
        assert bounded["messages"][-1]["content"] == unbounded["messages"][-1]["content"]


class TestLengthDisagreement:
    @DISAGREEMENT
    @pytest.mark.asyncio
    async def test_bounded_read_does_not_resurrect_the_deleted_tail(self, state: Any) -> None:
        _regenerate_truncation(state)
        data = await _get(state, f"?limit={SETTLED}")
        contents = [m["content"] for m in data["messages"]]
        assert "m10" not in contents and "m11" not in contents

    @DISAGREEMENT
    @pytest.mark.asyncio
    async def test_both_branches_agree_on_length(self, state: Any) -> None:
        _regenerate_truncation(state)
        bounded = await _get(state, f"?limit={SETTLED}")
        unbounded = await _get(state, "")
        assert [m["content"] for m in bounded["messages"]] == [
            m["content"] for m in unbounded["messages"]
        ]


class TestTheFrozenPrefixSurvives:
    @pytest.mark.asyncio
    async def test_older_session_rows_above_the_window_are_still_served(self, state: Any) -> None:
        """Window authority must not swallow the prefix the bound exists to page into."""
        _variant_switch(state)
        data = await _get(state, f"?limit={SETTLED}")
        contents = [m["content"] for m in data["messages"]]
        assert contents[:OLDER] == [f"m{i}" for i in range(OLDER)]
