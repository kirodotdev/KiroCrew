"""Tests for kiro_crew.imessage.client (watch resume, dedupe, capability probe).

The JSON-RPC peer is replaced with a stub, so these run on any host: no ``imsg``
binary, no Messages database, no Mac.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.imessage import client as client_mod
from kiro_crew.imessage.client import (
    DEDUPE_WINDOW,
    ECHO_TTL_S,
    ECHO_WINDOW,
    WATCH_BUFFER_LIMIT,
    IMessageClient,
    _echo_key,
    normalize_handle,
    parse_inbound,
    redact_handle,
)
from kiro_crew.imessage.rpc import RpcError, RpcTransportError
from kiro_crew.imessage.transport import IMessageTransport

#: The handle every fixture message comes from, and the one the self-chat tests
#: put on the allowlist -- in that case it is the user's OWN handle.
OWNER = "+15551234567"

#: A realistic bridge readiness snapshot for a DEFAULT install: the injected
#: helper is absent (bridge.ready false) yet typing and read are still listed,
#: which is the documented exception this client relies on.
DEFAULT_SNAPSHOT: dict[str, Any] = {
    "version": "0.9.0",
    "protocol_version": 1,
    "database": {"path": "/Users/me/Library/Messages/chat.db", "ready": True},
    "bridge": {"ready": False, "error": "The bridge is not started."},
    "methods": ["initialize", "status", "watch.subscribe", "send", "typing", "read"],
}


class StubPeer:
    """Records calls, replies from a queue, and can push notifications."""

    def __init__(
        self,
        argv: list[str],
        *,
        on_notification: Any = None,
        on_disconnect: Any = None,
        cwd: Any = None,
    ) -> None:
        self.argv = list(argv)
        self.on_notification = on_notification
        #: Held so a test can fire it, standing in for the bridge exiting.
        self.on_disconnect = on_disconnect
        #: Mirrors the real peer's tracked hand-off set.
        self.handler_tasks: set[asyncio.Task[None]] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.started = False
        #: method -> list of results/exceptions, consumed in order.
        self.replies: dict[str, list[Any]] = {}
        self.default_result: dict[str, Any] = {}

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        queue = self.replies.get(method)
        if queue:
            reply = queue.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return dict(self.default_result)

    def params_for(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Deliver a notification the way the real peer does.

        The real `JsonRpcPeer` hands each notification to its own task and
        swallows a raising handler (`_invoke_notification`), so awaiting the
        handler directly here would let an exception surface in the test that
        the production reader never sees -- and would hide the fact that a
        failed handler must not stop later notifications.
        """
        assert self.on_notification is not None

        async def _invoke() -> None:
            try:
                await self.on_notification(method, params)
            except Exception:
                pass

        task = asyncio.create_task(_invoke())
        self.handler_tasks.add(task)
        task.add_done_callback(self.handler_tasks.discard)
        # Yield so the task starts before the caller asserts on its effects.
        await asyncio.sleep(0)


FAKE_BRIDGE = "/opt/bin/imsg"


@pytest.fixture
def peers(monkeypatch: pytest.MonkeyPatch) -> list[StubPeer]:
    """Capture every peer the client constructs (one per ``start``)."""
    made: list[StubPeer] = []

    def _factory(argv: list[str], **kwargs: Any) -> StubPeer:
        peer = StubPeer(argv, **kwargs)
        peer.replies = {
            "initialize": [dict(DEFAULT_SNAPSHOT)],
            "watch.subscribe": [{"subscription": 1, "buffer_limit": WATCH_BUFFER_LIMIT}],
        }
        made.append(peer)
        return peer

    monkeypatch.setattr(client_mod, "JsonRpcPeer", _factory)
    # The executable is resolved in code, never supplied by a caller or by
    # config, so the RESOLVER is the seam a test substitutes at. Patching it
    # here keeps the suite independent of whether imsg is installed on the
    # machine running it, without reopening a caller-settable path.
    monkeypatch.setattr(client_mod, "resolve_bridge_path", lambda: FAKE_BRIDGE)
    return made


def _message(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 101,
        "guid": "GUID-1",
        "chat_id": 7,
        "chat_guid": "iMessage;-;+15551234567",
        "chat_identifier": "+15551234567",
        "is_group": False,
        "sender": "+15551234567",
        "is_from_me": False,
        "text": "hello",
        "created_at": "2026-08-21T05:00:00Z",
        "attachments": [],
    }
    base.update(over)
    return base


async def _client(tmp_path: Path, **kwargs: Any) -> IMessageClient:
    received: list[Any] = []

    async def handler(inbound: Any) -> None:
        received.append(inbound)

    kwargs.setdefault("on_message", handler)
    kwargs.setdefault("cursor_path", tmp_path / "cursor.json")
    imc = IMessageClient(**kwargs)
    imc.received = received  # type: ignore[attr-defined]
    await imc.start()
    return imc


class TestNormalizeHandle:
    def test_phone_formatting_is_ignored(self) -> None:
        assert normalize_handle("+61 400 000 000") == "+61400000000"
        assert normalize_handle("(555) 123-4567") == "5551234567"

    def test_email_folds_to_lowercase(self) -> None:
        assert normalize_handle("  Me@Example.COM ") == "me@example.com"

    def test_empty_stays_empty_so_it_can_never_match_an_allowlist(self) -> None:
        assert normalize_handle("") == ""
        assert normalize_handle("   ") == ""


class TestRedactHandle:
    def test_a_handle_is_never_logged_whole(self) -> None:
        assert redact_handle("+15551234567") == "+15***"
        assert redact_handle("") == "?"


