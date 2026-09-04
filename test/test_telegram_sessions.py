from __future__ import annotations

import asyncio
import importlib
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state
from test_telegram import FakeClient, FakeCtx, FakeProvider

from kiro_crew.history import ConversationLog
from kiro_crew.messaging import auto_title
from kiro_crew.messaging.link import UNBIND_REASON_UNSPECIFIED, ChannelLink
from kiro_crew.messaging.renderer import session_provenance_tag
from kiro_crew.messaging.session_resume import RoutingDecision
from kiro_crew.messaging.session_trust import clear_trusted_sessions
from kiro_crew.session import _opt_out_key
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.telegram.renderer import TelegramApprovalDecider
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES, TelegramInboundMessage
from kiro_crew.telegram.transport_dispatch import TelegramDispatcher


@pytest.fixture(autouse=True)
def _reset_shared_trust(monkeypatch: Any) -> Any:
    clear_trusted_sessions()
    monkeypatch.setattr(auto_title, "try_claim", lambda _key: False)
    yield
    clear_trusted_sessions()


class _Client(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.send_fails = False
        self.edit_fails = False

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> int | None:
        if self.send_fails:
            return None
        return await super().send_message(chat_id, text, **kwargs)

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: Any = None,
        reply_markup: Any = None,
        retry_plain: bool = True,
    ) -> bool:
        self.edits.append((message_id, text, reply_markup))
        return not self.edit_fails


