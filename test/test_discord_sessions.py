from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.discord.client import DiscordInteraction
from kiro_crew.discord.commands import parse_command
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import InboundMessage


class _Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[str, str, Any]] = []
        self.acked: list[str] = []
        self._mid = 100

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        components: Any = None,
        reply_to_message_id: Any = None,
    ) -> str:
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
    supports_steer = False

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
    def __init__(self) -> None:
        self.mirror_links: dict[str, ChannelLink] = {}
        self.inbound_keys: set[str] = set()
        self.last_key = ""
        self.provider = _Provider()

    def set_mirror_link(
        self,
        key: str,
        link: ChannelLink,
        *,
        accepts_inbound: bool = False,
    ) -> None:
        self.mirror_links[key] = link
        if accepts_inbound:
            self.inbound_keys.add(key)
        else:
            self.inbound_keys.discard(key)

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

    def clear_mirror_link(self, key: str) -> bool:
        self.inbound_keys.discard(key)
        return self.mirror_links.pop(key, None) is not None

    def max_generation(self, bucket: str) -> int:
        return -1

    def is_busy(self, key: str) -> bool:
        return False

    async def get_or_create(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]:
        self.last_key = key
        self.last_agent = kwargs.get("agent")
        return self.provider, getattr(self, "is_new_result", False), True

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

    def get_metadata(self, key: str) -> dict:
        return self.metadata.get(key, {})

    def list_sessions(self) -> list[dict]:
        self.list_calls += 1
        return list(self.rows)

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

    def append(self, key: str, role: str, content: str) -> None:
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
        return text, None


def _config() -> Any:
    return SimpleNamespace(
        discord=SimpleNamespace(soft_threshold_pct=80),
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


def _interaction(custom_id: str, message_id: str, channel_id: str = "c1") -> DiscordInteraction:
    return DiscordInteraction(
        interaction_id="i1",
        interaction_token="tok",
        channel_id=channel_id,
        user_id="u1",
        message_id=message_id,
        custom_id=custom_id,
        label="",
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


def test_sessions_command_aliases() -> None:
    assert parse_command("!sessions") == "sessions"
    assert parse_command("/sessions") == "sessions"


@pytest.mark.asyncio
async def test_sessions_requires_exactly_one_allowed_user() -> None:
    log = _log()
    dispatcher, client, _ = _dispatcher({"u1", "u2"}, log)

    await dispatcher.handle_message(_message("!sessions"))

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
    visible = "\n".join(
        [text for text, _ in client.sent] + [text for _, text, _ in client.edits]
    )
    assert "Resumed: Launch plan" in visible
    assert "omitted oldest" not in visible
    assert secret not in visible
    assert "@everyone" not in visible

    await dispatcher.handle_message(_message("continue here"))
    assert sessions.last_key == "dashboard:chat-1"


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


@pytest.mark.asyncio
async def test_own_session_still_gets_new_session_bookkeeping() -> None:
    """The guard must not disable bookkeeping for Discord's OWN conversation."""
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    sessions.is_new_result = True

    await dispatcher.handle_message(_message("hello there"))

    assert [key for key, _ in getattr(sessions, "set_channel_calls", [])] == [
        sessions.last_key
    ]
    assert [key for key, _ in getattr(log, "titles_set", [])] == [sessions.last_key]


@pytest.mark.asyncio
async def test_picker_refuses_outside_a_dm() -> None:
    """`!sessions` must not list or replay private history into a shared thread.

    The owner gate answers WHO may resume, not WHERE the result is shown: in a
    guild thread the picker's session titles and the 5-message replay would be
    readable by every member of that thread.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())

    await dispatcher.handle_message(_message("!sessions", thread_id="t9"))

    assert dispatcher._session_pickers == {}
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


@pytest.mark.asyncio
async def test_stale_picker_fails_closed() -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    for picker in dispatcher._session_pickers.values():
        picker.created_at -= 301

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

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
