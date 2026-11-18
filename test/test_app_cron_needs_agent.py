"""An app's scheduled model turn names one of the app's own agents.

A job that names no agent runs on the owner's default agent, with every tool the
owner has. So an app may not create one, or clear the agent of one it has, on any
entry it reaches: ``POST /api/crons`` and ``PATCH /api/crons/{id}`` from an app
token, the ``cron_add`` and ``cron_update`` tools from an app's session, and the App
Kit SDK (which the manifest bridge creates through). A job stored before that rule
is refused when it fires. The owner's own jobs are unchanged, a ``command`` or
``script`` job runs no agent and is not judged, and the bundled
``ops-mission-control`` app names its own agents, so an upgrade moves its existing
jobs onto them.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from slack_gateway_cron_exec_coverage_helpers import (
    _cron_message_cb,
    _job,
    _make_orchestrator,
    _message_arm_sessions,
    _mock_dashboard_state,
)

from kiro_crew import mcp_cron, member_memory_auth
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.cron import CronService
from kiro_crew.cron_service.identity import (
    APP_JOB_NEEDS_AGENT,
    app_schedule_lacks_agent,
    app_update_lacks_agent,
)
from kiro_crew.dashboard import session_control, token_auth
from kiro_crew.dashboard.handlers import cron as cron_handlers
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, bind_session_execution
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway_runtime.cron_fire import _app_job_lacks_agent, _CronCapture

APP = "shop"
OWN_AGENT = "shop-helper"
#: An app's session that runs on the app's own agent, and one on the owner's floor.
APP_SESSION = "dashboard:shop-1"
FLOOR_SESSION = "dashboard:shop-2"
OPS = "ops-mission-control"


def _template(name: str) -> ExecutionContext:
    return ExecutionContext(None, MemoryStoreRef("default"), "template", name)


@pytest.fixture
def svc(tmp_path) -> CronService:
    return CronService(base_dir=tmp_path / "crons")


@pytest.fixture
def sel_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import kiro_crew.apps.cron_sdk as cron_sdk_mod
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    # The SDK binds sel at import.
    monkeypatch.setattr(cron_sdk_mod, "sel", lambda: recorder)
    return recorder


def _denials(sel_calls: MagicMock) -> list[tuple[str, str]]:
    return [
        (call.kwargs["caller"], call.kwargs["operation"])
        for call in sel_calls.log_api_access.call_args_list
        if call.kwargs.get("outcome") == "denied"
    ]


# ── The rule ──

SKILL_VIEW = f"{NATIVE_SKILL_ALIAS_PREFIX}0a1b2c3d"


@pytest.mark.parametrize(
    ("fields", "lacks"),
    [
        ({}, True),
        ({"agent_id": "kirocrew"}, True),
        ({"agent_id": "  "}, True),
        ({"agent_id": OWN_AGENT}, False),
        ({"member_id": "m-1"}, False),
        ({"command": "echo hi"}, False),
        ({"script": "job.py:run"}, False),
        ({"agent_sequence": ["a", "b"]}, False),
        # Each dispatching step is judged: one that names no agent runs the floor.
        ({"agent_sequence": ["kirocrew", "b"]}, True),
        ({"agent_id": OWN_AGENT, "agent_sequence": ["a", ""]}, True),
        # One entry is dormant: dispatch falls through to agent_id.
        ({"agent_sequence": ["a"]}, True),
        # A skill view runs the agent it was projected from, which may be the floor.
        ({"agent_id": SKILL_VIEW}, True),
        ({"agent_sequence": [SKILL_VIEW, OWN_AGENT]}, True),
    ],
)
def test_which_schedules_name_no_agent(fields, lacks) -> None:
    agent, member = fields.pop("agent_id", ""), fields.pop("member_id", "")
    assert app_schedule_lacks_agent(agent, member, **fields) is lacks


def test_a_tool_create_runs_on_the_agent_its_session_runs_on() -> None:
    bind_session_execution(APP_SESSION, _template(OWN_AGENT))
    bind_session_execution(FLOOR_SESSION, _template("kirocrew"))

    assert app_schedule_lacks_agent("", "", maker_session=APP_SESSION) is False
    assert app_schedule_lacks_agent("", "", maker_session=FLOOR_SESSION) is True
    # Naming the floor is naming no agent, whatever the session runs on.
    assert app_schedule_lacks_agent("kirocrew", "", maker_session=APP_SESSION) is True


def test_an_update_that_clears_the_agent_lacks_one() -> None:
    job = SimpleNamespace(
        agent_id=OWN_AGENT,
        member_id="",
        execution_context=None,
        agent_sequence=[],
        command="",
        script="",
    )
    assert app_update_lacks_agent(job, "") is True
    assert app_update_lacks_agent(job, OWN_AGENT) is False
    assert app_update_lacks_agent(job, "other-agent") is False
    # An unchanged empty name moves nothing.
    assert app_update_lacks_agent(SimpleNamespace(**{**vars(job), "agent_id": ""}), "") is False


# ── POST and PATCH /api/crons ──


@pytest.fixture
def grant_crons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        token_auth, "_app_api_allowlist", lambda name: ("/api/crons", "/api/crons/*")
    )


async def _crons_request(
    svc: CronService, app_claim: str, method: str, path: str, body: dict
) -> tuple[int, dict]:
    @web.middleware
    async def identity(request: web.Request, handler):
        request["user"] = "local-app"
        request["app"] = app_claim
        if app_claim:
            denied = token_auth._enforce_app_scope(request, app_claim, request.path)
            if denied is not None:
                return denied
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(
        crons=svc, owner_id="", push_refresh=MagicMock(), has_slot=MagicMock(return_value=False)
    )
    app.router.add_post("/api/crons", cron_handlers.api_crons_create)
    app.router.add_patch("/api/crons/{job_id}", cron_handlers.api_cron_update)
    async with TestClient(TestServer(app)) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        resp = await client.request(method, path, json=body)
        return resp.status, await resp.json()


_BODY = {"name": "job", "message": "task", "every": 3600}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [None, "", "kirocrew"])
async def test_an_app_token_cannot_schedule_a_job_with_no_agent(
    svc, grant_crons, sel_calls, agent
) -> None:
    body = dict(_BODY) if agent is None else {**_BODY, "agent": agent}

    status, payload = await _crons_request(svc, APP, "POST", "/api/crons", body)

    assert (status, payload["code"]) == (400, "app_cron_agent_required"), payload
    assert payload["error"] == APP_JOB_NEEDS_AGENT
    assert svc.list_jobs() == []
    assert _denials(sel_calls) == [(f"app:{APP}", "crons.create")]


@pytest.mark.asyncio
async def test_an_app_token_cannot_clear_its_jobs_agent(svc, grant_crons, sel_calls) -> None:
    status, payload = await _crons_request(
        svc, APP, "POST", "/api/crons", {**_BODY, "agent": OWN_AGENT}
    )
    assert status == 200, payload
    job_id = payload["id"]

    status, payload = await _crons_request(svc, APP, "PATCH", f"/api/crons/{job_id}", {"agent": ""})

    assert (status, payload["code"]) == (400, "app_cron_agent_required"), payload
    (job,) = svc.list_jobs()
    assert (job.agent_id, job.execution_context["template_id"]) == (OWN_AGENT, OWN_AGENT)
    assert (f"app:{APP}", "crons.update") in _denials(sel_calls)


@pytest.mark.asyncio
async def test_the_owner_still_schedules_and_clears_with_no_agent(svc, sel_calls) -> None:
    status, payload = await _crons_request(svc, "", "POST", "/api/crons", dict(_BODY))
    assert status == 200, payload
    status, payload = await _crons_request(
        svc, "", "PATCH", f"/api/crons/{payload['id']}", {"agent": ""}
    )
    assert status == 200, payload

    (job,) = svc.list_jobs()
    assert (job.created_by, job.agent_id) == ("", "")
    assert _denials(sel_calls) == []


# ── POST /api/crons/tools ──


@pytest.fixture
def tool_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    async def _store(request: web.Request, state: object, operation: str):
        return "", None

    monkeypatch.setattr(cron_handlers, "resolve_lesson_memory_store", _store)
    monkeypatch.setattr(session_control, "is_person_tab", lambda state, session_key: False)
    monkeypatch.setattr(
        member_memory_auth,
        "memory_request_identity",
        lambda request: (request.headers.get("X-Session-Key", ""), True),
    )
    calls: list[tuple[str, dict]] = []

    def _local(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "ok"

    monkeypatch.setattr(mcp_cron, "_call_tool_locally", _local)
    bind_session_execution(APP_SESSION, _template(OWN_AGENT))
    bind_session_execution(FLOOR_SESSION, _template("kirocrew"))
    return calls


async def _tool(
    app_claim: str, session: str, name: str, arguments: dict, crons=None
) -> tuple[int, dict]:
    @web.middleware
    async def internal(request: web.Request, handler):
        request["internal_auth"] = True
        if app_claim:
            request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[internal])
    app["state"] = SimpleNamespace(push_refresh=MagicMock(), _slots={})
    if crons is not None:
        app["state"].crons = crons
    app.router.add_post("/api/crons/tools", cron_handlers.api_cron_tools)
    async with TestClient(TestServer(app)) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        resp = await client.post(
            "/api/crons/tools",
            json={"name": name, "arguments": arguments},
            headers={"X-Session-Key": session},
        )
        return resp.status, await resp.json()


_ADD = {"name": "job", "message": "task", "every": 3600}


@pytest.mark.asyncio
async def test_cron_add_from_an_app_session_on_the_floor_is_refused(tool_calls, sel_calls) -> None:
    status, payload = await _tool(APP, FLOOR_SESSION, "cron_add", dict(_ADD))

    assert (status, payload) == (200, {"result": f"Error: {APP_JOB_NEEDS_AGENT}"})
    assert tool_calls == []
    assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_claim", "session", "extra"),
    [
        # The app's session runs on the app's agent, so the job does too.
        (APP, APP_SESSION, {}),
        (APP, FLOOR_SESSION, {"agent": OWN_AGENT}),
        (APP, FLOOR_SESSION, {"command": "echo hi"}),
        # The owner's own session names no agent, as before.
        ("", "dashboard:mine", {}),
    ],
)
async def test_cron_add_still_runs_for_every_other_caller(
    tool_calls, sel_calls, app_claim, session, extra
) -> None:
    arguments = {**_ADD, **extra}

    status, payload = await _tool(app_claim, session, "cron_add", arguments)

    assert (status, payload) == (200, {"result": "ok"})
    assert [name for name, _ in tool_calls] == ["cron_add"]
    assert _denials(sel_calls) == []


@pytest.mark.asyncio
async def test_a_run_of_a_job_an_app_session_made_is_held_to_the_rule(
    tool_calls, sel_calls
) -> None:
    """The run carries no app claim and its job no ``app:`` tag; its record names the app."""
    run = "cron:a1b2c3d4"
    bind_session_execution(
        run, ExecutionContext(None, MemoryStoreRef("default"), "template", OWN_AGENT, app=APP)
    )

    status, payload = await _tool("", run, "cron_add", {**_ADD, "agent": "kirocrew"})
    assert payload == {"result": f"Error: {APP_JOB_NEEDS_AGENT}"}
    assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]

    # With no agent the job runs on the run's own template, the app's agent.
    status, payload = await _tool("", run, "cron_add", dict(_ADD))
    assert payload == {"result": "ok"}
    assert [name for name, _ in tool_calls] == ["cron_add"]


@pytest.mark.asyncio
async def test_cron_update_from_an_app_session_cannot_clear_the_agent(
    svc, tool_calls, sel_calls
) -> None:
    job = await svc.add_job_async("job", "task", every_secs=3600, agent_id=OWN_AGENT)

    status, payload = await _tool(
        APP, APP_SESSION, "cron_update", {"job_id": job.id, "agent": ""}, crons=svc
    )
    assert payload == {"result": f"Error: {APP_JOB_NEEDS_AGENT}"}
    status, payload = await _tool(
        APP, APP_SESSION, "cron_update", {"job_id": job.id, "message": "new"}, crons=svc
    )
    assert payload == {"result": "ok"}
    assert [name for name, _ in tool_calls] == ["cron_update"]


# ── The App Kit SDK ──


def test_the_sdk_refuses_a_model_job_with_no_agent(svc, sel_calls) -> None:
    sdk = CronSDK(APP, svc)

    with pytest.raises(ValueError, match="must name one of the app's own agents"):
        sdk.add_job(f"{APP}/poll", "go", every_secs=600)
    assert svc.list_jobs() == []
    assert ("cron_sdk", "cron_agent_vetted") in _denials(sel_calls)

    job = sdk.add_job(f"{APP}/poll", "go", every_secs=600, agent=OWN_AGENT)
    with pytest.raises(ValueError, match="must name one of the app's own agents"):
        sdk.update_job(job.id, agent_id="")
    assert sdk.update_job(job.id, message="changed").message == "changed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("command_crons_run_here")
async def test_the_sdk_still_creates_a_command_job_with_no_agent(svc) -> None:
    job = await CronSDK(APP, svc).add_job_async(f"{APP}/ping", "", every_secs=600, command="true")
    assert (job.created_by, job.agent_id, job.command) == (f"app:{APP}", "", "true")


# ── At fire time ──


def _capture(agent: str = "kirocrew", *, substituted: bool = False) -> _CronCapture:
    return _CronCapture("cron:j1", "go", [], _template(agent), "default", agent, substituted)


def _orch(slots: dict | None = None, jobs: list | None = None) -> SimpleNamespace:
    state = SimpleNamespace(_slots=slots or {}, crons=SimpleNamespace(_jobs=jobs or []))
    return SimpleNamespace(dashboard_state=state)


@pytest.mark.parametrize(
    ("job", "cap", "lacks"),
    [
        ({"created_by": f"app:{APP}"}, _capture(), True),
        ({"created_by": f"app:{APP}"}, _capture("my-default", substituted=True), True),
        ({"created_by": f"app:{APP}", "agent_id": OWN_AGENT}, _capture(OWN_AGENT), False),
        ({"created_by": f"app:{APP}", "command": "true"}, _capture(), False),
        ({"created_by": f"app:{APP}", "script": "j.py:run"}, _capture(), False),
        (
            {"created_by": f"app:{APP}", "agent_sequence": ["kirocrew", OWN_AGENT]},
            _CronCapture(
                "cron:j1",
                "go",
                ["kirocrew", OWN_AGENT],
                _template(OWN_AGENT),
                "default",
                OWN_AGENT,
                False,
            ),
            True,
        ),
        (
            {"created_by": f"app:{APP}", "agent_sequence": [OWN_AGENT, "other-agent"]},
            _CronCapture(
                "cron:j1",
                "go",
                [OWN_AGENT, "other-agent"],
                _template(OWN_AGENT),
                "default",
                OWN_AGENT,
                False,
            ),
            False,
        ),
        # The owner's own job keeps running on the default agent.
        ({"created_by": ""}, _capture(), False),
        ({"created_by": "U_OWNER"}, _capture("my-default", substituted=True), False),
    ],
)
def test_which_fires_are_refused(job, cap, lacks) -> None:
    assert _app_job_lacks_agent(_orch(), _job(**job), cap) is lacks


def test_a_job_whose_capture_names_an_app_is_the_apps() -> None:
    job = _job(created_by="", session_key="cron:gone1234")
    cap = _CronCapture(
        "cron:j1",
        "go",
        [],
        ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew", app=APP),
        "default",
        "kirocrew",
        False,
    )
    assert _app_job_lacks_agent(_orch(), job, cap) is True


def test_a_job_an_apps_session_made_is_judged_by_its_maker() -> None:
    slots = {"shop-2": SimpleNamespace(_app=APP, linked_session_key="")}
    made_by_app = _job(created_by="", session_key=FLOOR_SESSION)
    made_by_owner = _job(created_by="", session_key="dashboard:mine")

    assert _app_job_lacks_agent(_orch(slots), made_by_app, _capture()) is True
    assert _app_job_lacks_agent(_orch(slots), made_by_owner, _capture()) is False


def test_a_job_chain_an_apps_job_started_stays_the_apps() -> None:
    from kiro_crew.dashboard.token_auth_callers import app_owner_of_session_key

    # A is the app's; B was made by A's run, C by B's run. Neither carries a stamp.
    a = SimpleNamespace(
        id="aaaa0001", created_by=f"app:{APP}", session_key="", execution_context=None
    )
    b = SimpleNamespace(
        id="bbbb0002", created_by="", session_key="cron:aaaa0001:r1", execution_context={}
    )
    c = SimpleNamespace(
        id="cccc0003", created_by="", session_key="cron:bbbb0002:r2", execution_context={}
    )
    mine = SimpleNamespace(
        id="dddd0004", created_by="", session_key="dashboard:mine", execution_context={}
    )
    loop = SimpleNamespace(
        id="eeee0005", created_by="", session_key="cron:eeee0005:r3", execution_context={}
    )
    jobs = [a, b, c, mine, loop]

    assert app_owner_of_session_key(None, "cron:cccc0003:r9", jobs) == APP
    assert app_owner_of_session_key(None, "cron:bbbb0002", jobs) == APP
    assert app_owner_of_session_key(None, "cron:dddd0004:r9", jobs) == ""
    # No depth makes a descendant the owner's: a chain twelve jobs deep is still the app's.
    deep = [a]
    for n in range(12):
        deep.append(
            SimpleNamespace(
                id=f"f{n:07d}",
                created_by="",
                session_key=f"cron:{deep[-1].id}:r",
                execution_context={},
            )
        )
    assert app_owner_of_session_key(None, f"cron:{deep[-1].id}:r9", deep) == APP
    # A maker cycle ends once every job in it was visited.
    assert app_owner_of_session_key(None, "cron:eeee0005:r9", jobs) == ""
    # The fire-time check reads the same chain for a job made by such a run.
    made_by_chain = _job(created_by="", session_key="cron:cccc0003:r4")
    assert _app_job_lacks_agent(_orch({}, jobs), made_by_chain, _capture()) is True


def _fire_orch() -> object:
    orch = _make_orchestrator()
    orch.sessions = _message_arm_sessions()
    orch.dashboard_state = _mock_dashboard_state()
    orch.slack = MagicMock(
        open_dm=AsyncMock(return_value="D1"),
        post_blocks=AsyncMock(return_value="ts1"),
        post_message=AsyncMock(),
    )
    return orch


@pytest.mark.asyncio
async def test_an_apps_stored_job_with_no_agent_is_refused_when_it_fires() -> None:
    orch = _fire_orch()
    job = _job(id="jn1", name="old app job", session_key="", created_by=f"app:{APP}")

    orch._alert_cron_failure = AsyncMock()
    async with _cron_message_cb(orch, result_text="ran") as callback:
        assert await callback(job) is None
        turn = gw._cron_stream_with_posttoken_resume

    assert (job.last_status, job.last_error) == ("error", APP_JOB_NEEDS_AGENT)
    assert job.fire_time_denied is True
    turn.assert_not_awaited()
    orch._alert_cron_failure.assert_awaited_once_with(job, APP_JOB_NEEDS_AGENT, denied=True)


@pytest.mark.asyncio
async def test_the_owners_job_with_no_agent_still_fires() -> None:
    orch = _fire_orch()
    job = _job(id="jn2", name="owner job", session_key="", created_by="U_OWNER")

    async with _cron_message_cb(orch, result_text="ran") as callback:
        assert await callback(job) == "ran"


# ── ops-mission-control ──


def _ops_jobs(tmp_path, monkeypatch, seed) -> dict:
    import kiro_crew.apps.bridges as bridges
    import kiro_crew.apps.bridges_crons as bridges_crons

    monkeypatch.setattr(bridges, "_registration_denied", lambda *a, **k: False)

    async def _go() -> dict:
        svc = CronService(base_dir=tmp_path / "omc-crons")
        await svc.start()
        try:
            await seed(svc)
            await bridges_crons.register_app_crons_with_service(OPS, svc)
            return {job.name: job for job in await svc.list_jobs_async(include_disabled=True)}
        finally:
            await svc.stop()

    return asyncio.run(_go())


def _shipped_ops_defs() -> list[dict]:
    from pathlib import Path

    from kiro_crew.apps.bridges_crons import _cron_defs_from_manifest
    from kiro_crew.apps.manifest import AppManifest

    path = Path(__file__).resolve().parents[1] / "src/kiro_crew/apps/builtins/ops_mission_control"
    defs, _ = _cron_defs_from_manifest(OPS, AppManifest.from_json_file(path / "app.json"))
    return defs


def test_ops_mission_control_names_its_own_agent_for_every_job() -> None:
    from pathlib import Path

    from kiro_crew.apps.manifest import AppManifest

    path = Path(__file__).resolve().parents[1] / "src/kiro_crew/apps/builtins/ops_mission_control"
    manifest = AppManifest.from_json_file(path / "app.json")
    declared = {
        json.loads((path / spec).read_text(encoding="utf-8"))["name"] for spec in manifest.agents
    }
    for cron in manifest.crons:
        assert cron.agent in declared, cron.name
        assert not app_schedule_lacks_agent(cron.agent, ""), cron.name


def test_an_ops_cron_calls_the_apps_api_without_a_card(monkeypatch) -> None:
    """A stamped Ops cron on its shipped agent passes the gate's ``app-own-tools`` row."""
    from pathlib import Path

    from kiro_crew import hooks
    from kiro_crew.apps import app_agent_tools
    from kiro_crew.apps.manifest import AppManifest
    from kiro_crew.apps.own_agent_tools import app_run_of
    from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, HooksConfig, ToolCall
    from kiro_crew.mcp_tools.catalog import app_tools

    path = Path(__file__).resolve().parents[1] / "src/kiro_crew/apps/builtins/ops_mission_control"
    raw = json.loads((path / "app.json").read_text(encoding="utf-8"))
    manifest = AppManifest.from_dict(raw)
    agents = {json.loads((path / s).read_text(encoding="utf-8"))["name"] for s in manifest.agents}
    tools = [SimpleNamespace(app=OPS, name=t["name"]) for t in raw["agentTools"]["tools"]]
    monkeypatch.setattr(hooks, "_BUILTIN_APP_AGENTS", hooks._BUILTIN_APP_AGENTS)
    hooks.set_builtin_app_agents({name: OPS for name in agents})
    monkeypatch.setattr(app_agent_tools, "declared_tools", lambda: tuple(tools))
    monkeypatch.setattr(app_tools, "app_enabled_state", lambda app: app == OPS)
    assert manifest.crons
    for cron in manifest.crons:
        job = SimpleNamespace(created_by=f"app:{OPS}")
        client = SimpleNamespace(agent_name=cron.agent)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.usage.read_effective_agent", lambda c: c.agent_name
        )
        call = ToolCall(
            title="Running: @kirocrew-core/ops_mission_control_api",
            mcp_server="kirocrew-core",
            mcp_tool="ops_mission_control_api",
            identity_trusted=True,
            app_run=app_run_of(job, client),
        )
        verdict = HookManager(HooksConfig()).judge(call, app="", session_key=f"cron:{cron.name}")
        assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "app-own-tools"), cron.name


