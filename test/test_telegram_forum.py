"""Telegram forum topics: per-topic sessions behind a fail-closed gate.

Covers the shared forum authorization predicate, the thread id the client captures from
an update, the gate on transport.receive and on ``TelegramDispatcher`` callbacks
(transport_dispatch.py), per-topic session keys, forum config, reply threading into the
topic and the queue drain under the forum key.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import FakeClient, _dispatcher, _origin

from kiro_crew.messaging.renderer import DONE, TEXT_CHUNK, OutputEvent, session_provenance_tag
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.telegram.client import TelegramClient, TelegramInbound
from kiro_crew.telegram.renderer import TelegramApprovalDecider, TelegramRenderer
from kiro_crew.telegram.transport import (
    TELEGRAM_CAPABILITIES,
    TelegramInboundMessage,
    TelegramTransport,
    forum_gate_outcome,
)


class TestForumGateOutcome:
    """Direct unit test of the shared fail-closed forum authZ predicate used by
    BOTH transport.receive and dispatcher.on_callback. One
    predicate → the two call sites can never drift. Only a real forum Topic
    (supergroup + message_thread_id) of an allow-listed chat is authorized;
    ordinary groups and the supergroup General chat (no thread) are DENIED."""

    _LISTED = -1001234567890

    def test_private_is_authorized(self) -> None:
        assert (
            forum_gate_outcome("private", 7, None, allow_forum=False, allowed_forum_chat_ids=[])
            is None
        )

    def test_allowlisted_supergroup_topic_is_authorized(self) -> None:
        # supergroup + a real Topic thread + allow-listed chat_id -> authorized.
        assert (
            forum_gate_outcome(
                "supergroup",
                self._LISTED,
                5,
                allow_forum=True,
                allowed_forum_chat_ids=[self._LISTED],
            )
            is None
        )

    def test_supergroup_topic_not_allowlisted_denied_forum(self) -> None:
        # Real Topic, but the supergroup's chat_id is NOT allow-listed.
        assert (
            forum_gate_outcome(
                "supergroup",
                self._LISTED,
                5,
                allow_forum=True,
                allowed_forum_chat_ids=[-1009999999999],
            )
            == "denied_forum_not_allowed"
        )

    def test_supergroup_general_no_thread_denied(self) -> None:
        # General chat (no message_thread_id) is NOT a Topic -> denied even when
        # allow_forum is on and the chat_id IS allow-listed. Fail closed.
        assert (
            forum_gate_outcome(
                "supergroup",
                self._LISTED,
                None,
                allow_forum=True,
                allowed_forum_chat_ids=[self._LISTED],
            )
            == "denied_non_private_chat"
        )

    def test_ordinary_group_denied_even_with_thread(self) -> None:
        # An ordinary group can't have Topics; chat_type "group" is denied even
        # if a (spurious) thread id and allow-listed chat_id are supplied.
        assert (
            forum_gate_outcome(
                "group",
                self._LISTED,
                5,
                allow_forum=True,
                allowed_forum_chat_ids=[self._LISTED],
            )
            == "denied_non_private_chat"
        )

    def test_channel_denied_non_private(self) -> None:
        # chat_type dominates: a channel is denied even with a thread + "listed" id.
        assert (
            forum_gate_outcome(
                "channel",
                -100777,
                5,
                allow_forum=True,
                allowed_forum_chat_ids=[-100777],
            )
            == "denied_non_private_chat"
        )


class TestForumClientCapture:
    """The raw ``message_thread_id`` on a supergroup update is normalized onto
    ``TelegramInbound`` so downstream routing can key on the Topic."""

    def _dispatch_and_capture(self, update: dict) -> list[TelegramInbound]:
        captured: list[TelegramInbound] = []

        async def _on(inb: TelegramInbound) -> None:
            captured.append(inb)

        async def _go() -> None:
            cli = TelegramClient(token="x", on_message=_on)
            cli._dispatch(update)
            # flake-ok: pre-existing 20 ms barrier that lets the dispatched handler task run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)  # let the created handler task run

        asyncio.run(_go())
        return captured

    def test_message_thread_id_captured_into_inbound(self) -> None:
        captured = self._dispatch_and_capture(
            {
                "message": {
                    "message_id": 9,
                    "text": "hi",
                    "message_thread_id": 5,
                    "chat": {"id": -1001234567890, "type": "supergroup"},
                    "from": {"id": 7},
                }
            }
        )
        assert len(captured) == 1
        assert captured[0].message_thread_id == 5
        assert captured[0].chat_type == "supergroup"

    def test_dm_update_has_no_thread_id(self) -> None:
        captured = self._dispatch_and_capture(
            {
                "message": {
                    "message_id": 1,
                    "text": "hi",
                    "chat": {"id": 7, "type": "private"},
                    "from": {"id": 7},
                }
            }
        )
        assert len(captured) == 1
        assert captured[0].message_thread_id is None


class TestForumTransportGate:
    """Forum gating lives in transport.receive(): allow_forum + chat_id list.
    Fail closed — a group/supergroup message is dropped unless BOTH hold."""

    def _run_receive(
        self,
        inbound: TelegramInbound,
        *,
        allow_forum: bool,
        allowed_forum_chat_ids: list[int],
    ) -> list[InboundMessage]:
        dispatched: list[InboundMessage] = []

        async def _dispatch(m: InboundMessage) -> None:
            dispatched.append(m)

        t = TelegramTransport(
            FakeClient(),  # type: ignore[arg-type]
            allowed_user_ids=[7],
            allow_forum=allow_forum,
            allowed_forum_chat_ids=allowed_forum_chat_ids,
            dispatch=_dispatch,
        )
        asyncio.run(t.receive(inbound))
        return dispatched

    def test_forum_allowed_dispatches_with_thread(self) -> None:
        inbound = TelegramInbound(
            chat_id=-1001234567890,
            user_id=7,
            text="hi",
            chat_type="supergroup",
            message_thread_id=5,
        )
        out = self._run_receive(inbound, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        assert len(out) == 1
        assert getattr(out[0], "chat_type", None) == "supergroup"
        assert out[0].thread_id == "5"  # Topic id rides the base thread_id
        assert out[0].conversation_id == "-1001234567890"
        assert out[0].user_id == "7"

    def test_forum_general_denied_no_thread(self) -> None:
        # General chat (no message_thread_id) is NOT a real Topic -> DENIED at
        # the gate even when allow_forum is on and the chat_id is allow-listed.
        inbound = TelegramInbound(
            chat_id=-1001234567890,
            user_id=7,
            text="hi",
            chat_type="supergroup",
            message_thread_id=None,
        )
        assert (
            self._run_receive(inbound, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
            == []
        )

    def test_forum_denied_when_allow_forum_false(self) -> None:
        inbound = TelegramInbound(
            chat_id=-1001234567890,
            user_id=7,
            text="hi",
            chat_type="supergroup",
            message_thread_id=5,
        )
        assert (
            self._run_receive(inbound, allow_forum=False, allowed_forum_chat_ids=[-1001234567890])
            == []
        )

    def test_forum_denied_when_chat_id_not_allowlisted(self) -> None:
        inbound = TelegramInbound(
            chat_id=-1001234567890,
            user_id=7,
            text="hi",
            chat_type="supergroup",
            message_thread_id=5,
        )
        assert (
            self._run_receive(inbound, allow_forum=True, allowed_forum_chat_ids=[-1009999999999])
            == []
        )


class TestForumDispatchRouting:
    """Per-topic session-key shape + generation isolation (dispatcher level)."""

    def _forum_msg(
        self, thread: str | None, *, chat_id: str = "-1001234567890", text: str = "hello"
    ) -> TelegramInboundMessage:
        return TelegramInboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id=chat_id,
            text=text,
            chat_type="supergroup",
            thread_id=thread,
            message_id=1,
        )

    def test_forum_topic_session_key(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(self._forum_msg("5")))
        assert sess.successes == ["telegram:kirocrew:forum:-1001234567890:5"]

    # NB: there is deliberately NO "General session key" routing test — a
    # threadless supergroup (General) message is denied at the forum gate
    # (see TestForumGateOutcome.test_supergroup_general_no_thread_denied and
    # TestForumTransportGate.test_forum_general_denied_no_thread) and never
    # reaches handle_message, so no General route is ever served.

    def test_private_dm_key_unchanged_regression(self) -> None:
        # HARD INVARIANT: the private-DM key is byte-for-byte unchanged.
        d, cli, sess = _dispatcher({7})
        asyncio.run(
            d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )
        )
        assert sess.successes == ["telegram:kirocrew:direct:7"]

    def test_new_in_topic_isolates_generation(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(self._forum_msg("5", text="/new")))
        # Only THIS topic's generation advanced …
        assert d._conv.current_gen(("forum", "-1001234567890:5")) == 1
        # … a sibling topic and the DM are untouched.
        assert d._conv.current_gen(("forum", "-1001234567890:6")) == 0
        assert d._conv.current_gen(("direct", "7")) == 0

    def test_forum_outbound_send_threads_edit_does_not(self) -> None:
        # A forum renderer threads EVERY send into the Topic; edits carry NO
        # thread (FakeClient.edit_message has no message_thread_id param, so the
        # run completing at all proves edits are unthreaded).
        cli = FakeClient()
        r = TelegramRenderer(
            cli,
            -1001234567890,
            TELEGRAM_CAPABILITIES,  # type: ignore[arg-type]
            session_key="telegram:kirocrew:forum:-1001234567890:5",
            message_thread_id=5,
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hello topic"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        assert cli.send_threads  # at least one send happened
        assert all(tid == 5 for tid in cli.send_threads)  # every send threaded
        assert cli.edits  # the seal edited in place (unthreaded)


class TestForumConfig:
    @staticmethod
    def _load(data: dict) -> Any:
        """Load a KiroCrewConfig from an in-memory dict via a temp config file
        (mirrors the canonical loader entrypoint used across the config tests)."""
        import json
        import tempfile
        import unittest.mock
        from pathlib import Path

        from kiro_crew.config.loader import KiroCrewConfig

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                return KiroCrewConfig.load()
        finally:
            tmp.unlink(missing_ok=True)

    def test_forum_fields_default_closed(self) -> None:
        cfg = self._load({})
        assert cfg.telegram.allow_forum is False
        assert cfg.telegram.allowed_forum_chat_ids == []

    def test_forum_fields_parse_and_serialize(self) -> None:
        cfg = self._load(
            {
                "telegram": {
                    "allow_forum": True,
                    "allowed_forum_chat_ids": [-1001, "-1002", "bad", 1.5],
                }
            }
        )
        assert cfg.telegram.allow_forum is True
        # _coerce_int_ids keeps clean base-10 ints, drops the rest (fail closed).
        assert cfg.telegram.allowed_forum_chat_ids == [-1001, -1002]
        out = cfg.to_dict()  # asdict round-trips the new fields
        assert out["telegram"]["allow_forum"] is True
        assert out["telegram"]["allowed_forum_chat_ids"] == [-1001, -1002]


class TestForumReplyThreading:
    """Fix A: every dispatcher-originated send in a forum turn threads back into
    the user's Topic (message_thread_id=<topic>), never the supergroup General."""

    @staticmethod
    def _forum_msg(text: str, thread: str | None = "5") -> TelegramInboundMessage:
        return TelegramInboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id="-1001234567890",
            text=text,
            chat_type="supergroup",
            thread_id=thread,
            message_id=1,
        )

    def test_new_confirmation_threads_into_topic(self) -> None:
        d, cli, _ = _dispatcher({7})
        asyncio.run(d.handle_message(self._forum_msg("/new")))
        assert any("New conversation" in t for t, _ in cli.sent)
        # The confirmation landed IN Topic 5, not the supergroup's General.
        assert cli.send_threads == [5]

    def test_compact_status_threads_into_topic(self) -> None:
        d, cli, _ = _dispatcher({7})
        asyncio.run(d.handle_message(self._forum_msg("/compact")))
        # The "Compacting…" status message threads into the Topic.
        assert cli.send_threads == [5]
        assert any("Compact" in t for t, _ in cli.sent)

    def test_dm_confirmation_unthreaded_regression(self) -> None:
        # HARD INVARIANT: a private-DM /new reply carries NO message_thread_id.
        d, cli, _ = _dispatcher({7})
        asyncio.run(
            d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/new",
                )
            )
        )
        assert cli.send_threads == [None]


