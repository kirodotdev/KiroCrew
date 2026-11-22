"""``chat_folder_id`` on the ``cron_add`` and ``cron_update`` tools: the Schedule form's rules.

Each test drives the real ``POST /api/crons/tools`` route, which runs the tool in a worker
thread exactly as a member session's call runs, against a real ``DashboardState`` and a
real ``CronService`` on the test's data home. Only the transport attestation of the
caller's session is stubbed, as ``test_cron_tools_forwards_session_token`` does.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import mcp_cron, member_memory_auth
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronService
from kiro_crew.dashboard.agent_ipc import GRANT_KEY
from kiro_crew.dashboard.cron_inject import ensure_cron_slot
from kiro_crew.dashboard.handlers import cron
from kiro_crew.mcp_caller import CallerContext, current_caller, set_current_caller
from kiro_crew.session_surface import set_dashboard_surfaced
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, default_timeout

pytestmark = pytest.mark.asyncio

_KEY = "dashboard:alice"
FOLDER = "f00dcafe"
OTHER = "beadfeed"
DRAGGED = "deadbeef"
BOT_HOME = "b0700000"
_UNKNOWN = "That chat folder does not exist. Retry the folder list and pick another"
_JOB = {"name": "brief", "message": "Write the brief.", "every": 3600}


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The tab mint publishes into the process-global dashboard-surface registry."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


@pytest.fixture
def state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    async def _store(request: web.Request, state: object, operation: str):  # type: ignore[no-untyped-def]
        return "", None

    monkeypatch.setattr(cron, "resolve_lesson_memory_store", _store)
    monkeypatch.setattr(
        member_memory_auth,
        "memory_request_identity",
        lambda request: (request.headers.get("X-Session-Key", ""), True),
    )
    built = _make_state(tmp_path / "log")
    built.crons = CronService(base_dir=config_dir())
    built._folders = [
        {"id": fid, "name": f"folder-{fid}", "parent_id": ""} for fid in (FOLDER, OTHER, DRAGGED)
    ] + [
        {
            "id": BOT_HOME,
            "name": "Bot",
            "parent_id": "",
            "mate_home": True,
            "owner_app": "member:bot-store",
        }
    ]
    return built


def _app(  # type: ignore[no-untyped-def]
    state, app_claim: str = "", capability: bool = False
) -> web.Application:
    @web.middleware
    async def _internal(request: web.Request, handler):  # type: ignore[no-untyped-def]
        request["internal_auth"] = True
        if capability:
            # What the agent-socket gate stamps on a call a capability admitted.
            request[GRANT_KEY] = object()
        if app_claim:
            request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[_internal])
    app["state"] = state
    app.router.add_post("/api/crons/tools", cron.api_cron_tools)
    return app


async def _tool(  # type: ignore[no-untyped-def]
    state,
    name: str,
    arguments: dict[str, Any],
    key: str = _KEY,
    app_claim: str = "",
    headers: dict[str, str] | None = None,
    capability: bool = False,
) -> str:
    async with TestClient(TestServer(_app(state, app_claim, capability))) as client:
        resp = await client.post(
            "/api/crons/tools",
            json={"name": name, "arguments": arguments},
            headers={"X-Session-Key": key, **(headers or {})},
        )
        assert resp.status == 200, await resp.text()
        return str((await resp.json())["result"])


async def _add(state, **arguments: Any) -> str:  # type: ignore[no-untyped-def]
    result = await _tool(state, "cron_add", {**_JOB, **arguments})
    assert result.startswith("Added job: "), result
    return result.split("Added job: ", 1)[1].split(" ", 1)[0]


async def _mint_tab(state, job_id: str):  # type: ignore[no-untyped-def]
    """Make the job's tab exist through the run-start pre-create, and let its filing land."""
    await ensure_cron_slot(state, CronService(base_dir=config_dir()).get_job(job_id))
    if state._background_tasks:
        await asyncio.wait_for(
            asyncio.gather(*list(state._background_tasks)), FALLBACK_TIMEOUT_SECS
        )
    return state.get_slot(f"cron-{job_id}")


def _stored(state) -> list:  # type: ignore[no-untyped-def]
    return CronService(base_dir=config_dir()).list_jobs(include_disabled=True)


# 1. A real folder on a persistent agent job


async def test_cron_add_files_the_job_and_its_minted_tab(state) -> None:
    job_id = await _add(state, chat_folder_id=FOLDER)

    (job,) = _stored(state)
    assert (job.id, job.chat_folder_id) == (job_id, FOLDER)
    tab = await _mint_tab(state, job_id)
    assert tab.folder_id == FOLDER


