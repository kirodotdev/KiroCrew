"""Tests for workspace create/update with absolute path support."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import api_workspaces_create, api_workspaces_update


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/workspaces", api_workspaces_create)
    app.router.add_put("/api/workspaces/{name}", api_workspaces_update)
    # Both routes are owner-gated; without an owner identity every test below
    # would answer 403 on the gate instead of on the path handling it names.
    return as_owner(app)


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    """Set up a clean config dir with a default workspace."""
    cfg_dir = tmp_path / ".kirocrew"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(json.dumps({
        "workspaces": {"default": {"dir": "workspace"}}
    }))
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_path", lambda: cfg_file
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: cfg_dir
    )
    # Also patch the local import in handlers
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.config_dir", lambda: cfg_dir
    )
    return cfg_dir


class TestWorkspaceCreateAbsolutePath:
    @pytest.mark.asyncio
    async def test_create_with_absolute_path(self, cfg_env, mock_sel, tmp_path):
        abs_dir = tmp_path / "my-project"
        abs_dir.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "myproj", "dir": str(abs_dir)},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_create_with_relative_path(self, cfg_env, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "relws", "dir": "workspace-relws"},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_create_relative_traversal_blocked(self, cfg_env, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "evil", "dir": "../../etc"},
            )
            assert resp.status == 400
            assert "Invalid" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_absolute_path_skips_traversal_check(self, cfg_env, mock_sel, tmp_path):
        """Absolute paths outside config_dir are allowed (not traversal)."""
        outside = tmp_path / "outside"
        outside.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "outside", "dir": str(outside)},
            )
            assert resp.status == 200


class TestWorkspaceUpdateAbsolutePath:
    @pytest.mark.asyncio
    async def test_update_with_absolute_path(self, cfg_env, mock_sel, tmp_path):
        abs_dir = tmp_path / "new-location"
        abs_dir.mkdir()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/workspaces/default",
                json={"dir": str(abs_dir)},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_update_relative_traversal_blocked(self, cfg_env, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/workspaces/default",
                json={"dir": "../../../tmp"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_sensitive_path_blocked(self, cfg_env, mock_sel):
        """Absolute paths to sensitive dirs like ~/.aws are rejected."""
        import os
        sensitive = os.path.expanduser("~/.aws")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "evil", "dir": sensitive},
            )
            assert resp.status == 400
            assert "Invalid" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_update_sensitive_path_blocked(self, cfg_env, mock_sel):
        """Updating workspace to sensitive dir is rejected."""
        import os
        sensitive = os.path.expanduser("~/.ssh")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/workspaces/default",
                json={"dir": sensitive},
            )
            assert resp.status == 400
            assert "Invalid" in (await resp.json())["error"]


class TestWorkspaceCreateFieldTypes:
    """A non-string ``name`` or ``copy_from`` has no ``.strip()``, so it is a coded 400."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,field",
        [
            ({"name": 5}, "name"),
            ({"name": ["ws"]}, "name"),
            ({"name": None}, "name"),
            ({"name": "newws", "copy_from": 5}, "copy_from"),
            ({"name": "newws", "copy_from": {"a": 1}}, "copy_from"),
            ({"name": "newws", "copy_from": None}, "copy_from"),
        ],
        ids=repr,
    )
    async def test_non_string_field_is_a_coded_400(self, cfg_env, mock_sel, body, field):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/workspaces", json=body)
            assert resp.status == 400
            assert await resp.json() == {
                "error": f"{field} must be a string",
                "code": "invalid_field_type",
            }
        assert "newws" not in json.loads((cfg_env / "config.json").read_text())["workspaces"]

    @pytest.mark.asyncio
    async def test_blank_name_keeps_its_answer(self, cfg_env, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/workspaces", json={"name": "  "})
            assert resp.status == 400
            assert await resp.json() == {"error": "Workspace name is required"}


class _ReachedSensitiveCheck(Exception):
    pass


class TestCopyFromExtendedLengthPrefix:
    r"""Windows' ``resolve()`` keeps a caller-supplied ``\\?\`` prefix while the
    data home resolves plain. The fold is lexical, so faking the prefixed
    resolve result exercises the same comparison on any OS."""

    @pytest.fixture()
    def home(self, cfg_env, monkeypatch):
        (cfg_env / "workspace").mkdir()
        cfg = json.loads((cfg_env / "config.json").read_text())
        cfg["workspaces"]["src"] = {"dir": "workspace-src"}
        (cfg_env / "config.json").write_text(json.dumps(cfg))
        (cfg_env / "workspace-src").mkdir()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.files.data_home", lambda: cfg_env)
        return cfg_env

    @staticmethod
    def _prefix_resolve(monkeypatch, root, targets):
        from pathlib import Path

        real_resolve = Path.resolve

        # The data home object itself stays plain, as Windows resolves it.
        def resolve(self, *a, **kw):
            if self is not root and str(self) in targets:
                return Path("\\\\?\\" + str(self))
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", resolve)

    @staticmethod
    def _sensitive_sentinel(monkeypatch):
        def reached(_path):
            raise _ReachedSensitiveCheck

        monkeypatch.setattr("kiro_crew.dashboard.handlers.files.is_sensitive_path", reached)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("side", ["dst", "src"])
    async def test_in_root_prefixed_dir_passes_containment(self, home, mock_sel, monkeypatch, side):
        dst = home / "workspace-new1"
        src = home / "workspace-src"
        self._prefix_resolve(monkeypatch, home, {str(dst if side == "dst" else src)})
        self._sensitive_sentinel(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "new1", "copy_from": "src", "dir": str(dst)},
            )
            assert resp.status == 500, await resp.text()
        mock_sel.log_api_access.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "dir_of,error",
        [
            (lambda h: h.parent / "outside", "Invalid directory path"),
            (lambda h: h, "Cannot use config root as workspace directory"),
            (lambda h: h / "workspace-x.", "Invalid directory path"),
            (lambda h: h / "NUL", "Invalid directory path"),
        ],
        ids=["out-of-root", "data-home-root", "trailing-dot", "dos-device"],
    )
    async def test_prefixed_dir_refusals_unchanged(
        self, home, mock_sel, monkeypatch, dir_of, error
    ):
        target = dir_of(home)
        self._prefix_resolve(monkeypatch, home, {str(target)})
        self._sensitive_sentinel(monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/workspaces",
                json={"name": "new1", "copy_from": "src", "dir": str(target)},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == error
        assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"
