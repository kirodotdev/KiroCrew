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

import asyncio
import inspect
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments, Kiro Cloud included, are reachable only with the
    # developer flag; the flag-off tests below turn it back off.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


_STUB = """
import json, os, sys

RECORD = os.environ["KAS_STUB_RECORD"]
CLOUD = os.environ.get("KAS_STUB_CLOUD") == "1"
# Appends across processes: a restarted runtime is a new stub process, and a
# test that ends one runtime and starts the next reads both on one record.
seen = json.load(open(RECORD, encoding="utf-8")) if os.path.exists(RECORD) else []
pending_prompt = None
permission_pending = False

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
        seen.append({"method": method, "params": params, "id": mid, "result": msg.get("result")})
        with open(RECORD, "w", encoding="utf-8") as fh:
            json.dump(seen, fh)
    if method == "initialize":
        kiro = {"executionTargets": ["local", "cloud-sandbox"] if CLOUD else ["local"],
                "sessionSources": ["local", "remote"] if CLOUD else ["local"]}
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "agentCapabilities": {"loadSession": True, "_meta": {"kiro": kiro}},
        }})
    elif method == "session/load" and os.environ.get("KAS_STUB_FAIL_LOAD") == "1":
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "load failed"}})
    elif method in ("session/new", "session/load"):
        if method == "session/load" and os.environ.get("KAS_STUB_PERMISSION") == "1":
            permission_pending = True
            send({"jsonrpc": "2.0", "id": "permission-replayed", "method": "session/request_permission", "params": {
                "sessionId": params["sessionId"], "toolCall": {"toolCallId": "tc-replayed", "title": "write file", "kind": "edit"},
                "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"},
                            {"optionId": "reject", "kind": "reject_once", "name": "Reject"}]}})
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": params["sessionId"], "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "old replay"}}}})
        if method == "session/load" and os.environ.get("KAS_STUB_HANG_LOAD") == "1":
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": params.get("sessionId") or "cloud-session",
            "modes": {"currentModeId": "vibe",
                      "availableModes": [{"id": "vibe", "name": "vibe"}]},
        }})
    elif method == "session/prompt":
        if permission_pending:
            pending_prompt = mid
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif mid == "permission-replayed" and "result" in msg:
        permission_pending = False
        if pending_prompt is not None:
            send({"jsonrpc": "2.0", "id": pending_prompt, "result": {"stopReason": "end_turn"}})
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
    "name",
    [
        "create_session",
        "_finish_create_session",
        "load_session",
        "_wait_managed_mcp",
        "_spawn_answer_task",
    ],
)
def test_the_shared_session_start_path_has_no_cloud_branch(name):
    """Cloud shaping lives in the cloud harness, so an ordinary start evaluates none of it."""
    assert "cloud" not in inspect.getsource(getattr(AcpRuntime, name)).lower()


@pytest.mark.parametrize("backend", _SERVED_BACKENDS)
def test_every_served_host_answers_the_replay_seams_as_before(backend):
    """The replay seams answer as if they did not exist on every served host (H13)."""
    harness = harness_for(backend)
    assert harness.holds_replayed_permission("s1") is False
    runtime = SimpleNamespace(_answer_unroutable_permission=AsyncMock())
    msg = object()
    answer = harness.unroutable_permission(runtime, msg, "s1", reason="why")
    asyncio.run(answer())
    runtime._answer_unroutable_permission.assert_awaited_once_with(msg, "s1", reason="why")


@pytest.mark.parametrize("module", ["session_handle_lifecycle", "session_handle_turn"])
def test_the_shared_handle_paths_probe_no_cloud_attribute(module):
    """Kiro's own destroy and drain read the declared contract, never a getattr probe (H14)."""
    import importlib

    source = inspect.getsource(importlib.import_module(f"kiro_crew.acp.{module}"))
    assert 'getattr(self._runtime, "is_cloud_sandbox"' not in source


def test_the_resume_hold_is_no_step_of_the_shared_resume():
    """Only the cloud start takes the resume hold; the runtime's resume is unchanged (H13)."""
    from kiro_crew.acp.harness.base import HarnessAdapter

    assert not hasattr(HarnessAdapter, "resume_hold")
    source = inspect.getsource(AcpRuntime.load_session)
    assert "resume_hold" not in source and "hold_cloud_resume" not in source
    with pytest.raises(AcpRuntimeError):
        AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_KAS).hold_cloud_resume("s1")


