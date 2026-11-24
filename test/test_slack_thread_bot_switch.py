"""``!bot`` puts one Slack thread on a Crew Bot; ``/kirocrew bot`` lists the Bots.

A Bot is a ``config.agents`` record. ``slack.channels.<id>.agent`` already runs a whole
channel as one (``slack/channel_crew.py``). ``!bot <name>`` does the same for a single
thread: owner-only, stored as the thread's agent like ``!ta``, so the next turn starts
the session as the record (mode, ``crew_agent``, workspace) and other threads do not
move. Slack runs no slash command inside a thread, so ``/kirocrew bot`` only lists.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_transport_dispatch_helpers import (
    _MSG_TS,
    FakeSessions,
    RecordingSlackClient,
    ScriptedProvider,
    make_event,
)

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.config import live, loader
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig, WorkspaceConfig
from kiro_crew.memory_stores import persist_member_config, provision_member_memory
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack import channel_crew, events_slash
from kiro_crew.slack import handler as _handler
from kiro_crew.slack import transport_dispatch

_OWNER = "U_OWNER"
_SHARED = "U_SHARED"
_KEY = canonical_key(_MSG_TS)
_OTHER_TS = "1700000500.000100"
_OTHER_KEY = canonical_key(_OTHER_TS)


@pytest.fixture()
def bots(monkeypatch, tmp_path):
    """Two Crew Bots on their own workspaces."""
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    trees = {}
    for name in ("web-frontend", "research"):
        trees[name] = tmp_path / name
        trees[name].mkdir()
        cfg.workspaces[name] = WorkspaceConfig(dir=str(trees[name]))
    cfg.agents["web-frontend"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="web-frontend"
    )
    cfg.agents["research"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew-research", workspace="research", display_name="Research Bot"
    )
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    return trees


@pytest.fixture()
def audit(monkeypatch):
    monkeypatch.setattr(_handler, "_owner_id", _OWNER)
    monkeypatch.setattr(_handler, "_hydrated_sessions", set())
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    recorder = MagicMock()
    monkeypatch.setattr(_handler, "sel", lambda: recorder)
    return recorder


class _Sessions(FakeSessions):
    """Records the keyword arguments of every turn acquisition, and the removed keys."""

    def __init__(self):
        super().__init__(
            ScriptedProvider(
                [
                    make_event(EVENT_TEXT_CHUNK, text="hi"),
                    make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            )
        )
        self.calls: list[tuple[str, dict]] = []
        self.removed: list[str] = []
        self.live: set[str] = set()
        self.linked_owner: str | None = None

    def get_session_for_thread(self, thread_ts):
        return self.linked_owner

    def get_provider(self, session_key):
        return "acp" if session_key in self.live else None

    def resumable_sid(self, session_key):
        return None

    async def get_or_create(self, session_key, **kwargs):
        from kiro_crew.session import BACKGROUND_KEY

        if session_key != BACKGROUND_KEY:
            self.calls.append((session_key, kwargs))
            self.live.add(session_key)
        return await super().get_or_create(
            session_key, agent=kwargs.get("agent"), channel_id=kwargs.get("channel_id")
        )

    async def remove(self, session_key):
        self.removed.append(session_key)
        self.live.discard(session_key)


async def _send(slack, sessions, text, *, user_id=_OWNER, thread_ts=None, msg_ts=_MSG_TS, **kw):
    await transport_dispatch.handle_message_transport(
        slack=slack,
        sessions=sessions,
        channel="D1",
        text=text,
        thread_ts=thread_ts,
        msg_ts=msg_ts,
        user_id=user_id,
        context_builder=None,
        **{"conversation_log": None, **kw},
    )


def _posts(slack) -> list[str]:
    return [kw["text"] for (method, kw) in slack.transcript if method == "post_message"]


def _run(*messages, **kw):
    """Send each ``(text, user_id, thread_ts, msg_ts)`` in turn; return what came of it."""
    slack, sessions = RecordingSlackClient(), _Sessions()

    async def _scenario():
        for text, user_id, thread_ts, msg_ts in messages:
            await _send(
                slack, sessions, text, user_id=user_id, thread_ts=thread_ts, msg_ts=msg_ts, **kw
            )

    asyncio.run(_scenario())
    return slack, sessions


def _first(text, user_id=_OWNER):
    return (text, user_id, None, _MSG_TS)


def _reply(text, user_id=_OWNER):
    return (text, user_id, _MSG_TS, "1700000000.000200")


class TestBotSwitch:
    def test_the_next_reply_runs_as_the_bot(self, bots, audit):
        slack, sessions = _run(_first("!bot web-frontend"), _reply("hello"))

        assert sessions.removed == [_KEY]
        assert _handler._thread_agents[_KEY] == "web-frontend"
        [(key, kw)] = sessions.calls
        assert key == _KEY
        assert kw["agent"] == "kirocrew"
        assert kw["crew_agent"] == "web-frontend"
        assert kw["cwd"] == str(bots["web-frontend"])
        assert any("This thread now runs as Bot *web-frontend*" in p for p in _posts(slack))

    def test_a_display_name_or_other_case_names_the_bot(self, bots, audit):
        _slack, sessions = _run(_first("!bot research bot"), _reply("hello"))
        assert sessions.calls[0][1]["crew_agent"] == "research"

        _slack, sessions = _run(_first("!bot WEB-Frontend"), _reply("hello"))
        assert sessions.calls[-1][1]["crew_agent"] == "web-frontend"

    def test_a_bot_key_with_a_space_is_kept_whole(self, bots, audit):
        cfg = live.snapshot()
        assert cfg is not None
        cfg.agents["Code Review"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="web-frontend"
        )
        _slack, sessions = _run(_first("!bot Code Review"), _reply("hello"))

        assert _handler._thread_agents[_KEY] == "Code Review"
        assert sessions.calls[0][1]["crew_agent"] == "Code Review"

    def test_another_thread_is_unchanged(self, bots, audit):
        _slack, sessions = _run(
            _first("!bot web-frontend"),
            _reply("hello"),
            ("hi there", _OWNER, None, _OTHER_TS),
        )

        by_key = dict(sessions.calls)
        assert by_key[_KEY]["crew_agent"] == "web-frontend"
        assert by_key[_OTHER_KEY] == {
            "agent": "kirocrew",
            "channel_id": "D1",
            "start_priority": by_key[_OTHER_KEY]["start_priority"],
        }
        assert _OTHER_KEY not in _handler._thread_agents

    def test_switching_to_another_bot_in_the_same_thread(self, bots, audit):
        _slack, sessions = _run(
            _first("!bot web-frontend"),
            _reply("!bot research"),
            _reply("hello"),
        )

        assert sessions.removed == [_KEY, _KEY]
        [(_key, kw)] = sessions.calls
        assert kw["crew_agent"] == "research"
        assert kw["agent"] == "kirocrew-research"
        assert kw["cwd"] == str(bots["research"])

    def test_the_thread_bot_beats_the_channels_pin(self, bots, audit):
        _slack, sessions = _run(
            _first(
                "!bot research",
            ),
            _reply("hello"),
            agent_override="web-frontend",
        )

        assert sessions.calls[0][1]["crew_agent"] == "research"

    def test_off_goes_back_to_the_channel_agent(self, bots, audit):
        _slack, sessions = _run(
            _first("!bot research"),
            _reply("!bot off"),
            _reply("hello"),
            agent_override="web-frontend",
        )

        assert _KEY not in _handler._thread_agents
        [(_key, kw)] = sessions.calls
        assert kw["crew_agent"] == "web-frontend"

    def test_a_bot_named_default_is_a_bot_not_a_reset(self, bots, audit, monkeypatch):
        # A stock config's ``default`` record is a Crew Bot; ``!ta default`` resets, ``!bot`` does not.
        cfg = live.snapshot()
        assert cfg is not None
        cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="research")
        _slack, sessions = _run(_first("!bot default"), _reply("hello"))

        assert _handler._thread_agents[_KEY] == "default"
        assert sessions.calls[0][1]["crew_agent"] == "default"
        assert sessions.calls[0][1]["cwd"] == str(bots["research"])

    def test_a_channel_pin_alone_still_runs_the_bot(self, bots, audit):
        # The channel-pin path is untouched.
        _slack, sessions = _run(_first("hello"), agent_override="research")
        assert sessions.calls[0][1]["crew_agent"] == "research"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """A Bot with its own private (v2) memory store, in the config on disk."""
    home = tmp_path / "host-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    cfg.agents["vault"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="default")
    store = provision_member_memory(cfg, "vault")
    persist_member_config(cfg, "vault", create=True)
    loader._invalidate_config_cache()
    live_cfg = KiroCrewConfig.load()
    monkeypatch.setattr(live, "snapshot", lambda: live_cfg)
    return store


class TestBotMemory:
    """A Bot with private memory gets that store as the thread's memory, not Global."""

    def test_the_threads_memory_is_the_bots_store_and_survives_a_restart(self, vault, audit):
        from kiro_crew.context import store_of_session
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        _slack, sessions = _run(_first("!bot vault"), _reply("hello"), conversation_log=log)

        assert store_of_session(log, _KEY) == vault
        assert sessions.calls[0][1]["crew_agent"] == "vault"
        # A restart forgets the in-memory thread agent; the transcript brings the Bot back.
        from kiro_crew.slack.handler_runtime.inbound import _read_thread_overrides

        agent, _project = _read_thread_overrides(_KEY, log)
        assert agent == "vault"

    def test_a_thread_with_history_cannot_take_a_private_bot(self, vault, audit, monkeypatch):
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(ConversationLog, "has_messages", lambda self, key: True)
        slack, sessions = _run(_first("!bot vault"))

        assert any(p.startswith("❌") for p in _posts(slack)), _posts(slack)
        assert _KEY not in _handler._thread_agents
        assert sessions.removed == []

    def test_a_bound_thread_cannot_move_to_another_bot_or_reset(self, vault, bots, audit):
        slack, sessions = _run(_first("!bot vault"), _reply("!bot research"), _reply("!bot off"))

        posts = _posts(slack)
        assert posts[1].startswith("❌ This thread's memory belongs to Bot 'vault'"), posts
        assert posts[2].startswith("❌ This thread's memory belongs to Bot 'vault'"), posts
        assert _handler._thread_agents[_KEY] == "vault"

    @pytest.mark.parametrize(
        "moves",
        [
            ["!ta off"],
            ["!ta reviewer"],
            ["/agent reviewer"],
            ["!bot off", "!ta off"],
            ["!ta reviewer", "!bot off"],
            ["!bot research", "!ta reviewer", "!ta off"],
        ],
    )
    @pytest.mark.parametrize("restart", [False, True])
    def test_no_order_of_switches_moves_a_bound_thread_off_its_store(
        self, vault, bots, audit, monkeypatch, moves, restart
    ):
        from kiro_crew.context import store_of_session
        from kiro_crew.history import ConversationLog
        from kiro_crew.slack import thread_override_slots
        from kiro_crew.slack.handler_runtime.inbound import _read_thread_overrides

        monkeypatch.setattr(_handler, "_resolve_agent_name", lambda name, project=None: name)
        log = ConversationLog()
        _run(_first("!bot vault"), conversation_log=log)
        if restart:
            # A restart forgets every in-memory choice; the transcript and record remain.
            _handler._thread_agents.clear()
            _handler._hydrated_sessions.clear()
            thread_override_slots.unpin(_KEY, "agent")
            # The real hydration, which a fresh process runs on the thread's next message.
            monkeypatch.setattr(
                transport_dispatch, "_hydrate_thread_overrides", _handler._hydrate_thread_overrides
            )
        slack, sessions = _run(*(_reply(m) for m in moves), _reply("hello"), conversation_log=log)

        refusals = [p for p in _posts(slack) if p.startswith("❌ This thread's memory belongs")]
        assert len(refusals) == len(moves), _posts(slack)
        assert store_of_session(log, _KEY) == vault
        assert _read_thread_overrides(_KEY, log)[0] == "vault"
        assert sessions.calls[-1][1].get("crew_agent") == "vault"
        denied = [
            c.kwargs
            for c in audit.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "agent_denied_memory_binding"
        ]
        assert len(denied) == len(moves), denied
        assert all(d["metadata"]["user"] == _OWNER and d["metadata"]["reason"] for d in denied)

    def test_naming_the_same_bot_again_is_allowed(self, vault, audit):
        slack, _sessions = _run(_first("!bot vault"), _reply("!bot vault"))
        assert not any(p.startswith("❌") for p in _posts(slack)), _posts(slack)

    def test_a_global_memory_bot_replaces_an_earlier_selection(self, bots, audit):
        from kiro_crew.config.loader import resolve_agent_bindings
        from kiro_crew.dashboard.chat_handlers import _record_explicit_agent_selection
        from kiro_crew.history import ConversationLog
        from kiro_crew.slack.handler_runtime.inbound import _read_thread_overrides

        cfg = KiroCrewConfig.load()
        log = ConversationLog()
        # A template the dashboard picker recorded earlier on this thread.
        template = resolve_agent_bindings(
            cfg, "kirocrew", None, validate_memory_files=False, selection_kind="template"
        )
        asyncio.run(_record_explicit_agent_selection(_KEY, "kirocrew", template, config=cfg))
        log.update_metadata(_KEY, {"agent": "kirocrew"})
        assert _read_thread_overrides(_KEY, log)[0] == "kirocrew"

        _run(_first("!bot web-frontend"), _reply("hello"), conversation_log=log)

        assert _read_thread_overrides(_KEY, log)[0] == "web-frontend"

    def test_a_bot_on_a_legacy_named_store_reads_that_store(self, bots, audit, monkeypatch):
        from kiro_crew.config.sections import MemoryStoreConfig
        from kiro_crew.context import store_of_session
        from kiro_crew.history import ConversationLog

        cfg = live.snapshot()
        assert cfg is not None
        cfg.memory_stores["archive"] = MemoryStoreConfig()
        cfg.agents["research"].memory_store = "archive"
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
        _run(_first("!bot research"), _reply("hello"))

        assert store_of_session(ConversationLog(), _KEY) == "archive"

    def test_a_refused_override_slot_publishes_no_memory_binding(self, vault, audit, monkeypatch):
        from kiro_crew.execution_context import read_session_execution
        from kiro_crew.slack import thread_override_slots

        monkeypatch.setattr(thread_override_slots, "pin", lambda *a, **k: False)
        slack, sessions = _run(_first("!bot vault"))

        assert read_session_execution(_KEY) is None
        assert _KEY not in _handler._thread_agents
        assert sessions.removed == []
        assert thread_override_slots.refusal_text() in _posts(slack)

    def test_a_refused_binding_gives_the_override_slot_back(self, vault, audit, monkeypatch):
        from kiro_crew.history import ConversationLog
        from kiro_crew.slack import thread_override_slots

        monkeypatch.setattr(ConversationLog, "has_messages", lambda self, key: True)
        _run(_first("!bot vault"))

        assert not thread_override_slots.kind_is_pinned(_KEY, "agent")

    def test_another_thread_keeps_global_memory(self, vault, audit):
        from kiro_crew.context import store_of_session
        from kiro_crew.history import ConversationLog

        _run(_first("!bot vault"), ("hi", _OWNER, None, _OTHER_TS))
        assert store_of_session(ConversationLog(), _OTHER_KEY) == ""


