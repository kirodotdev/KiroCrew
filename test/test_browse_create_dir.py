"""Tests for ``POST /api/browse-dirs/create``: the project picker's New folder control.

The route makes one folder in the directory the picker's Browse pane shows. It is
owner-only, takes exactly one folder name, and refuses a name that already exists,
one with a slash, a protected location and a parent it cannot write to, creating
nothing in each case.
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

from conftest import make_dir_link
from kiro_crew import platform_compat
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import api_browse_create_dir
from kiro_crew.dashboard.handlers import files as files_mod
from kiro_crew.dashboard.handlers.files import _create_browse_dir_sync
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


@pytest.mark.asyncio
async def test_creates_the_folder_and_answers_its_path(tmp_path, mock_sel):
    status, data = await _post({"parent": str(tmp_path), "name": "new-project"})
    assert status == 201
    created = tmp_path / "new-project"
    assert created.is_dir()
    assert data == {"path": os.path.join(os.path.realpath(tmp_path), "new-project")}
    kwargs = mock_sel.log_api_access.call_args.kwargs
    assert (kwargs["operation"], kwargs["outcome"]) == ("browse_create_dir", "allowed")


@pytest.mark.asyncio
async def test_creation_runs_off_the_event_loop(tmp_path, mock_sel):
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
async def test_an_existing_name_is_a_conflict_and_left_alone(tmp_path, mock_sel):
    (tmp_path / "taken").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    for name in ("taken", "file.txt"):
        status, data = await _post({"parent": str(tmp_path), "name": name})
        assert (status, data["code"]) == (409, "already_exists")
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "x"


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
        3,
        "x" * 300,
    ],
)
async def test_a_name_that_is_not_one_folder_name_is_refused(tmp_path, mock_sel, name):
    status, data = await _post({"parent": str(tmp_path), "name": name})
    assert (status, data["code"]) == (400, "invalid_name")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"name": "x"}, {"parent": " ", "name": "x"}, {"parent": 3}])
async def test_a_create_must_name_its_parent(mock_sel, body):
    status, data = await _post(body)
    assert (status, data["code"]) == (400, "parent_required")


@pytest.mark.asyncio
async def test_a_missing_or_file_parent_is_not_found(tmp_path, mock_sel):
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    for parent in (tmp_path / "file.txt", tmp_path / "missing"):
        status, data = await _post({"parent": str(parent), "name": "x"})
        assert (status, data["code"]) == (404, "not_found")
    assert not (tmp_path / "missing").exists()


@pytest.mark.asyncio
async def test_a_protected_new_path_is_refused_before_it_exists(tmp_path, mock_sel, monkeypatch):
    monkeypatch.setattr(
        files_mod, "is_sensitive_canonical_path", lambda p: os.path.basename(p) == ".aws"
    )
    status, data = await _post({"parent": str(tmp_path), "name": ".aws"})
    assert (status, data["code"]) == (403, "access_denied")
    assert not (tmp_path / ".aws").exists()
    assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


def test_a_folder_inside_a_protected_directory_is_refused(tmp_path):
    """The real floor, unpatched: a protected directory protects what is under it,
    so one check on the new path also refuses a protected parent."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
        for parent, name in ((home, ".aws"), (home / ".ssh", "keys")):
            assert _create_browse_dir_sync(str(parent), name)[0] == "sensitive"
    assert sorted(p.name for p in home.iterdir()) == [".ssh"]
    assert list((home / ".ssh").iterdir()) == []


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_before_the_body_is_read(tmp_path, mock_sel):
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
async def test_a_parent_without_write_permission_says_so(tmp_path, mock_sel):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        status, data = await _post({"parent": str(locked), "name": "x"})
    finally:
        locked.chmod(0o755)
    assert (status, data["code"]) == (403, "permission_denied")
    assert not (locked / "x").exists()


def test_the_route_is_registered_on_the_real_table():
    app = web.Application()
    taskrunner.register(app)
    routes = {
        (route.method, route.resource.canonical): route.handler
        for route in app.router.routes()
        if route.resource is not None
    }
    assert routes[("POST", ROUTE)] is handlers.api_browse_create_dir


@pytest.mark.skipif(
    not platform_compat.IS_WINDOWS, reason="a drive-qualified name is a Windows path"
)
def test_a_drive_qualified_name_cannot_leave_the_parent(tmp_path):
    """``Z:x`` joins to a path on another drive, outside the parent: refused, not made."""
    drive = "Y" if str(tmp_path).upper().startswith("Z") else "Z"
    assert _create_browse_dir_sync(str(tmp_path), f"{drive}:x")[0] == "invalid_name"
    assert list(tmp_path.iterdir()) == []


def test_a_parent_swapped_for_a_link_after_the_check_creates_nothing(tmp_path, monkeypatch):
    # The parent passes the checks, then is renamed and replaced by a link to a
    # protected place before the create: the create must not follow the link.
    work = tmp_path / "work"
    work.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    real_check = files_mod.is_sensitive_canonical_path

    def check_then_swap(path):
        verdict = real_check(path)
        work.rename(tmp_path / "moved")
        make_dir_link(work, protected)
        return verdict

    monkeypatch.setattr(files_mod, "is_sensitive_canonical_path", check_then_swap)
    assert _create_browse_dir_sync(str(work), "new-project")[0] == "parent_changed"
    assert not (protected / "new-project").exists()