class TestForumQueueDrain:
    """Fix B: a message queued mid-turn in a forum Topic drains under the FORUM
    session key, not the DM key."""

    def test_the_surface_reports_whether_the_edit_landed(self) -> None:
        """A rate-limited chat answers a refusal rather than raising, and the registry
        can only keep that transition retryable if the wrapper reports it."""
        d, cli, _sess = _dispatcher({7})
        surface = d._receipt_surface(7, None)

        async def go() -> tuple[bool, bool]:
            cli.edit_ok = True
            ok = await surface.edit_receipt(11, "body")
            cli.edit_ok = False
            refused = await surface.edit_receipt(11, "body")
            return ok, refused

        ok, refused = asyncio.run(go())
        assert ok is True
        assert refused is False

    def test_queued_forum_message_drains_under_forum_key(self) -> None:
        d, cli, sess = _dispatcher({7})
        forum_key = "telegram:kirocrew:forum:-1001234567890:5"
        # Simulate one message queued mid-turn for this Topic. The Topic rides on the
        # ENTRY now, not on the drain call: the replay envelope comes from the queued
        # message's own origin.
        sess.queued.append(
            (
                "t0",
                "queued in the topic",
                _origin(7, -1001234567890, thread="5", chat_type="supergroup"),
            )
        )
        asyncio.run(d._drain_queue(forum_key))
        # The drained turn resolved to the FORUM key (carried via chat_type +
        # thread on the synthetic message), NOT the DM key.
        assert sess.successes == [forum_key]
        assert "telegram:kirocrew:direct:7" not in sess.successes

    def test_dm_queue_drains_under_dm_key_regression(self) -> None:
        # HARD INVARIANT: a DM drain still resolves to the DM key.
        d, cli, sess = _dispatcher({7})
        sess.queued.append(("t0", "queued dm", _origin()))
        asyncio.run(d._drain_queue("telegram:kirocrew:direct:7"))
        assert sess.successes == ["telegram:kirocrew:direct:7"]