class TestLatestChoiceWinsAfterRestart:
    """The latest of ``!bot <name>``, ``!bot off`` and ``!ta <name>`` is what a restart reads back."""

    @staticmethod
    def _after_restart(log):
        from kiro_crew.slack.handler_runtime.inbound import _read_thread_overrides

        _handler._thread_agents.clear()
        return _read_thread_overrides(_KEY, log)[0]

    def _run_commands(self, bots, monkeypatch, *commands):
        from kiro_crew.history import ConversationLog

        cfg = live.snapshot()
        assert cfg is not None
        cfg.agents["docs"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="research")
        monkeypatch.setattr(_handler, "_resolve_agent_name", lambda name, project=None: name)
        log = ConversationLog()
        _run(_first(commands[0]), *(_reply(c) for c in commands[1:]), conversation_log=log)
        return log

    def test_bot_then_another_bot(self, bots, audit, monkeypatch):
        log = self._run_commands(bots, monkeypatch, "!bot web-frontend", "!bot docs")
        assert self._after_restart(log) == "docs"

    def test_bot_then_off(self, bots, audit, monkeypatch):
        log = self._run_commands(bots, monkeypatch, "!bot web-frontend", "!bot off")
        assert self._after_restart(log) == ""

    def test_a_global_memory_bot_leaves_no_record_to_outlive_a_reset(
        self, bots, audit, monkeypatch
    ):
        from kiro_crew.execution_context import read_session_execution

        self._run_commands(bots, monkeypatch, "!bot web-frontend", "!bot off", "!ta reviewer")
        assert read_session_execution(_KEY) is None

    def test_bot_then_ta(self, bots, audit, monkeypatch):
        log = self._run_commands(bots, monkeypatch, "!bot web-frontend", "!ta reviewer")
        assert self._after_restart(log) == "reviewer"

    def test_ta_then_bot(self, bots, audit, monkeypatch):
        log = self._run_commands(bots, monkeypatch, "!ta reviewer", "!bot docs")
        assert self._after_restart(log) == "docs"

    def test_bot_then_ta_off(self, bots, audit, monkeypatch):
        log = self._run_commands(bots, monkeypatch, "!bot docs", "!ta off")
        assert self._after_restart(log) == ""


