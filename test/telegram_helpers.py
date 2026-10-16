"""Shared doubles for the Telegram channel tests.

``FakeClient`` captures outbound Bot API calls; ``FakeProvider``, ``FakeSessions`` and
``FakeCtx`` stand in for the provider, the session manager and the gateway context, and
``_dispatcher`` builds a ``TelegramDispatcher`` over them. ``_drop_live_config_snapshot``
is an autouse fixture: a test module that imports it clears, after each of its tests,
the live config snapshot ``_prime_live`` publishes.
"""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.messaging.link import UNBIND_REASON_UNSPECIFIED
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session import BACKGROUND_KEY, _opt_out_key
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.telegram.transport_dispatch import TelegramDispatcher, _origin_kwargs, _QueuedOrigin


def _tg_origin(
    user: int | str = 7,
    chat: int | str = 7,
    *,
    thread: str = "",
    chat_type: str = "private",
    username: str = "",
) -> _QueuedOrigin:
    """One queued message's origin: who sent it, and where its reply goes.

    Defaults are user 7 in chat 7, the DM these tests use throughout.
    """
    return _QueuedOrigin(
        user_id=str(user),
        chat_id=str(chat),
        thread_id=thread,
        chat_type=chat_type,
        username=username,
    )


def _origin(*a: Any, **kw: Any) -> dict[str, str]:
    """:func:`_tg_origin` as queue-entry kwargs, spelled by the PRODUCTION writer.

    A queue entry carries who sent it and where its reply goes, because the drain
    replays it under that envelope rather than under the turn that opened the queue.
    Built through ``_origin_kwargs`` rather than by spelling the storage keys, so
    renaming one moves this fixture with it instead of leaving it green against a
    shape production does not write.
    """
    return _origin_kwargs(_tg_origin(*a, **kw))


@pytest.fixture(autouse=True)
def _drop_live_config_snapshot():
    """Leave no primed config snapshot behind for the next test.

    ``_prime_live`` publishes into the process-global watcher, so without this
    the last test to prime would silently set the live config for every test
    after it in the same worker.
    """
    yield
    from kiro_crew.config import live

    live.reset_for_tests()


def _prime_live(cfg: Any) -> None:
    """Publish *cfg*'s ``telegram`` and ``messaging`` fields as the live snapshot.

    The dispatcher reads those two sections at POINT OF USE from the config
    watcher rather than from the ``cfg=`` copy it was constructed with, so a
    test that varies one of them has to put the value where the turn actually
    looks for it. Every field the test's SimpleNamespace carries is copied onto
    a real ``KiroCrewConfig``, so the production readers see real sections and
    the loader's own defaults fill the rest.

    Call it again after mutating ``d.cfg`` mid-test -- the snapshot is a copy,
    not a view.
    """
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    base = KiroCrewConfig()
    sections = {}
    for name in ("telegram", "messaging"):
        section = getattr(cfg, name, None)
        if section is None:
            continue
        overrides = {
            f.name: getattr(section, f.name)
            for f in dataclasses.fields(getattr(base, name))
            if hasattr(section, f.name)
        }
        sections[name] = dataclasses.replace(getattr(base, name), **overrides)
    live.reset_for_tests()
    live.watch().prime(dataclasses.replace(base, **sections))


