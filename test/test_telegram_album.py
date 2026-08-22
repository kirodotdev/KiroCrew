"""Telegram album (media group) coalescing — unit tests.

Telegram delivers an album as N separate ``message`` updates sharing one
``media_group_id``, with the caption on only one member. Without coalescing a
four-screenshot album becomes four turns, three of them a bare image with no
context. These tests pin the debounce that merges them into one.

Timing is driven by monkeypatching the module's window constants down to
near-zero rather than by sleeping real seconds, so the suite stays fast and
deterministic.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.telegram import client as tg_client
from kiro_crew.telegram.client import TelegramClient, TelegramInbound


def _make_client() -> tuple[TelegramClient, list[TelegramInbound]]:
    """A client wired to capture whatever reaches the message handler."""
    received: list[TelegramInbound] = []

    async def on_message(inbound: TelegramInbound) -> None:
        received.append(inbound)

    # The REAL constructor, not `__new__` plus a hand-listed set of fields: a double
    # that enumerates the constructor's state drifts every time the client gains a
    # field, and the failure lands as an AttributeError from production code rather
    # than as anything about albums. A token is all __init__ needs; nothing here
    # opens a session.
    client = TelegramClient(token="t:1")
    client._on_message = on_message
    return client, received


def _photo_update(
    *,
    file_id: str,
    group: str | None = None,
    caption: str = "",
    message_id: int = 1,
    chat_id: int = 100,
    user_id: int = 42,
) -> dict:
    msg: dict[str, Any] = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "username": "tester"},
        "photo": [
            {"file_id": f"{file_id}_s", "file_unique_id": "s", "file_size": 100},
            {"file_id": file_id, "file_unique_id": "l", "file_size": 9000},
        ],
    }
    if group is not None:
        msg["media_group_id"] = group
    if caption:
        msg["caption"] = caption
    return {"message": msg}


async def _drain(client: TelegramClient) -> None:
    """Let every pending timer + handler task settle."""
    for _ in range(12):
        pending = [t for t in list(client._handler_tasks) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


@pytest.fixture()
def fast_album(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the debounce windows so tests do not sleep real seconds."""
    monkeypatch.setattr(tg_client, "_ALBUM_WINDOW_S", 0.01)
    monkeypatch.setattr(tg_client, "_ALBUM_MAX_WAIT_S", 0.5)


# ── The core behaviour ────────────────────────────────────────────────────────