class _Sessions:
    def __init__(self) -> None:
        self.provider = FakeProvider()
        self.provider.cwd = "/workspace"
        self.last_key = ""
        self.last_agent: str | None = None
        self.last_model: str | None = None
        self.is_new_result = False
        self.resumed_result = True
        self.closing = False
        self.busy = False
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.released: list[str] = []
        self.set_channel_calls: list[tuple[str, str]] = []
        self.origin_links: dict[str, ChannelLink] = {}
        self.mirror_links: dict[str, ChannelLink] = {}
        self.inbound_keys: set[str] = set()
        self.mirror_opt_outs: set[str] = set()
        self.queued: list[tuple[str, str, dict[str, Any]]] = []
        self.flushed: list[dict[str, ChannelLink]] = []
        self.flush_error: Exception | None = None
        self.batch_failures = 0
        self.approval_policies: list[tuple[str, str]] = []

    async def get_or_create(
        self,
        key: str,
        *,
        agent: str | None = None,
        channel_id: str | None = None,
        model: str | None = None,
    ) -> tuple[FakeProvider, bool, bool]:
        self.last_key = key
        self.last_agent = agent
        self.last_model = model
        return self.provider, self.is_new_result, self.resumed_result

    def begin_turn(self, key: str) -> None:
        if self.closing:
            raise SessionClosingError("closing")

    async def set_channel(self, key: str, channel: str) -> None:
        self.set_channel_calls.append((key, channel))

    def set_origin_link(self, key: str, link: ChannelLink) -> None:
        self.origin_links[key] = link

    def get_origin_link(self, key: str) -> ChannelLink | None:
        return self.origin_links.get(key)

    def set_mirror_link(
        self,
        key: str,
        link: ChannelLink,
        *,
        accepts_inbound: bool = False,
        reason: str = UNBIND_REASON_UNSPECIFIED,
    ) -> None:
        rivals = [
            other
            for other, candidate in self.mirror_links.items()
            if other != key and candidate == link
        ]
        if rivals and (accepts_inbound or any(other in self.inbound_keys for other in rivals)):
            raise ConversationOwnershipConflict("conversation already held")
        self.mirror_links[key] = link
        if accepts_inbound:
            self.inbound_keys.add(key)
        else:
            self.inbound_keys.discard(key)

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        return self.mirror_links.get(key)

    def find_mirror_sessions(self, link: ChannelLink, *, inbound_only: bool = False) -> list[str]:
        return [
            key
            for key, candidate in self.mirror_links.items()
            if candidate == link and (not inbound_only or key in self.inbound_keys)
        ]

    def clear_mirror_link(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> bool:
        self.inbound_keys.discard(key)
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(
        self, link: ChannelLink, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> list[str]:
        cleared = self.find_mirror_sessions(link)
        for key in cleared:
            self.inbound_keys.discard(key)
            self.mirror_links.pop(key, None)
        return cleared

    async def aflush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error
        self.flushed.append(dict(self.mirror_links))

    @contextmanager
    def batched_save(self) -> Any:
        yield
        if self.batch_failures:
            self.batch_failures -= 1
            raise OSError("simulated batch write failure")

    def set_mirror_opt_out(self, key: str, opted_out: bool) -> None:
        if opted_out:
            self.mirror_opt_outs.add(_opt_out_key(key))
        else:
            self.mirror_opt_outs.discard(_opt_out_key(key))

    def mirror_opt_out(self, key: str) -> bool:
        return _opt_out_key(key) in self.mirror_opt_outs

    def max_generation(self, bucket: str) -> int:
        return -1

    def channel_key_for_stem(self, stem: str) -> str:
        return ""

    def is_busy(self, key: str) -> bool:
        return self.busy

    def has_session(self, key: str) -> bool:
        return True

    def get_provider(self, key: str) -> FakeProvider:
        return self.provider

    def get_pid(self, key: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        self.successes.append(key)

    async def record_failure(self, key: str) -> None:
        self.failures.append(key)

    def release(self, key: str) -> None:
        self.released.append(key)

    def check_context_usage(self, key: str, provider: Any) -> float:
        return 0.0

    def enqueue(self, key: str, ts: str, text: str, *, force: bool = False, **kwargs: Any) -> bool:
        if force or self.busy:
            self.queued.append((ts, text, kwargs))
            return True
        return False

    def dequeue(self, key: str) -> tuple[str, str, dict[str, Any]] | None:
        return self.queued.pop(0) if self.queued else None

    def clear_queue(self, key: str) -> None:
        self.queued.clear()

    async def try_acquire(self, key: str) -> bool:
        return not self.busy

    async def discard_conversation(self, key: str) -> None:
        return None

    def set_approval_policy(self, key: str, policy: str) -> None:
        self.approval_policies.append((key, policy))


def _config() -> Any:
    return SimpleNamespace(
        telegram=SimpleNamespace(
            soft_threshold_pct=80,
            allow_forum=True,
            allowed_forum_chat_ids=[-100],
            show_thinking=False,
            voice_replies=False,
            forum_activation="always",
        ),
        dashboard=SimpleNamespace(restore_window_minutes=30, surface_channel_sessions=True),
        agent=SimpleNamespace(default_agent="kirocrew"),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
        raw={},
    )


def _log(tmp_path: Any, *, agent: str = "") -> ConversationLog:
    from kiro_crew.history import allow_on_loop_persist

    log = ConversationLog(base_dir=tmp_path / "sessions")
    with allow_on_loop_persist():
        log.append("dashboard:chat-1", "assistant", "prior work")
        log.set_title("dashboard:chat-1", "Launch plan")
        if agent:
            log.update_metadata("dashboard:chat-1", {"agent": agent})
    return log


def _dispatcher(
    tmp_path: Any,
    *,
    allowed: set[int] | None = None,
    log: ConversationLog | None = None,
) -> tuple[TelegramDispatcher, _Client, _Sessions, ConversationLog]:
    sessions = _Sessions()
    conv_log = log or _log(tmp_path)
    dispatcher = TelegramDispatcher(
        sessions=sessions,  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=_config(),
        allowed_user_ids=allowed if allowed is not None else {7},
        conv_log=conv_log,
    )
    client = _Client()
    dispatcher.client = client  # type: ignore[assignment]
    return dispatcher, client, sessions, conv_log


def _dm(text: str, *, chat_id: int = 7, user_id: int = 7) -> TelegramInboundMessage:
    return TelegramInboundMessage(
        channel_type="telegram",
        user_id=str(user_id),
        conversation_id=str(chat_id),
        text=text,
        chat_type="private",
    )


def _topic(text: str, thread: int) -> TelegramInboundMessage:
    return TelegramInboundMessage(
        channel_type="telegram",
        user_id="7",
        conversation_id="-100",
        text=text,
        chat_type="supergroup",
        thread_id=str(thread),
    )


def _callback(data: str, *, message_id: int = 101, label: str = "") -> Any:
    return SimpleNamespace(
        callback_query_id="q1",
        user_id=7,
        chat_id=7,
        message_id=message_id,
        data=data,
        label=label,
        chat_type="private",
        message_thread_id=None,
    )


def _picker_button(client: _Client) -> tuple[str, int]:
    _text, markup = client.sent[-1]
    button = markup["inline_keyboard"][0][0]
    return str(button["callback_data"]), client._mid


def _resume_module() -> Any:
    return importlib.import_module("kiro_crew.telegram.session_resume")


async def _bind(
    dispatcher: TelegramDispatcher,
    client: _Client,
) -> None:
    await dispatcher.handle_message(_dm("/sessions"))
    data, message_id = _picker_button(client)
    await dispatcher.on_callback(_callback(data, message_id=message_id))


class TestTelegramSessionPicker:
    @pytest.mark.asyncio
    async def test_keyboard_payload_owner_message_index_and_double_press(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)

        await dispatcher.handle_message(_dm("/sessions"))

        data, message_id = _picker_button(client)
        markup = client.sent[-1][1]
        assert data.startswith("s:")
        assert data.isascii() and len(data.encode("ascii")) <= 64
        assert all(len(row) == 1 for row in markup["inline_keyboard"])

        await dispatcher.on_callback(_callback(data, message_id=message_id + 1))
        assert sessions.mirror_links == {}
        await dispatcher.on_callback(_callback(data.replace(":0", ":99"), message_id=message_id))
        assert sessions.mirror_links == {}
        await dispatcher.on_callback(_callback(data, message_id=message_id))
        assert sessions.find_mirror_sessions(
            ChannelLink("telegram", channel_id="7"), inbound_only=True
        ) == ["dashboard:chat-1"]
        before = dict(sessions.mirror_links)
        await dispatcher.on_callback(_callback(data, message_id=message_id))
        assert sessions.mirror_links == before
        assert "expired" in client.edits[-1][1].lower()

    @pytest.mark.asyncio
    async def test_failed_post_registers_no_picker(self, tmp_path: Any) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        client.send_fails = True

        await dispatcher.handle_message(_dm("/sessions"))

        assert len(dispatcher._session_resume.pickers) == 0

    @pytest.mark.parametrize(
        "message",
        [
            pytest.param(_dm("/sessions", chat_id=8), id="private-chat-not-user"),
            pytest.param(_topic("/sessions", 11), id="forum-topic"),
        ],
    )
    @pytest.mark.asyncio
    async def test_owner_gate_runs_before_history_read(
        self, tmp_path: Any, message: TelegramInboundMessage, monkeypatch: Any
    ) -> None:
        log = _log(tmp_path)
        reads = 0

        def _read() -> list[dict[str, Any]]:
            nonlocal reads
            reads += 1
            return []

        monkeypatch.setattr(log, "list_sessions", _read)
        dispatcher, _, _, _ = _dispatcher(tmp_path, log=log)

        await dispatcher.handle_message(message)

        assert reads == 0

    @pytest.mark.asyncio
    async def test_multiple_allowed_ids_are_not_an_owner(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        log = _log(tmp_path)
        read = AsyncMock()
        monkeypatch.setattr(log, "list_sessions", read)
        dispatcher, client, _, _ = _dispatcher(tmp_path, allowed={7, 8}, log=log)

        await dispatcher.handle_message(_dm("/sessions"))

        assert read.call_count == 0
        assert "single" in client.sent[-1][0] or "exactly one" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_expectation_is_durable_before_success_and_no_history_is_replayed(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        await dispatcher.handle_message(_dm("/sessions"))
        data, message_id = _picker_button(client)
        sent_before = len(client.sent)
        seen: list[Any] = []
        real_edit = client.edit_message

        async def _observe(*args: Any, **kwargs: Any) -> bool:
            if "Resumed" in str(args[2]):
                seen.append(await dispatcher._session_resume.expectations.get("chat:7"))
            return await real_edit(*args, **kwargs)

        client.edit_message = _observe  # type: ignore[method-assign]
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert seen and seen[0].key == "dashboard:chat-1"
        assert len(client.sent) == sent_before, "Telegram resume must not replay transcript rows"
        assert client.edits[-1][0] == message_id
        assert client.edits[-1][2] == {"inline_keyboard": []}
        assert sessions.last_key == ""

    def test_expectation_and_link_identity_include_the_full_topic(self, tmp_path: Any) -> None:
        mod = _resume_module()
        resume = mod.TelegramSessionResume(_Sessions(), _log(tmp_path), {7})

        assert resume.expectation_id(7, None) == "chat:7"
        assert resume.expectation_id(-100, 11) == "topic:-100:11"
        assert resume.expectation_id(-100, 12) == "topic:-100:12"
        assert resume.link_for(-100, 11) == ChannelLink(
            "telegram", channel_id="-100", thread_id="11"
        )

    @pytest.mark.asyncio
    async def test_pick_replaces_outbound_native_mirror_in_one_click(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert native_key not in sessions.mirror_links
        assert sessions.find_mirror_sessions(link, inbound_only=True) == ["dashboard:chat-1"]
        assert "Resumed" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_pick_never_displaces_deliberate_dashboard_mirror(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        deliberate_key = "dashboard:deliberate"
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)
        sessions.set_mirror_link(deliberate_key, link, accepts_inbound=False)

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert sessions.mirror_links == {
            native_key: link,
            deliberate_key: link,
        }
        assert sessions.inbound_keys == set()
        assert "already attached" in client.edits[-1][1]

    @pytest.mark.asyncio
    async def test_failed_takeover_restores_outbound_native_mirror(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)
        real_set = sessions.set_mirror_link

        def _fail_selected(
            key: str,
            candidate: ChannelLink,
            *,
            accepts_inbound: bool = False,
            reason: str = UNBIND_REASON_UNSPECIFIED,
        ) -> None:
            if key == "dashboard:chat-1" and accepts_inbound:
                raise OSError("simulated bind failure")
            real_set(
                key,
                candidate,
                accepts_inbound=accepts_inbound,
                reason=reason,
            )

        sessions.set_mirror_link = _fail_selected  # type: ignore[method-assign]
        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert sessions.mirror_links == {native_key: link}
        assert sessions.inbound_keys == set()
        assert "Couldn't resume" in client.edits[-1][1]
        expectation = await dispatcher._session_resume.expectations.get("chat:7")
        assert expectation is not None and expectation.retired

        sent_before = len(client.sent)
        await dispatcher.handle_message(_dm("continue natively"))
        assert sessions.last_key == native_key
        assert not any("NOT processed" in text for text, _ in client.sent[sent_before:])

    @pytest.mark.asyncio
    async def test_batch_write_failure_rolls_back_claim_and_expectation(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.set_mirror_link(native_key, link, accepts_inbound=False)
        sessions.batch_failures = 1

        await dispatcher.handle_message(_dm("/session launch"))
        data, message_id = _picker_button(client)
        await dispatcher.on_callback(_callback(data, message_id=message_id))

        assert sessions.mirror_links == {native_key: link}
        assert sessions.inbound_keys == set()
        expectation = await dispatcher._session_resume.expectations.get("chat:7")
        assert expectation is not None and expectation.retired
        assert "Couldn't resume" in client.edits[-1][1]


class TestTelegramInboundResumeRouting:
    @pytest.mark.asyncio
    async def test_ordinary_message_takes_one_routing_decision(self, tmp_path: Any) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        await dispatcher.handle_message(_dm("continue here"))

        dispatcher._session_resume.route.assert_awaited_once_with(7, 7, "private", None)
        assert sessions.last_key == "dashboard:chat-1"

    @pytest.mark.asyncio
    async def test_multi_user_binding_is_refused_before_it_can_route(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path, allowed={7, 8})
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        await dispatcher.handle_message(_dm("must stay private"))

        assert sessions.last_key == ""
        assert any("exactly one" in text for text, _ in client.sent)
        assert any("NOT processed" in text for text, _ in client.sent)

    @pytest.mark.parametrize(
        ("command", "handler", "target_kw"),
        [
            ("/compact", "_handle_compact", "session_key"),
            ("/stop", "_handle_stop", "session_key"),
            ("/model", "_handle_model", "session_key"),
            ("/title renamed", "_handle_title", "session_key"),
            ("/spawn do work", "_handle_spawn", "session_key"),
            ("/task run spec.md", "_handle_task", "session_key"),
            ("/link", "_handle_link", "resumed_key"),
        ],
    )
    @pytest.mark.asyncio
    async def test_session_scoped_commands_receive_resumed_key(
        self, tmp_path: Any, command: str, handler: str, target_kw: str
    ) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        mocked = AsyncMock()
        setattr(dispatcher, handler, mocked)

        await dispatcher.handle_message(_dm(command))

        mocked.assert_awaited_once()
        assert mocked.await_args.kwargs[target_kw] == "dashboard:chat-1"

    @pytest.mark.parametrize("command", ["/new", "/unlink"])
    @pytest.mark.asyncio
    async def test_recovery_command_releases_binding_and_next_message_runs_once(
        self, tmp_path: Any, command: str
    ) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        await _bind(dispatcher, client)
        link = dispatcher._session_resume.link_for(7, None)
        assert sessions.find_mirror_sessions(link, inbound_only=True) == ["dashboard:chat-1"]

        await dispatcher.handle_message(_dm(command))

        assert sessions.find_mirror_sessions(link, inbound_only=True) == []
        assert any("resumed session" in text for text, _ in client.sent)
        sent_before = len(client.sent)
        await dispatcher.handle_message(_dm("continue natively"))
        assert sessions.last_key != "dashboard:chat-1"
        assert len(client.sent) > sent_before
        assert not any("NOT processed" in text for text, _ in client.sent[sent_before:])

    @pytest.mark.parametrize("command", ["/help", "/status", "/new", "/unlink"])
    @pytest.mark.asyncio
    async def test_commands_remain_native_and_recovery_commands_bypass_refusals(
        self, tmp_path: Any, command: str
    ) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(refusal="must not run")
        )

        await dispatcher.handle_message(_dm(command))

        dispatcher._session_resume.route.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_refusal_delivery_leaves_settlement_owed(self, tmp_path: Any) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        decision = RoutingDecision(refusal="Detached")
        dispatcher._session_resume.route = AsyncMock(return_value=decision)
        dispatcher._session_resume.settle = AsyncMock()
        client.send_fails = True

        await dispatcher.handle_message(_dm("continue"))

        dispatcher._session_resume.settle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_refusal_delivery_settles(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        decision = RoutingDecision(refusal="Detached")
        dispatcher._session_resume.route = AsyncMock(return_value=decision)
        dispatcher._session_resume.settle = AsyncMock()

        await dispatcher.handle_message(_dm("continue"))

        dispatcher._session_resume.settle.assert_awaited_once_with(7, None, decision)

    @pytest.mark.asyncio
    async def test_topic_routing_locks_are_isolated(self, tmp_path: Any) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        topic_12_routed = asyncio.Event()
        crossed: list[bool] = []

        async def _route(
            user_id: int,
            chat_id: int,
            chat_type: str,
            thread: int | None,
        ) -> RoutingDecision:
            assert user_id == 7 and chat_type == "supergroup"
            if thread == 12:
                topic_12_routed.set()
            return RoutingDecision(refusal=f"Detached {thread}")

        real_send = client.send_message

        async def _send(chat_id: int, text: str, **kwargs: Any) -> int | None:
            if text == "Detached 11":
                try:
                    await asyncio.wait_for(topic_12_routed.wait(), timeout=1)
                    crossed.append(True)
                except asyncio.TimeoutError:
                    crossed.append(False)
            return await real_send(chat_id, text, **kwargs)

        dispatcher._session_resume.route = _route  # type: ignore[method-assign]
        client.send_message = _send  # type: ignore[method-assign]

        await asyncio.gather(
            dispatcher.handle_message(_topic("one", 11)),
            dispatcher.handle_message(_topic("two", 12)),
        )

        assert crossed == [True]

    @pytest.mark.asyncio
    async def test_same_route_waiters_settle_only_after_the_last_refusal(
        self, tmp_path: Any
    ) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        decision = RoutingDecision(refusal="Detached")
        dispatcher._session_resume.route = AsyncMock(return_value=decision)
        settlements: list[int] = []

        async def _settle(*args: Any) -> None:
            settlements.append(1)

        dispatcher._session_resume.settle = _settle  # type: ignore[method-assign]
        real_send = client.send_message
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def _send(chat_id: int, text: str, **kwargs: Any) -> int | None:
            if text == "Detached" and not first_started.is_set():
                first_started.set()
                await asyncio.wait_for(second_started.wait(), timeout=1)
            return await real_send(chat_id, text, **kwargs)

        client.send_message = _send  # type: ignore[method-assign]
        first = asyncio.create_task(dispatcher.handle_message(_dm("first")))
        await first_started.wait()
        second = asyncio.create_task(dispatcher.handle_message(_dm("second")))
        while len(dispatcher._routing_locks["chat:7"][1]) < 2:
            await asyncio.sleep(0)
        second_started.set()
        await asyncio.gather(first, second)

        assert settlements == [1]

    @pytest.mark.asyncio
    async def test_busy_resumed_session_refuses_without_queue_or_steer(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        sessions.busy = True

        await dispatcher.handle_message(_dm("continue"))

        assert any("busy" in text.lower() for text, _ in client.sent)
        assert sessions.queued == []
        assert sessions.provider.steered == []
        assert sessions.last_key == ""

    @pytest.mark.asyncio
    async def test_queue_drain_keeps_native_affinity_after_a_later_bind(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        native_key = dispatcher._session_key(("direct", "7"))
        sessions.queued.append(("1", "queued text", {}))
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        await dispatcher._drain_queue(native_key, 7, 7)

        dispatcher._session_resume.route.assert_not_awaited()
        assert sessions.last_key == native_key

    @pytest.mark.asyncio
    async def test_option_redispatch_routes_once_to_the_retained_resumed_key(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        tag = session_provenance_tag("dashboard:chat-1")

        await dispatcher.on_callback(_callback(f"opt:0:{tag}", label="Ship it"))

        dispatcher._session_resume.route.assert_awaited_once_with(7, 7, "private", None)
        assert sessions.last_key == "dashboard:chat-1"


class TestTelegramModelPickerRouting:
    @pytest.mark.asyncio
    async def test_unlink_makes_resumed_model_picker_stale(self, tmp_path: Any) -> None:
        dispatcher, client, sessions, _ = _dispatcher(tmp_path)
        sessions.provider.advertised = [{"modelId": "gpt-test", "name": "Test model"}]
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        await dispatcher.handle_message(_dm("/model"))
        model_message_id = client._mid
        await dispatcher.handle_message(_dm("/unlink"))
        await dispatcher.on_callback(_callback("m:1", message_id=model_message_id))

        assert sessions.provider.set_models == []
        assert "no longer controls" in client.edits[-1][1]


class TestTelegramApprovalResumeRouting:
    async def _arm(self, key: str, request_id: str = "rq1") -> asyncio.Future[bool]:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        token = TelegramApprovalDecider.key(key, request_id)
        TelegramApprovalDecider._REGISTRY[token] = future
        TelegramApprovalDecider.arm(token, "nonce")
        return future

    @pytest.mark.asyncio
    async def test_approval_uses_the_current_resumed_key(self, tmp_path: Any) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        future = await self._arm("dashboard:chat-1")
        try:
            await dispatcher.on_callback(_callback("a:rq1:nonce:1"))
            assert future.done() and future.result() is True
        finally:
            TelegramApprovalDecider._REGISTRY.clear()

    @pytest.mark.asyncio
    async def test_moved_binding_expires_old_trust_nonce_without_mutation(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        old = await self._arm("dashboard:chat-1")
        sessions.clear_mirror_link("dashboard:chat-1")
        sessions.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)
        try:
            await dispatcher.on_callback(_callback("a:rq1:nonce:t"))
            assert not old.done()
            assert sessions.approval_policies == []
        finally:
            TelegramApprovalDecider._REGISTRY.clear()

    @pytest.mark.asyncio
    async def test_ambiguous_binding_never_falls_back_to_native_approval(
        self, tmp_path: Any
    ) -> None:
        dispatcher, _, sessions, _ = _dispatcher(tmp_path)
        link = dispatcher._session_resume.link_for(7, None)
        for key in ("dashboard:chat-1", "dashboard:chat-2"):
            sessions.mirror_links[key] = link
            sessions.inbound_keys.add(key)
        native = dispatcher._session_key(("direct", "7"))
        future = await self._arm(native)
        try:
            await dispatcher.on_callback(_callback("a:rq1:nonce:1"))
            assert not future.done()
        finally:
            TelegramApprovalDecider._REGISTRY.clear()


class TestTelegramColdResume:
    @pytest.mark.parametrize(
        "recorded, expected",
        [
            pytest.param("research-agent", "research-agent", id="recorded"),
            pytest.param("default", "kirocrew", id="default-sentinel"),
            pytest.param("Auto", "kirocrew", id="auto-sentinel"),
            pytest.param("", "kirocrew", id="absent"),
        ],
    )
    @pytest.mark.asyncio
    async def test_persisted_agent_policy_is_used_consistently(
        self, tmp_path: Any, recorded: str, expected: str
    ) -> None:
        log = _log(tmp_path, agent=recorded)
        dispatcher, _, sessions, _ = _dispatcher(tmp_path, log=log)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        captured: list[str | None] = []
        real_persist = dispatcher._persist_turn

        def _persist(*args: Any, **kwargs: Any) -> None:
            captured.append(kwargs.get("agent", args[5] if len(args) > 5 else None))
            real_persist(*args, **kwargs)

        dispatcher._persist_turn = _persist  # type: ignore[method-assign]
        await dispatcher.handle_message(_dm("continue"))

        assert sessions.last_agent == expected
        assert dispatcher.ctx_builder.build_calls[-1]["agent"] == expected
        assert captured == [expected]

    @pytest.mark.asyncio
    async def test_persisted_agent_metadata_read_runs_off_loop(self, tmp_path: Any) -> None:
        log = _log(tmp_path, agent="research-agent")
        dispatcher, _, _, _ = _dispatcher(tmp_path, log=log)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        loop_thread = threading.get_ident()
        reads: list[int] = []
        real_get = log.get_metadata

        def _get(key: str) -> dict[str, Any]:
            reads.append(threading.get_ident())
            return real_get(key)

        log.get_metadata = _get  # type: ignore[method-assign]
        await dispatcher.handle_message(_dm("continue"))

        assert reads and all(thread_id != loop_thread for thread_id in reads)

    @pytest.mark.asyncio
    async def test_unreadable_agent_metadata_falls_back_to_route_agent(self, tmp_path: Any) -> None:
        log = _log(tmp_path)
        dispatcher, _, sessions, _ = _dispatcher(tmp_path, log=log)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )

        def _broken(key: str) -> dict[str, Any]:
            raise OSError("unreadable")

        log.get_metadata = _broken  # type: ignore[method-assign]
        await dispatcher.handle_message(_dm("continue"))

        assert sessions.last_agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_cold_resume_suppresses_every_native_only_side_effect(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kiro_crew.dashboard import channel_slots

        log = _log(tmp_path, agent="research-agent")
        dispatcher, _, sessions, _ = _dispatcher(tmp_path, log=log)
        sessions.is_new_result = True
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        surface = AsyncMock()
        monkeypatch.setattr(channel_slots, "surface_dispatcher_session", surface)
        title_claims: list[str] = []
        monkeypatch.setattr(auto_title, "try_claim", lambda key: title_claims.append(key) or True)
        before = log.get_metadata("dashboard:chat-1").get("title")

        await dispatcher.handle_message(_dm("continue"))

        assert sessions.set_channel_calls == []
        assert sessions.origin_links == {}
        assert log.get_metadata("dashboard:chat-1").get("title") == before
        assert title_claims == []
        surface.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resumed_turn_explicitly_broadcasts_channel_user_row(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kiro_crew.dashboard import channel_slots

        dispatcher, _, _, _ = _dispatcher(tmp_path)
        dispatcher._session_resume.route = AsyncMock(
            return_value=RoutingDecision(resumed_key="dashboard:chat-1")
        )
        broadcasts: list[bool] = []

        def _project(*_args: Any, **kwargs: Any) -> None:
            broadcasts.append(bool(kwargs.get("broadcast_user")))

        monkeypatch.setattr(channel_slots, "project_channel_turn_live", _project)

        await dispatcher.handle_message(_dm("continue"))

        assert broadcasts == [True]


class TestTelegramResumeIntegration:
    def test_dashboard_state_injection_reaches_the_resume_controller(self, tmp_path: Any) -> None:
        dispatcher, client, _, _ = _dispatcher(tmp_path)
        state = _make_state(tmp_path)
        transport = SimpleNamespace(channel_type="telegram", dispatcher=dispatcher, client=client)

        state.register_channel_transport(transport)

        assert dispatcher.dashboard_state is state
        assert dispatcher._session_resume.dashboard_state is state

    def test_link_spelling_matches_the_existing_mirror_path(self, tmp_path: Any) -> None:
        dispatcher, _, _, _ = _dispatcher(tmp_path)
        route = ("forum", "-100:11")

        assert dispatcher._session_resume.link_for(-100, 11) == dispatcher._origin_mirror_link(
            route, -100
        )

    def test_capability_exposes_inbound_resume(self) -> None:
        assert TELEGRAM_CAPABILITIES.supports_session_resume is True

    def test_dashboard_resume_target_requires_one_owner_dm(self) -> None:
        from kiro_crew.telegram.transport import TelegramTransport

        owner = TelegramTransport(_Client(), allowed_user_ids={7})
        assert owner.may_resume_from("7", None) is True
        assert owner.may_resume_from("8", None) is False
        assert owner.may_resume_from("7", "11") is False

        shared = TelegramTransport(_Client(), allowed_user_ids={7, 8})
        assert shared.may_resume_from("7", None) is False
        assert shared.may_resume_from("8", None) is False
