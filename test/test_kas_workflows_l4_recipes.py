"""Recipe routes: ``/state`` recipesRevision, recipe detail, create, delete.

Covers the recipe list following ``recipes_changed``, creating and deleting
recipes, and the recipe detail route. The owner /
engine gate is the shared ``_refusal`` (stubbed here, pinned by the routes that
already use it); the last test proves the new routes still refuse when the
switch is off, because their operations are not in the read-only-when-held set.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from kas_workflow_helpers import FakeRuntime, make_router

import kiro_crew.dashboard.handlers.kas_workflows as kas_handlers
from kiro_crew.dashboard import server
from kiro_crew.kas_workflows import router as router_mod
from kiro_crew.kas_workflows import wire

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _tmp_path_is_a_recent_project(_floor_monkeypatch, tmp_path):
    """These tests use tmp_path as the project, which sits outside the home root.

    Listing it as a recent project keeps the real project-root confinement in
    the path under test instead of switching it off.
    """
    _floor_monkeypatch.setattr(
        "kiro_crew.kas_workflows.project_root._recent_project_dirs", lambda: [str(tmp_path)]
    )


@pytest.fixture
def kas(monkeypatch):
    router = make_router(audit=lambda **_k: None)
    reader = FakeRuntime()
    # _reader() returns the first live runtime in _runtimes, so recipe listings
    # (recipes / recipe_detail / delete) read this one's answers.
    router.sink_for(reader)

    refusals: list[str] = []

    async def no_refusal(_request, operation):
        refusals.append(operation)
        return None

    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(router_mod, "_ROUTER", router)
    monkeypatch.setattr(kas_handlers, "_refusal", no_refusal)
    monkeypatch.setattr(
        kas_handlers, "_audit_authorization", lambda _r, op, outcome: audits.append((op, outcome))
    )
    return SimpleNamespace(router=router, reader=reader, refusals=refusals, audits=audits)


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(get_slot=lambda _k: None)
    server._register_mcp_routes(app)
    return app


# ── Gap 16F: the recipe list's poll key ──


async def test_state_carries_the_recipes_revision(kas):
    kas.router.recipes_revision = 4
    async with TestClient(TestServer(_app())) as client:
        state = await (await client.get("/api/workflows/kas/state")).json()
    assert state["recipesRevision"] == 4
    assert "workflow.kas.state" in kas.refusals


# ── Gap 27F: recipe detail route ──


async def test_recipe_detail_route_returns_steps_for_a_listed_file(kas, tmp_path):
    recipe_file = tmp_path / ".kiro" / "workflows" / "r.workflow.json"
    recipe_file.parent.mkdir(parents=True)
    recipe_file.write_text(
        json.dumps(
            {
                "name": "r",
                "steps": [
                    {"type": "step", "id": "plan", "agent": "wf-planner", "prompt": "Plan it."}
                ],
            }
        )
    )
    kas.reader.answers[wire.METHOD_LIST_RECIPES] = {
        "recipes": [{"name": "r", "source": str(recipe_file)}]
    }
    async with TestClient(TestServer(_app())) as client:
        missing = await client.get(
            "/api/workflows/kas/recipes/detail", params={"cwd": str(tmp_path)}
        )
        assert missing.status == 400, "source is required"
        resp = await client.get(
            "/api/workflows/kas/recipes/detail",
            params={"cwd": str(tmp_path), "source": str(recipe_file)},
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["definitionAvailable"] is True
    assert [(s["nodeId"], s["prompt"]) for s in body["steps"]] == [("plan", "Plan it.")]
    assert body["recipe"]["group"] == "workspace"
    assert "workflow.kas.recipe_detail" in kas.refusals


# ── Gap 18: create a recipe file (create-exclusive, path-confined) ──


async def test_create_route_writes_a_scaffold_once(kas, tmp_path):
    async with TestClient(TestServer(_app())) as client:
        assert (
            await client.post(
                "/api/workflows/kas/recipes",
                json={"cwd": str(tmp_path), "name": "ok", "scope": "x"},
            )
        ).status == 400, "bad scope"
        assert (
            await client.post(
                "/api/workflows/kas/recipes",
                json={"cwd": str(tmp_path), "name": "../evil", "scope": "workspace"},
            )
        ).status == 400, "a name that is not a safe basename"
        resp = await client.post(
            "/api/workflows/kas/recipes",
            json={"cwd": str(tmp_path), "name": "my-wf", "scope": "workspace"},
        )
        assert resp.status == 201, await resp.text()
        source = (await resp.json())["source"]
        again = await client.post(
            "/api/workflows/kas/recipes",
            json={"cwd": str(tmp_path), "name": "my-wf", "scope": "workspace"},
        )
        assert again.status == 409, "create is exclusive"
    assert source == os.path.join(
        os.path.realpath(str(tmp_path)), ".kiro", "workflows", "my-wf.workflow.json"
    )
    written = json.loads(open(source, encoding="utf-8").read())
    assert written["name"] == "my-wf" and written["steps"], "a valid launchable scaffold"
    assert ("workflow.kas.recipe_create", "allowed:my-wf") in kas.audits


async def test_create_refuses_a_symlinked_kiro_that_escapes_the_project(kas, tmp_path):
    # `.kiro` is a symlink out of the project, so <cwd>/.kiro/workflows resolves
    # outside it. The write must be refused before any directory is created.
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".kiro").symlink_to(outside, target_is_directory=True)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(
            "/api/workflows/kas/recipes",
            json={"cwd": str(project), "name": "x", "scope": "workspace"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "recipe_path_escape"
    assert not (outside / "workflows").exists(), "nothing was written outside the project"


# ── Gap 18: delete only a listed, non-built-in recipe file ──


async def test_delete_route_removes_only_a_listed_editable_file(kas, tmp_path):
    recipe_file = tmp_path / ".kiro" / "workflows" / "r.workflow.json"
    recipe_file.parent.mkdir(parents=True)
    recipe_file.write_text("{}")
    kas.reader.answers[wire.METHOD_LIST_RECIPES] = {
        "recipes": [{"name": "r", "source": str(recipe_file)}, {"name": "g", "builtIn": True}]
    }
    async with TestClient(TestServer(_app())) as client:
        unknown = await client.delete(
            "/api/workflows/kas/recipes",
            params={"cwd": str(tmp_path), "source": str(tmp_path / "nope.workflow.json")},
        )
        assert unknown.status == 404, "a source KAS does not list cannot be deleted"
        read_only = await client.delete(
            "/api/workflows/kas/recipes", params={"cwd": str(tmp_path), "source": "bundled://g"}
        )
        assert read_only.status == 409, "a built-in has no file to delete"
        assert recipe_file.exists()
        ok = await client.delete(
            "/api/workflows/kas/recipes", params={"cwd": str(tmp_path), "source": str(recipe_file)}
        )
        assert ok.status == 200, await ok.text()
    assert not recipe_file.exists()
    assert ("workflow.kas.recipe_delete", "allowed") in kas.audits


async def test_delete_refuses_a_listed_file_behind_a_symlinked_kiro(kas, tmp_path):
    # KAS lists what it finds under <cwd>/.kiro/workflows with symlinks
    # followed, so a project whose `.kiro` points outside it makes KAS list an
    # outside file. The delete must confine the file's REAL directory exactly as
    # create confines its write, and leave the outside file alone.
    outside = tmp_path / "outside"
    (outside / "workflows").mkdir(parents=True)
    victim = outside / "workflows" / "r.workflow.json"
    victim.write_text("{}")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".kiro").symlink_to(outside, target_is_directory=True)
    listed = project / ".kiro" / "workflows" / "r.workflow.json"
    kas.reader.answers[wire.METHOD_LIST_RECIPES] = {
        "recipes": [{"name": "r", "source": str(listed)}]
    }
    async with TestClient(TestServer(_app())) as client:
        resp = await client.delete(
            "/api/workflows/kas/recipes", params={"cwd": str(project), "source": str(listed)}
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "recipe_path_escape"
    assert victim.exists(), "nothing outside the project was removed"
    assert ("workflow.kas.recipe_delete", "allowed") not in kas.audits


# ── Gap 5 inheritance: the new routes refuse while the switch is off ──


async def test_the_new_recipe_routes_refuse_when_the_switch_is_off():
    # A read-only-when-held op stays open when off; a recipe op must not, so it
    # is never added to that set. This is what makes the new routes 409 off-KAS.
    for op in (
        "workflow.kas.recipe_create",
        "workflow.kas.recipe_delete",
        "workflow.kas.recipe_detail",
    ):
        assert op not in kas_handlers._READ_ONLY_WHEN_HELD
