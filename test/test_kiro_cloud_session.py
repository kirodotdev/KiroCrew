"""Kiro Cloud Sessions as a session environment.

A Kiro Cloud profile asks KAS to place the session in a Kiro cloud sandbox, using
the ``_meta.kiro`` keys kiro-cli's own ``--cloud`` flag sends on ``session/new``.
KAS forwards only the mode, the target and the repositories to that sandbox, so
Crew sends neither its agent spec nor its MCP servers, and it refuses to start
when KAS does not offer cloud sandboxes rather than quietly running locally.

The KAS stub records every request and advertises ``cloud-sandbox`` only when
the test asks it to, the way KAS gates it on its cloud link being configured.
"""

from __future__ import annotations

import inspect
import json
import sys
from types import SimpleNamespace

import pytest

from kiro_crew.acp import session_handle as sh
from kiro_crew.acp.harness import KasHarness, KiroHarness, harness_for
from kiro_crew.acp.harness.kas_cloud import KasCloudSandboxHarness
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config.session_environments import (
    SessionEnvironmentsError,
    load_kiro_cloud_profiles,
    validate_session_environments,
)

_SERVED_BACKENDS = [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX]

_STUB = """
import json, os, sys

RECORD = os.environ["KAS_STUB_RECORD"]
CLOUD = os.environ.get("KAS_STUB_CLOUD") == "1"
seen = []

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method != "initialize":
        seen.append({"method": method, "params": params})
        with open(RECORD, "w", encoding="utf-8") as fh:
            json.dump(seen, fh)
    if method == "initialize":
        kiro = {"executionTargets": ["local", "cloud-sandbox"] if CLOUD else ["local"],
                "sessionSources": ["local", "remote"] if CLOUD else ["local"]}
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {"loadSession": True, "_meta": {"kiro": kiro}},
        }})
    elif method in ("session/new", "session/load"):
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": params.get("sessionId") or "cloud-session",
            "modes": {"currentModeId": "vibe",
                      "availableModes": [{"id": "vibe", "name": "vibe"}]},
        }})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
"""


@pytest.fixture(autouse=True)
def _fast_no_report_ceiling(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05)


@pytest.fixture
def stub(tmp_path, monkeypatch):
    script = tmp_path / "kas_cloud_stub.py"
    script.write_text(_STUB)
    record = tmp_path / "record.json"
    launcher = tmp_path / "kiro-cli-stub"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    launcher.chmod(0o755)

    async def fake_bin(*, environ=None, home=None) -> str:
        return str(launcher)

    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", fake_bin)
    monkeypatch.setenv("KAS_STUB_RECORD", str(record))
    monkeypatch.setenv("KAS_STUB_CLOUD", "1")
    return record


