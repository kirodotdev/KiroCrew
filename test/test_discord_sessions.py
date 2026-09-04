from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_discord import MultipartFake

from kiro_crew.config.paths import config_dir
from kiro_crew.discord import session_resume
from kiro_crew.discord.client import DISCORD_CHUNK_LIMIT, DiscordInteraction
from kiro_crew.discord.commands import parse_command, parse_command_argument
from kiro_crew.discord.renderer import session_provenance_tag
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.messaging import resume_expectation
from kiro_crew.messaging import session_resume as session_resume_core
from kiro_crew.messaging.link import UNBIND_REASON_UNSPECIFIED, ChannelLink
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session import _opt_out_key
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.session_map import ConversationOwnershipConflict


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


class _Client(MultipartFake):
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, str, Any]] = []
        self.acked: list[str] = []
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

    async def ack_component_interaction(self, interaction_id: str, token: str) -> None:
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
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0
        self.flushed: list[dict] = []
        self.flush_error: Exception | None = None
        self.origin_links: dict[str, ChannelLink] = {}
        self.inbound_keys: set[str] = set()
        self.mirror_opt_outs: set[str] = set()
        # Every reason a clear was made with, so a test can assert the in-channel
        # unlink is attributed rather than landing as unattributed in the audit.
        self.unbind_reasons: list[str] = []
        self.last_key = ""
        self.targeted: list[tuple[str, str]] = []
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
        return self.mirror_links.get(key)

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

    def max_generation(self, bucket: str) -> int:
        return -1

    def is_busy(self, key: str) -> bool:
        return False

    async def try_acquire(self, key: str) -> bool:
        self.targeted.append(("try_acquire", key))
        return False

    def clear_queue(self, key: str) -> None:
        self.targeted.append(("clear_queue", key))

    async def get_or_create(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        self.last_key = key
        self.last_agent = kwargs.get("agent")
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
) -> tuple[DiscordDispatcher, _Client, _Sessions]:
    sessions = _Sessions()
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


def _picker_labels(client: _Client) -> list[str]:
    _, components = client.sent[-1]
    return [button["label"] for row in components for button in row["components"]]


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


@pytest.mark.asyncio
async def test_sessions_finds_a_session_by_conversation_content() -> None:
    """The original bug: searching a phrase from the CONVERSATION, not the title.

    The picker used to call ``list_sessions`` and filter on titles only, so a
    query the user remembered from the discussion could never match. Routing
    through the shared ``search_sessions`` -- the same one the dashboard uses --
    makes message content searchable.
    """
    log = _ConversationLog(
        [
            {"key": "dashboard_chat-0", "title": "Untitled", "memory_mode": "persistent"},
            {"key": "dashboard_chat-1", "title": "Also untitled", "memory_mode": "persistent"},
        ],
        {
            "dashboard:chat-0": [{"role": "user", "content": "unrelated chatter"}],
            "dashboard:chat-1": [
                {"role": "user", "content": "how do I link to a specific session?"}
            ],
        },
    )
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!session link to a specific session"))

    # Matched on content despite neither title containing the phrase.
    assert _picker_labels(client) == ["1. Also untitled"]


@pytest.mark.asyncio
async def test_sessions_cjk_query_reaches_title_fallback() -> None:
    """A spaceless CJK query must trigger the zero-hit TITLE fallback.

    The fallback used to gate on a whitespace word count, which a spaceless
    CJK query (one "word") never satisfied — so when the shared search found
    nothing, the fallback silently demanded the literal title substring. The
    gate now derives from the same parse_search_query needles as the shared
    search, so a title holding the query's words apart still resolves.
    """
    log = _ConversationLog(
        [
            {"key": "dashboard_chat-0", "title": "内存的泄漏问题排查", "memory_mode": "persistent"},
            {"key": "dashboard_chat-1", "title": "Unrelated", "memory_mode": "persistent"},
        ],
        {"dashboard:chat-0": [], "dashboard:chat-1": []},
    )
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!session 内存泄漏"))

    # The fake shared search misses (no literal phrase anywhere), so only the
    # title fallback — running the REAL parse + gate — can produce this row.
    assert _picker_labels(client) == ["1. 内存的泄漏问题排查"]


@pytest.mark.asyncio
async def test_sessions_delegates_to_the_shared_search() -> None:
    """Assert DELEGATION, not re-implemented ranking.

    The scoring formula belongs to KiroCrewHistory.search_sessions and is tested
    there; what this surface must guarantee is that it calls that search rather
    than growing a second one that drifts from the dashboard.
    """
    log = _log_with_titles("Codex compaction investigation")
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions codex"))

    assert log.search_calls, "picker did not call search_sessions"
    query, limit = log.search_calls[0]
    assert query == "codex"
    assert limit > 1, "must fetch more rows than one so filtering cannot starve the picker"


@pytest.mark.asyncio
async def test_sessions_empty_query_does_not_search() -> None:
    """A bare `!sessions` is a listing, not a search -- no query, no search call."""
    log = _log_with_titles("One", "Two")
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions"))

    assert log.search_calls == []
    assert log.list_calls >= 1
    assert len(_picker_labels(client)) == 2


def test_sessions_command_aliases() -> None:
    assert parse_command("!sessions") == "sessions"
    assert parse_command("/sessions") == "sessions"
    assert parse_command("!session Link to a specific session") == "sessions"
    assert parse_command_argument("!session Link to a specific session") == (
        "Link to a specific session"
    )
    assert parse_command_argument("!sessions") == ""


@pytest.mark.asyncio
async def test_sessions_keyword_filters_beyond_recent_limit() -> None:
    log = _log_with_titles(
        *(f"Routine session {index}" for index in range(12)),
        "Codex compaction investigation",
    )
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!session codex"))

    text, _ = client.sent[-1]
    assert _picker_labels(client) == ["1. Codex compaction investigation"]
    assert "Dashboard session search" in text
    assert "for `codex`" in text


@pytest.mark.asyncio
async def test_sessions_multi_word_query_matches_case_insensitively() -> None:
    log = _log_with_titles("Other work", "Link to a Specific Session")
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions specific link"))

    assert _picker_labels(client) == ["1. Link to a Specific Session"]


@pytest.mark.asyncio
async def test_sessions_no_match_is_explicit() -> None:
    dispatcher, client, _ = _dispatcher({"u1"}, _log())

    await dispatcher.handle_message(_message("!sessions missing topic"))

    text, components = client.sent[-1]
    assert components is None
    assert "No dashboard sessions matched `missing topic`" in text
    assert "Try fewer words" in text
    assert "`!sessions`" in text
    assert len(dispatcher._session_pickers) == 0


@pytest.mark.asyncio
async def test_empty_sessions_query_keeps_recent_order_and_discloses_cap() -> None:
    log = _log_with_titles(*(f"Recent session {index}" for index in range(12)))
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions   "))

    assert _picker_labels(client) == [f"{index + 1}. Recent session {index}" for index in range(10)]
    assert "Showing 10 of 12 most recent dashboard sessions" in client.sent[-1][0]


@pytest.mark.asyncio
async def test_sessions_search_cap_is_enforced_and_disclosed() -> None:
    log = _log_with_titles(*(f"Codex session {index}" for index in range(12)))
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions codex"))

    assert len(_picker_labels(client)) == 10
    assert "Showing 10 of 12 matching sessions" in client.sent[-1][0]


@pytest.mark.asyncio
async def test_sessions_requires_exactly_one_allowed_user() -> None:
    log = _log()
    dispatcher, client, _ = _dispatcher({"u1", "u2"}, log)

    await dispatcher.handle_message(_message("!sessions private"))

    assert log.list_calls == 0
    assert "exactly one" in client.sent[-1][0]
    assert client.sent[-1][1] is None


@pytest.mark.asyncio
async def test_sessions_lists_only_persistent_dashboard_sessions_and_redacts() -> None:
    secret = "ghp_" + "a" * 36
    log = _ConversationLog(
        [
            {
                "key": "dashboard_chat-1",
                "title": f"Deploy with {secret}",
                "memory_mode": "persistent",
            },
            {
                "key": "discord_kirocrew_direct_u1",
                "title": "Current Discord session",
                "memory_mode": "persistent",
            },
            {
                "key": "dashboard_private",
                "title": "Incognito",
                "memory_mode": "temporary",
            },
        ],
        {"dashboard:chat-1": []},
    )
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions"))

    text, components = client.sent[-1]
    buttons = [button for row in components for button in row["components"]]
    assert "Recent dashboard sessions" in text
    assert len(buttons) == 1
    assert buttons[0]["custom_id"].startswith("s:")
    assert secret not in buttons[0]["label"]
    assert "REDACTED" in buttons[0]["label"]


