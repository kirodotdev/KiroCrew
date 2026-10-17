"""The client, session and dispatcher doubles the Telegram session test modules share.

``_Client`` (a ``FakeClient`` whose sends and edits can be made to fail), ``_Sessions`` (the
session manager stand-in), ``_config`` / ``_log`` / ``_dispatcher`` (a real
``TelegramDispatcher`` over them), the inbound builders ``_dm``, ``_topic``, ``_callback``
and ``_picker_button``, and the autouse ``_reset_shared_trust``.
``test_telegram_sessions.py`` and ``test_telegram_sessions_resume.py`` import them by name.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from off_loop_helpers import off_loop
from test_telegram import FakeClient, FakeCtx, FakeProvider

from kiro_crew.history import ConversationLog, transcript_stem
from kiro_crew.messaging import auto_title
from kiro_crew.messaging.link import UNBIND_REASON_UNSPECIFIED, ChannelLink
from kiro_crew.messaging.session_trust import clear_trusted_sessions
from kiro_crew.session import _opt_out_key
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.telegram.transport import TelegramInboundMessage
from kiro_crew.telegram.transport_dispatch import TelegramDispatcher


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the Telegram session modules' autouse trust reset and auto-title stub, moved verbatim; none of them calls monkeypatch.undo()
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
        self.channel_keys: set[str] = set()
        self.reserved_generations: set[str] = set()

    async def get_or_create(
        self,
        key: str,
        *,
        agent: str | None = None,
        channel_id: str | None = None,
        model: str | None = None,
        start_priority=None,
    ) -> tuple[FakeProvider, bool, bool]:
        self.last_key = key
        self.last_agent = agent
        self.last_model = model
        self.channel_keys.add(key)
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

    def has_mirror_row(self, key: str) -> bool:
        return key in self.mirror_links

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

    def reserve_generation(self, session_key: str) -> None:
        self.reserved_generations.add(session_key)
        self.channel_keys.add(session_key)

    def max_generation(self, bucket: str) -> int:
        prefix = f"{bucket}:gen"
        return max(
            (
                int(key[len(prefix) :])
                for key in self.reserved_generations
                if key.startswith(prefix) and key[len(prefix) :].isdigit()
            ),
            default=-1,
        )

    def channel_key_for_stem(self, stem: str) -> str:
        for key in self.channel_keys | set(self.mirror_links):
            if transcript_stem(key) == stem:
                return key
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

    def clear_queue(self, key: str, owned_by: Any = None) -> None:
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
    """A conversation log holding one titled dashboard session.

    The seeding writes run on a worker thread, as the dashboard's own writers do.
    Nearly every caller is an ``async`` test, and on the event-loop thread
    ``replace_with_retry`` re-raises a Windows sharing violation on its first
    attempt rather than sleep the loop, so a scanner briefly holding the fresh
    temp file would fail the setup itself with WinError 5. Off the loop the
    bounded retry applies.
    """
    log = ConversationLog(base_dir=tmp_path / "sessions")

    def _seed() -> None:
        log.append("dashboard:chat-1", "assistant", "prior work")
        log.set_title("dashboard:chat-1", "Launch plan")
        if agent:
            log.update_metadata("dashboard:chat-1", {"agent": agent})

    off_loop(_seed)
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
