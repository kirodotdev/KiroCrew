"""Tests for kiro_crew.webex.client (WebexClient, low-level layer)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from kiro_crew.webex.client import (
    WEBEX_MAX_TEXT,
    WebexClient,
    WebexInbound,
    chunk_utf8,
    hydra_id,
    truncate_utf8,
)


class TestTruncateUtf8:
    def test_ascii_under_cap_unchanged(self) -> None:
        assert truncate_utf8("hello") == "hello"

    def test_caps_by_bytes_not_chars(self) -> None:
        # 4-byte emoji: 3000 chars = 12000 bytes — over the 7000-byte limit
        # while comfortably under a 7000-CHAR cap.
        text = "🐾" * 3000
        out = truncate_utf8(text)
        assert len(out.encode("utf-8")) <= WEBEX_MAX_TEXT
        assert len(out) < 3000

    def test_never_splits_a_code_point(self) -> None:
        # A boundary that lands mid-emoji must drop the partial sequence,
        # not raise or emit a replacement character.
        text = "a" + "🐾" * 2000
        out = truncate_utf8(text)
        assert "\ufffd" not in out
        out.encode("utf-8")  # round-trips cleanly

    def test_exact_boundary_kept(self) -> None:
        text = "x" * WEBEX_MAX_TEXT
        assert truncate_utf8(text) == text


class TestChunkUtf8:
    def test_empty_returns_empty(self) -> None:
        assert chunk_utf8("") == []

    def test_under_cap_single_chunk(self) -> None:
        assert chunk_utf8("hello") == ["hello"]

    def test_lossless_multibyte_split(self) -> None:
        # 3000 4-byte emoji = 12000 bytes: must split, never drop content.
        text = "🐾" * 3000
        chunks = chunk_utf8(text)
        assert len(chunks) > 1
        assert "".join(chunks) == text  # lossless
        for c in chunks:
            assert len(c.encode("utf-8")) <= WEBEX_MAX_TEXT
            assert "\ufffd" not in c

    def test_lossless_ascii_split(self) -> None:
        text = "x" * (WEBEX_MAX_TEXT + 100)
        chunks = chunk_utf8(text)
        assert chunks == ["x" * WEBEX_MAX_TEXT, "x" * 100]

    def test_mixed_content_boundary(self) -> None:
        # ASCII prefix pushes an emoji across the byte boundary — the split
        # must move the whole code point into the next chunk.
        text = "a" * (WEBEX_MAX_TEXT - 2) + "🐾🐾"
        chunks = chunk_utf8(text)
        assert "".join(chunks) == text
        for c in chunks:
            assert len(c.encode("utf-8")) <= WEBEX_MAX_TEXT


class TestReadyState:
    def test_ready_starts_unset(self) -> None:
        c = _client()
        assert not c.ready.is_set()

    @pytest.mark.asyncio
    async def test_wait_ready_times_out_when_never_connected(self) -> None:
        c = _client()
        assert await c.wait_ready(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_wait_ready_returns_true_once_set(self) -> None:
        c = _client()
        c.ready.set()
        assert await c.wait_ready(timeout=0.05) is True

    def test_notify_state_calls_observer_and_swallows_errors(self) -> None:
        c = _client()
        seen: list[tuple[bool, str]] = []
        c.on_state_change = lambda ok, err: seen.append((ok, err))
        c._notify_state(True, "")
        c._notify_state(False, "boom")
        assert seen == [(True, ""), (False, "boom")]

        def _raiser(ok: bool, err: str) -> None:
            raise RuntimeError("observer bug")

        c.on_state_change = _raiser
        c._notify_state(True, "")  # must not raise


class TestHydraId:
    def test_encodes_message_id(self) -> None:
        raw = "9ba21fc0-1234-11ee-a1b2-abcdefabcdef"
        encoded = hydra_id(raw, "MESSAGE")
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        assert decoded == f"ciscospark://us/MESSAGE/{raw}"

    def test_no_padding(self) -> None:
        assert "=" not in hydra_id("abc", "MESSAGE")

    def test_matches_webex_documented_id_format(self) -> None:
        """Webex issues UNPADDED base64 ids — this is the documented example
        message id from the Webex API reference, and our encoding of its
        decoded URI must reproduce it byte-for-byte. Note also that a
        ``ciscospark://us/MESSAGE/{uuid}`` URI is exactly 60 bytes (24-byte
        prefix + 36-byte canonical UUID), which is divisible by 3, so its
        base64 encoding NEVER carries padding — ``rstrip("=")`` is a no-op
        for every real message event and exists only as defense for
        non-canonical id shapes."""
        documented = (
            "Y2lzY29zcGFyazovL3VzL01FU1NBR0UvOTJkYjNiZTAtNDNiZC0xMWU2" "LThhZTktZGQ1YjNkZmM1NjVk"
        )
        assert hydra_id("92db3be0-43bd-11e6-8ae9-dd5b3dfc565d", "MESSAGE") == documented

    def test_uuid_uris_never_generate_padding(self) -> None:
        import base64
        import uuid

        for rtype in ("MESSAGE", "ROOM"):
            uri = f"ciscospark://us/{rtype}/{uuid.uuid4()}"
            assert len(uri) % 3 == 0  # divisible by 3 -> base64 never pads
            assert not base64.b64encode(uri.encode()).decode().endswith("=")

    def test_empty_returns_empty(self) -> None:
        assert hydra_id("", "MESSAGE") == ""


def _client(**kw: Any) -> WebexClient:
    return WebexClient(token="tok", **kw)


def _frame(
    verb: str = "post",
    actor_email: str = "user@example.com",
    raw_id: str = "raw-uuid",
    event_type: str = "conversation.activity",
) -> dict:
    return {
        "data": {
            "eventType": event_type,
            "activity": {
                "verb": verb,
                "actor": {"emailAddress": actor_email},
                "object": {"id": raw_id},
                "target": {"id": "room-uuid"},
            },
        }
    }


class TestHandleFrame:
    @pytest.mark.asyncio
    async def test_post_activity_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        fetched: list[str] = []

        async def fake_fetch(mid: str) -> dict:
            fetched.append(mid)
            return {
                "personEmail": "User@Example.com",
                "roomId": "ROOM",
                "text": "hello",
                "personId": "P1",
                "roomType": "direct",
            }

        received: list[WebexInbound] = []

        async def on_message(inbound: WebexInbound) -> None:
            received.append(inbound)

        monkeypatch.setattr(c, "fetch_message", fake_fetch)
        c.set_message_handler(on_message)
        c._handle_frame(_frame())
        await asyncio.gather(*c._handler_tasks)

        assert fetched == [hydra_id("raw-uuid", "MESSAGE")]
        assert len(received) == 1
        assert received[0].person_email == "user@example.com"  # lowercased
        assert received[0].room_id == "ROOM"
        assert received[0].room_type == "direct"

    @pytest.mark.asyncio
    async def test_self_message_skipped_by_actor_email(self) -> None:
        c = _client()
        c.bot_email = "bot@webex.bot"
        c._handle_frame(_frame(actor_email="Bot@Webex.Bot".lower()))
        assert c._handler_tasks == set()

    @pytest.mark.asyncio
    async def test_self_message_skipped_by_person_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        c.bot_person_id = "BOT_PID"

        async def fake_fetch(mid: str) -> dict:
            return {"personId": "BOT_PID", "personEmail": "x@y.z", "roomId": "R", "text": "t"}

        received: list[WebexInbound] = []

        async def on_message(inbound: WebexInbound) -> None:
            received.append(inbound)

        monkeypatch.setattr(c, "fetch_message", fake_fetch)
        c.set_message_handler(on_message)
        c._handle_frame(_frame())
        await asyncio.gather(*c._handler_tasks)
        assert received == []

    def test_non_post_verb_ignored(self) -> None:
        c = _client()
        c._handle_frame(_frame(verb="add"))
        assert c._handler_tasks == set()

    def test_other_event_type_ignored(self) -> None:
        c = _client()
        c._handle_frame(_frame(event_type="apheleia.subscription_update"))
        assert c._handler_tasks == set()

    def test_malformed_frames_ignored(self) -> None:
        c = _client()
        c._handle_frame("not a dict")
        c._handle_frame({"data": "not a dict"})
        c._handle_frame({"data": {"eventType": "conversation.activity", "activity": None}})
        assert c._handler_tasks == set()


class TestOutbound:
    @pytest.mark.asyncio
    async def test_send_to_room_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_api(method: str, path: str, payload: dict | None, timeout: int = 30):
            calls.append((method, path, payload))
            return {"id": "MSG1"}

        monkeypatch.setattr(c, "_api", fake_api)
        mid = await c.send_message("ROOMID", "hi")
        assert mid == "MSG1"
        method, path, payload = calls[0]
        assert (method, path) == ("POST", "/messages")
        assert payload == {"markdown": "hi", "roomId": "ROOMID"}

    @pytest.mark.asyncio
    async def test_send_to_email_uses_to_person_email(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        calls: list[dict | None] = []

        async def fake_api(method: str, path: str, payload: dict | None, timeout: int = 30):
            calls.append(payload)
            return {"id": "MSG1"}

        monkeypatch.setattr(c, "_api", fake_api)
        await c.send_message("user@example.com", "hi")
        assert calls[0] == {"markdown": "hi", "toPersonEmail": "user@example.com"}

    @pytest.mark.asyncio
    async def test_send_truncates_to_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        seen: list[str] = []

        async def fake_api(method: str, path: str, payload: dict | None, timeout: int = 30):
            assert payload is not None
            seen.append(payload["markdown"])
            return {"id": "M"}

        monkeypatch.setattr(c, "_api", fake_api)
        await c.send_message("R", "x" * (WEBEX_MAX_TEXT + 500))
        assert len(seen[0]) == WEBEX_MAX_TEXT

    @pytest.mark.asyncio
    async def test_edit_message_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_api(method: str, path: str, payload: dict | None, timeout: int = 30):
            calls.append((method, path, payload))
            return {}

        monkeypatch.setattr(c, "_api", fake_api)
        ok = await c.edit_message("MSG1", "ROOM", "new text")
        assert ok is True
        method, path, payload = calls[0]
        assert (method, path) == ("PUT", "/messages/MSG1")
        assert payload == {"roomId": "ROOM", "markdown": "new text"}

    @pytest.mark.asyncio
    async def test_edit_failure_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()

        async def fake_api(method: str, path: str, payload: dict | None, timeout: int = 30):
            return None  # e.g. 400 edit-limit reached

        monkeypatch.setattr(c, "_api", fake_api)
        assert await c.edit_message("MSG1", "ROOM", "text") is False


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_close_drains_handlers_and_blocks_new_session(self) -> None:
        c = _client()
        await c.close()
        # Handler tasks drained.
        assert c._handler_tasks == set()
        # A subsequent session acquisition must fail closed.
        with pytest.raises(RuntimeError, match="WebexClient is closed"):
            await c._ensure_session()

    @pytest.mark.asyncio
    async def test_close_cancels_in_flight_handler(self) -> None:
        c = _client()
        # Inject a never-completing handler task, mirroring how _handle_frame
        # tracks live turn tasks.
        task: asyncio.Task[Any] = asyncio.ensure_future(asyncio.sleep(100))
        c._handler_tasks.add(task)
        await c.close()
        assert task.done()
        assert task.cancelled()
        assert c._handler_tasks == set()