class TestBotList:
    def test_no_name_lists_the_bots_and_runs_no_turn(self, bots, audit):
        slack, sessions = _run(_first("!bot"))

        assert sessions.calls == []
        [post] = _posts(slack)
        assert "`web-frontend`" in post
        assert "`research` (Research Bot)" in post
        assert "This thread runs as" not in post

    def test_the_list_says_which_bot_the_thread_is_on(self, bots, audit):
        slack, _sessions = _run(_first("!bot research"), _reply("!bot"))
        assert "This thread runs as Bot *research*." in _posts(slack)[-1]

    def test_an_unknown_name_is_an_error_that_lists_the_bots(self, bots, audit):
        slack, sessions = _run(_first("!bot nope"), _reply("hello"))

        [error, *_] = _posts(slack)
        assert error.startswith("❌ Unknown Bot `nope`.")
        assert "`web-frontend`" in error and "`research`" in error
        assert _KEY not in _handler._thread_agents
        assert sessions.removed == []
        assert sessions.calls[0][1] == {
            "agent": "kirocrew",
            "channel_id": "D1",
            "start_priority": sessions.calls[0][1]["start_priority"],
        }

    def test_a_kiro_agent_that_is_no_bot_is_refused(self, bots, audit):
        slack, _sessions = _run(_first("!bot kirocrew"))
        assert _posts(slack)[0].startswith("❌ Unknown Bot `kirocrew`.")


