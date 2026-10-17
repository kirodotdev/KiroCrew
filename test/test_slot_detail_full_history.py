"""What ``GET /api/chat/slots/{slot}`` reports for a channel-born slot.

The refactor pointed a channel-born dashboard tab at the CHANNEL's own
transcript instead of a seeded copy, and capped the in-memory window at
``_RESTORE_WINDOW`` messages with ``_disk_older_count`` recording how many
on-disk lines the window omits.

The no-limit branch of the endpoint therefore reassembles
``disk[:older_count] + window`` and reports ``has_more=False``. These tests pin
down whether that claim is TRUE — i.e. whether the response really is the whole
conversation — for a transcript shorter than the window and for one longer than
it. If it is, no "load older messages" affordance is needed in the frontend;
``total`` is the real corpus size and there is nothing left on disk to fetch.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.history import _safe_key

CHANNEL_KEY = "slack:1785370133.085469"
SLOT_NAME = "slack_1785370133.085469"


def _write_transcript(tmp_path: Any, key: str, count: int) -> list[dict[str, Any]]:
    """Write *count* messages to *key*'s JSONL file and return them.

    Written directly rather than through ``ConversationLog.append`` so a
    700-message fixture does not pay 700 advisory-lock round trips.
    """
    path = tmp_path / f"{_safe_key(key)}.jsonl"
    msgs = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"m{i}",
            "ts": f"2026-07-30T00:00:{i % 60:02d}.{i:06d}Z",
        }
        for i in range(count)
    ]
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-07-30T00:00:00"})]
    lines += [json.dumps(m) for m in msgs]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return msgs


def _surface(state: Any, key: str) -> Any:
    """Surface *key* as a dashboard slot exactly as the reconciler does."""
    messages = state.conversation_log.read_messages_chained(key)
    slot = channel_slots.surface_channel_session(
        state,
        {"key": _safe_key(key), "title": "Ship the thing"},
        state.conversation_log.get_metadata(key),
        messages,
        session_key=key,
    )
    assert slot is not None
    return slot


@pytest.fixture()
def state(tmp_path: Any) -> Any:
    st = _make_state(tmp_path)
    st.push_slots_update = lambda: None  # type: ignore[method-assign]
    return st


class TestNoLimitBranchReturnsTheWholeConversation:
    """``has_more=False`` is only honest if nothing is left behind on disk."""

    @pytest.mark.asyncio
    async def test_transcript_shorter_than_the_window_is_returned_whole(
        self, state: Any, tmp_path: Any
    ) -> None:
        """62 messages: the window holds them all, so no disk read is needed."""
        _write_transcript(tmp_path, CHANNEL_KEY, 62)
        slot = _surface(state, CHANNEL_KEY)
        assert slot._disk_older_count == 0
        assert len(slot.messages) == 62

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(f"/api/chat/slots/{SLOT_NAME}")
            assert resp.status == 200
            body = await resp.json()

        assert len(body["messages"]) == 62
        assert body["total"] == 62
        assert body["has_more"] is False
        # Whole conversation, in order — first and last message both present.
        assert body["messages"][0]["content"] == "m0"
        assert body["messages"][-1]["content"] == "m61"

    @pytest.mark.asyncio
    async def test_transcript_longer_than_the_window_is_still_returned_whole(
        self, state: Any, tmp_path: Any
    ) -> None:
        """700 messages: 200 frozen-prefix lines are re-read and prepended.

        This is the case the plan assumed was truncated. It is not: the
        response carries all 700, so ``has_more=False`` is correct and
        ``total`` is the real corpus size.
        """
        _write_transcript(tmp_path, CHANNEL_KEY, 700)
        slot = _surface(state, CHANNEL_KEY)
        assert slot._disk_older_count == 700 - channel_slots._RESTORE_WINDOW
        assert len(slot.messages) == channel_slots._RESTORE_WINDOW

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(f"/api/chat/slots/{SLOT_NAME}")
            assert resp.status == 200
            body = await resp.json()

        assert len(body["messages"]) == 700
        assert body["total"] == 700
        assert body["has_more"] is False
        assert body["messages"][0]["content"] == "m0"
        assert body["messages"][-1]["content"] == "m699"
        # No line is duplicated or dropped at the prefix/window seam.
        assert [m["content"] for m in body["messages"]] == [f"m{i}" for i in range(700)]

    @pytest.mark.asyncio
    async def test_exactly_the_window_length_needs_no_disk_read(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Boundary: window-length transcript has an empty frozen prefix."""
        n = channel_slots._RESTORE_WINDOW
        _write_transcript(tmp_path, CHANNEL_KEY, n)
        slot = _surface(state, CHANNEL_KEY)
        assert slot._disk_older_count == 0

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{SLOT_NAME}")).json()

        assert len(body["messages"]) == n
        assert body["total"] == n
        assert body["has_more"] is False

    @pytest.mark.asyncio
    async def test_one_past_the_window_recovers_the_single_older_line(
        self, state: Any, tmp_path: Any
    ) -> None:
        """Boundary: an off-by-one at the seam would drop or dupe ``m0``."""
        n = channel_slots._RESTORE_WINDOW + 1
        _write_transcript(tmp_path, CHANNEL_KEY, n)
        slot = _surface(state, CHANNEL_KEY)
        assert slot._disk_older_count == 1

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{SLOT_NAME}")).json()

        assert [m["content"] for m in body["messages"]] == [f"m{i}" for i in range(n)]
        assert body["total"] == n
        assert body["has_more"] is False

    @pytest.mark.asyncio
    async def test_live_turns_appended_after_surfacing_are_included(
        self, state: Any, tmp_path: Any
    ) -> None:
        """The window keeps growing past ``_RESTORE_WINDOW`` in memory.

        ``_disk_older_count`` is frozen at surface time, so a slot that has
        since taken new turns must report prefix + the GROWN window, not a
        re-clamped 500.
        """
        _write_transcript(tmp_path, CHANNEL_KEY, 700)
        slot = _surface(state, CHANNEL_KEY)
        slot.append("user", "brand new", "msg msg-u", broadcast=False)
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{SLOT_NAME}")).json()

        assert len(body["messages"]) == 701
        assert body["total"] == 701
        assert body["has_more"] is False
        assert body["messages"][-1]["content"] == "brand new"
        assert body["messages"][0]["content"] == "m0"


