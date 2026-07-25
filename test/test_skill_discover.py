"""Tests for the multi-provider skill discover/install dashboard handlers.

Covers the UX-improvement behaviors added on top of the initial skill
browser:

- install returns 409 with code="exists" when the skill is already
  installed and no overwrite flag is set (I4)
- install with overwrite=true replaces the existing skill (I4)
- install response includes file_count (I1)
- preview returns full SKILL.md content + bundle file manifest (B7)
- search response includes the installs count (B2)

The provider is faked end-to-end so tests stay hermetic — no network.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so SkillsLoader/skills_dir resolve to a sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class FakeProvider:
    """Minimal SkillProvider double with a fixed bundle."""

    def __init__(self, bundle=None, results=None):
        self._bundle = bundle if bundle is not None else [
            ("SKILL.md", "---\nname: fake-skill\ndescription: A fake skill\n---\n# Fake"),
            ("rules/extra.md", "# Extra rules"),
        ]
        self._results = results or []

    @property
    def name(self):
        return "fakeprov"

    @property
    def display_name(self):
        return "Fake Provider"

    def is_available(self):
        return True

    async def search(self, query, *, limit=20):
        return self._results

    async def fetch_skill_content(self, skill_id):
        for path, content in self._bundle:
            if path == "SKILL.md":
                return content
        return None

    async def fetch_skill_bundle(self, skill_id):
        return list(self._bundle)


def _state_with_skills_loader(fake_home: Path):
    from kiro_crew.skills import SkillsLoader

    skills_dir = fake_home / ".kiro" / "crew" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    state = MagicMock(_slots={}, context_builder=None)
    state._standalone_skills = SkillsLoader(
        skills_path=skills_dir, install_builtins=False
    )
    return state, skills_dir


def _make_app(state, provider):
    from kiro_crew.dashboard.handlers import discover as discover_mod
    from kiro_crew.skill_providers.base import ProviderRegistry

    registry = ProviderRegistry()
    registry.register(provider)
    discover_mod._registry = registry  # inject the fake registry

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/skills/-/discover", discover_mod.api_skills_discover)
    app.router.add_get(
        "/api/skills/-/discover/preview", discover_mod.api_skills_discover_preview
    )
    app.router.add_post(
        "/api/skills/-/discover/install", discover_mod.api_skills_discover_install
    )
    return app


@pytest.fixture
def reset_registry():
    """Restore the module-level registry singleton after each test."""
    from kiro_crew.dashboard.handlers import discover as discover_mod

    old = discover_mod._registry
    yield
    discover_mod._registry = old


@pytest.mark.asyncio
class TestDiscoverInstall:
    async def _client(self, fake_home, provider=None):
        provider = provider or FakeProvider()
        state, skills_dir = _state_with_skills_loader(fake_home)
        app = _make_app(state, provider)
        client = TestClient(TestServer(app))
        await client.start_server()
        return client, skills_dir

    async def test_install_returns_file_count(self, fake_home, reset_registry):
        client, skills_dir = await self._client(fake_home)
        try:
            resp = await client.post(
                "/api/skills/-/discover/install",
                json={"provider": "fakeprov", "skill_id": "fake-skill"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["key"] == "fakeprov/fake-skill"
            assert data["file_count"] == 2
            assert data["kind"] == "created"
            assert (skills_dir / "fakeprov" / "fake-skill" / "SKILL.md").exists()
            assert (skills_dir / "fakeprov" / "fake-skill" / "rules" / "extra.md").exists()
        finally:
            await client.close()

    async def test_install_non_object_body_is_400(self, fake_home, reset_registry):
        # Valid JSON like [] has no .get() — must be a 400, not a 500.
        client, _ = await self._client(fake_home)
        try:
            resp = await client.post("/api/skills/-/discover/install", json=[])
            assert resp.status == 400
        finally:
            await client.close()

    async def test_install_non_string_field_is_400(self, fake_home, reset_registry):
        # {"provider": 1} has no .strip() — must be a 400, not a 500.
        client, _ = await self._client(fake_home)
        try:
            resp = await client.post(
                "/api/skills/-/discover/install",
                json={"provider": 1, "skill_id": "x"},
            )
            assert resp.status == 400
        finally:
            await client.close()

    async def test_install_non_bool_overwrite_is_400(self, fake_home, reset_registry):
        # bool("false") is True — a destructive overwrite demands a real bool.
        client, _ = await self._client(fake_home)
        try:
            resp = await client.post(
                "/api/skills/-/discover/install",
                json={"provider": "fakeprov", "skill_id": "fake-skill", "overwrite": "false"},
            )
            assert resp.status == 400
        finally:
            await client.close()

    async def test_install_conflict_returns_409(self, fake_home, reset_registry):
        client, skills_dir = await self._client(fake_home)
        try:
            body = {"provider": "fakeprov", "skill_id": "fake-skill"}
            first = await client.post("/api/skills/-/discover/install", json=body)
            assert first.status == 200

            second = await client.post("/api/skills/-/discover/install", json=body)
            assert second.status == 409
            data = await second.json()
            assert data["code"] == "exists"
            assert data["key"] == "fakeprov/fake-skill"
        finally:
            await client.close()

    async def test_install_overwrite_replaces(self, fake_home, reset_registry):
        client, skills_dir = await self._client(fake_home)
        try:
            body = {"provider": "fakeprov", "skill_id": "fake-skill"}
            first = await client.post("/api/skills/-/discover/install", json=body)
            assert first.status == 200

            resp = await client.post(
                "/api/skills/-/discover/install", json={**body, "overwrite": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["kind"] == "updated"
            assert data["file_count"] == 2
        finally:
            await client.close()

    async def test_install_overwrite_replaces_symlinked_skill_dir(
        self, fake_home, reset_registry, tmp_path
    ):
        # Security regression: a pre-planted symlink at the skill dir must be
        # REMOVED, never followed — otherwise the bundle (incl. nested paths
        # whose not-yet-existing parents dodge the parent-symlink guard) would
        # be written outside the skills root at the symlink target.
        client, skills_dir = await self._client(fake_home)
        try:
            outside = tmp_path / "outside-target"
            outside.mkdir()
            provider_dir = skills_dir / "fakeprov"
            provider_dir.mkdir(parents=True, exist_ok=True)
            link = provider_dir / "fake-skill"
            link.symlink_to(outside, target_is_directory=True)

            resp = await client.post(
                "/api/skills/-/discover/install",
                json={
                    "provider": "fakeprov",
                    "skill_id": "fake-skill",
                    "overwrite": True,
                },
            )
            assert resp.status == 200
            # The symlink was replaced by a real directory...
            assert not link.is_symlink()
            assert (link / "SKILL.md").exists()
            # ...and NOTHING landed at the old symlink target.
            assert list(outside.iterdir()) == []
        finally:
            await client.close()


@pytest.mark.asyncio
class TestDiscoverPreview:
    async def test_preview_returns_content_and_files(self, fake_home, reset_registry):
        provider = FakeProvider()
        state, _ = _state_with_skills_loader(fake_home)
        app = _make_app(state, provider)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get(
                "/api/skills/-/discover/preview",
                params={"provider": "fakeprov", "id": "fake-skill"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["description"] == "A fake skill"
            assert data["name"] == "fake-skill"
            assert data["content"].startswith("---\nname: fake-skill")
            assert data["files"] == ["SKILL.md", "rules/extra.md"]
            assert data["file_count"] == 2
        finally:
            await client.close()


@pytest.mark.asyncio
class TestDiscoverSearch:
    async def test_search_includes_installs(self, fake_home, reset_registry):
        from kiro_crew.skill_providers.base import SkillSearchResult

        provider = FakeProvider(results=[
            SkillSearchResult(
                id="fake-skill",
                name="Fake Skill",
                description="",
                provider="fakeprov",
                installs=4321,
            )
        ])
        state, _ = _state_with_skills_loader(fake_home)
        app = _make_app(state, provider)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get(
                "/api/skills/-/discover", params={"q": "fake"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert len(data["results"]) == 1
            assert data["results"][0]["installs"] == 4321
        finally:
            await client.close()

    async def test_limit_query_is_clamped_to_at_least_one(self, fake_home, reset_registry):
        """`limit` was clamped only on the upper end (min(..., 50)); a
        <=0 value survived, and merged[:limit] then silently dropped the last
        result (limit=-1) or returned nothing (limit=0), and &limit=-1 hit the
        provider URL. The handler must now clamp to >=1."""
        seen = {}

        class RecordingProvider(FakeProvider):
            async def search(self, query, *, limit=20):
                seen["limit"] = limit
                return self._results

        state, _ = _state_with_skills_loader(fake_home)
        app = _make_app(state, RecordingProvider())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for raw in ("-1", "0"):
                seen.clear()
                resp = await client.get(
                    "/api/skills/-/discover", params={"q": "fake", "limit": raw}
                )
                assert resp.status == 200
                assert seen["limit"] >= 1, f"limit={raw!r} not clamped: {seen}"
            # upper bound still enforced
            seen.clear()
            await client.get(
                "/api/skills/-/discover", params={"q": "fake", "limit": "999"}
            )
            assert seen["limit"] == 50
        finally:
            await client.close()
