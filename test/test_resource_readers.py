"""Exact-resource reads never start workers, refresh caches or select another workspace."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_taskrunner_reads_one_contained_run_and_redacts(tmp_path):
    from kiro_crew.dashboard.resource_readers import taskrunner_reader
    from kiro_crew.task_models import Project, Task, TaskStatus

    task = Task(index=0, title="finished", description="finished", status=TaskStatus.PASSED)
    task.result = "Authorization: Bearer secret-token-for-reader"
    run = Project(
        task_id="run1",
        name="one",
        spec_path="",
        spec_content="",
        work_dir=str(tmp_path / "project"),
    )
    run.tasks = [task]
    other = Project(
        task_id="run2",
        name="private",
        spec_path="",
        spec_content="",
        work_dir=str(tmp_path.parent / "other"),
    )
    reader = taskrunner_reader(SimpleNamespace(_runs={"run1": run, "run2": other}, _tasks={}))
    result = await reader(tmp_path, "run1", "result", "read", {})
    assert result["resource_id"] == "run1"
    assert "secret-token-for-reader" not in json.dumps(result)
    assert "private" not in json.dumps(result)
    with pytest.raises(ValueError, match="resource_scope_mismatch"):
        await reader(tmp_path, "run2", "state", "read", {})
    with pytest.raises(ValueError, match="unsupported_operation"):
        await reader(tmp_path, "run1", "state", "execute", {})


@pytest.mark.asyncio
async def test_issue_radar_uses_only_connected_exact_repo_cache(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.issue_radar.backend import resource_reads, store

    root = tmp_path / "radar"
    monkeypatch.setattr(store, "app_data_dir", lambda name: root)
    store.add_connected_repo("team", "allowed")
    store.add_connected_repo("team", "foreign")
    store.write_issues_cache("team", "allowed", [{"number": 3, "title": "saved"}])
    store.write_issues_cache("team", "foreign", [{"number": 4, "title": "foreign"}])
    rid = resource_reads.resource_id_for_repo(
        {"provider": "github", "host": "github.com", "owner": "team", "repo": "allowed"}
    )
    result = await resource_reads.read_resource(tmp_path, rid, "snapshot", "read", {})
    assert result["issues"][0]["title"] == "saved"
    assert "foreign" not in json.dumps(result)
    assert result["cache_only"] is True
    with pytest.raises(ValueError, match="invalid_body"):
        await resource_reads.read_resource(tmp_path, rid, "snapshot", "read", {"refresh": True})
    with pytest.raises(ValueError, match="resource_not_found"):
        await resource_reads.read_resource(tmp_path, "0" * 64, "snapshot", "read", {})


@pytest.mark.asyncio
async def test_research_reads_exact_campaign_without_workspace_inference(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.auto_research import resource_reads
    from kiro_crew.apps.builtins.auto_research.campaign import publication, storage

    seen = []
    monkeypatch.setattr(
        storage,
        "get_campaign",
        lambda cid: seen.append(cid) or {"id": cid, "status": "ready"},
    )
    monkeypatch.setattr(publication, "_read_report", lambda cid: "saved report")
    result = await resource_reads.read_resource(tmp_path, "abcd1234", "state", "read", {})
    assert result["state"]["id"] == "abcd1234"
    assert seen == ["abcd1234"]
    result = await resource_reads.read_resource(tmp_path, "abcd1234", "report", "read", {})
    assert result["report"] == "saved report"
    with pytest.raises(ValueError, match="unsupported_operation"):
        await resource_reads.read_resource(tmp_path, "abcd1234", "state", "start", {})


@pytest.mark.asyncio
async def test_design_tweak_filters_project_before_proposals_and_never_starts_preview(
    tmp_path, monkeypatch
):
    from kiro_crew.apps.builtins.design_tweak.backend import resource_reads

    data = tmp_path / "data"
    data.mkdir()
    queue = data / "queue"
    queue.mkdir()
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    foreign = tmp_path / "outside"
    foreign.mkdir()
    (data / "config.json").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": "p1",
                        "path": str(project),
                        "name": "allowed",
                        "previewUrl": "http://127.0.0.1:45555/",
                    },
                    {"id": "p2", "path": str(foreign), "name": "private"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (queue / "one.json").write_text(
        json.dumps(
            {
                "id": "one",
                "projectId": "p1",
                "projectRoot": str(project),
                "comments": [],
            }
        ),
        encoding="utf-8",
    )
    (queue / "two.json").write_text(
        json.dumps(
            {
                "id": "two",
                "projectId": "p2",
                "projectRoot": str(foreign),
                "comments": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(resource_reads, "app_data_dir", lambda name: data)
    result = await resource_reads.read_resource(workspace, "p1", "proposals", "read", {})
    assert [r["id"] for r in result["proposals"]] == ["one"]
    assert "private" not in json.dumps(result)
    result = await resource_reads.read_resource(workspace, "p1", "preview", "read", {})
    assert result["preview"]["started"] is False
    assert "previewUrl" not in result["preview"]  # persisted URL is not proof of a live preview
    with pytest.raises(ValueError, match="resource_scope_mismatch"):
        await resource_reads.read_resource(workspace, "p2", "preview", "read", {})


@pytest.mark.asyncio
async def test_taskrunner_symlink_escape_is_refused(tmp_path):
    from kiro_crew.dashboard.resource_readers import taskrunner_reader
    from kiro_crew.task_models import Project

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    reader = taskrunner_reader(
        SimpleNamespace(
            _runs={
                "run": Project(
                    task_id="run",
                    name="escape",
                    spec_path="",
                    spec_content="",
                    work_dir=str(link),
                )
            },
            _tasks={},
        )
    )
    with pytest.raises(ValueError, match="resource_scope_mismatch"):
        await reader(workspace, "run", "state", "read", {})


def test_projection_bounds_nested_provider_text_and_credential_fields():
    from kiro_crew.dashboard.resource_readers import safe_projection

    result = safe_projection({"token": "private-value", "rows": ["x" * 20000] * 100})
    assert result["token"] == "[REDACTED]"
    assert "private-value" not in json.dumps(result)
    assert len(json.dumps(result)) < 60000


@pytest.mark.asyncio
async def test_design_tweak_refuses_conflicting_project_root(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.design_tweak.backend import resource_reads

    data = tmp_path / "data"
    queue = data / "queue"
    queue.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (data / "config.json").write_text(
        json.dumps({"projects": [{"id": "p1", "path": str(project)}]}), encoding="utf-8"
    )
    (queue / "bad.json").write_text(
        json.dumps(
            {
                "id": "bad",
                "projectId": "p1",
                "projectRoot": str(tmp_path / "foreign"),
                "comments": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(resource_reads, "app_data_dir", lambda name: data)
    with pytest.raises(ValueError, match="resource_scope_mismatch"):
        await resource_reads.read_resource(workspace, "p1", "proposals", "read", {})
