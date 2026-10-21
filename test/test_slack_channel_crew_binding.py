"""A Slack channel bound to a Crew agent record runs chat turns as that record.

``slack.channels.<id>.agent`` may name a ``config.agents`` record. Before the
fix both chat call sites -- the messaging transport (``transport_dispatch``) and
the native turn (``handler_runtime/turn.py``) -- handed the record's NAME to
``get_or_create`` as the kiro-cli mode and no ``cwd``, so the session asked for
a mode that does not exist and ran in a per-session scratch directory. The cron
path collapses the name the same way (``cron_fire._resolve_cron_agent``).
"""

from __future__ import annotations

import asyncio
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
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack import transport_dispatch
from kiro_crew.slack.channel_crew import ChannelCrew, resolve_channel_crew


@pytest.fixture()
def crew_cfg(monkeypatch, tmp_path):
    """A config with one Crew record, ``web-frontend``, on its own workspace."""
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    tree = tmp_path / "web-frontend"
    tree.mkdir()
    cfg.workspaces["web-frontend"] = WorkspaceConfig(dir=str(tree))
    cfg.agents["web-frontend"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="web-frontend"
    )
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    return cfg, tree


class TestResolveChannelCrew:
    def test_crew_record_resolves_to_its_mode_identity_and_workspace(self, crew_cfg):
        _cfg, tree = crew_cfg
        assert resolve_channel_crew("web-frontend") == ChannelCrew(
            kiro_agent="kirocrew", crew_agent="web-frontend", cwd=str(tree)
        )

    def test_plain_kiro_mode_is_not_a_crew_record(self, crew_cfg):
        # A channel naming a kiro-cli mode directly keeps today's pass-through.
        assert resolve_channel_crew("kirocrew-lite") is None

    def test_empty_name_resolves_nothing(self, crew_cfg):
        assert resolve_channel_crew("") is None
        assert resolve_channel_crew(None) is None

    def test_unreadable_config_resolves_nothing(self, monkeypatch):
        def _boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(live, "snapshot", _boom)
        assert resolve_channel_crew("web-frontend") is None


class _KwargsSessions(FakeSessions):
    """Records every keyword ``get_or_create`` is called with for the turn."""

    def __init__(self, provider):
        super().__init__(provider)
        self.calls: list[dict] = []

    async def get_or_create(self, session_key, **kwargs):
        from kiro_crew.session import BACKGROUND_KEY

        if session_key != BACKGROUND_KEY:
            self.calls.append(kwargs)
        return await super().get_or_create(
            session_key, agent=kwargs.get("agent"), channel_id=kwargs.get("channel_id")
        )


def _run_transport(monkeypatch, *, agent_override, thread_agent=None):
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    thread_map: dict = {}
    if thread_agent is not None:
        thread_map[canonical_key(_MSG_TS)] = thread_agent
    monkeypatch.setattr(transport_dispatch, "_thread_agents", thread_map)
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="hi"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _KwargsSessions(provider)
    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=RecordingSlackClient(),
            sessions=sessions,
            channel="C1",
            text="hello",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=None,
            agent_override=agent_override,
        )
    )
    assert len(sessions.calls) == 1
    return sessions.calls[0]


class TestTransportHonorsChannelCrew:
    def test_crew_bound_channel_runs_the_record(self, monkeypatch, crew_cfg):
        _cfg, tree = crew_cfg
        kw = _run_transport(monkeypatch, agent_override="web-frontend")
        assert kw["agent"] == "kirocrew"
        assert kw["crew_agent"] == "web-frontend"
        assert kw["cwd"] == str(tree)

    def test_plain_mode_channel_is_unchanged(self, monkeypatch, crew_cfg):
        kw = _run_transport(monkeypatch, agent_override="ops-agent")
        assert kw["agent"] == "ops-agent"
        assert "crew_agent" not in kw
        assert "cwd" not in kw

    def test_thread_override_is_not_collapsed(self, monkeypatch, crew_cfg):
        # ``!agent`` chose the agent, not the channel binding: unchanged.
        kw = _run_transport(
            monkeypatch, agent_override="web-frontend", thread_agent="kirocrew-research"
        )
        assert kw["agent"] == "kirocrew-research"
        assert "cwd" not in kw

    def test_binding_restored_from_the_transcript_still_runs_the_record(
        self, monkeypatch, crew_cfg
    ):
        # After a restart ``_hydrate_thread_overrides`` restores the transcript
        # header's agent -- the record's name -- into ``_thread_agents``. That is
        # the channel binding coming back, not a ``!agent`` override.
        _cfg, tree = crew_cfg
        kw = _run_transport(monkeypatch, agent_override="web-frontend", thread_agent="web-frontend")
        assert kw["agent"] == "kirocrew"
        assert kw["crew_agent"] == "web-frontend"
        assert kw["cwd"] == str(tree)

    def test_dashboard_linked_session_keeps_its_own_project(self, monkeypatch, crew_cfg):
        # A thread a dashboard session owns runs under that session's key; its
        # slot's project is its cwd, so the channel workspace must not replace it.
        monkeypatch.setattr(transport_dispatch, "is_slack_born", lambda key: False)
        kw = _run_transport(monkeypatch, agent_override="web-frontend")
        assert kw["agent"] == "web-frontend"
        assert "cwd" not in kw


