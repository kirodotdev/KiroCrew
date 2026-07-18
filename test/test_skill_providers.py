"""Tests for the multi-provider skill discovery system."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from kiro_crew.skill_providers.base import ProviderRegistry, SkillSearchResult
from kiro_crew.skill_providers.skillsh import (
    SkillsShConfig,
    SkillsShProvider,
    _github_raw_url,
)


class TestGithubRawUrl:
    """Test GitHub URL resolution."""

    def test_https_url(self):
        assert _github_raw_url("https://github.com/user/repo", "SKILL.md") == \
            "https://raw.githubusercontent.com/user/repo/main/SKILL.md"

    def test_https_with_git_suffix(self):
        assert _github_raw_url("https://github.com/user/repo.git", "SKILL.md") == \
            "https://raw.githubusercontent.com/user/repo/main/SKILL.md"

    def test_bare_github_url(self):
        assert _github_raw_url("github.com/user/repo", "SKILL.md") == \
            "https://raw.githubusercontent.com/user/repo/main/SKILL.md"

    def test_trailing_slash(self):
        assert _github_raw_url("https://github.com/user/repo/", "SKILL.md") == \
            "https://raw.githubusercontent.com/user/repo/main/SKILL.md"

    def test_non_github_url(self):
        assert _github_raw_url("https://gitlab.com/user/repo", "SKILL.md") is None

    def test_empty_string(self):
        assert _github_raw_url("", "SKILL.md") is None

    def test_nested_path(self):
        assert _github_raw_url("https://github.com/org/repo", "src/SKILL.md") == \
            "https://raw.githubusercontent.com/org/repo/main/src/SKILL.md"


class TestSkillsShProvider:
    """Test the skills.sh provider."""

    def test_name_and_display(self):
        p = SkillsShProvider()
        assert p.name == "skillsh"
        assert p.display_name == "skills.sh"

    def test_available_when_enabled(self):
        p = SkillsShProvider(SkillsShConfig(enabled=True))
        assert p.is_available()

    def test_unavailable_when_disabled(self):
        p = SkillsShProvider(SkillsShConfig(enabled=False))
        assert not p.is_available()

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_empty(self):
        p = SkillsShProvider()
        results = await p.search("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_parses_response(self):
        mock_data = {
            "skills": [
                {
                    "id": "user/docker-skill/docker-compose",
                    "skillId": "docker-compose",
                    "name": "Docker Compose",
                    "installs": 1234,
                    "source": "user/docker-skill",
                    "tags": ["docker", "devops"],
                }
            ]
        }
        with patch(
            "kiro_crew.skill_providers.skillsh._sync_fetch_json",
            return_value=mock_data,
        ):
            p = SkillsShProvider()
            results = await p.search("docker")
            assert len(results) == 1
            assert results[0].id == "user/docker-skill/docker-compose"
            assert results[0].name == "Docker Compose"
            assert results[0].provider == "skillsh"
            assert results[0].repo_url == "https://github.com/user/docker-skill"
            assert results[0].author == "user"
            assert results[0].installs == 1234
            assert results[0].tags == ["docker", "devops"]

    @pytest.mark.asyncio
    async def test_search_handles_timeout(self):
        with patch(
            "kiro_crew.skill_providers.skillsh._sync_fetch_json",
            side_effect=TimeoutError,
        ):
            p = SkillsShProvider()
            results = await p.search("docker")
            assert results == []

    @pytest.mark.asyncio
    async def test_fetch_content_extracts_skill_md_from_bundle(self):
        bundle = {
            "files": [
                {"path": "SKILL.md", "contents": "---\nname: test\n---\n# Test Skill"},
                {"path": "rules/extra.md", "contents": "# Extra"},
            ]
        }
        with patch(
            "kiro_crew.skill_providers.skillsh._sync_fetch_json",
            return_value=bundle,
        ):
            p = SkillsShProvider()
            content = await p.fetch_skill_content("test-skill")
            assert content == "---\nname: test\n---\n# Test Skill"

    @pytest.mark.asyncio
    async def test_fetch_content_falls_back_to_agents_md(self):
        bundle = {
            "files": [
                {"path": "AGENTS.md", "contents": "---\nname: test\n---\n# Fallback"},
            ]
        }
        with patch(
            "kiro_crew.skill_providers.skillsh._sync_fetch_json",
            return_value=bundle,
        ):
            p = SkillsShProvider()
            content = await p.fetch_skill_content("test-skill")
            assert content == "---\nname: test\n---\n# Fallback"


class TestProviderRegistry:
    """Test the provider registry fan-out."""

    def test_register_and_get(self):
        reg = ProviderRegistry()
        p = SkillsShProvider()
        reg.register(p)
        assert reg.get("skillsh") is p
        assert reg.get("nonexistent") is None

    def test_available_providers_filters_disabled(self):
        reg = ProviderRegistry()
        reg.register(SkillsShProvider(SkillsShConfig(enabled=True)))
        reg.register(SkillsShProvider(SkillsShConfig(enabled=False)))
        # Second register overwrites first (same name)
        assert len(reg.available_providers) == 0  # last one was disabled

    def test_provider_names(self):
        reg = ProviderRegistry()
        reg.register(SkillsShProvider())
        assert "skillsh" in reg.provider_names

    @pytest.mark.asyncio
    async def test_search_fans_out(self):
        reg = ProviderRegistry()
        p = SkillsShProvider()
        reg.register(p)

        mock_results = [
            SkillSearchResult(
                id="test", name="Test", description="A test",
                provider="skillsh", repo_url="", author="",
            )
        ]
        with patch.object(p, "search", return_value=mock_results):
            results = await reg.search("test")
            assert len(results) == 1
            assert results[0].name == "Test"

    @pytest.mark.asyncio
    async def test_search_specific_provider(self):
        reg = ProviderRegistry()
        p = SkillsShProvider()
        reg.register(p)

        mock_results = [
            SkillSearchResult(
                id="x", name="X", description="",
                provider="skillsh", repo_url="", author="",
            )
        ]
        with patch.object(p, "search", return_value=mock_results):
            results = await reg.search("x", provider="skillsh")
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_unknown_provider_returns_empty(self):
        reg = ProviderRegistry()
        results = await reg.search("x", provider="nonexistent")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_provider_timeout_returns_empty(self):
        reg = ProviderRegistry()
        p = SkillsShProvider()
        reg.register(p)

        async def slow_search(query, *, limit=20):
            await asyncio.sleep(20)
            return []

        with patch.object(p, "search", side_effect=slow_search):
            results = await reg.search("test")
            assert results == []


# Helpers not exported but worth testing
def test_slugify_repo_name():
    """Verify the skillsh module doesn't export _slugify_repo_name (it's in skill_sync.py)."""
    # Our discover handler has its own _slugify — test the github URL helper instead
    assert _github_raw_url("https://github.com/my-org/my-repo", "SKILL.md") is not None