class TestParseInbound:
    def test_the_rowid_comes_from_id_not_rowid(self) -> None:
        # The bridge names the cursor field `id`; reading `rowid` would leave the
        # cursor at 0 and replay the whole history on every restart.
        inbound = parse_inbound(_message(id=4242))
        assert inbound is not None
        assert inbound.rowid == 4242

    def test_omitted_fields_are_absent_not_null(self) -> None:
        # The bridge omits inapplicable strings rather than sending null.
        inbound = parse_inbound({"id": 1, "sender": "+1", "text": "hi"})
        assert inbound is not None
        assert inbound.chat_guid == ""
        assert inbound.is_group is False

    def test_a_wrongly_typed_field_is_treated_as_absent(self) -> None:
        inbound = parse_inbound(_message(guid=12345, chat_id="seven"))
        assert inbound is not None
        assert inbound.guid == ""
        assert inbound.chat_id == 0

    def test_a_non_dict_payload_is_rejected(self) -> None:
        assert parse_inbound("nope") is None  # type: ignore[arg-type]

    def test_selector_prefers_the_portable_guid(self) -> None:
        inbound = parse_inbound(_message())
        assert inbound is not None
        # chat_id is scoped to one database instance, so it must not win.
        assert inbound.chat_selector == {"chat_guid": "iMessage;-;+15551234567"}

    def test_selector_falls_back_through_identifier_then_rowid(self) -> None:
        by_identifier = parse_inbound(_message(chat_guid=""))
        assert by_identifier is not None
        assert by_identifier.chat_selector == {"chat_identifier": "+15551234567"}
        by_rowid = parse_inbound(_message(chat_guid="", chat_identifier=""))
        assert by_rowid is not None
        assert by_rowid.chat_selector == {"chat_id": 7}
        none_at_all = parse_inbound(_message(chat_guid="", chat_identifier="", chat_id=0))
        assert none_at_all is not None
        assert none_at_all.chat_selector == {}


