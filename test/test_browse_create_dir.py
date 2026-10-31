"""Tests for ``POST /api/browse-dirs/create``: the project picker's New folder control.

The route makes one folder in the directory the picker's Browse pane shows. It is
owner-only, takes exactly one plain name, resolves its parent the way
``/api/browse-dirs`` does, refuses a sensitive parent or a sensitive new path, and
creates through the parent's pinned descriptor so a component swapped for a link
after the check is refused rather than followed.
"""

from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from conftest import requires_symlinks
from kiro_crew import pinned_fs, platform_compat
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import api_browse_create_dir
from kiro_crew.dashboard.handlers import files as files_mod
from kiro_crew.dashboard.handlers.files import _browse_dir_name_is_valid, _create_browse_dir_sync
from kiro_crew.dashboard.routes import taskrunner

ROUTE = "/api/browse-dirs/create"

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post(ROUTE, api_browse_create_dir)
    return as_owner(app)


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


async def _post(body: object, headers: dict | None = None) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(ROUTE, json=body, headers=headers or {})
        return resp.status, await resp.json()


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_the_folder_and_answers_its_path(self, tmp_path, mock_sel):
        status, data = await _post({"parent": str(tmp_path), "name": "new-project"})
        assert status == 201
        created = tmp_path / "new-project"
        assert created.is_dir()
        assert data == {"path": os.path.realpath(created), "parent": os.path.realpath(tmp_path)}
        mock_sel.log_api_access.assert_called_once()
        kwargs = mock_sel.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "browse_create_dir"
        assert kwargs["outcome"] == "allowed"
        assert kwargs["resources"] == data["path"]

    @pytest.mark.asyncio
    async def test_parent_with_a_trailing_separator_resolves_like_a_listing(
        self, tmp_path, mock_sel
    ):
        status, data = await _post({"parent": str(tmp_path) + os.sep, "name": "x"})
        assert status == 201
        assert data["parent"] == os.path.realpath(tmp_path)
        assert (tmp_path / "x").is_dir()

    @pytest.mark.asyncio
    async def test_a_dot_name_is_an_ordinary_folder(self, tmp_path, mock_sel):
        status, _ = await _post({"parent": str(tmp_path), "name": ".worktrees"})
        assert status == 201
        assert (tmp_path / ".worktrees").is_dir()

    @pytest.mark.asyncio
    async def test_an_existing_name_is_a_conflict(self, tmp_path, mock_sel):
        (tmp_path / "taken").mkdir()
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        for name in ("taken", "file.txt"):
            status, data = await _post({"parent": str(tmp_path), "name": name})
            assert status == 409
            assert data["code"] == "already_exists"
        assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "x"

    @pytest.mark.asyncio
    async def test_creation_runs_off_the_event_loop(self, tmp_path, mock_sel):
        loop_thread = threading.get_ident()
        ran_on: list[int] = []

        def spy(parent: str, name: str) -> tuple[str, str]:
            ran_on.append(threading.get_ident())
            return _create_browse_dir_sync(parent, name)

        with patch.object(files_mod, "_create_browse_dir_sync", spy):
            status, _ = await _post({"parent": str(tmp_path), "name": "off-loop"})
        assert status == 201
        assert ran_on and ran_on[0] != loop_thread

    @pytest.mark.asyncio
    async def test_a_platform_that_cannot_pin_creates_by_name(
        self, tmp_path, mock_sel, monkeypatch
    ):
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        status, _ = await _post({"parent": str(tmp_path), "name": "by-name"})
        assert status == 201
        assert (tmp_path / "by-name").is_dir()