async def _held_load(runtime, sid, **kwargs):
    """Reopen *sid* the way the provider's cloud start does: inside the resume hold."""
    hold = runtime.hold_cloud_resume(sid)
    handle = None
    try:
        handle = await runtime.load_session("", sid, **kwargs)
        return handle
    finally:
        hold.settle(handle)


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


@pytest.mark.asyncio
async def test_a_cloud_choice_is_unreachable_with_the_developer_flag_off(tmp_path, monkeypatch):
    """KIROCREW_DEV_MODE off: no Kiro Cloud profile is offered, a recorded cloud
    choice resolves to a local session, and the start never reaches the cloud path."""
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.dashboard.handlers import session_environments as handlers

    cloud = {"id": "cloud", "name": "Kiro Cloud", "repositories": ["acme/web"]}
    section = SimpleNamespace(kiro_cloud_profiles=[cloud], coder_url="", profiles=[])
    monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.config.live.snapshot", lambda: SimpleNamespace(session_environments=section)
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        lambda: SimpleNamespace(session_environments=section),
    )
    choices = sl.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set("s1", "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    assert choices.resolve_choice("s1") == ("cloud", "s1", sl.KIRO_CLOUD_PROVIDER_ID)
    assert [p["id"] for p in handlers._profiles()] == ["cloud"]

    monkeypatch.delenv("KIROCREW_DEV_MODE")
    assert handlers._profiles() == []
    assert choices.resolve_choice("s1") is None
    _governed(monkeypatch, None)
    checked = []
    assert await sl.prepare_for_session("s1", before_compute=lambda: checked.append(1)) is None
    assert checked == []


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


def _boardroom_profile(tmp_path, monkeypatch):
    """An app-bound profile withdrawing cloud sessions, under a policy that allows them."""
    from kiro_crew import execution_context
    from kiro_crew.platform import context
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "boardroom.json").write_text(
        json.dumps(
            {
                "name": "boardroom",
                "bind": {"type": "app", "id": "boardroom"},
                "capabilities": {"kiro_cloud_sessions": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    monkeypatch.setattr(gp, "_STORE", gp.ProfileStore())
    allow = _ceiling(capabilities={"kiro_cloud_sessions": {"enabled": True}})
    monkeypatch.setattr(context, "current_context", lambda: SimpleNamespace(governance=allow))
    # A transcript an agent rewrote to claim no app is never read for this.
    monkeypatch.setattr(
        execution_context, "read_session_execution", lambda *_a, **_kw: SimpleNamespace(app="")
    )


@pytest.mark.parametrize("parent_aged_out", [True, False])
def test_a_cloud_subagent_continuation_keeps_its_provider_and_cloud_session(
    tmp_path, monkeypatch, parent_aged_out
):
    """Root -> A -> B on Kiro Cloud; B re-inherits after A's row is gone (or still there)."""
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.testing.clock import ManualClock

    clock = ManualClock()
    clock.install(monkeypatch, sl)
    choices = sl.SessionEnvironmentChoices(tmp_path / "choices.json")
    choices.set("dashboard:root", "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    assert choices.inherit("subagent:a", "dashboard:root") == "cloud"
    assert choices.inherit("subagent:b", "subagent:a") == "cloud"
    assert choices.record_cloud_session("subagent:b", "cloud-b", profile_id="cloud")
    if parent_aged_out:
        choices.forget_inherited("subagent:a")
        clock.advance(sl.ENDED_ROW_TTL_SECS + 1)
        choices.inherit("subagent:c", "dashboard:root")  # any write prunes A's row
        assert choices.resolve("subagent:a") is None
    assert choices.inherit("subagent:b", "subagent:a") == "cloud"
    assert choices.resolve_choice("subagent:b") == (
        "cloud",
        "dashboard:root",
        sl.KIRO_CLOUD_PROVIDER_ID,
    )
    assert choices.cloud_session_recorded("subagent:b")


@pytest.mark.parametrize("unreadable", [True, False])
def test_an_unreadable_profile_file_refuses_a_cloud_start(tmp_path, monkeypatch, unreadable):
    """A profile whose binding cannot be read might bind this session, so cloud is refused."""
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.platform import context
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    if unreadable:
        # Present, but neither decodable nor parseable: no bind can be recovered.
        (profiles / "restricted.json").write_bytes(b"\xff\xfe\x00not json")
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    monkeypatch.setattr(gp, "_STORE", gp.ProfileStore())
    # Even a policy that allows cloud sessions does not override the unknown profile.
    allow = _ceiling(capabilities={"kiro_cloud_sessions": {"enabled": True}})
    monkeypatch.setattr(context, "current_context", lambda: SimpleNamespace(governance=allow))
    refusal = sl.kiro_cloud_governance_refusal("dashboard:unreadable-profile")
    if unreadable:
        assert refusal is not None and "restricted.json" in refusal
    else:
        assert refusal is None


@pytest.mark.parametrize("owning_app", ["boardroom", ""])
def test_an_app_bound_profile_governs_the_cloud_start(tmp_path, monkeypatch, owning_app):
    """The caller's trusted owning app picks its profile; transcript metadata does not."""
    from kiro_crew.coder import session_launch as sl

    _boardroom_profile(tmp_path, monkeypatch)
    refusal = sl.kiro_cloud_governance_refusal("dashboard:app-owned-cloud", app=owning_app)
    if owning_app:
        assert refusal is not None and sl.KIRO_CLOUD_CAPABILITY in refusal
    else:
        assert refusal is None


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", ["boardroom", ""])
async def test_session_allocation_hands_the_slot_app_to_the_provider(monkeypatch, app_name):
    """``owning_app`` is the caller's ``app_name`` (the slot's own app), not a factory kwarg."""
    from test_session import _mock_provider_factory

    from kiro_crew import session
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(session, "_is_acp_provider", lambda _provider: True)
    made = []
    inner = _mock_provider_factory()

    def factory(*args, **kwargs):
        assert "app_name" not in kwargs
        provider = inner(*args, **kwargs)
        made.append(provider)
        return provider

    mgr = session.SessionManager(KiroCrewConfig(), provider_factory=factory)
    try:
        kwargs = {"app_name": app_name} if app_name else {}
        await mgr.get_or_create("dashboard:app-owned-alloc", **kwargs)
        mgr.release("dashboard:app-owned-alloc")
        assert made and made[0].owning_app == app_name
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_the_provider_carries_its_allocated_app_into_the_cloud_start(
    handlers, tmp_path, monkeypatch
):
    """``owning_app`` (set by session allocation from the slot) reaches the cloud gate."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    _boardroom_profile(tmp_path, monkeypatch)
    key = "dashboard:app-owned-provider"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    provider.owning_app = "boardroom"
    runtime = MagicMock()
    runtime.spawn = AsyncMock(side_effect=AssertionError("cloud must be refused before spawn"))
    monkeypatch.setattr(acp, "AcpRuntime", lambda **_kw: runtime)
    with pytest.raises(SessionEnvironmentUnavailable, match="kiro_cloud_sessions"):
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
    choices.record_cloud_session(key, "cloud-1", profile_id="cloud")
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
        result = await response.json()
        assert result["locked"] is True
        assert result["provider"] == "kiro_cloud"
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
    from kiro_crew.platform.governance import Profile

    _governed(monkeypatch, None)
    assert sl.kiro_cloud_governance_refusal("dashboard:d1") is None
    # A policy and profile that govern nothing leave it alone too.
    _governed(monkeypatch, _ceiling(), Profile(name="empty"))
    assert sl.kiro_cloud_governance_refusal("dashboard:d1") is None


@pytest.mark.parametrize(
    "controls",
    [
        {"capabilities": {"browse": {"enabled": False}}},
        {"capabilities": {"cron": {"enabled": False}}},
        {"channels": {"members": {"mode": "allow", "allow": ["slack"]}}},
    ],
)
def test_a_ceiling_on_any_scope_refuses_a_cloud_session(monkeypatch, controls):
    from kiro_crew.coder import session_launch as sl

    # Not only tool scopes: a policy narrowing browse, cron or channels bounds
    # what the session's agent may do, and the cloud agent is outside it.
    _governed(monkeypatch, _ceiling(**controls))
    refusal = sl.kiro_cloud_governance_refusal("dashboard:d1")
    assert refusal is not None and sl.KIRO_CLOUD_CAPABILITY in refusal
    allow = {"kiro_cloud_sessions": {"enabled": True}}
    merged = {**controls, "capabilities": {**controls.get("capabilities", {}), **allow}}
    _governed(monkeypatch, _ceiling(**merged))
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
        {"id": "dev", "name": "Dev", "provider": "coder", "workdir": "/w", "template": "t"},
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
    assert choices.record_cloud_session("dashboard:c1", "cloud-1", profile_id="cloud") is True
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
    assert choices.record_cloud_session("dashboard:coder", "cloud-1", profile_id="cloud") is False
    assert choices.record_cloud_session("dashboard:none", "cloud-1", profile_id="cloud") is False
    assert "cloud_session" not in _choice_rows(tmp_path)["dashboard:coder"]
    with pytest.raises(ValueError):
        choices.record_cloud_session("dashboard:coder", "", profile_id="cloud")
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
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=backend, sandbox_mode="off"
    )
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
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
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
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_ending_the_runtime_leaves_the_cloud_session_and_the_next_start_reloads_it(
    handlers, stub, crew_agent, tmp_path, monkeypatch
):
    """Kiro's cloud keeps a session running with no client attached (kiro.dev/docs/cloud-sessions).

    So ending this host's runtime -- a chat's provider shut down, as a gateway
    stop or an idle reap does -- must send none of the verbs that would end it
    there: KAS relays ``session/cancel`` to the cloud turn, and its delete verb
    removes the cloud session and its sandbox. The next start must reopen THAT
    session by the id Kiro's cloud answered, never provision a second sandbox.
    """
    from kiro_crew.acp.types import METHOD_CANCEL, METHOD_SESSION_CLOSE
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:reconnect"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    ending = {METHOD_CANCEL, METHOD_SESSION_CLOSE, KasCloudSandboxHarness().teardown.method}
    first = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    try:
        try:
            await first._start_kiro_runtime_impl({}, {})
        finally:
            await first.shutdown()
        recorded = _choice_rows(tmp_path)[key]["cloud_session"]
        assert [r["method"] for r in json.loads(stub.read_text()) if r["method"] in ending] == []

        second = acp.AcpProvider(
            agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
        )
        second._client.set_resume_session_id(recorded)
        meta: dict[str, object] = {}
        try:
            await second._start_kiro_runtime_impl({}, meta)
        finally:
            await second.shutdown()
    finally:
        forget_remote_workspace(key)
    assert meta["resume_outcome"] == "loaded"
    [load] = _requests(stub, "session/load")
    assert load["sessionId"] == recorded
    assert load["_meta"] == {"kiro": {"sessionSource": "remote"}}
    # One sandbox for the whole conversation: the reload provisioned none.
    assert len(_requests(stub, "session/new")) == 1
    assert _choice_rows(tmp_path)[key]["cloud_session"] == recorded
    assert [r["method"] for r in json.loads(stub.read_text()) if r["method"] in ending] == []


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


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.parametrize("method", ["create_session", "load_session"])
@pytest.mark.parametrize("choice", ["cleared", "coder", "other-cloud", "unchanged"])
async def test_cloud_start_requires_the_prepared_choice_to_commit(
    handlers, stub, crew_agent, tmp_path, monkeypatch, method, choice
):
    import asyncio

    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp
    from kiro_crew.session_environment import SessionEnvironmentUnavailable, forget_remote_workspace

    key = "dashboard:cloud-choice-race"
    choices = handlers._choices()
    choices.set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    if method == "load_session":
        provider._client._resume_session_id = "cloud-session"
    placeholder = provider._client
    original = getattr(AcpRuntime, method)
    runtimes = []
    rows_before_record = []

    async def answer_then_change(runtime, *args, **kwargs):
        handle = await original(runtime, *args, **kwargs)
        runtimes.append(runtime)
        # The ACP reply is the seam before the durable record: model the
        # environment PUT winning while session/new or session/load awaited it.
        if choice == "cleared":
            await asyncio.to_thread(choices.set, key, None)
        elif choice == "coder":
            await asyncio.to_thread(choices.set, key, "dev", sl.PROVIDER_ID)
        elif choice == "other-cloud":
            await asyncio.to_thread(choices.set, key, "other", sl.KIRO_CLOUD_PROVIDER_ID)
        rows_before_record.append(await asyncio.to_thread(_choice_rows, tmp_path))
        return handle

    monkeypatch.setattr(AcpRuntime, method, answer_then_change)
    try:
        if choice == "unchanged":
            await provider._start_kiro_runtime_impl({}, {})
            assert provider._client is not placeholder
            assert _choice_rows(tmp_path)[key]["cloud_session"] == "cloud-session"
        else:
            with pytest.raises(SessionEnvironmentUnavailable, match="environment changed"):
                await provider._start_kiro_runtime_impl({}, {})
            assert provider._client is placeholder
            assert len(runtimes) == 1
            assert not runtimes[0].is_alive()
            assert _choice_rows(tmp_path) == rows_before_record[0]
            # A sandbox this start created and could not record is deleted; a
            # reopened session holds the conversation and is kept.
            deletes = _requests(stub, KasCloudSandboxHarness().teardown.method)
            if method == "create_session":
                assert [d.get("sessionId") for d in deletes] == ["cloud-session"]
            else:
                assert deletes == []
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)
    assert len(runtimes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud", [True, False])
async def test_gateway_drain_preserves_only_cloud_turns(tmp_path, monkeypatch, cloud):
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.session import SessionManager, _Session

    runtime = (
        _runtime(tmp_path) if cloud else AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_KAS)
    )
    handle = sh.AcpSessionHandle("cloud-drain", asyncio.Queue(), runtime)
    handle._turn_done.clear()
    monkeypatch.setattr(runtime, "is_alive", lambda: True)
    provider = AcpProvider(agent="kirocrew", acp_backend=ACP_BACKEND_KAS)
    monkeypatch.setattr(provider, "_client", AcpSessionProvider(handle, runtime, owns_runtime=True))
    cancel = AsyncMock(return_value="acked")
    monkeypatch.setattr(provider, "cancel", cancel)
    from kiro_crew.config import KiroCrewConfig

    manager = SessionManager(KiroCrewConfig())
    manager._sessions["dashboard:cloud-drain"] = _Session(provider=provider)
    assert await manager.drain_active_turns() == (0 if cloud else 1)
    assert cancel.await_count == (0 if cloud else 1)
    manager._sessions.clear()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito", "temporary"])
async def test_cloud_teardown_deletes_only_private_chats(stub, crew_agent, tmp_path, memory_mode):
    from kiro_crew.acp.session_provider import AcpSessionProvider

    runtime = _runtime(tmp_path)
    try:
        await runtime.spawn()
        handle = await runtime.create_session(cwd=tmp_path / "ws")
        provider = AcpSessionProvider(handle, runtime, owns_runtime=True)
        provider.memory_mode = memory_mode
        # Explicit ordinary teardown must detach, too; shutdown normally kills
        # the relay without asking terminate_session at all.
        if memory_mode == "persistent":
            await runtime.terminate_session(handle.session_id)
        await provider.shutdown()
    finally:
        await runtime.kill()
    assert len(_requests(stub, "_kiro/session/delete")) == (0 if memory_mode == "persistent" else 1)
    assert _requests(stub, "session/cancel") == []


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.parametrize("failure", ["permission_routing", "snapshot"])
async def test_cloud_reload_cleanup_never_deletes_the_loaded_session(
    stub, crew_agent, tmp_path, monkeypatch, failure
):
    runtime = _runtime(tmp_path)
    try:
        await runtime.spawn()

        async def refuse(*_a, **_k):
            raise RuntimeError("local setup failed")

        if failure == "permission_routing":
            monkeypatch.setattr(sh.AcpSessionHandle, "apply_session_permission_routing", refuse)
        else:
            monkeypatch.setattr(runtime, "_snapshot_descendants", refuse)
        with pytest.raises(RuntimeError, match="local setup failed"):
            await _held_load(runtime, "old-cloud", cwd=tmp_path / "ws")
        assert "old-cloud" not in runtime._session_queues
    finally:
        await runtime.kill()
    assert len(_requests(stub, "session/load")) == 1
    assert _requests(stub, "_kiro/session/delete") == []


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.parametrize("failure", ["permission_routing", "snapshot"])
async def test_a_refused_cloud_start_deletes_the_sandbox_it_minted(
    stub, crew_agent, tmp_path, monkeypatch, failure
):
    """A ``session/new`` no handle was returned for is freed: nothing records its id."""
    runtime = _runtime(tmp_path)
    try:
        await runtime.spawn()

        async def refuse(*_a, **_k):
            raise RuntimeError("local setup failed")

        if failure == "permission_routing":
            monkeypatch.setattr(sh.AcpSessionHandle, "apply_session_permission_routing", refuse)
        else:
            monkeypatch.setattr(runtime, "_snapshot_descendants", refuse)
        with pytest.raises(RuntimeError, match="local setup failed"):
            await runtime.create_session(cwd=tmp_path / "ws")
        assert runtime._unclaimed_new_sessions == set()
    finally:
        await runtime.kill()
    assert len(_requests(stub, "session/new")) == 1
    assert [p["sessionId"] for p in _requests(stub, "_kiro/session/delete")] == ["cloud-session"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.parametrize("ending", ["cancelled", "raised"])
async def test_a_start_ended_after_session_new_deletes_the_sandbox(
    stub, crew_agent, tmp_path, monkeypatch, ending
):
    """Closing the chat mid-setup (or a setup step that has no arm of its own
    failing) after ``session/new`` answered still frees the sandbox it minted."""
    runtime = _runtime(tmp_path)
    try:
        await runtime.spawn()
        reached = asyncio.Event()

        async def stall(*_a, **_k):
            reached.set()
            if ending == "raised":
                raise RuntimeError("served default unreadable")
            await asyncio.Event().wait()

        monkeypatch.setattr(sh.AcpSessionHandle, "ensure_served_default", stall)
        start = asyncio.create_task(runtime.create_session(cwd=tmp_path / "ws"))
        await asyncio.wait_for(reached.wait(), 30)
        if ending == "cancelled":
            start.cancel()
        with pytest.raises(asyncio.CancelledError if ending == "cancelled" else RuntimeError):
            await start
        assert runtime._unclaimed_new_sessions == set()
    finally:
        await runtime.kill()
    assert [p["sessionId"] for p in _requests(stub, "_kiro/session/delete")] == ["cloud-session"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_a_late_unadopted_cloud_start_deletes_its_sandbox(stub, crew_agent, tmp_path):
    runtime = _runtime(tmp_path)
    try:
        await runtime.spawn()
        await runtime._teardown_late_session("late-cloud")
    finally:
        await runtime.kill()
    assert [p["sessionId"] for p in _requests(stub, "_kiro/session/delete")] == ["late-cloud"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_the_entitlement_probe_deletes_its_cloud_session(stub, crew_agent, tmp_path):
    runtime = _runtime(tmp_path)
    try:
        await runtime.spawn()
        await runtime.probe_advertised_models(force=True)
    finally:
        await runtime.kill()
    probed = _requests(stub, "session/new")
    assert len(probed) == 1
    assert len(_requests(stub, "_kiro/session/delete")) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
@pytest.mark.parametrize("live_behind", [False, True])
async def test_replayed_cloud_permission_reaches_the_normal_approval_path(
    stub, crew_agent, tmp_path, monkeypatch, live_behind
):
    """A held replay reaches the approval path, even with a stranded request queued behind it.

    The stranded request is rejected by the pre-turn drain, which hands the held
    replay back to the queue first; the drain then reads the replay again and must
    keep it again rather than reject it as stranded.
    """
    monkeypatch.setenv("KAS_STUB_PERMISSION", "1")
    runtime = _runtime(tmp_path)
    try:
        await runtime.spawn()
        # The load response was read but the handle is not ready to consume.
        reached = asyncio.Event()
        release = asyncio.Event()

        async def snapshot():
            reached.set()
            await asyncio.wait_for(release.wait(), 30)

        monkeypatch.setattr(runtime, "_snapshot_descendants", snapshot)
        load = asyncio.create_task(_held_load(runtime, "old-cloud", cwd=tmp_path / "ws"))
        try:
            await asyncio.wait_for(reached.wait(), 30)
            await runtime._send_and_await("test/checkpoint", {}, timeout=30)
            assert not [
                r for r in json.loads(stub.read_text()) if r.get("id") == "permission-replayed"
            ]
        finally:
            release.set()
        handle = await asyncio.wait_for(load, 30)
        await asyncio.wait_for(asyncio.gather(*list(runtime._answer_tasks)), 30)
        assert isinstance(runtime._harness, KasCloudSandboxHarness)
        assert not runtime._harness.loading
        if live_behind:
            from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION, JsonRpcMessage

            handle._queue.put_nowait(
                JsonRpcMessage(
                    id="stranded-behind",
                    method=METHOD_REQUEST_PERMISSION,
                    params={
                        "sessionId": "old-cloud",
                        "toolCall": {"toolCallId": "tc-stranded", "title": "run"},
                        "options": [{"optionId": "reject", "kind": "reject_once", "name": "R"}],
                    },
                )
            )
        events = []

        async def turn():
            async for event in handle.prompt("Continue"):
                events.append(event)
                if event.kind == "permission_request":
                    await handle.reject_tool(event.request_id)

        await asyncio.wait_for(turn(), 30)
        requests = [e for e in events if e.kind == "permission_request"]
        assert len(requests) == 1
        assert requests[0].request_id == "permission-replayed"
        assert not any(e.text == "old replay" for e in events)
        answers = [r for r in json.loads(stub.read_text()) if r.get("id") == "permission-replayed"]
        assert len(answers) == 1
        assert answers[0]["result"]["outcome"]["optionId"] == "reject"
        if live_behind:
            stranded = [r for r in json.loads(stub.read_text()) if r.get("id") == "stranded-behind"]
            assert len(stranded) == 1
        assert not handle._cloud_replayed_permissions
    finally:
        await runtime.kill()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_cloud_permission_hold_expires_when_load_never_registers(
    stub, crew_agent, tmp_path, monkeypatch
):
    from kiro_crew.acp.harness import kas_cloud
    from kiro_crew.testing.wait import async_wait_until

    monkeypatch.setenv("KAS_STUB_PERMISSION", "1")
    monkeypatch.setenv("KAS_STUB_HANG_LOAD", "1")
    monkeypatch.setattr(kas_cloud, "CLOUD_PERMISSION_HOLD_SECS", 0)
    runtime = _runtime(tmp_path)
    load = None
    try:
        await runtime.spawn()
        load = asyncio.create_task(_held_load(runtime, "old-cloud", cwd=tmp_path / "ws"))

        def answered() -> bool:
            # The stub rewrites its record in place, so a read can land mid-write.
            try:
                rows = json.loads(stub.read_text()) if stub.exists() else []
            except json.JSONDecodeError:
                return False
            return any(r.get("id") == "permission-replayed" for r in rows)

        await async_wait_until(answered, timeout=30)
        answers = [r for r in json.loads(stub.read_text()) if r.get("id") == "permission-replayed"]
        assert answers[0]["result"]["outcome"]["optionId"] == "reject"
        assert "old-cloud" not in runtime._session_queues
    finally:
        if load is not None:
            load.cancel()
            await asyncio.gather(load, return_exceptions=True)
        await runtime.kill()
    assert isinstance(runtime._harness, KasCloudSandboxHarness)
    assert not runtime._harness.loading


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_the_cloud_start_holds_the_approval_its_reopen_replays(
    handlers, stub, crew_agent, tmp_path, monkeypatch
):
    """The provider's cloud reopen takes the resume hold, so a replay is kept, not rejected."""
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-held"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    monkeypatch.setenv("KAS_STUB_PERMISSION", "1")
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    provider._client.set_resume_session_id("old-cloud")
    meta: dict[str, object] = {}
    try:
        await provider._start_kiro_runtime_impl({}, meta)
        assert meta["resume_outcome"] == "loaded"
        runtime = provider._client._runtime
        await asyncio.wait_for(asyncio.gather(*list(runtime._answer_tasks)), 30)
        assert not runtime._harness.loading
        assert "permission-replayed" in provider._client._handle._cloud_replayed_permissions
        assert not [r for r in json.loads(stub.read_text()) if r.get("id") == "permission-replayed"]
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_cloud_resume_fallback_is_logged_and_surfaces_once_in_the_transcript(
    handlers, stub, crew_agent, tmp_path, monkeypatch, caplog
):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.providers import acp
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-fallback"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    # The session the reopen will fail on was recorded by an earlier start.
    assert handlers._choices().record_cloud_session(key, "old-cloud", profile_id="cloud")
    _governed(monkeypatch, None)
    monkeypatch.setenv("KAS_STUB_FAIL_LOAD", "1")
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    provider._client.set_resume_session_id("old-cloud")
    meta: dict[str, object] = {}
    rows = []
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *a, **kw: rows.append((a, kw)))
    try:
        await provider._start_kiro_runtime_impl({}, meta)
        assert meta["resume_outcome"] == "fallback_replay"
        assert provider._history_replay_needed
        assert len(_requests(stub, "session/new")) == 1
        assert "Couldn't reopen the cloud session" in caplog.text
        chat_runner._surface_startup_notice(MagicMock(), MagicMock(), provider)
        chat_runner._surface_startup_notice(MagicMock(), MagicMock(), provider)
        assert len(rows) == 1
        # An error row (errors-use-error-notice): something the user had did not
        # reopen. ``kind`` lets the error card show the localized sentence.
        assert rows[0][0][2] == "error"
        assert "earlier session aren't in it" in rows[0][0][3]
        assert "still listed in Kiro Web (app.kiro.dev)" in rows[0][0][3]
        assert rows[0][1]["meta"] == {"kind": "cloud_session_replayed"}
        # The session that could not be reopened is left in Kiro's cloud (the
        # user deletes it from Kiro Web); nothing is deleted here.
        assert _choice_rows(tmp_path)[key]["cloud_session"] == "cloud-session"
        assert _requests(stub, KasCloudSandboxHarness().teardown.method) == []
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="the stub launcher is a POSIX shell script")
async def test_close_all_stops_only_the_cloud_relay_with_an_unfinished_turn(
    handlers, stub, crew_agent, tmp_path, monkeypatch
):
    from kiro_crew.coder import session_launch as sl
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.providers import acp
    from kiro_crew.session import SessionManager, _Session
    from kiro_crew.session_environment import forget_remote_workspace

    key = "dashboard:cloud-shutdown"
    handlers._choices().set(key, "cloud", sl.KIRO_CLOUD_PROVIDER_ID)
    _governed(monkeypatch, None)
    provider = acp.AcpProvider(
        agent="kirocrew", session_key=key, acp_backend=ACP_BACKEND_KAS, sandbox_mode="off"
    )
    manager = SessionManager(KiroCrewConfig())
    try:
        await provider._start_kiro_runtime_impl({}, {})
        from kiro_crew.acp.session_provider import AcpSessionProvider

        inner = provider._client
        assert isinstance(inner, AcpSessionProvider)
        runtime = inner._runtime
        inner._handle._turn_done.clear()
        assert provider.has_unfinished_turn()
        manager._sessions[key] = _Session(provider=provider)
        await manager.close_all()
        assert not runtime.is_alive()
        assert not manager._sessions
        assert _requests(stub, "session/cancel") == []
        assert _requests(stub, "_kiro/session/delete") == []
    finally:
        await provider.shutdown()
        forget_remote_workspace(key)


@pytest.mark.asyncio
async def test_a_dashboard_turn_surfaces_the_startup_notice_once():
    """The claimed provider's start problem lands once, as an error row of its kind."""
    from turn_harness import ScriptedProvider, SlotSpec, TurnScript, run_turn

    class _Reopened(ScriptedProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.startup_notice = ("cloud_session_replayed", "channel fallback text")

    record = await run_turn(TurnScript(message="hi", provider=_Reopened), slot=SlotSpec())
    rows = [
        r for r in record.window if (r.get("meta") or {}).get("kind") == "cloud_session_replayed"
    ]
    assert len(rows) == 1
    assert rows[0]["role"] == "error"
    assert rows[0]["content"] == "channel fallback text"