async def test_cron_add_without_the_field_stays_unfiled(state) -> None:
    job_id = await _add(state)

    assert _stored(state)[0].chat_folder_id == ""
    assert (await _mint_tab(state, job_id)).folder_id == ""


# 2. The form's refusals, with nothing written


async def test_an_unknown_folder_gets_the_form_s_message(state) -> None:
    result = await _tool(state, "cron_add", {**_JOB, "chat_folder_id": "nope1234"})

    assert result == f"Error: {_UNKNOWN}, or choose not to file runs."
    assert _stored(state) == []


async def test_a_non_string_is_refused(state) -> None:
    result = await _tool(state, "cron_add", {**_JOB, "chat_folder_id": 7})

    assert result == "Error: chat_folder_id: expected str, got int"
    assert _stored(state) == []


async def test_a_stateless_job_gets_the_store_s_persistent_only_message(state) -> None:
    from kiro_crew.cron import _CHAT_FOLDER_NEEDS_PERSISTENT

    result = await _tool(
        state, "cron_add", {**_JOB, "persistent_session": False, "chat_folder_id": FOLDER}
    )

    assert result == f"Error: {_CHAT_FOLDER_NEEDS_PERSISTENT}"
    assert _stored(state) == []


# 3. A Bot's home, for a job on another store


async def test_a_bot_s_home_is_refused_for_a_job_on_another_store(state, monkeypatch) -> None:
    """Refused with the form's message, and recorded as the tool call, not as the form."""
    rows: list[dict[str, Any]] = []
    sink = SimpleNamespace(log_api_access=lambda **row: rows.append(row))
    monkeypatch.setattr(cron, "_sel", lambda: sink)

    result = await _tool(state, "cron_add", {**_JOB, "chat_folder_id": BOT_HOME})

    assert result == (
        "Error: That chat folder belongs to a Bot, so this schedule's runs cannot be "
        "filed there. Pick another folder, or choose not to file runs."
    )
    assert _stored(state) == []
    (denied,) = [row for row in rows if row.get("error") == "mate_home_locked"]
    assert (denied["caller"], denied["source"], denied["outcome"]) == ("mcp", "mcp", "denied")


async def test_an_app_s_refused_bot_home_row_names_the_app(state, monkeypatch) -> None:
    """An agent in an app's session is recorded as that app, as the form records it."""
    rows: list[dict[str, Any]] = []
    sink = SimpleNamespace(log_api_access=lambda **row: rows.append(row))
    monkeypatch.setattr(cron, "_sel", lambda: sink)

    result = await _tool(state, "cron_add", {**_JOB, "chat_folder_id": BOT_HOME}, app_claim="shop")

    assert result == (
        "Error: That chat folder belongs to a Bot, so this schedule's runs cannot be "
        "filed there. Pick another folder, or choose not to file runs."
    )
    assert _stored(state) == []
    (denied,) = [row for row in rows if row.get("error") == "mate_home_locked"]
    assert (denied["caller"], denied["source"], denied["outcome"]) == ("app:shop", "mcp", "denied")


def _bot_session() -> tuple[str, str]:
    """A Bot (crew member) on its own memory store, and a session running as it."""
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents["Bot"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="bot")
    store = provision_member_memory(cfg, "Bot")
    cfg.save()
    bot_key = "dashboard:bot-session"
    bind_session_execution(bot_key, resolve_member_execution(KiroCrewConfig.load(), "Bot"))
    return bot_key, store


async def test_a_bot_s_session_files_its_job_in_its_own_home(state) -> None:
    """The job binds to the calling Bot's store, so the Bot's own home is accepted."""
    from kiro_crew.dashboard.mate_folders import mate_principal

    bot_key, store = await asyncio.to_thread(_bot_session)
    own_home = "0e0e0e0e"
    state._folders.append(
        {
            "id": own_home,
            "name": "Bot",
            "parent_id": "",
            "mate_home": True,
            "owner_app": mate_principal(store),
        }
    )

    result = await _tool(state, "cron_add", {**_JOB, "chat_folder_id": own_home}, key=bot_key)

    assert result.startswith("Added job: "), result
    (job,) = _stored(state)
    assert (job.memory_store, job.chat_folder_id) == (store, own_home)


async def test_cron_update_into_a_bot_s_home_is_refused(state) -> None:
    job_id = await _add(state, chat_folder_id=FOLDER)

    result = await _tool(state, "cron_update", {"job_id": job_id, "chat_folder_id": BOT_HOME})

    assert result.startswith("Error: That chat folder belongs to a Bot")
    assert _stored(state)[0].chat_folder_id == FOLDER