class TestAlbumCoalescing:
    @pytest.mark.asyncio
    async def test_four_photo_album_becomes_one_turn(self, fast_album: None) -> None:
        """The regression this exists for: 4 updates -> 1 merged message."""
        client, received = _make_client()
        for i in range(4):
            client._dispatch(
                _photo_update(
                    file_id=f"photo{i}",
                    group="grp1",
                    # Telegram puts the caption on exactly one member.
                    caption="what is this error?" if i == 0 else "",
                    message_id=10 + i,
                )
            )
        # Nothing dispatched yet -- still inside the debounce window.
        assert received == []
        await _drain(client)

        assert len(received) == 1, "album must collapse to a single turn"
        merged = received[0]
        assert len(merged.attachments) == 4
        assert [a["file_id"] for a in merged.attachments] == [
            "photo0",
            "photo1",
            "photo2",
            "photo3",
        ], "member order must be preserved"
        assert merged.text == "what is this error?", "the caption must survive"

    @pytest.mark.asyncio
    async def test_caption_recovered_from_a_later_member(self, fast_album: None) -> None:
        """The caption is not assumed to be on the first member."""
        client, received = _make_client()
        client._dispatch(_photo_update(file_id="a", group="g", message_id=1))
        client._dispatch(
            _photo_update(file_id="b", group="g", caption="look here", message_id=2)
        )
        await _drain(client)

        assert len(received) == 1
        assert received[0].text == "look here"

    @pytest.mark.asyncio
    async def test_every_per_item_caption_survives(self, fast_album: None) -> None:
        """Telegram Desktop/Android allow a caption per media-group item.

        Keeping only the first would silently drop the user's own words for
        items 2..N, so every caption must reach the merged turn, in album order.
        """
        client, received = _make_client()
        client._dispatch(
            _photo_update(file_id="a", group="g", caption="first shot", message_id=1)
        )
        client._dispatch(_photo_update(file_id="b", group="g", message_id=2))
        client._dispatch(
            _photo_update(file_id="c", group="g", caption="and this one", message_id=3)
        )
        await _drain(client)

        assert len(received) == 1
        assert received[0].text == "first shot\n\nand this one", (
            "every per-item caption must survive, in album order"
        )

    @pytest.mark.asyncio
    async def test_head_message_id_is_used(self, fast_album: None) -> None:
        """A reply/steer-ack must target the album's FIRST message."""
        client, received = _make_client()
        for i, mid in enumerate((77, 78, 79)):
            client._dispatch(_photo_update(file_id=f"p{i}", group="g", message_id=mid))
        await _drain(client)

        assert received[0].message_id == 77

    @pytest.mark.asyncio
    async def test_non_album_message_is_unaffected(self, fast_album: None) -> None:
        """A single photo (no media_group_id) still dispatches immediately."""
        client, received = _make_client()
        client._dispatch(_photo_update(file_id="solo", caption="hi"))
        # No debounce for a non-album message -- the task exists right away.
        await _drain(client)

        assert len(received) == 1
        assert received[0].text == "hi"
        assert len(received[0].attachments) == 1
        assert client._albums == {}, "no album state for a non-album message"

    @pytest.mark.asyncio
    async def test_two_interleaved_albums_stay_separate(self, fast_album: None) -> None:
        """Distinct media_group_ids must not merge into each other."""
        client, received = _make_client()
        client._dispatch(_photo_update(file_id="a1", group="A", caption="album A"))
        client._dispatch(_photo_update(file_id="b1", group="B", caption="album B"))
        client._dispatch(_photo_update(file_id="a2", group="A"))
        client._dispatch(_photo_update(file_id="b2", group="B"))
        await _drain(client)

        assert len(received) == 2
        by_text = {m.text: m for m in received}
        assert set(by_text) == {"album A", "album B"}
        assert [a["file_id"] for a in by_text["album A"].attachments] == ["a1", "a2"]
        assert [a["file_id"] for a in by_text["album B"].attachments] == ["b1", "b2"]

    @pytest.mark.asyncio
    async def test_same_group_id_in_two_chats_does_not_merge(
        self, fast_album: None
    ) -> None:
        """A media_group_id collision across chats must not cross-deliver.

        Nothing guarantees Telegram's media_group_id is unique across the chats
        one bot serves. Keyed on the id alone, a collision merges both chats'
        members into ONE message addressed to the first chat -- swallowing the
        second chat's copy and leaking its content into the wrong conversation.
        Buffers are keyed by (chat_id, media_group_id) to remove the class.
        """
        client, received = _make_client()
        client._dispatch(
            _photo_update(file_id="chatA", group="dup", caption="from A", chat_id=111)
        )
        client._dispatch(
            _photo_update(file_id="chatB", group="dup", caption="from B", chat_id=222)
        )
        await _drain(client)

        assert len(received) == 2, "a cross-chat id collision must not merge"
        by_chat = {m.chat_id: m for m in received}
        assert set(by_chat) == {111, 222}
        assert [a["file_id"] for a in by_chat[111].attachments] == ["chatA"]
        assert [a["file_id"] for a in by_chat[222].attachments] == ["chatB"]
        assert by_chat[111].text == "from A"
        assert by_chat[222].text == "from B"

    @pytest.mark.asyncio
    async def test_buffer_is_drained_after_flush(self, fast_album: None) -> None:
        """No per-group state may survive the flush (memory bound)."""
        client, _received = _make_client()
        client._dispatch(_photo_update(file_id="x", group="g"))
        await _drain(client)

        assert client._albums == {}
        assert client._album_timers == {}
        assert client._album_first_seen == {}
        assert client._album_dropped == {}


# ── Bounded-ness (the two hazards named in the issue) ─────────────────────────


