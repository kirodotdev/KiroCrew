"""An app does not make a scheduled job or a background run a Bot's by naming the Bot.

The App Kit SDKs already hold this for their own creates (``apps.cron_sdk``,
``apps.spawn_sdk``). The HTTP routes an app token can reach, and the ``cron_add``
tool an agent in an app's session can call, take a Bot's name directly:
``POST /api/crons`` (``agent`` or ``member_id``), ``POST /api/crons/tools`` and
``POST /api/spawn`` (``crew`` / ``target_member``, or a parent session that is a
Bot's). Each refuses a caller with an app claim when the job or run would be a Bot's
own -- its memory store or its chat -- stores nothing and starts nothing. The owner
still schedules and delegates to a Bot, an app still names an agent of its own, and a
``config.agents`` row on the shared store is still an agent's name for an app.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from handlers_messaging_coverage_helpers import _Req, _state

import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew import mcp_cron, member_memory_auth
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.cron import CronService
from kiro_crew.dashboard import session_control, slot_ownership, token_auth
from kiro_crew.dashboard.handlers import cron as cron_handlers
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    resolve_member_execution,
)
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT
from kiro_crew.memory_stores import persist_member_config, provision_member_memory

APP = "shop"
OWNER_SUBJECT = "local-app"
#: A Bot with private memory, and a ``config.agents`` row on the shared store.
BOT = "reviewer"
SHARED_ROW = "plain"
#: A session that already runs as the Bot.
BOT_SESSION = "dashboard:bot-chat"
#: One of the app's own agents, which an app token's run names.
OWN_AGENT = "shop--helper"


@pytest.fixture
def bot() -> SimpleNamespace:
    """The Bot's id and store, with the shared-store row beside it."""
    cfg = KiroCrewConfig.load()
    cfg.agents[BOT] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, BOT)
    persist_member_config(cfg, BOT, create=True)
    cfg = KiroCrewConfig.load()
    cfg.agents[SHARED_ROW] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    cfg = KiroCrewConfig.load()
    execution = resolve_member_execution(cfg, BOT)
    bind_session_execution(BOT_SESSION, execution)
    return SimpleNamespace(member_id=cfg.agents[BOT].member_id, store=store, execution=execution)


@pytest.fixture
def sel_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    # The app-isolation rows go through slot_ownership, which binds sel at import.
    monkeypatch.setattr(slot_ownership, "sel", lambda: recorder)
    return recorder


def _denials(sel_calls: MagicMock) -> list[tuple[str, str]]:
    return [
        (call.kwargs["caller"], call.kwargs["operation"])
        for call in sel_calls.log_api_access.call_args_list
        if call.kwargs.get("outcome") == "denied"
    ]


# ── POST /api/crons ──


@pytest.fixture
def svc(tmp_path) -> CronService:
    return CronService(base_dir=tmp_path / "crons")


async def _create_cron(svc: CronService, app_claim: str, **fields) -> tuple[int, dict]:
    @web.middleware
    async def identity(request: web.Request, handler):
        request["user"] = OWNER_SUBJECT
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
    async with TestClient(TestServer(app)) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        resp = await client.post(
            "/api/crons", json={"name": "job", "message": "task", "every": 3600, **fields}
        )
        return resp.status, await resp.json()


@pytest.fixture
def grant_crons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        token_auth, "_app_api_allowlist", lambda name: ("/api/crons",) if name == APP else ()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["agent", "member_id"])
async def test_an_app_token_cannot_schedule_a_job_as_a_bot(
    svc, bot, grant_crons, sel_calls, field
) -> None:
    named = {"agent": BOT} if field == "agent" else {"member_id": bot.member_id}
    status, payload = await _create_cron(svc, APP, **named)

    assert status == 403, payload
    assert payload["code"] == "owner_only"
    assert svc.list_jobs() == []
    assert _denials(sel_calls) == [(f"app:{APP}", "crons.create")]


@pytest.mark.asyncio
async def test_the_owner_still_schedules_a_job_as_a_bot(svc, bot, sel_calls) -> None:
    status, payload = await _create_cron(svc, "", agent=BOT)

    assert status == 200, payload
    (job,) = svc.list_jobs()
    assert job.member_id == bot.member_id
    assert job.execution_context["store"]["store_id"] == bot.store
    assert _denials(sel_calls) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["shop--helper", SHARED_ROW])
async def test_an_app_token_still_schedules_its_own_job(
    svc, bot, grant_crons, sel_calls, agent
) -> None:
    """An agent of the app's own, or a shared-store row."""
    status, payload = await _create_cron(svc, APP, agent=agent)

    assert status == 200, payload
    (job,) = svc.list_jobs()
    assert (job.created_by, job.agent_id, job.member_id) == (f"app:{APP}", agent, "")
    assert job.execution_context["store"]["store_id"] == "default"
    assert job.execution_context["member_id"] is None
    assert _denials(sel_calls) == []


# ── POST /api/crons/tools ──


