"""The featured starting list of the Add Skill / Add Server dialogs.

Instead of an empty "Type at least 2 characters to search" screen, with
``featured=1`` and no query the discover routes return a bundled list,
shaped exactly like search hits, without calling any provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import discovery_featured
from kiro_crew.dashboard.handlers.mcp_discover import _derive_install_name


@pytest.fixture(autouse=True)
def _fresh_cache():
    discovery_featured._load.cache_clear()
    yield
    discovery_featured._load.cache_clear()


def _write(tmp_path: Path, monkeypatch, doc) -> None:
    path = tmp_path / "featured.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(discovery_featured, "_FILE", path)


# ---- the bundled file -------------------------------------------------------


def test_bundled_file_has_rows_for_both_dialogs():
    skills = discovery_featured.featured_entries("skills")
    servers = discovery_featured.featured_entries("mcp_servers")
    assert len(skills) >= 6
    assert len(servers) >= 6
    assert {e["provider"] for e in skills} == {"skillsh"}
    assert {e["provider"] for e in servers} == {"official"}


def test_bundled_ids_are_unique_and_installable_names_do_not_collide():
    skills = discovery_featured.featured_entries("skills")
    servers = discovery_featured.featured_entries("mcp_servers")
    assert len({e["id"] for e in skills}) == len(skills)
    # skills.sh ids are owner/repo/skill
    assert all(e["id"].count("/") == 2 for e in skills)
    # Two featured servers landing under one mcp.json name would 409 the second
    # install and badge both as installed after the first.
    names = [_derive_install_name(e["id"]) for e in servers]
    assert len(set(names)) == len(names)
    # Every row must show a label: title, or a name that is not the generic "mcp".
    assert all(e.get("title") or e["name"] != "mcp" for e in servers)


def test_bundled_file_ships_as_package_data():
    setup_cfg = (Path(__file__).resolve().parents[1] / "setup.cfg").read_text(encoding="utf-8")
    assert "data/discovery_featured.json" in setup_cfg


# ---- the loader -------------------------------------------------------------


def test_malformed_file_yields_no_rows(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "{not json")
    assert discovery_featured.featured_entries("skills") == []


def test_rows_missing_required_fields_or_with_wrong_types_are_dropped(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {
            "skills": [
                {"provider": "p", "id": "a/b/c", "name": "c", "description": 7},
                {"provider": "p", "id": "", "name": "x"},
                {"provider": "p", "name": "no-id"},
                "not a row",
            ]
        },
    )
    rows = discovery_featured.featured_entries("skills")
    assert len(rows) == 1
    assert "description" not in rows[0]  # non-string dropped, reads back as ""
    assert rows[0]["methods"] == []


# ---- /api/skills/-/discover -------------------------------------------------


class _FakeSkillProvider:
    def __init__(self, name="skillsh"):
        self._name = name
        self.search_calls = 0

    @property
    def name(self):
        return self._name

    @property
    def display_name(self):
        return "skills.sh"

    def is_available(self):
        return True

    async def search(self, query, *, limit=20):
        self.search_calls += 1
        return []

    async def fetch_skill_content(self, skill_id):
        return None


async def _skills_client(tmp_path, monkeypatch, provider, installed=()):
    from kiro_crew.dashboard.handlers import discover as mod
    from kiro_crew.skill_providers.base import ProviderRegistry

    registry = ProviderRegistry()
    registry.register(provider)
    monkeypatch.setattr(mod, "_registry", registry)
    skills = MagicMock()
    skills.list_skills.return_value = [{"key": k} for k in installed]
    monkeypatch.setattr(mod, "_get_skills", lambda state: skills)
    sel = MagicMock()
    monkeypatch.setattr(mod, "_sel", lambda: sel)
    app = web.Application()
    app["state"] = MagicMock()
    app.router.add_get("/api/skills/-/discover", mod.api_skills_discover)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, sel


@pytest.mark.asyncio
async def test_skills_featured_returns_bundled_rows_without_searching(tmp_path, monkeypatch):
    provider = _FakeSkillProvider()
    client, sel = await _skills_client(
        tmp_path, monkeypatch, provider, installed=["skillsh/vercel-labs-skills-find-skills"]
    )
    try:
        resp = await client.get("/api/skills/-/discover", params={"q": "", "featured": "1"})
        assert resp.status == 200
        data = await resp.json()
    finally:
        await client.close()
    ids = [r["id"] for r in data["results"]]
    assert ids == [e["id"] for e in discovery_featured.featured_entries("skills")]
    assert provider.search_calls == 0
    assert data["provider_outcomes"] == []
    assert all(r["provider"] == "skillsh" for r in data["results"])
    by_id = {r["id"]: r for r in data["results"]}
    assert by_id["vercel-labs/skills/find-skills"]["installed"] is True
    assert by_id["anthropics/skills/pdf"]["installed"] is False
    sel.log_tool_invocation.assert_not_called()


@pytest.mark.asyncio
async def test_skills_empty_query_without_flag_keeps_old_empty_answer(tmp_path, monkeypatch):
    client, _ = await _skills_client(tmp_path, monkeypatch, _FakeSkillProvider())
    try:
        data = await (await client.get("/api/skills/-/discover", params={"q": ""})).json()
    finally:
        await client.close()
    assert data == {"results": [], "providers": []}


@pytest.mark.asyncio
async def test_skills_featured_hides_rows_of_an_unregistered_provider(tmp_path, monkeypatch):
    # A discovery policy that refuses skills.sh never registers it.
    client, _ = await _skills_client(tmp_path, monkeypatch, _FakeSkillProvider(name="other"))
    try:
        data = await (await client.get("/api/skills/-/discover", params={"featured": "1"})).json()
    finally:
        await client.close()
    assert data["results"] == []


@pytest.mark.asyncio
async def test_skills_flag_is_ignored_when_a_query_is_typed(tmp_path, monkeypatch):
    provider = _FakeSkillProvider()
    client, _ = await _skills_client(tmp_path, monkeypatch, provider)
    try:
        data = await (
            await client.get("/api/skills/-/discover", params={"q": "pdf", "featured": "1"})
        ).json()
    finally:
        await client.close()
    assert provider.search_calls == 1
    assert data["results"] == []


# ---- /api/mcp/discover ------------------------------------------------------


class _FakeMcpProvider:
    def __init__(self, name="official"):
        self._name = name
        self.search_calls = 0

    @property
    def name(self):
        return self._name

    @property
    def display_name(self):
        return "MCP Registry"

    def is_available(self):
        return True

    async def search(self, query, *, limit=20):
        self.search_calls += 1
        return []

    async def fetch_detail(self, server_id):
        return None


async def _mcp_client(monkeypatch, provider, configured=()):
    from kiro_crew.dashboard.handlers import mcp_discover as mod
    from kiro_crew.mcp_providers.base import ProviderRegistry

    registry = ProviderRegistry()
    registry.register(provider)
    monkeypatch.setattr(mod, "_registry", registry)
    monkeypatch.setattr(mod, "_configured_server_names", lambda: set(configured))
    monkeypatch.setattr(mod, "sel", lambda: MagicMock())
    app = web.Application()
    app.router.add_get("/api/mcp/discover", mod.api_mcp_discover)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_mcp_featured_returns_bundled_rows_without_searching(monkeypatch):
    provider = _FakeMcpProvider()
    client = await _mcp_client(monkeypatch, provider, configured=["github-mcp-server"])
    try:
        resp = await client.get("/api/mcp/discover", params={"q": "", "featured": "1"})
        assert resp.status == 200
        data = await resp.json()
    finally:
        await client.close()
    assert [r["id"] for r in data["results"]] == [
        e["id"] for e in discovery_featured.featured_entries("mcp_servers")
    ]
    assert provider.search_calls == 0
    assert data["providers"] == ["official"]
    by_id = {r["id"]: r for r in data["results"]}
    assert by_id["io.github.github/github-mcp-server"]["installed"] is True
    assert by_id["io.github.github/github-mcp-server"]["display_provider"] == "MCP Registry"
    assert by_id["io.github.microsoft/playwright-mcp"]["installed"] is False
    assert by_id["io.github.microsoft/playwright-mcp"]["methods"] == ["npx"]


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "g"}, {"q": "g", "featured": "1"}])
@pytest.mark.asyncio
async def test_mcp_short_query_probe_is_unchanged(monkeypatch, params):
    provider = _FakeMcpProvider()
    client = await _mcp_client(monkeypatch, provider)
    try:
        data = await (await client.get("/api/mcp/discover", params=params)).json()
    finally:
        await client.close()
    assert data == {"results": [], "providers": ["official"], "provider_outcomes": []}
    assert provider.search_calls == 0


@pytest.mark.asyncio
async def test_mcp_featured_hides_rows_of_an_unregistered_provider(monkeypatch):
    client = await _mcp_client(monkeypatch, _FakeMcpProvider(name="capability"))
    try:
        data = await (await client.get("/api/mcp/discover", params={"featured": "1"})).json()
    finally:
        await client.close()
    assert data["results"] == []


class _FeaturingEditionManager:
    """An edition ``CapabilityManager`` whose registry marks some rows ``featured``."""

    def __init__(self, *, fail: bool = False) -> None:
        self.queries: list[str | None] = []
        self._fail = fail

    def available(self):
        return True

    async def registry(self, query=None):
        self.queries.append(query)
        if self._fail:
            raise RuntimeError("edition registry broke")
        return [
            {"id": "y", "title": "Y", "description": "", "installed": False, "featured": False},
            {
                "id": "x-mcp",
                "title": "X",
                "description": "x tools",
                "installed": "yes",
                "featured": True,
            },
            {"id": "z-mcp", "title": "Z", "description": "", "featured": "yes"},
        ]


@pytest.mark.asyncio
async def test_mcp_featured_lists_the_editions_own_featured_packages(monkeypatch):
    # An edition that withholds the official registry admits only its own
    # provider, so none of the bundled rows can show; its marked rows do.
    from kiro_crew.mcp_providers.capability import CapabilityProvider

    manager = _FeaturingEditionManager()
    client = await _mcp_client(monkeypatch, CapabilityProvider(lambda: manager))
    try:
        data = await (await client.get("/api/mcp/discover", params={"featured": "1"})).json()
    finally:
        await client.close()
    assert [r["id"] for r in data["results"]] == ["x-mcp"]
    row = data["results"][0]
    assert row["provider"] == "capability"
    assert row["display_provider"] == "Packages"
    assert row["methods"] == ["capability"]
    assert row["installed"] is True
    assert data["provider_outcomes"] == []
    assert manager.queries == [None]


@pytest.mark.asyncio
async def test_mcp_featured_survives_an_edition_registry_that_fails(monkeypatch):
    from kiro_crew.mcp_providers.capability import CapabilityProvider

    manager = _FeaturingEditionManager(fail=True)
    client = await _mcp_client(monkeypatch, CapabilityProvider(lambda: manager))
    try:
        resp = await client.get("/api/mcp/discover", params={"featured": "1"})
        data = await resp.json()
    finally:
        await client.close()
    assert resp.status == 200
    assert data["results"] == []