class TestStartupProbe:
    @pytest.mark.asyncio
    async def test_the_bridge_is_spawned_in_rpc_mode(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        # The path comes from the resolver, not from a caller argument: there is
        # no longer a settable cli_path for an agent-writable config to poison.
        assert peers[0].argv == [FAKE_BRIDGE, "rpc"]
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_db_path_override_is_passed_through(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path, db_path="/tmp/chat.db")
        assert peers[0].argv == [FAKE_BRIDGE, "rpc", "--db-path", "/tmp/chat.db"]
        await imc.close()

    @pytest.mark.asyncio
    async def test_typing_and_read_are_probed_from_the_readiness_snapshot(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        # Present even though bridge.ready is false — the documented exception.
        assert imc.typing_supported is True
        assert imc.read_supported is True
        assert peers[0].params_for("initialize") == [{"protocol_version": 1}]
        await imc.close()

    @pytest.mark.asyncio
    async def test_absent_optional_methods_degrade_silently(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _factory(argv: list[str], **kwargs: Any) -> StubPeer:
            peer = StubPeer(argv, **kwargs)
            snapshot = dict(DEFAULT_SNAPSHOT)
            snapshot["methods"] = ["initialize", "status", "watch.subscribe", "send"]
            peer.replies = {
                "initialize": [snapshot],
                "watch.subscribe": [{"subscription": 1}],
            }
            peers.append(peer)
            return peer

        monkeypatch.setattr(client_mod, "JsonRpcPeer", _factory)
        imc = await _client(tmp_path)
        assert imc.typing_supported is False
        assert imc.read_supported is False
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_missing_methods_list_denies_the_optional_calls(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _factory(argv: list[str], **kwargs: Any) -> StubPeer:
            peer = StubPeer(argv, **kwargs)
            peer.replies = {
                "initialize": [{"protocol_version": 1}],
                "watch.subscribe": [{"subscription": 1}],
            }
            peers.append(peer)
            return peer

        monkeypatch.setattr(client_mod, "JsonRpcPeer", _factory)
        imc = await _client(tmp_path)
        assert imc.typing_supported is False
        assert imc.read_supported is False
        await imc.close()

    @pytest.mark.asyncio
    async def test_an_unreadable_database_is_reported_not_swallowed(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Missing Full Disk Access is the common first-run failure, and it must
        # reach the operator as text rather than a channel that never answers.
        # The real shape is both halves failing together: the probe reports the
        # database as not ready AND the watch is refused with -32002, so the
        # probe's actionable message is what survives as the badge reason.
        def _factory(argv: list[str], **kwargs: Any) -> StubPeer:
            peer = StubPeer(argv, **kwargs)
            snapshot = dict(DEFAULT_SNAPSHOT)
            snapshot["database"] = {"ready": False, "error": "Full Disk Access required"}
            peer.replies = {
                "initialize": [snapshot],
                "watch.subscribe": [RpcError(-32002, "database unavailable")],
            }
            peers.append(peer)
            return peer

        monkeypatch.setattr(client_mod, "JsonRpcPeer", _factory)
        imc = IMessageClient(cursor_path=tmp_path / "c.json")
        with pytest.raises(RpcError):
            await imc.start()
        assert "Full Disk Access" in imc.last_error
        assert not imc.ready.is_set()
        await imc.close()


class TestWatchSubscription:
    @pytest.mark.asyncio
    async def test_a_fresh_install_subscribes_without_a_cursor(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        assert peers[0].params_for("watch.subscribe") == [{"buffer_limit": WATCH_BUFFER_LIMIT}]
        assert imc.ready.is_set()
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_persisted_cursor_is_replayed_on_the_next_start(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # Without this a gateway restart silently loses every message sent while
        # it was down.
        cursor = tmp_path / "cursor.json"
        cursor.write_text(json.dumps({"since_rowid": 9000}), encoding="utf-8")
        imc = await _client(tmp_path, cursor_path=cursor)
        assert peers[0].params_for("watch.subscribe") == [
            {"buffer_limit": WATCH_BUFFER_LIMIT, "since_rowid": 9000}
        ]
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_corrupt_cursor_file_starts_from_scratch(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        cursor = tmp_path / "cursor.json"
        cursor.write_text("{not json", encoding="utf-8")
        imc = await _client(tmp_path, cursor_path=cursor)
        assert peers[0].params_for("watch.subscribe") == [{"buffer_limit": WATCH_BUFFER_LIMIT}]
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_cursor_advances_and_persists_per_message(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        cursor = tmp_path / "cursor.json"
        imc = await _client(tmp_path, cursor_path=cursor)
        await peers[0].notify("message", {"subscription": 1, "message": _message(id=42)})
        assert json.loads(cursor.read_text(encoding="utf-8")) == {"since_rowid": 42}
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_cursor_advances_for_messages_this_channel_ignores(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # A cursor that only tracked DELIVERED messages would replay every
        # skipped row (the user's own traffic, group chats) on the next start.
        cursor = tmp_path / "cursor.json"
        imc = await _client(tmp_path, cursor_path=cursor, on_message=None)
        await peers[0].notify(
            "message", {"subscription": 1, "message": _message(id=77, is_from_me=True)}
        )
        assert json.loads(cursor.read_text(encoding="utf-8")) == {"since_rowid": 77}
        await imc.close()

    @pytest.mark.asyncio
    async def test_an_out_of_order_lower_rowid_never_rewinds_the_cursor(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        cursor = tmp_path / "cursor.json"
        imc = await _client(tmp_path, cursor_path=cursor)
        await peers[0].notify("message", {"subscription": 1, "message": _message(id=50)})
        await peers[0].notify(
            "message", {"subscription": 1, "message": _message(id=20, guid="GUID-2")}
        )
        assert json.loads(cursor.read_text(encoding="utf-8")) == {"since_rowid": 50}
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_readonly_home_does_not_stop_delivery(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        imc = await _client(tmp_path)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(client_mod, "atomic_write", _boom)
        await peers[0].notify("message", {"subscription": 1, "message": _message()})
        assert len(imc.received) == 1  # type: ignore[attr-defined]
        await imc.close()


class TestOverflowResume:
    @pytest.mark.asyncio
    async def test_overflow_resubscribes_at_the_returned_cursor(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # watch.overflow is TERMINAL: the subscription is already dead, so a
        # client that ignores it goes permanently silent under a burst.
        peer_replies = [{"subscription": 1}, {"subscription": 2}]
        imc = await _client(tmp_path)
        peer = peers[0]
        peer.replies["watch.subscribe"] = peer_replies[1:]
        await peer.notify(
            "watch.overflow",
            {
                "subscription": 1,
                "resume_after_rowid": 9000,
                "reason": "buffer_limit_exceeded",
                "terminal": True,
            },
        )
        await _until(lambda: len(peer.params_for("watch.subscribe")) == 2)
        assert peer.params_for("watch.subscribe")[1] == {
            "buffer_limit": WATCH_BUFFER_LIMIT,
            "since_rowid": 9000,
        }
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_resume_cursor_is_persisted_before_resubscribing(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        cursor = tmp_path / "cursor.json"
        imc = await _client(tmp_path, cursor_path=cursor)
        peers[0].replies["watch.subscribe"] = [{"subscription": 2}]
        await peers[0].notify("watch.overflow", {"subscription": 1, "resume_after_rowid": 500})
        await _until(lambda: len(peers[0].params_for("watch.subscribe")) == 2)
        assert json.loads(cursor.read_text(encoding="utf-8")) == {"since_rowid": 500}
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_failed_resubscribe_retries_and_reports_the_reason(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_mod, "RECONNECT_MIN_S", 0)
        monkeypatch.setattr(client_mod, "RECONNECT_MAX_S", 0)
        states: list[tuple[bool, str]] = []
        imc = await _client(tmp_path)
        imc.on_state_change = lambda connected, error: states.append((connected, error))
        peers[0].replies["watch.subscribe"] = [
            RpcError(-32002, "database unavailable"),
            {"subscription": 3},
        ]
        await peers[0].notify("watch.overflow", {"subscription": 1, "resume_after_rowid": 10})
        await _until(lambda: len(peers[0].params_for("watch.subscribe")) == 3)
        assert (False, "Messages database unavailable") in states
        assert imc.ready.is_set()
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_transport_failure_during_resubscribe_is_also_retried(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_mod, "RECONNECT_MIN_S", 0)
        monkeypatch.setattr(client_mod, "RECONNECT_MAX_S", 0)
        imc = await _client(tmp_path)
        peers[0].replies["watch.subscribe"] = [
            RpcTransportError("bridge exited"),
            {"subscription": 4},
        ]
        await peers[0].notify("watch.overflow", {"subscription": 1, "resume_after_rowid": 1})
        await _until(lambda: len(peers[0].params_for("watch.subscribe")) == 3)
        await imc.close()

    @pytest.mark.asyncio
    async def test_overflow_without_a_cursor_still_resubscribes(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        peers[0].replies["watch.subscribe"] = [{"subscription": 2}]
        await peers[0].notify("watch.overflow", {"subscription": 1})
        await _until(lambda: len(peers[0].params_for("watch.subscribe")) == 2)
        await imc.close()


class TestAtMostOnceDelivery:
    """Delivery is at-most-once, and that is asserted rather than assumed.

    The cursor advances when a row ARRIVES, matching Telegram's ``getUpdates``
    offset and ``issue_radar``'s watermark. The earlier at-least-once shape
    (advance only after the handler returned) is what forced a serialized worker,
    a buffer to serialize behind, and a consumer-retirement rule on reconnect --
    every piece of which turned out to be a defect. These tests pin the cheaper
    contract so a change back to at-least-once has to be deliberate.
    """

    @pytest.mark.asyncio
    async def test_the_cursor_advances_on_arrival_not_on_success(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        cursor = tmp_path / "cursor.json"
        started: list[Any] = []

        async def slow(inbound: Any) -> None:
            # Still running when the assertion below reads the cursor.
            started.append(inbound)
            await asyncio.sleep(0.05)

        imc = await _client(tmp_path, cursor_path=cursor, on_message=slow)
        await peers[0].notify("message", {"subscription": 1, "message": _message(id=4242)})
        for _ in range(500):
            if started:
                break
            await asyncio.sleep(0)
        assert json.loads(cursor.read_text(encoding="utf-8"))["since_rowid"] == 4242
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_raising_handler_does_not_replay_the_row(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The accepted cost of at-most-once: a failed row is dropped and logged,
        # and the user re-sends. Telegram, Webex and Teams all behave this way.
        cursor = tmp_path / "cursor.json"

        async def boom(_inbound: Any) -> None:
            raise RuntimeError("handler died mid-turn")

        imc = await _client(tmp_path, cursor_path=cursor, on_message=boom)
        await peers[0].notify("message", {"subscription": 1, "message": _message(id=4242)})
        for _ in range(500):
            if cursor.exists():
                break
            await asyncio.sleep(0)
        assert json.loads(cursor.read_text(encoding="utf-8"))["since_rowid"] == 4242
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_raising_handler_does_not_kill_inbound(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        delivered: list[Any] = []

        async def flaky(inbound: Any) -> None:
            if inbound.rowid == 1:
                raise RuntimeError("first one dies")
            delivered.append(inbound)

        imc = await _client(tmp_path, on_message=flaky)
        for rowid in (1, 2):
            await peers[0].notify(
                "message",
                {"subscription": 1, "message": _message(id=rowid, guid=f"G-{rowid}")},
            )
        for _ in range(500):
            if delivered:
                break
            await asyncio.sleep(0)
        assert [i.rowid for i in delivered] == [2]
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_row_with_no_handler_still_advances(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        cursor = tmp_path / "cursor.json"
        imc = IMessageClient(cursor_path=cursor)
        await imc.start()
        await peers[0].notify("message", {"subscription": 1, "message": _message(id=4245)})
        for _ in range(500):
            if cursor.exists():
                break
            await asyncio.sleep(0)
        assert json.loads(cursor.read_text(encoding="utf-8"))["since_rowid"] == 4245
        await imc.close()


class TestBridgeExit:
    """An unexpected bridge exit must be visible and retried, not silent.

    Before this, the reader simply ended: outstanding calls failed, but nothing
    marked the channel down and nothing retried, so inbound stopped while the
    dashboard still read connected -- for the rest of the process's life.
    """

    @pytest.mark.asyncio
    async def test_it_marks_the_channel_down_with_a_reason(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        states: list[tuple[bool, str]] = []
        imc = await _client(tmp_path)
        imc.on_state_change = lambda c, e: states.append((c, e))
        peers[0].on_disconnect("bridge exited (code 1)")
        assert states and states[-1][0] is False
        assert "bridge exited" in states[-1][1]
        assert not imc.ready.is_set()
        await imc.close()

    @pytest.mark.asyncio
    async def test_it_drops_the_dead_peer_and_respawns(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # Re-subscribing on the exited process could never succeed, so the
        # reconnect has to rebuild the peer -- a second StubPeer proves it did.
        imc = await _client(tmp_path)
        peers[0].on_disconnect("bridge exited")
        for _ in range(200):
            if len(peers) > 1:
                break
            await asyncio.sleep(0)
        assert len(peers) > 1, "reconnect did not spawn a new peer"
        await imc.close()

    @pytest.mark.asyncio
    async def test_our_own_close_does_not_report_a_disconnect(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # Closing deliberately must not look like the bridge dying, or shutdown
        # would flap the badge and start a reconnect against a closing client.
        states: list[tuple[bool, str]] = []
        imc = await _client(tmp_path)
        imc.on_state_change = lambda c, e: states.append((c, e))
        await imc.close()
        assert not any(e and "exited" in e for _c, e in states)


class TestDedupe:
    @pytest.mark.asyncio
    async def test_a_replayed_guid_is_delivered_once(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The overflow cursor is at or BEFORE the first dropped message, so the
        # bridge documents duplicate replay as possible by design.
        imc = await _client(tmp_path)
        for _ in range(3):
            await peers[0].notify("message", {"subscription": 1, "message": _message()})
        assert len(imc.received) == 1  # type: ignore[attr-defined]
        await imc.close()

    @pytest.mark.asyncio
    async def test_distinct_guids_all_get_through(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        for n in range(3):
            await peers[0].notify(
                "message",
                {"subscription": 1, "message": _message(id=100 + n, guid=f"G-{n}")},
            )
        assert len(imc.received) == 3  # type: ignore[attr-defined]
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_message_with_no_guid_is_not_suppressed(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # Dedupe keys on GUID; an absent one must not collapse into one bucket
        # and silently swallow real messages.
        imc = await _client(tmp_path)
        for n in range(2):
            await peers[0].notify(
                "message", {"subscription": 1, "message": _message(id=200 + n, guid="")}
            )
        assert len(imc.received) == 2  # type: ignore[attr-defined]
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_window_is_bounded_and_larger_than_the_watch_buffer(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # A window smaller than the buffer would let part of a full-buffer
        # overflow replay through as duplicates.
        assert DEDUPE_WINDOW > WATCH_BUFFER_LIMIT
        imc = await _client(tmp_path)
        for n in range(DEDUPE_WINDOW + 10):
            await peers[0].notify(
                "message",
                {"subscription": 1, "message": _message(id=1000 + n, guid=f"G-{n}")},
            )
        assert len(imc._seen_guids) <= DEDUPE_WINDOW
        await imc.close()


class TestOutbound:
    @pytest.mark.asyncio
    async def test_send_omits_the_service_on_the_default(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # Naming the default would exercise the SMS-fallback path on an install
        # that never asked for it.
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [{"ok": True, "id": 1979, "guid": "8DF"}]
        assert await imc.send("+15551234567", "hi") == "8DF"
        assert peers[0].params_for("send") == [{"to": "+15551234567", "text": "hi"}]
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_non_default_service_is_named(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path, service="auto")
        await imc.send("+1", "hi")
        assert peers[0].params_for("send") == [{"to": "+1", "text": "hi", "service": "auto"}]
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_missing_guid_is_success_not_failure(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # id/guid are best-effort in the bridge's contract.
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [{"ok": True}]
        assert await imc.send("+1", "hi") == ""
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_send_failure_raises_rather_than_reading_as_delivered(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        """A failed send must not be indistinguishable from a guid-less success.

        Returning ``""`` for both is what let a turn be recorded as answered
        when nothing reached the recipient, so the failure now propagates and
        each caller decides its own tolerance.
        """
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [RpcError(-32001, "delivery in flight")]
        with pytest.raises(RpcError):
            await imc.send("+1", "hi")
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_transport_failure_on_send_also_raises(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [RpcTransportError("bridge exited")]
        with pytest.raises(RpcTransportError):
            await imc.send("+1", "hi")
        await imc.close()

    @pytest.mark.asyncio
    async def test_empty_text_is_never_sent(self, tmp_path: Path, peers: list[StubPeer]) -> None:
        imc = await _client(tmp_path)
        assert await imc.send("+1", "") == ""
        assert peers[0].params_for("send") == []
        await imc.close()


class TestOptionalMethodsDegradePermanently:
    @pytest.mark.asyncio
    async def test_typing_is_disabled_after_one_rejection(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The parameter list is not part of the bridge's documented surface, so a
        # rejection means "not available here" -- retrying it every turn would
        # add a failed call to every single reply.
        imc = await _client(tmp_path)
        peers[0].replies["typing"] = [RpcError(-32602, "invalid params")]
        selector = {"chat_guid": "iMessage;-;+1"}
        await imc.send_typing(selector)
        assert imc.typing_supported is False
        await imc.send_typing(selector)
        assert len(peers[0].params_for("typing")) == 1
        await imc.close()

    @pytest.mark.asyncio
    async def test_read_is_disabled_after_one_rejection(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        peers[0].replies["read"] = [RpcError(-32601, "unknown method")]
        await imc.mark_read({"chat_guid": "g"})
        assert imc.read_supported is False
        await imc.mark_read({"chat_guid": "g"})
        assert len(peers[0].params_for("read")) == 1
        await imc.close()

    @pytest.mark.asyncio
    async def test_an_empty_selector_is_never_sent(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        await imc.send_typing({})
        await imc.mark_read({})
        assert peers[0].params_for("typing") == []
        assert peers[0].params_for("read") == []
        await imc.close()

    @pytest.mark.asyncio
    async def test_unprobed_methods_are_not_attempted(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        imc.typing_supported = False
        imc.read_supported = False
        await imc.send_typing({"chat_guid": "g"})
        await imc.mark_read({"chat_guid": "g"})
        assert peers[0].params_for("typing") == []
        assert peers[0].params_for("read") == []
        await imc.close()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_close_tears_down_the_peer_and_clears_ready(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        await imc.close()
        assert peers[0].closed
        assert not imc.ready.is_set()

    @pytest.mark.asyncio
    async def test_wait_ready_times_out_rather_than_hanging(self, tmp_path: Path) -> None:
        imc = IMessageClient(cursor_path=tmp_path / "c.json")
        assert await imc.wait_ready(timeout=0.01) is False

    @pytest.mark.asyncio
    async def test_a_handler_set_after_construction_still_receives(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # set_message_handler exists to break the client<->transport cycle.
        seen: list[Any] = []

        async def handler(inbound: Any) -> None:
            seen.append(inbound)

        imc = IMessageClient(cursor_path=tmp_path / "c.json")
        imc.set_message_handler(handler)
        await imc.start()
        await peers[0].notify("message", {"subscription": 1, "message": _message()})
        assert len(seen) == 1
        await imc.close()

    @pytest.mark.asyncio
    async def test_close_does_not_start_a_new_resubscribe(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        await imc.close()
        await imc._on_notification("watch.overflow", {"resume_after_rowid": 5})
        assert imc._resubscribe_task is None


class TestOwnEchoLedger:
    """The self-chat guard of issue #5246.

    In a self-chat the allow-listed handle is the identity the agent sends as, so
    ``is_from_me`` is the only thing separating the user's words from the agent's
    -- and the bridge writes it asynchronously (its 500ms watch debounce exists so
    a correction can land first) and documents ``sender`` as empty for some
    self-sent messages. These pin the one signal the client fully owns: a record
    of what it sent.
    """

    @pytest.mark.asyncio
    async def test_a_sent_body_coming_back_is_recognised_as_our_own(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        await imc.send("+1 (555) 123-4567", "on it")
        # Same body, same handle written differently, and the platform calling it
        # inbound -- which is exactly the shape that produced the loop.
        echoed = parse_inbound(_message(guid="OTHER", text="on it", is_from_me=False))
        assert echoed is not None
        assert imc.is_own_echo(echoed) is True
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_sent_guid_is_recognised_even_if_the_body_differs(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # `guid` is best-effort in the bridge's contract, so it is a second key
        # rather than the mechanism -- but a guid this client sent can never
        # legitimately arrive as user input, so it has no false positive.
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [{"ok": True, "id": 9, "guid": "SENT-GUID"}]
        assert await imc.send(OWNER, "on it") == "SENT-GUID"
        surprising = parse_inbound(_message(guid="SENT-GUID", text="not what we sent"))
        assert surprising is not None
        assert imc.is_own_echo(surprising) is True
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_match_is_consumed_so_a_genuine_repeat_gets_through(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # Consume-once is what keeps the guard from swallowing a conversation:
        # the echo is suppressed, and the user deliberately saying the same words
        # afterwards is ordinary input.
        imc = await _client(tmp_path)
        await imc.send(OWNER, "on it")
        echoed = parse_inbound(_message(guid="A", text="on it"))
        repeat = parse_inbound(_message(guid="B", text="on it"))
        assert echoed is not None and repeat is not None
        assert imc.is_own_echo(echoed) is True
        assert imc.is_own_echo(repeat) is False
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_guid_match_does_not_leave_the_body_live(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # A sent message is recognisable by GUID and by body. While those were
        # two independent entries, matching on the GUID left the body entry
        # alive, and the user's genuine repeat of those words inside the TTL was
        # then silently discarded -- no reply, no error (local GPT review of
        # b19990c9). One record per send is what makes that unreachable.
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [{"ok": True, "id": 9, "guid": "SENT-GUID"}]
        await imc.send(OWNER, "on it")
        echoed = parse_inbound(_message(guid="SENT-GUID", text="on it"))
        genuine = parse_inbound(_message(guid="LATER", text="on it"))
        assert echoed is not None and genuine is not None
        assert imc.is_own_echo(echoed) is True
        assert imc.is_own_echo(genuine) is False, "the body alias outlived the GUID match"
        assert imc._own_sends == []
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_body_match_does_not_leave_the_guid_live(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The mirror image, which the narrow fix for the above would have left
        # open: consume on the body, and the GUID of that same send must go too.
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [{"ok": True, "id": 9, "guid": "SENT-GUID"}]
        await imc.send(OWNER, "on it")
        echoed = parse_inbound(_message(guid="", text="on it"))
        assert echoed is not None
        assert imc.is_own_echo(echoed) is True
        assert imc._own_sends == []
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_definitively_rejected_send_leaves_no_record_behind(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # Rejected at validation, so nothing was delivered and nothing can echo.
        # Keeping the record would have the undelivered body suppress a genuine
        # message carrying those words for the rest of the TTL (local Opus
        # review of b19990c9).
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [RpcError(-32602, "invalid params")]
        with pytest.raises(RpcError):
            await imc.send(OWNER, "on it")
        assert imc._own_sends == []
        undelivered = parse_inbound(_message(text="on it"))
        assert undelivered is not None
        assert imc.is_own_echo(undelivered) is False
        await imc.close()

    @pytest.mark.asyncio
    async def test_an_ambiguous_failure_keeps_the_guard_and_extends_it(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The opposite failure mode of the cleanup above, and the one that
        # matters more (server GPT review of 20febaeb): the bridge documents
        # -32001 as "may have completed or remains in flight", so the message may
        # still be delivered and echo after this call failed. Dropping the record
        # there reopens the loop.
        #
        # The clock advances INSIDE the call, which is what a call that times out
        # actually does. The record is taken before the call, so by the time the
        # failure is handled its original expiry has passed -- advancing the clock
        # AROUND the call instead would make the refresh a no-op and this
        # assertion vacuous, which is how the first version of this test passed
        # against a mutation that deleted the refresh.
        clock = [1000.0]
        monkeypatch.setattr(client_mod.time, "monotonic", lambda: clock[0])
        imc = await _client(tmp_path)

        async def slow_failing_send(method: str, params: dict[str, Any], **_kw: Any) -> Any:
            if method == "send":
                clock[0] += ECHO_TTL_S + 5  # the call outlived the window
                raise RpcError(
                    -32001,
                    "delivery uncertain",
                    {"retry_safe": False, "disposition": "may_have_completed"},
                )
            return {"ok": True}

        imc._call = slow_failing_send  # type: ignore[method-assign]
        with pytest.raises(RpcError):
            await imc.send(OWNER, "on it")
        assert imc._own_sends != [], "an unproven failure must keep the guard"
        # A late delivery, past the expiry the record was originally given.
        late_echo = parse_inbound(_message(text="on it"))
        assert late_echo is not None
        assert imc.is_own_echo(late_echo) is True

    @pytest.mark.asyncio
    async def test_a_timeout_or_dead_bridge_keeps_the_guard(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # A transport failure proves nothing about what the bridge did with a
        # request already written to its stdin, so it is treated as delivered.
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [RpcTransportError("bridge stopped answering")]
        with pytest.raises(RpcTransportError):
            await imc.send(OWNER, "on it")
        assert imc._own_sends != []
        echoed = parse_inbound(_message(text="on it"))
        assert echoed is not None
        assert imc.is_own_echo(echoed) is True
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_bridge_proven_not_started_send_is_forgotten(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The bridge's own disposition is the authority when it gives one.
        imc = await _client(tmp_path)
        peers[0].replies["send"] = [
            RpcError(-32001, "not dispatched", {"retry_safe": True, "disposition": "not_started"})
        ]
        with pytest.raises(RpcError):
            await imc.send(OWNER, "on it")
        assert imc._own_sends == []
        await imc.close()

    @pytest.mark.asyncio
    async def test_an_exact_repeat_inside_the_window_is_suppressed_on_purpose(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The accepted cost, pinned so it is not "fixed" by accident. In an
        # ORDINARY chat the echo is dropped by is_from_me before the ledger is
        # consulted, so the record lives its full TTL and a user who sends the
        # agent's exact words back inside it is read as that echo.
        #
        # The obvious repair -- let the is_from_me copy consume the record -- is
        # what must NOT be done: in a self-chat that copy may arrive alongside the
        # unattributed echo, and spending the record on it reopens the loop. A
        # bounded silent drop is the deliberate trade against an unbounded one.
        imc = await _client(tmp_path)
        await imc.send(OWNER, "on it")
        attributed = parse_inbound(_message(text="on it", is_from_me=True))
        assert attributed is not None
        assert imc._own_sends != [], "the record must outlive the attributed copy"
        repeat = parse_inbound(_message(text="on it"))
        assert repeat is not None
        assert imc.is_own_echo(repeat) is True
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_slow_send_does_not_outlive_its_own_guard(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Third finding in this span, and the one the optional expiry exists for
        # (local GPT review of 8091b0e9). `_call` waits up to 30s and the TTL is
        # 30s, so with a pre-computed expiry a send that reached its timeout left
        # a record already eligible for pruning at the moment its echo arrived --
        # and the echo was answered, restoring the loop. An in-flight record has
        # no expiry at all, so there is nothing to prune.
        clock = [1000.0]
        monkeypatch.setattr(client_mod.time, "monotonic", lambda: clock[0])
        imc = await _client(tmp_path)
        verdicts: list[bool] = []

        async def slow_send(method: str, params: dict[str, Any], **_kw: Any) -> Any:
            if method == "send":
                # The call outlives the whole TTL, then the echo lands while it
                # is still awaiting a result.
                clock[0] += ECHO_TTL_S * 3
                mid_flight = parse_inbound(_message(text="on it"))
                assert mid_flight is not None
                verdicts.append(imc.is_own_echo(mid_flight))
            return {"ok": True}

        imc._call = slow_send  # type: ignore[method-assign]
        await imc.send(OWNER, "on it")
        assert verdicts == [True], "the guard expired while its own send was in flight"
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_clock_starts_when_the_send_resolves_not_when_it_began(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The corollary: a send that took a long time still gets a full window
        # afterwards, measured from its outcome rather than from its start.
        clock = [1000.0]
        monkeypatch.setattr(client_mod.time, "monotonic", lambda: clock[0])
        imc = await _client(tmp_path)

        async def slow_send(method: str, params: dict[str, Any], **_kw: Any) -> Any:
            if method == "send":
                clock[0] += ECHO_TTL_S * 3
            return {"ok": True}

        imc._call = slow_send  # type: ignore[method-assign]
        await imc.send(OWNER, "on it")
        clock[0] += ECHO_TTL_S - 1  # inside the window, counted from the outcome
        echoed = parse_inbound(_message(text="on it"))
        assert echoed is not None
        assert imc.is_own_echo(echoed) is True
        await imc.close()

    @pytest.mark.asyncio
    async def test_eviction_never_drops_an_unresolved_record(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # Fourth finding in this span (local GPT review of 1582c06f1), and the one
        # the two lanes disagreed on: eviction deleted from the front regardless of
        # state, so an unresolved record could be evicted and its echo answered --
        # while `is_own_echo` was carefully skipping exactly those records. The
        # input needs many sends outstanding at once, which this channel does not
        # do today, but the invariant is held rather than argued: unresolved
        # records are removed ONLY by their own resolution.
        imc = await _client(tmp_path)
        peers[0].default_result = {"ok": True}
        in_flight = imc._remember_own_send(_echo_key(OWNER, "unresolved reply"))
        assert in_flight.expires_at is None
        for i in range(ECHO_WINDOW + 20):
            await imc.send(OWNER, f"chunk {i}")
        assert in_flight in imc._own_sends, "the in-flight guard was evicted"
        echo = parse_inbound(_message(text="unresolved reply"))
        assert echo is not None
        assert imc.is_own_echo(echo) is True
        await imc.close()

    @pytest.mark.asyncio
    async def test_resolved_records_are_still_bounded(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The other half: exempting in-flight records must not stop the cap from
        # bounding the resolved ones, or a long conversation grows the list.
        imc = await _client(tmp_path)
        peers[0].default_result = {"ok": True}
        for i in range(ECHO_WINDOW + 40):
            await imc.send(OWNER, f"chunk {i}")
        assert len(imc._own_sends) <= ECHO_WINDOW
        await imc.close()

    @pytest.mark.asyncio
    async def test_every_suppression_is_logged_not_only_the_first(
        self, tmp_path: Path, peers: list[StubPeer], caplog: pytest.LogCaptureFixture
    ) -> None:
        # The drop is silent to the USER by construction -- replying would be the
        # loop -- so the log is the only signal it happened. Design Review flagged
        # that only the first suppression per handle was recorded, which leaves a
        # later drop indistinguishable from a message that never arrived.
        imc = await _client(tmp_path)
        peers[0].default_result = {"ok": True}
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.imessage.client"):
            for i in range(3):
                await imc.send(OWNER, "same words")
                echo = parse_inbound(_message(guid=f"E{i}", text="same words"))
                assert echo is not None
                assert imc.is_own_echo(echo) is True
        suppressed = [r for r in caplog.records if "echo" in r.getMessage()]
        assert len(suppressed) == 3, "a later suppression left no signal at all"
        # The loud one fires once; the rest stay at debug so a long conversation
        # does not write a warning per outbound message.
        assert sum(1 for r in suppressed if r.levelno == logging.WARNING) == 1
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_body_key_is_scoped_to_the_handle_it_was_sent_to(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        imc = await _client(tmp_path)
        await imc.send("+15559876543", "on it")
        from_someone_else = parse_inbound(_message(sender=OWNER, text="on it"))
        assert from_someone_else is not None
        assert imc.is_own_echo(from_someone_else) is False
        await imc.close()

    @pytest.mark.asyncio
    async def test_an_entry_older_than_the_ttl_no_longer_suppresses(
        self, tmp_path: Path, peers: list[StubPeer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The TTL is the exposure of the body key's false positive, so it has to
        # actually expire rather than pin the text forever.
        clock = [1000.0]
        monkeypatch.setattr(client_mod.time, "monotonic", lambda: clock[0])
        imc = await _client(tmp_path)
        await imc.send(OWNER, "on it")
        clock[0] += ECHO_TTL_S + 1
        stale = parse_inbound(_message(text="on it"))
        assert stale is not None
        assert imc.is_own_echo(stale) is False
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_composed_and_decomposed_body_compare_equal(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The body makes a round trip through Messages and back out of the
        # database; a normalization difference on one accented character would
        # silently reopen the loop.
        imc = await _client(tmp_path)
        await imc.send(OWNER, "caf\u00e9 ready")
        decomposed = parse_inbound(_message(text="cafe\u0301 ready"))
        assert decomposed is not None
        assert imc.is_own_echo(decomposed) is True
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_ledger_is_bounded(self, tmp_path: Path, peers: list[StubPeer]) -> None:
        # A long answer is delivered as many sends, so the ledger must not grow
        # with the conversation.
        imc = await _client(tmp_path)
        peers[0].default_result = {"ok": True}
        for i in range(ECHO_WINDOW + 40):
            await imc.send(OWNER, f"chunk {i}")
        assert len(imc._own_sends) <= ECHO_WINDOW
        await imc.close()

    @pytest.mark.asyncio
    async def test_the_body_is_remembered_before_the_send_returns(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The watch can emit the row for this message while `send` is still
        # awaiting its result, and an echo that arrives before it is remembered
        # is an echo that gets answered. Recording after the call would leave
        # that window open, so the test pins the ordering rather than the state.
        seen: list[bool] = []

        async def slow_send(method: str, params: dict[str, Any], **_kw: Any) -> Any:
            if method == "send":
                mid_flight = parse_inbound(_message(text="on it"))
                assert mid_flight is not None
                seen.append(imc.is_own_echo(mid_flight))
            return {"ok": True}

        imc = await _client(tmp_path)
        imc._call = slow_send  # type: ignore[method-assign]
        await imc.send(OWNER, "on it")
        assert seen == [True]
        await imc.close()

    @pytest.mark.asyncio
    async def test_a_self_chat_reply_does_not_come_back_as_a_new_turn(
        self, tmp_path: Path, peers: list[StubPeer]
    ) -> None:
        # The whole loop, end to end, through the real transport: the user's own
        # handle is the allowlist, the agent answers, and the answer arrives back
        # on the watch as an ordinary inbound row. Before the ledger this second
        # row started another turn, and that turn's reply started another.
        dispatched: list[Any] = []

        async def dispatch(inbound: Any) -> None:
            dispatched.append(inbound)

        imc = IMessageClient(cursor_path=tmp_path / "cursor.json")
        transport = IMessageTransport(imc, allowed_handles=[OWNER], dispatch=dispatch)
        imc.set_message_handler(transport.receive)
        await imc.start()

        await peers[0].notify(
            "message", {"subscription": 1, "message": _message(id=1, guid="IN", text="hi")}
        )
        assert len(dispatched) == 1

        peers[0].replies["send"] = [{"ok": True, "id": 2, "guid": "OUT"}]
        await transport.send_message(OWNER, "hello back")
        # The delivered copy: our own words, our own handle, and NOT attributed
        # to us by the platform.
        await peers[0].notify(
            "message",
            {
                "subscription": 1,
                "message": _message(id=3, guid="ECHO", text="hello back", is_from_me=False),
            },
        )
        assert len(dispatched) == 1, "the agent's own reply started another turn"
        await imc.close()


async def _until(predicate: object, timeout: float = 2.0) -> None:
    """Poll for a condition instead of sleeping a guessed interval."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met within the deadline")
