"""Organization skills synced from the Kiro profile reach every skill surface.

kiro-cli syncs a Kiro profile's organization skills to
``~/.kiro/cloud-cache/resolved/config/organization/skills``. The Skills tab, the
``$`` picker and ``skill_search`` all read the dashboard catalog, and a ``$name``
token expands through ``SkillsLoader``, so both must see that folder. kiro-cli
overwrites it on each sync, so every write is refused.

Tests pin $HOME to tmp_path so the real filesystem is never touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from skill_browser_helpers import (  # noqa: F401 -- the fixture is requested by name
    _make_app,
    _write_skill,
    fake_home_fixture,
)

from kiro_crew.config.paths import kiro_organization_dir

_ORG_SKILLS = (".kiro", "cloud-cache", "resolved", "config", "organization", "skills")


def _state():
    from kiro_crew.skills import SkillsLoader

    # A real loader, not a MagicMock: ``_get_skills`` would otherwise hand back
    # an auto-vivified mock and the listing would 500 serializing it.
    state = MagicMock(_slots={}, context_builder=None)
    state._standalone_skills = SkillsLoader(install_builtins=False)
    return state


def test_organization_dir_is_under_the_kiro_home(fake_home):
    assert kiro_organization_dir() == fake_home.joinpath(*_ORG_SKILLS).parent


def test_kiro_skill_roots_include_organization_only_when_synced(fake_home):
    from kiro_crew.dashboard.handlers._shared_skill_listing import _kiro_skill_roots

    assert "kiro-organization" not in dict(_kiro_skill_roots())
    fake_home.joinpath(*_ORG_SKILLS).mkdir(parents=True)
    assert dict(_kiro_skill_roots())["kiro-organization"] == fake_home.joinpath(*_ORG_SKILLS)


@pytest.mark.asyncio
async def test_listed_once_with_organization_source(fake_home):
    _write_skill(fake_home.joinpath(*_ORG_SKILLS), "code-review", description="org review")

    async with TestClient(TestServer(_make_app(_state()))) as client:
        resp = await client.get("/api/skills")
        assert resp.status == 200
        rows = [s for s in await resp.json() if s["name"] == "code-review"]

    # The loader and the open-standard listing both reach the file; one row is
    # kept, under the organization key, with the loader's key as an alias.
    assert [(r["key"], r["source"]) for r in rows] == [
        ("kiro-organization/code-review", "kiro-organization")
    ]
    assert "code-review" in rows[0]["aliases"]


def test_dollar_token_expands_an_organization_skill(fake_home):
    from kiro_crew.skills import SkillsLoader

    _write_skill(fake_home.joinpath(*_ORG_SKILLS), "code-review", body="ORG REVIEW BODY")
    loader = SkillsLoader(install_builtins=False)

    hits = loader.resolve_dollar_skills("please run $code-review now")

    assert [h[0] for h in hits] == ["code-review"]
    assert "ORG REVIEW BODY" in hits[0][2]


def test_a_local_skill_of_the_same_name_wins(fake_home):
    from kiro_crew.skills import SkillsLoader

    _write_skill(fake_home.joinpath(*_ORG_SKILLS), "code-review", body="ORG BODY")
    _write_skill(fake_home / ".kiro" / "crew" / "skills", "code-review", body="LOCAL BODY")
    loader = SkillsLoader(install_builtins=False)

    hits = loader.resolve_dollar_skills("$code-review")

    assert "LOCAL BODY" in hits[0][2]


@pytest.mark.asyncio
async def test_tree_browser_reads_it(fake_home):
    _write_skill(fake_home.joinpath(*_ORG_SKILLS), "code-review")

    async with TestClient(TestServer(_make_app(_state()))) as client:
        resp = await client.get("/api/skills/kiro-organization/code-review/-/tree")
        assert resp.status == 200
        assert any(e["path"] == "SKILL.md" for e in (await resp.json())["entries"])


@pytest.mark.asyncio
async def test_writes_are_refused(fake_home):
    from kiro_crew.dashboard.handlers import api_skill_detail, api_skills_create

    skill_md = _write_skill(fake_home.joinpath(*_ORG_SKILLS), "code-review") / "SKILL.md"
    before = skill_md.read_text()
    app = web.Application()
    app["state"] = _state()
    app.router.add_post("/api/skills", api_skills_create)
    app.router.add_put("/api/skills/{name:.+}", api_skill_detail)
    app.router.add_delete("/api/skills/{name:.+}", api_skill_detail)

    async with TestClient(TestServer(app)) as client:
        put = await client.put("/api/skills/kiro-organization/code-review", json={"content": "x"})
        delete = await client.delete("/api/skills/kiro-organization/code-review")
        create = await client.post(
            "/api/skills", json={"name": "kiro-organization/new", "content": "x"}
        )

    assert put.status >= 400 and delete.status >= 400 and create.status >= 400
    assert skill_md.read_text() == before
    assert not fake_home.joinpath(*_ORG_SKILLS, "new").exists()