@pytest.fixture
def tool_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _store(request: web.Request, state: object, operation: str):
        return "", None

    monkeypatch.setattr(cron_handlers, "resolve_lesson_memory_store", _store)
    # Whose tab the caller's session is does not bear on what it may name.
    monkeypatch.setattr(session_control, "is_person_tab", lambda state, session_key: False)
    monkeypatch.setattr(
        member_memory_auth,
        "memory_request_identity",
        lambda request: (request.headers.get("X-Session-Key", ""), True),
    )


@pytest.fixture
def tool_calls(tool_identity, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def _local(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "ok"

    monkeypatch.setattr(mcp_cron, "_call_tool_locally", _local)
    return calls


@pytest.fixture
def real_tool_dispatch(tool_identity, svc, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_cron, "CronService", lambda **kwargs: svc)


async def _cron_add(
    app_claim: str,
    session: str,
    arguments: dict,
    tool: str = "cron_add",
    crons: CronService | None = None,
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
            json={"name": tool, "arguments": arguments},
            headers={"X-Session-Key": session},
        )
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["agent", "member_id"])
async def test_an_agent_in_an_apps_session_cannot_schedule_a_job_as_a_bot(
    bot, tool_calls, sel_calls, field
) -> None:
    named = {"agent": BOT} if field == "agent" else {"member_id": bot.member_id}
    status, payload = await _cron_add(
        APP, "dashboard:shop-1", {"name": "job", "message": "task", **named}
    )

    assert status == 200, payload
    assert payload["result"].startswith("Error: an app's schedule cannot run as a Bot")
    assert tool_calls == []
    assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_claim", "session", "agent"),
    [
        # A person's own session names the Bot.
        ("", "dashboard:mine", BOT),
        # A session that already runs as the Bot schedules as itself.
        (APP, BOT_SESSION, BOT),
        # An app's session names an agent of its own, or a shared-store row.
        (APP, "dashboard:shop-1", "shop--helper"),
        (APP, "dashboard:shop-1", SHARED_ROW),
        (APP, "dashboard:shop-1", "default"),
    ],
)
async def test_the_cron_tool_still_runs_for_every_other_caller(
    bot, tool_calls, sel_calls, app_claim, session, agent
) -> None:
    arguments = {"name": "job", "message": "task", "agent": agent}
    status, payload = await _cron_add(app_claim, session, arguments)

    assert (status, payload) == (200, {"result": "ok"})
    assert tool_calls == [("cron_add", arguments)]
    assert _denials(sel_calls) == []


# ── A Bot's own file, named as a template ──

#: The file a Bot owns, under a name no ``config.agents`` row carries.
BOT_FILE = "reviewer-own"


@pytest.fixture
def bot_file(monkeypatch: pytest.MonkeyPatch) -> str:
    from kiro_crew import mate_worker

    monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: name == BOT_FILE)
    return BOT_FILE


@pytest.mark.asyncio
async def test_an_app_token_cannot_schedule_a_bots_own_file(
    svc, bot, bot_file, grant_crons, sel_calls
) -> None:
    status, payload = await _create_cron(svc, APP, agent=bot_file)

    assert status == 403, payload
    assert payload["code"] == "owner_only"
    assert svc.list_jobs() == []
    assert _denials(sel_calls) == [(f"app:{APP}", "crons.create")]


@pytest.mark.asyncio
async def test_an_agent_in_an_apps_session_cannot_schedule_a_bots_own_file(
    bot, bot_file, tool_calls, sel_calls
) -> None:
    status, payload = await _cron_add(
        APP, "dashboard:shop-1", {"name": "job", "message": "task", "agent": bot_file}
    )

    assert status == 200, payload
    assert payload["result"].startswith("Error: an app's schedule cannot run as a Bot")
    assert tool_calls == []


@pytest.mark.asyncio
async def test_the_owner_still_schedules_a_bots_own_file(svc, bot, bot_file, sel_calls) -> None:
    status, payload = await _create_cron(svc, "", agent=bot_file)

    assert status == 200, payload
    (job,) = svc.list_jobs()
    assert job.agent_id == bot_file
    assert _denials(sel_calls) == []


# ── Moving an existing job onto a Bot ──


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["bot", "bot_file"])
async def test_an_agent_in_an_apps_session_cannot_move_a_job_onto_a_bot(
    bot, bot_file, tool_calls, sel_calls, target
) -> None:
    """``cron_update`` takes the rule ``cron_add`` takes: a job the app's agent made
    carries no ``app:`` tag, so the fire-time rule never sees it."""
    agent = BOT if target == "bot" else bot_file
    arguments = {"job_id": "abc123", "agent": agent}
    status, payload = await _cron_add(APP, "dashboard:shop-1", arguments, tool="cron_update")

    assert status == 200, payload
    assert payload["result"] == (
        "Error: an app's schedule cannot run as a Bot; name an agent of the app's own"
    )
    assert tool_calls == []
    assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_claim", "session", "arguments"),
    [
        # The app's own agent, a shared-store row, or no agent change at all.
        (APP, "dashboard:shop-1", {"job_id": "abc123", "agent": OWN_AGENT}),
        (APP, "dashboard:shop-1", {"job_id": "abc123", "agent": SHARED_ROW}),
        (APP, "dashboard:shop-1", {"job_id": "abc123", "agent": "default"}),
        (APP, "dashboard:shop-1", {"job_id": "abc123", "name": "renamed"}),
        # A session that already runs as the Bot, and a person's own session.
        (APP, BOT_SESSION, {"job_id": "abc123", "agent": BOT}),
        ("", "dashboard:mine", {"job_id": "abc123", "agent": BOT}),
    ],
)
async def test_cron_update_still_runs_for_every_other_caller(
    bot, tool_calls, sel_calls, app_claim, session, arguments
) -> None:
    status, payload = await _cron_add(app_claim, session, arguments, tool="cron_update")

    assert (status, payload) == (200, {"result": "ok"})
    assert tool_calls == [("cron_update", arguments)]
    assert _denials(sel_calls) == []