@pytest.fixture
def crew_agent(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "description": "test crew agent",
                "prompt": "You are Kiro.",
                "tools": ["fs_read", "@kirocrew-core"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr("kiro_crew.agent.ensure_agent_materialized", lambda _a: True)
    return agents_dir


def _runtime(tmp_path, repos=("acme/web",)) -> AcpRuntime:
    runtime = AcpRuntime(
        work_dir=tmp_path / "ws",
        agent="kirocrew",
        sandbox_mode="off",
        acp_backend=ACP_BACKEND_KAS,
    )
    runtime.set_cloud_sandbox(repos)
    return runtime


def _requests(record, method):
    return [r["params"] for r in json.loads(record.read_text()) if r["method"] == method]


@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
class TestCloudSessionWire:
    @pytest.mark.asyncio
    async def test_session_new_asks_for_a_cloud_sandbox_with_none_of_crews_surface(
        self, stub, crew_agent, tmp_path
    ):
        runtime = _runtime(tmp_path)
        try:
            await runtime.spawn()
            await runtime.create_session(cwd=tmp_path / "ws", agent="kirocrew")
        finally:
            await runtime.kill()
        [params] = _requests(stub, "session/new")
        kiro = params["_meta"]["kiro"]
        assert kiro["executionTarget"] == {"kind": "cloud-sandbox"}
        assert kiro["sessionSource"] == "remote"
        assert kiro["repositories"] == ["acme/web"]
        assert "isEmptyWorkspace" not in kiro
        # The sandbox would drop these, so they are never sent.
        assert "customAgents" not in kiro
        assert params["mcpServers"] == []
        # Crew's agent is not registered there, so it is never activated.
        assert _requests(stub, "session/set_mode") == []

    @pytest.mark.asyncio
    async def test_no_repositories_opens_an_empty_workspace(self, stub, crew_agent, tmp_path):
        runtime = _runtime(tmp_path, repos=())
        try:
            await runtime.spawn()
            await runtime.create_session(cwd=tmp_path / "ws")
        finally:
            await runtime.kill()
        [params] = _requests(stub, "session/new")
        assert params["_meta"]["kiro"]["isEmptyWorkspace"] is True
        assert "repositories" not in params["_meta"]["kiro"]

    @pytest.mark.asyncio
    async def test_refused_when_kas_offers_no_cloud_sandbox(
        self, stub, crew_agent, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("KAS_STUB_CLOUD", "0")
        runtime = _runtime(tmp_path)
        try:
            await runtime.spawn()
            with pytest.raises(AcpRuntimeError, match="not available"):
                await runtime.create_session(cwd=tmp_path / "ws")
        finally:
            await runtime.kill()
        # Refused before anything went out: never a local session in its place.
        assert not stub.exists() or _requests(stub, "session/new") == []

    @pytest.mark.asyncio
    async def test_session_load_reopens_from_the_cloud_store(self, stub, crew_agent, tmp_path):
        runtime = _runtime(tmp_path)
        try:
            await runtime.spawn()
            await runtime.load_session("", "cloud-1", cwd=tmp_path / "ws", agent="kirocrew")
        finally:
            await runtime.kill()
        [params] = _requests(stub, "session/load")
        assert params["sessionId"] == "cloud-1"
        assert params["_meta"] == {"kiro": {"sessionSource": "remote"}}
        assert params["mcpServers"] == []
        assert _requests(stub, "session/set_mode") == []


def test_cloud_sandbox_needs_kas_and_an_unspawned_runtime(tmp_path):
    kiro = AcpRuntime(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_KIRO)
    with pytest.raises(AcpRuntimeError, match="KAS"):
        kiro.set_cloud_sandbox(())
    assert type(kiro._harness) is KiroHarness


def test_the_cloud_sandbox_is_a_harness_the_runtime_installs():
    """Every cloud answer comes from the installed harness, never from the backend table."""
    plain = AcpRuntime(work_dir="/workspace/ws", agent="kirocrew", acp_backend=ACP_BACKEND_KAS)
    assert type(plain._harness) is KasHarness
    cloud = AcpRuntime(work_dir="/workspace/ws", agent="kirocrew", acp_backend=ACP_BACKEND_KAS)
    cloud.set_cloud_sandbox(("acme/web",))
    assert isinstance(cloud._harness, KasCloudSandboxHarness)
    assert cloud._harness.repositories == ("acme/web",)
    assert cloud._expect_mcp_reports is False
    assert cloud._harness.notification_aliases.mcp_readiness is False
    assert not any(isinstance(harness_for(b), KasCloudSandboxHarness) for b in _SERVED_BACKENDS)


@pytest.mark.parametrize("backend", _SERVED_BACKENDS)
@pytest.mark.parametrize("method", ["session/new", "session/load"])
def test_every_served_host_sends_the_request_it_was_built(backend, method):
    """The request seam is an identity on every host the backend table serves (H13)."""
    params = {"cwd": "/workspace/ws", "mcpServers": [{"name": "kirocrew-core"}]}
    out, agent = harness_for(backend).session_request(
        method, params, "kirocrew", agent_capabilities={}
    )
    assert out is params
    assert agent == "kirocrew"


@pytest.mark.parametrize(
    "name", ["create_session", "_finish_create_session", "load_session", "_wait_managed_mcp"]
)
def test_the_shared_session_start_path_has_no_cloud_branch(name):
    """Cloud shaping lives in the cloud harness, so an ordinary start evaluates none of it."""
    assert "cloud" not in inspect.getsource(getattr(AcpRuntime, name)).lower()


def _section(**extra):
    return {"coder_url": "", "profiles": [], **extra}


def test_config_accepts_and_normalizes_a_cloud_profile():
    out = validate_session_environments(
        _section(
            kiro_cloud_profiles=[
                {"id": "cloud", "name": " Kiro Cloud ", "repositories": ["acme/web", "acme/web"]}
            ],
            default_profile="cloud",
        )
    )
    assert out["kiro_cloud_profiles"] == [
        {"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}
    ]
    assert out["default_profile"] == "cloud"


@pytest.mark.parametrize(
    "profile",
    [
        {"id": "c", "name": "C", "repositories": ["a b"]},
        {"id": "c", "name": "C", "repositories": ["--flag"]},
        {"id": "c", "name": "C", "repositories": ["a/b/c"]},
        {"id": "c", "name": "C", "repositories": "acme/web"},
        {"id": "c", "name": "", "repositories": []},
        {"id": "c", "name": "C", "repositories": [], "token": "x"},
    ],
)
def test_config_refuses_a_malformed_cloud_profile(profile):
    with pytest.raises(SessionEnvironmentsError, match=r"kiro_cloud_profiles\[0\]"):
        validate_session_environments(_section(kiro_cloud_profiles=[profile]))


def test_config_refuses_an_id_shared_with_another_provider():
    coder = {"id": "x", "name": "X", "template": "t", "workdir": "/w"}
    with pytest.raises(SessionEnvironmentsError, match="Kiro Cloud"):
        validate_session_environments(
            {
                "coder_url": "https://coder.example.com",
                "profiles": [coder],
                "kiro_cloud_profiles": [{"id": "x", "name": "X", "repositories": []}],
            }
        )


def test_load_keeps_valid_cloud_profiles_and_drops_bad_ones():
    raw = {
        "kiro_cloud_profiles": [
            {"id": "ok", "name": "OK", "repositories": []},
            {"id": "bad", "name": "Bad", "repositories": ["no spaces allowed"]},
        ]
    }
    assert [p["id"] for p in load_kiro_cloud_profiles(raw)] == ["ok"]


@pytest.mark.asyncio
async def test_prepare_returns_a_cloud_session_without_touching_any_compute(tmp_path, monkeypatch):
    from kiro_crew.coder import session_launch as sl

    section = SimpleNamespace(
        kiro_cloud_profiles=[{"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}],
    )
    monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.config.live.snapshot", lambda: SimpleNamespace(session_environments=section)
    )
    sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "s1", "cloud", sl.KIRO_CLOUD_PROVIDER_ID
    )

    def no_compute(*_a, **_k):
        raise AssertionError("a Kiro Cloud session provisions nothing")

    monkeypatch.setattr(sl, "CoderSessionEnvironments", no_compute)
    _governed(monkeypatch, None)
    checked = []
    prepared = await sl.prepare_for_session("s1", before_compute=lambda: checked.append(1))
    assert prepared == sl.PreparedCloudSession("cloud", "Kiro Cloud", ("acme/web",))
    assert checked == [1]


def _ceiling(**controls):
    from kiro_crew.platform.governance import parse_policy

    return parse_policy({"version": 1, "boot": {"fail_closed": True}, **controls})


def _governed(monkeypatch, ceiling, profile=None):
    from kiro_crew.platform import context, governance_profiles

    monkeypatch.setattr(context, "current_context", lambda: SimpleNamespace(governance=ceiling))
    monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda _key, **_kw: profile)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["researcher", ""])
async def test_cloud_provider_enforces_the_execution_agents_profile(
    handlers, tmp_path, monkeypatch, agent
):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.coder import session_launch as sl
    from kiro_crew.platform import context
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.providers import acp
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    execution_agent = agent or "kirocrew"
    (profiles / "restricted.json").write_text(
        json.dumps(
            {
                "name": "restricted",
                "bind": {"type": "task", "id": execution_agent},
                "tools": {"mode": "allow", "allow": ["fs_read"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    monkeypatch.setattr(gp, "_STORE", gp.ProfileStore())
    monkeypatch.setattr(context, "current_context", lambda: SimpleNamespace(governance=None))
    key = "dashboard:agent-bound-cloud"
    choices = sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    assert gp.resolve_active_scope(key) is None
    profile = gp.resolve_active_scope(key, agent=execution_agent)
    assert profile is not None and profile.name == "restricted"
    provider = acp.AcpProvider(agent=execution_agent, session_key=key, acp_backend=ACP_BACKEND_KAS)
    provider._client._agent = agent
    runtime = MagicMock()
    runtime.spawn = AsyncMock(side_effect=AssertionError("cloud must be refused before spawn"))
    monkeypatch.setattr(acp, "AcpRuntime", lambda **_kw: runtime)
    with pytest.raises(SessionEnvironmentUnavailable, match="tools"):
        await provider._start_kiro_runtime_impl({}, {})
    runtime.spawn.assert_not_awaited()
    runtime.set_cloud_sandbox.assert_not_called()


@pytest.mark.parametrize("source", ["policy", "profile", "withdrawal", "profile-grant"])
@pytest.mark.asyncio
async def test_cloud_capability_is_checked_without_tool_scopes(handlers, monkeypatch, source):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.platform.governance import CapabilityGate, Profile
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    allow = {"kiro_cloud_sessions": {"enabled": True}}
    deny = {"kiro_cloud_sessions": {"enabled": False}}
    ceiling = _ceiling(capabilities=deny) if source in ("policy", "profile-grant") else None
    if source == "withdrawal":
        ceiling = _ceiling(capabilities=allow)
    profile = (
        None
        if source == "policy"
        else Profile(
            name="p",
            controls={sl.KIRO_CLOUD_CAPABILITY: CapabilityGate(enabled=source == "profile-grant")},
        )
    )
    _governed(monkeypatch, ceiling, profile)
    key = "dashboard:capability-only"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    with pytest.raises(SessionEnvironmentUnavailable, match=sl.KIRO_CLOUD_CAPABILITY):
        await sl.prepare_for_session(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("target", [None, "dev"])
async def test_recorded_cloud_choice_stays_locked_without_its_profile(
    handlers, section, monkeypatch, reuse, target
):
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.coder import session_launch as sl
    from kiro_crew.dashboard import chat_runner

    key = "dashboard:cloud-locked"
    choices = handlers._choices()
    choices.set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    choices.record_cloud_session(key, "cloud-1")
    section.kiro_cloud_profiles = []
    if reuse:
        section.profiles.append(SimpleNamespace(**{**_CODER, "id": "cloud"}))
    slot = SimpleNamespace(
        key="cloud-locked",
        total_messages=2,
        messages=[],
        _lock=asyncio.Lock(),
        _pending_reset_history_key=None,
    )

    async def owner(request, operation):
        return None

    def no_spawn(*_a, **_kw):
        raise AssertionError("a locked choice must not prewarm a local runtime")

    monkeypatch.setattr(handlers, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(handlers, "effective_session_key", lambda _slot: key)
    monkeypatch.setattr(chat_runner, "schedule_eager_spawn", no_spawn)
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={slot.key: slot})
    app.router.add_get("/s/{slot}", handlers.api_chat_slot_environment_get)
    app.router.add_put("/s/{slot}", handlers.api_chat_slot_environment_put)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/s/cloud-locked")
        assert response.status == 200
        assert (await response.json())["locked"] is True
        response = await client.put("/s/cloud-locked", json={"profile": target})
        assert response.status == 409
        assert (await response.json())["code"] == "environment_locked"
    assert choices.resolve_choice(key) == ("cloud", key, sl.KIRO_CLOUD_PROVIDER_ID)
    assert slot._pending_reset_history_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ungoverned", "opt-in", "deny", "policy-error", "profile-error"])
async def test_cloud_launch_audits_every_governance_decision(handlers, monkeypatch, case):
    from unittest.mock import MagicMock

    from kiro_crew import sel as sel_module
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.platform import context, governance_profiles
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    ceiling = None if case == "ungoverned" else _ceiling(**_DENY_NC)
    if case == "opt-in":
        ceiling = _ceiling(**_DENY_NC, capabilities={"kiro_cloud_sessions": {"enabled": True}})
    _governed(monkeypatch, ceiling)

    def broken(*_a, **_kw):
        raise RuntimeError("governance unavailable")

    if case == "policy-error":
        monkeypatch.setattr(context, "current_context", broken)
    elif case == "profile-error":
        monkeypatch.setattr(governance_profiles, "resolve_active_scope", broken)
    audit = MagicMock()
    monkeypatch.setattr(sel_module, "sel", lambda: audit)
    key = "dashboard:cloud-audit"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    allowed = case in ("ungoverned", "opt-in")
    if allowed:
        assert isinstance(await sl.prepare_for_session(key), sl.PreparedCloudSession)
    else:
        with pytest.raises(SessionEnvironmentUnavailable) as exc:
            await sl.prepare_for_session(key)
    audit.log_governance_decision.assert_called_once()
    event = audit.log_governance_decision.call_args.kwargs
    assert event["session_key"] == key
    assert event["scope"] == sl.KIRO_CLOUD_CAPABILITY
    assert event["outcome"] == ("allowed" if allowed else "denied")
    assert event["reason"]
    if not allowed:
        assert event["reason"] == str(exc.value)
    if case.endswith("-error"):
        assert "could not be read" in event["reason"]


_DENY_NC = {"commands": {"mode": "deny", "deny": ["nc *"]}}


def test_an_ungoverned_host_may_start_a_cloud_session(monkeypatch):
    from kiro_crew.coder import session_launch as sl

    _governed(monkeypatch, None)
    assert sl.kiro_cloud_governance_refusal("dashboard:d1") is None
    # A ceiling that governs no tool-execution scope leaves it alone too.
    _governed(monkeypatch, _ceiling(capabilities={"cron": {"enabled": False}}))
    assert sl.kiro_cloud_governance_refusal("dashboard:d1") is None


def test_a_ceiling_on_tool_execution_refuses_a_cloud_session(monkeypatch):
    from kiro_crew.coder import session_launch as sl

    _governed(monkeypatch, _ceiling(**_DENY_NC))
    refusal = sl.kiro_cloud_governance_refusal("dashboard:d1")
    assert refusal is not None and "commands" in refusal
    assert sl.KIRO_CLOUD_CAPABILITY in refusal


def test_a_profile_on_tool_execution_refuses_a_cloud_session(monkeypatch):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.platform.governance import deny_all_profile

    _governed(monkeypatch, None, deny_all_profile())
    assert sl.kiro_cloud_governance_refusal("cron:job") is not None


def test_only_the_policy_can_allow_a_cloud_session(monkeypatch):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.platform.governance import CapabilityGate, Profile

    allow = {"kiro_cloud_sessions": {"enabled": True}}
    _governed(monkeypatch, _ceiling(**_DENY_NC, capabilities=allow))
    assert sl.kiro_cloud_governance_refusal("dashboard:d1") is None
    # Named without ``enabled``: the opt-in default keeps it refused.
    _governed(monkeypatch, _ceiling(**_DENY_NC, capabilities={"kiro_cloud_sessions": {}}))
    assert sl.kiro_cloud_governance_refusal("dashboard:d1") is not None
    # A profile can withdraw the policy's allowance...
    withdraw = Profile(name="p", controls={sl.KIRO_CLOUD_CAPABILITY: CapabilityGate(enabled=False)})
    _governed(monkeypatch, _ceiling(**_DENY_NC, capabilities=allow), withdraw)
    assert sl.kiro_cloud_governance_refusal("dashboard:d1") is not None
    # ...but not grant one the policy never gave.
    grant = Profile(name="p", controls={sl.KIRO_CLOUD_CAPABILITY: CapabilityGate(enabled=True)})
    _governed(monkeypatch, _ceiling(**_DENY_NC), grant)
    assert sl.kiro_cloud_governance_refusal("dashboard:d1") is not None


def test_an_unreadable_ceiling_refuses_a_cloud_session(monkeypatch):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.platform import context

    def broken():
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(context, "current_context", broken)
    refusal = sl.kiro_cloud_governance_refusal("dashboard:d1")
    assert refusal is not None and "could not be read" in refusal


@pytest.mark.asyncio
async def test_a_governed_host_refuses_the_cloud_start_before_it_is_prepared(tmp_path, monkeypatch):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    section = SimpleNamespace(
        kiro_cloud_profiles=[{"id": "cloud", "name": "Kiro Cloud", "repositories": []}],
    )
    monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.config.live.snapshot", lambda: SimpleNamespace(session_environments=section)
    )
    sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:d1", "cloud", sl.KIRO_CLOUD_PROVIDER_ID
    )
    _governed(monkeypatch, _ceiling(**_DENY_NC))
    with pytest.raises(SessionEnvironmentUnavailable, match="commands"):
        await sl.prepare_for_session("dashboard:d1")


def test_the_project_line_names_the_cloud_sandbox_and_no_workspace_tools():
    from kiro_crew import session_environment as se
    from kiro_crew.context_assembly.turn import rail_parts

    key = "dashboard:cloud-project-line"
    se.remember_remote_workspace(key, "Kiro Cloud", "")
    try:
        text = "".join(
            rail_parts(
                project="/workspace/repo",
                context_groups=None,
                board_tags=None,
                minimal_context=True,
                folder_path=None,
                session_key=key,
                agent=None,
                request_prefix_context=None,
            )
        )
    finally:
        se.forget_remote_workspace(key)
    assert "Kiro Cloud Sessions sandbox (Kiro Cloud)" in text
    # Neither the gateway checkout nor the Coder-style workspace tools apply.
    assert "/workspace/repo" not in text
    assert "@workspace" not in text


_CLOUD = {"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}
_CODER = {"id": "dev", "name": "Dev", "template": "t", "workdir": "/w"}


@pytest.fixture
def section():
    return SimpleNamespace(
        coder_url="https://c.example.com",
        profiles=[SimpleNamespace(**_CODER)],
        kiro_cloud_profiles=[_CLOUD],
        default_profile="",
    )


@pytest.fixture
def handlers(tmp_path, monkeypatch, section):
    from kiro_crew.config import live, loader, paths
    from kiro_crew.dashboard.handlers import session_environments as module

    # ``agent.kas_bundle`` is read by the KAS harness at spawn; off = the stub launcher.
    config = SimpleNamespace(session_environments=section, agent=SimpleNamespace(kas_bundle=False))
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(live, "snapshot", lambda: config)
    return module


def test_the_picker_lists_cloud_profiles_with_their_repositories(handlers):
    assert handlers._profiles() == [
        {"id": "dev", "name": "Dev", "provider": "coder", "workdir": "/w"},
        {
            "id": "cloud",
            "name": "Kiro Cloud",
            "provider": "kiro_cloud",
            "workdir": "",
            "repositories": ["acme/web"],
        },
    ]


@pytest.mark.asyncio
async def test_a_started_cloud_session_keeps_its_environment(handlers):
    from kiro_crew.coder import session_launch as sl

    started = SimpleNamespace(total_messages=2, messages=[])
    fresh = SimpleNamespace(total_messages=0, messages=[])
    choices = handlers._choices()
    choices.set("dashboard:c1", "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    # Every start so far was refused before session/new: nothing is in the cloud.
    assert await handlers._choice_locked(started, "dashboard:c1", "cloud") is False
    # No Coder registry row ever exists for a cloud session, yet once Kiro's
    # cloud answered with a session id its history lives there, so it is fixed.
    assert choices.record_cloud_session("dashboard:c1", "cloud-1") is True
    assert await handlers._choice_locked(started, "dashboard:c1", "cloud") is True
    assert await handlers._choice_locked(fresh, "dashboard:c1", "cloud") is False
    # A Coder choice with no workspace created can still move (unchanged rule).
    assert await handlers._choice_locked(started, "dashboard:c2", "dev") is False


@pytest.mark.asyncio
async def test_a_cloud_session_record_lands_only_on_a_cloud_row_and_a_bad_one_locks(
    handlers, tmp_path
):
    from kiro_crew.coder import session_launch as sl

    choices = handlers._choices()
    choices.set("dashboard:coder", "dev", sl.PROVIDER_ID)
    # The choice moved off the cloud (or was cleared) since the start: no record.
    assert choices.record_cloud_session("dashboard:coder", "cloud-1") is False
    assert choices.record_cloud_session("dashboard:none", "cloud-1") is False
    assert "cloud_session" not in _choice_rows(tmp_path)["dashboard:coder"]
    with pytest.raises(ValueError):
        choices.record_cloud_session("dashboard:coder", "")
    # A malformed record does not prove the cloud holds nothing: fixed.
    choices.set("dashboard:bad", "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    path = tmp_path / "coder" / "session-choices.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["sessions"]["dashboard:bad"]["cloud_session"] = 7
    path.write_text(json.dumps(raw), encoding="utf-8")
    started = SimpleNamespace(total_messages=1, messages=[])
    assert await handlers._choice_locked(started, "dashboard:bad", "cloud") is True


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.parametrize("cause", ["no-cloud-sandbox", "kiro-backend"])
async def test_a_chat_whose_cloud_start_was_refused_can_move_back_to_this_machine(
    handlers, stub, crew_agent, tmp_path, monkeypatch, cause
):
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.acp.types import ACP_BACKEND_KIRO
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.providers import acp

    key = "dashboard:refused"
    choices = handlers._choices()
    choices.set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    backend = ACP_BACKEND_KAS
    if cause == "no-cloud-sandbox":
        monkeypatch.setenv("KAS_STUB_CLOUD", "0")
    else:
        backend = ACP_BACKEND_KIRO
    provider = acp.AcpProvider(agent="kirocrew", session_key=key, acp_backend=backend)
    with pytest.raises(
        AcpRuntimeError, match="KAS" if cause == "kiro-backend" else "not available"
    ):
        await provider._start_kiro_runtime_impl({}, {})
    assert choices.cloud_session_recorded(key) is False
    # The refused turn's message is in the chat, yet nothing is in the cloud.
    slot = SimpleNamespace(
        key="refused",
        total_messages=1,
        messages=[],
        _lock=asyncio.Lock(),
        _pending_reset_history_key=None,
    )

    async def owner(request, operation):
        return None

    spawned = []
    monkeypatch.setattr(handlers, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(handlers, "effective_session_key", lambda _slot: key)
    monkeypatch.setattr(chat_runner, "schedule_eager_spawn", lambda s, sl_: spawned.append(sl_))
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={slot.key: slot})
    app.router.add_get("/s/{slot}", handlers.api_chat_slot_environment_get)
    app.router.add_put("/s/{slot}", handlers.api_chat_slot_environment_put)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/s/refused")
        assert (await response.json())["locked"] is False
        response = await client.put("/s/refused", json={"profile": None})
        assert response.status == 200
    assert choices.resolve_choice(key) is None
    assert slot._pending_reset_history_key == key
    assert spawned == [slot]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_a_cloud_start_records_the_cloud_session_on_the_choice_row(
    handlers, stub, crew_agent, tmp_path, monkeypatch
):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp

    key = "dashboard:answered"
    choices = handlers._choices()
    choices.set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    provider = acp.AcpProvider(agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS)
    try:
        await provider._start_kiro_runtime_impl({}, {})
    finally:
        await provider.shutdown()
    assert [p["_meta"]["kiro"]["executionTarget"] for p in _requests(stub, "session/new")] == [
        {"kind": "cloud-sandbox"}
    ]
    assert _choice_rows(tmp_path)[key]["cloud_session"] == "cloud-session"
    started = SimpleNamespace(total_messages=1, messages=[])
    assert await handlers._choice_locked(started, key, "cloud") is True


@pytest.mark.asyncio
async def test_browse_and_terminal_refuse_a_cloud_session_without_touching_coder(
    handlers, tmp_path, monkeypatch
):
    from kiro_crew.coder import session_browse as sb
    from kiro_crew.coder import session_launch as sl

    sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:c2", "cloud", sl.KIRO_CLOUD_PROVIDER_ID
    )

    def no_coder(*_a, **_k):
        raise AssertionError("a Kiro Cloud session has no Coder workspace")

    monkeypatch.setattr(sl, "CoderSessionEnvironments", no_coder)
    with pytest.raises(sb.CloudSandboxOnly, match="Kiro cloud sandbox"):
        await sb.browse_session("dashboard:c2", "list", ".")
    with pytest.raises(sb.CloudSandboxOnly):
        await sb.remote_terminal_argv("dashboard:c2")


@pytest.mark.asyncio
async def test_an_id_reused_by_another_provider_does_not_move_a_started_session(
    handlers, section, tmp_path, monkeypatch
):
    """The row's provider is checked: a reused id never moves a session across providers."""
    from kiro_crew.coder import session_browse as sb
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    choices = sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    # Started on Coder profile "gone"; that profile was deleted and a cloud one took its id.
    section.kiro_cloud_profiles.append({**_CLOUD, "id": "gone", "name": "Reused"})
    choices.set("dashboard:moved", "gone", sl.PROVIDER_ID)
    assert choices.resolve_choice("dashboard:moved") == ("gone", "dashboard:moved", "coder")
    _governed(monkeypatch, None)

    def no_coder(*_a, **_k):
        raise AssertionError("a refused session touches no compute")

    monkeypatch.setattr(sl, "CoderSessionEnvironments", no_coder)
    with pytest.raises(SessionEnvironmentUnavailable, match="runs on coder"):
        await sl.prepare_for_session("dashboard:moved")
    with pytest.raises(SessionEnvironmentUnavailable, match="runs on coder"):
        await sb.browse_session("dashboard:moved", "list", ".")
    # The reverse: a cloud session whose id now names the Coder profile "dev".
    choices.set("dashboard:back", "dev", sl.KIRO_CLOUD_PROVIDER_ID)
    with pytest.raises(SessionEnvironmentUnavailable, match="runs on kiro_cloud"):
        await sl.prepare_for_session("dashboard:back")


def _choice_rows(tmp_path):
    path = tmp_path / "coder" / "session-choices.json"
    return json.loads(path.read_text(encoding="utf-8"))["sessions"]


@pytest.mark.asyncio
async def test_an_id_saved_for_two_providers_is_neither_offered_nor_launched(
    handlers, section, tmp_path, monkeypatch
):
    from kiro_crew.coder import session_browse as sb
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    # A hand edit the config writer would refuse: "dev" names a Coder and a cloud profile.
    section.kiro_cloud_profiles.append({**_CLOUD, "id": "dev", "name": "Dev cloud"})
    assert [p["id"] for p in handlers._profiles()] == ["cloud"]
    # The Coder path refuses it too, so no caller can reach the Coder workspace by it.
    with pytest.raises(SessionEnvironmentUnavailable, match="more than one provider"):
        sl.CoderSessionEnvironments(section, tmp_path).profile("dev")
    # A session bound to "dev" before the collision existed.
    sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:d1", "dev"
    )

    def no_compute(*_a, **_k):
        raise AssertionError("an ambiguous profile id provisions nothing")

    monkeypatch.setattr(sl, "CoderSessionEnvironments", no_compute)
    with pytest.raises(SessionEnvironmentUnavailable, match="more than one provider"):
        await sl.prepare_for_session("dashboard:d1")
    with pytest.raises(SessionEnvironmentUnavailable, match="more than one provider"):
        await sb.browse_session("dashboard:d1", "list", ".")
    # It may have run on either provider, so a started session stays fixed.
    started = SimpleNamespace(total_messages=2, messages=[])
    assert await handlers._choice_locked(started, "dashboard:d1", "dev") is True


@pytest.mark.asyncio
async def test_the_choice_row_records_the_provider_the_profile_resolves_to(
    handlers, section, tmp_path, monkeypatch
):
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.coder import session_launch as sl
    from kiro_crew.dashboard import chat_runner

    async def owner(request, operation):
        return None

    monkeypatch.setattr(handlers, "require_owner_dashboard_request", owner)
    monkeypatch.setattr(handlers, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    monkeypatch.setattr(chat_runner, "schedule_eager_spawn", lambda state, slot: None)
    slots = {
        name: SimpleNamespace(key=name, _lock=asyncio.Lock(), _pending_reset_history_key=None)
        for name in ("a", "b")
    }
    app = web.Application()
    app["state"] = SimpleNamespace(_slots=slots)
    app.router.add_put("/s/{slot}", handlers.api_chat_slot_environment_put)
    async with TestClient(TestServer(app)) as client:
        assert (await client.put("/s/a", json={"profile": "cloud"})).status == 200
        assert (await client.put("/s/b", json={"profile": "dev"})).status == 200
    section.default_profile = "cloud"
    assert await handlers.apply_default_environment(SimpleNamespace(key="c", messages=[]))
    rows = _choice_rows(tmp_path)
    assert rows["dashboard:a"] == {"provider": "kiro_cloud", "configuration": "cloud"}
    assert rows["dashboard:b"] == {"provider": "coder", "configuration": "dev"}
    assert rows["dashboard:c"] == {"provider": "kiro_cloud", "configuration": "cloud"}
    # A subagent of the cloud session records the same provider, and still resolves.
    choices = sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    assert choices.inherit("sub-a", "dashboard:a") == "cloud"
    assert _choice_rows(tmp_path)["sub-a"]["provider"] == "kiro_cloud"
    assert choices.resolve("sub-a") == ("cloud", "dashboard:a")
