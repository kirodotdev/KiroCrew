"""Two package roots bundling one relative path: each copy keeps its own key.

``package/<rel>`` cannot say which root it means, so the resolver refuses it. The
qualified ``package/<qualifier>:<rel>`` key names one root, and the agent catalog
walk, the ``/api/skills`` rows and the resolver all hand out and accept that same
key for each copy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.dashboard.handlers._shared as _shared
from kiro_crew.platform.defaults import DefaultMcpToolingProvider
from kiro_crew.testing.links import make_dir_link


class _FakeState:
    def __init__(self):
        self._slots = {}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    from kiro_crew.platform.context import current_context

    current_context()
    data_home = tmp_path / "kirocrew_skills"
    data_home.mkdir()
    _floor_monkeypatch.setattr(_shared, "skills_dir", lambda: data_home)
    _floor_monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path)
    with patch.object(_shared.KiroCrewConfig, "load", side_effect=FileNotFoundError):
        yield


def _two_roots_sharing(tmp_path, monkeypatch, rel="shared-skill"):
    roots = [tmp_path / "p1" / "skills", tmp_path / "p2" / "skills"]
    for root in roots:
        (root / rel).mkdir(parents=True)
        (root / rel / "SKILL.md").write_text(f"# from {root.parent.name}", encoding="utf-8")
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: list(roots))
    return roots


def test_a_qualified_key_resolves_to_exactly_its_own_root(tmp_path, monkeypatch):
    root_a, root_b = _two_roots_sharing(tmp_path, monkeypatch)
    qa = _shared.package_root_qualifier(root_a)
    qb = _shared.package_root_qualifier(root_b)

    assert qa != qb
    assert _shared._resolve_package_skill_path("shared-skill") is None
    assert _shared._resolve_package_skill_path(f"{qa}:shared-skill") == (
        root_a / "shared-skill" / "SKILL.md"
    )
    assert (
        _shared._resolve_skill_root(f"package/{qb}:shared-skill", _FakeState())
        == (root_b / "shared-skill").resolve()
    )


def test_a_qualifier_naming_no_package_root_resolves_nothing(tmp_path, monkeypatch):
    _two_roots_sharing(tmp_path, monkeypatch)

    assert _shared._resolve_package_skill_path(f"{'0' * 16}:shared-skill") is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows forbids ':' in directory names")
def test_a_directory_named_with_a_colon_still_resolves_unqualified(tmp_path, monkeypatch):
    root = tmp_path / "pkg"
    (root / "a:b").mkdir(parents=True)
    (root / "a:b" / "SKILL.md").write_text("# colon", encoding="utf-8")
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [root])

    assert _shared._resolve_package_skill_path("a:b") == root / "a:b" / "SKILL.md"


def test_the_catalog_offers_one_key_per_copy_and_each_resolves_to_it(tmp_path, monkeypatch):
    root_a, root_b = _two_roots_sharing(tmp_path, monkeypatch)
    (root_a / "only-a").mkdir()
    (root_a / "only-a" / "SKILL.md").write_text("# a", encoding="utf-8")

    catalog = _shared.enumerate_skill_catalog(_FakeState())
    package_keys = {k: v for k, v in catalog.items() if k.startswith("package/")}

    assert "package/shared-skill" not in package_keys
    assert package_keys["package/only-a"] == root_a / "only-a" / "SKILL.md"
    qualified = [k for k in package_keys if k.endswith(":shared-skill")]
    assert {package_keys[k].resolve() for k in qualified} == {
        (root_a / "shared-skill" / "SKILL.md").resolve(),
        (root_b / "shared-skill" / "SKILL.md").resolve(),
    }
    for key, skill_md in package_keys.items():
        assert _shared._resolve_skill_root(key, _FakeState()) == skill_md.parent.resolve()


def test_a_root_and_its_alias_are_one_copy_not_a_collision(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / "shared-skill").mkdir(parents=True)
    (real / "shared-skill" / "SKILL.md").write_text("# one", encoding="utf-8")
    alias = tmp_path / "alias"
    make_dir_link(alias, real)
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [real, alias])

    package_keys = [
        k for k in _shared.enumerate_skill_catalog(_FakeState()) if k.startswith("package/")
    ]

    assert package_keys == ["package/shared-skill"]


def test_an_agent_can_map_each_copy_and_the_mapping_reads_back(tmp_path, monkeypatch):
    _two_roots_sharing(tmp_path, monkeypatch)
    keys = sorted(
        k for k in _shared.enumerate_skill_catalog(_FakeState()) if k.endswith(":shared-skill")
    )
    agent_path = tmp_path / "agent.json"
    data: dict = {}

    _applied, unknown, uris, _snap = _shared.apply_skill_mapping(
        data, agent_path, _FakeState(), keys
    )

    assert unknown == []
    assert len(set(uris)) == 2
    assert sorted(_shared.agent_skill_keys(data, agent_path, _FakeState())) == keys


def test_listed_rows_carry_the_key_the_catalog_offers(tmp_path, monkeypatch):
    root_a, root_b = _two_roots_sharing(tmp_path, monkeypatch)
    rows = [
        {
            "key": "package/shared-skill",
            "name": "shared-skill",
            "source": "package",
            "path": str(root / "shared-skill" / "SKILL.md"),
        }
        for root in (root_a, root_b)
    ]
    loader = SimpleNamespace(list_skills=lambda: [], catalog_project_skills=lambda _p: [])
    monkeypatch.setattr(_shared, "list_kiro_skills", lambda: [])

    listed = _shared.collect_skills_blocking(loader, rows, project_dir=None)

    catalog = _shared.enumerate_skill_catalog(_FakeState())
    by_path = {str(v): k for k, v in catalog.items()}
    package_rows = [r for r in listed if r["source"] == "package"]
    assert len({r["key"] for r in package_rows}) == 2
    for row in package_rows:
        assert row["key"] == by_path[row["path"]]


def test_nested_copies_listed_by_leaf_key_carry_the_key_the_catalog_offers(tmp_path, monkeypatch):
    roots = _two_roots_sharing(tmp_path, monkeypatch, rel="Pkg/shared-skill")
    rows = [
        {
            "key": "package/shared-skill",
            "name": "shared-skill",
            "source": "package",
            "path": str(root / "Pkg" / "shared-skill" / "SKILL.md"),
        }
        for root in roots
    ]
    loader = SimpleNamespace(list_skills=lambda: [], catalog_project_skills=lambda _p: [])
    monkeypatch.setattr(_shared, "list_kiro_skills", lambda: [])

    listed = _shared.collect_skills_blocking(loader, rows, project_dir=None)

    catalog = _shared.enumerate_skill_catalog(_FakeState())
    package_rows = [r for r in listed if r["source"] == "package"]
    assert len({r["key"] for r in package_rows}) == 2
    for row in package_rows:
        assert row["key"] in catalog
        assert catalog[row["key"]].resolve() == Path(row["path"]).resolve()


def test_a_core_root_keeps_a_package_key_it_claims_first(tmp_path, monkeypatch):
    core_copy = tmp_path / "kirocrew_skills" / "package" / "example" / "SKILL.md"
    core_copy.parent.mkdir(parents=True)
    core_copy.write_text("# core", encoding="utf-8")
    edition = tmp_path / "edition"
    (edition / "example").mkdir(parents=True)
    (edition / "example" / "SKILL.md").write_text("# edition", encoding="utf-8")
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [edition])

    assert _shared.enumerate_skill_catalog(_FakeState())["package/example"] == core_copy
    assert "package/example" not in _shared.package_skill_catalog()


def test_copies_listed_under_a_rel_key_and_a_leaf_key_can_both_be_mapped(tmp_path, monkeypatch):
    root_a, root_b = _two_roots_sharing(tmp_path, monkeypatch, rel="Pkg/shared-skill")
    rows = [
        {
            "key": key,
            "name": "shared-skill",
            "source": "package",
            "path": str(root / "Pkg" / "shared-skill" / "SKILL.md"),
        }
        for key, root in (("package/Pkg/shared-skill", root_a), ("package/shared-skill", root_b))
    ]
    loader = SimpleNamespace(list_skills=lambda: [], catalog_project_skills=lambda _p: [])
    monkeypatch.setattr(_shared, "list_kiro_skills", lambda: [])

    listed = [
        r for r in _shared.collect_skills_blocking(loader, rows, None) if r["source"] == "package"
    ]

    catalog = _shared.enumerate_skill_catalog(_FakeState())
    assert len(listed) == 2
    for row in listed:
        assert catalog[row["key"]].resolve() == Path(row["path"]).resolve()
    keys = sorted(r["key"] for r in listed)
    _applied, unknown, uris, _snap = _shared.apply_skill_mapping(
        {}, tmp_path / "agent.json", _FakeState(), keys
    )
    assert unknown == []
    assert len(set(uris)) == 2


def _wire_detail(monkeypatch, root_a, root_b):
    from kiro_crew.dashboard.handlers import prompts

    loader = MagicMock()
    loader.load_skill = lambda _name: None
    monkeypatch.setattr(prompts, "_get_skills", lambda _state: loader)
    mgr = MagicMock()
    mgr.available = lambda: True

    async def _list_skills():
        return [
            {"key": "package/shared-skill", "name": "shared-skill", "path": str(r)}
            for r in (root_a / "shared-skill" / "SKILL.md", root_b / "shared-skill" / "SKILL.md")
        ]

    mgr.list_skills = _list_skills
    monkeypatch.setattr(prompts, "_capability_manager", lambda: mgr)


def _detail_request(name):
    req = MagicMock()
    req.method = "GET"
    req.match_info = {"name": name}
    req.app = {"state": _FakeState()}
    req.query = {}
    req.headers = {}
    return req


@pytest.mark.asyncio
async def test_the_detail_view_reads_a_qualified_key(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import api_skill_detail

    root_a, root_b = _two_roots_sharing(tmp_path, monkeypatch)
    _wire_detail(monkeypatch, root_a, root_b)
    name = f"package/{_shared.package_root_qualifier(root_b)}:shared-skill"

    resp = await api_skill_detail(_detail_request(name))

    assert resp.status == 200
    assert json.loads(resp.body)["content"] == "# from p2"


@pytest.mark.asyncio
async def test_the_detail_view_refuses_an_unqualified_colliding_key(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import api_skill_detail

    root_a, root_b = _two_roots_sharing(tmp_path, monkeypatch)
    _wire_detail(monkeypatch, root_a, root_b)

    resp = await api_skill_detail(_detail_request("package/shared-skill"))

    assert resp.status == 404


@pytest.mark.asyncio
async def test_each_rekeyed_nested_leaf_row_opens_its_own_copy(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import api_skill_detail, prompts

    roots = [tmp_path / "p1" / "skills", tmp_path / "p2" / "skills"]
    rows = []
    for root, pkg in zip(roots, ("Pkg1", "Pkg2")):
        (root / pkg / "x").mkdir(parents=True)
        (root / pkg / "x" / "SKILL.md").write_text(f"# {pkg}", encoding="utf-8")
        rows.append(
            {
                "key": "package/x",
                "name": "x",
                "source": "package",
                "path": str(root / pkg / "x" / "SKILL.md"),
            }
        )
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: list(roots))
    monkeypatch.setattr(_shared, "list_kiro_skills", lambda: [])
    listing_loader = SimpleNamespace(list_skills=lambda: [], catalog_project_skills=lambda _p: [])
    listed = [
        r
        for r in _shared.collect_skills_blocking(listing_loader, [dict(r) for r in rows], None)
        if r["source"] == "package"
    ]
    loader = MagicMock()
    loader.load_skill = lambda _name: None
    monkeypatch.setattr(prompts, "_get_skills", lambda _state: loader)
    mgr = MagicMock()
    mgr.available = lambda: True

    async def _list_skills():
        return rows

    mgr.list_skills = _list_skills
    monkeypatch.setattr(prompts, "_capability_manager", lambda: mgr)

    assert sorted(r["key"] for r in listed) == ["package/Pkg1/x", "package/Pkg2/x"]
    for row in listed:
        resp = await api_skill_detail(_detail_request(row["key"]))
        assert resp.status == 200
        assert json.loads(resp.body)["content"] == Path(row["path"]).read_text(encoding="utf-8")


def _alias_rows(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / "shared-skill").mkdir(parents=True)
    (real / "shared-skill" / "SKILL.md").write_text("# one", encoding="utf-8")
    alias = tmp_path / "alias"
    make_dir_link(alias, real)
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [real, alias])
    return [
        {
            "key": "package/shared-skill",
            "name": "shared-skill",
            "source": "package",
            "path": str(root / "shared-skill" / "SKILL.md"),
        }
        for root in (real, alias)
    ]


@pytest.mark.asyncio
async def test_the_detail_view_serves_one_file_listed_through_an_alias(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import api_skill_detail, prompts

    rows = _alias_rows(tmp_path, monkeypatch)
    loader = MagicMock()
    loader.load_skill = lambda _name: None
    monkeypatch.setattr(prompts, "_get_skills", lambda _state: loader)
    mgr = MagicMock()
    mgr.available = lambda: True

    async def _list_skills():
        return rows

    mgr.list_skills = _list_skills
    monkeypatch.setattr(prompts, "_capability_manager", lambda: mgr)

    resp = await api_skill_detail(_detail_request("package/shared-skill"))

    assert resp.status == 200
    assert json.loads(resp.body)["content"] == "# one"


def test_an_aliased_listing_keeps_the_key_the_catalog_offers(tmp_path, monkeypatch):
    rows = _alias_rows(tmp_path, monkeypatch)
    loader = SimpleNamespace(list_skills=lambda: [], catalog_project_skills=lambda _p: [])
    monkeypatch.setattr(_shared, "list_kiro_skills", lambda: [])

    listed = _shared.collect_skills_blocking(loader, rows, project_dir=None)

    catalog = _shared.enumerate_skill_catalog(_FakeState())
    keys = {r["key"] for r in listed if r["source"] == "package"}
    assert keys == {"package/shared-skill"}
    assert keys <= set(catalog)


def test_hardlinked_copies_list_a_key_the_catalog_offers(tmp_path, monkeypatch):
    import os

    roots = [tmp_path / "p1" / "skills", tmp_path / "p2" / "skills"]
    for root in roots:
        (root / "shared-skill").mkdir(parents=True)
    first = roots[0] / "shared-skill" / "SKILL.md"
    first.write_text("# shared", encoding="utf-8")
    try:
        os.link(first, roots[1] / "shared-skill" / "SKILL.md")
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - host capability
        pytest.skip(f"filesystem does not support hardlinks: {exc}")
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: list(roots))
    rows = [
        {
            "key": "package/shared-skill",
            "name": "shared-skill",
            "source": "package",
            "path": str(root / "shared-skill" / "SKILL.md"),
        }
        for root in roots
    ]
    loader = SimpleNamespace(list_skills=lambda: [], catalog_project_skills=lambda _p: [])
    monkeypatch.setattr(_shared, "list_kiro_skills", lambda: [])

    listed = _shared.collect_skills_blocking(loader, rows, project_dir=None)

    catalog = _shared.enumerate_skill_catalog(_FakeState())
    package_rows = [r for r in listed if r["source"] == "package"]
    assert len(package_rows) == 1
    assert package_rows[0]["key"] in catalog
    assert set(package_rows[0]["aliases"]) <= set(catalog)