class TestAlbumBounds:
    @pytest.mark.asyncio
    async def test_member_cap_holds_and_is_reported(
        self, fast_album: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Over-cap members are counted and logged, never grown into."""
        client, received = _make_client()
        for i in range(tg_client._ALBUM_MAX_MEMBERS + 4):
            client._dispatch(_photo_update(file_id=f"p{i}", group="big", message_id=i))
        # Buffers are keyed by (chat_id, media_group_id) -- see
        # test_same_group_id_in_two_chats_does_not_merge.
        key = "100:big"
        # The buffer never exceeds the cap.
        assert len(client._albums[key]) == tg_client._ALBUM_MAX_MEMBERS
        assert client._album_dropped[key] == 4

        with caplog.at_level("WARNING", logger="kiro_crew.telegram.client"):
            await _drain(client)

        assert len(received) == 1
        assert len(received[0].attachments) == tg_client._ALBUM_MAX_MEMBERS
        assert any("exceeded" in r.getMessage() for r in caplog.records), (
            "an over-cap album must be visible in the log, not silently truncated"
        )

    @pytest.mark.asyncio
    async def test_group_cap_force_flushes_oldest_rather_than_dropping(
        self, fast_album: None
    ) -> None:
        """Exceeding the group cap must not lose a message."""
        client, received = _make_client()
        for i in range(tg_client._ALBUM_MAX_GROUPS + 1):
            client._dispatch(_photo_update(file_id=f"g{i}", group=f"grp{i}"))
        # The oldest was flushed to make room, so it is dispatched -- not dropped.
        assert len(client._albums) <= tg_client._ALBUM_MAX_GROUPS
        await _drain(client)

        got = {a["file_id"] for m in received for a in m.attachments}
        assert len(received) == tg_client._ALBUM_MAX_GROUPS + 1
        assert "g0" in got, "the evicted group must still reach the handler"

    @pytest.mark.asyncio
    async def test_hard_ceiling_flushes_a_never_ending_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stream that keeps appending cannot defer the flush forever.

        The idle window is set LONGER than the hard ceiling, so only the ceiling
        can trigger the flush -- if the rearm ignored it, this would hang.
        """
        monkeypatch.setattr(tg_client, "_ALBUM_WINDOW_S", 10.0)
        monkeypatch.setattr(tg_client, "_ALBUM_MAX_WAIT_S", 0.05)
        client, received = _make_client()

        client._dispatch(_photo_update(file_id="first", group="never"))
        # Keep rearming past the ceiling.
        for i in range(3):
            await asyncio.sleep(0.03)
            client._dispatch(_photo_update(file_id=f"more{i}", group="never"))

        await asyncio.wait_for(_drain(client), timeout=5)
        assert received, "the hard ceiling must force a flush"


# ── Shutdown ─────────────────────────────────────────────────────────────────


class TestAlbumShutdown:
    @pytest.mark.asyncio
    async def test_close_attempts_delivery_and_drains_the_buffer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``close()`` hands buffered albums to the handler and drops the state.

        Scoped deliberately to what this layer can actually promise. It is NOT
        "a buffered album is never lost": shutdown runs channel teardown and
        ``SessionManager.close_all()`` concurrently, and once ``_closing`` is set
        ``begin_turn`` raises ``SessionClosingError`` -- so the spawned handler
        may be refused downstream. That is the same gate a plain message
        arriving at shutdown already hits today, so this test pins the two things
        the client itself controls: delivery is *attempted*, and no album state
        survives a closed client.

        The idle window is set far longer than this test's patience, so the
        natural timer cannot be what invokes the handler -- only ``close()``
        flushing can. (With a short window the timer fires during the drain and
        the test passes even with the flush deleted -- verified by mutation.)
        """
        monkeypatch.setattr(tg_client, "_ALBUM_WINDOW_S", 30.0)
        monkeypatch.setattr(tg_client, "_ALBUM_MAX_WAIT_S", 30.0)
        client, received = _make_client()
        client._dispatch(_photo_update(file_id="pending", group="g", caption="q"))
        assert received == [], "still buffered"

        await client.close()
        # Bounded: give the flush's handler task a moment to run, but never wait
        # on the 30s timer -- if close() did not flush, this stays empty.
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.01)

        assert len(received) == 1, "close() must attempt delivery of buffered albums"
        assert received[0].text == "q"
        assert client._albums == {}, "a closed client must not retain album state"
        assert client._album_timers == {}
