"""Owner gate on the workspace CRUD routes and ``POST /api/members/{slug}/thread``.

Both halves are here on purpose. A file that proved only refusal could not tell a
working gate from one that rejects the owner too, and a file that proved only
that the owner still works could not tell a working gate from no gate at all. So
every route below is asserted twice: the non-owner is refused, and the owner
reaches the handler's own outcome.

Ordering is asserted too, because a gate placed below a referential guard leaks
what it was added to protect. Before this change (#6470) any authenticated
dashboard session reached all four -- including the token minted for an
allow-listed Slack user by ``!dashboard``, which carries an empty app identity
and so is authenticated but is not the owner.

The read path is the control: ``GET /api/workspaces`` is NOT gated, and a test
that did not pin that could not tell this change from one that gated the whole
resource.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import NoConfiguredOwner, as_owner

from kiro_crew.dashboard.handlers import (
    api_member_thread,
    api_workspaces,
    api_workspaces_create,
    api_workspaces_delete,
    api_workspaces_update,
)

# A caller that authenticated but is not the owner: the `!dashboard` shape, an
# empty app identity with a subject that is nobody's owner id.
NON_OWNER = {"X-Test-User": "someone-else"}
# An app token, for the members route's pre-existing existence-hiding denial.
APP_TOKEN = {"X-Test-App": "some-app"}


def _workspace_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/workspaces", api_workspaces)
    app.router.add_post("/api/workspaces", api_workspaces_create)
    app.router.add_put("/api/workspaces/{name}", api_workspaces_update)
    app.router.add_delete("/api/workspaces/{name}", api_workspaces_delete)
    return as_owner(app)


def _members_app() -> web.Application:
    app = web.Application()
    # Not None, so the handler's 503 state branch is not what answers; the gate
    # and then the slug grammar are.
    app["state"] = NoConfiguredOwner()
    app.router.add_post("/api/members/{slug}/thread", api_member_thread)
    return as_owner(app)


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    """A config dir carrying a default workspace plus one deletable extra."""
    cfg_dir = tmp_path / ".kirocrew"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "workspaces": {
                    "default": {"dir": "workspace"},
                    "spare": {"dir": "workspace-spare"},
                },
                "default_workspace": "default",
            }
        )
    )
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: cfg_dir)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.config_dir", lambda: cfg_dir)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files.data_home", lambda: tmp_path)
    return cfg_dir


@pytest.fixture(autouse=True)
def _quiet_sel():
    """Keep both the handlers' success audit and the gate's denial audit off disk."""
    with (
        patch("kiro_crew.dashboard.handlers.sel") as handler_sel,
        patch("kiro_crew.sel.sel") as gate_sel,
    ):
        handler_sel.return_value = MagicMock()
        gate_sel.return_value = MagicMock()
        yield


async def _json(resp):
    return await resp.json()


class TestWorkspaceCreateOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, cfg_env, tmp_path):
        target = tmp_path / "made-by-a-non-owner"
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "intruder", "dir": str(target)},
                headers=NON_OWNER,
            )
            assert resp.status == 403
            assert (await _json(resp))["code"] == "owner_only"
        # The refusal is the whole refusal: nothing was created on the way to it.
        assert not target.exists()
        assert "intruder" not in json.loads((cfg_env / "config.json").read_text())["workspaces"]

    @pytest.mark.asyncio
    async def test_owner_still_creates(self, cfg_env, tmp_path):
        target = tmp_path / "made-by-the-owner"
        target.mkdir()
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.post("/api/workspaces", json={"name": "owned", "dir": str(target)})
            assert resp.status == 200, await resp.text()
            assert (await _json(resp))["ok"] is True
        assert "owned" in json.loads((cfg_env / "config.json").read_text())["workspaces"]

    @pytest.mark.asyncio
    async def test_non_owner_is_refused_before_the_body_is_parsed(self, cfg_env):
        """A malformed body must still answer 403, not 400.

        400 here would mean the gate sits below the body read, which is how a
        non-owner learns the shape of a route they may not use.
        """
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                data="{",
                headers={"Content-Type": "application/json", **NON_OWNER},
            )
            assert resp.status == 403


class TestWorkspaceUpdateOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, cfg_env):
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.put(
                "/api/workspaces/spare", json={"dir": "moved"}, headers=NON_OWNER
            )
            assert resp.status == 403
            assert (await _json(resp))["code"] == "owner_only"
        stored = json.loads((cfg_env / "config.json").read_text())["workspaces"]
        assert stored["spare"]["dir"] == "workspace-spare"

    @pytest.mark.asyncio
    async def test_owner_still_updates(self, cfg_env):
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.put("/api/workspaces/spare", json={"dir": "moved"})
            assert resp.status == 200, await resp.text()
        stored = json.loads((cfg_env / "config.json").read_text())["workspaces"]
        assert stored["spare"]["dir"] == "moved"

    @pytest.mark.asyncio
    async def test_non_owner_gets_403_not_404_for_an_unknown_workspace(self, cfg_env):
        """Whether a workspace exists is not a non-owner's to learn."""
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.put(
                "/api/workspaces/no-such-workspace", json={"dir": "x"}, headers=NON_OWNER
            )
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_owner_still_gets_the_404(self, cfg_env):
        """The control for the test above: the 404 is intact, just owner-only."""
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.put("/api/workspaces/no-such-workspace", json={"dir": "x"})
            assert resp.status == 404


class TestWorkspaceDeleteOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, cfg_env):
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.delete("/api/workspaces/spare", headers=NON_OWNER)
            assert resp.status == 403
            assert (await _json(resp))["code"] == "owner_only"
        stored = json.loads((cfg_env / "config.json").read_text())["workspaces"]
        assert "spare" in stored, "the non-owner delete must not have reached cfg.save()"

    @pytest.mark.asyncio
    async def test_owner_still_deletes(self, cfg_env):
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.delete("/api/workspaces/spare")
            assert resp.status == 200, await resp.text()
        stored = json.loads((cfg_env / "config.json").read_text())["workspaces"]
        assert "spare" not in stored

    @pytest.mark.asyncio
    async def test_non_owner_gets_403_not_409_for_the_default_workspace(self, cfg_env):
        """The 409 guards are referential, so they must not answer first."""
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.delete("/api/workspaces/default", headers=NON_OWNER)
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_owner_still_gets_the_409(self, cfg_env):
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.delete("/api/workspaces/default")
            assert resp.status == 409


class TestWorkspaceReadStaysOpen:
    """The control: this change gates the mutations, not the resource."""

    @pytest.mark.asyncio
    async def test_get_is_not_gated(self, cfg_env):
        async with TestClient(TestServer(_workspace_app())) as client:
            resp = await client.get("/api/workspaces", headers=NON_OWNER)
            assert resp.status == 200
            names = [w["name"] for w in (await _json(resp))["workspaces"]]
            assert "spare" in names


class TestMemberThreadOwnerGate:
    """``NotASlug`` fails the slug grammar (``^[a-z0-9]...``), so the owner's 400
    proves the request reached the handler's own validation rather than the gate.
    Full owner-success for this route -- a thread actually created and bound --
    is covered by ``test_members_dm_thread``, whose app now supplies the owner.
    """

    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self):
        async with TestClient(TestServer(_members_app())) as client:
            resp = await client.post("/api/members/code-reviewer/thread", headers=NON_OWNER)
            assert resp.status == 403
            assert (await _json(resp))["code"] == "owner_only"

    @pytest.mark.asyncio
    async def test_non_owner_is_refused_before_the_slug_is_validated(self):
        async with TestClient(TestServer(_members_app())) as client:
            resp = await client.post("/api/members/NotASlug/thread", headers=NON_OWNER)
            assert resp.status == 403
            assert (await _json(resp))["code"] == "owner_only"

    @pytest.mark.asyncio
    async def test_owner_reaches_the_handler(self):
        """Same request, owner identity: past the gate and into slug validation."""
        async with TestClient(TestServer(_members_app())) as client:
            resp = await client.post("/api/members/NotASlug/thread")
            assert resp.status == 400
            assert (await _json(resp))["code"] == "invalid_member_slug"

    @pytest.mark.asyncio
    async def test_app_tokens_keep_the_existing_404(self):
        """The gate is ADDED below ``_deny_app_caller``, not in place of it.

        An app token still gets the module's existence-hiding 404 rather than
        the gate's 403, so this change does not alter what an app can infer.
        """
        async with TestClient(TestServer(_members_app())) as client:
            resp = await client.post(
                "/api/members/code-reviewer/thread", headers={**APP_TOKEN, **NON_OWNER}
            )
            assert resp.status == 404
            assert (await _json(resp))["code"] == "not_found"