class FakeClient:
    """Captures outbound Bot API calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[int, str, Any]] = []
        #: chat_id per send_message / edit_message call (parallel to `sent` / `edits`).
        #: Which CHAT an outbound call addressed is otherwise invisible here, and it is
        #: the whole question for a queue shared by two people: a receipt edited under
        #: the wrong chat's address reaches a chat that message id does not exist in.
        self.send_chats: list[int] = []
        self.edit_chats: list[int] = []
        self.drafts: list[tuple[int, str]] = []
        self.markup_edits: list[tuple[int, Any]] = []
        self.answered: list[str] = []
        self.reply_targets: list[Any] = []
        self.reactions: list[tuple[int, str]] = []
        # Forum-topic ids captured per outbound send/typing (parallel to sent).
        self.send_threads: list[Any] = []
        self.typing_threads: list[Any] = []
        self._mid = 100
        #: (markdown, reply_markup, thread) per sendRichMessage call.
        self.rich_sent: list[tuple[str, Any, Any]] = []
        self.rich_silent: list[bool] = []
        #: message_ids passed to deleteMessage.
        self.deleted: list[int] = []
        #: When True, send_rich_message reports failure (server lacks the API).
        self.rich_fails = False
        #: (files, thread, silent) per multipart upload call.
        self.media_sent: list[tuple[Any, Any, bool]] = []
        #: When True, the upload reports failure so recovery can be observed.
        self.media_fails = False
        #: disable_notification per send_message call (parallel to `sent`).
        self.send_silent: list[bool] = []

    async def send_typing(self, chat_id: int, *, message_thread_id: Any = None) -> None:
        self.typing_threads.append(message_thread_id)
        return None

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str,
        *,
        parse_mode: Any = None,
        message_thread_id: Any = None,
    ) -> bool:
        self.drafts.append((draft_id, text))
        return True

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: Any = None,
        reply_markup: Any = None,
        retry_plain: bool = True,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
        disable_notification: bool = False,
    ) -> int:
        # flake-ok: pre-existing zero-length yield standing in for a network await, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # yield like a real network await (exposes races)
        self._mid += 1
        self.sent.append((text, reply_markup))
        self.send_chats.append(chat_id)
        self.reply_targets.append(reply_to_message_id)
        self.send_threads.append(message_thread_id)
        self.send_silent.append(disable_notification)
        return self._mid

    async def send_media_group(
        self,
        chat_id: int,
        photos: Any,
        *,
        message_thread_id: Any = None,
        disable_notification: bool = False,
    ) -> list[int]:
        # flake-ok: pre-existing zero-length yield standing in for a network await, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        self.media_sent.append((list(photos), message_thread_id, disable_notification))
        if self.media_fails:
            return []
        self._mid += 1
        return [self._mid]

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
        self.edit_chats.append(chat_id)
        return getattr(self, "edit_ok", True)

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any = None
    ) -> bool:
        self.markup_edits.append((message_id, reply_markup))
        return True

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self.answered.append(callback_query_id)

    async def send_rich_message(
        self,
        chat_id: int,
        markdown: str,
        *,
        reply_markup: Any = None,
        message_thread_id: Any = None,
        disable_notification: bool = False,
        reply_to_message_id: Any = None,
    ) -> Any:
        # flake-ok: pre-existing zero-length yield standing in for a network await, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # yield like a real network await
        if self.rich_fails:
            return None
        self._mid += 1
        self.rich_silent.append(disable_notification)
        self.rich_sent.append((markdown, reply_markup, message_thread_id))
        # Recorded on the SAME list as sendMessage's, because the assertion callers
        # care about is "did the turn's first outbound quote the question", and
        # which of the two methods opened the turn is an implementation detail.
        self.reply_targets.append(reply_to_message_id)
        return self._mid

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    async def set_message_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    def final_text(self) -> Any:
        """Text the user ultimately sees on the live message: the last edit if it
        was edited (edit-streaming), else the last send."""
        if self.edits:
            return self.edits[-1][1]
        return self.sent[-1][0] if self.sent else None

    def final_markup(self) -> Any:
        if self.edits:
            return self.edits[-1][2]
        return self.sent[-1][1] if self.sent else None


class _Ev:
    def __init__(self, kind: str, text: str = "", stop_reason: str = "", title: str = "") -> None:
        self.kind = kind
        self.text = text
        self.stop_reason = stop_reason
        self.tool_call_id = ""
        self.title = title
        self.context_usage_pct = 0.0


class FakeProvider:
    supports_steer = True

    def __init__(self, reply: str = "Answer", models: list | None = None) -> None:
        self._reply = reply
        self.steered: list = []
        self.cancelled = 0
        self.active_turn = True  # gates _handle_busy's live-turn steer check
        # Models the backend "advertised" at session init, plus the ids a
        # /model press pushed through session/set_model.
        self.advertised: list = list(models or [])
        self.set_models: list[str] = []
        self.set_model_error: Exception | None = None
        self.client = SimpleNamespace(set_model=self._set_model)

    def available_models(self) -> list:
        return list(self.advertised)

    async def _set_model(self, model_id: str) -> None:
        if self.set_model_error is not None:
            raise self.set_model_error
        self.set_models.append(model_id)

    def has_active_turn(self) -> bool:
        return self.active_turn

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> str:
        self.cancelled += 1
        return "acked"

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text=f"{self._reply}: {message[:16]}")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def stream_command(self, command: str) -> Any:
        yield _Ev(EVENT_COMPACTION_STATUS, text="completed", title="ok")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def compact(self, context: str = "") -> None:
        return None

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": "ok"}

    async def approve_tool(self, request_id: Any) -> None:
        return None

    async def reject_tool(self, request_id: Any) -> None:
        return None


class FakeSessions:
    def __init__(self, raise_on_get: bool = False) -> None:
        self.released: list[str] = []
        self.acquired: list[str] = []
        #: Acquires of the shared BACKGROUND session (auto-title, one-liners).
        self.background: list[str] = []
        self.destroyed: list[str] = []
        self.discarded: list[str] = []
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.last_agent: Any = None
        self.last_model: Any = None
        self.raise_on_get = raise_on_get
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0
        self._busy = False
        self._has = True
        self.queued: list = []
        self._gp = FakeProvider()
        self.mirror_links: dict[str, Any] = {}
        self.origin_links: dict[str, Any] = {}
        self.inbound_keys: set[str] = set()
        self.mirror_opt_outs: set[str] = set()
        #: Per key, what ``get_or_create`` captured as the superseded store.
        self.allocation_predecessors: dict[str, str] = {}
        #: Per key, the model ``get_or_create`` was asked for (the boundary's stamp).
        self.requested_models: dict[str, str] = {}
        self.batch_depth = 0
        self.batched_writes: list[bool] = []
        self._pid: Any = None

    async def get_or_create(
        self,
        key: str,
        *,
        agent: Any = None,
        channel_id: Any = None,
        model: Any = None,
        start_priority=None,
    ) -> Any:
        # The shared BACKGROUND session is not a turn, and recording it in the
        # turn-scoped fields makes every turn test read as if two turns ran: the
        # auto-title task takes it fire-and-forget right after the answer lands, so
        # `last_agent` and `acquired` would show the background acquire instead of
        # the one under test. Kept on its own list so a test that wants to see the
        # background turn still can.
        if key == BACKGROUND_KEY:
            self.background.append(key)
            return FakeProvider(), True, False
        self.last_agent = agent
        self.last_model = model
        if self.raise_on_get:
            raise RuntimeError("cold-start failed")
        # The real boundary captures the store this allocation supersedes INSIDE
        # its registration's critical section; the double mirrors that contract by
        # reading its mapping stand-in at the moment it "allocates", when attached.
        reader = getattr(self, "mapped_sid", None)
        if callable(reader):
            self.allocation_predecessors[key] = str(reader(key) or "")
        # The real boundary stamps the model the allocation SELECTED on the
        # session; the double records the argument it was handed.
        self.requested_models[key] = str(model or "")
        return FakeProvider(), True, False

    def allocation_predecessor(self, key: str) -> str:
        return self.allocation_predecessors.get(key, "")

    def allocation_requested_model(self, key: str) -> str:
        return self.requested_models.get(key, "")

    def begin_turn(self, key: str) -> None:
        """The real manager's synchronous pre-dispatch closing gate."""
        self.begin_turns += 1
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    async def set_channel(self, key: str, channel: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        self.successes.append(key)

    async def record_failure(self, key: str) -> None:
        self.failures.append(key)

    def check_context_usage(self, key: str, provider: Any) -> float:
        return 10.0

    def release(self, key: str) -> None:
        # Background releases go on their own list, for the same reason as the
        # acquire in get_or_create: they are not this turn's.
        if key == BACKGROUND_KEY:
            self.background.append(f"release:{key}")
            return
        self.released.append(key)

    def get_provider(self, key: str) -> Any:
        return self._gp

    def get_pid(self, key: str) -> Any:
        return self._pid

    def is_busy(self, key: str) -> bool:
        return self._busy

    def max_generation(self, bucket: str) -> int:
        return -1

    def set_mirror_link(
        self,
        key: str,
        link: Any,
        *,
        accepts_inbound: bool = False,
        reason: str = UNBIND_REASON_UNSPECIFIED,
    ) -> None:
        self.batched_writes.append(self.batch_depth > 0)
        self.mirror_links[key] = link
        if accepts_inbound:
            self.inbound_keys.add(key)
        else:
            self.inbound_keys.discard(key)

    def get_mirror_link(self, key: str) -> Any:
        return self.mirror_links.get(key)

    def set_origin_link(self, key: str, link: Any) -> None:
        """The in-memory origin record the dispatcher writes beside the mirror bind."""
        self.origin_links[key] = link

    def get_origin_link(self, key: str) -> Any:
        return self.origin_links.get(key)

    def find_mirror_sessions(self, link: Any, *, inbound_only: bool = False) -> list[str]:
        return [
            key
            for key, candidate in self.mirror_links.items()
            if candidate == link and (not inbound_only or key in self.inbound_keys)
        ]

    async def aflush(self) -> None:
        return None

    @contextmanager
    def batched_save(self) -> Any:
        self.batch_depth += 1
        try:
            yield
        finally:
            self.batch_depth -= 1

    def set_mirror_opt_out(self, key: str, opted_out: bool) -> None:
        self.batched_writes.append(self.batch_depth > 0)
        if opted_out:
            self.mirror_opt_outs.add(_opt_out_key(key))
        else:
            self.mirror_opt_outs.discard(_opt_out_key(key))

    def mirror_opt_out(self, key: str) -> bool:
        return _opt_out_key(key) in self.mirror_opt_outs

    def clear_mirror_link(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> bool:
        self.batched_writes.append(self.batch_depth > 0)
        self.inbound_keys.discard(key)
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(
        self, link: Any, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> list[str]:
        cleared = [key for key, candidate in self.mirror_links.items() if candidate == link]
        for key in cleared:
            self.inbound_keys.discard(key)
            self.mirror_links.pop(key, None)
        return cleared

    def enqueue(self, key: str, ts: str, text: str, *, force: bool = False, **kw: Any) -> bool:
        if force or self._busy:
            self.queued.append((ts, text, kw))
            return True
        return False

    def dequeue(self, key: str) -> Any:
        return self.queued.pop(0) if self.queued else None

    def clear_queue(self, key: str, owned_by: Any = None) -> None:
        self.queued.clear()

    def has_session(self, key: str) -> bool:
        return self._has

    def channel_key_for_stem(self, stem: str) -> str:
        return ""

    async def try_acquire(self, key: str) -> bool:
        # Mirror the real atomic acquire-if-idle: refuse if a turn holds the
        # semaphore or no session exists; otherwise "acquire" and record it.
        if self._busy or not self._has:
            return False
        self.acquired.append(key)
        return True

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)

    async def discard_conversation(self, key: str) -> None:
        self.discarded.append(key)

    def compact_wait_budget_secs(self) -> float:
        """The real manager's resolved ``session.compact_wait_secs`` (unset: 300 s)."""
        return 300.0


class _FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, *a: Any, **k: Any) -> Any:
        return SimpleNamespace(action="allow")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = _FakeHooks()
        #: Every build_message call's kwargs, so a test can assert what the channel
        #: told the context builder — `blocks_reads`, `user_display_name`,
        #: `runtime_source` are only observable here.
        self.build_calls: list[dict[str, Any]] = []

    def build_message(self, text: str, is_new: bool, key: str, **kw: Any) -> Any:
        self.build_calls.append({"text": text, "is_new": is_new, "key": key, **kw})
        return text, None


def _cfg(
    soft: int = 80,
    default_agent: str = "",
    *,
    allow_forum: bool = False,
    allowed_forum_chat_ids: list | None = None,
    dm_scope: str = "per-channel-peer",
    show_thinking: bool = False,
    forum_activation: str = "always",
) -> Any:
    return SimpleNamespace(
        telegram=SimpleNamespace(
            soft_threshold_pct=soft,
            allow_forum=allow_forum,
            allowed_forum_chat_ids=allowed_forum_chat_ids or [],
            show_thinking=show_thinking,
            forum_activation=forum_activation,
        ),
        agent=SimpleNamespace(default_agent=default_agent),
        messaging=SimpleNamespace(
            dm_scope=dm_scope,
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(
    allowed: set[int],
    *,
    raise_on_get: bool = False,
    default_agent: str = "",
    allow_forum: bool = False,
    allowed_forum_chat_ids: list | None = None,
    dm_scope: str = "per-channel-peer",
    forum_activation: str = "always",
) -> tuple[TelegramDispatcher, FakeClient, FakeSessions]:
    sess = FakeSessions(raise_on_get=raise_on_get)
    cfg = _cfg(
        default_agent=default_agent,
        allow_forum=allow_forum,
        allowed_forum_chat_ids=allowed_forum_chat_ids,
        dm_scope=dm_scope,
        forum_activation=forum_activation,
    )
    _prime_live(cfg)
    d = TelegramDispatcher(
        sessions=sess,  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=cfg,
        allowed_user_ids=allowed,
        agent=None,
        conv_log=None,
    )
    cli = FakeClient()
    d.client = cli  # type: ignore[assignment]
    return d, cli, sess


def _dm(text: str, uid: str = "7") -> InboundMessage:
    return InboundMessage(channel_type="telegram", user_id=uid, conversation_id=uid, text=text)


def _press(data: str, *, message_id: int = 101, uid: int = 7, label: str = "") -> Any:
    return SimpleNamespace(
        callback_query_id="q1",
        user_id=uid,
        chat_id=uid,
        message_id=message_id,
        data=data,
        label=label,
        chat_type="private",
    )