@pytest.mark.asyncio
async def test_binding_claimed_during_header_edit_is_not_overwritten() -> None:
    """A link that lands while the header edit is in flight must win.

    `_bind_lock` only serialises Discord's own picker. A dashboard mirror POST
    or another channel's `!link` takes no such lock, so the conflict checks are
    re-run after the awaited edit; without that, this PR's own double-binding
    rules would be bypassed and the newer binding silently replaced.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    rival = ChannelLink(channel_type="telegram", channel_id="rival")

    async def _claim_mid_edit(*args: Any, **kwargs: Any) -> bool:
        # Simulates the dashboard/other-channel bind landing during the
        # Discord round-trip the real edit_message performs.
        sessions.set_mirror_link("dashboard:chat-1", rival)
        return True

    client.edit_message = _claim_mid_edit  # type: ignore[assignment]

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    # The rival binding survives, and the resume did NOT mark it inbound.
    assert sessions.mirror_links["dashboard:chat-1"] == rival
    assert "dashboard:chat-1" not in sessions.inbound_keys


@pytest.mark.asyncio
async def test_resumed_turn_lands_in_live_dashboard_window() -> None:
    """A resumed turn must enter the OPEN slot's window, not just disk.

    The dashboard save writes meta + frozen prefix + its own window + foreign
    tail. A disk-only append made before a later dashboard turn is therefore
    re-serialized AFTER it and the transcript reads back out of order. Landing
    the turn in the live window keeps it inside the region the save
    re-serializes. Mirrors dashboard/cron_inject.py.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)

    class _Slot:
        def __init__(self) -> None:
            self.messages: list[tuple[str, str]] = []

        def append(self, role: str, content: str, cls: str = "", **kw: Any) -> None:
            self.messages.append((role, content))

    class _State:
        def __init__(self) -> None:
            self.slot = _Slot()
            self.pushes = 0

        def get_slot(self, name: str) -> Any:
            return self.slot if name == "chat-1" else None

        def push_slots_update(self) -> None:
            self.pushes += 1

    state = _State()
    dispatcher._session_resume.dashboard_state = state

    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_key == "dashboard:chat-1"
    roles = [r for r, _ in state.slot.messages]
    assert roles == ["user", "assistant"], state.slot.messages
    assert state.slot.messages[0][1] == "continue here"
    assert state.pushes >= 1


@pytest.mark.asyncio
async def test_mirrored_turn_persists_idempotently() -> None:
    """The slot's own save re-serializes its window, so the disk write must not
    duplicate what the live slot already carries."""
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)

    calls: list[str] = []
    log.append_if_absent = lambda *a, **k: calls.append("if_absent")  # type: ignore[attr-defined]

    class _State:
        def get_slot(self, name: str) -> Any:
            return type("S", (), {"append": lambda *a, **k: None})()

        def push_slots_update(self) -> None:
            return None

    dispatcher._session_resume.dashboard_state = _State()
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    before = len(log.messages["dashboard:chat-1"])

    await dispatcher.handle_message(_message("continue here"))

    # Idempotent path used, and no plain append duplicated the turn on disk.
    assert calls, "expected append_if_absent when a live slot took the turn"
    assert len(log.messages["dashboard:chat-1"]) == before


@pytest.mark.asyncio
async def test_resumed_turn_without_live_slot_still_persists() -> None:
    """No open slot (the common phone-only case) → plain disk append."""
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    before = len(log.messages["dashboard:chat-1"])

    await dispatcher.handle_message(_message("continue here"))

    assert len(log.messages["dashboard:chat-1"]) > before


@pytest.mark.asyncio
async def test_stacked_dashboard_prefix_binds_canonical_session() -> None:
    log = _ConversationLog(
        [
            {
                "key": "dashboard_dashboard_chat-1",
                "title": "Launch plan",
                "memory_mode": "persistent",
            }
        ],
        {"dashboard:chat-1": [{"role": "assistant", "content": "prior work"}]},
    )
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert set(sessions.mirror_links) == {"dashboard:chat-1"}
    assert sessions.inbound_keys == {"dashboard:chat-1"}


@pytest.mark.asyncio
async def test_choice_binds_replays_and_routes_followup() -> None:
    secret = "ghp_" + "b" * 36
    messages = [
        {"role": "user", "content": "omitted oldest"},
        {"role": "assistant", "content": "context one"},
        {"role": "user", "content": f"credential {secret}"},
        {"role": "assistant", "content": "context three"},
        {"role": "user", "content": "@everyone context four"},
        {"role": "assistant", "content": "context five"},
    ]
    log = _log()
    log.messages["dashboard:chat-1"] = messages
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    link = ChannelLink(channel_type="discord", channel_id="c1")
    assert sessions.mirror_links["dashboard:chat-1"] == link
    assert "dashboard:chat-1" in sessions.inbound_keys
    visible = "\n".join([text for text, _ in client.sent] + [text for _, text, _ in client.edits])
    assert "Resumed: Launch plan" in visible
    assert "omitted oldest" not in visible
    assert secret not in visible
    assert "@everyone" not in visible

    await dispatcher.handle_message(_message("continue here"))
    assert sessions.last_key == "dashboard:chat-1"


@pytest.mark.parametrize("banner_lands", [True, False], ids=["banner-lands", "banner-lost"])
@pytest.mark.asyncio
async def test_resume_evidence_is_durable_before_the_success_banner(banner_lands) -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    resume = dispatcher._session_resume
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    real_edit, seen = client.edit_message, []

    async def _crash_boundary_edit(channel_id, message_id, text, *, components=None):
        # The gateway may die as Discord commits this edit; only durable state survives.
        if "Resumed" in text:
            seen.append(await resume._expectations.get("c1"))
            if not banner_lands:
                return False
        return await real_edit(channel_id, message_id, text, components=components)

    client.edit_message = _crash_boundary_edit  # type: ignore[method-assign]
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert seen and seen[0] is not None, "success banner shown before evidence was durable"
    assert ("dashboard:chat-1" in sessions.inbound_keys) is banner_lands
    assert await resume._expectations.get("c1") is not None, "a lost banner erased the evidence"


@pytest.mark.asyncio
async def test_resume_replay_sanitizes_internal_protocol() -> None:
    log = _log()
    log.messages["dashboard:chat-1"] = [
        {"role": "user", "content": "Conversation compacted: real question"},
        {
            "role": "assistant",
            "content": "✅ Conversation compacted: ## OBJECTIVE\ninternal guidance",
            "meta": {"kind": "compaction"},
        },
        {
            "role": "assistant",
            "content": "Conversation compacted: ## USER GUIDANCE\nlegacy internal body",
        },
        {
            "role": "assistant",
            "content": (
                "before [STEERING steer-7e6a4a0d-9431-4d2d-b000-000000000001: "
                "internal steer] after"
            ),
        },
        {"role": "assistant", "content": "real answer"},
    ]
    dispatcher, client, _ = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    visible = " ".join(text for text, _ in client.sent)
    assert "Conversation compacted: real question" in visible
    assert "real question" in visible and "real answer" in visible
    assert "before" in visible and "after" in visible
    assert "OBJECTIVE" not in visible and "USER GUIDANCE" not in visible
    assert "internal guidance" not in visible and "legacy internal body" not in visible
    assert "STEERING" not in visible and "internal steer" not in visible


@pytest.mark.asyncio
async def test_resume_replay_previews_long_markdown_without_breaking_a_fence() -> None:
    """A replayed transcript entry is shortened at a boundary the grammar accepts.

    A blind character slice lands inside the fenced code block and Discord then
    renders everything after it as literal backticks instead of code, so the one
    message meant to give the user context arrives unreadable. Taking the shared
    splitter's FIRST chunk shortens at a boundary instead: every chunk but the
    last is sealed, so the block the cut opened is closed.
    """
    body = "```python\n" + "x = 1\n" * 800 + "```\n"
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": body}]
    dispatcher, client, _ = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    replayed = [text for text, _ in client.sent if "x = 1" in text]
    assert len(replayed) == 1, replayed  # still ONE message per transcript entry
    preview = replayed[0]
    assert len(preview) <= DISCORD_CHUNK_LIMIT, len(preview)
    assert preview.count("```") % 2 == 0, preview[-40:]  # the block is closed
    assert preview.endswith(session_resume._REPLAY_TRUNCATED)  # and says so
    # The icon sits on its own line, so the opener still starts a line.
    assert preview.startswith("🤖\n```python\n")


@pytest.mark.asyncio
async def test_resume_replay_omits_a_fence_that_cannot_fit_safely() -> None:
    """An extreme opener cannot fit together with its synthetic closer.

    Sending that sealed chunk anyway lets Discord's client-side cap remove the
    closer and truncation marker. The replay keeps the canonical transcript
    intact and emits only the explicit marker for this pathological preview.
    """
    opener = "`" * 1001
    body = opener + "\n" + ("x" * 1800) + "\n"
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": body}]
    dispatcher, client, _ = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    replayed = [text for text, _ in client.sent if session_resume._REPLAY_TRUNCATED in text]
    assert replayed == [f"🤖\n{session_resume._REPLAY_TRUNCATED}"]
    assert len(replayed[0]) <= DISCORD_CHUNK_LIMIT
    assert opener not in replayed[0]