async def _empty_stream(*a, **kw):
    return
    yield  # makes this an async generator


def _native_mocks():
    slack = AsyncMock()
    slack.update_message = AsyncMock(return_value="1234.5678")
    slack.fetch_message = AsyncMock(return_value=None)
    client = AsyncMock()
    client.stream = _empty_stream
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    sessions = AsyncMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    for name in (
        "set_slack_link",
        "get_pid",
        "release",
        "is_cancelled",
        "check_context_usage",
        "record_success",
        "begin_turn",
    ):
        setattr(sessions, name, MagicMock(return_value=None))
    sessions.is_cancelled = MagicMock(return_value=False)
    sessions.check_context_usage = MagicMock(return_value=0.0)
    sessions.get_session_for_thread = MagicMock(return_value=None)
    sessions._sessions = {}
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("hello", MagicMock()))
    ctx.conversation_log = None
    return slack, sessions, ctx


@pytest.fixture()
def _native_agent_cache():
    from kiro_crew.slack import handler

    old_cached = handler._cached_default_agent
    old_thread = dict(handler._thread_agents)
    handler._cached_default_agent = "kirocrew"
    handler._thread_agents.clear()
    yield
    handler._cached_default_agent = old_cached
    handler._thread_agents.clear()
    handler._thread_agents.update(old_thread)


@pytest.mark.usefixtures("_native_agent_cache")
class TestNativeTurnHonorsChannelCrew:
    async def _run(self, channel_agent, thread_agent=None):
        from kiro_crew.slack import handler
        from kiro_crew.slack.handler import handle_message

        if thread_agent is not None:
            handler._thread_agents[canonical_key(_MSG_TS)] = thread_agent
        slack, sessions, ctx = _native_mocks()
        with patch("kiro_crew.slack.handler.publish_turn_identity"):
            await handle_message(
                slack=slack,
                sessions=sessions,
                channel="C123",
                text="hi",
                thread_ts=None,
                # A real Slack timestamp, so the session key is Slack-born.
                msg_ts=_MSG_TS,
                user_id="U1",
                context_builder=ctx,
                channel_agent=channel_agent,
            )
        sessions.get_or_create.assert_called_once()
        return sessions.get_or_create.call_args.kwargs, ctx

    @pytest.mark.asyncio
    async def test_crew_bound_channel_runs_the_record(self, crew_cfg):
        _cfg, tree = crew_cfg
        kw, ctx = await self._run("web-frontend")
        assert kw["agent"] == "kirocrew"
        assert kw["crew_agent"] == "web-frontend"
        assert kw["cwd"] == str(tree)
        # The context builder still sees the record's name, as before.
        assert ctx.build_message.call_args.kwargs.get("agent") == "web-frontend"

    @pytest.mark.asyncio
    async def test_binding_restored_from_the_transcript_still_runs_the_record(self, crew_cfg):
        _cfg, tree = crew_cfg
        kw, _ctx = await self._run("web-frontend", thread_agent="web-frontend")
        assert kw["agent"] == "kirocrew"
        assert kw["cwd"] == str(tree)

    @pytest.mark.asyncio
    async def test_thread_override_is_not_collapsed(self, crew_cfg):
        kw, _ctx = await self._run("web-frontend", thread_agent="kirocrew-research")
        assert kw["agent"] == "kirocrew-research"
        assert "cwd" not in kw

    @pytest.mark.asyncio
    async def test_plain_mode_channel_is_unchanged(self, crew_cfg):
        kw, _ctx = await self._run("siads-etl-test")
        assert kw["agent"] == "siads-etl-test"
        assert "cwd" not in kw
