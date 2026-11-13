"""A KAS session's ``toolsSettings.subagent.trustedAgents`` meets its own profiles.

The translated ``subagent`` allow skips the gate's per-spawn check, so it travels
only when nothing the gate could apply to THIS session governs ``use_subagent``:
the Level-1 ceiling, the session's surface profile (or the unattended deny-all),
every task-bound profile, and the profile bound to the session's app. A profile
bound to another surface or another app never judges this session's spawns, so
it does not withhold the allow here. The projection and the session-start notice
ask the same question. A spec's own sub-agent allow (``use_subagent`` in
``allowedTools``, a ``subagent`` allow in ``permissions``) meets the same profiles.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import spec_keys_without_carrier, to_client_custom_agent
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.governance import parse_policy
from kiro_crew.testing.ids import UNALLOCATABLE_PID

_SPEC = {
    "name": "a1",
    "prompt": "p",
    "tools": ["*"],
    "includeMcpJson": False,
    "toolsSettings": {"subagent": {"availableAgents": ["x"], "trustedAgents": ["x"]}},
}
_ALLOW = {"capability": "subagent", "match": ["x"], "effect": "allow"}
_DENY = {"capability": "subagent", "match": ["*"], "exclude": ["x"], "effect": "deny"}
_TRUSTED = "toolsSettings.subagent.trustedAgents"
_NO_SPAWN = {"spawn": {"enabled": False}}


def _write(directory, name, bind_type, bind_id):
    body = {
        "name": name,
        "bind": {"type": bind_type, "id": bind_id},
        "capabilities": _NO_SPAWN,
    }
    (directory / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """The shape an edition seeds: spawn off on unattended surfaces and one app."""
    directory = tmp_path / "profiles"
    directory.mkdir()
    _write(directory, "subagent", "surface", "subagent")
    _write(directory, "cron", "surface", "cron")
    _write(directory, "app-tunnel", "app", "tunnel")
    _write(directory, "app-meetings", "app", "meetings")
    monkeypatch.setattr(gp, "_PROFILES_DIR", directory)
    gp.reset_store()
    yield directory
    gp.reset_store()


@pytest.fixture
def ceiling(monkeypatch):
    """Installs a Level-1 ceiling for the predicate's ``current_context`` read."""

    def install(body):
        policy = parse_policy({"version": 1, "boot": {"fail_closed": True}, **body})
        monkeypatch.setattr(ctx_mod, "current_context", lambda: SimpleNamespace(governance=policy))

    return install


def _rules(session_key, app):
    out = to_client_custom_agent("a1", _SPEC, "p", session_key=session_key, governance_app=app)
    return out.get("permissions", {}).get("rules", [])


def _lost(session_key, app):
    return spec_keys_without_carrier(
        _SPEC, translates_tools_settings=True, session_key=session_key, governance_app=app
    )


_PERMITTED = [
    ("dashboard:s1", ""),
    ("dashboard:s1", "other-app"),
    ("taskrunner-1", ""),
    ("_host", ""),
]

_WITHHELD = [
    ("subagent:abc", ""),
    ("cron:j", ""),
    # An unattended sentinel with no profile resolves to the deny-all.
    ("_hb", ""),
    # An unkeyed projection keeps the rule for a writer with no session.
    ("", ""),
    ("dashboard:s1", "tunnel"),
    # An in-process app runner's session, whose key reads as an attended surface.
    ("meetings-a1-m1", "meetings"),
    # An app not known counts every app-bound profile.
    ("dashboard:s1", None),
]