@pytest.mark.asyncio
async def test_resume_replay_bounds_and_offloads_the_splitter_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript-sized pathological line is bounded and split off-loop."""
    probes: list[str] = []
    offloads: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def _capture(probe: str, limit: int, *, reserve: int = 0) -> list[str]:
        probes.append(probe)
        return ["safe preview"]

    async def _offload(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        offloads.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(session_resume, "split_markdown_safe", _capture)
    monkeypatch.setattr(session_resume.asyncio, "to_thread", _offload)

    preview = await session_resume._replay_preview(
        "`" * 1_000_000,
        session_resume._REPLAY_TEXT_LIMIT,
        reserve=session_resume._REPLAY_RESERVE,
    )

    assert len(probes) == 1
    assert len(probes[0]) <= session_resume._REPLAY_TEXT_LIMIT * 2
    assert offloads == [
        (
            _capture,
            (probes[0], session_resume._REPLAY_TEXT_LIMIT),
            {"reserve": session_resume._REPLAY_RESERVE},
        )
    ]
    assert preview == "safe preview" + session_resume._REPLAY_TRUNCATED


@pytest.mark.asyncio
async def test_cold_resume_does_not_stamp_channel_or_retitle() -> None:
    """A cold resumed session must not get new-session bookkeeping.

    The picker lists *history*, so most picks are not live and `get_or_create`
    returns is_new=True. Treating that as a new session used to (a) write
    `discord:<id>` into the dashboard session's legacy slack_channel_id — which
    survives `!unlink` and makes every later pick refuse with "already active on
    Slack" — and (b) overwrite the conversation's title with the Discord message.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior work"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert "dashboard:chat-1" in sessions.inbound_keys

    # Cold ACP session: the normal case for a picked history session.
    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_key == "dashboard:chat-1"
    # No channel stamp onto the resumed dashboard session...
    assert getattr(sessions, "set_channel_calls", []) == []
    # ...and its title is untouched.
    assert getattr(log, "titles_set", []) == []
    assert dispatcher.ctx_builder.last_build_kwargs["runtime_source"] == "discord"


@pytest.mark.asyncio
async def test_own_session_still_gets_new_session_bookkeeping() -> None:
    """The guard must not disable bookkeeping for Discord's OWN conversation."""
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    sessions.is_new_result = True

    await dispatcher.handle_message(_message("hello there"))

    assert [key for key, _ in getattr(sessions, "set_channel_calls", [])] == [sessions.last_key]
    assert [key for key, _ in getattr(log, "titles_set", [])] == [sessions.last_key]


@pytest.mark.asyncio
async def test_own_session_records_the_origin_conversation() -> None:
    """The auto-compact notice needs the REAL channel, not the DM's user-id bucket."""
    log = _log()
    dispatcher, _, sessions = _dispatcher({"u1"}, log)
    sessions.is_new_result = True

    await dispatcher.handle_message(_message("hello there", channel_id="c1"))

    link = sessions.get_origin_link(sessions.last_key)
    assert link is not None
    assert (link.channel_type, link.channel_id) == ("discord", "c1")


@pytest.mark.asyncio
async def test_new_own_session_surfaces_in_dashboard_immediately(monkeypatch) -> None:
    """Discord must not wait for the 30-second lifetime reconcile pass."""
    from kiro_crew.dashboard import channel_slots

    log = _log()
    dispatcher, _, sessions = _dispatcher({"u1"}, log)
    sessions.is_new_result = True
    state = object()
    dispatcher._session_resume.dashboard_state = state
    calls: list[tuple[Any, int]] = []

    async def _reconcile(candidate: Any, window_minutes: int) -> int:
        calls.append((candidate, window_minutes))
        return 1

    monkeypatch.setattr(channel_slots, "reconcile_channel_slots", _reconcile)

    await dispatcher.handle_message(_message("hello there"))

    assert calls == [(state, 30)]


@pytest.mark.asyncio
async def test_resumed_session_does_not_surface_duplicate_dashboard_slot(monkeypatch) -> None:
    """A Discord-driven dashboard resume already owns a slot."""
    from kiro_crew.dashboard import channel_slots

    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    calls: list[tuple[Any, int]] = []

    async def _reconcile(candidate: Any, window_minutes: int) -> int:
        calls.append((candidate, window_minutes))
        return 1

    monkeypatch.setattr(channel_slots, "reconcile_channel_slots", _reconcile)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    sessions.is_new_result = True

    await dispatcher.handle_message(_message("continue here"))

    assert calls == []


@pytest.mark.asyncio
async def test_picker_refuses_outside_a_dm() -> None:
    """`!sessions` must not list or replay private history into a shared thread.

    The owner gate answers WHO may resume, not WHERE the result is shown: in a
    guild thread the picker's session titles and the 5-message replay would be
    readable by every member of that thread.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())

    await dispatcher.handle_message(_message("!sessions", thread_id="t9"))

    assert len(dispatcher._session_pickers) == 0
    assert sessions.mirror_links == {}
    assert any("direct message" in text for text, _ in client.sent)


@pytest.mark.asyncio
async def test_persisted_default_agent_sentinel_is_not_forwarded() -> None:
    """`agent: "default"` is a sentinel, not an agent name.

    Most dashboard sessions record `"default"`, meaning "let the backend pick".
    Forwarding it reaches ACP `session/set_mode`, which rejects it with
    `Mode 'default' not found` and fails EVERY message sent to the resumed
    session. The channel's own agent must be used instead.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    log.metadata["dashboard:chat-1"] = {"agent": "default"}
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_key == "dashboard:chat-1"
    assert sessions.last_agent == "kirocrew"


@pytest.mark.asyncio
async def test_persisted_auto_agent_sentinel_is_not_forwarded() -> None:
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    log.metadata["dashboard:chat-1"] = {"agent": "Auto"}
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_agent == "kirocrew"