class TestBotSwitchIsOwnerOnly:
    def test_a_shared_user_is_refused_and_the_thread_is_unchanged(self, bots, audit):
        slack, sessions = _run(_first("!bot research", user_id=_SHARED), _reply("hello"))

        assert "⛔ Owner-only command." in _posts(slack)
        assert _KEY not in _handler._thread_agents
        assert sessions.removed == []
        assert "crew_agent" not in sessions.calls[0][1]
        audit.log_api_access.assert_any_call(
            caller=_SHARED,
            operation="slack.owner_command",
            outcome="denied",
            source="slack",
            resources="!bot",
            error="unauthorized sender",
        )

    def test_a_shared_user_cannot_list_the_bots_either(self, bots, audit):
        slack, _sessions = _run(_first("!bot", user_id=_SHARED))
        assert _posts(slack) == ["⛔ Owner-only command."]


class TestLinkedThread:
    def test_a_thread_linked_to_a_dashboard_chat_cannot_switch(self, bots, audit, monkeypatch):
        monkeypatch.setattr(_handler, "is_slack_born", lambda key: False)
        slack, sessions = _run(_first("!bot research"))

        assert "linked to a dashboard chat" in _posts(slack)[0]
        assert _KEY not in _handler._thread_agents
        assert sessions.removed == []

    def test_a_thread_the_index_links_to_a_dashboard_session_is_refused(self, bots, audit):
        # The native path hands the command a Slack key; the thread index names the owner.
        slack, sessions = RecordingSlackClient(), _Sessions()
        sessions.linked_owner = "dashboard:slot-1"

        async def _scenario():
            for text in ("!bot research", "!bot off"):
                await _send(slack, sessions, text)

        asyncio.run(_scenario())

        assert (
            _posts(slack).count(
                "❌ This thread is linked to a dashboard chat, so it cannot switch Bots."
            )
            == 2
        )
        assert _KEY not in _handler._thread_agents
        assert sessions.removed == []

    def test_off_in_a_linked_thread_leaves_the_dashboard_session_alone(
        self, bots, audit, monkeypatch
    ):
        monkeypatch.setattr(_handler, "is_slack_born", lambda key: False)
        slack, sessions = _run(_first("!bot off"))

        assert "linked to a dashboard chat" in _posts(slack)[0]
        assert sessions.removed == []