async def _update_cron(svc: CronService, app_claim: str, job_id: str, body: dict):
    @web.middleware
    async def identity(request: web.Request, handler):
        request["user"] = OWNER_SUBJECT
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
    app.router.add_patch("/api/crons/{job_id}", cron_handlers.api_cron_update)
    async with TestClient(TestServer(app)) as client:
        # flake-ok: the in-process test server answers on its own; pytest-timeout bounds the test
        resp = await client.patch(f"/api/crons/{job_id}", json=body)
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "body", "refused"),
    [
        (APP, {"agent": BOT}, True),
        (APP, {"agent_id": BOT}, True),
        (APP, {"agent": "bot_file"}, True),
        (APP, {"member_id": "bot"}, True),
        (APP, {"agent": OWN_AGENT}, False),
        (APP, {"agent": SHARED_ROW}, False),
        ("", {"agent": BOT}, False),
    ],
)
async def test_an_app_token_cannot_move_its_job_onto_a_bot(
    svc, bot, bot_file, grant_crons, sel_calls, caller, body, refused
) -> None:
    status, payload = await _create_cron(svc, APP, agent=OWN_AGENT)
    assert status == 200, payload
    job_id = payload["id"]
    body = {
        key: {"bot": bot.member_id, "bot_file": bot_file}.get(value, value)
        for key, value in body.items()
    }

    status, payload = await _update_cron(svc, caller, job_id, body)

    (job,) = svc.list_jobs()
    if refused:
        assert status == 403, payload
        assert payload["code"] == "owner_only"
        assert (job.agent_id, job.member_id) == (OWN_AGENT, "")
        assert _denials(sel_calls) == [(f"app:{APP}", "crons.update")]
    else:
        assert status == 200, payload
        assert _denials(sel_calls) == []


# ── POST /api/spawn ──


def _spawn(body: dict, app_claim: str) -> tuple[web.Response, MagicMock]:
    """Drive ``api_spawn`` with *body* as *app_claim* (``""`` is the owner)."""
    manager = MagicMock(max_concurrent=4, all_agents=[], _agents={}, _tasks={})
    manager.get.return_value = None
    manager.spawn.return_value = SimpleNamespace(
        id="a1", done=False, error="", error_code="", agent="kirocrew", memory_store="", crew=""
    )
    request = _Req(_state(subagents=manager), body, extra={"app": app_claim})
    return asyncio.run(messaging.api_spawn(request)), manager


@pytest.mark.parametrize("field", ["crew", "target_member"])
def test_an_app_token_cannot_start_a_run_as_a_bot(bot, sel_calls, field) -> None:
    response, manager = _spawn({"task": "summarise", "agent": OWN_AGENT, field: BOT}, APP)

    assert response.status == 404
    # The body a member that does not exist gets.
    assert json.loads(response.text) == {
        "error": "The target member does not exist.",
        "code": "unknown_member",
    }
    manager.spawn.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.create")]


def test_an_app_token_cannot_start_a_run_under_a_bots_session(bot, sel_calls) -> None:
    body = {"task": "summarise", "agent": OWN_AGENT, "parent_session": BOT_SESSION}
    response, manager = _spawn(body, APP)

    assert response.status == 409
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    manager.spawn.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.create")]


def test_the_owner_still_delegates_a_run_to_a_bot(bot, sel_calls) -> None:
    response, manager = _spawn({"task": "summarise", "crew": BOT}, "")

    assert response.status == 200, response.text
    assert manager.spawn.call_args.kwargs["memory_store"] == bot.store
    assert _denials(sel_calls) == []


@pytest.mark.parametrize("body", [{}, {"crew": SHARED_ROW}])
def test_an_app_token_still_starts_a_run_of_its_own(bot, sel_calls, body) -> None:
    response, manager = _spawn({"task": "summarise", "agent": OWN_AGENT, **body}, APP)

    assert response.status == 200, response.text
    assert manager.spawn.call_args.kwargs["memory_store"] == ""
    assert _denials(sel_calls) == []


def test_a_caller_that_already_runs_as_the_bot_hands_work_to_itself(bot) -> None:
    request = _Req(None, {}, extra={"app": APP})

    refused = messaging._app_bot_spawn_refusal(request, bot.execution, bot.execution, crew=BOT)

    assert refused is None


# ── what counts as a Bot's own ──