def test_the_ops_dispatch_agent_can_only_spawn_itself() -> None:
    from pathlib import Path

    from kiro_crew.subagent_parts.spawn_allowlists import agent_matches_allowlist, spawn_allowlist

    path = Path(__file__).resolve().parents[1] / "src/kiro_crew/apps/builtins/ops_mission_control"
    spec = json.loads((path / "agents/dispatch.json").read_text(encoding="utf-8"))
    allowlist = spawn_allowlist(spec)
    # A run that reads alert text must not start a child on a wider agent.
    assert allowlist is not None
    assert agent_matches_allowlist(spec["name"], allowlist)
    for other in ("kirocrew", "ops-mission-control-heartbeat"):
        assert not agent_matches_allowlist(other, allowlist), other


def test_an_upgrade_moves_existing_ops_jobs_onto_the_apps_agents(tmp_path, monkeypatch) -> None:
    """Jobs the previous manifest made, with no agent, are re-applied on the next pass."""
    from kiro_crew.apps.bridges_crons import app_cron_manifest_digest

    defs = _shipped_ops_defs()

    async def _seed(svc: CronService) -> None:
        for d in defs:
            schedule = {"every_secs": d["every"]} if d["every"] else {"cron_expr": d["cron_expr"]}
            await svc.add_job_if_absent_async(
                lambda existing: False,
                name=d["name"],
                message=d["message"],
                **schedule,
                created_by=f"app:{OPS}",
                persistent_session=False,
                silent=True,
                app_manifest_digest=app_cron_manifest_digest({**d, "agent": ""}),
            )

    jobs = _ops_jobs(tmp_path, monkeypatch, _seed)

    expected = {d["name"]: d["agent"] for d in defs}
    assert {name: job.agent_id for name, job in jobs.items()} == expected
    assert {name: job.execution_context["template_id"] for name, job in jobs.items()} == expected
    # The owner sets auto per job; the manifest asks for nothing.
    assert {job.approval_mode for job in jobs.values()} == {""}
    for job in jobs.values():
        assert job.app_manifest_digest == app_cron_manifest_digest(
            next(d for d in defs if d["name"] == job.name)
        )