class TestForumCallbackGate:
    """Fix C: forum callbacks are honored ONLY when the same gate
    transport.receive enforces passes (allow_forum AND chat_id allow-listed).
    Fail-closed authZ boundary — never open a callback from a non-allow-listed
    group. DM callbacks are unchanged (covered by TestDispatcher)."""

    @staticmethod
    def _opt_cb(tag: str = "") -> Any:
        data = f"opt:0:{tag}" if tag else "opt:0"
        return SimpleNamespace(
            callback_query_id="qf",
            user_id=7,
            chat_id=-1001234567890,
            message_id=50,
            data=data,
            label="Say Hi",
            chat_type="supergroup",
            message_thread_id=5,
        )

    def test_forum_callback_processed_when_allowlisted(self) -> None:
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        tag = session_provenance_tag(d._session_key(("forum", "-1001234567890:5")))
        asyncio.run(d.on_callback(self._opt_cb(tag)))  # type: ignore[arg-type]
        # Acked, and the [OPTIONS:] choice re-dispatched under the FORUM key.
        assert cli.answered == ["qf"]
        assert sess.successes == ["telegram:kirocrew:forum:-1001234567890:5"]
        # Every callback-originated send threaded back into the Topic.
        assert cli.send_threads and all(t == 5 for t in cli.send_threads)

    def test_forum_callback_denied_when_chat_id_not_allowlisted(self) -> None:
        # allow_forum on, but the supergroup's chat_id is NOT allow-listed.
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1009999999999])
        asyncio.run(d.on_callback(self._opt_cb()))  # type: ignore[arg-type]
        # Fail closed: not even acked, no keyboard retire, no re-dispatch.
        assert cli.answered == []
        assert cli.markup_edits == []
        assert sess.successes == []

    def test_forum_callback_general_no_thread_denied(self) -> None:
        # A press from the supergroup General chat (no message_thread_id) is NOT
        # a real Topic -> DENIED even when allow_forum is on and the chat_id IS
        # allow-listed. Mirrors the receive() gate exactly (fail closed).
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        cb = SimpleNamespace(
            callback_query_id="qg",
            user_id=7,
            chat_id=-1001234567890,
            message_id=51,
            data="opt:0",
            label="Say Hi",
            chat_type="supergroup",
            message_thread_id=None,
        )
        asyncio.run(d.on_callback(cb))  # type: ignore[arg-type]
        assert cli.answered == []
        assert cli.markup_edits == []
        assert sess.successes == []

    def test_forum_callback_approval_resolves_only_when_allowlisted(self) -> None:
        # Allow-listed: the approval decision resolves under the forum key.
        d, cli, _ = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(("forum", "-1001234567890:5")), "rqF")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            TelegramApprovalDecider.arm(key, "n1")
            try:
                cb = SimpleNamespace(
                    callback_query_id="qF",
                    user_id=7,
                    chat_id=-1001234567890,
                    message_id=60,
                    data="a:rqF:n1:1",
                    label="",
                    chat_type="supergroup",
                    message_thread_id=5,
                )
                await d.on_callback(cb)  # type: ignore[arg-type]
                return fut.done() and fut.result() is True
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)

        assert asyncio.run(_go()) is True

    def test_forum_callback_not_resolved_when_allow_forum_false(self) -> None:
        # allow_forum OFF -> the identical approval press must NOT resolve the
        # decider (fail closed) and must not even ack.
        d, cli, _ = _dispatcher({7}, allow_forum=False, allowed_forum_chat_ids=[-1001234567890])

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(("forum", "-1001234567890:5")), "rqF")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            # The nonce the renderer would have minted for this prompt; the press
            # must carry it or resolution refuses it as stale.
            nonce = "n1"
            TelegramApprovalDecider.arm(key, nonce)
            try:
                cb = SimpleNamespace(
                    callback_query_id="qF",
                    user_id=7,
                    chat_id=-1001234567890,
                    message_id=61,
                    data=f"a:rqF:{nonce}:1",
                    label="",
                    chat_type="supergroup",
                    message_thread_id=5,
                )
                await d.on_callback(cb)  # type: ignore[arg-type]
                return fut.done()
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)

        assert asyncio.run(_go()) is False
        assert cli.answered == []