class TestRefusals:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name",
        [
            "",
            ".",
            "..",
            "a/b",
            "a\\b",
            "nul\x00byte",
            "tab\tname",
            "line\nbreak",
            " padded ",
        ],
    )
    async def test_a_name_that_is_not_one_plain_component_is_refused(
        self, tmp_path, mock_sel, name
    ):
        status, data = await _post({"parent": str(tmp_path), "name": name})
        assert status == 400
        assert data["code"] == "invalid_name"
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name",
        [
            "x" * 256,
            # 86 CJK characters: within the field's 255-unit cap, past 255 UTF-8 bytes.
            "文" * 86,
        ],
    )
    async def test_a_name_past_the_byte_limit_is_too_long(self, tmp_path, mock_sel, name):
        status, data = await _post({"parent": str(tmp_path), "name": name})
        assert status == 400
        assert data["code"] == "name_too_long"
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_cjk_name_at_the_byte_limit_is_created(self, tmp_path, mock_sel):
        status, data = await _post({"parent": str(tmp_path), "name": "文" * 85})
        assert status == 201
        assert (tmp_path / ("文" * 85)).is_dir()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body", [{"name": "x"}, {"parent": "", "name": "x"}, {"parent": 3, "name": "x"}]
    )
    async def test_a_create_must_name_its_parent(self, mock_sel, body):
        status, data = await _post(body)
        assert status == 400
        assert data["code"] == "parent_required"

    @pytest.mark.asyncio
    async def test_a_non_string_name_is_refused(self, tmp_path, mock_sel):
        status, data = await _post({"parent": str(tmp_path), "name": ["x"]})
        assert status == 400
        assert data["code"] == "invalid_name"

    @pytest.mark.asyncio
    async def test_a_parent_that_is_not_a_directory_is_refused(self, tmp_path, mock_sel):
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        for parent in (tmp_path / "file.txt", tmp_path / "missing"):
            status, data = await _post({"parent": str(parent), "name": "x"})
            assert status == 400
            assert data["code"] == "not_a_directory"
        assert not (tmp_path / "missing").exists()

    @pytest.mark.asyncio
    async def test_a_sensitive_parent_is_refused_and_audited(self, tmp_path, mock_sel, monkeypatch):
        real = os.path.realpath(tmp_path)
        monkeypatch.setattr(files_mod, "is_sensitive_path", lambda p: p == real)
        status, data = await _post({"parent": str(tmp_path), "name": "x"})
        assert status == 403
        assert data["code"] == "access_denied"
        assert list(tmp_path.iterdir()) == []
        assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_a_sensitive_new_path_is_refused_before_it_exists(
        self, tmp_path, mock_sel, monkeypatch
    ):
        monkeypatch.setattr(
            files_mod, "is_sensitive_canonical_path", lambda p: os.path.basename(p) == ".ssh"
        )
        status, data = await _post({"parent": str(tmp_path), "name": ".ssh"})
        assert status == 403
        assert data["code"] == "access_denied"
        assert not (tmp_path / ".ssh").exists()

    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused_before_the_body_is_read(self, tmp_path, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                ROUTE, data=b"{not json", headers={"X-Test-User": "U_ALLOWED_TEAMMATE"}
            )
            assert resp.status == 403
            assert "owner_only" in await resp.text()
        status, _ = await _post(
            {"parent": str(tmp_path), "name": "x"}, headers={"X-Test-User": "U_ALLOWED_TEAMMATE"}
        )
        assert status == 403
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32" or _IS_ROOT, reason="needs POSIX permission bits")
    async def test_a_parent_without_write_permission_says_so(self, tmp_path, mock_sel):
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        try:
            status, data = await _post({"parent": str(locked), "name": "x"})
        finally:
            locked.chmod(0o755)
        assert status == 403
        assert data["code"] == "permission_denied"
        assert not (locked / "x").exists()


@requires_symlinks
@pytest.mark.skipif(not pinned_fs.supports_pinned_walk(), reason="needs descriptor-pinned walks")
def test_a_parent_swapped_for_a_link_after_the_check_is_refused(tmp_path):
    """The route resolves the parent first and creates later; a component replaced
    by a link in between must not steer the create into the link's target."""
    checked = tmp_path / "project"
    checked.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    resolved = os.path.realpath(checked)
    checked.rmdir()
    checked.symlink_to(elsewhere, target_is_directory=True)
    outcome, target = _create_browse_dir_sync(resolved, "new")
    assert outcome == "parent_changed"
    assert target == os.path.join(resolved, "new")
    assert list(elsewhere.iterdir()) == []


class TestNameRules:
    @pytest.mark.parametrize(
        "name", ["project", "my project", ".hidden", "v1.2", "ünïcode", "x" * 255]
    )
    def test_plain_names_are_accepted(self, name):
        assert _browse_dir_name_is_valid(name)

    def test_the_byte_limit_counts_utf8_bytes(self):
        assert _browse_dir_name_is_valid("é" * 127)
        assert not _browse_dir_name_is_valid("é" * 128)

    def test_a_lone_surrogate_is_refused(self):
        assert not _browse_dir_name_is_valid("bad\ud800")

    @pytest.mark.parametrize("name", ["del\x7fname", "nel\x85name", "csi\x9bname"])
    def test_every_control_character_is_refused(self, name):
        assert not _browse_dir_name_is_valid(name)

    @pytest.mark.parametrize(
        "name",
        ["a:b", "q?", "CON", "con.txt", "LPT1", "dot.", "pipe|", "COM0", "lpt¹", "CONIN$"],
    )
    def test_windows_refuses_its_own_reserved_names(self, name):
        with patch.object(platform_compat, "IS_WINDOWS", True):
            assert not _browse_dir_name_is_valid(name)

    @pytest.mark.parametrize("name", ["a:b", "CON", "dot."])
    def test_windows_rules_do_not_apply_elsewhere(self, name):
        with patch.object(platform_compat, "IS_WINDOWS", False):
            assert _browse_dir_name_is_valid(name)


def test_the_route_is_registered_on_the_real_table():
    app = web.Application()
    taskrunner.register(app)
    routes = {
        (route.method, route.resource.canonical): route.handler
        for route in app.router.routes()
        if route.resource is not None
    }
    assert routes[("POST", ROUTE)] is handlers.api_browse_create_dir