# 4. cron_update moves the job's existing tab as the form's save does


async def test_cron_update_moves_the_tab_to_the_new_folder(state) -> None:
    job_id = await _add(state, chat_folder_id=FOLDER)
    tab = await _mint_tab(state, job_id)

    result = await _tool(state, "cron_update", {"job_id": job_id, "chat_folder_id": OTHER})

    assert result.startswith("Updated job: "), result
    assert _stored(state)[0].chat_folder_id == OTHER
    assert tab.folder_id == OTHER


async def test_the_tab_move_finishes_with_one_default_executor_worker(state) -> None:
    """The tool call runs on its own pool, so while it waits for the tab move it holds
    no default-executor worker, and the move's slot save gets the only one."""
    job_id = await _add(state, chat_folder_id=FOLDER)
    tab = await _mint_tab(state, job_id)
    loop = asyncio.get_running_loop()
    previous = loop._default_executor  # type: ignore[attr-defined]  # set by the add's to_thread
    one_worker = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(one_worker)
    try:
        result = await asyncio.wait_for(
            _tool(state, "cron_update", {"job_id": job_id, "chat_folder_id": OTHER}),
            timeout=default_timeout(),
        )
    finally:
        loop.set_default_executor(previous)
        # A call parked on the only worker waits for a save queued behind it: cancel
        # the save, then join the worker off the loop, so the cancel can reach it.
        one_worker.shutdown(wait=False, cancel_futures=True)
        await asyncio.wait_for(asyncio.to_thread(one_worker.shutdown), timeout=default_timeout())

    assert result.startswith("Updated job: "), result
    assert _stored(state)[0].chat_folder_id == OTHER
    assert tab.folder_id == OTHER


async def test_a_tab_the_reader_dragged_elsewhere_stays(state) -> None:
    job_id = await _add(state, chat_folder_id=FOLDER)
    tab = await _mint_tab(state, job_id)
    tab.folder_id = DRAGGED

    await _tool(state, "cron_update", {"job_id": job_id, "chat_folder_id": OTHER})

    assert _stored(state)[0].chat_folder_id == OTHER
    assert tab.folder_id == DRAGGED


async def test_an_update_that_does_not_name_the_field_moves_nothing(state) -> None:
    """Turning persistence off clears the stored folder, as it always did through
    the tool; the tab stays where it is."""
    job_id = await _add(state, chat_folder_id=FOLDER)
    tab = await _mint_tab(state, job_id)

    result = await _tool(state, "cron_update", {"job_id": job_id, "persistent_session": False})

    assert result.startswith("Updated job: "), result
    assert _stored(state)[0].chat_folder_id == ""
    assert tab.folder_id == FOLDER


async def test_cron_update_with_an_unknown_folder_changes_nothing(state) -> None:
    job_id = await _add(state, chat_folder_id=FOLDER)
    tab = await _mint_tab(state, job_id)

    result = await _tool(state, "cron_update", {"job_id": job_id, "chat_folder_id": "nope1234"})

    assert result.startswith(f"Error: {_UNKNOWN}")
    assert _stored(state)[0].chat_folder_id == FOLDER
    assert tab.folder_id == FOLDER


# 5. The attended CLI's direct write cannot check the folder


@pytest.fixture
def cli_caller():
    previous = current_caller()
    set_current_caller(CallerContext(session_key="cli_chat", session_type="cli_chat"))
    yield
    set_current_caller(previous)


@pytest.mark.usefixtures("cli_caller")
async def test_the_cli_fallback_refuses_a_chat_folder_and_writes_nothing() -> None:
    result = mcp_cron._call_tool_locally("cron_add", {**_JOB, "chat_folder_id": FOLDER})

    assert result.startswith("Error: chat_folder_id needs the running gateway")
    assert CronService(base_dir=config_dir()).list_jobs(include_disabled=True) == []


@pytest.mark.usefixtures("cli_caller")
async def test_the_cli_fallback_refuses_a_chat_folder_on_update() -> None:
    added = mcp_cron._call_tool_locally("cron_add", _JOB)
    job_id = added.split("Added job: ", 1)[1].split(" ", 1)[0]

    result = mcp_cron._call_tool_locally(
        "cron_update", {"job_id": job_id, "chat_folder_id": FOLDER}
    )

    assert result.startswith("Error: chat_folder_id needs the running gateway")
    (job,) = CronService(base_dir=config_dir()).list_jobs(include_disabled=True)
    assert job.chat_folder_id == ""


