"""Dashboard move endpoints for crew-to-crew migration (issue #7577).

Tasks 2.6 / 3.8 / 4.8 -- the dashboard half. Three POST endpoints, one per unit
kind, each returning the migration PLAN (handoff id, allow-listed field count,
the target's blocking requirements, and any advisory findings) so the UI can show
the user what a move would do before the transmit step exists.

The gateway is the surface that CAN plan a session move: unlike the CLI it holds
the live slot, and unlike runs.json its task-run records carry WorkingMemory and
current_task. Both advantages are asserted here.

Side-effect discipline: MagicMock requests and fake state, mirroring
test_api_health.py. No aiohttp server, no gateway, no disk.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard.handlers import migration as mig_h
from kiro_crew.task_models import Project, Task, TaskStatus, WorkingMemory


def _req(state, *, match=None, body=None) -> web.Request:
    req = MagicMock(spec=web.Request)
    req.app = {"state": state}
    req.match_info = match or {}
    req.json = AsyncMock(return_value=body if body is not None else {})
    req.get = MagicMock(return_value="")
    req.headers = {}
    req.remote = "127.0.0.1"
    return req


def _cron_job():
    return CronJob(
        id="j1",
        name="nightly",
        message="run backup",
        schedule=CronSchedule(kind="cron", cron_expr="0 3 * * *"),
        agent_id="kirocrew",
        timezone="America/New_York",
    )


def _live_project():
    """A LIVE Project — carries the state runs.json does not persist."""
    return Project(
        spec_path="/repo/spec.md",
        spec_content="# plan",
        tasks=[
            Task(index=0, title="alpha", description="", status=TaskStatus.PASSED),
            Task(index=1, title="beta", description="", status=TaskStatus.PENDING),
        ],
        current_task=1,
        task_id="TASK_abc",
        replan_count=2,
        memory=WorkingMemory(files_changed=["src/x.py"], decisions=["chose Y"]),
        repo_root="/repo",
        branch_name="feat/x",
        worktree_path="/wt/x",
    )


# --------------------------------------------------------- cron move (2.6)


@pytest.mark.asyncio
async def test_cron_move_returns_a_plan():
    crons = SimpleNamespace(get_job=lambda jid: _cron_job() if jid == "j1" else None)
    state = SimpleNamespace(crons=crons)
    resp = await mig_h.api_cron_move(
        _req(state, match={"job_id": "j1"}, body={"to_crew": "remote-ec2"})
    )
    assert resp.status == 200
    plan = json.loads(resp.body)["plan"]
    assert plan["bundle_kind"] == "cron"
    assert plan["target_crew"] == "remote-ec2"
    assert plan["handoff_id"]
    assert plan["ships"] > 0
    kinds = {r["kind"] for r in plan["requirements"]}
    assert "agent" in kinds


@pytest.mark.asyncio
async def test_cron_move_requires_a_target_crew():
    state = SimpleNamespace(crons=SimpleNamespace(get_job=lambda jid: _cron_job()))
    resp = await mig_h.api_cron_move(_req(state, match={"job_id": "j1"}, body={}))
    assert resp.status == 400
    assert "to_crew" in json.loads(resp.body)["error"]


@pytest.mark.asyncio
async def test_cron_move_unknown_job_is_404():
    state = SimpleNamespace(crons=SimpleNamespace(get_job=lambda jid: None))
    resp = await mig_h.api_cron_move(_req(state, match={"job_id": "nope"}, body={"to_crew": "dst"}))
    assert resp.status == 404


# ------------------------------------------------------ taskrun move (4.8)


@pytest.mark.asyncio
async def test_taskrun_move_plan_uses_the_live_record_fidelity():
    runner = SimpleNamespace(_runs={"TASK_abc": _live_project()})
    state = SimpleNamespace(task_runner=runner)
    resp = await mig_h.api_taskrun_move(
        _req(state, match={"task_id": "TASK_abc"}, body={"to_crew": "dst"})
    )
    assert resp.status == 200
    plan = json.loads(resp.body)["plan"]
    assert plan["bundle_kind"] == "taskrun"
    # the gateway holds WorkingMemory + current_task, so there is NO fidelity gap
    keys = {f["detail_key"] for f in plan["findings"]}
    assert "memory" not in keys and "current_task" not in keys
    # completed work is reported as kept
    assert plan["completed_kept"] == 1


@pytest.mark.asyncio
async def test_taskrun_move_refuses_a_mid_execution_run_with_409():
    proj = _live_project()
    proj.tasks[1].status = TaskStatus.IN_PROGRESS
    state = SimpleNamespace(task_runner=SimpleNamespace(_runs={"TASK_abc": proj}))
    resp = await mig_h.api_taskrun_move(
        _req(state, match={"task_id": "TASK_abc"}, body={"to_crew": "dst"})
    )
    assert resp.status == 409
    assert "mid" in json.loads(resp.body)["error"].lower()


@pytest.mark.asyncio
async def test_taskrun_move_without_a_runner_is_503():
    state = SimpleNamespace(task_runner=None)
    resp = await mig_h.api_taskrun_move(
        _req(state, match={"task_id": "TASK_abc"}, body={"to_crew": "dst"})
    )
    assert resp.status == 503


# ------------------------------------------------------ session move (3.8)


@pytest.mark.asyncio
async def test_session_move_plan_is_built_from_the_live_slot():
    slot = SimpleNamespace(key="chat-3")
    state = SimpleNamespace(get_slot=lambda name: slot if name == "chat-3" else None)
    bundle = {
        "transcript": ["hi"],
        "layer_b": {"sid": "s1"},
        "project": "/Users/alice/wt",
        "agent": "kirocrew",
    }
    with patch.object(mig_h, "_build_session_bundle", AsyncMock(return_value=bundle)):
        resp = await mig_h.api_session_move(
            _req(state, match={"slot": "chat-3"}, body={"to_crew": "remote-ec2"})
        )
    assert resp.status == 200
    plan = json.loads(resp.body)["plan"]
    assert plan["bundle_kind"] == "session"
    # non-portable references are reported, not swallowed
    keys = {f["detail_key"] for f in plan["findings"]}
    assert "project" in keys
    # Layer B present -> no fidelity warning
    assert "layer_b" not in keys


@pytest.mark.asyncio
async def test_session_move_reports_a_missing_layer_b():
    slot = SimpleNamespace(key="chat-3")
    state = SimpleNamespace(get_slot=lambda name: slot)
    with patch.object(
        mig_h, "_build_session_bundle", AsyncMock(return_value={"transcript": ["hi"]})
    ):
        resp = await mig_h.api_session_move(
            _req(state, match={"slot": "chat-3"}, body={"to_crew": "dst"})
        )
    plan = json.loads(resp.body)["plan"]
    assert "layer_b" in {f["detail_key"] for f in plan["findings"]}


@pytest.mark.asyncio
async def test_session_move_carries_the_derived_requirements():
    """The endpoint used to hard-code requirements=[], so a session plan could
    never tell the user what the target had to have."""
    slot = SimpleNamespace(key="chat-3")
    state = SimpleNamespace(get_slot=lambda name: slot)
    bundle = {
        "transcript": ["hi"],
        "layer_b": {"sid": "s1"},
        "agent": "kirocrew-research",
        "project": "/Users/alice/wt",
        "mcp_servers": ["kirocrew-core"],
    }
    with patch.object(mig_h, "_build_session_bundle", AsyncMock(return_value=bundle)):
        resp = await mig_h.api_session_move(
            _req(state, match={"slot": "chat-3"}, body={"to_crew": "dst"})
        )
    plan = json.loads(resp.body)["plan"]
    kinds = {r["kind"] for r in plan["requirements"]}
    assert {"agent", "project_checkout", "mcp_server"} <= kinds
    mcp = next(r for r in plan["requirements"] if r["kind"] == "mcp_server")
    assert mcp["severity"] == "blocking"


# ------------- A4: the Schedule page can see where a job went (Req 7.3)


@pytest.mark.asyncio
async def test_the_cron_list_endpoint_reports_a_migrated_job(tmp_path, monkeypatch):
    """Req 7.3 names "the surface that listed the unit", and for most users that
    is the dashboard Schedule page, not `kirocrew cron list`. Without this the
    page shows a migrated job as an ordinary paused one."""
    from kiro_crew.dashboard.handlers import cron as cron_h
    from kiro_crew.migration import protocol as MP
    from kiro_crew.migration.tombstones import TombstoneRegistry

    reg = TombstoneRegistry(store_dir=tmp_path / "migration")
    reg.record(
        "cron",
        "job-1",
        MP.Tombstone(
            unit_kind="cron",
            target_crew=MP.CrewRef(crew_id="remote-ec2", label="EC2 box"),
            remote_unit_id="cron-77",
            migrated_ts=5.0,
        ),
    )

    monkeypatch.setattr(cron_h, "config_dir", lambda: tmp_path, raising=False)

    rows = cron_h.attach_migration_tombstones([{"id": "job-1"}, {"id": "job-2"}])

    moved = next(r for r in rows if r["id"] == "job-1")
    assert moved["migrated_to"]["crew_id"] == "remote-ec2"
    assert moved["migrated_to"]["label"] == "EC2 box"
    assert moved["migrated_to"]["remote_unit_id"] == "cron-77"
    # a job that never moved carries an explicit null, not a missing key —
    # the frontend should not have to distinguish absent from not-migrated
    assert next(r for r in rows if r["id"] == "job-2")["migrated_to"] is None


@pytest.mark.asyncio
async def test_the_cron_list_endpoint_survives_a_broken_registry(tmp_path, monkeypatch):
    """Same floor as the CLI: a broken registry must not take the listing down."""
    from kiro_crew.dashboard.handlers import cron as cron_h
    from kiro_crew.migration import tombstones as tomb_mod

    def boom(**_kw):
        raise OSError("nope")

    monkeypatch.setattr(tomb_mod, "TombstoneRegistry", boom)
    monkeypatch.setattr(cron_h, "config_dir", lambda: tmp_path, raising=False)

    rows = cron_h.attach_migration_tombstones([{"id": "job-1"}])
    assert rows[0]["migrated_to"] is None


@pytest.mark.asyncio
async def test_session_move_unknown_slot_is_404():
    state = SimpleNamespace(get_slot=lambda name: None)
    resp = await mig_h.api_session_move(
        _req(state, match={"slot": "ghost"}, body={"to_crew": "dst"})
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_all_three_endpoints_reject_a_blank_target():
    state = SimpleNamespace(
        crons=SimpleNamespace(get_job=lambda jid: _cron_job()),
        task_runner=SimpleNamespace(_runs={"TASK_abc": _live_project()}),
        get_slot=lambda name: SimpleNamespace(key="chat-3"),
    )
    for handler, match in (
        (mig_h.api_cron_move, {"job_id": "j1"}),
        (mig_h.api_taskrun_move, {"task_id": "TASK_abc"}),
        (mig_h.api_session_move, {"slot": "chat-3"}),
    ):
        resp = await handler(_req(state, match=match, body={"to_crew": "   "}))
        assert resp.status == 400, f"{handler.__name__} accepted a blank crew"


def test_the_three_routes_are_actually_registered():
    """A handler nobody routed to is dead code — assert the wiring, not just the
    function. Mirrors the reachability lesson: existence is not reachability."""
    from kiro_crew.dashboard import handlers as h

    # exported for server.py / routes/chat.py to reference
    for name in ("api_cron_move", "api_taskrun_move", "api_session_move"):
        assert hasattr(h, name), f"{name} is not exported from handlers"

    import inspect

    from kiro_crew.dashboard import server as server_mod
    from kiro_crew.dashboard.routes import chat as chat_routes

    server_src = inspect.getsource(server_mod)
    assert '"/api/crons/{job_id}/move"' in server_src
    assert '"/api/taskrunner/{task_id}/move"' in server_src
    chat_src = inspect.getsource(chat_routes)
    assert '"/api/chat/slots/{slot}/move"' in chat_src
