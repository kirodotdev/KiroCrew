"""Fakes and builders shared by the ``test_discord_sessions*.py`` suites.

A real ``DiscordDispatcher`` runs against these in-memory stand-ins for the Discord
client, the provider, the session store, the conversation log, hooks and context.
``test_discord_resumed_busy.py`` imports ``_bind_to_chat1``, ``_dispatcher``, ``_log``
and ``_message`` from ``test_discord_sessions``, which binds them from here.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_discord import MultipartFake

from kiro_crew.discord.client import DiscordInteraction
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.history import transcript_stem
from kiro_crew.messaging.link import UNBIND_REASON_UNSPECIFIED, ChannelLink
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session import _opt_out_key
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.session_map import ConversationOwnershipConflict


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse fixture of test_discord_sessions.py and the suites split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


class _Client(MultipartFake):
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, str, Any]] = []
        self.acked: list[str] = []
        self.dm_pairings: dict[str, str] = {}
        self._mid = 100
        self.send_fails = False

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: Any = None,
        reply_to_message_id: Any = None,
    ) -> str | None:
        if self.send_fails:
            return None
        self._mid += 1
        self.sent.append((text, components))
        return str(self._mid)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        text: str,
        *,
        components: Any = None,
    ) -> bool:
        self.edits.append((message_id, text, components))
        return True

    async def edit_message_components(
        self,
        channel_id: str,
        message_id: str,
        components: Any,
    ) -> bool:
        return True

    def remember_dm_recipient(self, channel_id: str, user_id: str) -> None:
        self.dm_pairings[channel_id] = user_id

    async def ack_component_interaction(
        self, interaction_id: str, token: str, *, destination: str = ""
    ) -> None:
        self.acked.append(interaction_id)

    async def send_typing(self, channel_id: str) -> None:
        return None

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        return None

    async def is_thread_channel(self, channel_id: str) -> bool:
        return True


class _Provider:
    supports_steer, cwd = False, str(Path.cwd())

    async def stream(self, message: str):
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        yield SimpleNamespace(
            kind=EVENT_TEXT_CHUNK,
            text=f"Answer: {message}",
            stop_reason="",
            tool_call_id="",
            title="",
            context_usage_pct=0.0,
        )
        yield SimpleNamespace(
            kind=EVENT_COMPLETE,
            text="",
            stop_reason="end_turn",
            tool_call_id="",
            title="",
            context_usage_pct=0.0,
        )


class _Sessions:
    async def aflush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error
        self.flushed.append(dict(self.mirror_links))

    def __init__(self) -> None:
        self.mirror_links: dict[str, ChannelLink] = {}
        # A slot's legacy Slack thread link, kept beside the mirror the way the
        # real map keeps ``slack_thread_ts``: ``get_mirror_link`` synthesizes a
        # Slack link from it when no explicit mirror is stored, and a clear of
        # the mirror never touches it.
        self.slack_threads: dict[str, tuple[str, str]] = {}
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0
        self.flushed: list[dict] = []
        self.flush_error: Exception | None = None
        self.reserve_error: Exception | None = None
        self.origin_links: dict[str, ChannelLink] = {}
        self.inbound_keys: set[str] = set()
        self.mirror_opt_outs: set[str] = set()
        # Every reason a clear was made with, so a test can assert the in-channel
        # unlink is attributed rather than landing as unattributed in the audit.
        self.unbind_reasons: list[str] = []
        self.last_key = ""
        self.targeted: list[tuple[str, str]] = []
        self.channel_keys: set[str] = set()
        self.reserved_generations: set[str] = set()
        self.provider = _Provider()

    def set_mirror_link(
        self,
        key: str,
        link: ChannelLink,
        *,
        accepts_inbound: bool = False,
        reason: str = UNBIND_REASON_UNSPECIFIED,
    ) -> None:
        # Interface parity with the real SessionMap: a conversation is exclusive
        # once it is inbound-committed — this claim is inbound-capable, or an
        # occupant already is. Two outbound-only mirrors are still allowed.
        # Without the rule here, the in-channel `!link` refusal path is
        # unreachable and a test for it would pass against unguarded production
        # code; with it WIDER than production, a test would pass against a
        # refusal production never makes.
        rivals = [
            other for other, held in self.mirror_links.items() if other != key and held == link
        ]
        if rivals and (accepts_inbound or any(other in self.inbound_keys for other in rivals)):
            raise ConversationOwnershipConflict(
                f"{link.channel_type} conversation is already held by {rivals[0]}"
            )
        self.mirror_links[key] = link
        if accepts_inbound:
            self.inbound_keys.add(key)
        else:
            self.inbound_keys.discard(key)

    def set_origin_link(self, key: str, link: ChannelLink) -> None:
        self.origin_links[key] = link

    @contextmanager
    def batched_save(self) -> Any:
        yield

    def set_mirror_opt_out(self, key: str, opted_out: bool) -> None:
        if opted_out:
            self.mirror_opt_outs.add(_opt_out_key(key))
        else:
            self.mirror_opt_outs.discard(_opt_out_key(key))

    def mirror_opt_out(self, key: str) -> bool:
        return _opt_out_key(key) in self.mirror_opt_outs

    def get_origin_link(self, key: str) -> ChannelLink | None:
        return self.origin_links.get(key)

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        # Interface parity with the real SessionMap: the explicit mirror first,
        # else a Slack link synthesized from the surviving thread link.
        link = self.mirror_links.get(key)
        if link is not None:
            return link
        thread = self.slack_threads.get(key)
        if thread is not None:
            return ChannelLink(channel_type="slack", channel_id=thread[1], thread_id=thread[0])
        return None

    def has_mirror_row(self, key: str) -> bool:
        # The explicit row alone, as the real map answers it: no synthesis.
        return key in self.mirror_links

    def find_mirror_sessions(
        self,
        link: ChannelLink,
        *,
        inbound_only: bool = False,
    ) -> list[str]:
        return [
            key
            for key, candidate in self.mirror_links.items()
            if candidate == link and (not inbound_only or key in self.inbound_keys)
        ]

    def clear_mirror_link(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> bool:
        self.unbind_reasons.append(reason)
        self.inbound_keys.discard(key)
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(
        self, link: ChannelLink, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> list[str]:
        self.unbind_reasons.append(reason)
        cleared = self.find_mirror_sessions(link)
        for key in cleared:
            self.inbound_keys.discard(key)
            self.mirror_links.pop(key, None)
        return cleared

    def reserve_generation(self, session_key: str) -> None:
        if self.reserve_error is not None:
            raise self.reserve_error
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
        return False

    async def try_acquire(self, key: str) -> bool:
        self.targeted.append(("try_acquire", key))
        return False

    def clear_queue(self, key: str, owned_by: Any = None) -> None:
        self.targeted.append(("clear_queue", key))

    async def get_or_create(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        self.last_key = key
        self.last_agent = kwargs.get("agent")
        self.channel_keys.add(key)
        return self.provider, getattr(self, "is_new_result", False), True

    def begin_turn(self, key: str) -> None:
        """The real manager's synchronous pre-dispatch closing gate.

        ``closing`` mirrors SessionManager._closing so this refuses the dispatch
        the way the real gate does once close_all has run.
        """
        self.begin_turns = getattr(self, "begin_turns", 0) + 1
        if getattr(self, "closing", False):
            raise SessionClosingError("SessionManager is closing")

    async def set_channel(self, key: str, channel: str) -> None:
        self.set_channel_calls = getattr(self, "set_channel_calls", [])
        self.set_channel_calls.append((key, channel))
        return None

    def record_success(self, key: str) -> None:
        return None

    async def record_failure(self, key: str) -> None:
        return None

    def release(self, key: str) -> None:
        return None

    def check_context_usage(self, key: str, provider: Any) -> float:
        return 0.0

    def dequeue(self, key: str) -> None:
        return None

    def has_session(self, key: str) -> bool:
        return True

    def get_provider(self, key: str) -> Any:
        return self.provider


class _ConversationLog:
    def __init__(self, rows: list[dict], messages: dict[str, list[dict]]) -> None:
        self.rows = rows
        self.messages = messages
        self.metadata: dict[str, dict] = {}
        self.list_calls = 0
        self.search_calls: list[tuple[str, int]] = []

    def get_metadata(self, key: str) -> dict:
        return self.metadata.get(key, {})

    def list_sessions(self) -> list[dict]:
        self.list_calls += 1
        return list(self.rows)

    def search_sessions(self, query: str, limit: int = 50) -> list[dict]:
        """Mirror KiroCrewHistory.search_sessions' FIELD COVERAGE and phrase
        semantics: one casefolded phrase matched against title OR message
        content, title hits ranked first. Deliberately not a reimplementation of
        the real scorer -- the ranking formula is tested in the history tests;
        what matters here is that the picker DELEGATES and renders the result."""
        self.search_calls.append((query, limit))
        needle = " ".join(query.casefold().split())
        title_hits: list[dict] = []
        content_hits: list[dict] = []
        for row in self.rows:
            title = " ".join(str(row.get("title") or "").casefold().split())
            # Rows carry the JSONL stem ("dashboard_chat-0") while the message
            # store is keyed canonically ("dashboard:chat-0"); the real history
            # reads both from one store, so canonicalise here or content is
            # always empty and the test silently passes for the wrong reason.
            raw_key = str(row.get("key") or "")
            canonical = raw_key
            while canonical.startswith("dashboard_"):
                canonical = canonical[len("dashboard_") :]
            canonical = f"dashboard:{canonical}" if canonical else raw_key
            body = " ".join(
                str(msg.get("content") or "")
                for msg in self.messages.get(canonical, self.messages.get(raw_key, []))
            ).casefold()
            if needle in title:
                title_hits.append(row)
            elif needle in body:
                content_hits.append(row)
        return (title_hits + content_hits)[:limit]

    def has_log(self, key: str) -> bool:
        return key in self.messages

    def recent(
        self,
        key: str,
        max_messages: int = 20,
        roles: set[str] | None = None,
    ) -> list[dict]:
        rows = self.messages.get(key, [])
        if roles:
            rows = [row for row in rows if row.get("role") in roles]
        return rows[-max_messages:]

    def append(
        self, key: str, role: str, content: str, agent: str | None = None, mid: str | None = None
    ) -> None:
        self.messages.setdefault(key, []).append({"role": role, "content": content})

    def update_metadata(self, key: str, fields: dict) -> None:
        self.metadata.setdefault(key, {}).update(fields)
        self.messages.setdefault(key, [])
        stem = transcript_stem(key)
        for row in self.rows:
            if row.get("key") == stem:
                row.update(fields)
                break
        else:
            self.rows.insert(0, {"key": stem, **fields})

    def update_metadata_if(self, key: str, fields: dict, guard: Any) -> bool:
        if not guard(self.metadata.get(key, {})):
            return False
        self.update_metadata(key, fields)
        return True

    def set_title(self, key: str, title: str) -> None:
        self.titles_set = getattr(self, "titles_set", [])
        self.titles_set.append((key, title))
        return None


class _Hooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, *args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(action="allow")


class _Context:
    hooks = _Hooks()

    def build_message(self, text: str, is_new: bool, key: str, **kwargs: Any) -> Any:
        self.last_build_kwargs = kwargs
        return text, None


def _config() -> Any:
    return SimpleNamespace(
        discord=SimpleNamespace(soft_threshold_pct=80),
        dashboard=SimpleNamespace(
            restore_window_minutes=30,
            surface_channel_sessions=True,
        ),
        agent=SimpleNamespace(default_agent="kirocrew"),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(
    allowed: set[str],
    log: _ConversationLog | None,
    *,
    sessions: _Sessions | None = None,
) -> tuple[DiscordDispatcher, _Client, _Sessions]:
    sessions = sessions or _Sessions()
    dispatcher = DiscordDispatcher(
        sessions=sessions,  # type: ignore[arg-type]
        ctx_builder=_Context(),  # type: ignore[arg-type]
        cfg=_config(),
        allowed_user_ids=allowed,
        conv_log=log,  # type: ignore[arg-type]
    )
    client = _Client()
    dispatcher.client = client  # type: ignore[assignment]
    return dispatcher, client, sessions


def _message(text: str, channel_id: str = "c1", thread_id: str = "") -> InboundMessage:
    return InboundMessage(
        channel_type="discord",
        user_id="u1",
        conversation_id=channel_id,
        text=text,
        thread_id=thread_id,
    )


def _interaction(
    custom_id: str, message_id: str, channel_id: str = "c1", label: str = ""
) -> DiscordInteraction:
    return DiscordInteraction(
        interaction_id="i1",
        interaction_token="tok",
        channel_id=channel_id,
        user_id="u1",
        message_id=message_id,
        custom_id=custom_id,
        label=label,
        guild_id="",
    )


def _picker_button(client: _Client) -> tuple[str, str]:
    _, components = client.sent[-1]
    button = components[0]["components"][0]
    return button["custom_id"], str(client._mid)


def _log(title: str = "Launch plan") -> _ConversationLog:
    return _ConversationLog(
        [{"key": "dashboard_chat-1", "title": title, "memory_mode": "persistent"}],
        {"dashboard:chat-1": []},
    )


def _log_with_titles(*titles: str) -> _ConversationLog:
    return _ConversationLog(
        [
            {
                "key": f"dashboard_chat-{index}",
                "title": title,
                "memory_mode": "persistent",
            }
            for index, title in enumerate(titles)
        ],
        {f"dashboard:chat-{index}": [] for index in range(len(titles))},
    )


async def _bind_to_chat1(
    dispatcher: DiscordDispatcher, client: _Client, log: _ConversationLog
) -> None:
    """Bind DM c1 to dashboard:chat-1 through the real picker flow."""
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"
