"""Organization tools derive their actor from a verified private session."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request
from member_memory_helpers import member_proof as _member_proof

from kiro_crew import mcp_work, organization_tools
from kiro_crew.dashboard.handlers import organization as routes
from kiro_crew.organization import OWNER, OrganizationError, OrganizationStore
from kiro_crew.organization_policy import role_spec
from kiro_crew.organization_service import owner_chat_request
from kiro_crew.validation import ValidationError

env = _member_env
member_proof = _member_proof


@pytest.mark.parametrize("guarded", (False, True))
def test_org_context_does_not_teach_unavailable_worker_dispatch(tmp_path, monkeypatch, guarded):
    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    monkeypatch.setattr("kiro_crew.context._member_backend_can_dispatch", lambda _cfg: True)
    monkeypatch.setattr(
        "kiro_crew.organization_policy.member_for_session",
        lambda _key: {"role": "engineer"} if guarded else None,
    )
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    context = builder.build_session_context(
        session_key="dashboard:alice", agent="alice", mode="member", blocks_reads=True
    )
    assert ("[CREW MEMBER OPERATING MODE]" in context) is not guarded
    assert ("DISPATCH it: open a worker session with session_create" in context) is not guarded


@pytest.fixture
def team(env, monkeypatch):
    store = OrganizationStore()
    alice = store.enroll(
        OWNER, name="alice", memory_store="member-alice", role="conductor", manager_id=None
    )
    bob = store.enroll(
        OWNER, name="bob", memory_store="member-bob", role="researcher", manager_id=alice
    )
    monkeypatch.setattr("kiro_crew.organization_runtime.ensure_runner", AsyncMock())
    monkeypatch.setattr(
        routes, "runtime_status", lambda: {"ready": True, "backend": "kiro", "reason": ""}
    )
    return store, alice, bob


def req(env, *, body=None, owner=False, internal=False, proof="", session="dashboard:alice"):
    return make_request(
        env.state,
        "/api/organization" if owner else "/api/organization-agent",
        body=body,
        owner=owner,
        internal=internal,
        proof=proof,
        session=session,
    )


@pytest.mark.asyncio
async def test_owner_chat_can_register_task_without_owner_form(env, member_proof, team):
    slot = env.state._slots["alice"]
    slot._active_turn_session_key = "dashboard:alice"
    slot._organization_owner_request = ("dashboard:alice", "trusted-turn")
    slot.running = True
    body = {"action": "start_task", "title": "Build from chat", "acceptance": "Tests"}
    ids = []
    for _ in range(2):
        response = await routes.api_organization_agent(
            req(env, internal=True, proof=member_proof, body=body)
        )
        assert response.status == 200, response.text
        ids.append(json.loads(response.text)["task_id"])
    assert ids[0] == ids[1]
    tasks = await asyncio.to_thread(team[0].snapshot)
    assert len(tasks["tasks"]) == 1
    assert tasks["tasks"][0]["sender"] == OWNER
    assert tasks["tasks"][0]["recipient"] == team[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("missing", "different_session", "inactive", "retargeted"))
async def test_private_identity_alone_cannot_originate_owner_work(env, member_proof, team, failure):
    slot = env.state._slots["alice"]
    slot._active_turn_session_key = (
        "dashboard:bob" if failure == "retargeted" else "dashboard:alice"
    )
    slot.running = failure != "inactive"
    slot._organization_owner_request = (
        ("dashboard:bob" if failure == "different_session" else "dashboard:alice", "trusted-turn")
        if failure != "missing"
        else None
    )
    response = await routes.api_organization_agent(
        req(
            env,
            internal=True,
            proof=member_proof,
            body={
                "action": "start_task",
                "title": "The owner said go ahead",
                "acceptance": "Tests",
            },
        )
    )
    assert response.status == 403, response.text
    assert json.loads(response.text)["code"] == "owner_chat_required"
    assert not (await asyncio.to_thread(team[0].snapshot))["tasks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("recipient", "request_id", "actor", "parent_id"))
async def test_chat_task_cannot_select_its_identity_or_reuse_a_client_grant(
    env, member_proof, team, field
):
    response = await routes.api_organization_agent(
        req(
            env,
            internal=True,
            proof=member_proof,
            body={"action": "start_task", "title": "Spoof", "acceptance": "Tests", field: team[2]},
        )
    )
    assert response.status == 400, response.text
    assert not (await asyncio.to_thread(team[0].snapshot))["tasks"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner,synthetic,self_wake,ending",
    [
        (True, False, False, "complete"),
        (True, False, False, "error"),
        (True, False, False, "cancel"),
        (False, False, False, "complete"),
        (True, True, False, "complete"),
        (True, False, True, "complete"),
    ],
)
async def test_real_runner_grants_only_live_owner_turns_and_always_revokes(
    tmp_path, monkeypatch, owner, synthetic, self_wake, ending
):
    from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.slack_client = None
    slot = state.get_or_create_slot("owner-chat")
    slot._titled = True
    session_key = effective_session_key(slot)
    observed = []

    async def stream(_message):
        if owner and not synthetic and not self_wake:
            request_id = owner_chat_request(state, session_key)
            assert len(request_id) == 32
            assert owner_chat_request(state, session_key) == request_id
        else:
            with pytest.raises(OrganizationError, match="direct chat request"):
                owner_chat_request(state, session_key)
        observed.append(True)
        if ending == "error":
            raise RuntimeError("Provider failed after a tool call")
        if ending == "cancel":
            raise asyncio.CancelledError
        yield AcpEvent(kind=EVENT_COMPLETE)

    client = MagicMock()
    client.stream = stream
    client.stream_command = stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.record_failure = AsyncMock()
    slot.task = asyncio.create_task(
        chat_runner._run_chat(
            state,
            slot,
            "Go ahead with the task we discussed",
            _directive_user_origin=True,
            _organization_owner_origin=owner,
            _synthetic_payload=synthetic,
            _directive_self_wake=self_wake,
        )
    )
    try:
        await asyncio.wait_for(slot.task, timeout=10)
    except asyncio.CancelledError:
        assert ending == "cancel"
    assert observed == [True]
    assert slot._organization_owner_request is None
    with pytest.raises(OrganizationError):
        owner_chat_request(state, session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("direct", "busy", "subagents"))
@pytest.mark.parametrize("caller,internal", (("owner", False), ("guest", False), ("owner", True)))
async def test_chat_ingress_verifies_owner_independently_of_human_origin(
    tmp_path, monkeypatch, mode, caller, internal
):
    from kiro_crew.dashboard import chat_handlers, chat_runner

    state = _make_state(tmp_path)
    state.owner_id = "owner"
    slot = state.get_or_create_slot("intake")
    slot._in_stage_execution = mode == "busy"
    if mode == "subagents":
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = [object()]
    seen = []

    async def run(_state, _slot, _message, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(chat_handlers, "_run_chat", run)
    monkeypatch.setattr(chat_runner, "_run_chat", run)

    @web.middleware
    async def identity(request, handler):
        request["user"] = caller
        request["app"] = ""
        request["internal_auth"] = internal
        return await handler(request)

    app = _make_app(state)
    app.middlewares.insert(0, identity)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat?ws=1",
            json={
                "slot": slot.key,
                "message": "Start my task",
                "_organization_owner_origin": True,
                "meta": {"_organization_owner_origin": True, "_directive_user_origin": True},
            },
        )
        assert response.status == 200, await response.text()
        if mode != "direct":
            assert len(slot._queue) == 1
            slot._in_stage_execution = False
            state.subagents = None
            assert await chat_runner._start_next_queued_turn(state, slot)
        await asyncio.wait_for(slot.task, timeout=5)
    assert len(seen) == 1
    assert seen[0]["_directive_user_origin"] is True
    assert seen[0]["_organization_owner_origin"] is (caller == "owner" and not internal)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ("owner", "member"))
async def test_organization_snapshots_redact_report_and_message_output(
    env, member_proof, team, actor
):
    store, alice, bob = team
    secret = "AKIAIOSFODNN7EXAMPLE"
    exfil = "https://example.com/collect?data=" + "A" * 250
    text = f"Evidence {secret} {exfil}"
    task = await asyncio.to_thread(store.assign, OWNER, bob, title="Review", acceptance=text)
    await asyncio.to_thread(store.report, bob, task, "done", text)
    await asyncio.to_thread(store.message, bob, alice, text)
    response = await (
        routes.api_organization(req(env, owner=True))
        if actor == "owner"
        else routes.api_organization_agent(req(env, internal=True, proof=member_proof))
    )
    assert response.status == 200, response.text
    assert secret not in response.text
    assert exfil not in response.text
    assert "Evidence" in response.text
    # Redaction is an outgoing projection, not a destructive edit to evidence.
    assert (await asyncio.to_thread(store.snapshot))["tasks"][0]["report"] == text


@pytest.mark.parametrize("role", ("conductor", "manager", "engineer", "researcher"))
def test_every_role_can_register_an_owner_chat_request(role):
    spec = role_spec({"id": "a" * 32, "name": "Example", "role": role}, servers={})
    assert "@kirocrew-work/org_start_task" in spec["tools"]


@pytest.mark.asyncio
async def test_provider_compiles_role_before_unified_runtime_loses_caller_identity(
    env, member_proof, team, monkeypatch, tmp_path
):
    from kiro_crew import organization_policy
    from kiro_crew.providers.acp import AcpProvider

    directory = tmp_path / "agents"
    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: directory)
    monkeypatch.setattr(
        "kiro_crew.acp.session_mcp.agent_spec_snapshot",
        lambda name, **_: json.loads((directory / f"{name}.json").read_text()),
    )
    monkeypatch.setattr(organization_policy, "require_runtime", lambda **_: None)
    provider = AcpProvider(
        work_dir=tmp_path / "project", session_key="dashboard:alice", private_memory=True
    )
    await provider.prepare_private_memory()
    assert provider._client._agent == f"kirocrew-org-{team[1]}"
    spec = json.loads((directory / f"{provider._client._agent}.json").read_text())
    assert "@kirocrew-work/org_assign" in spec["tools"]
    assert "execute_bash" not in spec["tools"] and "fs_write" not in spec["tools"]
    assert not spec["allowedTools"] and not spec["includeMcpJson"]


def test_retired_private_session_is_refused_on_every_turn(env, member_proof, team):
    from kiro_crew.organization_policy import member_for_session

    store, alice, bob = team
    store.retire(OWNER, bob)
    store.retire(OWNER, alice)
    with pytest.raises(OrganizationError, match="retired"):
        member_for_session("dashboard:alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ("provider", "client"))
@pytest.mark.parametrize("project_spec", (None, "override", "unreadable"))
async def test_private_launch_checks_the_spawn_resolvers_project_precedence(
    env, member_proof, team, monkeypatch, tmp_path, project_spec, launch
):
    from kiro_crew import organization_policy
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.providers.acp import AcpProvider

    directory = tmp_path / "agents"
    project = tmp_path / "project"
    project_agents = project / ".kiro" / "agents"
    project_agents.mkdir(parents=True)
    name = f"kirocrew-org-{team[1]}"
    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: directory)
    monkeypatch.setattr(session_mcp, "agent_spec_path", lambda agent: directory / f"{agent}.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr(organization_policy, "require_runtime", lambda **_: None)

    class LaunchReached(Exception):
        pass

    after_preparation = MagicMock(side_effect=LaunchReached)
    if launch == "provider":
        provider = AcpProvider(work_dir=project, session_key="dashboard:alice", private_memory=True)
        monkeypatch.setattr(provider, "_apply_effort_overlay", lambda: None)
        monkeypatch.setattr(provider, "_apply_tool_search_overlay", lambda: None)
        monkeypatch.setattr(provider, "_start_kiro_runtime", after_preparation)
        client = provider._client
        start = provider.start
    else:
        client = AcpClient(work_dir=project, session_key="dashboard:alice", private_memory=True)
        monkeypatch.setattr(
            "kiro_crew.acp.client.assert_voice_runtime_outside_agent_workspace", after_preparation
        )
        start = client._spawn

    if project_spec is not None:
        (project_agents / f"{name}.json").write_text(
            (
                json.dumps({"name": name, "tools": ["execute_bash"]})
                if project_spec == "override"
                else "{"
            ),
            encoding="utf-8",
        )
        with pytest.raises(OrganizationError) as caught:
            await asyncio.wait_for(start(), timeout=5)
        assert caught.value.code == "organization_spec_shadowed"
        after_preparation.assert_not_called()
    else:
        with pytest.raises(LaunchReached):
            await asyncio.wait_for(start(), timeout=5)
        after_preparation.assert_called_once()
        assert client._agent == name
        spec = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
        assert "@kirocrew-work/org_assign" in spec["tools"]
        assert "execute_bash" not in spec["tools"] and "fs_write" not in spec["tools"]
        assert not spec["allowedTools"] and not spec["includeMcpJson"]


@pytest.mark.asyncio
async def test_verified_member_can_delegate_and_read_only_its_view(env, member_proof, team):
    store, alice, bob = team
    goal = store.assign(OWNER, alice, title="Find evidence", acceptance="A cited finding")
    response = await routes.api_organization_agent(
        req(
            env,
            internal=True,
            proof=member_proof,
            body={
                "action": "assign",
                "recipient": bob,
                "parent_id": goal,
                "title": "Research",
                "acceptance": "A primary source",
            },
        )
    )
    assert response.status == 200, response.text
    task_id = json.loads(response.text)["task_id"]
    assert (
        next(task for task in store.snapshot()["tasks"] if task["id"] == task_id)["sender"] == alice
    )
    response = await routes.api_organization_agent(req(env, internal=True, proof=member_proof))
    assert response.status == 200
    assert json.loads(response.text)["actor"] == alice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "internal,proof_kind", [(False, "valid"), (True, "none"), (True, "forged")]
)
async def test_cookie_or_shared_secret_cannot_choose_a_member(
    env, member_proof, team, internal, proof_kind
):
    proof = {"valid": member_proof, "none": "", "forged": "forged"}[proof_kind]
    response = await routes.api_organization_agent(
        req(
            env,
            internal=internal,
            proof=proof,
            body={"action": "message", "recipient": team[2], "text": "Spoof"},
        )
    )
    assert response.status == 403
    assert not team[0].snapshot()["messages"]


@pytest.mark.asyncio
async def test_private_proof_cannot_borrow_a_different_session_header(env, member_proof, team):
    response = await routes.api_organization_agent(
        req(env, internal=True, proof=member_proof, session="dashboard:bob")
    )
    assert response.status == 403


@pytest.mark.asyncio
async def test_member_cannot_submit_an_actor_or_change_policy(env, member_proof, team):
    for body in (
        {"action": "message", "recipient": team[2], "text": "Spoof", "actor": OWNER},
        {"action": "configure", "enabled": False},
        {"action": "create_member", "role": "conductor"},
    ):
        response = await routes.api_organization_agent(
            req(env, internal=True, proof=member_proof, body=body)
        )
        assert response.status in (400, 403)
    assert not team[0].snapshot()["messages"]


@pytest.mark.asyncio
async def test_member_and_app_cannot_use_owner_surface(env, member_proof, team):
    request = req(env, owner=True, internal=True, proof=member_proof)
    assert (await routes.api_organization(request)).status == 403
    request = req(env, owner=True)
    request["app"] = "some-app"
    assert (await routes.api_organization(request)).status == 403
    request = req(env, owner=True)
    request["user"] = "someone-else"
    assert (await routes.api_organization(request)).status == 403
    response = await routes.api_organization(req(env, owner=True))
    assert response.status == 200, response.text


@pytest.mark.asyncio
async def test_retired_member_proof_cannot_resume_organization_actions(env, member_proof, team):
    store, alice, bob = team
    store.retire(OWNER, bob)
    store.retire(OWNER, alice)
    response = await routes.api_organization_agent(req(env, internal=True, proof=member_proof))
    assert response.status == 403


@pytest.mark.parametrize("name", organization_tools.ORGANIZATION_TOOLS)
def test_every_organization_tool_rejects_an_unverified_identity(name, monkeypatch):
    monkeypatch.setattr(mcp_work, "_strict_caller", lambda: ("", "identity refused"))
    dispatch = []
    monkeypatch.setattr(
        organization_tools, "dispatch", lambda *args, **kwargs: dispatch.append(args)
    )
    assert mcp_work._call_tool_inner(name, {}) == "identity refused"
    assert not dispatch


@pytest.mark.parametrize("name", organization_tools.ORGANIZATION_TOOLS)
def test_no_schema_accepts_an_actor_memory_or_session_override(name):
    schema = organization_tools.ORGANIZATION_SCHEMAS[name]
    assert not {"actor", "memory_store", "session", "session_key"} & {f.name for f in schema.fields}
    with pytest.raises(ValidationError):
        organization_tools.validate(name, {"actor": OWNER})


def test_tool_transport_uses_the_same_verified_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(
        organization_tools,
        "_post",
        lambda path, payload, **kw: calls.append((path, payload, kw)) or {"ok": True},
    )
    result = organization_tools.dispatch(
        "org_message", {"recipient": "owner", "text": "A question"}, session_key="verified-caller"
    )
    assert json.loads(result) == {"ok": True}
    assert calls == [
        (
            "/api/organization-agent",
            {"action": "message", "recipient": "owner", "text": "A question"},
            {"session_key": "verified-caller"},
        )
    ]


@pytest.mark.parametrize(
    "name,args",
    [
        pytest.param("org_message", {"recipient": "owner", "text": "A question"}, id="post"),
        pytest.param("org_inbox", {}, id="get"),
    ],
)
@pytest.mark.parametrize("expected_outcome", ["failed", "completed"])
def test_gateway_results_preserve_payload_and_sel_outcome(
    monkeypatch, name, args, expected_outcome
):
    from kiro_crew import mcp_core, mcp_shared

    session_key = "dashboard:organization-audit"
    secret = "AKIAIOSFODNN7EXAMPLE"
    code, field = (
        ("outside_reporting_line", "recipient")
        if name == "org_message"
        else ("member_session_unverified", "session_key")
    )
    response = (
        {
            "error": f"Request refused: {secret}",
            "code": code,
            "field": field,
            "details": {"message": f"Diagnostic: {secret}", "attempt": 1},
        }
        if expected_outcome == "failed"
        else {"ok": True, "messages": [{"text": "Résumé", "error": "quoted report data"}]}
    )
    get, post = Mock(return_value=response), Mock(return_value=response)
    sink = Mock()
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: session_key)
    monkeypatch.setattr(mcp_work, "_resolve_session_key", lambda: session_key)
    monkeypatch.setattr(organization_tools, "_get", get)
    monkeypatch.setattr(organization_tools, "_post", post)
    monkeypatch.setattr(mcp_shared, "sel", lambda: sink)

    result = mcp_work._call_tool(name, args)

    sink.log_tool_invocation.assert_called_once()
    audit = sink.log_tool_invocation.call_args.kwargs
    assert audit["outcome"] == expected_outcome
    assert audit["session_key"] == session_key
    assert audit["source"] == "mcp"
    assert audit["tool_name"] == name
    assert audit["downstream_service"] == "kirocrew-work"
    if name == "org_inbox":
        get.assert_called_once_with("/api/organization-agent", session_key=session_key)
        post.assert_not_called()
    else:
        post.assert_called_once_with(
            "/api/organization-agent", {"action": "message", **args}, session_key=session_key
        )
        get.assert_not_called()
    if expected_outcome == "failed":
        assert result.startswith("Error:")
        payload = json.loads(result.removeprefix("Error:"))
        assert set(payload) == set(response)
        assert payload["code"] == code
        assert payload["field"] == field
        assert payload["error"].startswith("Request refused: ")
        assert set(payload["details"]) == {"message", "attempt"}
        assert payload["details"]["message"].startswith("Diagnostic: ")
        assert payload["details"]["attempt"] == 1
        assert secret not in result
        assert secret not in audit["error"]
        assert audit["error"] == result[:500]
    else:
        assert json.loads(result) == response
        assert result == json.dumps(response, ensure_ascii=False, indent=2)
        assert audit["error"] == ""


@pytest.mark.parametrize("role", ("conductor", "manager", "researcher"))
def test_protected_roles_have_no_execution_or_mutation_tool(role):
    spec = role_spec({"id": "a" * 32, "name": "Example", "role": role}, servers={})
    assert not {"execute_bash", "fs_write", "code", "session", "introspect"} & set(spec["tools"])
    assert spec["allowedTools"] == []
    assert spec["hooks"] == {}
    assert spec["includeMcpJson"] is False
    assert all(not tool.startswith("@kirocrew-dashboard") for tool in spec["tools"])
    assert "@kirocrew-core/spawn_run" not in spec["tools"]
    assert "@kirocrew-core/send_message" not in spec["tools"]
    assert "@kirocrew-core" not in spec["tools"]


def test_engineer_keeps_implementation_tools_but_no_hiring_or_manager_review():
    spec = role_spec({"id": "a" * 32, "name": "Engineer", "role": "engineer"}, servers={})
    assert {"execute_bash", "fs_write"} <= set(spec["tools"])
    assert "@kirocrew-work/org_report" in spec["tools"]
    assert not {
        "@kirocrew-work/org_hire",
        "@kirocrew-work/org_assign",
        "@kirocrew-work/org_review",
    } & set(spec["tools"])
    assert spec["allowedTools"] == []