class TestProjection:
    @pytest.mark.parametrize(("session_key", "app"), _PERMITTED)
    def test_the_allow_travels_where_no_profile_of_the_session_governs(
        self, profiles, session_key, app
    ):
        rules = _rules(session_key, app)
        assert _ALLOW in rules
        assert _DENY in rules

    @pytest.mark.parametrize(("session_key", "app"), _WITHHELD)
    def test_the_allow_is_withheld_where_one_does(self, profiles, session_key, app):
        rules = _rules(session_key, app)
        assert _ALLOW not in rules
        # The deny only narrows, so it travels whatever governs the session.
        assert _DENY in rules

    def test_a_task_bound_profile_withholds_on_every_surface(self, profiles):
        # The gate's agent identity may name it, whatever the projection's id is.
        _write(profiles, "researcher", "task", "researcher")
        gp.reset_store()
        assert _ALLOW not in _rules("dashboard:s1", "")

    @pytest.mark.parametrize(
        "spawn",
        [
            {"enabled": False},
            {"enabled": True, "scopes": {"agents": {"mode": "allow", "allow": ["x"]}}},
        ],
    )
    def test_a_ceiling_that_restricts_spawning_still_withholds(self, profiles, ceiling, spawn):
        ceiling({"capabilities": {"spawn": spawn}})
        assert _ALLOW not in _rules("dashboard:s1", "")

    def test_an_unresolved_store_answers_as_a_writer_with_no_session(self, profiles, monkeypatch):
        monkeypatch.setattr(gp._STORE, "resolved", lambda: False)
        assert _ALLOW not in _rules("dashboard:s1", "")

    def test_a_reload_between_the_two_reads_answers_as_a_writer_with_no_session(
        self, profiles, monkeypatch
    ):
        real = gp.resolve_active_scope

        def resolve_then_reload(*args, **kwargs):
            profile = real(*args, **kwargs)
            gp._STORE._snap = dataclasses.replace(gp._STORE._snap)
            return profile

        monkeypatch.setattr(gp, "resolve_active_scope", resolve_then_reload)
        assert gp.session_profiles_govern("use_subagent", "dashboard:s1", app="") is True

    def test_an_evaluation_error_withholds(self, profiles, monkeypatch):
        from kiro_crew.platform.governance import may_skip_gate_for_session

        def boom(*_a, **_kw):
            raise RuntimeError("profile store broken")

        permits = may_skip_gate_for_session("dashboard:s1", "")
        assert permits("use_subagent")
        monkeypatch.setattr(gp, "session_profiles_govern", boom)
        assert not permits("use_subagent")


_AUTHORED = {
    "allowedTools": {"allowedTools": ["use_subagent"]},
    "permissions": {"permissions": {"rules": [{"capability": "subagent", "effect": "allow"}]}},
}
_AUTHORED_ALLOW = {"capability": "subagent", "effect": "allow"}


class TestAuthoredAllow:
    """A spec's own sub-agent allow meets the same profiles as a ``trustedAgents`` one."""

    @staticmethod
    def _rules(authored, session_key, app):
        spec = {"name": "a1", "prompt": "p", "tools": ["*"], "includeMcpJson": False}
        spec.update(_AUTHORED[authored])
        out = to_client_custom_agent("a1", spec, "p", session_key=session_key, governance_app=app)
        return out.get("permissions", {}).get("rules", [])

    @pytest.mark.parametrize("authored", sorted(_AUTHORED))
    @pytest.mark.parametrize(("session_key", "app"), _PERMITTED)
    def test_it_travels_where_no_profile_of_the_session_governs(
        self, profiles, authored, session_key, app
    ):
        assert _AUTHORED_ALLOW in self._rules(authored, session_key, app)

    @pytest.mark.parametrize("authored", sorted(_AUTHORED))
    @pytest.mark.parametrize(("session_key", "app"), _WITHHELD)
    def test_it_is_withheld_where_one_does(self, profiles, authored, session_key, app):
        assert _AUTHORED_ALLOW not in self._rules(authored, session_key, app)


class TestNotice:
    @pytest.mark.parametrize(("session_key", "app"), _PERMITTED)
    def test_a_travelling_allow_is_not_named(self, profiles, session_key, app):
        assert _TRUSTED not in _lost(session_key, app)

    @pytest.mark.parametrize(("session_key", "app"), _WITHHELD)
    def test_a_withheld_allow_is_named(self, profiles, session_key, app):
        assert _TRUSTED in _lost(session_key, app)