def test_a_bots_own_is_its_memory_store_or_its_chat() -> None:
    from kiro_crew.execution_context import is_bot_own_execution

    def execution(kind: str, *, store: str = "default", member_id=None, template="kirocrew"):
        return ExecutionContext(member_id, MemoryStoreRef(store, member_id), kind, template)

    assert is_bot_own_execution(execution("template", store="reviewer-store", member_id="m-1"))
    assert is_bot_own_execution(execution("member", store="reviewer-store"))
    assert is_bot_own_execution(execution("member", template=MATE_CONDUCTOR_AGENT))
    # A legacy Bot on a named store writes a template record with no id.
    assert is_bot_own_execution(execution("template", store="reviewer-store"))
    assert not is_bot_own_execution(execution("member"))
    assert not is_bot_own_execution(execution("template"))
    assert not is_bot_own_execution(execution("template", template=MATE_CONDUCTOR_AGENT))
    assert not is_bot_own_execution(None)


# ── A Bot on the shared store, named from an app's session on the shared store ──

#: A ``config.agents`` row on the shared store whose chat runs on the conductor.
CONDUCTOR_ROW = "lead"
#: A ``config.agents`` row on the shared store whose template is a Bot's own file.
OWN_FILE_ROW = "lead-file"
#: An app's session whose own record is on the shared store.
APP_SESSION = "dashboard:shop-1"


@pytest.fixture
def shared_bots(bot, bot_file) -> SimpleNamespace:
    """Two Bots on the shared store, and an app's session on that store too."""
    cfg = KiroCrewConfig.load()
    cfg.agents[CONDUCTOR_ROW] = KiroCrewAgentConfig(kiro_agent=MATE_CONDUCTOR_AGENT)
    cfg.agents[OWN_FILE_ROW] = KiroCrewAgentConfig(kiro_agent=bot_file)
    cfg.save()
    cfg = KiroCrewConfig.load()
    app_execution = ExecutionContext(None, MemoryStoreRef("default"), "template", OWN_AGENT)
    bind_session_execution(APP_SESSION, app_execution)
    conductor = resolve_member_execution(cfg, CONDUCTOR_ROW)
    assert conductor.store == app_execution.store
    return SimpleNamespace(app=app_execution, conductor=conductor)


def _spawn_as(body: dict, execution: ExecutionContext) -> tuple[web.Response, MagicMock]:
    """``_spawn`` from an app's session whose attested record is *execution*."""
    from kiro_crew.dashboard.handlers._shared_member_scope import _MEMBER_SCOPE_KEY, MemberScope

    manager = MagicMock(max_concurrent=4, all_agents=[], _agents={}, _tasks={})
    manager.get.return_value = None
    manager.spawn.return_value = SimpleNamespace(
        id="a1", done=False, error="", error_code="", agent="kirocrew", memory_store="", crew=""
    )
    scope = MemberScope(APP_SESSION, True, execution.store.legacy_name, execution)
    request = _Req(_state(subagents=manager), body, extra={"app": APP, _MEMBER_SCOPE_KEY: scope})
    return asyncio.run(messaging.api_spawn(request)), manager


@pytest.mark.parametrize("crew", [CONDUCTOR_ROW, OWN_FILE_ROW])
def test_an_apps_session_on_the_shared_store_cannot_start_a_run_as_a_shared_store_bot(
    shared_bots, sel_calls, crew
) -> None:
    response, manager = _spawn_as(
        {"task": "summarise", "agent": OWN_AGENT, "crew": crew}, shared_bots.app
    )

    assert response.status == 404, response.text
    assert json.loads(response.text)["code"] == "unknown_member"
    manager.spawn.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [CONDUCTOR_ROW, OWN_FILE_ROW])
async def test_an_apps_session_on_the_shared_store_cannot_schedule_a_shared_store_bot(
    shared_bots, tool_calls, sel_calls, row
) -> None:
    """``member_id`` is the member selection; how ``agent`` classifies such a row is
    ``split_cron_agent_member``'s, outside the same-Bot rule this pins."""
    status, payload = await _cron_add(
        APP, APP_SESSION, {"name": "job", "message": "task", "member_id": row}
    )

    assert status == 200, payload
    assert payload["result"].startswith("Error: an app's schedule cannot run as a Bot")
    assert tool_calls == []


def test_a_member_session_on_the_shared_store_hands_work_to_itself(shared_bots) -> None:
    request = _Req(None, {}, extra={"app": APP})
    own = shared_bots.conductor

    assert messaging._app_bot_spawn_refusal(request, own, own, crew=CONDUCTOR_ROW) is None


@pytest.mark.asyncio
async def test_a_member_session_on_the_shared_store_schedules_as_itself(
    shared_bots, tool_calls, sel_calls
) -> None:
    session = "dashboard:lead-chat"
    bind_session_execution(session, shared_bots.conductor)
    arguments = {"name": "job", "message": "task", "agent": CONDUCTOR_ROW}

    status, payload = await _cron_add(APP, session, arguments)

    assert (status, payload) == (200, {"result": "ok"})
    assert tool_calls == [("cron_add", arguments)]


