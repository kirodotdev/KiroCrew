"""Member chat dispatch and published task snapshots share the real ledger."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import member_memory_auth, members, work_ledger
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.routes import agents as member_routes
from kiro_crew.member_memory_auth import bind_private_session_store
from kiro_crew.memory_stores import provision_member_memory

MEMBER = "Reviewer"
SLUG = "reviewer"


@pytest.fixture
def reporting_agents(tmp_path, monkeypatch):
    from kiro_crew import agent, agent_discovery
    from kiro_crew.acp import session_mcp

    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", directory)
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", directory)
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _: True)
    (directory / "kirocrew-worker.json").write_text(
        json.dumps(
            {
                "name": "kirocrew-worker",
                "tools": ["@kirocrew-work"],
                "mcpServers": {"kirocrew-work": {"command": "kirocrew", "args": ["mcp-work"]}},
            }
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def member_app(monkeypatch, reporting_agents):
    cfg = KiroCrewConfig.load()
    cfg.agents[MEMBER] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, MEMBER)
    cfg.save()
    key = members.member_slot_key(SLUG, store)

    # Exercise real ownership records and readers; publication atomicity is
    # covered separately and the host may lack renameat2 (older Linux libc).
    def publish_fixture(staging, destination):
        assert not destination.exists()
        staging.rename(destination)

    with monkeypatch.context() as publication:
        publication.setattr(member_memory_auth, "_publish_private_binding_dir", publish_fixture)
        bind_private_session_store(f"dashboard:{key}", store)
    members.write_dm_binding(SLUG, member=MEMBER, slot_key=key, memory_store=store)
    slot = SimpleNamespace(
        key=key,
        mode=members.DM_SLOT_MODE,
        agent=MEMBER,
        memory_store=store,
        running=False,
        _lock=asyncio.Lock(),
    )
    slots = {key: slot}
    app = web.Application()

    @web.middleware
    async def internal_identity(request, handler):
        if request.headers.get("X-Test-Internal"):
            request["internal_auth"] = True
        return await handler(request)

    app.middlewares.append(internal_identity)
    app["state"] = SimpleNamespace(
        owner_id="", _slots=slots, get_slot=slots.get, broadcast_ws=Mock()
    )
    member_routes.register(app)
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    return as_owner(app), cfg, slot


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_private_member_dispatch_binds_before_first_turn_and_reports(member_app, monkeypatch):
    """Real spawn admission and HTTP work routes, with only the LLM replaced."""
    from unittest.mock import AsyncMock

    from test_subagent import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew import context
    from kiro_crew.agent_panel import panel_path
    from kiro_crew.dashboard.handlers import _shared, agent_panel, messaging
    from kiro_crew.dashboard.handlers import work_ledger as work_routes
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_persistence import read_run_memory_store

    app, _, slot = member_app
    parent = f"dashboard:{slot.key}"
    state = app["state"]
    state.conversation_log = None
    state._restricted_keys = set()
    slot.workspace = "default"
    slot.is_restricted = False
    sessions = _mock_sessions()
    sessions._pool_cwd = ""
    state.sessions = sessions
    sessions.get_mirror_link.return_value = None
    sessions.get_approval_policy.return_value = "auto"
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.conversation_log = None
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    state.subagents = manager
    monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: False)
    monkeypatch.setattr(context, "prepare_store_vectors", AsyncMock())

    def publish_fixture(staging, destination):
        assert not destination.exists()
        staging.rename(destination)

    monkeypatch.setattr(member_memory_auth, "_publish_private_binding_dir", publish_fixture)

    async def verified_scope(request):
        # Stand in for the authenticated MCP process envelope; all store and
        # delegation checks below still resolve real protected ownership files.
        key = request.headers.get("X-Test-Verified-Session")
        store = await asyncio.to_thread(context.store_of_session, None, key) if key else None
        return _shared.MemberScope(key, True, store)

    monkeypatch.setattr(_shared, "member_request_scope", verified_scope)
    app.router.add_post("/api/spawn", messaging.api_spawn)
    app.router.add_get("/api/work-ledger/brief", work_routes.api_work_brief)
    app.router.add_post("/api/work-ledger/report", work_routes.api_work_report)
    app.router.add_post("/api/work-ledger/record", work_routes.api_work_ledger_record)
    app.router.add_get("/api/work-ledger", work_routes.api_work_ledger_get)
    agent_panel.register_agent_panel_routes(app)

    def headers(key):
        return {"X-Test-Internal": "1", "X-Session-Key": key, "X-Test-Verified-Session": key}

    work_ledger.ensure_conductor("peer-secret")
    work_ledger.apply_conductor_action(
        "peer-secret", "create", title="Peer task", acceptance={"kind": "human_approval"}
    )
    async with TestClient(TestServer(app)) as client:
        missing = await client.post(
            "/api/agent-panel/publish",
            headers=headers(parent),
            json={"template": "tasks", "data": {"conductor": {"slot_key": "peer-secret"}}},
        )
        assert missing.status == 404, await missing.text()
        assert (await missing.json())["code"] == "no_ledger"
        created = await client.post(
            "/api/work-ledger/record",
            headers=headers(parent),
            json={
                "action": "create",
                "title": "Verify task",
                "acceptance": {"kind": "human_approval", "description": "Report evidence"},
            },
        )
        assert created.status == 200, await created.text()
        item_id = (await created.json())["item"]["item_id"]
        observed = []
        provider = sessions.get_or_create.return_value[0]
        provider.cwd = ""
        provider.capabilities = capabilities_for(ACP_BACKEND_KIRO)
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0
        opaque_key = "abcdef" + "G7H8J9K0L1M2N3P4Q5R6S7T8U9V0W1X2Y3"

        async def stream(*args, **kwargs):
            worker = sessions.get_or_create.call_args.args[0]
            assert work_ledger.read_binding(worker) == (slot.key, item_id)
            assert read_run_memory_store(worker.split(":", 1)[1]) == slot.memory_store
            assert context.store_of_session(None, worker) == slot.memory_store
            manual_bind = await client.post(
                "/api/work-ledger/record",
                headers=headers(parent),
                json={"action": "bind", "item_id": item_id, "worker_session_key": worker},
            )
            assert manual_bind.status == 404
            assert (await manual_bind.json())["code"] == "unknown_worker_session"
            brief = await client.get("/api/work-ledger/brief", headers=headers(worker))
            assert brief.status == 200, await brief.text()
            for status in ("progress", "blocked", "done"):
                report = await client.post(
                    "/api/work-ledger/report",
                    headers=headers(worker),
                    json={
                        "status": status,
                        "summary": f"Worker reports {status}",
                        "artifacts": {"aws_secret_access_key": opaque_key, "tests": "passed"},
                    },
                )
                assert report.status == 200, await report.text()
                board = await (await client.get("/api/work-ledger", headers=headers(parent))).json()
                assert board["items"][0]["artifacts"]["aws_secret_access_key"] == opaque_key
                observed.append((board["items"][0]["status"], board["items"][0]["state"]))
                published = await client.post(
                    "/api/agent-panel/publish",
                    headers=headers(parent),
                    json={
                        "template": "tasks",
                        "data": {
                            "conductor": {"slot_key": "peer-secret", "goal": "Invented goal"},
                            "items": [
                                {"item_id": "invented", "status": "done", "state": "accepted"}
                            ],
                            "summary": "Please review progress",
                            "unused": "Not part of the task snapshot",
                        },
                    },
                )
                assert published.status == 200, await published.text()
                panel = await client.get(f"/api/members/{SLUG}/panel?member={MEMBER}")
                assert panel.status == 200, await panel.text()
                snapshot = await panel.json()
                data = snapshot["panel"]["data"]
                assert data["conductor"] == board["conductor"]
                assert data["summary"] == "Please review progress"
                assert set(data) == {"summary", "conductor", "items"}
                assert data["items"][0]["item_id"] == item_id
                assert data["items"][0]["state"] == "open"
                assert (
                    data["items"][0]["artifacts"]["aws_secret_access_key"]
                    == REDACTED_CREDENTIAL_TAG
                )
                assert data["items"][0]["artifacts"]["tests"] == "passed"
                assert opaque_key not in snapshot["html"]
                assert "events" not in data["items"][0]
                assert "worker_session_key" not in data["items"][0]
                assert data["items"][0]["has_worker"] is True
                assert worker not in snapshot["html"]
                assert data["items"][0]["status"] == status
                assert 'id="kt-root"' in snapshot["html"]
            if False:
                yield

        provider.stream.side_effect = stream
        response = await client.post(
            "/api/spawn",
            headers=headers(parent),
            json={
                "task": "Read the brief and report",
                "parent_session": parent,
                "work_item_id": item_id,
            },
        )
        assert response.status == 200, await response.text()
        try:
            await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        finally:
            await manager.cancel_all()
        info = next(iter(manager._agents.values()))
        assert info.error == "", info.error
        assert observed == [("progress", "open"), ("blocked", "open"), ("done", "open")]
        assert info.memory_store == slot.memory_store
        assert info.agent == "kirocrew-worker"
        duplicate = await client.post(
            "/api/spawn",
            headers=headers(parent),
            json={"task": "Duplicate", "parent_session": parent, "work_item_id": item_id},
        )
        assert duplicate.status == 409, await duplicate.text()
        assert (await duplicate.json())["code"] == "already_bound"
        for action in (
            {"action": "verdict", "verdict": "pass"},
            {"action": "close", "state": "accepted"},
        ):
            accepted = await client.post(
                "/api/work-ledger/record",
                headers=headers(parent),
                json={"item_id": item_id, **action},
            )
            assert accepted.status == 200, await accepted.text()
        board = await (await client.get("/api/work-ledger", headers=headers(parent))).json()
        assert board["items"][0]["state"] == "accepted"
        published = await client.post(
            "/api/agent-panel/publish",
            headers=headers(parent),
            json={"template": "tasks", "data": {"summary": "Acceptance recorded"}},
        )
        assert published.status == 200, await published.text()
        accepted_panel = await (
            await client.get(f"/api/members/{SLUG}/panel?member={MEMBER}")
        ).json()
        assert accepted_panel["panel"]["data"]["items"][0]["state"] == "accepted"
        assert opaque_key not in await asyncio.to_thread(
            panel_path(SLUG).read_text, encoding="utf-8"
        )
        borrowed = headers(parent)
        borrowed["X-Session-Key"] = "subagent:" + info.id
        refused = await client.get("/api/work-ledger/brief", headers=borrowed)
        assert refused.status == 403
        assert (await refused.json())["code"] == "member_session_unverified"
        refused_publish = await client.post(
            "/api/agent-panel/publish",
            headers=borrowed,
            json={"template": "tasks", "data": {"items": []}},
        )
        assert refused_publish.status == 403
        assert (await refused_publish.json())["code"] == "member_session_unverified"


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize("failed_stage", ["vectors", "provider", "context"])
async def test_failed_run_preparation_leaves_task_available_for_fresh_dispatch(
    member_app, monkeypatch, failed_stage
):
    from unittest.mock import AsyncMock

    from test_subagent import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew import context
    from kiro_crew.subagent import SubagentManager

    _, _, slot = member_app
    work_ledger.ensure_conductor(slot.key)
    item_id = work_ledger.apply_conductor_action(
        slot.key, "create", title="Retry preparation", acceptance={"kind": "human_approval"}
    )["item"].item_id
    sessions = _mock_sessions()
    sessions._pool_cwd = ""
    sessions.get_approval_policy.return_value = "auto"
    provider = sessions.get_or_create.return_value[0]
    provider.cwd = ""
    provider.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    provider.context_window_tokens = lambda: 0
    provider.context_used_tokens = lambda: 0
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.conversation_log = None
    prepare = AsyncMock()
    monkeypatch.setattr(context, "prepare_store_vectors", prepare)

    def publish_fixture(staging, destination):
        assert not destination.exists()
        staging.rename(destination)

    monkeypatch.setattr(member_memory_auth, "_publish_private_binding_dir", publish_fixture)
    failing = {
        "vectors": prepare,
        "provider": sessions.get_or_create,
        "context": ctx.build_message,
    }[failed_stage]
    failing.side_effect = RuntimeError("Preparation unavailable")
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    manager._spawn_stagger_secs = 0
    monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: False)

    def dispatch():
        return manager.spawn(
            "Read the brief and report",
            parent_session_key=f"dashboard:{slot.key}",
            memory_store=slot.memory_store,
            work_item_id=item_id,
        )

    try:
        first = dispatch()
        assert first is not None
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        assert first.done and "Preparation unavailable" in first.error
        provider.stream.assert_not_called()
        assert work_ledger.read_binding(f"subagent:{first.id}") is None
        assert work_ledger.read_work_item(slot.key, item_id).worker_session_key is None
        failing.side_effect = None

        async def stream(*args, **kwargs):
            worker = sessions.get_or_create.call_args.args[0]
            assert work_ledger.read_binding(worker) == (slot.key, item_id)
            work_ledger.apply_worker_report(slot.key, item_id, status="done", summary="Retried")
            if False:
                yield

        provider.stream.side_effect = stream
        second = dispatch()
        assert second is not None and second.id != first.id
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        assert second.done and not second.error
        assert work_ledger.read_binding(f"subagent:{second.id}") == (slot.key, item_id)
        assert work_ledger.read_work_item(slot.key, item_id).summary == "Retried"
    finally:
        await manager.cancel_all()


def test_task_dispatch_uses_strict_identity_on_the_wire(monkeypatch):
    from unittest.mock import Mock

    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools.spawn import spawn_run

    post = Mock(return_value={"id": "abcdef12"})
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "wrong-parent")
    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda *a, **kw: ("member-own", ""))
    monkeypatch.setattr(mcp_core, "_post", post)
    result = spawn_run("spawn_run", {"task": "Report progress", "work_item_id": "it_12345678"})
    assert "abcdef12" in result
    post.assert_called_once_with(
        "/api/spawn",
        {
            "task": "Report progress",
            "agent": "",
            "parent_session": "member-own",
            "work_item_id": "it_12345678",
        },
        session_key="member-own",
    )
    post.reset_mock()
    assert "requires one task" in spawn_run(
        "spawn_run", {"tasks": ["one", "two"], "work_item_id": "it_12345678"}
    )
    post.assert_not_called()
    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda *a, **kw: ("", "Identity unavailable")
    )
    assert (
        spawn_run("spawn_run", {"task": "one", "work_item_id": "it_12345678"})
        == "Identity unavailable"
    )
    post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize("selected_agent", ["custom-worker", "kirocrew-worker"])
@pytest.mark.parametrize("excludes_report", [False, True])
async def test_incompatible_project_agent_leaves_item_retryable(
    reporting_agents, tmp_path, monkeypatch, selected_agent, excludes_report
):
    """The project spec wins, including when it shadows the shipped worker."""
    from test_subagent import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew.agent_discovery import warm_project_agent_names
    from kiro_crew.subagent import SubagentManager

    parent = "dashboard:project-owner"
    work_ledger.ensure_conductor(parent.removeprefix("dashboard:"))
    item_id = work_ledger.apply_conductor_action(
        parent.removeprefix("dashboard:"),
        "create",
        title="Check project",
        acceptance={"kind": "human_approval"},
    )["item"].item_id
    project = tmp_path / "project"
    specs = project / ".kiro" / "agents"
    specs.mkdir(parents=True)
    spec_path = specs / f"{selected_agent}.json"
    spec = {
        "name": selected_agent,
        "tools": ["@kirocrew-work/work_brief"],
        "mcpServers": {"kirocrew-work": {"command": "kirocrew", "args": ["mcp-work"]}},
    }
    if excludes_report:
        spec["tools"] = ["@kirocrew-work"]
        spec["excludedTools"] = ["@kirocrew-work/work_report"]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    await warm_project_agent_names(str(project), operation="test", source="unknown")
    sessions = _mock_sessions()
    sessions._pool_cwd = str(project)
    sessions.get_approval_policy.return_value = "auto"
    provider = sessions.get_or_create.return_value[0]
    provider.cwd = str(project)
    provider.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    provider.context_window_tokens = lambda: 0
    provider.context_used_tokens = lambda: 0
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.conversation_log = None
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    manager._spawn_stagger_secs = 0

    def dispatch():
        return manager.spawn(
            "Read and report",
            parent_session_key=parent,
            agent="" if selected_agent == "kirocrew-worker" else selected_agent,
            work_item_id=item_id,
        )

    try:
        refused = dispatch()
        assert refused is not None
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        assert refused.done and "work_reporting_unavailable" in refused.error
        sessions.get_or_create.assert_awaited_once()
        provider.stream.assert_not_called()
        assert work_ledger.read_binding(f"subagent:{refused.id}") is None
        assert (
            work_ledger.read_work_item(
                parent.removeprefix("dashboard:"), item_id
            ).worker_session_key
            is None
        )

        spec["tools"] = ["@kirocrew-work"]
        spec["excludedTools"] = []
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        accepted = dispatch()
        assert accepted is not None
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        assert accepted.done and not accepted.error
        assert accepted.agent == selected_agent
        assert work_ledger.read_binding(f"subagent:{accepted.id}") == ("project-owner", item_id)
        assert sessions.get_or_create.await_count == 2
    finally:
        await manager.cancel_all()


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize("pool_has_reporting", [False, True])
async def test_reporting_check_uses_allocated_worker_directory(
    reporting_agents, tmp_path, pool_has_reporting
):
    from test_subagent import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew.subagent import SubagentManager

    conductor = "worker-directory"
    work_ledger.ensure_conductor(conductor)
    item_id = work_ledger.apply_conductor_action(
        conductor, "create", title="Read and report", acceptance={"kind": "human_approval"}
    )["item"].item_id
    pool = tmp_path / "pool"
    allocated = tmp_path / "allocated"
    for directory, reporting in ((pool, pool_has_reporting), (allocated, not pool_has_reporting)):
        specs = directory / ".kiro" / "agents"
        specs.mkdir(parents=True)
        (specs / "kirocrew-worker.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew-worker",
                    "tools": ["@kirocrew-work"] if reporting else ["fs_read"],
                    "mcpServers": {"kirocrew-work": {"command": "kirocrew", "args": ["mcp-work"]}},
                }
            ),
            encoding="utf-8",
        )
    sessions = _mock_sessions()
    sessions._pool_cwd = str(pool)
    sessions.get_approval_policy.return_value = "auto"
    provider = sessions.get_or_create.return_value[0]
    provider.cwd = str(allocated)
    provider.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    provider.context_window_tokens = lambda: 0
    provider.context_used_tokens = lambda: 0
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.conversation_log = None
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    try:
        info = manager.spawn(
            "Read and report", parent_session_key=f"dashboard:{conductor}", work_item_id=item_id
        )
        assert info is not None
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        assert info.done
        assert "cwd" not in sessions.get_or_create.call_args.kwargs
        if pool_has_reporting:
            assert "work_reporting_unavailable" in info.error
            provider.stream.assert_not_called()
            assert work_ledger.read_binding(f"subagent:{info.id}") is None
        else:
            assert not info.error
            assert work_ledger.read_binding(f"subagent:{info.id}") == (conductor, item_id)
            provider.stream.assert_called_once()
    finally:
        await manager.cancel_all()


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_storeless_ledger_worker_uses_own_strict_reporting_identity(
    reporting_agents, monkeypatch
):
    from test_session_sharing import _mock_sessions
    from test_subagent import _mock_ctx_builder_auto_spawn

    from kiro_crew import mcp_work
    from kiro_crew.subagent import SubagentManager

    cfg = KiroCrewConfig.load()
    cfg.agent.session_sharing = True
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    parent = "dashboard:global-conductor"
    work_ledger.ensure_conductor(parent.removeprefix("dashboard:"))
    item_id = work_ledger.apply_conductor_action(
        parent.removeprefix("dashboard:"),
        "create",
        title="Report progress",
        acceptance={"kind": "human_approval"},
    )["item"].item_id
    sessions = _mock_sessions(sharing_eligible=True)
    sessions._pool_cwd = ""
    provider = sessions.get_or_create.return_value[0]
    provider.cwd = ""
    provider.capabilities = capabilities_for(ACP_BACKEND_KIRO)
    provider.context_window_tokens = lambda: 0
    provider.context_used_tokens = lambda: 0
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.conversation_log = None
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", parent)
    reported = []

    def report(path, body, *, session_key):
        assert path == "/api/work-ledger/report"
        conductor, bound_item = work_ledger.read_binding(session_key)
        assert (conductor, bound_item) == ("global-conductor", item_id)
        work_ledger.apply_worker_report(conductor, bound_item, **body)
        reported.append(session_key)
        return {"ok": True}

    monkeypatch.setattr(mcp_work, "_post", report)

    async def stream(*args, **kwargs):
        # Model the dedicated provider's process environment at the transport
        # boundary; exercise the real strict resolver and worker tool dispatch.
        worker = sessions.get_or_create.call_args.args[0]
        with monkeypatch.context() as process:
            process.setenv("KIROCREW_SESSION_KEY", worker)
            result = mcp_work._call_tool_inner(
                "work_report", {"status": "done", "summary": "Scoped report"}
            )
        assert not result.startswith("Error:"), result
        if False:
            yield

    provider.stream.side_effect = stream
    try:
        info = manager.spawn("Read and report", parent_session_key=parent, work_item_id=item_id)
        assert info is not None
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        assert info.done and not info.error
        assert info.memory_store == ""
        sessions.get_subagent_runtime.assert_not_awaited()
        sessions.get_or_create.assert_awaited_once()
        assert reported == [f"subagent:{info.id}"]
        assert (
            work_ledger.read_work_item(parent.removeprefix("dashboard:"), item_id).summary
            == "Scoped report"
        )
    finally:
        await manager.cancel_all()


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_task_closed_during_spawn_approval_never_starts_provider(member_app):
    from test_subagent import _mock_ctx_builder, _mock_sessions

    from kiro_crew.subagent import SubagentManager

    _, _, slot = member_app
    work_ledger.ensure_conductor(slot.key)
    item_id = work_ledger.apply_conductor_action(
        slot.key, "create", title="May be cancelled", acceptance={"kind": "human_approval"}
    )["item"].item_id
    sessions = _mock_sessions()
    sessions.get_approval_policy.return_value = ""
    ctx = _mock_ctx_builder()
    ctx.conversation_log = None
    approval_started = asyncio.Event()
    allow = asyncio.Event()

    async def approve(*args):
        approval_started.set()
        await asyncio.wait_for(allow.wait(), timeout=5)
        return True

    manager = SubagentManager(sessions=sessions, ctx_builder=ctx, on_spawn_approval=approve)
    info = manager.spawn(
        "Execute after approval",
        parent_session_key=f"dashboard:{slot.key}",
        memory_store=slot.memory_store,
        work_item_id=item_id,
    )
    assert info is not None
    try:
        await asyncio.wait_for(approval_started.wait(), timeout=5)
        assert work_ledger.read_binding(f"subagent:{info.id}") is None
        work_ledger.apply_conductor_action(slot.key, "close", item_id=item_id, state="abandoned")
        allow.set()
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        sessions.get_or_create.assert_not_awaited()
        assert info.done and "closed" in info.error
        assert work_ledger.read_binding(f"subagent:{info.id}") is None
    finally:
        allow.set()
        await manager.cancel_all()