class TestPlumbing:
    """The session's key and app reach both readers."""

    @staticmethod
    async def _app_handed_to_the_harness(scope, session_key):
        from kiro_crew.acp import runtime_session_setup
        from kiro_crew.acp.harness import SessionExtras

        seen: dict = {}

        async def session_extras(agent, **kw):
            seen.update(kw)
            return SessionExtras()

        runtime = SimpleNamespace(
            _agent_capabilities={},
            _harness=SimpleNamespace(session_extras=session_extras),
            _mcp_gateway_overlay=None,
            _work_dir=None,
            _refuse_if_loader_unreachable=lambda *_a: None,
            _governance_scope=scope,
        )
        await runtime_session_setup._kas_custom_agents(runtime, "a", session_key=session_key)
        return seen["governance_app"]

    @pytest.mark.asyncio
    async def test_the_runtime_hands_its_own_session_the_app(self):
        scope = ("dashboard:s1", "tunnel")
        assert await self._app_handed_to_the_harness(scope, "dashboard:s1") == "tunnel"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("scope", "session_key"),
        [
            # Another session on the same process, a subagent sharing its parent's.
            (("dashboard:s1", ""), "subagent:abc"),
            # A pooled process, built before any session claimed it.
            (("", ""), "dashboard:s1"),
            # A runtime built with no provider behind it.
            (None, "dashboard:s1"),
        ],
    )
    async def test_any_other_session_gets_an_unknown_app(self, scope, session_key):
        assert await self._app_handed_to_the_harness(scope, session_key) is None

    @pytest.mark.asyncio
    async def test_the_kas_harness_hands_the_app_to_the_projection(self, monkeypatch, tmp_path):
        from kiro_crew import agent as agent_mod
        from kiro_crew.acp.harness import harness_for
        from kiro_crew.acp.types import ACP_BACKEND_KAS
        from kiro_crew.config import paths as paths_mod
        from kiro_crew.mcp_gateway import session_servers as session_servers_mod

        monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(
            session_servers_mod, "injection_server_names", lambda overlay, agent: frozenset()
        )
        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)
        (tmp_path / "a.json").write_text(
            json.dumps({"name": "a", "prompt": "p", "tools": []}), encoding="utf-8"
        )
        seen: list = []

        def build(_d, a, _spec, **kw):
            seen.append(kw["governance_app"])
            return [{"name": a}]

        monkeypatch.setattr(kas_agents, "build_kas_custom_agents", build)
        await harness_for(ACP_BACKEND_KAS).session_extras(
            "a", work_dir=str(tmp_path), session_key="dashboard:s1", governance_app="tunnel"
        )
        assert seen == ["tunnel"]

    @pytest.mark.asyncio
    async def test_the_provider_scopes_its_runtime_to_its_session_and_app(self):
        from kiro_crew.providers.acp import AcpProvider

        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(acp_backend="")
        provider._client = MagicMock()
        provider._client.backend = ""
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_socket = None
        provider._client._resume_session_id = ""
        provider._client._session_key = "dashboard:chat-1"
        provider._client._channel_id = None
        provider._client._model = "auto"
        provider.owning_app = "tunnel"
        handle = MagicMock(session_id="live-sess-id", available_models=[], set_model=AsyncMock())
        runtime = MagicMock(pid=UNALLOCATABLE_PID, spawn=AsyncMock(), kill=AsyncMock())
        runtime.is_alive = MagicMock(return_value=True)
        runtime.saw_not_logged_in = MagicMock(return_value=False)
        runtime.saw_sandbox_init_failure = MagicMock(return_value=False)
        runtime.saw_delegate_launch_failure = MagicMock(return_value=False)
        runtime.settle_stderr = AsyncMock()
        runtime.create_session = AsyncMock(return_value=handle)
        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime) as built,
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda h, r, **kw: MagicMock(_handle=h, _runtime=r, resumed=False),
            ),
            patch("pathlib.Path.exists", return_value=True),
        ):
            await provider._start_kiro_runtime()
        assert built.call_args.kwargs["governance_scope"] == ("dashboard:chat-1", "tunnel")

    @pytest.mark.asyncio
    async def test_a_meeting_agent_session_runs_under_the_app_its_gate_judges(self):
        # The provider's owning_app is the app its projection is judged for, so an
        # app runner that hands the gate its own app names that app at allocation.
        # The meeting dispatch is one AgentTurnSDK.run turn, the door every app's
        # agent turns pass through.
        from kiro_crew import llm_helpers
        from kiro_crew.apps.builtins.meetings.backend.domain import session as meeting

        judged: dict = {}

        async def stream(_provider, _text, **kw):
            judged.update(kw)
            return ""

        sessions = SimpleNamespace(
            get_or_create=AsyncMock(return_value=(MagicMock(), False, False)),
            get_agent=MagicMock(return_value="a1"),
            release=MagicMock(),
        )
        with patch.object(llm_helpers, "stream_and_collect", stream):
            await meeting.dispatch_to_agent(
                sessions, "meetings-a1-m1", "a line", "a1", hooks=MagicMock()
            )
        assert judged["app"]
        assert sessions.get_or_create.await_args.kwargs.get("owning_app") == judged["app"]

    @pytest.mark.asyncio
    async def test_a_rebuilt_session_keeps_the_app_its_gate_judges(self, monkeypatch):
        # A reset successor and a compaction restart allocate from
        # allocation_identity. Without the app, the successor's projection would
        # skip the app-bound profile the slot's gate still judges its calls under.
        from session_helpers import _mock_provider_factory

        from kiro_crew import session
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session_lifecycle_parts.parked_queue import allocation_identity

        monkeypatch.setattr(session, "_is_acp_provider", lambda _provider: True)
        inner = _mock_provider_factory()
        made: list = []

        def factory(*args, **kwargs):
            provider = inner(*args, **kwargs)
            provider.cwd = None
            made.append(provider)
            return provider

        mgr = session.SessionManager(KiroCrewConfig(), provider_factory=factory)
        key = "dashboard:app-slot"
        try:
            await mgr.get_or_create(key, app_name="tunnel")
            mgr.release(key)
            identity = allocation_identity(mgr, key, mgr._sessions[key])
            await mgr.reset(key)
            await mgr.get_or_create(key, **identity)
            mgr.release(key)
        finally:
            await mgr.close_all()
        assert [provider.owning_app for provider in made] == ["tunnel", "tunnel"]

    def test_the_dashboard_notice_asks_for_the_providers_session_and_app(self, monkeypatch):
        from kiro_crew.acp.types import ACP_BACKEND_KAS
        from kiro_crew.agent_sdk import spec_hooks
        from kiro_crew.agent_sdk.capabilities import capabilities_for
        from kiro_crew.dashboard import chat_runner

        seen: dict = {}

        def lost(_spec, **kw):
            seen.update(kw)
            return []

        # Keyword-tolerant: the reader also takes the session's ``kas_checkout``.
        monkeypatch.setattr(spec_hooks, "_agent_spec_with_origin", lambda *_a, **_k: ({}, "user"))
        monkeypatch.setattr(kas_agents, "spec_keys_without_carrier", lost)
        client = SimpleNamespace(
            capabilities=capabilities_for(ACP_BACKEND_KAS),
            cwd="",
            client=SimpleNamespace(initialize_result={}),
            _session_key="dashboard:s1",
            owning_app="tunnel",
        )
        asyncio.run(
            chat_runner._prepare_spec_hooks(
                SimpleNamespace(), SimpleNamespace(), client, "a", is_new=False
            )
        )
        assert (seen["session_key"], seen["governance_app"]) == ("dashboard:s1", "tunnel")