class TestResolveBotName:
    def test_exact_key_then_case_folded_key_or_display_name(self, bots):
        assert channel_crew.resolve_bot_name("research") == "research"
        assert channel_crew.resolve_bot_name("RESEARCH") == "research"
        assert channel_crew.resolve_bot_name("research bot") == "research"
        assert channel_crew.resolve_bot_name("nope") is None
        assert channel_crew.resolve_bot_name("") is None

    def test_an_unreadable_config_lists_no_bots(self, monkeypatch):
        def _boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(live, "snapshot", _boom)
        assert channel_crew.crew_bots() == {}
        assert channel_crew.bots_text() == "(none found)"


class _Session:
    posted: list[dict]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        type(self).posted.append(kwargs["json"])


async def _slash(text, *, owner=True):
    _Session.posted = []
    orch = MagicMock()
    orch.slack_command = "kirocrew"
    orch._owner_id = _OWNER
    orch._handler_tasks = set()
    payload = {
        "command": "/kirocrew",
        "text": text,
        "user_id": _OWNER if owner else _SHARED,
        "response_url": "https://hooks.example.invalid/x",
    }
    with (
        patch("kiro_crew.slack.events.aiohttp.ClientSession", _Session),
        patch("kiro_crew.slack.events.is_allowed_user", return_value=owner),
        patch("kiro_crew.slack.events.sel", return_value=MagicMock()),
        patch("kiro_crew.slack.events_slash.code_channel_slash", return_value=False),
    ):
        await events_slash._handle_slash(orch, payload)
        await asyncio.gather(*list(orch._handler_tasks))
    return [p["text"] for p in _Session.posted]


class TestSlashBot:
    def test_it_lists_the_bots_and_points_at_the_thread_command(self, bots):
        [text] = asyncio.run(_slash("bot"))
        assert "`web-frontend`" in text and "`research` (Research Bot)" in text
        assert "`!bot <name>`" in text

    def test_a_known_name_names_the_command_to_send_in_the_thread(self, bots):
        [text] = asyncio.run(_slash("bot research"))
        assert "`!bot research`" in text

    def test_an_unknown_name_is_an_error_that_lists_the_bots(self, bots):
        [text] = asyncio.run(_slash("bot nope"))
        assert text.startswith("❌ Unknown Bot `nope`.") and "`research`" in text

    def test_a_shared_user_is_refused(self, bots):
        assert asyncio.run(_slash("bot research", owner=False)) == [
            "⛔ You are not authorized to use this command."
        ]

    def test_help_lists_it(self):
        assert "bot" in events_slash.SLASH_REGISTRY