@pytest.mark.asyncio
async def test_resumed_session_runs_under_its_own_agent() -> None:
    """A resumed session keeps its own agent, not Discord's default.

    On a cold start get_or_create applies the agent it is handed, so using the
    Discord default would run the dashboard conversation under a different system
    prompt and a different allowedTools set.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    log.metadata["dashboard:chat-1"] = {"agent": "research-agent"}
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_key == "dashboard:chat-1"
    assert sessions.last_agent == "research-agent"


@pytest.mark.asyncio
async def test_resumed_session_without_recorded_agent_falls_back() -> None:
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_agent == "kirocrew"


async def _bind_to_chat1(
    dispatcher: DiscordDispatcher, client: _Client, log: _ConversationLog
) -> None:
    """Bind DM c1 to dashboard:chat-1 through the real picker flow."""
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"


@pytest.mark.asyncio
async def test_option_press_routes_into_resumed_session() -> None:
    """An [OPTIONS:] press on a bound DM is the RESUMED session's turn content.

    The press re-dispatches with ``interpret_commands=False`` because the label is
    model-authored text that must never execute as a command — but that flag must
    not also skip resume routing. The buttons were rendered on the bound session's
    own reply (and carry its provenance tag), so the choice belongs to that
    session. Before the fix the press ran in the native DM session: the click
    answered a question nobody asked there, while the bound session kept waiting
    for its answer (live incident 2026-08-30: a merge-approach choice for the
    bound session green-lit the native session's parked debug plan instead).
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await _bind_to_chat1(dispatcher, client, log)

    tag = session_provenance_tag("dashboard:chat-1")
    await dispatcher.on_interaction(_interaction(f"opt:0:{tag}", "m-opts", label="Ship option (a)"))

    assert sessions.last_key == "dashboard:chat-1"
    # The label reached the session as literal turn content, commands off.
    assert any(t.startswith("> Ship option (a)") for t, _ in client.sent)


@pytest.mark.asyncio
async def test_untagged_press_is_refused_fail_closed() -> None:
    """A pre-provenance button (bare ``opt:<i>``) is refused, never routed.

    Discord replays old components indefinitely, so an untagged press can never
    prove which session it belongs to — and routing it by current binding is
    exactly the cross-session injection this fix exists to stop (server review
    round 1, span 405abadda748: the legacy pass-through never ages out, so it
    was the same hole with a different door). Fail closed with a type-it-instead
    notice; no echo, no turn anywhere.
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await _bind_to_chat1(dispatcher, client, log)

    await dispatcher.on_interaction(_interaction("opt:0", "m-opts", label="Ship option (a)"))

    assert any("predate" in t for t, _ in client.sent)
    assert not any(t.startswith("> Ship option (a)") for t, _ in client.sent)
    assert sessions.last_key == ""  # no turn ran anywhere


@pytest.mark.asyncio
async def test_tagged_press_is_revalidated_after_idle_rotation() -> None:
    """Idle/daily rotation between the gate and the turn invalidates the tag.

    ``maybe_rotate`` can bump the native generation AFTER the pre-busy gate
    passed, so the turn would run under a key the tag never named. The gate is
    re-run against the FINAL key after rotation — the same invariant ``!new``
    enforces, applied to the reset nobody typed.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    tag = session_provenance_tag(dispatcher.current_session_key("u1"))
    scope = dispatcher._scope_id("u1")

    def _rotate_now(*args: object, **kwargs: object) -> None:
        dispatcher._conv.bump_gen(scope)

    dispatcher._conv.maybe_rotate = _rotate_now  # type: ignore[method-assign]

    await dispatcher.on_interaction(_interaction(f"opt:0:{tag}", "m-opts", label="Choice A"))

    assert any("moved away" in t for t, _ in client.sent)
    assert sessions.last_key == ""  # the rotated generation never ran the press


@pytest.mark.asyncio
async def test_stale_tagged_press_after_rebind_is_refused() -> None:
    """A button minted by session A, pressed after the DM was rebound to B, refuses.

    Discord replays old components indefinitely: A's reply keeps its live buttons
    after ``!unlink`` + ``!sessions`` rebinds the DM to B. Routing alone would send
    the press into B — injecting A's model-authored choice into an unrelated
    transcript, the GPT round-1 blocker. The provenance tag makes the press valid
    only while the conversation still targets the session that posted it.
    """
    log = _log_with_titles("Alpha plan", "Beta plan")
    log.messages["dashboard:chat-0"] = [{"role": "assistant", "content": "prior a"}]
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior b"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)

    # Bind A (chat-0) through the real picker, as the incident did.
    await dispatcher.handle_message(_message("!sessions Alpha"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-0"
    stale_tag = session_provenance_tag("dashboard:chat-0")

    # Rebind to B (chat-1): unlink, then pick again.
    await dispatcher.handle_message(_message("!unlink"))
    await dispatcher.handle_message(_message("!sessions Beta"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"
    sessions.last_key = ""

    await dispatcher.on_interaction(
        _interaction(f"opt:0:{stale_tag}", "m-old-opts", label="Ship option (a)")
    )

    assert any("moved away" in t for t, _ in client.sent)
    assert sessions.last_key == ""  # no turn ran in B, in A, or natively


@pytest.mark.asyncio
async def test_press_tagged_for_pre_new_conversation_is_refused() -> None:
    """``!new`` invalidates the previous conversation's buttons.

    The native key embeds the generation, so a press carrying the pre-``!new``
    tag no longer matches — honest, since the conversation that asked the
    question is over. Before the tag the press ran silently in the fresh
    generation, answering a question it never asked.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    old_tag = session_provenance_tag(dispatcher.current_session_key("u1"))

    await dispatcher.handle_message(_message("!new"))
    assert dispatcher.current_session_key("u1") != ""
    sessions.last_key = ""

    await dispatcher.on_interaction(_interaction(f"opt:0:{old_tag}", "m-opts", label="Choice A"))

    assert any("moved away" in t for t, _ in client.sent)
    assert sessions.last_key == ""


@pytest.mark.asyncio
async def test_stale_press_against_busy_target_is_refused_not_queued() -> None:
    """The provenance gate precedes the busy check — pinned as ordering.

    A stale press must be refused BEFORE ``is_busy`` runs: the busy path either
    queues (native) or posts the busy refusal (resumed), and a queued stale
    press would be drained and replayed WITHOUT its tag, executing unchecked
    later. It also must not leak whether the current target is mid-turn.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    old_tag = session_provenance_tag(dispatcher.current_session_key("u1"))
    await dispatcher.handle_message(_message("!new"))
    sessions.last_key = ""
    sessions.is_busy = lambda key: True  # type: ignore[method-assign]
    enqueued: list = []
    sessions.enqueue = lambda *a, **k: enqueued.append(a)  # type: ignore[attr-defined]

    await dispatcher.on_interaction(_interaction(f"opt:0:{old_tag}", "m-opts", label="Choice A"))

    assert any("moved away" in t for t, _ in client.sent)
    assert not any("busy" in t for t, _ in client.sent)
    assert enqueued == []
    assert sessions.last_key == ""


@pytest.mark.asyncio
async def test_valid_tagged_press_against_busy_target_is_refused_not_queued_or_steered() -> None:
    """A VALID press whose target is mid-turn refuses — it never enters the queue.

    ``_handle_busy`` enqueues bare text (or steers it mid-turn), and the drain
    replays queued text WITHOUT the provenance tag — so a ``!new`` or idle
    rotation between enqueue and drain would execute the model-authored choice
    in a conversation the tag never named (server review round 2, span
    405abadda748). The busy path is therefore unreachable for any tagged press.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    tag = session_provenance_tag(dispatcher.current_session_key("u1"))
    sessions.is_busy = lambda key: True  # type: ignore[method-assign]
    enqueued: list = []
    sessions.enqueue = lambda *a, **k: enqueued.append(a)  # type: ignore[attr-defined]
    steered: list = []
    sessions.steer = lambda *a, **k: steered.append(a)  # type: ignore[attr-defined]

    await dispatcher.on_interaction(_interaction(f"opt:0:{tag}", "m-opts", label="Choice A"))

    assert any("busy" in t and "NOT applied" in t for t, _ in client.sent)
    assert enqueued == [] and steered == []
    assert sessions.last_key == ""  # no turn ran anywhere


@pytest.mark.asyncio
async def test_option_press_after_binding_destroyed_is_refused_not_native() -> None:
    """A press whose binding died mid-flight gets the Detached refusal.

    The refusal is the honest outcome routing already produces for a typed
    message; the press must not fall back to a silent native turn, which is the
    exact misroute the routing gate exists to stop.
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await _bind_to_chat1(dispatcher, client, log)
    stale_tag = session_provenance_tag("dashboard:chat-1")
    # The binding dies out from under the DM (a housekeeping clear, not !unlink).
    sessions.clear_mirror_link("dashboard:chat-1")

    await dispatcher.on_interaction(
        _interaction(f"opt:0:{stale_tag}", "m-opts", label="Ship option (a)")
    )

    assert any("Detached" in t for t, _ in client.sent)
    assert sessions.last_key == ""  # no turn ran anywhere


@pytest.mark.asyncio
async def test_synthetic_dispatch_without_origin_tag_keeps_native_affinity() -> None:
    """Untagged synthetic turns keep the legacy skip: native session, no routing.

    Queue drains replay messages the native session accepted while busy, and
    AutoNudge fires target the native key their loop rotation-checked — routing
    either into a binding created later would run them in a session that never
    queued or armed them. This pins that ``interpret_commands=False`` with no
    ``origin_tag`` still means "skip routing" for those callers.
    """
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await _bind_to_chat1(dispatcher, client, log)

    await dispatcher.handle_message(_message("nudge cycle text"), interpret_commands=False)

    assert sessions.last_key != "dashboard:chat-1"
    assert sessions.last_key.startswith("discord")


@pytest.mark.asyncio
async def test_stale_picker_fails_closed(monkeypatch) -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    # Expire by moving the TTL, not by reaching into a registry entry: the constant is
    # the contract, and a test that ages private state passes even if the TTL stops
    # being consulted.
    monkeypatch.setattr(session_resume_core, "PICKER_TTL_SECS", -1)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert sessions.mirror_links == {}
    assert any("expired" in text for _, text, _ in client.edits)


@pytest.mark.asyncio
async def test_picker_nonce_is_bound_to_its_registered_choices() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    await dispatcher.handle_message(_message("!sessions"))
    _, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction("s:not-the-picker:0", message_id))

    assert sessions.mirror_links == {}
    assert any("expired" in text for _, text, _ in client.edits)


@pytest.mark.asyncio
async def test_choice_refuses_session_linked_elsewhere() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="telegram", channel_id="other"),
    )
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert sessions.mirror_links["dashboard:chat-1"].channel_type == "telegram"
    assert any("already active on Telegram" in text for _, text, _ in client.edits)


@pytest.mark.asyncio
async def test_choice_refuses_occupied_discord_conversation() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:other",
        ChannelLink(channel_type="discord", channel_id="c1"),
        accepts_inbound=True,
    )
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert "dashboard:chat-1" not in sessions.mirror_links
    assert any("!unlink" in text for _, text, _ in client.edits)


@pytest.mark.asyncio
async def test_choice_refusal_for_outbound_mirror_names_unlink() -> None:
    # The outbound-only occupant used to get "Unlink the existing dashboard
    # mirror first" — an instruction with no in-channel action. `!unlink` now
    # clears outbound mirrors by location, so the guidance is unified and must
    # name the command for BOTH occupant kinds.
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:other",
        ChannelLink(channel_type="discord", channel_id="c1"),
    )
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert "dashboard:chat-1" not in sessions.mirror_links
    assert any("Run `!unlink` first" in text for _, text, _ in client.edits)
    # And the instruction is followable: the sweep frees the location, after
    # which the conflict check no longer refuses.
    sessions.clear_mirror_links_at(ChannelLink(channel_type="discord", channel_id="c1"))
    conflict = dispatcher._session_resume._binder.binding_conflict(
        "dashboard:chat-1",
        "chat one",
        ChannelLink(channel_type="discord", channel_id="c1"),
    )
    assert conflict is None


@pytest.mark.asyncio
async def test_leave_resumed_session_frees_whole_location() -> None:
    # The resumed-session release must clear co-located occupants too. A session
    # map can hold that state — written before conversations became exclusive, or
    # hand-edited — and the release has to free all of it. `set_mirror_link`
    # refuses to create it, so the rows go in directly.
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    loc = ChannelLink(channel_type="discord", channel_id="c1")
    sessions.mirror_links["dashboard:resumed"] = loc
    sessions.inbound_keys.add("dashboard:resumed")
    sessions.mirror_links["dashboard:bystander"] = loc

    released = await dispatcher._session_resume.leave_resumed_session("c1")

    assert released == "dashboard:resumed"
    assert sessions.mirror_links == {}


@pytest.mark.asyncio
async def test_outbound_only_mirror_does_not_hijack_inbound_turn() -> None:
    dispatcher, _, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="discord", channel_id="c1"),
    )

    await dispatcher.handle_message(_message("hello"))

    assert sessions.last_key == dispatcher._session_key("u1")


@pytest.mark.asyncio
async def test_link_refuses_while_resumed_instead_of_stranding_binding() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    link = ChannelLink(channel_type="discord", channel_id="c1")
    sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

    await dispatcher.handle_message(_message("!link"))

    # The resumed binding is untouched and still the inbound route.
    assert sessions.find_mirror_sessions(link, inbound_only=True) == ["dashboard:chat-1"]
    assert "!unlink" in client.sent[-1][0]


@pytest.mark.asyncio
async def test_unlink_returns_to_native_discord_context() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="discord", channel_id="c1"),
        accepts_inbound=True,
    )

    await dispatcher.handle_message(_message("!unlink"))
    await dispatcher.handle_message(_message("hello"))

    assert "dashboard:chat-1" not in sessions.mirror_links
    assert "Back to your Discord conversation" in client.sent[0][0]
    assert sessions.last_key == dispatcher._session_key("u1")


