"""Private direct MCP cron calls cross the verified host boundary, never write locally."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_core, mcp_cron, member_memory_auth, platform_compat
from kiro_crew.config import paths
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.cron import CronService
from kiro_crew.dashboard.server import _register_mcp_routes
from kiro_crew.history import ConversationLog
from kiro_crew.mcp_caller import CallerContext, current_caller, set_current_caller
from kiro_crew.memory_stores import provision_member_memory


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:alice")
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"test-{pid}")
    monkeypatch.setattr(member_memory_auth, "_proof_process_scope", lambda pid: ["fixture", pid])
    monkeypatch.setattr(platform_compat, "get_ppid", lambda pid: 0)
    cfg = KiroCrewConfig.load()
    log = ConversationLog()
    identities = {}
    for pid, member in enumerate(("alice", "bob"), start=70001):
        cfg.agents[member] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        store = provision_member_memory(cfg, member)
        cfg.save()
        key = f"dashboard:{member}"
        log.update_metadata(key, {"agent": member, "memory_store": store})
        member_memory_auth.bind_private_session_store(key, store)
        member_memory_auth.publish_member_session_pid(pid, key, memory_store=store)
        proof = member_memory_auth.issue_member_session_proof(key, pid)
        assert proof
        identities[member] = SimpleNamespace(pid=pid, key=key, store=store, proof=proof)
    # Synthetic process/start/isolation identity replaces the OS lookup. All protected
    # records, proof verification, config/store validation and cron files are real.
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: 70001)
    previous = current_caller()
    set_current_caller(None)
    state = SimpleNamespace(conversation_log=log, push_refresh=mock.Mock())
    yield SimpleNamespace(home=tmp_path, state=state, identities=identities)
    set_current_caller(previous)


def app_for(env):
    @web.middleware
    async def authenticate(request, handler):
        if request.headers.get("X-Internal-Secret") == "fixture-internal-secret":
            request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[authenticate])
    app["state"] = env.state
    _register_mcp_routes(app)
    return app


async def invoke(client, env, name, arguments, *, member="alice", internal=True, proof=True):
    identity = env.identities[member]
    headers = {"X-Session-Key": identity.key}
    if internal:
        headers["X-Internal-Secret"] = "fixture-internal-secret"
    if proof:
        headers[member_memory_auth.PROOF_HEADER] = identity.proof
    response = await client.post(
        "/api/crons/tools", json={"name": name, "arguments": arguments}, headers=headers
    )
    return response.status, await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated_proof", [False, True])
async def test_private_direct_cron_lifecycle_uses_host_store(env, monkeypatch, delegated_proof):
    monkeypatch.setattr(paths, "private_runtime_log_dir", lambda: env.home / "agent-logs")
    loop = asyncio.get_running_loop()
    opened_by = []

    def host_service(*args, **kwargs):
        caller = current_caller()
        assert caller is not None and caller.from_gateway
        assert caller.session_key == env.identities["alice"].key
        opened_by.append(caller.session_key)
        return CronService(*args, **kwargs)

    monkeypatch.setattr(mcp_cron, "CronService", host_service)
    async with TestClient(TestServer(app_for(env))) as client:

        def post(path, body, *, session_key):
            assert path == "/api/crons/tools"
            assert session_key == env.identities["alice"].key
            status, payload = asyncio.run_coroutine_threadsafe(
                invoke(client, env, body["name"], body["arguments"], proof=delegated_proof), loop
            ).result(timeout=10)
            assert status == 200, payload
            return payload

        monkeypatch.setattr(mcp_core, "_post", post)
        result = await asyncio.to_thread(
            mcp_cron._call_tool,
            "cron_add",
            {"name": "private reminder", "message": "go", "every": 120},
        )
        assert result.startswith("Added job"), result
        job = CronService(base_dir=env.home).list_jobs()[0]
        assert (job.member_id, job.memory_store, job.session_key) == (
            "alice",
            env.identities["alice"].store,
            env.identities["alice"].key,
        )
        result = await asyncio.to_thread(mcp_cron._call_tool, "cron_list", {})
        assert job.id in result
        for name, arguments, prefix in (
            ("cron_update", {"job_id": job.id, "message": "updated"}, "Updated job"),
            ("cron_pause", {"job_id": job.id}, "Paused job"),
            ("cron_resume", {"job_id": job.id}, "Resumed job"),
            ("cron_remove", {"job_id": job.id}, "Removed job"),
        ):
            result = await asyncio.to_thread(mcp_cron._call_tool, name, arguments)
            assert result.startswith(prefix), result
        assert CronService(base_dir=env.home).list_jobs(include_disabled=True) == []
    assert len(opened_by) == 6
    assert current_caller() is None


@pytest.mark.asyncio
async def test_proxy_preserves_validation_governance_and_member_scope(env, monkeypatch):
    async with TestClient(TestServer(app_for(env))) as client:
        for member in ("alice", "bob"):
            status, body = await invoke(
                client,
                env,
                "cron_add",
                {"name": member, "message": "go", "every": 120},
                member=member,
            )
            assert status == 200 and body["result"].startswith("Added job"), body
        jobs = {job.name: job for job in CronService(base_dir=env.home).list_jobs()}
        status, body = await invoke(client, env, "cron_list", {"verbose": True})
        assert status == 200 and jobs["alice"].id in body["result"]
        assert jobs["bob"].id not in body["result"]
        status, body = await invoke(client, env, "cron_remove", {"job_id": jobs["bob"].id})
        assert status == 200 and body["result"].startswith("Error:")
        assert CronService(base_dir=env.home).get_job(jobs["bob"].id) is not None
        status, body = await invoke(client, env, "cron_add", {"name": 7, "every": 120})
        assert status == 200 and body["result"].startswith("Error:")
        from kiro_crew.platform import governance_profiles

        def deny(*args, **kwargs):
            assert kwargs["session_key"] == env.identities["alice"].key
            return SimpleNamespace(permitted=False, reason="fixture policy")

        monkeypatch.setattr(governance_profiles, "governance_permits", deny)
        status, body = await invoke(
            client, env, "cron_add", {"name": "blocked", "message": "go", "every": 120}
        )
        assert status == 200 and "blocked by governance policy" in body["result"]
        status, body = await invoke(client, env, "cron_remove_all", {})
        assert status == 200 and body["result"] == "Removed 1 job(s)."
        assert [job.name for job in CronService(base_dir=env.home).list_jobs()] == ["bob"]


@pytest.mark.asyncio
async def test_private_deterministic_refusal_propagates_without_persisting(env):
    async with TestClient(TestServer(app_for(env))) as client:
        status, body = await invoke(
            client, env, "cron_add", {"name": "scriptless", "command": "echo hello", "every": 120}
        )
    assert status == 200
    assert "memory_unavailable" in body["result"] and "require an agent task" in body["result"]
    assert CronService(base_dir=env.home).list_jobs(include_disabled=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["browser", "unverified", "global", "header", "store", "metadata"])
async def test_endpoint_refuses_unverified_scope_before_opening_cron_store(env, monkeypatch, case):
    if case in ("unverified", "global", "header"):
        answer = {
            "unverified": (None, False),
            "global": (None, True),
            "header": ("dashboard:bob", True),
        }[case]
        monkeypatch.setattr(member_memory_auth, "memory_request_identity", lambda request: answer)
    elif case == "store":
        monkeypatch.setattr(
            member_memory_auth,
            "memory_request_bound_store",
            lambda request: env.identities["bob"].store,
        )
    elif case == "metadata":
        env.state.conversation_log.update_metadata(
            "dashboard:alice", {"memory_store": env.identities["bob"].store}
        )
    opening = mock.Mock(side_effect=AssertionError("unauthorized cron store access"))
    monkeypatch.setattr(mcp_cron, "CronService", opening)
    async with TestClient(TestServer(app_for(env))) as client:
        status, body = await invoke(client, env, "cron_list", {}, internal=case != "browser")
    assert status == (503 if case == "metadata" else 403), body
    opening.assert_not_called()
    assert not (env.home / "crons.json").exists()
    assert not (env.home / ".crons.lock").exists()


@pytest.mark.asyncio
async def test_unknown_tool_and_malformed_arguments_never_open_store(env, monkeypatch):
    opening = mock.Mock(side_effect=AssertionError("invalid dispatch"))
    monkeypatch.setattr(mcp_cron, "CronService", opening)
    async with TestClient(TestServer(app_for(env))) as client:
        for name, arguments in (("owner_delete_all", {}), ("cron_add", [])):
            status, body = await invoke(client, env, name, arguments)
            assert status == 400 and body["code"] == "invalid_cron_tool"
    opening.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        {"error": "gateway unavailable", "transport_error": True},
        {"error": "member session unverified"},
        {"unexpected": "response"},
    ],
)
def test_proxy_failure_never_falls_back_or_retries(env, monkeypatch, response):
    monkeypatch.setattr(paths, "private_runtime_log_dir", lambda: env.home / "agent-logs")
    post = mock.Mock(return_value=response)
    opening = mock.Mock(side_effect=AssertionError("sandbox must not write cron files"))
    monkeypatch.setattr(mcp_core, "_post", post)
    monkeypatch.setattr(mcp_cron, "CronService", opening)
    result = mcp_cron._call_tool("cron_add", {"name": "once", "message": "go", "every": 120})
    assert result.startswith("Error:")
    if response.get("transport_error") or "error" not in response:
        assert "check cron_list" in result.lower()
    post.assert_called_once()
    opening.assert_not_called()


def test_private_proxy_retains_direct_channel_default(env, monkeypatch):
    monkeypatch.setattr(paths, "private_runtime_log_dir", lambda: env.home / "agent-logs")
    monkeypatch.setenv("KIROCREW_CHANNEL_ID", "C0ABC123")
    post = mock.Mock(return_value={"result": "Added job"})
    monkeypatch.setattr(mcp_core, "_post", post)
    mcp_cron._call_tool("cron_add", {"name": "channel", "message": "go", "every": 120})
    assert post.call_args.args[1]["arguments"]["channel"] == "C0ABC123"


def test_global_v1_direct_runtime_keeps_local_dispatch(env, monkeypatch):
    monkeypatch.setattr(paths, "private_runtime_log_dir", lambda: None)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global")
    post = mock.Mock(side_effect=AssertionError("V1 must not require private proxy"))
    monkeypatch.setattr(mcp_core, "_post", post)
    result = mcp_cron._call_tool("cron_add", {"name": "global", "message": "go", "every": 120})
    assert result.startswith("Added job"), result
    job = CronService(base_dir=env.home).list_jobs()[0]
    assert job.memory_store == "" and job.member_id == ""
    post.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_failure_clears_request_caller_and_reports_uncertain_outcome(
    env, monkeypatch
):
    seen = []

    def fail(name, arguments):
        seen.append(current_caller().session_key)
        raise OSError("fixture store outage")

    monkeypatch.setattr(mcp_cron, "_call_tool_locally", fail)
    outer = CallerContext(session_key="dashboard:outside")
    set_current_caller(outer)
    async with TestClient(TestServer(app_for(env))) as client:
        status, body = await invoke(client, env, "cron_list", {})
    assert status == 503 and body["code"] == "cron_tool_failed"
    assert "Check cron_list" in body["error"]
    assert seen == [env.identities["alice"].key]
    assert current_caller() is outer
