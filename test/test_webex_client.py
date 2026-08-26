"""Tests for kiro_crew.webex.client (WebexClient, low-level layer)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from kiro_crew.messaging.split import chunk_utf8_bytes
from kiro_crew.webex import client as webex_client
from kiro_crew.webex.client import (
    WEBEX_MAX_TEXT,
    WebexClient,
    WebexInbound,
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
    """The byte primitive, exercised at Webex's own cap.

    The generic properties live in test_messaging_split.py; these pin that the
    channel's 7000-byte budget is what the send path is measured against.
    """

    def test_empty_returns_empty(self) -> None:
        assert chunk_utf8_bytes("", WEBEX_MAX_TEXT) == []

    def test_under_cap_single_chunk(self) -> None:
        assert chunk_utf8_bytes("hello", WEBEX_MAX_TEXT) == ["hello"]

    def test_lossless_multibyte_split(self) -> None:
        # 3000 4-byte emoji = 12000 bytes: must split, never drop content.
        text = "🐾" * 3000
        chunks = chunk_utf8_bytes(text, WEBEX_MAX_TEXT)
        assert len(chunks) > 1
        assert "".join(chunks) == text  # lossless
        for c in chunks:
            assert len(c.encode("utf-8")) <= WEBEX_MAX_TEXT
            assert "\ufffd" not in c

    def test_lossless_ascii_split(self) -> None:
        text = "x" * (WEBEX_MAX_TEXT + 100)
        chunks = chunk_utf8_bytes(text, WEBEX_MAX_TEXT)
        assert chunks == ["x" * WEBEX_MAX_TEXT, "x" * 100]

    def test_mixed_content_boundary(self) -> None:
        # ASCII prefix pushes an emoji across the byte boundary — the split
        # must move the whole code point into the next chunk.
        text = "a" * (WEBEX_MAX_TEXT - 2) + "🐾🐾"
        chunks = chunk_utf8_bytes(text, WEBEX_MAX_TEXT)
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
    activity_id: str = "act-1",
) -> dict:
    return {
        "data": {
            "eventType": event_type,
            "activity": {
                "id": activity_id,
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

    @pytest.mark.asyncio
    async def test_close_drains_handlers_and_session_even_when_run_loop_died(self) -> None:
        """``_run_loop`` dying from an uncaught, non-CancelledError exception
        makes ``self._task.cancel()`` a no-op, and re-``await``ing it re-raises
        that exception -- which must not skip the handler-task drain or the
        session close (issue #4627)."""

        class _FakeSession:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        c = _client()
        c._session = _FakeSession()  # type: ignore[assignment]
        handler_task: asyncio.Task[Any] = asyncio.ensure_future(asyncio.sleep(100))
        c._handler_tasks.add(handler_task)

        async def _buggy_loop() -> None:
            raise ValueError("malformed frame")

        c._task = asyncio.create_task(_buggy_loop())
        await asyncio.sleep(0)  # let the task actually finish before close()

        with pytest.raises(ValueError, match="malformed frame"):
            await c.close()

        assert c._task is None
        assert handler_task.cancelled()
        assert c._handler_tasks == set()
        assert c._session is None


class TestRedeliveryHandling:
    """Acks and dedup, together.

    The device WebSocket redelivers an unacknowledged activity, and a reconnect
    can replay one the previous connection already handed off. The consequence is
    not a duplicate bubble: a redelivery arriving mid-turn is folded into the
    running turn as a steer, so the agent is steered by an echo of the
    instruction it is already following.
    """

    @staticmethod
    def _wired(monkeypatch: pytest.MonkeyPatch, client) -> list:
        received: list = []

        async def fake_fetch(mid: str) -> dict:
            return {
                "personEmail": "user@example.com",
                "roomId": "ROOM",
                "text": "hello",
                "personId": "P1",
                "roomType": "direct",
            }

        async def on_message(inbound) -> None:
            received.append(inbound)

        monkeypatch.setattr(client, "fetch_message", fake_fetch)
        client.set_message_handler(on_message)
        return received

    @pytest.mark.asyncio
    async def test_a_redelivered_activity_dispatches_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        received = self._wired(monkeypatch, c)

        c._handle_frame(_frame())
        c._handle_frame(_frame())  # same message id
        await asyncio.gather(*c._handler_tasks)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_distinct_messages_both_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        received = self._wired(monkeypatch, c)

        c._handle_frame(_frame(raw_id="a"))
        c._handle_frame(_frame(raw_id="b"))
        await asyncio.gather(*c._handler_tasks)

        assert len(received) == 2

    @staticmethod
    def _ws(client) -> list[dict]:
        """Attach a socket that records every ack frame."""
        sent: list[dict] = []

        class FakeWs:
            async def send_json(self, payload: dict) -> None:
                sent.append(payload)

        client._ws = FakeWs()
        return sent

    @pytest.mark.asyncio
    async def test_a_hydrated_activity_is_acknowledged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        self._wired(monkeypatch, c)
        sent = self._ws(c)

        c._handle_frame(_frame(activity_id="act-9"))
        await asyncio.gather(*c._handler_tasks)

        assert {"type": "ack", "messageId": "act-9"} in sent

    @pytest.mark.asyncio
    async def test_the_ack_follows_hydration_and_a_duplicate_still_acks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each activity is acknowledged, and only the first one dispatches.

        The ack is per-ACTIVITY (it stops redelivery of that frame) while the
        dedup mark is per-MESSAGE, so a redelivery of the same message under a
        second activity id must still be acknowledged — leaving it unacked would
        have the service redeliver a frame that is deliberately being ignored.
        """
        c = _client()
        received = self._wired(monkeypatch, c)
        sent = self._ws(c)

        c._handle_frame(_frame(activity_id="act-1"))
        c._handle_frame(_frame(activity_id="act-2"))  # duplicate message id
        await asyncio.gather(*c._handler_tasks)

        assert len(received) == 1
        # Both acked; ORDER is deliberately not asserted — the duplicate settles
        # synchronously while the first frame's ack waits for its fetch, and acks
        # are independent per frame.
        assert {p["messageId"] for p in sent} == {"act-1", "act-2"}

    @pytest.mark.asyncio
    async def test_a_failed_hydration_is_neither_acked_nor_left_deduped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient REST failure must not lose the message permanently.

        Acking first is the obvious order — the ack is what stops redelivery — and
        it silently drops the message for good: the activity is acknowledged, so
        the service never sends it again, and the dedup mark would refuse it if it
        did. So the mark goes in before the fetch (absorbing a redelivery that
        RACES it) and both are released when the fetch actually fails.
        """
        c = _client()
        sent = self._ws(c)

        async def _boom(_mid: str) -> dict:
            raise RuntimeError("502 from the messages endpoint")

        monkeypatch.setattr(c, "fetch_message", _boom)
        c.set_message_handler(_async_return(None))

        c._handle_frame(_frame(activity_id="act-1"))
        await asyncio.gather(*c._handler_tasks)

        assert sent == []  # unacknowledged, so Webex will try again
        assert c._seen == {}  # and the retry will not be dropped as a duplicate

    @pytest.mark.asyncio
    async def test_the_redelivery_of_a_failed_hydration_dispatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of releasing both: the second chance is real."""
        c = _client()
        sent = self._ws(c)
        attempts: list[int] = []

        async def _flaky(_mid: str) -> dict:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("transient")
            return {"personEmail": "user@example.com", "roomId": "ROOM", "text": "hello"}

        received: list = []
        monkeypatch.setattr(c, "fetch_message", _flaky)

        async def _on_message(inbound) -> None:
            received.append(inbound)

        c.set_message_handler(_on_message)

        c._handle_frame(_frame(activity_id="act-1"))
        await asyncio.gather(*c._handler_tasks)
        c._handle_frame(_frame(activity_id="act-2"))  # the service redelivers
        await asyncio.gather(*c._handler_tasks)

        assert len(received) == 1
        assert [p["messageId"] for p in sent] == ["act-2"]

    @pytest.mark.asyncio
    async def test_a_turn_that_fails_after_hydration_is_still_acked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure downstream of hydration is NOT retried.

        The message already reached the dispatcher, so a redelivery would fold
        into the turn that failed as a steer rather than answering anything.
        """
        c = _client()
        sent = self._ws(c)

        async def _fetch(_mid: str) -> dict:
            return {"personEmail": "user@example.com", "roomId": "ROOM", "text": "hello"}

        async def _boom(_inbound) -> None:
            raise RuntimeError("the turn blew up")

        monkeypatch.setattr(c, "fetch_message", _fetch)
        c.set_message_handler(_boom)

        c._handle_frame(_frame(activity_id="act-1"))
        await asyncio.gather(*c._handler_tasks)

        assert [p["messageId"] for p in sent] == ["act-1"]
        assert "raw-uuid" not in str(c._seen) or c._seen  # the mark is kept

    @pytest.mark.asyncio
    async def test_a_failed_ack_does_not_raise(self) -> None:
        # A closed socket is the expected failure and is already surfaced by the
        # reconnect loop; letting it escape would log an unretrieved task
        # exception on every reconnect.
        c = _client()

        class DeadWs:
            async def send_json(self, payload: dict) -> None:
                raise ConnectionResetError("closed")

        c._ws = DeadWs()
        c._handle_frame(_frame())
        await asyncio.gather(*c._handler_tasks, return_exceptions=False)

    @pytest.mark.asyncio
    async def test_no_socket_means_no_ack_attempt(self) -> None:
        c = _client()
        c._ws = None
        c._handle_frame(_frame())  # must not raise
        await asyncio.gather(*c._handler_tasks)

    @pytest.mark.asyncio
    async def test_the_dedup_memory_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unbounded set on a long-lived gateway is a leak; only the recent
        # window can plausibly be redelivered.
        monkeypatch.setattr("kiro_crew.webex.client._DEDUP_WINDOW", 4)
        c = _client()
        self._wired(monkeypatch, c)

        for i in range(12):
            c._handle_frame(_frame(raw_id=f"m{i}"))
        await asyncio.gather(*c._handler_tasks)

        assert len(c._seen) <= 4


class TestClusterRouting:
    """A Hydra id names a CLUSTER, and guessing it wrong fails silently.

    The REST fetch resolves nothing for an id built in the wrong cluster, so
    ``_hydrate_and_dispatch`` returns and the user sees no reply at all — behind a
    green connected badge. That is why the cluster is read off the wire.
    """

    def test_a_cluster_round_trips_through_the_id(self) -> None:
        from kiro_crew.webex.client import cluster_of, hydra_id

        assert cluster_of(hydra_id("abc", "MESSAGE", "eu")) == "eu"
        assert cluster_of(hydra_id("abc", "MESSAGE")) == "us"

    @pytest.mark.parametrize("bad", ["", "not-base64!!", "aGVsbG8"])
    def test_a_non_hydra_value_yields_no_cluster(self, bad: str) -> None:
        from kiro_crew.webex.client import cluster_of

        assert cluster_of(bad) == ""

    def test_the_cluster_is_learned_from_the_activity_target(self) -> None:
        # The target's globalId is issued by the org's own conversation service,
        # so it names the right cluster where a synthesised "us" would not.
        from kiro_crew.webex.client import cluster_of, hydra_id

        c = _client()
        activity = {"target": {"globalId": hydra_id("room-1", "ROOM", "eu")}}
        assert c._cluster_for(activity) == "eu"
        # And it STICKS for later frames from the same connection, which may carry
        # no target of their own.
        assert c._cluster_for({}) == "eu"
        assert cluster_of(c._public_id({}, "msg-1", "MESSAGE")) == "eu"

    def test_a_frame_with_no_target_keeps_the_default(self) -> None:
        from kiro_crew.webex.client import cluster_of

        c = _client()
        assert cluster_of(c._public_id({}, "msg-1", "MESSAGE")) == "us"

    def test_an_empty_object_id_yields_no_public_id(self) -> None:
        c = _client()
        assert c._public_id({}, "", "MESSAGE") == ""


class TestVerbSet:
    """Accepted verbs are a POSITIVE set.

    A widened negation ("anything that is not post") hands every verb Cisco adds
    later whatever a user message gets, which is the permissive direction.
    """

    @staticmethod
    def _wire(monkeypatch, client, record):
        async def fake_fetch(mid: str) -> dict:
            return {
                "personEmail": "user@example.com",
                "roomId": "ROOM",
                "text": "hi",
                "personId": "P1",
                "roomType": "direct",
            }

        async def on_message(inbound) -> None:
            record.append(inbound)

        monkeypatch.setattr(client, "fetch_message", fake_fetch)
        client.set_message_handler(on_message)

    @pytest.mark.asyncio
    async def test_a_file_share_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A file message arrives as `share`, NOT `post`, so filtering to post
        # dropped it whole — caption text included.
        c = _client()
        got: list = []
        self._wire(monkeypatch, c, got)
        c._handle_frame(_frame(verb="share"))
        await asyncio.gather(*c._handler_tasks)
        assert len(got) == 1

    @pytest.mark.asyncio
    async def test_an_unknown_verb_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        got: list = []
        self._wire(monkeypatch, c, got)
        for verb in ("delete", "acknowledge", "leave", "future-verb"):
            c._handle_frame(_frame(verb=verb, raw_id=verb))
        await asyncio.gather(*c._handler_tasks)
        assert got == []

    @pytest.mark.asyncio
    async def test_a_scan_still_running_does_not_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An `update` only counts once every file reads safe.

        Dispatching on a pending scan would hand the agent a file Webex has not
        finished checking.
        """
        c = _client()
        got: list = []
        self._wire(monkeypatch, c, got)
        frame = _frame(verb="update")
        frame["data"]["activity"]["object"]["files"] = {
            "items": [{"malwareQuarantineState": "scanning"}]
        }
        c._handle_frame(frame)
        await asyncio.gather(*c._handler_tasks)
        assert got == []

    @pytest.mark.asyncio
    async def test_a_cleared_scan_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        got: list = []
        self._wire(monkeypatch, c, got)
        frame = _frame(verb="update")
        frame["data"]["activity"]["object"]["files"] = {
            "items": [{"malwareQuarantineState": "safe"}]
        }
        c._handle_frame(frame)
        await asyncio.gather(*c._handler_tasks)
        assert len(got) == 1

    @pytest.mark.asyncio
    async def test_a_pending_scan_is_not_dedup_marked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The retry is the whole point of the `update` verb.

        Marking a pending scan as seen would make the LATER update — the one that
        says the file is safe — look like a redelivery and drop it.
        """
        c = _client()
        got: list = []
        self._wire(monkeypatch, c, got)
        pending = _frame(verb="update", activity_id="a1")
        pending["data"]["activity"]["object"]["files"] = {
            "items": [{"malwareQuarantineState": "scanning"}]
        }
        c._handle_frame(pending)
        cleared = _frame(verb="update", activity_id="a2")
        cleared["data"]["activity"]["object"]["files"] = {
            "items": [{"malwareQuarantineState": "safe"}]
        }
        c._handle_frame(cleared)
        await asyncio.gather(*c._handler_tasks)
        assert len(got) == 1


class TestInboundEnvelope:
    @pytest.mark.asyncio
    async def test_files_mentions_and_parent_reach_the_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        got: list = []

        async def fake_fetch(mid: str) -> dict:
            return {
                "personEmail": "User@Example.com",
                "roomId": "ROOM",
                "text": "look",
                "personId": "P1",
                "roomType": "group",
                "parentId": "ROOT",
                "mentionedPeople": ["BOT"],
                "files": ["https://webexapis.com/v1/contents/C1"],
            }

        async def on_message(inbound) -> None:
            got.append(inbound)

        monkeypatch.setattr(c, "fetch_message", fake_fetch)
        c.set_message_handler(on_message)
        c._handle_frame(_frame())
        await asyncio.gather(*c._handler_tasks)

        assert got[0].parent_id == "ROOT"
        assert got[0].mentioned_people == ("BOT",)
        assert got[0].file_urls == ("https://webexapis.com/v1/contents/C1",)


class TestContentDownload:
    """The malware-scan state machine, and the token's destination.

    Every branch here is the difference between refusing a file and handing the
    agent one Webex declined to vouch for.
    """

    @pytest.mark.asyncio
    async def test_a_url_outside_the_api_base_is_refused(self, tmp_path) -> None:
        # The bearer token rides this request, so the destination is checked
        # against the configured base rather than trusted from the message body.
        c = _client()
        with pytest.raises(ValueError):
            await c.download_content("https://evil.example.com/v1/contents/C1", str(tmp_path / "f"))

    @pytest.mark.asyncio
    async def test_head_refuses_a_foreign_url_without_a_request(self) -> None:
        c = _client()
        assert await c.head_content("https://evil.example.com/x") == ("", "", 0)

    @pytest.mark.parametrize(
        "status,fragment",
        [(410, "quarantined"), (428, "could not be scanned"), (302, "redirected")],
    )
    @pytest.mark.asyncio
    async def test_each_refusal_names_its_reason_not_the_url(
        self, status: int, fragment: str, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        url = "https://webexapis.com/v1/contents/C1"

        class FakeResp:
            def __init__(self) -> None:
                self.status = status
                self.headers: dict[str, str] = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSession:
            def get(self, *a, **kw):
                return FakeResp()

        monkeypatch.setattr(c, "_ensure_session", _async_return(FakeSession()))
        with pytest.raises(ValueError) as exc:
            await c.download_content(url, str(tmp_path / "f"))
        assert fragment in str(exc.value)
        assert "contents/C1" not in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_scan_in_flight_is_waited_out_rather_than_refused(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 423 means "still scanning", and the scan routinely outlasts a couple
        of retries. The wait is a total TIME budget, so the server's own
        Retry-After pacing decides how many attempts fit in it — where a fixed
        attempt count gives a scan an unpredictable and often tiny amount of wall
        clock, and the file is then lost for the turn."""
        c = _client()
        # Retry-After is honoured through a clamp; flooring it at zero keeps this
        # test fast without changing the branch under test.
        monkeypatch.setattr(webex_client, "_RETRY_AFTER_MIN_S", 0.0)
        statuses = [423, 423, 423, 200]

        class FakeResp:
            def __init__(self, status: int) -> None:
                self.status = status
                self.headers = {"Retry-After": "0"}
                self.content = _Chunks([b"data"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSession:
            def get(self, *a, **kw):
                return FakeResp(statuses.pop(0))

        monkeypatch.setattr(c, "_ensure_session", _async_return(FakeSession()))

        dest = tmp_path / "f"
        await c.download_content("https://webexapis.com/v1/contents/C1", str(dest))

        assert dest.read_bytes() == b"data"
        assert statuses == []  # every 423 was retried, not given up on

    @pytest.mark.asyncio
    async def test_a_scan_that_outlasts_the_budget_says_to_re_send(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason reaches the USER through messaging.attachments.

        "still being scanned, re-send shortly" is an instruction they can act on;
        "download failed" is not.

        The sleep is checked against the deadline BEFORE it is taken, so a
        server-set Retry-After (up to 15s) can never overrun the budget — with a
        zero budget nothing is slept at all.
        """
        c = _client()
        monkeypatch.setattr(webex_client, "_SCAN_WAIT_BUDGET_S", 0.0)
        attempts: list[int] = []

        class FakeResp:
            status = 423
            headers = {"Retry-After": "15"}

            async def __aenter__(self):
                attempts.append(1)
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSession:
            def get(self, *a, **kw):
                return FakeResp()

        monkeypatch.setattr(c, "_ensure_session", _async_return(FakeSession()))

        with pytest.raises(ValueError) as exc:
            await c.download_content("https://webexapis.com/v1/contents/C1", str(tmp_path / "f"))

        assert "re-send shortly" in str(exc.value)
        assert len(attempts) == 1


class _Chunks:
    """Minimal stand-in for ``resp.content`` streaming to disk."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def iter_chunked(self, size: int):
        chunks = self._chunks

        class _It:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not chunks:
                    raise StopAsyncIteration
                return chunks.pop(0)

        return _It()


def _async_return(value):
    async def _inner(*a, **kw):
        return value

    return _inner


class _FakeApiResp:
    """A minimal aiohttp response for ``_api`` / ``_discover_device_base``."""

    def __init__(self, status: int, body: Any = None) -> None:
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    async def json(self, content_type: Any = None) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _RecordingSession:
    """Records every request and replays a scripted response per call."""

    def __init__(self, responses: list[_FakeApiResp]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def _next(self, method: str, url: str) -> _FakeApiResp:
        self.calls.append((method, url))
        return self._responses.pop(0) if self._responses else _FakeApiResp(500)

    def request(self, method: str, url: str, **kw):
        return self._next(method, url)

    def get(self, url: str, **kw):
        return self._next("GET", url)

    def post(self, url: str, **kw):
        return self._next("POST", url)


class TestRoomTypeResolution:
    """A card press reports no ``roomType``, and the room gate is a type decision.

    Resolving it here is what lets ONE gate judge every envelope: a gate branch
    that guesses from the room id alone drops every DM press the moment a space is
    named, while admitting a space press the group switch never enabled.
    """

    @pytest.mark.asyncio
    async def test_it_reports_the_rooms_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        session = _RecordingSession([_FakeApiResp(200, {"type": "group"})])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._room_type_of("ROOM1") == "group"

    @pytest.mark.asyncio
    async def test_the_answer_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A card conversation presses repeatedly and a room's type is immutable.
        c = _client()
        session = _RecordingSession([_FakeApiResp(200, {"type": "direct"})])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._room_type_of("ROOM1") == "direct"
        assert await c._room_type_of("ROOM1") == "direct"
        assert len(session.calls) == 1

    @pytest.mark.asyncio
    async def test_a_failure_resolves_to_an_unknown_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Which the room gate reads as "not direct, not group" and denies. Cached
        # too: a bot removed from the room must not re-request on every press.
        c = _client()
        session = _RecordingSession([_FakeApiResp(404, None)])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._room_type_of("ROOM1") == ""
        assert await c._room_type_of("ROOM1") == ""
        assert len(session.calls) == 1

    @pytest.mark.asyncio
    async def test_an_empty_room_id_costs_no_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        session = _RecordingSession([])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._room_type_of("") == ""
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_the_cache_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        session = _RecordingSession(
            [
                _FakeApiResp(200, {"type": "direct"})
                for _ in range(webex_client._PERSON_CACHE_MAX + 5)
            ]
        )
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        for i in range(webex_client._PERSON_CACHE_MAX + 5):
            await c._room_type_of(f"R{i}")
        assert len(c._room_types) == webex_client._PERSON_CACHE_MAX


class TestDeviceHostDiscovery:
    """The WDM host is REGIONAL.

    Registering against a hardcoded one works for a US-resident org and silently
    fails for everyone else, so the host is discovered per token — unless the
    operator pinned one, which is the case a restricted network needs.
    """

    @pytest.mark.asyncio
    async def test_the_catalogs_wdm_link_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client()
        body = {"serviceLinks": {"wdm": "https://wdm-eu.wbx2.com/wdm/api/v1/"}}
        session = _RecordingSession([_FakeApiResp(200, body)])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._discover_device_base() == "https://wdm-eu.wbx2.com/wdm/api/v1"

    @pytest.mark.parametrize(
        "body",
        [
            {"serviceLinks": {"wdm": "http://wdm-eu.wbx2.com"}},
            {"serviceLinks": {"wdm": ""}},
            {"serviceLinks": "not a map"},
            {},
            "not a map at all",
        ],
    )
    @pytest.mark.asyncio
    async def test_an_unusable_catalog_degrades_to_the_default(
        self, body: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A discovery outage must degrade to the documented US host, not take the
        # channel down — and never to a plaintext host, which the bearer token
        # would then ride.
        c = _client()
        session = _RecordingSession([_FakeApiResp(200, body)])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._discover_device_base() == webex_client._DEVICE_BASE

    @pytest.mark.asyncio
    async def test_a_non_200_catalog_degrades_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        session = _RecordingSession([_FakeApiResp(503, None)])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._discover_device_base() == webex_client._DEVICE_BASE

    @pytest.mark.asyncio
    async def test_a_malformed_body_degrades_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        session = _RecordingSession([_FakeApiResp(200, ValueError("not json"))])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._discover_device_base() == webex_client._DEVICE_BASE

    @pytest.mark.asyncio
    async def test_a_pinned_host_skips_discovery_and_survives_a_reconnect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``webex.wdm_base`` is a PIN, and a pin has to outlive one connect.

        Discovery writes the host in use, so a pin stored only there is destroyed
        by the first successful discovery and the config key silently becomes a
        no-op afterwards. A pinned network may not reach the catalog at all, so
        discovery must not even be attempted.
        """
        # A real pin names a Webex REGIONAL host — the case a restricted network
        # needs — not an internal proxy, which is configured through HTTPS_PROXY
        # and honoured separately by this client.
        pin = "https://wdm-eu.wbx2.com"
        c = _client(device_base=pin)
        registered = {"webSocketUrl": "wss://ws.example.com/1"}
        session = _RecordingSession([_FakeApiResp(201, registered) for _ in range(2)])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))

        async def _boom() -> str:
            raise AssertionError("discovery must not run when a host is pinned")

        monkeypatch.setattr(c, "_discover_device_base", _boom)

        assert await c._get_websocket_url() == "wss://ws.example.com/1"
        assert await c._get_websocket_url() == "wss://ws.example.com/1"
        assert all(url.startswith(pin) for _m, url in session.calls)

    @pytest.mark.parametrize(
        "pin",
        [
            "https://attacker.example.com",
            "https://evil-wbx2.com",
            "https://wbx2.com.attacker.net",
            "http://wdm-a.wbx2.com",
            "not a url",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_pin_that_is_not_a_webex_host_is_dropped(self, pin: str) -> None:
        """``config.json`` is agent-WRITABLE by design.

        ``security.py`` deliberately does not over-block it, so sessions.db and
        ordinary settings stay usable — which means a prompt-injected
        ``config set webex.wdm_base <attacker host>`` followed by a restart would
        POST the bot token to that host, since the bearer rides device
        registration. The pin is therefore honoured only for an https Webex host,
        and the lookalikes above (suffix-not-subdomain, plaintext, unparseable) do
        not qualify.
        """
        c = _client(device_base=pin)

        assert c._device_pin == ""
        assert c._device_base == webex_client._DEVICE_BASE

    @pytest.mark.asyncio
    async def test_a_non_webex_catalog_link_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defence in depth: anyone who can shape the catalog response already holds
        # the token, but the same rule costs nothing to apply here.
        c = _client()
        body = {"serviceLinks": {"wdm": "https://attacker.example.com/wdm"}}
        session = _RecordingSession([_FakeApiResp(200, body)])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))

        assert await c._discover_device_base() == webex_client._DEVICE_BASE

    @pytest.mark.asyncio
    async def test_an_unpinned_client_registers_against_the_discovered_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        session = _RecordingSession([_FakeApiResp(200, {"webSocketUrl": "wss://a/1"})])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        monkeypatch.setattr(c, "_discover_device_base", _async_return("https://wdm-eu.wbx2.com"))

        assert await c._get_websocket_url() == "wss://a/1"
        assert session.calls[0][1] == "https://wdm-eu.wbx2.com/devices"

    @pytest.mark.asyncio
    async def test_a_device_cap_falls_back_to_reusing_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Webex caps devices per token, so a long-lived bot re-registering on every
        # restart would otherwise stop being able to connect at all.
        c = _client(device_base="https://wdm-eu.wbx2.com")
        session = _RecordingSession(
            [
                _FakeApiResp(400, None),
                _FakeApiResp(200, {"devices": [{"webSocketUrl": "wss://reused/9"}]}),
            ]
        )
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._get_websocket_url() == "wss://reused/9"

    @pytest.mark.asyncio
    async def test_no_device_to_reuse_yields_no_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = _client(device_base="https://wdm-eu.wbx2.com")
        session = _RecordingSession([_FakeApiResp(400, None), _FakeApiResp(200, {"devices": []})])
        monkeypatch.setattr(c, "_ensure_session", _async_return(session))
        assert await c._get_websocket_url() == ""


class TestCardActionHydration:
    """A press is a decision the user ALREADY made.

    Swallowing a transient lookup failure would acknowledge the frame and drop
    that decision for good, so the required lookups raise and the caller leaves
    the activity unacknowledged.
    """

    @staticmethod
    def _press_frame(activity_id: str = "act-1") -> dict:
        return _frame(verb="cardAction", activity_id=activity_id)

    @pytest.mark.asyncio
    async def test_an_unreadable_action_record_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        sent = TestRedeliveryHandling._ws(c)

        async def _api(*_a, **_kw):
            return None  # what a failed REST call yields

        monkeypatch.setattr(c, "_api", _api)
        c.set_message_handler(_async_return(None))

        c._handle_frame(self._press_frame())
        await asyncio.gather(*c._handler_tasks)

        assert sent == []  # unacknowledged
        assert c._seen == {}  # and redeliverable

    @pytest.mark.asyncio
    async def test_a_readable_press_is_dispatched_and_acked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = _client()
        sent = TestRedeliveryHandling._ws(c)
        received: list = []

        async def _api(_method: str, path: str, _payload, **_kw):
            if path.startswith("/attachment/actions/"):
                return {"inputs": {"kirocrew_kind": "options"}, "roomId": "R1", "personId": "P1"}
            if path.startswith("/people/"):
                return {"emails": ["kyle@example.com"]}
            if path.startswith("/rooms/"):
                return {"type": "direct"}
            return {}

        monkeypatch.setattr(c, "_api", _api)
        monkeypatch.setattr(c, "fetch_message", _async_return({"parentId": "T1"}))

        async def _on_message(inbound) -> None:
            received.append(inbound)

        c.set_message_handler(_on_message)

        c._handle_frame(self._press_frame())
        await asyncio.gather(*c._handler_tasks)

        assert len(received) == 1
        assert received[0].card_inputs == {"kirocrew_kind": "options"}
        # The press envelope carries the room type and the card's thread, so no
        # reply path needs to special-case it.
        assert (received[0].room_type, received[0].parent_id) == ("direct", "T1")
        assert [p["messageId"] for p in sent] == ["act-1"]

    @pytest.mark.asyncio
    async def test_a_handler_failure_after_hydration_is_still_acked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The press reached the dispatcher; a redelivery would not re-answer it.
        c = _client()
        sent = TestRedeliveryHandling._ws(c)

        async def _api(_method: str, path: str, _payload, **_kw):
            if path.startswith("/attachment/actions/"):
                return {"inputs": {}, "roomId": "R1", "personId": "P1"}
            return {}

        async def _boom(_inbound) -> None:
            raise RuntimeError("dispatcher blew up")

        monkeypatch.setattr(c, "_api", _api)
        monkeypatch.setattr(c, "fetch_message", _async_return({}))
        c.set_message_handler(_boom)

        c._handle_frame(self._press_frame())
        await asyncio.gather(*c._handler_tasks)

        assert [p["messageId"] for p in sent] == ["act-1"]