def test_the_same_bot_is_the_same_named_store_or_the_same_member_on_the_shared_store() -> None:
    from kiro_crew.execution_context import is_same_bot

    def execution(kind: str, name: str = "", *, store: str = "default", member_id=None):
        return ExecutionContext(
            member_id, MemoryStoreRef(store, member_id), kind, "kirocrew", selection_name=name
        )

    named = execution("member", "reviewer", store="reviewer-store", member_id="m-1")
    worker = execution("template", "kirocrew-mate-worker", store="reviewer-store", member_id="m-1")
    assert is_same_bot(named, named)
    assert is_same_bot(worker, named)
    assert is_same_bot(execution("template", store="legacy"), execution("member", store="legacy"))
    assert not is_same_bot(named, execution("member", "other", store="other-store"))
    assert not is_same_bot(named, execution("member", "reviewer"))
    # On the shared store only a member selection of the same name is the same Bot.
    assert is_same_bot(execution("member", "lead"), execution("member", "lead"))
    assert not is_same_bot(execution("member", "lead"), execution("member", "other"))
    assert not is_same_bot(execution("template", "lead"), execution("member", "lead"))
    assert not is_same_bot(execution("member", "lead"), execution("template", "lead"))
    assert not is_same_bot(execution("member"), execution("member"))
    assert not is_same_bot(execution("template"), execution("template"))
    assert not is_same_bot(None, named)
    assert not is_same_bot(named, None)


# ── Legacy aliases must take the same Bot check as explicit member selections ──

LEGACY_ROW = "archivist"
BROKEN_ROW = "unbound"


@pytest.fixture
def cron_aliases(shared_bots) -> None:
    from kiro_crew.config.sections import MemoryStoreConfig

    cfg = KiroCrewConfig.load()
    cfg.agents[LEGACY_ROW] = KiroCrewAgentConfig(kiro_agent="kirocrew", memory_store="archive")
    cfg.memory_stores["archive"] = MemoryStoreConfig()
    cfg.agents[BROKEN_ROW] = KiroCrewAgentConfig(kiro_agent="kirocrew", memory_store="missing")
    # A plain config row must not mask a Bot's own file with the same name.
    cfg.agents[BOT_FILE] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    assert not cfg.agents[LEGACY_ROW].member_id


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [LEGACY_ROW, CONDUCTOR_ROW, OWN_FILE_ROW, BOT_FILE, BROKEN_ROW])
@pytest.mark.parametrize("tool", ["cron_add", "cron_update"])
async def test_app_cron_tools_refuse_legacy_bot_aliases(
    cron_aliases, tool_calls, sel_calls, agent, tool
) -> None:
    arguments = {"name": "job", "message": "task", "agent": agent}
    if tool == "cron_update":
        arguments["job_id"] = "abc123"

    status, payload = await _cron_add(APP, APP_SESSION, arguments, tool=tool)

    assert status == 200, payload
    assert payload["result"] == (
        "Error: an app's schedule cannot run as a Bot; name an agent of the app's own"
    )
    assert tool_calls == []
    assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [LEGACY_ROW, CONDUCTOR_ROW, OWN_FILE_ROW, BOT_FILE, BROKEN_ROW])
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_app_cron_http_refuses_legacy_bot_aliases(
    svc, cron_aliases, grant_crons, sel_calls, agent, operation
) -> None:
    if operation == "update":
        status, payload = await _create_cron(svc, APP, agent=OWN_AGENT)
        assert status == 200, payload
        before = asdict(svc.list_jobs()[0])
        status, payload = await _update_cron(svc, APP, payload["id"], {"agent": agent})
        assert asdict(svc.list_jobs()[0]) == before
    else:
        status, payload = await _create_cron(svc, APP, agent=agent)
        assert svc.list_jobs() == []

    assert status == 403, payload
    assert payload["code"] == "owner_only"
    assert _denials(sel_calls) == [(f"app:{APP}", f"crons.{operation}")]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [LEGACY_ROW, CONDUCTOR_ROW, OWN_FILE_ROW])
@pytest.mark.parametrize("caller", ["owner", "same_bot"])
@pytest.mark.parametrize("tool", ["cron_add", "cron_update"])
async def test_cron_tools_allow_legacy_bot_self_and_owner(
    cron_aliases, tool_calls, sel_calls, agent, caller, tool
) -> None:
    session = "dashboard:alias-chat"
    if caller == "same_bot":
        bind_session_execution(session, resolve_member_execution(KiroCrewConfig.load(), agent))
    arguments = {"name": "job", "message": "task", "agent": agent}
    if tool == "cron_update":
        arguments["job_id"] = "abc123"

    status, payload = await _cron_add(APP if caller == "same_bot" else "", session, arguments, tool)

    assert (status, payload) == (200, {"result": "ok"})
    assert tool_calls == [(tool, arguments)]
    assert _denials(sel_calls) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "agent"),
    [(APP, "default"), (APP, SHARED_ROW), ("", LEGACY_ROW), ("", CONDUCTOR_ROW)],
)
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_cron_http_allows_plain_aliases_and_owner(
    svc, cron_aliases, grant_crons, sel_calls, caller, agent, operation
) -> None:
    if operation == "update":
        status, payload = await _create_cron(svc, APP, agent=OWN_AGENT)
        assert status == 200, payload
        status, payload = await _update_cron(svc, caller, payload["id"], {"agent": agent})
    else:
        status, payload = await _create_cron(svc, caller, agent=agent)

    assert status == 200, payload
    assert svc.list_jobs()[0].agent_id == agent
    assert _denials(sel_calls) == []


