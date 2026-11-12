"""``new_chat_per_run``: each run of a stateless cron opens a chat tab of its own.

A stateless job has no tab, so its result could be read but not replied to. With
the switch on, the normal delivery binds ``cron-<id>-<run>`` to that run's own
``cron:<id>:<run>`` session, and a job without it keeps the one ``cron-<id>`` tab.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronJob, CronService
from kiro_crew.dashboard.chat_utils_slots import dashboard_slot_key
from kiro_crew.session_surface import set_dashboard_surfaced

JOB_ID = "ab12cd34"


@pytest.fixture(autouse=True)
def _isolate(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    _floor_monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    _floor_monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    _floor_monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:new-chat-slot")
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _state():
    """A dashboard state whose slot table is a real dict (see test_cron_first_run_tab)."""
    state = MagicMock()
    slots: dict[str, MagicMock] = {}
    state._slots = slots
    state._background_tasks = set()

    def get_or_create_slot(name=None, agent="", origin=""):
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot.linked_session_key = ""
            slot.messages = []
            slot.folder_id = ""

            def append(role, content, cls, broadcast=True, meta=None, mint_mid=True):
                msg = {"role": role, "content": content, "cls": cls, "meta": {"mid": "m"}}
                slot.messages.append(msg)
                return msg

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.get_slot = lambda name: slots.get(name)
    state.has_slot = lambda name: name in slots
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = []
    state.notify = MagicMock()
    return state


def _job(**over) -> CronJob:
    fields = {
        "id": JOB_ID,
        "name": "monthly status",
        "message": "draft this month's update",
        "persistent_session": False,
        "new_chat_per_run": True,
    }
    fields.update(over)
    return CronJob(**fields)


async def _deliver(state, job, session_key, result):
    from kiro_crew.slack import gateway

    gw = gateway.GatewayOrchestrator.__new__(gateway.GatewayOrchestrator)
    gw.dashboard_state = state
    gw.slack = None
    gw._deliver_cron_to_channel = AsyncMock(return_value=False)
    job.set_run_result(result)
    await gateway.GatewayOrchestrator._deliver_cron_agent_result(
        gw, job, session_key, MagicMock(), result, f"hash-{result}", None, None
    )
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.wait(others, timeout=5)


@pytest.mark.asyncio
async def test_two_runs_open_two_tabs_on_their_own_sessions():
    state = _state()
    job = _job()
    first, second = f"cron:{JOB_ID}:11111111", f"cron:{JOB_ID}:22222222"

    await _deliver(state, job, first, "draft one")
    await _deliver(state, job, second, "draft two")

    assert sorted(state._slots) == [f"cron-{JOB_ID}-11111111", f"cron-{JOB_ID}-22222222"]
    one, two = state._slots[f"cron-{JOB_ID}-11111111"], state._slots[f"cron-{JOB_ID}-22222222"]
    assert (one.linked_session_key, two.linked_session_key) == (first, second)
    assert "draft one" in one.messages[-1]["content"]
    assert "draft two" in two.messages[-1]["content"]
    assert not any("draft one" in m["content"] for m in two.messages)
    assert state.notify.call_args.kwargs["meta"]["slot"] == f"cron-{JOB_ID}-22222222"
    # A run with a tab is kept like a chat, not pruned with untabbed run transcripts.
    assert [c.args for c in state.conversation_log.update_metadata.call_args_list] == [
        (first, {"cron_run": "tab"}),
        (second, {"cron_run": "tab"}),
    ]


@pytest.mark.asyncio
async def test_a_run_whose_transcript_cannot_be_kept_opens_no_tab():
    # The tab would outlive its transcript: the prune deletes an "ephemeral" run
    # after 7 days. So the failed re-tag withholds the tab; the result still notifies.
    state = _state()
    state.conversation_log.update_metadata.side_effect = OSError("disk full")

    await _deliver(state, _job(), f"cron:{JOB_ID}:11111111", "draft")

    assert state._slots == {}
    assert "slot" not in state.notify.call_args.kwargs["meta"]
    assert "draft" in state.notify.call_args.args[2]


@pytest.mark.asyncio
async def test_a_job_without_the_switch_keeps_its_one_tab():
    state = _state()
    job = _job(persistent_session=True, new_chat_per_run=False)

    await _deliver(state, job, f"cron:{JOB_ID}", "digest")

    assert list(state._slots) == [f"cron-{JOB_ID}"]
    assert state._slots[f"cron-{JOB_ID}"].linked_session_key == f"cron:{JOB_ID}"


@pytest.mark.asyncio
async def test_a_hidden_run_opens_no_tab():
    state = _state()

    await _deliver(state, _job(hide_in_chat=True), f"cron:{JOB_ID}:11111111", "draft")

    assert state._slots == {}
    assert "slot" not in state.notify.call_args.kwargs["meta"]


def test_a_run_key_resolves_to_its_own_tab():
    run_key = f"cron:{JOB_ID}:11111111"
    set_dashboard_surfaced({run_key})
    assert dashboard_slot_key(run_key) == f"cron-{JOB_ID}-11111111"
    # A run tab named by its transcript stem resolves by its published name.
    set_dashboard_surfaced({run_key}, {run_key: f"cron_{JOB_ID}_11111111"})
    assert dashboard_slot_key(run_key) == f"cron_{JOB_ID}_11111111"
    # Only the job's tab is open: a run key still resolves to it.
    set_dashboard_surfaced({f"cron:{JOB_ID}"})
    assert dashboard_slot_key(run_key) == f"cron-{JOB_ID}"


def test_the_store_refuses_the_switch_on_a_persistent_job(tmp_path):
    svc = CronService()
    with pytest.raises(ValueError, match="fresh session per run"):
        svc.add_job(name="m", message="x", every_secs=3600, new_chat_per_run=True)
    job = svc.add_job(name="p", message="x", every_secs=3600)
    with pytest.raises(ValueError, match="fresh session per run"):
        svc.update_job(job.id, new_chat_per_run=True)
    assert svc.get_job(job.id).new_chat_per_run is False


def test_the_switch_round_trips_and_persistence_turns_it_off(tmp_path):
    svc = CronService()
    job = svc.add_job(
        name="m", message="x", every_secs=86400, persistent_session=False, new_chat_per_run=True
    )
    plain = svc.add_job(name="p", message="x", every_secs=3600)
    raw = {j["id"]: j for j in json.loads((tmp_path / "crons.json").read_text())["jobs"]}
    assert raw[job.id]["new_chat_per_run"] is True
    assert "new_chat_per_run" not in raw[plain.id]
    assert CronService().get_job(job.id).new_chat_per_run is True

    updated = svc.update_job(job.id, persistent_session=True)
    assert (updated.persistent_session, updated.new_chat_per_run) == (True, False)
    assert CronService().get_job(job.id).new_chat_per_run is False


def test_the_store_refuses_the_switch_on_a_job_with_no_chat_tab(tmp_path):
    svc = CronService()
    with pytest.raises(ValueError, match="single-agent"):
        svc.add_job(
            name="c",
            message="",
            command="true",
            every_secs=3600,
            persistent_session=False,
            new_chat_per_run=True,
        )
    job = svc.add_job(name="s", message="", command="true", every_secs=3600)
    with pytest.raises(ValueError, match="single-agent"):
        svc.update_job(job.id, persistent_session=False, new_chat_per_run=True)


@pytest.mark.parametrize(
    "schedule",
    [
        {"every_secs": 86400 - 60},
        {"cron_expr": "0 * * * *"},
        {"cron_expr": "0 9,21 * * *"},
        {"cron_expr": "@hourly"},
        {"cron_expr": "*/5 * * 11 mon#5"},  # matches no day in 2000; first fires 2004
    ],
)
def test_the_store_refuses_the_switch_on_a_job_that_runs_more_than_once_a_day(tmp_path, schedule):
    svc = CronService()
    with pytest.raises(ValueError, match="at most once a day"):
        svc.add_job(
            name="m", message="x", persistent_session=False, new_chat_per_run=True, **schedule
        )
    # An update that turns the switch on, or moves a per-run job onto the
    # schedule, is refused the same way, and leaves the job as it was.
    plain = svc.add_job(name="p", message="x", persistent_session=False, **schedule)
    with pytest.raises(ValueError, match="at most once a day"):
        svc.update_job(plain.id, new_chat_per_run=True)
    assert svc.get_job(plain.id).new_chat_per_run is False
    daily = svc.add_job(
        name="d",
        message="x",
        cron_expr="0 9 * * *",
        persistent_session=False,
        new_chat_per_run=True,
    )
    with pytest.raises(ValueError, match="at most once a day"):
        svc.update_job(daily.id, **schedule)
    assert svc.get_job(daily.id).schedule.cron_expr == "0 9 * * *"


@pytest.mark.parametrize(
    "schedule",
    [
        {"every_secs": 86400},
        {"cron_expr": "0 9 * * *"},
        {"cron_expr": "0 9 8 * *"},
        {"cron_expr": "0 9 1,2 * *"},  # exactly 24 hours apart
        {"cron_expr": "@weekly"},
        {"at_ts": 4102444800.0},
    ],
)
def test_the_store_takes_the_switch_on_a_job_that_runs_at_most_once_a_day(tmp_path, schedule):
    job = CronService().add_job(
        name="m", message="x", persistent_session=False, new_chat_per_run=True, **schedule
    )
    assert job.new_chat_per_run is True


def test_cron_add_with_the_switch_gets_a_fresh_session_per_run():
    from kiro_crew.mcp_cron import _call_tool_inner

    result = _call_tool_inner(
        "cron_add", {"name": "monthly", "message": "hi", "every": 86400, "new_chat_per_run": True}
    )

    assert "Added job" in result
    job = next(j for j in CronService().list_jobs() if j.name == "monthly")
    assert (job.persistent_session, job.new_chat_per_run) == (False, True)


def _rest_app(handler, route: str, **store):
    from types import SimpleNamespace

    from aiohttp import web
    from dashboard_owner_helpers import as_owner

    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**store),
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
        _folders=[],
    )
    app.router.add_route("*", route, handler)
    return as_owner(app)


@pytest.mark.asyncio
async def test_the_rest_routes_take_the_switch_and_refuse_a_non_boolean():
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons_create

    add = AsyncMock(return_value=_job())
    create = {"name": "m", "message": "x", "every": 3600}
    async with TestClient(
        TestServer(_rest_app(api_crons_create, "/api/crons", add_job_async=add))
    ) as c:
        assert (await c.post("/api/crons", json={**create, "new_chat_per_run": True})).status == 200
        refused = await c.post("/api/crons", json={**create, "new_chat_per_run": "true"})
        assert refused.status == 400
        assert (await refused.json())["code"] == "invalid_new_chat_per_run"
    assert add.await_count == 1
    assert add.await_args.kwargs["new_chat_per_run"] is True
    assert add.await_args.kwargs["persistent_session"] is False

    update = AsyncMock(return_value=_job())
    app = _rest_app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
    async with TestClient(TestServer(app)) as c:
        refused = await c.patch(f"/api/crons/{JOB_ID}", json={"new_chat_per_run": "false"})
        assert refused.status == 400
        update.assert_not_awaited()
        assert (
            await c.patch(f"/api/crons/{JOB_ID}", json={"new_chat_per_run": True})
        ).status == 200
    assert update.await_args.kwargs["new_chat_per_run"] is True
    assert update.await_args.kwargs["persistent_session"] is False