class TestExplicitPaginationStillAgrees:
    """The legacy ``limit``/``before`` branch must span the same corpus.

    A frontend that asks for a page must not see a different conversation
    length than the one the no-limit open reported.
    """

    @pytest.mark.asyncio
    async def test_limited_page_reports_the_full_corpus_as_total(
        self, state: Any, tmp_path: Any
    ) -> None:
        _write_transcript(tmp_path, CHANNEL_KEY, 700)
        _surface(state, CHANNEL_KEY)

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{SLOT_NAME}?limit=50")).json()

        assert len(body["messages"]) == 50
        assert body["total"] == 700
        # Genuine truncation on this branch DOES set has_more.
        assert body["has_more"] is True
        assert body["messages"][-1]["content"] == "m699"

    @pytest.mark.asyncio
    async def test_walking_before_backwards_reaches_the_first_message(
        self, state: Any, tmp_path: Any
    ) -> None:
        """``before`` is a chained-disk index; the walk must terminate at 0."""
        _write_transcript(tmp_path, CHANNEL_KEY, 700)
        _surface(state, CHANNEL_KEY)

        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (
                await client.get(f"/api/chat/slots/{SLOT_NAME}?limit=100&before=100")
            ).json()

        assert [m["content"] for m in body["messages"]] == [f"m{i}" for i in range(100)]
        assert body["has_more"] is False
        assert body["total"] == 700