# ── The record a create stores is the record judged ──

#: A session that runs as the Bot, on a record whose template is the Bot's own file.
OWN_FILE_SESSION = "dashboard:lead-file-chat"
REFUSED = "Error: an app's schedule cannot run as a Bot; name an agent of the app's own"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "arguments", "refused"),
    [
        # Nothing named: the job runs as its maker.
        (APP_SESSION, {}, False),
        (APP_SESSION, {"agent": OWN_AGENT}, False),
        (APP_SESSION, {"agent": OWN_AGENT, "member_id": "default"}, False),
        (APP_SESSION, {"agent": SHARED_ROW}, False),
        # A Bot's own file, whatever member_id says.
        (APP_SESSION, {"agent": BOT_FILE}, True),
        (APP_SESSION, {"agent": BOT_FILE, "member_id": "default"}, True),
        (APP_SESSION, {"agent": BOT_FILE, "member_id": "bot"}, True),
        # A Bot's member under a plain template.
        (APP_SESSION, {"agent": OWN_AGENT, "member_id": "bot"}, True),
        # A member that does not resolve.
        (APP_SESSION, {"agent": OWN_AGENT, "member_id": "nobody"}, True),
        # A legacy alias with no member id, on a private store.
        (APP_SESSION, {"agent": LEGACY_ROW}, True),
        # Bot X's file while running as Bot Y.
        (BOT_SESSION, {"agent": BOT_FILE, "member_id": "bot"}, True),
        (BOT_SESSION, {"agent": BOT_FILE}, True),
        # The same Bot schedules as itself, its member or its own file.
        (BOT_SESSION, {"agent": BOT}, False),
        (BOT_SESSION, {"member_id": "bot"}, False),
        (OWN_FILE_SESSION, {"agent": BOT_FILE}, False),
    ],
)
async def test_cron_add_judges_the_record_the_create_stores(
    bot, cron_aliases, tool_calls, sel_calls, session, arguments, refused
) -> None:
    from dataclasses import replace

    # A Bot whose own record runs its own file.
    bind_session_execution(OWN_FILE_SESSION, replace(bot.execution, template_id=BOT_FILE))
    arguments = {
        "name": "job",
        "message": "task",
        **{key: {"bot": bot.member_id}.get(value, value) for key, value in arguments.items()},
    }

    status, payload = await _cron_add(APP, session, arguments)

    assert status == 200, payload
    if refused:
        assert payload["result"] == REFUSED
        assert tool_calls == []
        assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]
    else:
        assert payload == {"result": "ok"}
        assert tool_calls == [("cron_add", arguments)]


@pytest.mark.asyncio
async def test_an_app_token_cannot_schedule_a_bots_own_file_under_the_default_row(
    svc, cron_aliases, grant_crons, sel_calls
) -> None:
    status, payload = await _create_cron(svc, APP, agent=BOT_FILE, member_id="default")

    assert status == 403, payload
    assert payload["code"] == "owner_only"
    assert svc.list_jobs() == []
    assert _denials(sel_calls) == [(f"app:{APP}", "crons.create")]


@pytest.mark.asyncio
async def test_an_app_token_cannot_move_its_job_onto_a_bots_own_file_under_the_default_row(
    svc, cron_aliases, grant_crons, sel_calls
) -> None:
    status, payload = await _create_cron(svc, APP, agent=OWN_AGENT)
    assert status == 200, payload
    before = asdict(svc.list_jobs()[0])

    status, payload = await _update_cron(
        svc, APP, payload["id"], {"agent": BOT_FILE, "member_id": "default"}
    )

    assert status == 403, payload
    assert payload["code"] == "owner_only"
    assert asdict(svc.list_jobs()[0]) == before
    assert _denials(sel_calls) == [(f"app:{APP}", "crons.update")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "refused"), [(BOT_FILE, True), (OWN_AGENT, False)])
async def test_cron_update_judges_the_job_it_changes(
    svc, cron_aliases, tool_calls, sel_calls, agent, refused
) -> None:
    """The tool's job carries no ``app:`` tag; its own capture is re-pointed and judged."""
    job = svc.add_job(
        name="job", message="task", every_secs=3600, agent_id=SHARED_ROW, session_key=APP_SESSION
    )
    arguments = {"job_id": job.id, "agent": agent}

    status, payload = await _cron_add(APP, APP_SESSION, arguments, "cron_update", crons=svc)

    assert status == 200, payload
    assert payload == ({"result": REFUSED} if refused else {"result": "ok"})
    assert tool_calls == ([] if refused else [("cron_update", arguments)])