@pytest.mark.asyncio
async def test_new_leaves_resumed_session_and_advances_native_generation() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    old_key = dispatcher._session_key("u1")
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="discord", channel_id="c1"),
        accepts_inbound=True,
    )

    await dispatcher.handle_message(_message("!new"))

    assert "dashboard:chat-1" not in sessions.mirror_links
    assert dispatcher._session_key("u1") != old_key
    assert "left the resumed session" in client.sent[-1][0]


def _store_path() -> Path:
    trust = config_dir() / resume_expectation._TRUST_SUBDIR
    trust.mkdir(parents=True, exist_ok=True)
    return trust / resume_expectation.store_filename("discord")


def _gate_send(client, marker: str, during) -> list[str]:
    fired: list[str] = []
    real_send = client.send_message

    async def _send(channel_id: str, text: str, **kwargs):
        if marker in text and not fired:
            fired.append(text)
            await during()
        return await real_send(channel_id, text, **kwargs)

    client.send_message = _send  # type: ignore[method-assign]
    return fired


async def _attach(dispatcher: DiscordDispatcher, client: _Client) -> ChannelLink:
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    return ChannelLink(channel_type="discord", channel_id="c1")


class TestDashboardConnectedConversationResumes:
    """The reported bug: replying to a message from Kiro Crew forked a new session.

    A dashboard session connected to a Discord conversation, sent into it, and
    the user replied there. Instead of continuing that session, the reply started
    a brand-new one with no history — so the user was answered by an agent that
    had never seen the conversation it was replying inside.

    The inbound resolver was never at fault. ``resumed_session`` filters on the
    binding's inbound marker, and the dashboard's connect never set it, so the
    resolver correctly found no owner and the dispatcher fell through to Discord's
    own route-derived session key. These tests pin the marker's effect on routing
    from both sides.
    """

    @pytest.mark.asyncio
    async def test_reply_resumes_the_connected_session(self) -> None:
        dispatcher, _client, sessions = _dispatcher({"u1"}, _log())
        # What a dashboard connect leaves behind once the transport declares
        # `supports_session_resume` — the binding plus its inbound marker.
        sessions.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="discord", channel_id="c1"),
            accepts_inbound=True,
        )

        assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"
        assert dispatcher._inbound_session_key("u1", "c1") == "dashboard:chat-1"

    @pytest.mark.asyncio
    async def test_an_outbound_only_binding_still_forks(self) -> None:
        """The bug's exact mechanism, pinned so the marker cannot be dropped.

        Same binding, no inbound marker — which is all main's connect could
        write. The reply must NOT resolve to the connected session, and the key
        the dispatcher falls back to is a Discord-native one, i.e. a different
        conversation with none of the dashboard transcript. This is the
        before-state; `accepts_inbound` is the whole difference.
        """
        dispatcher, _client, sessions = _dispatcher({"u1"}, _log())
        sessions.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="discord", channel_id="c1"),
        )

        assert dispatcher._session_resume.resumed_session("c1") is None
        forked = dispatcher._inbound_session_key("u1", "c1")
        assert forked != "dashboard:chat-1"
        assert forked.startswith("discord:")

    @pytest.mark.asyncio
    async def test_two_owners_route_nowhere_which_is_why_exclusivity_ships_here(
        self,
    ) -> None:
        """Why the ownership rule belongs with the inbound marker.

        "No owner" and "two owners" land on the same ``None``. So setting the
        inbound marker without enforcing one-session-per-conversation would not
        fix the fork — it would only move it: a duplicated binding sends the
        reply to NO session, silently forking exactly as before, and now with no
        way for the user to tell why.
        """
        dispatcher, _client, sessions = _dispatcher({"u1"}, _log())
        link = ChannelLink(channel_type="discord", channel_id="c1")
        # Planted past the writer's guard: the state a map file can still hold.
        sessions.mirror_links["dashboard:chat-1"] = link
        sessions.inbound_keys.add("dashboard:chat-1")
        sessions.mirror_links["dashboard:chat-2"] = link
        sessions.inbound_keys.add("dashboard:chat-2")

        assert dispatcher._session_resume.resumed_session("c1") is None
        assert dispatcher._inbound_session_key("u1", "c1").startswith("discord:")

    @pytest.mark.asyncio
    async def test_inchannel_link_reports_the_refusal_instead_of_failing(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log())
        loc = ChannelLink(channel_type="discord", channel_id="c1")
        # Planted past the writer's guard: the duplicate state a map file can
        # still hold, and the only state that reaches the handler's writer.
        for occupant in ("dashboard:chat-7", "dashboard:chat-8"):
            sessions.mirror_links[occupant] = loc
            sessions.inbound_keys.add(occupant)
        assert (
            dispatcher._session_resume.resumed_session("c1") is None
        ), "setup no longer produces the ambiguous state this test needs"

        await dispatcher.handle_message(_message("!link"))

        assert any(
            "`!unlink`" in text for text, _ in client.sent
        ), f"the refusal was not reported to the channel: {client.sent}"
        assert set(sessions.mirror_links) == {
            "dashboard:chat-7",
            "dashboard:chat-8",
        }, "an occupant was displaced, or the link was written anyway"

    @pytest.mark.asyncio
    async def test_two_outbound_mirrors_are_still_allowed(self) -> None:
        _dispatcher_, _client, sessions = _dispatcher({"u1"}, _log())
        loc = ChannelLink(channel_type="discord", channel_id="c1")
        sessions.set_mirror_link("dashboard:chat-1", loc)
        # Must not raise.
        sessions.set_mirror_link("dashboard:chat-2", loc)
        assert sessions.find_mirror_sessions(loc, inbound_only=True) == []


