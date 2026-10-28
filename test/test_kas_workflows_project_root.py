"""A Kiro Workflows project directory is confined to an allowed root before any filesystem use.

The two entry points are the dashboard handler (``_project_dir``, request data)
and the router (``_project``, what the handler hands it). Each must refuse a
directory outside every root even when it exists, must not be led out of a root
by a symlink, and must not stat a path outside every root at all.
"""

from __future__ import annotations

import os

import pytest

import kiro_crew.dashboard.handlers.kas_workflows as kas_handlers
from kiro_crew.kas_workflows import project_root
from kiro_crew.kas_workflows import router as router_mod
from kiro_crew.kas_workflows.router import KasWorkflowError


@pytest.fixture
def roots(tmp_path, _floor_monkeypatch):
    """home = tmp_path/home, no recent projects, no gateway project directory."""
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _floor_monkeypatch.setattr(project_root, "_home", lambda: str(home))
    _floor_monkeypatch.setattr(project_root, "_recent_project_dirs", lambda: [])
    _floor_monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    return home, outside


def test_handler_accepts_a_project_under_home(roots):
    home, _ = roots
    assert kas_handlers._project_dir(str(home / "proj")) == os.path.realpath(home / "proj")


def test_handler_refuses_an_existing_directory_outside_every_root(roots):
    _, outside = roots
    assert kas_handlers._project_dir(str(outside)) is None


def test_handler_refuses_a_dotdot_escape_from_home(roots):
    home, outside = roots
    assert kas_handlers._project_dir(str(home / ".." / outside.name)) is None


def test_handler_refuses_a_symlink_in_home_that_leads_outside(roots):
    home, outside = roots
    (home / "link").symlink_to(outside, target_is_directory=True)
    assert kas_handlers._project_dir(str(home / "link")) is None


def test_handler_refuses_a_sibling_that_only_shares_the_home_prefix(roots, tmp_path):
    home, _ = roots
    sibling = tmp_path / (home.name + "-other")
    sibling.mkdir()
    assert kas_handlers._project_dir(str(sibling)) is None


def test_handler_refuses_relative_and_non_string_values(roots):
    assert kas_handlers._project_dir("proj") is None
    assert kas_handlers._project_dir(None) is None
    assert kas_handlers._project_dir(["/"]) is None


def test_handler_accepts_a_recent_project_outside_home(roots, _floor_monkeypatch):
    _, outside = roots
    _floor_monkeypatch.setattr(project_root, "_recent_project_dirs", lambda: [str(outside)])
    assert kas_handlers._project_dir(str(outside)) == os.path.realpath(outside)


def test_handler_accepts_a_server_held_chat_project_outside_home(roots):
    _, outside = roots
    assert kas_handlers._project_dir(str(outside), server_held=str(outside)) == os.path.realpath(
        outside
    )


def test_nothing_outside_every_root_is_stat_ed(roots, _floor_monkeypatch):
    _, outside = roots
    touched: list[str] = []
    real_isdir, real_realpath = os.path.isdir, os.path.realpath

    def isdir(path):
        touched.append(str(path))
        return real_isdir(path)

    def realpath(path, *args, **kwargs):
        touched.append(str(path))
        return real_realpath(path, *args, **kwargs)

    _floor_monkeypatch.setattr(project_root.os.path, "isdir", isdir)
    roots_list = project_root.allowed_project_roots()
    _floor_monkeypatch.setattr(project_root.os.path, "realpath", realpath)
    assert project_root.confine_project_dir(str(outside / "x"), roots_list) is None
    assert not any(t.startswith(str(outside)) for t in touched)


def test_router_refuses_a_directory_outside_every_root(roots):
    _, outside = roots
    with pytest.raises(KasWorkflowError):
        router_mod._project(str(outside))


def test_router_refuses_a_symlink_in_home_that_leads_outside(roots):
    home, outside = roots
    (home / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(KasWorkflowError):
        router_mod._project(str(home / "link"))


def test_router_accepts_a_project_under_home(roots):
    home, _ = roots
    assert router_mod._project(str(home / "proj")) == os.path.realpath(home / "proj")


def test_a_recent_project_outside_home_is_a_root(roots, _floor_monkeypatch):
    _, outside = roots
    _floor_monkeypatch.setattr(project_root, "_recent_project_dirs", lambda: [str(outside)])
    assert router_mod._project(str(outside)) == os.path.realpath(outside)


def test_a_sibling_that_only_shares_the_home_prefix_is_refused(roots, tmp_path):
    home, _ = roots
    sibling = tmp_path / (home.name + "-other")
    sibling.mkdir()
    with pytest.raises(KasWorkflowError):
        router_mod._project(str(sibling))


def test_router_refuses_a_dotdot_escape_from_home(roots):
    home, outside = roots
    with pytest.raises(KasWorkflowError):
        router_mod._project(str(home / ".." / outside.name))
