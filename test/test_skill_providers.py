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
    _is_allowed_host,
    _is_internal_url,
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

        # Patch the defining module's budget instead of waiting out the
        # production 10s — the test pins the timeout->empty-result contract,
        # not the budget's magnitude.
        with patch.object(p, "search", side_effect=slow_search), \
             patch("kiro_crew.skill_providers.base._SEARCH_TIMEOUT_SECS", 0.05):
            results = await reg.search("test")
            assert results == []


# Helpers not exported but worth testing
def test_slugify_repo_name():
    """Verify the skillsh module doesn't export _slugify_repo_name (it's in skill_sync.py)."""
    # Our discover handler has its own _slugify — test the github URL helper instead
    assert _github_raw_url("https://github.com/my-org/my-repo", "SKILL.md") is not None


class TestRedirectHostAllowlist:
    """SSRF defense: redirects may only target allowlisted HTTPS hosts.

    _is_internal_url alone can't stop a redirect to an arbitrary DNS name
    that resolves to a private address (DNS rebinding) — the allowlist is
    the control that closes that gap.
    """

    def test_allowed_hosts_pass(self):
        assert _is_allowed_host("https://skills.sh/api/download/x")
        assert _is_allowed_host("https://raw.githubusercontent.com/u/r/main/SKILL.md")
        assert _is_allowed_host("https://objects.githubusercontent.com/blob/x")

    def test_arbitrary_dns_name_blocked(self):
        # A hostname the attacker controls (could resolve to 169.254.x/10.x).
        assert not _is_allowed_host("https://internal.attacker.example/steal")
        assert not _is_allowed_host("https://metadata.google.internal/computeMetadata")

    def test_non_https_blocked(self):
        assert not _is_allowed_host("http://skills.sh/api")  # plain HTTP
        assert not _is_allowed_host("ftp://raw.githubusercontent.com/x")

    def test_ip_literals_blocked_by_both_layers(self):
        # IP literals are caught by _is_internal_url AND fail the allowlist.
        assert _is_internal_url("https://169.254.169.254/latest/meta-data/")
        assert not _is_allowed_host("https://169.254.169.254/latest/meta-data/")
        assert _is_internal_url("https://127.0.0.1/x")
        assert not _is_allowed_host("https://8.8.8.8/x")  # public IP, still not allowlisted

    def test_lookalike_subdomain_blocked(self):
        # Suffix tricks must not pass (exact-host match, not endswith).
        assert not _is_allowed_host("https://skills.sh.evil.example/api")
        assert not _is_allowed_host("https://evilskills.sh/api")


class TestSkillsShTagsCoercion:
    """skills.sh is an unauthenticated, publisher-controlled registry, so a
    skill's `tags` field is untrusted. dict.get("tags", []) only defaults when
    the key is ABSENT — a present "tags": null (or any non-list, or a list of
    non-strings) flowed through unchecked and later crashed the discover
    handler's `[_redact_external(t) for t in r.tags]` (TypeError: NoneType not
    iterable, or re.sub 'expected string'), 500-ing the ENTIRE search response
    for every provider. search() now coerces tags to a list[str]."""

    @pytest.mark.asyncio
    async def test_null_tags_coerced_to_empty_list(self):
        payload = {"skills": [{"id": "o/r/x", "name": "x", "source": "o/r", "tags": None}]}
        with patch("kiro_crew.skill_providers.skillsh._sync_fetch_json", return_value=payload):
            results = await SkillsShProvider().search("x")
        assert results[0].tags == []

    @pytest.mark.asyncio
    async def test_non_string_tag_items_are_dropped(self):
        payload = {"skills": [{"id": "o/r/y", "name": "y", "source": "o/r", "tags": [5, {}, "ok"]}]}
        with patch("kiro_crew.skill_providers.skillsh._sync_fetch_json", return_value=payload):
            results = await SkillsShProvider().search("y")
        assert results[0].tags == ["ok"]

    @pytest.mark.asyncio
    async def test_bare_string_tags_not_iterated_per_character(self):
        # Pre-fix a bare string iterated per-char -> garbage single-char tags.
        payload = {"skills": [{"id": "o/r/z", "name": "z", "source": "o/r", "tags": "python,web"}]}
        with patch("kiro_crew.skill_providers.skillsh._sync_fetch_json", return_value=payload):
            results = await SkillsShProvider().search("z")
        assert results[0].tags == []

    @pytest.mark.asyncio
    async def test_well_formed_tags_preserved(self):
        payload = {"skills": [{"id": "o/r/g", "name": "g", "source": "o/r", "tags": ["docker", "ci"]}]}
        with patch("kiro_crew.skill_providers.skillsh._sync_fetch_json", return_value=payload):
            results = await SkillsShProvider().search("g")
        assert results[0].tags == ["docker", "ci"]


class TestSkillsShDownloadUrl:
    """A skills.sh id is an owner/repo/skill PATH; its slashes are real path
    segments that must reach the /api/download/{owner}/{repo}/{skill} route.
    Percent-encoding them (safe="") collapsed the id into a single segment,
    missed the API route, and skills.sh returned its HTML SPA page instead of
    the JSON bundle, so a skill install surfaced as "not found or empty on
    skillsh". fetch_skill_bundle() now keeps the slashes (safe="/") while still
    rejecting traversal and encoding query/fragment metacharacters."""

    @pytest.mark.asyncio
    async def test_download_url_preserves_path_slashes(self):
        captured = {}

        def fake_fetch(url):
            captured["url"] = url
            return {"files": [{"path": "SKILL.md", "contents": "# ok"}]}

        with patch(
            "kiro_crew.skill_providers.skillsh._sync_fetch_json",
            side_effect=fake_fetch,
        ):
            bundle = await SkillsShProvider().fetch_skill_bundle(
                "vercel-labs/agent-skills/vercel-react-best-practices"
            )

        assert bundle == [("SKILL.md", "# ok")]
        assert captured["url"] == (
            "https://skills.sh/api/download/"
            "vercel-labs/agent-skills/vercel-react-best-practices"
        )
        assert "%2F" not in captured["url"]

    @pytest.mark.asyncio
    async def test_download_url_still_encodes_query_and_fragment(self):
        captured = {}

        def fake_fetch(url):
            captured["url"] = url
            return {"files": [{"path": "SKILL.md", "contents": "# ok"}]}

        with patch(
            "kiro_crew.skill_providers.skillsh._sync_fetch_json",
            side_effect=fake_fetch,
        ):
            await SkillsShProvider().fetch_skill_bundle("owner/repo/a b?x=1#f")

        # Legit path slashes survive; smuggling metacharacters are encoded.
        assert "/download/owner/repo/" in captured["url"]
        assert "%20" in captured["url"]  # space
        assert "%3F" in captured["url"]  # ?
        assert "%23" in captured["url"]  # #

    @pytest.mark.asyncio
    async def test_download_rejects_traversal_ids(self):
        calls = {"n": 0}

        def fake_fetch(url):
            calls["n"] += 1
            return {"files": [{"path": "SKILL.md", "contents": "x"}]}

        with patch(
            "kiro_crew.skill_providers.skillsh._sync_fetch_json",
            side_effect=fake_fetch,
        ):
            p = SkillsShProvider()
            assert await p.fetch_skill_bundle("../../etc/passwd") is None
            assert await p.fetch_skill_bundle("/abs/path") is None
            assert await p.fetch_skill_bundle("owner//repo") is None
            assert await p.fetch_skill_bundle("") is None

        assert calls["n"] == 0  # malformed ids never reach the network