class TestBindingLostUnderTheConversation:

    @pytest.mark.asyncio
    async def test_a_vanished_binding_is_refused_exactly_once(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = await _attach(dispatcher, client)
        store = dispatcher._session_resume._expectations
        real_retire = store.retire_if

        async def _retire_after_durability(channel_id: str, version: int):
            assert link not in sessions.flushed[-1].values()
            return await real_retire(channel_id, version)

        store.retire_if = _retire_after_durability  # type: ignore[method-assign]
        sessions.clear_mirror_links_at(link)
        sessions.last_key = ""
        await dispatcher.handle_message(_message("what did we decide?"))
        assert "Detached" in client.sent[-1][0]
        assert "Launch plan" in client.sent[-1][0]
        assert sessions.last_key == "", "the refused message still ran a turn"
        await dispatcher.handle_message(_message("resending this"))
        assert len([t for t, _ in client.sent if "Detached" in t]) == 1
        assert sessions.last_key == dispatcher._session_key("u1")

    @pytest.mark.asyncio
    async def test_a_synthetic_nudge_bypasses_detach_routing(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await dispatcher._session_resume._expectations.record(
            "c1", "dashboard:chat-1", "Launch plan"
        )
        nudge = _message("[auto-nudge cycle 1]\ncheck")
        await dispatcher.handle_message(nudge, interpret_commands=False)
        assert not any("Detached" in text for text, _ in client.sent)
        assert sessions.last_key == dispatcher._session_key("u1")

    @pytest.mark.asyncio
    async def test_the_binding_is_durably_gone_before_the_record_is(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await _attach(dispatcher, client)
        store = dispatcher._session_resume._expectations
        sessions.flush_error = OSError("disk full")
        await dispatcher.handle_message(_message("!unlink"))
        assert any("NOT completed" in text for text, _ in client.sent)
        assert await store.get("c1") is not None, "failed durability retired the record"
        assert not sessions.flushed
        await dispatcher.handle_message(_message("is this still attached?"))
        assert await store.get("c1") is not None, "failed settlement retired the record"
        sessions.flush_error = None
        real_retire, durable_first = store.retire_if, []

        async def _observe_retire(channel_id: str, version: int):
            durable_first.append(bool(sessions.flushed))
            return await real_retire(channel_id, version)

        store.retire_if = _observe_retire  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("!unlink"))
        link = dispatcher._session_resume.link_for("c1")
        assert sessions.flushed and not any(link in snap.values() for snap in sessions.flushed)
        assert durable_first == [True], "the retry retired the record before durability"
        retired = await store.get("c1")
        assert retired is not None and retired.retired

    @pytest.mark.asyncio
    async def test_a_same_key_rebind_during_unlink_cannot_defeat_the_exit(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log())
        link = await _attach(dispatcher, client)
        store = dispatcher._session_resume._expectations
        real_aflush = sessions.aflush

        async def _rebind_same_key_during_aflush() -> None:
            await real_aflush()
            sessions.mirror_links["dashboard:chat-1"] = link
            sessions.inbound_keys.add("dashboard:chat-1")

        sessions.aflush = _rebind_same_key_during_aflush  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("!unlink"))
        expected = await store.get("c1")
        assert expected is not None and expected.retired, "a raced owner suppressed retirement"
        sessions.last_key = ""
        await dispatcher.handle_message(_message("still here?"))
        assert sessions.last_key == "", "the rebound session was entered in silence"
        assert "Now linked to" in client.sent[-1][0], client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_releasing_an_unrecorded_binding_leaves_evidence_before_mutation(self) -> None:
        """A failed release records evidence first, then changes NOTHING -- live map too.

        The in-memory clear happens before the flush, so a flush failure used to leave the
        binding gone in this process while the command reported failure. Two things were
        wrong with that pair: the very next message was refused with "Detached: no longer
        linked" moments after being told the unlink did not happen, and the map on disk
        still held the binding, so a restart revived it and split one history in two -- the
        exact harm the sibling test names. The release now rolls the clear back, so the
        report, the live map and the persisted map all agree and the conversation carries
        on where the user was told it would.
        """
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1")
        # A dashboard-created binding that never carried a message: no record exists.
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        store = dispatcher._session_resume._expectations
        sessions.flush_error = OSError("disk full")
        await dispatcher.handle_message(_message("!unlink"))
        assert any("NOT completed" in text for text, _ in client.sent)
        expected = await store.get("c1")
        assert expected is not None and not expected.retired, "mutated without durable evidence"
        assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"
        await dispatcher.handle_message(_message("where did we land?"))
        assert sessions.last_key == "dashboard:chat-1", "the resumed session must carry on"
        assert not any("Detached" in text for text, _ in client.sent)

    @pytest.mark.parametrize("recorded", [False, True], ids=["bare", "recorded"])
    @pytest.mark.asyncio
    async def test_a_rebind_inside_unlink_retire_keeps_its_record(self, recorded: bool) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = await _attach(dispatcher, client)
        resume, store = dispatcher._session_resume, dispatcher._session_resume._expectations
        real_retire_if = store.retire_if

        async def _rebind(channel_id: str, version: int):
            assert resume._bind_lock.locked(), "release settlement escaped the bind lock"
            applied = await real_retire_if(channel_id, version)
            sessions.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)
            if recorded:
                await store.record(channel_id, "dashboard:chat-2", "Pricing review")
            return applied

        store.retire_if = _rebind  # type: ignore[method-assign]
        await resume.leave_resumed_session("c1")
        current = await store.get("c1")
        assert current is not None and current.key == (
            "dashboard:chat-2" if recorded else "dashboard:chat-1"
        )
        assert current.retired is not recorded
        assert resume.resumed_session("c1") == "dashboard:chat-2"

    @pytest.mark.asyncio
    async def test_unlink_leaves_nothing_to_report(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await _attach(dispatcher, client)
        await dispatcher.handle_message(_message("!unlink"))
        await dispatcher.handle_message(_message("hello"))
        assert not any("Detached" in text for text, _ in client.sent)
        assert sessions.last_key == dispatcher._session_key("u1")

    @pytest.mark.parametrize("delivered", [True, False], ids=["sent", "send-failed"])
    @pytest.mark.asyncio
    async def test_a_moved_link_is_announced_and_adopted_only_once_delivered(
        self, delivered
    ) -> None:
        log = _log_with_titles("Launch plan", "Pricing review")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        before = await dispatcher._session_resume._expectations.get("c1")
        sessions.clear_mirror_links_at(link)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        sessions.last_key = ""
        client.send_fails = not delivered
        await dispatcher.handle_message(_message("carry on"))
        assert sessions.last_key == "", "the message ran in the swapped session unannounced"
        record = await dispatcher._session_resume._expectations.get("c1")
        if not delivered:
            assert record == before, "the adopt applied without the user being told"
            return
        assert "Now linked to" in client.sent[-1][0], client.sent[-1][0]
        assert record is not None and record.key == "dashboard:chat-1"
        await dispatcher.handle_message(_message("carry on"))
        assert sessions.last_key == "dashboard:chat-1"

    @pytest.mark.parametrize(
        "command, refused",
        [
            pytest.param("!compact", True, id="compact"),
            pytest.param("!stop", True, id="stop"),
            pytest.param("!link", True, id="link"),
            pytest.param("!sessions", False, id="sessions-recovers"),
            pytest.param("!new", False, id="new-leaves"),
            pytest.param("!unlink", False, id="unlink-leaves"),
            pytest.param("!help", False, id="help-inert"),
        ],
    )
    @pytest.mark.asyncio
    async def test_session_targeting_commands_are_refused_before_they_act(
        self, command, refused
    ) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = await _attach(dispatcher, client)
        sessions.clear_mirror_links_at(link)
        await dispatcher.handle_message(_message(command))
        got = [text for text, _ in client.sent if "Detached" in text]
        assert bool(got) is refused, f"{command} refusal={bool(got)}: {client.sent}"
        if refused:
            assert sessions.targeted == [], f"{command} reached a session anyway"

    @pytest.mark.parametrize("attached", [True, False], ids=["with-record", "no-record"])
    @pytest.mark.parametrize("leave_command", ["!new", "!unlink"])
    @pytest.mark.asyncio
    async def test_ambiguity_refuses_every_message_and_never_runs_natively(
        self, attached, leave_command
    ) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = (
            await _attach(dispatcher, client)
            if attached
            else ChannelLink(channel_type="discord", channel_id="c1")
        )
        for key in ("dashboard:chat-8", "dashboard:chat-9"):
            sessions.mirror_links[key] = link
            sessions.inbound_keys.add(key)
        assert dispatcher._session_resume.resolve_inbound("c1").ambiguous
        sessions.last_key = ""
        await dispatcher.handle_message(_message("first"))
        await dispatcher.handle_message(_message("!compact"))
        assert len([t for t, _ in client.sent if "Ambiguous link" in t]) == 2
        assert sessions.last_key == "", "a message ran while routing was denied"
        assert sessions.targeted == [], "!compact reached a session anyway"
        if attached:
            assert await dispatcher._session_resume._expectations.get("c1") is not None
        elsewhere = ChannelLink(channel_type="discord", channel_id="c2")
        sessions.mirror_links["dashboard:elsewhere"] = elsewhere
        await dispatcher.handle_message(_message(leave_command))
        assert not dispatcher._session_resume.resolve_inbound(
            "c1"
        ).ambiguous, f"{leave_command} left ambiguous bindings: {sessions.mirror_links}"
        assert sessions.mirror_links["dashboard:elsewhere"] == elsewhere, "cleared c2"
        await dispatcher.handle_message(_message("after leaving"))
        assert sessions.last_key == dispatcher._session_key("u1"), client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_dashboard_created_binding_is_tracked_from_its_first_message(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1")
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        dispatcher._session_resume.conv_log.metadata["dashboard:chat-1"] = {"title": "Launch plan"}
        await dispatcher.handle_message(_message("hello"))
        assert sessions.last_key == "dashboard:chat-1", "the bound session did not run"
        assert not any("Detached" in text for text, _ in client.sent)
        tracked = await dispatcher._session_resume._expectations.get("c1")
        assert tracked is not None and tracked.key == "dashboard:chat-1"
        assert tracked.title == "Launch plan"

    @pytest.mark.parametrize("delivered", [True, False], ids=["sent", "send-failed"])
    @pytest.mark.asyncio
    async def test_pre_notice_waiters_are_refused_before_settlement(
        self, delivered, monkeypatch
    ) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        store = dispatcher._session_resume._expectations
        await store.record("c1", "dashboard:chat-1", "Launch plan")
        client.send_fails = not delivered
        third_in_governance = asyncio.Event()
        release_third = asyncio.Event()
        governance_calls = 0

        async def _governance(_channel: str) -> bool:
            nonlocal governance_calls
            governance_calls += 1
            if governance_calls == 3:
                third_in_governance.set()
                await release_third.wait()
            return True

        monkeypatch.setitem(
            dispatcher.handle_message.__globals__,
            "channel_inbound_permitted",
            _governance,
        )
        siblings: list[asyncio.Task[None]] = []
        detached_sends = 0
        real_send = client.send_message

        async def _gate_waiters(channel_id: str, text: str, **kwargs):
            nonlocal detached_sends
            if "Detached" in text:
                detached_sends += 1
                if detached_sends == 1:
                    siblings.append(
                        asyncio.create_task(dispatcher.handle_message(_message("second")))
                    )
                    await asyncio.sleep(0)
                    assert len(dispatcher._routing_locks["c1"][1]) == 2
                    siblings.append(
                        asyncio.create_task(dispatcher.handle_message(_message("third")))
                    )
                    await asyncio.wait_for(third_in_governance.wait(), timeout=1)
                elif detached_sends == 2:
                    release_third.set()
            return await real_send(channel_id, text, **kwargs)

        client.send_message = _gate_waiters  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("first"))
        assert len(siblings) == 2
        await siblings[0]
        release_third.set()
        await siblings[1]
        assert detached_sends == 3, "a pre-notice waiter was not refused"
        record = await store.get("c1")
        assert sessions.last_key == "", "a pre-notice waiter ran natively"
        if delivered:
            assert len([t for t, _ in client.sent if "Detached" in t]) == 3
            assert record is not None and record.retired, "the last notice did not settle"
            await dispatcher.handle_message(_message("after"))
            assert sessions.last_key == dispatcher._session_key("u1")
        else:
            assert record is not None, "an undelivered notice consumed the record"
            assert sessions.last_key == "", "a message ran with nothing delivered"

    @pytest.mark.asyncio
    async def test_a_second_channel_is_not_blocked_by_the_first(self, monkeypatch) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        monkeypatch.setitem(
            dispatcher.handle_message.__globals__,
            "channel_inbound_permitted",
            client.is_thread_channel,
        )
        store = dispatcher._session_resume._expectations
        for channel in ("c1", "c2"):
            await store.record(channel, "dashboard:chat-1", "Launch plan")
        c2_routed = asyncio.Event()
        crossed: list[bool] = []
        real_route = dispatcher._session_resume.route
        real_send = client.send_message

        async def _signal_when_c2_routes(channel_id: str):
            decision = await real_route(channel_id)
            if channel_id == "c2":
                c2_routed.set()
            return decision

        async def _park_first_send_until_c2_routes(channel_id: str, text: str, **kwargs):
            if "Detached" in text and channel_id == "c1" and not crossed:
                try:
                    await asyncio.wait_for(c2_routed.wait(), timeout=10)
                    crossed.append(True)
                except asyncio.TimeoutError:
                    crossed.append(False)
                finally:
                    c2_routed.set()
            return await real_send(channel_id, text, **kwargs)

        dispatcher._session_resume.route = _signal_when_c2_routes  # type: ignore[method-assign]
        client.send_message = _park_first_send_until_c2_routes  # type: ignore[method-assign]
        await asyncio.gather(
            dispatcher.handle_message(_message("one", channel_id="c1")),
            dispatcher.handle_message(_message("two", channel_id="c2")),
        )
        assert crossed == [True], "c2 could not route while c1 held its own lock"
        assert len([t for t, _ in client.sent if "Detached" in t]) == 2
        assert sessions.last_key == "", "a refused message ran"


class TestExpectationStoreInvariants:

    @pytest.mark.asyncio
    async def test_the_store_is_gated_from_agent_tools_yet_writable_by_the_gateway(
        self, monkeypatch
    ) -> None:
        from kiro_crew.security import is_sensitive_path

        restricted: list[str] = []
        pc = resume_expectation.platform_compat
        monkeypatch.setattr(pc, "restrict_to_owner", restricted.append)
        store = resume_expectation.ResumeExpectations("discord")
        await store.record("c1", "dashboard:chat-1", "Launch plan")
        assert restricted, "the record was written without restricting it to its owner"
        path = store._loaded_from
        assert path is not None and is_sensitive_path(str(path)), (
            f"{path} is reachable by agent file tools -- an injected agent could "
            "delete the record and the binding together and route silently"
        )
        assert path.exists() and (await store.get("c1")) is not None

    # Read inbound and written from the picker, both ON the loop, so the recorded
    # threads are the real filesystem helpers' -- not one mocked call.
    @pytest.mark.asyncio
    async def test_every_store_filesystem_helper_runs_off_the_loop_thread(
        self, monkeypatch
    ) -> None:
        threads: list[int] = []
        for name in ("_resolve", "_write"):
            real = getattr(resume_expectation, name)

            def _record(*args, _real=real, **kwargs):
                threads.append(threading.get_ident())
                return _real(*args, **kwargs)

            monkeypatch.setattr(resume_expectation, name, _record)
        store = resume_expectation.ResumeExpectations("discord")
        loop_thread = threading.get_ident()
        await store.record("c1", "dashboard:chat-1", "Launch plan")
        current = await store.get("c1")
        assert current is not None
        assert await store.retire_if("c1", current.version) is True
        assert await store.retire_if("c1", current.version) is False
        assert threads, "no filesystem helper ran at all"
        assert loop_thread not in threads, f"store I/O ran on the loop: {threads}"

    def test_the_store_exposes_no_synchronous_accessor(self) -> None:
        cls = resume_expectation.ResumeExpectations
        public = [n for n in vars(cls) if not n.startswith("_")]
        assert public, "the store lost its public surface"
        for name in public:
            assert asyncio.iscoroutinefunction(getattr(cls, name)), f"{name} is sync"

    @pytest.mark.asyncio
    async def test_concurrent_records_do_not_lose_each_other(self) -> None:
        store = resume_expectation.ResumeExpectations("discord")
        await asyncio.gather(
            store.record("c1", "dashboard:chat-1", "One"),
            store.record("c2", "dashboard:chat-2", "Two"),
        )
        reloaded = resume_expectation.ResumeExpectations("discord")
        for channel, key in (("c1", "dashboard:chat-1"), ("c2", "dashboard:chat-2")):
            record = await reloaded.get(channel)
            assert record is not None and record.key == key, f"{channel} was lost"


class TestRoutingUnderConcurrentRebinding:

    @pytest.mark.asyncio
    async def test_an_unlink_landing_inside_the_store_read_never_falls_native(self) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = await _attach(dispatcher, client)
        native = dispatcher._session_key("u1")
        sessions.last_key = ""
        store = dispatcher._session_resume._expectations
        real_get = store.get

        async def _unlink_during_get(channel_id: str):
            record = await real_get(channel_id)
            # The dashboard releasing the conversation mid-decision.
            sessions.clear_mirror_links_at(link)
            return record

        store.get = _unlink_during_get  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("what did we decide?"))
        assert sessions.last_key != native, "the message ran natively with no warning"
        assert sessions.last_key == "", f"the message ran at all: {sessions.last_key}"
        assert "Detached" in client.sent[-1][0], client.sent[-1][0]

    @pytest.mark.parametrize(
        "notice, rebind_key, rebind_title, survivor",
        [
            pytest.param(
                "Detached",
                "dashboard:chat-1",
                "Pricing review",
                "dashboard:chat-1",
                id="picker-bind-vs-stale-clear",
            ),
            pytest.param(
                "Now linked to",
                "dashboard:chat-2",
                "Budget",
                "dashboard:chat-2",
                id="third-rebind-vs-stale-adopt",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_record_written_during_the_send_survives_the_stale_settle(
        self, notice, rebind_key, rebind_title, survivor
    ) -> None:
        log = _log_with_titles("Launch plan", "Pricing review", "Budget")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        sessions.clear_mirror_links_at(link)
        if notice == "Now linked to":
            # An owner must be live for the decision to be a hotswap rather than a loss.
            sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        fired: list[str] = []
        real_send = client.send_message

        async def _rebind_inside_send(channel_id: str, text: str, **kwargs):
            if notice in text and not fired:
                fired.append(text)
                await dispatcher._session_resume._expectations.record(
                    "c1", rebind_key, rebind_title
                )
            return await real_send(channel_id, text, **kwargs)

        client.send_message = _rebind_inside_send  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("what did we decide?"))
        assert fired, "the race never fired -- the test proves nothing"
        current = await dispatcher._session_resume._expectations.get("c1")
        assert (
            current is not None and current.key == survivor and not current.retired
        ), "the stale settle overwrote the record written during the send"

    @pytest.mark.parametrize(
        "text",
        ["hello", "!compact", "!stop", "!link"],
        ids=["message", "compact", "stop", "link"],
    )
    @pytest.mark.asyncio
    async def test_nothing_re_resolves_the_binding_after_the_decision(self, text) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await _attach(dispatcher, client)
        resume = dispatcher._session_resume
        real_route = resume.route

        def _forbidden(channel_id: str):
            raise AssertionError("the binding was re-resolved after the decision")

        async def _route_then_seal(channel_id: str):
            decision = await real_route(channel_id)
            resume.resolve_inbound = _forbidden  # type: ignore[method-assign]
            return decision

        resume.route = _route_then_seal  # type: ignore[method-assign]
        await dispatcher.handle_message(_message(text))
        targeted = [key for _verb, key in sessions.targeted]
        assert all(
            k == "dashboard:chat-1" for k in targeted
        ), f"{text} acted on {targeted} instead of the decided session"
        if text == "hello":
            assert sessions.last_key == "dashboard:chat-1"

    @pytest.mark.parametrize("rebound", [False, True], ids=["unlink", "rebind"])
    @pytest.mark.asyncio
    async def test_a_bootstrap_owner_that_moves_mid_decision_is_refused(self, rebound) -> None:
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1")
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        resume = dispatcher._session_resume

        async def _move_during_title(session_key: str) -> str:
            # The dashboard moves the conversation while the decision reads the title.
            sessions.clear_mirror_links_at(link)
            if rebound:
                sessions.set_mirror_link("dashboard:chat-9", link, accepts_inbound=True)
            return "Launch plan"

        resume._title_of = _move_during_title  # type: ignore[method-assign]
        await dispatcher.handle_message(_message("what did we decide?"))
        assert sessions.last_key == "", f"the message ran at all: {sessions.last_key}"
        assert "changing right now" in client.sent[-1][0], client.sent[-1][0]
        assert await resume._expectations.get("c1") is None, "the moved owner was recorded"


class TestPersistenceFailureIsFailClosed:

    @staticmethod
    def _break_writes(monkeypatch) -> dict:
        broken = {"fail": True}
        real_write = resume_expectation.atomic_write

        def _enospc(*args, **kwargs):
            if broken["fail"]:
                raise OSError(28, "No space left on device")
            return real_write(*args, **kwargs)

        monkeypatch.setattr(resume_expectation, "atomic_write", _enospc)
        return broken

    @pytest.mark.asyncio
    async def test_the_store_raises_instead_of_reporting_a_lost_write(self, monkeypatch) -> None:
        store = resume_expectation.ResumeExpectations("discord")
        self._break_writes(monkeypatch)
        with pytest.raises(resume_expectation.ExpectationStoreError):
            await store.record("c1", "dashboard:chat-0", "Launch plan")
        assert await store.get("c1") is None, "in-memory state kept an undurable record"

    @pytest.mark.parametrize("side", ["write", "read"])
    @pytest.mark.asyncio
    async def test_a_store_it_cannot_trust_refuses_until_repaired(self, side, monkeypatch) -> None:
        """Read and write failures refuse until the store is repaired."""
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        link = ChannelLink(channel_type="discord", channel_id="c1")
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        broken = self._break_writes(monkeypatch) if side == "write" else None
        if broken is None:
            _store_path().write_text('{"c1": {"key": "dash', encoding="utf-8")

        await dispatcher.handle_message(_message("hello"))
        assert sessions.last_key == "", "the turn ran on a store it could not trust"
        assert "Can't read or save" in client.sent[-1][0], client.sent[-1][0]
        _store_path().unlink() if broken is None else broken.update(fail=False)
        await dispatcher.handle_message(_message("hello again"))
        assert sessions.last_key == "dashboard:chat-1", "no recovery after repair"

    @pytest.mark.parametrize("mode", ["alone", "concurrent-bind", "claim-lost"])
    @pytest.mark.asyncio
    async def test_a_pick_that_cannot_take_effect_binds_nothing(self, mode, monkeypatch) -> None:
        """A failed pick binds nothing and never rolls back a concurrent bind."""
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        await dispatcher.handle_message(_message("!sessions"))
        custom_id, message_id = _picker_button(client)
        target = ChannelLink(channel_type="discord", channel_id="c1")
        fired: list[str] = []

        async def _fail_the_write(channel_id, key, title):
            if mode == "concurrent-bind":  # the dashboard completing the same bind
                sessions.set_mirror_link(key, target, accepts_inbound=True)
            fired.append(key)
            raise resume_expectation.ExpectationStoreError("no space left on device")

        def _lose_the_claim(key, link, *, accepts_inbound=False):
            fired.append(key)
            raise ConversationOwnershipConflict("claimed while we were writing")

        if mode == "claim-lost":
            monkeypatch.setattr(sessions, "set_mirror_link", _lose_the_claim)
        else:
            monkeypatch.setattr(dispatcher._session_resume._expectations, "record", _fail_the_write)

        await dispatcher.on_interaction(_interaction(custom_id, message_id))
        assert fired, "the pick never reached the failure -- the test proves nothing"
        assert sessions.find_mirror_sessions(target, inbound_only=True) == (
            [fired[0]] if mode == "concurrent-bind" else []
        ), "a bind outlived an unpersisted record, or a rollback erased a live one"
        edits = [text for _mid, text, _c in client.edits]
        if mode == "claim-lost":
            assert any("Another session just connected" in t for t in edits), edits
            assert not any(
                "Couldn't save" in t for t in edits
            ), f"a claim race was reported as a storage failure: {edits}"
            return
        assert any("NOT resumed" in t for t in edits), edits

    @pytest.mark.asyncio
    async def test_a_settle_that_cannot_persist_refuses_again(self, monkeypatch) -> None:
        """An unpersisted settle is not done, so the refusal is still owed."""
        dispatcher, client, sessions = _dispatcher({"u1"}, _log("Launch plan"))
        sessions.clear_mirror_links_at(await _attach(dispatcher, client))
        self._break_writes(monkeypatch)

        await dispatcher.handle_message(_message("what did we decide?"))
        await dispatcher.handle_message(_message("again"))
        assert (
            len([t for t, _ in client.sent if "Detached" in t]) == 2
        ), "the unpersisted settle was treated as done"
        assert sessions.last_key == "", "a refused message ran"

    @pytest.mark.parametrize(
        "payload, loads",
        [
            pytest.param(
                '{"c1": {"key": "dashboard:chat-0", "title": "T", "version": 3}}',
                True,
                id="well-formed-row",
            ),
            pytest.param(
                '{"c1": {"key": "dashboard:chat-0", "title": "T"}}', False, id="row-missing-version"
            ),
            pytest.param("{not json", False, id="malformed-json"),
            pytest.param('["c1"]', False, id="wrong-toplevel-shape"),
            pytest.param('{"c1": {"title": "T"}}', False, id="row-missing-key"),
            pytest.param('{"c1": {"key": "k", "version": "x"}}', False, id="non-numeric-version"),
            pytest.param('{"c1": {"key": "k", "version": true}}', False, id="boolean-version"),
            pytest.param(
                '{"c1": {"key": "k", "version": 1, "retired": "yes"}}',
                False,
                id="non-boolean-retired",
            ),
            pytest.param('{"c1": {"key": "k", "version": "1"}}', False, id="string-version"),
            pytest.param('{"c1": {"key": "k", "version": 1.25}}', False, id="fractional-version"),
            pytest.param(b"\xff\xfe{}", False, id="invalid-utf8-bytes"),
            pytest.param('{"c1": {"key": "k", "version": 1e999}}', False, id="infinite-version"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_store_that_cannot_be_trusted_is_not_an_empty_store(
        self, payload, loads
    ) -> None:
        """Corruption read as "no records" says never-attached, so the turn runs
        natively -- this bug's fail-open form."""
        path = _store_path()
        path.write_bytes(payload) if isinstance(payload, bytes) else path.write_text(payload)
        store = resume_expectation.ResumeExpectations("discord")
        if loads:
            record = await store.get("c1")
            assert record is not None and record.version == 3
            return
        with pytest.raises(resume_expectation.ExpectationStoreError):
            await store.get("c1")


class TestSettlementReconcilesLiveState:
    """Settlement reconciles dashboard rebinds that do not bump store versions."""

    @pytest.mark.parametrize(
        "rebind_keys, expect_second_runs",
        [
            pytest.param(["dashboard:chat-1"], False, id="different-key"),
            pytest.param(["dashboard:chat-0"], True, id="same-key"),
            pytest.param([], True, id="absent"),
            pytest.param(["dashboard:chat-1", "dashboard:chat-2"], False, id="ambiguous"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_owner_arriving_during_the_send_is_never_entered_silently(
        self, rebind_keys, expect_second_runs
    ) -> None:
        """Barrier-controlled: the rebind lands strictly inside the notice send."""
        log = _log_with_titles("Launch plan", "Pricing review", "Budget")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        sessions.clear_mirror_links_at(link)

        async def _rebind():
            for key in rebind_keys:
                sessions.mirror_links[key] = link
                sessions.inbound_keys.add(key)

        released = _gate_send(client, "Detached", _rebind)
        await dispatcher.handle_message(_message("what did we decide?"))
        assert released, "the interleaving never fired -- the test proves nothing"
        sessions.last_key = ""
        await dispatcher.handle_message(_message("resending this"))
        if expect_second_runs:
            assert sessions.last_key != "", "the resend was refused with nothing pending"
        else:
            assert (
                sessions.last_key == ""
            ), f"the resend entered {sessions.last_key} without a notice"
            assert "NOT processed" in client.sent[-1][0], client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_an_adopt_whose_owner_moved_again_adopts_nothing(self) -> None:
        """The notice named B; if the owner is C by the time it lands, adopting B
        records a link the user was never in."""
        log = _log_with_titles("Launch plan", "Pricing review", "Budget")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        before = await dispatcher._session_resume._expectations.get("c1")
        assert before is not None and before.key == "dashboard:chat-0"
        sessions.clear_mirror_links_at(link)
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        async def _move_again():
            sessions.clear_mirror_links_at(link)
            sessions.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        moved = _gate_send(client, "Now linked to", _move_again)
        await dispatcher.handle_message(_message("carry on"))
        assert moved, "the race never fired -- the test proves nothing"
        assert (
            await dispatcher._session_resume._expectations.get("c1") == before
        ), "adopted a session whose notice no longer described the live state"
        sessions.last_key = ""
        await dispatcher.handle_message(_message("resending"))
        assert sessions.last_key == "", "the resend entered the third owner in silence"
        assert (
            "Launch plan" in client.sent[-1][0]
        ), f"the notice named a link the user was never in: {client.sent[-1][0]}"

    @pytest.mark.parametrize("mode", ["bare", "same-key", "recorded", "adopt-fails"])
    @pytest.mark.asyncio
    async def test_an_owner_arriving_during_retirement_keeps_durable_evidence(
        self, mode: str, monkeypatch
    ) -> None:
        log = _log_with_titles("Launch plan", "Pricing review")
        dispatcher, client, sessions = _dispatcher({"u1"}, log)
        link = await _attach(dispatcher, client)
        sessions.clear_mirror_links_at(link)
        store = dispatcher._session_resume._expectations
        real_aflush = sessions.aflush
        raced: list[str] = []

        async def _rebind_during_aflush() -> None:
            await real_aflush()
            rebind_key = "dashboard:chat-0" if mode == "same-key" else "dashboard:chat-1"
            sessions.mirror_links[rebind_key] = link
            sessions.inbound_keys.add(rebind_key)
            if mode == "recorded":
                await store.record("c1", "dashboard:chat-1", "Pricing review")
            raced.append("c1")

        sessions.aflush = _rebind_during_aflush  # type: ignore[method-assign]
        if mode == "adopt-fails":
            real_write = resume_expectation.atomic_write
            writes = {"count": 0}

            def _fail_second_write(*args, **kwargs):
                writes["count"] += 1
                if writes["count"] == 2:
                    raise OSError(28, "No space left on device")
                return real_write(*args, **kwargs)

            monkeypatch.setattr(resume_expectation, "atomic_write", _fail_second_write)
        await dispatcher.handle_message(_message("what did we decide?"))
        assert raced, "the rebind did not land inside aflush"
        current = await store.get("c1")
        assert current is not None, "retirement deleted the evidence beside a live owner"
        if mode == "recorded":
            assert current.key == "dashboard:chat-1" and not current.retired
            return
        assert current.key == "dashboard:chat-0" and current.retired
        sessions.last_key = ""
        await dispatcher.handle_message(_message("resending this"))
        assert sessions.last_key == "", "the resend entered the new owner in silence"
        assert "Now linked to" in client.sent[-1][0], client.sent[-1][0]
        current = await store.get("c1")
        assert current is not None and current.retired is (mode == "adopt-fails")