def test_the_create_and_the_check_share_one_capture(
    svc, shared_bots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bind_cron_memory`` stores what ``cron_capture_execution`` returns, and the
    check judges that same return: a Bot's record handed back refuses."""
    from kiro_crew.cron_service import identity

    calls: list[tuple] = []
    real = identity.cron_capture_execution

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(identity, "cron_capture_execution", spy)
    job = svc.add_job(
        name="job", message="task", every_secs=3600, agent_id=OWN_AGENT, session_key=APP_SESSION
    )
    assert calls == [(APP_SESSION, "", OWN_AGENT, "")]
    assert job.execution_context == real(APP_SESSION, "", OWN_AGENT, "").to_record()

    calls.clear()
    assert not identity.app_schedule_names_bot(OWN_AGENT, "", maker_session=APP_SESSION)
    assert calls == [(APP_SESSION, "", OWN_AGENT, "")]

    monkeypatch.setattr(identity, "cron_capture_execution", lambda *args: shared_bots.conductor)
    assert identity.app_schedule_names_bot(OWN_AGENT, "", maker_session=APP_SESSION)


# ── Bundled apps are not caught: no agent, the app's own worker, the manifest crons ──

#: The worker file an app ships: a user-level agent, not a ``config.agents`` row.
APP_WORKER = f"{APP}--worker"


def test_a_schedule_with_no_agent_does_not_name_a_bot(bot) -> None:
    from kiro_crew.cron_service.identity import app_schedule_names_bot

    assert app_schedule_names_bot("", "", maker_session=APP_SESSION) is False
    assert app_schedule_names_bot("", "") is False


@pytest.mark.asyncio
async def test_an_app_token_edits_a_job_of_its_own_agent(svc, bot, grant_crons, sel_calls) -> None:
    status, payload = await _create_cron(svc, APP, agent=APP_WORKER)
    assert status == 200, payload
    job_id = payload["id"]

    for body in ({"message": "another task"}, {"every": 7200}):
        status, payload = await _update_cron(svc, APP, job_id, body)
        assert status == 200, payload

    (job,) = svc.list_jobs()
    assert (job.created_by, job.agent_id, job.member_id) == (f"app:{APP}", APP_WORKER, "")
    assert job.message == "another task"
    assert job.schedule.every_secs == 7200
    assert _denials(sel_calls) == []


def test_an_apps_own_worker_does_not_name_a_bot(bot) -> None:
    from kiro_crew import mate_worker
    from kiro_crew.cron_service.identity import app_schedule_names_bot

    assert APP_WORKER not in KiroCrewConfig.load().agents
    assert mate_worker.is_mate_owned_agent(APP_WORKER) is False
    assert app_schedule_names_bot(APP_WORKER, "") is False
    assert app_schedule_names_bot(APP_WORKER, "", maker_session=APP_SESSION) is False


@pytest.mark.asyncio
async def test_an_apps_session_schedules_the_apps_own_worker(bot, tool_calls, sel_calls) -> None:
    arguments = {"name": "job", "message": "task", "agent": APP_WORKER}

    status, payload = await _cron_add(APP, APP_SESSION, arguments)

    assert (status, payload) == (200, {"result": "ok"})
    assert tool_calls == [("cron_add", arguments)]
    assert _denials(sel_calls) == []


@pytest.mark.asyncio
async def test_an_app_token_schedules_the_apps_own_worker(svc, bot, grant_crons, sel_calls) -> None:
    status, payload = await _create_cron(svc, APP, agent=APP_WORKER)

    assert status == 200, payload
    (job,) = svc.list_jobs()
    assert (job.created_by, job.agent_id, job.member_id) == (f"app:{APP}", APP_WORKER, "")
    assert _denials(sel_calls) == []


def _bundled_manifest_crons() -> list[tuple[str, object]]:
    """Every cron a bundled app's ``app.json`` declares, as ``(app name, cron)``."""
    from pathlib import Path

    from kiro_crew.apps.manifest import AppManifest

    builtins = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "apps" / "builtins"
    found = []
    for path in sorted(builtins.glob("*/app.json")):
        manifest = AppManifest.from_json_file(path)
        found.extend((manifest.name, cron) for cron in manifest.crons)
    return found


def test_bundled_app_manifest_crons_are_not_refused(bot) -> None:
    from kiro_crew.cron_service.identity import app_schedule_names_bot
    from kiro_crew.cron_service.model import CronJob
    from kiro_crew.slack.gateway_runtime.cron_fire import _app_job_runs_as_bot

    crons = _bundled_manifest_crons()
    assert {cron.name for app, cron in crons if app == "ops-mission-control"} >= {
        "dispatch",
        "reconcile",
        "rotation-check",
        "ledger-hygiene",
    }

    cfg = KiroCrewConfig.load()
    shared = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    for app, cron in crons:
        agent = cron.agent or ""
        assert app_schedule_names_bot(agent, "") is False, (app, cron.name)
        job = CronJob(
            id="j1",
            name=f"{app}/{cron.name}",
            message=cron.message,
            agent_id=agent,
            agent_sequence=list(cron.agent_sequence or []),
            command=cron.command or "",
            script=cron.script or "",
            created_by=f"app:{app}",
        )
        assert _app_job_runs_as_bot(job, shared, "kirocrew", cfg) is False, (app, cron.name)


def test_ops_mission_control_registers_all_four_manifest_crons(tmp_path, bot, monkeypatch) -> None:
    import kiro_crew.apps.bridges as bridges
    import kiro_crew.apps.bridges_crons as bridges_crons

    monkeypatch.setattr(bridges, "_registration_denied", lambda *a, **k: False)
    app = "ops-mission-control"

    async def _go() -> tuple[list[str], dict]:
        svc = CronService(base_dir=tmp_path / "omc-crons")
        await svc.start()
        try:
            created = await bridges_crons.register_app_crons_with_service(app, svc)
            jobs = {job.name: job for job in await svc.list_jobs_async(include_disabled=True)}
        finally:
            await svc.stop()
        return created, jobs

    created, jobs = asyncio.run(_go())

    from pathlib import Path

    from kiro_crew.apps.manifest import AppManifest

    root = Path(__file__).resolve().parents[1] / "src/kiro_crew/apps/builtins/ops_mission_control"
    shipped = AppManifest.from_json_file(root / "app.json").crons
    expected = {f"{app}/{c.name}": (f"app:{app}", c.agent) for c in shipped}
    assert expected
    assert set(created) == set(expected)
    assert {name: (job.created_by, job.agent_id) for name, job in jobs.items()} == expected


# ── Authorization sees the values that tool validation will store ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "target"),
    [("agent", BOT), ("member_id", BOT), ("agent", BOT_FILE), ("agent", LEGACY_ROW)],
)
async def test_cron_add_refuses_padded_bot_selections(
    svc, cron_aliases, real_tool_dispatch, sel_calls, field, target
) -> None:
    arguments = {"name": "job", "message": "task", "every": 3600, field: f" {target} "}

    status, payload = await _cron_add(APP, APP_SESSION, arguments, crons=svc)

    assert (status, payload) == (200, {"result": REFUSED})
    assert svc.list_jobs() == []
    assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]


