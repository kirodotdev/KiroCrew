"""A Kiro Workflows project directory is confined to an allowed root before any filesystem use.

The router's ``_project`` takes the directory a caller names. It must refuse a
directory outside every root even when it exists, must not be led out of a root
by a symlink, and must not stat a path outside every root at all.
"""

from __future__ import annotations

import os

import pytest

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


def test_router_refuses_a_directory_outside_every_root(roots):
    _, outside = roots
    with pytest.raises(KasWorkflowError):
        router_mod._project(str(outside))


def test_router_refuses_a_dotdot_escape_from_home(roots):
    home, outside = roots
    with pytest.raises(KasWorkflowError):
        router_mod._project(str(home / ".." / outside.name))


def test_router_refuses_a_symlink_in_home_that_leads_outside(roots):
    home, outside = roots
    (home / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(KasWorkflowError):
        router_mod._project(str(home / "link"))


def test_router_accepts_a_project_under_home(roots):
    home, _ = roots
    assert router_mod._project(str(home / "proj")) == os.path.realpath(home / "proj")


def test_the_gateway_project_and_recent_projects_are_roots_and_relative_ones_are_not(
    roots, tmp_path, _floor_monkeypatch
):
    home, outside = roots
    env_dir = tmp_path / "env-project"
    env_dir.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(env_dir))
    _floor_monkeypatch.setattr(
        project_root, "_recent_project_dirs", lambda: [str(outside), "relative/dir", ""]
    )
    found = project_root.allowed_project_roots(extra=[str(home / "proj")])
    assert os.path.normpath(env_dir) in found
    assert os.path.normpath(outside) in found
    assert os.path.normpath(home / "proj") in found
    assert not any(not os.path.isabs(r) for r in found)


@pytest.mark.parametrize(
    "content, expected",
    [
        ('["/a", "", 3, "/b"]', ["/a", "/b"]),
        ('{"not": "a list"}', []),
        ("not json", []),
        (None, []),
    ],
)
def test_the_recent_projects_file_is_read_defensively(
    tmp_path, _floor_monkeypatch, content, expected
):
    from kiro_crew.config import paths

    _floor_monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    if content is not None:
        (tmp_path / "recent_projects.json").write_text(content, encoding="utf-8")
    assert project_root._recent_project_dirs() == expected


def test_the_recent_projects_read_is_capped(tmp_path, _floor_monkeypatch):
    from kiro_crew.config import paths

    _floor_monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    many = [f"/p{n}" for n in range(project_root._MAX_RECENT_ROOTS + 5)]
    (tmp_path / "recent_projects.json").write_text(str(many).replace("'", '"'), encoding="utf-8")
    assert project_root._recent_project_dirs() == many[: project_root._MAX_RECENT_ROOTS]


@pytest.mark.parametrize("raw", [None, 7, "", "relative/x", "/a\x00b"])
def test_a_directory_that_is_not_an_absolute_path_string_is_refused(roots, raw):
    home, _ = roots
    assert project_root.confine_project_dir(raw, [str(home)]) is None