# 6. The folder is deleted between the first check and the write


async def test_a_folder_deleted_before_the_add_commits_is_refused(state) -> None:
    async def hold_folders(section):  # type: ignore[no-untyped-def]  # the cleanup won the lock first
        return await section([])

    state.hold_folders = hold_folders

    result = await _tool(state, "cron_add", {**_JOB, "chat_folder_id": FOLDER})

    assert result.startswith(f"Error: {_UNKNOWN}")
    assert not any(job.chat_folder_id == FOLDER for job in _stored(state))


async def test_a_folder_deleted_before_the_update_commits_is_refused(state) -> None:
    job_id = await _add(state)

    async def hold_folders(section):  # type: ignore[no-untyped-def]  # the cleanup won the lock first
        return await section([])

    state.hold_folders = hold_folders

    result = await _tool(state, "cron_update", {"job_id": job_id, "chat_folder_id": FOLDER})

    assert result.startswith(f"Error: {_UNKNOWN}")
    assert _stored(state)[0].chat_folder_id == ""


async def test_the_add_is_written_inside_the_folder_store_hold(state) -> None:
    """The existence re-check and the job write share one hold of the lock the
    folder cleanup deletes under, so neither can commit between the two."""
    real_hold = state.hold_folders
    seen: list[tuple[int, int]] = []

    async def hold_folders(section):  # type: ignore[no-untyped-def]
        async def _watched(folders):  # type: ignore[no-untyped-def]
            before = len(_stored(state))
            result = await section(folders)
            seen.append((before, len(_stored(state))))
            return result

        return await real_hold(_watched)

    state.hold_folders = hold_folders

    await _add(state, chat_folder_id=FOLDER)

    assert seen == [(0, 1)]


# 7. An agent in a session a channel can drive leaves the sidebar alone

#: The internal headers the cron MCP server's loopback request carries.
_CRON_SERVER = {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-cron"}
_SLACK_THREAD = "slack:1785370133.085469"
_CONTAINED = (
    "Error: this session can receive turns from a channel conversation, so an "
    "agent cannot change the person's sidebar from it"
)


@pytest.mark.parametrize("key", [_SLACK_THREAD, _KEY])
async def test_a_channel_reachable_session_cannot_file_its_job(state, monkeypatch, key) -> None:
    """The refusal every agent sidebar write gets, with nothing stored.

    A ``channel:`` agent may not call ``cron_add`` at all, so the channel-driven
    caller here is a Slack thread's session. The person's own session, sending the
    same headers, still files its job.
    """
    from kiro_crew.dashboard import chat_folders

    rows: list[dict[str, Any]] = []
    sink = SimpleNamespace(log_api_access=lambda **row: rows.append(row))
    monkeypatch.setattr(chat_folders, "sel", lambda: sink)

    result = await _tool(
        state, "cron_add", {**_JOB, "chat_folder_id": FOLDER}, key=key, headers=_CRON_SERVER
    )

    denied = [row for row in rows if row.get("source") == "channel_containment"]
    if key == _KEY:
        assert result.startswith("Added job: "), result
        assert [job.chat_folder_id for job in _stored(state)] == [FOLDER]
        assert denied == []
    else:
        assert result == _CONTAINED
        assert _stored(state) == []
        assert [(row["caller"], row["operation"], row["outcome"]) for row in denied] == [
            ("kirocrew-cron", "cron.create", "denied")
        ]


async def test_an_agent_socket_call_from_a_channel_reachable_session_is_refused(
    state, monkeypatch
) -> None:
    """A call the agent socket admitted carries a capability and no internal secret,
    and gets the same refusal and row, naming the cron server, with nothing stored."""
    from kiro_crew.dashboard import chat_folders

    rows: list[dict[str, Any]] = []
    sink = SimpleNamespace(log_api_access=lambda **row: rows.append(row))
    monkeypatch.setattr(chat_folders, "sel", lambda: sink)

    result = await _tool(
        state,
        "cron_add",
        {**_JOB, "chat_folder_id": FOLDER},
        key=_SLACK_THREAD,
        headers={"X-Internal-Caller": "kirocrew-cron"},
        capability=True,
    )

    assert result == _CONTAINED
    assert _stored(state) == []
    denied = [row for row in rows if row.get("source") == "channel_containment"]
    assert [(row["caller"], row["operation"], row["outcome"]) for row in denied] == [
        ("kirocrew-cron", "cron.create", "denied")
    ]