@pytest.mark.asyncio
async def test_cron_update_refuses_a_padded_bot_selection(
    svc, bot, real_tool_dispatch, sel_calls
) -> None:
    job = svc.add_job(
        name="job", message="task", every_secs=3600, agent_id=OWN_AGENT, session_key=APP_SESSION
    )
    before = asdict(job)

    status, payload = await _cron_add(
        APP, APP_SESSION, {"job_id": f" {job.id} ", "agent": f" {BOT} "}, "cron_update", crons=svc
    )

    assert (status, payload) == (200, {"result": REFUSED})
    assert asdict(svc.get_job(job.id)) == before
    assert _denials(sel_calls) == [(f"app:{APP}", "cron.tools")]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["cron_add", "cron_update"])
async def test_cron_tools_store_the_trimmed_app_agent(
    svc, bot, real_tool_dispatch, sel_calls, tool
) -> None:
    arguments = {"agent": f" {OWN_AGENT} "}
    if tool == "cron_add":
        arguments.update(name="job", message="task", every=3600)
    else:
        job = svc.add_job(
            name="job",
            message="task",
            every_secs=3600,
            agent_id=SHARED_ROW,
            session_key=APP_SESSION,
        )
        arguments["job_id"] = f" {job.id} "

    status, payload = await _cron_add(APP, APP_SESSION, arguments, tool, crons=svc)

    assert status == 200, payload
    assert not payload["result"].startswith("Error:"), payload
    (stored,) = svc.list_jobs()
    assert (stored.agent_id, stored.member_id) == (OWN_AGENT, "")
    assert _denials(sel_calls) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["cron_add", "cron_update"])
async def test_cron_tools_preserve_validation_errors_before_bot_judgment(
    svc, bot, real_tool_dispatch, sel_calls, tool
) -> None:
    # Invalid input must retain the dispatcher's error instead of a Bot refusal.
    arguments = {"agent": BOT, "unknown": "value"}
    expected = mcp_cron._call_tool_locally(tool, arguments)

    status, payload = await _cron_add(APP, APP_SESSION, arguments, tool, crons=svc)

    assert expected.startswith("Error:"), expected
    assert expected != REFUSED
    assert (status, payload) == (200, {"result": expected})
    assert svc.list_jobs() == []
    assert _denials(sel_calls) == []


@pytest.mark.parametrize("tool", ["cron_add", "cron_update"])
@pytest.mark.parametrize("target", [BOT, BOT_FILE, LEGACY_ROW, OWN_AGENT])
def test_cron_argument_normalization_is_idempotent(tool, target) -> None:
    arguments = {"name": " job ", "message": " task ", "agent": f" \u200b{target}\u200b "}
    if tool == "cron_add":
        arguments["member_id"] = f" {BOT} "
    else:
        arguments["job_id"] = " abc123 "
    cleaned = mcp_cron._validate_args(tool, arguments)

    assert cleaned["agent"] == target
    assert cleaned["name"] == "job"
    assert cleaned["message"] == "task"
    assert cleaned.get("member_id", cleaned.get("job_id")) == (
        BOT if tool == "cron_add" else "abc123"
    )
    assert mcp_cron._validate_args(tool, cleaned) == cleaned
