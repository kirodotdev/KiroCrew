"""App launch containment and workflow rerun idempotency at the entry points."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request
from test_shared_crew_entry_points import service as _service

from kiro_crew.apps import spawn_sdk
from kiro_crew.dashboard.handlers import workflows
from kiro_crew.shared_crew import admission_entry, containment
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.testing.wait import await_until

service = _service


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ["sync", "async", "deferred_task"])
async def test_app_spawn_task_inherits_prepared_principal(service, monkeypatch, launch):
    monkeypatch.setattr(
        spawn_sdk,
        "list_agents",
        lambda: [SimpleNamespace(name="demo--bg", filename="demo--bg.json")],
    )
    monkeypatch.setattr(spawn_sdk, "is_internal_agent_spec", lambda _agent: False)
    monkeypatch.setattr(containment, "_ACTIVE_SLICES", service._slices)
    expected = containment.principal_slice_name(Principal.system("app:demo").id)
    seen = []
    tasks = []
    release = asyncio.Event()
    info = SimpleNamespace(id="app-child", error="", done=False, parent_session_key="")

    async def child():
        if launch == "deferred_task":
            await await_until(release)
        seen.append(containment.scope_parent_slice())

    def spawn(_task, **_kwargs):
        seen.append(containment.scope_parent_slice())
        tasks.append(containment.start_run_task(child(), info))
        return info

    async def spawn_async(task, **kwargs):
        return spawn(task, **kwargs)

    manager = SimpleNamespace(spawn=spawn, _agents={})
    if launch != "sync":
        manager.spawn_async = spawn_async
    impl = spawn_sdk.build_spawn_impl(manager)
    try:
        with containment.principal_scope("p_other"):
            assert await impl("work", "demo--bg", True, "auto", "demo") == "app-child"
            assert containment._SCOPE_PRINCIPAL.get() == "p_other"
        release.set()
        await await_until(asyncio.gather(*tasks))
        assert seen == [expected, expected]
    finally:
        release.set()
        await await_until(asyncio.gather(*tasks))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_app_spawn_passthrough_clears_and_restores_scope(monkeypatch, failure):
    monkeypatch.setattr(
        spawn_sdk,
        "list_agents",
        lambda: [SimpleNamespace(name="demo--bg", filename="demo--bg.json")],
    )
    monkeypatch.setattr(spawn_sdk, "is_internal_agent_spec", lambda _agent: False)
    admission = SimpleNamespace(
        rejected=False, work=None, abandon=AsyncMock(), attach_child=AsyncMock()
    )
    monkeypatch.setattr(admission_entry, "admit_app_spawn", AsyncMock(return_value=admission))
    seen = []

    async def spawn_async(_task, **_kwargs):
        seen.append(containment._SCOPE_PRINCIPAL.get())
        if failure:
            raise RuntimeError("launch failed")
        return SimpleNamespace(id="app-child", error="")

    impl = spawn_sdk.build_spawn_impl(SimpleNamespace(spawn_async=spawn_async))
    with containment.principal_scope("p_other"):
        if failure:
            with pytest.raises(RuntimeError, match="launch failed"):
                await impl("work", "demo--bg", True, "auto", "demo")
            admission.abandon.assert_awaited_once()
            admission.attach_child.assert_not_awaited()
        else:
            assert await impl("work", "demo--bg", True, "auto", "demo") == "app-child"
            admission.abandon.assert_not_awaited()
        assert containment._SCOPE_PRINCIPAL.get() == "p_other"
    assert seen == [None]


@pytest.mark.asyncio
async def test_bodyless_rerun_key_conflicts_across_target_runs(service, monkeypatch):
    rerun = AsyncMock(return_value={"run_id": "new-run"})
    svc = SimpleNamespace(rerun_subtree=rerun)
    monkeypatch.setattr(workflows, "_svc", lambda _request: svc)
    monkeypatch.setattr(workflows, "_run_scope_refusal", AsyncMock(return_value=None))

    async def request(run_id):
        req = make_mocked_request(
            "POST",
            f"/api/workflows/runs/{run_id}/rerun",
            match_info={"run_id": run_id},
            headers={"X-Session-Key": "same-session", "Idempotency-Key": "same-key"},
        )
        return await workflows.api_workflow_run_rerun(req)

    first = await request("run-a")
    replay = await request("run-a")
    conflict = await request("run-b")
    assert first.status == replay.status == 200
    assert json.loads(replay.body)["replayed"] is True
    assert json.loads(replay.body)["run_id"] == "new-run"
    assert conflict.status == 409
    assert json.loads(conflict.body)["code"] == "idempotency_conflict"
    rerun.assert_awaited_once_with(
        "run-a", 0, source=None, caller_session="same-session", owner=False
    )


def test_shared_crew_feature_map_names_backend_owners():
    root = Path(__file__).resolve().parents[1]
    feature_map = (root / "docs/feature-map/README.md").read_text(encoding="utf-8")
    [row] = [
        line for line in feature_map.splitlines() if line.startswith("| Shared Crew backend |")
    ]
    assert "API only" in row and "None (backend only)" in row
    for name in ("routes_admission", "routes_collab", "routes_capabilities"):
        owner = f"shared_crew/{name}.py"
        assert owner in row
        assert (root / "src/kiro_crew" / owner).is_file()
    assert "GET /api/shared-crew/capacity" in row
    assert "POST /api/shared-crew/work/{work_id}/cancel" in row


@pytest.mark.asyncio
async def test_app_startup_hooks_find_admission_ready(monkeypatch):
    """App hooks find wiring installed without opening the admission store."""
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.apps import dev_mode
    from kiro_crew.dashboard import server
    from kiro_crew.shared_crew import wiring

    order: list[str] = []

    def install(_state):
        order.append("wiring")

    async def startup(**_kwargs):
        order.append("app hooks")

    async def no_pause(_what):
        return None

    async def no_watcher(_broadcast):
        return None

    builds = AsyncMock(side_effect=AssertionError("pre-bind service construction"))
    monkeypatch.setattr(wiring, "install", install)
    monkeypatch.setattr(wiring.admission_gate, "current_service", builds)
    monkeypatch.setattr(server, "on_gateway_startup", startup)
    monkeypatch.setattr(server, "init_hooks_system", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "init_hook_reconciler", lambda **_kw: None)
    monkeypatch.setattr(server, "start_enabled_app_backends", lambda: [])
    monkeypatch.setattr(server.cautious_boot, "pause_before", no_pause)
    monkeypatch.setattr(dev_mode, "init_dev_mode_watcher", no_watcher)
    app = web.Application()
    await server._start_app_backends(app, MagicMock())
    for hook in list(app.on_startup):
        await hook(app)
    assert order == ["wiring", "app hooks"]
    builds.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_app_first_spawn_resolves_config_off_loop(service, monkeypatch, enabled):
    import threading

    from kiro_crew.config import sections, sections_shared_crew
    from kiro_crew.shared_crew import admission_gate as ag

    loop_thread = threading.get_ident()
    reads = []

    def load():
        reads.append(threading.get_ident())
        assert reads[-1] != loop_thread
        return sections_shared_crew.SharedCrewConfig(admission_enabled=enabled)

    builds = AsyncMock(return_value=service)
    monkeypatch.setattr(ag, "_SERVICE", None)
    monkeypatch.setattr(ag, "current_service", builds)
    monkeypatch.setattr(sections, "load_shared_crew_config", load)
    result = await admission_entry.admit_app_spawn(None, "demo")
    assert len(reads) == 1
    if enabled:
        builds.assert_awaited_once()
        assert result.admitted and result.work is not None
        assert result.work.budget_key == Principal.system("app:demo").id
        await result.abandon()
    else:
        builds.assert_not_awaited()
        assert result.passthrough and result.service is None


@pytest.mark.asyncio
async def test_app_first_spawn_unreadable_config_never_passes_through(monkeypatch):
    from unittest.mock import Mock

    from kiro_crew.config import sections
    from kiro_crew.shared_crew import admission_gate as ag

    monkeypatch.setattr(ag, "_SERVICE", None)
    monkeypatch.setattr(
        sections, "load_shared_crew_config", Mock(side_effect=ValueError("bad config"))
    )
    with pytest.raises(ValueError, match="bad config"):
        await admission_entry.admit_app_spawn(None, "demo")
